"""Create all baseline map-layer inputs directly from a read-only Endfield install.

The coordinator is intentionally conservative: VFS metadata, resource indexes,
Map01/Map02 instances, truthful unresolved asset tables, and a bootstrap layer
dataset are automatic. Geometry/material closure is never fabricated; its
availability is reported separately for the Blender worker.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback
from typing import Any, Callable

from audit_endfield_map_layers import audit as audit_map_layers
from build_endfield_map_layers import BuildConfig, build_all
from endfield_runtime_config import (
    ConfigurationError,
    configured_path,
    load_json_config,
    user_runtime_config_path,
)
from extractor_core import (
    build_asset_resolution_database,
    build_instance_database,
    build_resource_database,
    build_unresolved_asset_database,
    extract_candidate_bundles,
    extract_terrain_surface,
    extract_bootstrap_metadata,
    read_bundle_candidate_names_by_map,
    read_bundle_closure_names_by_map,
    write_bundle_scan_index,
)
from extractor_core.runtime_manifest import augment_runtime_manifest
from extractor_core.assets import ASSET_RESOLUTION_POLICY_VERSION


CONFIG_FORMAT = "EndfieldFirstRunConfig/1"
STATE_FORMAT = "EndfieldFirstRunState/1"
REPORT_FORMAT = "EndfieldFirstRunReport/1"
EVENT_FORMAT = "EndfieldFirstRunEvent/1"
SCRIPT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_ROOT / "endfield.runtime.example.json"
RUNTIME_MANIFEST = SCRIPT_ROOT / "webui" / "src" / "data" / "region_map_runtime_manifest.json"
DEPENDENCY_BOOTSTRAP = SCRIPT_ROOT / "endfield_dependency_bootstrap.py"


class FirstRunError(RuntimeError):
    pass


def automatic_asset_upgrade_needed(
    meta: dict[str, Any],
    scan_path: Path | None,
    scene_manifest_path: Path | None = None,
    resource_database_path: Path | None = None,
    instances_path: Path | None = None,
) -> bool:
    if scan_path is None or not scan_path.is_file():
        return False
    if meta.get("format") != "EndfieldAutomaticAssetResolutionSummary/1":
        return True
    if meta.get("asset_resolution_policy_version") != ASSET_RESOLUTION_POLICY_VERSION:
        return True
    inputs = {
        "bundle_scan_sha256": scan_path,
        "scene_probe_manifest_sha256": scene_manifest_path,
        "resource_database_sha256": resource_database_path,
        "instances_sha256": instances_path,
    }
    for key, path in inputs.items():
        if path is None:
            continue
        if not path.is_file() or meta.get(key) != sha256_file(path):
            return True
    return False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def descriptor(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {"path": str(resolved), "bytes": resolved.stat().st_size, "sha256": sha256_file(resolved)}


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"Temporary state file already exists: {temporary}")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def write_json_new(path: Path, value: Any) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite: {path}")
    write_json_atomic(path, value)


def activate_runtime_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Activate audited first-run paths without placing data in the source tree."""

    target = user_runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target.is_file():
        current = load_json_config(target)
        if current == payload:
            return {"status": "already_active", **descriptor(target)}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = next_available(target.with_name(f"{target.stem}.backup_{stamp}{target.suffix}"))
        shutil.copy2(target, backup)
    write_json_atomic(target, payload)
    result = {"status": "activated", **descriptor(target)}
    if backup is not None:
        result["previousConfigBackup"] = descriptor(backup)
    return result


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate_game_root(game_root: Path) -> Path:
    root = game_root.resolve()
    checks = {
        "Endfield.exe": root / "Endfield.exe",
        "globalgamemanagers": root / "Endfield_Data" / "globalgamemanagers",
        "VFS": root / "Endfield_Data" / "StreamingAssets" / "VFS",
    }
    missing = [name for name, path in checks.items() if not path.exists()]
    if missing:
        raise FirstRunError(
            f"Selected game directory is invalid: {root}. Missing: {', '.join(missing)}. "
            "Select the directory that directly contains Endfield.exe."
        )
    return root


def validate_blender(path: Path | None, required: bool) -> Path | None:
    if path is None:
        if required:
            raise FirstRunError(
                "Blender is not configured. Pass --blender-exe or set ENDFIELD_BLENDER to blender.exe."
            )
        return None
    resolved = path.resolve()
    if not resolved.is_file() or resolved.name.casefold() != "blender.exe":
        raise FirstRunError(f"Blender executable is invalid: {resolved}. Select blender.exe itself.")
    return resolved


def ensure_writable_root(path: Path, label: str, game_root: Path, create: bool) -> Path:
    root = path.resolve()
    if is_inside(root, game_root):
        raise FirstRunError(f"{label} cannot be inside the game installation: {root}")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    elif not root.is_dir():
        parent = root.parent
        if not parent.is_dir():
            raise FirstRunError(f"{label} parent does not exist: {parent}")
    return root


