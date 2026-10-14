"""Regression checks for the bounded VFXAdvance mask-only contract."""
from __future__ import annotations

import unittest

from endfield_scene_material_contract import MaterialRecord
from endfield_scene_vfxadvance_contract import (
    vfxadvance_blend_dissolve_disturb_issues,
    vfxadvance_mask_only_issues,
)


def _material() -> MaterialRecord:
    floats = {
        "_UseMask": 1, "_UseUnderGround": 1, "_UseMaskTexAsAlpha": 1,
        "_UseMaskTex2AsAlpha": 1, "_UseMaskTex3AsAlpha": 1,
        "_UseMainTexAsAlpha": 0, "_UseMask2": 0, "_UseMask3": 0,
        "_UseBlend": 0, "_UseBlend2": 0, "_UseBlend3": 0,
        "_UseDissolve": 0, "_UseDisturb": 0, "_UseSoftBlend": 0,
        "_UseFog": 0, "_UseNearCameraFade": 0, "_UseScreenUV": 0,
        "_UsePolarUV": 0, "_UseCutOff": 0, "_UseFresnel": 0,
        "_UseFlowmap": 0, "_UseEmission": 0, "_UseVertexOffset": 0,
        "_DisableVertColor": 0, "_DisableParticleInstanceColor": 0,
        "_BlendMode": 0, "_InParticle": 1, "_SurfaceType": 1,
        "_SrcBlend": 1, "_DstBlend": 10, "_AlphaSrcBlend": 1,
        "_AlphaDstBlend": 10, "_CullMode": 0, "_ZWrite": 0,
        "_ZTest": 8, "_TintColorIntensity": 5.8, "_TintColorAlpha": 1,
    }
    colors = {
        "_TintColor": [0, 1, 0.9063432, 1],
        "_MainTexUVSpeed": [-0.01, 0, 0, 0],
        "_MainTexUVRotateMat": [1, 0, 0, 1],
        "_MainTexUVWeights": [1, 0, 0, 0],
        "_MaskTexUVSpeed": [0, 0, 0, 0],
        "_MaskTexUVRotateMat": [1, 0, 0, 1],
        "_MaskTexUVWeights": [1, 0, 0, 0],
    }
    sampler = {"FilterMode": 1, "WrapU": 0, "WrapV": 0, "WrapW": 0,
               "Aniso": 1, "MipBias": 0.0}
    return {
        "Name": "M_fxmap_shu_07_02", "Source": "CAB-material", "PathId": 5,
        "Shader": {"Source": "CAB-shader", "PathId": 6, "Type": "Shader",
                   "Name": "HGRP/Effect/VFXAdvance", "PassNames": ["ForwardOnly"]},
        "RenderState": {"DisabledShaderPasses": ["GBuffer"],
                         "ValidKeywords": ["_USE_MASK", "_USE_UNDER_GROUND"],
                         "CustomRenderQueue": 3000},
        "Textures": [
            {"Property": "_MainTex", "Texture": {"Source": "CAB-main", "PathId": 10,
             "Type": "Texture2D", "ColorSpace": 1, "TextureSampler": sampler},
             "Scale": [5, 1], "Offset": [0, 0]},
            {"Property": "_MaskTex", "Texture": {"Source": "CAB-mask", "PathId": 11,
             "Type": "Texture2D", "ColorSpace": 1, "TextureSampler": sampler},
             "Scale": [1, 1], "Offset": [0, 0]},
        ],
        "Floats": floats, "Colors": colors,
    }


