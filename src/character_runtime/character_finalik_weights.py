"""Native CharacterAnimationBlackboard weight update for the FinalIK Grounder.

Inputs are resolved runtime observations, not guessed from an animation name.
This kernel covers UpdateFootIK up to the three Grounder weight writes; ground
queries and subsequent cloth-parameter feedback belong to separate stages.
"""
from dataclasses import dataclass

from character_runtime_ik_weights import _input_float32, _native_clamp_01, _round_float32


@dataclass(frozen=True)
class FinalIKWeightState:
    pelvis_weight: float
    keeping_foot_weight: float
    keeping_foot_target: float


@dataclass(frozen=True)
class FinalIKWeightInputs:
    """Resolved curve and runtime values at the native update boundary.

    special_idle_active is the resolved source query result, including its
    component/context gates; it is not an unconditional feature-enable flag.
    """

    foot_ik_curve: float
    foot_ik_foot_curve: float
    foot_ik_adsorb_curve: float
    in_air: bool
    is_accelerating: bool
    in_ultimate_skill: bool
    special_idle_active: bool
    predicted_floor_theta: float
    floor_theta_by_foot: float
    floor_theta_by_root: float
    previous_floor_feet_theta: float
    desired_gait: float


@dataclass(frozen=True)
class FinalIKWeightResult:
    state: FinalIKWeightState
    foot_adsorb_weight: float


def _lerp_native(start: float, end: float, fraction: float) -> float:
    return _round_float32(start + _round_float32(_round_float32(end - start) * _native_clamp_01(fraction)))


def update_finalik_weights(state: FinalIKWeightState, inputs: FinalIKWeightInputs,
                          delta_seconds: float) -> FinalIKWeightResult:
    """Advance source pelvis/foot weights without introducing host clamping rules."""
    if not isinstance(state, FinalIKWeightState) or not isinstance(inputs, FinalIKWeightInputs):
        raise TypeError('Expected resolved FinalIK weight state and runtime inputs')
    for field in ('in_air', 'is_accelerating', 'in_ultimate_skill', 'special_idle_active'):
        if type(getattr(inputs, field)) is not bool:
            raise TypeError(f'{field} must be a boolean runtime observation')
    pelvis = _input_float32(state.pelvis_weight)
    keeping = _input_float32(state.keeping_foot_weight)
    target = _input_float32(state.keeping_foot_target)
    curve = _input_float32(inputs.foot_ik_curve)
    foot_curve = _input_float32(inputs.foot_ik_foot_curve)
    adsorb_curve = _input_float32(inputs.foot_ik_adsorb_curve)
    predicted = _input_float32(inputs.predicted_floor_theta)
    by_foot = _input_float32(inputs.floor_theta_by_foot)
    by_root = _input_float32(inputs.floor_theta_by_root)
    previous_theta = _input_float32(inputs.previous_floor_feet_theta)
    gait = _input_float32(inputs.desired_gait)
    delta = _input_float32(delta_seconds)
    if delta < 0.0:
        raise ValueError('delta_seconds must be nonnegative')
    if inputs.in_air:
        pelvis = _lerp_native(pelvis, 0.0, _round_float32(delta * 360.0))
    else:
        desired = curve
        if inputs.is_accelerating:
            correction = min(_round_float32(abs(predicted) - 10.0), 0.0)
            desired = _round_float32(desired + _round_float32(correction * _round_float32(0.08)))
        attenuation = 1.0
        if inputs.special_idle_active and by_foot > -90.0 and by_root > -90.0:
            difference = min(abs(_round_float32(previous_theta - by_foot)), abs(_round_float32(by_foot - by_root)))
            limited = min(max(difference, 10.0), 30.0)
            attenuation = _round_float32(1.0 - _native_clamp_01(
                _round_float32(_round_float32(limited - 10.0) * _round_float32(0.05))))
        desired = _round_float32(desired * attenuation)
        if inputs.in_ultimate_skill:
            pelvis = desired
        else:
            rate = 8.0 if pelvis < desired and gait > 0.0 else 3.0
            pelvis = _lerp_native(pelvis, desired, _round_float32(rate * delta))
        target = _native_clamp_01(_round_float32(1.0 - foot_curve))
        keeping = _lerp_native(keeping, target, _round_float32(delta + delta))
    adsorption = _native_clamp_01(_round_float32(1.0 - adsorb_curve))
    return FinalIKWeightResult(FinalIKWeightState(pelvis, keeping, target), adsorption)
