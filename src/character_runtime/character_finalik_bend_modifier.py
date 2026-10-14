"""Source FinalIK bend modifiers with explicit initialization and runtime reads.

Separate functions mirror evidenced enum branches; no pole, target, parent
rotation, solver initiation or one-frame history is inferred by this module.
Arm's axis-direction heuristic is not implemented here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from character_finalik_bend_direction import cross, squared_length
from character_finalik_bone_rotation import Quaternion, inverse, multiply, quaternion, rotate_bend_normal
from character_finalik_end_rotation import slerp_world_rotation
from character_finalik_grounder import Vector3
from character_finalik_trigonometric_inputs import vector
from character_finalik_weights import _lerp_native
from character_runtime_ik_weights import _input_float32, _native_clamp_01, _round_float32


@dataclass(frozen=True)
class FinalIKAnimationBendState:
    maintain_for_one_frame: bool
    animation_normal: Vector3


@dataclass(frozen=True)
class FinalIKAnimationBendResult:
    normal: Vector3
    state: FinalIKAnimationBendState


def request_maintained_bend(state: FinalIKAnimationBendState, initiated: bool,
                            upper_world_rotation: Quaternion, default_local_bend_normal: Vector3
                            ) -> FinalIKAnimationBendState:
    """Port1832FB510: preserve on uninitiated solver, otherwise capture."""
    if type(initiated) is not bool:
        raise ValueError('Expected explicit boolean solver initiated state')
    if not initiated:
        return state
    return FinalIKAnimationBendState(True,
        rotate_bend_normal(upper_world_rotation, default_local_bend_normal))


def _blend_vectors(current: Vector3, target: Vector3, weight: float) -> Vector3:
    return cast(Vector3, tuple(_lerp_native(left, right, weight)
        for left, right in zip(current, target, strict=True)))


def evaluate_animation_bend(state: FinalIKAnimationBendState, current_normal: Vector3,
                            modifier_weight: float, initiated: bool, upper_world_rotation: Quaternion,
                            default_local_bend_normal: Vector3) -> FinalIKAnimationBendResult:
    """Port Animation0 capture/consume and non-normalized vector interpolation.

    OnUpdateVirtual calls this only when the clamped position weight is
    positive. The caller must retain this state when that outer gate is off.
    """
    if type(state.maintain_for_one_frame) is not bool:
        raise ValueError('Expected explicit boolean one-frame bend flag')
    weight = _input_float32(modifier_weight)
    normal = vector(current_normal, 'current_normal')
    if weight <= 0.:
        return FinalIKAnimationBendResult(normal, state)
    captured = state if state.maintain_for_one_frame else request_maintained_bend(
        state, initiated, upper_world_rotation, default_local_bend_normal)
    result = _blend_vectors(normal, vector(captured.animation_normal, 'animation_normal'),
        _native_clamp_01(weight))
    return FinalIKAnimationBendResult(result, FinalIKAnimationBendState(False, captured.animation_normal))


def evaluate_target_bend(current_normal: Vector3, modifier_weight: float,
                         ik_rotation: Quaternion, default_end_world_rotation: Quaternion) -> Vector3:
    """Port Target1 using the actual initiation-time bone3DefaultRotation."""
    normal = vector(current_normal, 'current_normal')
    weight = _input_float32(modifier_weight)
    if weight <= 0.:
        return normal
    delta = multiply(quaternion(ik_rotation, 'ik_rotation'),
        inverse(quaternion(default_end_world_rotation, 'default_end_world_rotation')))
    return rotate_bend_normal(slerp_world_rotation((0., 0., 0., 1.), delta, weight), normal)


def evaluate_parent_bend(current_normal: Vector3, modifier_weight: float,
                         current_parent_world_rotation: Quaternion,
                         stored_parent_default_rotation: Quaternion,
                         stored_default_root_rotation: Quaternion) -> Vector3:
    """Port Parent2; preserve the native three-product initialization convention."""
    normal = vector(current_normal, 'current_normal')
    weight = _input_float32(modifier_weight)
    if weight <= 0.:
        return normal
    relative = multiply(quaternion(current_parent_world_rotation, 'current_parent_world_rotation'),
        inverse(quaternion(stored_parent_default_rotation, 'stored_parent_default_rotation')))
    delta = multiply(relative, inverse(quaternion(stored_default_root_rotation, 'stored_default_root_rotation')))
    return rotate_bend_normal(slerp_world_rotation((0., 0., 0., 1.), delta, weight), normal)


def evaluate_goal_bend(current_normal: Vector3, modifier_weight: float,
                       upper_world_position: Vector3, ik_position: Vector3,
                       bend_goal_world_position: Vector3) -> Vector3:
    """Port Goal4 cross order, exact near-zero gate and vector-Lerp branch.

    ik_position is raw IKPosition+0x14 before weighted target preparation,
    not the position-stage target stored later at weightIKPosition+0x98.
    """
    normal = vector(current_normal, 'current_normal')
    weight = _input_float32(modifier_weight)
    if weight <= 0.:
        return normal
    upper = vector(upper_world_position, 'upper_world_position')
    target = vector(ik_position, 'ik_position')
    goal = vector(bend_goal_world_position, 'bend_goal_world_position')
    f = _round_float32
    goal_direction = cast(Vector3, tuple(f(a - b) for a, b in zip(goal, upper, strict=True)))
    target_direction = cast(Vector3, tuple(f(a - b) for a, b in zip(target, upper, strict=True)))
    goal_normal = cross(goal_direction, target_direction)
    if squared_length(goal_normal) < f(1e-10):
        return normal
    return _blend_vectors(normal, goal_normal, weight) if weight < 1. else goal_normal
