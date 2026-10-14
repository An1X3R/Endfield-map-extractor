from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from endfield_scene_export import SceneArtifact
from webui_instanced_results import build_instanced_scene_export_result


class InstancedSceneResultTests(unittest.TestCase):
    def _artifact(self, root: Path, water_status: str) -> SceneArtifact:
        batch = root / "scenes" / "batch_0001"
        batch.mkdir(parents=True)
        scene_path = batch / "scene.blend"
        audit_path = batch / "scene_audit.json"
        plan_path = batch / "scene_plan.json"
        scene_path.write_bytes(b"verified blend payload")

        water_depth_pending = [{
            "entity_id": -1,
            "entity_base": "selection",
            "component": "water_depth",
            "reason": "terrain_height_source_missing",
            "detail": "Water depth has no verified heightfield context",
        }]
        plan = {
            "format": "EndfieldPortableScenePlan/1",
            "instances": [{"entity_id": 1, "category": "building"}],
            "dynamic_geometry_instances": [{"entity_id": 2, "category": "vegetation"}],
            "surface_effect_instances": [],
            "effect_instances": [{"entity_id": -10, "component_kind": "mixed"}],
            "direct_effect_renderers": [],
            "light_instances": [],
            "water_geometry": [{"entity_id": 200}],
            "water_materials": [],
            "terrain_surfaces": [],
            "terrain_details": [],
            "pending": [{
                "entity_id": 99,
                "entity_base": "decal",
                "component": "projected_decal",
                "reason": "decal_receiver_missing",
                "detail": "No receiver surface",
            }],
            "water_depth_pending": water_depth_pending,
            "direct_effect_pending": [{
                "source_key": "stream:1:2",
                "source_pair_index": 0,
                "component": "direct_effect",
                "reason": "renderer_material_missing",
                "detail": "Material evidence is incomplete",
            }],
            "dynamic_geometry_pending": [{
                "source_key": "stream:3:4",
                "component": "dynamic_geometry",
                "reason": "mesh_export_missing",
                "detail": "Mesh evidence is incomplete",
            }],
        }
        audit = {
            "format": "EndfieldPortableSceneAudit/1",
            "reopen_verified": False,
            "instances": 2,
            "pending": 9,
            "decals": [{"entity_id": 99, "status": "no_receiver_surface"}],
            "water": [{"entity_id": 200, "status": water_status}],
            "effects": [{
                "entity_id": -10,
                "object": "EF_FX_MESH_-10_1_0",
                "system_source": "CAB-1",
                "system_path_id": -1,
                "owner_source": "CAB-1",
                "owner_path_id": -2,
                "status": "unsupported_material",
                "reason": "FX mesh material features are not implemented: material=M_fx, features=('particle_input_on_mesh_renderer',)",
            }, {
                "entity_id": -10,
                "object": "EF_FX_-10_101",
                "system_source": "CAB-1",
                "system_path_id": 101,
                "owner_source": "CAB-1",
                "owner_path_id": -2,
                "status": "static_preview_partial",
                "unsupported_modules": ["InitialModule.StartRotationY"],
            }],
            "lights": [{"map_id": "dung01_sdg003", "entity_id": 300,
                        "object": "EF_LIGHT_dung01_sdg003_300",
                        "status": "source_light_preview_partial"}],
            "material_pending": [{"material": "M_source", "component": "emission_sweep",
                                  "status": "source_formula_preview_runtime_unresolved"}],
            "direct_effect_pending": plan["direct_effect_pending"],
            "dynamic_geometry_pending": plan["dynamic_geometry_pending"],
            "water_depth_context_pending": water_depth_pending,
        }
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
        return cast(SceneArtifact, {
            "path": str(scene_path.relative_to(root)),
            "audit": str(audit_path.relative_to(root)),
            "plan": str(plan_path.relative_to(root)),
            "bytes": scene_path.stat().st_size,
            "sha256": hashlib.sha256(scene_path.read_bytes()).hexdigest(),
            "instances": 2,
            "pending": 9,
        })

    def test_result_deduplicates_plan_and_audit_issues_and_hashes_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = self._artifact(root, "bottom_geometry_incomplete")

            result = build_instanced_scene_export_result(root, [artifact])

            self.assertEqual(result["format"], "InstancedSceneExportResult/1")
            self.assertEqual(result["status"], "partial")
            self.assertEqual([file["role"] for file in result["files"]], ["scene", "audit", "plan"])
            self.assertTrue(all(len(file["sha256"]) == 64 for file in result["files"]))
            self.assertEqual(len(result["pending"]), 9)
            self.assertEqual(sum(item["code"].startswith("decal.") for item in result["pending"]), 1)
            self.assertEqual(result["component_summary"]["building"]["exported"], 1)
            self.assertEqual(result["component_summary"]["vegetation"]["exported"], 1)
            self.assertEqual(result["component_summary"]["effects"]["pending"], 3)
            self.assertEqual(result["component_summary"]["water"]["pending"], 2)
            self.assertEqual(result["component_summary"]["effects"]["unsupported"], 1)
            self.assertEqual(result["component_summary"]["particle"]["exported"], 1)
            self.assertEqual(result["component_summary"]["particle"]["pending"], 1)
            self.assertEqual(result["component_summary"]["unknown"]["pending"], 2)
            mesh_fx_pending = next(item for item in result["pending"]
                                   if item["code"] == "effect.unsupported_material")
            particle_fx_pending = next(item for item in result["pending"]
                                       if item["code"] == "effect.static_preview_partial"
                                       and item["group_id"] == "particle")
            self.assertEqual(mesh_fx_pending["group_id"], "effects")
            self.assertIn("EF_FX_MESH_-10_1_0", mesh_fx_pending["message"])
            self.assertIn("particle_input_on_mesh_renderer", mesh_fx_pending["message"])
            self.assertIn("InitialModule.StartRotationY", particle_fx_pending["message"])
            self.assertIn("EF_FX_-10_101", particle_fx_pending["message"])
            self.assertNotIn("Unresolved", " ".join(item["message"] for item in result["pending"]))
            self.assertTrue(all("定位：" in item["message"] and "batch_0001" in item["message"]
                                for item in result["pending"]))
            water_message = next(item["message"] for item in result["pending"]
                                 if item["code"] == "water_depth.terrain_height_source_missing")
            self.assertIn("缺少经验证的地形高度源", water_message)
            self.assertIn("实体 ID -1", water_message)
            self.assertIn("batch_0001", water_message)
            light_message = next(item["message"] for item in result["pending"]
                                 if item["code"] == "lighting.source_light_preview_partial")
            self.assertIn("EF_LIGHT_dung01_sdg003_300", light_message)
            material_message = next(item["message"] for item in result["pending"]
                                    if item["code"].startswith("material."))
            self.assertIn("材质 M_source", material_message)

    def test_unknown_audit_status_produces_nullable_export_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = self._artifact(root, "future_water_status")

            result = build_instanced_scene_export_result(root, [artifact])

            self.assertIsNone(result["component_summary"]["water"]["exported"])
            self.assertEqual(result["component_summary"]["water"]["pending"], 2)

    def test_rejects_path_escape_and_scene_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = self._artifact(root, "restored")
            escaped = dict(artifact)
            escaped["path"] = "../scene.blend"
            with self.assertRaisesRegex(ValueError, "safe relative output path"):
                build_instanced_scene_export_result(root, [cast(SceneArtifact, escaped)])

            corrupted = dict(artifact)
            corrupted["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
                build_instanced_scene_export_result(root, [cast(SceneArtifact, corrupted)])


if __name__ == "__main__":
    unittest.main()
