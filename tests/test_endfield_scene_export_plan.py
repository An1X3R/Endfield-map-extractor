"""Portable scene-plan smoke tests with real on-disk first-run-shaped inputs."""

from __future__ import annotations

import json
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from endfield_scene_export_plan import (
    _manifest_index,
    _merge_mesh_exports,
    _water_global_resource_path,
    load_scene_export_plan,
    owns_static_footprint,
    prepare_scene_export_plan,
)
from endfield_scene_io import scene_io_session
from webui_source_contract import selected_static_categories


class PortableScenePlanTests(unittest.TestCase):
    def test_static_mesh_parts_preserve_entity_identity_and_require_the_complete_source_set(self) -> None:
        from copy import deepcopy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "part.obj"
            mesh.write_text("o part\nv 0 0 0\nv 1 0 0\nv 0 0 1\nf 1 2 3\n", encoding="utf-8")
            sidecar = root / "sidecar.json"
            sidecar.write_text("{}", encoding="utf-8")
            paths = ["assets/a_lod0.fbx##a_lod0", "assets/b_lod0.fbx##b_lod0"]
            source = [{"mesh_path": path, "material_path": "assets/surface.mat", "material_id": "01",
                       "mesh_id": str(index + 2), "submesh": 0, "flags": 134217728,
                       "reference_component": 69, "source_draw_index": index,
                       "lod_max_squared": 1000., "lod_min_squared": .7} for index, path in enumerate(paths)]
            fields = ("mesh_path", "material_path", "material_id", "mesh_id", "submesh", "flags")
            rows = [{"entity_id": 7, "entity_base": "P_source", "category": "vegetation",
                     "source_path": "InitChunkData", "source_sha256": "0" * 64, "group_index": 0, "entity_index": 0,
                     "matrix": [1.] * 16, "mesh_path": path, "mesh_file": str(mesh),
                     "mesh_asset": {"Source": "cab", "PathId": index + 1, "Name": f"part{index}"},
                     "renderer_pairs": [{field: source[index][field] for field in fields}],
                     "submesh_slot_indices": [0], "material_refs": [None], "undrawn_submeshes": [],
                     "mesh_part_key": f"InitChunkData:entity:7/mesh:{source[index]['mesh_id']}",
                     "source_renderer_pairs": source, "source_draw_indices": [index], "selected_mesh_paths": paths,
                     "lod_selection": "coauthored_highest_detail_source_interval"} for index, path in enumerate(paths)]
            payload = {"format": "EndfieldPortableScenePlan/1", "map_id": "map01",
                       "bounds": {"xmin": 0, "xmax": 1, "zmin": 0, "zmax": 1},
                       "scene_manifests": [], "terrain_surfaces": [], "category_filter": [],
                       "decal_manifest": str(sidecar), "decal_materials": str(sidecar),
                       "instances": rows, "pending": [], "source_sha256": {"InitChunkData": "0" * 64},
                       "summary": {"selected_static": 1, "ready_static": 2, "ready_static_entities": 1,
                                   "partial_static_entities": 0, "complete_static_entities": 1,
                                   "selected_decals": 0, "ready_decals": 0, "pending": 0}}
            path = root / "plan.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_scene_export_plan(path)
            self.assertEqual([7, 7], [row["entity_id"] for row in loaded["instances"]])
            for changed, message in (
                ({**payload, "instances": rows[:1]}, "part set is incomplete"),
                ({**payload, "instances": [rows[0], rows[0]]}, "identity is repeated"),
                ({**payload, "summary": {**payload["summary"], "ready_static_entities": 2}}, "unique static source entities"),
                ({**payload, "summary": {**payload["summary"], "complete_static_entities": 0}}, "partial/complete static source entities"),
            ):
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    load_scene_export_plan(path)
            for field, value, message in (("source_draw_indices", [0], "draws differ"),
                    ("mesh_part_key", "invented", "key differs"),
                    ("matrix", [2.] * 16, "original entity evidence"),
                    ("selected_mesh_paths", paths[:1], "co-authored source mesh set")):
                changed = deepcopy(payload)
                changed["instances"][1][field] = value
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                    load_scene_export_plan(path)

    def test_static_shadow_only_exclusions_keep_source_evidence_and_reject_visible_draws(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sidecar = root / "sidecar.json"
            sidecar.write_text("{}", encoding="utf-8")
            exclusion = {"entity_id": 7, "entity_base": "source_shadow_shell", "source_path": "InitChunkData",
                         "source_sha256": "0" * 64, "group_index": 1, "entity_index": 2, "role": "shadow_only",
                         "source_draws": [{"mesh_path": "source_lod6.fbx##source_lod6", "material_path": "shadow.mat",
                            "mesh_id": "01", "material_id": "02", "submesh": 0, "flags": 1024,
                            "reference_component": 68, "source_draw_index": 0,
                            "lod_max_squared": 1000., "lod_min_squared": -.0004}]}
            payload = {"format": "EndfieldPortableScenePlan/1", "map_id": "map01",
                "bounds": {"xmin": 0, "xmax": 1, "zmin": 0, "zmax": 1},
                "scene_manifests": [], "terrain_surfaces": [], "category_filter": [],
                "decal_manifest": str(sidecar), "decal_materials": str(sidecar),
                "instances": [], "pending": [], "source_sha256": {"InitChunkData": "0" * 64},
                "static_role_exclusions": [exclusion],
                "summary": {"selected_static": 0, "ready_static": 0, "selected_decals": 0,
                            "ready_decals": 0, "pending": 0, "excluded_static_shadow_only": 1}}
            plan = root / "plan.json"
            plan.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(load_scene_export_plan(plan)["static_role_exclusions"], [exclusion])
            for changed, message in (({**exclusion, "source_sha256": "1" * 64}, "source SHA-256"),
                ({**exclusion, "source_draws": [*exclusion["source_draws"],
                   {**exclusion["source_draws"][0], "flags": 0}]}, "validated shadow-only draws")):
                plan.write_text(json.dumps({**payload, "static_role_exclusions": [changed]}), encoding="utf-8")
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    load_scene_export_plan(plan)
            plan.write_text(json.dumps({**payload, "summary": {**payload["summary"],
                                       "excluded_static_shadow_only": 0}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "summary differs from static role exclusions"):
                load_scene_export_plan(plan)

    def test_static_submesh_pending_keeps_the_unknown_slot_out_of_ready_rows(self) -> None:
        from copy import deepcopy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sidecar = root / "sidecar.json"
            sidecar.write_text("{}", encoding="utf-8")
            mesh_file = root / "machine.obj"
            mesh_file.write_text("o machine\nv 0 0 0\nv 1 0 0\nv 0 0 1\nf 1 2 3\n", encoding="utf-8")
            mesh_path = "assets/machine_lod0.fbx##machine_lod0"
            ordinary_draw = {"mesh_path": mesh_path, "material_path": "assets/m_machine.mat",
                "material_id": "01", "mesh_id": "02", "submesh": 0, "flags": 134217728,
                "reference_component": 69, "source_draw_index": 0,
                "lod_max_squared": 1000.0, "lod_min_squared": 0.04}
            proxy_draw = {"mesh_path": mesh_path, "material_path": "assets/m_shadowproxy_static_opaque.mat",
                "material_id": "03", "mesh_id": "02", "submesh": 1, "flags": 134217728,
                "reference_component": 69, "source_draw_index": 1,
                "lod_max_squared": 1000.0, "lod_min_squared": 0.04}
            pair_fields = ("material_id", "material_path", "mesh_id", "mesh_path", "submesh", "flags")
            ordinary_ref = {"Source": "cab-machine", "PathId": 1, "Name": "M_machine"}
            proxy_ref = {"Source": "cab-machine", "PathId": 2, "Name": "M_shadowproxy_static_opaque"}
            row = {"entity_id": 7, "entity_base": "P_mod_map02_machine+1_002_01", "category": "building",
                "source_path": "InitChunkData_0_0_0_0", "source_sha256": "0" * 64,
                "group_index": 0, "entity_index": 4, "matrix": [1.0] * 16,
                "mesh_path": mesh_path, "mesh_file": str(mesh_file),
                "mesh_asset": {"Source": "cab-machine", "PathId": 3, "Name": "machine_lod0"},
                "renderer_pairs": [{field: ordinary_draw[field] for field in pair_fields},
                    {field: proxy_draw[field] for field in pair_fields}],
                "source_draw_evidence": [ordinary_draw, proxy_draw],
                "submesh_slot_indices": [0, 1], "material_refs": [ordinary_ref, proxy_ref],
                "undrawn_submeshes": [], "selected_submeshes": [0]}
            pending = {"entity_id": 7, "entity_base": row["entity_base"],
                "component": "static_mesh_submesh", "reason": "source_surface_role_unverified",
                "detail": "Material role is not validated for ordinary surface export: M_shadowproxy_static_opaque",
                "source_path": row["source_path"], "source_sha256": row["source_sha256"],
                "group_index": 0, "entity_index": 4, "mesh_path": mesh_path, "submesh": 1,
                "material_name": "M_shadowproxy_static_opaque", "material_path": proxy_draw["material_path"],
                "material_id": proxy_draw["material_id"], "mesh_id": proxy_draw["mesh_id"],
                "source_draw": proxy_draw}
            payload = {"format": "EndfieldPortableScenePlan/1", "map_id": "map02",
                "bounds": {"xmin": 0, "xmax": 1, "zmin": 0, "zmax": 1},
                "scene_manifests": [], "terrain_surfaces": [], "category_filter": [],
                "decal_manifest": str(sidecar), "decal_materials": str(sidecar),
                "instances": [row], "pending": [pending],
                "source_sha256": {row["source_path"]: row["source_sha256"]},
                "summary": {"selected_static": 1, "ready_static": 1, "ready_static_entities": 1,
                    "partial_static_entities": 1, "complete_static_entities": 0,
                    "selected_decals": 0, "ready_decals": 0, "pending": 1}}
            plan = root / "plan.json"
            plan.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_scene_export_plan(plan)
            self.assertEqual(loaded["instances"][0]["selected_submeshes"], [0])
            self.assertEqual(loaded["pending"][0]["source_draw"], proxy_draw)
            self.assertEqual(loaded["summary"]["ready_static_entities"], 1)
            self.assertEqual(loaded["summary"]["partial_static_entities"], 1)
            self.assertEqual(loaded["summary"]["complete_static_entities"], 0)
            for changed, message in (
                ({**row, "selected_submeshes": [0, 1]}, "selects a pending source submesh"),
                ({**pending, "source_draw": {**proxy_draw, "source_draw_index": 0}}, "differs from ready source draw evidence"),
            ):
                invalid = deepcopy(payload)
                if "selected_submeshes" in changed:
                    invalid["instances"] = [changed]
                else:
                    invalid["pending"] = [changed]
                plan.write_text(json.dumps(invalid), encoding="utf-8")
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    load_scene_export_plan(plan)
            for summary, message in (
                ({**payload["summary"], "partial_static_entities": 0}, "partial/complete static source entities"),
                ({**payload["summary"], "complete_static_entities": 1}, "partial/complete static source entities"),
                ({**payload["summary"], "complete_static_entities": -1}, "Invalid scene summary count: complete_static_entities"),
                ({key: value for key, value in payload["summary"].items()
                 if key != "ready_static_entities"}, "requires ready_static_entities"),
            ):
                plan.write_text(json.dumps({**payload, "summary": summary}), encoding="utf-8")
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    load_scene_export_plan(plan)

    def test_manifest_mesh_duplicates_keep_canonical_identity_and_reject_multiple_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "scene.json"
            first = root / "first.obj"
            second = root / "second.obj"
            first.write_bytes(b"mesh")
            second.write_bytes(b"mesh")
            asset = {"Source": "source", "PathId": 1, "Name": "mesh"}
            payload = {"Format": "EndfieldSceneProbe/1", "Containers": [], "MeshExports": [
                {"Asset": asset, "File": "first.obj"},
                {"Asset": asset, "File": "./first.obj"},
                {"Asset": asset, "File": "missing.obj"},
            ]}
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual({("source", 1): (asset, first.resolve())}, _manifest_index(manifest)[1])
            manifest.write_text(json.dumps({**payload, "MeshExports": [
                *payload["MeshExports"], {"Asset": asset, "File": "second.obj"},
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "multiple export files"):
                _manifest_index(manifest)

    def test_manifest_index_rechecks_mesh_export_presence_after_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "scene.json"
            mesh = root / "mesh.obj"
            manifest.write_text(json.dumps({
                "Format": "EndfieldSceneProbe/1",
                "Containers": [],
                "MeshExports": [{"Asset": {"Source": "source", "PathId": 1, "Name": "mesh"},
                                 "File": "mesh.obj"}],
            }), encoding="utf-8")
            mesh.write_text("mesh", encoding="utf-8")
            with scene_io_session():
                _, first_exports = _manifest_index(manifest)
                self.assertIn(("source", 1), first_exports)
                mesh.unlink()
                _, missing_exports = _manifest_index(manifest)
                self.assertNotIn(("source", 1), missing_exports)
                mesh.write_text("mesh", encoding="utf-8")
                _, restored_exports = _manifest_index(manifest)
                self.assertIn(("source", 1), restored_exports)

    def test_serialized_draw_parameters_validate_binary_source_identity(self) -> None:
        from extract_scene_renderer_materials import serialized_draw_parameters
        matrix = struct.pack("<16f", *range(16))
        values = [index / 4 for index in range(20)]
        raw = matrix + bytes(112) + struct.pack("<20f", *values)
        self.assertEqual(serialized_draw_parameters(raw, matrix), values)
        for damaged, expected_matrix in ((raw[:-4], matrix), (raw, bytes(64)),
                                          (raw[:176] + struct.pack("<20f", float("inf"), *values[1:]), matrix)):
            with self.assertRaises(ValueError):
                serialized_draw_parameters(damaged, expected_matrix)

    def test_crossing_geometry_has_one_partition_owner(self) -> None:
        selection = {"xmin": 0.0, "xmax": 256.0, "zmin": 0.0, "zmax": 128.0}
        batches = [{**selection, "xmin": 0.0, "xmax": 128.0},
                   {**selection, "xmin": 128.0, "xmax": 256.0}]
        cases = [((-20.0, 40.0), (-30.0, 200.0, 0.0, 20.0, 20.0, 80.0), [True, False]),
                 ((200.0, 40.0), (50.0, 210.0, 0.0, 20.0, 20.0, 80.0), [False, True]),
                 ((-20.0, 40.0), (-30.0, 0.0, 0.0, 20.0, 20.0, 80.0), [False, False])]
        for origin, footprint, expected in cases:
            with self.subTest(origin=origin, footprint=footprint):
                actual = [owns_static_footprint(batch, selection, origin, footprint) for batch in batches]
                self.assertEqual(actual, expected)
                self.assertEqual(any(actual), owns_static_footprint(selection, selection, origin, footprint))

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

    def test_water_global_path_is_map_scoped_and_unique(self) -> None:
        map01 = "Assets/Beyond/Scenes/LunaScenes/Map01/ReleaseData/WaterData/Global_Water_Config.asset"
        map02 = "Assets/Beyond/Scenes/LunaScenes/Map02/ReleaseData/WaterData/Global_Water_Config.asset"
        index = {b"one": (map01,), b"two": (map02,)}
        self.assertEqual(_water_global_resource_path(index, "map02"), map02)
        self.assertIsNone(_water_global_resource_path({b"one": (map01,)}, "map02"))
        office = "Assets/Beyond/Scenes/LunaScenes/indie_dg005/ReleaseData/WaterData/Global_Water_Config.asset"
        self.assertEqual(_water_global_resource_path({b"office": (office,), **index}, "indie_dg005"), office)
        with self.assertRaises(ValueError):
            _water_global_resource_path(index, "../indie_dg005")
        with self.assertRaisesRegex(ValueError, "WATER_GLOBAL_PATH_AMBIGUOUS"):
            _water_global_resource_path(
                {b"one": (map02,), b"two": (map02.replace("ReleaseData", "ReleaseData/Alternate"),)},
                "map02",
            )

    def test_legacy_plan_defaults_water_rows_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sidecar = root / "sidecar.json"
            sidecar.write_text("{}", encoding="utf-8")
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "format": "EndfieldPortableScenePlan/1", "map_id": "map01",
                "bounds": {"xmin": 0, "xmax": 1, "zmin": 0, "zmax": 1},
                "scene_manifests": [], "terrain_surfaces": [], "category_filter": [],
                "decal_manifest": str(sidecar), "decal_materials": str(sidecar),
                "instances": [], "pending": [], "source_sha256": {},
                "summary": {"selected_static": 0, "ready_static": 0, "selected_decals": 0,
                            "ready_decals": 0, "pending": 0},
            }), encoding="utf-8")
            loaded = load_scene_export_plan(plan)
            self.assertEqual(loaded["static_role_exclusions"], [])
            self.assertEqual(loaded["water_geometry"], [])
            self.assertEqual(loaded["water_materials"], [])
            self.assertEqual(loaded["water_depth_surfaces"], [])
            self.assertEqual(loaded["water_depth_instances"], [])
            self.assertEqual(loaded["water_depth_pending"], [])
            self.assertEqual(loaded["effect_instances"], [])
            baseline = json.loads(plan.read_text(encoding="utf-8"))
            plan.write_text(json.dumps({**baseline, "water_transport_lighting_status": "not_applicable"}),
                            encoding="utf-8")
            self.assertEqual(load_scene_export_plan(plan)["water_transport_lighting_status"], "not_applicable")
            for invalid in ({"water_transport_lighting_status": "unknown"},
                            {"water_transport_lighting_status": "not_applicable", "water_geometry": [{}]},
                            {"water_transport_lighting": {"map_id": "map01", "entity_id": 7,
                                                           "ambient_radiance": [0., 0., 0.],
                                                           "radiance_scale": 1.}}):
                plan.write_text(json.dumps({**baseline, **invalid}), encoding="utf-8")
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    load_scene_export_plan(plan)
            plan.write_text(json.dumps({**baseline, "fog_depth": {
                "subdivision_level": 4, "future_field": "ignored"}}), encoding="utf-8")
            self.assertEqual(load_scene_export_plan(plan)["fog_depth"], {"subdivision_level": 4})
            for invalid in ({}, {"subdivision_level": True}, {"subdivision_level": -1},
                            {"subdivision_level": 7}, {"subdivision_level": 1.5}):
                plan.write_text(json.dumps({**baseline, "fog_depth": invalid}), encoding="utf-8")
                with self.subTest(fog_depth=invalid), self.assertRaisesRegex(ValueError, "fog_depth"):
                    load_scene_export_plan(plan)
            plan.write_text(json.dumps(baseline), encoding="utf-8")
            for scene_id in ("dung01_cdg001", "dung02_cdg001", "indie_dg005"):
                standalone = {**json.loads(plan.read_text(encoding="utf-8")), "map_id": scene_id}
                plan.write_text(json.dumps(standalone), encoding="utf-8")
                self.assertEqual(load_scene_export_plan(plan)["map_id"], scene_id)
            for invalid_id in ("../map01", "map01/other", "C:\\map01", ""):
                standalone["map_id"] = invalid_id
                plan.write_text(json.dumps(standalone), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_scene_export_plan(plan)

    def test_loader_accepts_explicit_water_pending_preflight_plan(self) -> None:
        from endfield_scene_lights_plan import build_water_transport_directional_dependency
        from extractor_core.lights import LightProperty, NativeLight, NativeLightInventory

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sidecar = root / "sidecar.json"
            sidecar.write_text("{}", encoding="utf-8")
            init_path = "Data/Streaming/PC/map01/Streaming/InitChunkData_0_0_0_0.bytes"
            stream_path = init_path.replace("/InitChunkData_", "/StreamingChunkData_")
            light = NativeLight(
                "map01", 90982682, "Sun", (20.0, 30.0, 40.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0),
                1, 0, (1.0, 1.0, 1.0, 1.0), 2.0, 10.0, 45.0, 20.0, 0.0, 6500.0,
                False, False, False, 1.0, None, 131072,
                tuple(LightProperty(index, 1, "00") for index in range(60)),
                tuple(LightProperty(index, 1, "00") for index in range(21)),
                (init_path,), (stream_path,), "0" * 64,
            )
            inventory = NativeLightInventory("map01", (light,),
                                             ((init_path, "1" * 64), (stream_path, "2" * 64)), 1)
            dependency = build_water_transport_directional_dependency(inventory, "map01", light.entity_id)
            pending = {"entity_id": 7, "entity_base": "water", "component": "water_geometry",
                       "reason": "resource_pending", "detail": "water geometry awaits first resource preparation"}
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "format": "EndfieldPortableScenePlan/1", "map_id": "map01",
                "bounds": {"xmin": 0, "xmax": 1, "zmin": 0, "zmax": 1},
                "scene_manifests": [], "terrain_surfaces": [], "category_filter": ["water"],
                "decal_manifest": str(sidecar), "decal_materials": str(sidecar),
                "instances": [], "pending": [pending], "source_sha256": dict(dependency.source_sha256),
                "light_instances": list(dependency.rows), "water_geometry": [], "water_materials": [],
                "water_transport_lighting": {"map_id": "map01", "entity_id": light.entity_id,
                                              "ambient_radiance": [0.1, 0.2, 0.3], "radiance_scale": 1.0},
                "summary": {"selected_static": 0, "ready_static": 0, "selected_decals": 0,
                            "ready_decals": 0, "pending": 1},
            }), encoding="utf-8")
            loaded = load_scene_export_plan(plan)
            self.assertEqual(loaded["pending"], [pending])
            self.assertEqual(loaded["water_transport_lighting"]["entity_id"], light.entity_id)
            self.assertEqual(loaded["light_instances"][0]["selection_scope"], "water_transport_dependency")

    def test_bottom_dependencies_are_validated_without_counting_as_exported_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sidecar = root / "sidecar.json"
            sidecar.write_text("{}", encoding="utf-8")
            mesh = root / "bottom.obj"
            mesh.write_text("o bottom\nv 0 0 0\nv 1 0 0\nv 0 0 1\nf 1 2 3\n", encoding="utf-8")
            row = {"entity_id": 7, "entity_base": "P_pool_bottom", "category": "building",
                   "source_path": "InitChunkData_pool", "source_sha256": "0" * 64,
                   "group_index": 0, "entity_index": 0, "mesh_path": "pool.fbx##bottom",
                   "mesh_file": str(mesh), "mesh_asset": {"Source": "cab", "PathId": 1, "Name": "bottom"},
                   "matrix": [1.0] * 16, "submesh_slot_indices": [0], "material_refs": [None],
                   "undrawn_submeshes": [], "renderer_pairs": [
                       {"material_id": "01", "material_path": "pool.mat", "mesh_id": "02",
                        "mesh_path": "pool.fbx##bottom", "submesh": 0, "flags": 0}]}
            payload = {"format": "EndfieldPortableScenePlan/1", "map_id": "map01",
                       "bounds": {"xmin": 0, "xmax": 1, "zmin": 0, "zmax": 1},
                       "scene_manifests": [], "terrain_surfaces": [], "category_filter": ["water"],
                       "decal_manifest": str(sidecar), "decal_materials": str(sidecar),
                       "instances": [], "water_depth_instances": [row], "pending": [],
                       "source_sha256": {"InitChunkData_pool": "0" * 64},
                       "summary": {"selected_static": 0, "ready_static": 0, "selected_decals": 0,
                                   "ready_decals": 0, "pending": 0}}
            plan = root / "plan.json"
            plan.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_scene_export_plan(plan)
            self.assertEqual(loaded["instances"], [])
            self.assertEqual(len(loaded["water_depth_instances"]), 1)
            self.assertEqual(loaded["water_depth_instances"][0]["selected_submeshes"], [0])
            for invalid in ([], [1], [-1], [True], [0, 0]):
                row["selected_submeshes"] = invalid
                plan.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "selected source submesh"):
                    load_scene_export_plan(plan)
            row.pop("selected_submeshes")
            split = {**row, "submesh_slot_indices": [0, 0], "material_refs": [None, None],
                     "selected_submeshes": [0]}
            surface = {**split, "selected_submeshes": [1]}
            split_payload = {**payload, "water_depth_instances": [split], "surface_effect_instances": [surface]}
            plan.write_text(json.dumps(split_payload), encoding="utf-8")
            self.assertEqual(load_scene_export_plan(plan)["surface_effect_instances"][0]["selected_submeshes"], [1])
            for changed, detail in (({"selected_submeshes": [0]}, "submeshes overlap"),
                                    ({"entity_index": 1}, "source differs")):
                split_payload["surface_effect_instances"] = [{**surface, **changed}]
                plan.write_text(json.dumps(split_payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, detail):
                    load_scene_export_plan(plan)
            row["serialized_draw_parameters"] = [index / 4 for index in range(20)]
            plan.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_scene_export_plan(plan)
            self.assertEqual(loaded["water_depth_instances"][0]["serialized_draw_parameters"],
                             row["serialized_draw_parameters"])
            for invalid in ([0.0] * 19, [float("nan")] * 20, [True] * 20):
                row["serialized_draw_parameters"] = invalid
                plan.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "invalid 20-float draw parameters"):
                    load_scene_export_plan(plan)
            row["serialized_draw_parameters"] = None
            plan.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(load_scene_export_plan(plan)["water_depth_instances"][0]["serialized_draw_parameters"])
            mesh.unlink()
            with self.assertRaises(FileNotFoundError):
                load_scene_export_plan(plan)

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
                    database.execute(
                        "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?)",
                        (1, "HLOD0_3_14_Cluster_1837366762_150", 18, b"", "InitChunkData_0_0_0_0.bytes", 0, 0, 4.0, 4.0),
                    )
            for name in ("assets.sqlite", "resources.sqlite", "bundles.sqlite", "paths.bin"):
                (cache / name).write_bytes(b"")
            manifest = cache / "scene_manifest.json"
            manifest.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Containers": [],
                                            "MeshExports": [], "Materials": [], "TextureExports": []}), encoding="utf-8")
            terrain = cache / "terrain_surface"
            terrain.mkdir()
            (terrain / "terrain_surface.json").write_text("{}", encoding="utf-8")
            config = cache / "runtime_config.json"
            config.write_text(json.dumps({
                "format": "EndfieldRuntimeConfig/1",
                "paths": {"game_root": "../game", "cache_root": ".", "export_root": "../export"},
                "extraction": {"resourceDatabase": "resources.sqlite",
                               "bundleScanIndex": "bundles.sqlite",
                               "maps": {"map01": {"instances": "instances.sqlite", "assets": "assets.sqlite",
                                                  "sceneProbe": {"path": "scene_manifest.json"},
                                                  "terrainSurfaces": ["terrain_surface"]}}},
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
            self.assertEqual(actual["summary"]["ready_static_entities"], 0)
            self.assertEqual(actual["summary"]["partial_static_entities"], 0)
            self.assertEqual(actual["summary"]["complete_static_entities"], 0)
            self.assertEqual(actual["water_geometry"], [])
            self.assertEqual(actual["water_materials"], [])
            self.assertNotIn("water_transport_lighting", actual)
            self.assertNotIn("water_transport_lighting_status", actual)
            self.assertEqual(actual["water_depth_surfaces"], [])
            self.assertEqual(actual["terrain_surfaces"], [])
            self.assertEqual(actual["pending"], [])
            self.assertNotIn("fog_depth", actual)
            snapshot = {
                "format": "EndfieldSceneColorSnapshot/1",
                "camera_matrix_world": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                "lens_mm": 50.0, "resolution": [320, 240], "engine": "CYCLES",
                "samples": 8, "time_seconds": 1.0, "clip_start": 0.1, "clip_end": 500.0,
            }
            snapshot_plan_data = json.loads(output.read_text(encoding="utf-8"))
            snapshot_plan_data["category_filter"] = ["effects"]
            snapshot_plan_data["scene_color_snapshot"] = snapshot
            snapshot_plan_path = selected_export / "snapshot_plan.json"
            snapshot_plan_path.write_text(json.dumps(snapshot_plan_data), encoding="utf-8")
            self.assertEqual(load_scene_export_plan(snapshot_plan_path)["scene_color_snapshot"], snapshot)
            snapshot_plan_data["scene_color_snapshot"]["clip_end"] = snapshot["clip_start"]
            snapshot_plan_path.write_text(json.dumps(snapshot_plan_data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "clip distances"):
                load_scene_export_plan(snapshot_plan_path)
            snapshot["clip_end"] = 500.0
            with self.assertRaisesRegex(ValueError, "effects layer and effects group"):
                prepare_scene_export_plan(config, {**canonical, "scene_color_snapshot": snapshot},
                                          selected_export / "invalid_snapshot_plan.json", None)
            fog_plan = prepare_scene_export_plan(config, {**canonical, "fog_depth": {"subdivision_level": 3}},
                                                 selected_export / "fog_plan.json", None)
            self.assertEqual(load_scene_export_plan(fog_plan)["fog_depth"], {"subdivision_level": 3})
            self.assertEqual(actual["summary"]["ready_decals"], 0)
            self.assertEqual(actual["summary"]["excluded_hlod"], 0)
            self.assertEqual(json.loads(Path(actual["decal_manifest"]).read_text())["records"], [])
            with self.assertRaisesRegex(ValueError, "Plan output must be new"):
                prepare_scene_export_plan(config, canonical, output, None)
            terrain_request = {**canonical, "export_groups": ["terrain"],
                               "layers": {"instances": False, "terrain": True}}
            from extractor_core.resources import BootstrapExtractionError

            # Selecting ordinary geometry now requires its actual Dynamic ECS source group.
            with self.assertRaisesRegex(BootstrapExtractionError, "Required VFS group 23D53F5D is incomplete"):
                prepare_scene_export_plan(config, terrain_request, selected_export / "terrain_plan.json", None)
            disabled_request = {**canonical, "export_groups": ["all"],
                                "layers": {"instances": True, "terrain": False}}
            with self.assertRaisesRegex(BootstrapExtractionError, "Required VFS group 23D53F5D is incomplete"):
                prepare_scene_export_plan(config, disabled_request, selected_export / "disabled_plan.json", None)

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
            original = json.loads(plan.read_text(encoding="utf-8"))
            for field, value in (
                ("entity_base", "HLOD1_1_7_Cluster_740179475_150"),
                ("mesh_path", "assets/hlod_v2/pc/hlod0/mesh/proxy.fbx##proxy"),
                ("mesh_asset", {"Source": "cab", "PathId": 1, "Name": "S_HLOD0_3_14_Cluster_1837366762"}),
            ):
                with self.subTest(field=field):
                    candidate = json.loads(json.dumps(original))
                    candidate["instances"][0][field] = value
                    plan.write_text(json.dumps(candidate), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "excluded HLOD instance: entity_id=1"):
                        load_scene_export_plan(plan)


if __name__ == "__main__":
    unittest.main()
