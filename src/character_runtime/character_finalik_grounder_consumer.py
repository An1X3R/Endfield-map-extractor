"""Consume the evidenced two-leg Grounder segment with explicit query outputs.

This schedules pelvis preparation, source SetLegIK inputs, Animation0 limbs,
foot post writes and pelvis history capture. Grounding queries, spine/body
rotation writes, callback delegates and global BipedIK scheduling are external.
"""
from __future__ import annotations

from dataclasses import dataclass

from character_animation_goals import GoalRig, LocalTransform, Vec3, _worlds
from character_finalik_animation_limb import (
    FinalIKAnimationLimbRequest, FinalIKAnimationLimbResult,
    FinalIKAnimationLimbState, solve_animation_limb,
)
from character_finalik_bone_rotation import Quaternion
from character_finalik_grounder import FinalIKLegInput, prepare_finalik_leg_target
from character_finalik_grounder_pose import (
    GrounderPelvisPoseResult, GrounderPelvisState, apply_grounder_pelvis,
    finish_grounder_pelvis,
)
from character_finalik_grounder_post import (
    GrounderFootLimits, GrounderFootPostInputs, GrounderFootPostResult,
    prepare_grounder_foot_post, write_grounder_foot_post,
)
from character_finalik_pose import FinalIKDirectCalibration
from character_finalik_weights import FinalIKWeightResult


@dataclass(frozen=True)
class GrounderLegObservation:
    grounded_position: Vec3
    ik_foot_position: Vec3 | None
    last_frame_invalid: bool
    rotation_offset: Quaternion
    rotation_weight: float
    maintain_rotation_weight: float
    bend_modifier_weight: float
    maintain_bend_requested: bool
    maintain_rotation_requested: bool


@dataclass(frozen=True)
class GrounderPoseRequest:
    weights: FinalIKWeightResult
    pelvis_ik_offset: Vec3
    floor_feet_theta_by_foot: float
    floor_feet_theta_by_logic: float
    limits: GrounderFootLimits
    legs: tuple[GrounderLegObservation, GrounderLegObservation]
    enabled: bool
    initiated: bool


@dataclass(frozen=True)
class GrounderPoseState:
    pelvis: GrounderPelvisState
    limbs: tuple[FinalIKAnimationLimbState, FinalIKAnimationLimbState]


@dataclass(frozen=True)
class GrounderPoseResult:
    locals: tuple[LocalTransform, ...]
    state: GrounderPoseState
    pelvis_pre: GrounderPelvisPoseResult
    limbs: tuple[FinalIKAnimationLimbResult, ...]
    foot_post: tuple[GrounderFootPostResult, ...]


def solve_grounder_pose(rig: GoalRig, locals_: tuple[LocalTransform, ...], pelvis_index: int,
                        calibrations: tuple[FinalIKDirectCalibration, FinalIKDirectCalibration],
                        baseline_bend_normals: tuple[Vec3, Vec3], request: GrounderPoseRequest,
                        state: GrounderPoseState) -> GrounderPoseResult:
    """Apply one bounded source segment; caller authenticates left/right refs.

    An inactive Grounder returns before its SetLegIK dispatch. This does not
    simulate an independently scheduled BipedIK update with existing targets.
    """
    if not isinstance(request, GrounderPoseRequest) or not isinstance(state, GrounderPoseState):
        raise TypeError('Grounder consumer requires typed request and persistent state')
    if any(not isinstance(values, tuple) or len(values) != 2
           for values in (calibrations, baseline_bend_normals, request.legs, state.limbs)):
        raise ValueError('Grounder consumer requires exactly two source-ordered legs')
    if calibrations[0].limb.end == calibrations[1].limb.end:
        raise ValueError('Grounder source foot references must be distinct')
    begun = apply_grounder_pelvis(rig, locals_, pelvis_index, state.pelvis,
        request.weights, request.pelvis_ik_offset, request.enabled, request.initiated)
    if not begun.preparation.active:
        return GrounderPoseResult(begun.locals, GrounderPoseState(begun.state, state.limbs), begun, (), ())
    pre = _worlds(rig, begun.locals)
    pose = begun.locals
    solved_limbs: list[FinalIKAnimationLimbResult] = []
    for calibration, baseline, observation, limb_state in zip(
            calibrations, baseline_bend_normals, request.legs, state.limbs, strict=True):
        if not isinstance(observation, GrounderLegObservation):
            raise TypeError('Grounder leg queries must be explicit typed observations')
        foot = pre[calibration.limb.end]
        target = prepare_finalik_leg_target(FinalIKLegInput(foot.rotation,
            observation.grounded_position, observation.ik_foot_position, observation.last_frame_invalid,
            request.weights.state.pelvis_weight, request.weights.state.keeping_foot_weight,
            request.weights.foot_adsorb_weight))
        solved = solve_animation_limb(rig, pose, calibration, baseline,
            FinalIKAnimationLimbRequest(target.ik_position_weight, target.ik_position,
                target.saved_foot_rotation, observation.rotation_weight, observation.maintain_rotation_weight,
                observation.bend_modifier_weight, request.initiated,
                observation.maintain_bend_requested, observation.maintain_rotation_requested), limb_state)
        pose = solved.locals
        solved_limbs.append(solved)
    post: list[GrounderFootPostResult] = []
    for calibration, observation in zip(calibrations, request.legs, strict=True):
        limb = calibration.limb
        worlds = _worlds(rig, pose)
        result = prepare_grounder_foot_post(GrounderFootPostInputs(worlds[limb.end].rotation,
            pre[limb.end].rotation, worlds[limb.middle].rotation, observation.rotation_offset,
            request.weights.state.pelvis_weight, request.weights.state.keeping_foot_weight,
            request.floor_feet_theta_by_foot, request.floor_feet_theta_by_logic, request.limits, request.enabled))
        pose = write_grounder_foot_post(rig, pose, limb.end, result)
        post.append(result)
    pelvis = finish_grounder_pelvis(begun.state, pose[pelvis_index].position, request.weights, request.enabled)
    return GrounderPoseResult(pose, GrounderPoseState(pelvis, (solved_limbs[0].state, solved_limbs[1].state)),
        begun, tuple(solved_limbs), tuple(post))
