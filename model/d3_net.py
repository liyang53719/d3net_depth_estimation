import torch
import torch.nn as nn
from torch import cat
import torch.nn.functional as F
import torch.utils.model_zoo as model_zoo
from collections import OrderedDict
from torchvision import models
import re

from ipdb import set_trace as st

from conv_blocks import get_decoder_block, conv3x3, conv4x4, UpsampleBlock, BasicBlock
import networks.weight_initialization as w_init

__all__ = ['DenseNet', 'densenet121', 'densenet169', 'densenet201', 'densenet161']


model_urls = {
    'densenet121': 'https://download.pytorch.org/models/densenet121-a639ec97.pth',
    'densenet169': 'https://download.pytorch.org/models/densenet169-b2777c0a.pth',
    'densenet201': 'https://download.pytorch.org/models/densenet201-c1103571.pth',
    'densenet161': 'https://download.pytorch.org/models/densenet161-8d451a50.pth',
}


def denseUnet121(pretrained=False, d_block_type='basic', init_method='normal', version=1, **kwargs):
    r"""Densenet-121 model from
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """

    d_block = get_decoder_block(d_block_type)
    model = DenseUNet(num_init_features=64, growth_rate=32, block_config=(6, 12, 24, 16), d_block=d_block,
                          **kwargs)

    if pretrained:
        w_init.init_weights(model, init_method)
        # Get state dict from the actual model
        model_dict = model.state_dict()
        pretrained_dict = models.densenet121(pretrained=True).state_dict()
        # exclude_model_dict = ["features.conv0.weight"]
        model_shapes = [v.shape for k, v in model_dict.items()]
        exclude_model_dict = []
        exclude_model_dict = [k for k, v in pretrained_dict.items() if v.shape not in model_shapes]
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict and k not in exclude_model_dict}

        # added to pytorch 0.4
        pattern = re.compile(
            r'^(.*denselayer\d+\.(?:norm|relu|conv))\.((?:[12])\.(?:weight|bias|running_mean|running_var))$')
        # state_dict = model_zoo.load_url(model_urls['densenet121'])
        for key in list(pretrained_dict.keys()):
            res = pattern.match(key)
            if res:
                new_key = res.group(1) + res.group(2)
                pretrained_dict[new_key] = pretrained_dict[key]
                del pretrained_dict[key]

        model_dict.update(pretrained_dict)
        model.load_state_dict(model_dict)

    return model


class _DenseLayer(nn.Sequential):
    def __init__(self, num_input_features, growth_rate, bn_size, drop_rate):
        super(_DenseLayer, self).__init__()
        self.add_module('norm1', nn.BatchNorm2d(num_input_features)),
        self.add_module('relu1', nn.ReLU(inplace=True)),
        self.add_module('conv1', nn.Conv2d(num_input_features, bn_size * growth_rate,
                                            kernel_size=1, stride=1, bias=False)),
        self.add_module('norm2', nn.BatchNorm2d(bn_size * growth_rate)),
        self.add_module('relu2', nn.ReLU(inplace=True)),
        self.add_module('conv2', nn.Conv2d(bn_size * growth_rate, growth_rate,
                                            kernel_size=3, stride=1, padding=1, bias=False)),
        self.drop_rate = drop_rate

    def forward(self, x):
        new_features = super(_DenseLayer, self).forward(x)
        if self.drop_rate > 0:
            new_features = F.dropout(new_features, p=self.drop_rate, training=self.training)
        return torch.cat([x, new_features], 1)


class _DenseBlock(nn.Sequential):
    def __init__(self, num_layers, num_input_features, bn_size, growth_rate, drop_rate):
        super(_DenseBlock, self).__init__()
        for i in range(num_layers):
            layer = _DenseLayer(num_input_features + i * growth_rate, growth_rate, bn_size, drop_rate)
            self.add_module('denselayer%d' % (i + 1), layer)


class _Transition(nn.Sequential):
    def __init__(self, num_input_features, num_output_features):
        super(_Transition, self).__init__()
        self.add_module('norm', nn.BatchNorm2d(num_input_features))
        self.add_module('relu', nn.ReLU(inplace=True))
        self.add_module('conv', nn.Conv2d(num_input_features, num_output_features,
                                          kernel_size=1, stride=1, bias=False))
        # self.add_module('pool', nn.AvgPool2d(kernel_size=2, stride=2))