class VFXAdvanceContractTests(unittest.TestCase):
    def test_accepts_mask_branch_and_ignores_inactive_mask_alpha_flags(self) -> None:
        self.assertEqual(vfxadvance_mask_only_issues(_material(), 4), ())

    def test_accepts_supported_source_sampler_and_color_space_values(self) -> None:
        material = _material()
        for slot in material["Textures"]:
            texture = slot["Texture"]
            texture["ColorSpace"] = 0
            texture["TextureSampler"] = {
                "FilterMode": 2, "WrapU": 1, "WrapV": 1, "WrapW": 1,
                "Aniso": 4, "MipBias": 0.5,
            }
        self.assertEqual(vfxadvance_mask_only_issues(material, 4), ())

    def test_rejects_sampler_wrap_modes_without_builder_support(self) -> None:
        for material, issues_fn in (
            (_material(), vfxadvance_mask_only_issues),
            (_blend_material(), vfxadvance_blend_dissolve_disturb_issues),
        ):
            sampler = material["Textures"][0]["Texture"]["TextureSampler"]
            for wrap_u, wrap_v in ((2, 2), (0, 1)):
                sampler["WrapU"], sampler["WrapV"] = wrap_u, wrap_v
                self.assertIn("texture_sampler_wrap_not_supported:_MainTex",
                              issues_fn(material, 4))
            sampler["WrapU"], sampler["WrapV"] = 0, 0

    def test_keeps_blend_dissolve_disturb_variant_pending(self) -> None:
        material = _material()
        material["Floats"]["_UseDissolve"] = 1
        material["RenderState"]["ValidKeywords"].append("_USE_DISSOLVE")
        issues = vfxadvance_mask_only_issues(material, 4)
        self.assertIn("valid_keywords_do_not_match_mask_underground_branch", issues)
        self.assertIn("unsupported_float:_UseDissolve", issues)

    def test_requires_the_source_color_stream_and_uv0_mesh(self) -> None:
        self.assertIn("source_vertex_color_rgba_required",
                      vfxadvance_mask_only_issues(_material(), 0))
        material = _material()
        material["Colors"]["_MainTexUVWeights"] = [0, 1, 0, 0]
        self.assertIn("uv_selector_not_uv0:_MainTexUVWeights",
                      vfxadvance_mask_only_issues(material, 4))

    def test_rejects_unsupported_mask_blend_and_vertex_color_modes(self) -> None:
        material = _material()
        material["Floats"]["_BlendMode"] = 1
        material["Floats"]["_DisableParticleInstanceColor"] = 1
        issues = vfxadvance_mask_only_issues(material, 4)
        self.assertIn("unsupported_float:_BlendMode", issues)
        self.assertIn("unsupported_float:_DisableParticleInstanceColor", issues)

    def test_rejects_out_of_range_tint_color_alpha(self) -> None:
        material = _material()
        material["Colors"]["_TintColor"][3] = 1.1
        self.assertIn("tint_color_alpha_invalid", vfxadvance_mask_only_issues(material, 4))

    def test_accepts_blend_dissolve_disturb_branch_and_inactive_mask_alpha_flags(self) -> None:
        self.assertEqual(vfxadvance_blend_dissolve_disturb_issues(_blend_material(), 4), ())

    def test_rejects_extra_active_feature_in_blend_dissolve_disturb_branch(self) -> None:
        material = _blend_material()
        material["Floats"]["_UseEmission"] = 1
        self.assertIn("active_feature_flags_do_not_match_blend_dissolve_disturb_underground_branch",
                      vfxadvance_blend_dissolve_disturb_issues(material, 4))

    def test_rejects_non_uv0_or_non_rgba_source_stream(self) -> None:
        material = _blend_material()
        material["Colors"]["_DissolveUVWeights"] = [0, 1, 0, 0]
        issues = vfxadvance_blend_dissolve_disturb_issues(material, 3)
        self.assertIn("uv_selector_not_uv0:_DissolveUVWeights", issues)
        self.assertIn("source_vertex_color_rgba_required", issues)

    def test_rejects_missing_active_texture_binding(self) -> None:
        material = _blend_material()
        material["Textures"] = [slot for slot in material["Textures"]
                                if slot["Property"] != "_DisturbTex1"]
        self.assertIn("texture_binding_not_unique:_DisturbTex1",
                      vfxadvance_blend_dissolve_disturb_issues(material, 4))

    def test_rejects_unsupported_blend_mode_and_tint_color_alpha(self) -> None:
        material = _blend_material()
        material["Floats"]["_BlendMode"] = 1
        material["Colors"]["_TintColor"][3] = -0.1
        issues = vfxadvance_blend_dissolve_disturb_issues(material, 4)
        self.assertIn("unsupported_float:_BlendMode", issues)
        self.assertIn("tint_color_alpha_invalid", issues)


def _blend_material() -> MaterialRecord:
    material = _material()
    material["Name"] = "M_fxmap_shu_09_1"
    material["RenderState"]["ValidKeywords"] = [
        "_USE_BLEND", "_USE_DISSOLVE", "_USE_DISTURB", "_USE_UNDER_GROUND",
    ]
    material["Floats"].update({
        "_UseMask": 0, "_UseMaskTexAsAlpha": 0, "_UseBlend": 1,
        "_UseBlend2": 0, "_UseBlend3": 0, "_UseDissolve": 1,
        "_UseDisturb": 1, "_UseUnderGround": 1, "_MainTexUseDisturb": 1,
        "_BlendTexUseDisturb": 1, "_DissolveTexUseDisturb": 1,
        "_DisturbTex1Normal": 0, "_DisturbTex1UseInkSimulation": 0,
        "_Bi_Disturb": 0, "_DisturbUIntensity1": 0.01,
        "_DisturbVIntensity1": -0.09, "_DissolveScheduleOffset": 0.229,
        "_DissolveEdgeSharp": 0.8, "_EdgeDistance": 0.001,
        "_TintColorIntensity": 2.1, "_TintColorAlpha": 0.26,
        "_BlendMode": 0, "_DisableParticleInstanceColor": 0,
    })
    material["Colors"].update({
        "_BlendTexUVSpeed": [0, -0.05, 0, 0],
        "_BlendTexUVRotateMat": [1, 0, 0, 1],
        "_BlendTexUVWeights": [1, 0, 0, 0],
        "_DissolveUVSpeed": [0, -0.11, 0, 0],
        "_DissolveUVRotateMat": [0, -1, 1, 0],
        "_DissolveUVWeights": [1, 0, 0, 0],
        "_DisturbUVSpeed1": [0, 0.14, 0, 0],
        "_DisturbUVRotateMat1": [1, 0, 0, 1],
        "_DisturbUVWeights1": [1, 0, 0, 0],
        "_BlendTint": [0, 2.4335723, 1.8347349, 1],
        "_EdgeColor": [1, 1, 1, 1],
    })
    texture_specs = (
        ("_BlendTex", "CAB-blend", 12, [20, 0.5], [0, 0]),
        ("_DissolveTex", "CAB-dissolve", 13, [1, 10], [0, 0]),
        ("_DisturbTex1", "CAB-disturb", 14, [21.7, -0.38], [0, 0]),
    )
    for prop, source, path_id, scale, offset in texture_specs:
        material["Textures"].append({
            "Property": prop,
            "Texture": {"Source": source, "PathId": path_id, "Type": "Texture2D",
                         "ColorSpace": 1, "TextureSampler": {
                             "FilterMode": 1, "WrapU": 0, "WrapV": 0, "WrapW": 0,
                             "Aniso": 1, "MipBias": 0.0,
                         }},
            "Scale": scale,
            "Offset": offset,
        })
    main_slot = next(slot for slot in material["Textures"] if slot["Property"] == "_MainTex")
    main_slot["Scale"] = [1, 1]
    main_slot["Texture"]["Source"] = "CAB-main"
    main_slot["Texture"]["PathId"] = 15
    return material


if __name__ == "__main__":
    unittest.main()
