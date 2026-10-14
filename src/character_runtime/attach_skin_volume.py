"""Bind current Skin volume equations without changing the emission bus.

Source global axes use the explicit preview conversion X=(1,0,0), Y=(0,0,1),
Z=(0,-1,0). Camera axes are live Blender camera-to-world preview vectors, not
a captured native inverse-view matrix. Atlas visibility is the existing explicit
screen-shadow preview input. The caller supplies all four CP vectors and owns
the final additive lighting composition and file save.
"""
from __future__ import annotations

import json
import bpy

from blender_scene_materials import layout_shader_node_tree
from character_node_graph import node
from character_weather_nodes import combine_vector, vector_math
from character_skin_volume import Vector4, build_body_skin_volume, build_face_skin_volume
from patch_character_hgrp import by_label, source_keywords

SECTION = "Skin Native Volume Binding"
VOLUME_LABEL = "Compiled Skin Volume / Explicit Preview Frames"


def _source_input(tree: bpy.types.NodeTree, owner: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
    socket = owner.inputs.get(name)
    if socket is None or len(socket.links) != 1:
        raise ValueError(f"{tree.name}: required single input link missing: {owner.label}/{name}")
    return socket.links[0].from_socket


def _source_surface_normal(
    tree: bpy.types.NodeTree,
    surface: bpy.types.Node,
    geometry_normal: bpy.types.NodeSocket,
) -> bpy.types.NodeSocket:
    normal = surface.inputs.get("Normal")
    if normal is None:
        raise ValueError(f"{tree.name}: source Principled surface has no Normal input")
    links = tuple(normal.links)
    if len(links) > 1:
        raise ValueError(f"{tree.name}: source Principled Normal has {len(links)} links; expected at most one")
    if len(links) == 1:
        return links[0].from_socket

    raw_floats = tree.get("source_floats")
    if not isinstance(raw_floats, str):
        raise ValueError(f"{tree.name}: source float evidence is missing; cannot validate an unlinked Normal input")
    floats = json.loads(raw_floats)
    if not isinstance(floats, dict) or "_UseBumpMap" not in floats:
        raise ValueError(f"{tree.name}: source _UseBumpMap evidence is missing or malformed")
    use_bump_map = floats["_UseBumpMap"]
    if isinstance(use_bump_map, bool) or not isinstance(use_bump_map, (int, float)):
        raise TypeError(f"{tree.name}: source _UseBumpMap must be numeric, got {use_bump_map!r}")
    if "_NORMALMAP" in source_keywords(tree) or float(use_bump_map) == 1.0:
        raise ValueError(f"{tree.name}: source enables a normal map but the Principled Normal input is unlinked")
    return geometry_normal


def _same_surface_normal_source(left: bpy.types.NodeSocket, right: bpy.types.NodeSocket) -> bool:
    if left == right:
        return True
    return all(socket.node.type == "NEW_GEOMETRY" and socket.name == "Normal" for socket in (left, right))


def _source_texture(tree: bpy.types.NodeTree, slot: str) -> bpy.types.Node:
    matches = tuple(item for item in tree.nodes if item.type == "TEX_IMAGE" and item.get("source_slot") == slot)
    if len(matches) != 1 or matches[0].image is None or matches[0].image.packed_file is None:
        raise ValueError(f"{tree.name}: exact packed source texture required: {slot}, matches={len(matches)}")
    return matches[0]


def _threshold_output(sdf: bpy.types.Node) -> bpy.types.NodeSocket:
    """Expose the existing mirrored RG half-sum in the loaded kernel only."""
    kernel = sdf.node_tree
    if kernel is None:
        raise ValueError("Loaded source SDF instance has no kernel")
    threshold = by_label(kernel, "Source SDF threshold half").outputs[0]
    output = by_label(kernel, "Verified local SDF outputs")
    name = "SDF Threshold Sample"
    if sdf.outputs.get(name) is None:
        socket = kernel.interface.new_socket(name=name, in_out="OUTPUT", socket_type="NodeSocketFloat")
        socket.description = "Current mirrored source Lightmap (R+G)*0.5, before directional threshold remapping."
        kernel.links.new(threshold, output.inputs[name])
    elif len(output.inputs[name].links) != 1 or output.inputs[name].links[0].from_socket != threshold:
        raise ValueError(f"{kernel.name}: existing {name} output does not carry the verified source threshold")
    layout_shader_node_tree(kernel)
    return sdf.outputs[name]


def _camera_axis(tree: bpy.types.NodeTree, vector: tuple[float, float, float], label: str) -> bpy.types.NodeSocket:
    transform = node(tree, "ShaderNodeVectorTransform", label, SECTION)
    transform.vector_type = "VECTOR"
    transform.convert_from = "CAMERA"
    transform.convert_to = "WORLD"
    transform.inputs[0].default_value = vector
    return transform.outputs[0]


def _body_radial(tree: bpy.types.NodeTree, position: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Use live host instance origin as an explicit uncaptured per-object input."""
    origin = node(tree, "ShaderNodeVectorTransform", "Skin volume live host object origin", SECTION)
    origin.vector_type = "POINT"
    origin.convert_from = "OBJECT"
    origin.convert_to = "WORLD"
    origin.inputs[0].default_value = (0.0, 0.0, 0.0)
    radial = vector_math(tree, "SUBTRACT", position, origin.outputs[0], "Skin volume position minus host origin").outputs[0]
    components = node(tree, "ShaderNodeSeparateXYZ", "Skin volume radial host components", SECTION)
    tree.links.new(radial, components.inputs[0])
    flat = combine_vector(tree, (components.outputs["X"], components.outputs["Y"], 6.103515625e-5), "Skin volume preview horizontal radial")
    return vector_math(tree, "NORMALIZE", flat, (0.0, 0.0, 0.0), "Skin volume normalized preview radial").outputs[0]


def attach_skin_volume(tree: bpy.types.ShaderNodeTree, cp8: Vector4, cp9: Vector4, cp14: Vector4, cp15: Vector4) -> bpy.types.NodeSocket:
    """Attach one source Skin group and return its unconsumed additive color."""
    if tree.get("source_shader") != "HGRP/CharacterNPR_Skin":
        raise ValueError(f"{tree.name}: volume binding requires exact Skin source shader")
    if any(item.label == VOLUME_LABEL for item in tree.nodes):
        raise ValueError(f"{tree.name}: Skin volume is already attached")
    keywords = source_keywords(tree)
    face = "_SDFLIGHTMAP" in keywords
    bsdfs = tuple(item for item in tree.nodes if item.type == "BSDF_PRINCIPLED")
    if len(bsdfs) != 1:
        raise ValueError(f"{tree.name}: expected one source Principled surface, got {len(bsdfs)}")
    geometry = node(tree, "ShaderNodeNewGeometry", "Skin volume live surface geometry", SECTION)
    source_normal = _source_surface_normal(tree, bsdfs[0], geometry.outputs["Normal"])
    diffuse = by_label(tree, "Verified local HGRP diffuse equations")
    diffuse_color = _source_input(tree, diffuse, "Diffuse Color")
    state = by_label(tree, "Explicit HGRP state and preview inputs")
    if state.outputs.get("Preview Atlas Visibility") is None:
        raise ValueError(f"{tree.name}: explicit screen-shadow preview state is missing")
    base = _source_texture(tree, "_BaseMap")
    sdf = by_label(tree, "Native Face SDF / Explicit Adjustable Basis") if face else None
    mask = _source_texture(tree, "_SDFMask") if face else None
    if face:
        if sdf is None or mask is None:
            raise ValueError(f"{tree.name}: source SDF consumers are missing")
        if not _same_surface_normal_source(_source_input(tree, sdf, "Source Surface Normal"), source_normal):
            raise ValueError(f"{tree.name}: source SDF kernel Normal differs from the validated source surface Normal")
        face_right = _source_input(tree, sdf, "Face Right")
        face_forward = _source_input(tree, sdf, "Face Forward")
        mask_g = _source_input(tree, sdf, "SDF Mask G")
        radial = by_label(tree, "Source radial hemisphere normalized").outputs[0]
        normal = sdf.outputs["Blended Surface Normal"]
        kernel = build_face_skin_volume("EF Current Skin Face Volume", cp8, cp9, cp14, cp15)
    else:
        normal = source_normal
        kernel = build_body_skin_volume("EF Current Skin Body Volume", cp8, cp9, cp15)
    instance = node(tree, "ShaderNodeGroup", VOLUME_LABEL, SECTION)
    instance.node_tree = kernel
    if not face:
        radial = _body_radial(tree, geometry.outputs["Position"])
    for name, value in (
        ("Source Surface Normal", normal), ("View Direction", geometry.outputs["Incoming"]),
        ("Camera Forward", _camera_axis(tree, (0.0, 0.0, 1.0), "Skin volume live preview camera forward")),
        ("Camera Inverse View X", _camera_axis(tree, (1.0, 0.0, 0.0), "Skin volume live preview inverse-view X")),
        ("Camera Inverse View Y", _camera_axis(tree, (0.0, 1.0, 0.0), "Skin volume live preview inverse-view Y")),
        ("Radial Flat Direction", radial), ("Diffuse Color", diffuse_color),
        ("Base Alpha", base.outputs["Alpha"]), ("Screen Shadow Y", state.outputs["Preview Atlas Visibility"]),
    ):
        tree.links.new(value, instance.inputs[name])
    for name, axis in (("Source World X", (1.0, 0.0, 0.0)), ("Source World Y", (0.0, 0.0, 1.0)), ("Source World Z", (0.0, -1.0, 0.0))):
        tree.links.new(combine_vector(tree, axis, "Skin volume explicit preview " + name), instance.inputs[name])
    if face:
        tree.links.new(face_right, instance.inputs["Face Right"])
        tree.links.new(face_forward, instance.inputs["Face Forward"])
        tree.links.new(mask_g, instance.inputs["SDF Mask G"])
        tree.links.new(mask.outputs["Alpha"], instance.inputs["SDF Mask W"])
        tree.links.new(_threshold_output(sdf), instance.inputs["SDF Threshold Sample"])
    instance["binding_scope"] = "Explicit preview world/camera frame and atlas visibility; live source mapped/SDF normals and textures; not a captured native instance/global state."
    layout_shader_node_tree(kernel)
    layout_shader_node_tree(tree)
    return instance.outputs["Combined Skin Volume"]
