"""Classify the source-backed VFXAdvance mask-only direct-effect branch."""
from __future__ import annotations

from endfield_scene_material_contract import MaterialRecord, validate_material, validate_texture_sampler


VFXADVANCE_MASK_ONLY_EVIDENCE_VARIANT = 163
VFXADVANCE_BLEND_DISSOLVE_DISTURB_EVIDENCE_VARIANT = 179
_SHADER = "HGRP/Effect/VFXAdvance"
_EXPECTED_KEYWORDS = frozenset(("_USE_MASK", "_USE_UNDER_GROUND"))
_INACTIVE_MASK_ALPHA_FLAGS = frozenset(("_UseMaskTex2AsAlpha", "_UseMaskTex3AsAlpha"))
_EXPECTED_ACTIVE_FLAGS = frozenset(("_UseMask", "_UseUnderGround", "_UseMaskTexAsAlpha"))
_BLEND_DISSOLVE_DISTURB_KEYWORDS = frozenset((
    "_USE_BLEND", "_USE_DISSOLVE", "_USE_DISTURB", "_USE_UNDER_GROUND",
))
_INACTIVE_BLEND_ALPHA_FLAGS = frozenset(("_UseMaskTex2AsAlpha", "_UseMaskTex3AsAlpha"))
_BLEND_DISSOLVE_DISTURB_ACTIVE_FLAGS = frozenset((
    "_UseBlend", "_UseDissolve", "_UseDisturb", "_UseUnderGround",
))


def vfxadvance_mask_only_issues(record: MaterialRecord,
                                vertex_color_components: int | None) -> tuple[str, ...]:
    """Return source-contract issues for the mask and underground feature branch."""
    validated = validate_material(record)
    if validated["Shader"].get("Name") != _SHADER:
        return ("shader_family_not_vfxadvance",)

    issues: list[str] = []
    state = validated.get("RenderState")
    if state is None:
        issues.append("render_state_missing")
    else:
        keywords = state["ValidKeywords"]
        if len(keywords) != len(set(keywords)) or frozenset(keywords) != _EXPECTED_KEYWORDS:
            issues.append("valid_keywords_do_not_match_mask_underground_branch")
        if "GBuffer" not in state["DisabledShaderPasses"]:
            issues.append("gbuffer_pass_not_disabled")

    floats = validated["Floats"]
    for name, expected in (
        ("_UseMask", 1), ("_UseUnderGround", 1), ("_UseMaskTexAsAlpha", 1),
        ("_UseMainTexAsAlpha", 0), ("_UseMask2", 0), ("_UseMask3", 0),
        ("_UseBlend", 0), ("_UseBlend2", 0), ("_UseBlend3", 0),
        ("_UseDissolve", 0), ("_UseDisturb", 0), ("_UseSoftBlend", 0),
        ("_UseFog", 0), ("_UseNearCameraFade", 0), ("_UseScreenUV", 0),
        ("_UsePolarUV", 0), ("_UseCutOff", 0), ("_UseFresnel", 0),
        ("_UseFlowmap", 0), ("_UseEmission", 0), ("_UseVertexOffset", 0),
        ("_DisableVertColor", 0), ("_DisableParticleInstanceColor", 0),
        ("_BlendMode", 0), ("_InParticle", 1), ("_SurfaceType", 1),
        ("_SrcBlend", 1), ("_DstBlend", 10), ("_AlphaSrcBlend", 1),
        ("_AlphaDstBlend", 10), ("_CullMode", 0), ("_ZWrite", 0),
        ("_ZTest", 8),
    ):
        if name not in floats or floats[name] != expected:
            issues.append(f"unsupported_float:{name}")

    active_flags = frozenset(name for name, value in floats.items()
                             if name.startswith("_Use") and name not in _INACTIVE_MASK_ALPHA_FLAGS and value != 0)
    if active_flags != _EXPECTED_ACTIVE_FLAGS:
        issues.append("active_feature_flags_do_not_match_mask_underground_branch")

    if validated["Shader"].get("PassNames") != ["ForwardOnly"]:
        issues.append("shader_pass_set_not_forward_only")

    colors = validated["Colors"]
    for name in ("_TintColor", "_MainTexUVSpeed", "_MainTexUVRotateMat",
                 "_MainTexUVWeights", "_MaskTexUVSpeed", "_MaskTexUVRotateMat",
                 "_MaskTexUVWeights"):
        if name not in colors:
            issues.append(f"color_property_missing:{name}")
    for name in ("_MainTexUVWeights", "_MaskTexUVWeights"):
        if colors.get(name) != [1, 0, 0, 0]:
            issues.append(f"uv_selector_not_uv0:{name}")
    for name in ("_MainTexUVSpeed", "_MaskTexUVSpeed"):
        value = colors.get(name)
        if not isinstance(value, list) or len(value) != 4 or value[2:] != [0, 0]:
            issues.append(f"custom_uv_speed_not_supported:{name}")
    tint_intensity = floats.get("_TintColorIntensity")
    if type(tint_intensity) not in (int, float) or tint_intensity <= 0:
        issues.append("tint_intensity_invalid")
    tint_alpha = floats.get("_TintColorAlpha")
    if type(tint_alpha) not in (int, float) or not 0 <= tint_alpha <= 1:
        issues.append("tint_alpha_invalid")
    tint_color = colors.get("_TintColor")
    if (not isinstance(tint_color, list) or len(tint_color) != 4
            or type(tint_color[3]) not in (int, float) or not 0 <= tint_color[3] <= 1):
        issues.append("tint_color_alpha_invalid")

    for prop in ("_MainTex", "_MaskTex"):
        slots = [slot for slot in validated["Textures"] if slot["Property"] == prop]
        if len(slots) != 1:
            issues.append(f"texture_binding_not_unique:{prop}")
            continue
        reference = slots[0]["Texture"]
        if reference is None or reference.get("Type") != "Texture2D" or reference.get("PathId") == 0:
            issues.append(f"texture_binding_missing:{prop}")
            continue
        if reference.get("ColorSpace") not in (0, 1):
            issues.append(f"texture_color_space_not_supported:{prop}")
        sampler_value = reference.get("TextureSampler")
        if sampler_value is None:
            issues.append(f"texture_sampler_missing:{prop}")
            continue
        sampler = validate_texture_sampler(sampler_value, f"material={validated['Name']}, property={prop}")
        if (sampler["WrapU"], sampler["WrapV"]) not in ((0, 0), (1, 1)):
            issues.append(f"texture_sampler_wrap_not_supported:{prop}")

    if vertex_color_components is not None and vertex_color_components != 4:
        issues.append("source_vertex_color_rgba_required")
    return tuple(issues)


