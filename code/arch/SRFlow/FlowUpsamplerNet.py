"""
20-Mai-2020
https://github.com/victorca25/BasicSR/blob/14aced7d1049a283761c145f3cf300a94c6ac4b9/codes/models/modules/architectures/SRFlow/FlowUpsamplerNet.py
"""
import numpy as np
import torch
from torch import nn as nn

# from models.modules.architectures.glow import flow, thops
from arch.glow import flow, thops
from arch.SRFlow.Split import Split2d
from arch.SRFlow.glow_arch import f_conv2d_bias
from arch.glow.Step import FlowStep

# from options.options import opt_get
import yaml

with open("config.yaml", "r") as ymlfile:
    cfg = yaml.safe_load(ymlfile)


class FlowUpsamplerNet(nn.Module):
    """Modified from Glow FlowNet"""

    def __init__(
        self,
        image_shape,
        hidden_channels,
        K,
        L=None,
        actnorm_scale=1.0,
        flow_permutation=None,
        flow_coupling="affine",
        LU_decomposed=False,
        opt=None,
    ):
        super().__init__()
        self.layers = nn.ModuleList()
        self.output_shapes = []
        # self.L = opt_get(opt, ['network_G', 'flow', 'L'])
        self.L = cfg["network_G"]["flow"]["L"]
        # self.K = opt_get(opt, ['network_G', 'flow', 'K'])
        self.K = cfg["network_G"]["flow"]["K"]
        if isinstance(self.K, int):
            self.K = [
                K
                for K in [
                    K,
                ]
                * (self.L + 1)
            ]

        self.opt = opt
        H, W, self.C = image_shape
        self.check_image_shape()

        if cfg["scale"] == 16:
            self.levelToName = {
                0: "fea_up16",
                1: "fea_up8",
                2: "fea_up4",
                3: "fea_up2",
                4: "fea_up1",
            }

        if cfg["scale"] == 8:
            self.levelToName = {
                0: "fea_up8",
                1: "fea_up4",
                2: "fea_up2",
                3: "fea_up1",
                4: "fea_up0",
            }

        elif cfg["scale"] == 4:
            self.levelToName = {
                0: "fea_up4",
                1: "fea_up2",
                2: "fea_up1",
                3: "fea_up0",
                4: "fea_up-1",
            }

        elif cfg["scale"] == 1:
            self.levelToName = {0: "fea_up1", 1: "fea_up1", 2: "fea_up-1"}

        affineInCh = self.get_affineInCh()
        flow_permutation = self.get_flow_permutation(flow_permutation)

        # normOpt = opt_get(opt, ['network_G', 'flow', 'norm'])
        # normOpt = cfg['network_G']['flow']['norm'] # empty
        normOpt = None

        conditional_channels = {}
        n_rrdb = self.get_n_rrdb_channels()
        # n_bypass_channels = opt_get(opt, ['network_G', 'flow', 'levelConditional', 'n_channels'])
        # n_bypass_channels = cfg['network_G']['flow']['levelConditional']['n_channels']] # empty
        n_bypass_channels = None
        conditional_channels[0] = n_rrdb
        for level in range(1, self.L + 1):
            # Level 1 gets conditionals from 2, 3, 4 => L - level
            # Level 2 gets conditionals from 3, 4
            # Level 3 gets conditionals from 4
            # Level 4 gets conditionals from None
            n_bypass = (
                0 if n_bypass_channels is None else (self.L - level) * n_bypass_channels
            )
            conditional_channels[level] = n_rrdb + n_bypass

        # Upsampler
        for level in range(1, self.L + 1):
            # 1. Squeeze
            H, W = self.arch_squeeze(H, W)

            # 2. K FlowStep
            self.arch_additionalFlowAffine(
                H, LU_decomposed, W, actnorm_scale, hidden_channels
            )
            self.arch_FlowStep(
                H,
                self.K[level],
                LU_decomposed,
                W,
                actnorm_scale,
                affineInCh,
                flow_coupling,
                flow_permutation,
                hidden_channels,
                normOpt,
                n_conditinal_channels=conditional_channels[level],
            )
            # Split
            self.arch_split(H, W, level, self.L)

        # if opt_get(opt, ['network_G', 'flow', 'split', 'enable']):
        if cfg["network_G"]["flow"]["split"]["enable"] == True:
            self.f = f_conv2d_bias(affineInCh, 2 * 3 * 64 // 2 // 2)
        else:
            self.f = f_conv2d_bias(affineInCh, 2 * 3 * 64)

        self.H = H
        self.W = W
        self.scaleH = 160 / H
        self.scaleW = 160 / W

    def get_n_rrdb_channels(self):
        # blocks = opt_get(opt, ['network_G', 'flow', 'stackRRDB', 'blocks'])
        blocks = cfg["network_G"]["flow"]["stackRRDB"]["blocks"]
        n_rrdb = 64 if blocks is None else (len(blocks) + 1) * 64
        return n_rrdb

    def arch_FlowStep(
        self,
        H,
        K,
        LU_decomposed,
        W,
        actnorm_scale,
        affineInCh,
        flow_coupling,
        flow_permutation,
        hidden_channels,
        normOpt,
        n_conditinal_channels=None,
    ):
        condAff = self.get_condAffSetting()
        if condAff is not None:
            condAff["in_channels_rrdb"] = n_conditinal_channels

        for k in range(K):
            # position_name = get_position_name(H, self.opt['scale'])
            position_name = get_position_name(H, cfg["scale"])
            if normOpt:
                normOpt["position"] = position_name

            self.layers.append(
                FlowStep(
                    in_channels=self.C,
                    hidden_channels=hidden_channels,
                    actnorm_scale=actnorm_scale,
                    flow_permutation=flow_permutation,
                    flow_coupling=flow_coupling,
                    acOpt=condAff,
                    position=position_name,
                    LU_decomposed=LU_decomposed,
                    idx=k,
                    normOpt=normOpt,
                )
            )
            self.output_shapes.append([-1, self.C, H, W])

    def get_condAffSetting(self):
        # condAff = opt_get(opt, ['network_G', 'flow', 'condAff']) or None
        condAff = None
        # condAff = opt_get(opt, ['network_G', 'flow', 'condFtAffine']) or condAff
        return condAff

    def arch_split(self, H, W, L, levels):
        # correct_splits = opt_get(opt, ['network_G', 'flow', 'split', 'correct_splits'], False)
        correct_splits = False
        correction = 0 if correct_splits else 1
        # if opt_get(opt, ['network_G', 'flow', 'split', 'enable']) and L < levels - correction:
        if cfg["network_G"]["flow"]["split"]["enable"] and L < levels - correction:
            # logs_eps = opt_get(opt, ['network_G', 'flow', 'split', 'logs_eps']) or 0
            logs_eps = 0
            # consume_ratio = opt_get(opt, ['network_G', 'flow', 'split', 'consume_ratio']) or 0.5
            consume_ratio = 0.5
            # position_name = get_position_name(H, self.opt['scale'])
            position_name = get_position_name(H, cfg["scale"])
            # position = position_name if opt_get(opt, ['network_G', 'flow', 'split', 'conditional']) else None
            position = position_name if None else None
            # cond_channels = opt_get(opt, ['network_G', 'flow', 'split', 'cond_channels'])
            cond_channels = None
            cond_channels = 0 if cond_channels is None else cond_channels

            # t = opt_get(opt, ['network_G', 'flow', 'split', 'type'], 'Split2d')
            t = "Split2d"

            if t == "Split2d":
                split = Split2d(
                    num_channels=self.C,
                    logs_eps=logs_eps,
                    position=position,
                    cond_channels=cond_channels,
                    consume_ratio=consume_ratio,
                )
            self.layers.append(split)
            self.output_shapes.append([-1, split.num_channels_pass, H, W])
            self.C = split.num_channels_pass

    def arch_additionalFlowAffine(
        self, H, LU_decomposed, W, actnorm_scale, hidden_channels
    ):
        # if 'additionalFlowNoAffine' in opt['network_G']['flow']:
        if "additionalFlowNoAffine" in cfg["network_G"]["flow"]:
            # n_additionalFlowNoAffine = int(opt['network_G']['flow']['additionalFlowNoAffine'])
            n_additionalFlowNoAffine = int(
                cfg["network_G"]["flow"]["additionalFlowNoAffine"]
            )
            for _ in range(n_additionalFlowNoAffine):
                self.layers.append(
                    FlowStep(
                        in_channels=self.C,
                        hidden_channels=hidden_channels,
                        actnorm_scale=actnorm_scale,
                        flow_permutation="invconv",
                        flow_coupling="noCoupling",
                        LU_decomposed=LU_decomposed,
                    )
                )
                self.output_shapes.append([-1, self.C, H, W])

    def arch_squeeze(self, H, W):
        self.C, H, W = self.C * 4, H // 2, W // 2
        self.layers.append(flow.SqueezeLayer(factor=2))
        self.output_shapes.append([-1, self.C, H, W])
        return H, W

    def get_flow_permutation(self, flow_permutation):
        # flow_permutation = opt['network_G']['flow'].get('flow_permutation', 'invconv')
        flow_permutation = "invconv"
        return flow_permutation

    def get_affineInCh(self):
        # affineInCh = opt_get(self.opt, ['network_G', 'flow', 'stackRRDB', 'blocks']) or []
        affineInCh = cfg["network_G"]["flow"]["stackRRDB"]["blocks"]
        affineInCh = (len(affineInCh) + 1) * 64
        return affineInCh

    def check_image_shape(self):
        assert self.C == 1 or self.C == 3, (
            "image_shape should be HWC, like (64, 64, 3)" "self.C == 1 or self.C == 3"
        )

    def forward(
        self,
        gt=None,
        rrdbResults=None,
        z=None,
        epses=None,
        logdet=0.0,
        reverse=False,
        eps_std=None,
        y_onehot=None,
    ):
        if reverse:
            epses_copy = [eps for eps in epses] if isinstance(epses, list) else epses

            sr, logdet = self.decode(
                rrdbResults,
                z,
                eps_std,
                epses=epses_copy,
                logdet=logdet,
                y_onehot=y_onehot,
            )
            return sr, logdet
        else:
            assert gt is not None
            assert rrdbResults is not None
            z, logdet = self.encode(
                gt, rrdbResults, logdet=logdet, epses=epses, y_onehot=y_onehot
            )

            return z, logdet

    def encode(self, gt, rrdbResults, logdet=0.0, epses=None, y_onehot=None):
        fl_fea = gt
        reverse = False
        level_conditionals = {}
        bypasses = {}

        # L = opt_get(self.opt, ['network_G', 'flow', 'L'])
        L = cfg["network_G"]["flow"]["L"]

        for level in range(1, L + 1):
            bypasses[level] = torch.nn.functional.interpolate(
                gt,
                scale_factor=2**-level,
                mode="bilinear",
                align_corners=False,
                recompute_scale_factor=False,
            )

        for layer, shape in zip(self.layers, self.output_shapes):
            size = shape[2]
            level = int(np.log(160 / size) / np.log(2))

            if level > 0 and level not in level_conditionals.keys():
                level_conditionals[level] = rrdbResults[self.levelToName[level]]

            level_conditionals[level] = rrdbResults[self.levelToName[level]]

            if isinstance(layer, FlowStep):
                fl_fea, logdet = layer(
                    fl_fea,
                    logdet,
                    reverse=reverse,
                    rrdbResults=level_conditionals[level],
                )
            elif isinstance(layer, Split2d):
                fl_fea, logdet = self.forward_split2d(
                    epses,
                    fl_fea,
                    layer,
                    logdet,
                    reverse,
                    level_conditionals[level],
                    y_onehot=y_onehot,
                )
            else:
                fl_fea, logdet = layer(fl_fea, logdet, reverse=reverse)

        z = fl_fea

        if not isinstance(epses, list):
            return z, logdet

        epses.append(z)
        return epses, logdet

    def forward_preFlow(self, fl_fea, logdet, reverse):
        if hasattr(self, "preFlow"):
            for l in self.preFlow:
                fl_fea, logdet = l(fl_fea, logdet, reverse=reverse)
        return fl_fea, logdet

    def forward_split2d(
        self, epses, fl_fea, layer, logdet, reverse, rrdbResults, y_onehot=None
    ):
        ft = None if layer.position is None else rrdbResults[layer.position]
        fl_fea, logdet, eps = layer(
            fl_fea, logdet, reverse=reverse, eps=epses, ft=ft, y_onehot=y_onehot
        )

        if isinstance(epses, list):
            epses.append(eps)
        return fl_fea, logdet

    def decode(
        self, rrdbResults, z, eps_std=None, epses=None, logdet=0.0, y_onehot=None
    ):
        z = epses.pop() if isinstance(epses, list) else z

        fl_fea = z
        # debug.imwrite("fl_fea", fl_fea)
        bypasses = {}
        level_conditionals = {}

        # opt_get(self.opt, ['network_G', 'flow', 'levelConditional', 'conditional'])
        if not None == True:
            for level in range(self.L + 1):
                level_conditionals[level] = rrdbResults[self.levelToName[level]]

        for layer, shape in zip(reversed(self.layers), reversed(self.output_shapes)):
            size = shape[2]
            level = int(np.log(160 / size) / np.log(2))
            # size = fl_fea.shape[2]
            # level = int(np.log(160 / size) / np.log(2))

            if isinstance(layer, Split2d):
                fl_fea, logdet = self.forward_split2d_reverse(
                    eps_std,
                    epses,
                    fl_fea,
                    layer,
                    rrdbResults[self.levelToName[level]],
                    logdet=logdet,
                    y_onehot=y_onehot,
                )
            elif isinstance(layer, FlowStep):
                fl_fea, logdet = layer(
                    fl_fea,
                    logdet=logdet,
                    reverse=True,
                    rrdbResults=level_conditionals[level],
                )
            else:
                fl_fea, logdet = layer(fl_fea, logdet=logdet, reverse=True)

        sr = fl_fea

        assert sr.shape[1] == 3
        return sr, logdet

    def forward_split2d_reverse(
        self, eps_std, epses, fl_fea, layer, rrdbResults, logdet, y_onehot=None
    ):
        ft = None if layer.position is None else rrdbResults[layer.position]
        fl_fea, logdet = layer(
            fl_fea,
            logdet=logdet,
            reverse=True,
            eps=epses.pop() if isinstance(epses, list) else None,
            eps_std=eps_std,
            ft=ft,
            y_onehot=y_onehot,
        )
        return fl_fea, logdet


def get_position_name(H, scale):
    downscale_factor = 160 // H
    position_name = "fea_up{}".format(scale / downscale_factor)
    return position_name
