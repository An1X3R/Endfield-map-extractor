"""Durable character preparation and export jobs independent of map readiness."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import traceback
from typing import Protocol, cast
from uuid import uuid4

from webui_character_contract import (
    CharacterJobKind, CharacterJobNotFound, CharacterRequestConflict, CharacterRequestError,
    json_object, normalize_character_request, public_request, request_digest, request_uuid,
)
from webui_character_results import build_character_result


TERMINAL = frozenset({"completed", "partial", "failed", "cancelled"})
PHASES = frozenset({"preparing", "extracting", "building", "auditing"})
Progress = Callable[[str, float, str], None]


class CharacterServiceProtocol(Protocol):
    """Boundary owned by the configured reader and resource service."""

    def catalog(self) -> dict[str, object]: ...
    def detail(self, selection_id: str) -> dict[str, object]: ...
    def animations(self, selection_id: str, variant_id: str, offset: int, limit: int) -> dict[str, object]: ...
    def canonical_export(self, payload: dict[str, object]) -> dict[str, object]: ...
    def canonical_preparation(self, payload: dict[str, object]) -> dict[str, object]: ...
    def prepare(self, canonical: dict[str, object], output_dir: Path,
                check_cancelled: Callable[[], None], progress: Progress) -> dict[str, object]: ...
    def export(self, canonical: dict[str, object], output_dir: Path,
               check_cancelled: Callable[[], None], progress: Progress) -> dict[str, object]: ...


class CharacterJobCancelled(RuntimeError):
    """The owned character job was cancelled cooperatively."""


def _write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _public_job(record: dict[str, object]) -> dict[str, object]:
    job = json_object(record["job"], "job")
    return {**{field: job[field] for field in ("format", "job_id", "kind", "state", "phase", "progress",
                                               "message", "cancel_requested", "result", "error")},
            "request": public_request(json_object(record["request"], "request"))}


class CharacterCoordinator:
    """Persist private execution inputs separately from the public job DTO."""

    def __init__(self, service: CharacterServiceProtocol, jobs_root: Path) -> None:
        self.service = service
        self.root = jobs_root.expanduser().resolve()
        self._lock = threading.RLock()
        self._capacity = threading.BoundedSemaphore(2)
        self._session = str(uuid4())
        self._threads: dict[str, threading.Thread] = {}
        self._closing = False

    def catalog(self) -> dict[str, object]:
        return self.service.catalog()

    def detail(self, selection_id: str) -> dict[str, object]:
        return self.service.detail(selection_id)

    def animations(self, selection_id: str, variant_id: str, offset: int, limit: int) -> dict[str, object]:
        return self.service.animations(selection_id, variant_id, offset, limit)

    def shutdown(self) -> None:
        """Cancel owned tasks and join outside the record lock within ten seconds."""
        with self._lock:
            self._closing = True
            threads = tuple(self._threads.items())
            for job_id, _ in threads:
                self.cancel(job_id)
        deadline = time.monotonic() + 10.0
        for _, thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        alive = [job_id for job_id, thread in threads if thread.is_alive()]
        if alive:
            raise RuntimeError(f"Character workers did not stop after cancellation: diagnostic_ids={alive}")

    def _path(self, job_id: str) -> Path:
        path = (self.root / "records" / (request_uuid(job_id) + ".json")).resolve()
        if not path.is_relative_to(self.root):
            raise CharacterRequestError("Character job storage must remain inside its configured root")
        return path

    def _save(self, record: dict[str, object]) -> None:
        job = json_object(record["job"], "job")
        events = cast(list[dict[str, object]], record["events"])
        event = {"sequence": len(events) + 1, "timestamp": datetime.now(timezone.utc).isoformat(),
                 **{field: job[field] for field in ("state", "phase", "progress", "message", "cancel_requested")}}
        _write(self._path(str(job["job_id"])), {**record, "session": self._session, "events": [*events, event]})

    def _load(self, job_id: str) -> dict[str, object]:
        path = self._path(job_id)
        if not path.is_file():
            raise CharacterJobNotFound("Character job does not exist")
        record = json_object(json.loads(path.read_text(encoding="utf-8")), "record")
        job = json_object(record.get("job"), "job")
        request = json_object(record.get("request"), "request")
        events = record.get("events")
        if (job.get("job_id") != job_id or request.get("request_id") != job_id
                or record.get("request_digest") != request_digest(request)
                or record.get("kind") not in {"preparation", "export"}
                or job.get("state") not in TERMINAL | {"queued", "running"}
                or not isinstance(events, list)):
            raise CharacterRequestError("Stored character job has invalid identity or state")
        for index, value in enumerate(events):
            if json_object(value, "event").get("sequence") != index + 1:
                raise CharacterRequestError("Stored character event sequence is invalid")
        self._check_boundary(str(record["kind"]), json_object(record.get("canonical"), "canonical"))
        if record.get("session") != self._session and job["state"] not in TERMINAL:
            record = {**record, "job": {**job, "state": "failed", "phase": "failed",
                       "message": "后端曾中断，已保留部分文件；请新建任务重试。",
                       "error": {"code": "worker_interrupted", "message": "原任务进程已结束，请新建任务重试。",
                                 "stage": job["phase"], "retryable": True, "diagnostic_id": job_id}}}
            self._save(record)
            return self._load(job_id)
        return record

    def get(self, job_id: str) -> dict[str, object]:
        with self._lock:
            return _public_job(self._load(job_id))

    def events(self, job_id: str, after: int, limit: int) -> dict[str, object]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise CharacterRequestError("Event cursor requires after >= 0 and limit within 1..500")
        with self._lock:
            record = self._load(job_id)
            events = cast(list[dict[str, object]], record["events"])
            if after > len(events):
                raise CharacterRequestError("Event cursor exceeds the stored sequence; reconnect from zero")
            page = events[after:after + limit]
            return {"format": "CharacterJobEvents/1", "job_id": job_id, "events": page,
                    "next_after": after + len(page), "has_more": after + len(page) < len(events)}

    def _canonical(self, kind: CharacterJobKind, request: dict[str, object]) -> dict[str, object]:
        canonical = (self.service.canonical_preparation(deepcopy(request)) if kind == "preparation"
                     else self.service.canonical_export(deepcopy(request)))
        result = json_object(canonical, "canonical_request")
        if result.get("request_id") != request["request_id"] or result.get("format") != request["format"]:
            raise CharacterRequestError("Character service changed the request identity")
        self._check_boundary(kind, result)
        return result

    def _check_boundary(self, kind: str, canonical: dict[str, object]) -> None:
        source = canonical.get("source") if kind == "preparation" else canonical.get("context")
        game_value = json_object(source, "character source").get("game_root")
        if not isinstance(game_value, str) or not Path(game_value).is_absolute():
            raise CharacterRequestError("Character service must provide an absolute game source boundary")
        game = Path(game_value).resolve()
        root = self.root.resolve()
        if root.is_relative_to(game) or game.is_relative_to(root):
            raise CharacterRequestError("Character job storage must not overlap the game installation")
        output_value = json_object(source, "character source").get("output_root")
        if not isinstance(output_value, str) or not Path(output_value).is_absolute():
            raise CharacterRequestError("Character service must provide the selected absolute output root")
        output = Path(output_value).resolve()
        if output.is_relative_to(game) or game.is_relative_to(output) or output.is_file():
            raise CharacterRequestError("Character output must not overlap the game installation")

    def _output_path(self, job_id: str, record: dict[str, object]) -> Path:
        canonical = json_object(record["canonical"], "canonical")
        kind = str(record["kind"])
        self._check_boundary(kind, canonical)
        source = json_object(canonical["source"] if kind == "preparation" else canonical["context"], "character source")
        selected_root = Path(str(source["output_root"])).resolve()
        output = (selected_root / "character_jobs" / "artifacts" / request_uuid(job_id)).resolve()
        if not output.is_relative_to(selected_root):
            raise CharacterRequestError("Character artifacts must remain inside the selected output root")
        return output

    def validate(self, payload: object) -> dict[str, object]:
        kind, request = normalize_character_request(payload)
        self._canonical(kind, request)
        return {"format": "CharacterRequestValidation/1", "status": "valid", "kind": kind,
                "canonical_request": public_request(request), "issues": []}

    def submit(self, payload: object) -> dict[str, object]:
        kind, request = normalize_character_request(payload)
        job_id = str(request["request_id"])
        digest = request_digest(request)
        with self._lock:
            if self._path(job_id).exists():
                record = self._load(job_id)
                if record["request_digest"] != digest:
                    raise CharacterRequestConflict("request_id already belongs to different parameters; create a new UUID")
                return _public_job(record)
            if self._closing:
                raise CharacterRequestError("Character worker is closing; reconnect after restarting the launcher")
            canonical = self._canonical(kind, request)
            job: dict[str, object] = {"format": "CharacterJob/1", "job_id": job_id, "kind": kind,
                "state": "queued", "phase": "queued", "progress": 0.0, "message": "角色任务已排队",
                "cancel_requested": False, "result": None, "error": None}
            record = {"kind": kind, "request": request, "request_digest": digest,
                      "canonical": canonical, "job": job, "events": []}
            self._save(record)
            thread = threading.Thread(target=self._run, args=(job_id,), daemon=True,
                                      name="endfield-character-" + job_id)
            self._threads[job_id] = thread
            try:
                thread.start()
            except RuntimeError as error:
                self._failure(job_id, error)
                raise CharacterRequestError("Unable to start character worker; inspect its diagnostic ID") from error
            return _public_job(self._load(job_id))

    def _update(self, job_id: str, changes: Mapping[str, object]) -> None:
        with self._lock:
            record = self._load(job_id)
            job = json_object(record["job"], "job")
            if job["state"] in TERMINAL:
                return
            self._save({**record, "job": {**job, **changes}})

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self.get(job_id)
            terminal = job["state"] in TERMINAL
            if not terminal:
                changes: dict[str, object] = {"cancel_requested": True, "message": "已请求取消，正在清理角色工作进程"}
                if job["state"] == "queued":
                    changes.update(state="cancelled", phase="cancelled", message="角色任务已取消")
                self._update(job_id, changes)
            return {"format": "CharacterJobCancelAck/1", "job_id": job_id, "accepted": True,
                    "state": "already_terminal" if terminal else "accepted"}

    def _check(self, job_id: str) -> None:
        job = self.get(job_id)
        if job["cancel_requested"] or job["state"] in {"cancelled", "failed"}:
            raise CharacterJobCancelled("Character job cancellation requested")

    def _progress(self, job_id: str, stage: str, fraction: float, message: str) -> None:
        if not isinstance(stage, str) or type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError("Character progress requires a stage and finite fraction within [0, 1]")
        with self._lock:
            self._check(job_id)
            current = self.get(job_id)
            phase = stage if stage in PHASES else "preparing"
            labels = {"preparing": "正在准备角色资源", "extracting": "正在读取角色资源",
                      "building": "正在构建角色文件", "auditing": "正在核对角色交付文件"}
            self._update(job_id, {"state": "running", "phase": phase,
                                 "progress": max(float(cast(float, current["progress"])), min(0.99, fraction)),
                                 "message": labels[phase]})

    def _failure(self, job_id: str, error: Exception) -> None:
        with self._lock:
            record = self._load(job_id)
            job = json_object(record["job"], "job")
            if job["state"] in TERMINAL:
                return
            safe = getattr(error, "safe_message", None)
            message = safe if isinstance(safe, str) and safe.strip() else "角色任务失败；请使用诊断编号查看本地任务日志后重试。"
            public = {"code": type(error).__name__, "message": message, "stage": job["phase"],
                      "retryable": True, "diagnostic_id": job_id}
            self._save({**record, "diagnostic": {"type": type(error).__name__, "message": str(error),
                                                "traceback": traceback.format_exc()},
                        "job": {**job, "state": "failed", "phase": "failed", "message": message, "error": public}})

    def _complete(self, job_id: str, output: Path, result: dict[str, object]) -> None:
        with self._lock:
            self._check(job_id)
            current = self._load(job_id)
            if output.resolve() != self._output_path(job_id, current):
                raise CharacterRequestError("Character completion output differs from the selected folder")
            job = json_object(current["job"], "job")
            marker = output / "INCOMPLETE"
            _write(output / "character_result.json", result)
            marker.unlink()
            try:
                self._save({**current, "job": {**job, "state": result["status"], "phase": result["status"],
                            "progress": 1.0, "message": "角色任务完成", "result": result}})
            except OSError:
                marker.write_text(job_id, encoding="ascii")
                raise

    def _run(self, job_id: str) -> None:
        acquired = False
        try:
            while not acquired:
                self._check(job_id)
                acquired = self._capacity.acquire(timeout=0.1)
            self._check(job_id)
            with self._lock:
                record = self._load(job_id)
                kind = str(record["kind"])
                canonical = json_object(record["canonical"], "canonical")
            output = self._output_path(job_id, record)
            output.mkdir(parents=True, exist_ok=False)
            marker = output / "INCOMPLETE"
            marker.write_text(job_id, encoding="ascii")
            self._progress(job_id, "preparing", 0.01, "Prepare character resources")
            execute = self.service.prepare if kind == "preparation" else self.service.export
            manifest = execute(canonical, output, lambda: self._check(job_id),
                               lambda stage, fraction, message: self._progress(job_id, stage, fraction, message))
            self._progress(job_id, "auditing", 0.99, "Verify delivery")
            result = build_character_result(output, json_object(manifest, "manifest"), kind)
            self._complete(job_id, output, result)
        except CharacterJobCancelled:
            self._update(job_id, {"state": "cancelled", "phase": "cancelled", "message": "角色任务已取消，部分文件保留"})
        except Exception as error:
            self._failure(job_id, error)
        finally:
            if acquired:
                self._capacity.release()
            with self._lock:
                self._threads.pop(job_id, None)
