import torch
from torch.optim.optimizer import Optimizer
from .nmf import _proj_func, _get_norm


eps = 1e-8


class BetaMu(Optimizer):
    def __init__(self, params, beta=1, l1_reg=0, l2_reg=0, sparsity=None, orthogonal=None):
        if not 0.0 <= l1_reg:
            raise ValueError("Invalid l1_reg value: {}".format(l1_reg))
        if not 0.0 <= l2_reg:
            raise ValueError("Invalid l2_reg value: {}".format(l2_reg))
        if sparsity is not None and not 0.0 < sparsity < 1.:
            raise ValueError("Invalid sparsity value: {}".format(sparsity))
        defaults = dict(beta=beta, l1_reg=l1_reg, l2_reg=l2_reg, orthogonal=orthogonal)
        super(BetaMu, self).__init__(params, defaults)

    @torch.no_grad()
    def step(self, closure):
        """Performs a single optimization step.

        Arguments:
            closure (callable): A closure that reevaluates the model
                and returns the target and predict Tensor.
        """

        # Make sure the closure is always called with grad enabled
        closure = torch.enable_grad()(closure)

        status_cache = dict()

        for group in self.param_groups:
            for p in group['params']:
                status_cache[id(p)] = p.requires_grad
                p.requires_grad = False

        for group in self.param_groups:
            beta = group['beta']
            l1_reg = group['l1_reg']
            l2_reg = group['l2_reg']
            ortho = group['sparsity']

            if beta < 1:
                gamma = 1 / (2 - beta)
            elif beta > 2:
                gamma = 1 / (beta - 1)
            else:
                gamma = 1

            for p in group['params']:
                if not status_cache[id(p)]:
                    continue
                p.requires_grad = True

                V, WH = closure()
                if not WH.requires_grad:
                    p.requires_grad = False
                    continue

                if beta == 2:
                    output_neg = V
                    output_pos = WH
                elif beta == 1:
                    output_neg = V / WH
                    output_pos = torch.ones_like(WH)
                elif beta == 0:
                    output_neg = V / (WH * WH)
                    output_pos = 1 / WH
                else:
                    output_neg = WH.pow(beta - 2) * V
                    output_pos = WH.pow(beta - 1)
                # first backward
                WH.backward(output_neg, retain_graph=True)
                neg = torch.clone(p.grad).detach()
                p.grad.zero_()
                WH.backward(output_pos)
                pos = torch.clone(p.grad).detach()
                p.grad.add_(-neg)

                if l1_reg > 0:
                    pos.add_(l1_reg)
                if l2_reg > 0:
                    pos.add_(p, alpha=l2_reg)

                if ortho > 0:
                    pos.add_(p.sum(1, keepdims=True) - p, alpha=ortho)

                pos.add_(eps)
                neg.add_(eps)
                multiplier = neg.div_(pos)
                if gamma != 1:
                    multiplier.pow_(gamma)

                p.mul_(multiplier)
                p.requires_grad = False

        for group in self.param_groups:
            for p in group['params']:
                p.requires_grad = status_cache[id(p)]

        return None


class SparsityProj(Optimizer):
    def __init__(self, params, sparsity, axis=1, max_iter=10):
        if not 0.0 < sparsity < 1.:
            raise ValueError("Invalid sparsity value: {}".format(sparsity))
        defaults = dict(sparsity=sparsity, lr=1, axis=axis, max_iter=max_iter)
        super(SparsityProj, self).__init__(params, defaults)

    @torch.no_grad()
    def step(self, closure):
        loss = None

        for group in self.param_groups:
            sparsity = group['sparsity']
            lr = group['lr']
            axis = group['axis']
            max_iter = group['max_iter']

            with torch.enable_grad():
                init_loss = closure()
                init_loss.backward()

            params = [p for p in group['params'] if p.grad is not None]

            for i in range(max_iter):
                for p in params:
                    norms = _get_norm(p, axis)
                    p.add_(p.grad, alpha=-lr)
                    dim = p.numel() // p.shape[axis]
                    L1 = dim ** 0.5 * (1 - sparsity) + sparsity
                    for j in range(p.shape[axis]):
                        slicer = (slice(None),) * axis + (j,)
                        p[slicer] = _proj_func(p[slicer], L1 * norms[j], norms[j] ** 2)

                loss = closure()
                if loss <= init_loss:
                    break

                for p in params:
                    p.add_(p.grad, alpha=lr)
                lr *= 0.5

            lr *= 1.2

            group['lr'] = lr
        return loss