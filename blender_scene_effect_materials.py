"""Portable particle materials for the verified VFXBaseV2 texture-role variants.

Source blend/channel semantics are retained. Missing runtime screen buffers and
pipeline controls are exposed as explicit preview conditions, never as fidelity.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import bpy

from blender_scene_materials import layout_shader_node_tree
from blender_scene_effect_layers import make_vfx_feature_source, vfx_feature_issues, vfx_custom1_channels
from endfield_scene_material_contract import MaterialRecord, reference_key, resolve_export_path


def _link(links: bpy.types.NodeLinks, target: bpy.types.NodeSocket,
          value: bpy.types.NodeSocket | float | tuple[float, ...]) -> None:
    if isinstance(value, bpy.types.NodeSocket):
        links.new(value, target)
    else:
        target.default_value = value


def _math(tree: bpy.types.NodeTree, label: str, operation: str,
          left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeMath")
    node.operation, node.label, node.name = operation, label, label
    _link(tree.links, node.inputs[0], left)
    _link(tree.links, node.inputs[1], right)
    return node.outputs[0]


def _vector(tree: bpy.types.NodeTree, label: str, operation: str,
            left: bpy.types.NodeSocket | tuple[float, ...],
            right: bpy.types.NodeSocket | tuple[float, ...]) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation, node.label, node.name = operation, label, label
    _link(tree.links, node.inputs[0], left)
    _link(tree.links, node.inputs[1], right)
    return node.outputs[0]


def _scale(tree: bpy.types.NodeTree, label: str, rgb: bpy.types.NodeSocket,
           factor: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation, node.label, node.name = "SCALE", label, label
    tree.links.new(rgb, node.inputs[0])
    _link(tree.links, node.inputs[3], factor)
    return node.outputs[0]


def _control(tree: bpy.types.NodeTree, label: str, value: float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeValue")
    node.name = node.label = label
    node.outputs[0].default_value = value
    node["endfield_layout_section"] = "Explicit Preview Conditions"
    node["endfield_layout_note"] = "Editable reference state"
    return node.outputs[0]


def _composite_group() -> bpy.types.NodeTree:
    name = "Endfield FX Source Over or Additive v1"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_fx_contract") != "premultiplied_source_destination/1":
            raise ValueError(f"Conflicting FX node group: {name}")
        return existing
    group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    group["endfield_fx_contract"] = "premultiplied_source_destination/1"
    group.interface.new_socket(name="Source RGB", in_out="INPUT", socket_type="NodeSocketColor")
    group.interface.new_socket(name="Coverage", in_out="INPUT", socket_type="NodeSocketFloat")
    group.interface.new_socket(name="Output Alpha", in_out="INPUT", socket_type="NodeSocketFloat")
    group.interface.new_socket(name="Surface", in_out="OUTPUT", socket_type="NodeSocketShader")
    entry = group.nodes.new("NodeGroupInput")
    premultiplied = _scale(group, "Premultiply Source Once", entry.outputs["Source RGB"], entry.outputs["Coverage"])
    remaining = _math(group, "Destination Factor", "SUBTRACT", 1.0, entry.outputs["Output Alpha"])
    transparent = group.nodes.new("ShaderNodeBsdfTransparent")
    group.links.new(remaining, transparent.inputs[0])
    emission = group.nodes.new("ShaderNodeEmission")
    group.links.new(premultiplied, emission.inputs["Color"])
    add = group.nodes.new("ShaderNodeAddShader")
    group.links.new(transparent.outputs[0], add.inputs[0])
    group.links.new(emission.outputs[0], add.inputs[1])
    output = group.nodes.new("NodeGroupOutput")
    group.links.new(add.outputs[0], output.inputs[0])
    layout_shader_node_tree(group)
    return group


def make_effect_material(record: MaterialRecord,
                         textures: dict[tuple[str, int], tuple[Path, str]],
                         snapshot_time: float, vertex_streams: tuple[int, ...]) -> bpy.types.Material:
    """Build only a supported shader feature combination with exact source textures."""
    shader = record["Shader"].get("Name")
    if shader != "HGRP/Effect/VFXBaseV2":
        raise NotImplementedError(f"FX shader family is not implemented: shader={shader}, material={record['Name']}")
    floats = record["Floats"]
    unsupported = vfx_feature_issues(record, vertex_streams)
    if unsupported:
        raise NotImplementedError(f"FX material features are not implemented: material={record['Name']}, features={unsupported}")
    if floats["_SurfaceType"] != 1 or floats["_SrcBlend"] != 1 or floats["_DstBlend"] != 10:
        raise NotImplementedError(f"FX blend factors are not verified: material={record['Name']}")
    if floats.get("_AlphaSrcBlend") != 1 or floats.get("_AlphaDstBlend") != 10:
        raise NotImplementedError(f"FX alpha blend factors are not verified: material={record['Name']}")
    if floats["_BlendMode"] not in (0, 1) or floats["_UseMainTexAsAlpha"] not in (0, 1):
        raise NotImplementedError(f"FX blend/channel mode is not verified: material={record['Name']}")
    if floats.get("_CullMode") not in (0, 2) or any(floats.get(name) != value for name, value in (("_ZWrite", 0), ("_ZTest", 4), ("_DisableZTest", 0))):
        raise NotImplementedError(f"FX culling/depth state is not verified: material={record['Name']}")
    slots = [slot for slot in record["Textures"] if slot["Property"] == "_MainTex" and slot["Texture"]]
    if len(slots) != 1:
        raise FileNotFoundError(f"FX requires a unique MainTex binding: material={record['Name']}")
    slot = slots[0]
    ref = slot["Texture"]
    if ref is None:
        raise FileNotFoundError(f"FX MainTex is null: material={record['Name']}")
    key = reference_key(ref)
    if key not in textures:
        raise FileNotFoundError(f"FX texture was not decoded: material={record['Name']}, reference={key}")
    root, filename = textures[key]
    path = resolve_export_path(root / filename)
    if not path.is_file():
        raise FileNotFoundError(path)
    if ref.get("ColorSpace") not in (0, 1):
        raise ValueError(f"FX texture source color space is missing: material={record['Name']}, reference={key}")
    identity = hashlib.sha256(json.dumps([record, str(path), snapshot_time, vertex_streams], sort_keys=True).encode()).hexdigest()[:24]
    name = "EF_FX_" + identity
    existing = bpy.data.materials.get(name)
    if existing is not None:
        return existing
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.surface_render_method = "BLENDED"
    material.use_transparency_overlap = True
    material["endfield_fx_source_material"] = record["Name"]
    material["endfield_fx_source"] = record["Source"]
    material["endfield_fx_path_id"] = str(record["PathId"])
    material["endfield_fx_status"] = "static_preview_subset"
    material["endfield_fx_custom1_channels"] = json.dumps(vfx_custom1_channels(record))
    limitations = ["Game exposure and grading not captured"]
    for prop, reason in (("_UseFog", "Game fog not reconstructed"),
                         ("_UseSoftBlend", "Screen-depth soft intersection not reconstructed"),
                         ("_UseNearCameraFade", "Game near-camera fade fixed at one")):
        if floats.get(prop, 0):
            limitations.append(reason)
    tree = material.node_tree
    tree.nodes.clear()
    source = make_vfx_feature_source(tree, record, textures, snapshot_time, vertex_streams)
    rgb, alpha = source.rgb, source.alpha
    material["endfield_fx_limitations"] = json.dumps([*limitations, *source.limitations])
    excess = _vector(tree, "Exposure Threshold", "SUBTRACT", rgb, (floats["_ExpThreshold"],) * 3)
    excess = _vector(tree, "Positive Exposure Excess", "MAXIMUM", excess, (0, 0, 0))
    rgb = _vector(tree, "Exposure Gain", "ADD", rgb, _scale(tree, "Exposure Intensity", excess, floats["_ExpIntensity"]))
    rgb = _scale(tree, "Reference Exposure", rgb, _control(tree, "Exposure - Reference 1", 1.0))
    rgb = _vector(tree, "Clamp Source RGB Low", "MAXIMUM", rgb, (0, 0, 0))
    rgb = _vector(tree, "Clamp Source RGB High", "MINIMUM", rgb, (1000, 1000, 1000))
    alpha = _math(tree, "Reference Depth and Runtime Fade", "MULTIPLY", alpha,
                  _control(tree, "Screen and Runtime Fade - Reference 1", 1.0))
    alpha = _math(tree, "Opacity Lower Bound", "MAXIMUM", alpha, 0.0)
    alpha = _math(tree, "Opacity Upper Bound", "MINIMUM", alpha, 1.0)
    composite = tree.nodes.new("ShaderNodeGroup")
    composite.node_tree = _composite_group()
    composite.label = "Source Blend and Transparent Background"
    tree.links.new(rgb, composite.inputs["Source RGB"])
    tree.links.new(alpha, composite.inputs["Coverage"])
    output_alpha = _math(tree, "Source BlendMode Output Alpha", "MULTIPLY", alpha, 1 - floats["_BlendMode"])
    tree.links.new(output_alpha, composite.inputs["Output Alpha"])
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    surface = composite.outputs[0]
    if floats["_CullMode"] == 2:
        geometry = tree.nodes.new("ShaderNodeNewGeometry")
        transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
        cull = tree.nodes.new("ShaderNodeMixShader")
        cull.name = cull.label = "Source Backface Culling"
        tree.links.new(geometry.outputs["Backfacing"], cull.inputs[0])
        tree.links.new(surface, cull.inputs[1])
        tree.links.new(transparent.outputs[0], cull.inputs[2])
        surface = cull.outputs[0]
    tree.links.new(surface, output.inputs["Surface"])
    layout_shader_node_tree(tree)
    return material
