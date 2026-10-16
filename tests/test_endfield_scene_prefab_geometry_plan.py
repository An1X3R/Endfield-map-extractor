"""Exercise ordinary prefab hierarchy handoff with real files and source references."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from extractor_core.dynamic_models import DynamicModelInventory, DynamicModelRecord, DynamicModelSource
from endfield_scene_material_contract import reference_key, validate_material
from endfield_scene_mesh_uv_contract import MeshUvCatalog
from endfield_scene_prefab_bounds import select_prefab_geometry_inventory, validate_prefab_selection_evidence
from endfield_scene_prefab_geometry_plan import (build_prefab_geometry_plan, observed_prefab_geometry_paths,
                                               prefab_material_pass_indices, validate_prefab_geometry_plan)
from tests.test_scene_materials import material_record


class PrefabGeometryPlanTests(unittest.TestCase):
    def test_source_lod_and_two_placements_with_shared_guid_survive_portable_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "mesh.obj"
            mesh.write_text("o source\nv 0 0 0\nv 1 0 0\nv 0 1 0\ng source\nf 1 2 3\n", encoding="utf-8")
            (root / "texture.png").write_bytes(b"source texture payload")
            prefab = "assets/source.prefab"
            mesh_asset = {"Source": "CAB-mesh", "PathId": 20, "Type": "Mesh", "Name": "P_prop_lod9"}
            material = validate_material(material_record())
            material_ref = {"Source": material["Source"], "PathId": material["PathId"], "Type": "Material", "Name": material["Name"]}
            root_ref = {"Source": "CAB-prefab", "PathId": 1, "Type": "GameObject"}
            base = {"Source": "CAB-prefab", "GameObjectActive": True, "LocalPosition": [0, 0, 0],
                    "LocalRotation": [0, 0, 0, 1], "LocalScale": [1, 1, 1]}
            root_node = {**base, "PathId": 1, "Name": "root", "TransformPathId": 11,
                         "Parent": {"Source": "CAB-prefab", "PathId": 0, "Type": "Transform"}}
            children = [{**base, "PathId": index, "Name": f"child{index}", "TransformPathId": 10 + index,
                         "Parent": {"Source": "CAB-prefab", "PathId": 11, "Type": "Transform"},
                         "RendererType": "MeshRenderer", "RendererEnabled": True,
                         "RendererRole": {"CastShadows": 1, "PlatformCastShadows": 0, "PipelineFields": {"m_CastShadows": 1}, "RawHex": "01"},
                         "RendererAsset": {"Source": "CAB-prefab", "PathId": 100 + index, "Type": "MeshRenderer"},
                         "Mesh": mesh_asset, "Materials": [material_ref]} for index in (2, 3, 4, 5)]
            children[0]["Name"] = "shadowProxy_name_is_not_a_role"
            children[2]["Name"] = "ordinary_leaf_name_is_not_a_role"
            children[2]["RendererRole"] = {"CastShadows": 1, "PlatformCastShadows": 3, "PipelineFields": {"m_CastShadows": 1}, "RawHex": "03"}
            children[3]["RendererRole"] = {"CastShadows": 1, "PlatformCastShadows": 4, "PipelineFields": {"m_CastShadows": 1}, "RawHex": "04"}
            manifest = root / "scene_manifest.json"
            data = {"Format": "EndfieldSceneProbe/1", "Containers": [{"Container": prefab, "Asset": root_ref}],
                    "Nodes": [root_node, *children], "MeshExports": [{"Asset": mesh_asset, "File": mesh.name}],
                    "LodGroups": [{"Asset": {"Source": "CAB-prefab", "PathId": 1000}, "GameObject": root_ref,
                                   "Enabled": True, "Levels": [{"Renderers": [child["RendererAsset"]]} for child in children]}]}
            inactive_ref = {"Source": "CAB-prefab", "PathId": 6, "Type": "GameObject"}
            data["Nodes"].append({**base, **inactive_ref, "Name": "inactive", "TransformPathId": 16,
                "GameObjectActive": False, "Parent": {"Source": "CAB-prefab", "PathId": 11, "Type": "Transform"}})
            data["LodGroups"].extend([
                {"Asset": {"Source": "CAB-prefab", "PathId": 1001}, "GameObject": root_ref, "Enabled": True, "Levels": []},
                {"Asset": {"Source": "CAB-prefab", "PathId": 1002}, "GameObject": inactive_ref, "Enabled": True, "Levels": None}])
            manifest.write_text(json.dumps(data), encoding="utf-8")
            source = DynamicModelSource("source.bytes", "0" * 32, "a" * 64, 192, 2)
            record = DynamicModelRecord(key="source.bytes:model:0:0", source_virtual_path="source.bytes", grid_index=0,
                model_index=0, record_offset=0, guid_words=(1, 2, 3, 4), path_hash_hex="0" * 16, position=(1., 2., 3.),
                rotation_euler_degrees=(0., 90., 0.), scale=(1., 1., 1.), grid_chain=(1, 2, 3), visibility_center=(0., 0., 0.),
                base_radius=100., min_distance=0., importance=1, raw_record_hex="00" * 96, prefab_path="Assets/source.prefab")
            inventory = DynamicModelInventory("map02", (source,), (record, replace(record,
                key="source.bytes:model:0:1", model_index=1, record_offset=96, position=(4., 5., 6.))))
            materials = {reference_key(material): material}
            textures = {("cab-texture", 3): (root, "texture.png")}
            plan = build_prefab_geometry_plan(inventory, (manifest,), frozenset({"prop", "unknown"}),
                {"assets/source.fbx": {reference_key(mesh_asset)}}, {reference_key(mesh_asset): (mesh_asset, mesh)},
                materials, textures, MeshUvCatalog({}, {}), {})
            self.assertEqual(plan.pending, ())
            self.assertEqual(len(plan.rows), 2)
            self.assertEqual(len(plan.exclusions), 6)
            self.assertEqual(sum(row["role"] == "source_renderer_shadows_only" for row in plan.exclusions), 4)
            self.assertTrue(all(row["entity_base"] == "shadowProxy_name_is_not_a_role" for row in plan.rows))
            self.assertEqual([row["renderer_owner"]["PathId"] for row in plan.rows], [2, 2])
            self.assertNotEqual(plan.rows[0]["source_key"], plan.rows[1]["source_key"])
            self.assertEqual(plan.rows[0]["matrix"][12:15], [1., 2., 3.])
            self.assertEqual(observed_prefab_geometry_paths((manifest,), frozenset({prefab}), materials, textures), frozenset({prefab}))
            payload = json.loads(json.dumps({"prefab_geometry_instances": plan.rows, "prefab_geometry_pending": plan.pending,
                "prefab_placements": plan.placements, "prefab_geometry_exclusions": plan.exclusions,
                "scene_manifests": [str(manifest)], "source_sha256": {source.virtual_path: source.decoded_sha256},
                "summary": {"selected_prefab_placements": 2, "ready_prefab_geometry": 2, "pending_prefab_geometry": 0, "excluded_prefab_geometry": 6}}))
            validate_prefab_geometry_plan(payload)
            external = build_prefab_geometry_plan(inventory, (manifest,), frozenset({"prop", "unknown"}),
                {}, {reference_key(mesh_asset): (mesh_asset, mesh)}, materials, textures, MeshUvCatalog({}, {}), {})
            self.assertFalse(external.rows)
            self.assertTrue(all("external mesh requires an exact source container" in row["detail"] for row in external.pending))
            mesh_asset["Source"] = "CAB-prefab"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            embedded = build_prefab_geometry_plan(inventory, (manifest,), frozenset({"prop", "unknown"}),
                {}, {reference_key(mesh_asset): (mesh_asset, mesh)}, materials, textures, MeshUvCatalog({}, {}), {})
            self.assertFalse(embedded.pending)
            self.assertEqual(len(embedded.rows), 2)
            self.assertTrue(all(row["mesh_resource_binding"] == "prefab_embedded_asset" for row in embedded.rows))
            self.assertTrue(all(row["mesh_path"] == "assets/source.prefab##p_prop_lod9" for row in embedded.rows))
            self.assertEqual(embedded.required_resource_paths, (prefab,))
            embedded_payload = {**payload, "prefab_geometry_instances": json.loads(json.dumps(embedded.rows)),
                                "prefab_geometry_exclusions": json.loads(json.dumps(embedded.exclusions))}
            validate_prefab_geometry_plan(embedded_payload)
            embedded_payload["prefab_geometry_instances"][0]["mesh_path"] = "assets/invented.fbx##p_prop_lod9"
            with self.assertRaisesRegex(ValueError, "mesh resource binding differs"):
                validate_prefab_geometry_plan(embedded_payload)
            mesh_asset["Source"] = "CAB-mesh"
            children[0]["RendererRole"]["PlatformCastShadows"] = 3
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "shadow-only source renderer role"):
                validate_prefab_geometry_plan(payload)
            del children[0]["RendererRole"]["PlatformCastShadows"]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(observed_prefab_geometry_paths((manifest,), frozenset({prefab}), materials, textures), frozenset())
            children[0]["RendererRole"]["PlatformCastShadows"] = 0
            manifest.write_text(json.dumps(data), encoding="utf-8")
            payload["prefab_geometry_instances"][0]["matrix"][12] += 1
            with self.assertRaisesRegex(ValueError, "source TRS"):
                validate_prefab_geometry_plan(payload)
            effects = build_prefab_geometry_plan(inventory, (manifest,), frozenset({"prop", "unknown"}),
                {"assets/arts/effects/source.fbx": {reference_key(mesh_asset)}}, {reference_key(mesh_asset): (mesh_asset, mesh)},
                materials, textures, MeshUvCatalog({}, {}), {})
            self.assertFalse(effects.rows)
            self.assertEqual([row["reason"] for row in effects.pending], ["effect_renderer_role_requires_separate_support"] * 2)
            original_mesh = mesh.read_text(encoding="utf-8")
            mesh.write_text("o source\nv 0 0 0\nv 1 0 0\nv 0 1 0\n" +
                            "".join(f"g source_{index}\nf 1 2 3\n" for index in range(3)), encoding="utf-8")
            children[0]["Materials"] = [material_ref, material_ref]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            partial = build_prefab_geometry_plan(inventory, (manifest,), frozenset({"prop", "unknown"}),
                {"assets/source.fbx": {reference_key(mesh_asset)}}, {reference_key(mesh_asset): (mesh_asset, mesh)},
                materials, textures, MeshUvCatalog({}, {}), {})
            self.assertEqual(len(partial.rows), 2)
            self.assertEqual([row["selected_submeshes"] for row in partial.rows], [[0, 1], [0, 1]])
            self.assertEqual([row["material_refs"][-1] for row in partial.rows], [None, None])
            partial_payload = {**payload, "prefab_geometry_instances": json.loads(json.dumps(partial.rows)),
                "prefab_geometry_pending": json.loads(json.dumps(partial.pending)),
                "summary": {**payload["summary"], "pending_prefab_geometry": 2}}
            validate_prefab_geometry_plan(partial_payload)
            partial_payload["prefab_geometry_pending"] = []
            with self.assertRaisesRegex(ValueError, "lacks explicit pending"):
                validate_prefab_geometry_plan(partial_payload)
            mesh.write_text(original_mesh, encoding="utf-8")
            children[0]["Materials"] = [material_ref]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(effects.pending[0]["source_renderer"]["Materials"], children[0]["Materials"])
            effect_material = {**material, "Name": "M_fx_source", "Shader": {**material["Shader"], "Name": "HGRP/Effect/VFXBaseV2"}}
            effects = build_prefab_geometry_plan(inventory, (manifest,), frozenset({"prop", "unknown"}),
                {"assets/source.fbx": {reference_key(mesh_asset)}}, {reference_key(mesh_asset): (mesh_asset, mesh)},
                {reference_key(material): effect_material}, textures, MeshUvCatalog({}, {}), {})
            self.assertFalse(effects.rows)
            self.assertEqual([row["reason"] for row in effects.pending], ["effect_renderer_role_requires_separate_support"] * 2)
            data["MeshBounds"] = [{"Asset": mesh_asset, "Minimum": [-1, 0, -1], "Maximum": [1, 1, 1]}]
            root_node["LocalPosition"] = [9999, 9999, 9999]
            children[0]["LocalPosition"] = [10, 0, 0]
            children[0]["LocalRotation"] = [0, 2 ** -0.5, 0, 2 ** -0.5]
            children[0]["LocalScale"] = [-2, 1, 1]
            children[2]["LocalPosition"] = [-9999, 0, 0]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            exterior = replace(record, position=(-5., 0., 5.), rotation_euler_degrees=(0., 0., 0.))
            far = replace(record, key="source.bytes:model:0:1", model_index=1, record_offset=96,
                          position=(100., 0., 5.), rotation_euler_degrees=(0., 0., 0.),
                          visibility_center=(5., 0., 5.), base_radius=1e9)
            all_models = DynamicModelInventory("map02", (source,), (exterior, far))
            whole = (0., 10., 0., 10.)
            left = select_prefab_geometry_inventory(all_models, (manifest,), (0., 5., 0., 10.), whole)
            right = select_prefab_geometry_inventory(all_models, (manifest,), (5., 10., 0., 10.), whole)
            independent = select_prefab_geometry_inventory(all_models, (manifest,), (5., 10., 0., 10.), (5., 10., 0., 10.))
            self.assertEqual([row.key for row in left.inventory.records], [exterior.key])
            self.assertFalse(right.inventory.records)
            self.assertEqual(independent.inventory.records, left.inventory.records)
            self.assertFalse(left.pending)
            self.assertEqual(left.outside_count, 1)
            proof = left.evidence[exterior.key]
            self.assertAlmostEqual(proof["minimum"][0], 4.)
            self.assertAlmostEqual(proof["maximum"][0], 6.)
            self.assertAlmostEqual(proof["minimum"][2], 3.)
            self.assertAlmostEqual(proof["maximum"][2], 7.)
            children[0]["RendererType"] = "SkinnedMeshRenderer"
            children[0]["RendererRole"]["SourceFields"] = {"m_AABB": {
                "m_Center": {"x": 0, "y": .5, "z": 0}, "m_Extent": {"x": 1, "y": .5, "z": 1}}}
            data["MeshBounds"][0]["Minimum"] = [-9999, -9999, -9999]
            data["MeshBounds"][0]["Maximum"] = [9999, 9999, 9999]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            skin = select_prefab_geometry_inventory(all_models, (manifest,), (0., 5., 0., 10.), whole)
            self.assertEqual(skin.inventory.records, left.inventory.records)
            self.assertEqual(skin.evidence[exterior.key]["minimum"], proof["minimum"])
            self.assertIn("skinned_renderer_local_bounds", skin.evidence[exterior.key]["renderers"][0]["bounds_source"])
            children[0]["Mesh"] = None
            children[0]["RendererRole"]["SourceFields"]["m_Mesh"] = {"m_FileID": 0, "m_PathID": 0}
            manifest.write_text(json.dumps(data), encoding="utf-8")
            empty_skin = select_prefab_geometry_inventory(all_models, (manifest,), whole, whole)
            self.assertFalse(empty_skin.inventory.records)
            self.assertFalse(empty_skin.pending)
            self.assertEqual(empty_skin.empty_count, 2)
            children[0]["RendererRole"]["SourceFields"]["m_Mesh"]["m_PathID"] = 999
            manifest.write_text(json.dumps(data), encoding="utf-8")
            unresolved_skin = select_prefab_geometry_inventory(all_models, (manifest,), whole, whole)
            self.assertEqual(len(unresolved_skin.pending), 2)
            self.assertTrue(all("unresolved" in row["detail"] for row in unresolved_skin.pending))
            children[0]["Mesh"] = mesh_asset
            children[0]["RendererType"] = "MeshRenderer"
            del children[0]["RendererRole"]["SourceFields"]
            data["MeshBounds"][0]["Minimum"] = [-1, 0, -1]
            data["MeshBounds"][0]["Maximum"] = [1, 1, 1]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            from dataclasses import asdict
            validated = {**asdict(exterior), "geometry_selection": proof}
            validate_prefab_selection_evidence(validated, dict(zip(("xmin", "xmax", "zmin", "zmax"), (0., 5., 0., 10.))))
            proof["minimum"][0] -= 1
            with self.assertRaisesRegex(ValueError, "differ from source geometry"):
                validate_prefab_selection_evidence(validated, dict(zip(("xmin", "xmax", "zmin", "zmax"), (0., 5., 0., 10.))))
            del data["MeshBounds"]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            missing_bounds = select_prefab_geometry_inventory(all_models, (manifest,), whole, whole)
            self.assertFalse(missing_bounds.inventory.records)
            self.assertEqual([row["reason"] for row in missing_bounds.pending], ["prefab_geometry_bounds_unavailable"] * 2)
            del data["LodGroups"]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            missing = build_prefab_geometry_plan(inventory, (manifest,), frozenset({"prop", "unknown"}),
                {"assets/source.fbx": {reference_key(mesh_asset)}}, {reference_key(mesh_asset): (mesh_asset, mesh)},
                materials, textures, MeshUvCatalog({}, {}), {})
            self.assertFalse(missing.rows)
            self.assertEqual(len(missing.pending), 2)
            self.assertEqual(observed_prefab_geometry_paths((manifest,), frozenset({prefab}), materials, textures), frozenset())

    def test_native_material_array_preserves_surplus_passes(self) -> None:
        self.assertEqual(prefab_material_pass_indices(2, 4), [[0], [1, 2, 3]])
        self.assertEqual(prefab_material_pass_indices(3, 1), [[0], [0], [0]])
        self.assertEqual(prefab_material_pass_indices(3, 2), [[0], [1], []])
        with self.assertRaisesRegex(ValueError, "mapping unsupported"):
            prefab_material_pass_indices(3, 0)


if __name__ == "__main__":
    unittest.main()
