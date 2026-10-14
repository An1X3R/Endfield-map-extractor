"""Validate scene keys before using them in VFS paths or cache directories."""
from __future__ import annotations

import re


def validate_scene_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", value) is None:
        raise ValueError(f"Invalid scene ID: {value!r}; expected lowercase ASCII letters, digits and underscores")
    return value
