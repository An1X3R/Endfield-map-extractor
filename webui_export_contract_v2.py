"""Versioned, dependency-free WebUI export contract.

This module only validates and canonicalizes requests. It deliberately does not
start Blender or touch the game installation; an HTTP or desktop adapter can
call it before creating an asynchronous job.
"""

from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from webui_source_contract import normalize_export_groups, validate_source_paths


CONTRACT_FORMAT = "EndfieldWebUIExportJob/2"
CANONICAL_FORMAT = "EndfieldCanonicalExportJob/2"
EVENT_FORMAT = "EndfieldWebUIEvent/1"
JOB_STATES = ("queued", "preparing", "extracting", "building", "auditing", "completed", "failed", "cancelled")
JOB_PHASES = ("queued", "preparing", "extracting", "building", "auditing", "completed", "failed", "cancelled")
SELECTION_MODES = ("full_system_by_anchor", "anchor_overlap", "all_active")
WATER_MODES = ("stable_eevee", "flowmap_fresnel")
BATCH_MODES = ("per_sector", "cluster_4x4_sectors", "merged_selection")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


@dataclass(frozen=True)
class Bounds:
    xmin: float
    xmax: float
    zmin: float
    zmax: float

    def validate(self) -> None:
        values = (self.xmin, self.xmax, self.zmin, self.zmax)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Selection bounds must be finite")
        if self.xmax <= self.xmin or self.zmax <= self.zmin:
            raise ValueError("Selection bounds must have positive area")


@dataclass(frozen=True)
class MapSpec:
    map_id: str
    world_bounds: Bounds
    selection_grid: float
    preview_v_direction: str
    baseline_policy: str
    water_binding_status: str

    def validate(self) -> None:
        self.world_bounds.validate()
        if not math.isfinite(self.selection_grid) or self.selection_grid <= 0:
            raise ValueError("selection_grid must be positive and finite")
        if self.preview_v_direction not in {"z_increasing", "z_decreasing"}:
            raise ValueError("preview_v_direction is invalid")


MAP_SPECS = {
    # Map01 preview uses v=1-(unityZ+1024)/2048. Central water binding remains unresolved.
    "map01": MapSpec("map01", Bounds(-1024.0, 1024.0, -1024.0, 1024.0), 128.0, "z_decreasing", "map01_formal_baseline", "external_candidate_only"),
    "map02": MapSpec("map02", Bounds(-2048.0, 2048.0, -2048.0, 2048.0), 128.0, "z_increasing", "map02_versioned_baseline", "map_local_rules"),
}


def _ordered(first: Any, second: Any) -> tuple[float, float]:
    first_value, second_value = float(first), float(second)
    return (first_value, second_value) if first_value <= second_value else (second_value, first_value)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(max(value, minimum), maximum)


def _snap_down(value: float, origin: float, grid: float) -> float:
    return origin + math.floor((value - origin) / grid) * grid


def _snap_up(value: float, origin: float, grid: float) -> float:
    return origin + math.ceil((value - origin) / grid) * grid


def _preview_bounds(selection: Mapping[str, Any], spec: MapSpec) -> Bounds:
    umin, umax = _ordered(selection["umin"], selection["umax"])
    vmin, vmax = _ordered(selection["vmin"], selection["vmax"])
    if not all(0.0 <= value <= 1.0 for value in (umin, umax, vmin, vmax)):
        raise ValueError("Preview coordinates must be within [0, 1]")
    world = spec.world_bounds
    width, height = world.xmax - world.xmin, world.zmax - world.zmin
    xmin, xmax = world.xmin + umin * width, world.xmin + umax * width
    if spec.preview_v_direction == "z_increasing":
        zmin, zmax = world.zmin + vmin * height, world.zmin + vmax * height
    else:
        zmin, zmax = world.zmax - vmax * height, world.zmax - vmin * height
    return Bounds(xmin, xmax, zmin, zmax)


def _world_bounds(selection: Mapping[str, Any]) -> Bounds:
    xmin, xmax = _ordered(selection["xmin"], selection["xmax"])
    zmin, zmax = _ordered(selection["zmin"], selection["zmax"])
    return Bounds(xmin, xmax, zmin, zmax)


