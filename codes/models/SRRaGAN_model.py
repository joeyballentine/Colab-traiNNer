
from __future__ import absolute_import

import os
import logging
from collections import OrderedDict

import torch
import torch.nn as nn

import models.networks as networks
from .base_model import BaseModel

logger = logging.getLogger('base')

from . import losses
from . import optimizers
from . import schedulers

from dataops.batchaug import BatchAug
from dataops.filters import FilterHigh, FilterLow #, FilterX


class SRRaGANModel(BaseModel):
    def __init__(self, opt):
        super(SRRaGANModel, self).__init__(opt)
        train_opt = opt['train']

        # set if data should be normalized (-1,1) or not (0,1)
        if self.is_train:
            if opt['datasets']['train']['znorm']:
                z_norm = opt['datasets']['train']['znorm']
            else:
                z_norm = False
        
        # define networks and load pretrained models
        self.netG = networks.define_G(opt).to(self.device)  # G
        if self.is_train:
            self.netG.train()
            if train_opt['gan_weight']:
                self.netD = networks.define_D(opt).to(self.device)  # D
                self.netD.train()
        self.load()  # load G and D if needed

        # define losses, optimizer and scheduler
        if self.is_train:
            """
            Setup network cap
            """
            # define if the generator will have a final capping mechanism in the output
            self.outm = train_opt['finalcap'] if train_opt['finalcap'] else None

            """
            Setup batch augmentations
            """
            self.mixup = train_opt['mixup'] if train_opt['mixup'] else None
            if self.mixup: 
                #TODO: cutblur and cutout need model to be modified so LR and HR have the same dimensions (1x)
                self.mixopts = train_opt['mixopts'] if train_opt['mixopts'] else ["blend", "rgb", "mixup", "cutmix", "cutmixup"] #, "cutout", "cutblur"]
                self.mixprob = train_opt['mixprob'] if train_opt['mixprob'] else [1.0, 1.0, 1.0, 1.0, 1.0] #, 1.0, 1.0]
                self.mixalpha = train_opt['mixalpha'] if train_opt['mixalpha'] else [0.6, 1.0, 1.2, 0.7, 0.7] #, 0.001, 0.7]
                self.aux_mixprob = train_opt['aux_mixprob'] if train_opt['aux_mixprob'] else 1.0
                self.aux_mixalpha = train_opt['aux_mixalpha'] if train_opt['aux_mixalpha'] else 1.2
                self.mix_p = train_opt['mix_p'] if train_opt['mix_p'] else None
            
            """
            Setup frequency separation
            """
            self.fs = train_opt['fs'] if train_opt['fs'] else None
            self.f_low = None
            self.f_high = None
            if self.fs:
                lpf_type = train_opt['lpf_type'] if train_opt['lpf_type'] else "average"
                hpf_type = train_opt['hpf_type'] if train_opt['hpf_type'] else "average"
                self.f_low = FilterLow(filter_type=lpf_type).to(self.device)
                self.f_high = FilterHigh(filter_type=hpf_type).to(self.device)

            """
            Initialize losses
            """
            #Initialize the losses with the opt parameters
            # Generator losses:
            self.generatorlosses = losses.GeneratorLoss(opt, self.device)
            # TODO: show the configured losses names in logger
            # print(self.generatorlosses.loss_list)

            # Discriminator loss:
            if train_opt['gan_type'] and train_opt['gan_weight']:
                self.cri_gan = True
                diffaug = train_opt['diffaug'] if train_opt['diffaug'] else None
                dapolicy = None
                if diffaug: #TODO: this if should not be necessary
                    dapolicy = train_opt['dapolicy'] if train_opt['dapolicy'] else 'color,translation,cutout' #original
                self.adversarial = losses.Adversarial(train_opt=train_opt, device=self.device, diffaug = diffaug, dapolicy = dapolicy)
                # D_update_ratio and D_init_iters are for WGAN
                self.D_update_ratio = train_opt['D_update_ratio'] if train_opt['D_update_ratio'] else 1
                self.D_init_iters = train_opt['D_init_iters'] if train_opt['D_init_iters'] else 0
            else:
                self.cri_gan = False
 
            """
            Prepare optimizers
            """
            if self.cri_gan:
                self.optimizers, self.optimizer_G, self.optimizer_D = optimizers.get_optimizers(
                    self.cri_gan, self.netD, self.netG, train_opt, logger, self.optimizers)
            else:
                self.optimizers, self.optimizer_G = optimizers.get_optimizers(
                    None, None, self.netG, train_opt, logger, self.optimizers)

            """
            Prepare schedulers
            """
            self.schedulers = schedulers.get_schedulers(
                optimizers=self.optimizers, schedulers=self.schedulers, train_opt=train_opt)

            #Keep log in loss class instead?
            self.log_dict = OrderedDict()
        
        # print network
        """ 
        TODO:
        Network summary? Make optional with parameter
            could be an selector between traditional print_network() and summary()
        """
        #self.print_network() #TODO

        # for using virtual batch
        self.virtual_batch = opt["datasets"]["train"]["virtual_batch_size"] \
                if opt["datasets"]["train"]["virtual_batch_size"] \
                and opt["datasets"]["train"]["virtual_batch_size"] \
                >= opt["datasets"]["train"]["batch_size"] \
                else opt["datasets"]["train"]["batch_size"]
        self.accumulations = self.virtual_batch // opt["datasets"]["train"]["batch_size"]
        self.optimizer_G.zero_grad()
        if self. cri_gan:
            self.optimizer_D.zero_grad()

    def feed_data(self, data, need_HR=True):
        # LR images
        self.var_L = data['LR'].to(self.device)
        if need_HR:  # train or val
            # HR images
            self.var_H = data['HR'].to(self.device)
            # discriminator references
            input_ref = data['ref'] if 'ref' in data else data['HR']
            self.var_ref = input_ref.to(self.device)

    def feed_data_batch(self, data, need_HR=True):
        # LR
        self.var_L = data
        
    def optimize_parameters(self, step):       
        # G
        # freeze discriminator while generator is trained to prevent BP
        if self.cri_gan:
            for p in self.netD.parameters():
                p.requires_grad = False

        # batch (mixup) augmentations
        aug = None
        if self.mixup:
            self.var_H, self.var_L, mask, aug = BatchAug(
                self.var_H, self.var_L,
                self.mixopts, self.mixprob, self.mixalpha,
                self.aux_mixprob, self.aux_mixalpha, self.mix_p
                )
        
        ### Network forward, generate SR        
        if self.outm: #if the model has the final activation option
            self.fake_H = self.netG(self.var_L, outm=self.outm)
        else: #regular models without the final activation option
            self.fake_H = self.netG(self.var_L)
         
        # batch (mixup) augmentations
        # cutout-ed pixels are discarded when calculating loss by masking removed pixels
        if aug == "cutout":
            self.fake_H, self.var_H = self.fake_H*mask, self.var_H*mask
        
        l_g_total = 0

        """
        Calculate and log losses
        """
        loss_results = []
        # training generator and discriminator
        if self.cri_gan:
            # update generator alternatively
            if step % self.D_update_ratio == 0 and step > self.D_init_iters:
                # regular losses
                loss_results, self.log_dict = self.generatorlosses(self.fake_H, self.var_H, self.log_dict, self.f_low)
                l_g_total += sum(loss_results)/self.accumulations

                # adversarial loss
                l_g_gan = self.adversarial(
                    self.fake_H, self.var_ref, netD=self.netD, 
                    stage='generator', fsfilter = self.f_high) # (sr, hr)
                self.log_dict['l_g_gan'] = l_g_gan.item()
                
                l_g_total += l_g_gan/self.accumulations
                l_g_total.backward()

                # only step and clear gradient if virtual batch has completed
                if (step + 1) % self.accumulations == 0:
                    self.optimizer_G.step()
                    self.optimizer_G.zero_grad()

            # update discriminator
            # unfreeze discriminator
            for p in self.netD.parameters():
                p.requires_grad = True
            l_d_total = 0
            
            l_d_total, gan_logs = self.adversarial(
                self.fake_H, self.var_ref, netD=self.netD, 
                stage='discriminator', fsfilter = self.f_high) # (sr, hr)

            for g_log in gan_logs:
                self.log_dict[g_log] = gan_logs[g_log]

            l_d_total /= self.accumulations
            l_d_total.backward()

            # only step and clear gradient if virtual batch has completed
            if (step + 1) % self.accumulations == 0:
                self.optimizer_D.step()
                self.optimizer_D.zero_grad()
                
        # only training generator
        else:
            loss_results, self.log_dict = self.generatorlosses(self.fake_H, self.var_H, self.log_dict, self.f_low)
            l_g_total += sum(loss_results)/self.accumulations
            l_g_total.backward()

            # only step and clear gradient if virtual batch has completed
            if (step + 1) % self.accumulations == 0:
                self.optimizer_G.step()
                self.optimizer_G.zero_grad()
        
    def test(self):
        self.netG.eval()
        with torch.no_grad():
            if self.is_train:
                self.fake_H = self.netG(self.var_L)
            else:
                #self.fake_H = self.netG(self.var_L, isTest=True)
                self.fake_H = self.netG(self.var_L)
        self.netG.train()

    def get_current_log(self):
        return self.log_dict

    def get_current_visuals(self, need_HR=True):
        out_dict = OrderedDict()
        out_dict['LR'] = self.var_L.detach()[0].float().cpu()
        out_dict['SR'] = self.fake_H.detach()[0].float().cpu()
        if need_HR:
            out_dict['HR'] = self.var_H.detach()[0].float().cpu()
        #TODO for PPON ?
        #if get stages 1 and 2
            #out_dict['SR_content'] = ...
            #out_dict['SR_structure'] = ...
        return out_dict

    def get_current_visuals_batch(self, need_HR=True):
        out_dict = OrderedDict()
        out_dict['LR'] = self.var_L.detach().float().cpu()
        out_dict['SR'] = self.fake_H.detach().float().cpu()
        if need_HR:
            out_dict['HR'] = self.var_H.detach().float().cpu()
        #TODO for PPON ?
        #if get stages 1 and 2
            #out_dict['SR_content'] = ...
            #out_dict['SR_structure'] = ...
        return out_dict
        
    def print_network(self):
        # Generator
        s, n = self.get_network_description(self.netG)
        if isinstance(self.netG, nn.DataParallel):
            net_struc_str = '{} - {}'.format(self.netG.__class__.__name__,
                                             self.netG.module.__class__.__name__)
        else:
            net_struc_str = '{}'.format(self.netG.__class__.__name__)

        logger.info('Network G structure: {}, with parameters: {:,d}'.format(net_struc_str, n))
        logger.info(s)
        if self.is_train:
            # Discriminator
            if self.cri_gan:
                s, n = self.get_network_description(self.netD)
                if isinstance(self.netD, nn.DataParallel):
                    net_struc_str = '{} - {}'.format(self.netD.__class__.__name__,
                                                    self.netD.module.__class__.__name__)
                else:
                    net_struc_str = '{}'.format(self.netD.__class__.__name__)

                logger.info('Network D structure: {}, with parameters: {:,d}'.format(net_struc_str, n))
                logger.info(s)

            #TODO: feature network is not being trained, is it necessary to visualize? Maybe just name?
            # maybe show the generatorlosses instead?
            '''
            if self.generatorlosses.cri_fea:  # F, Perceptual Network
                #s, n = self.get_network_description(self.netF)
                s, n = self.get_network_description(self.generatorlosses.netF) #TODO
                #s, n = self.get_network_description(self.generatorlosses.loss_list.netF) #TODO
                if isinstance(self.generatorlosses.netF, nn.DataParallel):
                    net_struc_str = '{} - {}'.format(self.generatorlosses.netF.__class__.__name__,
                                                    self.generatorlosses.netF.module.__class__.__name__)
                else:
                    net_struc_str = '{}'.format(self.generatorlosses.netF.__class__.__name__)

                logger.info('Network F structure: {}, with parameters: {:,d}'.format(net_struc_str, n))
                logger.info(s)
            '''

    def load(self):
        load_path_G = self.opt['path']['pretrain_model_G']
        if load_path_G is not None:
            logger.info('Loading pretrained model for G [{:s}] ...'.format(load_path_G))
            strict = self.opt['path']['strict'] if self.opt['path']['strict'] else None
            self.load_network(load_path_G, self.netG, strict)
        if self.opt['is_train'] and self.opt['train']['gan_weight']:
            load_path_D = self.opt['path']['pretrain_model_D']
            if self.opt['is_train'] and load_path_D is not None:
                logger.info('Loading pretrained model for D [{:s}] ...'.format(load_path_D))
                self.load_network(load_path_D, self.netD)

    def save(self, iter_step):
        self.save_network(self.netG, 'G', iter_step)
        if self.cri_gan:
            self.save_network(self.netD, 'D', iter_step)
