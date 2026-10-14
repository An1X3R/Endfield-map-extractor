"""Bind compiled Skin surface color operations to an existing source shader group."""
from __future__ import annotations

import bpy

from character_skin_surface import (
    build_body_mainlit_chroma,
    build_skin_rim_albedo,
    source_parameters,
)
from character_node_graph import math_node, node


SURFACE_SECTION = "Native Skin Surface Attachment"
ATTACHMENT_VERSION = "HGRP-Skin-Surface-Attachment/1"
RIM_SECTION = "Native Skin Surface Attachment"


def _one_node(tree: bpy.types.NodeTree, label: str) -> bpy.types.Node:
    matches = [item for item in tree.nodes if item.label == label]
    if len(matches) != 1:
        raise ValueError(f"{tree.name}: expected exactly one node labelled {label!r}, found {len(matches)}")
    return matches[0]


def _socket(node_value: bpy.types.Node, direction: str, name: str) -> bpy.types.NodeSocket:
    sockets = node_value.inputs if direction == "input" else node_value.outputs
    matches = [item for item in sockets if item.name == name]
    if len(matches) != 1:
        raise ValueError(f"{node_value.id_data.name}/{node_value.label}: expected one {direction} socket {name!r}, found {len(matches)}")
    return matches[0]


def _linked_from(
    tree: bpy.types.NodeTree,
    target: bpy.types.NodeSocket,
    expected_node: bpy.types.Node,
    expected_socket: bpy.types.NodeSocket,
) -> None:
    links = tuple(target.links)
    if len(links) != 1 or links[0].from_node != expected_node or links[0].from_socket != expected_socket:
        source_description = ", ".join(f"{link.from_node.label}/{link.from_socket.name}" for link in links) or "unlinked"
        raise ValueError(
            f"{tree.name}: expected {target.node.label}/{target.name} from "
            f"{expected_node.label}/{expected_socket.name}, found {source_description}"
        )


def _replace_link(
    tree: bpy.types.NodeTree,
    target: bpy.types.NodeSocket,
    expected_node: bpy.types.Node,
    expected_socket: bpy.types.NodeSocket,
    replacement: bpy.types.NodeSocket,
) -> None:
    _linked_from(tree, target, expected_node, expected_socket)
    tree.links.remove(target.links[0])
    tree.links.new(replacement, target)


def _face_rim_inputs(
    tree: bpy.types.NodeTree,
    mask: bpy.types.Node,
    camera_world: bpy.types.NodeSocket,
    face_right: bpy.types.NodeSocket,
    face_forward: bpy.types.NodeSocket,
) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket]:
    if mask.get("source_slot") != "_SDFMask":
        raise ValueError(f"{tree.name}: face rim source must have source_slot '_SDFMask'")
    image = mask.image
    if image is None:
        raise ValueError(f"{tree.name}: face rim mask node has no loaded image")
    if image.colorspace_settings.name != "Non-Color":
        raise ValueError(f"{tree.name}: face rim mask must use Non-Color, found {image.colorspace_settings.name}")
    vector_input = _socket(mask, "input", "Vector")
    if len(vector_input.links) != 1:
        raise ValueError(f"{tree.name}: face rim mask Vector must retain one source UV mapping")

    channel_node = node(tree, "ShaderNodeSeparateColor", "Native SDFMask RGBA channels", RIM_SECTION)
    channel_node.mode = "RGB"
    tree.links.new(_socket(mask, "output", "Color"), channel_node.inputs["Color"])

    camera_x = node(tree, "ShaderNodeVectorMath", "Native camera face-right projection", RIM_SECTION)
    camera_x.operation = "DOT_PRODUCT"
    tree.links.new(camera_world, camera_x.inputs[0])
    tree.links.new(face_right, camera_x.inputs[1])
    camera_z = node(tree, "ShaderNodeVectorMath", "Native camera face-forward projection", RIM_SECTION)
    camera_z.operation = "DOT_PRODUCT"
    tree.links.new(camera_world, camera_z.inputs[0])
    tree.links.new(face_forward, camera_z.inputs[1])
    x_squared = math_node(tree, "MULTIPLY", camera_x.outputs["Value"], camera_x.outputs["Value"], "Native camera face-right squared", RIM_SECTION)
    z_squared = math_node(tree, "MULTIPLY", camera_z.outputs["Value"], camera_z.outputs["Value"], "Native camera face-forward squared", RIM_SECTION)
    xz_squared = math_node(tree, "ADD", x_squared, z_squared, "Native camera face XZ length squared", RIM_SECTION)
    xz_safe = math_node(tree, "MAXIMUM", xz_squared, 1.1754943508222875e-38, "Native camera XZ normalization floor", RIM_SECTION)
    xz_length = math_node(tree, "SQRT", xz_safe, 0.0, "Native camera face XZ length", RIM_SECTION)
    camera_z_normalized = math_node(tree, "DIVIDE", camera_z.outputs["Value"], xz_length, "Native normalized camera face-forward", RIM_SECTION)
    half_bias = math_node(tree, "ADD", camera_z_normalized, 0.5, "Native camera gate half bias", RIM_SECTION)
    camera_gate = math_node(tree, "MINIMUM", 1.0, math_node(tree, "MAXIMUM", 0.0, half_bias, "Native camera gate lower clamp", RIM_SECTION), "Native camera gate upper clamp", RIM_SECTION)
    one_minus_camera_gate = math_node(tree, "SUBTRACT", 1.0, camera_gate, "Native camera gate complement", RIM_SECTION)
    mask_gate_delta = math_node(tree, "MULTIPLY", one_minus_camera_gate, channel_node.outputs["Green"], "Native SDFMask gate interpolation delta", RIM_SECTION)
    mask_gate = math_node(tree, "ADD", camera_gate, mask_gate_delta, "Native SDFMask camera gate", RIM_SECTION)
    upstream_gate = math_node(tree, "MULTIPLY", channel_node.outputs["Red"], mask_gate, "Native SDFMask rim gate", RIM_SECTION)
    return upstream_gate, channel_node.outputs["Blue"]