def _resolve_bounds(selection: Mapping[str, Any], spec: MapSpec) -> Bounds:
    selection_type = selection.get("type")
    raw = _preview_bounds(selection, spec) if selection_type == "preview_rect" else _world_bounds(selection) if selection_type == "world_bounds" else None
    if raw is None:
        raise ValueError(f"Unsupported selection type: {selection_type!r}")
    raw.validate()
    world = spec.world_bounds
    if raw.xmax <= world.xmin or raw.xmin >= world.xmax or raw.zmax <= world.zmin or raw.zmin >= world.zmax:
        raise ValueError("Selection does not intersect the map")
    clipped = Bounds(_clamp(raw.xmin, world.xmin, world.xmax), _clamp(raw.xmax, world.xmin, world.xmax), _clamp(raw.zmin, world.zmin, world.zmax), _clamp(raw.zmax, world.zmin, world.zmax))
    snapped = Bounds(
        _clamp(_snap_down(clipped.xmin, world.xmin, spec.selection_grid), world.xmin, world.xmax),
        _clamp(_snap_up(clipped.xmax, world.xmin, spec.selection_grid), world.xmin, world.xmax),
        _clamp(_snap_down(clipped.zmin, world.zmin, spec.selection_grid), world.zmin, world.zmax),
        _clamp(_snap_up(clipped.zmax, world.zmin, spec.selection_grid), world.zmin, world.zmax),
    )
    snapped.validate()
    return snapped


def _validate_output(output: Mapping[str, Any], selected_game_root: str | None = None) -> dict[str, str]:
    root_text = str(output.get("root") or "")
    if not root_text:
        raise ValueError("output.root is required")
    root = Path(root_text).expanduser().resolve()
    game_root_text = selected_game_root or os.environ.get("ENDFIELD_GAME_ROOT")
    game_root = Path(game_root_text).expanduser().resolve() if game_root_text else None
    if root == Path(root.anchor):
        raise ValueError("output.root cannot be a drive root; select a dedicated folder")
    if not root.is_dir():
        raise ValueError(f"output.root is not an existing directory: {root}")
    if not os.access(root, os.W_OK):
        raise ValueError(f"output.root is not writable: {root}")
    if game_root is not None:
        try:
            root.relative_to(game_root)
        except ValueError:
            pass
        else:
            raise ValueError("output.root cannot be inside the game installation")
        try:
            game_root.relative_to(root)
        except ValueError:
            pass
        else:
            raise ValueError("output.root cannot contain the game installation")
    label = str(output.get("label") or "webui")
    if not LABEL_RE.fullmatch(label):
        raise ValueError("output.label must be 1-80 ASCII characters: letters, digits, '.', '_' or '-'")
    return {"root": str(root), "label": label}


