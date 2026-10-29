"""Run portable scene builds from a user's prepared runtime and selected Blender."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
import threading
from typing import NotRequired, TypedDict, cast

from endfield_scene_material_contract import require_object
from endfield_scene_io import SceneIoSession, scene_io_session, single_scene_plan_io_session
from endfield_scene_pipeline import SceneWorkerCleanupError
from endfield_scene_capacity import (
    ActiveMemoryReservation, ProcessMemorySnapshot, memory_admission,
    process_memory_snapshot, system_memory_snapshot,
)
from endfield_scene_memory import (
    SceneMemoryCalibration, SceneMemoryCatalog, SceneMemoryEstimate, SceneMemoryWorkload,
    cached_scene_memory_catalog, merge_scene_memory_calibrations, read_scene_memory_calibrations, scene_memory_catalog_key,
    scene_memory_estimate, scene_memory_tool_signature, scene_memory_workload,
    write_scene_memory_calibrations,
)


_LOGGER = logging.getLogger(__name__)
_SCENE_EXPORT_LOCK = threading.Lock()
_SCENE_PROCESS_TERMINATION_ATTEMPTS = 3
_SCENE_PROCESS_TERMINATION_TIMEOUT_SECONDS = 5.0
_SCENE_PROCESS_EXIT_WAIT_SECONDS = 0.5
_SCENE_PROCESS_RETRY_DELAY_SECONDS = 0.2


class SceneArtifact(TypedDict):
    path: str
    audit: str
    plan: str
    bytes: int
    sha256: str
    instances: int
    pending: int


class SceneStageTiming(TypedDict):
    stage: str
    batch: int
    seconds: float
    started_seconds: NotRequired[float]
    memory: NotRequired[dict[str, int]]


@dataclass(frozen=True)
class PreparedSceneBatch:
    index: int
    plan: Path
    directory: Path
    memory_workload: SceneMemoryWorkload | None
    memory_estimate: SceneMemoryEstimate | None


@dataclass(frozen=True)
class _SceneMemoryObservation:
    current: ProcessMemorySnapshot
    peak_working_set_bytes: int
    peak_commit_bytes: int
    minimum_available_physical_bytes: int
    minimum_available_commit_bytes: int
    samples: int


class SceneProcessCleanupError(SceneWorkerCleanupError):
    """A helper process remained alive after bounded termination attempts."""


def scene_runtime(runtime_path: Path, game_root: Path, map_id: str) -> dict[str, object]:
    runtime = require_object(json.loads(runtime_path.read_text(encoding="utf-8-sig")), str(runtime_path))
    if runtime.get("format") != "EndfieldRuntimeConfig/1":
        raise ValueError(f"Unsupported runtime configuration: {runtime_path}")
    paths = require_object(runtime.get("paths"), "runtime paths")
    configured_game = Path(str(paths["game_root"])).expanduser()
    if not configured_game.is_absolute():
        configured_game = runtime_path.parent / configured_game
    if configured_game.resolve() != game_root.resolve():
        raise ValueError("Selected game differs from the prepared runtime; prepare the selected installation first")
    extraction = require_object(runtime.get("extraction"), "runtime extraction")
    maps = require_object(extraction.get("maps"), "runtime maps")
    entry = require_object(maps.get(map_id), f"runtime map {map_id}")
    probe = require_object(entry.get("sceneProbe"), f"SceneProbe is not ready for {map_id}; resume first-run preparation")
    for label, value in (("instances", entry.get("instances")), ("sceneProbe", probe.get("path"))):
        if not isinstance(value, str) or not value:
            raise ValueError(f"Missing prepared {label} for {map_id}")
        path = Path(value)
        resolved = path if path.is_absolute() else runtime_path.parent / path
        if not resolved.is_file():
            raise FileNotFoundError(f"Prepared {label} is missing: {resolved}")
    return runtime


def scene_batches(canonical: Mapping[str, object]) -> tuple[dict[str, float], ...]:
    bounds = require_object(canonical.get("bounds"), "canonical bounds")
    values = {name: float(cast(float, bounds[name])) for name in ("xmin", "xmax", "zmin", "zmax")}
    mode = canonical["batch_mode"]
    if mode == "merged_selection":
        return (values,)
    if mode not in ("per_sector", "cluster_4x4_sectors"):
        raise ValueError(f"Unsupported scene batch mode: {mode}")
    size = 128 if mode == "per_sector" else 512
    import math
    batches = []
    for z in range(math.floor(values["zmin"] / size) * size, math.ceil(values["zmax"] / size) * size, size):
        for x in range(math.floor(values["xmin"] / size) * size, math.ceil(values["xmax"] / size) * size, size):
            batches.append({"xmin": max(values["xmin"], x), "xmax": min(values["xmax"], x + size),
                            "zmin": max(values["zmin"], z), "zmax": min(values["zmax"], z + size)})
    return tuple(batches)


def run_scene_process(command: list[str], log: Path, environment: Mapping[str, str],
                      check_cancelled: Callable[[], None]) -> None:
    _run_observed_scene_process(command, log, environment, check_cancelled, _unmeasured_process)


def _unmeasured_process(process: subprocess.Popen[bytes]) -> None:
    """Keep non-Blender helpers independent of platform-specific memory probes."""


def _run_observed_scene_process(command: list[str], log: Path, environment: Mapping[str, str],
                                check_cancelled: Callable[[], None],
                                observe: Callable[[subprocess.Popen[bytes]], None]) -> None:
    """Run one helper and preserve the triggering error if process cleanup fails."""
    with log.open("x", encoding="utf-8") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                   env=dict(environment), creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        failure: BaseException | None = None
        try:
            while process.poll() is None:
                check_cancelled()
                observe(process)
                time.sleep(0.2)
            observe(process)
            if process.returncode != 0:
                raise RuntimeError(f"Scene helper {command[0]} exited with code {process.returncode}; inspect {log}")
        except BaseException as error:
            failure = error

        try:
            if process.poll() is None:
                _terminate_scene_process(process, log, failure)
        except BaseException as cleanup_error:
            if failure is not None:
                raise cleanup_error from failure
            raise

        if failure is not None:
            raise failure.with_traceback(failure.__traceback__)


def _terminate_scene_process(process: subprocess.Popen[bytes], log: Path,
                             original_error: BaseException | None) -> None:
    """Try bounded process-tree termination and return only after the child exits."""
    last_failure = "termination request did not stop the process"
    for attempt in range(1, _SCENE_PROCESS_TERMINATION_ATTEMPTS + 1):
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                stopped = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, text=True, check=False,
                    timeout=_SCENE_PROCESS_TERMINATION_TIMEOUT_SECONDS,
                )
                if process.poll() is not None:
                    return
                if stopped.returncode != 0:
                    last_failure = f"taskkill exit={stopped.returncode}; stderr={stopped.stderr.strip()}"
                else:
                    last_failure = "taskkill reported success but the process remains alive"
            elif attempt == 1:
                process.terminate()
            else:
                process.kill()
        except subprocess.TimeoutExpired as error:
            last_failure = f"termination request timed out: {error}"
        except OSError as error:
            last_failure = f"termination request failed: {type(error).__name__}: {error}"

        if process.poll() is not None:
            return
        try:
            process.wait(timeout=_SCENE_PROCESS_EXIT_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            last_failure = f"process remained alive after termination request: {last_failure}"
        except OSError as error:
            last_failure = f"process wait failed: {type(error).__name__}: {error}; {last_failure}"
        if process.poll() is not None:
            return

        _LOGGER.warning(
            "scene_process_termination_attempt_failed",
            extra={
                "scene_process_pid": process.pid,
                "scene_process_log": str(log),
                "scene_process_attempt": attempt,
                "scene_process_max_attempts": _SCENE_PROCESS_TERMINATION_ATTEMPTS,
                "scene_process_os": os.name,
                "scene_process_failure": last_failure,
            },
        )
        if attempt < _SCENE_PROCESS_TERMINATION_ATTEMPTS:
            time.sleep(_SCENE_PROCESS_RETRY_DELAY_SECONDS)

    original = "none" if original_error is None else f"{type(original_error).__name__}: {original_error}"
    _LOGGER.warning(
        "scene_process_exit_unconfirmed",
        extra={
            "scene_process_pid": process.pid,
            "scene_process_log": str(log),
            "scene_process_attempts": _SCENE_PROCESS_TERMINATION_ATTEMPTS,
            "scene_process_os": os.name,
            "scene_process_failure": last_failure,
            "scene_process_original_error_type": "none" if original_error is None else type(original_error).__name__,
            "scene_process_original_error": original,
        },
    )
    raise SceneProcessCleanupError(
        f"Cannot confirm scene helper exit after {_SCENE_PROCESS_TERMINATION_ATTEMPTS} attempts: "
        f"pid={process.pid}, log={log}, last_failure={last_failure}, original_error={original}"
    )


def copy_preflight_plan(source: Path, target: Path) -> Path:
    """Stage a completed, current preflight without rebuilding identical scene rows."""
    from endfield_scene_export_plan import load_scene_export_plan

    plan = load_scene_export_plan(source)
    sidecars = {"decal_manifest": target.with_name(target.stem + "_decals.json"),
                "decal_materials": target.with_name(target.stem + "_decal_materials.json")}
    for path in (target, *sidecars.values()):
        if path.exists():
            raise FileExistsError(f"Scene preflight destination already exists: {path}")
    for field, destination in sidecars.items():
        with Path(plan[field]).open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
    with target.open("x", encoding="utf-8") as stream:
        json.dump({**plan, **{field: str(path) for field, path in sidecars.items()}}, stream, indent=2)
    return target


def _can_reuse_preflight_plan(canonical: Mapping[str, object],
                             batches: tuple[dict[str, float], ...]) -> bool:
    """Require the exact selection that the final batch will copy from preflight."""
    return (len(batches) == 1 and batches[0] == canonical["bounds"]
            and canonical.get("selection_bounds", batches[0]) == batches[0])


def _scene_export_io_session(canonical: Mapping[str, object],
                             batches: tuple[dict[str, float], ...]) -> AbstractContextManager[SceneIoSession]:
    """Choose index retention from the exact final-plan reuse lifecycle."""
    if _can_reuse_preflight_plan(canonical, batches):
        return single_scene_plan_io_session()
    return scene_io_session()


def build_scene_exports(runtime_path: Path, canonical: Mapping[str, object], output: Path,
                        check_cancelled: Callable[[], None], progress: Callable[[float, str], None]) -> list[SceneArtifact]:
    # Map and independent-scene HTTP coordinators share the same resource budget.
    while not _SCENE_EXPORT_LOCK.acquire(timeout=0.25):
        check_cancelled()
    try:
        check_cancelled()
        batches = scene_batches(canonical)
        with _scene_export_io_session(canonical, batches) as io_session:
            return _build_scene_exports(runtime_path, canonical, output, check_cancelled, progress, io_session, batches)
    finally:
        _SCENE_EXPORT_LOCK.release()


def _refresh_after_resource_writes(io_session: SceneIoSession, resource_audit: Mapping[str, object]) -> None:
    """Refresh payload paths; source caches independently verify their input versions."""
    if "extraction" in resource_audit:
        io_session.refresh_resource_paths()


def _build_scene_exports(runtime_path: Path, canonical: Mapping[str, object], output: Path,
                         check_cancelled: Callable[[], None], progress: Callable[[float, str], None],
                         io_session: SceneIoSession, batches: tuple[dict[str, float], ...]) -> list[SceneArtifact]:
    from endfield_scene_export_plan import prepare_scene_export_plan
    from endfield_scene_resources import ensure_scene_resources
    from prepare_terrain_detail import prepare_scene_terrain_details

    source = require_object(canonical.get("source"), "scene source")
    game = Path(str(source["game_root"])).resolve()
    runtime = scene_runtime(runtime_path, game, str(canonical["map_id"]))
    paths = require_object(runtime["paths"], "runtime paths")
    cache = Path(str(paths["cache_root"])).expanduser()
    if not cache.is_absolute():
        cache = runtime_path.parent / cache
    temporary = cache.resolve() / "scene_export_temp"
    if temporary.is_relative_to(game) or output.resolve().is_relative_to(game):
        raise ValueError("Scene output and temporary files must be outside the game installation")
    temporary.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    builder = Path(__file__).resolve().parent / "blender_export_prepared_scene.py"
    started = time.perf_counter()
    timings: list[SceneStageTiming] = []
    progress(0.0, "Checking exact scene resource dependencies for the complete selection")
    attempted: set[str] = set()
    passes: list[dict[str, object]] = []
    while True:
        check_cancelled()
        stage_started = time.perf_counter()
        preflight = prepare_scene_export_plan(
            runtime_path, canonical,
            output / "resource_preflight" / f"pass_{len(passes) + 1:02d}" / "scene_plan.json",
            check_cancelled)
        timings.append({"stage": "resource_plan", "batch": 0, "seconds": time.perf_counter() - stage_started})
        prepared = require_object(json.loads(preflight.read_text(encoding="utf-8")), "resource preflight")
        requested = prepared.get("required_resource_paths")
        if not isinstance(requested, list) or any(not isinstance(path, str) for path in requested):
            raise ValueError("Resource preflight requires an array of exact resource paths")
        discovered = set(cast(list[str], requested))
        if passes and not discovered - attempted:
            break
        stage_started = time.perf_counter()
        resource_audit = ensure_scene_resources(runtime_path, preflight, check_cancelled,
                                               lambda value, message: progress(value * 0.15, message))
        _refresh_after_resource_writes(io_session, resource_audit)
        timings.append({"stage": "resource_prepare", "batch": 0, "seconds": time.perf_counter() - stage_started})
        passes.append(resource_audit)
        attempted.update(discovered)
        if (not discovered or resource_audit.get("missing") == []
                or resource_audit.get("cachedAttempt") is True):
            break
    resource_audit = {**passes[-1], "dependencyPasses": passes}
    (output / "resource_preflight" / "resource_audit.json").write_text(json.dumps(resource_audit, indent=2), encoding="utf-8")
    from endfield_scene_pipeline import run_scene_pipeline

    requested = os.environ.get("ENDFIELD_SCENE_WORKERS", "2")
    if requested not in ("1", "2"):
        raise ValueError("ENDFIELD_SCENE_WORKERS must be 1 or 2; unset it for automatic admission")
    capacity = 2 if requested == "2" and len(batches) > 1 and (os.cpu_count() or 1) >= 4 else 1
    measure_memory = os.name == "nt"
    initial_memory = system_memory_snapshot() if measure_memory or capacity > 1 else None
    initial_available = initial_memory.available_physical_bytes if initial_memory is not None else 0
    reserve_bytes = max(2 * 1024 ** 3, (initial_memory.total_physical_bytes + 9) // 10) if initial_memory is not None else 0
    tool_signature = scene_memory_tool_signature(Path(str(source["blender_exe"]))) if measure_memory else ""
    calibration_path = cache.resolve() / "scene_memory_calibration.json"
    calibrations = read_scene_memory_calibrations(calibration_path, tool_signature) if measure_memory else ()
    catalogs: dict[tuple[tuple[str, int, int], ...], SceneMemoryCatalog] = {}
    workloads: dict[int, SceneMemoryWorkload] = {}
    estimates: dict[int, SceneMemoryEstimate] = {}
    observations: dict[int, _SceneMemoryObservation] = {}
    live_reservations: dict[int, ActiveMemoryReservation] = {}
    memory_lock = threading.Lock()
    completed_count = 0
    admission: dict[int, dict[str, int | bool | str]] = {}

    def prepare_batch(index: int, check: Callable[[], None]) -> PreparedSceneBatch:
        bounds = batches[index]
        check()
        directory = output / "scenes" / f"batch_{index + 1:04d}"
        directory.mkdir(parents=True, exist_ok=False)
        progress(0.15 + 0.85 * completed_count / len(batches), f"Preparing scene batch {index + 1}/{len(batches)}")
        request = {**canonical, "bounds": bounds, "selection_bounds": canonical["bounds"]}
        stage_started = time.perf_counter()
        # Resource writes trigger a fresh preflight pass above before it can be reused.
        same_selection = _can_reuse_preflight_plan(canonical, batches)
        plan = (copy_preflight_plan(preflight, directory / "scene_plan.json") if same_selection else
                prepare_scene_export_plan(runtime_path, request, directory / "scene_plan.json", check))
        timings.append({"stage": "plan_reuse" if same_selection else "batch_plan", "batch": index + 1,
                        "seconds": time.perf_counter() - stage_started})
        stage_started = time.perf_counter()
        detail_audit = prepare_scene_terrain_details(
            runtime_path, plan, check,
            lambda value, message: progress(0.15 + 0.85 * completed_count / len(batches), message))
        timings.append({"stage": "terrain_detail", "batch": index + 1, "seconds": time.perf_counter() - stage_started})
        (directory / "terrain_detail_audit.json").write_text(json.dumps(detail_audit, indent=2), encoding="utf-8")
        check()
        workload: SceneMemoryWorkload | None = None
        estimate: SceneMemoryEstimate | None = None
        if measure_memory:
            stage_started = time.perf_counter()
            catalog_key = scene_memory_catalog_key(plan)
            if catalog_key not in catalogs:
                if len(catalogs) >= 4:
                    catalogs.pop(next(iter(catalogs)))
                catalogs[catalog_key] = cached_scene_memory_catalog(cache.resolve(), catalog_key, tool_signature)
            workload = scene_memory_workload(plan, catalogs[catalog_key])
            with memory_lock:
                estimate = scene_memory_estimate(workload, calibrations, tool_signature)
                workloads[index] = workload
                estimates[index] = estimate
            timings.append({"stage": "memory_workload", "batch": index + 1,
                            "seconds": time.perf_counter() - stage_started})
        if index == len(batches) - 1:
            # No further preparation needs these snapshots while Blender finishes.
            io_session.invalidate()
        return PreparedSceneBatch(index, plan, directory, workload, estimate)

    def can_overlap(item: PreparedSceneBatch, active: tuple[PreparedSceneBatch, ...]) -> bool:
        if item.memory_workload is None:
            raise ValueError(f"Concurrent scene admission requires a measured-platform workload: batch={item.index + 1}")
        with memory_lock:
            estimate = scene_memory_estimate(item.memory_workload, calibrations, tool_signature)
            estimates[item.index] = estimate
            reservations = tuple(live_reservations[row.index] for row in active if row.index in live_reservations)
            ready = len(reservations) == len(active) and all(row.budget_bytes > 0 for row in reservations)
        if not estimate.calibrated or not ready:
            admission[item.index] = {"allowed": False, "reason": "unobserved_workload" if not estimate.calibrated else "active_worker_unmeasured"}
            return False
        decision = memory_admission(estimate.budget_bytes, reservations, system_memory_snapshot(), reserve_bytes)
        admission[item.index] = {**asdict(decision), "estimated_candidate_bytes": estimate.budget_bytes,
                                 "reason": "live_physical_and_commit_growth_reservation"}
        return decision.allowed

    def execute_batch(item: PreparedSceneBatch, check: Callable[[], None]) -> tuple[SceneArtifact, SceneStageTiming]:
        index, plan, directory = item.index, item.plan, item.directory
        blend = directory / (str(canonical["map_id"]) + ".blend")
        audit = directory / "scene_audit.json"
        command = [str(source["blender_exe"]), "--background", "--factory-startup", "--python-exit-code", "1",
                   "--python", str(builder), "--", "--plan", str(plan), "--output", str(blend), "--audit", str(audit)]
        stage_started = time.perf_counter()
        if item.memory_workload is not None:
            with memory_lock:
                estimate = scene_memory_estimate(item.memory_workload, calibrations, tool_signature)
                estimates[index] = estimate
                live_reservations[index] = ActiveMemoryReservation(estimate.budget_bytes, ProcessMemorySnapshot(0, 0, 0, 0))

            def observe(process: subprocess.Popen[bytes]) -> None:
                current = process_memory_snapshot(int(process._handle))
                system = system_memory_snapshot()
                with memory_lock:
                    previous = observations.get(index)
                    observations[index] = _SceneMemoryObservation(
                        current, max(current.peak_working_set_bytes, previous.peak_working_set_bytes if previous else 0),
                        max(current.peak_commit_bytes, current.private_commit_bytes, previous.peak_commit_bytes if previous else 0),
                        min(system.available_physical_bytes, previous.minimum_available_physical_bytes if previous else system.available_physical_bytes),
                        min(system.available_commit_bytes, previous.minimum_available_commit_bytes if previous else system.available_commit_bytes),
                        previous.samples + 1 if previous else 1)
                    live_reservations[index] = ActiveMemoryReservation(estimate.budget_bytes, current)

            _run_observed_scene_process(command, directory / "blender.log", environment, check, observe)
        else:
            run_scene_process(command, directory / "blender.log", environment, check)
        timing: SceneStageTiming = {"stage": "blender_build_verify", "batch": index + 1,
                                    "seconds": time.perf_counter() - stage_started,
                                    "started_seconds": stage_started - started}
        if item.memory_workload is not None:
            with memory_lock:
                observation = observations[index]
            timing["memory"] = {"peak_working_set_bytes": observation.peak_working_set_bytes,
                                "peak_commit_bytes": observation.peak_commit_bytes,
                                "minimum_available_physical_bytes": observation.minimum_available_physical_bytes,
                                "minimum_available_commit_bytes": observation.minimum_available_commit_bytes,
                                "samples": observation.samples}
        report = require_object(json.loads(audit.read_text(encoding="utf-8")), "scene audit")
        if report.get("reopen_verified") is not True or not blend.is_file():
            raise ValueError(f"Blender did not produce a verified scene: {audit}")
        with blend.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        artifact: SceneArtifact = {"path": str(blend.relative_to(output)), "audit": str(audit.relative_to(output)),
                        "plan": str(plan.relative_to(output)), "bytes": blend.stat().st_size, "sha256": digest,
                        "instances": int(cast(int, report["instances"])), "pending": int(cast(int, report["pending"]))}
        return artifact, timing

    def completed(index: int, result: tuple[SceneArtifact, SceneStageTiming]) -> None:
        nonlocal completed_count, calibrations
        if measure_memory:
            with memory_lock:
                observation = observations[index]
                calibration = SceneMemoryCalibration(tool_signature, workloads[index], observation.peak_working_set_bytes,
                                                      observation.peak_commit_bytes, True)
                calibrations = merge_scene_memory_calibrations((*calibrations, calibration))
                live_reservations.pop(index)
            write_scene_memory_calibrations(calibration_path, calibrations)
        completed_count += 1
        timings.append(result[1])
        progress(0.15 + 0.85 * completed_count / len(batches), f"Verified scene batch {index + 1}/{len(batches)}")

    built = run_scene_pipeline(len(batches), capacity, prepare_batch, execute_batch, can_overlap, check_cancelled, completed)
    (output / "scene_performance.json").write_text(json.dumps({
        "format": "EndfieldScenePerformance/1", "elapsed_seconds": time.perf_counter() - started,
        "stages": timings,
        "shared_io": io_session.statistics(),
        "scheduling": {"workers": capacity, "requested_limit": int(requested), "initial_available_bytes": initial_available,
                       "system_reserve_bytes": reserve_bytes, "admission": admission,
                       "memory_policy": "verified process peak feature envelope; 25% or 256 MiB margin; unobserved workloads run solo; live physical and commit growth reserved; not a hard memory limit",
                       "calibration_path": str(calibration_path) if measure_memory else None,
                       "tool_signature": tool_signature,
                       "memory_estimates": {index + 1: asdict(estimate) for index, estimate in estimates.items()},
                       "memory_workloads": {index + 1: asdict(workload) for index, workload in workloads.items()},
                       "preparation_overlaps_build": True},
    }, indent=2), encoding="utf-8")
    return [artifact for artifact, _ in built]
