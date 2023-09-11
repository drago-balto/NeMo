# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import itertools
import time
from contextlib import contextmanager
from functools import partial
from typing import Any, Dict, Optional, Union

import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn as nn
from einops import rearrange, repeat
from omegaconf import DictConfig, OmegaConf, open_dict
from pytorch_lightning import Trainer
from pytorch_lightning.accelerators import CPUAccelerator
from pytorch_lightning.utilities.distributed import rank_zero_only
from torch._inductor import config as inductor_config
from torch.optim.lr_scheduler import LambdaLR
from torchvision.utils import make_grid
from tqdm import tqdm

from nemo.collections.multimodal.data.stable_diffusion.stable_diffusion_dataset import (
    build_train_valid_datasets,
    build_train_valid_precached_datasets,
)
from nemo.collections.multimodal.models.multimodal_base_model import MegatronMultimodalModel
from nemo.collections.multimodal.models.stable_diffusion.diffusion_model import DiffusionModel
from nemo.collections.multimodal.models.stable_diffusion.ldm.autoencoder import (
    AutoencoderKL,
    IdentityFirstStage,
    VQModelInterface,
)
from nemo.collections.multimodal.models.stable_diffusion.samplers.ddim import DDIMSampler
from nemo.collections.multimodal.modules.stable_diffusion.diffusionmodules.util import (
    extract_into_tensor,
    make_beta_schedule,
    noise_like,
)
from nemo.collections.multimodal.modules.stable_diffusion.distributions.distributions import (
    DiagonalGaussianDistribution,
    normal_kl,
)
from nemo.collections.multimodal.parts.stable_diffusion.utils import (
    count_params,
    default,
    exists,
    isimage,
    ismap,
    log_txt_as_img,
    mean_flat,
)
from nemo.collections.multimodal.parts.utils import randn_like
from nemo.collections.nlp.modules.common.megatron.build_model import build_model
from nemo.collections.nlp.parts.utils_funcs import get_last_rank
from nemo.core.classes.common import Serialization
from nemo.utils import logging

try:
    from apex import amp
    from apex.transformer.enums import AttnMaskType
    from apex.transformer.pipeline_parallel.utils import get_num_microbatches

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False

try:
    from megatron.core import parallel_state
    from megatron.core.pipeline_parallel.schedules import get_forward_backward_func

    HAVE_MEGATRON_CORE = True

except (ImportError, ModuleNotFoundError):

    HAVE_MEGATRON_CORE = False

__conditioning_keys__ = {'concat': 'c_concat', 'crossattn': 'c_crossattn', 'adm': 'y'}


def random_dropout(embeddings, drop_rate):
    r"""
    Function to perform random dropout for embeddings.
    When we drop embeddings, we zero them out.
    Args:
        embeddings (tensor): Input embeddings
        drop_rate (float): Rate of dropping the embedding.
    """
    nsamples = embeddings.shape[0]
    zero_flag = torch.ones(nsamples, 1, 1, device=torch.cuda.current_device()).to(embeddings.dtype) * (1 - drop_rate)
    zero_flag = torch.bernoulli(zero_flag).cuda(non_blocking=True)
    embeddings = embeddings * zero_flag
    return embeddings


def disabled_train(self, mode=True):
    """Overwrite model.train with this function to make sure train/eval mode
    does not change anymore."""
    return self


def uniform_on_device(r1, r2, shape, device):
    return (r1 - r2) * torch.rand(*shape, device=device) + r2


