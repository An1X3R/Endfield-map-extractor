"""Low-level, source-grounded Skin surface color nodes for Blender."""
from __future__ import annotations

from dataclasses import dataclass
import json
from math import isfinite
from typing import TypeAlias

import bpy

from character_node_graph import color_mix, math_node, node

RGB: TypeAlias = tuple[float, float, float]
SurfaceScalar: TypeAlias = float | bpy.types.NodeSocket
SURFACE_SECTION = "Current Skin Surface Color"
LUMA_WEIGHTS: RGB = (0.2126729041337967, 0.7151522040367126, 0.07217500358819962)
FACE_CHROMA_FACTOR = 1.2000000476837158


@dataclass(frozen=True)
class SkinSurfaceParameters:
    """Source material values consumed by the current compiled Skin surface branch."""

    sdf_rim_color: RGB
    skin_rim_off_scale: float
    face_rim_off_scale: float
    shadow_color_brightness: float
    shadow_color_saturation: float
    use_sdf_lightmap: float

    def __post_init__(self) -> None:
        _validate_rgb(self.sdf_rim_color, "_SDFRimColor")
        for name, value in (
            ("_SkinRimOffScale", self.skin_rim_off_scale),
            ("_FaceRimOffScale", self.face_rim_off_scale),
            ("_ShadowColorBrightness", self.shadow_color_brightness),
            ("_ShadowColorSaturation", self.shadow_color_saturation),
            ("_UseSDFLightmap", self.use_sdf_lightmap),
        ):
            _validate_number(value, name)


def _validate_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a numeric source value, got {type(value).__name__}")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite, got {result}")
    return result


def _validate_rgb(value: RGB, name: str) -> None:
    if len(value) != 3:
        raise ValueError(f"{name} must contain exactly three channels")
    for index, channel in enumerate(value):
        _validate_number(channel, f"{name}[{index}]")


def _source_object(source_group: bpy.types.NodeTree, property_name: str) -> dict[str, object]:
    encoded = source_group.get(property_name)
    if not isinstance(encoded, str):
        raise ValueError(f"{source_group.name}: missing string custom property {property_name}")
    try:
        parsed: object = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise ValueError(f"{source_group.name}: invalid JSON in {property_name}: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{source_group.name}: {property_name} must decode to an object")
    values: dict[str, object] = {}
    for key, value in parsed.items():
        if not isinstance(key, str):
            raise ValueError(f"{source_group.name}: {property_name} contains a non-string key")
        values[key] = value
    return values


def _source_float(source_group: bpy.types.NodeTree, values: dict[str, object], key: str) -> float:
    if key not in values:
        raise ValueError(f"{source_group.name}: source_floats is missing {key}")
    return _validate_number(values[key], key)


def _source_rgb(source_group: bpy.types.NodeTree, values: dict[str, object], key: str) -> RGB:
    raw = values.get(key)
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError(f"{source_group.name}: source_colors.{key} must contain four RGBA values")
    return (
        _validate_number(raw[0], f"{key}.r"),
        _validate_number(raw[1], f"{key}.g"),
        _validate_number(raw[2], f"{key}.b"),
    )


def source_parameters(source_group: bpy.types.NodeTree) -> SkinSurfaceParameters:
    """Read named values from an already-loaded HGRP Skin source group."""
    if source_group.get("source_shader") != "HGRP/CharacterNPR_Skin":
        raise ValueError(f"{source_group.name}: expected source_shader HGRP/CharacterNPR_Skin")
    floats = _source_object(source_group, "source_floats")
    colors = _source_object(source_group, "source_colors")
    return SkinSurfaceParameters(
        sdf_rim_color=_source_rgb(source_group, colors, "_SDFRimColor"),
        skin_rim_off_scale=_source_float(source_group, floats, "_SkinRimOffScale"),
        face_rim_off_scale=_source_float(source_group, floats, "_FaceRimOffScale"),
        shadow_color_brightness=_source_float(source_group, floats, "_ShadowColorBrightness"),
        shadow_color_saturation=_source_float(source_group, floats, "_ShadowColorSaturation"),
        use_sdf_lightmap=_source_float(source_group, floats, "_UseSDFLightmap"),
    )


def _scalar(
    tree: bpy.types.NodeTree,
    operation: str,
    first: SurfaceScalar,
    second: SurfaceScalar,
    label: str,
) -> bpy.types.NodeSocket:
    return math_node(tree, operation, first, second, label, SURFACE_SECTION)


