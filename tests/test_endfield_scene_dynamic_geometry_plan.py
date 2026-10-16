"""Exercise multipart Dynamic ECS planning and portable loading with on-disk meshes."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from endfield_scene_dynamic_geometry_plan import build_dynamic_geometry_plan
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_mesh_uv_contract import MeshUvCatalog
from extract_scene_renderer_materials import RendererDraw
from extractor_core.dynamic_renderers import (DynamicRendererPair, DynamicRendererRecord,
                                              DynamicRendererSource, DynamicSceneRendererInventory)


class DynamicGeometryPartTests(unittest.TestCase):
    def test_multimesh_records_preserve_draws_and_deduplicate_each_part_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = "Data/DynamicStreaming/PC/Scene/map01/fb_init_0_7.bytes"
            paths = ("assets/coauthored_a_lod0.fbx##a_lod0", "assets/coauthored_b_lod0.fbx##b_lod0")
            pairs = tuple(DynamicRendererPair(path, f"assets/surface{index}.mat", str(index), str(index + 3),
                                              index * 40, RendererDraw(0, 0, 1000., 100.), "00", "00")
                          for index, path in enumerate(paths))
            record = DynamicRendererRecord(
                key=f"{source}:0:0", source_virtual_path=source, chunk_id=7, init_scene_index=0, group_index=0,
                record_index=0, reference_keys=("actual-main:0:0",), name=None,
                matrix=(1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.), bounds=None,
                serialized_draw_parameters=(0.,) * 20, common_instance_offset=0,
                renderer_reference_offset=256, runtime_draw_offset=512, pairs=pairs)
            inventory = DynamicSceneRendererInventory("map01", (
                DynamicRendererSource(source, "0" * 32, 1, 1, "0" * 64, "init"),), (), (), (record,))
            containers, exports, materials = {}, {}, {}
            for index, path in enumerate(paths):
                mesh = root / f"mesh{index}.obj"
                mesh.write_text("g surface\nv 0 0 0\nv 1 0 0\nv 0 0 1\nf 1 2 3\n", encoding="utf-8")
                asset = {"Source": "cab", "PathId": index + 1, "Name": f"{'a' if index == 0 else 'b'}_lod0"}
                containers[path.split("##", 1)[0]] = {("cab", index + 1)}
                exports[("cab", index + 1)] = (asset, mesh)
                containers[f"assets/surface{index}.mat"] = {("cab", index + 3)}
                materials[("cab", index + 3)] = {"Source": "cab", "PathId": index + 3, "Name": f"surface{index}",
                    "Shader": {"Source": "cab", "PathId": 5, "Name": "HGRP/Lit"}, "Floats": {}, "Colors": {},
                    "Textures": [{"Property": "_BaseColorMap", "Texture": None, "Scale": [1., 1.], "Offset": [0., 0.]}]}
            bounds = {"xmin": -1., "xmax": 1., "zmin": -1., "zmax": 1.}
            args = (bounds, bounds, frozenset({"unknown", "prop", "vegetation"}), containers, exports, materials, {})
            result = build_dynamic_geometry_plan(inventory, *args, (), MeshUvCatalog({}, {}))
            self.assertEqual((result.selected_count, len(result.rows), result.pending), (1, 2, ()))
            self.assertEqual({row["source_key"] for row in result.rows}, {record.key})
            self.assertEqual([row["source_draw_indices"] for row in result.rows], [[0], [1]])
            self.assertEqual(len({row["mesh_part_key"] for row in result.rows}), 2)
            self.assertTrue(all(len(row["source_renderer_pairs"]) == 2 for row in result.rows))
            static_rows = tuple({**row, "entity_id": index + 1, "entity_index": 0}
                                for index, row in enumerate(result.rows))
            partial = build_dynamic_geometry_plan(inventory, *args, static_rows[:1], MeshUvCatalog({}, {}))
            self.assertEqual((len(partial.rows), partial.excluded_static_duplicates), (1, ()))
            complete = build_dynamic_geometry_plan(inventory, *args, static_rows, MeshUvCatalog({}, {}))
            self.assertEqual((complete.rows, complete.excluded_static_duplicates), ((), (record.key,)))
            incomplete = {**containers, "assets/surface1.mat": set()}
            failed = build_dynamic_geometry_plan(inventory, bounds, bounds, args[2], incomplete, exports, materials,
                                                  {}, (), MeshUvCatalog({}, {}))
            self.assertEqual((failed.rows, len(failed.pending), failed.selected_count), ((), 1, 1))
            self.assertEqual(set(failed.required_resource_paths), {*paths, "assets/surface0.mat", "assets/surface1.mat"})
            divergent = replace(inventory, records=(replace(record, pairs=(pairs[0], replace(
                pairs[1], draw=RendererDraw(0, 0, 900., 90.)))),))
            rejected = build_dynamic_geometry_plan(divergent, *args, (), MeshUvCatalog({}, {}))
            self.assertEqual((rejected.rows, len(rejected.pending)), ((), 1))
            sidecar = root / "empty.json"
            sidecar.write_text("{}", encoding="utf-8")
            payload = {"format": "EndfieldPortableScenePlan/1", "map_id": "map01", "bounds": bounds,
                       "scene_manifests": [], "terrain_surfaces": [], "category_filter": [],
                       "decal_manifest": str(sidecar), "decal_materials": str(sidecar), "instances": [], "pending": [],
                       "source_sha256": dict(result.source_sha256), "dynamic_geometry_instances": list(result.rows),
                       "summary": {"selected_static": 0, "ready_static": 0, "selected_decals": 0, "ready_decals": 0,
                                   "pending": 0, "selected_dynamic_geometry": 1, "ready_dynamic_geometry": 2,
                                   "ready_dynamic_geometry_records": 1, "pending_dynamic_geometry": 0,
                                   "excluded_dynamic_static_duplicates": 0}}
            plan = root / "plan.json"
            plan.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(len(load_scene_export_plan(plan)["dynamic_geometry_instances"]), 2)
            for rows, summary, message in ((result.rows[:1], {**payload["summary"], "ready_dynamic_geometry": 1}, "part set is incomplete"),
                    ((result.rows[0], result.rows[0]), payload["summary"], "identity is repeated"),
                    (result.rows, {**payload["summary"], "ready_dynamic_geometry_records": 2}, "unique dynamic source records")):
                plan.write_text(json.dumps({**payload, "dynamic_geometry_instances": rows, "summary": summary}), encoding="utf-8")
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    load_scene_export_plan(plan)


if __name__ == "__main__":
    unittest.main()
