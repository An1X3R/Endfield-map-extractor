"""Read the verified global PhysicsManager settings from a Unity data file."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast


class PhysicsSettingsError(RuntimeError):
    """The source file does not contain one readable PhysicsManager gravity."""


@dataclass(frozen=True, slots=True)
class PhysicsSettings:
    """Physics values tied to the exact serialized source object and file."""

    source: str
    source_sha256: str
    path_id: int
    gravity: tuple[float, float, float]


def _finite_gravity(value: object, source: Path, path_id: int) -> tuple[float, float, float]:
    if not isinstance(value, Mapping):
        raise PhysicsSettingsError(
            f"PhysicsManager.m_Gravity is not a vector mapping: source={source}, path_id={path_id}"
        )

    fields = cast(Mapping[object, object], value)
    components: list[float] = []
    for axis in ("x", "y", "z"):
        component = fields.get(axis)
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise PhysicsSettingsError(
                f"PhysicsManager.m_Gravity.{axis} is not numeric: source={source}, path_id={path_id}"
            )
        numeric = float(component)
        if not math.isfinite(numeric):
            raise PhysicsSettingsError(
                f"PhysicsManager.m_Gravity.{axis} is not finite: source={source}, path_id={path_id}"
            )
        components.append(numeric)
    return components[0], components[1], components[2]


def read_physics_settings(source: Path) -> PhysicsSettings:
    """Read one exact PhysicsManager object without modifying its Unity file."""
    if not isinstance(source, Path):
        raise TypeError(f"source must be a pathlib.Path, got {type(source).__name__}")

    resolved_source = source.expanduser().resolve()
    if not resolved_source.is_file():
        raise FileNotFoundError(f"PhysicsManager source is not a file: {resolved_source}")
    source_bytes = resolved_source.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    try:
        import UnityPy
    except ImportError as exc:
        raise PhysicsSettingsError(
            "UnityPy is required to read globalgamemanagers; install the project's pinned UnityPy dependency"
        ) from exc

    environment: object = UnityPy.load(source_bytes, path=str(resolved_source))

    raw_objects = getattr(environment, "objects", None)
    if not isinstance(raw_objects, Iterable) or isinstance(raw_objects, (str, bytes, bytearray)):
        raise PhysicsSettingsError(
            f"UnityPy returned no object collection: source={resolved_source}"
        )

    manager_objects = tuple(
        asset
        for asset in raw_objects
        if getattr(getattr(asset, "type", None), "name", None) == "PhysicsManager"
    )
    if len(manager_objects) != 1:
        raise PhysicsSettingsError(
            "Expected exactly one PhysicsManager object: "
            f"source={resolved_source}, count={len(manager_objects)}"
        )

    manager = manager_objects[0]
    raw_path_id = getattr(manager, "path_id", None)
    if isinstance(raw_path_id, bool) or not isinstance(raw_path_id, int):
        raise PhysicsSettingsError(
            f"PhysicsManager path_id is not an integer: source={resolved_source}"
        )
    read_typetree = getattr(manager, "read_typetree", None)
    if not callable(read_typetree):
        raise PhysicsSettingsError(
            f"PhysicsManager has no readable type tree: source={resolved_source}, path_id={raw_path_id}"
        )
    type_tree: object = read_typetree()
    if not isinstance(type_tree, Mapping):
        raise PhysicsSettingsError(
            f"PhysicsManager type tree is not a mapping: source={resolved_source}, path_id={raw_path_id}"
        )
    tree_fields = cast(Mapping[object, object], type_tree)
    if "m_Gravity" not in tree_fields:
        raise PhysicsSettingsError(
            f"PhysicsManager.m_Gravity is missing: source={resolved_source}, path_id={raw_path_id}"
        )
    gravity = _finite_gravity(tree_fields["m_Gravity"], resolved_source, raw_path_id)

    return PhysicsSettings(
        source=str(resolved_source),
        source_sha256=source_sha256,
        path_id=raw_path_id,
        gravity=gravity,
    )