def _rim_inputs(tree: bpy.types.NodeTree) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket]:
    face_basis = [item for item in tree.nodes if item.label == "Native Face SDF / Explicit Adjustable Basis"]
    if face_basis:
        if len(face_basis) != 1:
            raise ValueError(f"{tree.name}: multiple Native Face SDF basis nodes")
        masks = [
            item
            for item in tree.nodes
            if item.type == "TEX_IMAGE" and item.get("source_slot") == "_SDFMask"
        ]
        if len(masks) != 1:
            raise ValueError(f"{tree.name}: expected one preserved _SDFMask image node, found {len(masks)}")
        camera = _socket(_one_node(tree, "SDF explicit Blender camera frame"), "output", "Vector")
        camera_link = _socket(face_basis[0], "input", "Camera Direction")
        _linked_from(tree, camera_link, camera.node, camera)
        face_right_node = _one_node(tree, "Face Right live anchor basis")
        face_forward_node = _one_node(tree, "Face Forward live anchor basis")
        face_right = _socket(face_right_node, "output", "Vector")
        face_forward = _socket(face_forward_node, "output", "Vector")
        _linked_from(tree, _socket(face_basis[0], "input", "Face Right"), face_right_node, face_right)
        _linked_from(tree, _socket(face_basis[0], "input", "Face Forward"), face_forward_node, face_forward)
        return _face_rim_inputs(tree, masks[0], camera, face_right, face_forward)
    one = node(tree, "ShaderNodeValue", "Native body rim gate", RIM_SECTION)
    one.outputs[0].default_value = 1.0
    region = node(tree, "ShaderNodeValue", "Native body rim region", RIM_SECTION)
    region.outputs[0].default_value = 1.0
    return one.outputs[0], region.outputs[0]


def _preflight_source_response(tree: bpy.types.NodeTree) -> tuple[bpy.types.Node, bpy.types.NodeSocket]:
    ggx = _one_node(tree, "Compiled Skin main-light GGX")
    normal = _socket(ggx, "input", "Normal")
    view = _socket(ggx, "input", "View")
    if len(normal.links) != 1 or len(view.links) != 1:
        raise ValueError(f"{tree.name}: compiled Skin GGX Normal and View must each have one current source")
    source_response = _one_node(tree, "Skin diffuse and source GGX")
    main_color = _socket(source_response, "output", "Color")
    emission = _one_node(tree, "Artist-lit NPR diffuse - shared engines")
    _linked_from(tree, _socket(emission, "input", "Color"), source_response, main_color)
    return source_response, main_color


