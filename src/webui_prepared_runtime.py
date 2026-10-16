"""Validate portable scene runtimes before exposing the preparation-ready gate."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from endfield_level_maps import level_map_source_context
from endfield_scene_export import scene_runtime
from endfield_scene_material_contract import require_object
from endfield_scene_resources import resolve_path
from webui_export_worker import load_audited_dataset
from extractor_core.instances import validate_instance_source_generation


def validate_prepared_runtime(runtime_path: Path, request: Mapping[str, object]) -> Path:
    """Require matching selected paths, audited layers and current source indexes."""
    runtime = scene_runtime(runtime_path, Path(str(request["game_root"])), "map01")
    scene_runtime(runtime_path, Path(str(request["game_root"])), "map02")
    paths = require_object(runtime.get("paths"), "runtime.paths")
    for key in ("game_root", "export_root", "cache_root"):
        selected = Path(str(request[key])).resolve()
        configured = resolve_path(paths.get(key), runtime_path.parent)
        if configured != selected:
            raise ValueError(f"Prepared runtime path differs from selection: {key}, selected={selected}, prepared={configured}")
    build = require_object(runtime.get("blenderBuild"), "runtime.blenderBuild")
    if build.get("status") != "ready":
        raise ValueError("Prepared runtime has no completed Blender scene capability; resume preparation")
    stage = resolve_path(paths.get("stage119_root"), runtime_path.parent)
    manifest, _ = load_audited_dataset(stage)
    extraction = require_object(runtime.get("extraction"), "runtime.extraction")
    if str(extraction.get("mapLayerFingerprint", "")).casefold() != str(manifest["datasetFingerprint"]).casefold():
        raise ValueError("Prepared layer fingerprint differs from the selected runtime")
    maps = require_object(extraction.get("maps"), "runtime.extraction.maps")
    for map_id in ("map01", "map02"):
        entry = require_object(maps.get(map_id), f"runtime map {map_id}")
        validate_instance_source_generation(Path(str(request["game_root"])), map_id,
                                            resolve_path(entry.get("instances"), runtime_path.parent))
    level_map_source_context(runtime_path)
    return stage
