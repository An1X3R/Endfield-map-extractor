"""Compose the source Animation bend branch and direct-limb pose solver.

Requests describe explicit pre-solve MaintainBend/MaintainRotation calls.
The static baseline normal is restored by construction, while one-frame
requests and weighted position remain caller-owned immutable state.
"""
from __future__ import annotations

from dataclasses import dataclass

from character_animation_goals import GoalRig, LocalTransform, Vec3, _worlds
from character_finalik_bend_modifier import (
    FinalIKAnimationBendState, evaluate_animation_bend, request_maintained_bend,
)
from character_finalik_bone_rotation import Quaternion
from character_finalik_end_rotation import FinalIKEndRotationState, request_maintained_rotation
from character_finalik_pose import (
    FinalIKDirectCalibration, FinalIKLimbRequest, FinalIKPositionRequest,
    require_direct_limb, solve_direct_limb,
)
from character_finalik_trigonometric_inputs import vector
from character_runtime_ik_weights import _input_float32, _native_clamp_01


@dataclass(frozen=True)
class FinalIKAnimationLimbState:
    bend: FinalIKAnimationBendState
    end: FinalIKEndRotationState
    weighted_position: Vec3


@dataclass(frozen=True)
class FinalIKAnimationLimbRequest:
    position_weight: float
    target_position: Vec3
    target_rotation: Quaternion
    rotation_weight: float
    maintain_rotation_weight: float
    bend_modifier_weight: float
    initiated: bool
    maintain_bend_requested: bool
    maintain_rotation_requested: bool


@dataclass(frozen=True)
class FinalIKAnimationLimbResult:
    locals: tuple[LocalTransform, ...]
    state: FinalIKAnimationLimbState
    modified_bend_normal: Vec3
    position_stage_active: bool
    end_world_writes: tuple[Quaternion, ...]


def solve_animation_limb(rig: GoalRig, locals_: tuple[LocalTransform, ...],
                         calibration: FinalIKDirectCalibration, baseline_bend_normal: Vec3,
                         request: FinalIKAnimationLimbRequest,
                         state: FinalIKAnimationLimbState) -> FinalIKAnimationLimbResult:
    """Evaluate an explicit Animation0 update without inferring runtime inputs."""
    if not isinstance(request, FinalIKAnimationLimbRequest) or not isinstance(state, FinalIKAnimationLimbState):
        raise TypeError('Animation limb requires typed request and state values')
    for name in ('initiated', 'maintain_bend_requested', 'maintain_rotation_requested'):
        if type(getattr(request, name)) is not bool:
            raise ValueError(f'Animation limb requires an explicit boolean: field={name}')
    limb = calibration.limb
    require_direct_limb(rig, limb, locals_)
    normal = vector(baseline_bend_normal, 'baseline_bend_normal')
    modifier_weight = _input_float32(request.bend_modifier_weight)
    position_weight = _native_clamp_01(_input_float32(request.position_weight))
    worlds = _worlds(rig, locals_)
    upper_rotation = worlds[limb.upper].rotation
    end_rotation = worlds[limb.end].rotation
    bend_state = (request_maintained_bend(state.bend, request.initiated, upper_rotation,
                                        calibration.upper.default_local_bend_normal)
                  if request.maintain_bend_requested else state.bend)
    end_state = (request_maintained_rotation(state.end, request.initiated, end_rotation)
                 if request.maintain_rotation_requested else state.end)
    if position_weight > 0.0:
        bend = evaluate_animation_bend(bend_state, normal, modifier_weight, request.initiated,
                                      upper_rotation, calibration.upper.default_local_bend_normal)
        normal, bend_state = bend.normal, bend.state
    solved = solve_direct_limb(rig, locals_, calibration,
        FinalIKLimbRequest(FinalIKPositionRequest(position_weight, request.target_position,
                                               normal, state.weighted_position),
                           request.target_rotation, request.rotation_weight, request.maintain_rotation_weight),
        end_state)
    return FinalIKAnimationLimbResult(solved.locals,
        FinalIKAnimationLimbState(bend_state, solved.end_rotation_state, solved.weighted_position_state),
        normal, solved.position_stage_active, solved.end_world_writes)
