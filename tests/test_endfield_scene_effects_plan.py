"""Integration checks for versioned FX prefab evidence and bounded anchors."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endfield_scene_effects_plan import build_effect_plan, observed_effect_prefabs
from endfield_scene_io import read_scene_manifest, scene_io_session
from endfield_scene_resources import ready_resource_paths
from extractor_core.effects import EffectRecord, EffectSource, MapEffectInventory
from extractor_core.particle_snapshot import PARTICLE_PARAMETER_MODULE_FIELDS, PARTICLE_PARAMETER_SCHEMA_VERSION


PREFAB = "assets/effects/p_fx.prefab"


def _manifest(version: int) -> dict[str, object]:
    initial: dict[str, object] = {}
    if version in (5, PARTICLE_PARAMETER_SCHEMA_VERSION):
        initial = {"StartRotationX": {}, "StartRotationY": {}, "StartRotation": {},
                   "GravityModifier": {}, "RandomizeRotationDirection": 0.0, "Rotation3D": False}
    return {
        "Format": "EndfieldSceneProbe/1",
        "Containers": [{"Container": PREFAB, "Asset": {"Source": "CAB-root", "PathId": 1,
                                                     "Type": "GameObject", "Name": "P_fx"}}],
        "Nodes": [
            {"Source": "CAB-root", "PathId": 1, "TransformPathId": 10,
             "Parent": {"Source": "CAB-root", "PathId": 0, "Type": "Transform"}},
            {"Source": "CAB-root", "PathId": 2, "TransformPathId": 20,
             "Parent": {"Source": "CAB-root", "PathId": 10, "Type": "Transform"}},
        ],
        "Particles": {
            "DataScope": "component_parameters_no_simulation", "ModuleDataScope": "typed_modules",
            "Systems": [{"Asset": {"Source": "CAB-root", "PathId": 3, "Type": "ParticleSystem"},
                         "Parameters": {**{field: {} for field in PARTICLE_PARAMETER_MODULE_FIELDS},
                                        "SchemaVersion": version, "Initial": initial}}],
            "Renderers": [{"Asset": {"Source": "CAB-root", "PathId": 4, "Type": "ParticleSystemRenderer"},
                           "Materials": [{"IsResolved": True, "ResolutionStatus": "resolved",
                                          "Target": {"Source": "CAB-material", "PathId": 5, "Type": "Material"}}]}],
            "Pairs": [{"GameObject": {"Source": "CAB-root", "PathId": 2, "Type": "GameObject"},
                       "System": {"Source": "CAB-root", "PathId": 3, "Type": "ParticleSystem"},
                       "Renderer": {"Source": "CAB-root", "PathId": 4, "Type": "ParticleSystemRenderer"}}],
            "Diagnostics": [],
        },
        "Materials": [{"Name": "M_fx", "Source": "CAB-material", "PathId": 5,
                       "Shader": {"Source": "CAB-shader", "PathId": 6,
                                  "Name": "HGRP/Effect/VFXBase"},
                       "Textures": [], "Floats": {}, "Colors": {}}],
        "TextureExports": [],
        "MeshExports": [],
    }


class EffectPlanIntegrationTests(unittest.TestCase):
    def test_supported_versions_and_new_module_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version in (4, 5, 6):
                manifest = root / f"schema_{version}.json"
                manifest.write_text(json.dumps(_manifest(version)), encoding="utf-8")
                self.assertEqual(PREFAB in observed_effect_prefabs((manifest,)), version in (5, 6))
            for field in ("RotationOverLifetime", "ClampVelocity"):
                manifest = root / f"missing_{field}.json"
                payload = _manifest(6)
                del payload["Particles"]["Systems"][0]["Parameters"][field]
                manifest.write_text(json.dumps(payload), encoding="utf-8")
                self.assertNotIn(PREFAB, observed_effect_prefabs((manifest,)))

    def test_readiness_rechecks_payloads_with_shared_manifest_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "scene_manifest.json"
            texture = {"Source": "CAB-texture", "PathId": 7,
                       "Type": "Texture2D", "ColorSpace": 0}
            payload = _manifest(PARTICLE_PARAMETER_SCHEMA_VERSION)
            payload["Materials"][0]["Textures"] = [
                {"Property": "_MainTex", "Texture": texture,
                 "Scale": [1.0, 1.0], "Offset": [0.0, 0.0]}]
            payload["TextureExports"] = [{"Asset": texture, "File": "texture.png"}]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            texture_file = root / "texture.png"
            texture_file.write_bytes(b"texture fixture")
            with scene_io_session():
                snapshot = read_scene_manifest(manifest)
                self.assertIn(PREFAB, ready_resource_paths((manifest,)))
                texture_file.unlink()
                self.assertNotIn(PREFAB, ready_resource_paths((manifest,)))
                texture_file.write_bytes(b"restored texture fixture")
                self.assertIn(PREFAB, ready_resource_paths((manifest,)))
                self.assertIs(snapshot, read_scene_manifest(manifest))

    def test_mesh_renderer_prefab_and_mixed_particle_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "mesh_scene_manifest.json"
            (root / "mesh.obj").write_text("g mesh\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
            payload = _manifest(PARTICLE_PARAMETER_SCHEMA_VERSION)
            payload["Nodes"][0]["GameObjectActive"] = True
            owner = payload["Nodes"][1]
            owner.update({
                "GameObjectActive": True, "RendererEnabled": True,
                "RendererType": "MeshRenderer",
                "RendererAsset": {"Source": "CAB-root", "PathId": 8, "Type": "MeshRenderer"},
                "Mesh": {"Source": "CAB-mesh", "PathId": 9, "Type": "Mesh"},
                "Materials": [{"Source": "CAB-material", "PathId": 5, "Type": "Material"}],
            })
            payload["MeshExports"] = [{
                "Asset": {"Source": "CAB-mesh", "PathId": 9, "Type": "Mesh"},
                "File": "mesh.obj", "VertexCount": 3, "VertexColorComponents": 0,
                "Uv0Components": 0, "Uv1Components": 0, "Uv0File": None, "Uv1File": None,
            }]
            payload["Particles"]["Systems"] = []
            payload["Particles"]["Renderers"] = []
            payload["Particles"]["Pairs"] = []
            old_manifest = root / "old_mesh_scene_manifest.json"
            old_payload = json.loads(json.dumps(payload))
            del old_payload["MeshExports"][0]["Uv0Components"]
            del old_payload["MeshExports"][0]["Uv1Components"]
            old_manifest.write_text(json.dumps(old_payload), encoding="utf-8")
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            source = "Data/DynamicStreaming/PC/Scene/map02/fb_main_1_0000_0000.bytes"
            inventory = MapEffectInventory("map02", (EffectSource(source, "A" * 32, 0, 1, 1),),
                                           (EffectRecord(source + ":0:0", source, 0, 0, 0,
                                                         "0" * 16, PREFAB, (0.0, 2.0, 0.0),
                                                         (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),), (PREFAB,))
            materials = {("cab-material", 5): {
                "Name": "M_fx", "Source": "CAB-material", "PathId": 5,
                "Shader": {"Source": "CAB-shader", "PathId": 6, "Name": "HGRP/Effect/VFXBaseV2"},
                "Textures": [], "Floats": {}, "Colors": {},
            }}
            bounds = {"xmin": 0.0, "xmax": 1.0, "zmin": 0.0, "zmax": 1.0}
            result = build_effect_plan(inventory, bounds, (manifest,), materials, {})
            self.assertEqual((len(result.rows), len(result.pending)), (1, 0))
            self.assertEqual(result.rows[0]["component_kind"], "mesh_renderer")
            self.assertIn(PREFAB, observed_effect_prefabs((manifest,)))
            self.assertNotIn(PREFAB, observed_effect_prefabs((old_manifest,)))
            selected = build_effect_plan(inventory, bounds, (old_manifest, manifest), materials, {})
            self.assertEqual(selected.rows[0]["scene_manifest"], str(manifest.resolve()))

            payload["Particles"] = _manifest(PARTICLE_PARAMETER_SCHEMA_VERSION)["Particles"]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            mixed = build_effect_plan(inventory, bounds, (manifest,), materials, {})
            self.assertEqual(mixed.rows[0]["component_kind"], "mixed")
            payload["Particles"]["Pairs"] = []
            payload["Particles"]["Systems"][0]["GameObject"] = {
                "Target": {"Source": "CAB-root", "PathId": 2, "Type": "GameObject"}}
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            invalid = build_effect_plan(inventory, bounds, (manifest,), materials, {})
            self.assertEqual((len(invalid.rows), invalid.pending[0]["reason"]),
                             (0, "particle_component_invalid"))

    def test_only_missing_particle_material_dependencies_require_reprobe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "scene_manifest.json"
            payload = _manifest(PARTICLE_PARAMETER_SCHEMA_VERSION)
            material = payload["Materials"][0]
            material["Shader"]["Name"] = "HGRP/Effect/UnimplementedVariant"
            material["Floats"]["_UseDissolve"] = 1.0
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIn(PREFAB, observed_effect_prefabs((manifest,)))
            self.assertIn(PREFAB, ready_resource_paths((manifest,)))

            pointer = payload["Particles"]["Renderers"][0]["Materials"][0]
            pointer["IsResolved"] = False
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotIn(PREFAB, ready_resource_paths((manifest,)))
            pointer["IsResolved"] = True

            payload["Materials"] = []
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotIn(PREFAB, observed_effect_prefabs((manifest,)))
            self.assertNotIn(PREFAB, ready_resource_paths((manifest,)))

            payload["Materials"] = [material]
            material["Shader"].pop("Name")
            material["Textures"] = [{"Property": "_MainTex", "Scale": [1.0, 1.0],
                                      "Offset": [0.0, 0.0],
                                      "Texture": {"Source": "CAB-texture", "PathId": 7,
                                                  "Type": "Texture2D", "ColorSpace": 0}}]
            payload["TextureExports"] = [{"Asset": {"Source": "CAB-texture", "PathId": 7,
                                                     "Type": "Texture2D", "ColorSpace": 0},
                                          "File": "texture.bin"}]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotIn(PREFAB, observed_effect_prefabs((manifest,)))
            self.assertNotIn(PREFAB, ready_resource_paths((manifest,)))
            old = root / "old_without_pairs.json"
            old_payload = _manifest(PARTICLE_PARAMETER_SCHEMA_VERSION)
            old_payload["Particles"]["Pairs"] = []
            old_payload["Materials"] = []
            old.write_text(json.dumps(old_payload), encoding="utf-8")
            self.assertIn(PREFAB, ready_resource_paths((old,)))
            self.assertNotIn(PREFAB, ready_resource_paths((old, manifest)))
            (root / "texture.bin").write_bytes(b"texture fixture")
            self.assertIn(PREFAB, observed_effect_prefabs((manifest,)))
            self.assertIn(PREFAB, ready_resource_paths((manifest,)))

    def test_empty_active_vfx_texture_refreshes_only_missing_shader_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "scene_manifest.json"
            payload = _manifest(PARTICLE_PARAMETER_SCHEMA_VERSION)
            material = payload["Materials"][0]
            material["Shader"]["Name"] = "HGRP/Effect/VFXBaseV2"
            material["Textures"] = [{"Property": "_MainTex", "Scale": [1.0, 1.0],
                                      "Offset": [0.0, 0.0], "Texture": None}]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotIn(PREFAB, ready_resource_paths((manifest,)))
            old = root / "legacy.json"
            old.write_text(json.dumps(payload), encoding="utf-8")
            material["Shader"]["TextureDefaults"] = [
                {"Property": "_MainTex", "Name": "white", "Dimension": 2}]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIn(PREFAB, ready_resource_paths((manifest,)))
            self.assertIn(PREFAB, ready_resource_paths((old, manifest)))
            material["Shader"]["TextureDefaults"] = []
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIn(PREFAB, ready_resource_paths((manifest,)))
            del material["Shader"]["TextureDefaults"]
            material["Textures"][0]["Property"] = "_SampleTex5"
            material["Floats"]["_UseSampleTex5"] = 0
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIn(PREFAB, ready_resource_paths((manifest,)))
            material["Floats"]["_UseSampleTex5"] = 1
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotIn(PREFAB, ready_resource_paths((manifest,)))

    def test_missing_fx_texture_color_space_requires_reprobe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "legacy.json"
            texture = {"Source": "CAB-texture", "PathId": 7, "Type": "Texture2D"}
            payload = _manifest(PARTICLE_PARAMETER_SCHEMA_VERSION)
            material = payload["Materials"][0]
            material["Shader"]["Name"] = "HGRP/Effect/VFXBaseV2"
            material["Textures"] = [{"Property": "_MainTex", "Texture": texture,
                                      "Scale": [1.0, 1.0], "Offset": [0.0, 0.0]}]
            payload["TextureExports"] = [{"Asset": texture, "File": "texture.png"}]
            (root / "texture.png").write_bytes(b"texture fixture")
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            self.assertNotIn(PREFAB, ready_resource_paths((manifest,)))

            supplement = root / "supplement.json"
            current_texture = {**texture, "ColorSpace": 0}
            supplement.write_text(json.dumps({
                "Format": "EndfieldSceneProbe/1", "Materials": [], "Containers": [],
                "MeshExports": [],
                "TextureExports": [{"Asset": current_texture, "File": "texture.png"}],
            }), encoding="utf-8")
            self.assertIn(PREFAB, ready_resource_paths((manifest, supplement)))

    def test_offset_texture_sampler_refresh_is_feature_scoped_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "legacy.json"
            texture = {"Source": "CAB-offset", "PathId": 8, "Type": "Texture2D",
                       "ColorSpace": 0}
            payload = _manifest(PARTICLE_PARAMETER_SCHEMA_VERSION)
            material = payload["Materials"][0]
            material["Shader"]["Name"] = "HGRP/Effect/VFXBaseV2"
            material["Floats"]["_UseVertexOffset"] = 1.0
            material["Textures"] = [{"Property": "_OffsetTex", "Texture": texture,
                                      "Scale": [1.0, 1.0], "Offset": [0.0, 0.0]}]
            payload["TextureExports"] = [{"Asset": texture, "File": "offset.png"}]
            (root / "offset.png").write_bytes(b"offset fixture")
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            self.assertNotIn(PREFAB, ready_resource_paths((manifest,)))

            supplement = root / "sampler.json"
            sampler = {"FilterMode": 1, "WrapU": 0, "WrapV": 1, "WrapW": 0,
                       "Aniso": 1, "MipBias": 0.0}
            supplement.write_text(json.dumps({
                "Format": "EndfieldSceneProbe/1", "Materials": [], "Containers": [],
                "MeshExports": [],
                "TextureExports": [{
                    "Asset": {**texture, "TextureSampler": sampler}, "File": "offset.png"}],
            }), encoding="utf-8")
            self.assertIn(PREFAB, ready_resource_paths((manifest, supplement)))

            material["Floats"]["_UseVertexOffset"] = 0.0
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIn(PREFAB, ready_resource_paths((manifest,)))

            invalid = {**sampler, "WrapU": 9}
            supplement.write_text(json.dumps({
                "Format": "EndfieldSceneProbe/1", "Materials": [], "Containers": [],
                "MeshExports": [],
                "TextureExports": [{
                    "Asset": {**texture, "TextureSampler": invalid}, "File": "offset.png"}],
            }), encoding="utf-8")
            material["Floats"]["_UseVertexOffset"] = 1.0
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid texture sampler field"):
                ready_resource_paths((manifest, supplement))

    def test_current_schema_wins_over_older_snapshot_for_same_prefab(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "a_old.json"
            current = root / "z_current.json"
            old.write_text(json.dumps(_manifest(4)), encoding="utf-8")
            current.write_text(json.dumps(_manifest(PARTICLE_PARAMETER_SCHEMA_VERSION)), encoding="utf-8")
            source = "Data/DynamicStreaming/PC/Scene/map01/fb_main_1_0000_0000.bytes"
            inventory = MapEffectInventory("map01", (EffectSource(source, "A" * 32, 0, 1, 1),),
                                           (EffectRecord(source + ":0:0", source, 0, 0, 0,
                                                         "0" * 16, PREFAB, (0.0, 2.0, 0.0),
                                                         (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),), (PREFAB,))
            materials = {("cab-material", 5): {
                "Name": "M_fx", "Source": "CAB-material", "PathId": 5,
                "Shader": {"Source": "CAB-shader", "PathId": 6, "Name": "HGRP/Effect/VFXBase"},
                "Textures": [], "Floats": {}, "Colors": {},
            }}
            bounds = {"xmin": 0.0, "xmax": 1.0, "zmin": 0.0, "zmax": 1.0}
            self.assertNotIn(PREFAB, observed_effect_prefabs((old,)))
            self.assertIn(PREFAB, observed_effect_prefabs((current,)))
            result = build_effect_plan(inventory, bounds, (old, current), materials, {})
            self.assertEqual((result.selected_count, len(result.rows), len(result.pending)), (1, 1, 0))
            self.assertEqual(result.rows[0]["scene_manifest"], str(current.resolve()))
            self.assertEqual(result.rows[0]["matrix"][12:15], [0.0, 2.0, 0.0])
            old_only = build_effect_plan(inventory, bounds, (old,), materials, {})
            self.assertEqual(old_only.pending[0]["reason"], "particle_parameter_schema_unsupported")


if __name__ == "__main__":
    unittest.main()