def _vector_math(
    tree: bpy.types.NodeTree,
    operation: str,
    first: bpy.types.NodeSocket,
    second: bpy.types.NodeSocket | RGB,
    label: str,
) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeVectorMath", label, SURFACE_SECTION)
    result.operation = operation
    tree.links.new(first, result.inputs[0])
    if isinstance(second, bpy.types.NodeSocket):
        tree.links.new(second, result.inputs[1])
    else:
        result.inputs[1].default_value = second
    output_name = "Value" if operation == "DOT_PRODUCT" else "Vector"
    return result.outputs[output_name]


def _combine_gray(tree: bpy.types.NodeTree, value: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeCombineXYZ", label, SURFACE_SECTION)
    for socket in result.inputs:
        tree.links.new(value, socket)
    return result.outputs[0]


def _luminance(tree: bpy.types.NodeTree, color: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return _vector_math(tree, "DOT_PRODUCT", color, LUMA_WEIGHTS, label)


def _vector_mix(
    tree: bpy.types.NodeTree,
    first: bpy.types.NodeSocket,
    second: bpy.types.NodeSocket,
    factor: SurfaceScalar,
    label: str,
) -> bpy.types.NodeSocket:
    difference = _vector_math(tree, "SUBTRACT", second, first, label + " delta")
    scaled = _vector_math(
        tree,
        "MULTIPLY",
        difference,
        _combine_gray(tree, factor if isinstance(factor, bpy.types.NodeSocket) else _scalar(tree, "ADD", factor, 0.0, label + " factor value"), label + " factor RGB"),
        label + " scaled delta",
    )
    return _vector_math(tree, "ADD", first, scaled, label + " result")


def _clamp(tree: bpy.types.NodeTree, value: SurfaceScalar, low: float, high: float, label: str) -> bpy.types.NodeSocket:
    return _scalar(tree, "MINIMUM", high, _scalar(tree, "MAXIMUM", low, value, label + " lower"), label + " upper")


def _face_chroma(tree: bpy.types.NodeTree, color: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    luminance = _luminance(tree, color, label + " source luminance")
    gray = _combine_gray(tree, luminance, label + " grayscale")
    return _vector_mix(tree, gray, color, FACE_CHROMA_FACTOR, label + " source 1.2 chroma")


def build_skin_rim_albedo(
    tree: bpy.types.NodeTree,
    parameters: SkinSurfaceParameters,
    base_albedo: bpy.types.NodeSocket,
    normal_dot_view: bpy.types.NodeSocket,
    skin_region_blend: bpy.types.NodeSocket,
    upstream_rim_gate: bpy.types.NodeSocket,
) -> bpy.types.NodeSocket:
    """Build current SDF rim tint; the upstream gate and region blend stay explicit."""
    ndotv = _clamp(tree, normal_dot_view, 0.0, 1.0, "Native Skin NdotV")
    facing = _clamp(
        tree,
        _scalar(tree, "ADD", _scalar(tree, "MULTIPLY", ndotv, 0.85, "Native rim facing slope"), 0.15, "Native rim facing bias"),
        0.0,
        1.0,
        "Native rim facing clamp",
    )
    view_rim = _scalar(tree, "SUBTRACT", 1.0, facing, "Native rim view complement")
    scale_delta = _scalar(
        tree,
        "SUBTRACT",
        parameters.skin_rim_off_scale,
        parameters.face_rim_off_scale,
        "Native face-to-skin scale delta",
    )
    scale = _scalar(
        tree,
        "ADD",
        parameters.face_rim_off_scale,
        _scalar(tree, "MULTIPLY", scale_delta, skin_region_blend, "Native skin-region scale contribution"),
        "Native selected rim scale",
    )
    gated = _scalar(tree, "MULTIPLY", upstream_rim_gate, scale, "Native upstream gate times rim scale")
    rim_factor = _clamp(tree, _scalar(tree, "MULTIPLY", view_rim, gated, "Native rim factor before clamp"), 0.0, 1.0, "Native rim factor")
    rim_tint = color_mix(
        tree,
        "MIX",
        rim_factor,
        (1.0, 1.0, 1.0, 1.0),
        (*parameters.sdf_rim_color, 1.0),
        "Native SDF rim tint",
        SURFACE_SECTION,
    )
    return color_mix(tree, "MULTIPLY", 1.0, base_albedo, rim_tint, "Native rim albedo", SURFACE_SECTION)


def build_face_mainlit_branch(
    tree: bpy.types.NodeTree,
    main_lit_color: bpy.types.NodeSocket,
    alternate_color: bpy.types.NodeSocket,
    final_color: bpy.types.NodeSocket,
    first_blend: bpy.types.NodeSocket,
    final_blend: bpy.types.NodeSocket,
) -> bpy.types.NodeSocket:
    """Build the face variant's exact nested `_1647` chroma/blend expression."""
    chroma = _face_chroma(tree, main_lit_color, "Native face main-lit branch")
    first = _vector_mix(tree, chroma, alternate_color, first_blend, "Native face first color blend")
    return _vector_mix(tree, first, final_color, final_blend, "Native face final color blend")


def build_body_mainlit_chroma(tree: bpy.types.NodeTree, main_lit_color: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Build the body variant's luminance-dependent chroma boost before its CP8 term."""
    luminance = _luminance(tree, main_lit_color, "Native body main-lit luminance")
    gray = _combine_gray(tree, luminance, "Native body main-lit grayscale")
    over_mid = _clamp(tree, _scalar(tree, "SUBTRACT", luminance, 0.5, "Native body luminance above midpoint"), 0.0, 0.5, "Native body chroma control")
    factor = _scalar(
        tree,
        "ADD",
        _scalar(tree, "MULTIPLY", over_mid, over_mid, "Native body chroma control squared"),
        1.0,
        "Native body chroma factor",
    )
    return _vector_mix(tree, gray, main_lit_color, factor, "Native body main-lit chroma")


def _validate_rgb_input(value: RGB, name: str) -> None:
    _validate_rgb(value, name)


def evaluate_luminance(color: RGB) -> float:
    """Evaluate the compiled shader's Rec.709-like luminance dot product."""
    _validate_rgb_input(color, "color")
    return sum(channel * weight for channel, weight in zip(color, LUMA_WEIGHTS, strict=True))


def evaluate_skin_rim_albedo(
    parameters: SkinSurfaceParameters,
    base_albedo: RGB,
    normal_dot_view: float,
    skin_region_blend: float,
    upstream_rim_gate: float,
) -> RGB:
    """Evaluate the current face/body rim albedo equation for numeric fixtures."""
    _validate_rgb_input(base_albedo, "base_albedo")
    ndotv = min(1.0, max(0.0, _validate_number(normal_dot_view, "normal_dot_view")))
    region = _validate_number(skin_region_blend, "skin_region_blend")
    gate = _validate_number(upstream_rim_gate, "upstream_rim_gate")
    facing = min(1.0, max(0.0, ndotv * 0.85 + 0.15))
    scale = parameters.face_rim_off_scale + (parameters.skin_rim_off_scale - parameters.face_rim_off_scale) * region
    factor = min(1.0, max(0.0, (1.0 - facing) * gate * scale))
    tint = tuple(1.0 + (rim - 1.0) * factor for rim in parameters.sdf_rim_color)
    return tuple(base * channel for base, channel in zip(base_albedo, tint, strict=True))


def evaluate_face_mainlit_branch(
    main_lit_color: RGB,
    alternate_color: RGB,
    final_color: RGB,
    first_blend: float,
    final_blend: float,
) -> RGB:
    """Evaluate the face variant's `_1647` nested lerps with explicit source weights."""
    for name, color in (("main_lit_color", main_lit_color), ("alternate_color", alternate_color), ("final_color", final_color)):
        _validate_rgb_input(color, name)
    first_factor = _validate_number(first_blend, "first_blend")
    final_factor = _validate_number(final_blend, "final_blend")
    luminance = evaluate_luminance(main_lit_color)
    chroma = tuple(luminance + (value - luminance) * FACE_CHROMA_FACTOR for value in main_lit_color)
    first = tuple(a + (b - a) * first_factor for a, b in zip(chroma, alternate_color, strict=True))
    return tuple(a + (b - a) * final_factor for a, b in zip(first, final_color, strict=True))


def evaluate_body_mainlit_chroma(main_lit_color: RGB) -> RGB:
    """Evaluate the body variant's luminance-dependent chroma branch."""
    _validate_rgb_input(main_lit_color, "main_lit_color")
    luminance = evaluate_luminance(main_lit_color)
    control = min(0.5, max(0.0, luminance - 0.5))
    factor = control * control + 1.0
    return tuple(luminance + (value - luminance) * factor for value in main_lit_color)
