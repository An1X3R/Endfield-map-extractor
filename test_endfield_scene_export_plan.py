"""Portable scene-plan smoke tests with real on-disk first-run-shaped inputs."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from endfield_scene_export_plan import _merge_mesh_exports, load_scene_export_plan, prepare_scene_export_plan
from webui_source_contract import selected_static_categories


class PortableScenePlanTests(unittest.TestCase):
    def test_selected_static_categories_respect_independent_layers(self) -> None:
        layers = {"instances": True, "roads": False, "vegetation": True, "terrain": False}
        self.assertEqual(selected_static_categories(layers, ["all"]),
                         frozenset({"building", "prop", "unknown", "vegetation"}))
        self.assertEqual(selected_static_categories(layers, ["road", "building", "vegetation"]),
                         frozenset({"building", "vegetation"}))
        self.assertEqual(selected_static_categories({**layers, "instances": False, "roads": True}, ["road"]),
                         frozenset({"road"}))
        with self.assertRaisesRegex(ValueError, "layers.roads must be boolean"):
            selected_static_categories({**layers, "roads": 1}, ["all"])

    def test_shared_mesh_reference_requires_identical_obj_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.obj"
            second = root / "second.obj"
            first.write_bytes(b"v 0 0 0\n")
            second.write_bytes(b"v 0 0 0\n")
            asset = {"Source": "CAB-shared", "PathId": 7, "Name": "mesh"}
            key = ("cab-shared", 7)
            primary = {key: (asset, first)}
            self.assertEqual(_merge_mesh_exports(primary, {key: (asset, second)}), primary)
            self.assertEqual(primary[key][1], first)
            second.write_bytes(b"v 1 0 0\n")
            with self.assertRaisesRegex(ValueError, "conflicting OBJ content"):
                _merge_mesh_exports(primary, {key: (asset, second)})

    def test_configured_roots_and_empty_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Endfield_Data" / "StreamingAssets" / "VFS").mkdir(parents=True)
            cache = root / "cache"
            cache.mkdir()
            export = root / "export"
            export.mkdir()
            selected_export = root / "selected_export"
            selected_export.mkdir()
            instances = cache / "instances.sqlite"
            with closing(sqlite3.connect(instances)) as database:
                with database:
                    database.execute(
                        "CREATE TABLE instances (entity_id INTEGER,entity_base TEXT,matrix_type INTEGER,"
                        "matrix_col_major BLOB,source_path TEXT,group_index INTEGER,entity_index INTEGER,"
                        "x REAL,z REAL)"
                    )
            for name in ("assets.sqlite", "resources.sqlite", "bundles.sqlite", "paths.bin"):
                (cache / name).write_bytes(b"")
            manifest = cache / "scene_manifest.json"
            manifest.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Containers": [],
                                            "MeshExports": [], "Materials": [], "TextureExports": []}), encoding="utf-8")
            config = cache / "runtime_config.json"
            config.write_text(json.dumps({
                "format": "EndfieldRuntimeConfig/1",
                "paths": {"game_root": "../game", "cache_root": ".", "export_root": "../export"},
                "extraction": {"resourceDatabase": "resources.sqlite",
                               "bundleScanIndex": "bundles.sqlite",
                               "maps": {"map01": {"instances": "instances.sqlite", "assets": "assets.sqlite",
                                                  "sceneProbe": {"path": "scene_manifest.json"}, "terrainSurfaces": []}}},
            }), encoding="utf-8")
            (cache / "first_run_state.json").write_text(json.dumps({
                "steps": {"bootstrap_metadata": {"outputs": {"main_paths": {"path": "paths.bin"}}}}
            }), encoding="utf-8")
            canonical = {"format": "EndfieldCanonicalExportJob/2", "map_id": "map01",
                         "bounds": {"xmin": 0, "xmax": 32, "zmin": 0, "zmax": 32},
                         "output": {"root": str(selected_export)},
                         "export_groups": ["vegetation"],
                         "layers": {"instances": True, "terrain": False}}
            output = selected_export / "plan.json"
            actual = load_scene_export_plan(prepare_scene_export_plan(config, canonical, output, None))
            self.assertEqual(actual["instances"], [])
            self.assertEqual(actual["pending"], [])
            self.assertEqual(actual["summary"]["ready_decals"], 0)
            self.assertEqual(json.loads(Path(actual["decal_manifest"]).read_text())["records"], [])
            with self.assertRaisesRegex(ValueError, "Plan output must be new"):
                prepare_scene_export_plan(config, canonical, output, None)

    def test_loader_rejects_missing_exact_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sidecar = root / "sidecar.json"
            sidecar.write_text("{}", encoding="utf-8")
            mesh = root / "mesh.obj"
            mesh.write_text("o test\n", encoding="utf-8")
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "format": "EndfieldPortableScenePlan/1", "map_id": "map01",
                "bounds": {"xmin": 0, "xmax": 1, "zmin": 0, "zmax": 1},
                "scene_manifests": [], "terrain_surfaces": [], "category_filter": ["road"],
                "decal_manifest": str(sidecar), "decal_materials": str(sidecar),
                "source_sha256": {}, "pending": [],
                "summary": {"selected_static": 1, "ready_static": 1, "selected_decals": 0,
                            "ready_decals": 0, "pending": 0},
                "instances": [{"entity_id": 1, "entity_base": "P_road", "category": "road",
                               "source_path": "InitChunkData_1", "source_sha256": "0" * 64,
                               "group_index": 0, "entity_index": 0, "mesh_path": "asset.fbx##mesh",
                               "mesh_file": str(mesh), "mesh_asset": {"Source": "cab", "PathId": 1, "Name": "mesh"},
                               "matrix": [1.0] * 16, "renderer_pairs": [], "submesh_slot_indices": [0],
                               "material_refs": [None], "undrawn_submeshes": [0]}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source hash mismatch"):
                load_scene_export_plan(plan)


if __name__ == "__main__":
    unittest.main()
