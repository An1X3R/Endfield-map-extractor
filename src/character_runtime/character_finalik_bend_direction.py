"""Source FinalIK knee geometry with explicit world-space bend inputs.

The normal LookRotation basis is reproduced geometrically. Its degenerate
FromToRotation fallback is rejected explicitly until separately verified.
This does not perform native bone rotation writes or modify source-goal defaults.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import cast

from character_finalik_grounder import Vector3
from character_finalik_trigonometric_inputs import vector
from character_finalik_weights import _lerp_native
from character_runtime_ik_weights import _input_float32, _native_clamp_01, _round_float32


@dataclass(frozen=True)
class FinalIKBendInputs:
    upper_position: Vector3
    weighted_target_position: Vector3
    first_squared_length: float
    second_squared_length: float
    weighted_bend_normal: Vector3
    current_upper_to_middle: Vector3
    position_weight: float


@dataclass(frozen=True)
class FinalIKBendPrepared:
    target_distance: float
    along_distance: float
    height: float
    geometric_upper_to_middle: Vector3
    weighted_upper_to_middle: Vector3
    weighted_middle_position: Vector3


def cross(left: Vector3, right: Vector3) -> Vector3:
    f = _round_float32
    return (f(f(left[1] * right[2]) - f(left[2] * right[1])),
            f(f(left[2] * right[0]) - f(left[0] * right[2])),
            f(f(left[0] * right[1]) - f(left[1] * right[0])))


def squared_length(value: Vector3) -> float:
    f = _round_float32
    return f(f(f(value[0] * value[0]) + f(value[1] * value[1])) + f(value[2] * value[2]))


def unit(value: Vector3, field: str) -> Vector3:
    length = _round_float32(math.sqrt(squared_length(value)))
    if length < _round_float32(1e-5):
        raise ValueError(f'Native LookRotation degenerate fallback is not verified: field={field}')
    return cast(Vector3, tuple(_round_float32(component / length) for component in value))


def prepare_bend_direction(inputs: FinalIKBendInputs) -> FinalIKBendPrepared:
    """Consume GetBendDirection and the subsequent OnUpdate direction Lerp.

    Lengths are native TrigonometricBone.sqrMag fields, not fresh distances from
    an arbitrary rig pose. Outside reach, native height clamps to zero without
    clamping the target distance or along component. weighted_middle_position
    is an angular aim point; native bone rotation preserves the initialized
    segment length, so this is not a prediction of the final knee position.
    Callers must honor the preceding position-stage gate before calling here.
    """
    if not isinstance(inputs, FinalIKBendInputs):
        raise TypeError('Expected explicit FinalIKBendInputs')
    upper = vector(inputs.upper_position, 'upper_position')
    target = vector(inputs.weighted_target_position, 'weighted_target_position')
    normal = vector(inputs.weighted_bend_normal, 'weighted_bend_normal')
    current = vector(inputs.current_upper_to_middle, 'current_upper_to_middle')
    first, second = _input_float32(inputs.first_squared_length), _input_float32(inputs.second_squared_length)
    weight = _native_clamp_01(_input_float32(inputs.position_weight))
    if first <= 0. or second <= 0.:
        raise ValueError('Native initialized limb squared lengths must be positive')
    f = _round_float32
    delta = cast(Vector3, tuple(f(end - origin) for origin, end in zip(upper, target, strict=True)))
    magnitude_squared = squared_length(delta)
    # The original zero-distance gate adds squared terms in y,x,z order.
    gate_squared = f(f(f(delta[1] * delta[1]) + f(delta[0] * delta[0])) + f(delta[2] * delta[2]))
    if gate_squared < f(1e-10):
        direction, distance, along, height = (0., 0., 0.), 0., 0., 0.
    else:
        distance = f(math.sqrt(magnitude_squared))
        along = f(f(f(f(first + magnitude_squared) - second) * .5) / distance)
        height = f(math.sqrt(max(0., f(first - f(along * along)))))
        forward = unit(delta, 'target_delta')
        # Source up is Cross(delta/distance, weighted_bend_normal). LookRotation
        # forms right=normalize(Cross(up,forward)), then up=Cross(forward,right).
        source_forward = cast(Vector3, tuple(f(component / distance) for component in delta))
        up = cross(source_forward, normal)
        right = unit(cross(up, forward), 'bend_plane')
        oriented_up = cross(forward, right)
        direction = cast(Vector3, tuple(f(f(axis * height) + f(fwd * along))
            for axis, fwd in zip(oriented_up, forward, strict=True)))
    weighted = cast(Vector3, tuple(_lerp_native(origin, end, weight)
        for origin, end in zip(current, direction, strict=True)))
    weighted_gate = f(f(f(weighted[1] * weighted[1]) + f(weighted[0] * weighted[0])) + f(weighted[2] * weighted[2]))
    if weighted_gate < f(1e-10):
        raise ValueError('Native OnUpdate near-zero direction fallback is not verified')
    middle = cast(Vector3, tuple(f(origin + displacement)
        for origin, displacement in zip(upper, weighted, strict=True)))
    return FinalIKBendPrepared(distance, along, height, direction, weighted, middle)
