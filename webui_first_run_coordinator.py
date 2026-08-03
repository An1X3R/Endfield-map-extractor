"""Asynchronous local coordinator for WebUI first-run data preparation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Mapping
import uuid

from webui_first_run_contract_v1 import (
    EVENTS_FORMAT,
    STATUS_FORMAT,
    FirstRunRequestError,
    canonicalize_first_run_request,
    validate_first_run_paths,
)


class FirstRunCoordinatorError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        http_status: int = 409,
        retryable: bool = True,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.details = dict(details or {})


class FirstRunCancelled(RuntimeError):
    pass


STATE_VALUES = {"idle", "queued", "running", "partial", "failed", "cancelled", "completed"}
PHASES = (
    "idle",
    "dependencies",
    "preflight",
    "helper_dependencies",
    "bootstrap_metadata",
    "resource_index",
    "bundle_scan_index",
    "scene_discovery",
    "scene_candidate_bundles",
    "asset_bootstrap",
    "map_layers",
    "audit",
    "complete",
)


def request_identity(request: Mapping[str, Any]) -> dict[str, str]:
    canonical = json.dumps(dict(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "run_name": str(request["run_name"]),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper(),
    }


def default_capabilities() -> dict[str, Any]:
    return {
        "map_dataset": "not_ready",
        "asset_resolution": {"map01": "not_ready", "map02": "not_ready"},
        "blender_build": "not_ready",
        "profile_scan": "not_ready",
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def next_available(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}.retry{index:03d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FirstRunCoordinatorError(f"No retry path is available for {path}")


def build_first_run_command(
    interpreter: Path,
    script: Path,
    request: Mapping[str, Any],
    *,
    cancel_marker: Path,
    resume: bool,
) -> list[str]:
    command = [
        str(interpreter),
        str(script),
        "--game-root",
        str(request["game_root"]),
        "--cache-root",
        str(request["cache_root"]),
        "--export-root",
        str(request["export_root"]),
        "--run-name",
        str(request["run_name"]),
        "--cancel-marker",
        str(cancel_marker),
        "--no-activate-runtime-config",
    ]
    if resume:
        command.append("--resume")
    return command


class FirstRunCoordinator:
    def __init__(
        self,
        *,
        extractor_root: Path,
        interpreter_provider: Callable[[], Path],
        requirements_path: Path | None = None,
        default_run_name: str = "bootstrap_v1",
        on_ready: Callable[[Path, Path], None] | None = None,
        initial_request: Mapping[str, Any] | None = None,
        initial_defaults: Mapping[str, Any] | None = None,
    ) -> None:
        self.extractor_root = extractor_root.resolve()
        self.interpreter_provider = interpreter_provider
        self.requirements_path = requirements_path.resolve() if requirements_path is not None else None
        self.default_run_name = default_run_name
        self.on_ready = on_ready
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._cancel_requested = False
        self._cancel_marker: Path | None = None
        self._request: dict[str, Any] | None = None
        self._defaults = {
            "game_root": None,
            "export_root": None,
            "cache_root": None,
            "run_name": self.default_run_name,
            **dict(initial_defaults or {}),
        }
        self._status: dict[str, Any] = {
            "format": STATUS_FORMAT,
            "state": "idle",
            "phase": "idle",
            "progress": 0.0,
            "progress_scope": "phase",
            "phase_index": 0,
            "phase_count": len(PHASES),
            "message": "First-run data preparation has not started",
            "ready": False,
            "cancel_requested": False,
            "resume_available": False,
            "defaults": dict(self._defaults),
            "capabilities": default_capabilities(),
        }
        if initial_request is not None:
            request = canonicalize_first_run_request(
                initial_request,
                default_run_name=self.default_run_name,
            )
            self._request = request
            self._defaults.update(request)
            self._restore_existing(request)

    def validate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = canonicalize_first_run_request(payload, default_run_name=self.default_run_name)
        return validate_first_run_paths(request)

    def submit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        validation = self.validate(payload)
        if validation["status"] != "valid":
            raise FirstRunRequestError(
                "first_run_paths_incomplete",
                "First-run paths are incomplete: " + ", ".join(validation["missing"]),
                details={"missing": validation["missing"], "validation": validation},
            )
        request = dict(validation["canonical"])
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise FirstRunCoordinatorError(
                    "already_running", "First-run data preparation is already running."
                )
            if self._status.get("ready") and self._request == request:
                return {**self._decorate_status(dict(self._status)), "disposition": "already_ready"}
            self._request = request
            self._defaults.update(request)
            self._cancel_requested = False
            self._cancel_marker = None
            self._status = {
                "format": STATUS_FORMAT,
                "job_id": request["run_name"],
                "state": "queued",
                "phase": "preflight",
                "progress": 0.0,
                "progress_scope": "phase",
                "phase_index": PHASES.index("preflight"),
                "phase_count": len(PHASES),
                "message": "First-run data preparation queued",
                "ready": False,
                "cancel_requested": False,
                "resume_available": self._paths(request)["state"].is_file(),
                "submitted_at": utc_now(),
                "request": request,
                "request_identity": request_identity(request),
                "defaults": dict(self._defaults),
                "capabilities": default_capabilities(),
            }
            thread = threading.Thread(
                target=self._run,
                name=f"endfield-first-run-{request['run_name']}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self._decorate_status(dict(self._status))

    def _paths(self, request: Mapping[str, Any]) -> dict[str, Path]:
        export_root = Path(str(request["export_root"]))
        run_name = str(request["run_name"])
        run_root = Path(str(request["cache_root"])) / run_name
        return {
            "run_root": run_root,
            "state": run_root / "first_run_state.json",
            "runtime": run_root / "endfield.runtime.local.json",
            "log_root": export_root / "log" / f"first_run_{run_name}",
            "stage_root": export_root / "cache" / f"map_layers_{run_name}",
        }

    @staticmethod
    def _runtime_stage(runtime_path: Path, fallback: Path) -> Path:
        if runtime_path.is_file():
            try:
                payload = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
                value = (payload.get("paths") or {}).get("stage119_root")
                if isinstance(value, str) and value.strip():
                    return Path(value).expanduser().resolve()
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        return fallback

    @staticmethod
    def _descriptor(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest().upper()}

    def _result_summary(
        self,
        paths: Mapping[str, Path],
        state_payload: Mapping[str, Any],
        source_status: str,
        *,
        exit_code: int | None = None,
    ) -> dict[str, Any]:
        runtime_path = paths["runtime"]
        actual_stage = self._runtime_stage(runtime_path, paths["stage_root"])
        capabilities = default_capabilities()
        runtime_payload: dict[str, Any] = {}
        if runtime_path.is_file():
            try:
                runtime_payload = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                runtime_payload = {}
        extraction_maps = ((runtime_payload.get("extraction") or {}).get("maps") or {})
        for map_id in ("map01", "map02"):
            map_payload = extraction_maps.get(map_id) or {}
            status = map_payload.get("assetClosureStatus")
            if isinstance(status, str):
                capabilities["asset_resolution"][map_id] = status
        capabilities["map_dataset"] = "ready" if actual_stage.is_dir() else "not_ready"
        blender_build = runtime_payload.get("blenderBuild") or {}
        if isinstance(blender_build.get("status"), str):
            capabilities["blender_build"] = blender_build["status"]
        profile_path_value = (runtime_payload.get("paths") or {}).get("blender_profile_registry")
        if isinstance(profile_path_value, str) and profile_path_value.strip():
            profile_path = Path(profile_path_value).expanduser().resolve()
            if profile_path.is_file():
                try:
                    profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
                    scan_status = (profile.get("scan") or {}).get("status")
                    if isinstance(scan_status, str):
                        capabilities["profile_scan"] = scan_status
                except (OSError, json.JSONDecodeError):
                    pass
        reports = sorted(
            paths["log_root"].glob("first_run_report*.json") if paths["log_root"].is_dir() else [],
            key=lambda item: item.stat().st_mtime_ns,
        )
        final_report = reports[-1] if reports else None
        deferred_capabilities: list[Any] = []
        if final_report is not None:
            try:
                report_payload = json.loads(final_report.read_text(encoding="utf-8-sig"))
                value = report_payload.get("deferredCapabilities") or []
                if isinstance(value, list):
                    deferred_capabilities = value
            except (OSError, json.JSONDecodeError):
                pass
        result: dict[str, Any] = {
            "source_status": source_status,
            "runtime_config": str(runtime_path) if runtime_path.is_file() else None,
            "stage_root": str(actual_stage) if actual_stage.is_dir() else None,
            "state_path": str(paths["state"]) if paths["state"].is_file() else None,
            "log_root": str(paths["log_root"]),
            "final_report": self._descriptor(final_report) if final_report is not None else None,
            "retryable": source_status != "completed",
            "partial_outputs_preserved": paths["state"].is_file(),
            "deferred_capabilities": deferred_capabilities,
            "capabilities": capabilities,
        }
        if exit_code is not None:
            result["exit_code"] = exit_code
        state_error = state_payload.get("error")
        if state_error:
            result["error"] = {
                "code": "first_run_failed",
                "message": str(state_error),
                "retryable": True,
            }
        return result

    def _decorate_status(self, status: dict[str, Any]) -> dict[str, Any]:
        state = str(status.get("state") or "idle")
        if state not in STATE_VALUES:
            state = "failed"
            status["state"] = state
        ready = bool(status.get("ready"))
        running = state in {"queued", "running"}
        resume_available = bool(status.get("resume_available")) and not ready
        cancel_requested = bool(status.get("cancel_requested"))
        status.update(
            format=STATUS_FORMAT,
            ready=ready,
            progress_scope="phase",
            phase_count=len(PHASES),
            phase_index=status.get("phase_index", self._phase_index(str(status.get("phase") or state))),
            resume_available=resume_available,
            cancel_requested=cancel_requested,
            defaults=dict(self._defaults),
            actions={
                "can_validate": True,
                "can_start": not running and not ready,
                "can_resume": not running and resume_available,
                "can_cancel": running and not cancel_requested,
            },
        )
        return status

    @staticmethod
    def _phase_index(phase: str) -> int | None:
        try:
            return PHASES.index(phase)
        except ValueError:
            return None

    def _restore_existing(self, request: Mapping[str, Any]) -> None:
        paths = self._paths(request)
        if not paths["state"].is_file():
            return
        state_payload = json.loads(paths["state"].read_text(encoding="utf-8"))
        source_status = str(state_payload.get("status") or "unknown")
        actual_stage = self._runtime_stage(paths["runtime"], paths["stage_root"])
        ready = source_status == "completed" and paths["runtime"].is_file() and actual_stage.is_dir()
        restored_state = (
            "completed"
            if ready
            else "partial"
            if source_status in {"completed_with_deferred_helpers", "running"}
            else source_status
            if source_status in {"failed", "cancelled"}
            else "idle"
        )
        result = self._result_summary(paths, state_payload, source_status)
        self._status = self._decorate_status({
            "format": STATUS_FORMAT,
            "job_id": request["run_name"],
            "state": restored_state,
            "phase": restored_state,
            "progress": 1.0 if restored_state in {"completed", "partial", "failed", "cancelled"} else 0.0,
            "message": (
                "Required first-run data is ready"
                if ready
                else "Existing first-run data can be resumed"
            ),
            "ready": ready,
            "restored": True,
            "request": dict(request),
            "request_identity": request_identity(request),
            "resume_available": not ready,
            "cancel_requested": False,
            "capabilities": result["capabilities"],
            "result": result,
        })
        if ready and self.on_ready is not None:
            self.on_ready(paths["runtime"], actual_stage)

    def _set_status(self, **changes: Any) -> None:
        with self._lock:
            self._status = self._decorate_status(
                {**self._status, **changes, "format": STATUS_FORMAT, "updated_at": utc_now()}
            )

    def _spawn_process(self, command: list[str], console: Any) -> subprocess.Popen[str]:
        kwargs: dict[str, Any] = {
            "cwd": str(self.extractor_root),
            "stdout": console,
            "stderr": subprocess.STDOUT,
            "text": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        with self._lock:
            self._process = process
        return process

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)

    def _wait_process(
        self,
        process: subprocess.Popen[str],
        *,
        cancel_marker: Path | None = None,
    ) -> int:
        cancellation_started: float | None = None
        while process.poll() is None:
            if self._cancel_requested:
                if cancel_marker is not None and not cancel_marker.exists():
                    cancel_marker.parent.mkdir(parents=True, exist_ok=True)
                    cancel_marker.write_text(utc_now() + "\n", encoding="ascii")
                if cancellation_started is None:
                    cancellation_started = time.monotonic()
                if cancel_marker is None or time.monotonic() - cancellation_started >= 1.0:
                    self._terminate_process_tree(process)
                    raise FirstRunCancelled("First-run data preparation was cancelled")
            time.sleep(0.1)
        if self._cancel_requested:
            raise FirstRunCancelled("First-run data preparation was cancelled")
        return int(process.returncode or 0)

    def _run(self) -> None:
        request = dict(self._request or {})
        paths = self._paths(request)
        export_root = Path(str(request["export_root"]))
        console_path = next_available(export_root / "log" / f"webui_first_run_{request['run_name']}.console.log")
        console_path.parent.mkdir(parents=True, exist_ok=True)
        cancel_marker = paths["log_root"] / f"CANCEL_REQUESTED.webui.{uuid.uuid4().hex[:12]}"
        self._cancel_marker = cancel_marker
        resume = paths["state"].is_file()
        self._set_status(
            state="running",
            phase="dependencies",
            progress=0.0,
            message="Preparing the isolated Python runtime",
            started_at=utc_now(),
            console_log=str(console_path),
            resume=resume,
            resume_available=resume,
            cancel_requested=self._cancel_requested,
        )
        try:
            interpreter = self.interpreter_provider().resolve()
            if self._cancel_requested:
                raise FirstRunCancelled("First-run data preparation was cancelled")
            command = build_first_run_command(
                interpreter,
                self.extractor_root / "endfield_first_run.py",
                request,
                cancel_marker=cancel_marker,
                resume=resume,
            )
            with console_path.open("x", encoding="utf-8", errors="replace") as console:
                if self.requirements_path is not None:
                    dependency_process = self._spawn_process(
                        [
                            str(interpreter),
                            "-m",
                            "pip",
                            "install",
                            "--disable-pip-version-check",
                            "--requirement",
                            str(self.requirements_path),
                        ],
                        console,
                    )
                    dependency_code = self._wait_process(dependency_process)
                    if dependency_code != 0:
                        raise FirstRunCoordinatorError(
                            "dependency_install_failed",
                            f"Python dependency installation failed with exit code {dependency_code}",
                        )
                    console.flush()
                process = self._spawn_process(command, console)
                self._set_status(phase="preflight", progress=0.0, message="First-run extraction started")
                exit_code = self._wait_process(process, cancel_marker=cancel_marker)

            state_payload: dict[str, Any] = {}
            if paths["state"].is_file():
                state_payload = json.loads(paths["state"].read_text(encoding="utf-8"))
            source_status = str(state_payload.get("status") or "failed")
            actual_stage = self._runtime_stage(paths["runtime"], paths["stage_root"])
            if (
                source_status == "completed"
                and exit_code == 0
                and paths["runtime"].is_file()
                and actual_stage.is_dir()
            ):
                final_state = "completed"
                ready = True
            elif source_status == "completed_with_deferred_helpers" and exit_code == 0:
                final_state = "partial"
                ready = False
            elif source_status == "cancelled" or self._cancel_requested:
                final_state = "cancelled"
                ready = False
            else:
                final_state = "failed"
                ready = False
            result = self._result_summary(paths, state_payload, source_status, exit_code=exit_code)
            self._set_status(
                state=final_state,
                phase="complete" if ready else final_state,
                progress=1.0,
                message=(
                    "Required first-run data is ready"
                    if ready
                    else "First-run data is partial; resume after resolving deferred helpers"
                    if final_state == "partial"
                    else "First-run data preparation cancelled"
                    if final_state == "cancelled"
                    else "First-run data preparation failed"
                ),
                ready=ready,
                completed_at=utc_now(),
                resume_available=not ready and paths["state"].is_file(),
                cancel_requested=False,
                capabilities=result["capabilities"],
                result=result,
                error=result.get("error"),
            )
            if ready and self.on_ready is not None:
                self.on_ready(paths["runtime"], actual_stage)
        except FirstRunCancelled:
            state_payload = {}
            if paths["state"].is_file():
                try:
                    state_payload = json.loads(paths["state"].read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    state_payload = {}
            result = self._result_summary(paths, state_payload, "cancelled")
            self._set_status(
                state="cancelled",
                phase="cancelled",
                progress=1.0,
                message="First-run data preparation cancelled; partial outputs were preserved",
                ready=False,
                completed_at=utc_now(),
                cancel_requested=False,
                resume_available=paths["state"].is_file(),
                capabilities=result["capabilities"],
                result=result,
                error={"code": "cancelled", "message": "Cancellation completed", "retryable": True},
            )
        except Exception as error:
            code = error.code if isinstance(error, FirstRunCoordinatorError) else "backend_failure"
            retryable = error.retryable if isinstance(error, FirstRunCoordinatorError) else True
            self._set_status(
                state="failed",
                phase="failed",
                progress=1.0,
                message="First-run backend failed before completion",
                ready=False,
                completed_at=utc_now(),
                cancel_requested=False,
                resume_available=paths["state"].is_file(),
                error={"code": code, "message": str(error), "retryable": retryable},
            )
        finally:
            with self._lock:
                self._process = None

    @staticmethod
    def _last_event(path: Path) -> dict[str, Any] | None:
        if not path.is_file() or path.stat().st_size == 0:
            return None
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 65536))
            lines = handle.read().splitlines()
        for line in reversed(lines):
            if line.strip():
                return json.loads(line.decode("utf-8"))
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            status = dict(self._status)
            request = dict(self._request) if self._request is not None else None
            running = self._thread is not None and self._thread.is_alive()
        if running and request is not None:
            event = self._last_event(self._paths(request)["log_root"] / "events.jsonl")
            if event:
                status.update(
                    phase=event.get("phase", status.get("phase")),
                    progress=float(event.get("progress", status.get("progress", 0.0))),
                    phase_index=self._phase_index(str(event.get("phase") or status.get("phase") or "running")),
                    message=event.get("message", status.get("message")),
                    source_status=event.get("status", "running"),
                )
        return self._decorate_status(status)

    def events(self, after_sequence: int = 0, limit: int = 250) -> dict[str, Any]:
        limit = max(1, min(500, int(limit)))
        with self._lock:
            request = dict(self._request) if self._request is not None else None
        if request is None:
            return {
                "format": EVENTS_FORMAT,
                "events": [],
                "next_after": after_sequence,
                "has_more": False,
                "progress_scope": "phase",
            }
        path = self._paths(request)["log_root"] / "events.jsonl"
        if not path.is_file():
            return {
                "format": EVENTS_FORMAT,
                "events": [],
                "next_after": after_sequence,
                "has_more": False,
                "progress_scope": "phase",
            }
        events = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number <= after_sequence or not line.strip():
                    continue
                event = json.loads(line)
                event["source_sequence"] = event.get("sequence")
                event["sequence"] = line_number
                event["progress_scope"] = "phase"
                event["phase_index"] = self._phase_index(str(event.get("phase") or ""))
                event["phase_count"] = len(PHASES)
                events.append(event)
                if len(events) > limit:
                    break
        has_more = len(events) > limit
        events = events[:limit]
        next_after = int(events[-1]["sequence"]) if events else after_sequence
        return {
            "format": EVENTS_FORMAT,
            "events": events,
            "next_after": next_after,
            "has_more": has_more,
            "progress_scope": "phase",
        }

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            if not running:
                return self._decorate_status(dict(self._status))
            self._cancel_requested = True
        self._set_status(
            message="Cooperative cancellation requested",
            cancel_requested=True,
        )
        return self.status()
