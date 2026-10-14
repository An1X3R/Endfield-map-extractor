"""Read and sample unweighted scalar facial animation curves from TypeTree probes."""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import cast


class FacialCurveFormatError(ValueError):
    """Raised when a serialized facial curve probe is missing required data."""


class UnsupportedWeightedModeError(ValueError):
    """Raised when sampling a curve whose tangent weighting is unsupported."""


@dataclass(frozen=True, slots=True)
class FacialCurveKey:
    time: float
    value: float
    in_slope: float
    out_slope: float
    weighted_mode: int
    in_weight: float
    out_weight: float


@dataclass(frozen=True, slots=True)
class FacialCurve:
    field_name: str
    control_name: str
    pre_infinity: int
    post_infinity: int
    rotation_order: int
    keys: tuple[FacialCurveKey, ...]


@dataclass(frozen=True, slots=True)
class FacialAnimation:
    asset_path: str
    source_cab: str
    source_path_id: str
    name: str
    duration: float
    additive_clip: int
    override_clip: int
    pause_auto_blink: int
    curves: tuple[FacialCurve, ...]


@dataclass(frozen=True, slots=True)
class FacialCurveValue:
    field_name: str
    control_name: str
    value: float


def _required(value: object, field_name: str, context: str) -> object:
    if not isinstance(value, dict) or field_name not in value:
        raise FacialCurveFormatError(f"Missing required field {context}.{field_name}")
    return cast(dict[str, object], value)[field_name]


def _as_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise FacialCurveFormatError(f"Expected object at {context}, got {type(value).__name__}")
    return cast(dict[str, object], value)


def _as_array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise FacialCurveFormatError(f"Expected array at {context}, got {type(value).__name__}")
    return cast(list[object], value)


def _as_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise FacialCurveFormatError(f"Expected non-empty string at {context}")
    return value


