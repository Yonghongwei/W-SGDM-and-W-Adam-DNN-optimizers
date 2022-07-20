import math
import torch.distributed as dist
import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.functional import pad


class WAdam(optim.Optimizer):
    def __init__(self,
                 model,
                 lr=0.001,
                 betas=(0.9, 0.999),
                 eps=1e-8,
                 weight_decouple = True,
                 amsgrad=False,
                 stat_decay=0.95,
                 dampening=0.001,
                 weight_decay=0,
                 Txx=50,
                 Tsvd=500,
                 single_gpu=True,
                 svd=True,
                 known_modules={'Linear', 'Conv2d'}):
        if lr < 0.0:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if weight_decay < 0.0:
            raise ValueError("Invalid weight_decay value: {}".format(weight_decay))
        defaults = dict(lr=lr, betas=betas, eps=eps,amsgrad=amsgrad,
                        weight_decay=weight_decay)
        super(WAdam, self).__init__(model.parameters(), defaults)
        self.XXHandler = ComputeXX()
        self.known_modules = known_modules
        self.modules = []
        self.model = model
        self.dampening=dampening
        self.m_xx = {}
        self.T={}
        self.stat_decay = stat_decay
        self.Txx = Txx
        self.Tsvd = Tsvd
        self.steps = 0
        self._prepare_model()
        self.single_gpu = single_gpu
        self.svd=svd
        self.weight_decouple=weight_decouple
        if not self.single_gpu:
           self.size=dist.get_world_size()


    def _save_input(self, module, input):
        if torch.is_grad_enabled() and self.steps % self.Txx == 0:
            xx = self.XXHandler(input[0].data, module)
            if self.steps == 0:
                self.m_xx[module] = torch.diag(xx.new(xx.size(0)).fill_(0))
            self.m_xx[module]+=(xx-self.m_xx[module])*(1 - self.stat_decay)

    def _prepare_model(self):
        for module in self.model.modules():
            classname = module.__class__.__name__
            if classname in self.known_modules:
                self.modules.append(module)
                module.register_forward_pre_hook(self._save_input)

    def _update_T(self, m):
        """Do eigen decomposition for computing inverse of the ~ fisher.
        :param m: The layer
        :return: no returns.
        """
        if self.svd:
           d_x, Q_x = torch.symeig(self.m_xx[m], eigenvectors=True)
           dampening=d_x.max()*self.dampening           
           d_x=1/(d_x+dampening)**(0.5) 
           self.T[m]= ( Q_x*(d_x.unsqueeze(0))) @ Q_x.t()           
        else:
           max_ev_xx=max_eignvalue(self.m_xx[m])
           dampening=max_ev_xx**self.dampening 
           self.T[m]=isqrt_newton_schulz(self.m_xx[m]+dampening*torch.eye(self.m_xx[m].size(0)).to(self.m_xx[m]))
         
    @staticmethod
    def _get_matrix_form_grad(m, classname):
        """
        :param m: the layer
        :param classname: the class name of the layer
        :return: a matrix form of the gradient. it should be a [output_dim, input_dim] matrix.
        """
        if classname == 'Conv2d':
            p_grad_mat = m.weight.grad.data.view(m.weight.grad.data.size(0), -1)  # n_filters * (in_c * kw * kh)
        else:
            p_grad_mat = m.weight.grad.data
        if m.bias is not None:
            p_grad_mat = torch.cat([p_grad_mat, m.bias.grad.data.view(-1, 1)], 1)
        return p_grad_mat

    def _get_modified_grad(self, m, p_grad_mat):
        """
        :param m:  the layer
        :param p_grad_mat: the gradients in matrix form
        :return: a list of gradients w.r.t to the parameters in `m`
        """
        v =p_grad_mat @ self.T[m]
        v=v*(p_grad_mat.norm()/(v.norm()+1e-12))
        if m.bias is not None:
            v = [v[:, :-1], v[:, -1:]]
            v[0] = v[0].view(m.weight.grad.data.size())
            v[1] = v[1].view(m.bias.grad.data.size())
        else:
            v = [v.view(m.weight.grad.data.size())]
        return v

    def _update_grad(self,update_T=True):
        for m in self.modules:
           classname = m.__class__.__name__
           if    (classname== 'Conv2d' and m.groups==1) or classname== 'Linear' :              
              if self.steps % self.Tsvd == 0 and update_T==True:
                  self._update_T(m)
              if m.weight.grad is not None:
                p_grad_mat = self._get_matrix_form_grad(m, classname)
                v = self._get_modified_grad(m, p_grad_mat)
                m.weight.grad.data.copy_(v[0])
                if m.bias is not None:
                   m.bias.grad.data.copy_(v[1])

    def allreduce_factors(self):
        if self.size == 1:
            return
        for m in self.model.modules():
            classname = m.__class__.__name__
            if classname in self.known_modules:
                dist.all_reduce(self.m_xx[m])
                self.m_xx[m] /= self.size

    def _step(self, closure=None):
        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                # Perform optimization step
                grad = p.grad
                if grad.is_sparse:
                    raise RuntimeError('AdamW does not support sparse gradients')
                amsgrad = group['amsgrad']
                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    state['step'] = 0
                    # Exponential moving average of gradient values
                    state['exp_avg'] = torch.zeros_like(p, memory_format=torch.preserve_format)
                    # Exponential moving average of squared gradient values
                    state['exp_avg_sq'] = torch.zeros_like(p, memory_format=torch.preserve_format)
                    if amsgrad:
                        # Maintains max of all exp. moving avg. of sq. grad. values
                        state['max_exp_avg_sq'] = torch.zeros_like(p, memory_format=torch.preserve_format)
                exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']
                if amsgrad:
                    max_exp_avg_sq = state['max_exp_avg_sq']
                beta1, beta2 = group['betas']

                if group['weight_decay'] != 0 and self.weight_decouple==False:
                    grad = grad.add(p.data, alpha=group['weight_decay'])

                state['step'] += 1
                bias_correction1 = 1 - beta1 ** state['step']
                bias_correction2 = 1 - beta2 ** state['step']

                # Decay the first and second moment running average coefficient
                exp_avg.mul_(beta1).add_(grad, alpha=1 - beta1)
                exp_avg_sq.mul_(beta2).addcmul_(grad, grad, value=1 - beta2)
                if amsgrad:
                    # Maintains the maximum of all 2nd moment running avg. till now
                    torch.max(max_exp_avg_sq, exp_avg_sq, out=max_exp_avg_sq)
                    # Use the max. for normalizing running avg. of gradient
                    denom = (max_exp_avg_sq.sqrt() / math.sqrt(bias_correction2)).add_(group['eps'])
                else:
                    denom = (exp_avg_sq.sqrt() / math.sqrt(bias_correction2)).add_(group['eps'])
                step_size = group['lr'] / bias_correction1*(1-self.stat_decay**(int(self.steps/self.Txx)+1))

                G_grad=(exp_avg/denom)
                if group['weight_decay'] != 0 and self.weight_decouple==True:
                    G_grad = G_grad.add(p.data, alpha=group['weight_decay'])

                p.data.add_( G_grad, alpha=-step_size)
        return loss

    def step(self, closure=None):
        if self.steps % self.Tsvd  == 0 and not self.single_gpu:
            self.allreduce_factors()
        self._update_grad()
        self._step(closure)
        self.steps += 1






