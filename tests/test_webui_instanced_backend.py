"""Exercise scene request boundaries and durable task recovery on real files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import threading
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webui_instanced_contract import SceneRequestError, canonical_scene_request, scene_build_request
from webui_instanced_worker import InstancedSceneCoordinator, SceneJobCancelled


class InstancedBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.game = self.root / "game"
        (self.game / "Endfield_Data" / "StreamingAssets" / "VFS").mkdir(parents=True)
        (self.game / "Endfield.exe").write_bytes(b"")
        (self.game / "Endfield_Data" / "globalgamemanagers").write_bytes(b"")
        self.blender = self.root / "blender.exe"
        self.blender.write_bytes(b"")
        self.runtime = self.root / "runtime.json"
        self.runtime.write_text("{}", encoding="utf-8")
        self.paths = {"game_root": str(self.game), "cache_root": str(self.root / "cache")}
        self.request = {
            "format": "InstancedSceneExportRequest/1", "request_id": str(uuid4()),
            "scene_id": "indie_test", "catalog_id": "catalog", "first_run_identity": "identity",
            "export_mode": "blend", "export_groups": ["building", "lighting"],
            "effects_mode": "full_system_by_anchor", "source": {**self.paths, "blender_exe": str(self.blender)},
            "output": {"root": str(self.root / "exports"), "label": "integration"},
        }

    def canonical(self, value: dict[str, object]):
        return canonical_scene_request(value, "catalog", "identity", self.paths,
                                       self.root, frozenset({"indie_test"}))

    def status(self) -> dict[str, object]:
        return {"ready": True, "state": "completed", "request_identity": {"sha256": "identity"},
                "result": {"runtime_config": str(self.runtime)}}

    def test_request_rejects_stale_identity_and_game_output(self) -> None:
        with self.assertRaisesRegex(SceneRequestError, "身份"):
            self.canonical({**self.request, "first_run_identity": "stale"})
        with self.assertRaisesRegex(SceneRequestError, "重叠"):
            self.canonical({**self.request, "output": {"root": str(self.game / "export"), "label": "blocked"}})
        self.assertFalse((self.game / "export").exists())

    def test_request_rejects_cache_change_and_unimplemented_mode(self) -> None:
        source = {**self.request["source"], "cache_root": str(self.root / "other-cache")}
        with self.assertRaisesRegex(SceneRequestError, "首次准备"):
            self.canonical({**self.request, "source": source})
        with self.assertRaisesRegex(SceneRequestError, "尚未接入"):
            self.canonical({**self.request, "export_mode": "data_package"})

    def test_builder_adapter_preserves_physical_id_and_group_selection(self) -> None:
        request = self.canonical(self.request)
        bounds = {"xmin": -50.0, "xmax": 300.0, "zmin": 40.0, "zmax": 150.0}
        plan = scene_build_request(request, bounds)
        self.assertEqual(plan["map_id"], "indie_test")
        self.assertEqual(plan["bounds"], bounds)
        self.assertTrue(plan["layers"]["lights"])
        self.assertFalse(plan["layers"]["water"])

    def test_restart_reports_interrupted_job_and_preserves_event_identity(self) -> None:
        request = self.canonical(self.request)
        job_id = request["request_id"]
        path = self.root / "instanced_jobs" / (job_id + ".json")
        path.parent.mkdir()
        job = {"format": "InstancedSceneJob/1", "job_id": job_id, "state": "building",
               "phase": "building", "progress": 0.6, "message": "building", "cancel_requested": False,
               "request": request, "result": None, "error": None}
        path.write_text(json.dumps({"session": "old-process", "job": job, "events": []}), encoding="utf-8")
        coordinator = InstancedSceneCoordinator(self.status)
        restored = coordinator.get(job_id)
        self.assertEqual(restored["state"], "failed")
        self.assertEqual(restored["error"]["code"], "worker_interrupted")
        events = coordinator.events(job_id, 0)
        self.assertEqual(events["job_id"], job_id)
        self.assertEqual(events["next_after"], 1)
        self.assertEqual(coordinator.cancel(job_id)["state"], "already_terminal")
        self.assertEqual(coordinator.events(job_id, 1)["events"], [])

    def test_shutdown_cancels_real_helper_and_queued_job_and_rejects_submissions(self) -> None:
        from endfield_scene_export import _run_observed_scene_process

        coordinator = InstancedSceneCoordinator(self.status)
        self.addCleanup(coordinator.shutdown)
        jobs: list[str] = []
        for state in ("building", "queued"):
            request = self.canonical({**self.request, "request_id": str(uuid4())})
            job_id = request["request_id"]
            jobs.append(job_id)
            coordinator._save(coordinator._job_path(job_id), {
                "format": "InstancedSceneJob/1", "job_id": job_id, "state": state, "phase": state,
                "progress": 0.0, "message": state, "cancel_requested": False,
                "request": request, "result": None, "error": None,
            }, [])
        started = threading.Event()
        processes: list[subprocess.Popen[bytes]] = []
        errors: list[BaseException] = []

        def observe(process: subprocess.Popen[bytes]) -> None:
            if not processes:
                processes.append(process)
            started.set()

        def run_helper() -> None:
            try:
                with coordinator._execution_lock:
                    _run_observed_scene_process([sys.executable, "-c", "import time; time.sleep(60)"],
                        self.root / "helper.log", dict(os.environ), lambda: coordinator._check(jobs[0]), observe)
            except SceneJobCancelled:
                coordinator._update(jobs[0], {"state": "cancelled", "phase": "cancelled"})
            except BaseException as error:
                errors.append(error)
            finally:
                with coordinator._lock:
                    coordinator._threads.pop(jobs[0], None)

        running = threading.Thread(target=run_helper)
        queued = threading.Thread(target=coordinator._run, args=(jobs[1], self.runtime))
        coordinator._threads[jobs[0]] = running
        running.start()
        self.assertTrue(started.wait(5.0), "Real scene helper did not start")
        coordinator._threads[jobs[1]] = queued
        queued.start()
        coordinator.shutdown()
        self.assertFalse(running.is_alive())
        self.assertFalse(queued.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(coordinator._threads, {})
        for job_id in jobs:
            self.assertEqual(coordinator.get(job_id)["state"], "cancelled")
        self.assertFalse((self.root / "exports").exists())
        with self.assertRaisesRegex(SceneRequestError, "closing"):
            coordinator.submit({**self.request, "request_id": str(uuid4())})

    def test_explicit_fog_depth_survives_request_adapter(self) -> None:
        bounds = {"xmin": 0., "xmax": 128., "zmin": 0., "zmax": 128.}
        self.assertNotIn("fog_depth", scene_build_request(self.canonical(self.request), bounds))
        value = {**self.request, "export_groups": ["building", "effects"],
                 "fog_depth": {"subdivision_level": 4, "unused": "ignored"}}
        canonical = self.canonical(value)
        self.assertEqual(scene_build_request(canonical, bounds)["fog_depth"], {"subdivision_level": 4})
        self.assertEqual(value["fog_depth"], {"subdivision_level": 4, "unused": "ignored"})
        for invalid in (None, {}, {"subdivision_level": True}, {"subdivision_level": 7}):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, "fog_depth"):
                self.canonical({**value, "fog_depth": invalid})
        with self.assertRaisesRegex(SceneRequestError, "fog_depth"):
            self.canonical({**self.request, "fog_depth": {"subdivision_level": 4}})

    def test_explicit_water_transport_survives_independent_scene_adapter(self) -> None:
        value = {**self.request, "export_groups": ["water", "lighting"],
                 "water_transport_lighting": {
                     "map_id": "indie_test", "entity_id": 90982682,
                     "ambient_radiance": [.18, .22, .28], "radiance_scale": 1.25,
                 }}
        canonical = self.canonical(value)
        bounds = {"xmin": 0.0, "xmax": 128.0, "zmin": 0.0, "zmax": 128.0}
        plan = scene_build_request(canonical, bounds)
        self.assertEqual(plan["water_transport_lighting"], value["water_transport_lighting"])
        self.assertTrue(plan["layers"]["water"])
        self.assertTrue(plan["layers"]["lights"])
        with self.assertRaisesRegex(SceneRequestError, "map_id must match"):
            self.canonical({**value, "water_transport_lighting": {
                **value["water_transport_lighting"], "map_id": "map01"}})
        with self.assertRaisesRegex(SceneRequestError, "requires water and lighting"):
            self.canonical({**value, "export_groups": ["building", "lighting"]})

    def test_explicit_scene_color_snapshot_survives_independent_scene_adapter(self) -> None:
        bounds = {"xmin": 0.0, "xmax": 128.0, "zmin": 0.0, "zmax": 128.0}
        snapshot = {
            "format": "EndfieldSceneColorSnapshot/1",
            "camera_matrix_world": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
            "lens_mm": 35.0, "resolution": [320, 240], "engine": "BLENDER_EEVEE_NEXT",
            "samples": 8, "time_seconds": 0.0, "clip_start": 0.1, "clip_end": 200.0,
        }
        value = {**self.request, "export_groups": ["effects"], "scene_color_snapshot": snapshot}
        canonical = self.canonical(value)
        self.assertEqual(canonical["scene_color_snapshot"], snapshot)
        self.assertEqual(scene_build_request(canonical, bounds)["scene_color_snapshot"], snapshot)
        self.assertNotIn("scene_color_snapshot", scene_build_request(self.canonical(self.request), bounds))
        with self.assertRaisesRegex(SceneRequestError, "effects export group"):
            self.canonical({**value, "export_groups": ["building"]})
        with self.assertRaisesRegex(SceneRequestError, "clip distances"):
            self.canonical({**value, "scene_color_snapshot": {**snapshot, "clip_end": 0.1}})


if __name__ == "__main__":
    unittest.main()
