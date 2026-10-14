"""Apply evidenced FinalIK position writes to a direct source-bone hierarchy.

Caller-owned calibration and runtime inputs remain explicit. The composed
limb entry applies independent end rotation and maintenance after position.
Neither entry selects terrain, infers history, or schedules Animator/Grounder
callbacks. Local translations and scales are preserved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from character_animation_goals import GoalRig, LimbSpec, LocalTransform, Vec3, _worlds, _write_rotation
from character_finalik_bend_direction import FinalIKBendInputs, prepare_bend_direction, squared_length
from character_finalik_bone_rotation import (
    FinalIKBoneCalibration, Quaternion, calibrate_bone_rotation, get_bone_world_rotation, rotate_bend_normal,
)
from character_finalik_end_rotation import FinalIKEndRotationState, apply_end_rotation, prepare_end_rotation
from character_finalik_trigonometric_inputs import FinalIKTrigonometricInputs, prepare_trigonometric_inputs, vector
from character_runtime_ik_weights import _round_float32


@dataclass(frozen=True)
class FinalIKDirectCalibration:
    limb: LimbSpec
    upper: FinalIKBoneCalibration
    middle: FinalIKBoneCalibration


@dataclass(frozen=True)
class FinalIKPositionRequest:
    weight: float
    target_position: Vec3
    modified_bend_normal: Vec3
    previous_weighted_position: Vec3


@dataclass(frozen=True)
class FinalIKPositionResult:
    locals: tuple[LocalTransform, ...]
    weighted_position_state: Vec3
    position_stage_active: bool


@dataclass(frozen=True)
class FinalIKLimbRequest:
    position: FinalIKPositionRequest
    target_rotation: Quaternion
    rotation_weight: float
    maintain_rotation_weight: float


@dataclass(frozen=True)
class FinalIKLimbResult:
    locals: tuple[LocalTransform, ...]
    weighted_position_state: Vec3
    end_rotation_state: FinalIKEndRotationState
    position_stage_active: bool
    end_world_writes: tuple[Quaternion, ...]


def require_direct_limb(rig: GoalRig, limb: LimbSpec, locals_: tuple[LocalTransform, ...]) -> None:
    if len(locals_) != len(rig.parents):
        raise ValueError('FinalIK position source local count differs from the rig')
    if limb not in rig.limbs:
        raise ValueError(f'FinalIK limb does not belong to the supplied rig: limb={limb.name}')
    if rig.parents[limb.middle] != limb.upper or rig.parents[limb.end] != limb.middle:
        raise ValueError(f'FinalIK direct hierarchy requires upper -> middle -> end parents: limb={limb.name}')


def difference(left: Vec3, right: Vec3) -> Vec3:
    """Subtract native float32 Transform position inputs."""
    f = _round_float32
    return cast(Vec3, tuple(f(f(a) - f(b)) for a, b in zip(left, right, strict=True)))


def calibrate_direct_limb(rig: GoalRig, locals_: tuple[LocalTransform, ...], limb: LimbSpec,
                          bend_normal: Vec3) -> FinalIKDirectCalibration:
    """Initialize both source bones from an explicit caller-selected pose."""
    require_direct_limb(rig, limb, locals_)
    normal = vector(bend_normal, 'initial_bend_normal')
    worlds = _worlds(rig, locals_)
    upper, middle, end = (worlds[index] for index in (limb.upper, limb.middle, limb.end))
    return FinalIKDirectCalibration(
        limb,
        calibrate_bone_rotation(upper.position, middle.position, upper.rotation, normal),
        calibrate_bone_rotation(middle.position, end.position, middle.rotation, normal),
    )


def solve_direct_limb_position(rig: GoalRig, locals_: tuple[LocalTransform, ...],
                               calibration: FinalIKDirectCalibration,
                               request: FinalIKPositionRequest) -> FinalIKPositionResult:
    """Apply upper then middle rotations, re-reading the updated middle pose."""
    if not isinstance(calibration, FinalIKDirectCalibration) or not isinstance(request, FinalIKPositionRequest):
        raise TypeError('FinalIK position requires typed calibration and request values')
    limb = calibration.limb
    require_direct_limb(rig, limb, locals_)
    worlds = _worlds(rig, locals_)
    upper, middle, end = (worlds[index] for index in (limb.upper, limb.middle, limb.end))
    current_normal = rotate_bend_normal(upper.rotation, calibration.upper.default_local_bend_normal)
    prepared = prepare_trigonometric_inputs(FinalIKTrigonometricInputs(
        request.weight, 0.0, request.target_position, end.position,
        request.previous_weighted_position, request.modified_bend_normal, current_normal,
    ))
    if not prepared.position_stage_active:
        return FinalIKPositionResult(locals_, prepared.weighted_position_state, False)
    target, bend_normal = prepared.solver_target_position, prepared.solver_bend_normal
    if target is None or bend_normal is None:
        raise AssertionError('Active native position stage did not provide target and bend normal')
    first = difference(middle.position, upper.position)
    second = difference(end.position, middle.position)
    bend = prepare_bend_direction(FinalIKBendInputs(
        upper.position, target, squared_length(first), squared_length(second),
        bend_normal, first, prepared.position_weight,
    ))
    upper_rotation = get_bone_world_rotation(bend.weighted_upper_to_middle, bend_normal, calibration.upper)
    parent = rig.parents[limb.upper]
    parent_rotation = worlds[parent].rotation if parent >= 0 else (0.0, 0.0, 0.0, 1.0)
    upper_locals = _write_rotation(locals_, limb.upper, upper_rotation, parent_rotation)
    updated = _worlds(rig, upper_locals)
    middle_pose = updated[limb.middle]
    middle_normal = rotate_bend_normal(middle_pose.rotation, calibration.middle.default_local_bend_normal)
    middle_rotation = get_bone_world_rotation(difference(target, middle_pose.position), middle_normal, calibration.middle)
    result = _write_rotation(upper_locals, limb.middle, middle_rotation, updated[limb.upper].rotation)
    return FinalIKPositionResult(result, prepared.weighted_position_state, True)


def solve_direct_limb(rig: GoalRig, locals_: tuple[LocalTransform, ...],
                      calibration: FinalIKDirectCalibration, request: FinalIKLimbRequest,
                      end_state: FinalIKEndRotationState) -> FinalIKLimbResult:
    """Run capture, position, independent rotation, and maintenance in source order."""
    if not isinstance(request, FinalIKLimbRequest) or not isinstance(end_state, FinalIKEndRotationState):
        raise TypeError('FinalIK limb requires a typed request and explicit end-rotation history')
    limb = calibration.limb
    require_direct_limb(rig, limb, locals_)
    before = _worlds(rig, locals_)[limb.end].rotation
    prepared = prepare_end_rotation(end_state, request.position.weight,
                                    request.maintain_rotation_weight, before)
    position = solve_direct_limb_position(rig, locals_, calibration, request.position)
    worlds = _worlds(rig, position.locals)
    end = apply_end_rotation(prepared, worlds[limb.end].rotation,
                             request.target_rotation, request.rotation_weight)
    result = (_write_rotation(position.locals, limb.end, end.rotation, worlds[limb.middle].rotation)
              if end.world_writes else position.locals)
    return FinalIKLimbResult(result, position.weighted_position_state, prepared.state,
                             position.position_stage_active, end.world_writes)