class DDPM(torch.nn.Module):
    def __init__(self, cfg):
        super().__init__()
        assert cfg.parameterization in ["eps", "x0", "v"], 'currently only supporting "eps" and "x0" and "v"'
        self.parameterization = cfg.parameterization
        logging.info(f"{self.__class__.__name__}: Running in {self.parameterization}-prediction mode")
        self.cond_stage_model = None
        self.clip_denoised = cfg.clip_denoised
        self.log_every_t = cfg.log_every_t
        self.first_stage_key = cfg.first_stage_key
        self.image_size = cfg.image_size  # try conv?
        self.channels = cfg.channels
        self.channels_last = cfg.get("channels_last", False)
        self.use_positional_encodings = cfg.use_positional_encodings
        self.model = DiffusionWrapper(
            cfg.unet_config,
            cfg.conditioning_key,
            cfg.inductor,
            cfg.inductor_cudagraphs,
            cfg.get("capture_cudagraph_iters", -1),
        )
        self.model_type = None
        count_params(self.model, verbose=True)

        self.v_posterior = cfg.v_posterior
        self.original_elbo_weight = cfg.original_elbo_weight
        self.l_simple_weight = cfg.l_simple_weight

        self.register_schedule(
            given_betas=cfg.given_betas,
            beta_schedule=cfg.beta_schedule,
            timesteps=cfg.timesteps,
            linear_start=cfg.linear_start,
            linear_end=cfg.linear_end,
            cosine_s=cfg.cosine_s,
        )

        self.loss_type = cfg.loss_type

        self.learn_logvar = cfg.learn_logvar
        self.logvar = torch.full(fill_value=cfg.logvar_init, size=(self.num_timesteps,))
        if self.learn_logvar:
            self.logvar = nn.Parameter(self.logvar, requires_grad=True)

        self.rng = torch.Generator(device=torch.cuda.current_device(),)

    def register_schedule(
        self,
        given_betas=None,
        beta_schedule="linear",
        timesteps=1000,
        linear_start=1e-4,
        linear_end=2e-2,
        cosine_s=8e-3,
    ):
        if exists(given_betas):
            betas = given_betas
        else:
            betas = make_beta_schedule(
                beta_schedule, timesteps, linear_start=linear_start, linear_end=linear_end, cosine_s=cosine_s
            )
        alphas = 1.0 - betas
        alphas_cumprod = np.cumprod(alphas, axis=0)
        alphas_cumprod_prev = np.append(1.0, alphas_cumprod[:-1])

        (timesteps,) = betas.shape
        self.num_timesteps = int(timesteps)
        self.linear_start = linear_start
        self.linear_end = linear_end
        assert alphas_cumprod.shape[0] == self.num_timesteps, 'alphas have to be defined for each timestep'

        to_torch = partial(torch.tensor, dtype=torch.float32)

        self.register_buffer('betas', to_torch(betas))
        self.register_buffer('alphas_cumprod', to_torch(alphas_cumprod))
        self.register_buffer('alphas_cumprod_prev', to_torch(alphas_cumprod_prev))

        # calculations for diffusion q(x_t | x_{t-1}) and others
        self.register_buffer('sqrt_alphas_cumprod', to_torch(np.sqrt(alphas_cumprod)))
        self.register_buffer('sqrt_one_minus_alphas_cumprod', to_torch(np.sqrt(1.0 - alphas_cumprod)))
        self.register_buffer('log_one_minus_alphas_cumprod', to_torch(np.log(1.0 - alphas_cumprod)))
        self.register_buffer('sqrt_recip_alphas_cumprod', to_torch(np.sqrt(1.0 / alphas_cumprod)))
        self.register_buffer('sqrt_recipm1_alphas_cumprod', to_torch(np.sqrt(1.0 / alphas_cumprod - 1)))

        # calculations for posterior q(x_{t-1} | x_t, x_0)
        posterior_variance = (1 - self.v_posterior) * betas * (1.0 - alphas_cumprod_prev) / (
            1.0 - alphas_cumprod
        ) + self.v_posterior * betas
        # above: equal to 1. / (1. / (1. - alpha_cumprod_tm1) + alpha_t / beta_t)
        self.register_buffer('posterior_variance', to_torch(posterior_variance))
        # below: log calculation clipped because the posterior variance is 0 at the beginning of the diffusion chain
        self.register_buffer('posterior_log_variance_clipped', to_torch(np.log(np.maximum(posterior_variance, 1e-20))))
        self.register_buffer(
            'posterior_mean_coef1', to_torch(betas * np.sqrt(alphas_cumprod_prev) / (1.0 - alphas_cumprod))
        )
        self.register_buffer(
            'posterior_mean_coef2', to_torch((1.0 - alphas_cumprod_prev) * np.sqrt(alphas) / (1.0 - alphas_cumprod))
        )

        if self.parameterization == "eps":
            lvlb_weights = self.betas ** 2 / (
                2 * self.posterior_variance * to_torch(alphas) * (1 - self.alphas_cumprod)
            )
        elif self.parameterization == "x0":
            lvlb_weights = 0.5 * np.sqrt(torch.Tensor(alphas_cumprod)) / (2.0 * 1 - torch.Tensor(alphas_cumprod))
        elif self.parameterization == "v":
            lvlb_weights = torch.ones_like(
                self.betas ** 2 / (2 * self.posterior_variance * to_torch(alphas) * (1 - self.alphas_cumprod))
            )
        else:
            raise NotImplementedError("mu not supported")
        # TODO how to choose this term
        lvlb_weights[0] = lvlb_weights[1]
        self.register_buffer('lvlb_weights', lvlb_weights, persistent=False)
        assert not torch.isnan(self.lvlb_weights).all()

    def init_from_ckpt(
        self, path, ignore_keys=list(), only_model=False, load_vae=True, load_unet=True, load_encoder=True,
    ):
        pl_sd = torch.load(path, map_location="cpu")
        if "state_dict" in list(pl_sd.keys()):
            pl_sd = pl_sd["state_dict"]

        sd = {}
        first_key = list(pl_sd.keys())[0]
        # State keys of model trained with TorchDynamo changed from
        # "model.xxx" to "model._orig_mod.xxx"
        for k, v in pl_sd.items():
            new_k = k.replace("._orig_mod", "")
            # compatibility for stable diffusion old checkpoint
            # remove megatron wrapper prefix
            if first_key == "model.betas":
                new_k = new_k.lstrip("model.")
            sd[new_k] = v

        logging.info(f"Loading {path}")
        logging.info(f"It has {len(sd)} entries")
        logging.info(f"Existing model has {len(self.state_dict())} entries")

        keys = list(sd.keys())
        for k in keys:
            for ik in ignore_keys:
                if k.startswith(ik):
                    logging.info("Deleting ignored key {} from state_dict.".format(k))
                    del sd[k]

        if not load_vae:
            deleted = 0
            keys = list(sd.keys())
            for k in keys:
                if k.startswith("first_stage_model"):
                    deleted += 1
                    del sd[k]
            logging.info(f"Deleted {deleted} keys from `first_stage_model` state_dict.")

        if not load_encoder:
            deleted = 0
            keys = list(sd.keys())
            for k in keys:
                if k.startswith("cond_stage_model"):
                    deleted += 1
                    del sd[k]
            logging.info(f"Deleted {deleted} keys from `cond_stage_model` state_dict.")

        if not load_unet:
            deleted = 0
            keys = list(sd.keys())
            for k in keys:
                if k.startswith("model.diffusion_model"):
                    deleted += 1
                    del sd[k]
            logging.info(f"Deleted {deleted} keys from `model.diffusion_model` state_dict.")

        missing, unexpected = (
            self.load_state_dict(sd, strict=False) if not only_model else self.model.load_state_dict(sd, strict=False)
        )
        logging.info(f"Restored from {path} with {len(missing)} missing and {len(unexpected)} unexpected keys")
        if len(missing) > 0:
            logging.info(f"Missing Keys: {missing}")
        if len(unexpected) > 0:
            logging.info(f"Unexpected Keys: {unexpected}")

    def q_mean_variance(self, x_start, t):
        """
        Get the distribution q(x_t | x_0).
        :param x_start: the [N x C x ...] tensor of noiseless inputs.
        :param t: the number of diffusion steps (minus 1). Here, 0 means one step.
        :return: A tuple (mean, variance, log_variance), all of x_start's shape.
        """
        mean = extract_into_tensor(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start
        variance = extract_into_tensor(1.0 - self.alphas_cumprod, t, x_start.shape)
        log_variance = extract_into_tensor(self.log_one_minus_alphas_cumprod, t, x_start.shape)
        return mean, variance, log_variance

    def predict_start_from_noise(self, x_t, t, noise):
        return (
            extract_into_tensor(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t
            - extract_into_tensor(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape) * noise
        )

    def predict_start_from_z_and_v(self, x_t, t, v):
        return (
            extract_into_tensor(self.sqrt_alphas_cumprod, t, x_t.shape) * x_t
            - extract_into_tensor(self.sqrt_one_minus_alphas_cumprod, t, x_t.shape) * v
        )

    def predict_eps_from_z_and_v(self, x_t, t, v):
        return (
            extract_into_tensor(self.sqrt_alphas_cumprod, t, x_t.shape) * v
            + extract_into_tensor(self.sqrt_one_minus_alphas_cumprod, t, x_t.shape) * x_t
        )

    def q_posterior(self, x_start, x_t, t):
        posterior_mean = (
            extract_into_tensor(self.posterior_mean_coef1, t, x_t.shape) * x_start
            + extract_into_tensor(self.posterior_mean_coef2, t, x_t.shape) * x_t
        )
        posterior_variance = extract_into_tensor(self.posterior_variance, t, x_t.shape)
        posterior_log_variance_clipped = extract_into_tensor(self.posterior_log_variance_clipped, t, x_t.shape)
        return posterior_mean, posterior_variance, posterior_log_variance_clipped

    def p_mean_variance(self, x, t, clip_denoised: bool):
        model_out = self.model(x, t)
        if self.parameterization == "eps":
            x_recon = self.predict_start_from_noise(x, t=t, noise=model_out)
        elif self.parameterization == "x0":
            x_recon = model_out
        if clip_denoised:
            x_recon.clamp_(-1.0, 1.0)

        model_mean, posterior_variance, posterior_log_variance = self.q_posterior(x_start=x_recon, x_t=x, t=t)
        return model_mean, posterior_variance, posterior_log_variance

    @torch.no_grad()
    def p_sample(self, x, t, clip_denoised=True, repeat_noise=False):
        b, *_, device = *x.shape, x.device
        model_mean, _, model_log_variance = self.p_mean_variance(x=x, t=t, clip_denoised=clip_denoised)
        noise = noise_like(x.shape, device, repeat_noise)
        # no noise when t == 0
        nonzero_mask = (1 - (t == 0).float()).reshape(b, *((1,) * (len(x.shape) - 1)))
        return model_mean + nonzero_mask * (0.5 * model_log_variance).exp() * noise

    @torch.no_grad()
    def p_sample_loop(self, shape, return_intermediates=False):
        device = self.betas.device
        b = shape[0]
        img = torch.randn(shape, generator=self.rng, device=device)
        intermediates = [img]
        for i in tqdm(reversed(range(0, self.num_timesteps)), desc='Sampling t', total=self.num_timesteps):
            img = self.p_sample(
                img, torch.full((b,), i, device=device, dtype=torch.long), clip_denoised=self.clip_denoised
            )
            if i % self.log_every_t == 0 or i == self.num_timesteps - 1:
                intermediates.append(img)
        if return_intermediates:
            return img, intermediates
        return img

    @torch.no_grad()
    def sample(self, batch_size=16, return_intermediates=False):
        image_size = self.image_size
        channels = self.channels
        return self.p_sample_loop(
            (batch_size, channels, image_size, image_size), return_intermediates=return_intermediates
        )

    def q_sample(self, x_start, t, noise=None):
        noise = default(noise, lambda: randn_like(x_start, generator=self.rng))
        return (
            extract_into_tensor(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start
            + extract_into_tensor(self.sqrt_one_minus_alphas_cumprod, t, x_start.shape) * noise
        )

    def get_v(self, x, noise, t):
        return (
            extract_into_tensor(self.sqrt_alphas_cumprod, t, x.shape) * noise
            - extract_into_tensor(self.sqrt_one_minus_alphas_cumprod, t, x.shape) * x
        )

    def get_loss(self, pred, target, mean=True):
        if self.loss_type == 'l1':
            loss = (target - pred).abs()
            if mean:
                loss = loss.mean()
        elif self.loss_type == 'l2':
            if mean:
                loss = torch.nn.functional.mse_loss(target, pred)
            else:
                loss = torch.nn.functional.mse_loss(target, pred, reduction='none')
        else:
            raise NotImplementedError("unknown loss type '{loss_type}'")

        return loss

    def p_losses(self, x_start, t, noise=None):
        noise = default(noise, lambda: randn_like(x_start, generator=self.rng))
        x_noisy = self.q_sample(x_start=x_start, t=t, noise=noise)
        model_out = self.model(x_noisy, t)

        loss_dict = {}
        if self.parameterization == "eps":
            target = noise
        elif self.parameterization == "x0":
            target = x_start
        elif self.parameterization == "v":
            target = self.get_v(x_start, noise, t)
        else:
            raise NotImplementedError(f"Paramterization {self.parameterization} not yet supported")

        loss = self.get_loss(model_out, target, mean=False).mean(dim=[1, 2, 3])

        log_prefix = 'train' if self.training else 'val'

        loss_dict.update({f'{log_prefix}/loss_simple': loss.mean()})
        loss_simple = loss.mean() * self.l_simple_weight

        loss_vlb = (self.lvlb_weights[t] * loss).mean()
        loss_dict.update({f'{log_prefix}/loss_vlb': loss_vlb})

        loss = loss_simple + self.original_elbo_weight * loss_vlb

        loss_dict.update({f'{log_prefix}/loss': loss})

        return loss, loss_dict

    def forward(self, x, *args, **kwargs):
        # b, c, h, w, device, img_size, = *x.shape, x.device, self.image_size
        # assert h == img_size and w == img_size, f'height and width of image must be {img_size}'
        t = torch.randint(0, self.num_timesteps, (x.shape[0],), generator=self.rng, device=x.device).long()
        return self.p_losses(x, t, *args, **kwargs)

    def get_input(self, batch, k):
        x = batch[k]
        if len(x.shape) == 3:
            x = x[..., None]
        if self.channels_last:
            x = x.permute(0, 3, 1, 2).to(non_blocking=True)
        else:
            x = rearrange(x, "b h w c -> b c h w")
            x = x.to(memory_format=torch.contiguous_format, non_blocking=True)
        return x

    def shared_step(self, batch):
        x = self.get_input(batch, self.first_stage_key)
        loss, loss_dict = self(x)
        return loss, loss_dict

    def _get_rows_from_list(self, samples):
        n_imgs_per_row = len(samples)
        denoise_grid = rearrange(samples, 'n b c h w -> b n c h w')
        denoise_grid = rearrange(denoise_grid, 'b n c h w -> (b n) c h w')
        denoise_grid = make_grid(denoise_grid, nrow=n_imgs_per_row)
        return denoise_grid

    @torch.no_grad()
    def log_images(self, batch, N=8, n_row=2, sample=True, return_keys=None, **kwargs):
        log = dict()
        x = self.get_input(batch, self.first_stage_key)
        N = min(x.shape[0], N)
        n_row = min(x.shape[0], n_row)
        x = x[:N]
        log["inputs"] = x

        # get diffusion row
        diffusion_row = list()
        x_start = x[:n_row]

        for t in range(self.num_timesteps):
            if t % self.log_every_t == 0 or t == self.num_timesteps - 1:
                t = repeat(torch.tensor([t]), '1 -> b', b=n_row)
                t = t.long()
                noise = randn_like(x_start, generator=self.rng)
                x_noisy = self.q_sample(x_start=x_start, t=t, noise=noise)
                diffusion_row.append(x_noisy)

        log["diffusion_row"] = self._get_rows_from_list(diffusion_row)

        if sample:
            # get denoise row
            with self.ema_scope("Plotting"):
                samples, denoise_row = self.sample(batch_size=N, return_intermediates=True)

            log["samples"] = samples
            log["denoise_row"] = self._get_rows_from_list(denoise_row)

        if return_keys:
            if np.intersect1d(list(log.keys()), return_keys).shape[0] == 0:
                return log
            else:
                return {key: log[key] for key in return_keys}
        return log


class LatentDiffusion(DDPM, Serialization):
    """main class"""

    def __init__(self, cfg):
        self.num_timesteps_cond = default(cfg.num_timesteps_cond, 1)
        self.scale_by_std = cfg.scale_by_std
        assert self.num_timesteps_cond <= cfg.timesteps
        # for backwards compatibility after implementation of DiffusionWrapper
        if cfg.conditioning_key is None:
            conditioning_key = 'concat' if cfg.concat_mode else 'crossattn'
        else:
            conditioning_key = cfg.conditioning_key
        if cfg.cond_stage_config == '__is_unconditional__':
            conditioning_key = None
        ckpt_path = cfg.ckpt_path
        ignore_keys = cfg.ignore_keys
        cfg.conditioning_key = conditioning_key
        super().__init__(cfg=cfg)
        self.precision = cfg.precision
        self.concat_mode = cfg.concat_mode
        self.cond_stage_trainable = cfg.cond_stage_trainable
        self.cond_stage_key = cfg.cond_stage_key

        self.num_downs = 0
        if "ddconfig" in cfg.first_stage_config and "ch_mult" in cfg.first_stage_config.ddconfig:
            self.num_downs = len(cfg.first_stage_config.ddconfig.ch_mult) - 1
        if not cfg.scale_by_std:
            self.scale_factor = cfg.scale_factor
        else:
            self.register_buffer('scale_factor', torch.tensor(cfg.scale_factor))
        self.instantiate_first_stage(cfg.first_stage_config)
        self.instantiate_cond_stage(cfg.cond_stage_config)
        self.cond_stage_forward = cfg.cond_stage_forward
        self.clip_denoised = False
        self.bbox_tokenizer = None
        self.text_embedding_dropout_rate = cfg.text_embedding_dropout_rate
        self.fused_opt = cfg.fused_opt

        self.restarted_from_ckpt = False
        if ckpt_path is not None:
            load_vae = True if cfg.load_vae is None else cfg.load_vae
            load_unet = True if cfg.load_unet is None else cfg.load_unet
            load_encoder = True if cfg.load_encoder is None else cfg.load_encoder

            self.init_from_ckpt(
                ckpt_path, ignore_keys, load_vae=load_vae, load_unet=load_unet, load_encoder=load_encoder,
            )
            self.restarted_from_ckpt = True

        if self.channels_last:
            self.first_stage_model = self.first_stage_model.to(memory_format=torch.channels_last)
            self.model = self.model.to(memory_format=torch.channels_last)

    def make_cond_schedule(self,):
        self.cond_ids = torch.full(size=(self.num_timesteps,), fill_value=self.num_timesteps - 1, dtype=torch.long)
        ids = torch.round(torch.linspace(0, self.num_timesteps - 1, self.num_timesteps_cond)).long()
        self.cond_ids[: self.num_timesteps_cond] = ids

    def on_train_batch_start(self, batch, batch_idx, dataloader_idx=0):
        # only for very first batch
        # set rescale weight to 1./std of encodings
        logging.info("### USING STD-RESCALING ###")
        x = super().get_input(batch, self.first_stage_key)
        encoder_posterior = self.encode_first_stage(x)
        z = self.get_first_stage_encoding(encoder_posterior).detach()
        del self.scale_factor
        self.register_buffer('scale_factor', 1.0 / z.flatten().std())
        logging.info(f"setting self.scale_factor to {self.scale_factor}")
        logging.info("### USING STD-RESCALING ###")

    def register_schedule(
        self,
        given_betas=None,
        beta_schedule="linear",
        timesteps=1000,
        linear_start=1e-4,
        linear_end=2e-2,
        cosine_s=8e-3,
    ):
        super().register_schedule(given_betas, beta_schedule, timesteps, linear_start, linear_end, cosine_s)

        self.shorten_cond_schedule = self.num_timesteps_cond > 1
        if self.shorten_cond_schedule:
            self.make_cond_schedule()

    def instantiate_first_stage(self, config):
        model = LatentDiffusion.from_config_dict(config)
        self.first_stage_model = model.eval()
        self.first_stage_model.train = disabled_train
        for param in self.first_stage_model.parameters():
            param.requires_grad = False

    def instantiate_cond_stage(self, config):
        if not self.cond_stage_trainable:
            if config == "__is_first_stage__":
                logging.info("Using first stage also as cond stage.")
                self.cond_stage_model = self.first_stage_model
            elif config == "__is_unconditional__":
                logging.info(f"Training {self.__class__.__name__} as an unconditional model.")
                self.cond_stage_model = None
                # self.be_unconditional = True
            else:
                model = LatentDiffusion.from_config_dict(config)
                self.cond_stage_model = model.eval()
                self.cond_stage_model.train = disabled_train
                for param in self.cond_stage_model.parameters():
                    param.requires_grad = False
        else:
            assert config != '__is_first_stage__'
            assert config != '__is_unconditional__'
            model = LatentDiffusion.from_config_dict(config)
            self.cond_stage_model = model

    def _get_denoise_row_from_list(self, samples, desc='', force_no_decoder_quantization=False):
        denoise_row = []
        for zd in tqdm(samples, desc=desc):
            denoise_row.append(self.decode_first_stage(zd, force_not_quantize=force_no_decoder_quantization))
        n_imgs_per_row = len(denoise_row)
        denoise_row = torch.stack(denoise_row)  # n_log_step, n_row, C, H, W
        denoise_grid = rearrange(denoise_row, 'n b c h w -> b n c h w')
        denoise_grid = rearrange(denoise_grid, 'b n c h w -> (b n) c h w')
        denoise_grid = make_grid(denoise_grid, nrow=n_imgs_per_row)
        return denoise_grid

    def get_first_stage_encoding(self, encoder_posterior):
        if isinstance(encoder_posterior, DiagonalGaussianDistribution):
            z = encoder_posterior.sample()
        elif isinstance(encoder_posterior, torch.Tensor):
            z = encoder_posterior
        else:
            raise NotImplementedError(f"encoder_posterior of type '{type(encoder_posterior)}' not yet implemented")
        return self.scale_factor * z

    def get_learned_conditioning(self, c):
        if self.cond_stage_forward is None:
            if hasattr(self.cond_stage_model, 'encode') and callable(self.cond_stage_model.encode):
                c = self.cond_stage_model.encode(c)
                if isinstance(c, DiagonalGaussianDistribution):
                    c = c.mode()
            else:
                c = self.cond_stage_model(c)
        else:
            assert hasattr(self.cond_stage_model, self.cond_stage_forward)
            c = getattr(self.cond_stage_model, self.cond_stage_forward)(c)
        return c

    def meshgrid(self, h, w):
        y = torch.arange(0, h).view(h, 1, 1).repeat(1, w, 1)
        x = torch.arange(0, w).view(1, w, 1).repeat(h, 1, 1)

        arr = torch.cat([y, x], dim=-1)
        return arr

    def delta_border(self, h, w):
        """
        :param h: height
        :param w: width
        :return: normalized distance to image border,
         wtith min distance = 0 at border and max dist = 0.5 at image center
        """
        lower_right_corner = torch.tensor([h - 1, w - 1]).view(1, 1, 2)
        arr = self.meshgrid(h, w) / lower_right_corner
        dist_left_up = torch.min(arr, dim=-1, keepdims=True)[0]
        dist_right_down = torch.min(1 - arr, dim=-1, keepdims=True)[0]
        edge_dist = torch.min(torch.cat([dist_left_up, dist_right_down], dim=-1), dim=-1)[0]
        return edge_dist

    def get_weighting(self, h, w, Ly, Lx, device):
        weighting = self.delta_border(h, w)
        weighting = torch.clip(
            weighting, self.split_input_params["clip_min_weight"], self.split_input_params["clip_max_weight"],
        )
        weighting = weighting.view(1, h * w, 1).repeat(1, 1, Ly * Lx).to(device)

        if self.split_input_params["tie_braker"]:
            L_weighting = self.delta_border(Ly, Lx)
            L_weighting = torch.clip(
                L_weighting,
                self.split_input_params["clip_min_tie_weight"],
                self.split_input_params["clip_max_tie_weight"],
            )

            L_weighting = L_weighting.view(1, 1, Ly * Lx).to(device)
            weighting = weighting * L_weighting
        return weighting

    def get_fold_unfold(self, x, kernel_size, stride, uf=1, df=1):  # todo load once not every time, shorten code
        """
        :param x: img of size (bs, c, h, w)
        :return: n img crops of size (n, bs, c, kernel_size[0], kernel_size[1])
        """
        bs, nc, h, w = x.shape

        # number of crops in image
        Ly = (h - kernel_size[0]) // stride[0] + 1
        Lx = (w - kernel_size[1]) // stride[1] + 1

        if uf == 1 and df == 1:
            fold_params = dict(kernel_size=kernel_size, dilation=1, padding=0, stride=stride)
            unfold = torch.nn.Unfold(**fold_params)

            fold = torch.nn.Fold(output_size=x.shape[2:], **fold_params)

            weighting = self.get_weighting(kernel_size[0], kernel_size[1], Ly, Lx, x.device).to(x.dtype)
            normalization = fold(weighting).view(1, 1, h, w)  # normalizes the overlap
            weighting = weighting.view((1, 1, kernel_size[0], kernel_size[1], Ly * Lx))

        elif uf > 1 and df == 1:
            fold_params = dict(kernel_size=kernel_size, dilation=1, padding=0, stride=stride)
            unfold = torch.nn.Unfold(**fold_params)

            fold_params2 = dict(
                kernel_size=(kernel_size[0] * uf, kernel_size[0] * uf),
                dilation=1,
                padding=0,
                stride=(stride[0] * uf, stride[1] * uf),
            )
            fold = torch.nn.Fold(output_size=(x.shape[2] * uf, x.shape[3] * uf), **fold_params2)

            weighting = self.get_weighting(kernel_size[0] * uf, kernel_size[1] * uf, Ly, Lx, x.device).to(x.dtype)
            normalization = fold(weighting).view(1, 1, h * uf, w * uf)  # normalizes the overlap
            weighting = weighting.view((1, 1, kernel_size[0] * uf, kernel_size[1] * uf, Ly * Lx))

        elif df > 1 and uf == 1:
            fold_params = dict(kernel_size=kernel_size, dilation=1, padding=0, stride=stride)
            unfold = torch.nn.Unfold(**fold_params)

            fold_params2 = dict(
                kernel_size=(kernel_size[0] // df, kernel_size[0] // df),
                dilation=1,
                padding=0,
                stride=(stride[0] // df, stride[1] // df),
            )
            fold = torch.nn.Fold(output_size=(x.shape[2] // df, x.shape[3] // df), **fold_params2)

            weighting = self.get_weighting(kernel_size[0] // df, kernel_size[1] // df, Ly, Lx, x.device).to(x.dtype)
            normalization = fold(weighting).view(1, 1, h // df, w // df)  # normalizes the overlap
            weighting = weighting.view((1, 1, kernel_size[0] // df, kernel_size[1] // df, Ly * Lx))

        else:
            raise NotImplementedError

        return fold, unfold, normalization, weighting

    @torch.no_grad()
    def get_input(
        self,
        batch,
        k,
        return_first_stage_outputs=False,
        force_c_encode=False,
        cond_key=None,
        return_original_cond=False,
        bs=None,
    ):
        if self.first_stage_key.endswith("encoded"):
            # Loading encoded images from the disk
            encoder_posterior = batch[self.first_stage_key]  # torch.Size([3, 4, 64, 64])
        elif self.first_stage_key.endswith("moments"):
            # Loading distribution from disk and sampling encoded
            distribution = batch[self.first_stage_key]  # torch.size([3, 1, 8, 64, 64])
            distribution = torch.squeeze(distribution, dim=1)
            encoder_posterior = DiagonalGaussianDistribution(distribution)
        else:
            # Loading images from disk and encoding them
            x = super().get_input(batch, k)
            if bs is not None:
                x = x[:bs]
            encoder_posterior = self.encode_first_stage(x)

        # z.shape = torch.Size([BS, 4, 64, 64])
        z = self.get_first_stage_encoding(encoder_posterior).detach()

        if self.model.conditioning_key is not None:
            if cond_key is None:
                cond_key = self.cond_stage_key
            if cond_key != self.first_stage_key:
                if cond_key in ['captions', 'coordinates_bbox', 'txt'] or cond_key.endswith("encoded"):
                    xc = batch[cond_key]
                elif cond_key == 'class_label':
                    xc = batch
                else:
                    xc = super().get_input(batch, cond_key)
            else:
                xc = x
            if (not self.cond_stage_trainable or force_c_encode) and (not cond_key.endswith('encoded')):
                if isinstance(xc, dict) or isinstance(xc, list):
                    # import pudb; pudb.set_trace()
                    c = self.get_learned_conditioning(xc)
                else:
                    c = self.get_learned_conditioning(xc)
            else:
                c = xc
            if bs is not None:
                c = c[:bs]

            if self.use_positional_encodings:
                pos_x, pos_y = self.compute_latent_shifts(batch)
                ckey = __conditioning_keys__[self.model.conditioning_key]
                c = {ckey: c, 'pos_x': pos_x, 'pos_y': pos_y}

            if self.text_embedding_dropout_rate > 0:
                assert self.text_embedding_dropout_rate < 1.0
                c = random_dropout(c, drop_rate=self.text_embedding_dropout_rate)

        else:
            c = None
            xc = None
            if self.use_positional_encodings:
                pos_x, pos_y = self.compute_latent_shifts(batch)
                c = {'pos_x': pos_x, 'pos_y': pos_y}
        out = [z, c]
        if return_first_stage_outputs:
            xrec = self.decode_first_stage(z)
            out.extend([x, xrec])
        if return_original_cond:
            out.append(xc)
        return out

    @torch.no_grad()
    def decode_first_stage(self, z, predict_cids=False, force_not_quantize=False):
        if predict_cids:
            if z.dim() == 4:
                z = torch.argmax(z.exp(), dim=1).long()
            z = self.first_stage_model.quantize.get_codebook_entry(z, shape=None)
            z = rearrange(z, 'b h w c -> b c h w').contiguous()

        z = 1.0 / self.scale_factor * z

        if hasattr(self, "split_input_params"):
            if self.split_input_params["patch_distributed_vq"]:
                ks = self.split_input_params["ks"]  # eg. (128, 128)
                stride = self.split_input_params["stride"]  # eg. (64, 64)
                uf = self.split_input_params["vqf"]
                bs, nc, h, w = z.shape
                if ks[0] > h or ks[1] > w:
                    ks = (min(ks[0], h), min(ks[1], w))
                    logging.info("reducing Kernel")

                if stride[0] > h or stride[1] > w:
                    stride = (min(stride[0], h), min(stride[1], w))
                    logging.info("reducing stride")

                fold, unfold, normalization, weighting = self.get_fold_unfold(z, ks, stride, uf=uf)

                z = unfold(z)  # (bn, nc * prod(**ks), L)
                # 1. Reshape to img shape
                z = z.view((z.shape[0], -1, ks[0], ks[1], z.shape[-1]))  # (bn, nc, ks[0], ks[1], L )

                # 2. apply model loop over last dim
                if isinstance(self.first_stage_model, VQModelInterface):
                    output_list = [
                        self.first_stage_model.decode(
                            z[:, :, :, :, i], force_not_quantize=predict_cids or force_not_quantize
                        )
                        for i in range(z.shape[-1])
                    ]
                else:

                    output_list = [self.first_stage_model.decode(z[:, :, :, :, i]) for i in range(z.shape[-1])]

                o = torch.stack(output_list, axis=-1)  # # (bn, nc, ks[0], ks[1], L)
                o = o * weighting
                # Reverse 1. reshape to img shape
                o = o.view((o.shape[0], -1, o.shape[-1]))  # (bn, nc * ks[0] * ks[1], L)
                # stitch crops together
                decoded = fold(o)
                decoded = decoded / normalization  # norm is shape (1, 1, h, w)
                return decoded
            else:
                if isinstance(self.first_stage_model, VQModelInterface):
                    return self.first_stage_model.decode(z, force_not_quantize=predict_cids or force_not_quantize)
                else:
                    return self.first_stage_model.decode(z)

        else:
            if isinstance(self.first_stage_model, VQModelInterface):
                return self.first_stage_model.decode(z, force_not_quantize=predict_cids or force_not_quantize)
            else:
                return self.first_stage_model.decode(z)

    # same as above but without decorator
    def differentiable_decode_first_stage(self, z, predict_cids=False, force_not_quantize=False):
        if predict_cids:
            if z.dim() == 4:
                z = torch.argmax(z.exp(), dim=1).long()
            z = self.first_stage_model.quantize.get_codebook_entry(z, shape=None)
            z = rearrange(z, 'b h w c -> b c h w').contiguous()

        z = 1.0 / self.scale_factor * z

        if hasattr(self, "split_input_params"):
            if self.split_input_params["patch_distributed_vq"]:
                ks = self.split_input_params["ks"]  # eg. (128, 128)
                stride = self.split_input_params["stride"]  # eg. (64, 64)
                uf = self.split_input_params["vqf"]
                bs, nc, h, w = z.shape
                if ks[0] > h or ks[1] > w:
                    ks = (min(ks[0], h), min(ks[1], w))
                    logging.info("reducing Kernel")

                if stride[0] > h or stride[1] > w:
                    stride = (min(stride[0], h), min(stride[1], w))
                    logging.info("reducing stride")

                fold, unfold, normalization, weighting = self.get_fold_unfold(z, ks, stride, uf=uf)

                z = unfold(z)  # (bn, nc * prod(**ks), L)
                # 1. Reshape to img shape
                z = z.view((z.shape[0], -1, ks[0], ks[1], z.shape[-1]))  # (bn, nc, ks[0], ks[1], L )

                # 2. apply model loop over last dim
                if isinstance(self.first_stage_model, VQModelInterface):
                    output_list = [
                        self.first_stage_model.decode(
                            z[:, :, :, :, i], force_not_quantize=predict_cids or force_not_quantize
                        )
                        for i in range(z.shape[-1])
                    ]
                else:

                    output_list = [self.first_stage_model.decode(z[:, :, :, :, i]) for i in range(z.shape[-1])]

                o = torch.stack(output_list, axis=-1)  # # (bn, nc, ks[0], ks[1], L)
                o = o * weighting
                # Reverse 1. reshape to img shape
                o = o.view((o.shape[0], -1, o.shape[-1]))  # (bn, nc * ks[0] * ks[1], L)
                # stitch crops together
                decoded = fold(o)
                decoded = decoded / normalization  # norm is shape (1, 1, h, w)
                return decoded
            else:
                if isinstance(self.first_stage_model, VQModelInterface):
                    return self.first_stage_model.decode(z, force_not_quantize=predict_cids or force_not_quantize)
                else:
                    return self.first_stage_model.decode(z)

        else:
            if isinstance(self.first_stage_model, VQModelInterface):
                return self.first_stage_model.decode(z, force_not_quantize=predict_cids or force_not_quantize)
            else:
                return self.first_stage_model.decode(z)

    @torch.no_grad()
    def encode_first_stage(self, x):
        if hasattr(self, "split_input_params"):
            if self.split_input_params["patch_distributed_vq"]:
                ks = self.split_input_params["ks"]  # eg. (128, 128)
                stride = self.split_input_params["stride"]  # eg. (64, 64)
                df = self.split_input_params["vqf"]
                self.split_input_params['original_image_size'] = x.shape[-2:]
                bs, nc, h, w = x.shape
                if ks[0] > h or ks[1] > w:
                    ks = (min(ks[0], h), min(ks[1], w))
                    logging.info("reducing Kernel")

                if stride[0] > h or stride[1] > w:
                    stride = (min(stride[0], h), min(stride[1], w))
                    logging.info("reducing stride")

                fold, unfold, normalization, weighting = self.get_fold_unfold(x, ks, stride, df=df)
                z = unfold(x)  # (bn, nc * prod(**ks), L)
                # Reshape to img shape
                z = z.view((z.shape[0], -1, ks[0], ks[1], z.shape[-1]))  # (bn, nc, ks[0], ks[1], L )

                output_list = [self.first_stage_model.encode(z[:, :, :, :, i]) for i in range(z.shape[-1])]

                o = torch.stack(output_list, axis=-1)
                o = o * weighting

                # Reverse reshape to img shape
                o = o.view((o.shape[0], -1, o.shape[-1]))  # (bn, nc * ks[0] * ks[1], L)
                # stitch crops together
                decoded = fold(o)
                decoded = decoded / normalization
                return decoded

            else:
                return self.first_stage_model.encode(x)
        else:
            return self.first_stage_model.encode(x)

    def shared_step(self, batch, **kwargs):
        x, c = self.get_input(batch, self.first_stage_key)
        loss = self(x, c)
        return loss

    def forward(self, x, c, *args, **kwargs):
        t = torch.randint(0, self.num_timesteps, (x.shape[0],), generator=self.rng, device=x.device).long()
        if self.model.conditioning_key is not None:
            assert c is not None
            if self.cond_stage_trainable:
                c = self.get_learned_conditioning(c)
            if self.shorten_cond_schedule:  # TODO: drop this option
                tc = self.cond_ids[t]
                c = self.q_sample(x_start=c, t=tc, noise=randn_like(c.float(), generator=self.rng))
        return self.p_losses(x, c, t, *args, **kwargs)

    def _rescale_annotations(self, bboxes, crop_coordinates):  # TODO: move to dataset
        def rescale_bbox(bbox):
            x0 = clamp((bbox[0] - crop_coordinates[0]) / crop_coordinates[2])
            y0 = clamp((bbox[1] - crop_coordinates[1]) / crop_coordinates[3])
            w = min(bbox[2] / crop_coordinates[2], 1 - x0)
            h = min(bbox[3] / crop_coordinates[3], 1 - y0)
            return x0, y0, w, h

        return [rescale_bbox(b) for b in bboxes]

    def apply_model(self, x_noisy, t, cond, return_ids=False):

        if isinstance(cond, dict):
            # hybrid case, cond is exptected to be a dict
            for key in cond:
                if not isinstance(cond[key], list):
                    cond[key] = [cond[key]]
        else:
            if not isinstance(cond, list):
                cond = [cond]
            key = 'c_concat' if self.model.conditioning_key == 'concat' else 'c_crossattn'
            cond = {key: cond}

        if hasattr(self, "split_input_params"):
            assert len(cond) == 1  # todo can only deal with one conditioning atm
            assert not return_ids
            ks = self.split_input_params["ks"]  # eg. (128, 128)
            stride = self.split_input_params["stride"]  # eg. (64, 64)

            h, w = x_noisy.shape[-2:]

            fold, unfold, normalization, weighting = self.get_fold_unfold(x_noisy, ks, stride)

            z = unfold(x_noisy)  # (bn, nc * prod(**ks), L)
            # Reshape to img shape
            z = z.view((z.shape[0], -1, ks[0], ks[1], z.shape[-1]))  # (bn, nc, ks[0], ks[1], L )
            z_list = [z[:, :, :, :, i] for i in range(z.shape[-1])]

            if (
                self.cond_stage_key in ["image", "LR_image", "segmentation", 'bbox_img']
                and self.model.conditioning_key
            ):  # todo check for completeness
                c_key = next(iter(cond.keys()))  # get key
                c = next(iter(cond.values()))  # get value
                assert len(c) == 1  # todo extend to list with more than one elem
                c = c[0]  # get element

                c = unfold(c)
                c = c.view((c.shape[0], -1, ks[0], ks[1], c.shape[-1]))  # (bn, nc, ks[0], ks[1], L )

                cond_list = [{c_key: [c[:, :, :, :, i]]} for i in range(c.shape[-1])]

            elif self.cond_stage_key == 'coordinates_bbox':
                assert (
                    'original_image_size' in self.split_input_params
                ), 'BoudingBoxRescaling is missing original_image_size'

                # assuming padding of unfold is always 0 and its dilation is always 1
                n_patches_per_row = int((w - ks[0]) / stride[0] + 1)
                full_img_h, full_img_w = self.split_input_params['original_image_size']
                # as we are operating on latents, we need the factor from the original image size to the
                # spatial latent size to properly rescale the crops for regenerating the bbox annotations
                num_downs = self.first_stage_model.encoder.num_resolutions - 1
                rescale_latent = 2 ** (num_downs)

                # get top left postions of patches as conforming for the bbbox tokenizer, therefore we
                # need to rescale the tl patch coordinates to be in between (0,1)
                tl_patch_coordinates = [
                    (
                        rescale_latent * stride[0] * (patch_nr % n_patches_per_row) / full_img_w,
                        rescale_latent * stride[1] * (patch_nr // n_patches_per_row) / full_img_h,
                    )
                    for patch_nr in range(z.shape[-1])
                ]

                # patch_limits are tl_coord, width and height coordinates as (x_tl, y_tl, h, w)
                patch_limits = [
                    (x_tl, y_tl, rescale_latent * ks[0] / full_img_w, rescale_latent * ks[1] / full_img_h)
                    for x_tl, y_tl in tl_patch_coordinates
                ]
                # patch_values = [(np.arange(x_tl,min(x_tl+ks, 1.)),np.arange(y_tl,min(y_tl+ks, 1.))) for x_tl, y_tl in tl_patch_coordinates]

                # tokenize crop coordinates for the bounding boxes of the respective patches
                patch_limits_tknzd = [
                    torch.LongTensor(self.bbox_tokenizer._crop_encoder(bbox))[None] for bbox in patch_limits
                ]  # list of length l with tensors of shape (1, 2)
                logging.info(patch_limits_tknzd[0].shape)
                # cut tknzd crop position from conditioning
                assert isinstance(cond, dict), 'cond must be dict to be fed into model'
                cut_cond = cond['c_crossattn'][0][..., :-2]
                logging.info(cut_cond.shape)

                adapted_cond = torch.stack([torch.cat([cut_cond, p], dim=1) for p in patch_limits_tknzd])
                adapted_cond = rearrange(adapted_cond, 'l b n -> (l b) n')
                logging.info(adapted_cond.shape)
                adapted_cond = self.get_learned_conditioning(adapted_cond)
                logging.info(adapted_cond.shape)
                adapted_cond = rearrange(adapted_cond, '(l b) n d -> l b n d', l=z.shape[-1])
                logging.info(adapted_cond.shape)

                cond_list = [{'c_crossattn': [e]} for e in adapted_cond]

            else:
                cond_list = [cond for i in range(z.shape[-1])]  # Todo make this more efficient

            # apply model by loop over crops
            output_list = [self.model(z_list[i], t, **cond_list[i]) for i in range(z.shape[-1])]
            assert not isinstance(
                output_list[0], tuple
            )  # todo cant deal with multiple model outputs check this never happens

            o = torch.stack(output_list, axis=-1)
            o = o * weighting
            # Reverse reshape to img shape
            o = o.view((o.shape[0], -1, o.shape[-1]))  # (bn, nc * ks[0] * ks[1], L)
            # stitch crops together
            x_recon = fold(o) / normalization

        else:
            x_recon = self.model(x_noisy, t, **cond)

        if isinstance(x_recon, tuple) and not return_ids:
            return x_recon[0]
        else:
            return x_recon

    def _predict_eps_from_xstart(self, x_t, t, pred_xstart):
        return (
            extract_into_tensor(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t - pred_xstart
        ) / extract_into_tensor(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape)

    def _prior_bpd(self, x_start):
        """
        Get the prior KL term for the variational lower-bound, measured in
        bits-per-dim.
        This term can't be optimized, as it only depends on the encoder.
        :param x_start: the [N x C x ...] tensor of inputs.
        :return: a batch of [N] KL values (in bits), one per batch element.
        """
        batch_size = x_start.shape[0]
        t = torch.tensor([self.num_timesteps - 1] * batch_size, device=x_start.device)
        qt_mean, _, qt_log_variance = self.q_mean_variance(x_start, t)
        kl_prior = normal_kl(mean1=qt_mean, logvar1=qt_log_variance, mean2=0.0, logvar2=0.0)
        return mean_flat(kl_prior) / np.log(2.0)

    def p_losses(self, x_start, cond, t, noise=None):
        noise = default(noise, lambda: randn_like(x_start, generator=self.rng))
        x_noisy = self.q_sample(x_start=x_start, t=t, noise=noise)
        model_output = self.apply_model(x_noisy, t, cond)

        loss_dict = {}
        prefix = 'train' if self.training else 'val'

        if self.parameterization == "x0":
            target = x_start
        elif self.parameterization == "eps":
            target = noise
        elif self.parameterization == "v":
            target = self.get_v(x_start, noise, t)
        else:
            raise NotImplementedError()

        if (self.precision == 'bf16') or (int(self.precision) == 16):
            model_output = model_output.type(torch.float32)
        loss_simple = self.get_loss(model_output, target, mean=False).mean([1, 2, 3])
        loss_dict.update({f'{prefix}/loss_simple': loss_simple.mean()})
        self.logvar = self.logvar.cuda(non_blocking=True)
        logvar_t = self.logvar[t].cuda(non_blocking=True)
        loss = loss_simple / torch.exp(logvar_t) + logvar_t
        # loss = loss_simple / torch.exp(self.logvar) + self.logvar
        if self.learn_logvar:
            loss_dict.update({f'{prefix}/loss_gamma': loss.mean()})
            loss_dict.update({'logvar': self.logvar.data.mean()})

        loss = self.l_simple_weight * loss.mean()

        loss_vlb = self.get_loss(model_output, target, mean=False).mean(dim=(1, 2, 3))
        loss_vlb = (self.lvlb_weights[t] * loss_vlb).mean()
        loss_dict.update({f'{prefix}/loss_vlb': loss_vlb})
        loss += self.original_elbo_weight * loss_vlb
        loss_dict.update({f'{prefix}/loss': loss})

        return loss, loss_dict

    def p_mean_variance(
        self,
        x,
        c,
        t,
        clip_denoised: bool,
        return_codebook_ids=False,
        quantize_denoised=False,
        return_x0=False,
        score_corrector=None,
        corrector_kwargs=None,
    ):
        t_in = t
        model_out = self.apply_model(x, t_in, c, return_ids=return_codebook_ids)

        if score_corrector is not None:
            assert self.parameterization == "eps"
            model_out = score_corrector.modify_score(self, model_out, x, t, c, **corrector_kwargs)

        if return_codebook_ids:
            model_out, logits = model_out

        if self.parameterization == "eps":
            x_recon = self.predict_start_from_noise(x, t=t, noise=model_out)
        elif self.parameterization == "x0":
            x_recon = model_out
        else:
            raise NotImplementedError()

        if clip_denoised:
            x_recon.clamp_(-1.0, 1.0)
        if quantize_denoised:
            x_recon, _, [_, _, indices] = self.first_stage_model.quantize(x_recon)
        model_mean, posterior_variance, posterior_log_variance = self.q_posterior(x_start=x_recon, x_t=x, t=t)
        if return_codebook_ids:
            return model_mean, posterior_variance, posterior_log_variance, logits
        elif return_x0:
            return model_mean, posterior_variance, posterior_log_variance, x_recon
        else:
            return model_mean, posterior_variance, posterior_log_variance

    @torch.no_grad()
    def p_sample(
        self,
        x,
        c,
        t,
        clip_denoised=False,
        repeat_noise=False,
        return_codebook_ids=False,
        quantize_denoised=False,
        return_x0=False,
        temperature=1.0,
        noise_dropout=0.0,
        score_corrector=None,
        corrector_kwargs=None,
    ):
        b, *_, device = *x.shape, x.device
        outputs = self.p_mean_variance(
            x=x,
            c=c,
            t=t,
            clip_denoised=clip_denoised,
            return_codebook_ids=return_codebook_ids,
            quantize_denoised=quantize_denoised,
            return_x0=return_x0,
            score_corrector=score_corrector,
            corrector_kwargs=corrector_kwargs,
        )
        if return_codebook_ids:
            raise DeprecationWarning("Support dropped.")
            model_mean, _, model_log_variance, logits = outputs
        elif return_x0:
            model_mean, _, model_log_variance, x0 = outputs
        else:
            model_mean, _, model_log_variance = outputs

        noise = noise_like(x.shape, device, repeat_noise) * temperature
        if noise_dropout > 0.0:
            noise = torch.nn.functional.dropout(noise, p=noise_dropout)
        # no noise when t == 0
        nonzero_mask = (1 - (t == 0).float()).reshape(b, *((1,) * (len(x.shape) - 1)))

        if return_codebook_ids:
            return model_mean + nonzero_mask * (0.5 * model_log_variance).exp() * noise, logits.argmax(dim=1)
        if return_x0:
            return model_mean + nonzero_mask * (0.5 * model_log_variance).exp() * noise, x0
        else:
            return model_mean + nonzero_mask * (0.5 * model_log_variance).exp() * noise

    @torch.no_grad()
    def progressive_denoising(
        self,
        cond,
        shape,
        verbose=True,
        callback=None,
        quantize_denoised=False,
        img_callback=None,
        mask=None,
        x0=None,
        temperature=1.0,
        noise_dropout=0.0,
        score_corrector=None,
        corrector_kwargs=None,
        batch_size=None,
        x_T=None,
        start_T=None,
        log_every_t=None,
    ):
        if not log_every_t:
            log_every_t = self.log_every_t
        timesteps = self.num_timesteps
        if batch_size is not None:
            b = batch_size if batch_size is not None else shape[0]
            shape = [batch_size] + list(shape)
        else:
            b = batch_size = shape[0]
        if x_T is None:
            img = torch.randn(shape, generator=self.rng, device=torch.cuda.current_device())
        else:
            img = x_T
        intermediates = []
        if cond is not None:
            if isinstance(cond, dict):
                cond = {
                    key: cond[key][:batch_size]
                    if not isinstance(cond[key], list)
                    else list(map(lambda x: x[:batch_size], cond[key]))
                    for key in cond
                }
            else:
                cond = [c[:batch_size] for c in cond] if isinstance(cond, list) else cond[:batch_size]

        if start_T is not None:
            timesteps = min(timesteps, start_T)
        iterator = (
            tqdm(reversed(range(0, timesteps)), desc='Progressive Generation', total=timesteps)
            if verbose
            else reversed(range(0, timesteps))
        )
        if type(temperature) == float:
            temperature = [temperature] * timesteps

        for i in iterator:
            ts = torch.full((b,), i, device=torch.cuda.current_device(), dtype=torch.long)
            if self.shorten_cond_schedule:
                assert self.model.conditioning_key != 'hybrid'
                tc = self.cond_ids[ts].to(cond.device)
                cond = self.q_sample(x_start=cond, t=tc, noise=randn_like(cond, generator=self.rng))

            img, x0_partial = self.p_sample(
                img,
                cond,
                ts,
                clip_denoised=self.clip_denoised,
                quantize_denoised=quantize_denoised,
                return_x0=True,
                temperature=temperature[i],
                noise_dropout=noise_dropout,
                score_corrector=score_corrector,
                corrector_kwargs=corrector_kwargs,
            )
            if mask is not None:
                assert x0 is not None
                img_orig = self.q_sample(x0, ts)
                img = img_orig * mask + (1.0 - mask) * img

            if i % log_every_t == 0 or i == timesteps - 1:
                intermediates.append(x0_partial)
            if callback:
                callback(i)
            if img_callback:
                img_callback(img, i)
        return img, intermediates

    @torch.no_grad()
    def p_sample_loop(
        self,
        cond,
        shape,
        return_intermediates=False,
        x_T=None,
        verbose=True,
        callback=None,
        timesteps=None,
        quantize_denoised=False,
        mask=None,
        x0=None,
        img_callback=None,
        start_T=None,
        log_every_t=None,
    ):

        if not log_every_t:
            log_every_t = self.log_every_t
        device = self.betas.device
        b = shape[0]
        if x_T is None:
            img = torch.randn(shape, generator=self.rng, device=device)
        else:
            img = x_T

        intermediates = [img]
        if timesteps is None:
            timesteps = self.num_timesteps

        if start_T is not None:
            timesteps = min(timesteps, start_T)
        iterator = (
            tqdm(reversed(range(0, timesteps)), desc='Sampling t', total=timesteps)
            if verbose
            else reversed(range(0, timesteps))
        )

        if mask is not None:
            assert x0 is not None
            assert x0.shape[2:3] == mask.shape[2:3]  # spatial size has to match

        for i in iterator:
            ts = torch.full((b,), i, device=device, dtype=torch.long)
            if self.shorten_cond_schedule:
                assert self.model.conditioning_key != 'hybrid'
                tc = self.cond_ids[ts].to(cond.device)
                cond = self.q_sample(x_start=cond, t=tc, noise=randn_like(cond, generator=self.rng))

            img = self.p_sample(img, cond, ts, clip_denoised=self.clip_denoised, quantize_denoised=quantize_denoised)
            if mask is not None:
                img_orig = self.q_sample(x0, ts)
                img = img_orig * mask + (1.0 - mask) * img

            if i % log_every_t == 0 or i == timesteps - 1:
                intermediates.append(img)
            if callback:
                callback(i)
            if img_callback:
                img_callback(img, i)

        if return_intermediates:
            return img, intermediates
        return img

    @torch.no_grad()
    def sample(
        self,
        cond,
        batch_size=16,
        return_intermediates=False,
        x_T=None,
        verbose=True,
        timesteps=None,
        quantize_denoised=False,
        mask=None,
        x0=None,
        shape=None,
        **kwargs,
    ):
        if shape is None:
            shape = (batch_size, self.channels, self.image_size, self.image_size)
        if cond is not None:
            if isinstance(cond, dict):
                cond = {
                    key: cond[key][:batch_size]
                    if not isinstance(cond[key], list)
                    else list(map(lambda x: x[:batch_size], cond[key]))
                    for key in cond
                }
            else:
                cond = [c[:batch_size] for c in cond] if isinstance(cond, list) else cond[:batch_size]
        return self.p_sample_loop(
            cond,
            shape,
            return_intermediates=return_intermediates,
            x_T=x_T,
            verbose=verbose,
            timesteps=timesteps,
            quantize_denoised=quantize_denoised,
            mask=mask,
            x0=x0,
        )

    @torch.no_grad()
    def sample_log(self, cond, batch_size, ddim, ddim_steps, **kwargs):

        if ddim:
            ddim_sampler = DDIMSampler(self)
            shape = (self.channels, self.image_size, self.image_size)
            samples, intermediates = ddim_sampler.sample(ddim_steps, batch_size, shape, cond, verbose=False, **kwargs)

        else:
            samples, intermediates = self.sample(cond=cond, batch_size=batch_size, return_intermediates=True, **kwargs)

        return samples, intermediates

    @torch.no_grad()
    def log_images(
        self,
        batch,
        N=8,
        n_row=4,
        sample=True,
        ddim_steps=200,
        ddim_eta=1.0,
        return_keys=None,
        quantize_denoised=True,
        inpaint=True,
        plot_denoise_rows=False,
        plot_progressive_rows=True,
        plot_diffusion_rows=True,
        **kwargs,
    ):

        use_ddim = ddim_steps is not None

        log = dict()
        z, c, x, xrec, xc = self.get_input(
            batch,
            self.first_stage_key,
            return_first_stage_outputs=True,
            force_c_encode=True,
            return_original_cond=True,
            bs=N,
        )
        N = min(x.shape[0], N)
        n_row = min(x.shape[0], n_row)
        log["inputs"] = x
        log["reconstruction"] = xrec
        if self.model.conditioning_key is not None:
            if hasattr(self.cond_stage_model, "decode"):
                xc = self.cond_stage_model.decode(c)
                log["conditioning"] = xc
            elif self.cond_stage_key in ["caption"]:
                xc = log_txt_as_img((x.shape[2], x.shape[3]), batch["caption"])
                log["conditioning"] = xc
            elif self.cond_stage_key == 'class_label':
                xc = log_txt_as_img((x.shape[2], x.shape[3]), batch["human_label"])
                log['conditioning'] = xc
            elif isimage(xc):
                log["conditioning"] = xc
            if ismap(xc):
                log["original_conditioning"] = self.to_rgb(xc)

        if plot_diffusion_rows:
            # get diffusion row
            diffusion_row = list()
            z_start = z[:n_row]
            for t in range(self.num_timesteps):
                if t % self.log_every_t == 0 or t == self.num_timesteps - 1:
                    t = repeat(torch.tensor([t]), '1 -> b', b=n_row)
                    t = t.long()
                    noise = randn_like(z_start, generator=self.rng)
                    z_noisy = self.q_sample(x_start=z_start, t=t, noise=noise)
                    diffusion_row.append(self.decode_first_stage(z_noisy))

            diffusion_row = torch.stack(diffusion_row)  # n_log_step, n_row, C, H, W
            diffusion_grid = rearrange(diffusion_row, 'n b c h w -> b n c h w')
            diffusion_grid = rearrange(diffusion_grid, 'b n c h w -> (b n) c h w')
            diffusion_grid = make_grid(diffusion_grid, nrow=diffusion_row.shape[0])
            log["diffusion_row"] = diffusion_grid

        if sample:
            # get denoise row
            with self.ema_scope("Plotting"):
                samples, z_denoise_row = self.sample_log(
                    cond=c, batch_size=N, ddim=use_ddim, ddim_steps=ddim_steps, eta=ddim_eta
                )
                # samples, z_denoise_row = self.sample(cond=c, batch_size=N, return_intermediates=True)
            x_samples = self.decode_first_stage(samples)
            log["samples"] = x_samples
            if plot_denoise_rows:
                denoise_grid = self._get_denoise_row_from_list(z_denoise_row)
                log["denoise_row"] = denoise_grid

            if (
                quantize_denoised
                and not isinstance(self.first_stage_model, AutoencoderKL)
                and not isinstance(self.first_stage_model, IdentityFirstStage)
            ):
                # also display when quantizing x0 while sampling
                with self.ema_scope("Plotting Quantized Denoised"):
                    samples, z_denoise_row = self.sample_log(
                        cond=c,
                        batch_size=N,
                        ddim=use_ddim,
                        ddim_steps=ddim_steps,
                        eta=ddim_eta,
                        quantize_denoised=True,
                    )
                    # samples, z_denoise_row = self.sample(cond=c, batch_size=N, return_intermediates=True,
                    #                                      quantize_denoised=True)
                x_samples = self.decode_first_stage(samples)
                log["samples_x0_quantized"] = x_samples

            if inpaint:
                # make a simple center square
                b, h, w = z.shape[0], z.shape[2], z.shape[3]
                mask = torch.ones(N, h, w)
                # zeros will be filled in
                mask[:, h // 4 : 3 * h // 4, w // 4 : 3 * w // 4] = 0.0
                mask = mask[:, None, ...]
                with self.ema_scope("Plotting Inpaint"):
                    samples, _ = self.sample_log(
                        cond=c, batch_size=N, ddim=use_ddim, eta=ddim_eta, ddim_steps=ddim_steps, x0=z[:N], mask=mask
                    )
                x_samples = self.decode_first_stage(samples)
                log["samples_inpainting"] = x_samples
                log["mask"] = mask

                # outpaint
                with self.ema_scope("Plotting Outpaint"):
                    samples, _ = self.sample_log(
                        cond=c, batch_size=N, ddim=use_ddim, eta=ddim_eta, ddim_steps=ddim_steps, x0=z[:N], mask=mask
                    )
                x_samples = self.decode_first_stage(samples)
                log["samples_outpainting"] = x_samples

        if plot_progressive_rows:
            with self.ema_scope("Plotting Progressives"):
                img, progressives = self.progressive_denoising(
                    c, shape=(self.channels, self.image_size, self.image_size), batch_size=N
                )
            prog_row = self._get_denoise_row_from_list(progressives, desc="Progressive Generation")
            log["progressive_row"] = prog_row

        if return_keys:
            if np.intersect1d(list(log.keys()), return_keys).shape[0] == 0:
                return log
            else:
                return {key: log[key] for key in return_keys}
        return log

    def parameters(self):
        params = list(self.model.parameters())
        if self.cond_stage_trainable:
            logging.info(f"{self.__class__.__name__}: Also optimizing conditioner params!")
            params = params + list(self.cond_stage_model.parameters())
        if self.learn_logvar:
            logging.info('Diffusion model optimizing logvar')
            params.append(self.logvar)
        return params

    @torch.no_grad()
    def to_rgb(self, x):
        x = x.float()
        if not hasattr(self, "colorize"):
            self.colorize = torch.randn(3, x.shape[1], 1, 1, generator=self.rng).to(x)
        x = nn.functional.conv2d(x, weight=self.colorize)
        x = 2.0 * (x - x.min()) / (x.max() - x.min()) - 1.0
        return x

    def set_input_tensor(self, input_tensor):
        """See megatron.model.transformer.set_input_tensor()"""
        # only required for pipeline parallelism
        pass


class MegatronLatentDiffusion(MegatronMultimodalModel):
    """Megatron LatentDiffusion Model."""

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        if not HAVE_APEX:
            raise ImportError(
                "Apex was not found. Please see the NeMo README for installation instructions: https://github.com/NVIDIA/NeMo#megatron-gpt."
            )
        if not HAVE_MEGATRON_CORE:
            raise ImportError(
                "megatron-core was not found. Please see the NeMo README for installation instructions: https://github.com/NVIDIA/NeMo#megatron-gpt."
            )

        # this prevents base constructor from initializing tokenizer
        self.tokenizer = None
        super().__init__(cfg, trainer=trainer)

        self._validate_trainer()

        # megatron_amp_O2 is not yet supported in diffusion models
        self.megatron_amp_O2 = cfg.get('megatron_amp_O2', False)

        self.model = self.model_provider_func()

        self.conditioning_keys = []

        if self.trainer.precision == 'bf16':
            self.autocast_dtype = torch.bfloat16
        elif int(self.trainer.precision) == 32:
            self.autocast_dtype = torch.float
        elif int(self.trainer.precision) == 16:
            self.autocast_dtype = torch.half
        else:
            raise ValueError('precision must be in [32, 16, "bf16"]')

    def get_module_list(self):
        if isinstance(self.model, list):
            return [model.module if isinstance(model, Float16Module) else model for model in self.model]
        elif isinstance(self.model, Float16Module):
            return [self.model.module]
        else:
            return [self.model]

    def model_provider_func(self, pre_process=True, post_process=True):
        """Model depends on pipeline paralellism."""
        model = LatentDiffusion(cfg=self.cfg)
        return model

    def forward(self, x, c, *args, **kwargs):
        output_tensor = self.model(x, c, *args, **kwargs)
        return output_tensor

    @rank_zero_only
    @torch.no_grad()
    def on_train_batch_start(self, batch, batch_idx, dataloader_idx=0):
        if self.cfg.scale_by_std and self.current_epoch == 0 and self.global_step == 0 and batch_idx == 0:
            assert self.cfg.scale_factor == 1.0, 'rather not use custom rescaling and std-rescaling simultaneously'
            batch[self.cfg.first_stage_key] = batch[self.cfg.first_stage_key].cuda(non_blocking=True)
            self.model.on_train_batch_start(batch, batch_idx)

    def training_step(self, dataloader_iter, batch_idx):
        """
            Our dataloaders produce a micro-batch and then we fetch
            a number of microbatches depending on the global batch size and model parallel size
            from the dataloader to produce a list of microbatches.
            Batch should be a list of microbatches and those microbatches should on CPU.
            Microbatches are then moved to GPU during the pipeline.
            The list of microbatches is then piped through the pipeline using Apex fwd/bwd functions.
        """
        tensor_shape = None  # Placeholder

        # we zero grads here because we also call backward in the megatron-core fwd/bwd functions
        self._optimizer.zero_grad()

        # run forward and backwards passes for an entire global batch
        # we do this inside training_step to support pipeline parallelism
        fwd_bwd_function = get_forward_backward_func()

        losses_reduced_per_micro_batch = fwd_bwd_function(
            forward_step_func=self.get_forward_output_and_loss_func(),
            data_iterator=dataloader_iter,
            model=[self.model],
            num_microbatches=get_num_microbatches(),
            forward_only=False,
            tensor_shape=tensor_shape,
            dtype=self.autocast_dtype,
            grad_scaler=self.trainer.precision_plugin.scaler.scale if self.cfg.precision == 16 else None,
            sequence_parallel=self.cfg.get('sequence_parallel', False),
            enable_autocast=True,
        )

        # losses_reduced_per_micro_batch is a list of dictionaries
        # [{"loss": 0.1}, {"loss": 0.2}, ...] which are from gradient accumulation steps
        # only the last stages of the pipeline return losses
        loss_dict = {}
        if losses_reduced_per_micro_batch:
            # average loss across micro batches
            for key in losses_reduced_per_micro_batch[0]:
                loss_tensors_list = [loss_reduced[key] for loss_reduced in losses_reduced_per_micro_batch]
                loss_tensor = torch.stack(loss_tensors_list)
                loss_dict[key] = loss_tensor.mean()
            loss_mean = loss_dict["train/loss"]
        else:
            loss_mean = torch.tensor(0.0, device=torch.cuda.current_device())

        torch.distributed.broadcast(loss_mean, get_last_rank())

        # when using sequence parallelism, the sequence parallel layernorm grads must be all-reduced
        if self.cfg.get('tensor_model_parallel_size', 1) > 1 and self.cfg.get('sequence_parallel', False):
            self.allreduce_sequence_parallel_gradients()

        if self.with_distributed_adam:
            # gradients are reduced internally in distributed optimizer
            pass
        elif self.megatron_amp_O2:
            # # when using pipeline parallelism grads must be all-reduced after the pipeline (not asynchronously)
            # if self.cfg.get('pipeline_model_parallel_size', 1) > 1 or self.cfg.get('sequence_parallel', False):
            #     # main grads are stored in the MainParamsOptimizer wrapper
            #     self._optimizer.allreduce_main_grads()
            self._optimizer.allreduce_main_grads()
        else:
            # async grad allreduce is not currently implemented for O1/autocasting mixed precision training
            # so we all-reduce gradients after the pipeline
            self.allreduce_gradients()  # @sangkug we think this is causing memory to blow up (hurts perf)

        if self.cfg.precision == 16:
            loss_scale = self.trainer.precision_plugin.scaler._scale
            if loss_scale is not None:
                self.log('loss_scale', loss_scale, batch_size=1)

        self.log_dict(loss_dict, prog_bar=False, logger=True, on_step=True, rank_zero_only=True, batch_size=1)
        self.log('reduced_train_loss', loss_mean, prog_bar=False, rank_zero_only=True, batch_size=1)
        lr = self._optimizer.param_groups[0]['lr']
        self.log('lr', lr, prog_bar=True, rank_zero_only=True, batch_size=1)
        self.log('global_step', self.trainer.global_step + 1, prog_bar=True, rank_zero_only=True, batch_size=1)
        self.log(
            'consumed_samples',
            self.compute_consumed_samples(self.trainer.global_step + 1 - self.init_global_step),
            prog_bar=True,
            rank_zero_only=True,
            batch_size=1,
        )
        self.log("timestamp", int(time.time() * 1e3), batch_size=1, rank_zero_only=True)
        return loss_mean

    def backward(self, *args, **kwargs):
        """ LightningModule hook to do backward.
            We want this to do nothing since we run backward in the fwd/bwd functions from apex.
            No need to call it here.
        """
        pass

    def optimizer_zero_grad(self, *args, **kwargs):
        """ LightningModule hook to zero grad.
            We want this to do nothing as we are zeroing grads during the training_step.
        """
        pass

    def _append_sequence_parallel_module_grads(self, module, grads):
        """ Helper method for allreduce_sequence_parallel_gradients"""

        for param in module.parameters():
            sequence_parallel_param = getattr(param, 'sequence_parallel', False)
            if sequence_parallel_param and param.requires_grad:
                if self.megatron_amp_O2:
                    grad = param.main_grad
                else:
                    grad = param.grad
                grads.append(grad.data)

    def get_forward_output_and_loss_func(self):
        def process_batch(batch):
            """ Prepares the global batch for apex fwd/bwd functions.
                Global batch is a list of micro batches.
            """
            # noise_map, condition
            batch[self.cfg.first_stage_key] = batch[self.cfg.first_stage_key].cuda(non_blocking=True)
            if isinstance(batch[self.cfg.cond_stage_key], torch.Tensor):
                # in the case of precached text embeddings, cond_stage is also a tensor
                batch[self.cfg.cond_stage_key] = batch[self.cfg.cond_stage_key].cuda(non_blocking=True)

            # SD has more dedicated structure for encoding, so we enable autocasting here as well
            with torch.cuda.amp.autocast(
                self.autocast_dtype in (torch.half, torch.bfloat16), dtype=self.autocast_dtype,
            ):
                x, c = self.model.get_input(batch, self.cfg.first_stage_key)

            if not isinstance(c, dict):
                return [x, c]

            if len(self.conditioning_keys) == 0:
                self.conditioning_keys = list(c.keys())
            c_list = [c[key] for key in self.conditioning_keys]
            return [x, *c_list]

        def fwd_output_and_loss_func(dataloader_iter, model):
            batch = next(dataloader_iter)
            batch = process_batch(batch)
            batch = [x.cuda(non_blocking=True) for x in batch]
            if len(self.conditioning_keys) == 0:
                x, c = batch
            else:
                x = batch[0]
                c = {}
                for idx, key in enumerate(self.conditioning_keys):
                    c[key] = batch[1 + idx]
            loss, loss_dict = model(x, c)

            def dummy(output_tensor):
                return loss, loss_dict

            # output_tensor, and a function to convert output_tensor to loss + loss_dict
            return loss, dummy

        return fwd_output_and_loss_func

    def get_forward_output_only_func(self):
        def fwd_output_only_func(batch, model):
            raise NotImplementedError

        return fwd_output_only_func

    def validation_step(self, dataloader_iter, batch_idx):
        tensor_shape = None  # Placeholder
        fwd_bwd_function = get_forward_backward_func()

        losses_reduced_per_micro_batch = fwd_bwd_function(
            forward_step_func=self.get_forward_output_and_loss_func(),
            data_iterator=dataloader_iter,
            model=[self.model],
            num_microbatches=get_num_microbatches(),
            forward_only=True,
            tensor_shape=tensor_shape,
            dtype=self.autocast_dtype,
            sequence_parallel=self.cfg.get('sequence_parallel', False),
            enable_autocast=True,
        )

        # only the last stages of the pipeline return losses
        val_loss_dict = {}
        if losses_reduced_per_micro_batch:
            # average loss across micro batches
            for key in losses_reduced_per_micro_batch[0]:
                loss_tensors_list = [loss_reduced[key] for loss_reduced in losses_reduced_per_micro_batch]
                loss_tensor = torch.stack(loss_tensors_list)
                val_loss_dict[key] = loss_tensor.mean()

        self.log_dict(val_loss_dict, prog_bar=False, logger=True, on_step=False, on_epoch=True, batch_size=1)

    def setup(self, stage=None):
        """ PTL hook that is executed after DDP spawns.
            We setup datasets here as megatron datasets require DDP to instantiate.
            See https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#setup for more information.
        Args:
            stage (str, optional): Can be 'fit', 'validate', 'test' or 'predict'. Defaults to None.
        """
        self.model.rng.manual_seed(self.cfg.seed + 100 * parallel_state.get_data_parallel_rank())

        # log number of parameters
        if isinstance(self.model, list):
            num_parameters_on_device = sum(
                [sum([p.nelement() for p in model_module.parameters()]) for model_module in self.model]
            )
        else:
            num_parameters_on_device = sum([p.nelement() for p in self.model.parameters()])

        # to be summed across data parallel group
        total_num_parameters = torch.tensor(num_parameters_on_device).cuda(non_blocking=True)

        torch.distributed.all_reduce(total_num_parameters, group=parallel_state.get_model_parallel_group())

        logging.info(
            f'Pipeline model parallel rank: {parallel_state.get_pipeline_model_parallel_rank()}, '
            f'Tensor model parallel rank: {parallel_state.get_tensor_model_parallel_rank()}, '
            f'Number of model parameters on device: {num_parameters_on_device:.2e}. '
            f'Total number of model parameters: {total_num_parameters:.2e}.'
        )

        resume_checkpoint_path = self.trainer._checkpoint_connector.resume_from_checkpoint_fit_path
        if resume_checkpoint_path:
            init_consumed_samples = self._extract_consumed_samples_from_ckpt(resume_checkpoint_path)
        else:
            init_consumed_samples = 0
        self.init_consumed_samples = init_consumed_samples
        self.init_global_step = self.trainer.global_step

        # allowing restored models to optionally setup datasets
        self.build_train_valid_test_datasets()

        # Batch size need to be provided for webdatset
        self._num_micro_batches = get_num_microbatches()
        self._micro_batch_size = self.cfg.micro_batch_size

        self.setup_training_data(self.cfg.data)
        self.setup_validation_data(self.cfg.data)
        self.setup_test_data(self.cfg.data)

    def build_train_valid_test_datasets(self):
        logging.info('Building datasets for Stable Diffusion...')
        if self.trainer.limit_val_batches > 1.0 and isinstance(self.trainer.limit_val_batches, float):
            raise ValueError("limit_val_batches must be an integer or float less than or equal to 1.0.")

        if self.cfg.first_stage_key.endswith("encoded"):
            self._train_ds, self._validation_ds = build_train_valid_precached_datasets(
                model_cfg=self.cfg, consumed_samples=self.compute_consumed_samples(0),
            )
        elif self.cfg.first_stage_key.endswith("moments"):
            self._train_ds, self._validation_ds = build_train_valid_precached_datasets(
                model_cfg=self.cfg, consumed_samples=self.compute_consumed_samples(0),
            )
        else:
            self._train_ds, self._validation_ds = build_train_valid_datasets(
                model_cfg=self.cfg, consumed_samples=self.compute_consumed_samples(0)
            )
        self._test_ds = None

        if self._train_ds is not None:
            logging.info(f'Length of train dataset: {len(self._train_ds)}')
        if self._validation_ds is not None:
            logging.info(f'Length of val dataset: {len(self._validation_ds)}')
        if self._test_ds is not None:
            logging.info(f'Length of test dataset: {len(self._test_ds)}')
        logging.info(f'Finished building datasets for LatentDiffusion.')
        return self._train_ds, self._validation_ds, self._test_ds

    def setup_training_data(self, cfg):
        if hasattr(self, '_train_ds') and self._train_ds is not None:
            consumed_samples = self.compute_consumed_samples(0)
            logging.info(
                f'Setting up train dataloader with len(len(self._train_ds)): {len(self._train_ds)} and consumed samples: {consumed_samples}'
            )
            self._train_dl = torch.utils.data.DataLoader(
                self._train_ds,
                batch_size=self._micro_batch_size,
                num_workers=cfg.num_workers,
                pin_memory=True,
                drop_last=True,
                persistent_workers=True,
            )

    def setup_validation_data(self, cfg):
        if hasattr(self, '_validation_ds') and self._validation_ds is not None:
            consumed_samples = 0
            logging.info(
                f'Setting up validation dataloader with len(len(self._validation_ds)): {len(self._validation_ds)} and consumed samples: {consumed_samples}'
            )
            self._validation_dl = torch.utils.data.DataLoader(
                self._validation_ds,
                batch_size=self._micro_batch_size,
                num_workers=cfg.num_workers,
                pin_memory=True,
                drop_last=False,
                persistent_workers=True,
            )

    def setup_test_data(self, cfg):
        if hasattr(self, '_test_ds') and self._test_ds is not None:
            consumed_samples = 0
            logging.info(
                f'Setting up test dataloader with len(len(self._test_ds)): {len(self._test_ds)} and consumed samples: {consumed_samples}'
            )
            self._test_dl = torch.utils.data.DataLoader(
                self._test_ds, batch_size=self._micro_batch_size, num_workers=cfg.num_workers, pin_memory=True,
            )

    def transfer_batch_to_device(self, batch: Any, device: torch.device, dataloader_idx: int) -> Any:
        """ PTL hook: https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#transfer-batch-to-device
            When using pipeline parallelism, we need the global batch to remain on the CPU,
            since the memory overhead will be too high when using a large number of microbatches.
            Microbatches are transferred from CPU to GPU inside the pipeline.
        """
        return batch

    def _validate_trainer(self):
        """ Certain trainer configurations can break training.
            Here we try to catch them and raise an error.
        """
        if self.trainer.accumulate_grad_batches > 1:
            raise ValueError(
                f'Gradient accumulation is done within training_step. trainer.accumulate_grad_batches must equal 1'
            )

    @classmethod
    def list_available_models(cls):
        return None

    def parameters(self):
        if isinstance(self.model, list):
            return itertools.chain.from_iterable(module.parameters() for module in self.model)
        else:
            return self.model.parameters()

    def save_to(self, save_path: str):
        # Replace .nemo path in config for NeMo CLIP
        cfg = self._cfg
        if cfg.get('cond_stage_config').get('restore_from_path'):
            with open_dict(cfg):
                cfg.cond_stage_config.restore_from_path = None
                cfg.cond_stage_config.cfg = self.model.cond_stage_model.cfg
            self._cfg = cfg
        super().save_to(save_path)


class DiffusionWrapper(pl.LightningModule, Serialization):
    def __init__(
        self,
        diff_model_config,
        conditioning_key,
        inductor: bool = False,
        inductor_cudagraphs: bool = False,
        capture_cudagraph_iters: int = -1,
    ):
        super().__init__()
        self.diffusion_model = DiffusionWrapper.from_config_dict(diff_model_config)
        self.conditioning_key = conditioning_key
        assert self.conditioning_key in [None, 'concat', 'crossattn', 'hybrid', 'adm']

        # Fusing VAE and CLIP doesn't give benefit
        if inductor:
            # TorchInductor with CUDA graph can lead to OOM
            inductor_config.triton.cudagraphs = inductor_cudagraphs
            self.diffusion_model = torch.compile(self.diffusion_model)
        # CUDA graph
        self.capture_cudagraph_iters = capture_cudagraph_iters
        self.iterations = 0
        self.graphed_diffusion_model = None

    def forward(self, x, t, c_concat: list = None, c_crossattn: list = None):
        if self.conditioning_key is None:
            out = self.diffusion_model(x, t)
        elif self.conditioning_key == 'concat':
            xc = torch.cat([x] + c_concat, dim=1)
            out = self.diffusion_model(xc, t)
        elif self.conditioning_key == 'crossattn':
            cc = torch.cat(c_crossattn, 1)
            if self.iterations == self.capture_cudagraph_iters:
                logging.info("Capturing CUDA graph for module: %s", self.diffusion_model.__class__.__name__)
                self.graphed_diffusion_model = torch.cuda.make_graphed_callables(self.diffusion_model, (x, t, cc))

            if 0 <= self.capture_cudagraph_iters <= self.iterations:
                out = self.graphed_diffusion_model(x, t, cc)
            else:
                out = self.diffusion_model(x, t, context=cc)
            self.iterations += 1
        elif self.conditioning_key == 'hybrid':
            xc = torch.cat([x] + c_concat, dim=1)
            cc = torch.cat(c_crossattn, 1)
            out = self.diffusion_model(xc, t, context=cc)
        elif self.conditioning_key == 'adm':
            cc = c_crossattn[0]
            out = self.diffusion_model(x, t, y=cc)
        else:
            raise NotImplementedError()

        return out