def next_available(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}.retry{index:03d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FirstRunError(f"No retry path is available for {path}")


def run_logged_process(
    command: list[str],
    *,
    cwd: Path,
    log_path: Path,
    cancelled: Callable[[], bool],
) -> tuple[int, str]:
    """Run a first-run helper visibly through its log and support cancellation."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("x", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        while process.poll() is None:
            if cancelled():
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise FirstRunError("First-run extraction was cancelled; helper partial outputs were preserved.")
            import time

            time.sleep(0.2)
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    return int(process.returncode or 0), tail


class EventWriter:
    def __init__(self, path: Path | None):
        self.path = path
        self.sequence = 0

    def emit(
        self,
        phase: str,
        progress: float,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        status: str = "running",
    ) -> None:
        self.sequence += 1
        row = {
            "format": EVENT_FORMAT,
            "sequence": self.sequence,
            "timestamp": utc_now(),
            "phase": phase,
            "progress": max(0.0, min(1.0, float(progress))),
            "status": status,
            "message": message,
        }
        if details:
            row["details"] = details
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        try:
            print(json.dumps(row, ensure_ascii=False), flush=True)
        except OSError:
            # Detached Windows launchers can invalidate stdout while the resumable job continues.
            pass


def recover_completed_scene_discovery(
    run_root: Path,
    log_root: Path,
    bundle_scan_index: Path,
) -> dict[str, Any] | None:
    discovery_parent = run_root / "scene_discovery"
    if not discovery_parent.is_dir():
        return None
    roots = sorted(
        (
            path
            for path in discovery_parent.iterdir()
            if path.is_dir() and path.name.startswith("map_scene_candidates")
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for discovery_root in roots:
        scan_results = discovery_root / "bundle_candidates.jsonl"
        cab_results = discovery_root / "bundle_cab_inventory.jsonl"
        if not scan_results.is_file() or not cab_results.is_file():
            continue
        manifests = {
            map_id: discovery_root / map_id / "scene_probe" / "scene_manifest.json"
            for map_id in ("map01", "map02")
        }
        if not all(path.is_file() and path.stat().st_size > 0 for path in manifests.values()):
            continue
        try:
            for manifest_path in manifests.values():
                with manifest_path.open("r", encoding="utf-8") as handle:
                    if not isinstance(json.load(handle), dict):
                        raise ValueError(f"Scene manifest root is not an object: {manifest_path}")
            names_by_map, closure_meta = read_bundle_closure_names_by_map(
                scan_results, cab_results
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if any(not names_by_map.get(map_id) for map_id in ("map01", "map02")):
            continue

        map_outputs: dict[str, Any] = {}
        for map_id in ("map01", "map02"):
            bundle_root = discovery_root / map_id / "bundles"
            bundle_files = [path for path in bundle_root.rglob("*") if path.is_file()]
            map_outputs[map_id] = {
                "bundleCount": len(names_by_map[map_id]),
                "candidateBundles": {
                    "root": str(bundle_root),
                    "meta": {
                        "format": "EndfieldCandidateBundleSet/1",
                        "requested_count": str(len(names_by_map[map_id])),
                        "extracted_count": str(len(bundle_files)),
                        "bytes": str(sum(path.stat().st_size for path in bundle_files)),
                        "recovered": True,
                    },
                },
                "sceneManifest": descriptor(manifests[map_id]),
            }

        def latest_log(pattern: str) -> dict[str, Any] | None:
            candidates = sorted(
                (log_root / "scene_discovery").glob(pattern),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            return descriptor(candidates[0]) if candidates else None

        return {
            "status": "completed",
            "coverage": "map01_map02_container_candidates_plus_cab_external_closure",
            "scanIndex": descriptor(bundle_scan_index),
            "scannerResults": descriptor(scan_results),
            "scannerConsole": latest_log("bundle_scanner*.log"),
            "cabInventory": descriptor(cab_results),
            "cabConsole": latest_log("bundle_cab_inventory*.log"),
            "closureStatus": "expanded_from_cab_inventory",
            "closure": closure_meta,
            "maps": map_outputs,
            "recoveredFromCompletedOutputs": True,
            "limitations": [
                "Automatic extraction does not promote unreviewed geometry/material bindings.",
                "Blender profiles remain not_ready until builder-specific audits pass.",
            ],
        }


def read_config(path: Path | None) -> tuple[dict[str, Any], Path | None]:
    if path is not None:
        resolved = path.resolve()
        payload = load_json_config(resolved)
        if payload.get("format") not in {CONFIG_FORMAT, "EndfieldRuntimeConfig/1"}:
            raise ConfigurationError(
                f"Unsupported first-run configuration format: {payload.get('format')!r}"
            )
        return payload, resolved
    for name in ("endfield.runtime.local.json", "endfield.local.json"):
        local = SCRIPT_ROOT / name
        if local.is_file():
            payload = load_json_config(local)
            if payload.get("format") not in {CONFIG_FORMAT, "EndfieldRuntimeConfig/1"}:
                raise ConfigurationError(
                    f"Unsupported first-run configuration format: {payload.get('format')!r}"
                )
            return payload, local.resolve()
    return {}, None


def optional_blender(
    cli_value: Path | None,
    config: dict[str, Any],
    config_path: Path | None,
) -> Path | None:
    value = cli_value or os.environ.get("ENDFIELD_BLENDER") or (config.get("paths") or {}).get("blenderExe")
    if not value:
        return None
    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute() and config_path is not None:
        candidate = config_path.parent / candidate
    return candidate.resolve()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automatically prepare portable Endfield map extraction inputs from a read-only game install."
    )
    parser.add_argument("--config", type=Path, help="Optional EndfieldFirstRunConfig/1 local JSON")
    parser.add_argument("--game-root", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--export-root", type=Path)
    parser.add_argument("--blender-exe", type=Path)
    parser.add_argument("--run-name", default="bootstrap_v1")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--require-blender", action="store_true")
    parser.add_argument(
        "--skip-helper-bootstrap",
        action="store_true",
        help="Skip the automatic pinned SceneProbe/BundleScanner build (base map extraction still works).",
    )
    parser.add_argument(
        "--skip-scene-discovery",
        action="store_true",
        help="Skip automatic BundleScanner candidate discovery and SceneProbe manifest extraction.",
    )
    parser.add_argument(
        "--skip-shader-diagnostics",
        action="store_true",
        help="Build helpers without the optional shader diagnostic patch.",
    )
    parser.add_argument(
        "--no-activate-runtime-config",
        action="store_true",
        help="Do not activate the generated runtime config in the per-user local config directory.",
    )
    parser.add_argument("--cancel-marker", type=Path)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--runtime-region-evidence",
        type=Path,
        help=(
            "Optional extracted MapRegionTable JSONL/literal log. When present, "
            "new H-tile levels discovered by SceneProbe are added to a run-local runtime manifest."
        ),
    )
    return parser.parse_args()


def resolve_paths(args: argparse.Namespace) -> dict[str, Any]:
    config, config_path = read_config(args.config)
    paths = dict(config.get("paths") or {})
    runtime_style = config.get("format") == "EndfieldRuntimeConfig/1"
    def config_path_value(camel: str, snake: str) -> Any:
        return paths.get(snake if runtime_style else camel)
    config_dir = config_path.parent if config_path else None
    game_root = configured_path(
        args.game_root,
        env_name="ENDFIELD_GAME_ROOT",
        label="Endfield game root",
        config_value=config_path_value("gameRoot", "game_root"),
        config_dir=config_dir,
        kind="dir",
    )
    cache_root = configured_path(
        args.cache_root,
        env_name="ENDFIELD_CACHE_ROOT",
        label="Extraction cache root",
        config_value=config_path_value("cacheRoot", "cache_root"),
        config_dir=config_dir,
    )
    export_root = configured_path(
        args.export_root,
        env_name="ENDFIELD_EXPORT_ROOT",
        label="Export root",
        config_value=config_path_value("exportRoot", "export_root"),
        config_dir=config_dir,
    )
    if runtime_style:
        runtime_blender = paths.get("blender_exe")
        config = dict(config)
        config["paths"] = {**paths, "blenderExe": runtime_blender}
    blender = optional_blender(args.blender_exe, config, config_path)
    return {
        "config": config,
        "configPath": config_path,
        "gameRoot": game_root,
        "cacheRoot": cache_root,
        "exportRoot": export_root,
        "blenderExe": blender,
    }


def plan(paths: dict[str, Any], run_name: str) -> dict[str, Any]:
    game_root = validate_game_root(paths["gameRoot"])
    cache_root = paths["cacheRoot"].resolve()
    export_root = paths["exportRoot"].resolve()
    run_root = cache_root / run_name
    log_root = export_root / "log" / f"first_run_{run_name}"
    stage_base = export_root / "cache" / f"map_layers_{run_name}"
    return {
        "format": "EndfieldFirstRunPlan/1",
        "gameRoot": str(game_root),
        "gameReadOnly": True,
        "cacheRoot": str(cache_root),
        "exportRoot": str(export_root),
        "runRoot": str(run_root),
        "logRoot": str(log_root),
        "stageBase": str(stage_base),
        "blenderExe": str(paths["blenderExe"]) if paths["blenderExe"] else None,
        "steps": [
            "extract_bootstrap_vfs_metadata",
            "bootstrap_scene_probe_and_bundle_scanner",
            "build_resource_index",
            "write_bundle_scanner_index",
            "scan_map01_map02_scene_candidates",
            "build_full_bundle_cab_inventory",
            "extract_map_scene_dependency_closures_and_payloads",
            "build_map01_instances",
            "build_map02_instances",
            "build_map01_map02_asset_resolution_tables",
            "discover_runtime_levels_and_ownership",
            "extract_map01_full_terrain_surface",
            "extract_map02_quadrant_terrain_surfaces",
            "build_bootstrap_map_layer_dataset",
            "audit_bootstrap_map_layer_dataset",
            "write_runtime_config",
        ],
        "automatic": True,
        "geometryClosure": {
            "status": "automatic_pending",
            "reason": "BundleScanner and SceneProbe discovery will run automatically when pinned helpers are available.",
        },
    }


def run() -> int:
    args = arguments()
    paths = resolve_paths(args)
    selected_blender = validate_blender(paths["blenderExe"], args.require_blender)
    execution_plan = plan(paths, args.run_name)
    game_root = Path(execution_plan["gameRoot"])
    if args.plan_only:
        print(json.dumps(execution_plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    cache_root = ensure_writable_root(Path(execution_plan["cacheRoot"]), "Cache root", game_root, True)
    export_root = ensure_writable_root(Path(execution_plan["exportRoot"]), "Export root", game_root, True)
    run_root = Path(execution_plan["runRoot"])
    log_root = Path(execution_plan["logRoot"])
    if run_root.exists() and not args.resume:
        raise FirstRunError(f"Run root already exists; pass --resume or choose another --run-name: {run_root}")
    if log_root.exists() and not args.resume:
        raise FirstRunError(f"Log root already exists; pass --resume or choose another --run-name: {log_root}")
    run_root.mkdir(parents=True, exist_ok=True)
    log_root.mkdir(parents=True, exist_ok=True)
    state_path = run_root / "first_run_state.json"
    events_path = args.events.resolve() if args.events else log_root / "events.jsonl"
    report_path = args.report.resolve() if args.report else log_root / "first_run_report.json"
    cancel_marker = args.cancel_marker.resolve() if args.cancel_marker else log_root / "CANCEL_REQUESTED"
    if not is_inside(events_path, log_root) or not is_inside(report_path, log_root) or not is_inside(cancel_marker, log_root):
        raise FirstRunError(f"Events, report, and cancel marker must stay inside {log_root}")
    if events_path.exists() and not args.resume:
        raise FirstRunError(f"Events file already exists: {events_path}")
    if report_path.exists() and args.resume:
        report_path = next_available(report_path)
    emitter = EventWriter(events_path)

    state: dict[str, Any]
    if args.resume and state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("format") != STATE_FORMAT:
            raise FirstRunError(f"Unsupported first-run state: {state_path}")
    else:
        state = {
            "format": STATE_FORMAT,
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "status": "running",
            "gameRoot": str(game_root),
            "gameReadOnly": True,
            "cacheRoot": str(cache_root),
            "exportRoot": str(export_root),
            "steps": {},
        }
        write_json_atomic(state_path, state)

    def cancelled() -> bool:
        return cancel_marker.exists()

    def save_state() -> None:
        state["updatedAt"] = utc_now()
        write_json_atomic(state_path, state)

    def progress(phase: str, value: float, message: str, details: dict[str, Any] | None) -> None:
        if cancelled():
            raise FirstRunError("First-run extraction was cancelled; partial outputs were preserved.")
        emitter.emit(phase, value, message, details)

    def step_done(name: str) -> bool:
        return (state.get("steps") or {}).get(name, {}).get("status") == "completed"

    def complete_step(name: str, outputs: dict[str, Any]) -> None:
        state["steps"][name] = {"status": "completed", "completedAt": utc_now(), "outputs": outputs}
        save_state()

    try:
        emitter.emit("preflight", 0.0, "Validated read-only game and writable output boundaries")
        vfs_root = game_root / "Endfield_Data" / "StreamingAssets" / "VFS"

        dependency_result: dict[str, Any]
        if args.skip_helper_bootstrap:
            dependency_result = {
                "status": "skipped",
                "reason": "--skip-helper-bootstrap",
            }
        elif step_done("helper_dependencies"):
            dependency_result = state["steps"]["helper_dependencies"]["outputs"]
        else:
            emitter.emit(
                "helper_dependencies",
                0.0,
                "Preparing pinned SceneProbe and BundleScanner dependencies",
            )
            dependency_workspace = run_root / "dependencies"
            dependency_log = log_root / "dependency_bootstrap"
            command = [
                sys.executable,
                str(DEPENDENCY_BOOTSTRAP),
                "--workspace",
                str(dependency_workspace),
                "--log-dir",
                str(dependency_log),
            ]
            if not args.skip_shader_diagnostics:
                command.append("--include-shader-diagnostics")
            process = subprocess.run(
                command,
                cwd=str(SCRIPT_ROOT),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            console_path = next_available(dependency_log / "console.log")
            console_path.parent.mkdir(parents=True, exist_ok=True)
            console_path.write_text(process.stdout or "", encoding="utf-8", newline="\n")
            dependency_report = dependency_log / "dependency_bootstrap_report.json"
            if process.returncode == 0 and dependency_report.is_file():
                payload = json.loads(dependency_report.read_text(encoding="utf-8"))
                dependency_result = {
                    "status": "passed",
                    "report": descriptor(dependency_report),
                    "outputs": payload.get("outputs") or {},
                }
                complete_step("helper_dependencies", dependency_result)
                emitter.emit(
                    "helper_dependencies",
                    1.0,
                    "Pinned extraction helpers are ready",
                )
            else:
                dependency_result = {
                    "status": "deferred",
                    "error": (process.stdout or "dependency bootstrap failed").strip()[-2000:],
                    "console": descriptor(console_path),
                    "retryAutomatic": True,
                }
                state["steps"]["helper_dependencies"] = {
                    "status": "deferred",
                    "updatedAt": utc_now(),
                    "outputs": dependency_result,
                }
                save_state()
                emitter.emit(
                    "helper_dependencies",
                    1.0,
                    "Helper bootstrap deferred; continuing baseline map extraction",
                    {"console": str(console_path)},
                    status="deferred",
                )

        if step_done("bootstrap_metadata"):
            metadata_outputs = state["steps"]["bootstrap_metadata"]["outputs"]
            metadata = {key: Path(value["path"]) for key, value in metadata_outputs.items()}
        else:
            metadata_dir = next_available(run_root / "bootstrap_metadata")
            metadata = extract_bootstrap_metadata(
                vfs_root, metadata_dir, progress=progress, cancelled=cancelled
            )
            complete_step("bootstrap_metadata", {key: descriptor(value) for key, value in metadata.items()})

        if step_done("resource_index"):
            resources_db = Path(state["steps"]["resource_index"]["outputs"]["database"]["path"])
        else:
            resources_db = next_available(run_root / "resource_index.sqlite")
            meta = build_resource_database(
                vfs_root,
                metadata["manifest"],
                metadata["main_paths"],
                resources_db,
                progress=progress,
                cancelled=cancelled,
            )
            complete_step("resource_index", {"database": descriptor(resources_db), "meta": meta})

        if step_done("bundle_scan_index"):
            bundle_scan_index = Path(
                state["steps"]["bundle_scan_index"]["outputs"]["index"]["path"]
            )
        else:
            bundle_scan_index = next_available(run_root / "scene_discovery" / "bundle_scan_index.tsv")
            meta = write_bundle_scan_index(resources_db, vfs_root, bundle_scan_index)
            complete_step(
                "bundle_scan_index",
                {"index": descriptor(bundle_scan_index), "meta": meta},
            )
            emitter.emit(
                "scene_discovery",
                0.05,
                "Prepared BundleScanner input directly from the generated resource index",
            )

        scene_discovery_result: dict[str, Any]
        if args.skip_scene_discovery:
            scene_discovery_result = {
                "status": "skipped",
                "reason": "--skip-scene-discovery",
                "scanIndex": descriptor(bundle_scan_index),
            }
        elif step_done("scene_discovery"):
            scene_discovery_result = state["steps"]["scene_discovery"]["outputs"]
        elif dependency_result.get("status") != "passed":
            scene_discovery_result = {
                "status": "deferred",
                "reason": "Pinned BundleScanner/SceneProbe helpers are not available yet.",
                "retryAutomatic": True,
                "scanIndex": descriptor(bundle_scan_index),
            }
            state["steps"]["scene_discovery"] = {
                "status": "deferred",
                "updatedAt": utc_now(),
                "outputs": scene_discovery_result,
            }
            save_state()
        elif recovered_scene := recover_completed_scene_discovery(
            run_root, log_root, bundle_scan_index
        ):
            scene_discovery_result = recovered_scene
            complete_step("scene_discovery", scene_discovery_result)
            emitter.emit(
                "scene_discovery",
                1.0,
                "Recovered completed Map01/Map02 scene payloads after launcher interruption",
            )
        else:
            helper_outputs = dict(dependency_result.get("outputs") or {})
            scanner = Path(str((helper_outputs.get("bundleScanner") or {}).get("path") or ""))
            scene_probe = Path(str((helper_outputs.get("sceneProbe") or {}).get("path") or ""))
            if not scanner.is_file() or not scene_probe.is_file():
                raise FirstRunError(
                    "Dependency bootstrap passed without usable BundleScanner/SceneProbe executables."
                )
            discovery_root = next_available(run_root / "scene_discovery" / "map_scene_candidates")
            discovery_root.mkdir(parents=True, exist_ok=False)
            scan_results = discovery_root / "bundle_candidates.jsonl"
            scan_console = next_available(log_root / "scene_discovery" / "bundle_scanner.log")
            emitter.emit(
                "scene_discovery",
                0.1,
                "Scanning read-only bundles for Map01/Map02 scene candidates",
            )
            scan_command = [
                str(scanner),
                "--input",
                str(bundle_scan_index),
                "--output",
                str(scan_results),
                "--progress-every",
                "1000",
                "--contains",
                "map01",
                "--contains",
                "map02",
            ]
            return_code, tail = run_logged_process(
                scan_command,
                cwd=SCRIPT_ROOT,
                log_path=scan_console,
                cancelled=cancelled,
            )
            if return_code != 0 or not scan_results.is_file():
                scene_discovery_result = {
                    "status": "deferred",
                    "reason": f"BundleScanner failed with exit code {return_code}: {tail.strip()}",
                    "retryAutomatic": True,
                    "scanIndex": descriptor(bundle_scan_index),
                    "console": descriptor(scan_console),
                }
                state["steps"]["scene_discovery"] = {
                    "status": "deferred",
                    "updatedAt": utc_now(),
                    "outputs": scene_discovery_result,
                }
                save_state()
            else:
                cab_results = discovery_root / "bundle_cab_inventory.jsonl"
                cab_console = next_available(log_root / "scene_discovery" / "bundle_cab_inventory.log")
                emitter.emit(
                    "scene_discovery",
                    0.35,
                    "Building the full CAB-to-bundle dependency inventory",
                )
                cab_code, cab_tail = run_logged_process(
                    [
                        str(scanner),
                        "--input",
                        str(bundle_scan_index),
                        "--output",
                        str(cab_results),
                        "--progress-every",
                        "1000",
                        "--cab-only",
                    ],
                    cwd=SCRIPT_ROOT,
                    log_path=cab_console,
                    cancelled=cancelled,
                )
                if cab_code == 0 and cab_results.is_file():
                    names_by_map, closure_meta = read_bundle_closure_names_by_map(
                        scan_results, cab_results
                    )
                    closure_status = "expanded_from_cab_inventory"
                else:
                    names_by_map = read_bundle_candidate_names_by_map(scan_results)
                    closure_meta = {
                        "format": "EndfieldAutomaticBundleClosure/1",
                        "status": "candidate_roots_only",
                        "reason": f"CAB inventory failed with exit code {cab_code}: {cab_tail.strip()}",
                    }
                    closure_status = "candidate_roots_only"

                map_outputs: dict[str, Any] = {}
                probe_failures = 0
                for map_index, map_id in enumerate(("map01", "map02"), start=1):
                    candidate_names = names_by_map[map_id]
                    candidate_bundles = discovery_root / map_id / "bundles"
                    bundle_meta = extract_candidate_bundles(
                        resources_db,
                        vfs_root,
                        candidate_names,
                        candidate_bundles,
                        progress=progress,
                        cancelled=cancelled,
                    )
                    scene_manifest: dict[str, Any] | None = None
                    if candidate_names:
                        scene_probe_output = discovery_root / map_id / "scene_probe"
                        probe_console = next_available(
                            log_root / "scene_discovery" / f"{map_id}_scene_probe.log"
                        )
                        probe_code, probe_tail = run_logged_process(
                            [
                                str(scene_probe),
                                str(candidate_bundles),
                                str(scene_probe_output),
                                "--include-unreferenced",
                            ],
                            cwd=SCRIPT_ROOT,
                            log_path=probe_console,
                            cancelled=cancelled,
                        )
                        manifest_path = scene_probe_output / "scene_manifest.json"
                        if probe_code == 0 and manifest_path.is_file():
                            scene_manifest = descriptor(manifest_path)
                        else:
                            probe_failures += 1
                            scene_manifest = {
                                "status": "deferred",
                                "reason": (
                                    f"SceneProbe failed with exit code {probe_code}: "
                                    f"{probe_tail.strip()}"
                                ),
                                "console": descriptor(probe_console),
                            }
                    else:
                        probe_failures += 1
                    map_outputs[map_id] = {
                        "bundleCount": len(candidate_names),
                        "candidateBundles": {"root": str(candidate_bundles), "meta": bundle_meta},
                        "sceneManifest": scene_manifest,
                    }
                    emitter.emit(
                        "scene_discovery",
                        0.6 + 0.2 * map_index,
                        f"Extracted automatic scene payload for {map_id}",
                    )

                scene_discovery_result = {
                    "status": "completed" if probe_failures == 0 else "completed_with_deferred_probes",
                    "coverage": "map01_map02_container_candidates_plus_cab_external_closure",
                    "scanIndex": descriptor(bundle_scan_index),
                    "scannerResults": descriptor(scan_results),
                    "scannerConsole": descriptor(scan_console),
                    "cabInventory": descriptor(cab_results) if cab_results.is_file() else None,
                    "cabConsole": descriptor(cab_console),
                    "closureStatus": closure_status,
                    "closure": closure_meta,
                    "maps": map_outputs,
                    "limitations": [
                        "Automatic extraction does not promote unreviewed geometry/material bindings.",
                        "Blender profiles remain not_ready until builder-specific audits pass.",
                    ],
                }
                complete_step("scene_discovery", scene_discovery_result)
                emitter.emit(
                    "scene_discovery",
                    1.0,
                    "Automatic Map01/Map02 scene payload extraction completed",
                    status=scene_discovery_result["status"],
                )

        instance_paths: dict[str, Path] = {}
        asset_paths: dict[str, Path] = {}
        asset_summaries: dict[str, Any] = {}
        scanner_descriptor = scene_discovery_result.get("scannerResults")
        asset_scan_path = (
            Path(scanner_descriptor["path"])
            if isinstance(scanner_descriptor, dict) and scanner_descriptor.get("path")
            else None
        )
        for map_index, map_id in enumerate(("map01", "map02"), start=1):
            instance_step = f"{map_id}_instances"
            if step_done(instance_step):
                instance_path = Path(state["steps"][instance_step]["outputs"]["database"]["path"])
            else:
                instance_path = next_available(run_root / map_id / "instances.sqlite")
                meta = build_instance_database(
                    map_id, vfs_root, instance_path, progress=progress, cancelled=cancelled
                )
                complete_step(instance_step, {"database": descriptor(instance_path), "meta": meta})
            instance_paths[map_id] = instance_path

            asset_step = f"{map_id}_assets"
            existing_asset_outputs = (
                state["steps"][asset_step]["outputs"] if step_done(asset_step) else {}
            )
            scene_manifest_descriptor = (
                (scene_discovery_result.get("maps") or {}).get(map_id, {}).get("sceneManifest")
            )
            scene_manifest_path = (
                Path(scene_manifest_descriptor["path"])
                if isinstance(scene_manifest_descriptor, dict) and scene_manifest_descriptor.get("path")
                else None
            )
            upgrade_asset_step = automatic_asset_upgrade_needed(
                existing_asset_outputs.get("meta") or {},
                asset_scan_path,
                scene_manifest_path,
                resources_db,
                instance_path,
            )
            if step_done(asset_step) and not upgrade_asset_step:
                asset_path = Path(existing_asset_outputs["database"]["path"])
            else:
                if asset_scan_path is not None and asset_scan_path.is_file():
                    asset_path = next_available(run_root / map_id / "assets.automatic.sqlite")
                    meta = build_asset_resolution_database(
                        map_id, instance_path, resources_db, asset_scan_path, asset_path,
                        scene_probe_manifest=scene_manifest_path,
                    )
                else:
                    asset_path = next_available(run_root / map_id / "assets.bootstrap.sqlite")
                    meta = build_unresolved_asset_database(instance_path, asset_path)
                complete_step(asset_step, {"database": descriptor(asset_path), "meta": meta})
            asset_paths[map_id] = asset_path
            asset_summaries[map_id] = state["steps"][asset_step]["outputs"].get("meta") or {}
            emitter.emit(
                "asset_bootstrap",
                map_index / 2,
                f"Prepared automatic asset resolution index for {map_id}",
            )

        terrain_surfaces: dict[str, list[Path]] = {"map01": [], "map02": []}
        if step_done("map01_terrain"):
            terrain_surfaces["map01"] = [
                Path(state["steps"]["map01_terrain"]["outputs"]["manifest"]["path"]).parent
            ]
        else:
            terrain_root = next_available(run_root / "map01" / "terrain_surface_full")
            terrain_meta = extract_terrain_surface(
                "map01", vfs_root, terrain_root, progress=progress, cancelled=cancelled
            )
            manifest_path = terrain_root / "terrain_surface.json"
            complete_step(
                "map01_terrain", {"manifest": descriptor(manifest_path), "meta": terrain_meta}
            )
            terrain_surfaces["map01"] = [terrain_root]

        map02_quadrants = {
            "nw": (-2048.0, 0.0, 0.0, 2048.0),
            "ne": (0.0, 2048.0, 0.0, 2048.0),
            "sw": (-2048.0, 0.0, -2048.0, 0.0),
            "se": (0.0, 2048.0, -2048.0, 0.0),
        }
        for quadrant, bounds in map02_quadrants.items():
            terrain_step = f"map02_terrain_{quadrant}"
            if step_done(terrain_step):
                terrain_root = Path(
                    state["steps"][terrain_step]["outputs"]["manifest"]["path"]
                ).parent
            else:
                terrain_root = next_available(run_root / "map02" / f"terrain_surface_{quadrant}")
                terrain_meta = extract_terrain_surface(
                    "map02",
                    vfs_root,
                    terrain_root,
                    bounds=bounds,
                    progress=progress,
                    cancelled=cancelled,
                )
                manifest_path = terrain_root / "terrain_surface.json"
                complete_step(
                    terrain_step, {"manifest": descriptor(manifest_path), "meta": terrain_meta}
                )
            terrain_surfaces["map02"].append(terrain_root)

        runtime_manifest_path = RUNTIME_MANIFEST
        if state.get("steps", {}).get("runtime_manifest", {}).get("status") == "completed":
            runtime_manifest_path = Path(
                state["steps"]["runtime_manifest"]["outputs"]["manifest"]["path"]
            )
        elif args.runtime_region_evidence is not None:
            region_evidence = args.runtime_region_evidence.expanduser().resolve()
            if not region_evidence.is_file():
                raise FirstRunError(f"Runtime region evidence is missing: {region_evidence}")
            map02_scene_descriptor = (
                (scene_discovery_result.get("maps") or {}).get("map02", {}).get("sceneManifest")
            )
            scene_manifest_path = (
                Path(map02_scene_descriptor["path"])
                if isinstance(map02_scene_descriptor, dict) and map02_scene_descriptor.get("path")
                else None
            )
            if scene_manifest_path is None or not scene_manifest_path.is_file():
                raise FirstRunError(
                    "Runtime region evidence was supplied, but the Map02 SceneProbe manifest is unavailable."
                )
            runtime_manifest_path = next_available(run_root / "runtime_manifest.generated.json")
            generated_manifest = augment_runtime_manifest(
                json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8")),
                scene_manifest=scene_manifest_path,
                region_records=region_evidence,
                map_id="map02",
            )
            write_json_new(runtime_manifest_path, generated_manifest)
            complete_step(
                "runtime_manifest",
                {
                    "manifest": descriptor(runtime_manifest_path),
                    "baseManifest": descriptor(RUNTIME_MANIFEST),
                    "regionEvidence": descriptor(region_evidence),
                    "sceneManifest": descriptor(scene_manifest_path),
                },
            )
            emitter.emit(
                "runtime_manifest",
                1.0,
                "Discovered runtime levels and ownership from extracted evidence",
                {"manifest": str(runtime_manifest_path)},
            )

        asset_database_hashes = {
            map_id: state["steps"][f"{map_id}_assets"]["outputs"]["database"]["sha256"]
            for map_id in ("map01", "map02")
        }
        map_layer_outputs = state["steps"]["map_layers"]["outputs"] if step_done("map_layers") else {}
        map_layers_current = map_layer_outputs.get("assetDatabaseSha256") == asset_database_hashes
        if step_done("map_layers") and map_layers_current:
            stage_root = Path(state["steps"]["map_layers"]["outputs"]["root"])
            manifest = json.loads((stage_root / "region_map_layer_manifest.json").read_text(encoding="utf-8"))
            build_summary = json.loads((stage_root / "build_summary.json").read_text(encoding="utf-8"))
        else:
            stage_root = next_available(Path(execution_plan["stageBase"]))
            emitter.emit("map_layers", 0.0, "Building bootstrap map-layer dataset")
            build_summary = build_all(
                BuildConfig(
                    output_root=stage_root,
                    export_root=export_root,
                    game_root=game_root,
                    runtime_manifest=runtime_manifest_path,
                    map01_instances=instance_paths["map01"],
                    map01_assets=asset_paths["map01"],
                    map02_instances=instance_paths["map02"],
                    map02_assets=asset_paths["map02"],
                    bootstrap_mode=True,
                )
            )
            manifest_path = stage_root / "region_map_layer_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            complete_step(
                "map_layers",
                {
                    "root": str(stage_root),
                    "manifest": descriptor(manifest_path),
                    "fingerprint": manifest["datasetFingerprint"],
                    "assetDatabaseSha256": asset_database_hashes,
                },
            )

        audit_outputs = (
            state["steps"]["map_layer_audit"]["outputs"]
            if step_done("map_layer_audit")
            else {}
        )
        audit_current = audit_outputs.get("fingerprint") == manifest["datasetFingerprint"]
        if step_done("map_layer_audit") and audit_current:
            audit_report = state["steps"]["map_layer_audit"]["outputs"]["audit"]
        else:
            emitter.emit("audit", 0.0, "Auditing bootstrap map-layer dataset")
            audit_payload = audit_map_layers(stage_root, verify_source_hashes=True)
            if audit_payload.get("status") != "passed":
                raise FirstRunError("Bootstrap map-layer audit failed; inspect the preserved report and outputs.")
            stage_audit_path = stage_root / "full_audit.json"
            write_json_new(stage_audit_path, audit_payload)
            audit_path = next_available(log_root / "map_layer_audit.json")
            write_json_new(audit_path, audit_payload)
            audit_report = descriptor(audit_path)
            complete_step(
                "map_layer_audit",
                {
                    "audit": audit_report,
                    "stageAudit": descriptor(stage_audit_path),
                    "fingerprint": manifest["datasetFingerprint"],
                },
            )

        runtime_config_path = run_root / "endfield.runtime.local.json"
        profile_registry_path = run_root / "blender_worker_profiles.local.json"
        webui_output_root = export_root / "webui_exports"
        webui_output_root.mkdir(parents=True, exist_ok=True)
        runtime_maps = {}
        scene_maps = scene_discovery_result.get("maps") or {}
        for map_id in ("map01", "map02"):
            summary = asset_summaries[map_id]
            unresolved = int((summary.get("uniqueByMethod") or {}).get("unresolved", 0))
            status = (
                "automatic_resolved"
                if summary.get("format") == "EndfieldAutomaticAssetResolutionSummary/1" and unresolved == 0
                else "automatic_partial"
                if summary.get("format") == "EndfieldAutomaticAssetResolutionSummary/1"
                else "unresolved"
            )
            runtime_maps[map_id] = {
                "instances": str(instance_paths[map_id]),
                "assets": str(asset_paths[map_id]),
                "assetClosureStatus": status,
                "assetResolutionSummary": summary,
                "terrainSurfaces": [str(path) for path in terrain_surfaces[map_id]],
                "sceneProbe": (scene_maps.get(map_id) or {}).get("sceneManifest"),
            }
        if not step_done("runtime_config"):
            worker_scripts = []
            for role, path in (
                ("coordinator", SCRIPT_ROOT / "endfield_blender_selection_worker_v1.py"),
                ("auditor", SCRIPT_ROOT / "blender_audit_endfield_selection_worker_v1.py"),
                ("merger", SCRIPT_ROOT / "blender_merge_endfield_selection_worker_v1.py"),
            ):
                worker_scripts.append({"role": role, **descriptor(path)})
            profile_payload = {
                "format": "EndfieldBlenderWorkerProfiles/1",
                "workerFormat": "EndfieldBlenderSelectionWorker/1",
                "stage119Fingerprint": manifest["datasetFingerprint"],
                "axisContract": "(X,Y,Z)=(Unity X,-Unity Z,Unity Y)",
                "workerScripts": worker_scripts,
                "profiles": [],
                "scan": {
                    "status": "not_ready",
                    "reason": "Geometry/material closures have not passed automatic first-run audit.",
                    "sceneDiscoveryStatus": scene_discovery_result.get("status"),
                },
            }
            write_json_new(profile_registry_path, profile_payload)
            runtime_payload = {
                "format": "EndfieldRuntimeConfig/1",
                "paths": {
                    "game_root": str(game_root),
                    "cache_root": str(cache_root),
                    "export_root": str(export_root),
                    "stage119_root": str(stage_root),
                    "webui_output_root": str(webui_output_root),
                    "blender_exe": str(selected_blender) if selected_blender else None,
                    "blender_profile_registry": str(profile_registry_path),
                },
                "extraction": {
                    "gameReadOnly": True,
                    "mapLayerFingerprint": manifest["datasetFingerprint"],
                    "resourceDatabase": str(resources_db),
                    "bundleScanIndex": str(bundle_scan_index),
                    "sceneDiscovery": scene_discovery_result,
                    "runtimeManifest": descriptor(runtime_manifest_path),
                    "maps": runtime_maps,
                },
                "helperDependencies": dependency_result,
                "sceneDiscovery": scene_discovery_result,
                "blenderBuild": {
                    "status": "not_ready",
                    "reason": "Automatic geometry/material SceneProbe closure has not passed its capability gate.",
                },
            }
            write_json_new(runtime_config_path, runtime_payload)
            complete_step("runtime_config", {"config": descriptor(runtime_config_path)})
        else:
            profile_payload = json.loads(profile_registry_path.read_text(encoding="utf-8"))
            profile_payload["stage119Fingerprint"] = manifest["datasetFingerprint"]
            profile_payload["scan"] = {
                "status": "not_ready",
                "reason": "Geometry/material closures have not passed automatic first-run audit.",
                "sceneDiscoveryStatus": scene_discovery_result.get("status"),
            }
            write_json_atomic(profile_registry_path, profile_payload)
            runtime_payload = json.loads(runtime_config_path.read_text(encoding="utf-8"))
            runtime_payload["helperDependencies"] = dependency_result
            runtime_payload["sceneDiscovery"] = scene_discovery_result
            runtime_payload.setdefault("paths", {})["stage119_root"] = str(stage_root)
            runtime_payload.setdefault("extraction", {})["mapLayerFingerprint"] = manifest[
                "datasetFingerprint"
            ]
            runtime_payload["extraction"]["sceneDiscovery"] = scene_discovery_result
            runtime_payload.setdefault("extraction", {})["runtimeManifest"] = descriptor(runtime_manifest_path)
            runtime_payload["extraction"]["maps"] = runtime_maps
            write_json_atomic(runtime_config_path, runtime_payload)
            complete_step("runtime_config", {"config": descriptor(runtime_config_path)})

        runtime_payload = json.loads(runtime_config_path.read_text(encoding="utf-8"))
        portable_ready = scene_discovery_result.get("status") == "completed" and all(
            isinstance(entry.get("sceneProbe"), dict)
            and Path(entry["sceneProbe"]["path"]).is_file()
            for entry in runtime_maps.values()
        )
        runtime_payload["paths"]["blender_exe"] = str(selected_blender) if selected_blender else None
        runtime_payload["blenderBuild"] = {
            "status": "ready" if portable_ready else "not_ready",
            "contract": "EndfieldPortableScenePlan/1",
            "reason": "Per-selection exact ECS plans; unresolved assets are reported explicitly"
            if portable_ready else "Resume first-run preparation to generate SceneProbe geometry and material caches",
        }
        write_json_atomic(runtime_config_path, runtime_payload)
        if portable_ready:
            from endfield_map_overviews import LAYOUT_PATH, build_map_overviews

            overview_sources = {
                "datasetFingerprint": manifest["datasetFingerprint"],
                "runtimeManifest": descriptor(runtime_manifest_path)["sha256"],
                "previewLayout": descriptor(LAYOUT_PATH)["sha256"],
                "sceneManifests": {map_id: entry["sceneProbe"]["sha256"] for map_id, entry in runtime_maps.items()},
                "generator": "EndfieldMapOverviews/1",
                "generatorSha256": descriptor(Path(__file__).with_name("endfield_map_overviews.py"))["sha256"],
            }
            previous_overviews = state.get("steps", {}).get("map_overviews", {}).get("outputs", {})
            if step_done("map_overviews") and previous_overviews.get("sources") == overview_sources:
                overview_descriptor = previous_overviews["manifest"]
                if not Path(overview_descriptor["path"]).is_file():
                    raise FirstRunError("Prepared overview manifest is missing; restore the cache before resuming")
            else:
                emitter.emit("map_overviews", 0.0, "Composing map images from extracted H tiles")
                overview_descriptor = build_map_overviews(runtime_config_path, next_available(run_root / "map_overviews"))
                complete_step("map_overviews", {"manifest": overview_descriptor, "sources": overview_sources})
            runtime_payload["extraction"]["mapOverviews"] = overview_descriptor
            write_json_atomic(runtime_config_path, runtime_payload)
            emitter.emit("map_overviews", 1.0, "Prepared map images for the WebUI")
        complete_step("runtime_config", {"config": descriptor(runtime_config_path)})
        activation = (
            {"status": "skipped", "reason": "--no-activate-runtime-config"}
            if args.no_activate_runtime_config
            else activate_runtime_config(runtime_payload)
        )

        deferred_capabilities = []
        if dependency_result.get("status") != "passed":
            deferred_capabilities.append("helper_dependencies")
        if scene_discovery_result.get("status") != "completed":
            deferred_capabilities.append("scene_discovery")
        completion_status = "completed" if not deferred_capabilities else "completed_with_deferred_helpers"
        state["status"] = completion_status
        state["completedAt"] = utc_now()
        save_state()
        report = {
            "format": REPORT_FORMAT,
            "status": completion_status,
            "completedAt": utc_now(),
            "gameReadOnly": True,
            "runRoot": str(run_root),
            "logRoot": str(log_root),
            "stageRoot": str(stage_root),
            "datasetFingerprint": manifest["datasetFingerprint"],
            "runtimeConfig": descriptor(runtime_config_path),
            "runtimeManifest": descriptor(runtime_manifest_path),
            "runtimeActivation": activation,
            "audit": audit_report,
            "mapLayers": build_summary.get("maps"),
            "helperDependencies": dependency_result,
            "sceneDiscovery": scene_discovery_result,
            "automaticDataExtraction": {
                "maps": runtime_maps,
                "terrainIncluded": True,
                "assetResolutionIncluded": True,
                "scenePayloadIncludedWhenHelpersAvailable": True,
                "manualHistoricalInputsRequired": False,
            },
            "deferredCapabilities": deferred_capabilities,
            "blenderBuild": runtime_payload["blenderBuild"],
        }
        write_json_new(report_path, report)
        emitter.emit("complete", 1.0, "First-run baseline extraction completed", status=completion_status)
        return 0
    except Exception as error:
        state["status"] = "cancelled" if cancelled() else "failed"
        state["error"] = f"{type(error).__name__}: {error}"
        state["traceback"] = traceback.format_exc()
        save_state()
        failure = {
            "format": REPORT_FORMAT,
            "status": state["status"],
            "failedAt": utc_now(),
            "error": state["error"],
            "statePath": str(state_path),
            "partialOutputsPreserved": True,
            "resumeCommand": f'python "{Path(__file__).resolve()}" --resume --run-name "{args.run_name}"',
        }
        failure_path = next_available(report_path) if report_path.exists() else report_path
        write_json_new(failure_path, failure)
        emitter.emit("failed", 1.0, str(error), status=state["status"])
        return 3 if cancelled() else 1


def main() -> int:
    try:
        return run()
    except (ConfigurationError, FirstRunError, FileNotFoundError, FileExistsError, ValueError) as error:
        print(f"FIRST_RUN_ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
