# Copyright 2022 Katherine Crowson and The HuggingFace Team. All rights reserved.
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

from dataclasses import dataclass
from typing import Optional, Tuple, Union

import numpy as np
import torch

from scipy import integrate

from ..configuration_utils import ConfigMixin, register_to_config
from ..utils import BaseOutput
from .scheduling_utils import BaseScheduler, SchedulerMixin


@dataclass
class LMSDiscreteSchedulerOutput(BaseOutput):
    """
    Output class for the scheduler's step function output.

    Args:
        prev_sample (`torch.FloatTensor` of shape `(batch_size, num_channels, height, width)` for images):
            Computed sample (x_{t-1}) of previous timestep. `prev_sample` should be used as next model input in the
            denoising loop.
        pred_original_sample (`torch.FloatTensor` of shape `(batch_size, num_channels, height, width)` for images):
            The predicted denoised sample (x_{0}) based on the model output from the current timestep.
            `pred_original_sample` can be used to preview progress or for guidance.
    """

    prev_sample: torch.FloatTensor
    pred_original_sample: Optional[torch.FloatTensor] = None


class LMSDiscreteScheduler(BaseScheduler, SchedulerMixin, ConfigMixin):
    """
    Linear Multistep Scheduler for discrete beta schedules. Based on the original k-diffusion implementation by
    Katherine Crowson:
    https://github.com/crowsonkb/k-diffusion/blob/481677d114f6ea445aa009cf5bd7a9cdee909e47/k_diffusion/sampling.py#L181

    [`~ConfigMixin`] takes care of storing all config attributes that are passed in the scheduler's `__init__`
    function, such as `num_train_timesteps`. They can be accessed via `scheduler.config.num_train_timesteps`.
    [`~ConfigMixin`] also provides general loading and saving functionality via the [`~ConfigMixin.save_config`] and
    [`~ConfigMixin.from_config`] functions.

    Args:
        num_train_timesteps (`int`): number of diffusion steps used to train the model.
        beta_start (`float`): the starting `beta` value of inference.
        beta_end (`float`): the final `beta` value.
        beta_schedule (`str`):
            the beta schedule, a mapping from a beta range to a sequence of betas for stepping the model. Choose from
            `linear` or `scaled_linear`.
        trained_betas (`np.ndarray`, optional):
            option to pass an array of betas directly to the constructor to bypass `beta_start`, `beta_end` etc.

    """

    @register_to_config
    def __init__(
        self,
        num_train_timesteps: int = 1000,
        beta_start: float = 0.0001,
        beta_end: float = 0.02,
        beta_schedule: str = "linear",
        trained_betas: Optional[np.ndarray] = None,
    ):
        if trained_betas is not None:
            self.betas = torch.from_numpy(trained_betas)
        if beta_schedule == "linear":
            self.betas = torch.linspace(beta_start, beta_end, num_train_timesteps, dtype=torch.float32)
        elif beta_schedule == "scaled_linear":
            # this schedule is very specific to the latent diffusion model.
            self.betas = (
                torch.linspace(beta_start**0.5, beta_end**0.5, num_train_timesteps, dtype=torch.float32) ** 2
            )
        else:
            raise NotImplementedError(f"{beta_schedule} does is not implemented for {self.__class__}")

        self.alphas = 1.0 - self.betas
        self.alphas_cumprod = torch.cumprod(self.alphas, dim=0)

        sigmas = ((1 - self.alphas_cumprod) / self.alphas_cumprod) ** 0.5
        sigmas = sigmas[::-1].copy()
        self.sigmas = np.concatenate([sigmas, [0.0]]).astype(np.float32)

        # setable values
        self.num_inference_steps = None
        self.timesteps = np.arange(0, num_train_timesteps)[::-1].copy()
        self.schedule = np.linspace(0, num_train_timesteps - 1, num_train_timesteps, dtype=float)
        self.derivatives = []

    def scale_initial_noise(self, noise: torch.FloatTensor):
        """
        Scales the initial noise to the correct range for the scheduler.
        """
        return noise * self.sigmas[0]

    def scale_model_input(self, sample: torch.FloatTensor, step: int):
        """
        Scales the model input (`sample`) to the correct range for the scheduler.
        """
        sigma = self.sigmas[self.num_inference_steps - step - 1]
        return sample / ((sigma**2 + 1) ** 0.5)

    def get_noise_condition(self, step: int):
        """
        Returns the input noise condition for a model.
        """
        return self.schedule[step]

    def get_lms_coefficient(self, order, t, current_order):
        """
        Compute a linear multistep coefficient.

        Args:
            order (TODO):
            t (TODO):
            current_order (TODO):
        """

        def lms_derivative(tau):
            prod = 1.0
            for k in range(order):
                if current_order == k:
                    continue
                prod *= (tau - self.sigmas[t - k]) / (self.sigmas[t - current_order] - self.sigmas[t - k])
            return prod

        integrated_coeff = integrate.quad(lms_derivative, self.sigmas[t], self.sigmas[t + 1], epsrel=1e-4)[0]

        return integrated_coeff

    def set_timesteps(self, num_inference_steps: int):
        """
        Sets the timesteps used for the diffusion chain. Supporting function to be run before inference.

        Args:
            num_inference_steps (`int`):
                the number of diffusion steps used when generating samples with a pre-trained model.
        """
        self.num_inference_steps = num_inference_steps
        timesteps = np.arange(0, num_inference_steps)[::-1].copy()

        self.schedule = np.linspace(0, self.config.num_train_timesteps - 1, num_inference_steps, dtype=float)
        sigmas = np.array(((1 - self.alphas_cumprod) / self.alphas_cumprod) ** 0.5)
        sigmas = np.interp(self.schedule[::-1], np.arange(0, len(sigmas)), sigmas)
        sigmas = np.concatenate([sigmas, [0.0]]).astype(np.float32)
        self.sigmas = torch.from_numpy(sigmas)

        self.timesteps = timesteps.astype(int)
        self.derivatives = []

    def step(
        self,
        model_output: torch.FloatTensor,
        timestep: int,
        sample: torch.FloatTensor,
        order: int = 4,
        return_dict: bool = True,
    ) -> Union[LMSDiscreteSchedulerOutput, Tuple]:
        """
        Predict the sample at the previous timestep by reversing the SDE. Core function to propagate the diffusion
        process from the learned model outputs (most often the predicted noise).

        Args:
            model_output (`torch.FloatTensor`): direct output from learned diffusion model.
            timestep (`int`): current discrete timestep in the diffusion chain.
            sample (`torch.FloatTensor`):
                current instance of sample being created by diffusion process.
            order: coefficient for multi-step inference.
            return_dict (`bool`): option for returning tuple rather than LMSDiscreteSchedulerOutput class

        Returns:
            [`~schedulers.scheduling_utils.LMSDiscreteSchedulerOutput`] or `tuple`:
            [`~schedulers.scheduling_utils.LMSDiscreteSchedulerOutput`] if `return_dict` is True, otherwise a `tuple`.
            When returning a tuple, the first element is the sample tensor.

        """
        # FIXME: accounting for the descending sigmas
        timestep = int(len(self.timesteps) - timestep - 1)
        sigma = self.sigmas[timestep]

        # 1. compute predicted original sample (x_0) from sigma-scaled predicted noise
        pred_original_sample = sample - sigma * model_output

        # 2. Convert to an ODE derivative
        derivative = (sample - pred_original_sample) / sigma
        self.derivatives.append(derivative)
        if len(self.derivatives) > order:
            self.derivatives.pop(0)

        # 3. Compute linear multistep coefficients
        order = min(timestep + 1, order)
        lms_coeffs = [self.get_lms_coefficient(order, timestep, curr_order) for curr_order in range(order)]

        # 4. Compute previous sample based on the derivatives path
        prev_sample = sample + sum(
            coeff * derivative for coeff, derivative in zip(lms_coeffs, reversed(self.derivatives))
        )

        if not return_dict:
            return (prev_sample,)

        return LMSDiscreteSchedulerOutput(prev_sample=prev_sample, pred_original_sample=pred_original_sample)

    def add_noise(
        self,
        original_samples: Union[torch.FloatTensor, np.ndarray],
        noise: Union[torch.FloatTensor, np.ndarray],
        timesteps: Union[torch.IntTensor, np.ndarray],
    ) -> Union[torch.FloatTensor, np.ndarray]:
        sigmas = self.sigmas.to(original_samples.device)
        timesteps = timesteps.to(original_samples.device)
        # FIXME: accounting for the descending sigmas
        timesteps = self.num_inference_steps - timesteps - 1

        sigmas = sigmas[timesteps].flatten()
        while len(sigmas.shape) < len(original_samples.shape):
            sigmas = sigmas.unsqueeze(-1)

        noisy_samples = original_samples + noise * sigmas
        return noisy_samples

    def __len__(self):
        return self.config.num_train_timesteps
