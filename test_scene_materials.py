"""Exercise material catalog contracts with real files and no Blender dependency."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endfield_scene_material_contract import load_catalog, supported_slots, validate_material, surface_limitations
from endfield_scene_material_contract import resolve_export_path
from endfield_scene_material_contract import merge_material_evidence


def material_record() -> dict[str, object]:
    return {"Name": "surface", "Source": "CAB-material", "PathId": 1,
            "Shader": {"Source": "CAB-shader", "PathId": 2},
            "Textures": [{"Property": "_BaseColorMap", "Texture": {"Source": "CAB-texture", "PathId": 3},
                          "Scale": [1, 1], "Offset": [0, 0]}],
            "Floats": {}, "Colors": {"_BaseColor": [1, 1, 1, 1]}}


class SceneMaterialTests(unittest.TestCase):
    def test_render_state_enrichment_preserves_source_and_rejects_conflicts(self) -> None:
        original = validate_material(material_record())
        state = {"DisabledShaderPasses": ["DepthOnly"], "ValidKeywords": [], "CustomRenderQueue": 2000}
        enriched = validate_material({**original, "Shader": {**original["Shader"], "Name": "HGRP/Lit",
            "PassNames": ["HGBuffer", "ShadowCaster", "DepthOnly"]}, "RenderState": state})
        self.assertEqual(enriched, merge_material_evidence(original, enriched))
        self.assertEqual(enriched, merge_material_evidence(enriched, original))
        self.assertNotIn("RenderState", original)
        with self.assertRaisesRegex(ValueError, "Conflicting material render state"):
            merge_material_evidence(enriched, {**enriched, "RenderState": {**state, "DisabledShaderPasses": []}})
        with self.assertRaisesRegex(ValueError, "integer CustomRenderQueue"):
            validate_material({**original, "RenderState": {**state, "CustomRenderQueue": "2000"}})

    def test_long_export_paths_remain_visible_to_the_shared_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = '/'.join(('a' * 90, 'b' * 90, 'c' * 90, 'texture.png'))
            texture = resolve_export_path(root / relative)
            texture.parent.mkdir(parents=True)
            texture.write_bytes(b'path visibility contract')
            manifest = root / 'probe.json'
            manifest.write_text(json.dumps({'Format': 'EndfieldSceneProbe/1', 'Materials': [material_record()],
                'TextureExports': [{'Asset': {'Source': 'CAB-texture', 'PathId': 3}, 'File': relative}]}), encoding='utf-8')
            _, textures = load_catalog((manifest,))
            folder, name = textures[('cab-texture', 3)]
            self.assertEqual(b'path visibility contract', (folder / name).read_bytes())

    def test_unsupported_enabled_emission_is_rejected_before_scene_mutation(self) -> None:
        original = material_record()
        textures = [*original['Textures'], {'Property': '_EmissiveMap', 'Texture': {'Source': 'CAB-emission', 'PathId': 8},
                                           'Scale': [1, 1], 'Offset': [0, 0]}]
        enabled = validate_material({**original, 'Textures': textures,
                                     'Floats': {'_EnableEmissiveMap': 1, '_EmissiveMaskChannel': 5}})
        with self.assertRaisesRegex(ValueError, 'Unsupported emissive mask selection'):
            supported_slots(enabled)
        disabled = validate_material({**original, 'Textures': textures,
                                      'Floats': {'_EnableEmissiveMap': 0, '_EmissiveMaskChannel': 5}})
        self.assertEqual(('_BaseColorMap',), tuple(slot['Property'] for slot in supported_slots(disabled)))

    def test_procedural_foliage_keeps_static_surface_with_explicit_glow_limitation(self) -> None:
        original = material_record()
        row = validate_material({**original, "Floats": {"_EnableEmissiveMap": 1}, "Textures": [
            *original["Textures"],
            {"Property": "_FoliageGlowNoise", "Texture": {"Source": "CAB-noise", "PathId": 7},
             "Scale": [1, 1], "Offset": [0, 0]}]})
        self.assertEqual(("_BaseColorMap",), tuple(slot["Property"] for slot in supported_slots(row)))
        self.assertIn("procedural_foliage_glow_not_reconstructed", surface_limitations(row))

    def test_shared_texture_catalog_and_conflicting_materials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            texture = root / "texture.png"
            texture.write_bytes(b"catalog checks paths without decoding image data")
            record = material_record()
            manifest = {"Format": "EndfieldSceneProbe/1", "Materials": [record],
                        "TextureExports": [{"Asset": {"Source": "CAB-texture", "PathId": 3}, "File": "texture.png"}]}
            first = root / "first.json"
            first.write_text(json.dumps(manifest), encoding="utf-8")
            materials, textures = load_catalog((first, first))
            self.assertEqual(1, len(materials))
            self.assertEqual((root, "texture.png"), textures[("cab-texture", 3)])
            second = root / "second.json"
            second.write_text(json.dumps({**manifest, "Materials": [{**record, "Name": "conflicting"}]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Conflicting material"):
                load_catalog((first, second))

    def test_missing_texture_remains_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Materials": [material_record()],
                                      "TextureExports": []}), encoding="utf-8")
            materials, textures = load_catalog((path,))
            self.assertEqual({}, textures)
            self.assertEqual(1, len(supported_slots(materials[("cab-material", 1)])))

    def test_texture_color_space_enrichment_preserves_legacy_catalog_and_rejects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "texture.png").write_bytes(b"catalog path contract")
            original = material_record()
            legacy = root / "legacy.json"
            original_json = json.dumps({"Format": "EndfieldSceneProbe/1", "Materials": [original],
                "TextureExports": [{"Asset": {"Source": "CAB-texture", "PathId": 3}, "File": "texture.png"}]})
            legacy.write_text(original_json, encoding="utf-8")
            evidence = root / "evidence.json"
            conflict = root / "conflict.json"
            for path, space in ((evidence, 0), (conflict, 1)):
                path.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Materials": [],
                    "TextureExports": [{"Asset": {"Source": "CAB-texture", "PathId": 3,
                        "ColorSpace": space}, "File": "texture.png"}]}), encoding="utf-8")
            for paths in ((legacy, evidence), (evidence, legacy)):
                materials, textures = load_catalog(paths)
                self.assertEqual(0, materials[("cab-material", 1)]["Textures"][0]["Texture"]["ColorSpace"])
                self.assertEqual((root, "texture.png"), textures[("cab-texture", 3)])
            self.assertEqual(original_json, legacy.read_text(encoding="utf-8"))
            legacy_materials, _ = load_catalog((legacy,))
            self.assertNotIn("ColorSpace", legacy_materials[("cab-material", 1)]["Textures"][0]["Texture"])
            with self.assertRaisesRegex(ValueError, "Conflicting texture ColorSpace"):
                load_catalog((legacy, evidence, conflict))

    def test_nonfinite_values_and_missing_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            validate_material({**material_record(), "Floats": {"_Metallic": float("nan")}})
        with self.assertRaisesRegex(ValueError, "Textures"):
            validate_material({**material_record(), "Textures": None})
        with self.assertRaisesRegex(ValueError, "integer PathId"):
            validate_material({**material_record(), "PathId": True})

    def test_optional_unbound_slots_are_not_required_textures(self) -> None:
        row = validate_material({**material_record(), "Textures": [
            {"Property": "_NormalMap", "Texture": None, "Scale": [1, 1], "Offset": [0, 0]}]})
        self.assertEqual((), supported_slots(row))

    def test_enabled_mask_without_texture_fails_explicitly(self) -> None:
        row = validate_material({**material_record(), "Floats": {"_EnableTriChannelMask": 1}})
        with self.assertRaisesRegex(ValueError, "lacks authored texture"):
            supported_slots(row)

    def test_serialized_null_is_authored_empty_without_mutating_input(self) -> None:
        source = {**material_record(), "Floats": {"_EnableEmissiveMap": 1}, "Textures": [
            {"Property": "_EmissiveMap", "Texture": {"Source": "CAB-texture", "PathId": 0},
             "Scale": [1, 1], "Offset": [0, 0]}]}
        before = json.dumps(source)
        row = validate_material(source)
        self.assertIsNone(row["Textures"][0]["Texture"])
        self.assertEqual((), supported_slots(row))
        self.assertEqual(before, json.dumps(source))


if __name__ == "__main__":
    unittest.main()
