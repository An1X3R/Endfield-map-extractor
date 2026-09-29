"""Evaluate SceneProbe particle curves without collapsing random or temporal variation.

The current subset supports constants and unweighted Hermite/stepped segments.
Loop extrapolation uses Unity AnimationCurve WrapMode.Loop (2). Other wrap
modes and weighted segments fail explicitly until their semantics are verified.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
from collections.abc import Mapping
from typing import cast


class ParticleCurveError(ValueError):
    """The serialized curve or evaluation request is invalid."""


class UnsupportedParticleCurve(ParticleCurveError):
    """Evaluation requires semantics outside the verified subset."""


@dataclass(frozen=True)
class CurveKey:
    time: float
    value: float
    in_slope: float
    out_slope: float
    weighted_mode: int
    in_weight: float
    out_weight: float


@dataclass(frozen=True)
class ParticleCurve:
    keys: tuple[CurveKey, ...]
    pre_infinity: int
    post_infinity: int
    rotation_order: int


@dataclass(frozen=True)
class ParticleMinMaxCurve:
    mode: int
    scalar: float
    min_scalar: float
    max_curve: ParticleCurve
    min_curve: ParticleCurve


def _record(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ParticleCurveError(f"Expected a curve object: field={context}")
    return cast(Mapping[str, object], value)


def _required(record: Mapping[str, object], key: str, context: str) -> object:
    if key not in record:
        raise ParticleCurveError(f"Required curve field is missing: field={context}.{key}")
    return record[key]


def _finite(value: object, context: str) -> float:
    if type(value) not in (int, float):
        raise ParticleCurveError(f"Expected a finite number: field={context}, value={value!r}")
    try:
        number = float(value)
    except OverflowError as error:
        raise ParticleCurveError(f"Number exceeds float range: field={context}") from error
    if not math.isfinite(number):
        raise ParticleCurveError(f"Expected a finite number: field={context}, value={value!r}")
    return number


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise ParticleCurveError(f"Expected an integer: field={context}, value={value!r}")
    return cast(int, value)


def _slope(value: object, context: str) -> float:
    if value == "Infinity":
        return math.inf
    if value == "-Infinity":
        return -math.inf
    return _finite(value, context)


def _key(value: object, context: str) -> CurveKey:
    row = _record(value, context)
    mode = _integer(_required(row, "WeightedMode", context), f"{context}.WeightedMode")
    if mode not in (0, 1, 2, 3):
        raise UnsupportedParticleCurve(f"Unknown tangent weight mode: field={context}, mode={mode}")
    return CurveKey(
        _finite(_required(row, "Time", context), f"{context}.Time"),
        _finite(_required(row, "Value", context), f"{context}.Value"),
        _slope(_required(row, "InSlope", context), f"{context}.InSlope"),
        _slope(_required(row, "OutSlope", context), f"{context}.OutSlope"),
        mode,
        _finite(_required(row, "InWeight", context), f"{context}.InWeight"),
        _finite(_required(row, "OutWeight", context), f"{context}.OutWeight"),
    )


def parse_particle_curve(value: object, context: str) -> ParticleCurve:
    """Validate a SceneProbe curve while retaining inactive serialized metadata."""
    row = _record(value, context)
    key_rows = _required(row, "Keys", context)
    if not isinstance(key_rows, list):
        raise ParticleCurveError(f"Curve keys must be an array: field={context}.Keys")
    keys = tuple(_key(key, f"{context}.Keys[{index}]") for index, key in enumerate(key_rows))
    if any(left.time >= right.time for left, right in zip(keys, keys[1:])):
        raise ParticleCurveError(f"Curve key times must increase strictly: field={context}.Keys")
    return ParticleCurve(
        keys,
        _integer(_required(row, "PreInfinity", context), f"{context}.PreInfinity"),
        _integer(_required(row, "PostInfinity", context), f"{context}.PostInfinity"),
        _integer(_required(row, "RotationOrder", context), f"{context}.RotationOrder"),
    )


def parse_minmax_curve(value: object, context: str) -> ParticleMinMaxCurve:
    """Read the required current SceneProbe MinMaxCurve fields, ignoring extras."""
    row = _record(value, context)
    mode = _integer(_required(row, "MinMaxState", context), f"{context}.MinMaxState")
    if mode not in (0, 1, 2, 3):
        raise UnsupportedParticleCurve(f"Unknown particle curve mode: field={context}, mode={mode}")
    return ParticleMinMaxCurve(
        mode,
        _finite(_required(row, "Scalar", context), f"{context}.Scalar"),
        _finite(_required(row, "MinScalar", context), f"{context}.MinScalar"),
        parse_particle_curve(_required(row, "MaxCurve", context), f"{context}.MaxCurve"),
        parse_particle_curve(_required(row, "MinCurve", context), f"{context}.MinCurve"),
    )


def evaluate_particle_curve(curve: ParticleCurve, time: float) -> float:
    """Evaluate Hermite segments with explicitly supported source wrap modes."""
    sample_time = _finite(time, "sampleTime")
    keys = curve.keys
    if not keys:
        raise ParticleCurveError("Cannot evaluate an active curve without keys")
    if sample_time < keys[0].time or sample_time > keys[-1].time:
        wrap_mode = curve.pre_infinity if sample_time < keys[0].time else curve.post_infinity
        span = keys[-1].time - keys[0].time
        if wrap_mode != 2 or span <= 0:
            raise UnsupportedParticleCurve(
                f"Curve extrapolation is not verified: time={sample_time}, "
                f"domain=({keys[0].time}, {keys[-1].time}), "
                f"preInfinity={curve.pre_infinity}, postInfinity={curve.post_infinity}"
            )
        sample_time = keys[0].time + (sample_time - keys[0].time) % span
    index = bisect_right(tuple(key.time for key in keys), sample_time) - 1
    left = keys[index]
    if left.time == sample_time:
        return left.value
    right = keys[index + 1]
    if left.weighted_mode & 2 or right.weighted_mode & 1:
        raise UnsupportedParticleCurve(
            f"Weighted curve segment is not verified: start={left.time}, end={right.time}"
        )
    if math.isinf(left.out_slope) or math.isinf(right.in_slope):
        return left.value
    span = right.time - left.time
    t = (sample_time - left.time) / span
    t2, t3 = t * t, t * t * t
    value = ((2 * t3 - 3 * t2 + 1) * left.value
             + (t3 - 2 * t2 + t) * span * left.out_slope
             + (-2 * t3 + 3 * t2) * right.value
             + (t3 - t2) * span * right.in_slope)
    return _finite(value, "evaluatedCurveValue")


def evaluate_minmax_curve(curve: ParticleMinMaxCurve, time: float, random_factor: float) -> float:
    """Keep the caller's stable random factor and the shared curve multiplier."""
    sample_time = _finite(time, "sampleTime")
    factor = _finite(random_factor, "randomFactor")
    if not 0 <= factor <= 1:
        raise ParticleCurveError(f"Random factor must be in [0, 1]: value={factor}")
    if curve.mode == 0:
        return curve.scalar
    if curve.mode == 3:
        value = (1 - factor) * curve.min_scalar + factor * curve.scalar
    elif curve.mode == 1:
        value = evaluate_particle_curve(curve.max_curve, sample_time) * curve.scalar
    elif curve.mode == 2:
        low = evaluate_particle_curve(curve.min_curve, sample_time)
        high = evaluate_particle_curve(curve.max_curve, sample_time)
        value = ((1 - factor) * low + factor * high) * curve.scalar
    else:
        raise UnsupportedParticleCurve(f"Unknown particle curve mode: mode={curve.mode}")
    return _finite(value, "evaluatedMinMaxValue")
