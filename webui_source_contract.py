"""Source-path and export-group validation shared by future WebUI adapters."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping


GAME_ROOT_NAME = "Endfield Game"
EXPORT_GROUPS = (
    "lighting",
    "particle",
    "vegetation",
    "terrain",
    "road",
    "water",
    "effects",
    "unknown",
)


def _path(value: Any, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return Path(text).expanduser().resolve()


def validate_source_paths(source: Mapping[str, Any]) -> dict[str, Any]:
    """Validate selected paths without scanning or modifying the game install.

    The public WebUI exports signed data packages. Blender is therefore an
    optional future capability, not a prerequisite for a package export.
    """
    game_root = _path(source.get("game_root"), "source.game_root")
    if game_root.name.casefold() != GAME_ROOT_NAME.casefold():
        raise ValueError(f"source.game_root must select the '{GAME_ROOT_NAME}' directory")
    if not game_root.is_dir():
        raise ValueError(f"source.game_root is not an existing directory: {game_root}")
    blender_text = str(source.get("blender_exe") or "").strip()
    blender_exe = Path(blender_text).expanduser().resolve() if blender_text else None
    if blender_exe is not None:
        if blender_exe.name.casefold() != "blender.exe":
            raise ValueError("source.blender_exe must point to blender.exe")
        if not blender_exe.is_file():
            raise ValueError(f"source.blender_exe is not an existing file: {blender_exe}")
    export_root_text = os.environ.get("ENDFIELD_EXPORT_ROOT")
    if export_root_text:
        export_root = Path(export_root_text).expanduser().resolve()
        try:
            game_root.relative_to(export_root)
        except ValueError:
            pass
        else:
            raise ValueError("source.game_root cannot be inside the export root")
    result = {
        "game_root": str(game_root),
        "game_read_only": True,
        "package_detection": "pending",
    }
    if blender_exe is not None:
        result["blender_exe"] = str(blender_exe)
    return result


def normalize_export_groups(values: Any) -> list[str]:
    if values is None:
        return ["all"]
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError("export_groups must be an array of strings")
    normalized = [str(value).strip() for value in values if str(value).strip()]
    if not normalized or "all" in normalized:
        return ["all"]
    invalid = [value for value in normalized if value not in EXPORT_GROUPS]
    if invalid:
        raise ValueError("Unknown export groups: " + ", ".join(invalid))
    return list(dict.fromkeys(normalized))
