from __future__ import annotations

import hashlib
import gzip
import json
import os
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audit_endfield_map_layers import audit
from build_endfield_map_layers import BuildConfig, build_all


FIXED_TIME = "2026-07-31T23:59:00+00:00"
BIG_PATH_ID = 9007199254740993
NEGATIVE_BIG_PATH_ID = -901356662582800350


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def create_instance_database(path: Path, map_id: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE instances (
            entity_id INTEGER PRIMARY KEY, name TEXT, entity_base TEXT,
            matrix_type INTEGER NOT NULL, matrix_col_major BLOB NOT NULL,
            x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL,
            source_path TEXT NOT NULL, group_index INTEGER NOT NULL,
            entity_index INTEGER NOT NULL
        );
        CREATE TABLE duplicates (entity_id INTEGER NOT NULL, source_path TEXT NOT NULL, same_payload INTEGER NOT NULL);
        """
    )
    matrix = struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
    rows = [
        (1, "road_a", "road_segment", -100.0, 0.0, -100.0),
        (2, "tree_a", "tree_oak", 0.0, 0.0, 0.0),
        (3, "building_a", "building_wall", 10.0, 0.0, 10.0),
        (4, "outside_ui", "prop_crate", 200.0, 0.0, 200.0),
        (5, "machine_a", "prop_machine", 20.0, 0.0, 20.0),
    ]
    for entity_id, name, entity_base, x, y, z in rows:
        connection.execute(
            "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                entity_id,
                name,
                entity_base,
                1,
                matrix,
                x,
                y,
                z,
                f"Assets/Beyond/Scenes/{map_id}/lv001/chunk_{entity_id}",
                0,
                entity_id - 1,
            ),
        )
    connection.commit()
    connection.close()


def create_asset_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE resolutions (
            entity_base TEXT PRIMARY KEY, instance_count INTEGER NOT NULL,
            method TEXT NOT NULL, container_path TEXT, logical_name TEXT,
            root_cab TEXT, model_asset_name TEXT
        )
        """
    )
    methods = {
        "road_segment": "model",
        "tree_oak": "unresolved",
        "building_wall": "report_only_known_proxy_or_nonvisible_renderer",
        "prop_crate": "prefab",
    }
    for entity_base, method in methods.items():
        connection.execute(
            "INSERT INTO resolutions VALUES (?,?,?,?,?,?,?)",
            (entity_base, 1, method, f"assets/{entity_base}.prefab", entity_base, "CAB-test", entity_base),
        )
    connection.commit()
    connection.close()


def prototype(name: str) -> dict[str, object]:
    return {
        "name": name,
        "missing": False,
        "container": f"assets/{name}.prefab",
        "root_asset": {
            "Source": "CAB-fx",
            "PathId": NEGATIVE_BIG_PATH_ID,
            "Type": "GameObject",
            "Name": name,
        },
        "nodes": [
            {
                "name": name,
                "source": "CAB-fx",
                "path_id": BIG_PATH_ID,
                "transform_path_id": BIG_PATH_ID + 10,
                "parent": {"Source": "CAB-fx", "PathId": 0, "Type": "Transform"},
                "local_position": [0, 0, 0],
                "local_rotation": [0, 0, 0, 1],
                "local_scale": [1, 1, 1],
                "renderer_type": None,
                "mesh": None,
                "materials": [],
                "particle_systems": [],
                "particle_renderers": [],
            },
            {
                "name": "Emitter",
                "source": "CAB-fx",
                "path_id": NEGATIVE_BIG_PATH_ID,
                "transform_path_id": BIG_PATH_ID + 11,
                "parent": {"Source": "CAB-fx", "PathId": BIG_PATH_ID + 10, "Type": "Transform"},
                "local_position": [1, 2, 3],
                "local_rotation": [0, 0, 0, 1],
                "local_scale": [1, 1, 1],
                "renderer_type": "ParticleSystemRenderer",
                "mesh": {"Source": "CAB-fx", "PathId": BIG_PATH_ID + 20, "Type": "Mesh", "Name": "Mesh"},
                "materials": [
                    {
                        "Source": "CAB-fx",
                        "PathId": NEGATIVE_BIG_PATH_ID - 1,
                        "Type": "Material",
                        "Name": "Material",
                    }
                ],
                "particle_systems": [
                    {
                        "game_object": {"file_id": 0, "path_id": BIG_PATH_ID + 30},
                        "length_in_sec": 5,
                        "simulation_speed": 1,
                        "looping": True,
                        "prewarm": False,
                        "play_on_awake": True,
                        "move_with_transform": 1,
                        "scaling_mode": 1,
                        "max_alive_distance": 30,
                        "enabled_modules": ["InitialModule", "EmissionModule"],
                    }
                ],
                "particle_renderers": [{}],
            },
        ],
        "particle_system_count": 1,
        "particle_renderer_count": 1,
        "mesh_renderer_count": 0,
    }


