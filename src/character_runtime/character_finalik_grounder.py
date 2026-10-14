"""Source GrounderBipedIK.SetLegIK bridge for explicit post-grounding inputs.

The caller must resolve the actual GrounderBipedIK component and its BipedIK
reference. This bridge does not generate collision results or solve limb bones.
"""
from dataclasses import dataclass
from typing import cast

from character_runtime_ik_weights import _input_float32, _native_clamp_01, _round_float32

Vector3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]


@dataclass(frozen=True)
class FinalIKLegInput:
    animated_foot_rotation: Quaternion
    grounded_position: Vector3
    ik_foot_position: Vector3 | None
    last_frame_invalid: bool
    grounder_weight: float
    maintain_pelvis_foot_weight: float
    foot_adsorb_weight: float


@dataclass(frozen=True)
class FinalIKLegTarget:
    saved_foot_rotation: Quaternion
    ik_position: Vector3
    ik_position_weight: float


def _components(values: tuple[float, ...], size: int, field: str) -> tuple[float, ...]:
    if not isinstance(values, tuple) or len(values) != size:
        raise ValueError(f'{field} must contain exactly {size} components')
    return tuple(_input_float32(value) for value in values)


def prepare_finalik_leg_target(inputs: FinalIKLegInput) -> FinalIKLegTarget:
    """Port source position preparation and adsorption weight without a host IK substitution.

    ik_foot_position is present only when both source IKFootBone transforms are
    valid and the limb index is 0 or 1; otherwise native code copies the ground
    target directly. Foot adsorption weight is copied without an extra clamp.
    """
    if not isinstance(inputs, FinalIKLegInput) or type(inputs.last_frame_invalid) is not bool:
        raise TypeError('Expected FinalIKLegInput from the resolved GrounderBipedIK component')
    rotation = _components(inputs.animated_foot_rotation, 4, 'animated_foot_rotation')
    grounded = _components(inputs.grounded_position, 3, 'grounded_position')
    overall = _input_float32(inputs.grounder_weight)
    maintain = _input_float32(inputs.maintain_pelvis_foot_weight)
    adsorption = _input_float32(inputs.foot_adsorb_weight)
    result = grounded
    if inputs.ik_foot_position is not None:
        foot = _components(inputs.ik_foot_position, 3, 'ik_foot_position')
        fraction = _native_clamp_01(_round_float32(overall * maintain))
        result = foot if inputs.last_frame_invalid else tuple(
            _round_float32(origin + _round_float32(_round_float32(target - origin) * fraction))
            for origin, target in zip(foot, grounded))
    return FinalIKLegTarget(cast(Quaternion, rotation), cast(Vector3, result), adsorption)
