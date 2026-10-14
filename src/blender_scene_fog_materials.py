"""Build a node preview for the compiled HGRP/FogSimple mesh shader."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple, cast

import bpy

from blender_scene_materials import layout_shader_node_tree, texture_image
from endfield_scene_material_contract import (
    has_shader_white_texture_default,
    reference_key,
    resolve_export_path,
    validate_texture_sampler,
)
from endfield_scene_fog_contract import (
    MaterialRecord,
    fog_texture_slot as _texture_slot,
    validate_fog_material,
)


class _TextureSample(NamedTuple):
    value: bpy.types.NodeSocket
    limitations: tuple[str, ...]


def _link(tree: bpy.types.NodeTree, target: bpy.types.NodeSocket,
          value: bpy.types.NodeSocket | float | tuple[float, ...]) -> None:
    if isinstance(value, bpy.types.NodeSocket):
        tree.links.new(value, target)
    else:
        target.default_value = value


def _math(tree: bpy.types.NodeTree, label: str, operation: str,
          left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float,
          clamp: bool) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeMath")
    node.name = node.label = label
    node.operation = operation
    node.use_clamp = clamp
    _link(tree, node.inputs[0], left)
    _link(tree, node.inputs[1], right)
    return node.outputs[0]


def _vector(tree: bpy.types.NodeTree, label: str, operation: str,
            left: bpy.types.NodeSocket | tuple[float, ...],
            right: bpy.types.NodeSocket | tuple[float, ...]) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.name = node.label = label
    node.operation = operation
    _link(tree, node.inputs[0], left)
    _link(tree, node.inputs[1], right)
    return node.outputs[0]


def _scale(tree: bpy.types.NodeTree, label: str, vector: bpy.types.NodeSocket,
           factor: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.name = node.label = label
    node.operation = "SCALE"
    tree.links.new(vector, node.inputs[0])
    _link(tree, node.inputs[3], factor)
    return node.outputs[0]


def _sampler(record: MaterialRecord, property_name: str,
             reference: Mapping[str, object]) -> tuple[str, str, tuple[str, ...]]:
    sampler_value = reference.get("TextureSampler")
    if sampler_value is None:
        return "REPEAT", "Linear", (f"{property_name} sampler settings were not serialized; preview uses Linear/REPEAT",)
    sampler = validate_texture_sampler(sampler_value, f"FogSimple material={record['Name']} property={property_name}")
    wraps = (sampler["WrapU"], sampler["WrapV"])
    if wraps == (0, 0):
        extension = "REPEAT"
    elif wraps == (1, 1):
        extension = "EXTEND"
    else:
        raise NotImplementedError(f"FogSimple sampler wrap is unsupported: material={record['Name']}, property={property_name}, wrap={wraps}")
    limitations = tuple(reason for condition, reason in (
        (sampler["FilterMode"] == 2, f"{property_name} trilinear mip filtering is approximated by Blender Linear"),
        (sampler["Aniso"] > 1, f"{property_name} anisotropic filtering is not reconstructed"),
        (sampler["MipBias"] != 0, f"{property_name} mip bias is not reconstructed"),
    ) if condition)
    interpolation = "Closest" if sampler["FilterMode"] == 0 else "Linear"
    return extension, interpolation, limitations


def _sample_texture(tree: bpy.types.NodeTree, record: MaterialRecord, property_name: str,
                    textures: dict[tuple[str, int], tuple[Path, str]],
                    uv: bpy.types.NodeSocket) -> _TextureSample:
    slot = _texture_slot(record, property_name)
    reference = slot["Texture"]
    if reference is None:
        if not has_shader_white_texture_default(record, property_name):
            raise FileNotFoundError(f"FogSimple texture has no source binding or white shader default: material={record['Name']}, property={property_name}")
        value = tree.nodes.new("ShaderNodeValue")
        value.name = value.label = f"Source {property_name} White Default"
        value.outputs[0].default_value = 1.0
        value["endfield_source_default"] = "serialized_shader_texture_property:white:2D"
        value["endfield_layout_section"] = "Source Texture and UV"
        return _TextureSample(value.outputs[0], ())

    asset = cast(Mapping[str, object], reference)
    key = reference_key(asset)
    if key not in textures:
        raise FileNotFoundError(f"FogSimple texture was not decoded: material={record['Name']}, property={property_name}, reference={key}")
    root, filename = textures[key]
    path = resolve_export_path(root / filename)
    if not path.is_file():
        raise FileNotFoundError(f"FogSimple exported texture is missing: material={record['Name']}, property={property_name}, path={path}")
    color_space = asset.get("ColorSpace")
    if type(color_space) is not int or color_space not in (0, 1):
        raise ValueError(f"FogSimple texture ColorSpace is missing or invalid: material={record['Name']}, property={property_name}, reference={key}, value={color_space!r}")
    extension, interpolation, limitations = _sampler(record, property_name, asset)
    node = tree.nodes.new("ShaderNodeTexImage")
    node.name = node.label = f"Source {property_name}"
    node.image = texture_image(path, "Non-Color" if color_space == 0 else "sRGB", "STRAIGHT")
    node.extension = extension
    node.interpolation = interpolation
    tree.links.new(uv, node.inputs["Vector"])
    node["endfield_layout_section"] = "Source Texture and UV"
    node["endfield_texture_identity"] = f"{key[0]}:{key[1]}"
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    separate.name = separate.label = f"{property_name} Scalar Mask"
    separate.mode = "RGB"
    tree.links.new(node.outputs["Color"], separate.inputs["Color"])
    return _TextureSample(separate.outputs["Red"], limitations)


def _mapped_uv(tree: bpy.types.NodeTree, label: str, uv: bpy.types.NodeSocket,
               slot: Mapping[str, object]) -> bpy.types.NodeSocket:
    scale_value = slot.get("Scale")
    offset_value = slot.get("Offset")
    if (not isinstance(scale_value, list) or len(scale_value) != 2
            or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in scale_value)
            or not isinstance(offset_value, list) or len(offset_value) != 2
            or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in offset_value)):
        raise ValueError(f"FogSimple UV scale/offset is invalid: property={label}, scale={scale_value!r}, offset={offset_value!r}")
    scaled = _vector(tree, f"{label} UV Scale", "MULTIPLY", uv,
                     (float(scale_value[0]), float(scale_value[1]), 1.0))
    return _vector(tree, f"{label} UV Offset", "ADD", scaled,
                   (float(offset_value[0]), float(offset_value[1]), 0.0))


def _fade_window(tree: bpy.types.NodeTree, label: str, depth: bpy.types.NodeSocket,
                 start: float, end: float) -> tuple[bpy.types.NodeSocket, bool]:
    if end < start:
        raise ValueError(f"FogSimple fade window end precedes start: label={label}, start={start}, end={end}")
    delta = _math(tree, f"{label} Depth Offset", "SUBTRACT", depth, start, False)
    if end == start:
        return _math(tree, f"{label} Zero Width Limit", "GREATER_THAN", delta, 0.0, False), True
    return _math(tree, f"{label} Ramp", "DIVIDE", delta, end - start, True), False


def _fresnel(tree: bpy.types.NodeTree, geometry: bpy.types.Node,
             floats: Mapping[str, float]) -> bpy.types.NodeSocket:
    orientation = _math(tree, "Fog Fresnel Backface Sign", "MULTIPLY",
                        geometry.outputs["Backfacing"], -2.0, False)
    orientation = _math(tree, "Fog Fresnel Normal Sign", "ADD", orientation, 1.0, False)
    normal = _scale(tree, "Fog Fresnel Source Normal", geometry.outputs["Normal"], orientation)
    dot = tree.nodes.new("ShaderNodeVectorMath")
    dot.name = dot.label = "Fog Fresnel Signed Facing"
    dot.operation = "DOT_PRODUCT"
    tree.links.new(normal, dot.inputs[0])
    tree.links.new(geometry.outputs["Incoming"], dot.inputs[1])
    facing = _math(tree, "Fog Fresnel Facing Clamp", "MINIMUM",
                   _math(tree, "Fog Fresnel Facing Low", "MAXIMUM", dot.outputs["Value"], 0.0, False),
                   1.0, False)
    flipped = _math(tree, "Fog Fresnel Flip Mix", "ADD",
                    _math(tree, "Fog Fresnel Unflipped", "MULTIPLY", 1.0 - floats["_FlipFresnel"],
                          _math(tree, "Fog Fresnel Edge", "SUBTRACT", 1.0, facing, False), False),
                    _math(tree, "Fog Fresnel Flipped", "MULTIPLY", floats["_FlipFresnel"], facing, False), False)
    powered = _math(tree, "Fog Fresnel Positive Epsilon", "MAXIMUM", flipped, 0.00048828125, False)
    powered = _math(tree, "Fog Fresnel Contract", "POWER", powered, floats["_FresnelContract"], False)
    return _math(tree, "Fog Fresnel Intensity Mix", "ADD",
                 _math(tree, "Fog Fresnel Base", "SUBTRACT", 1.0, floats["_FresnelIntensity"], False),
                 _math(tree, "Fog Fresnel Weighted", "MULTIPLY", powered,
                       floats["_FresnelIntensity"], False), False)


def _top_fade(tree: bpy.types.NodeTree, position: bpy.types.NodeSocket,
              contract: float, offset: float) -> bpy.types.NodeSocket:
    """Evaluate source object-origin height normalized by its transformed local up axis."""
    origin = tree.nodes.new("ShaderNodeVectorTransform")
    origin.name = origin.label = "Fog Source Object Origin"
    origin.vector_type = "POINT"
    origin.convert_from = "OBJECT"
    origin.convert_to = "WORLD"
    origin.inputs[0].default_value = (0.0, 0.0, 0.0)
    delta = _vector(tree, "Fog Object Origin Minus Surface", "SUBTRACT", origin.outputs[0], position)
    height = tree.nodes.new("ShaderNodeSeparateXYZ")
    height.name = height.label = "Fog Object Relative World Height"
    tree.links.new(delta, height.inputs[0])
    up = tree.nodes.new("ShaderNodeVectorTransform")
    up.name = up.label = "Fog Source Local Up To World"
    up.vector_type = "VECTOR"
    up.convert_from = "OBJECT"
    up.convert_to = "WORLD"
    up.inputs[0].default_value = (0.0, 0.0, 1.0)
    length = tree.nodes.new("ShaderNodeVectorMath")
    length.name = length.label = "Fog Transformed Local Up Length"
    length.operation = "LENGTH"
    tree.links.new(up.outputs[0], length.inputs[0])
    denominator = _math(tree, "Fog Source Matrix Length Minimum", "MAXIMUM",
                        length.outputs["Value"], 1.1754943508222875e-38, False)
    normalized = _math(tree, "Fog Normalized Object Height", "DIVIDE", height.outputs["Z"], denominator, False)
    scaled = _math(tree, "Fog Source Top Fade Contract", "MULTIPLY", normalized, contract, False)
    return _math(tree, "Fog Source Top Fade Ramp", "ADD", scaled, offset, True)


def make_fog_material(record: MaterialRecord,
                      textures: dict[tuple[str, int], tuple[Path, str]],
                      snapshot_time: float, vertex_color_components: int | None) -> bpy.types.Material:
    """Build a cross-engine FogSimple card preview from source mesh UV0 and material records."""
    if type(snapshot_time) not in (int, float) or not math.isfinite(snapshot_time):
        raise ValueError(f"FogSimple snapshot time must be finite: material={record.get('Name')}, value={snapshot_time!r}")
    source, floats = validate_fog_material(record, vertex_color_components)
    name = source["Name"]
    source_json = json.dumps(source, sort_keys=True, allow_nan=False)
    texture_keys: list[object] = []
    for property_name in ("_MainTex", "_NoiseTex"):
        reference = _texture_slot(source, property_name)["Texture"]
        if reference is None:
            if not has_shader_white_texture_default(source, property_name):
                raise FileNotFoundError(f"FogSimple texture has no white default: material={name}, property={property_name}")
            texture_keys.append("shader_white_default")
        else:
            key = reference_key(reference)
            if key not in textures:
                raise FileNotFoundError(f"FogSimple texture was not decoded: material={name}, property={property_name}, reference={key}")
            root, filename = textures[key]
            path = resolve_export_path(root / filename)
            if not path.is_file():
                raise FileNotFoundError(f"FogSimple exported texture is missing: material={name}, property={property_name}, path={path}")
            texture_keys.append([list(key), str(path.resolve()).casefold()])
    identity = hashlib.sha256(json.dumps(
        ["FogSimpleObjectHeight/2", source_json, texture_keys, float(snapshot_time), vertex_color_components],
        sort_keys=True, allow_nan=False).encode()).hexdigest()[:24]
    material_name = "EF_FOG_SIMPLE_" + identity
    existing = bpy.data.materials.get(material_name)
    if existing is not None:
        if existing.get("endfield_fx_source_material_json") != source_json:
            raise ValueError(f"Cached FogSimple material conflicts with source: material={material_name}")
        if not existing.use_nodes or existing.node_tree is None:
            raise ValueError(f"Cached FogSimple material lost its node tree: material={material_name}")
        return existing

    material = bpy.data.materials.new(material_name)
    material.use_nodes = True
    material.surface_render_method = "BLENDED"
    material.use_transparency_overlap = True
    color = source["Colors"]["_BaseColor"]
    material.diffuse_color = (float(color[0]), float(color[1]), float(color[2]), 1.0)
    material["endfield_fx_source_material"] = name
    material["endfield_fx_source"] = source["Source"]
    material["endfield_fx_path_id"] = str(source["PathId"])
    material["endfield_fx_source_material_json"] = source_json
    material["endfield_fx_vertex_color_status"] = "not_consumed_by_shader"
    if vertex_color_components is not None:
        material["endfield_fx_source_vertex_color_components"] = vertex_color_components
    material["endfield_fx_status"] = "source_formula_preview_partial"
    material["endfield_fx_source_blend"] = "One/One" if floats["_DstBlend"] == 1.0 else "One/OneMinusSrcAlpha"
    material["endfield_fx_source_z_test"] = "LEqual"
    material["endfield_fx_source_z_write"] = False
    material["endfield_fx_source_depth_only_pass"] = "disabled"
    limitations = [
        "Unity per-camera exposure and grading are not reconstructed",
        "Zero-width serialized fade windows use their non-equality step limit; exact threshold equality is undefined in the compiled shader",
    ]
    if floats["_UseSoftBlend"] == 1.0:
        limitations.append("Screen-space scene-depth soft intersection is not available in the cross-engine node preview; source softblend is held at one")

    tree = material.node_tree
    tree.nodes.clear()
    coordinates = tree.nodes.new("ShaderNodeTexCoord")
    coordinates.name = coordinates.label = "FogSimple Source Mesh UV0"
    coordinates["endfield_source_uv"] = "UV0 active mesh coordinates"
    coordinates["endfield_layout_section"] = "Source Texture and UV"
    main_uv = _mapped_uv(tree, "Fog MainTex", coordinates.outputs["UV"], _texture_slot(source, "_MainTex"))
    noise_uv = _mapped_uv(tree, "Fog NoiseTex", coordinates.outputs["UV"], _texture_slot(source, "_NoiseTex"))
    main_sample = _sample_texture(tree, source, "_MainTex", textures, main_uv)
    static_noise = _sample_texture(tree, source, "_NoiseTex", textures, noise_uv)
    limitations.extend(main_sample.limitations)
    limitations.extend(static_noise.limitations)

    source_time = tree.nodes.new("ShaderNodeValue")
    source_time.name = "EF FX Source Time Seconds"
    source_time.label = "EF FX Source Time Seconds (Unity _Time.y)"
    source_time.outputs[0].default_value = float(snapshot_time)
    source_time["endfield_layout_section"] = "Source Time and Motion"
    animated_scale = _math(tree, "Fog Wind Separate Scale Blend X", "ADD",
                           1.0 - floats["_UseSeparateScale"],
                           floats["_WindScaleX"] * floats["_UseSeparateScale"], False)
    animated_scale_y = _math(tree, "Fog Wind Separate Scale Blend Y", "ADD",
                             1.0 - floats["_UseSeparateScale"],
                             floats["_WindScaleY"] * floats["_UseSeparateScale"], False)
    wind_scale = tree.nodes.new("ShaderNodeCombineXYZ")
    wind_scale.name = wind_scale.label = "Fog Wind Scale XY"
    tree.links.new(_math(tree, "Fog Wind Scale X", "MULTIPLY", animated_scale,
                         floats["_WindScale"], False), wind_scale.inputs["X"])
    tree.links.new(_math(tree, "Fog Wind Scale Y", "MULTIPLY", animated_scale_y,
                         floats["_WindScale"], False), wind_scale.inputs["Y"])
    wind_scale.inputs["Z"].default_value = 1.0
    wind_uv = _vector(tree, "Fog Wind UV Scale", "MULTIPLY", coordinates.outputs["UV"], wind_scale.outputs[0])
    wind_time = _math(tree, "Fog Wind Time Speed", "MULTIPLY", source_time.outputs[0],
                      floats["_WindSpeed"], False)
    wind_offset = tree.nodes.new("ShaderNodeCombineXYZ")
    wind_offset.name = wind_offset.label = "Fog Wind Direction Offset"
    tree.links.new(_math(tree, "Fog Wind Direction X", "MULTIPLY", -floats["_WindDirX"], wind_time, False),
                   wind_offset.inputs["X"])
    tree.links.new(_math(tree, "Fog Wind Direction Y", "MULTIPLY", floats["_WindDirY"], wind_time, False),
                   wind_offset.inputs["Y"])
    wind_offset.inputs["Z"].default_value = 0.0
    wind_uv = _vector(tree, "Fog Wind UV Pan", "ADD", wind_uv, wind_offset.outputs[0])
    animated_noise = _sample_texture(tree, source, "_NoiseTex", textures, wind_uv)
    limitations.extend(animated_noise.limitations)

    static_mix = _math(tree, "Fog Static Noise Weight", "MULTIPLY",
                        static_noise.value, floats["_FogNoiseIntensity"], False)
    static_mix = _math(tree, "Fog Static Noise Base", "ADD", 1.0 - floats["_FogNoiseIntensity"],
                        static_mix, False)
    contrast_scale = 2.0 * floats["_WindNoiseContrast"] + 1.0
    wind_noise = _math(tree, "Fog Wind Noise Contrast", "MULTIPLY",
                       animated_noise.value, contrast_scale, False)
    wind_noise = _math(tree, "Fog Wind Noise Contrast Offset", "SUBTRACT",
                       wind_noise, floats["_WindNoiseContrast"], True)
    wind_noise = _math(tree, "Fog Wind Noise Intensity", "ADD",
                       1.0 - floats["_WindNoiseIntensity"],
                       _math(tree, "Fog Wind Noise Weighted", "MULTIPLY", wind_noise,
                             floats["_WindNoiseIntensity"], False), False)
    wind_mix = _math(tree, "Fog Wind Enable", "ADD", 1.0 - floats["_UseWind"],
                     _math(tree, "Fog Wind Weighted", "MULTIPLY", wind_noise,
                           floats["_UseWind"], False), False)
    alpha = _math(tree, "Fog Main Mask and Static Noise", "MULTIPLY", main_sample.value, static_mix, False)
    alpha = _math(tree, "Fog Animated Wind Noise", "MULTIPLY", alpha, wind_mix, False)
    alpha = _math(tree, "Fog Base Color Alpha", "MULTIPLY", alpha, float(color[3]), False)
    alpha = _math(tree, "Fog Base Alpha Intensity", "MULTIPLY", alpha,
                  floats["_BaseAlphaIntensity"], False)

    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    geometry.name = geometry.label = "FogSimple World Coordinates"
    camera = tree.nodes.new("ShaderNodeCameraData")
    camera.name = camera.label = "FogSimple Camera Coordinates"
    view_depth = _math(tree, "Fog View Depth Absolute", "ABSOLUTE", camera.outputs["View Z Depth"], 0.0, False)
    distance_fade, distance_degenerate = _fade_window(
        tree, "Fog Distance Fade", view_depth, floats["_DistanceFadeStart"], floats["_DistanceFadeEnd"])
    near_fade, near_degenerate = _fade_window(
        tree, "Fog Near Camera Fade", view_depth, floats["_NearCameraFadeDistanceStart"],
        floats["_NearCameraFadeDistanceEnd"])
    alpha = _math(tree, "Fog Distance Fade Pair", "MULTIPLY", alpha, distance_fade, False)
    alpha = _math(tree, "Fog Near Fade Pair", "MULTIPLY", alpha, near_fade, False)
    if distance_degenerate or near_degenerate:
        limitations.append("One or more distance windows have equal endpoints and were represented as step limits")

    top_fade = _top_fade(tree, geometry.outputs["Position"], floats["_TopFadeContract"], floats["_TopFadeOffset"])
    alpha = _math(tree, "Fog Top Fade", "MULTIPLY", alpha, top_fade, False)
    rgb_weight = wind_mix
    if floats["_UseFresnel"] == 1.0:
        fresnel = _fresnel(tree, geometry, floats)
        alpha = _math(tree, "Fog Fresnel Alpha", "MULTIPLY", alpha, fresnel, False)
        rgb_weight = _math(tree, "Fog Source Wind and Fresnel RGB", "MULTIPLY", wind_mix, fresnel, False)

    if floats["_UseSoftBlend"] == 1.0:
        soft_reference = tree.nodes.new("ShaderNodeValue")
        soft_reference.name = soft_reference.label = "Fog Soft Depth Blend - Reference 1"
        soft_reference.outputs[0].default_value = 1.0
        soft_reference["endfield_preview_condition"] = "Screen depth is not reconstructed"
        alpha = _math(tree, "Fog Soft Depth Blend Reference", "MULTIPLY", alpha,
                      soft_reference.outputs[0], False)
    alpha = _math(tree, "Fog Source Alpha Lower Clamp", "MAXIMUM", alpha, 0.0, False)
    alpha = _math(tree, "Fog Source Alpha Upper Clamp", "MINIMUM", alpha, 1.0, False)

    tint = tree.nodes.new("ShaderNodeRGB")
    tint.name = tint.label = "Fog Source BaseColor"
    tint.outputs[0].default_value = (float(color[0]), float(color[1]), float(color[2]), 1.0)
    weighted_rgb = _scale(tree, "Fog Source RGB Before Premultiply", tint.outputs[0], rgb_weight)
    premultiplied = _scale(tree, "Fog Source Premultiplied RGB", weighted_rgb, alpha)
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    transparent.name = transparent.label = "Fog Source Destination Contribution"
    destination_factor = 1.0 if floats["_DstBlend"] == 1.0 else _math(
        tree, "Fog Source Over Destination Factor", "SUBTRACT", 1.0, alpha, False)
    transparent_color = tree.nodes.new("ShaderNodeCombineColor")
    transparent_color.name = transparent_color.label = "Fog Source Destination RGBA"
    transparent_color.mode = "RGB"
    for channel in ("Red", "Green", "Blue"):
        _link(tree, transparent_color.inputs[channel], destination_factor)
    tree.links.new(transparent_color.outputs["Color"], transparent.inputs["Color"])
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = emission.label = "Fog Source Premultiplied Color"
    tree.links.new(premultiplied, emission.inputs["Color"])
    composite = tree.nodes.new("ShaderNodeAddShader")
    composite.name = composite.label = "Fog Source Blend"
    tree.links.new(transparent.outputs[0], composite.inputs[0])
    tree.links.new(emission.outputs[0], composite.inputs[1])
    surface: bpy.types.NodeSocket = composite.outputs[0]
    if floats["_CullMode"] == 2.0:
        backface = tree.nodes.new("ShaderNodeBsdfTransparent")
        backface.name = backface.label = "Fog Source Culled Backface"
        cull = tree.nodes.new("ShaderNodeMixShader")
        cull.name = cull.label = "Fog Source Backface Culling"
        tree.links.new(geometry.outputs["Backfacing"], cull.inputs[0])
        tree.links.new(surface, cull.inputs[1])
        tree.links.new(backface.outputs[0], cull.inputs[2])
        surface = cull.outputs[0]
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.name = output.label = "FogSimple Preview Output"
    tree.links.new(surface, output.inputs["Surface"])
    material["endfield_fx_limitations"] = json.dumps(list(dict.fromkeys(limitations)))
    material["endfield_fx_fog_simple_shader"] = "HGRP/FogSimple"
    material["endfield_fx_fog_top_fade_coordinates"] = "object_origin_minus_world_position_z_divided_by_transformed_local_up_length; uv_not_used"
    material["endfield_fx_fog_contract"] = "FogSimpleObjectHeight/2"
    material["endfield_fx_fog_softblend_status"] = (
        "constant_one_screen_depth_unavailable" if floats["_UseSoftBlend"] == 1.0 else "not_enabled")
    material["endfield_fx_status"] = "source_formula_preview_partial" if limitations else "source_formula_preview"
    layout_shader_node_tree(tree)
    return material
