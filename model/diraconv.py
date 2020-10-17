import torch
from torch import nn
import torch.nn.functional as F
from torch.nn.init import dirac_
import pdb

def normalize(w):
    """Normalizes weight tensor over full filter."""
    return F.normalize(w.view(w.shape[0], -1)).view_as(w)


class DiracConv(nn.Module):

    def init_params(self, out_channels):
        self.alpha = nn.Parameter(torch.Tensor(out_channels).fill_(1))
        self.beta = nn.Parameter(torch.Tensor(out_channels).fill_(0.1))
        self.register_buffer('delta', dirac_(self.weight.data.clone()))
        assert self.delta.shape == self.weight.shape
        self.v = (-1,) + (1,) * (self.weight.dim() - 1)

    def transform_weight(self):

        return self.alpha.view(*self.v) * self.delta + self.beta.view(*self.v) * self.weight

class DiracConv2d(nn.Conv2d, DiracConv):
    """Dirac parametrized convolutional layer.
    Works the same way as `nn.Conv2d`, but has additional weight parametrizatoin:
        :math:`\alpha\delta + \beta W`,
    where:
        :math:`\alpha` and :math:`\beta` are learnable scalars,
        :math:`\delta` is such a tensor so that `F.conv2d(x, delta) = x`, ie
            Kroneker delta
        `W` is weight tensor
    It is user's responsibility to set correcting padding. Only stride=1 supported.
    """

    def __init__(self, in_channels, out_channels, kernel_size, stride, padding=0, dilation=1, bias=True):
        super().__init__(in_channels, out_channels, kernel_size, stride=stride, padding=padding, dilation=dilation, bias=bias)
        self.init_params(out_channels)

    def forward(self, input):
        return F.conv2d(input, self.transform_weight(), self.bias, self.stride, self.padding, self.dilation)
    
    def forward_skip(self, input):
        return F.conv2d(input, self.alpha.view(*self.v) * self.delta, self.bias, self.stride, self.padding, self.dilation)