def canonicalize_job(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("format") != CONTRACT_FORMAT:
        raise ValueError(f"Unsupported job format: {payload.get('format')!r}")
    map_id = str(payload.get("map_id") or "")
    spec = MAP_SPECS.get(map_id)
    if spec is None:
        raise ValueError(f"Unsupported map_id: {map_id!r}")
    spec.validate()
    bounds = _resolve_bounds(dict(payload.get("selection") or {}), spec)
    layers = dict(payload.get("layers") or {})
    normalized_layers = {
        name: bool(layers.get(name, True))
        for name in ("terrain", "instances", "roads", "vegetation", "water", "effects", "lights")
    }
    if not any(normalized_layers.values()):
        raise ValueError("At least one export layer must be enabled")
    water_mode = str(payload.get("water_mode") or "stable_eevee")
    if water_mode not in WATER_MODES:
        raise ValueError(f"Unsupported water_mode: {water_mode!r}")
    effects = dict(payload.get("effects") or {})
    selection_mode = str(effects.get("selection_mode") or "full_system_by_anchor")
    if selection_mode not in SELECTION_MODES:
        raise ValueError(f"Unsupported effects.selection_mode: {selection_mode!r}")
    batch_mode = str(payload.get("batch_mode") or "per_sector")
    if batch_mode not in BATCH_MODES:
        raise ValueError(f"Unsupported batch_mode: {batch_mode!r}")
    raw_source = payload.get("source")
    source = None if raw_source is None else validate_source_paths(dict(raw_source))
    export_mode = payload.get("export_mode", "data_package")
    if export_mode not in ("data_package", "blend"):
        raise ValueError(f"Unsupported export_mode: {export_mode!r}")
    if export_mode == "blend" and (source is None or not source.get("blender_exe")):
        raise ValueError("Blender scene export requires source.blender_exe and source.game_root")
    export_groups = normalize_export_groups(payload.get("export_groups"))
    request_id = payload.get("request_id")
    if request_id is not None and not isinstance(request_id, str):
        raise ValueError("request_id must be a string or null")
    raw_job_id = payload.get("job_id")
    if raw_job_id is None:
        job_id = str(uuid.uuid4())
    elif not isinstance(raw_job_id, str) or not JOB_ID_RE.fullmatch(raw_job_id):
        raise ValueError("job_id must be 1-80 ASCII characters: letters, digits, '.', '_' or '-'")
    else:
        job_id = raw_job_id
    return {
        "format": CANONICAL_FORMAT,
        "request_id": request_id,
        "job_id": job_id,
        "map_id": map_id,
        "bounds": asdict(bounds),
        "bounds_semantics": "half_open_xz",
        "selection_grid": spec.selection_grid,
        "preview": {"origin": "top_left", "v_direction": spec.preview_v_direction},
        "baseline_policy": spec.baseline_policy,
        "water_binding_status": spec.water_binding_status,
        "source": source,
        "export_mode": export_mode,
        "layers": normalized_layers,
        "export_groups": export_groups,
        "water_mode": water_mode,
        "effects": {"selection_mode": selection_mode},
        "batch_mode": batch_mode,
        "output": _validate_output(
            dict(payload.get("output") or {}),
            str(source.get("game_root")) if source else None,
        ),
    }


def estimate_workload(canonical: Mapping[str, Any]) -> dict[str, int]:
    bounds = canonical["bounds"]
    grid = float(canonical["selection_grid"])
    width = max(1, math.ceil((float(bounds["xmax"]) - float(bounds["xmin"])) / grid))
    height = max(1, math.ceil((float(bounds["zmax"]) - float(bounds["zmin"])) / grid))
    layers = canonical.get("layers") or {}
    enabled_layers = sum(1 for name in ("terrain", "instances", "water", "effects") if layers.get(name))
    return {"grid_width": width, "grid_height": height, "grid_cells": width * height, "enabled_layers": enabled_layers}


def new_job_record(canonical: Mapping[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {"format": "EndfieldWebUIJob/1", "job_id": canonical["job_id"], "state": "queued", "phase": "queued", "progress": 0.0, "created_at": now, "updated_at": now, "canonical": dict(canonical), "estimate": estimate_workload(canonical), "result": None, "error": None, "cancel_requested": False}


def make_event(job_id: str, sequence: int, state: str, phase: str, progress: float, message: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if state not in JOB_STATES or phase not in JOB_PHASES:
        raise ValueError("Unknown job state or phase")
    if not 0.0 <= progress <= 1.0:
        raise ValueError("progress must be within [0, 1]")
    return {"format": EVENT_FORMAT, "job_id": job_id, "sequence": int(sequence), "timestamp": datetime.now(timezone.utc).isoformat(), "state": state, "phase": phase, "progress": float(progress), "message": str(message), "payload": dict(payload or {})}


def validate_transition(previous: str, next_state: str) -> None:
    allowed = {"queued": {"preparing", "cancelled", "failed"}, "preparing": {"extracting", "cancelled", "failed"}, "extracting": {"building", "cancelled", "failed"}, "building": {"auditing", "cancelled", "failed"}, "auditing": {"completed", "cancelled", "failed"}, "completed": set(), "failed": set(), "cancelled": set()}
    if next_state not in allowed.get(previous, set()):
        raise ValueError(f"Invalid job transition: {previous} -> {next_state}")
