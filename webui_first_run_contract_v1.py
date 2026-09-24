"""Versioned backend contract for WebUI-triggered first-run data preparation."""

from __future__ import annotations

from pathlib import Path
import os
import re
import shutil
from typing import Any, Mapping


REQUEST_FORMAT = "EndfieldWebUIFirstRunRequest/1"
VALIDATION_FORMAT = "EndfieldWebUIFirstRunValidation/1"
STATUS_FORMAT = "EndfieldWebUIFirstRunStatus/1"
EVENTS_FORMAT = "EndfieldWebUIFirstRunEvents/1"
ERROR_FORMAT = "EndfieldWebUIFirstRunError/1"
RUN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
BASELINE_CACHE_BYTES = 64 * 1024**3
BASELINE_EXPORT_BYTES = 2 * 1024**3


class FirstRunRequestError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.details = dict(details or {})


def error_response(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": ERROR_FORMAT,
        "error": {"code": code, "message": message, "retryable": retryable},
    }
    if details:
        payload["error"]["details"] = dict(details)
    return payload


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _writable_parent(path: Path) -> bool:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK)


def _disk_space(path: Path, required_bytes: int) -> dict[str, Any]:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    try:
        usage = shutil.disk_usage(candidate)
    except OSError:
        return {
            "status": "unknown",
            "path": str(path),
            "required_bytes": required_bytes,
            "free_bytes": None,
            "warning": True,
        }
    return {
        "status": "baseline_estimate",
        "path": str(path),
        "required_bytes": required_bytes,
        "free_bytes": usage.free,
        "warning": usage.free < required_bytes,
    }


def canonicalize_first_run_request(
    payload: Mapping[str, Any],
    *,
    default_run_name: str = "bootstrap_v1",
) -> dict[str, Any]:
    if payload.get("format") != REQUEST_FORMAT:
        raise FirstRunRequestError("invalid_request", f"format must be {REQUEST_FORMAT}")

    game_text = payload.get("game_root")
    export_text = payload.get("export_root")
    cache_text = payload.get("cache_root")
    blender_provided = "blender_exe" in payload
    blender_text = payload.get("blender_exe")
    run_name = payload.get("run_name", default_run_name)
    if not isinstance(game_text, str) or not game_text.strip():
        raise FirstRunRequestError("invalid_request", "game_root must be a non-empty path")
    if not isinstance(export_text, str) or not export_text.strip():
        raise FirstRunRequestError("invalid_request", "export_root must be a non-empty path")
    if cache_text is not None and (not isinstance(cache_text, str) or not cache_text.strip()):
        raise FirstRunRequestError(
            "invalid_request", "cache_root must be a non-empty path when provided"
        )
    if blender_provided and (
        not isinstance(blender_text, str) or not blender_text.strip()
    ):
        raise FirstRunRequestError(
            "invalid_request", "blender_exe must be a non-empty path when provided"
        )
    if not isinstance(run_name, str) or not RUN_NAME_RE.fullmatch(run_name):
        raise FirstRunRequestError(
            "invalid_request", "run_name must match [A-Za-z0-9][A-Za-z0-9._-]{0,79}"
        )

    game_root = Path(game_text).expanduser().resolve()
    export_root = Path(export_text).expanduser().resolve()
    cache_root = (
        Path(cache_text).expanduser().resolve()
        if isinstance(cache_text, str)
        else (export_root / "cache" / "first_run_work").resolve()
    )
    blender_exe = (
        Path(blender_text).expanduser().resolve()
        if blender_provided and isinstance(blender_text, str)
        else None
    )
    if any(
        (
            _inside(game_root, export_root),
            _inside(game_root, cache_root),
            _inside(export_root, game_root),
            _inside(cache_root, game_root),
        )
    ):
        raise FirstRunRequestError(
            "invalid_request",
            "game_root, export_root, and cache_root must be disjoint external roots",
        )
    if blender_exe is not None:
        if not blender_exe.is_file() or blender_exe.name.casefold() != "blender.exe":
            raise FirstRunRequestError(
                "invalid_request",
                f"blender_exe must point to an existing blender.exe: {blender_exe}",
            )
        if _inside(game_root, blender_exe):
            raise FirstRunRequestError(
                "invalid_request",
                "blender_exe must be outside game_root to preserve the game read-only boundary",
            )

    canonical = {
        "format": REQUEST_FORMAT,
        "game_root": str(game_root),
        "export_root": str(export_root),
        "cache_root": str(cache_root),
        "run_name": run_name,
    }
    if blender_exe is not None:
        canonical["blender_exe"] = str(blender_exe)
    return canonical


def validate_first_run_paths(request: Mapping[str, Any]) -> dict[str, Any]:
    game_root = Path(str(request["game_root"]))
    export_root = Path(str(request["export_root"]))
    cache_root = Path(str(request["cache_root"]))
    checks = {
        "game_executable": (game_root / "Endfield.exe").is_file(),
        "globalgamemanagers": (game_root / "Endfield_Data" / "globalgamemanagers").is_file(),
        "vfs": (game_root / "Endfield_Data" / "StreamingAssets" / "VFS").is_dir(),
        "export_root": export_root.is_dir(),
        "export_writable": export_root.is_dir() and os.access(export_root, os.W_OK),
        "cache_root_not_file": not cache_root.is_file(),
        "cache_parent_writable": _writable_parent(cache_root),
    }
    if "blender_exe" in request:
        blender_exe = Path(str(request["blender_exe"]))
        checks["blender_exe"] = (
            blender_exe.is_file() and blender_exe.name.casefold() == "blender.exe"
        )
    missing = [name for name, valid in checks.items() if not valid]
    space = {
        "cache": _disk_space(cache_root, BASELINE_CACHE_BYTES),
        "export": _disk_space(export_root, BASELINE_EXPORT_BYTES),
    }
    return {
        "format": VALIDATION_FORMAT,
        "status": "valid" if not missing else "incomplete",
        "game_read_only": True,
        "checks": checks,
        "missing": missing,
        "canonical": dict(request),
        "space": space,
        "space_warning": any(bool(item["warning"]) for item in space.values()),
        "prerequisites": {
            "git_available": shutil.which("git") is not None,
            "dotnet_available": shutil.which("dotnet") is not None,
            "network_required_for_initial_helper_checkout": True,
            "helper_commit_is_pinned": True,
        },
    }
