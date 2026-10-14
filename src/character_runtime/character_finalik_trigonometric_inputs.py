"""Native FinalIK weighted position/bend preprocessing with explicit caller state.

The inputs belong after Grounder target preparation and OnUpdateVirtual's bend
modifier. Outputs are world-space target/bend vectors for a two-bone consumer;
the solver, bone rotation writes, body correction and scheduling are separate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from character_finalik_grounder import Vector3
from character_finalik_weights import _lerp_native
from character_runtime_ik_weights import _input_float32, _native_clamp_01


@dataclass(frozen=True)
class FinalIKTrigonometricInputs:
    position_weight: float
    rotation_weight: float
    ik_position: Vector3
    current_end_position: Vector3
    previous_weighted_position: Vector3
    modified_bend_normal: Vector3
    current_world_bend_normal: Vector3


@dataclass(frozen=True)
class FinalIKTrigonometricPrepared:
    position_weight: float
    rotation_weight: float
    position_stage_active: bool
    weighted_position_state: Vector3
    solver_target_position: Vector3 | None
    solver_bend_normal: Vector3 | None


def vector(values: Vector3, field: str) -> Vector3:
    if not isinstance(values, tuple) or len(values) != 3:
        raise ValueError(f'Expected three explicit world-space components: field={field}')
    return (_input_float32(values[0]), _input_float32(values[1]), _input_float32(values[2]))


def prepare_trigonometric_inputs(inputs: FinalIKTrigonometricInputs) -> FinalIKTrigonometricPrepared:
    """Port OnUpdate's clamp/gate and weighted target/bend input arithmetic.

    current_world_bend_normal is bone1.worldRotation * defaultLocalBendNormal.
    modified_bend_normal is the actual post-OnUpdateVirtual source bendNormal.
    Neither vector is normalized here: the original preprocessing does not do so.
    A zero position weight preserves weightIKPosition and skips the position
    solver; it does not imply a zero rotation weight or a rotation-stage skip.
    """
    if not isinstance(inputs, FinalIKTrigonometricInputs):
        raise TypeError('Expected explicit FinalIKTrigonometricInputs')
    weight = _native_clamp_01(_input_float32(inputs.position_weight))
    rotation_weight = _native_clamp_01(_input_float32(inputs.rotation_weight))
    target = vector(inputs.ik_position, 'ik_position')
    current = vector(inputs.current_end_position, 'current_end_position')
    previous = vector(inputs.previous_weighted_position, 'previous_weighted_position')
    modified = vector(inputs.modified_bend_normal, 'modified_bend_normal')
    animated = vector(inputs.current_world_bend_normal, 'current_world_bend_normal')
    if weight <= 0.:
        return FinalIKTrigonometricPrepared(weight, rotation_weight, False, previous, None, None)
    weighted_target = cast(Vector3, tuple(_lerp_native(origin, end, weight) for origin, end in zip(current, target, strict=True)))
    weighted_bend = cast(Vector3, tuple(_lerp_native(origin, end, weight) for origin, end in zip(animated, modified, strict=True)))
    return FinalIKTrigonometricPrepared(weight, rotation_weight, True,
        weighted_target, weighted_target, weighted_bend)
