"""Versioned coordinator for bounded Endfield Blender reconstruction.

The worker only reads reviewed extraction caches. It never writes to the game
installation and refuses to overwrite an existing output directory or file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping

from endfield_runtime_config import (
    ConfigurationError,
    RuntimeConfig,
    configured_optional_path,
    configured_path,
    load_runtime_config,
    path_hint,
)


WORKER_FORMAT = "EndfieldBlenderSelectionWorker/1"
JOB_FORMAT = "EndfieldBlenderSelectionJob/1"
CANONICAL_FORMAT = "EndfieldCanonicalExportJob/2"
PROFILE_FORMAT = "EndfieldBlenderWorkerProfiles/1"
MANIFEST_FORMAT = "EndfieldBlenderSelectionBuild/1"
EVENT_FORMAT = "EndfieldBlenderWorkerEvent/1"
AXIS_CONTRACT = "(X,Y,Z)=(Unity X,-Unity Z,Unity Y)"
SECTOR_SIZE = 128.0
PROFILE_PATH = Path(__file__).with_name("endfield_blender_worker_profiles_v1.json")
AUDITOR_PATH = Path(__file__).with_name("blender_audit_endfield_selection_worker_v1.py")
MERGER_PATH = Path(__file__).with_name("blender_merge_endfield_selection_worker_v1.py")
MAP_BOUNDS = {
    "map01": (-1024.0, 1024.0, -1024.0, 1024.0),
    "map02": (-2048.0, 2048.0, -2048.0, 2048.0),
}
BATCH_MODES = {"per_sector", "cluster_4x4_sectors", "merged_selection"}
SECTOR_RE = re.compile(r"^sector:(-?\d+):(-?\d+)$")


class WorkerError(RuntimeError):
    pass


class JobCancelled(WorkerError):
    pass


def resolve_runtime_paths(args: argparse.Namespace) -> tuple[dict[str, Path | None], RuntimeConfig]:
    """Resolve CLI > environment > local config > portable worker defaults.

    Resolution is intentionally metadata-only. Runtime validation below remains
    responsible for checking game, cache, profile, and Blender prerequisites.
    """

    config = load_runtime_config(args.runtime_config, root=Path(__file__).resolve().parent)
    config_dir = config.directory
    paths = {
        "game_root": configured_path(
            args.game_root,
            env_name="ENDFIELD_GAME_ROOT",
            label="Endfield game root",
            config_value=config.value("game_root"),
            config_dir=config_dir,
            cli_option="--game-root",
            config_key="game_root",
        ),
        "export_root": configured_path(
            args.export_root,
            env_name="ENDFIELD_EXPORT_ROOT",
            label="Endfield export root",
            config_value=config.value("export_root"),
            config_dir=config_dir,
            cli_option="--export-root",
            config_key="export_root",
        ),
        "stage119_root": configured_path(
            args.stage119_root,
            env_name="ENDFIELD_STAGE119_ROOT",
            env_aliases=("ENDFIELD_MAP_LAYER_ROOT",),
            label="Stage119 cache root",
            config_value=config.value("stage119_root"),
            config_dir=config_dir,
            cli_option="--stage119-root",
            config_key="stage119_root",
        ),
        "blender_exe": configured_optional_path(
            args.blender_exe,
            env_name="ENDFIELD_BLENDER",
            label="Blender executable",
            config_value=config.value("blender_exe"),
            config_dir=config_dir,
            cli_option="--blender-exe",
            config_key="blender_exe",
        ),
        "profile_registry": configured_path(
            args.profile_registry,
            env_name="ENDFIELD_BLENDER_PROFILE_REGISTRY",
            label="Blender profile registry",
            config_value=config.value("blender_profile_registry"),
            config_dir=config_dir,
            kind="file",
            cli_option="--profile-registry",
            config_key="blender_profile_registry",
        ),
    }
    return paths, config


def utc_timestamp() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def file_descriptor(path: Path, root: Path | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    name = str(resolved)
    if root is not None:
        try:
            name = resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return {"path": name, "bytes": resolved.stat().st_size, "sha256": sha256_file(resolved)}


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_output_boundary(output_dir: Path, game_root: Path, export_root: Path) -> None:
    output = output_dir.resolve()
    if not is_inside(output, export_root):
        raise WorkerError(f"outputDir must be inside {export_root}: {output}")
    if is_inside(output, game_root):
        raise WorkerError(f"outputDir cannot be inside the game installation: {output}")
    if output.exists():
        raise WorkerError(f"refusing to overwrite existing output directory: {output}")


def ensure_game_read_only_boundary(game_root: Path) -> None:
    if not game_root.is_dir():
        raise WorkerError(
            f"Endfield game directory is missing: {game_root}. "
            + path_hint(cli_option="--game-root", env_name="ENDFIELD_GAME_ROOT", config_key="game_root")
        )
    vfs = game_root / "Endfield_Data" / "StreamingAssets" / "VFS"
    if not vfs.is_dir():
        raise WorkerError(
            f"Endfield game directory is incomplete; VFS is missing: {vfs}. "
            "Select the read-only 'Endfield Game' directory."
        )


def finite_bounds(value: Mapping[str, Any]) -> tuple[float, float, float, float]:
    bounds = tuple(float(value[name]) for name in ("xmin", "xmax", "zmin", "zmax"))
    if not all(math.isfinite(item) for item in bounds):
        raise WorkerError("bounds must be finite")
    if bounds[1] <= bounds[0] or bounds[3] <= bounds[2]:
        raise WorkerError("bounds must use positive half-open ranges")
    return bounds


def format_bounds(bounds: tuple[float, float, float, float]) -> dict[str, float]:
    return dict(zip(("xmin", "xmax", "zmin", "zmax"), bounds))


def parse_sector_key(value: str) -> tuple[int, int]:
    match = SECTOR_RE.fullmatch(value)
    if not match:
        raise WorkerError(f"invalid sector key: {value!r}")
    return int(match.group(1)), int(match.group(2))


def sector_key(sector: tuple[int, int]) -> str:
    return f"sector:{sector[0]}:{sector[1]}"


def sector_bounds(sector: tuple[int, int]) -> tuple[float, float, float, float]:
    x, z = sector
    return x * SECTOR_SIZE, (x + 1) * SECTOR_SIZE, z * SECTOR_SIZE, (z + 1) * SECTOR_SIZE


def sectors_for_bounds(bounds: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    xmin, xmax, zmin, zmax = bounds
    sx0, sx1 = math.floor(xmin / SECTOR_SIZE), math.ceil(xmax / SECTOR_SIZE) - 1
    sz0, sz1 = math.floor(zmin / SECTOR_SIZE), math.ceil(zmax / SECTOR_SIZE) - 1
    return [(x, z) for z in range(sz0, sz1 + 1) for x in range(sx0, sx1 + 1)]


def intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[1] and a[1] > b[0] and a[2] < b[3] and a[3] > b[2]


def intersection(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> tuple[float, float, float, float] | None:
    result = max(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), min(a[3], b[3])
    return result if result[0] < result[1] and result[2] < result[3] else None


def union_sector_bounds(sectors: Iterable[tuple[int, int]]) -> tuple[float, float, float, float]:
    values = list(sectors)
    if not values:
        raise WorkerError("cannot form bounds from an empty sector set")
    boxes = [sector_bounds(item) for item in values]
    return min(v[0] for v in boxes), max(v[1] for v in boxes), min(v[2] for v in boxes), max(v[3] for v in boxes)


def safe_label(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("_.")
    if not cleaned:
        raise WorkerError("job label is empty after ASCII sanitization")
    return cleaned[:80]


def _nested(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def normalize_job(
    raw: Mapping[str, Any],
    *,
    export_package: Path | None,
    output_override: Path | None,
    batch_override: str | None,
    log_override: Path | None,
    events_override: Path | None,
    cancel_override: Path | None,
    blender_override: Path | None,
    runtime_paths: Mapping[str, Path | None] | None = None,
) -> dict[str, Any]:
    runtime = dict(runtime_paths or {})
    configured_export_root = Path(str(runtime["export_root"])).resolve()
    configured_game_root = Path(str(runtime["game_root"])).resolve()
    configured_stage119_root = Path(str(runtime["stage119_root"])).resolve()
    configured_blender_exe = runtime.get("blender_exe")
    if raw.get("format") not in {JOB_FORMAT, CANONICAL_FORMAT}:
        raise WorkerError(f"unsupported job format: {raw.get('format')!r}")
    worker = dict(raw.get("blender") or {})
    source = dict(raw.get("source") or {})
    map_id = str(_nested(raw, "mapId", "map_id") or "")
    if map_id not in MAP_BOUNDS:
        raise WorkerError(f"unsupported mapId: {map_id!r}")
    bounds = finite_bounds(dict(raw.get("bounds") or {}))
    batch_mode = str(
        batch_override
        or _nested(raw, "batchMode", "batch_mode")
        or _nested(worker, "batchMode", "batch_mode")
        or "per_sector"
    )
    if batch_mode not in BATCH_MODES:
        raise WorkerError(f"unsupported batchMode: {batch_mode!r}")
    layers = {name: bool((raw.get("layers") or {}).get(name, False)) for name in (
        "terrain", "instances", "roads", "vegetation", "effects", "water", "lights"
    )}
    if not any(layers.values()):
        raise WorkerError("at least one layer must be enabled")
    effects = dict(raw.get("effects") or {})
    effects_mode = str(_nested(effects, "selectionMode", "selection_mode") or "full_system_by_anchor")
    if layers["effects"] and effects_mode != "full_system_by_anchor":
        raise WorkerError("effects must use full_system_by_anchor")
    raw_groups = _nested(raw, "exportGroups", "export_groups") or ["all"]
    export_groups = [str(value) for value in raw_groups]
    if not export_groups:
        export_groups = ["all"]
    output_value = output_override or _nested(raw, "outputDir", "output_dir") or _nested(worker, "outputDir", "output_dir")
    output_block = dict(raw.get("output") or {})
    if output_value is None and export_package is not None:
        output_value = export_package / "blender_build"
    if output_value is None and output_block.get("root"):
        output_value = Path(str(output_block["root"])) / (
            safe_label(str(output_block.get("label") or "webui"))
            + f"__{map_id}__{safe_label(str(_nested(raw, 'jobId', 'job_id') or 'job'))}__blender"
        )
    if output_value is None:
        raise WorkerError("outputDir is required")
    output_dir = Path(output_value).expanduser().resolve()
    label = safe_label(str(raw.get("label") or output_block.get("label") or raw.get("job_id") or raw.get("jobId") or "selection"))
    job_id = safe_label(str(_nested(raw, "jobId", "job_id") or label))
    log_value = log_override or _nested(raw, "logDir", "log_dir") or _nested(worker, "logDir", "log_dir")
    log_dir = Path(log_value).resolve() if log_value else (configured_export_root / "log" / f"{label}__{job_id}").resolve()
    events_value = events_override or _nested(raw, "eventsPath", "events_path") or _nested(worker, "eventsPath", "events_path")
    cancel_value = cancel_override or _nested(raw, "cancelMarker", "cancel_marker") or _nested(worker, "cancelMarker", "cancel_marker")
    sectors_value = _nested(raw, "sectorKeys", "sector_keys") or _nested(worker, "sectorKeys", "sector_keys") or []
    explicit_sectors = [str(value) for value in sectors_value]
    for value in explicit_sectors:
        parse_sector_key(value)
    derived = [sector_key(item) for item in sectors_for_bounds(bounds)]
    ordered_sectors = list(dict.fromkeys([*derived, *explicit_sectors]))
    stage119 = dict(raw.get("stage119") or {})
    fingerprint = str(
        _nested(stage119, "fingerprint", "datasetFingerprint")
        or raw.get("datasetFingerprint")
        or ""
    )
    if fingerprint and not re.fullmatch(r"[0-9A-F]{64}", fingerprint):
        raise WorkerError(f"invalid Stage119 fingerprint: {fingerprint}")
    blender_value = (
        blender_override
        or _nested(raw, "blenderExe", "blender_exe")
        or _nested(worker, "blenderExe", "blender_exe")
        or _nested(source, "blenderExe", "blender_exe")
        or configured_blender_exe
    )
    return {
        "format": JOB_FORMAT,
        "sourceFormat": raw.get("format"),
        "jobId": job_id,
        "label": label,
        "mapId": map_id,
        "bounds": format_bounds(bounds),
        "boundsSemantics": "half-open [min,max)",
        "sectorKeys": ordered_sectors,
        "derivedSectorKeys": derived,
        "batchMode": batch_mode,
        "layers": layers,
        "exportGroups": export_groups,
        "effects": {"selectionMode": effects_mode},
        "waterMode": str(_nested(raw, "waterMode", "water_mode") or "stable_eevee"),
        "outputDir": str(output_dir),
        "logDir": str(log_dir),
        "eventsPath": str(Path(events_value).resolve()) if events_value else str(log_dir / "events.jsonl"),
        "cancelMarker": str(Path(cancel_value).resolve()) if cancel_value else str(log_dir / "CANCEL_REQUESTED"),
        "gameRoot": str(Path(
            _nested(raw, "gameRoot", "game_root")
            or _nested(source, "gameRoot", "game_root")
            or configured_game_root
        ).resolve()),
        "exportRoot": str(configured_export_root),
        "stage119": {
            "root": str(Path(_nested(stage119, "root") or configured_stage119_root).resolve()),
            "fingerprint": fingerprint,
        },
        "blenderExe": str(Path(blender_value).resolve()) if blender_value else None,
        "exportPackage": str(export_package.resolve()) if export_package else None,
    }


def load_job_from_package(package: Path) -> dict[str, Any]:
    canonical_path = package / "canonical_request.json"
    selection_path = package / "selection.json"
    manifest_path = package / "export_manifest.json"
    audit_path = package / "audit.json"
    for path in (canonical_path, selection_path, manifest_path, audit_path):
        if not path.is_file():
            raise WorkerError(f"required file missing from export package: {path}")
    raw = read_json(canonical_path)
    selection = read_json(selection_path)
    manifest = read_json(manifest_path)
    audit = read_json(audit_path)
    if audit.get("status") != "passed":
        raise WorkerError("WebUI export package audit is not passing")
    if str(audit.get("canonicalRequestSha256") or "").upper() != sha256_file(canonical_path):
        raise WorkerError("WebUI export package canonical request hash mismatch")
    if str(audit.get("manifestSha256") or "").upper() != sha256_file(manifest_path):
        raise WorkerError("WebUI export package manifest hash mismatch")
    if str(audit.get("selectionSha256") or "").upper() != sha256_file(selection_path):
        raise WorkerError("WebUI export package selection hash mismatch")
    if selection.get("format") != "EndfieldRegionSelectionExport/1" or manifest.get("format") != "EndfieldWebUIRegionExport/1":
        raise WorkerError("WebUI export package format mismatch")
    canonical_map = str(_nested(raw, "mapId", "map_id") or "")
    if manifest.get("mapId") != canonical_map or selection.get("mapId") != canonical_map:
        raise WorkerError("WebUI export package mapId mismatch")
    canonical_bounds = format_bounds(finite_bounds(dict(raw.get("bounds") or {})))
    selection_bounds = format_bounds(finite_bounds(dict(selection.get("bounds") or {})))
    if selection_bounds != canonical_bounds or (manifest.get("selection") or {}).get("bounds") != selection.get("bounds"):
        raise WorkerError("WebUI export package bounds mismatch")
    manifest_selection = manifest.get("selection") or {}
    if manifest_selection.get("sectorKeys") != selection.get("sectorKeys"):
        raise WorkerError("WebUI export package sectorKeys mismatch")
    effects = dict(raw.get("effects") or {})
    canonical_effects = str(_nested(effects, "selectionMode", "selection_mode") or "full_system_by_anchor")
    if selection.get("effectsPolicy") != canonical_effects or manifest_selection.get("effectsPolicy") != canonical_effects:
        raise WorkerError("WebUI export package effects policy mismatch")
    fingerprint = str(manifest.get("datasetFingerprint") or "")
    if not re.fullmatch(r"[0-9A-F]{64}", fingerprint):
        raise WorkerError("WebUI export package has an invalid Stage119 fingerprint")
    raw = dict(raw)
    raw["sectorKeys"] = selection.get("sectorKeys") or []
    raw["datasetFingerprint"] = manifest.get("datasetFingerprint")
    return raw


def load_profiles(path: Path) -> dict[str, Any]:
    registry = read_json(path)
    if registry.get("format") != PROFILE_FORMAT:
        raise WorkerError(f"unsupported profile registry: {registry.get('format')!r}")
    fingerprint = str(registry.get("stage119Fingerprint") or "")
    if not re.fullmatch(r"[0-9A-F]{64}", fingerprint):
        raise WorkerError("profile registry has an invalid Stage119 fingerprint")
    shared = dict(registry.get("sharedLockedInputs") or {})
    for profile in registry.get("profiles") or []:
        set_name = profile.get("lockedInputSet")
        if set_name:
            if set_name not in shared:
                raise WorkerError(f"unknown shared locked-input set: {set_name}")
            profile["lockedInputs"] = [
                *shared[set_name],
                *(profile.get("lockedInputs") or []),
            ]
    return registry


def verify_worker_scripts(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = registry.get("workerScripts") or []
    roles = {str(item.get("role")) for item in items}
    if roles != {"coordinator", "auditor", "merger"}:
        raise WorkerError(f"profile registry workerScripts are incomplete: {sorted(roles)}")
    return [verify_locked_file(item) for item in items]


def verify_locked_file(
    item: Mapping[str, Any],
    verification_cache: dict[Path, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    path = Path(str(item["path"])).resolve()
    cached = verification_cache.get(path) if verification_cache is not None else None
    if cached is None:
        if not path.is_file():
            raise WorkerError(f"locked input missing: {path}")
        cached = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        if verification_cache is not None:
            verification_cache[path] = cached
    size = int(cached["bytes"])
    expected_size = item.get("bytes")
    if expected_size is not None and size != int(expected_size):
        raise WorkerError(f"locked input size changed: {path}")
    actual = str(cached["sha256"]).upper()
    if actual != str(item["sha256"]).upper():
        raise WorkerError(f"locked input hash changed: {path}")
    return {"path": str(path), "bytes": size, "sha256": actual, "role": item.get("role")}


def verify_probe_audit(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    audit_path = Path(str(item["path"])).resolve()
    verify_locked_file(item)
    payload = read_json(audit_path)
    if not bool((payload.get("summary") or {}).get("assertions_passed")):
        raise WorkerError(f"probe audit is not passing: {audit_path}")
    verified = []
    for probe in payload.get("probes") or []:
        path = Path(str(probe["path"])).resolve()
        expected = str(probe["sha256"]).upper()
        if not path.is_file() or sha256_file(path) != expected:
            raise WorkerError(f"reviewed probe changed or is missing: {path}")
        verified.append({"path": str(path), "sha256": expected})
    return verified


def verify_map01_probe_catalog(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    report_path = Path(str(item["path"])).resolve()
    verify_locked_file(item)
    payload = read_json(report_path)
    hashes = ((payload.get("probe_inputs") or {}).get("used_manifest_hashes") or {})
    if not hashes:
        raise WorkerError(f"Map01 probe catalog has no reviewed hashes: {report_path}")
    verified = []
    for value, expected_value in hashes.items():
        path = Path(str(value)).resolve()
        expected = str(expected_value).upper()
        if not path.is_file() or sha256_file(path) != expected:
            raise WorkerError(f"reviewed Map01 probe changed or is missing: {path}")
        verified.append({"path": str(path), "sha256": expected})
    return verified


def verify_profile(
    profile: Mapping[str, Any],
    blender_override: str | None,
    *,
    include_water: bool = True,
    verification_cache: dict[Path, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    locked_items = [
        item for item in profile.get("lockedInputs") or []
        if include_water or not str(item.get("role") or "").startswith("water_")
    ]
    locked = [verify_locked_file(item, verification_cache) for item in locked_items]
    locked_paths = {Path(item["path"]).resolve() for item in locked_items}
    water_payload = profile.get("waterPayload")
    if include_water and water_payload:
        required_water_paths = {
            Path(str(water_payload[name])).resolve()
            for name in ("builder", "modeSwitcher", "sourceWaterBlend", "objProbe", "flowmapAtlas")
        }
        missing_water_locks = sorted(str(path) for path in required_water_paths - locked_paths)
        if missing_water_locks:
            raise WorkerError(
                f"profile {profile['profileId']} water payload paths are not hash-locked: {missing_water_locks}"
            )
    if include_water and profile.get("mapId") == "map02" and not water_payload:
        raise WorkerError(f"profile {profile['profileId']} has no reviewed partial water candidate payload")
    probes: list[dict[str, Any]] = []
    if profile.get("probeAudit"):
        probes = verify_probe_audit(profile["probeAudit"])
    if profile.get("probeCatalog"):
        probes = verify_map01_probe_catalog(profile["probeCatalog"])
    blender = Path(blender_override or str(profile["blenderExe"])).resolve()
    if not blender.is_file():
        raise WorkerError(f"Blender executable missing: {blender}")
    if blender.name.lower() != "blender.exe":
        raise WorkerError(f"Blender override must point to blender.exe: {blender}")
    blender_hash = sha256_file(blender)
    allowed_hashes = {str(value).upper() for value in profile.get("allowedBlenderSha256") or []}
    if allowed_hashes and blender_hash not in allowed_hashes:
        raise WorkerError(f"Blender executable is not allowlisted for profile {profile['profileId']}: {blender}")
    return {
        "profileId": profile["profileId"],
        "status": profile["status"],
        "blender": {"path": str(blender), "bytes": blender.stat().st_size, "sha256": blender_hash},
        "lockedInputs": locked,
        "probeManifests": probes,
        "waterPayloadVerified": bool(include_water and water_payload),
    }


def profile_region(profile: Mapping[str, Any]) -> tuple[float, float, float, float]:
    return finite_bounds(profile["region"])


def profiles_for_map(registry: Mapping[str, Any], map_id: str) -> list[dict[str, Any]]:
    profiles = [dict(value) for value in registry.get("profiles") or [] if value.get("mapId") == map_id]
    if not profiles:
        raise WorkerError(f"no profiles configured for {map_id}")
    return profiles


def partition_bounds(
    bounds: tuple[float, float, float, float], profiles: Iterable[Mapping[str, Any]]
) -> list[tuple[dict[str, Any], tuple[float, float, float, float]]]:
    result = []
    for value in profiles:
        overlap = intersection(bounds, profile_region(value))
        if overlap is None:
            continue
        result.append((dict(value), overlap))
    xcuts = sorted({bounds[0], bounds[1], *(
        value for profile, _ in result for value in (max(bounds[0], profile_region(profile)[0]), min(bounds[1], profile_region(profile)[1]))
    )})
    zcuts = sorted({bounds[2], bounds[3], *(
        value for profile, _ in result for value in (max(bounds[2], profile_region(profile)[2]), min(bounds[3], profile_region(profile)[3]))
    )})
    for x0, x1 in zip(xcuts, xcuts[1:]):
        for z0, z1 in zip(zcuts, zcuts[1:]):
            if x1 <= x0 or z1 <= z0:
                continue
            center = ((x0 + x1) / 2.0, (z0 + z1) / 2.0)
            owners = [
                profile for profile, _ in result
                if profile_region(profile)[0] <= center[0] < profile_region(profile)[1]
                and profile_region(profile)[2] <= center[1] < profile_region(profile)[3]
            ]
            if len(owners) != 1:
                raise WorkerError(
                    f"selection is not fully covered or overlaps at {center}; owners={len(owners)}"
                )
    return result


def plan_logical_units(
    bounds: tuple[float, float, float, float], sectors: list[tuple[int, int]], batch_mode: str
) -> list[dict[str, Any]]:
    selected = [sector for sector in sectors if intersects(sector_bounds(sector), bounds)]
    if not selected:
        return []
    if batch_mode == "per_sector":
        groups = [([sector], sector_key(sector).replace(":", "_")) for sector in selected]
    elif batch_mode == "cluster_4x4_sectors":
        buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for sector in selected:
            buckets.setdefault((math.floor(sector[0] / 4), math.floor(sector[1] / 4)), []).append(sector)
        groups = [(items, f"cluster4_{key[0]}_{key[1]}") for key, items in sorted(buckets.items(), key=lambda row: (row[0][1], row[0][0]))]
    else:
        groups = [(selected, "merged_selection")]
    result = []
    for index, (items, tag) in enumerate(groups, 1):
        unit_bounds = bounds if batch_mode == "merged_selection" else intersection(bounds, union_sector_bounds(items))
        if unit_bounds is None:
            continue
        result.append({
            "unitId": f"unit-{index:04d}",
            "tag": tag,
            "sectorKeys": [sector_key(item) for item in items],
            "bounds": format_bounds(unit_bounds),
        })
    return result


def layer_status(job: Mapping[str, Any]) -> dict[str, Any]:
    requested = job["layers"]
    map_id = job["mapId"]
    status: dict[str, Any] = {}
    status["terrain"] = {"requested": requested["terrain"], "status": "built" if requested["terrain"] else "omitted"}
    exact_groups = set(job.get("exportGroups") or ["all"]) == {"all"}
    status["instances"] = {
        "requested": requested["instances"],
        "status": (
            "built" if requested["instances"] and exact_groups
            else "built_unfiltered_group_filter_deferred" if requested["instances"]
            else "omitted_after_build"
        ),
        "requestedExportGroups": job.get("exportGroups") or ["all"],
    }
    status["roads"] = {"requested": requested["roads"], "status": "included_in_instances_unfiltered" if requested["roads"] and requested["instances"] else "deferred_category_filter" if requested["roads"] else "omitted"}
    status["vegetation"] = {"requested": requested["vegetation"], "status": "included_in_instances_unfiltered" if requested["vegetation"] and requested["instances"] else "deferred_category_filter" if requested["vegetation"] else "omitted"}
    status["effects"] = {
        "requested": requested["effects"],
        "status": "deferred_missing_blender_payload" if requested["effects"] else "omitted",
        "selectionPolicy": job["effects"]["selectionMode"],
        "note": "Stage119 anchor membership is real, but the arbitrary-region Blender effect payload is not closed.",
    }
    status["water"] = {
        "requested": requested["water"],
        "status": (
            "candidate_overlay_not_built"
            if requested["water"] and map_id == "map01"
            else "candidate_overlay_pending"
            if requested["water"]
            else "omitted"
        ),
        "waterMode": job["waterMode"],
    }
    if requested["water"] and map_id == "map02":
        status["water"]["coverageStatus"] = "partial_reviewed_pcg_meshes"
    status["lights"] = {
        "requested": requested["lights"],
        "status": "not_started" if requested["lights"] and map_id == "map01" else "partial_zero_records" if requested["lights"] else "omitted",
        "recordCount": 0,
    }
    return status


def profile_command(
    profile: Mapping[str, Any], blender: Path, output: Path, bounds: tuple[float, float, float, float], job: Mapping[str, Any]
) -> tuple[list[str], dict[str, str]]:
    args = [str(blender), "--background", "--factory-startup", "--python", str(Path(profile["builder"]).resolve()), "--"]
    args.extend(["--work-root", str(Path(profile["workRoot"]).resolve())])
    args.extend(["--output", str(output.resolve())])
    args.extend(["--assets-db", str(Path(profile["assetsDb"]).resolve())])
    args.extend(["--terrain-surface", str(Path(profile["terrainSurface"]).resolve())])
    option_paths = {
        "terrainContentMask": "--terrain-content-mask",
        "probeRegistry": "--extra-probe-registry",
        "ecsMaterialBindings": "--ecs-material-bindings",
        "ecsGeometryBindings": "--ecs-geometry-bindings",
        "ecsInstanceBindings": "--ecs-instance-bindings",
        "reviewedCompositePrefabBindings": "--reviewed-composite-prefab-bindings",
        "reviewedFamilyMaterialBindings": "--reviewed-family-material-bindings",
    }
    for name, option in option_paths.items():
        value = profile.get(name)
        if value:
            args.extend([option, str(Path(value).resolve())])
    if profile.get("probeCatalog"):
        catalog = read_json(Path(profile["probeCatalog"]["path"]))
        hashes = ((catalog.get("probe_inputs") or {}).get("used_manifest_hashes") or {})
        primary = (Path(profile["workRoot"]) / "scene_probe").resolve()
        for value in sorted(hashes):
            probe_root = Path(value).resolve().parent
            if probe_root != primary:
                args.extend(["--extra-probe", str(probe_root)])
    for option in profile.get("builderFlags") or []:
        args.append(str(option))
    if not job["layers"]["terrain"] and profile["mapId"] == "map02":
        args.append("--skip-terrain")
    args.extend([
        "--xmin", format(bounds[0], ".17g"), "--xmax", format(bounds[1], ".17g"),
        "--zmin", format(bounds[2], ".17g"), "--zmax", format(bounds[3], ".17g"),
    ])
    environment = os.environ.copy()
    environment["ENDFIELD_CANONICAL_SELECTION_BOUNDS"] = json.dumps(
        [float(job["bounds"][name]) for name in ("xmin", "xmax", "zmin", "zmax")],
        separators=(",", ":"),
    )
    if profile.get("baseBuilderEnv"):
        environment["ENDFIELD_BASE_BUILDER"] = str(Path(profile["baseBuilderEnv"]).resolve())
    return args, environment


def water_import_command(
    profile: Mapping[str, Any], blender: Path, input_blend: Path, output_blend: Path,
    audit_output: Path, bounds: tuple[float, float, float, float],
) -> list[str]:
    payload = profile.get("waterPayload")
    if not payload:
        raise WorkerError(f"profile {profile['profileId']} has no reviewed partial water candidate payload")
    return [
        str(blender), "--background", "--factory-startup", "--python",
        str(Path(payload["builder"]).resolve()), "--",
        "--input", str(input_blend.resolve()),
        "--source-water-blend", str(Path(payload["sourceWaterBlend"]).resolve()),
        "--obj-probe", str(Path(payload["objProbe"]).resolve()),
        "--flowmap-atlas", str(Path(payload["flowmapAtlas"]).resolve()),
        "--output", str(output_blend.resolve()),
        "--audit-output", str(audit_output.resolve()),
        "--xmin", format(bounds[0], ".17g"), "--xmax", format(bounds[1], ".17g"),
        "--zmin", format(bounds[2], ".17g"), "--zmax", format(bounds[3], ".17g"),
        "--stage", "121", "--parent-stage", "120",
    ]


def water_mode_command(
    profile: Mapping[str, Any], blender: Path, input_blend: Path, output_blend: Path,
    audit_output: Path, mode: str,
) -> list[str]:
    payload = profile.get("waterPayload")
    if not payload:
        raise WorkerError(f"profile {profile['profileId']} has no reviewed water payload")
    return [
        str(blender), "--background", "--factory-startup", "--python",
        str(Path(payload["modeSwitcher"]).resolve()), "--",
        "--input", str(input_blend.resolve()), "--output", str(output_blend.resolve()),
        "--audit-output", str(audit_output.resolve()), "--mode", mode,
    ]


def run_process(
    command: list[str], environment: Mapping[str, str], stdout_path: Path, stderr_path: Path,
    cancel_marker: Path, emitter: "EventEmitter", phase: str,
) -> None:
    if cancel_marker.exists():
        raise JobCancelled("cancel requested before Blender phase")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("x", encoding="utf-8", errors="replace") as stdout, stderr_path.open("x", encoding="utf-8", errors="replace") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=dict(environment), text=True)
        cancel_seen = False
        while process.poll() is None:
            if cancel_marker.exists() and not cancel_seen:
                cancel_seen = True
                emitter.emit("building", phase, emitter.progress, "Cancellation requested; waiting for the active Blender phase to finish", {"pid": process.pid, "cancelGranularity": "phase_boundary"})
            time.sleep(0.5)
        if process.returncode != 0 and cancel_marker.exists():
            raise JobCancelled(
                f"cancel requested and active process exited with code {process.returncode}"
            )
        if process.returncode != 0:
            raise WorkerError(f"process failed with exit code {process.returncode}: {command[0]}")
    if cancel_marker.exists():
        raise JobCancelled("cancel requested; active Blender phase completed")


class EventEmitter:
    def __init__(self, path: Path, job_id: str) -> None:
        self.path = path
        self.job_id = job_id
        self.sequence = 0
        self.progress = 0.0

    def emit(self, state: str, phase: str, progress: float, message: str, payload: Mapping[str, Any] | None = None) -> None:
        self.sequence += 1
        if state not in {"failed", "cancelled"}:
            progress = max(self.progress, progress)
        self.progress = progress
        event = {
            "format": EVENT_FORMAT,
            "jobId": self.job_id,
            "sequence": self.sequence,
            "timestamp": utc_timestamp(),
            "state": state,
            "phase": phase,
            "progress": progress,
            "message": message,
            "payload": dict(payload or {}),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        print(json.dumps(event, ensure_ascii=False), flush=True)


def blender_audit_command(
    blender: Path, blend: Path, audit: Path, unit_manifest: Path, expected_map: str,
    bounds: tuple[float, float, float, float], remove_instances: bool, remove_terrain: bool,
) -> list[str]:
    command = [
        str(blender), "--background", "--factory-startup", "--python", str(AUDITOR_PATH.resolve()), "--",
        "--input", str(blend.resolve()), "--output", str(audit.resolve()),
        "--unit-manifest", str(unit_manifest.resolve()), "--expected-map", expected_map,
        "--xmin", format(bounds[0], ".17g"), "--xmax", format(bounds[1], ".17g"),
        "--zmin", format(bounds[2], ".17g"), "--zmax", format(bounds[3], ".17g"), "--stamp",
    ]
    if remove_instances:
        command.append("--remove-instances")
    if remove_terrain:
        command.append("--remove-terrain")
    return command


def build_manifest_status(status: Mapping[str, Any]) -> str:
    requested = [value for value in status.values() if value.get("requested")]
    return "completed" if all(
        str(value["status"]).startswith("built")
        or value["status"] == "included_in_instances_unfiltered"
        for value in requested
    ) else "completed_with_deferred_layers"


def water_steps_for_job(job: Mapping[str, Any]) -> int:
    if job["mapId"] != "map02" or not job["layers"]["water"]:
        return 0
    return 2 if job["waterMode"] == "flowmap_fresnel" else 1


def validate_stage119(stage119: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(str(stage119["root"])).resolve()
    if not root.is_dir():
        raise WorkerError(
            f"Stage119 cache directory is missing: {root}. "
            + path_hint(
                cli_option="--stage119-root",
                env_name="ENDFIELD_STAGE119_ROOT",
                config_key="stage119_root",
            )
        )
    manifest_path = root / "region_map_layer_manifest.json"
    audit_path = root / "full_audit.json"
    if not manifest_path.is_file() or not audit_path.is_file():
        missing = [path.name for path in (manifest_path, audit_path) if not path.is_file()]
        raise WorkerError(
            f"Stage119 cache is incomplete at {root}; missing: {', '.join(missing)}. "
            "Use the audited Stage119 cache root, not a parent log directory."
        )
    manifest = read_json(manifest_path)
    audit = read_json(audit_path)
    fingerprint = str(manifest.get("datasetFingerprint") or "")
    requested = str(stage119.get("fingerprint") or "")
    if not re.fullmatch(r"[0-9A-F]{64}", fingerprint):
        raise WorkerError("Stage119 manifest fingerprint is invalid")
    if requested and requested != fingerprint:
        raise WorkerError("Stage119 job fingerprint does not match the configured dataset")
    if audit.get("datasetFingerprint") != fingerprint or audit.get("status") != "passed":
        raise WorkerError("Stage119 full audit is not passing")
    return {
        "root": str(root),
        "fingerprint": fingerprint,
        "manifest": file_descriptor(manifest_path),
        "fullAudit": file_descriptor(audit_path),
    }


def execute(job: Mapping[str, Any], registry: Mapping[str, Any], registry_path: Path, plan_only: bool = False) -> dict[str, Any]:
    output_dir = Path(job["outputDir"]).resolve()
    game_root = Path(job["gameRoot"]).resolve()
    if not job.get("exportRoot"):
        raise WorkerError("exportRoot is required after runtime configuration resolution")
    export_root = Path(str(job["exportRoot"])).resolve()
    ensure_game_read_only_boundary(game_root)
    ensure_output_boundary(output_dir, game_root, export_root)
    log_dir = Path(job["logDir"]).resolve()
    if not is_inside(log_dir, export_root / "log") or is_inside(log_dir, game_root):
        raise WorkerError(f"logDir must remain inside {export_root / 'log'}: {log_dir}")
    if log_dir.exists():
        raise WorkerError(f"refusing to overwrite existing logDir: {log_dir}")
    if is_inside(output_dir, log_dir) or is_inside(log_dir, output_dir):
        raise WorkerError("outputDir and logDir must not contain one another")
    events_path = Path(job["eventsPath"]).resolve()
    cancel_marker = Path(job["cancelMarker"]).resolve()
    for runtime_path, role in ((events_path, "eventsPath"), (cancel_marker, "cancelMarker")):
        if not is_inside(runtime_path, export_root / "log") or is_inside(runtime_path, game_root):
            raise WorkerError(f"{role} must remain inside {export_root / 'log'} and outside the game: {runtime_path}")
    if events_path.exists():
        raise WorkerError(f"refusing to append to an existing eventsPath: {events_path}")
    stage119_evidence = validate_stage119(job["stage119"])
    if registry.get("stage119Fingerprint") != stage119_evidence["fingerprint"]:
        raise WorkerError("Blender profile registry fingerprint does not match the configured Stage119 dataset")
    worker_script_evidence = verify_worker_scripts(registry)
    if set(job.get("exportGroups") or ["all"]) != {"all"}:
        raise WorkerError(
            "category-filtered Blender export is not implemented; exportGroups must be ['all']"
        )
    bounds = finite_bounds(job["bounds"])
    map_bounds = MAP_BOUNDS[job["mapId"]]
    parsed = [parse_sector_key(value) for value in job["sectorKeys"]]
    mapped = [item for item in parsed if intersects(sector_bounds(item), map_bounds) and intersects(sector_bounds(item), bounds)]
    retained = [item for item in parsed if item not in mapped]
    logical_units = plan_logical_units(bounds, mapped, job["batchMode"])
    profiles = profiles_for_map(registry, job["mapId"])
    for unit in logical_units:
        unit_bounds = finite_bounds(unit["bounds"])
        unit["partitions"] = [
            {"profileId": profile["profileId"], "bounds": format_bounds(part_bounds), "status": profile["status"]}
            for profile, part_bounds in partition_bounds(unit_bounds, profiles)
        ]
    plan = {
        "format": "EndfieldBlenderSelectionPlan/1",
        "workerFormat": WORKER_FORMAT,
        "mapId": job["mapId"],
        "batchMode": job["batchMode"],
        "bounds": job["bounds"],
        "logicalUnits": logical_units,
        "mappedSectorKeys": [sector_key(item) for item in mapped],
        "retainedUnmappedSectorKeys": [sector_key(item) for item in retained],
        "layerStatus": layer_status(job),
        "cancelGranularity": "phase_boundary",
        "workerScripts": worker_script_evidence,
    }
    if plan_only:
        return plan
    output_dir.mkdir(parents=True, exist_ok=False)
    log_dir.mkdir(parents=True, exist_ok=False)
    incomplete = output_dir / "INCOMPLETE"
    incomplete.write_text("Blender selection build is running.\n", encoding="ascii")
    emitter = EventEmitter(events_path, job["jobId"])
    manifest_path = output_dir / "build_manifest.json"
    write_json(output_dir / "normalized_job.json", job)
    write_json(output_dir / "build_plan.json", plan)
    layer_state = plan["layerStatus"]
    registry_descriptor = file_descriptor(registry_path)
    started = utc_timestamp()
    built_units = []
    water_builds: list[dict[str, Any]] = []
    profile_evidence: dict[str, Any] = {}
    try:
        emitter.emit("preparing", "preflight", 0.03, "Validating profile hashes and read-only boundaries")
        used_ids = {part["profileId"] for unit in logical_units for part in unit["partitions"]}
        by_id = {profile["profileId"]: profile for profile in profiles}
        verification_cache: dict[Path, dict[str, Any]] = {}
        for profile_id in sorted(used_ids):
            profile_evidence[profile_id] = verify_profile(
                by_id[profile_id],
                job.get("blenderExe"),
                include_water=bool(job["layers"]["water"]),
                verification_cache=verification_cache,
            )
        if cancel_marker.exists():
            raise JobCancelled("cancel requested before build")
        emitter.emit("building", "assembly", 0.10, "Starting bounded Blender assembly", {"unitCount": len(logical_units)})
        builds_dir = output_dir / "blends"
        partial_dir = output_dir / "partitions"
        audits_dir = output_dir / "audits"
        logs_dir = log_dir / "processes"
        builds_dir.mkdir()
        partial_dir.mkdir()
        audits_dir.mkdir()
        logs_dir.mkdir()
        water_steps_per_partition = water_steps_for_job(job)
        total_steps = sum(
            len(unit["partitions"]) * (1 + water_steps_per_partition) + 1
            for unit in logical_units
        )
        completed_steps = 0
        for unit_index, unit in enumerate(logical_units, 1):
            unit_bounds = finite_bounds(unit["bounds"])
            partition_outputs: list[Path] = []
            unit_profiles = []
            for part_index, part in enumerate(unit["partitions"], 1):
                profile = by_id[part["profileId"]]
                evidence = profile_evidence[part["profileId"]]
                blender = Path(evidence["blender"]["path"])
                part_bounds = finite_bounds(part["bounds"])
                part_output = partial_dir / f"{unit['unitId']}__part-{part_index:02d}__{profile['profileId']}.blend"
                build_output = (
                    partial_dir / f"{unit['unitId']}__part-{part_index:02d}__{profile['profileId']}__base.blend"
                    if job["mapId"] == "map02" and job["layers"]["water"]
                    else part_output
                )
                command, environment = profile_command(profile, blender, build_output, part_bounds, job)
                command_record = {
                    "profileId": profile["profileId"], "bounds": part["bounds"],
                    "command": command, "stdout": str(logs_dir / f"{unit['unitId']}__part-{part_index:02d}.stdout.log"),
                    "stderr": str(logs_dir / f"{unit['unitId']}__part-{part_index:02d}.stderr.log"),
                }
                write_json(partial_dir / f"{unit['unitId']}__part-{part_index:02d}.command.json", command_record)
                progress = 0.10 + 0.82 * completed_steps / max(1, total_steps)
                emitter.emit("building", "assembly", progress, f"Building {unit['unitId']} partition {part_index}/{len(unit['partitions'])}", {"profileId": profile["profileId"], "bounds": part["bounds"]})
                run_process(
                    command, environment,
                    logs_dir / f"{unit['unitId']}__part-{part_index:02d}.stdout.log",
                    logs_dir / f"{unit['unitId']}__part-{part_index:02d}.stderr.log",
                    cancel_marker, emitter, "assembly",
                )
                if not build_output.is_file():
                    raise WorkerError(f"Blender completed without output: {build_output}")
                completed_steps += 1
                if job["mapId"] == "map02" and job["layers"]["water"]:
                    water_intermediate = (
                        partial_dir / f"{unit['unitId']}__part-{part_index:02d}__{profile['profileId']}__water_modes.blend"
                        if job["waterMode"] == "flowmap_fresnel"
                        else part_output
                    )
                    water_audit = audits_dir / f"{unit['unitId']}__part-{part_index:02d}__water_build.json"
                    import_command = water_import_command(
                        profile, blender, build_output, water_intermediate, water_audit, part_bounds
                    )
                    emitter.emit(
                        "building", "water_import",
                        0.10 + 0.82 * completed_steps / max(1, total_steps),
                        f"Importing partial reviewed PCG water candidate for {unit['unitId']} partition {part_index}",
                        {"profileId": profile["profileId"], "bounds": part["bounds"]},
                    )
                    run_process(
                        import_command, environment,
                        logs_dir / f"{unit['unitId']}__part-{part_index:02d}__water.stdout.log",
                        logs_dir / f"{unit['unitId']}__part-{part_index:02d}__water.stderr.log",
                        cancel_marker, emitter, "water_import",
                    )
                    water_payload = read_json(water_audit)
                    if not water_payload.get("passed") or not water_intermediate.is_file():
                        raise WorkerError(f"reviewed partial PCG water candidate build did not pass: {water_audit}")
                    completed_steps += 1
                    mode_audit: Path | None = None
                    if job["waterMode"] == "flowmap_fresnel":
                        mode_audit = audits_dir / f"{unit['unitId']}__part-{part_index:02d}__water_mode.json"
                        mode_command = water_mode_command(
                            profile, blender, water_intermediate, part_output, mode_audit, job["waterMode"]
                        )
                        emitter.emit(
                            "building", "water_mode",
                            0.10 + 0.82 * completed_steps / max(1, total_steps),
                            f"Selecting {job['waterMode']} water for {unit['unitId']} partition {part_index}",
                        )
                        run_process(
                            mode_command, environment,
                            logs_dir / f"{unit['unitId']}__part-{part_index:02d}__water_mode.stdout.log",
                            logs_dir / f"{unit['unitId']}__part-{part_index:02d}__water_mode.stderr.log",
                            cancel_marker, emitter, "water_mode",
                        )
                        mode_payload = read_json(mode_audit)
                        if not mode_payload.get("passed") or not part_output.is_file():
                            raise WorkerError(f"water mode selection did not pass: {mode_audit}")
                        completed_steps += 1
                    water_builds.append({
                        "unitId": unit["unitId"], "profileId": profile["profileId"],
                        "bounds": part["bounds"], "mode": job["waterMode"],
                        "selectedRecordCount": int((water_payload.get("water") or {}).get("selected_records") or 0),
                        "sourceRecordCount": int((water_payload.get("water") or {}).get("source_records") or 0),
                        "buildAudit": file_descriptor(water_audit, output_dir),
                        "modeAudit": file_descriptor(mode_audit, output_dir) if mode_audit else None,
                    })
                partition_outputs.append(part_output)
                unit_profiles.append(profile["profileId"])
            final_blend = builds_dir / f"{job['label']}__{job['mapId']}__{job['batchMode']}__{unit['tag']}.blend"
            final_blender = Path(profile_evidence[unit_profiles[0]]["blender"]["path"])
            if len(partition_outputs) == 1:
                partition_outputs[0].replace(final_blend)
            else:
                merge_command = [
                    str(final_blender), "--background", "--factory-startup", "--python", str(MERGER_PATH.resolve()), "--",
                    "--output", str(final_blend), "--map-id", job["mapId"],
                    "--xmin", format(unit_bounds[0], ".17g"), "--xmax", format(unit_bounds[1], ".17g"),
                    "--zmin", format(unit_bounds[2], ".17g"), "--zmax", format(unit_bounds[3], ".17g"),
                ]
                for part_output in partition_outputs:
                    merge_command.extend(["--input", str(part_output)])
                emitter.emit("building", "merge", 0.72, f"Merging {len(partition_outputs)} profile partitions for {unit['unitId']}")
                run_process(
                    merge_command, os.environ.copy(), logs_dir / f"{unit['unitId']}__merge.stdout.log",
                    logs_dir / f"{unit['unitId']}__merge.stderr.log", cancel_marker, emitter, "merge",
                )
                if not final_blend.is_file() or final_blend.stat().st_size <= 0:
                    raise WorkerError(f"merge completed without a valid output: {final_blend}")
            unit_manifest = {
                "format": "EndfieldBlenderSelectionUnit/1", "jobId": job["jobId"],
                "unitId": unit["unitId"], "mapId": job["mapId"], "bounds": unit["bounds"],
                "sectorKeys": unit["sectorKeys"], "profiles": unit_profiles,
                "axisContract": AXIS_CONTRACT, "layerStatus": layer_state,
                "stage119Fingerprint": stage119_evidence["fingerprint"],
            }
            unit_manifest_path = audits_dir / f"{unit['unitId']}__manifest.json"
            write_json(unit_manifest_path, unit_manifest)
            audit_path = audits_dir / f"{unit['unitId']}__reopen_audit.json"
            audit_command = blender_audit_command(
                final_blender, final_blend, audit_path, unit_manifest_path, job["mapId"], unit_bounds,
                remove_instances=not job["layers"]["instances"],
                remove_terrain=not job["layers"]["terrain"],
            )
            emitter.emit("auditing", "reopen_audit", 0.10 + 0.82 * completed_steps / max(1, total_steps), f"Reopening and auditing {unit['unitId']}")
            run_process(
                audit_command, os.environ.copy(), logs_dir / f"{unit['unitId']}__audit.stdout.log",
                logs_dir / f"{unit['unitId']}__audit.stderr.log", cancel_marker, emitter, "reopen_audit",
            )
            audit_payload = read_json(audit_path)
            if not bool((audit_payload.get("summary") or {}).get("assertionsPassed")):
                raise WorkerError(f"reopen audit did not pass: {audit_path}")
            built_units.append({
                **{key: unit[key] for key in ("unitId", "tag", "sectorKeys", "bounds")},
                "profiles": unit_profiles,
                "blend": file_descriptor(final_blend, output_dir),
                "audit": file_descriptor(audit_path, output_dir),
                "unitManifest": file_descriptor(unit_manifest_path, output_dir),
            })
            completed_steps += 1
        if job["mapId"] == "map02" and job["layers"]["water"]:
            selected_water_records = sum(item["selectedRecordCount"] for item in water_builds)
            layer_state["water"].update({
                "status": (
                    "candidate_overlay_built"
                    if selected_water_records
                    else "candidate_overlay_no_records"
                ),
                "selectedRecordCount": selected_water_records,
                "sourceRecordCount": max((item["sourceRecordCount"] for item in water_builds), default=0),
                "coverageStatus": "partial_reviewed_pcg_meshes",
            })
        status = build_manifest_status(layer_state) if built_units else "completed_without_blend_unmapped_only"
        manifest = {
            "format": MANIFEST_FORMAT, "workerFormat": WORKER_FORMAT, "status": status,
            "jobId": job["jobId"], "startedAt": started, "completedAt": utc_timestamp(),
            "mapId": job["mapId"], "batchMode": job["batchMode"], "bounds": job["bounds"],
            "boundsSemantics": "half-open [min,max)", "axisContract": AXIS_CONTRACT,
            "stage119": stage119_evidence, "profileRegistry": registry_descriptor,
            "workerScripts": worker_script_evidence,
            "profileEvidence": profile_evidence, "sectorKeys": job["sectorKeys"],
            "mappedSectorKeys": plan["mappedSectorKeys"],
            "retainedUnmappedSectorKeys": plan["retainedUnmappedSectorKeys"],
            "layerStatus": layer_state, "waterBuilds": water_builds,
            "cancelGranularity": "phase_boundary",
            "outputs": built_units,
            "warnings": [
                "Map02 profile inputs are quadrant-specific reviewed candidates; no single whole-map final audit exists."
            ] if job["mapId"] == "map02" else [],
        }
        write_json(manifest_path, manifest)
        manifest["manifestSha256"] = sha256_file(manifest_path)
        write_json(output_dir / "result.json", manifest)
        incomplete.unlink()
        emitter.emit("completed", "completed", 1.0, f"Blender selection build finished: {status}", {"manifest": str(manifest_path), "outputCount": len(built_units)})
        return manifest
    except JobCancelled as error:
        partial_artifacts = [file_descriptor(path, output_dir) for path in output_dir.rglob("*.blend") if path.is_file()]
        payload = {"format": MANIFEST_FORMAT, "status": "cancelled", "jobId": job["jobId"], "message": str(error), "partialOutputs": built_units, "partialArtifacts": partial_artifacts, "cancelGranularity": "phase_boundary"}
        write_json(output_dir / "cancelled.json", payload)
        emitter.emit("cancelled", "cancelled", emitter.progress, str(error), {"partialOutput": str(output_dir)})
        return payload
    except Exception as error:
        partial_artifacts = [file_descriptor(path, output_dir) for path in output_dir.rglob("*.blend") if path.is_file()]
        payload = {"format": MANIFEST_FORMAT, "status": "failed", "jobId": job["jobId"], "errorType": type(error).__name__, "message": str(error), "partialOutputs": built_units, "partialArtifacts": partial_artifacts}
        write_json(output_dir / "failure.json", payload)
        emitter.emit("failed", "failed", emitter.progress, str(error), {"partialOutput": str(output_dir), "errorType": type(error).__name__})
        raise


def verify_all_profiles(registry: Mapping[str, Any], blender_override: Path | None) -> dict[str, Any]:
    results = []
    verification_cache: dict[Path, dict[str, Any]] = {}
    for profile in registry.get("profiles") or []:
        results.append(
            verify_profile(
                profile,
                str(blender_override.resolve()) if blender_override else None,
                include_water=True,
                verification_cache=verification_cache,
            )
        )
    return {
        "format": "EndfieldBlenderProfileAudit/1",
        "status": "passed",
        "workerScripts": verify_worker_scripts(registry),
        "profiles": results,
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build audited Endfield .blend files for a half-open selection")
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--job", type=Path, help="EndfieldBlenderSelectionJob/1 or EndfieldCanonicalExportJob/2 JSON")
    source.add_argument("--export-package", type=Path, help="Stage119 WebUI export package containing canonical_request.json")
    parser.add_argument("--profile-registry", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--batch-mode", choices=sorted(BATCH_MODES))
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--cancel-marker", type=Path)
    parser.add_argument("--blender-exe", type=Path)
    parser.add_argument("--game-root", type=Path)
    parser.add_argument("--export-root", type=Path)
    parser.add_argument("--stage119-root", type=Path)
    parser.add_argument("--runtime-config", type=Path, help="Optional EndfieldRuntimeConfig/1 local JSON file.")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--verify-profiles", action="store_true")
    parser.add_argument("--report", type=Path, help="Optional JSON report path for --verify-profiles")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    runtime_paths, _runtime_config = resolve_runtime_paths(args)
    registry_path = runtime_paths["profile_registry"]
    registry = load_profiles(registry_path)
    if args.verify_profiles:
        result = verify_all_profiles(registry, args.blender_exe)
        if args.report:
            report = args.report.resolve()
            if report.exists():
                raise WorkerError(f"refusing to overwrite report: {report}")
            write_json(report, result)
            print(json.dumps({"status": result["status"], "profileCount": len(result["profiles"]), "report": str(report)}, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.job is None and args.export_package is None:
        raise WorkerError("--job or --export-package is required unless --verify-profiles is used")
    package = args.export_package.resolve() if args.export_package else None
    raw = load_job_from_package(package) if package else read_json(args.job.resolve())
    job = normalize_job(
        raw, export_package=package, output_override=args.output_dir,
        batch_override=args.batch_mode, log_override=args.log_dir, events_override=args.events,
        cancel_override=args.cancel_marker, blender_override=args.blender_exe,
        runtime_paths=runtime_paths,
    )
    result = execute(job, registry, registry_path, plan_only=args.plan_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") == "cancelled":
        return 3
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigurationError, WorkerError, OSError, ValueError, KeyError) as error:
        print(f"WORKER_ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
