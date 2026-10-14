"""Source-derived scalar cloth controls for the branch without ClothCombine.

This is the scalar stage only. Animator state/layer mixing, world contacts and
physics cloth are separate inputs and are not reconstructed by these functions.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import math
import struct


@dataclass(frozen=True)
class ClothCurveInputs:
    ClothOpen: float
    ClothOpenAdd: float
    ClothLean: float
    ClothLeanAdd: float
    ClothLeanAdd1: float
    ClothLeanAddP: float
    ClothFront: float
    ClothFrontAdd: float
    ClothBevel: float
    ClothRightLeft: float
    ClothRightLeftAdd: float
    ClothIKLeg: float
    ClothIKDirection: float
    HairRoll: float
    HairUpDown: float
    HairRightLeft: float
    TailUpDown: float
    TailRightLeft: float
    ArmModifyWeight: float


@dataclass(frozen=True)
class ClothRuntimeInputs:
    transition_offset: float
    styled_body_add: bool


def _f32(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError('Cloth scalar input must be finite')
    return struct.unpack('<f', struct.pack('<f', value))[0]


def _add(left: float, right: float) -> float:
    return _f32(_f32(left) + _f32(right))


def _sub(left: float, right: float) -> float:
    return _f32(_f32(left) - _f32(right))


def _mul(left: float, right: float) -> float:
    return _f32(_f32(left) * _f32(right))


def _signed_time(value: float) -> float:
    return _mul(_add(value, 1.0), 0.5)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, _f32(value)))


def compute_cloth_parameters(curves: ClothCurveInputs,
                             runtime: ClothRuntimeInputs) -> dict[str, float]:
    """Compute the native scalar expressions without adding output clamps."""
    for field in fields(curves):
        _f32(getattr(curves, field.name))
    _f32(runtime.transition_offset)
    if type(runtime.styled_body_add) is not bool:
        raise TypeError('styled_body_add must be a source/runtime boolean')
    lean = _add(_add(_add(curves.ClothLeanAdd, curves.ClothLean),
                     curves.ClothLeanAdd1), curves.ClothLeanAddP)
    front = _add(_add(_add(curves.ClothFrontAdd, curves.ClothFront), 1.0),
                 runtime.transition_offset)
    arm = 0.0 if runtime.styled_body_add else _f32(curves.ArmModifyWeight)
    return {
        'C_ClothCloseOpen': _signed_time(_add(curves.ClothOpenAdd, curves.ClothOpen)),
        'C_ClothLean': lean,
        'C_ClothLeanRev': _sub(1.0, lean),
        'C_ClothLeanAdd': _f32(runtime.transition_offset),
        'C_ClothTiltFB_Straight': _f32(curves.ClothBevel),
        'C_ClothTiltFB_Bevel': _sub(1.0, curves.ClothBevel),
        'C_ClothTiltFB': _sub(1.0, _mul(front, 0.5)),
        'C_ClothTiltLR': _signed_time(_add(curves.ClothRightLeftAdd, curves.ClothRightLeft)),
        'C_ClothIKLegL': abs(_clamp(curves.ClothIKLeg, -1.0, 0.0)),
        'C_ClothIKLegR': abs(_clamp(curves.ClothIKLeg, 0.0, 1.0)),
        'C_ClothTPoseWeight': _sub(1.0, abs(curves.ClothIKLeg)),
        'C_ClothIKDirection': _signed_time(curves.ClothIKDirection),
        'C_HairRoll': _signed_time(curves.HairRoll),
        'C_HairU_Weight': abs(_clamp(curves.HairUpDown, 0.0, 1.0)),
        'C_HairM_Weight': _sub(1.0, abs(curves.HairUpDown)),
        'C_HairD_Weight': abs(_clamp(curves.HairUpDown, -1.0, 0.0)),
        'C_HairYaw': _signed_time(curves.HairRightLeft),
        'C_TailD_Weight': _sub(1.0, abs(_clamp(curves.TailUpDown, 0.0, 1.0))),
        'C_TailU_Weight': abs(_clamp(curves.TailUpDown, 0.0, 1.0)),
        'C_TailYaw': _signed_time(curves.TailRightLeft),
        'C_ArmModify': arm,
        'C_ArmModifyRev': _clamp(_sub(1.0, arm), 0.0, 1.0),
    }


def step_cloth_transition_offset(previous: float, delta_time: float,
                                 front_scale: float, physics_weight: float,
                                 accelerating: bool) -> float:
    """Rebuild the native frame-rate-dependent transition correction."""
    values = tuple(_f32(value) for value in (previous, delta_time, front_scale, physics_weight))
    previous, delta_time, front_scale, physics_weight = values
    if delta_time <= 0.0:
        raise ValueError('Cloth transition delta_time must be positive')
    if type(accelerating) is not bool:
        raise TypeError('accelerating must be a source/runtime boolean')
    rounded_rate = round(_f32(1.0 / delta_time))
    frame_deficit = max(0.0, _sub(40.0, float(rounded_rate)))
    target = _mul(_mul(_mul(frame_deficit, front_scale), physics_weight), 0.05)
    target = _mul(target, 1.0 if accelerating else 0.0)
    rate = 2.0 if target <= previous else 10.0
    amount = _clamp(_mul(rate, delta_time), 0.0, 1.0)
    return _add(_mul(_sub(target, previous), amount), previous)
