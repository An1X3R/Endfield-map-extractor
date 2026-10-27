"""Validate scene keys before using them in VFS paths or cache directories."""
from __future__ import annotations

import re


def validate_scene_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value) is None:
        raise ValueError(f"Invalid scene ID: {value!r}; expected lowercase ASCII letters, digits and underscores")
    return value


def static_level_namespace(map_id: str, value: object) -> int:
    """Read an explicit level scope for the source's five-bit static entity namespace."""
    match = re.fullmatch(rf"{re.escape(map_id)}_lv([0-9]{{3}})", value) if isinstance(value, str) else None
    if match is None or not 0 < int(match[1]) < 32:
        raise ValueError(f"static_level_id must name a level in {map_id} with namespace 1..31: {value!r}")
    return int(match[1])
