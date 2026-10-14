"""FinalIK independent end rotation and one-frame maintenance lifecycle.

The caller supplies actual world rotations at each source read boundary.
Transform propagation, target capture and solver initiation remain external.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import cast

from character_finalik_bone_rotation import Quaternion, quaternion
from character_runtime_ik_weights import _input_float32, _round_float32


@dataclass(frozen=True)
class FinalIKEndRotationState:
    maintain_for_one_frame: bool
    requested_maintain_rotation: Quaternion
    rotation_before_solve: Quaternion


@dataclass(frozen=True)
class FinalIKEndRotationPrepared:
    state: FinalIKEndRotationState
    maintenance_weight: float


@dataclass(frozen=True)
class FinalIKEndRotationResult:
    rotation: Quaternion
    world_writes: tuple[Quaternion, ...]


def _weight(value: float) -> float:
    return min(1., max(0., _input_float32(value)))


def slerp_world_rotation(current: Quaternion, target: Quaternion, weight: float) -> Quaternion:
    """Port UnityPlayer1801AC320 f32 order and its .95 nlerp switch.

    Python transcendental functions are rounded to f32; native CRT precision
    is measured independently rather than claimed bitwise equivalent.
    """
    a = quaternion(current, 'current')
    b = quaternion(target, 'target')
    t = _weight(weight)
    f = _round_float32
    dot = f(f(f(f(a[1] * b[1]) + f(a[0] * b[0])) + f(a[2] * b[2])) + f(a[3] * b[3]))
    if dot < 0.:
        dot = -dot
        b = cast(Quaternion, tuple(-item for item in b))
    if dot >= f(.95):
        values = tuple(f(f(f(right - left) * t) + left) for left, right in zip(a, b, strict=True))
        norm = f(math.sqrt(f(f(f(f(values[0] * values[0]) + f(values[1] * values[1]))
            + f(values[2] * values[2])) + f(values[3] * values[3]))))
        return cast(Quaternion, tuple(f(item / norm) for item in values))
    angle = f(math.acos(dot))
    reciprocal = f(1. / f(math.sin(angle)))
    right_scale = f(math.sin(f(angle * t)))
    left_scale = f(math.sin(f(f(1. - t) * angle)))
    return cast(Quaternion, tuple(f(f(f(left_scale * left) + f(right_scale * right)) * reciprocal)
        for left, right in zip(a, b, strict=True)))


def request_maintained_rotation(state: FinalIKEndRotationState, initiated: bool,
                                current_end_rotation: Quaternion) -> FinalIKEndRotationState:
    """Port IKSolverLimb.MaintainRotation1832FB4A0 initiation gate."""
    if type(initiated) is not bool:
        raise ValueError('Expected boolean solver initiated state')
    if not initiated:
        return state
    return FinalIKEndRotationState(True, quaternion(current_end_rotation, 'current_end_rotation'),
        state.rotation_before_solve)


def prepare_end_rotation(state: FinalIKEndRotationState, position_weight: float,
                         maintain_rotation_weight: float, current_end_rotation: Quaternion
                         ) -> FinalIKEndRotationPrepared:
    """Consume the one-frame request only at the positive pre-solve gate."""
    if type(state.maintain_for_one_frame) is not bool:
        raise ValueError('Expected boolean one-frame maintenance flag')
    position = _weight(position_weight)
    # OnUpdateVirtual clamps maintenance only when position solving is active.
    maintain = _weight(maintain_rotation_weight) if position > 0. else _input_float32(maintain_rotation_weight)
    product = _round_float32(maintain * position)
    if product <= 0.:
        return FinalIKEndRotationPrepared(state, product)
    before = (quaternion(state.requested_maintain_rotation, 'requested_maintain_rotation')
        if state.maintain_for_one_frame else quaternion(current_end_rotation, 'current_end_rotation'))
    return FinalIKEndRotationPrepared(FinalIKEndRotationState(False,
        state.requested_maintain_rotation, before), product)


def apply_end_rotation(prepared: FinalIKEndRotationPrepared, current_after_position: Quaternion,
                       target_rotation: Quaternion, rotation_weight: float) -> FinalIKEndRotationResult:
    """Apply independent rotation first, then the Limb post-solve maintenance."""
    current = quaternion(current_after_position, 'current_after_position')
    weight = _weight(rotation_weight)
    maintenance = _input_float32(prepared.maintenance_weight)
    writes: tuple[Quaternion, ...] = ()
    if weight > 0.:
        current = slerp_world_rotation(current, target_rotation, weight)
        writes += (current,)
    if maintenance > 0.:
        current = slerp_world_rotation(current, prepared.state.rotation_before_solve,
            maintenance)
        writes += (current,)
    return FinalIKEndRotationResult(current, writes)
