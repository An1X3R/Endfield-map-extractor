"""Check character contracts and real durable files without running game readers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from webui_character_contract import (
    CharacterRequestConflict, CharacterRequestError, normalize_character_request,
    public_request, request_digest,
)
from webui_character_results import build_character_result
from webui_character_worker import CharacterCoordinator, CharacterJobCancelled


def export_request(job_id: str) -> dict[str, object]:
    return {"format": "CharacterExportRequest/1", "request_id": job_id, "catalog_id": "catalog",
            "source_identity": "identity", "selection_id": "character:test", "variant_id": "characters",
            "export_profile_id": "hgrp-preview", "output_label": "test"}


class CharacterContractTests(unittest.TestCase):
    def test_reference_selector_is_distinct_from_generic_action(self) -> None:
        request = export_request(str(uuid4()))
        reference = {"reference_id": "a" * 64, "maximum_stored_frames": 480}
        _, value = normalize_character_request({**request, "options": {"animation": reference}})
        self.assertEqual(value["options"]["animation"], reference)
        _, generic = normalize_character_request({**request, "options": {"animation": {
            "action_id": "a" * 64, "maximum_stored_frames": 480}}})
        self.assertNotEqual(request_digest(value), request_digest(generic))
        self.assertEqual(public_request(value)["options"]["animation"], reference)
        for animation in ({**reference, "action_id": "a" * 64}, {"maximum_stored_frames": 480},
                          {**reference, "reference_id": "bad"}, {**reference, "reference_id": "A" * 64}):
            with self.assertRaises(CharacterRequestError):
                normalize_character_request({**request, "options": {"animation": animation}})

    def test_exact_envelope_rejects_invalid_uuid_profile_and_label(self) -> None:
        request = export_request(str(uuid4()))
        kind, normalized = normalize_character_request({**request, "private_path": "secret"})
        self.assertEqual(kind, "export")
        self.assertEqual(normalized, request)
        for changes in ({"request_id": "bad"}, {"export_profile_id": "invented"}, {"output_label": "../escape"},
                        {"catalog_id": ""}, {"variant_id": None}):
            with self.assertRaises(CharacterRequestError):
                normalize_character_request({**request, **changes})

    def test_preparation_source_never_appears_in_public_request(self) -> None:
        request = {"format": "CharacterPreparationRequest/1", "request_id": str(uuid4()),
                   "source": {field: f"F:/private/{field}" for field in
                              ("game_root", "blender_exe", "cache_root", "output_root")}}
        kind, normalized = normalize_character_request(request)
        self.assertEqual(kind, "preparation")
        self.assertEqual(public_request(normalized), {"format": request["format"], "request_id": request["request_id"]})
        self.assertNotIn("private", json.dumps(public_request(normalized)))

    def test_manifest_verifies_real_files_and_rejects_traversal_hash_and_private_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = b"delivery fixture"
            (root / "character.blend").write_bytes(body)
            file = {"relative_path": "character.blend", "role": "character", "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest()}
            manifest = {"status": "completed", "files": [file], "private_source": "secret"}
            result = build_character_result(root, manifest, "export")
            self.assertEqual(result["files"], [file])
            self.assertNotIn("private_source", result)
            for changes in ({"relative_path": "../character.blend"}, {"relative_path": "F:\\secret.blend"},
                            {"bytes": 999}, {"sha256": "0" * 64}, {"role": "bundle"}):
                with self.assertRaises(ValueError):
                    build_character_result(root, {"status": "completed", "files": [{**file, **changes}]}, "export")
            with self.assertRaises(ValueError):
                build_character_result(root, {"status": "completed", "files": [file, file]}, "export")
            with self.assertRaises(ValueError):
                build_character_result(root, {"status": "completed", "files": []}, "export")
            self.assertEqual(build_character_result(root, {"status": "completed", "files": []}, "preparation")["files"], [])

    def test_manifest_cannot_claim_complete_with_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                build_character_result(Path(temporary), {"status": "completed", "files": [],
                    "pending": [{"code": "missing", "message": "Pending"}]}, "preparation")


class CharacterDurableTests(unittest.TestCase):
    def setUp(self) -> None:
        from endfield_character_service import CharacterService
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.worker = CharacterCoordinator(CharacterService(None), self.root)

    def store(self, state: str, session: str) -> str:
        job_id = str(uuid4())
        request = export_request(job_id)
        job = {"format": "CharacterJob/1", "job_id": job_id, "kind": "export", "state": state,
               "phase": "building", "progress": 0.4, "message": "Building", "cancel_requested": False,
               "result": None, "error": None, "private_path": "not-public"}
        record = {"session": session, "kind": "export", "request": request, "request_digest": request_digest(request),
                  "canonical": {**request, "private_path": "not-public", "context": {
                      "game_root": str(self.root.parent / (self.root.name + "_readonly_game")),
                      "output_root": str(self.root / "selected_outputs")}}, "diagnostic": "not-public",
                  "job": job, "events": []}
        path = self.root / "records" / f"{job_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return job_id

    def test_shutdown_cancels_owned_queued_thread_and_rejects_new_submissions(self) -> None:
        job_id = self.store("queued", self.worker._session)
        self.worker._capacity.acquire()
        self.worker._capacity.acquire()
        self.addCleanup(self.worker._capacity.release)
        self.addCleanup(self.worker._capacity.release)
        thread = threading.Thread(target=self.worker._run, args=(job_id,))
        self.worker._threads[job_id] = thread
        thread.start()
        self.worker.shutdown()
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.worker.get(job_id)["state"], "cancelled")
        self.assertEqual(self.worker._threads, {})
        self.assertFalse((self.root / "selected_outputs").exists())
        with self.assertRaisesRegex(CharacterRequestError, "closing"):
            self.worker.submit(export_request(str(uuid4())))

    def test_restart_marks_interrupted_and_pages_events_without_map_or_catalog(self) -> None:
        job_id = self.store("running", "old-session")
        job = self.worker.get(job_id)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "worker_interrupted")
        self.assertNotIn("not-public", json.dumps(job))
        page = self.worker.events(job_id, 0, 1)
        self.assertEqual(page["next_after"], 1)
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(self.worker.events(job_id, 1, 1)["events"], [])
        self.assertEqual(self.worker.cancel(job_id)["state"], "already_terminal")
        for after, limit in ((-1, 1), (2, 1), (0, 0), (0, 501)):
            with self.assertRaises(CharacterRequestError):
                self.worker.events(job_id, after, limit)

    def test_idempotency_works_when_current_service_is_unavailable(self) -> None:
        job_id = self.store("completed", "old-session")
        result = self.worker.submit(export_request(job_id))
        self.assertEqual(result["state"], "completed")
        with self.assertRaises(CharacterRequestConflict):
            self.worker.submit({**export_request(job_id), "output_label": "different"})
        with self.assertRaises(CharacterRequestError):
            self.worker.submit(export_request(str(uuid4())))
        self.assertEqual(len(list((self.root / "records").glob("*.json"))), 1)

    def test_cancel_queued_is_terminal_and_running_waits_for_cleanup(self) -> None:
        queued = self.store("queued", self.worker._session)
        self.worker.cancel(queued)
        self.assertEqual(self.worker.get(queued)["state"], "cancelled")
        running = self.store("running", self.worker._session)
        self.worker.cancel(running)
        self.assertTrue(self.worker.get(running)["cancel_requested"])
        self.assertEqual(self.worker.get(running)["state"], "running")

    def test_real_unconfigured_service_failure_keeps_incomplete_output(self) -> None:
        job_id = self.store("queued", self.worker._session)
        self.worker._run(job_id)
        job = self.worker.get(job_id)
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(job["result"])
        output = self.root / "selected_outputs/character_jobs/artifacts" / job_id
        self.assertTrue((output / "INCOMPLETE").is_file())
        self.assertFalse((output / "character_result.json").exists())
        self.assertFalse((self.root / "artifacts").exists())
        self.assertNotIn("not-public", json.dumps(job))

    def test_selected_output_is_independent_of_server_records_and_survives_reconnect(self) -> None:
        from endfield_character_service import CharacterService
        job_id = self.store("queued", self.worker._session)
        record = json.loads((self.root / "records" / f"{job_id}.json").read_text(encoding="utf-8"))
        fixed_records = self.root / "server_side_jobs"
        worker = CharacterCoordinator(CharacterService(None), fixed_records)
        record["session"] = worker._session
        path = fixed_records / "records" / f"{job_id}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        worker._run(job_id)
        expected = self.root / "selected_outputs/character_jobs/artifacts" / job_id
        self.assertTrue((expected / "INCOMPLETE").exists())
        self.assertTrue(path.exists())
        self.assertFalse((fixed_records / "artifacts").exists())
        restored = CharacterCoordinator(CharacterService(None), fixed_records)
        stored = restored._load(job_id)
        self.assertEqual(restored._output_path(job_id, stored), expected.resolve())
        self.assertEqual(restored.get(job_id)["state"], "failed")

    def test_validation_rejects_job_root_overlapping_game_without_writing(self) -> None:
        from endfield_character_service import CharacterService, _PATH_DEFAULTS, _VALUE_DEFAULTS
        game = self.root / "game"
        (game / "Endfield_Data/StreamingAssets/VFS").mkdir(parents=True)
        (game / "Endfield.exe").write_bytes(b"")
        tool = self.root / "blender.exe"
        tool.write_bytes(b"")
        config_path = self.root / "character.json"
        config_path.write_text(json.dumps({"format": "EndfieldCharacterRuntime/1", "exporter": str(tool),
            "state_root": str(self.root / "state"), "export_defaults": {
                **{field: str(tool) for field in _PATH_DEFAULTS}, **{field: "test" for field in _VALUE_DEFAULTS}}}), encoding="utf-8")
        service = CharacterService(config_path)
        request = {"format": "CharacterPreparationRequest/1", "request_id": str(uuid4()),
                   "source": {"game_root": str(game), "blender_exe": str(tool),
                              "cache_root": str(self.root / "cache"), "output_root": str(self.root / "outputs")}}
        for root in (game / "jobs", self.root):
            worker = CharacterCoordinator(service, root)
            with self.assertRaisesRegex(CharacterRequestError, "overlap"):
                worker.validate(request)
            self.assertFalse((root / "records").exists())
        allowed = self.root / "jobs"
        validation = CharacterCoordinator(service, allowed).validate(request)
        self.assertEqual(validation["status"], "valid")
        self.assertNotIn(str(game), json.dumps(validation))
        self.assertFalse(allowed.exists())

    def test_launcher_keeps_character_config_out_of_first_run_public_defaults(self) -> None:
        from launch_webui import start_path_picker
        config = self.root / "private_character_runtime.json"
        config.write_text(json.dumps({"format": "EndfieldCharacterRuntime/1", "state_root": "state"}), encoding="utf-8")
        defaults = {"character_config": str(config), "export_root": str(self.root / "exports")}
        server, thread = start_path_picker(None, first_run_defaults=defaults)
        try:
            public = server.first_run.status()
            self.assertNotIn("character_config", public["defaults"])
            self.assertNotIn(str(config), json.dumps(public))
            self.assertEqual(defaults["character_config"], str(config))
            self.assertEqual(server.character.service.config_path, config.resolve())
            self.assertEqual(server.character.root, (self.root / "exports/character_jobs").resolve())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cancel_and_final_commit_coordinate_on_real_artifacts(self) -> None:
        for cancelled in (True, False):
            job_id = self.store("running", self.worker._session)
            output = self.root / "selected_outputs/character_jobs/artifacts" / job_id
            output.mkdir(parents=True)
            (output / "INCOMPLETE").write_text(job_id, encoding="ascii")
            body = b"verified character artifact"
            (output / "character.blend").write_bytes(body)
            result = build_character_result(output, {"status": "completed", "files": [{"relative_path": "character.blend",
                "role": "character", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}]}, "export")
            if cancelled:
                self.worker.cancel(job_id)
                with self.assertRaises(CharacterJobCancelled):
                    self.worker._complete(job_id, output, result)
                self.assertTrue((output / "INCOMPLETE").exists())
                self.assertFalse((output / "character_result.json").exists())
                self.assertIsNone(self.worker.get(job_id)["result"])
            else:
                self.worker._complete(job_id, output, result)
                self.assertEqual(self.worker.get(job_id)["state"], "completed")
                self.assertEqual(json.loads((output / "character_result.json").read_text(encoding="utf-8")), result)
                self.assertFalse((output / "INCOMPLETE").exists())
                self.assertEqual(self.worker.cancel(job_id)["state"], "already_terminal")

    def test_full_error_is_private_and_public_progress_is_monotonic(self) -> None:
        job_id = self.store("running", self.worker._session)
        self.worker._progress(job_id, "extracting", 0.8, "F:/private/tool/output")
        self.worker._progress(job_id, "building", 0.2, "F:/private/tool/output")
        self.assertEqual(self.worker.get(job_id)["progress"], 0.8)
        self.worker._failure(job_id, RuntimeError("F:/private/native/evidence.dll"))
        public = self.worker.get(job_id)
        events = self.worker.events(job_id, 0, 500)
        self.assertNotIn("F:/private", json.dumps([public, events]))
        record = json.loads((self.root / "records" / f"{job_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(record["diagnostic"]["message"], "F:/private/native/evidence.dll")

    def test_http_token_routes_and_existing_job_reconnect_without_map_ready(self) -> None:
        from launch_webui import PathPickerHandler, PathPickerServer
        server = PathPickerServer(("127.0.0.1", 0), PathPickerHandler)
        server.token = "character-contract-test-token"
        server.character = self.worker
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        origin = f"http://{host}:{port}"
        try:
            with self.assertRaises(HTTPError) as denied:
                urlopen(origin + "/characters", timeout=5)
            self.assertEqual(denied.exception.code, 404)
            job_id = self.store("completed", "old-session")
            headers = {"X-Endfield-Path-Picker-Token": server.token, "Content-Type": "application/json"}
            with urlopen(Request(origin + f"/character-jobs/{job_id}", headers=headers), timeout=5) as response:
                job = json.load(response)
            self.assertEqual(job["job_id"], job_id)
            self.assertNotIn("not-public", json.dumps(job))
            with urlopen(Request(origin + "/characters", headers=headers), timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIsInstance(json.load(response), dict)
            data = json.dumps(export_request(job_id)).encode()
            with urlopen(Request(origin + "/character-jobs", data=data, headers=headers), timeout=5) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(json.load(response)["job_id"], job_id)
            with self.assertRaises(HTTPError) as conflict:
                urlopen(Request(origin + "/character-jobs", data=json.dumps({**export_request(job_id),
                    "output_label": "different"}).encode(), headers=headers), timeout=5)
            self.assertEqual(conflict.exception.code, 409)
            with self.assertRaises(HTTPError) as wrong_route:
                urlopen(Request(origin + f"/character-preparation/{job_id}", headers=headers), timeout=5)
            self.assertEqual(wrong_route.exception.code, 404)
            with self.assertRaises(HTTPError) as unavailable:
                urlopen(Request(origin + "/character-jobs", data=json.dumps(export_request(str(uuid4()))).encode(),
                                headers=headers), timeout=5)
            public_error = unavailable.exception.read().decode()
            self.assertNotIn("Traceback", public_error)
            self.assertNotIn("F:\\", public_error)
            preparation = {"format": "CharacterPreparationRequest/1", "request_id": str(uuid4()),
                           "source": {field: "F:/private/unconfigured" for field in
                                      ("game_root", "blender_exe", "cache_root", "output_root")}}
            with self.assertRaises(HTTPError) as invalid_preparation:
                urlopen(Request(origin + "/character-preparation/validate", data=json.dumps(preparation).encode(),
                                headers=headers), timeout=5)
            self.assertEqual(invalid_preparation.exception.code, 400)
            self.assertNotIn("F:/private", invalid_preparation.exception.read().decode())
            queued = self.store("queued", self.worker._session)
            with urlopen(Request(origin + f"/character-jobs/{queued}/cancel", data=b"{}", headers=headers), timeout=5) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(json.load(response)["state"], "accepted")
            with urlopen(Request(origin + f"/character-jobs/{queued}/events?after=0&limit=1", headers=headers), timeout=5) as response:
                events = json.load(response)
            self.assertEqual(events["events"][0]["state"], "cancelled")
            self.assertEqual(events["next_after"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
