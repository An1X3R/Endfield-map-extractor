from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from endfield_map_layer_contract import (
    RuntimeResolver,
    convex_hull,
    ensure_new_output_root,
    sector_for_point,
    sector_key,
    sector_keys_for_bounds,
    sha256_file,
    validate_browser_safe_path_ids,
    validate_dataset_index,
    validate_layer_record,
    write_jsonl_gzip,
)


class MapLayerContractTests(unittest.TestCase):
    def test_sector_formula_handles_negative_and_boundaries(self) -> None:
        self.assertEqual(sector_for_point(-0.001, -128.001), (-1, -2))
        self.assertEqual(sector_for_point(0.0, 127.999), (0, 0))
        self.assertEqual(sector_for_point(128.0, 128.0), (1, 1))
        self.assertEqual(sector_key(-1, 2), "sector:-1:2")

    def test_bounds_use_half_open_sector_coverage(self) -> None:
        self.assertEqual(
            sector_keys_for_bounds({"xmin": 0, "xmax": 128, "zmin": 0, "zmax": 128}),
            ["sector:0:0"],
        )
        self.assertEqual(
            sector_keys_for_bounds({"xmin": 127, "xmax": 129, "zmin": 0, "zmax": 1}),
            ["sector:0:0", "sector:1:0"],
        )

    def test_convex_hull_is_closed_and_deterministic(self) -> None:
        points = [(1, 1), (0, 0), (1, 0), (0, 1), (0.5, 0.5), (1, 1)]
        self.assertEqual(convex_hull(points), [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]])

    def test_runtime_resolver_matches_webui_sector_key(self) -> None:
        resolver = RuntimeResolver(
            {
                "format": "EndfieldRegionMapRuntimeManifest/1",
                "resolverVersion": "endfield-sector128-v1",
                "maps": {
                    "map01": {
                        "domainId": "domain_1",
                        "worldBounds": {"xmin": -128, "xmax": 128, "zmin": -128, "zmax": 128},
                        "ownershipGrid": [["map01_lv001", None], [None, "map01_lv001"]],
                        "levels": [{"idNum": 1, "levelId": "map01_lv001"}],
                    }
                },
            }
        )
        resolved = resolver.resolve_point("map01", 0, 0)
        self.assertEqual(resolved.key, "sector:0:0")
        self.assertEqual(resolved.level_id, "map01_lv001")
        self.assertEqual(resolver.unity_to_blender_position((2, 3, 4)), {"x": 2.0, "y": -4.0, "z": 3.0})
        self.assertEqual(resolver.unity_to_blender_direction((0, -1, 0)), {"x": 0.0, "y": -0.0, "z": -1.0})

    def test_deterministic_gzip_has_identical_hash(self) -> None:
        rows = [{"z": 2, "a": 1}, {"name": "武陵"}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.jsonl.gz"
            right = root / "right.jsonl.gz"
            self.assertEqual(write_jsonl_gzip(left, rows)[0], 2)
            self.assertEqual(write_jsonl_gzip(right, rows)[0], 2)
            self.assertEqual(sha256_file(left), sha256_file(right))
            self.assertEqual(left.read_bytes(), right.read_bytes())

    def test_output_guard_requires_new_export_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_root = root / "exports"
            export_root.mkdir()
            game_root = root / "game"
            game_root.mkdir()
            with patch.dict(
                os.environ,
                {"ENDFIELD_EXPORT_ROOT": str(export_root), "ENDFIELD_GAME_ROOT": str(game_root)},
                clear=False,
            ):
                created = ensure_new_output_root(export_root / "new-build")
                self.assertTrue(created.is_dir())
                with self.assertRaises(FileExistsError):
                    ensure_new_output_root(created)
                with self.assertRaises(ValueError):
                    ensure_new_output_root(root / "outside")

    def test_record_validator_rejects_sector_key_mismatch(self) -> None:
        valid = {
            "recordType": "instance",
            "instanceId": "i",
            "assetId": "a",
            "mapId": "map01",
            "positionUnity": {"x": 0, "y": 0, "z": 0},
            "anchorSector": {"sectorX": 0, "sectorZ": 0, "key": "sector:0:0"},
            "sectorKeys": ["sector:0:0"],
            "category": "prop",
            "bindingStatus": "mapped",
        }
        validate_layer_record(valid)
        invalid = json.loads(json.dumps(valid))
        invalid["anchorSector"]["key"] = "sector:1:0"
        with self.assertRaises(ValueError):
            validate_layer_record(invalid)

    def test_path_ids_must_be_decimal_strings(self) -> None:
        value = {
            "rootAsset": {"pathId": "-901356662582800350"},
            "members": [{"transformPathId": "9007199254740993", "parentTransformPathId": None}],
        }
        validate_browser_safe_path_ids(value)
        invalid = json.loads(json.dumps(value))
        invalid["members"][0]["transformPathId"] = 9007199254740993
        with self.assertRaises(ValueError):
            validate_browser_safe_path_ids(invalid)

    def test_road_index_sector_keys_must_match_road_shards(self) -> None:
        index = {
            "format": "EndfieldMapLayerDataset/1",
            "datasetId": "map01.roads.stage119",
            "generatorVersion": "stage119-v1",
            "generatedAt": "2026-07-31T23:59:00+00:00",
            "mapId": "map01",
            "domainId": "domain_1",
            "layer": "roads",
            "recordType": "instance-view",
            "recordEncoding": "view-reference",
            "geometryTypes": ["point"],
            "scan": {"status": "partial", "coverageScope": "fixture", "recordsFound": 1, "limitations": []},
            "recordCount": 1,
            "coverageCount": 1,
            "duplicateCount": 0,
            "pendingCount": 0,
            "bindingCounts": {"mapped": 1},
            "viewOf": "map01/instances/index.json",
            "filter": {"category": "road"},
            "sectorKeys": ["sector:0:0"],
            "roadShards": [
                {
                    "sectorKey": "sector:0:0",
                    "path": "map01/instances/sectors/s+0000_+0000.jsonl.gz",
                    "sourceShardRecordCount": 2,
                    "roadRecordCount": 1,
                    "sourceShardSha256": "A" * 64,
                }
            ],
            "sourceEvidenceIds": [],
            "validation": {},
        }
        validate_dataset_index(index)
        index["sectorKeys"] = ["sector:1:0"]
        with self.assertRaises(ValueError):
            validate_dataset_index(index)


if __name__ == "__main__":
    unittest.main()