class BuilderEndToEndTests(unittest.TestCase):
    def create_fixture(self, root: Path) -> BuildConfig:
        inputs = root / "inputs"
        inputs.mkdir()
        runtime = inputs / "runtime.json"
        ownership = [
            ["{map}_lv001", "{map}_lv001", "{map}_lv001", "{map}_lv001"],
            ["{map}_lv001", "{map}_lv001", "{map}_lv001", "{map}_lv001"],
            ["{map}_lv001", "{map}_lv001", "{map}_lv001", "{map}_lv001"],
            ["{map}_lv001", "{map}_lv001", "{map}_lv001", None],
        ]
        maps = {}
        for map_id, domain_id in (("map01", "domain_1"), ("map02", "domain_2")):
            maps[map_id] = {
                "domainId": domain_id,
                "worldBounds": {"xmin": -256, "xmax": 256, "zmin": -256, "zmax": 256},
                "ownershipGrid": [
                    [value.format(map=map_id) if value else None for value in row] for row in ownership
                ],
                "levels": [
                    {
                        "idNum": 1,
                        "levelId": f"{map_id}_lv001",
                        "worldRect": {"xmin": -256, "xmax": 256, "zmin": -256, "zmax": 256},
                    }
                ],
            }
        write_json(
            runtime,
            {
                "format": "EndfieldRegionMapRuntimeManifest/1",
                "resolverVersion": "endfield-sector128-v1",
                "coordinateConvention": {
                    "unity": "X/Y/Z metres",
                    "screen": "+Z north/up",
                    "blender": "(X,Y,Z)=(Unity X,-Unity Z,Unity Y)",
                },
                "maps": maps,
            },
        )

        instance_paths = {}
        asset_paths = {}
        for map_id in ("map01", "map02"):
            instance_paths[map_id] = inputs / f"{map_id}_instances.sqlite"
            asset_paths[map_id] = inputs / f"{map_id}_assets.sqlite"
            create_instance_database(instance_paths[map_id], map_id)
            create_asset_database(asset_paths[map_id])

        scene_root = inputs / "map01_water_scene"
        flowmap = scene_root / "textures" / "flow.png"
        flowmap.parent.mkdir(parents=True)
        flowmap.write_bytes(b"fixture-flowmap")
        scene_manifest = scene_root / "scene_manifest.json"
        raw_probe = inputs / "map01_water_raw_probe.json"
        write_json(scene_manifest, {"format": "fixture"})
        write_json(raw_probe, {"format": "fixture"})
        map01_water = inputs / "map01_water.json"
        write_json(
            map01_water,
            {
                "format": "EndfieldMap01WaterDecodedStage101/1",
                "scene_manifest": str(scene_manifest),
                "probe": str(raw_probe),
                "summary": {"sector_count": 1},
                "configs": [{"name": "Config_water_Map01_0", "materialIndex": 0}],
                "sectors": [
                    {
                        "name": "T_0_0",
                        "texture": {"file_id": 0, "path_id": BIG_PATH_ID + 40},
                        "ripple_layer_count": 64,
                        "texture_name": "fixture-flowmap",
                        "texture_export": {"File": "textures\\flow.png"},
                    }
                ],
            },
        )

        mesh = inputs / "map02_cross_sector_water.obj"
        mesh.write_text(
            "g water\n"
            "v 120 5 0\n"
            "v 140 5 0\n"
            "v 140 6 20\n"
            "v 120 6 20\n"
            "f 1 2 3 4\n",
            encoding="utf-8",
        )
        mesh_sha = hashlib.sha256(mesh.read_bytes()).hexdigest().upper()
        map02_water = inputs / "map02_water.json"
        write_json(
            map02_water,
            {
                "format": "EndfieldMap02DamWaterBindingStage55/1",
                "binding": {
                    "configName": "Config_water_Map02_29",
                    "candidateConfigIndex": 29,
                    "materialIndex": 29,
                    "relevantFields": {"useFlowMap": 1},
                },
                "meshes": [
                    {
                        "path": str(mesh),
                        "sha256": mesh_sha,
                        "vertices": 4,
                        "bounds": {"min": [120, 5, 0], "max": [140, 6, 20]},
                    }
                ],
            },
        )

        map01_effects = inputs / "map01_effects.json"
        map01_prefab = "P_fx_test"
        map01_effect = {
            "file": "fixture.bytes",
            "grid_unique_id": 1,
            "record_index": 2,
            "position": [0, 0, 0],
            "bounds_min": [-1, -1, -1],
            "bounds_max": [1, 1, 1],
            "rotation_euler": [0, 0, 0],
            "scale": [1, 1, 1],
            "prefab_paths": [f"assets/{map01_prefab}.prefab"],
            "conditions": [{"level_num": 1}],
            "default_active": True,
        }
        write_json(
            map01_effects,
            {
                "effects": [map01_effect],
                "anchors": [{"index": 0, "unity_position": [0, 0, 0]}],
                "anchor_links": [
                    {
                        "anchor_index": 0,
                        "candidate_effects": [
                            {
                                "file": "fixture.bytes",
                                "grid_unique_id": 1,
                                "record_index": 2,
                                "default_active": True,
                                "aabb_contains_anchor": True,
                            }
                        ],
                    }
                ],
            },
        )
        map01_semantics = inputs / "map01_semantics.json"
        write_json(map01_semantics, {"prototypes": [prototype(map01_prefab)]})

        map02_prefab = "P_fxmap_map02_lv001_test"
        map02_effects = inputs / "map02_effects.json"
        write_json(
            map02_effects,
            {
                "instances": [
                    {
                        "key": ["fixture", 1, 2],
                        "object": "energy-flow",
                        "prefab": map02_prefab,
                        "group": "defense-array",
                        "unity_position": [10, 2, 10],
                        "unity_rotation_euler": [0, 0, 0],
                        "unity_scale": [1, 1, 1],
                        "matrix_world": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 10, -10, 2, 1],
                    }
                ]
            },
        )
        map02_semantics = inputs / "map02_semantics.json"
        write_json(map02_semantics, {"prototypes": [prototype(map02_prefab)]})

        light_report = inputs / "light_report.md"
        light_report.write_text("# fixture\nNo valid Map01 scan.\n", encoding="utf-8")
        light_scans = []
        for index in range(2):
            path = inputs / f"map02_light_scan_{index}.json"
            write_json(
                path,
                {
                    "format": "EndfieldLightProbe/1",
                    "input": f"closure-{index}",
                    "inputFileCount": 1,
                    "loadedAssetFileCount": 1,
                    "gameObjectCount": 1,
                    "targetObjectCount": 0,
                    "records": [],
                },
            )
            light_scans.append(path)

        return BuildConfig(
            output_root=root / "exports" / "placeholder",
            export_root=root / "exports",
            game_root=root / "game",
            runtime_manifest=runtime,
            map01_instances=instance_paths["map01"],
            map01_assets=asset_paths["map01"],
            map02_instances=instance_paths["map02"],
            map02_assets=asset_paths["map02"],
            map01_water=map01_water,
            map02_water=map02_water,
            map01_effects=map01_effects,
            map01_effect_semantics=map01_semantics,
            map02_effects=map02_effects,
            map02_effect_semantics=map02_semantics,
            map01_light_report=light_report,
            map02_light_scans=tuple(light_scans),
            generated_at=FIXED_TIME,
            max_instances_per_map=5,
        )

    def test_small_build_is_valid_and_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_root = root / "exports"
            export_root.mkdir()
            game_root = root / "game"
            game_root.mkdir()
            base = self.create_fixture(root)
            with patch.dict(
                os.environ,
                {"ENDFIELD_EXPORT_ROOT": str(export_root), "ENDFIELD_GAME_ROOT": str(game_root)},
                clear=False,
            ):
                first = build_all(BuildConfig(**{**base.__dict__, "output_root": export_root / "build-a"}))
                second = build_all(BuildConfig(**{**base.__dict__, "output_root": export_root / "build-b"}))
            self.assertEqual(first["validationStatus"], "passed")
            self.assertEqual(first["datasetFingerprint"], second["datasetFingerprint"])
            left_files = {
                path.relative_to(export_root / "build-a").as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (export_root / "build-a").rglob("*")
                if path.is_file()
            }
            right_files = {
                path.relative_to(export_root / "build-b").as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (export_root / "build-b").rglob("*")
                if path.is_file()
            }
            self.assertEqual(left_files, right_files)
            manifest = json.loads((export_root / "build-a" / "region_map_layer_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["maps"]["map01"]["layers"]["instances"]["recordCount"], 5)
            self.assertEqual(manifest["maps"]["map02"]["layers"]["water"]["recordCount"], 1)
            self.assertIn("schemas/endfield_map_layer_dataset.schema.json", manifest["schemas"])
            validation = json.loads((export_root / "build-a" / "validation.json").read_text(encoding="utf-8"))
            self.assertEqual(validation["status"], "passed")

            instance_index = json.loads(
                (export_root / "build-a" / "map01" / "instances" / "index.json").read_text(encoding="utf-8")
            )
            self.assertEqual(instance_index["datasetId"], "map01.instances.stage119")
            self.assertEqual(
                instance_index["assetResolutionCounts"],
                {"missing_resolution": 1, "proxy_only": 1, "resolved": 2, "unresolved": 1},
            )
            self.assertEqual(instance_index["assetResolutionInstanceCounts"], instance_index["assetResolutionCounts"])
            self.assertEqual(instance_index["assetPendingCount"], 3)
            self.assertEqual(instance_index["assetPendingInstanceCount"], 3)
            with gzip.open(export_root / "build-a" / "map01" / "instances" / "assets.jsonl.gz", "rt", encoding="utf-8") as handle:
                assets = [json.loads(line) for line in handle]
            by_entity = {row["entityBase"]: row for row in assets}
            self.assertEqual(by_entity["tree_oak"]["resolutionStatus"], "unresolved")
            self.assertEqual(by_entity["building_wall"]["resolutionStatus"], "proxy_only")
            self.assertEqual(by_entity["prop_machine"]["lookupStatus"], "missing")
            self.assertEqual(by_entity["prop_machine"]["resolutionStatus"], "missing_resolution")

            with gzip.open(
                export_root / "build-a" / "map01" / "effects" / "prototypes.jsonl.gz", "rt", encoding="utf-8"
            ) as handle:
                effect_prototype = json.loads(next(handle))
            with gzip.open(
                export_root / "build-a" / "map01" / "effects" / "prototype_members.jsonl.gz",
                "rt",
                encoding="utf-8",
            ) as handle:
                members = [json.loads(line) for line in handle]
            emitter = next(row for row in members if row["name"] == "Emitter")
            self.assertEqual(effect_prototype["rootAsset"]["pathId"], str(NEGATIVE_BIG_PATH_ID))
            self.assertEqual(emitter["pathId"], str(NEGATIVE_BIG_PATH_ID))
            self.assertEqual(emitter["transformPathId"], str(BIG_PATH_ID + 11))
            self.assertEqual(emitter["parentTransformPathId"], str(BIG_PATH_ID + 10))
            self.assertEqual(emitter["mesh"]["pathId"], str(BIG_PATH_ID + 20))
            self.assertEqual(emitter["materials"][0]["pathId"], str(NEGATIVE_BIG_PATH_ID - 1))
            self.assertEqual(emitter["particleSystems"][0]["gameObject"]["source"], 0)
            self.assertEqual(emitter["particleSystems"][0]["gameObject"]["pathId"], str(BIG_PATH_ID + 30))
            self.assertEqual(json.loads(json.dumps(emitter))["pathId"], str(NEGATIVE_BIG_PATH_ID))

            with gzip.open(export_root / "build-a" / "map01" / "water" / "records.jsonl.gz", "rt", encoding="utf-8") as handle:
                water = json.loads(next(handle))
            self.assertEqual(water["source"]["texturePathId"], str(BIG_PATH_ID + 40))

            roads = json.loads((export_root / "build-a" / "map01" / "roads" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(roads["sectorKeys"], ["sector:-1:-1"])
            self.assertEqual(len(roads["roadShards"]), 1)
            self.assertEqual(roads["roadShards"][0]["roadRecordCount"], 1)
            self.assertEqual(roads["roadShards"][0]["path"], instance_index["shards"][0]["path"])
            self.assertFalse((export_root / "build-a" / "map01" / "roads" / "records.jsonl.gz").exists())

            audit_report = audit(export_root / "build-a", verify_source_hashes=True)
            self.assertEqual(audit_report["status"], "passed")


if __name__ == "__main__":
    unittest.main()
