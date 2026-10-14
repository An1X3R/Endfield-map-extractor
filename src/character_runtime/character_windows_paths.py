"""Build native paths that pass Windows' extended-length path prefix."""
from __future__ import annotations

import os
from pathlib import Path


def extended_path(path: Path | str) -> str:
    """Return an absolute Windows path that supports paths longer than MAX_PATH."""
    raw_path = os.fspath(path)
    if os.name != "nt" or raw_path.startswith("\\\\?\\"):
        return raw_path
    absolute_path = os.path.abspath(raw_path)
    if absolute_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute_path[2:]
    return "\\\\?\\" + absolute_path


def io_path(path: Path | str) -> Path:
    """Return a pathlib path prepared for extended-length local file access."""
    return Path(extended_path(path))