def attach_skin_surface(tree: bpy.types.NodeTree) -> bpy.types.NodeSocket:
    """Build verified rim albedo and return final post-main luminance-dependent chroma.

    Only the current non-LOD body and face groups with the compiled Skin GGX branch are supported.
    The returned color is not connected to Emission or an additional-light path.
    """
    if tree.get("source_shader") != "HGRP/CharacterNPR_Skin":
        raise ValueError(f"{tree.name}: expected source_shader HGRP/CharacterNPR_Skin")
    if tree.get("skin_surface_attachment_version") == ATTACHMENT_VERSION:
        raise ValueError(f"{tree.name}: {ATTACHMENT_VERSION} is already attached")
    if any(item.get("endfield_layout_section") == SURFACE_SECTION for item in tree.nodes):
        raise ValueError(f"{tree.name}: unmarked Skin Surface Attachment nodes exist; refusing to double-attach")

    if not any(item.label == "Compiled Skin main-light GGX" for item in tree.nodes):
        raise ValueError(f"{tree.name}: current LOD group has no compiled Skin GGX branch; it remains unchanged")
    parameters = source_parameters(tree)
    source_diffuse = _one_node(tree, "Source diffuse albedo coefficient")
    principled = _one_node(tree, "CharacterNPR_Skin shared Eevee / Cycles surface")
    diffuse_albedo_input = _socket(source_diffuse, "input", "Color1")
    source_albedo_links = tuple(diffuse_albedo_input.links)
    if len(source_albedo_links) != 1:
        raise ValueError(
            f"{tree.name}: source diffuse albedo Color1 must have exactly one upstream albedo link, "
            f"found {len(source_albedo_links)}"
        )
    albedo_link = source_albedo_links[0]
    albedo_node = albedo_link.from_node
    albedo = albedo_link.from_socket
    _linked_from(tree, _socket(principled, "input", "Base Color"), albedo_node, albedo)
    specular_color = _one_node(tree, "Skin source metallic specular color")
    specular_albedo = _socket(specular_color, "input", "Color2")
    _linked_from(tree, specular_albedo, albedo_node, albedo)
    source_response, main_response = _preflight_source_response(tree)

    gate, region = _rim_inputs(tree)
    normal = _socket(_one_node(tree, "Compiled Skin main-light GGX"), "input", "Normal").links[0].from_socket
    view = _socket(_one_node(tree, "Compiled Skin main-light GGX"), "input", "View").links[0].from_socket
    dot = node(tree, "ShaderNodeVectorMath", "Skin rim normal dot view", SURFACE_SECTION)
    dot.operation = "DOT_PRODUCT"
    tree.links.new(normal, dot.inputs[0])
    tree.links.new(view, dot.inputs[1])

    rim_albedo = build_skin_rim_albedo(tree, parameters, albedo, dot.outputs["Value"], region, gate)
    _replace_link(tree, diffuse_albedo_input, albedo_node, albedo, rim_albedo)
    _replace_link(tree, _socket(principled, "input", "Base Color"), albedo_node, albedo, rim_albedo)
    _replace_link(tree, specular_albedo, albedo_node, albedo, rim_albedo)

    main_source_node_label = main_response.node.label
    main_source_socket_name = main_response.name
    post_main = build_body_mainlit_chroma(tree, main_response)
    post_main.node.label = "Native Skin post-main chroma result"
    tree["skin_surface_attachment_version"] = ATTACHMENT_VERSION
    if any(item.label == "Native Face SDF / Explicit Adjustable Basis" for item in tree.nodes):
        rim_scope = "Face rim gate uses current Non-Color SDFMask R/G and camera-forward frame; region blend uses SDFMask B. "
    else:
        rim_scope = "Body rim gate and region blend are fixed to 1 by the current body variant. "
    tree["skin_surface_attachment_scope"] = (
        rim_scope
        + "Rim albedo is wired to source diffuse, source metallic/specular color and Principled base color. "
        "Returned color is the post-main luminance-dependent chroma result; final output links are unchanged."
    )
    tree["skin_surface_rim_albedo_node"] = next(
        item.name for item in tree.nodes if item.label == "Native rim albedo"
    )
    tree["skin_surface_post_main_source"] = main_source_node_label
    tree["skin_surface_post_main_socket"] = main_source_socket_name
    tree["skin_surface_source_specular_rewired"] = True
    return post_main
