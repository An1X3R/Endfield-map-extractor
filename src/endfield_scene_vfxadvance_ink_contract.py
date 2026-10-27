"""Classify texture-backed ink variants with explicit missing simulation inputs."""
from __future__ import annotations

from endfield_scene_material_contract import MaterialRecord, validate_material, validate_texture_sampler


INK_UV_PROPERTIES: tuple[tuple[str, str, str, str], ...] = (
    ("_MainTex", "_MainTexUVWeights", "_MainTexUVSpeed", "_MainTexUVRotateMat"),
    ("_MaskTex", "_MaskTexUVWeights", "_MaskTexUVSpeed", "_MaskTexUVRotateMat"),
    ("_BlendTex", "_BlendTexUVWeights", "_BlendTexUVSpeed", "_BlendTexUVRotateMat"),
    ("_DisturbTex1", "_DisturbUVWeights1", "_DisturbUVSpeed1", "_DisturbUVRotateMat1"),
)
INK_DISSOLVE_UV: tuple[str, str, str, str] = (
    "_DissolveTex", "_DissolveUVWeights", "_DissolveUVSpeed", "_DissolveUVRotateMat",
)
_BASE_KEYWORDS = frozenset(("_INK_DISTORT", "_USE_BLEND", "_USE_DISTURB", "_USE_MASK"))
INK_EVIDENCE_VARIANTS: dict[frozenset[str], int] = {
    _BASE_KEYWORDS | {"_USE_FOG"}: 340,
    _BASE_KEYWORDS | {"_USE_DISSOLVE"}: 309,
}


def vfxadvance_ink_texture_issues(record: MaterialRecord,
                                 vertex_color_components: int) -> tuple[str, ...]:
    """Require verified texture branches; runtime ink remains an explicit host input."""
    source = validate_material(record)
    if source["Shader"].get("Name") != "HGRP/Effect/VFXAdvance":
        return ("shader_family_not_vfxadvance",)
    state = source.get("RenderState")
    if state is None:
        return ("render_state_missing",)
    keywords = frozenset(state["ValidKeywords"])
    if keywords not in INK_EVIDENCE_VARIANTS or len(keywords) != len(state["ValidKeywords"]):
        return ("unverified_texture_backed_ink_variant",)
    issues: list[str] = []
    if vertex_color_components != 0:
        issues.append("blend_vertex_attenuation_requires_colorless_mesh")
    if "ForwardOnly" in state["DisabledShaderPasses"]:
        issues.append("forward_pass_disabled")
    floats, colors = source["Floats"], source["Colors"]
    for name, expected in (
        ("_BlendMode", 0), ("_SurfaceType", 1), ("_SrcBlend", 1), ("_DstBlend", 10),
        ("_AlphaSrcBlend", 1), ("_AlphaDstBlend", 10), ("_ZWrite", 0), ("_ZTest", 4),
        ("_UseNearCameraFade", 0), ("_EnableInteriorMapping", 0),
    ):
        if floats.get(name) != expected:
            issues.append(f"unsupported_float:{name}")
    for name in ("_InParticle", "_DisableVertColor",
                 "_UseMainTexAsAlpha", "_UseMaskTexAsAlpha", "_Bi_Disturb", "_DisturbTex1Normal"):
        if floats.get(name) not in (0, 1):
            issues.append(f"unsupported_selector:{name}")
    if floats.get("_CullMode") not in (0, 1, 2):
        issues.append("unsupported_cull_mode")
    for name in ("_TintColorIntensity", "_TintColorAlpha", "_DisturbUIntensity1", "_DisturbVIntensity1",
                 "_MainTexUseDisturb", "_MaskTexUseDisturb", "_BlendTexUseDisturb",
                 "_MaskTexUseInkSimulation", "_DisturbTex1UseInkSimulation"):
        if name not in floats:
            issues.append(f"float_missing:{name}")
    for name in ("_TintColor", "_BlendTint"):
        if name not in colors:
            issues.append(f"color_missing:{name}")
    uv_properties = INK_UV_PROPERTIES
    if "_USE_DISSOLVE" in keywords:
        uv_properties = (*uv_properties, INK_DISSOLVE_UV)
        for name in ("_DissolveTexUseDisturb", "_DissolveScheduleOffset", "_DissolveEdgeSharp", "_EdgeDistance"):
            if name not in floats:
                issues.append(f"float_missing:{name}")
        if "_EdgeColor" not in colors:
            issues.append("color_missing:_EdgeColor")
    for texture, weights, speed, rotation in uv_properties:
        for name in (weights, speed, rotation):
            if name not in colors:
                issues.append(f"color_missing:{name}")
        if weights in colors and colors[weights][1:] != [0, 0, 0]:
            issues.append(f"unsupported_uv_stream:{weights}")
        if speed in colors and colors[speed][2:] != [0, 0]:
            issues.append(f"custom_uv_speed_required:{speed}")
        slots = [slot for slot in source["Textures"] if slot["Property"] == texture]
        if len(slots) != 1 or slots[0]["Texture"] is None:
            issues.append(f"texture_binding_missing:{texture}")
            continue
        reference = slots[0]["Texture"]
        if reference["PathId"] == 0 or reference.get("ColorSpace") not in (0, 1):
            issues.append(f"unsupported_texture:{texture}")
        sampler = reference.get("TextureSampler")
        if sampler is None:
            issues.append(f"texture_sampler_missing:{texture}")
        else:
            validated = validate_texture_sampler(sampler, f"material={source['Name']}, property={texture}")
            if (validated["WrapU"], validated["WrapV"]) not in ((0, 0), (1, 1)):
                issues.append(f"texture_sampler_wrap_not_supported:{texture}")
    return tuple(issues)
