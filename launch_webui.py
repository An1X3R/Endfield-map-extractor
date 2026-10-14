"""Start the local Endfield Atlas Composer WebUI.

This launcher starts the local Vite server and the token-protected asynchronous
export bridge. It never writes to the game installation, and it keeps Stage119
map-layer and exported data-package paths server-only.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Mapping
from urllib.request import urlopen
from urllib.parse import parse_qs, unquote, urlparse
import webbrowser

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from endfield_runtime_config import (
    ConfigurationError,
    RuntimeConfig,
    configured_optional_path,
    configured_path,
    load_runtime_config,
    path_hint,
)
from webui_export_worker import ExportCoordinator
from bootstrap_environment import require_virtualenv, setup_command, venv_python
from webui_first_run_contract_v1 import (
    REQUEST_FORMAT,
    FirstRunRequestError,
    canonicalize_first_run_request,
    error_response,
    validate_first_run_paths,
)
from webui_first_run_coordinator import FirstRunCoordinator, FirstRunCoordinatorError
from webui_instanced_worker import InstancedSceneCoordinator, SceneJobNotFound
from webui_character_contract import CharacterJobNotFound, CharacterRequestConflict, CharacterRequestError
from webui_character_worker import CharacterCoordinator


WEBUI_ROOT = ROOT / "webui"
FIRST_RUN_SCRIPT = ROOT / "src" / "endfield_first_run.py"
PYTHON_REQUIREMENTS = ROOT / "requirements.txt"
PICKER_LOCK = threading.Lock()


def resolve_character_config(explicit: Path | None, environment: Mapping[str, str], root: Path) -> Path | None:
    """Resolve an optional local reader pack without embedding workstation paths."""
    configured = environment.get("ENDFIELD_CHARACTER_CONFIG", "").strip()
    if explicit is not None:
        selected = explicit
    elif configured:
        selected = Path(configured)
    else:
        selected = root / ".tools" / "character" / "runtime.json"
        if not selected.exists():
            selected = root / "endfield.character.local.json"
        if not selected.exists():
            return None
    resolved = selected.expanduser().resolve()
    if not resolved.is_file():
        raise ConfigurationError(f"Character runtime configuration file is missing: {resolved}")
    return resolved


def validate_selected_path(kind: str, selected: Path) -> dict[str, object]:
    if kind == "game":
        checks = {
            "executable": (selected / "Endfield.exe").is_file(),
            "globalgamemanagers": (selected / "Endfield_Data" / "globalgamemanagers").is_file(),
            "vfs": (selected / "Endfield_Data" / "StreamingAssets" / "VFS").is_dir(),
        }
        return {"valid": all(checks.values()), "readOnly": True, "checks": checks}
    if kind == "output":
        valid = selected.is_dir() and os.access(selected, os.W_OK)
        return {"valid": valid, "writable": valid, "writableParent": valid}
    if kind == "cache":
        candidate = selected
        while not candidate.exists() and candidate.parent != candidate:
            candidate = candidate.parent
        writable_parent = candidate.is_dir() and os.access(candidate, os.W_OK)
        valid = not selected.is_file() and writable_parent
        return {"valid": valid, "writable": selected.is_dir() and writable_parent, "writableParent": writable_parent}
    if kind == "blender":
        valid = selected.is_file() and selected.name.lower() == "blender.exe"
        return {"valid": valid, "executable": valid}
    return {"valid": False}


def choose_native_path(kind: str, current: str) -> dict[str, object]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return {
            "format": "EndfieldPathPickerResponse/1",
            "status": "unavailable",
            "error": "tkinter_unavailable",
        }

    initial = Path(current) if current else ROOT
    initial_dir = initial if initial.is_dir() else initial.parent
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    try:
        if kind == "blender":
            value = filedialog.askopenfilename(
                parent=root,
                title="选择 Blender 可执行文件",
                initialdir=str(initial_dir),
                initialfile=initial.name if initial.name.lower() == "blender.exe" else "blender.exe",
                filetypes=(("Blender executable", "blender.exe"), ("Executable files", "*.exe")),
            )
        else:
            title = (
                "选择游戏目录（只读）"
                if kind == "game"
                else "选择首次提取缓存目录"
                if kind == "cache"
                else "选择输出目录"
            )
            value = filedialog.askdirectory(parent=root, title=title, initialdir=str(initial_dir), mustexist=True)
    finally:
        root.destroy()

    if not value:
        return {"format": "EndfieldPathPickerResponse/1", "status": "cancelled", "kind": kind}
    selected = Path(value).resolve()
    validation = validate_selected_path(kind, selected)
    return {
        "format": "EndfieldPathPickerResponse/1",
        "status": "selected",
        "kind": kind,
        "path": str(selected),
        **validation,
    }


def collect_first_run_paths(args: argparse.Namespace) -> None:
    """Collect only missing first-run paths with native Windows dialogs."""

    runtime = load_runtime_config(args.runtime_config, root=ROOT)
    selections = (
        ("game", "game_root", "ENDFIELD_GAME_ROOT", runtime.value("game_root"), True),
        ("output", "export_root", "ENDFIELD_EXPORT_ROOT", runtime.value("export_root"), True),
        ("blender", "blender_exe", "ENDFIELD_BLENDER", runtime.value("blender_exe"), False),
    )
    for kind, argument_name, environment_name, config_value, required in selections:
        current_value = getattr(args, argument_name, None) or os.environ.get(environment_name) or config_value
        if current_value:
            continue
        result = choose_native_path(kind, "")
        if result.get("status") == "selected" and result.get("valid") and result.get("path"):
            setattr(args, argument_name, Path(str(result["path"])))
            continue
        if required:
            reason = str(result.get("error") or result.get("status") or "not selected")
            raise ConfigurationError(
                f"First-run {kind} path is required and was not selected ({reason}). "
                "Pass the matching CLI option or environment variable, then retry."
            )


class PathPickerServer(ThreadingHTTPServer):
    token: str
    coordinator: ExportCoordinator
    first_run: FirstRunCoordinator
    instanced: InstancedSceneCoordinator
    character: CharacterCoordinator
    require_first_run_ready: bool

    def server_close(self) -> None:
        errors: list[Exception] = []
        try:
            for name in ("first_run", "character", "instanced", "coordinator"):
                if hasattr(self, name):
                    try:
                        getattr(self, name).shutdown()
                    except Exception as error:
                        error.add_note(f"Coordinator shutdown failed: {name}")
                        errors.append(error)
        finally:
            super().server_close()
        if errors:
            raise ExceptionGroup("WebUI workers failed to shut down cleanly", errors)


class UnavailableExportCoordinator:
    def shutdown(self) -> None:
        """No workers exist before the scene runtime is configured."""
        return None

    def _unavailable(self) -> None:
        raise FirstRunCoordinatorError(
            "first_run_required",
            "Prepare the required local map data before using map export jobs.",
            retryable=True,
        )

    def validate(self, _payload: object) -> dict[str, object]:
        self._unavailable()
        return {}

    def submit(self, _payload: object) -> dict[str, object]:
        self._unavailable()
        return {}

    def get(self, _job_id: str) -> dict[str, object]:
        self._unavailable()
        return {}

    def events(self, _job_id: str, _after: int = 0) -> list[dict[str, object]]:
        self._unavailable()
        return []

    def cancel(self, _job_id: str) -> dict[str, object]:
        self._unavailable()
        return {}


class PathPickerHandler(BaseHTTPRequestHandler):
    server: PathPickerServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-Endfield-Path-Picker-Token", ""),
            self.server.token,
        )

    def read_json_body(self, maximum: int = 1024 * 1024) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > maximum:
            raise ValueError("request_body_too_large")
        payload = self.rfile.read(length).decode("utf-8") if length else "{}"
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("request_body_must_be_object")
        return value

    def handle_error(self, error: Exception) -> None:
        if isinstance(error, FileNotFoundError):
            self.send_json(404, {"error": "job_not_found", "message": str(error)})
        elif isinstance(error, FirstRunRequestError):
            self.send_json(
                error.http_status,
                error_response(
                    error.code,
                    str(error),
                    retryable=error.retryable,
                    details=error.details,
                ),
            )
        elif isinstance(error, FirstRunCoordinatorError):
            self.send_json(
                error.http_status,
                error_response(
                    error.code,
                    str(error),
                    retryable=error.retryable,
                    details=error.details,
                ),
            )
        elif isinstance(error, (ValueError, json.JSONDecodeError, UnicodeDecodeError)):
            self.send_json(400, error_response("invalid_request", str(error)))
        else:
            self.send_json(500, error_response("internal_error", str(error), retryable=True))

    def handle_instanced_error(self, error: Exception) -> None:
        if isinstance(error, SceneJobNotFound):
            self.send_json(404, error_response("instanced_job_not_found", str(error), retryable=False))
        elif isinstance(error, FileNotFoundError):
            self.send_json(409, error_response("instanced_source_missing", str(error), retryable=True))
        else:
            self.handle_error(error)

    def handle_character_error(self, error: Exception) -> None:
        if isinstance(error, CharacterJobNotFound):
            self.send_json(404, error_response("character_job_not_found", "角色任务不存在。", retryable=False))
        elif isinstance(error, CharacterRequestConflict):
            self.send_json(409, error_response("character_request_conflict", str(error), retryable=False))
        elif isinstance(error, CharacterRequestError):
            self.send_json(400, error_response("character_invalid_request", str(error), retryable=False))
        else:
            safe = getattr(error, "safe_message", None)
            code = getattr(error, "code", "character_service_error")
            message = safe if isinstance(safe, str) else "角色接口不可用，请检查角色配置或本地后端日志。"
            status = getattr(error, "http_status", 400 if isinstance(error, (ValueError, UnicodeDecodeError)) else 500)
            self.send_json(status if type(status) is int and 400 <= status <= 599 else 500,
                           error_response(code if isinstance(code, str) else "character_service_error",
                                          message, retryable=True))

    def character_get(self, path: str, query: str) -> None:
        try:
            actions = re.fullmatch(r"/characters/([^/]+)/animations", path)
            if actions:
                params = parse_qs(query)
                self.send_json(200, self.server.character.animations(
                    unquote(actions.group(1)), params.get("variant_id", [""])[0],
                    int(params.get("offset", ["0"])[0]), int(params.get("limit", ["100"])[0])))
                return
            if path == "/characters":
                self.send_json(200, self.server.character.catalog())
                return
            detail = re.fullmatch(r"/characters/([^/]+)", path)
            task = re.fullmatch(r"/(character-jobs|character-preparation)/([A-Za-z0-9-]+)(/events)?", path)
            if detail:
                self.send_json(200, self.server.character.detail(unquote(detail.group(1))))
            elif task:
                job_id = task.group(2)
                job = self.server.character.get(job_id)
                expected = "preparation" if task.group(1) == "character-preparation" else "export"
                if job["kind"] != expected:
                    raise CharacterJobNotFound("Character job belongs to a different route")
                params = parse_qs(query)
                result = (self.server.character.events(job_id, int(params.get("after", ["0"])[0]),
                                                      int(params.get("limit", ["250"])[0]))
                          if task.group(3) else job)
                self.send_json(200, result)
            else:
                self.send_json(404, {"error": "character_route_not_found", "message": "角色接口不存在。"})
        except Exception as error:
            self.handle_character_error(error)

    def character_post(self, path: str) -> None:
        try:
            task = re.fullmatch(r"/(character-jobs|character-preparation)/([A-Za-z0-9-]+)/cancel", path)
            expected = "preparation" if path.startswith("/character-preparation") else "export"
            if task:
                if self.server.character.get(task.group(2))["kind"] != expected:
                    raise CharacterJobNotFound("Character job belongs to a different route")
                self.send_json(202, self.server.character.cancel(task.group(2)))
            elif path in {"/character-jobs", "/character-jobs/validate", "/character-preparation", "/character-preparation/validate"}:
                payload = self.read_json_body()
                required_format = "CharacterPreparationRequest/1" if expected == "preparation" else "CharacterExportRequest/1"
                if payload.get("format") != required_format:
                    raise CharacterRequestError("Request format does not match this character route")
                result = (self.server.character.validate(payload) if path.endswith("/validate")
                          else self.server.character.submit(payload))
                self.send_json(200 if path.endswith("/validate") else 202, result)
            else:
                self.send_json(404, {"error": "character_route_not_found", "message": "角色接口不存在。"})
        except Exception as error:
            self.handle_character_error(error)

    def do_GET(self) -> None:
        if not self.authorized():
            self.send_json(404, {"error": "not_found"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/characters" or parsed.path.startswith(("/characters/", "/character-jobs/", "/character-preparation/")):
            self.character_get(parsed.path, parsed.query)
            return
        if parsed.path == "/instanced-scenes" or parsed.path.startswith(("/instanced-scenes/", "/instanced-scene-jobs/")):
            try:
                query = parse_qs(parsed.query)
                catalog_id = query.get("catalog_id", [None])[0]
                scene_match = re.fullmatch(r"/instanced-scenes/([a-z][a-z0-9_]*)(/preview)?", parsed.path)
                task_match = re.fullmatch(r"/instanced-scene-jobs/([A-Za-z0-9-]+)(/events)?", parsed.path)
                if parsed.path == "/instanced-scenes":
                    self.send_json(200, self.server.instanced.catalog())
                elif scene_match:
                    result = (self.server.instanced.preview(scene_match.group(1), catalog_id) if scene_match.group(2)
                              else self.server.instanced.detail(scene_match.group(1), catalog_id))
                    self.send_json(200, result)
                elif task_match:
                    result = (self.server.instanced.events(task_match.group(1), int(query.get("after", ["0"])[0]))
                              if task_match.group(2) else self.server.instanced.get(task_match.group(1)))
                    self.send_json(200, result)
                else:
                    self.send_json(404, {"error": "instanced_route_not_found", "message": "独立场景接口不存在"})
            except Exception as error:
                self.handle_instanced_error(error)
            return
        if parsed.path == "/health":
            self.send_json(200, {"format": "EndfieldLocalBridgeHealth/1", "status": "ready"})
            return
        if parsed.path == "/first-run":
            self.send_json(200, self.server.first_run.status())
            return
        if parsed.path == "/first-run/events":
            try:
                query = parse_qs(parsed.query)
                after_text = query.get("after", ["0"])[0]
                limit_text = query.get("limit", ["250"])[0]
                self.send_json(
                    200,
                    self.server.first_run.events(max(0, int(after_text)), max(1, min(500, int(limit_text)))),
                )
            except Exception as error:
                self.handle_error(error)
            return
        events_match = re.fullmatch(r"/jobs/([A-Za-z0-9._-]+)/events", parsed.path)
        job_match = re.fullmatch(r"/jobs/([A-Za-z0-9._-]+)", parsed.path)
        try:
            if events_match:
                after_text = parse_qs(parsed.query).get("after", ["0"])[0]
                after = max(0, int(after_text))
                events = self.server.coordinator.events(events_match.group(1), after)
                self.send_json(200, {"format": "EndfieldWebUIEvents/1", "events": events})
                return
            if job_match:
                self.send_json(200, self.server.coordinator.get(job_match.group(1)))
                return
        except Exception as error:
            self.handle_error(error)
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self.authorized():
            self.send_json(404, {"error": "not_found"})
            return
        parsed = urlparse(self.path)
        if parsed.path in {"/character-jobs", "/character-preparation"} or parsed.path.startswith(("/character-jobs/", "/character-preparation/")):
            self.character_post(parsed.path)
            return
        if parsed.path == "/instanced-scene-jobs" or parsed.path.startswith("/instanced-scene-jobs/"):
            try:
                cancel = re.fullmatch(r"/instanced-scene-jobs/([A-Za-z0-9-]+)/cancel", parsed.path)
                if cancel:
                    self.send_json(202, self.server.instanced.cancel(cancel.group(1)))
                elif parsed.path in {"/instanced-scene-jobs", "/instanced-scene-jobs/validate"}:
                    payload = self.read_json_body()
                    if parsed.path.endswith("/validate"):
                        self.send_json(200, self.server.instanced.validate(payload))
                    else:
                        self.send_json(202, self.server.instanced.submit(payload))
                else:
                    self.send_json(404, {"error": "instanced_route_not_found", "message": "独立场景接口不存在"})
            except Exception as error:
                self.handle_instanced_error(error)
            return
        cancel_match = re.fullmatch(r"/jobs/([A-Za-z0-9._-]+)/cancel", parsed.path)
        if parsed.path in {"/first-run", "/first-run/validate", "/first-run/cancel"}:
            try:
                if parsed.path == "/first-run/cancel":
                    self.send_json(202, self.server.first_run.cancel())
                    return
                payload = self.read_json_body()
                result = (
                    self.server.first_run.validate(payload)
                    if parsed.path.endswith("/validate")
                    else self.server.first_run.submit(payload)
                )
                self.send_json(
                    200 if parsed.path.endswith("/validate") or result.get("disposition") == "already_ready" else 202,
                    result,
                )
            except Exception as error:
                self.handle_error(error)
            return
        if parsed.path in {"/jobs/validate", "/jobs"}:
            try:
                if self.server.require_first_run_ready and not self.server.first_run.status().get("ready"):
                    self.send_json(
                        409,
                        {
                            **error_response(
                                "first_run_required",
                                "Prepare the required local map data before submitting a map extraction job.",
                                retryable=True,
                            ),
                            "firstRun": self.server.first_run.status(),
                        },
                    )
                    return
                payload = self.read_json_body()
                result = (
                    self.server.coordinator.validate(payload)
                    if parsed.path == "/jobs/validate"
                    else self.server.coordinator.submit(payload)
                )
                self.send_json(200 if parsed.path.endswith("validate") else 202, result)
            except Exception as error:
                self.handle_error(error)
            return
        if cancel_match:
            try:
                self.send_json(202, self.server.coordinator.cancel(cancel_match.group(1)))
            except Exception as error:
                self.handle_error(error)
            return
        if parsed.path != "/pick":
            self.send_json(404, {"error": "not_found"})
            return
        try:
            payload = self.read_json_body(8192)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "invalid_request"})
            return
        kind = payload.get("kind")
        current = payload.get("current", "")
        if kind not in {"game", "output", "cache", "blender"} or not isinstance(current, str):
            self.send_json(400, error_response("invalid_path_kind", "Unsupported path picker kind."))
            return
        with PICKER_LOCK:
            self.send_json(200, choose_native_path(kind, current))


def start_path_picker(
    stage119_root: Path | None,
    runtime_config: Path | None = None,
    *,
    first_run_name: str = "bootstrap_v1",
    require_first_run_ready: bool = False,
    first_run_defaults: dict[str, object] | None = None,
    on_stage_ready: Callable[[Path, Path], None] | None = None,
) -> tuple[PathPickerServer, threading.Thread]:
    server = PathPickerServer(("127.0.0.1", 0), PathPickerHandler)
    server.token = secrets.token_urlsafe(32)
    server.require_first_run_ready = require_first_run_ready
    server.coordinator = (
        ExportCoordinator(stage119_root=stage119_root, runtime_config=runtime_config)
        if stage119_root is not None and stage119_root.is_dir()
        else UnavailableExportCoordinator()
    )  # type: ignore[assignment]

    def activate_generated_runtime(generated_config: Path, generated_stage: Path) -> None:
        replacement = ExportCoordinator(
            stage119_root=generated_stage,
            runtime_config=generated_config,
        )
        server.coordinator.shutdown()
        server.coordinator = replacement
        if on_stage_ready is not None:
            on_stage_ready(generated_config, generated_stage)

    character_defaults = first_run_defaults or {}
    public_first_run_defaults = {name: value for name, value in character_defaults.items() if name != "character_config"}
    complete_initial_request = (
        {**public_first_run_defaults, "format": REQUEST_FORMAT}
        if public_first_run_defaults
        and all(public_first_run_defaults.get(name) for name in ("game_root", "export_root", "cache_root"))
        else None
    )

    def prepared_first_run_interpreter() -> Path:
        interpreter = venv_python()
        if not interpreter.is_file():
            raise FirstRunCoordinatorError(
                "python_environment_not_prepared",
                f"Prepared Python environment is missing: {interpreter}. Run: {setup_command()}",
                http_status=503,
                retryable=False,
            )
        return interpreter

    server.first_run = FirstRunCoordinator(
        extractor_root=ROOT,
        interpreter_provider=prepared_first_run_interpreter,
        requirements_path=PYTHON_REQUIREMENTS,
        default_run_name=first_run_name,
        on_ready=activate_generated_runtime,
        initial_request=complete_initial_request,
        initial_defaults=public_first_run_defaults,
    )
    server.instanced = InstancedSceneCoordinator(server.first_run.status)
    from endfield_character_service import CharacterService
    character_config = character_defaults.get("character_config")
    character_export_root = character_defaults.get("export_root")
    server.character = CharacterCoordinator(
        CharacterService(Path(str(character_config)) if character_config else None),
        (Path(str(character_export_root)) if character_export_root else ROOT / "exports") / "character_jobs",
    )
    thread = threading.Thread(target=server.serve_forever, name="endfield-path-picker", daemon=True)
    thread.start()
    return server, thread


def resolve_runtime_paths(args: argparse.Namespace) -> tuple[dict[str, Path | None], RuntimeConfig]:
    """Resolve launcher paths without touching game, cache, or Blender data."""

    runtime = load_runtime_config(args.runtime_config, root=ROOT)
    config_dir = runtime.directory
    paths = {
        "game_root": configured_path(
            args.game_root,
            env_name="ENDFIELD_GAME_ROOT",
            label="Endfield game root",
            config_value=runtime.value("game_root"),
            config_dir=config_dir,
            cli_option="--game-root",
            config_key="game_root",
        ),
        "export_root": configured_path(
            args.export_root,
            env_name="ENDFIELD_EXPORT_ROOT",
            label="Endfield export root",
            config_value=runtime.value("export_root"),
            config_dir=config_dir,
            cli_option="--export-root",
            config_key="export_root",
        ),
        "blender_exe": configured_optional_path(
            getattr(args, "blender_exe", None),
            env_name="ENDFIELD_BLENDER",
            label="Blender executable",
            config_value=runtime.value("blender_exe"),
            config_dir=config_dir,
            cli_option="--blender-exe",
            config_key="blender_exe",
        ),
    }
    paths["cache_root"] = configured_path(
        getattr(args, "cache_root", None),
        env_name="ENDFIELD_CACHE_ROOT",
        label="Endfield extraction cache root",
        config_value=runtime.value("cache_root"),
        config_dir=config_dir,
        default=Path(str(paths["export_root"])) / "cache" / "first_run_work",
        cli_option="--cache-root",
        config_key="cache_root",
    )
    paths["stage119_root"] = configured_path(
            getattr(args, "stage119_root", None),
            env_name="ENDFIELD_STAGE119_ROOT",
            label="Stage119 cache root",
            config_value=runtime.value("stage119_root"),
            config_dir=config_dir,
            default=Path(str(paths["export_root"])) / "cache" / f"map_layers_{getattr(args, 'first_run_name', 'bootstrap_v1')}",
            cli_option="--stage119-root",
            config_key="stage119_root",
        )
    paths["webui_output_root"] = configured_path(
        args.webui_output_root,
        env_name="ENDFIELD_WEBUI_OUTPUT_ROOT",
        label="WebUI output root",
        config_value=runtime.value("webui_output_root"),
        config_dir=config_dir,
        default=paths["export_root"] / "webui_exports",
        cli_option="--webui-output-root",
        config_key="webui_output_root",
    )
    return paths, runtime


def resolve_deferred_runtime_paths(
    args: argparse.Namespace,
) -> tuple[dict[str, Path | None], RuntimeConfig]:
    """Resolve optional setup defaults without requiring a configured machine."""

    runtime = load_runtime_config(args.runtime_config, root=ROOT)
    config_dir = runtime.directory

    def optional(
        cli_value: Path | str | None,
        env_name: str,
        config_key: str,
    ) -> Path | None:
        return configured_optional_path(
            cli_value,
            env_name=env_name,
            label=config_key,
            config_value=runtime.value(config_key),
            config_dir=config_dir,
            cli_option=f"--{config_key.replace('_', '-')}",
            config_key=config_key,
        )

    game_root = optional(args.game_root, "ENDFIELD_GAME_ROOT", "game_root")
    export_root = optional(args.export_root, "ENDFIELD_EXPORT_ROOT", "export_root")
    cache_root = optional(args.cache_root, "ENDFIELD_CACHE_ROOT", "cache_root")
    stage119_root = optional(args.stage119_root, "ENDFIELD_STAGE119_ROOT", "stage119_root")
    webui_output_root = optional(
        args.webui_output_root,
        "ENDFIELD_WEBUI_OUTPUT_ROOT",
        "webui_output_root",
    )
    if export_root is not None:
        cache_root = cache_root or (export_root / "cache" / "first_run_work").resolve()
        stage119_root = stage119_root or (
            export_root / "cache" / f"map_layers_{args.first_run_name}"
        ).resolve()
        webui_output_root = webui_output_root or (export_root / "webui_exports").resolve()
    return {
        "game_root": game_root,
        "export_root": export_root,
        "cache_root": cache_root,
        "stage119_root": stage119_root,
        "webui_output_root": webui_output_root,
        "blender_exe": optional(args.blender_exe, "ENDFIELD_BLENDER", "blender_exe"),
    }, runtime


def write_stage_pointer(path: Path, stage_root: Path | None, runtime_config: Path | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "EndfieldStagePointer/1",
        "stage_root": str(stage_root) if stage_root is not None and stage_root.is_dir() else None,
        "runtime_config": str(runtime_config) if runtime_config is not None and runtime_config.is_file() else None,
        "updated_at": time.time(),
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_runtime_paths(paths: dict[str, Path | None], *, include_interactive_paths: bool) -> list[str]:
    """Report missing runtime prerequisites without opening data or Blender."""

    errors: list[str] = []
    stage119_root = paths["stage119_root"]
    assert stage119_root is not None
    if not stage119_root.is_dir():
        errors.append(
            f"Stage119 cache directory is missing: {stage119_root}. "
            + path_hint(
                cli_option="--stage119-root",
                env_name="ENDFIELD_STAGE119_ROOT",
                config_key="stage119_root",
            )
        )
    else:
        missing = [
            name
            for name in ("region_map_layer_manifest.json", "full_audit.json")
            if not (stage119_root / name).is_file()
        ]
        if missing:
            errors.append(
                f"Stage119 cache is incomplete at {stage119_root}; missing: {', '.join(missing)}. "
                "Use the audited Stage119 cache root, not a parent log directory."
            )
    if not include_interactive_paths:
        return errors

    game_root = paths["game_root"]
    assert game_root is not None
    if not game_root.is_dir():
        errors.append(
            f"Endfield game directory is missing: {game_root}. "
            + path_hint(cli_option="--game-root", env_name="ENDFIELD_GAME_ROOT", config_key="game_root")
        )
    else:
        missing = [
            str(relative)
            for relative in (
                Path("Endfield.exe"),
                Path("Endfield_Data") / "globalgamemanagers",
                Path("Endfield_Data") / "StreamingAssets" / "VFS",
            )
            if not (game_root / relative).exists()
        ]
        if missing:
            errors.append(
                f"Endfield game directory is incomplete at {game_root}; missing: {', '.join(missing)}. "
                "Select the installation directory containing Endfield.exe and Endfield_Data."
            )
    blender_exe = paths["blender_exe"]
    if blender_exe is not None and (not blender_exe.is_file() or blender_exe.name.casefold() != "blender.exe"):
        errors.append(
            f"Blender executable is unavailable: {blender_exe}. "
            + path_hint(cli_option="--blender-exe", env_name="ENDFIELD_BLENDER", config_key="blender_exe")
        )
    output_root = paths["webui_output_root"]
    assert output_root is not None
    if not output_root.is_dir():
        errors.append(
            f"WebUI output directory is missing: {output_root}. "
            + path_hint(
                cli_option="--webui-output-root",
                env_name="ENDFIELD_WEBUI_OUTPUT_ROOT",
                config_key="webui_output_root",
            )
        )
    return errors


def bootstrap_first_run(
    paths: dict[str, Path | None],
    *,
    run_name: str,
    skip_helpers: bool,
    skip_scene_discovery: bool,
) -> Path:
    """Build the missing local dataset from the selected read-only game install."""

    if not FIRST_RUN_SCRIPT.is_file():
        raise ConfigurationError(f"First-run coordinator is missing: {FIRST_RUN_SCRIPT}")
    game_root = paths["game_root"]
    cache_root = paths["cache_root"]
    export_root = paths["export_root"]
    assert game_root is not None and cache_root is not None and export_root is not None
    try:
        interpreter = require_virtualenv()
    except RuntimeError as error:
        raise ConfigurationError(str(error)) from error
    command = [
        str(interpreter),
        str(FIRST_RUN_SCRIPT),
        "--game-root",
        str(game_root),
        "--cache-root",
        str(cache_root),
        "--export-root",
        str(export_root),
        "--run-name",
        run_name,
    ]
    if paths["blender_exe"] is not None:
        command.extend(["--blender-exe", str(paths["blender_exe"])])
    if skip_helpers:
        command.append("--skip-helper-bootstrap")
    if skip_scene_discovery:
        command.append("--skip-scene-discovery")
    state_path = cache_root / run_name / "first_run_state.json"
    if state_path.is_file():
        command.append("--resume")
    print("Stage data is missing; running the automatic read-only first-run extraction now.")
    process = subprocess.run(command, cwd=str(ROOT), check=False)
    if process.returncode != 0:
        raise ConfigurationError(
            f"Automatic first-run extraction failed with exit code {process.returncode}. "
            f"Inspect {export_root / 'log' / ('first_run_' + run_name)} and rerun this launcher."
        )
    generated = cache_root / run_name / "endfield.runtime.local.json"
    if not generated.is_file():
        raise ConfigurationError(
            f"First-run extraction completed without its runtime configuration: {generated}"
        )
    return generated


def is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.15)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def resolve_port(requested: int) -> int:
    for port in range(requested, requested + 20):
        if is_port_available(port):
            return port
    raise RuntimeError(f"No free local port was found in {requested}-{requested + 19}.")


def frontend_command(
    npm: str,
    webui_root: Path,
    *,
    development: bool,
    port: int,
) -> list[str]:
    if not (webui_root / "package.json").is_file():
        raise FileNotFoundError(f"WebUI package is missing: {webui_root}")
    if not (webui_root / "node_modules").is_dir():
        raise RuntimeError(
            f"WebUI dependencies are missing at {webui_root / 'node_modules'}. "
            f'Run: powershell -ExecutionPolicy Bypass -File "{ROOT / "setup_development_environment.ps1"}"'
        )
    if development:
        return [npm, "run", "dev", "--", "--port", str(port)]
    if not (webui_root / "dist" / "index.html").is_file():
        raise RuntimeError(
            f"Built WebUI is missing: {webui_root / 'dist' / 'index.html'}. "
            f'Run: powershell -ExecutionPolicy Bypass -File "{ROOT / "setup_development_environment.ps1"}"'
        )
    return [npm, "run", "preview", "--", "--port", str(port)]


def wait_for_server(url: str, process: subprocess.Popen[object], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urlopen(url, timeout=0.5) as response:
                if 200 <= response.status < 500:
                    return True
        except OSError:
            time.sleep(0.2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the Endfield local WebUI.")
    parser.add_argument("--port", type=int, help="Preferred local port (default: 4174 preview, 5173 development).")
    parser.add_argument("--development", action="store_true", help="Run Vite's development server instead of the built preview.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser after startup.")
    parser.add_argument(
        "--stage119-root",
        type=Path,
        help="Stage119 cache root (CLI > ENDFIELD_STAGE119_ROOT > local config > portable default).",
    )
    parser.add_argument("--game-root", type=Path, help="Default game root offered to the local WebUI.")
    parser.add_argument("--export-root", type=Path, help="Default export-root boundary offered to the local WebUI.")
    parser.add_argument("--cache-root", type=Path, help="External first-run cache root; defaults under export-root/cache.")
    parser.add_argument("--webui-output-root", type=Path, help="Default WebUI output directory offered to the local WebUI.")
    parser.add_argument("--blender-exe", type=Path, help="Default Blender executable offered to the local WebUI.")
    parser.add_argument("--runtime-config", type=Path, help="Optional EndfieldRuntimeConfig/1 local JSON file.")
    parser.add_argument("--character-config", type=Path,
                        help="Character reader configuration (CLI > ENDFIELD_CHARACTER_CONFIG > endfield.character.local.json).")
    parser.add_argument("--first-run-name", default="bootstrap_v1", help="Stable resumable first-run identifier.")
    parser.add_argument("--no-first-run", action="store_true", help="Do not generate a missing Stage dataset automatically.")
    first_run_mode = parser.add_mutually_exclusive_group()
    first_run_mode.add_argument(
        "--defer-first-run-to-webui",
        dest="defer_first_run_to_webui",
        action="store_true",
        help="Start the WebUI first and let its setup panel prepare the required local data (default).",
    )
    first_run_mode.add_argument(
        "--prepare-before-webui",
        dest="defer_first_run_to_webui",
        action="store_false",
        help="Legacy developer mode: resolve paths and prepare a missing Stage before starting the WebUI.",
    )
    parser.set_defaults(defer_first_run_to_webui=True)
    parser.add_argument("--no-setup-dialogs", action="store_true", help="Do not open native path dialogs when first-run paths are unconfigured.")
    parser.add_argument("--skip-first-run-helpers", action="store_true", help="Skip prepared pinned .NET helpers during first-run scene discovery.")
    parser.add_argument("--skip-scene-discovery", action="store_true", help="Skip automatic bundle candidate discovery during first run.")
    parser.add_argument(
        "--check-paths",
        action="store_true",
        help="Validate configured game, Stage119 cache, Blender, and output paths without starting WebUI or Blender.",
    )
    args = parser.parse_args()

    try:
        character_config = resolve_character_config(args.character_config, os.environ, ROOT)
    except ConfigurationError as error:
        print(f"CONFIGURATION_ERROR: {error}", file=sys.stderr)
        return 2

    if args.defer_first_run_to_webui:
        try:
            runtime_paths, runtime_config = resolve_deferred_runtime_paths(args)
        except ConfigurationError as error:
            print(f"CONFIGURATION_ERROR: {error}", file=sys.stderr)
            return 2
    else:
        try:
            runtime_paths, runtime_config = resolve_runtime_paths(args)
        except ConfigurationError as error:
            if args.no_setup_dialogs or args.runtime_config is not None:
                print(f"CONFIGURATION_ERROR: {error}", file=sys.stderr)
                return 2
            try:
                collect_first_run_paths(args)
                runtime_paths, runtime_config = resolve_runtime_paths(args)
            except ConfigurationError as setup_error:
                print(f"CONFIGURATION_ERROR: {setup_error}", file=sys.stderr)
                return 2

    stage_errors = (
        []
        if args.defer_first_run_to_webui
        else validate_runtime_paths(runtime_paths, include_interactive_paths=False)
    )
    if stage_errors and not args.check_paths and not args.no_first_run and not args.defer_first_run_to_webui:
        try:
            generated_config = bootstrap_first_run(
                runtime_paths,
                run_name=args.first_run_name,
                skip_helpers=args.skip_first_run_helpers,
                skip_scene_discovery=args.skip_scene_discovery,
            )
            args.runtime_config = generated_config
            args.stage119_root = None
            args.game_root = None
            args.export_root = None
            args.cache_root = None
            args.webui_output_root = None
            args.blender_exe = None
            runtime_paths, runtime_config = resolve_runtime_paths(args)
        except ConfigurationError as error:
            print(f"CONFIGURATION_ERROR: {error}", file=sys.stderr)
            return 2

    if args.defer_first_run_to_webui:
        path_errors: list[str] = []
    else:
        path_errors = validate_runtime_paths(runtime_paths, include_interactive_paths=args.check_paths)
    if path_errors:
        for error in path_errors:
            print(f"CONFIGURATION_ERROR: {error}", file=sys.stderr)
        return 2
    if args.check_paths:
        configured = all(runtime_paths.get(name) is not None for name in ("game_root", "export_root", "cache_root"))
        setup_validation = None
        if args.defer_first_run_to_webui and configured:
            setup_validation = validate_first_run_paths(
                canonicalize_first_run_request(
                    {
                        "format": REQUEST_FORMAT,
                        "game_root": str(runtime_paths["game_root"]),
                        "export_root": str(runtime_paths["export_root"]),
                        "cache_root": str(runtime_paths["cache_root"]),
                        "run_name": args.first_run_name,
                    },
                    default_run_name=args.first_run_name,
                )
            )
        print(
            json.dumps(
                {
                    "format": "EndfieldRuntimePathCheck/1",
                    "status": (
                        setup_validation["status"]
                        if setup_validation is not None
                        else "setup_required"
                        if args.defer_first_run_to_webui
                        else "passed"
                    ),
                    "runtimeConfig": str(runtime_config.path) if runtime_config.path else None,
                    "paths": {
                        name: str(value) if value is not None else None
                        for name, value in runtime_paths.items()
                    },
                    "validation": setup_validation,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not (WEBUI_ROOT / "package.json").is_file():
        print(f"WebUI package is missing: {WEBUI_ROOT}", file=sys.stderr)
        return 2

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        print(
            f'Node.js/npm was not found. Install Node.js, then run: powershell -ExecutionPolicy Bypass -File "{ROOT / "setup_development_environment.ps1"}"',
            file=sys.stderr,
        )
        return 2

    preferred_port = args.port if args.port is not None else (5173 if args.development else 4174)
    try:
        command = frontend_command(npm, WEBUI_ROOT, development=args.development, port=preferred_port)
    except (FileNotFoundError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        port = resolve_port(preferred_port)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2

    stage119_root = runtime_paths["stage119_root"]
    runtime_environment: dict[str, str] = {}
    for key, value in (
        ("ENDFIELD_GAME_ROOT", runtime_paths["game_root"]),
        ("ENDFIELD_EXPORT_ROOT", runtime_paths["export_root"]),
        ("ENDFIELD_STAGE119_ROOT", stage119_root),
        ("ENDFIELD_MAP_LAYER_ROOT", stage119_root),
        ("ENDFIELD_WEBUI_OUTPUT_ROOT", runtime_paths["webui_output_root"]),
    ):
        if value is not None:
            runtime_environment[key] = str(value)
    if runtime_paths["blender_exe"] is not None:
        runtime_environment["ENDFIELD_BLENDER"] = str(runtime_paths["blender_exe"])
    if runtime_config.path is not None:
        runtime_environment["ENDFIELD_RUNTIME_CONFIG"] = str(runtime_config.path)
    os.environ.update(runtime_environment)
    environment = os.environ.copy()
    environment["ENDFIELD_BACKEND_URL"] = os.environ.get("ENDFIELD_BACKEND_URL", "")
    environment["ENDFIELD_PREVIEW_ASSET_ROOT"] = os.environ.get("ENDFIELD_PREVIEW_ASSET_ROOT", "")
    environment["VITE_ENDFIELD_GAME_ROOT"] = str(runtime_paths["game_root"] or "")
    environment["VITE_ENDFIELD_WEBUI_OUTPUT_ROOT"] = str(runtime_paths["webui_output_root"] or "")
    environment["VITE_ENDFIELD_CACHE_ROOT"] = str(runtime_paths["cache_root"] or "")
    environment["VITE_ENDFIELD_BLENDER"] = str(runtime_paths["blender_exe"] or "")

    pointer_root = Path(tempfile.gettempdir()) / "EndfieldExtractor"
    stage_pointer = pointer_root / f"stage-pointer-{os.getpid()}-{secrets.token_hex(6)}.json"
    write_stage_pointer(stage_pointer, stage119_root, runtime_config.path)
    environment["ENDFIELD_STAGE_POINTER_FILE"] = str(stage_pointer)

    def activate_vite_stage(generated_config: Path, generated_stage: Path) -> None:
        write_stage_pointer(stage_pointer, generated_stage, generated_config)

    defaults = {
        "character_config": str(character_config) if character_config is not None else None,
        "game_root": str(runtime_paths["game_root"]) if runtime_paths["game_root"] is not None else None,
        "export_root": str(runtime_paths["export_root"]) if runtime_paths["export_root"] is not None else None,
        "cache_root": str(runtime_paths["cache_root"]) if runtime_paths["cache_root"] is not None else None,
        "run_name": args.first_run_name,
    }
    if runtime_paths["blender_exe"] is not None:
        defaults["blender_exe"] = str(runtime_paths["blender_exe"])

    picker_server, picker_thread = start_path_picker(
        stage119_root,
        runtime_config.path,
        first_run_name=args.first_run_name,
        require_first_run_ready=args.defer_first_run_to_webui,
        first_run_defaults=defaults,  # type: ignore[arg-type]
        on_stage_ready=activate_vite_stage,
    )
    picker_host, picker_port = picker_server.server_address
    environment["ENDFIELD_PATH_PICKER_URL"] = f"http://{picker_host}:{picker_port}"
    environment["ENDFIELD_PATH_PICKER_TOKEN"] = picker_server.token

    url = f"http://127.0.0.1:{port}/"
    command = frontend_command(npm, WEBUI_ROOT, development=args.development, port=port)
    server_kind = "Vite development server" if args.development else "Vite preview server with local API middleware"
    if not args.development:
        print("Serving the built WebUI through Vite preview; local API middleware remains enabled.")
    print(f"Starting Endfield WebUI at {url}")
    if port != preferred_port:
        print(f"Port {preferred_port} was busy; using {port} instead.")
    print(f"Mode: {server_kind}.")
    if args.defer_first_run_to_webui:
        print("The WebUI will open first. Choose directories and extract required files in the setup panel.")
    print("Keep this server window open. Press Ctrl+C or close it to stop the WebUI.")

    process = subprocess.Popen(command, cwd=WEBUI_ROOT, env=environment)
    try:
        if wait_for_server(url, process):
            if not args.no_browser:
                webbrowser.open(url)
        else:
            print("The WebUI server did not become ready. Check the output above.", file=sys.stderr)
            return process.wait()
        return process.wait()
    except KeyboardInterrupt:
        print("\nStopping WebUI...")
        process.terminate()
        try:
            return process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()
    finally:
        picker_server.shutdown()
        picker_server.server_close()
        picker_thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
