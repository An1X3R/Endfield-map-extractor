"""Persistent single-worker coordination for asynchronous asset exports."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
import json
import math
from pathlib import Path
import threading
import traceback
from typing import Protocol, cast
from uuid import uuid4

from webui_asset_contract import ASSET_KINDS, parse_asset_export_request
from webui_character_contract import (
    CharacterJobNotFound,
    CharacterRequestConflict,
    CharacterRequestError,
    request_digest,
    request_uuid,
)
from webui_character_worker import _write


TERMINAL_STATES = frozenset({"completed", "partial", "failed", "cancelled"})
JOB_STATES = TERMINAL_STATES | {"queued", "running"}
SHUTDOWN_TIMEOUT_SECONDS = 10.0
Progress = Callable[[str, float, str], None]


class AssetServiceProtocol(Protocol):
    """Boundary to the configured catalog, source readers and exporters."""

    def catalog(self, kind: str) -> dict[str, object]: ...
    def validate(self, payload: object) -> dict[str, object]: ...
    def export(
        self,
        payload: object,
        output: Path,
        check_cancelled: Callable[[], None],
        progress: Progress,
    ) -> dict[str, object]: ...
    def output_root(self) -> Path: ...
    def game_root(self) -> Path: ...


class AssetJobCancelled(RuntimeError):
    """The asset job received a cooperative cancellation request."""


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CharacterRequestError(f"Stored asset {field} must be an object")
    return cast(dict[str, object], value)


def _absolute_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CharacterRequestError(f"Stored asset {field} must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise CharacterRequestError(f"Stored asset {field} must be an absolute path")
    return path.resolve()


def _parse_request(payload: object) -> dict[str, object]:
    try:
        parsed = parse_asset_export_request(payload)
    except ValueError as error:
        raise CharacterRequestError(str(error)) from error
    return cast(dict[str, object], parsed.as_json())


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _public_job(record: dict[str, object]) -> dict[str, object]:
    job = _object(record.get("job"), "job")
    request = _object(record.get("request"), "request")
    return {
        "format": job["format"],
        "job_id": job["job_id"],
        "kind": job["kind"],
        "state": job["state"],
        "phase": job["phase"],
        "progress": job["progress"],
        "message": job["message"],
        "request": request,
        "result": job["result"],
        "error": job["error"],
    }


class AssetCoordinator:
    """Persist asset jobs and execute at most one export at a time."""

    def __init__(self, service: AssetServiceProtocol, jobs_root: Path) -> None:
        if not isinstance(jobs_root, Path):
            raise CharacterRequestError("Asset job root must be a filesystem path")
        self.service = service
        self.root = jobs_root.expanduser().resolve()
        self._lock = threading.RLock()
        self._session = str(uuid4())
        self._closing = False
        self._cancel_events: dict[str, threading.Event] = {}
        self._futures: dict[str, Future[None]] = {}
        self._recover_interrupted_jobs()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="endfield-asset")

    def _records_root(self) -> Path:
        records = (self.root / "records").resolve()
        if not records.is_relative_to(self.root):
            raise CharacterRequestError("Asset job records must remain inside the configured job root")
        return records

    def _game_root(self) -> Path:
        value = self.service.game_root()
        if not isinstance(value, Path) or not value.is_absolute():
            raise CharacterRequestError("Asset service must provide an absolute game source root")
        return value.expanduser().resolve()

    def _output_root(self) -> Path:
        value = self.service.output_root()
        if not isinstance(value, Path) or not value.is_absolute():
            raise CharacterRequestError("Asset service must provide an absolute output root")
        return value.expanduser().resolve()

    def _check_record_boundary(self, game_root: Path) -> None:
        if _paths_overlap(self._records_root(), game_root):
            raise CharacterRequestError("Asset job records must not overlap the game installation")

    def _submission_roots(self) -> tuple[Path, Path]:
        game_root = self._game_root()
        output_root = self._output_root()
        records_root = self._records_root()
        if not game_root.is_dir():
            raise CharacterRequestError("Configured game source root is not an existing directory")
        if not output_root.is_dir():
            raise CharacterRequestError("Configured asset output root is not an existing directory")
        if (
            _paths_overlap(output_root, game_root)
            or _paths_overlap(records_root, game_root)
            or _paths_overlap(output_root, records_root)
        ):
            raise CharacterRequestError("Asset source, output and job records must use disjoint directories")
        return game_root, output_root

    def _record_path(self, job_id: str) -> Path:
        canonical_id = request_uuid(job_id)
        records_root = self._records_root()
        path = (records_root / (canonical_id + ".json")).resolve()
        if not path.is_relative_to(records_root):
            raise CharacterRequestError("Asset job record path escaped its configured root")
        return path

    def _save(self, record: dict[str, object]) -> None:
        job = _object(record.get("job"), "job")
        _write(self._record_path(str(job["job_id"])), record)

    def _load(self, job_id: str) -> dict[str, object]:
        path = self._record_path(job_id)
        if not path.is_file():
            raise CharacterJobNotFound("Asset job does not exist")
        record = _object(json.loads(path.read_text(encoding="utf-8")), "record")
        job = _object(record.get("job"), "job")
        stored_request = _object(record.get("request"), "request")
        request = _parse_request(stored_request)
        stored_game_root = _absolute_path(record.get("game_root"), "game_root")
        output_root = _absolute_path(record.get("output_root"), "output_root")
        self._check_record_boundary(stored_game_root)
        if _paths_overlap(output_root, stored_game_root):
            raise CharacterRequestError("Stored asset output overlaps the game installation")
        if _paths_overlap(output_root, self._records_root()):
            raise CharacterRequestError("Stored asset output overlaps the asset job records")
        progress = job.get("progress")
        if (
            record.get("format") != "AssetJobRecord/1"
            or job.get("format") != "AssetJob/1"
            or job.get("job_id") != job_id
            or job.get("kind") != request["kind"]
            or job.get("state") not in JOB_STATES
            or not isinstance(job.get("kind"), str)
            or not isinstance(job.get("phase"), str)
            or not isinstance(job.get("message"), str)
            or "result" not in job
            or type(progress) not in (int, float)
            or not math.isfinite(float(cast(float, progress)))
            or not 0.0 <= float(cast(float, progress)) <= 1.0
            or type(job.get("cancel_requested")) is not bool
            or "error" not in job
            or job.get("result") is not None and not isinstance(job.get("result"), dict)
            or job.get("error") is not None and not isinstance(job.get("error"), dict)
            or request != stored_request
            or record.get("request_digest") != request_digest(request)
        ):
            raise CharacterRequestError("Stored asset job has invalid identity, request or state")
        return record

    def _recover_interrupted_jobs(self) -> None:
        records_root = self._records_root()
        if not records_root.exists():
            return
        if not records_root.is_dir():
            raise CharacterRequestError("Asset job records path is not a directory")
        for path in sorted(records_root.glob("*.json")):
            job_id = request_uuid(path.stem)
            record = self._load(job_id)
            job = _object(record.get("job"), "job")
            if job["state"] in TERMINAL_STATES:
                continue
            message = "Asset worker was interrupted by a restart; submit a new request."
            updated_job = {
                **job,
                "state": "failed",
                "phase": "failed",
                "message": message,
                "error": {"message": message, "diagnostic_id": job_id},
            }
            diagnostic = {
                "type": "WorkerInterruptedError",
                "message": message,
                "traceback": "No traceback is available for a previous process.",
            }
            self._save({**record, "diagnostic": diagnostic, "job": updated_job})

    def catalog(self, kind: str) -> dict[str, object]:
        if kind not in ASSET_KINDS:
            raise CharacterRequestError(f"Unsupported asset catalog kind: {kind!r}")
        return self.service.catalog(kind)

    def _service_validation(self, request: dict[str, object]) -> dict[str, object]:
        response = self.service.validate(deepcopy(request))
        if not isinstance(response, dict) or any(not isinstance(key, str) for key in response):
            raise CharacterRequestError("Asset service validation must return an object")
        if response.get("status") != "valid":
            raise CharacterRequestError("Asset service did not validate the export request")
        return response

    def validate(self, payload: object) -> dict[str, object]:
        request = _parse_request(payload)
        self._submission_roots()
        return self._service_validation(request)

    def submit(self, payload: object) -> dict[str, object]:
        request = _parse_request(payload)
        job_id = request_uuid(request["request_id"])
        digest = request_digest(request)
        record_path = self._record_path(job_id)
        with self._lock:
            if record_path.exists():
                record = self._load(job_id)
                if record.get("request_digest") != digest:
                    raise CharacterRequestConflict("request_id already belongs to different asset parameters")
                return _public_job(record)
            if self._closing:
                raise CharacterRequestError("Asset worker is shutting down")

        game_root, output_root = self._submission_roots()
        self._service_validation(request)

        with self._lock:
            if record_path.exists():
                record = self._load(job_id)
                if record.get("request_digest") != digest:
                    raise CharacterRequestConflict("request_id already belongs to different asset parameters")
                return _public_job(record)
            if self._closing:
                raise CharacterRequestError("Asset worker is shutting down")
            if self._submission_roots() != (game_root, output_root):
                raise CharacterRequestError("Asset source or output configuration changed during submission")
            job: dict[str, object] = {
                "format": "AssetJob/1",
                "job_id": job_id,
                "kind": request["kind"],
                "state": "queued",
                "phase": "queued",
                "progress": 0.0,
                "message": "Asset export queued",
                "cancel_requested": False,
                "result": None,
                "error": None,
            }
            record: dict[str, object] = {
                "format": "AssetJobRecord/1",
                "session": self._session,
                "game_root": str(game_root),
                "output_root": str(output_root),
                "request": request,
                "request_digest": digest,
                "job": job,
            }
            self._save(record)
            self._cancel_events[job_id] = threading.Event()
            future = self._executor.submit(self._run, job_id)
            self._futures[job_id] = future
            future.add_done_callback(lambda completed: self._forget_future(job_id, completed))
            return _public_job(self._load(job_id))

    def _forget_future(self, job_id: str, completed: Future[None]) -> None:
        with self._lock:
            if self._futures.get(job_id) is completed:
                self._futures.pop(job_id)

    def _artifact_path(self, job_id: str, record: dict[str, object]) -> Path:
        output_root = _absolute_path(record.get("output_root"), "output_root")
        artifact = (output_root / "asset_jobs" / request_uuid(job_id)).resolve()
        if not artifact.is_relative_to(output_root):
            raise CharacterRequestError("Asset artifact path escaped the configured output root")
        if _paths_overlap(artifact, _absolute_path(record.get("game_root"), "game_root")):
            raise CharacterRequestError("Asset artifact path overlaps the game installation")
        return artifact

    def _update_locked(self, job_id: str, changes: Mapping[str, object]) -> dict[str, object]:
        record = self._load(job_id)
        job = _object(record.get("job"), "job")
        if job["state"] in TERMINAL_STATES:
            return record
        updated = {**record, "job": {**job, **changes}}
        self._save(updated)
        return updated

    def _check_cancelled(self, job_id: str) -> None:
        with self._lock:
            event = self._cancel_events.get(job_id)
            if event is not None:
                if event.is_set():
                    raise AssetJobCancelled("Asset job cancellation requested")
                return
            record = self._load(job_id)
            job = _object(record.get("job"), "job")
            if job["cancel_requested"] or job["state"] == "cancelled":
                raise AssetJobCancelled("Asset job cancellation requested")

    def _progress(self, job_id: str, phase: str, fraction: float, message: str) -> None:
        if not isinstance(phase, str) or not phase.strip():
            raise ValueError("Asset progress phase must be non-empty text")
        if type(fraction) not in (int, float) or not math.isfinite(float(fraction)) or not 0.0 <= fraction <= 1.0:
            raise ValueError("Asset progress fraction must be finite and within [0, 1]")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Asset progress message must be non-empty text")
        with self._lock:
            self._check_cancelled(job_id)
            record = self._load(job_id)
            job = _object(record.get("job"), "job")
            if job["state"] != "running":
                raise CharacterRequestError("Asset progress arrived outside a running job")
            current = float(cast(float, job["progress"]))
            updated_job = {
                **job,
                "phase": phase,
                "progress": max(current, min(0.99, float(fraction))),
                "message": message.strip(),
            }
            self._save({**record, "job": updated_job})

    def _cancelled(self, job_id: str) -> None:
        with self._lock:
            self._update_locked(job_id, {
                "state": "cancelled",
                "phase": "cancelled",
                "message": "Asset export cancelled; partial files were retained.",
                "cancel_requested": True,
            })

    def _failure(self, job_id: str, error: Exception) -> None:
        with self._lock:
            record = self._load(job_id)
            job = _object(record.get("job"), "job")
            if job["state"] in TERMINAL_STATES:
                return
            safe_message = getattr(error, "safe_message", None)
            message = (
                safe_message.strip()
                if isinstance(safe_message, str) and safe_message.strip()
                else "Asset export failed; use the diagnostic ID to inspect the local job record."
            )
            public_error = {"message": message, "diagnostic_id": job_id}
            diagnostic = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": traceback.format_exc(),
            }
            updated_job = {
                **job,
                "state": "failed",
                "phase": "failed",
                "message": message,
                "error": public_error,
            }
            self._save({**record, "diagnostic": diagnostic, "job": updated_job})

    def _complete(self, job_id: str, output: Path, result: dict[str, object]) -> None:
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise CharacterRequestError("Asset export result must contain only JSON-native values") from error
        with self._lock:
            self._check_cancelled(job_id)
            record = self._load(job_id)
            if output.resolve() != self._artifact_path(job_id, record):
                raise CharacterRequestError("Asset completion output differs from its configured artifact path")
            job = _object(record.get("job"), "job")
            status = result.get("status")
            if status not in {"completed", "partial"}:
                raise CharacterRequestError("Asset export result must have completed or partial status")
            self._save({**record, "job": {
                **job,
                "state": status,
                "phase": status,
                "progress": 1.0,
                "message": "Asset export completed" if status == "completed" else "Asset export completed with pending items",
                "result": result,
            }})

    def _run(self, job_id: str) -> None:
        try:
            with self._lock:
                record = self._load(job_id)
                job = _object(record.get("job"), "job")
                if job["state"] != "queued":
                    return
                self._check_cancelled(job_id)
                record = self._update_locked(job_id, {
                    "state": "running",
                    "phase": "exporting",
                    "progress": 0.01,
                    "message": "Asset export is starting",
                })
            game_root, output_root = self._submission_roots()
            if game_root != _absolute_path(record.get("game_root"), "game_root"):
                raise CharacterRequestError("Configured game source changed before asset export")
            if output_root != _absolute_path(record.get("output_root"), "output_root"):
                raise CharacterRequestError("Configured asset output changed before asset export")
            output = self._artifact_path(job_id, record)
            self._check_cancelled(job_id)
            request = _object(record.get("request"), "request")
            result = self.service.export(
                deepcopy(request),
                output,
                lambda: self._check_cancelled(job_id),
                lambda phase, fraction, message: self._progress(job_id, phase, fraction, message),
            )
            if not isinstance(result, dict) or any(not isinstance(key, str) for key in result):
                raise CharacterRequestError("Asset service export must return an object")
            self._check_cancelled(job_id)
            self._complete(job_id, output, result)
        except AssetJobCancelled:
            self._cancelled(job_id)
        except Exception as error:
            try:
                self._failure(job_id, error)
            except Exception as persistence_error:
                error.add_note(f"Asset failure record could not be saved: {persistence_error!r}")
            raise
        finally:
            with self._lock:
                self._cancel_events.pop(job_id, None)

    def get(self, job_id: str) -> dict[str, object]:
        canonical_id = request_uuid(job_id)
        with self._lock:
            return _public_job(self._load(canonical_id))

    def cancel(self, job_id: str) -> dict[str, object]:
        canonical_id = request_uuid(job_id)
        with self._lock:
            record = self._load(canonical_id)
            job = _object(record.get("job"), "job")
            terminal = job["state"] in TERMINAL_STATES
            if not terminal:
                event = self._cancel_events.get(canonical_id)
                if event is not None:
                    event.set()
                if job["state"] == "queued":
                    future = self._futures.get(canonical_id)
                    if future is not None:
                        future.cancel()
                    self._cancel_events.pop(canonical_id, None)
                    self._update_locked(canonical_id, {
                        "state": "cancelled",
                        "phase": "cancelled",
                        "message": "Asset export cancelled while queued",
                        "cancel_requested": True,
                    })
                else:
                    self._update_locked(canonical_id, {
                        "message": "Cancellation requested; waiting for the exporter to stop",
                        "cancel_requested": True,
                    })
            return {
                "format": "AssetJobCancelAck/1",
                "job_id": canonical_id,
                "accepted": True,
                "state": "already_terminal" if terminal else "accepted",
            }

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            futures = tuple(self._futures.items())
            for job_id, future in futures:
                record = self._load(job_id)
                job = _object(record.get("job"), "job")
                if job["state"] in TERMINAL_STATES:
                    continue
                event = self._cancel_events.get(job_id)
                if event is not None:
                    event.set()
                if job["state"] == "queued":
                    future.cancel()
                    self._cancel_events.pop(job_id, None)
                    self._update_locked(job_id, {
                        "state": "cancelled",
                        "phase": "cancelled",
                        "message": "Asset export cancelled during shutdown",
                        "cancel_requested": True,
                    })
                else:
                    self._update_locked(job_id, {
                        "message": "Shutdown requested cancellation; waiting for the exporter to stop",
                        "cancel_requested": True,
                    })
            pending_futures = tuple(future for _, future in futures if not future.cancelled())
            self._executor.shutdown(wait=False, cancel_futures=True)
        _, pending = wait(pending_futures, timeout=SHUTDOWN_TIMEOUT_SECONDS)
        if pending:
            job_ids = sorted(job_id for job_id, future in futures if future in pending)
            raise RuntimeError(f"Asset workers did not stop after cancellation: job_ids={job_ids}")
