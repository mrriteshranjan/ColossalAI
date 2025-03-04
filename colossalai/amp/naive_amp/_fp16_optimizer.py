#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import torch
import torch.distributed as dist

try:
    import colossal_C
except:
    print('Colossalai should be built with cuda extension to use the FP16 optimizer')

from torch.optim import Optimizer
from colossalai.core import global_context as gpc
from colossalai.context import ParallelMode
from colossalai.logging import get_dist_logger
from colossalai.utils import (copy_tensor_parallel_attributes, clip_grad_norm_fp32, multi_tensor_applier)
from torch.distributed import ProcessGroup
from .grad_scaler import BaseGradScaler
from ._utils import has_inf_or_nan, zero_gard_by_list

__all__ = ['FP16Optimizer']


def _multi_tensor_copy_this_to_that(this, that, overflow_buf=None):
    """
    adapted from Megatron-LM (https://github.com/NVIDIA/Megatron-LM)

    Use multi-tensor-applier to copy values from one list to another.
    We don't have a blfoat16 implementation so for now if the overflow_buf
    is not provided, we default back to simple loop copy to be compatible
    with bfloat16.
    """
    if overflow_buf:
        overflow_buf.fill_(0)
        # Scaling with factor `1.0` is equivalent to copy.
        multi_tensor_applier(colossal_C.multi_tensor_scale, overflow_buf, [this, that], 1.0)
    else:
        for this_, that_ in zip(this, that):
            that_.copy_(this_)


class DynamicGradScaler:

    def __init__(self,
                 initial_scale,
                 min_scale,
                 growth_factor,
                 backoff_factor,
                 growth_interval,
                 hysteresis,
                 max_scale: int = None,
                 verbose: bool = False):
        """"Grad scaler with dynamic scale that gets adjusted
        during training."""
        assert initial_scale > 0.0
        self._scale = torch.cuda.FloatTensor([initial_scale])

        # Lower bound on the scale.
        assert min_scale > 0.0
        assert min_scale <= initial_scale
        self.min_scale = torch.cuda.FloatTensor([min_scale])
        # Growth and backoff factors for the scale.
        assert growth_factor > 1.0
        self.growth_factor = torch.cuda.FloatTensor([growth_factor])
        assert backoff_factor < 1.0
        assert backoff_factor > 0.0
        self.backoff_factor = torch.cuda.FloatTensor([backoff_factor])
        # Interval over which if we don't see any inf/nan,
        # we will scale the grad scale by the growth factor.
        assert growth_interval > 0
        self.growth_interval = growth_interval
        # Number of inf/nans we should see before scaling down
        # the grad scale by the backoff factor.
        assert hysteresis > 0
        self.hysteresis = hysteresis
        if max_scale is not None:
            assert max_scale > 1 and initial_scale <= max_scale
        self._max_scale = max_scale

        # Trackers.
        self._growth_tracker = 0
        self._hysteresis_tracker = self.hysteresis

        self._logger = get_dist_logger()
        self.verbose = verbose

    @property
    def scale(self):
        return self._scale

    @property
    def inv_scale(self):
        return self._scale.double().reciprocal().float()

    def update(self, found_inf):

        # If we have an inf/nan, growth tracker is set to 0
        # and hysterisis tracker is reduced by 1.
        if found_inf:
            self._growth_tracker = 0
            self._hysteresis_tracker -= 1
            # Now if we are out of hysteresis count, scale down the loss.
            if self._hysteresis_tracker <= 0:
                self._scale = torch.max(self._scale * self.backoff_factor, self.min_scale)
            if self.verbose:
                self._logger.info(f'overflow occurs, loss scale is adjusted to {self._scale}', ranks=[0])
        else:
            # If there is no nan/inf, increment the growth tracker.
            self._growth_tracker += 1
            # If we have had enough consequitive intervals with no nan/inf:
            if self._growth_tracker == self.growth_interval:
                # Reset the tracker and hysteresis trackers,
                self._growth_tracker = 0
                self._hysteresis_tracker = self.hysteresis
                # and scale up the loss scale.
                if self._max_scale is not None and self._scale >= self._max_scale:
                    if self.verbose:
                        self._logger.info(
                            f'Current loss scale {self._scale} has reached the max scale {self._max_scale} allowed',
                            ranks=[0])
                else:
                    self._scale = self._scale * self.growth_factor
                    if self.verbose:
                        self._logger.info(f'no consecutive overflow, loss scale is adjusted to {self._scale}',
                                          ranks=[0])

    def state_dict(self):
        state_dict = {}
        state_dict['max_scale'] = self._max_scale
        state_dict['scale'] = self._scale
        state_dict['growth_tracker'] = self._growth_tracker
        state_dict['hysteresis_tracker'] = self._hysteresis_tracker
        return state_dict

    def load_state_dict(self, state_dict):
        self._scale = state_dict['scale'].cuda(torch.cuda.current_device())
        self._growth_tracker = state_dict['growth_tracker']
        self._hysteresis_tracker = state_dict['hysteresis_tracker']
        self._max_scale = state_dict['max_scale']


