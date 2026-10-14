"""Native Grounder pelvis history and explicit post-grounding position writes.

Terrain/ground smoothing remains an external input. The pre stage restores an
unchanged last-solved pelvis before adding the world offset; the post stage
captures the local position after the caller's limb and foot rotation writes.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast

from character_animation_goals import GoalRig, LocalTransform, Vec3, _inverse, _rotate, _sub, _worlds
from character_finalik_trigonometric_inputs import vector
from character_finalik_weights import FinalIKWeightResult
from character_runtime_ik_weights import _input_float32, _round_float32


@dataclass(frozen=True)
class GrounderPelvisState:
    animated_local_position: Vec3
    solved_local_position: Vec3
    last_weight: float
    last_adsorb_weight: float


@dataclass(frozen=True)
class GrounderPelvisPreparation:
    state: GrounderPelvisState
    local_position: Vec3
    active: bool
    local_position_written: bool
    clear_leg_ik_position_weights: bool
    grounding_reset_required: bool


@dataclass(frozen=True)
class GrounderPelvisPoseResult:
    locals: tuple[LocalTransform, ...]
    state: GrounderPelvisState
    preparation: GrounderPelvisPreparation
    written_world_position: Vec3 | None


def resolved_weights(weights: FinalIKWeightResult) -> tuple[float, float]:
    if not isinstance(weights, FinalIKWeightResult):
        raise TypeError('Grounder pelvis requires the resolved FinalIK weight result')
    return _input_float32(weights.state.pelvis_weight), _input_float32(weights.foot_adsorb_weight)


def validate_state(state: GrounderPelvisState) -> GrounderPelvisState:
    if not isinstance(state, GrounderPelvisState):
        raise TypeError('Expected explicit GrounderPelvisState history')
    return GrounderPelvisState(vector(state.animated_local_position, 'animated_local_position'),
                              vector(state.solved_local_position, 'solved_local_position'),
                              _input_float32(state.last_weight), _input_float32(state.last_adsorb_weight))


def prepare_grounder_pelvis(state: GrounderPelvisState, current_local_position: Vec3,
                            weights: FinalIKWeightResult, enabled: bool, initiated: bool) -> GrounderPelvisPreparation:
    """Match OnSolverUpdate's enabled/weight and local-history prefix."""
    if type(enabled) is not bool or type(initiated) is not bool:
        raise TypeError('Grounder enabled and initiated must be explicit booleans')
    history = validate_state(state)
    current = vector(current_local_position, 'current_local_position')
    weight, adsorb = resolved_weights(weights)
    f = _round_float32
    if not enabled or (weight <= 0.0 and adsorb <= 0.0 and history.last_weight <= 0.0 and history.last_adsorb_weight <= 0.0):
        return GrounderPelvisPreparation(history, current, False, False, False, False)
    clear_weights = weight <= 0.0 and adsorb <= 0.0 and initiated
    if weight <= 0.0:
        history = GrounderPelvisState(current, current, history.last_weight, history.last_adsorb_weight)
    dx, dy, dz = (f(a - b) for a, b in zip(current, history.solved_local_position, strict=True))
    squared = f(f(f(dy * dy) + f(dx * dx)) + f(dz * dz))
    restore = squared < f(1e-10)
    local = history.animated_local_position if restore else current
    history = GrounderPelvisState(history.animated_local_position if restore else current,
                                  history.solved_local_position, weight, adsorb)
    return GrounderPelvisPreparation(history, local, True, restore, clear_weights, weight <= f(0.01))


def offset_grounder_pelvis(current_world_position: Vec3, pelvis_ik_offset: Vec3, grounder_weight: float) -> Vec3:
    """The source world write has no extra Clamp01 on Grounder.weight."""
    current = vector(current_world_position, 'current_world_position')
    offset = vector(pelvis_ik_offset, 'pelvis_ik_offset')
    weight = _input_float32(grounder_weight)
    f = _round_float32
    return cast(Vec3, tuple(f(origin + f(delta * weight)) for origin, delta in zip(current, offset, strict=True)))


def finish_grounder_pelvis(state: GrounderPelvisState, current_local_position: Vec3,
                           weights: FinalIKWeightResult, enabled: bool) -> GrounderPelvisState:
    """Capture the post-solver local pelvis only under native post gates."""
    if type(enabled) is not bool:
        raise TypeError('Grounder post enabled must be an explicit boolean')
    history = validate_state(state)
    current = vector(current_local_position, 'post_solver_local_position')
    weight, _ = resolved_weights(weights)
    if weight <= 0.0 or not enabled:
        return history
    return replace(history, solved_local_position=current)


def apply_grounder_pelvis(rig: GoalRig, locals_: tuple[LocalTransform, ...], pelvis_index: int, state: GrounderPelvisState,
                          weights: FinalIKWeightResult, pelvis_ik_offset: Vec3,
                          enabled: bool, initiated: bool) -> GrounderPelvisPoseResult:
    """Consume the native world write on the existing uniform-scale rig.

    The caller resolves the BipedReferences pelvis index explicitly and must
    apply SetLegIK/limb/foot stages before finish_grounder_pelvis.
    Host world-to-local propagation uses the existing source-rig convention;
    its floating-point matrix reconstruction is distinct from native arithmetic.
    """
    if type(pelvis_index) is not int or len(locals_) != len(rig.parents) or pelvis_index not in rig.required_order:
        raise ValueError('Grounder pelvis requires the exact compiled source rig')
    index = pelvis_index
    preparation = prepare_grounder_pelvis(state, locals_[index].position, weights, enabled, initiated)
    offset = vector(pelvis_ik_offset, 'pelvis_ik_offset')
    if not preparation.active:
        return GrounderPelvisPoseResult(locals_, preparation.state, preparation, None)
    restored = tuple(replace(local, position=preparation.local_position) if i == index else local
                     for i, local in enumerate(locals_))
    worlds = _worlds(rig, restored)
    weight, _ = resolved_weights(weights)
    desired = offset_grounder_pelvis(worlds[index].position, offset, weight)
    parent_index = rig.parents[index]
    if parent_index < 0:
        local_position = desired
    else:
        parent = worlds[parent_index]
        delta = _rotate(_inverse(parent.rotation), _sub(desired, parent.position))
        local_position = cast(Vec3, tuple(_round_float32(value / parent.scale) for value in delta))
    posed = tuple(replace(local, position=local_position) if i == index else local
                  for i, local in enumerate(restored))
    return GrounderPelvisPoseResult(posed, preparation.state, preparation, desired)
