from __future__ import annotations

from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import jsonschema
import launch_webui

from launch_webui import (
    PathPickerHandler,
    PathPickerServer,
    resolve_deferred_runtime_paths,
    validate_selected_path,
)
from webui_first_run_contract_v1 import (
    REQUEST_FORMAT,
    canonicalize_first_run_request,
    error_response,
    validate_first_run_paths,
)
from webui_first_run_coordinator import build_first_run_command
from webui_first_run_coordinator import FirstRunCoordinator


class WebUIFirstRunBackendTests(unittest.TestCase):
    def make_game_root(self, root: Path) -> Path:
        game = root / "Endfield Game"
        (game / "Endfield_Data" / "StreamingAssets" / "VFS").mkdir(parents=True)
        (game / "Endfield.exe").write_bytes(b"")
        (game / "Endfield_Data" / "globalgamemanagers").write_bytes(b"")
        return game

    def test_contract_validates_complete_external_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            export = root / "exports"
            export.mkdir()
            request = canonicalize_first_run_request(
                {
                    "format": REQUEST_FORMAT,
                    "game_root": str(game),
                    "export_root": str(export),
                    "run_name": "public_verify",
                }
            )
            validation = validate_first_run_paths(request)

            self.assertEqual("valid", validation["status"])
            self.assertTrue(validation["game_read_only"])
            self.assertNotIn("blender_exe", request)
            self.assertEqual(str(export / "cache" / "first_run_work"), request["cache_root"])
            self.assertIn("cache", validation["space"])
            self.assertIn("git_available", validation["prerequisites"])

    def test_optional_blender_path_is_checked_without_crossing_game_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            export = root / "exports"
            export.mkdir()
            blender = root / "tools" / "blender.exe"
            blender.parent.mkdir()
            blender.write_bytes(b"fixture executable")
            request = canonicalize_first_run_request(
                {
                    "format": REQUEST_FORMAT,
                    "game_root": str(game),
                    "export_root": str(export),
                    "blender_exe": str(blender),
                }
            )
            validation = validate_first_run_paths(request)

            self.assertEqual(str(blender.resolve()), request["blender_exe"])
            self.assertTrue(validation["game_read_only"])
            self.assertTrue(validation["checks"]["blender_exe"])
            self.assertEqual("valid", validation["status"])

            with self.assertRaisesRegex(ValueError, "non-empty path"):
                canonicalize_first_run_request(
                    {
                        "format": REQUEST_FORMAT,
                        "game_root": str(game),
                        "export_root": str(export),
                        "blender_exe": None,
                    }
                )
            with self.assertRaisesRegex(ValueError, "existing blender.exe"):
                canonicalize_first_run_request(
                    {
                        "format": REQUEST_FORMAT,
                        "game_root": str(game),
                        "export_root": str(export),
                        "blender_exe": str(root / "missing.exe"),
                    }
                )
            with self.assertRaisesRegex(ValueError, "existing blender.exe"):
                canonicalize_first_run_request(
                    {
                        "format": REQUEST_FORMAT,
                        "game_root": str(game),
                        "export_root": str(export),
                        "blender_exe": str(root / "tools" / "not-blender.exe"),
                    }
                )

            game_blender = game / "tools" / "blender.exe"
            game_blender.parent.mkdir()
            game_blender.write_bytes(b"fixture executable")
            with self.assertRaisesRegex(ValueError, "outside game_root"):
                canonicalize_first_run_request(
                    {
                        "format": REQUEST_FORMAT,
                        "game_root": str(game),
                        "export_root": str(export),
                        "blender_exe": str(game_blender),
                    }
                )

    def test_first_run_child_uses_external_cache_for_temp_and_pip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            export = root / "exports"
            export.mkdir()
            cache = root / "external-cache"
            request = canonicalize_first_run_request(
                {
                    "format": REQUEST_FORMAT,
                    "game_root": str(game),
                    "export_root": str(export),
                    "cache_root": str(cache),
                    "run_name": "process-env-check",
                }
            )
            coordinator = FirstRunCoordinator(
                extractor_root=root,
                interpreter_provider=lambda: Path(sys.executable),
            )
            report_path = root / "child-environment.json"
            code = (
                "import json,os,sys;"
                "keys=('TEMP','TMP','PIP_CACHE_DIR','PYTHONDONTWRITEBYTECODE');"
                "json.dump({key:os.environ.get(key) for key in keys},open(sys.argv[1],'w'))"
            )
            original_environment = {
                key: os.environ.get(key)
                for key in ("TEMP", "TMP", "PIP_CACHE_DIR", "PYTHONDONTWRITEBYTECODE")
            }
            with (root / "child.log").open("w", encoding="utf-8") as console:
                process = coordinator._spawn_process(
                    [sys.executable, "-c", code, str(report_path)],
                    console,
                    request=request,
                )
                self.assertEqual(0, process.wait(timeout=10))

            child_environment = json.loads(report_path.read_text(encoding="utf-8"))
            process_root = cache / "webui_first_run_process"
            self.assertEqual(str(process_root / "temp"), child_environment["TEMP"])
            self.assertEqual(child_environment["TEMP"], child_environment["TMP"])
            self.assertEqual(str(process_root / "pip"), child_environment["PIP_CACHE_DIR"])
            self.assertEqual("1", child_environment["PYTHONDONTWRITEBYTECODE"])
            self.assertTrue((process_root / "temp").is_dir())
            self.assertTrue((process_root / "pip").is_dir())
            self.assertFalse(Path(child_environment["TEMP"]).is_relative_to(game))
            self.assertFalse(Path(child_environment["PIP_CACHE_DIR"]).is_relative_to(game))
            self.assertEqual(
                original_environment,
                {key: os.environ.get(key) for key in original_environment},
            )

    def test_scene_input_capability_requires_complete_game_bound_sceneprobe_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            export = root / "exports"
            export.mkdir()
            cache = root / "external-cache"
            request = canonicalize_first_run_request(
                {
                    "format": REQUEST_FORMAT,
                    "game_root": str(game),
                    "export_root": str(export),
                    "cache_root": str(cache),
                    "run_name": "scene-input-check",
                }
            )
            coordinator = FirstRunCoordinator(
                extractor_root=root,
                interpreter_provider=lambda: Path(sys.executable),
            )
            paths = coordinator._paths(request)
            paths["run_root"].mkdir(parents=True)
            maps = {}
            for map_id in ("map01", "map02"):
                instances = paths["run_root"] / f"{map_id}.sqlite"
                sceneprobe = paths["run_root"] / f"{map_id}_sceneprobe.json"
                instances.write_bytes(b"read-only fixture")
                sceneprobe.write_text("{}", encoding="utf-8")
                maps[map_id] = {"instances": str(instances), "sceneProbe": {"path": str(sceneprobe)}}
            paths["runtime"].write_text(
                json.dumps(
                    {
                        "format": "EndfieldRuntimeConfig/1",
                        "paths": {"game_root": str(game)},
                        "extraction": {"maps": maps},
                    }
                ),
                encoding="utf-8",
            )

            completed = coordinator._result_summary(paths, {}, "completed", request, exit_code=0)
            partial = coordinator._result_summary(paths, {}, "completed_with_deferred_helpers", request, exit_code=0)
            failed_exit = coordinator._result_summary(paths, {}, "completed", request, exit_code=1)

            self.assertEqual("ready", completed["capabilities"]["blender_build"])
            self.assertEqual("not_ready", partial["capabilities"]["blender_build"])
            self.assertEqual("not_ready", failed_exit["capabilities"]["blender_build"])

    def test_deferred_launcher_paths_allow_a_fresh_unconfigured_clone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(
                runtime_config=None,
                game_root=None,
                export_root=None,
                cache_root=None,
                stage119_root=None,
                webui_output_root=None,
                blender_exe=None,
                first_run_name="fresh",
            )
            clean_environment = {
                "LOCALAPPDATA": str(Path(temp) / "LocalAppData"),
                "PATH": os.environ.get("PATH", ""),
            }
            with mock.patch.dict(os.environ, clean_environment, clear=True):
                paths, runtime = resolve_deferred_runtime_paths(args)
            self.assertIsNone(runtime.path)
            self.assertTrue(all(value is None for value in paths.values()))

    def test_output_and_cache_picker_validation_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = validate_selected_path("output", root)
            cache = validate_selected_path("cache", root / "new-cache")
            self.assertTrue(output["valid"])
            self.assertTrue(output["writable"])
            self.assertTrue(cache["valid"])
            self.assertFalse(cache["writable"])
            self.assertTrue(cache["writableParent"])

    def test_bridge_forwards_cache_picker_kind_without_overwriting_selection(self) -> None:
        server = PathPickerServer(("127.0.0.1", 0), PathPickerHandler)
        server.token = "test-token"
        server.first_run = mock.Mock()
        server.coordinator = mock.Mock()
        server.require_first_run_ready = True
        captured = []

        def fake_picker(kind, current):
            captured.append((kind, current))
            return {
                "format": "EndfieldPathPickerResponse/1",
                "status": "selected",
                "kind": kind,
                "path": current,
                "valid": True,
                "writableParent": True,
            }

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        request = Request(
            root + "/pick",
            data=json.dumps({"kind": "cache", "current": "X:\\cache"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Endfield-Path-Picker-Token": "test-token",
            },
            method="POST",
        )
        try:
            with mock.patch.object(launch_webui, "choose_native_path", side_effect=fake_picker):
                with urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(200, response.status)
            self.assertEqual("selected", payload["status"])
            self.assertEqual([("cache", "X:\\cache")], captured)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_contract_rejects_output_inside_game(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            with self.assertRaisesRegex(ValueError, "disjoint external roots"):
                canonicalize_first_run_request(
                    {
                        "format": REQUEST_FORMAT,
                        "game_root": str(game),
                        "export_root": str(game / "exports"),
                    }
                )

    def test_versioned_response_schemas_validate_backend_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            export = root / "exports"
            export.mkdir()
            request = canonicalize_first_run_request(
                {
                    "format": REQUEST_FORMAT,
                    "game_root": str(game),
                    "export_root": str(export),
                }
            )
            blender = root / "tools" / "blender.exe"
            blender.parent.mkdir()
            blender.write_bytes(b"fixture executable")
            blender_request = canonicalize_first_run_request(
                {
                    "format": REQUEST_FORMAT,
                    "game_root": str(game),
                    "export_root": str(export),
                    "blender_exe": str(blender),
                }
            )
            coordinator = FirstRunCoordinator(
                extractor_root=root,
                interpreter_provider=lambda: Path(sys.executable),
            )
            fixtures = (
                ("webui_first_run_validation_v1.schema.json", validate_first_run_paths(request)),
                ("webui_first_run_validation_v1.schema.json", validate_first_run_paths(blender_request)),
                ("webui_first_run_status_v1.schema.json", coordinator.status()),
                ("webui_first_run_events_v1.schema.json", coordinator.events()),
                (
                    "webui_first_run_error_v1.schema.json",
                    error_response("first_run_required", "Prepare data", retryable=True),
                ),
            )
            schema_root = Path(__file__).resolve().parent
            for schema_name, payload in fixtures:
                schema = json.loads(
                    (schema_root / schema_name).read_text(encoding="utf-8")
                )
                jsonschema.Draft202012Validator(schema).validate(payload)
            request_schema = json.loads(
                (schema_root / "webui_first_run_job_v1.schema.json").read_text(encoding="utf-8")
            )
            request_validator = jsonschema.Draft202012Validator(request_schema)
            base_request = {
                "format": REQUEST_FORMAT,
                "game_root": str(game),
                "export_root": str(export),
            }
            request_validator.validate(base_request)
            request_validator.validate(
                {**base_request, "blender_exe": str(root / "tools" / "blender.exe")}
            )
            with self.assertRaises(jsonschema.ValidationError):
                request_validator.validate({**base_request, "blender_exe": 17})

    def test_command_passes_optional_blender_without_changing_legacy_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base_request = {
                "game_root": str(root / "Endfield Game"),
                "cache_root": str(root / "cache"),
                "export_root": str(root / "exports"),
                "run_name": "bootstrap_v1",
            }
            python = root / "python.exe"
            script = root / "endfield_first_run.py"
            cancel_marker = root / "cancel.marker"
            legacy_command = build_first_run_command(
                python, script, base_request, cancel_marker=cancel_marker, resume=False
            )
            blender = root / "blender.exe"
            blender.write_bytes(b"fixture executable")
            command = build_first_run_command(
                python,
                script,
                {**base_request, "blender_exe": str(blender)},
                cancel_marker=cancel_marker,
                resume=True,
            )

            self.assertNotIn("--blender-exe", legacy_command)
            self.assertIn("--resume", command)
            self.assertIn("--no-activate-runtime-config", command)
            blender_index = command.index("--blender-exe")
            self.assertEqual(
                ["--blender-exe", str(blender)],
                command[blender_index:blender_index + 2],
            )
            self.assertNotIn("--skip-helper-bootstrap", command)
            self.assertNotIn("--skip-scene-discovery", command)

    def test_completed_state_restores_ready_gate_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            export = root / "exports"
            cache = root / "cache"
            run_name = "restored"
            run_root = cache / run_name
            stage_root = export / "cache" / f"map_layers_{run_name}"
            export.mkdir()
            run_root.mkdir(parents=True)
            stage_root.mkdir(parents=True)
            (run_root / "first_run_state.json").write_text(
                json.dumps({"format": "EndfieldFirstRunState/1", "status": "completed"}),
                encoding="utf-8",
            )
            (run_root / "endfield.runtime.local.json").write_text(
                json.dumps({"format": "EndfieldRuntimeConfig/1", "paths": {}}),
                encoding="utf-8",
            )
            activated = []
            coordinator = FirstRunCoordinator(
                extractor_root=root,
                interpreter_provider=lambda: Path("python"),
                initial_request={
                    "format": REQUEST_FORMAT,
                    "game_root": str(game),
                    "export_root": str(export),
                    "cache_root": str(cache),
                    "run_name": run_name,
                },
                on_ready=lambda runtime, stage: activated.append((runtime, stage)),
            )

            status = coordinator.status()
            self.assertTrue(status["ready"])
            self.assertEqual("completed", status["state"])
            self.assertEqual([(run_root / "endfield.runtime.local.json", stage_root)], activated)
            duplicate = coordinator.submit(
                {
                    "format": REQUEST_FORMAT,
                    "game_root": str(game),
                    "export_root": str(export),
                    "cache_root": str(cache),
                    "run_name": run_name,
                }
            )
            self.assertEqual("already_ready", duplicate["disposition"])

    def test_completed_state_uses_runtime_retry_stage_instead_of_base_guess(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            export = root / "exports"
            cache = root / "cache"
            run_name = "retry-stage"
            run_root = cache / run_name
            actual_stage = export / "cache" / f"map_layers_{run_name}.retry001"
            export.mkdir()
            run_root.mkdir(parents=True)
            actual_stage.mkdir(parents=True)
            (run_root / "first_run_state.json").write_text(
                json.dumps({"format": "EndfieldFirstRunState/1", "status": "completed"}),
                encoding="utf-8",
            )
            runtime_path = run_root / "endfield.runtime.local.json"
            runtime_path.write_text(
                json.dumps(
                    {
                        "format": "EndfieldRuntimeConfig/1",
                        "paths": {"stage119_root": str(actual_stage)},
                    }
                ),
                encoding="utf-8",
            )
            activated = []
            coordinator = FirstRunCoordinator(
                extractor_root=root,
                interpreter_provider=lambda: Path(sys.executable),
                initial_request={
                    "format": REQUEST_FORMAT,
                    "game_root": str(game),
                    "export_root": str(export),
                    "cache_root": str(cache),
                    "run_name": run_name,
                },
                on_ready=lambda runtime, stage: activated.append((runtime, stage)),
            )
            self.assertTrue(coordinator.status()["ready"])
            self.assertEqual(str(actual_stage), coordinator.status()["result"]["stage_root"])
            self.assertEqual([(runtime_path, actual_stage)], activated)

    def test_status_actions_cover_idle_partial_failed_cancelled_and_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            idle = FirstRunCoordinator(
                extractor_root=root,
                interpreter_provider=lambda: Path(sys.executable),
                initial_defaults={"run_name": "matrix"},
            ).status()
            self.assertEqual("idle", idle["state"])
            self.assertTrue(idle["actions"]["can_start"])
            self.assertFalse(idle["actions"]["can_resume"])
            self.assertEqual("phase", idle["progress_scope"])

            game = self.make_game_root(root)
            export = root / "exports"
            cache = root / "cache"
            export.mkdir()
            for source_status, expected_state in (
                ("running", "partial"),
                ("failed", "failed"),
                ("cancelled", "cancelled"),
            ):
                run_name = f"matrix-{source_status}"
                run_root = cache / run_name
                run_root.mkdir(parents=True)
                (run_root / "first_run_state.json").write_text(
                    json.dumps({"format": "EndfieldFirstRunState/1", "status": source_status}),
                    encoding="utf-8",
                )
                status = FirstRunCoordinator(
                    extractor_root=root,
                    interpreter_provider=lambda: Path(sys.executable),
                    initial_request={
                        "format": REQUEST_FORMAT,
                        "game_root": str(game),
                        "export_root": str(export),
                        "cache_root": str(cache),
                        "run_name": run_name,
                    },
                ).status()
                self.assertEqual(expected_state, status["state"])
                self.assertTrue(status["resume_available"])
                self.assertTrue(status["actions"]["can_resume"])

    def test_dependency_process_tree_can_be_cancelled_and_status_is_observable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            export = root / "exports"
            cache = root / "cache"
            export.mkdir()
            requirements = root / "requirements.txt"
            requirements.write_text("fixture\n", encoding="utf-8")
            pid_file = root / "child.pid"
            coordinator = FirstRunCoordinator(
                extractor_root=root,
                interpreter_provider=lambda: Path(sys.executable),
                requirements_path=requirements,
            )
            original_spawn = coordinator._spawn_process
            spawned: list[subprocess.Popen[str]] = []

            def spawn_long_tree(_command, console, *, request):
                code = (
                    "import pathlib,subprocess,sys,time;"
                    "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                    f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid));"
                    "time.sleep(60)"
                )
                process = original_spawn([sys.executable, "-c", code], console, request=request)
                spawned.append(process)
                return process

            with mock.patch.object(coordinator, "_spawn_process", side_effect=spawn_long_tree):
                coordinator.submit(
                    {
                        "format": REQUEST_FORMAT,
                        "game_root": str(game),
                        "export_root": str(export),
                        "cache_root": str(cache),
                        "run_name": "cancel-dependencies",
                    }
                )
                deadline = time.monotonic() + 10
                while not pid_file.is_file() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(pid_file.is_file())
                requested = coordinator.cancel()
                self.assertTrue(requested["cancel_requested"])
                self.assertFalse(requested["actions"]["can_cancel"])
                while coordinator._thread and coordinator._thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.05)

            self.assertTrue(spawned)
            self.assertIsNotNone(spawned[0].poll())
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            if os.name == "nt":
                tasklist = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {child_pid}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                child_alive = str(child_pid) in tasklist.stdout
            else:
                try:
                    os.kill(child_pid, 0)
                except OSError:
                    child_alive = False
                else:
                    child_alive = True
            self.assertFalse(child_alive)
            final = coordinator.status()
            self.assertEqual("cancelled", final["state"])
            self.assertFalse(final["cancel_requested"])
            self.assertTrue(final["error"]["retryable"])

    def test_resumed_event_sequences_are_paged_by_file_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            export = root / "exports"
            cache = root / "cache"
            run_name = "events"
            events_path = export / "log" / f"first_run_{run_name}" / "events.jsonl"
            export.mkdir()
            events_path.parent.mkdir(parents=True)
            rows = [
                {"format": "EndfieldFirstRunEvent/1", "sequence": sequence, "message": str(index)}
                for index, sequence in enumerate((1, 2, 1, 2), start=1)
            ]
            events_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            coordinator = FirstRunCoordinator(
                extractor_root=root,
                interpreter_provider=lambda: Path("python"),
                initial_request={
                    "format": REQUEST_FORMAT,
                    "game_root": str(game),
                    "export_root": str(export),
                    "cache_root": str(cache),
                    "run_name": run_name,
                },
            )

            page = coordinator.events(after_sequence=1, limit=2)
            self.assertEqual([2, 3], [event["sequence"] for event in page["events"]])
            self.assertEqual([2, 1], [event["source_sequence"] for event in page["events"]])
            self.assertEqual(3, page["next_after"])
            self.assertTrue(page["has_more"])

    def test_local_bridge_exposes_versioned_first_run_api_and_export_gate(self) -> None:
        class FakeFirstRun:
            ready = False

            def status(self):
                return {"format": "EndfieldWebUIFirstRunStatus/1", "state": "idle", "ready": self.ready}

            def events(self, after, _limit=250):
                return {"format": "EndfieldWebUIFirstRunEvents/1", "events": [{"sequence": after + 1}]}

            def validate(self, payload):
                return {"format": "EndfieldWebUIFirstRunValidation/1", "status": "valid", "canonical": payload}

            def submit(self, _payload):
                return {"format": "EndfieldWebUIFirstRunStatus/1", "state": "queued", "ready": False}

            def cancel(self):
                return {"format": "EndfieldWebUIFirstRunStatus/1", "state": "cancelled", "ready": False}

        class FakeExportCoordinator:
            def validate(self, payload):
                return {"status": "valid", "payload": payload}

            def submit(self, payload):
                return {"job_id": "job", "payload": payload}

        server = PathPickerServer(("127.0.0.1", 0), PathPickerHandler)
        server.token = "test-token"
        server.first_run = FakeFirstRun()  # type: ignore[assignment]
        server.coordinator = FakeExportCoordinator()  # type: ignore[assignment]
        server.require_first_run_ready = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"

        def request(path: str, payload=None):
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            value = Request(
                root + path,
                data=body,
                headers={
                    "X-Endfield-Path-Picker-Token": "test-token",
                    **({"Content-Type": "application/json"} if body is not None else {}),
                },
                method="POST" if body is not None else "GET",
            )
            with urlopen(value, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))

        try:
            status, payload = request("/first-run")
            self.assertEqual(200, status)
            self.assertEqual("EndfieldWebUIFirstRunStatus/1", payload["format"])
            status, payload = request("/first-run/events?after=4")
            self.assertEqual(5, payload["events"][0]["sequence"])
            status, payload = request("/first-run/validate", {"format": REQUEST_FORMAT})
            self.assertEqual(200, status)
            with self.assertRaises(HTTPError) as blocked:
                request("/jobs", {"format": "EndfieldWebUIExportJob/2"})
            self.assertEqual(409, blocked.exception.code)
            server.first_run.ready = True  # type: ignore[attr-defined]
            status, payload = request("/jobs", {"format": "EndfieldWebUIExportJob/2"})
            self.assertEqual(202, status)
            self.assertEqual("job", payload["job_id"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_real_bridge_initializes_with_complete_runtime_defaults(self) -> None:
        first_run_name = "public_update_material_regions_20260907_0001"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            game = self.make_game_root(root)
            export = root / "exports"
            cache = root / "cache"
            export.mkdir()
            cache.mkdir()
            defaults = {
                "game_root": str(game),
                "export_root": str(export),
                "cache_root": str(cache),
                "run_name": first_run_name,
            }
            original_defaults = dict(defaults)
            server, thread = launch_webui.start_path_picker(
                None,
                first_run_name=first_run_name,
                first_run_defaults=defaults,
            )
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/first-run",
                    headers={"X-Endfield-Path-Picker-Token": server.token},
                )
                with urlopen(request, timeout=5) as response:
                    status = json.loads(response.read().decode("utf-8"))
                self.assertEqual(200, response.status)
                self.assertEqual("idle", status["state"])
                self.assertEqual(first_run_name, status["defaults"]["run_name"])
                self.assertEqual(str(game.resolve()), status["defaults"]["game_root"])
                self.assertEqual(original_defaults, defaults)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