class FP16Optimizer(Optimizer):
    """Float16 optimizer for fp16 and bf16 data types.

    :param optimizer: base optimizer such as Adam or SGD
    :type optimizer: torch.optim.Optimizer
    :param clip_grad: clip gradeints with this global L2 norm. Note that clipping is ignored if clip_grad == 0
    :type param clip_grad: float
    :param log_num_zeros_in_grad: return number of zeros in the gradients.
    :type log_num_zeros_in_grad: bool
    :param initial_scale: initial scale of gradient scaler
    :type initial_scale: int
    :param growth_factor: the growth rate of loss scale
    :type growth_factor: int
    :param backoff_factor: the decrease rate of loss scale
    :type backoff_factor: float
    :param hysterisis: delay shift in dynamic loss scaling
    :type hysterisis: int
    :param max_scale: maximum loss scale allowed
    :type max_scale: int
    :param verbose: if set to `True`, will print debug info
    :type verbose: bool
    """

    def __init__(self,
                 optimizer: Optimizer,
                 grad_scaler: BaseGradScaler,
                 verbose: bool = False,
                 clip_grad_norm=0,
                 dp_process_group: ProcessGroup = None,
                 mp_process_group: ProcessGroup = None):
        # have a defaults for compatibility with pytorch optim
        self._optimizer = optimizer
        self._defaults = optimizer.defaults

        # fp16-related params
        assert isinstance(grad_scaler, BaseGradScaler)
        self._grad_scaler = grad_scaler
        self._found_overflow = torch.cuda.FloatTensor([0.0])
        self._dummy_overflow_buf = torch.cuda.IntTensor([0])

        # misc params
        self._clip_grad_max_norm = clip_grad_norm

        # get process group
        def _get_process_group(parallel_mode):
            if gpc.is_initialized(ParallelMode.DATA) and gpc.get_world_size(ParallelMode.DATA):
                return gpc.get_group(ParallelMode.DATA)
            else:
                return None

        if dp_process_group is None:
            dp_process_group = _get_process_group(ParallelMode.DATA)
        if mp_process_group is None:
            mp_process_group = _get_process_group(ParallelMode.MODEL)

        self._dp_process_group = dp_process_group
        self._mp_process_group = mp_process_group

        # we maintain three groups of parameters
        # so that the model can have a mixture
        # of fp16 and fp32 params
        # fp16_param_groups: the fp16 params of the model
        # fp32_master_param_groups: the fp32 params cast from the fp16 param of the model
        # fp32_param_groups: the fp32 params of the model
        # NOTE:
        # 1. fp16_param_groups and fp32_master_param_groups have one-to-one correspondence
        # 2. fp32_param_groups and fp16_param_groups are exclusive of each other
        self._fp16_param_groups = []
        self._fp32_master_param_groups = []
        self._fp32_param_groups = []

        # For all the groups in the original optimizer:
        for param_group in self._optimizer.param_groups:
            fp16_params = []
            fp32_master_params = []
            fp32_params = []
            # For all the parameters in this group:
            for i, param in enumerate(param_group['params']):
                if param.requires_grad:
                    # float16 params:
                    if param.type() in ['torch.cuda.HalfTensor']:
                        fp16_params.append(param)

                        # Create a fp32 copy
                        fp32_param = param.detach().clone().float()
                        # Copy tensor model parallel attributes.
                        copy_tensor_parallel_attributes(param, fp32_param)

                        # Replace the optimizer params with the new fp32 copy.
                        param_group['params'][i] = fp32_param
                        fp32_master_params.append(fp32_param)

                        # Reset existing state dict key to the new main param.
                        if param in self._optimizer.state:
                            self._optimizer.state[fp32_param] = self._optimizer.state.pop(param)

                    # fp32 params.
                    elif param.type() == 'torch.cuda.FloatTensor':
                        fp32_params.append(param)
                    else:
                        raise TypeError('Expected parameter of type torch.cuda.FloatTensor '
                                        f'or torch.cuda.HalfTensor, but got {param.type()}')

            self._fp16_param_groups.append(fp16_params)
            self._fp32_master_param_groups.append(fp32_master_params)
            self._fp32_param_groups.append(fp32_params)

        # Leverage state_dict() and load_state_dict() to
        # recast preexisting per-param state tensors
        self._optimizer.load_state_dict(self._optimizer.state_dict())

        # log config
        self._logger = get_dist_logger()
        if verbose:
            self._logger.info(
                f"\n=========  FP16 Optimizer Config =========\n"
                f"Optimizer: {optimizer.__class__.__name__}\n"
                f"clip_grad_norm = {clip_grad_norm}\n"
                f"grad_scaler = {self._grad_scaler.__class__.__name__}"
                f"==========================================",
                ranks=[0])

    @property
    def grad_scaler(self):
        return self._grad_scaler

    @property
    def loss_scale(self):
        return self._grad_scaler.scale

    @property
    def optimizer(self):
        return self._optimizer

    @property
    def defaults(self):
        return self._defaults

    def _check_overflow(self):
        # clear previous overflow record
        self._found_overflow.fill_(0.0)

        # check for overflow
        for group in self._optimizer.param_groups:
            for p in group['params']:
                if has_inf_or_nan(p.grad):
                    self._found_overflow.fill_(1.0)
                    break

        # all-reduce across dp group
        if self._dp_process_group:
            dist.all_reduce(self._found_overflow, op=dist.ReduceOp.MAX, group=self._dp_process_group)

        # all-reduce over model parallel group
        if self._mp_process_group:
            dist.all_reduce(self._found_overflow, op=dist.ReduceOp.MAX, group=self._mp_process_group)

        return self._found_overflow.item() > 0

    def zero_grad(self, set_to_none=True):
        # set_to_none = True can save some memory space
        for param_group in self._optimizer.param_groups:
            zero_gard_by_list(param_group['params'], set_to_none=set_to_none)

    def _get_fp32_param_groups_to_update(self):
        return self._fp32_master_param_groups + self._fp32_param_groups

    def _unscale_grads(self):
        for group in self._get_fp32_param_groups_to_update():
            for p in group:
                if p.grad is not None:
                    p.grad.data.div_(self.loss_scale)

    def _assign_grad_to_fp32_master_param(self):
        # This only needs to be done for the float16 group.
        for fp16_param_group, fp32_master_param_group in zip(self._fp16_param_groups, self._fp32_master_param_groups):
            for fp16_param, fp32_param in zip(fp16_param_group, fp32_master_param_group):
                fp32_param.grad = fp16_param.grad.float()
                # clear unneeded grad on fp16 param
                fp16_param.grad = None

    def _update_fp16_param_from_fp32_param(self):
        fp16_param_data = []
        fp32_master_param_data = []
        for fp16_group, fp32_group in zip(self._fp16_param_groups, self._fp32_master_param_groups):
            for fp16_param, fp32_param in zip(fp16_group, fp32_group):
                fp16_param_data.append(fp16_param.data)
                fp32_master_param_data.append(fp32_param.data)
        _multi_tensor_copy_this_to_that(this=fp32_master_param_data,
                                        that=fp16_param_data,
                                        overflow_buf=self._dummy_overflow_buf)

    def step(self):
        # Copy gradients from model params to main params.
        self._assign_grad_to_fp32_master_param()
        self._unscale_grads()

        overflow = self._check_overflow()
        self._grad_scaler.update(overflow)

        if overflow:
            self.zero_grad()
            return False, None

        # Clip the main gradients.
        grad_norm = None
        if self._clip_grad_max_norm > 0.0:
            grad_norm = self.clip_grad_norm(self._clip_grad_max_norm)

        # Step the optimizer.
        self._optimizer.step()

        # Update params from main params.
        self._update_fp16_param_from_fp32_param()

        # Successful update.
        return True, grad_norm

    def backward(self, loss):
        scaled_loss = loss * self.grad_scaler.scale
        scaled_loss.backward()

    def state_dict(self):
        state_dict = {}
        state_dict['optimizer'] = self._optimizer.state_dict()
        if self.grad_scaler:
            state_dict['grad_scaler'] = self.grad_scaler.state_dict()
        state_dict['fp32_master_param_groups'] = self._fp32_master_param_groups
        return state_dict

    def load_state_dict(self, state_dict):
        # Optimizer.
        self._optimizer.load_state_dict(state_dict['optimizer'])

        # Grad scaler.
        if 'grad_scaler' in state_dict:
            self.grad_scaler.load_state_dict(state_dict['grad_scaler'])

        # Copy data for the main params.
        if 'fp32_master_param_groups' in state_dict:
            for current_group, ckpt_group in zip(self._fp32_master_param_groups,
                                                 state_dict['fp32_master_param_groups']):
                for current_param, ckpt_param in zip(current_group, ckpt_group):
                    current_param.data.copy_(ckpt_param.data)

    def clip_grad_norm(self, clip_grad):
        params = []
        for param_group in self._optimizer.param_groups:
            for param in param_group['params']:
                params.append(param)
        return clip_grad_norm_fp32(params, clip_grad)

    # Promote state so it can be retrieved or set via
    # "optimizer_instance.state"
    def _get_state(self):
        return self._optimizer.state

    def _set_state(self, value):
        self._optimizer.state = value

    state = property(_get_state, _set_state)

    # Promote param_groups so it can be retrieved or set via
    # "optimizer_instance.param_groups"
    # (for example, to adjust the learning rate)
    def _get_param_groups(self):
        return self._optimizer.param_groups

    def _set_param_groups(self, value):
        self._optimizer.param_groups = value

    param_groups = property(_get_param_groups, _set_param_groups)
