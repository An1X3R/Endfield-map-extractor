"""Export audited selection records and optional portable Blender scenes.

Scene jobs reuse prepared caches and read current ECS evidence from the game.
All writes remain in the user's external cache and output directories.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping

from endfield_runtime_config import (
    RuntimeConfig,
    configured_optional_path,
    configured_path,
    load_runtime_config,
    path_hint,
)
from webui_export_contract_v2 import canonicalize_job, estimate_workload
from webui_job_store import JobStore


EXPECTED_MANIFEST_FORMAT = "EndfieldRegionMapLayerManifest/1"
BLENDER_PROFILES = Path(__file__).resolve().with_name("endfield_blender_worker_profiles_v1.json")


class JobCancelled(RuntimeError):
    pass


def resolve_runtime_paths(
    *,
    stage119_root: Path | str | None = None,
    runtime_config: Path | str | None = None,
) -> tuple[dict[str, Path | None], RuntimeConfig]:
    """Resolve worker defaults without opening cache records or Blender."""

    config = load_runtime_config(runtime_config, root=Path(__file__).resolve().parent)
    config_dir = config.directory
    return {
        "stage119_root": configured_path(
            stage119_root,
            env_name="ENDFIELD_STAGE119_ROOT",
            env_aliases=("ENDFIELD_MAP_LAYER_ROOT",),
            label="Stage119 cache root",
            config_value=config.value("stage119_root"),
            config_dir=config_dir,
            cli_option="--stage119-root",
            config_key="stage119_root",
        ),
        "export_root": configured_optional_path(
            None,
            env_name="ENDFIELD_EXPORT_ROOT",
            label="Endfield export root",
            config_value=config.value("export_root"),
            config_dir=config_dir,
            cli_option="--export-root",
            config_key="export_root",
        ),
        "game_root": configured_optional_path(
            None,
            env_name="ENDFIELD_GAME_ROOT",
            label="Endfield game root",
            config_value=config.value("game_root"),
            config_dir=config_dir,
            cli_option="--game-root",
            config_key="game_root",
        ),
        "blender_exe": configured_optional_path(
            None,
            env_name="ENDFIELD_BLENDER",
            label="Blender executable",
            config_value=config.value("blender_exe"),
            config_dir=config_dir,
            cli_option="--blender-exe",
            config_key="blender_exe",
        ),
        "profile_registry": configured_optional_path(
            None,
            env_name="ENDFIELD_BLENDER_PROFILE_REGISTRY",
            label="Blender profile registry",
            config_value=config.value("blender_profile_registry"),
            config_dir=config_dir,
            cli_option="--profile-registry",
            config_key="blender_profile_registry",
        ) or (BLENDER_PROFILES if BLENDER_PROFILES.is_file() else None),
    }, config


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def safe_child(root: Path, relative: str) -> Path:
    candidate = (root / Path(relative.replace("/", "\\"))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Stage119 path escapes dataset root: {relative}") from error
    return candidate


def records_path(index: Mapping[str, Any]) -> str | None:
    value = index.get("records")
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("path"), str):
        return str(value["path"])
    return None


def iter_jsonl_gzip(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def selected_sector_keys(bounds: Mapping[str, Any]) -> list[str]:
    xmin, xmax = float(bounds["xmin"]), float(bounds["xmax"])
    zmin, zmax = float(bounds["zmin"]), float(bounds["zmax"])
    sx_min = math.floor(xmin / 128.0)
    sx_max = math.ceil(xmax / 128.0) - 1
    sz_min = math.floor(zmin / 128.0)
    sz_max = math.ceil(zmax / 128.0) - 1
    return [
        f"sector:{sector_x}:{sector_z}"
        for sector_z in range(sz_min, sz_max + 1)
        for sector_x in range(sx_min, sx_max + 1)
    ]


class ExportCoordinator:
    def __init__(
        self,
        stage119_root: Path | str | None = None,
        store: JobStore | None = None,
        *,
        runtime_config: Path | str | None = None,
    ) -> None:
        self.runtime_paths, self.runtime_config = resolve_runtime_paths(
            stage119_root=stage119_root,
            runtime_config=runtime_config,
        )
        self.stage119_root = self.runtime_paths["stage119_root"]
        configured_job_root = os.environ.get("ENDFIELD_WEBUI_JOB_ROOT", "").strip()
        if configured_job_root:
            job_root = Path(configured_job_root).expanduser().resolve()
        elif self.runtime_paths["export_root"] is not None:
            job_root = self.runtime_paths["export_root"] / "log" / "webui_jobs"
        else:
            job_root = self.stage119_root.parent / "webui_jobs"
        self.store = store or JobStore(job_root)
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def validate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        canonical = canonicalize_job(payload)
        if canonical["export_mode"] == "blend":
            self._validate_scene_runtime(canonical)
            return {
                "format": "EndfieldWebUIJobValidation/1", "status": "valid", "canonical": canonical,
                "estimate": estimate_workload(canonical), "exportMode": "blend",
                "capabilities": {"data_package": "ready", "blender_build": "ready"},
                "blenderBuild": {"status": "ready", "scheduled": False},
            }
        return {
            "format": "EndfieldWebUIJobValidation/1",
            "status": "valid",
            "canonical": canonical,
            "estimate": estimate_workload(canonical),
            "exportMode": "data_package",
            "capabilities": {
                "data_package": "ready",
                "blender_build": "not_ready",
                "profile_scan": "not_ready",
            },
            "blenderBuild": {
                "status": "not_ready",
                "scheduled": False,
                "reason": "Portable reviewed Blender profiles are not available in this public release.",
            },
        }

    def submit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        canonical = canonicalize_job(payload)
        if canonical["export_mode"] == "blend":
            self._validate_scene_runtime(canonical)
        job = self.store.create(canonical)
        thread = threading.Thread(
            target=self._run,
            args=(str(job["job_id"]),),
            name=f"endfield-export-{job['job_id']}",
            daemon=True,
        )
        with self._lock:
            self._threads[str(job["job_id"])] = thread
        thread.start()
        return job

    def _validate_scene_runtime(self, canonical: Mapping[str, Any]) -> None:
        from endfield_scene_export import scene_runtime
        if self.runtime_config.path is None:
            raise ValueError("Blender export requires a prepared runtime configuration; complete first-run preparation")
        scene_runtime(self.runtime_config.path, Path(canonical["source"]["game_root"]), canonical["map_id"])

    def get(self, job_id: str) -> dict[str, Any]:
        return self.store.get(job_id)

    def events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.store.events(job_id, after)

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self.store.request_cancel(job_id)

    def _check_cancelled(self, job_id: str) -> None:
        if self.store.cancel_requested(job_id):
            raise JobCancelled(job_id)

    def _load_dataset(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        if not self.stage119_root.is_dir():
            raise RuntimeError(
                f"Stage119 cache directory is missing: {self.stage119_root}. "
                + path_hint(
                    cli_option="--stage119-root",
                    env_name="ENDFIELD_STAGE119_ROOT",
                    config_key="stage119_root",
                )
            )
        manifest_path = self.stage119_root / "region_map_layer_manifest.json"
        audit_path = self.stage119_root / "full_audit.json"
        missing = [path.name for path in (manifest_path, audit_path) if not path.is_file()]
        if missing:
            raise RuntimeError(
                f"Stage119 cache is incomplete at {self.stage119_root}; missing: {', '.join(missing)}. "
                "Use the audited Stage119 cache root, not a parent log directory."
            )
        manifest = read_json(manifest_path)
        audit = read_json(audit_path)
        fingerprint = str(manifest.get("datasetFingerprint") or "")
        if (
            manifest.get("format") != EXPECTED_MANIFEST_FORMAT
            or len(fingerprint) != 64
            or any(character not in "0123456789ABCDEF" for character in fingerprint)
            or audit.get("status") != "passed"
            or audit.get("datasetFingerprint") != fingerprint
        ):
            raise RuntimeError("Stage119 dataset failed the export worker version gate")
        return manifest, {}

    def _index(self, map_id: str, layer: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
        relative = manifest["maps"][map_id]["layers"][layer]["indexPath"]
        return read_json(safe_child(self.stage119_root, str(relative)))

    def _write_records(
        self,
        output: Path,
        records: Iterable[dict[str, Any]],
        job_id: str,
    ) -> dict[str, Any]:
        count = 0
        with gzip.open(output, "xt", encoding="utf-8", newline="\n") as stream:
            for record in records:
                self._check_cancelled(job_id)
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
        return {
            "path": output.name,
            "recordCount": count,
            "bytes": output.stat().st_size,
            "sha256": sha256_file(output),
        }

    def _selected_instance_records(
        self,
        index: Mapping[str, Any],
        sectors: set[str],
        categories: set[str] | None,
        job_id: str,
    ) -> Iterable[dict[str, Any]]:
        for shard in index.get("shards") or []:
            if shard.get("sectorKey") not in sectors:
                continue
            for record in iter_jsonl_gzip(safe_child(self.stage119_root, str(shard["path"]))):
                self._check_cancelled(job_id)
                if categories is None or record.get("category") in categories:
                    yield record

    def _selected_flat_records(
        self,
        index: Mapping[str, Any],
        sectors: set[str],
        job_id: str,
    ) -> Iterable[dict[str, Any]]:
        relative = records_path(index)
        if not relative:
            return
        for record in iter_jsonl_gzip(safe_child(self.stage119_root, relative)):
            self._check_cancelled(job_id)
            if sectors.intersection(record.get("sectorKeys") or []):
                yield record

    def _selected_road_records(
        self,
        index: Mapping[str, Any],
        sectors: set[str],
        job_id: str,
    ) -> Iterable[dict[str, Any]]:
        for shard in index.get("roadShards") or []:
            if shard.get("sectorKey") not in sectors:
                continue
            for record in iter_jsonl_gzip(safe_child(self.stage119_root, str(shard["path"]))):
                self._check_cancelled(job_id)
                if record.get("category") == "road":
                    yield record

    def _run(self, job_id: str) -> None:
        output_dir: Path | None = None
        try:
            job = self.store.transition(job_id, "preparing", progress=0.05, message="Validating Stage119 and output boundaries")
            canonical = job["canonical"]
            manifest, _ = self._load_dataset()
            self._check_cancelled(job_id)

            output_root = Path(canonical["output"]["root"]).resolve()
            output_root.mkdir(parents=True, exist_ok=True)
            output_dir = output_root / f"{canonical['output']['label']}__{canonical['map_id']}__{job_id[:8]}"
            output_dir.mkdir(exist_ok=False)
            (output_dir / "INCOMPLETE").write_text("Export is running.\n", encoding="ascii")
            (output_dir / "canonical_request.json").write_text(
                json.dumps(canonical, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            self.store.transition(job_id, "extracting", progress=0.16, message="Reading selected Stage119 sector records")
            sector_list = selected_sector_keys(canonical["bounds"])
            sectors = set(sector_list)
            map_id = str(canonical["map_id"])
            groups = set(canonical.get("export_groups") or ["all"])
            all_groups = "all" in groups
            layer_dir = output_dir / "layers"
            layer_dir.mkdir()
            artifacts: dict[str, Any] = {}
            warnings: list[str] = []

            from webui_source_contract import selected_static_categories
            categories = selected_static_categories(canonical["layers"], groups)
            if categories:
                index = self._index(map_id, "instances", manifest)
                artifacts["instances"] = self._write_records(
                    layer_dir / "instances.jsonl.gz",
                    self._selected_instance_records(index, sectors, categories, job_id),
                    job_id,
                )

            if canonical["layers"].get("roads") and (all_groups or "road" in groups):
                index = self._index(map_id, "roads", manifest)
                artifacts["roads"] = self._write_records(
                    layer_dir / "roads.jsonl.gz",
                    self._selected_road_records(index, sectors, job_id),
                    job_id,
                )

            if canonical["layers"].get("water") and (all_groups or "water" in groups):
                index = self._index(map_id, "water", manifest)
                artifacts["water"] = self._write_records(
                    layer_dir / "water.jsonl.gz",
                    self._selected_flat_records(index, sectors, job_id),
                    job_id,
                )
                if map_id == "map01":
                    warnings.append("Map01 water remains candidate sector coverage, not verified surface geometry.")

            if canonical["layers"].get("effects") and (all_groups or groups.intersection({"effects", "particle"})):
                index = self._index(map_id, "effects", manifest)
                artifacts["effects"] = self._write_records(
                    layer_dir / "effects.jsonl.gz",
                    self._selected_flat_records(index, sectors, job_id),
                    job_id,
                )

            if canonical["layers"].get("lights") and (all_groups or "lighting" in groups):
                index = self._index(map_id, "lights", manifest)
                artifacts["lights"] = self._write_records(
                    layer_dir / "lights.jsonl.gz",
                    self._selected_flat_records(index, sectors, job_id),
                    job_id,
                )
                warnings.append(f"Light scan status is {index.get('scan', {}).get('status', 'unknown')}.")

            if canonical["layers"].get("terrain") and (all_groups or "terrain" in groups):
                warnings.append("Terrain selection is recorded by sector; Stage119 does not contain terrain mesh payloads.")

            self._check_cancelled(job_id)
            self.store.transition(job_id, "building", progress=0.22, message="Signing the selected Stage119 data package")
            selection = {
                "format": "EndfieldRegionSelectionExport/1",
                "mapId": map_id,
                "bounds": canonical["bounds"],
                "boundsSemantics": "half-open [min,max)",
                "sectorKeys": sector_list,
                "effectsPolicy": canonical["effects"]["selection_mode"],
                "waterMode": canonical["water_mode"],
                "blenderAxis": "(X,Y,Z)=(Unity X,-Unity Z,Unity Y)",
            }
            selection_path = output_dir / "selection.json"
            selection_path.write_text(json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            scene_artifacts = []
            blender_build = {
                "status": "not_ready", "scheduled": False,
                "reason": "Data-package export does not schedule Blender",
            }
            if canonical["export_mode"] == "blend":
                from endfield_scene_export import build_scene_exports
                scene_artifacts = build_scene_exports(
                    self.runtime_config.path, canonical, output_dir,
                    lambda: self._check_cancelled(job_id),
                    lambda value, message: self.store.update_progress(
                        job_id, phase="building", progress=0.24 + value * 0.66, message=message),
                )
                blender_build = {
                    "status": "partial" if any(item["pending"] for item in scene_artifacts) else "completed",
                    "scheduled": True, "scenes": scene_artifacts,
                    "pending": sum(item["pending"] for item in scene_artifacts),
                }
                warnings = [warning for warning in warnings if "does not contain terrain mesh" not in warning]
                if blender_build["pending"]:
                    warnings.append(f"Scene reports retain {blender_build['pending']} unresolved resources; inspect scene_plan.json and scene_audit.json")

            export_manifest = {
                "format": "EndfieldWebUIRegionExport/1",
                "jobId": job_id,
                "datasetFingerprint": manifest["datasetFingerprint"],
                "mapId": map_id,
                "selection": selection,
                "artifacts": artifacts,
                "warnings": warnings,
                "exportMode": canonical["export_mode"],
                "capabilities": {
                    "data_package": "ready",
                    "blender_build": blender_build["status"] if scene_artifacts else "not_ready",
                    "profile_scan": "not_ready",
                },
                "blenderBuild": blender_build,
            }
            manifest_path = output_dir / "export_manifest.json"
            manifest_path.write_text(json.dumps(export_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            audit = {
                "status": "passed",
                "scope": "artifact integrity and scene reopen; resource completeness is reported separately",
                "sceneResolutionStatus": blender_build["status"],
                "canonicalRequestSha256": sha256_file(output_dir / "canonical_request.json"),
                "manifestSha256": sha256_file(manifest_path),
                "selectionSha256": sha256_file(selection_path),
                "artifactCount": len(artifacts),
                "recordCount": sum(int(item["recordCount"]) for item in artifacts.values()),
            }
            audit_path = output_dir / "audit.json"
            audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (output_dir / "INCOMPLETE").unlink()

            self._check_cancelled(job_id)
            self.store.transition(job_id, "auditing", progress=0.94, message="Verifying the signed data package and final hashes")
            self.store.complete(job_id, {
                "package": {
                    "output_dir": str(output_dir),
                    "manifest": str(manifest_path),
                    "audit": str(audit_path),
                    **audit,
                    "status": "completed",
                    "auditStatus": audit["status"],
                    "exportMode": canonical["export_mode"],
                    "warnings": warnings,
                },
                "blenderBuild": blender_build,
            })
        except JobCancelled:
            current = self.store.get(job_id)
            if current["state"] not in {"completed", "failed", "cancelled"}:
                self.store.transition(job_id, "cancelled", progress=float(current["progress"]), message="Export cancelled; partial files were preserved")
        except Exception as error:
            self.store.fail(job_id, {
                "type": type(error).__name__,
                "message": str(error),
                "partial_output": str(output_dir) if output_dir else None,
            })
        finally:
            with self._lock:
                self._threads.pop(job_id, None)
