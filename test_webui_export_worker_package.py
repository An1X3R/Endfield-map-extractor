from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from webui_export_worker import ExportCoordinator
from webui_job_store import JobStore


class PackageOnlyCoordinator(ExportCoordinator):
    def _load_dataset(self):
        return {
            "format": "EndfieldRegionMapLayerManifest/1",
            "datasetFingerprint": "A" * 64,
            "maps": {"map01": {"layers": {}}},
        }, {}


class PackageOnlyExportTests(unittest.TestCase):
    def request(self, output: Path) -> dict:
        return {
            "format": "EndfieldWebUIExportJob/2",
            "map_id": "map01",
            "selection": {
                "type": "world_bounds",
                "xmin": 0,
                "xmax": 128,
                "zmin": 0,
                "zmax": 128,
            },
            "layers": {
                "terrain": True,
                "instances": False,
                "water": False,
                "effects": False,
            },
            "export_groups": ["terrain"],
            "water_mode": "stable_eevee",
            "effects": {"selection_mode": "full_system_by_anchor"},
            "output": {"root": str(output), "label": "package-only"},
        }

    def test_job_completes_without_profiles_or_blender(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            output = root / "output"
            jobs = root / "jobs"
            stage.mkdir()
            output.mkdir()
            coordinator = PackageOnlyCoordinator(stage119_root=stage, store=JobStore(jobs))

            validation = coordinator.validate(self.request(output))
            self.assertEqual(validation["exportMode"], "data_package")
            self.assertEqual(validation["capabilities"]["data_package"], "ready")
            self.assertEqual(validation["blenderBuild"]["status"], "not_ready")

            submitted = coordinator.submit(self.request(output))
            job_id = submitted["job_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                completed = coordinator.get(job_id)
                if completed["state"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            else:
                self.fail("package-only export did not reach a terminal state")

            self.assertEqual(completed["state"], "completed", completed.get("error"))
            self.assertEqual(completed["result"]["package"]["status"], "completed")
            self.assertEqual(completed["result"]["blenderBuild"]["status"], "not_ready")
            package = Path(completed["result"]["package"]["output_dir"])
            manifest = json.loads((package / "export_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["exportMode"], "data_package")
            self.assertEqual(manifest["blenderBuild"]["status"], "not_ready")
            self.assertFalse(any(package.glob("blender_worker_*")))


if __name__ == "__main__":
    unittest.main()
