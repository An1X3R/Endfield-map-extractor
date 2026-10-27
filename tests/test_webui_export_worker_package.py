from __future__ import annotations

import gzip
import json
import tempfile
import time
import unittest
from pathlib import Path

from webui_export_worker import ExportCoordinator
from webui_job_store import JobStore


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
            fingerprint = "A" * 64
            (stage / "region_map_layer_manifest.json").write_text(json.dumps({
                "format": "EndfieldRegionMapLayerManifest/1", "datasetFingerprint": fingerprint,
                "maps": {"map01": {"layers": {"instances": {"indexPath": "instances.json"}}}},
            }), encoding="utf-8")
            (stage / "full_audit.json").write_text(json.dumps({"status": "passed", "datasetFingerprint": fingerprint}), encoding="utf-8")
            (stage / "instances.json").write_text(json.dumps({"shards": []}), encoding="utf-8")
            output.mkdir()
            coordinator = ExportCoordinator(stage119_root=stage, store=JobStore(jobs))

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

    def test_static_level_id_filters_package_instances_and_rejects_invalid_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            output = root / "output"
            jobs = root / "jobs"
            stage.mkdir()
            output.mkdir()
            fingerprint = "B" * 64
            (stage / "region_map_layer_manifest.json").write_text(json.dumps({
                "format": "EndfieldRegionMapLayerManifest/1", "datasetFingerprint": fingerprint,
                "maps": {"map02": {"layers": {"instances": {"indexPath": "instances.json"}}}},
            }), encoding="utf-8")
            (stage / "full_audit.json").write_text(
                json.dumps({"status": "passed", "datasetFingerprint": fingerprint}), encoding="utf-8")

            instance_dir = stage / "instances"
            instance_dir.mkdir()
            shard_path = instance_dir / "sector-0-0.jsonl.gz"
            selected_entity_id = (10 << 27) | 7
            excluded_entity_id = (9 << 27) | 11
            source_records = [
                {"instanceId": f"map02:instance:{selected_entity_id}", "category": "prop", "assetId": "selected"},
                {"instanceId": f"map02:instance:{excluded_entity_id}", "category": "prop", "assetId": "other-level"},
            ]
            with gzip.open(shard_path, "wt", encoding="utf-8") as stream:
                for record in source_records:
                    stream.write(json.dumps(record) + "\n")
            index = {
                "mapId": "map02",
                "shards": [{"sectorKey": "sector:0:0", "path": "instances/sector-0-0.jsonl.gz"}],
            }
            (stage / "instances.json").write_text(json.dumps(index), encoding="utf-8")

            coordinator = ExportCoordinator(stage119_root=stage, store=JobStore(jobs))
            request = self.request(output)
            request["map_id"] = "map02"
            request["static_level_id"] = "map02_lv010"
            request["layers"] = {
                "terrain": False, "instances": True, "roads": False, "vegetation": False,
                "water": False, "effects": False, "lights": False,
            }
            request["export_groups"] = ["prop"]

            validation = coordinator.validate(request)
            self.assertEqual(validation["canonical"]["static_level_id"], "map02_lv010")
            submitted = coordinator.submit(request)
            job_id = submitted["job_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                completed = coordinator.get(job_id)
                if completed["state"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            else:
                self.fail("level-scoped package export did not reach a terminal state")

            self.assertEqual(completed["state"], "completed", completed.get("error"))
            package = Path(completed["result"]["package"]["output_dir"])
            canonical = json.loads((package / "canonical_request.json").read_text(encoding="utf-8"))
            self.assertEqual(canonical["static_level_id"], "map02_lv010")
            with gzip.open(package / "layers" / "instances.jsonl.gz", "rt", encoding="utf-8") as stream:
                exported_records = [json.loads(line) for line in stream if line.strip()]
            self.assertEqual([record["assetId"] for record in exported_records], ["selected"])

            for index, invalid_record in enumerate((
                {"category": "prop"},
                {"instanceId": "map02:instance:not-an-integer", "category": "prop"},
            )):
                invalid_path = instance_dir / f"invalid-{index}.jsonl.gz"
                with gzip.open(invalid_path, "wt", encoding="utf-8") as stream:
                    stream.write(json.dumps(invalid_record) + "\n")
                invalid_index = {
                    "shards": [{"sectorKey": "sector:0:0", "path": f"instances/{invalid_path.name}"}],
                }
                with self.assertRaisesRegex(ValueError, "static_level_id.*instanceId"):
                    list(coordinator._selected_instance_records(
                        invalid_index, {"sector:0:0"}, {"prop"}, "invalid-instance-id", 10))
                self.assertEqual(
                    list(coordinator._selected_instance_records(
                        invalid_index, {"sector:0:0"}, {"prop"}, "unscoped-instance-id", None)),
                    [invalid_record],
                )


if __name__ == "__main__":
    unittest.main()
