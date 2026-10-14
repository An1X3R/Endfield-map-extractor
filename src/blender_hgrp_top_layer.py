"""Decode the two-map HGRP Lit top layer without inventing runtime terrain data."""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import bpy

from blender_hgrp_surface import packed_normal_group, authored_albedo_output
from blender_scene_effect_layers import _link, _math, _scale, _vec, _sampler_settings
from blender_scene_materials import texture_image
from endfield_scene_material_contract import MaterialRecord, reference_key, has_shader_white_texture_default


class LayerSample(NamedTuple):
    rgb: bpy.types.NodeSocket
    alpha: bpy.types.NodeSocket


def _mix(tree: bpy.types.NodeTree, label: str, left: bpy.types.NodeSocket | tuple[float, ...],
         right: bpy.types.NodeSocket | tuple[float, ...], factor: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeMixRGB")
    node.name = node.label = label
    _link(tree, node.inputs[0], factor)
    _link(tree, node.inputs[1], left)
    _link(tree, node.inputs[2], right)
    return node.outputs[0]


def _component(tree: bpy.types.NodeTree, value: bpy.types.NodeSocket, index: int) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(value, node.inputs[0])
    return node.outputs[index]


def _clamp(tree: bpy.types.NodeTree, label: str, value: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeClamp")
    node.label = label
    tree.links.new(value, node.inputs[0])
    return node.outputs[0]


def _uv(tree: bpy.types.NodeTree, channel: int) -> bpy.types.NodeSocket:
    if channel == 0:
        return tree.nodes.new("ShaderNodeTexCoord").outputs["UV"]
    node = tree.nodes.new("ShaderNodeUVMap")
    node.uv_map = f"EF_SOURCE_UV{channel}"
    node.label = f"Source TEXCOORD{channel}; source-absent channels use explicit zero reference"
    return node.outputs[0]


def _sample(material: bpy.types.Material, record: MaterialRecord, property_name: str,
            textures: dict[tuple[str, int], tuple[Path, str]], uv: bpy.types.NodeSocket,
            colorspace: str, pack: bool) -> LayerSample:
    slot = next(row for row in record["Textures"] if row["Property"] == property_name)
    ref = slot["Texture"]
    if ref is None:
        if has_shader_white_texture_default(record, property_name):
            color = material.node_tree.nodes.new("ShaderNodeRGB")
            color.outputs[0].default_value = (1, 1, 1, 1)
            color.label = property_name + " authored white default"
            alpha = material.node_tree.nodes.new("ShaderNodeValue")
            alpha.outputs[0].default_value = 1
            return LayerSample(color.outputs[0], alpha.outputs[0])
        raise ValueError(f"Top layer requires bound texture: material={record['Name']}, property={property_name}")
    key = reference_key(ref)
    if key not in textures:
        raise ValueError(f"Top layer texture export missing: material={record['Name']}, property={property_name}, reference={key}")
    root, relative = textures[key]
    path = root / relative.replace("\\", "/")
    if not path.is_file():
        raise FileNotFoundError(path)
    sampler = _sampler_settings(record, property_name, ref)
    if sampler.issue is not None:
        raise ValueError(f"Unsupported top layer sampler: material={record['Name']}, issue={sampler.issue}")
    node = material.node_tree.nodes.new("ShaderNodeTexImage")
    node.name = node.label = property_name
    node.image = texture_image(path, colorspace, "CHANNEL_PACKED")
    if pack and not node.image.packed_file:
        node.image.pack()
    node.extension = sampler.extension
    node.interpolation = sampler.interpolation
    material.node_tree.links.new(uv, node.inputs["Vector"])
    return LayerSample(node.outputs["Color"], node.outputs["Alpha"])


def _decoded_normal(tree: bpy.types.NodeTree, texture: LayerSample, strength: float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeGroup")
    node.node_tree = packed_normal_group()
    node.inputs["Multiply Red By Alpha"].default_value = 0
    node.inputs["Normal Strength"].default_value = strength
    tree.links.new(texture.rgb, node.inputs["Packed RGB"])
    tree.links.new(texture.alpha, node.inputs["Packed Alpha"])
    return _vec(tree, "Decoded tangent normal", "SUBTRACT",
                _scale(tree, "Decode signed normal", node.outputs["Encoded Normal"], 2), (1, 1, 1))


def _base_normal(tree: bpy.types.NodeTree, texture: bpy.types.Node, strength: float) -> bpy.types.NodeSocket:
    """Apply the compiled base-only RG deadzone before reconstructing tangent Z."""
    components = []
    for index in (0, 1):
        value = _math(tree, "Base normal signed scale", "MULTIPLY", _component(tree, texture.outputs["Color"], index), 2)
        value = _math(tree, "Base normal signed offset", "SUBTRACT", value, 1)
        magnitude = _math(tree, "Base normal magnitude", "ABSOLUTE", value, 0)
        dead = _math(tree, "Base normal source deadzone", "LESS_THAN", magnitude, .012)
        keep = _math(tree, "Base normal keep channel", "SUBTRACT", 1, dead)
        components.append(_math(tree, "Base normal cleared channel", "MULTIPLY", value, keep))
    squared = [_math(tree, "Base normal squared channel", "MULTIPLY", value, value) for value in components]
    length = _math(tree, "Base normal XY length squared", "ADD", *squared)
    remaining = _math(tree, "Base normal Z squared", "SUBTRACT", 1, length)
    z = _math(tree, "Base normal reconstructed Z", "SQRT", _clamp(tree, "Base normal Z domain", remaining), 0)
    combine = tree.nodes.new("ShaderNodeCombineXYZ")
    for index, component in enumerate(components):
        tree.links.new(_math(tree, "Base normal source strength", "MULTIPLY", component, strength), combine.inputs[index])
    tree.links.new(z, combine.inputs[2])
    return combine.outputs[0]


def _world_normal(tree: bpy.types.NodeTree, tangent: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    encoded = _vec(tree, "Encoded tangent normal", "ADD", _scale(tree, "Normal encode scale", tangent, .5), (.5, .5, .5))
    normal = tree.nodes.new("ShaderNodeNormalMap")
    tree.links.new(encoded, normal.inputs["Color"])
    return normal.outputs[0]


def apply_top_layer(material: bpy.types.Material, principled: bpy.types.Node, record: MaterialRecord,
                    textures: dict[tuple[str, int], tuple[Path, str]], pack: bool) -> None:
    """Restore source top/height blending; leave unavailable runtime terrain inputs explicit."""
    tree = material.node_tree
    existing = set(tree.nodes)
    floats, colors = record["Floats"], record["Colors"]
    required = ("_Layer1Tilling", "_TopBlendWithBumpMap", "_TopBlendThreshold", "_TopBlendSmoothness",
                "_LayerBlendHeightTransition", "_Layer1Saturation", "_Layer1ColorBrighterScale",
                "_Layer1BumpScale", "_Layer1BaseNormalIntensity", "_LayerMetallicType", "_Layer1Metallic")
    missing = sorted(set(required) - floats.keys())
    if missing or not {"_Layer1Offset", "_Layer1TintColor"} <= colors.keys():
        raise ValueError(f"Top layer source properties missing: material={record['Name']}, floats={missing}")
    for prop, selector in (("_BaseColorMap", "_BaseUVSet"), ("_NormalMap", "_BasePbrMapUVSet")):
        slot = next(row for row in record["Textures"] if row["Property"] == prop)
        for node in tuple(tree.nodes):
            if node.type == "TEX_IMAGE" and node.label == prop:
                coords = _vec(tree, prop + " source ST scale", "MULTIPLY", _uv(tree, int(floats[selector])),
                              (*slot["Scale"], 1))
                coords = _vec(tree, prop + " source ST offset", "ADD", coords, (*slot["Offset"], 0))
                tree.links.new(coords, node.inputs["Vector"])
    uv_mode = int(floats["_LayerBlendUVType"])
    if uv_mode == 1:
        position = tree.nodes.new("ShaderNodeNewGeometry").outputs["Position"]
        world = _vec(tree, "Unity world XZ", "MULTIPLY", position, (1, -1, 0))
        coords = world
    else:
        coords = _uv(tree, 2 if uv_mode == 2 else 0)
    coords = _scale(tree, "Layer source tiling", coords, floats["_Layer1Tilling"])
    offset = colors["_Layer1Offset"]
    coords = _vec(tree, "Layer source offset", "ADD", coords, (offset[0], offset[1], 0))
    layer = _sample(material, record, "_Layer1BaseMap", textures, coords, "sRGB", pack)
    bump = _sample(material, record, "_Layer1BumpMap", textures, coords, "Non-Color", pack)
    mask = _sample(material, record, "_LayerBlendMaskMap", textures, coords, "Non-Color", pack)
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    normal_input = principled.inputs["Normal"]
    base_texture = next((node for node in existing if node.type == "TEX_IMAGE" and node.label == "_NormalMap"), None)
    if base_texture is None:
        raise ValueError(f"Top layer requires source base normal: material={record['Name']}")
    base_ts = _base_normal(tree, base_texture, floats["_NormalScale"])
    base_world = _world_normal(tree, base_ts)
    up = _mix(tree, "Top normal influence", geometry.outputs["Normal"], base_world, floats["_TopBlendWithBumpMap"])
    threshold = _math(tree, "Top threshold", "SUBTRACT", _component(tree, up, 2), floats["_TopBlendThreshold"])
    factor = _clamp(tree, "Top surface coverage", _math(tree, "Top transition", "DIVIDE", threshold,
                    max(1.175494e-38, floats["_TopBlendSmoothness"])))
    if floats["_LayerBlendHeight"] != 0:
        inverse = _math(tree, "Base coverage", "SUBTRACT", 1, factor)
        heights = (_math(tree, "Base weighted height", "MULTIPLY", inverse, _component(tree, mask.rgb, 0)),
                   _math(tree, "Layer weighted height", "MULTIPLY", factor, layer.alpha))
        peak = _math(tree, "Height peak", "MAXIMUM", *heights)
        weights = []
        for label, height, coverage in zip(("Base", "Layer"), heights, (inverse, factor)):
            value = _math(tree, label + " height transition", "ADD", height, floats["_LayerBlendHeightTransition"])
            value = _math(tree, label + " relative height", "SUBTRACT", value, peak)
            value = _math(tree, label + " positive height", "MAXIMUM", value, 0)
            value = _math(tree, label + " height epsilon", "ADD", value, 1e-6)
            weights.append(_math(tree, label + " height weight", "MULTIPLY", value, coverage))
        total = _math(tree, "Height total", "ADD", *weights)
        total = _math(tree, "Height normalization floor", "MAXIMUM", total, 1.175494e-38)
        factor = _math(tree, "Normalized layer height blend", "DIVIDE", weights[1], total)
    base_input = principled.inputs["Base Color"]
    if base_input.is_linked:
        base = base_input.links[0].from_socket
    else:
        constant = tree.nodes.new("ShaderNodeRGB")
        constant.label = "Authored constant base color"
        constant.outputs[0].default_value = tuple(base_input.default_value)
        base = authored_albedo_output(material, record, constant.outputs[0])
    if not base_input.is_linked and not has_shader_white_texture_default(record, "_BaseColorMap"):
        material["endfield_top_base_reference"] = "authored_constant_base_color; unbound texture default unverified"
    luma = tree.nodes.new("ShaderNodeVectorMath")
    luma.operation = "DOT_PRODUCT"
    luma.inputs[1].default_value = (.212672904, .715152204, .0721750036)
    tree.links.new(layer.rgb, luma.inputs[0])
    color = _mix(tree, "Layer saturation", luma.outputs["Value"], layer.rgb,
                 min(1, max(0, floats["_Layer1Saturation"] + 1)))
    color = _vec(tree, "Layer tint", "MULTIPLY", color, tuple(colors["_Layer1TintColor"][:3]))
    color = _scale(tree, "Layer brightness", color, floats["_Layer1ColorBrighterScale"])
    tree.links.new(_mix(tree, "Top layer albedo", base, color, factor), base_input)
    for name, layer_value in (("Metallic", layer.alpha if floats["_LayerMetallicType"] != 0 else floats["_Layer1Metallic"]),
                              ("Roughness", _component(tree, bump.rgb, 2))):
        socket = principled.inputs[name]
        base_value = socket.links[0].from_socket if socket.is_linked else socket.default_value
        base_socket = tree.nodes.new("ShaderNodeValue").outputs[0] if isinstance(base_value, (int, float)) else base_value
        if isinstance(base_value, (int, float)):
            base_socket.default_value = base_value
        layer_socket = tree.nodes.new("ShaderNodeValue").outputs[0] if isinstance(layer_value, (int, float)) else layer_value
        if isinstance(layer_value, (int, float)):
            layer_socket.default_value = layer_value
        tree.links.new(_mix(tree, "Top layer " + name, base_socket, layer_socket, factor), socket)
    layer_ts = _decoded_normal(tree, bump, floats["_Layer1BumpScale"])
    t = _vec(tree, "RNM base", "ADD", base_ts, (0, 0, 1))
    u = _vec(tree, "RNM layer", "MULTIPLY", layer_ts, (-1, -1, 1))
    dot = tree.nodes.new("ShaderNodeVectorMath")
    dot.operation = "DOT_PRODUCT"
    tree.links.new(t, dot.inputs[0])
    tree.links.new(u, dot.inputs[1])
    denominator = _math(tree, "RNM denominator", "MAXIMUM", _component(tree, t, 2), 1e-5)
    ratio = _math(tree, "RNM ratio", "DIVIDE", dot.outputs["Value"], denominator)
    rnm = _vec(tree, "Reoriented layer normal", "SUBTRACT", _scale(tree, "RNM projection", t, ratio), u)
    layered = _mix(tree, "Base normal contribution", layer_ts, rnm, floats["_Layer1BaseNormalIntensity"])
    blended = _mix(tree, "Top layer tangent normal", base_ts, layered, factor)
    tree.links.new(_world_normal(tree, blended), normal_input)
    material["endfield_top_layer_contract"] = "HGRPLitTopLayer/1"
    material["endfield_top_layer_limitations"] = "Runtime terrain atlas and runtime UV injection are not reconstructed; source-absent UV streams use audited zero reference."
    for node in set(tree.nodes) - existing:
        node["endfield_layout_section"] = "Source Top Layer"
