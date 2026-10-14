from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webui_export_worker import ExportCoordinator
from webui_job_store import JobStore


class SceneJobQueueTests(unittest.TestCase):
    def test_shutdown_cancels_active_and_queued_jobs_and_rejects_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            game, blender, runtime = self.create_runtime(root)
            coordinator = ExportCoordinator(
                stage119_root=self.create_stage(root), store=JobStore(root / "jobs"),
                runtime_config=runtime,
            )
            entered = threading.Event()

            def wait_for_cancel() -> None:
                entered.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    job_id = coordinator._active_scene
                    if job_id is not None:
                        coordinator._check_cancelled(job_id)
                    time.sleep(0.01)
                raise TimeoutError("Shutdown did not cancel the active scene job")

            with patch.object(coordinator, "_load_dataset", side_effect=wait_for_cancel):
                first = coordinator.submit(self.scene_request(output, game, blender))
                self.assertTrue(entered.wait(5))
                second = coordinator.submit(self.scene_request(output, game, blender))
                dispatcher = coordinator._scene_dispatcher
                coordinator.shutdown()
            self.assertIsNotNone(dispatcher)
            self.assertFalse(dispatcher.is_alive())
            for job in (first, second):
                self.assertEqual(coordinator.get(str(job["job_id"]))["state"], "cancelled")
            self.assertEqual(list(output.iterdir()), [])
            with self.assertRaisesRegex(RuntimeError, "shutting down"):
                coordinator.submit(self.scene_request(output, game, blender))
            with self.assertRaisesRegex(RuntimeError, "shutting down"):
                coordinator.submit(self.package_request(output))
            coordinator.shutdown()

    @staticmethod
    def create_runtime(root: Path) -> tuple[Path, Path, Path]:
        game = root / "game"
        game.mkdir()
        (game / "Endfield.exe").write_bytes(b"")
        global_managers = game / "Endfield_Data" / "globalgamemanagers"
        global_managers.parent.mkdir()
        global_managers.write_bytes(b"")
        (game / "Endfield_Data" / "StreamingAssets" / "VFS").mkdir(parents=True)
        blender = root / "blender.exe"
        blender.write_bytes(b"")
        prepared = root / "prepared"
        prepared.mkdir()
        instances = prepared / "instances.json"
        instances.write_text("{}", encoding="utf-8")
        scene_manifest = prepared / "scene_manifest.json"
        scene_manifest.write_text("{}", encoding="utf-8")
        runtime = root / "runtime.json"
        runtime.write_text(json.dumps({
            "format": "EndfieldRuntimeConfig/1",
            "paths": {"game_root": str(game)},
            "extraction": {"maps": {"map01": {
                "instances": str(instances),
                "sceneProbe": {"path": str(scene_manifest)},
            }}},
        }), encoding="utf-8")
        return game, blender, runtime

    @staticmethod
    def scene_request(output: Path, game: Path, blender: Path) -> dict[str, object]:
        return {
            "format": "EndfieldWebUIExportJob/2",
            "map_id": "map01",
            "selection": {"type": "world_bounds", "xmin": 0, "xmax": 128, "zmin": 0, "zmax": 128},
            "layers": {"terrain": True, "instances": False, "roads": False,
                        "vegetation": False, "water": False, "effects": False, "lights": False},
            "export_groups": ["terrain"],
            "water_mode": "stable_eevee",
            "effects": {"selection_mode": "full_system_by_anchor"},
            "export_mode": "blend",
            "source": {"game_root": str(game), "blender_exe": str(blender)},
            "output": {"root": str(output), "label": "scene-queue"},
        }

    @staticmethod
    def package_request(output: Path) -> dict[str, object]:
        return {
            "format": "EndfieldWebUIExportJob/2",
            "map_id": "map01",
            "selection": {"type": "world_bounds", "xmin": 0, "xmax": 128, "zmin": 0, "zmax": 128},
            "layers": {"terrain": True, "instances": False, "roads": False,
                        "vegetation": False, "water": False, "effects": False, "lights": False},
            "export_groups": ["terrain"],
            "water_mode": "stable_eevee",
            "effects": {"selection_mode": "full_system_by_anchor"},
            "output": {"root": str(output), "label": "package"},
        }

    @staticmethod
    def create_stage(root: Path) -> Path:
        stage = root / "stage"
        stage.mkdir()
        fingerprint = "A" * 64
        (stage / "region_map_layer_manifest.json").write_text(json.dumps({
            "format": "EndfieldRegionMapLayerManifest/1",
            "datasetFingerprint": fingerprint,
            "maps": {"map01": {"layers": {"instances": {"indexPath": "instances.json"}}}},
        }), encoding="utf-8")
        (stage / "full_audit.json").write_text(json.dumps({
            "status": "passed", "datasetFingerprint": fingerprint,
        }), encoding="utf-8")
        (stage / "instances.json").write_text(json.dumps({"shards": []}), encoding="utf-8")
        return stage

    def test_scene_queue_is_fifo_cancellable_and_does_not_block_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            game, blender, runtime = self.create_runtime(root)
            stage = self.create_stage(root)
            coordinator = ExportCoordinator(
                stage119_root=stage,
                store=JobStore(root / "jobs"),
                runtime_config=runtime,
            )

            with coordinator._scene_condition:
                first = coordinator.submit(self.scene_request(output, game, blender))
                second = coordinator.submit(self.scene_request(output, game, blender))
                self.assertEqual(
                    list(coordinator._scene_queue),
                    [str(first["job_id"]), str(second["job_id"])],
                )
                package = coordinator.submit(self.package_request(output))
                self.assertEqual(package["state"], "queued")
                self.assertEqual(coordinator.cancel(str(first["job_id"]))["state"], "cancelled")
                self.assertEqual(coordinator.cancel(str(second["job_id"]))["state"], "cancelled")
                dispatcher = coordinator._scene_dispatcher

            if dispatcher is not None:
                dispatcher.join(timeout=5)
                self.assertFalse(dispatcher.is_alive())
            self.assertEqual(coordinator.get(str(first["job_id"]))["state"], "cancelled")
            self.assertEqual(coordinator.get(str(second["job_id"]))["state"], "cancelled")
            self.assertEqual(list(output.glob("scene-queue__*")), [])

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                completed = coordinator.get(str(package["job_id"]))
                if completed["state"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            else:
                self.fail("data-package export did not finish while scene jobs were queued")
            self.assertEqual(completed["state"], "completed", completed.get("error"))
            self.assertEqual(completed["result"]["package"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