def center_crop(layer, max_height, max_width):
    #https://github.com/Lasagne/Lasagne/blob/master/lasagne/layers/merge.py#L162
    #Author does a center crop which crops both inputs (skip and upsample) to size of minimum dimension on both w/h
    batch_size, n_channels, layer_height, layer_width = layer.size()
    xy1 = (layer_width - max_width) // 2
    xy2 = (layer_height - max_height) // 2
    return layer[:, :, xy2:(xy2 + max_height), xy1:(xy1 + max_width)]


class _TransitionUp(nn.Sequential):
    def __init__(self, num_input_features, num_output_features):
        super(_TransitionUp, self).__init__()
        self.transition_upsample = nn.Sequential()
        self.transition_upsample.add_module('d_transition1', _Transition(num_input_features, num_input_features // 2))
        num_features = num_input_features // 2
        self.transition_upsample.add_module('upsample', UpsampleBlock(num_features, num_features))
        # center crop
        self.last_transition = nn.Sequential()
        self.last_transition.add_module('d_transition2', _Transition(num_input_features, num_output_features))

    def forward(self, x, skip):
        out = self.transition_upsample(x)
        print(out.size(2))
        out = center_crop(out, skip.size(2), skip.size(3))
        print(skip.size(2))
        out = torch.cat([out, skip], 1)
        out = self.last_transition(out)
        return out


class DenseUNet(nn.Module):
    """Densenet-BC model class, based on
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_
    Args:
        growth_rate (int) - how many filters to add each layer (`k` in paper)
        block_config (list of 4 ints) - how many layers in each pooling block
        num_init_features (int) - the number of filters to learn in the first convolution layer
        bn_size (int) - multiplicative factor for number of bottle neck layers
          (i.e. bn_size * k features in the bottleneck layer)
        drop_rate (float) - dropout rate after each dense layer
        num_classes (int) - number of classification classes
    """

    def __init__(self, d_block, input_nc=3, output_nc=1, growth_rate=32,
                 block_config=(6, 12, 24, 16), num_init_features=64, bn_size=4,
                 drop_rate=0, num_classes=1000, use_dropout=False, use_skips=True,
                 bilinear_trick=False, outputSize=[427, 571], use_semantics=False):

        super(DenseUNet, self).__init__()

        self.use_skips = use_skips
        self.bilinear_trick = bilinear_trick
        self.use_semantics = use_semantics

        if self.use_skips:
            ngf_mult = 2
        else:
            ngf_mult = 1

        self.relu_type = nn.LeakyReLU(0.2, inplace=True)     # nn.ReLU(inplace=True)

        # First convolution
        self.features = nn.Sequential(OrderedDict([
            ('conv0', nn.Conv2d(input_nc, num_init_features, kernel_size=7, stride=2, padding=3, bias=False)),
            ('norm0', nn.BatchNorm2d(num_init_features)),
            ('relu0', self.relu_type),
            # ('pool0', nn.MaxPool2d(kernel_size=3, stride=2, padding=1)),
            ('downconv0', nn.Conv2d(num_init_features, num_init_features, kernel_size=4, stride=2,
                                    padding=1, bias=False)),
            ('norm1', nn.BatchNorm2d(num_init_features)),
            ('relu1', self.relu_type)
        ]))

        # Each denseblock
        num_features = num_init_features
        for i, num_layers in enumerate(block_config):
            block = _DenseBlock(num_layers=num_layers,
                                num_input_features=num_features,
                                bn_size=bn_size, growth_rate=growth_rate, drop_rate=drop_rate)
            self.features.add_module('denseblock%d' % (i + 1), block)
            num_features = num_features + num_layers * growth_rate
            if i != len(block_config) - 1:
                trans = _Transition(num_input_features=num_features, num_output_features=num_features // 2)
                self.features.add_module('transition%d' % (i + 1), trans)
                self.features.add_module('transition%dpool' % (i + 1), nn.AvgPool2d(kernel_size=2, stride=2))
                num_features = num_features // 2

        # Final batch norm
        self.features.add_module('norm5', nn.BatchNorm2d(num_features))

        # Linear layer
        # self.classifier = nn.Linear(num_features, num_classes)

        # each decoder block
        self.decoder = nn.Sequential()
        if use_semantics:
            self.decoder_sem = nn.Sequential()
        for i in reversed(range(2, 6)):
            mult = 1 if i == 5 else ngf_mult
            dropout = use_dropout if i > 3 else False
            self.decoder.add_module('d_block{}'.format(i),
                                    self._make_decoder_layer(num_features * mult,
                                                             int(num_features / 2), block=d_block,
                                                             use_dropout=dropout))
            if use_semantics:
                self.decoder_sem.add_module('d_block{}'.format(i),
                                        self._make_decoder_layer(num_features * mult,
                                                                int(num_features / 2), block=d_block,
                                                                use_dropout=dropout))
            num_features = int(num_features / 2)

        self.decoder.add_module('d_block{}'.format(i - 1),
                                self._make_decoder_layer(num_features * mult,
                                                         num_features, block=d_block,
                                                         use_dropout=False))
        if use_semantics:
                self.decoder_sem.add_module('d_block{}'.format(i - 1),
                                        self._make_decoder_layer(num_features * mult,
                                                                num_features, block=d_block,
                                                                use_dropout=False))
        self.last_conv = conv3x3(num_features, output_nc)
        
        if use_semantics:
            self.last_conv_sem = conv3x3(num_features, num_classes)
            # self.upsample = nn.Upsample([480,], mode='bilinear')=
        
        if self.bilinear_trick:
            outputSize = tuple(reversed(outputSize)) if outputSize[0] > outputSize[1] else tuple(outputSize)
            self.upsample = nn.Sequential(OrderedDict([
                ('up_tranf', nn.Upsample(outputSize, mode='bilinear')),
                ('conv', conv3x3(output_nc, output_nc))]))

        self.Tanh = nn.Tanh()

    def _make_decoder_layer(self, inplanes, outplanes, block, use_dropout=True):
        layers = []
        layers.append(block(inplanes, outplanes, upsample=True, use_dropout=use_dropout))
        return nn.Sequential(*layers)

    def get_decoder_input(self, e_out, d_out):
        if self.use_skips:
            return cat((e_out, d_out), 1)
        else:
            return d_out

    def forward(self, x):
        # features = self.features(x)
        # input is ngf x 256 x 256
        out = self.features.conv0(x)
        out = self.features.norm0(out)
        out_conv1 = self.features.relu0(out)
        # input is ngf x 128 x 128
        out = self.features.downconv0(out_conv1)
        out = self.features.norm1(out)
        out = self.features.relu1(out)

        # input is ngf x 64 x 64
        out = self.features.denseblock1(out)
        # input is ngf * 4 x 64 x 64
        tb_denseblock1 = self.features.transition1(out)     # transition block
        # input is ngf * 2 x 64 x 64
        out = self.features.transition1pool(tb_denseblock1)
        # input is ngf * 2 x 32 x 32
        out = self.features.denseblock2(out)
        # input is ngf * 8 x 32 x 32
        tb_denseblock2 = self.features.transition2(out)
        # input is ngf * 4 x 32 x 32
        out = self.features.transition2pool(tb_denseblock2)
        # input is ngf * 4 x 16 x 16
        out = self.features.denseblock3(out)
        # input is ngf * 16 x 16 x 16
        tb_denseblock3 = self.features.transition3(out)
        # input is ngf * 16 x 16 x 16
        out = self.features.transition3pool(tb_denseblock3)
        # input is ngf * 8 x 8 x 8
        out = self.features.denseblock4(out)
        # input is ngf * 16 x 8 x 8
        out = self.features.norm5(out)
        out = self.relu_type(out)

        # Here comes the decoder
        # input is ngf * 16 x 8 x 8
        out = self.decoder.d_block5(out)
        # input is (ngf * 8) x 16 x 16
        out = self.decoder.d_block4(self.get_decoder_input(tb_denseblock3, out))
        # input is (ngf * 4) x 32 x 32
        out_d3 = self.decoder.d_block3(self.get_decoder_input(tb_denseblock2, out))
        # input is (ngf * 2) x 64 x 64
        out_reg_d2 = self.decoder.d_block2(self.get_decoder_input(tb_denseblock1, out_d3))
        # input is ngf x 128 x 128
        out_reg_d1 = self.decoder.d_block1(self.get_decoder_input(out_conv1, out_reg_d2))
        # input is ngf x 256 x 256
        out_reg_last = self.last_conv(out_reg_d1)

        if self.bilinear_trick:
            out = self.upsample(out)
        out_reg = self.Tanh(out_reg_last)

        if self.use_semantics:
             # input is (ngf * 2) x 64 x 64
            out_sem_d2 = self.decoder_sem.d_block2(self.get_decoder_input(tb_denseblock1, out_d3))
            # input is ngf x 128 x 128
            out_sem_d1 = self.decoder_sem.d_block1(self.get_decoder_input(out_conv1, out_sem_d2))
            # input is number_of_classes x 256 x 256 
            out_sem_last = self.last_conv_sem(out_sem_d1)
            # compare with image same size, apply bilinear upsample:
            # out_sem_last = self.upsample(out_sem_last)

        if self.use_semantics:
            return out_reg, out_sem_last

        return out_reg
