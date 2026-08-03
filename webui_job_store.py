"""Small durable job/event store for WebUI adapters.

The store is intentionally execution-agnostic. A coordinator owns Blender
processes and uses this module to persist state, logs, and cooperative cancel
requests under the external log root.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from webui_export_contract_v2 import make_event, new_job_record, validate_transition


JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class JobStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _job_dir(self, job_id: str) -> Path:
        job_id = str(job_id)
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("Invalid job_id")
        return self.root / job_id

    def _job_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _events_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "events.jsonl"

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent), text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            Path(temporary).replace(path)
        finally:
            temporary_path = Path(temporary)
            if temporary_path.exists():
                temporary_path.unlink()

    def _read(self, job_id: str) -> dict[str, Any]:
        path = self._job_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def _event(self, job: Mapping[str, Any], message: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        events_path = self._events_path(str(job["job_id"]))
        sequence = 1
        if events_path.is_file():
            sequence += sum(1 for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip())
        event = make_event(str(job["job_id"]), sequence, str(job["state"]), str(job["phase"]), float(job["progress"]), message, payload)
        with events_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def create(self, canonical: Mapping[str, Any]) -> dict[str, Any]:
        job = new_job_record(canonical)
        job_dir = self._job_dir(str(job["job_id"]))
        if job_dir.exists():
            raise FileExistsError(job_dir)
        job_dir.mkdir(parents=True)
        self._write_json(job_dir / "job.json", job)
        self._event(job, "Job queued")
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        return self._read(job_id)

    def events(self, job_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        path = self._events_path(job_id)
        if not path.is_file():
            return []
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if int(event.get("sequence", 0)) > int(after_sequence):
                result.append(event)
        return result

    def log(self, job_id: str, message: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        job = self._read(job_id)
        return self._event(job, message, payload)

    def update_progress(
        self,
        job_id: str,
        *,
        phase: str,
        progress: float,
        message: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        job = self._read(job_id)
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        if phase not in {"queued", "preparing", "extracting", "building", "auditing"}:
            raise ValueError("Unknown phase")
        next_progress = float(progress)
        if not 0.0 <= next_progress <= 1.0:
            raise ValueError("progress must be within [0, 1]")
        job["phase"] = phase
        job["progress"] = max(float(job.get("progress", 0.0)), next_progress)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_json(self._job_path(job_id), job)
        self._event(job, message, payload)
        return job

    def transition(self, job_id: str, next_state: str, phase: str | None = None, progress: float | None = None, message: str = "", payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        job = self._read(job_id)
        validate_transition(str(job["state"]), next_state)
        if phase is not None and phase not in {"queued", "preparing", "extracting", "building", "auditing", "completed", "failed", "cancelled"}:
            raise ValueError("Unknown phase")
        if progress is not None and not 0.0 <= float(progress) <= 1.0:
            raise ValueError("progress must be within [0, 1]")
        job["state"] = next_state
        job["phase"] = phase or next_state
        job["progress"] = float(progress if progress is not None else (1.0 if next_state == "completed" else job["progress"]))
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_json(self._job_path(job_id), job)
        self._event(job, message or f"Job {next_state}", payload)
        return job

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        job = self._read(job_id)
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        marker = self._job_dir(job_id) / "cancel.request"
        marker.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="ascii")
        job["cancel_requested"] = True
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_json(self._job_path(job_id), job)
        self._event(job, "Cooperative cancellation requested")
        return job

    def cancel_requested(self, job_id: str) -> bool:
        return (self._job_dir(job_id) / "cancel.request").is_file()

    def complete(self, job_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
        job = self._read(job_id)
        validate_transition(str(job["state"]), "completed")
        job["state"] = "completed"
        job["phase"] = "completed"
        job["progress"] = 1.0
        job["result"] = dict(result)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_json(self._job_path(job_id), job)
        self._event(job, "Export completed", {"result": dict(result)})
        return job

    def fail(self, job_id: str, error: Mapping[str, Any]) -> dict[str, Any]:
        job = self._read(job_id)
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        validate_transition(str(job["state"]), "failed")
        job["state"] = "failed"
        job["phase"] = "failed"
        job["error"] = dict(error)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_json(self._job_path(job_id), job)
        self._event(job, "Export failed", {"error": dict(error)})
        return job
