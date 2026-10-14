from __future__ import annotations

import unittest

from endfield_scene_vfxrefract_contract import read_scene_color_snapshot, vfxrefract_snapshot_issues


def snapshot() -> dict[str, object]:
    return {
        "format": "EndfieldSceneColorSnapshot/1",
        "camera_matrix_world": [1, 0, 0, 0.25, 0, 1, 0, 0.5, 0, 0, 1, 0.75, 0, 0, 0, 1],
        "lens_mm": 50,
        "resolution": [640, 480],
        "engine": "BLENDER_EEVEE_NEXT",
        "samples": 16,
        "time_seconds": 2.5,
        "clip_start": 0.1,
        "clip_end": 1000,
    }


def material_record() -> dict[str, object]:
    floats = {
        "_SurfaceType": 1, "_BlendMode": 0, "_SrcBlend": 5, "_DstBlend": 10,
        "_AlphaSrcBlend": 1, "_AlphaDstBlend": 10, "_ZWrite": 0, "_ZTest": 4,
        "_DisableZTest": 0, "_UseMask": 1, "_UseRBOffset": 1, "_UseRGBOffset": 0,
        "_UseBlend": 0, "_UseSaturation": 0, "_UseColorTex": 0, "_UseFresnel": 0,
        "_UseDissolve": 0, "_UseSpread": 0, "_UseTrail": 0, "_UseSoftBlend": 0,
        "_UsePolarUV": 0, "_RefractSwitchUV": 0, "_MaskSwitchUV": 0,
        "_DisableParticleInstanceColor": 0, "_DisableOccludedRefract": 0,
        "_RefractIsNormal": 1, "_RefractUseRBOffset": 1, "_UseMaskTexAsAlpha": 0,
        "_UseNearCameraFade": 0, "_MaskTexUseRefract": 0, "_CullMode": 0,
    }
    return {
        "Name": "M_test_refract",
        "Source": "cab",
        "PathId": 7,
        "Shader": {"Source": "shader-cab", "PathId": 8, "Name": "HGRP/Effect/VFXRefract"},
        "Textures": [],
        "Floats": floats,
        "Colors": {"_RefractUVSpeed": [0, 0, 0, 0], "_MaskTexUVSpeed": [0, 0, 0, 0]},
        "RenderState": {"ValidKeywords": ["_USE_MASK", "_USE_RBOFFSET"], "DisabledShaderPasses": [],
                         "CustomRenderQueue": 3000},
    }


class SceneColorSnapshotContractTests(unittest.TestCase):
    def test_snapshot_normalizes_supported_fields_and_ignores_extras(self) -> None:
        value = {**snapshot(), "future_metadata": {"ignored": True}}
        normalized = read_scene_color_snapshot(value)
        self.assertEqual(normalized["camera_matrix_world"][3:12:4], [0.25, 0.5, 0.75])
        self.assertEqual(normalized["lens_mm"], 50.0)
        self.assertNotIn("future_metadata", normalized)

    def test_snapshot_rejects_invalid_required_values(self) -> None:
        cases = (
            ({key: value for key, value in snapshot().items() if key != "clip_start"}, "clip_start"),
            ({**snapshot(), "camera_matrix_world": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, -1, 0, 0, 0, 0, 1]}, "determinant"),
            ({**snapshot(), "camera_matrix_world": [1, 0, 0, 0, 0.01, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}, "orthonormal"),
            ({**snapshot(), "camera_matrix_world": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0.01, 0, 1]}, "bottom row"),
            ({**snapshot(), "lens_mm": True}, "lens_mm"),
            ({**snapshot(), "resolution": [640, True]}, "resolution"),
            ({**snapshot(), "resolution": [4097, 480]}, "resolution"),
            ({**snapshot(), "samples": 0}, "samples"),
            ({**snapshot(), "time_seconds": float("inf")}, "time_seconds"),
            ({**snapshot(), "clip_start": 2, "clip_end": 2}, "clip distances"),
        )
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                read_scene_color_snapshot(value)

    def test_vfxrefract_source_rules_and_optional_color_stream(self) -> None:
        record = material_record()
        self.assertEqual(vfxrefract_snapshot_issues(record, None), ())
        self.assertEqual(vfxrefract_snapshot_issues(record, 0), ())
        self.assertEqual(vfxrefract_snapshot_issues(record, 4), ())
        self.assertIn("source_rgba_or_explicit_missing_color_reference_required",
                      vfxrefract_snapshot_issues(record, 3))
        changed = {**record, "Floats": {**record["Floats"], "_UseFresnel": 1}}
        self.assertIn("_UseFresnel", vfxrefract_snapshot_issues(changed, None))
        changed = {**record, "RenderState": {**record["RenderState"], "ValidKeywords": ["_USE_MASK"]}}
        self.assertIn("compiled_mask_rb_keyword_contract", vfxrefract_snapshot_issues(changed, None))
        changed = {**record, "Shader": {**record["Shader"], "Name": "HGRP/Effect/VFXAdvance"}}
        self.assertIn("shader_family", vfxrefract_snapshot_issues(changed, None))


if __name__ == "__main__":
    unittest.main()
