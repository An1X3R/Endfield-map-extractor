"""Coordinate source-verified, cancellable exports of individual discovered maps."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from threading import Event, RLock, Thread
from uuid import uuid4

from endfield_character_cache import CharacterSourceChangedError, source_stamp
from endfield_character_sources import CharacterSourceContext, game_source_stamps, validate_character_storage
from endfield_level_maps import (LevelMapCatalog, discover_level_maps, export_level_map,
    level_map_source_context, public_level_map_catalog, read_level_map_manifest, resolve_level_map_image)
from endfield_scene_helpers import require_prepared_scene_helpers
from endfield_scene_material_contract import require_object
from export_level_map import bind_level_map_registry
from webui_level_maps import LevelMapsNotPrepared, prepared_level_map_manifest

_TERMINAL = frozenset(("completed", "failed", "cancelled"))


class LevelMapPreparationUnavailable(RuntimeError):
    """A source runtime must be prepared before its maps can be discovered."""


class LevelMapPreparationConflict(RuntimeError):
    """Another task or source generation prevents the requested preparation."""


class LevelMapPreparationCancelled(RuntimeError):
    """Cooperative cancellation stops before publishing a new runtime descriptor."""


@dataclass(frozen=True)
class MapPreparationJob:
    job_id: str
    level_id: str
    catalog_id: str
    state: str
    phase: str
    progress: float
    message: str
    cancel_requested: bool
    error: dict[str, str] | None
    result: dict[str, object] | None


@dataclass(frozen=True)
class MapPreparationSource:
    runtime: Path
    source_key: str
    context: CharacterSourceContext
    catalog: LevelMapCatalog


def runtime_source_key(payload: bytes) -> str:
    """Ignore only the map binding when identifying the active source configuration."""
    runtime = require_object(json.loads(payload.decode("utf-8-sig")), "runtime")
    if runtime.get("format") != "EndfieldRuntimeConfig/1":
        raise ValueError("Map preparation requires EndfieldRuntimeConfig/1")
    extraction = require_object(runtime.get("extraction"), "runtime.extraction")
    source = {**runtime, "extraction": {key: value for key, value in extraction.items() if key != "levelMaps"}}
    return sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def catalog_game_identity(catalog: LevelMapCatalog) -> str:
    """Use the exporter's exact source generation identity for reuse decisions."""
    return sha256(json.dumps([asdict(stamp) for stamp in catalog.game_stamps], sort_keys=True).encode()).hexdigest()


def source_unchanged(source: MapPreparationSource) -> None:
    """Check lightweight stamps without repeating VFS discovery for every poll."""
    context = source.context
    indexes = (context.resource_database, context.provider_index, context.cab_inventory)
    if tuple(source_stamp(path) for path in indexes) != source.catalog.index_stamps:
        raise CharacterSourceChangedError("Map source indexes changed; refresh the map catalog")
    if game_source_stamps(context.game_root) != [asdict(stamp) for stamp in source.catalog.game_stamps]:
        raise CharacterSourceChangedError("Game source files changed; prepare the current game source again")


def current_map_entries(runtime: Path, catalog: LevelMapCatalog) -> tuple[Path | None, list[dict[str, object]]]:
    """Reject stale registered images rather than presenting them as current maps."""
    try:
        manifest = prepared_level_map_manifest(runtime)
    except LevelMapsNotPrepared:
        return None, []
    registry = read_level_map_manifest(manifest, manifest.parent)
    entries = [require_object(row, "level map entry") for row in registry["entries"]]
    identity = catalog_game_identity(catalog)
    configs = {entry.level_id: entry.config_sha256 for entry in catalog.entries}
    for entry in entries:
        config = require_object(entry.get("sourceConfig"), "level map sourceConfig")
        if entry.get("sourceGameIdentity") != identity or configs.get(str(entry.get("levelId"))) != config.get("sha256"):
            raise LevelMapPreparationConflict(f"Registered map does not match the current source: {entry.get('levelId')}; remove the stale map binding before retrying")
    return manifest, entries


def publish_level_map(staged: Path, root: Path, job_id: str, existing: list[dict[str, object]]) -> Path:
    """Publish a new registry without modifying the registry currently in use."""
    if re.fullmatch(r"[a-f0-9]{32}", job_id) is None:
        raise ValueError("Map registry publication requires a safe job identifier")
    registry = read_level_map_manifest(staged, staged.parent)
    added: list[dict[str, object]] = []
    for row in registry["entries"]:
        entry = require_object(row, "staged level map")
        image = require_object(entry["image"], "staged level map image")
        relative = (staged.parent.relative_to(root) / str(image["relativePath"])).as_posix()
        added.append({**entry, "image": {**image, "relativePath": relative}})
    if len(added) != 1 or any(row["levelId"] == added[0]["levelId"] for row in existing):
        raise ValueError("Map task must publish exactly one previously unregistered level")
    manifest = root / f"registry-{job_id}.json"
    with manifest.open("x", encoding="utf-8") as stream:
        json.dump({"format": "EndfieldLevelMaps/1", "entries": [*existing, *added]}, stream, ensure_ascii=False, indent=2)
    resolve_level_map_image(manifest, root, str(added[0]["levelId"]))
    return manifest