class ComputeXX:

    @classmethod
    def compute_XX(cls, x, layer):
        return cls.__call__(x, layer)

    @classmethod
    def __call__(cls, x, layer):
        if isinstance(layer, nn.Linear):
            xx = cls.linear(x, layer)
        elif isinstance(layer, nn.Conv2d):
            xx = cls.conv2d(x, layer)
        else:
            xx = None
        return xx

    @staticmethod
    def conv2d(x, layer):
        """
        input: Cout Cin k k
        return:
        xx: Cin*k*k Cin*k*k
        """
        x = _extract_patches(x, layer.kernel_size, layer.stride, layer.padding)
        if layer.bias is not None:
            x = torch.cat([x, x.new(x.size(0), 1).fill_(1)], 1)  
        xx=x.t() @ (x *(1/ (x.size(0))))
        return xx

    @staticmethod
    def linear(x, layer):
        if layer.bias is not None:
            #print(x.size(),'x')
            if x.dim()==2:
              x = torch.cat([x, x.new(x.size(0), 1).fill_(1)], 1)
            if x.dim()>2:
               x=x.view(-1,x.size(-1))           
               x = torch.cat([x, x.new(x.size(0), 1).fill_(1)], 1)             
        xx=x.t() @ (x *(1/ (x.size(0))))
        return xx

def _extract_patches(x, kernel_size, stride, padding):
    if padding[0] + padding[1] > 0:
        x = F.pad(x, (padding[1], padding[1], padding[0],padding[0])).data  # Actually check dims
    x = F.unfold(x,kernel_size=kernel_size,padding=0, stride=stride).permute(0,2,1)
    x=x.contiguous().view(x.size(0)*x.size(1),x.size(2))
    return x



def isqrt_newton_schulz(A, numIters=10):
    dim = A.shape[0]
    normA=A.trace()
    Y = A.div(normA)
    I = torch.eye(dim,dtype=A.dtype,device=A.device)
    Z = torch.eye(dim,dtype=A.dtype,device=A.device)

    for i in range(numIters):
        T = 0.5*(3.0*I - Z@Y)
        Y = Y@T
        Z = T@Z
    #A_sqrt = Y*torch.sqrt(normA)
    A_isqrt = Z / torch.sqrt(normA)
    return A_isqrt
    
    
def max_eignvalue(A, numIters=10):    
    v=torch.ones(A.size(0),1).to(A)
    for i in range(numIters):
        u=(A*v).sum(dim=0,keepdim=True)  
        u=u*(1/u.norm())     
        v=(A*u).sum(dim=1,keepdim=True)
        max_ev=v.norm()      
    return max_ev    
    

#if __name__ == '__main__':