def _as_number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FacialCurveFormatError(f"Expected number at {context}, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise FacialCurveFormatError(f"Expected finite number at {context}, got {result}")
    return result


def _as_integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FacialCurveFormatError(f"Expected integer at {context}, got {type(value).__name__}")
    return value


def _as_path_id(value: object, context: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise FacialCurveFormatError(f"Expected decimal PathID at {context}")
    result = str(value)
    if not result or (result[0] == "-" and not result[1:].isdigit()) or (result[0] != "-" and not result.isdigit()):
        raise FacialCurveFormatError(f"Expected decimal PathID at {context}, got {result!r}")
    return result


def _read_curve(field_name: str, entry_value: object, curve_index: int) -> FacialCurve:
    context = f"{field_name}[{curve_index}]"
    entry = _as_object(entry_value, context)
    control_name = _as_text(_required(entry, "_ctrlName", context), f"{context}._ctrlName")
    raw_curve = _as_object(_required(entry, "_curve", context), f"{context}._curve")
    raw_keys = _as_array(_required(raw_curve, "m_Curve", f"{context}._curve"), f"{context}._curve.m_Curve")
    if not raw_keys:
        raise FacialCurveFormatError(f"Curve has no keys at {context}.m_Curve")
    keys: list[FacialCurveKey] = []
    previous_time: float | None = None
    for key_index, key_value in enumerate(raw_keys):
        key_context = f"{context}.m_Curve[{key_index}]"
        raw_key = _as_object(key_value, key_context)
        key = FacialCurveKey(
            time=_as_number(_required(raw_key, "time", key_context), f"{key_context}.time"),
            value=_as_number(_required(raw_key, "value", key_context), f"{key_context}.value"),
            in_slope=_as_number(_required(raw_key, "inSlope", key_context), f"{key_context}.inSlope"),
            out_slope=_as_number(_required(raw_key, "outSlope", key_context), f"{key_context}.outSlope"),
            weighted_mode=_as_integer(_required(raw_key, "weightedMode", key_context), f"{key_context}.weightedMode"),
            in_weight=_as_number(_required(raw_key, "inWeight", key_context), f"{key_context}.inWeight"),
            out_weight=_as_number(_required(raw_key, "outWeight", key_context), f"{key_context}.outWeight"),
        )
        if previous_time is not None and key.time <= previous_time:
            raise FacialCurveFormatError(f"Curve key times must be strictly increasing at {key_context}")
        previous_time = key.time
        keys.append(key)
    return FacialCurve(
        field_name=field_name,
        control_name=control_name,
        pre_infinity=_as_integer(_required(raw_curve, "m_PreInfinity", f"{context}._curve"), f"{context}.m_PreInfinity"),
        post_infinity=_as_integer(_required(raw_curve, "m_PostInfinity", f"{context}._curve"), f"{context}.m_PostInfinity"),
        rotation_order=_as_integer(_required(raw_curve, "m_RotationOrder", f"{context}._curve"), f"{context}.m_RotationOrder"),
        keys=tuple(keys),
    )


def read_facial_animation_probe(
    probe_path: str | Path,
    asset_path: str,
    expected_cab: str,
    expected_path_id: str,
) -> FacialAnimation:
    """Parse one exact MonoBehaviour TypeTree probe into immutable curve records."""
    path = Path(probe_path)
    records_value = json.loads(path.read_text(encoding="utf-8"))
    records = _as_array(records_value, str(path))
    if len(records) != 1:
        raise FacialCurveFormatError(f"Expected one probed object at {path}, got {len(records)}")
    record = _as_object(records[0], f"{path}[0]")
    source_cab = _as_text(_required(record, "source", str(path)), f"{path}.source")
    source_path_id = _as_path_id(_required(record, "pathId", str(path)), f"{path}.pathId")
    if source_cab != expected_cab or source_path_id != expected_path_id:
        raise FacialCurveFormatError(
            f"Probe identity mismatch at {path}: CAB={source_cab}, PathID={source_path_id}; "
            f"expected CAB={expected_cab}, PathID={expected_path_id}"
        )
    type_tree = _as_object(_required(record, "typeTree", str(path)), f"{path}.typeTree")
    name = _as_text(_required(type_tree, "m_Name", f"{path}.typeTree"), f"{path}.typeTree.m_Name")
    duration = _as_number(_required(type_tree, "_duration", f"{path}.typeTree"), f"{path}.typeTree._duration")
    if duration <= 0.0:
        raise FacialCurveFormatError(f"Animation duration must be positive at {path}: {duration}")
    curves: list[FacialCurve] = []
    for field_name, field_value in type_tree.items():
        if "Curve" not in field_name:
            continue
        entries = _as_array(field_value, f"{path}.typeTree.{field_name}")
        for curve_index, entry in enumerate(entries):
            curves.append(_read_curve(field_name, entry, curve_index))
    if not curves:
        raise FacialCurveFormatError(f"No scalar curves found at {path}")
    return FacialAnimation(
        asset_path=asset_path,
        source_cab=source_cab,
        source_path_id=source_path_id,
        name=name,
        duration=duration,
        additive_clip=_as_integer(_required(type_tree, "_bIsAdditiveClip", f"{path}.typeTree"), f"{path}.typeTree._bIsAdditiveClip"),
        override_clip=_as_integer(_required(type_tree, "_bIsOverride", f"{path}.typeTree"), f"{path}.typeTree._bIsOverride"),
        pause_auto_blink=_as_integer(_required(type_tree, "_bIsPauseAutoBlink", f"{path}.typeTree"), f"{path}.typeTree._bIsPauseAutoBlink"),
        curves=tuple(curves),
    )


def evaluate_curve(keys: tuple[FacialCurveKey, ...], time_seconds: float) -> float:
    """Evaluate an unweighted scalar curve with cubic Hermite interpolation."""
    if not math.isfinite(time_seconds):
        raise ValueError(f"Sample time must be finite, got {time_seconds}")
    if not keys:
        raise ValueError("Cannot evaluate a curve without keys")
    unsupported_modes = sorted({key.weighted_mode for key in keys if key.weighted_mode != 0})
    if unsupported_modes:
        raise UnsupportedWeightedModeError(
            f"Only weightedMode=0 is implemented; encountered modes {unsupported_modes}"
        )
    if any(right.time <= left.time for left, right in zip(keys, keys[1:])):
        raise ValueError("Curve key times must be strictly increasing")
    if len(keys) == 1:
        return keys[0].value
    times = tuple(key.time for key in keys)
    if time_seconds <= times[0]:
        return keys[0].value
    if time_seconds >= times[-1]:
        return keys[-1].value
    left_index = max(0, min(bisect_left(times, time_seconds) - 1, len(keys) - 2))
    left = keys[left_index]
    right = keys[left_index + 1]
    duration = right.time - left.time
    if duration <= 0.0:
        raise ValueError(f"Curve segment duration must be positive, got {duration}")
    normalized_time = (time_seconds - left.time) / duration
    tangent_left = left.out_slope * duration
    tangent_right = right.in_slope * duration
    u2 = normalized_time * normalized_time
    u3 = u2 * normalized_time
    h00 = 2.0 * u3 - 3.0 * u2 + 1.0
    h10 = u3 - 2.0 * u2 + normalized_time
    h01 = -2.0 * u3 + 3.0 * u2
    h11 = u3 - u2
    return h00 * left.value + h10 * tangent_left + h01 * right.value + h11 * tangent_right


def evaluate_facial_animation(animation: FacialAnimation, time_seconds: float) -> tuple[FacialCurveValue, ...]:
    """Sample each source curve independently without merging control names."""
    if not math.isfinite(animation.duration) or animation.duration <= 0.0:
        raise ValueError(f"Animation duration must be positive and finite, got {animation.duration}")
    if not math.isfinite(time_seconds) or time_seconds < 0.0 or time_seconds > animation.duration:
        raise ValueError(
            f"Animation sample time must be within [0, {animation.duration}], got {time_seconds}"
        )
    return tuple(
        FacialCurveValue(
            field_name=curve.field_name,
            control_name=curve.control_name,
            value=evaluate_curve(curve.keys, time_seconds),
        )
        for curve in animation.curves
    )