class LevelMapPreparationCoordinator:
    """Own one external extraction task while exposing immutable status snapshots."""

    def __init__(self, runtime_provider: Callable[[], Path | None], state_root: Path) -> None:
        self._runtime_provider = runtime_provider
        self._state_root = state_root.resolve()
        self._job_roots: dict[str, Path] = {}
        self._lock = RLock()
        self._source: MapPreparationSource | None = None
        self._job: MapPreparationJob | None = None
        self._thread: Thread | None = None
        self._cancel = Event()
        self._closed = False

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise LevelMapPreparationCancelled("Map preparation was cancelled; completed files are retained")

    def _get_source(self) -> MapPreparationSource:
        runtime = self._runtime_provider()
        if runtime is None:
            raise LevelMapPreparationUnavailable("请先完成场景首次准备，再发现并准备地图。")
        runtime = runtime.resolve()
        key = runtime_source_key(runtime.read_bytes())
        if self._source is not None and self._source.runtime == runtime and self._source.source_key == key:
            source_unchanged(self._source)
            return self._source
        context = level_map_source_context(runtime)
        for destination in (runtime.parent, context.output_root):
            validate_character_storage(context.game_root, destination)
        catalog = discover_level_maps(context, lambda: None)
        source = MapPreparationSource(runtime, key, context, catalog)
        self._source = source
        return source

    def catalog(self) -> dict[str, object]:
        with self._lock:
            source = self._get_source()
            if self._job is not None and self._job.state not in _TERMINAL and self._job.catalog_id != source.catalog.catalog_id:
                raise LevelMapPreparationConflict("Active map task belongs to a different source; cancel it before preparing this source")
            _, prepared = current_map_entries(source.runtime, source.catalog)
            return {**public_level_map_catalog(source.catalog), "preparedLevelIds": [row["levelId"] for row in prepared],
                    "activeJob": self._public(self._job) if self._job is not None and self._job.catalog_id == source.catalog.catalog_id else None}

    def _public(self, job: MapPreparationJob) -> dict[str, object]:
        return {"format": "EndfieldLevelMapPreparationJob/1", "progress_scope": "phase", **asdict(job)}

    def _save(self, job: MapPreparationJob) -> None:
        state_root = self._job_roots.get(job.job_id, self._state_root)
        state_root.mkdir(parents=True, exist_ok=True)
        target = state_root / f"{job.job_id}.json"
        temporary = target.with_suffix(f".{uuid4().hex}.tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(self._public(job), stream, ensure_ascii=False, indent=2)
        temporary.replace(target)
        self._job = job

    def start(self, payload: object) -> dict[str, object]:
        request = require_object(payload, "map preparation request")
        if request.get("format") != "EndfieldLevelMapPreparationRequest/1":
            raise ValueError("Expected EndfieldLevelMapPreparationRequest/1")
        with self._lock:
            if self._closed:
                raise LevelMapPreparationUnavailable("Map preparation service has stopped")
            if self._job is not None and self._job.state not in _TERMINAL:
                raise LevelMapPreparationConflict(f"Map preparation is already active: {self._job.job_id}")
            source = self._get_source()
            if request.get("catalog_id") != source.catalog.catalog_id:
                raise LevelMapPreparationConflict("Map catalog changed; refresh it before starting preparation")
            matches = [entry for entry in source.catalog.entries if entry.level_id == request.get("level_id")]
            if len(matches) != 1 or matches[0].inverse_xz:
                raise ValueError(f"Map level is absent or requires unsupported inverse-XZ orientation: {request.get('level_id')}")
            job = MapPreparationJob(uuid4().hex, matches[0].level_id, source.catalog.catalog_id,
                "queued", "queued", 0., "地图准备任务已排队", False, None, None)
            self._cancel.clear()
            self._job_roots[job.job_id] = source.context.output_root / "level_map_jobs"
            self._save(job)
            self._thread = Thread(target=self._run, args=(source, job), name=f"level-map-{job.job_id}", daemon=True)
            self._thread.start()
            return self._public(job)

    def status(self, job_id: str) -> dict[str, object]:
        if re.fullmatch(r"[a-f0-9]{32}", job_id) is None:
            raise FileNotFoundError("Unknown map preparation job identifier")
        with self._lock:
            if self._job is not None and self._job.job_id == job_id:
                return self._public(self._job)
            candidates = tuple(dict.fromkeys((self._job_roots.get(job_id, self._state_root), self._state_root,
                *((self._source.context.output_root / "level_map_jobs",) if self._source is not None else ()))))
            matches = [root / f"{job_id}.json" for root in candidates if (root / f"{job_id}.json").is_file()]
            if not matches:
                raise FileNotFoundError(f"Unknown map preparation job: {job_id}")
            if len(matches) != 1:
                raise LevelMapPreparationConflict(f"Ambiguous stored map preparation job: {job_id}")
            path = matches[0]
            payload = require_object(json.loads(path.read_text(encoding="utf-8")), "map preparation job")
            if payload.get("job_id") != job_id or payload.get("format") != "EndfieldLevelMapPreparationJob/1":
                raise ValueError(f"Stored map task identity is invalid: {path}")
            if payload.get("state") not in _TERMINAL:
                raise LevelMapPreparationConflict("Map task was interrupted by a service restart; completed files are retained, start a new task")
            return payload

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            snapshot = self.status(job_id)
            if snapshot["state"] in _TERMINAL:
                return snapshot
            if self._job is None or self._job.job_id != job_id:
                raise LevelMapPreparationConflict("Map task is not owned by the current service")
            self._cancel.set()
            self._save(replace(self._job, cancel_requested=True, message="正在停止地图准备"))
            return self._public(self._job)

    def _progress(self, phase: str, fraction: float, message: str) -> None:
        self._check_cancelled()
        if not math.isfinite(fraction) or not 0. <= fraction <= 1.:
            raise ValueError(f"Invalid map preparation progress: phase={phase}, fraction={fraction}")
        with self._lock:
            if self._job is None:
                raise RuntimeError("Map preparation status is missing")
            self._save(replace(self._job, state="running", phase=phase, progress=min(fraction, .98), message=message))

    def _run(self, source: MapPreparationSource, job: MapPreparationJob) -> None:
        try:
            self._progress("verifying", .02, "正在校验当前地图来源")
            original = source.runtime.read_bytes()
            if runtime_source_key(original) != source.source_key:
                raise LevelMapPreparationConflict("Active runtime source changed before map preparation")
            source_unchanged(source)
            manifest, entries = current_map_entries(source.runtime, source.catalog)
            reused = any(entry["levelId"] == job.level_id for entry in entries)
            if reused:
                if manifest is None:
                    raise RuntimeError("Registered map has no registry")
                resolve_level_map_image(manifest, manifest.parent, job.level_id)
            else:
                self._check_cancelled()
                helpers = require_prepared_scene_helpers(source.runtime, source.context.cache_root)
                self._check_cancelled()
                root = manifest.parent if manifest is not None else source.context.output_root / "level_maps" / source.catalog.catalog_id[:16]
                validate_character_storage(source.context.game_root, root)
                staged = export_level_map(source.context, source.catalog, job.level_id, helpers.probe,
                    root / "jobs" / job.job_id, self._check_cancelled, self._progress)
                self._progress("publishing", .98, "正在登记已完成的地图")
                manifest = publish_level_map(staged, root, job.job_id, entries)
            source_unchanged(source)
            with self._lock:
                self._check_cancelled()
                active_runtime = self._runtime_provider()
                if active_runtime is None or active_runtime.resolve() != source.runtime or source.runtime.read_bytes() != original:
                    raise LevelMapPreparationConflict("Active runtime changed during map preparation; completed map retained without rebinding")
                if not reused:
                    bind_level_map_registry(source.runtime, original, manifest)
                self._save(replace(self._job or job, state="completed", phase="completed", progress=1.,
                    message="已校验并复用现有地图" if reused else "地图准备完成", result={"level_id": job.level_id,
                    "reused": reused, "manifest": str(manifest), "runtime": str(source.runtime)}))
        except LevelMapPreparationCancelled as error:
            with self._lock:
                self._save(replace(self._job or job, state="cancelled", phase="cancelled", message=str(error)))
        except Exception as error:
            with self._lock:
                self._save(replace(self._job or job, state="failed", phase="failed", message=str(error),
                    error={"type": type(error).__name__, "message": str(error)}))
            raise

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel.set()
            thread = self._thread
        if thread is not None:
            thread.join(10)
            if thread.is_alive():
                raise RuntimeError("Map preparation did not stop within 10 seconds")