def vfxadvance_blend_dissolve_disturb_issues(
        record: MaterialRecord, vertex_color_components: int | None) -> tuple[str, ...]:
    """Return source-contract issues for the blend, dissolve, disturb, and underground branch."""
    validated = validate_material(record)
    if validated["Shader"].get("Name") != _SHADER:
        return ("shader_family_not_vfxadvance",)

    issues: list[str] = []
    state = validated.get("RenderState")
    if state is None:
        issues.append("render_state_missing")
    else:
        keywords = state["ValidKeywords"]
        if (len(keywords) != len(set(keywords))
                or frozenset(keywords) != _BLEND_DISSOLVE_DISTURB_KEYWORDS):
            issues.append("valid_keywords_do_not_match_blend_dissolve_disturb_underground_branch")
        if "GBuffer" not in state["DisabledShaderPasses"]:
            issues.append("gbuffer_pass_not_disabled")

    floats = validated["Floats"]
    for name, expected in (
        ("_UseMask", 0), ("_UseBlend", 1), ("_UseBlend2", 0), ("_UseBlend3", 0),
        ("_UseDissolve", 1), ("_UseDisturb", 1), ("_UseUnderGround", 1),
        ("_UseMask2", 0), ("_UseMask3", 0), ("_UseMainTexAsAlpha", 0),
        ("_UseSoftBlend", 0), ("_UseFog", 0), ("_UseNearCameraFade", 0),
        ("_UseScreenUV", 0), ("_UsePolarUV", 0), ("_UseCutOff", 0),
        ("_UseFresnel", 0), ("_UseFlowmap", 0), ("_UseEmission", 0),
        ("_UseVertexOffset", 0), ("_DisableVertColor", 0),
        ("_DisableParticleInstanceColor", 0), ("_BlendMode", 0), ("_InParticle", 1),
        ("_SurfaceType", 1), ("_SrcBlend", 1), ("_DstBlend", 10),
        ("_AlphaSrcBlend", 1), ("_AlphaDstBlend", 10), ("_CullMode", 0),
        ("_ZWrite", 0), ("_ZTest", 8), ("_MainTexUseDisturb", 1),
        ("_BlendTexUseDisturb", 1), ("_DissolveTexUseDisturb", 1),
        ("_DisturbTex1Normal", 0), ("_DisturbTex1UseInkSimulation", 0),
        ("_Bi_Disturb", 0),
    ):
        if name not in floats or floats[name] != expected:
            issues.append(f"unsupported_float:{name}")

    active_flags = frozenset(name for name, value in floats.items()
                             if name.startswith("_Use")
                             and name not in _INACTIVE_BLEND_ALPHA_FLAGS and value != 0)
    if active_flags != _BLEND_DISSOLVE_DISTURB_ACTIVE_FLAGS:
        issues.append("active_feature_flags_do_not_match_blend_dissolve_disturb_underground_branch")

    if validated["Shader"].get("PassNames") != ["ForwardOnly"]:
        issues.append("shader_pass_set_not_forward_only")

    colors = validated["Colors"]
    uv_properties = (
        ("_MainTexUVSpeed", "_MainTexUVRotateMat", "_MainTexUVWeights"),
        ("_BlendTexUVSpeed", "_BlendTexUVRotateMat", "_BlendTexUVWeights"),
        ("_DissolveUVSpeed", "_DissolveUVRotateMat", "_DissolveUVWeights"),
        ("_DisturbUVSpeed1", "_DisturbUVRotateMat1", "_DisturbUVWeights1"),
    )
    for speed_name, rotation_name, weights_name in uv_properties:
        for name in (speed_name, rotation_name, weights_name):
            if name not in colors:
                issues.append(f"color_property_missing:{name}")
        if colors.get(weights_name) != [1, 0, 0, 0]:
            issues.append(f"uv_selector_not_uv0:{weights_name}")
        speed = colors.get(speed_name)
        if not isinstance(speed, list) or len(speed) != 4 or speed[2:] != [0, 0]:
            issues.append(f"custom_uv_speed_not_supported:{speed_name}")

    for name in ("_TintColor", "_BlendTint", "_EdgeColor"):
        if name not in colors:
            issues.append(f"color_property_missing:{name}")
    for name in ("_TintColorIntensity", "_TintColorAlpha", "_DisturbUIntensity1",
                 "_DisturbVIntensity1", "_DissolveScheduleOffset", "_DissolveEdgeSharp",
                 "_EdgeDistance"):
        if name not in floats:
            issues.append(f"float_property_missing:{name}")
    if floats.get("_TintColorIntensity", 0) <= 0:
        issues.append("tint_intensity_invalid")
    if not 0 <= floats.get("_TintColorAlpha", -1) <= 1:
        issues.append("tint_alpha_invalid")
    tint_color = colors.get("_TintColor")
    if (not isinstance(tint_color, list) or len(tint_color) != 4
            or type(tint_color[3]) not in (int, float) or not 0 <= tint_color[3] <= 1):
        issues.append("tint_color_alpha_invalid")
    if floats.get("_DissolveEdgeSharp", 0) <= 0:
        issues.append("dissolve_edge_sharp_invalid")

    for prop in ("_MainTex", "_BlendTex", "_DissolveTex", "_DisturbTex1"):
        slots = [slot for slot in validated["Textures"] if slot["Property"] == prop]
        if len(slots) != 1:
            issues.append(f"texture_binding_not_unique:{prop}")
            continue
        reference = slots[0]["Texture"]
        if reference is None or reference.get("Type") != "Texture2D" or reference.get("PathId") == 0:
            issues.append(f"texture_binding_missing:{prop}")
            continue
        if reference.get("ColorSpace") not in (0, 1):
            issues.append(f"texture_color_space_not_supported:{prop}")
        sampler_value = reference.get("TextureSampler")
        if sampler_value is None:
            issues.append(f"texture_sampler_missing:{prop}")
        else:
            sampler = validate_texture_sampler(sampler_value,
                                               f"material={validated['Name']}, property={prop}")
            if (sampler["WrapU"], sampler["WrapV"]) not in ((0, 0), (1, 1)):
                issues.append(f"texture_sampler_wrap_not_supported:{prop}")

    if vertex_color_components is not None and vertex_color_components != 4:
        issues.append("source_vertex_color_rgba_required")
    return tuple(issues)
