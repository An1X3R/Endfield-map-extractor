"""Explicit authored flow recipes for a single prepared scene selection."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal, TypedDict


class FlowRibbon(TypedDict):
    name: str
    center: tuple[float, float, float]
    radii: tuple[float, float]
    width: float
    height_wave: float
    phase: float
    segments: int


class FlowPresentation(TypedDict):
    format: Literal["EndfieldFlowPresentation/1"]
    coordinate_space: Literal["BLENDER_WORLD"]
    provenance: str
    color: tuple[float, float, float]
    speed: float
    opacity: float
    intensity: float
    streak_density: float
    ribbons: list[FlowRibbon]


def _record(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"effect_flow_presentation.{field} must be an object with string keys")
    return dict(value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"effect_flow_presentation.{field} must be nonempty text")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"effect_flow_presentation.{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"effect_flow_presentation.{field} is out of range") from error
    if not math.isfinite(number):
        raise ValueError(f"effect_flow_presentation.{field} must be finite")
    return number


def _vector(value: object, size: int, field: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(f"effect_flow_presentation.{field} must contain {size} numbers")
    return tuple(_number(item, f"{field}[{index}]") for index, item in enumerate(value))


def _ribbon(value: object, index: int) -> FlowRibbon:
    field = f"ribbons[{index}]"
    row = _record(value, field)
    center = _vector(row.get("center"), 3, f"{field}.center")
    radii = _vector(row.get("radii"), 2, f"{field}.radii")
    width = _number(row.get("width"), f"{field}.width")
    segments = row.get("segments")
    if type(segments) is not int or not 16 <= segments <= 4096:
        raise ValueError(f"effect_flow_presentation.{field}.segments must be an integer in 16..4096")
    if min(radii) <= 0 or width <= 0:
        raise ValueError(f"effect_flow_presentation.{field} requires positive radii and width")
    return {"name": _text(row.get("name"), f"{field}.name"),
            "center": (center[0], center[1], center[2]), "radii": (radii[0], radii[1]),
            "width": width, "height_wave": _number(row.get("height_wave"), f"{field}.height_wave"),
            "phase": _number(row.get("phase"), f"{field}.phase"), "segments": segments}


def read_flow_presentation(value: object) -> FlowPresentation:
    """Validate and normalize an authored recipe without changing the input."""
    source = _record(value, "recipe")
    if source.get("format") != "EndfieldFlowPresentation/1":
        raise ValueError("Unsupported effect_flow_presentation format")
    if source.get("coordinate_space") != "BLENDER_WORLD":
        raise ValueError("effect_flow_presentation requires explicit BLENDER_WORLD coordinates")
    rows = source.get("ribbons")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError("effect_flow_presentation.ribbons must contain 1..64 authored ribbons")
    ribbons = [_ribbon(row, index) for index, row in enumerate(rows)]
    names = [row["name"] for row in ribbons]
    if len(set(names)) != len(names):
        raise ValueError("effect_flow_presentation ribbon names must be unique")
    color = _vector(source.get("color"), 3, "color")
    opacity = _number(source.get("opacity"), "opacity")
    intensity = _number(source.get("intensity"), "intensity")
    density = _number(source.get("streak_density"), "streak_density")
    if min(color) < 0 or not 0 <= opacity <= 1 or intensity < 0 or density <= 0:
        raise ValueError("effect_flow_presentation requires nonnegative color/intensity, opacity0..1 and positive density")
    return {"format": "EndfieldFlowPresentation/1", "coordinate_space": "BLENDER_WORLD",
            "provenance": _text(source.get("provenance"), "provenance"),
            "color": (color[0], color[1], color[2]), "speed": _number(source.get("speed"), "speed"),
            "opacity": opacity, "intensity": intensity, "streak_density": density, "ribbons": ribbons}
