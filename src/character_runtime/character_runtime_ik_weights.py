"""HG IKRigging weights; not the distinct RootMotion.FinalIK character chain."""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct


@dataclass(frozen=True)
class IKWeightRemap:
    weight_argument_float32: float
    new_constraint_m_weight: float
    prepare_effector_constraint_m_data_rotation_weight: float


@dataclass(frozen=True)
class IKLimbWeightInput:
    """Post-ground-probe inputs to one valid limb in IKRigging.LateTick."""

    hit: bool
    animator_curve_weight: float
    foot_lock_weight: float
    previous_constraint_weight: float


def _round_float32(value: float) -> float:
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _input_float32(value: float) -> float:
    if type(value) is not float:
        raise TypeError("weight must be a Python float representing a native float32 input")
    if not math.isfinite(value):
        raise ValueError("weight must be finite")
    rounded = _round_float32(value)
    if not math.isfinite(rounded):
        raise ValueError("weight is outside the finite float32 range")
    return rounded


def _native_clamp_01(value: float) -> float:
    zero = 0.0
    one = 1.0
    if value < zero:
        return zero
    if value <= one:
        return value
    return one


def remap_ik_weight(weight: float) -> IKWeightRemap:
    input_weight = _input_float32(weight)
    first_target = _native_clamp_01(input_weight)
    delta = _round_float32(input_weight - 0.5)
    doubled_delta = _round_float32(delta + delta)
    second_target = _native_clamp_01(doubled_delta)
    return IKWeightRemap(input_weight, first_target, second_target)


def evaluate_limb_weight(inputs: IKLimbWeightInput, delta_seconds: float) -> IKWeightRemap:
    """Evaluate the native post-probe weight branch with explicit runtime inputs.

    This does not synthesize ground hits, Animator curves or foot-lock state.
    A missed probe releases the previous constraint weight using clamped Lerp;
    a hit takes the larger of the Animator and foot-lock weights.
    """
    if not isinstance(inputs, IKLimbWeightInput) or type(inputs.hit) is not bool:
        raise TypeError('Limb input must contain an explicit boolean ground-probe hit')
    delta = _input_float32(delta_seconds)
    if delta < 0:
        raise ValueError('IK delta_seconds must be nonnegative')
    curve = _input_float32(inputs.animator_curve_weight)
    locked = _input_float32(inputs.foot_lock_weight)
    previous = _input_float32(inputs.previous_constraint_weight)
    if not 0 <= previous <= 1:
        raise ValueError('Previous constraint weight must be within [0, 1]')
    weight = curve if curve > locked else locked
    if not inputs.hit:
        release_fraction = _native_clamp_01(_round_float32(delta * 20.0))
        difference = _round_float32(0.0 - previous)
        weight = _round_float32(previous + _round_float32(difference * release_fraction))
    return remap_ik_weight(weight)
