from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Callable

import bpy

from character_hgrp_diffuse import build_hgrp_diffuse
from character_npr_nodes import luminance, shadow_color
from character_node_graph import color_mix, input_socket, math_node as _math_node, node

TANGENT_SIGN_ATTRIBUTE = "EF_HGRP_EYE_TANGENT_SIGN"
EYE_SOURCE_SHADER = "HGRP/CharacterNPR_Eye"
SOURCE_COMPILED_EYE_VARIANTS = frozenset({(0, 0, 0), (0, 0, 1), (0, 1, 1), (1, 0, 1), (1, 1, 1)})


@dataclass(frozen=True)
class EyeMatcapParameters:
    parallax_scale: float
    normal_scale: float
    metallic: float
    alpha_premultiply: float
    avatar_customize_enabled: float
    matcap_color: tuple[float, float, float, float]
    highlight_color: tuple[float, float, float, float]
    scattering_color: tuple[float, float, float, float]
    eye_tint_color: tuple[float, float, float, float]
    use_matcap: float
    use_eye_highlight: float
    use_diff_ramp: float
    base_color_alpha: float


def source_parameters(tree: bpy.types.ShaderNodeTree) -> EyeMatcapParameters:
    """Read only the source eye properties required by the verified branches."""
    if tree.get("source_shader") != EYE_SOURCE_SHADER:
        raise ValueError(f"Expected source eye shader group: {tree.name}")
    floats = json.loads(tree["source_floats"])
    colors = json.loads(tree["source_colors"])
    required_floats = (
        "_ParallaxScale",
        "_MatcapNormalScale",
        "_UseMatcap",
        "_EyeHighLight",
        "_UseDiffRampMap",
        "_Metallic",
        "_AlphaPremultiply",
        "_AvatarCustomizeEnable",
    )
    required_colors = ("_MatcapColor", "_EyeHighLightColor", "_EyeScatteringColor", "_BaseColor", "_EyeTintColor")
    if any(name not in floats for name in required_floats) or any(name not in colors for name in required_colors):
        raise ValueError(f"Source eye material is missing required properties: {tree.name}")
    float_values = {name: float(floats[name]) for name in required_floats}
    color_values = {name: tuple(float(channel) for channel in colors[name]) for name in required_colors}
    if not all(math.isfinite(value) for value in float_values.values()) or any(len(value) != 4 or not all(math.isfinite(channel) for channel in value) for value in color_values.values()):
        raise ValueError(f"Source eye parameters contain invalid numeric values: {tree.name}")
    if any(float_values[name] not in (0.0, 1.0) for name in ("_UseMatcap", "_EyeHighLight", "_UseDiffRampMap")):
        raise ValueError(f"Source eye feature switches are not binary: {tree.name}")
    if float_values["_AvatarCustomizeEnable"] not in (0.0, 1.0) or float_values["_AlphaPremultiply"] not in (0.0, 1.0):
        raise ValueError(f"Source eye feature switches are not binary: {tree.name}")
    return EyeMatcapParameters(
        float_values["_ParallaxScale"],
        float_values["_MatcapNormalScale"],
        float_values["_Metallic"],
        float_values["_AlphaPremultiply"],
        float_values["_AvatarCustomizeEnable"],
        color_values["_MatcapColor"],
        color_values["_EyeHighLightColor"],
        color_values["_EyeScatteringColor"],
        color_values["_EyeTintColor"],
        float_values["_UseMatcap"],
        float_values["_EyeHighLight"],
        float_values["_UseDiffRampMap"],
        color_values["_BaseColor"][3],
    )


def _eye_variant_key(parameters: EyeMatcapParameters) -> tuple[int, int, int]:
    return (
        int(parameters.use_matcap),
        int(parameters.use_eye_highlight),
        int(parameters.use_diff_ramp),
    )


def _input_socket(
    tree: bpy.types.ShaderNodeTree,
    name: str,
    kind: str,
    value: float | tuple[float, float, float, float],
    description: str,
) -> None:
    matches = [item for item in tree.interface.items_tree if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == name]
    if matches:
        if len(matches) != 1 or matches[0].socket_type != kind:
            raise ValueError(f"Eye group input is ambiguous or has the wrong type: {tree.name}, {name}")
        return
    input_socket(tree, name, kind, value, description)
    owners: list[bpy.types.NodeTree] = list(bpy.data.node_groups)
    owners.extend(material.node_tree for material in bpy.data.materials if material.use_nodes and material.node_tree)
    seen: set[int] = set()
    for owner in owners:
        pointer = owner.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        for instance in owner.nodes:
            if instance.type != "GROUP" or instance.node_tree != tree:
                continue
            socket = instance.inputs.get(name)
            if socket is None:
                raise ValueError(f"New Eye group input did not propagate to its existing instance: tree={tree.name}, input={name}, owner={owner.name}")
            if socket.is_linked:
                raise ValueError(f"New Eye group input is unexpectedly linked: tree={tree.name}, input={name}, owner={owner.name}")
            socket.default_value = value


def _exact_node(tree: bpy.types.ShaderNodeTree, node_type: str, label: str) -> bpy.types.Node:
    matches = [item for item in tree.nodes if item.type == node_type and item.label == label]
    if len(matches) != 1:
        raise ValueError(f"Expected one exact eye graph node: tree={tree.name}, type={node_type}, label={label}, matches={len(matches)}")
    return matches[0]


def _texture_and_mapping(tree: bpy.types.ShaderNodeTree, prefix: str) -> tuple[bpy.types.Node, bpy.types.Node, bpy.types.Node]:
    textures = [item for item in tree.nodes if item.type == "TEX_IMAGE" and item.label.startswith(prefix)]
    if len(textures) != 1 or textures[0].image is None or not textures[0].image.packed_file:
        raise ValueError(f"Source eye texture is absent, unpacked, or ambiguous: tree={tree.name}, slot={prefix}")
    texture = textures[0]
    mapping_links = texture.inputs["Vector"].links
    if len(mapping_links) != 1 or mapping_links[0].from_node.type != "MAPPING":
        raise ValueError(f"Source eye texture mapping is missing: tree={tree.name}, slot={prefix}")
    mapping = mapping_links[0].from_node
    uv_links = mapping.inputs["Vector"].links
    if len(uv_links) != 1 or uv_links[0].from_node.type != "UVMAP":
        raise ValueError(f"Source eye UV input is missing: tree={tree.name}, slot={prefix}")
    uv_node = uv_links[0].from_node
    if uv_node.uv_map != "UV0":
        raise ValueError(f"Source eye material does not use audited UV0: tree={tree.name}, slot={prefix}, uv={uv_node.uv_map}")
    return texture, mapping, uv_node


def _math(tree: bpy.types.NodeTree, operation: str, left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
    return _math_node(tree, operation, left, right, label, "Source Eye Matcap")

def _vector_node(
    tree: bpy.types.ShaderNodeTree,
    operation: str,
    first: bpy.types.NodeSocket,
    second: bpy.types.NodeSocket | None,
    label: str,
) -> bpy.types.Node:
    result = node(tree, "ShaderNodeVectorMath", label, "Source Eye Matcap")
    result.operation = operation
    tree.links.new(first, result.inputs[0])
    if second is not None:
        tree.links.new(second, result.inputs[1])
    return result


def _scale_vector(tree: bpy.types.ShaderNodeTree, vector: bpy.types.NodeSocket, scale: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeVectorMath", label, "Source Eye Matcap")
    result.operation = "SCALE"
    tree.links.new(vector, result.inputs[0])
    if isinstance(scale, bpy.types.NodeSocket):
        tree.links.new(scale, result.inputs["Scale"])
    else:
        result.inputs["Scale"].default_value = scale
    return result.outputs["Vector"]


def _sum_vectors(tree: bpy.types.ShaderNodeTree, first: bpy.types.NodeSocket, second: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return _vector_node(tree, "ADD", first, second, label).outputs["Vector"]


def _clamp(tree: bpy.types.ShaderNodeTree, value: bpy.types.NodeSocket, low: float, high: float, label: str) -> bpy.types.NodeSocket:
    return _math(tree, "MINIMUM", _math(tree, "MAXIMUM", value, low, label + " lower"), high, label + " upper")


def _smoothstep(tree: bpy.types.ShaderNodeTree, value: bpy.types.NodeSocket, edge0: float, edge1: float, label: str) -> bpy.types.NodeSocket:
    normalized = _clamp(tree, _math(tree, "DIVIDE", _math(tree, "SUBTRACT", value, edge0, label + " edge offset"), edge1 - edge0, label + " normalize"), 0.0, 1.0, label + " clamp")
    cubic = _math(tree, "SUBTRACT", 3.0, _math(tree, "MULTIPLY", 2.0, normalized, label + " double"), label + " cubic inner")
    return _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", normalized, normalized, label + " square"), cubic, label)


def _combine_color(tree: bpy.types.ShaderNodeTree, value: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeCombineColor", label, "Source Eye Matcap")
    for channel in ("Red", "Green", "Blue"):
        tree.links.new(value, result.inputs[channel])
    return result.outputs["Color"]


def _lerp(tree: bpy.types.ShaderNodeTree, left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float, factor: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    delta = _math(tree, "SUBTRACT", right, left, label + " delta")
    scaled = _math(tree, "MULTIPLY", factor, delta, label + " scaled delta")
    return _math(tree, "ADD", left, scaled, label)


def _build_source_eye_diffuse(
    tree: bpy.types.ShaderNodeTree,
    source_input: bpy.types.Node,
    shared_state: bpy.types.Node,
    diffuse_group: bpy.types.ShaderNodeTree,
    artist_state: bpy.types.Node,
    light_normal: bpy.types.NodeSocket,
    iris_mask: bpy.types.NodeSocket,
    base_albedo: bpy.types.NodeSocket,
    eye_blend: bpy.types.NodeSocket,
    eye_base_alpha: bpy.types.NodeSocket,
    ramp_texture: bpy.types.Node | None,
    view_ramp_alpha: bpy.types.NodeSocket | None,
    camera_forward: bpy.types.NodeSocket,
    eye_shadow: bpy.types.NodeSocket,
    parameters: EyeMatcapParameters,
) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    """Build the source Eye ComputeNPRDiffuse call and its explicit dark branch."""
    white = (1.0, 1.0, 1.0, 1.0)
    eye_tint = color_mix(
        tree,
        "MIX",
        iris_mask,
        source_input.outputs["Eye Tint Color"],
        white,
        "Source EyeTintColor applies inside the iris only",
        "Source Eye Customization",
    )
    enabled_tint = color_mix(
        tree,
        "MIX",
        source_input.outputs["Avatar Customization Enabled"],
        white,
        eye_tint,
        "Source AvatarCustomizeEnable gate",
        "Source Eye Customization",
    )
    eye_albedo = color_mix(tree, "MULTIPLY", 1.0, base_albedo, enabled_tint, "Source Eye albedo with explicit tint state", "Source Eye Customization")
    eye_diffuse_fraction = _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", 1.0, parameters.metallic, "Source Eye one minus metallic"), 0.96, "Source Eye dielectric diffuse coefficient")
    diffuse_color = color_mix(tree, "MULTIPLY", 1.0, eye_albedo, _combine_color(tree, eye_diffuse_fraction, "Source Eye diffuse fraction color"), "Source Eye diffuse color", "Source Eye ComputeNPRDiffuse")
    highlighted_diffuse = color_mix(tree, "MULTIPLY", 1.0, diffuse_color, eye_blend, "Source Eye diffuse times EyeBlend", "Source Eye ComputeNPRDiffuse")

    source_floats = json.loads(tree["source_floats"])
    source_samples = {
        str(item["source_slot"]): item
        for item in tree.nodes
        if item.type == "TEX_IMAGE" and "source_slot" in item
    }
    source_shadow = shadow_color(tree, eye_albedo, source_floats, source_samples, artist_state)
    cp0_z = shared_state.outputs.get("CP0 Z Ambient Floor")
    if cp0_z is None:
        raise ValueError(f"Shared HGRP state has no CP0.z output: {shared_state.name}")
    scaled_shadow = color_mix(
        tree,
        "MULTIPLY",
        1.0,
        source_shadow,
        _combine_color(tree, _math(tree, "MULTIPLY", eye_diffuse_fraction, cp0_z, "Source Eye shadow coefficient times CP0.z"), "Source Eye scaled shadow color"),
        "Source Eye scaled shadow albedo",
        "Source Eye ComputeNPRDiffuse",
    )

    world_to_object = node(tree, "ShaderNodeVectorTransform", "Eye adjusted light world to object", "Source Eye Diffuse Ramp")
    world_to_object.vector_type = "VECTOR"
    world_to_object.convert_from = "WORLD"
    world_to_object.convert_to = "OBJECT"
    tree.links.new(artist_state.outputs["Direction"], world_to_object.inputs["Vector"])
    local_light = _vector_node(tree, "NORMALIZE", world_to_object.outputs["Vector"], None, "Normalize Eye object-space adjusted light").outputs["Vector"]
    local_axes = node(tree, "ShaderNodeSeparateXYZ", "Eye local light components", "Source Eye Diffuse Ramp")
    tree.links.new(local_light, local_axes.inputs["Vector"])
    projected_local = node(tree, "ShaderNodeCombineXYZ", "Eye local light with zero Y", "Source Eye Diffuse Ramp")
    tree.links.new(local_axes.outputs["X"], projected_local.inputs["X"])
    tree.links.new(local_axes.outputs["Z"], projected_local.inputs["Z"])
    projected_local.inputs["Y"].default_value = 0.0
    projected_local_normal = _vector_node(tree, "NORMALIZE", projected_local.outputs["Vector"], None, "Normalize Eye local XZ light").outputs["Vector"]
    object_to_world = node(tree, "ShaderNodeVectorTransform", "Eye projected light object to world", "Source Eye Diffuse Ramp")
    object_to_world.vector_type = "VECTOR"
    object_to_world.convert_from = "OBJECT"
    object_to_world.convert_to = "WORLD"
    tree.links.new(projected_local_normal, object_to_world.inputs["Vector"])
    projected_light = _vector_node(tree, "NORMALIZE", object_to_world.outputs["Vector"], None, "Normalize Eye projected world light").outputs["Vector"]
    ramp_dot = node(tree, "ShaderNodeVectorMath", "Source Eye ramp normal dot projected light", "Source Eye Diffuse Ramp")
    ramp_dot.operation = "DOT_PRODUCT"
    tree.links.new(light_normal, ramp_dot.inputs[0])
    tree.links.new(projected_light, ramp_dot.inputs[1])
    cp_ramp_bias = _math(
        tree,
        "MULTIPLY",
        source_input.outputs["Eye Ramp CP11 W"],
        source_input.outputs["Eye Ramp CP12 X"],
        "Source Eye CP11.w times CP12.x ramp bias",
    )
    ramp_signed = _clamp(tree, _math(tree, "ADD", cp_ramp_bias, ramp_dot.outputs["Value"], "Source Eye ramp signed input"), -1.0, 1.0, "Clamp source Eye ramp input")
    if parameters.use_diff_ramp == 1.0:
        if ramp_texture is None or view_ramp_alpha is None:
            raise ValueError(f"Enabled Eye diffuse ramp has no packed source texture: {tree.name}")
        ramp_u = _math(tree, "ADD", _math(tree, "MULTIPLY", ramp_signed, 0.5, "Source Eye ramp half scale"), 0.5, "Source Eye ramp U")
        ramp_uv = node(tree, "ShaderNodeCombineXYZ", "Source Eye diffuse-ramp UV", "Source Eye Diffuse Ramp")
        tree.links.new(ramp_u, ramp_uv.inputs["X"])
        ramp_uv.inputs["Y"].default_value = 0.5
        ramp_uv.inputs["Z"].default_value = 0.0
        for link in tuple(ramp_texture.inputs["Vector"].links):
            tree.links.remove(link)
        tree.links.new(ramp_uv.outputs["Vector"], ramp_texture.inputs["Vector"])
        ramp_color = ramp_texture.outputs["Color"]
        ramp_alpha = ramp_texture.outputs["Alpha"]
        selected_view_ramp_alpha = view_ramp_alpha
    else:
        if ramp_texture is not None or view_ramp_alpha is not None:
            raise ValueError(f"Disabled Eye diffuse ramp unexpectedly has a source texture binding: {tree.name}")
        procedural_ramp = _smoothstep(tree, ramp_signed, 0.25, 1.0, "Ruri reference procedural Eye diffuse ramp")
        ramp_color = _combine_color(tree, procedural_ramp, "Ruri reference procedural Eye ramp color")
        ramp_alpha = procedural_ramp
        camera_dot = node(tree, "ShaderNodeVectorMath", "Ruri reference Eye normal dot camera forward", "Source Eye Diffuse Ramp")
        camera_dot.operation = "DOT_PRODUCT"
        tree.links.new(light_normal, camera_dot.inputs[0])
        tree.links.new(camera_forward, camera_dot.inputs[1])
        selected_view_ramp_alpha = _smoothstep(tree, camera_dot.outputs["Value"], 0.25, 1.0, "Ruri reference procedural Eye view ramp alpha")
    min_ramp_alpha = _math(tree, "MINIMUM", ramp_alpha, 1.0, "Source minimum ramp alpha")

    half_min = 6.103515625e-5
    normal_components = node(tree, "ShaderNodeSeparateXYZ", "Eye flat-light normal components", "Source Eye ComputeNPRDiffuse")
    tree.links.new(light_normal, normal_components.inputs["Vector"])
    flat_normal = node(tree, "ShaderNodeCombineXYZ", "Source flat Eye light normal", "Source Eye ComputeNPRDiffuse")
    tree.links.new(normal_components.outputs["X"], flat_normal.inputs["X"])
    tree.links.new(normal_components.outputs["Z"], flat_normal.inputs["Z"])
    flat_normal.inputs["Y"].default_value = half_min
    flat_light_normal = _vector_node(tree, "NORMALIZE", flat_normal.outputs["Vector"], None, "Normalize source flat Eye light normal").outputs["Vector"]

    native = node(tree, "ShaderNodeGroup", "Verified Eye ComputeNPRDiffuse", "Source Eye ComputeNPRDiffuse")
    native.node_tree = diffuse_group
    linked_state_names = (
        "Ambient Color",
        "CP6 Ambient Direction",
        "Ambient Intensity",
        "CP0 Y Lit Scale",
        "CP0 Z Ambient Floor",
        "CP0 W Dark Scale",
        "CP1 X Dark Brightness Mix",
        "CP1 Y Shadow Strength",
        "CP7 X Bias",
        "CP7 Y Scale",
        "CP7 Z Offset",
        "CP12 Y Light Color Mix",
    )
    for name in linked_state_names:
        state_socket = shared_state.outputs.get(name)
        input_socket_value = native.inputs.get(name)
        if state_socket is None or input_socket_value is None:
            raise ValueError(f"Eye ComputeNPRDiffuse state binding is missing: group={shared_state.name}, socket={name}")
        tree.links.new(state_socket, input_socket_value)
    light_strength_color = _combine_color(tree, shared_state.outputs["Light Intensity"], "Host Sun strength as source light color scale")
    host_light_radiance = color_mix(
        tree,
        "MULTIPLY",
        1.0,
        shared_state.outputs["Light Color"],
        light_strength_color,
        "Host Sun RGB times strength for source blendedLightCol",
        "Source Eye ComputeNPRDiffuse",
    )
    tree.links.new(host_light_radiance, native.inputs["Light Color"])
    native.inputs["Light Intensity"].default_value = 1.0
    native.inputs["Cast Shadow"].default_value = 1.0
    tree.links.new(highlighted_diffuse, native.inputs["Diffuse Color"])
    tree.links.new(ramp_color, native.inputs["Ramp Color"])
    tree.links.new(flat_light_normal, native.inputs["Hemisphere Normal"])
    tree.links.new(selected_view_ramp_alpha, native.inputs["Shadow A"])
    tree.links.new(min_ramp_alpha, native.inputs["Shadow B"])
    tree.links.new(ramp_alpha, native.inputs["Ramp Alpha"])
    tree.links.new(scaled_shadow, native.inputs["Scaled Shadow Albedo"])

    cp6_dot = node(tree, "ShaderNodeVectorMath", "Eye dark branch normal dot CP6", "Source Eye ComputeNPRDiffuse")
    cp6_dot.operation = "DOT_PRODUCT"
    tree.links.new(flat_light_normal, cp6_dot.inputs[0])
    tree.links.new(shared_state.outputs["CP6 Ambient Direction"], cp6_dot.inputs[1])
    cp7_biased = _math(tree, "ADD", cp6_dot.outputs["Value"], shared_state.outputs["CP7 X Bias"], "Eye dark branch CP7 bias")
    cp7_saturated = _clamp(tree, cp7_biased, 0.0, 1.0, "Eye dark branch hemisphere saturate")
    hemisphere = _math(tree, "ADD", _math(tree, "MULTIPLY", cp7_saturated, shared_state.outputs["CP7 Y Scale"], "Eye dark branch CP7 scale"), shared_state.outputs["CP7 Z Offset"], "Eye dark branch CP7 offset")
    shadow_strength = _math(tree, "MULTIPLY", min_ramp_alpha, shared_state.outputs["CP1 Y Shadow Strength"], "Eye dark branch shadow strength")
    shadow_ambient_color = color_mix(tree, "MIX", shadow_strength, shared_state.outputs["Ambient Color"], white, "Eye dark branch shadow ambient", "Source Eye ComputeNPRDiffuse")
    shadow_ambient = color_mix(tree, "MULTIPLY", 1.0, shadow_ambient_color, _combine_color(tree, hemisphere, "Eye dark branch hemisphere color"), "Eye dark branch hemisphere ambient", "Source Eye ComputeNPRDiffuse")
    ambient_intensity = shared_state.outputs["Ambient Intensity"]
    dark_base = _math(tree, "MINIMUM", _math(tree, "ADD", 0.65, _math(tree, "MULTIPLY", 0.35, ambient_intensity, "Eye dark brightness ambient term"), "Eye dark brightness base"), 1.5, "Eye dark brightness base cap")
    dark_alternate = _clamp(tree, ambient_intensity, 1.25, 1.75, "Eye dark brightness alternate")
    dark_brightness = _lerp(tree, dark_base, dark_alternate, shared_state.outputs["CP1 X Dark Brightness Mix"], "Eye dark branch brightness")
    full_dark = color_mix(tree, "MULTIPLY", 1.0, shadow_ambient, _combine_color(tree, _math(tree, "MULTIPLY", dark_brightness, shared_state.outputs["CP0 W Dark Scale"], "Eye dark branch CP0.w scale"), "Eye dark branch scalar color"), "Source Eye full diffuse dark branch", "Source Eye ComputeNPRDiffuse")
    full_diffuse = color_mix(tree, "MIX", eye_shadow, full_dark, native.outputs["Full Diffuse"], "Source Eye shadow selects full diffuse", "Source Eye ComputeNPRDiffuse")

    diffuse_luminance = luminance(tree, diffuse_color, "Source Eye dark diffuse luminance")
    luminance_color = _combine_color(tree, diffuse_luminance, "Source Eye diffuse luminance color")
    saturated_diffuse = color_mix(
        tree,
        "ADD",
        1.0,
        color_mix(tree, "MULTIPLY", 1.0, color_mix(tree, "SUBTRACT", 1.0, diffuse_color, luminance_color, "Source Eye diffuse chroma", "Source Eye ComputeNPRDiffuse"), (1.2, 1.2, 1.2, 1.0), "Source Eye dark no-highlight saturation", "Source Eye ComputeNPRDiffuse"),
        luminance_color,
        "Source Eye dark no-highlight diffuse",
        "Source Eye ComputeNPRDiffuse",
    )
    dark_endpoint = highlighted_diffuse if parameters.use_eye_highlight == 1.0 else saturated_diffuse
    npr_dark = color_mix(tree, "MIX", view_ramp_alpha, scaled_shadow, dark_endpoint, "Source Eye NPR dark branch", "Source Eye ComputeNPRDiffuse")
    npr_diffuse = color_mix(tree, "MIX", eye_shadow, npr_dark, native.outputs["NPR Color"], "Source Eye shadow selects NPR diffuse", "Source Eye ComputeNPRDiffuse")
    alpha_premultiply = _math(
        tree,
        "ADD",
        1.0 - parameters.alpha_premultiply,
        _math(tree, "MULTIPLY", eye_base_alpha, parameters.alpha_premultiply, "Source Eye premultiplied alpha term"),
        "Source Eye alpha-premultiply switch",
    )
    return eye_albedo, full_diffuse, npr_diffuse, _combine_color(tree, alpha_premultiply, "Source Eye premultiplied alpha color"), selected_view_ramp_alpha, min_ramp_alpha


def _build_matcap_geometry(
    tree: bpy.types.ShaderNodeTree,
    geometry: bpy.types.Node,
    uv_node: bpy.types.Node,
    uv_split: bpy.types.Node,
    frac_uv: tuple[bpy.types.NodeSocket, bpy.types.NodeSocket],
    dist_sq: bpy.types.NodeSocket,
    base_mapping: bpy.types.Node,
    matcap_mapping: bpy.types.Node,
    source_input: bpy.types.Node,
    parameters: EyeMatcapParameters,
) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.Node, bpy.types.Node]:
    tangent = node(tree, "ShaderNodeTangent", "Source UV0 tangent", "Source Eye Matcap")
    tangent.direction_type = "UV_MAP"
    tangent.uv_map = uv_node.uv_map
    normal = _vector_node(tree, "NORMALIZE", geometry.outputs["Normal"], None, "Normalize source eye normal").outputs["Vector"]
    tangent_normalized = _vector_node(tree, "NORMALIZE", tangent.outputs["Tangent"], None, "Normalize source UV0 tangent").outputs["Vector"]
    tangent_sign = node(tree, "ShaderNodeAttribute", "Source UV tangent handedness", "Source Eye Matcap")
    tangent_sign.attribute_type = "GEOMETRY"
    tangent_sign.attribute_name = TANGENT_SIGN_ATTRIBUTE
    raw_bitangent = _vector_node(tree, "CROSS_PRODUCT", normal, tangent_normalized, "Source normal cross tangent")
    bitangent = _scale_vector(tree, raw_bitangent.outputs["Vector"], tangent_sign.outputs["Fac"], "Apply source UV tangent sign")
    incoming = node(tree, "ShaderNodeNewGeometry", "Surface-to-camera view ray", "Source Eye Matcap")
    view = _vector_node(tree, "NORMALIZE", incoming.outputs["Incoming"], None, "Normalize positive surface-to-camera view ray").outputs["Vector"]
    tv = node(tree, "ShaderNodeVectorMath", "Source tangent dot view", "Source Eye Matcap")
    tv.operation = "DOT_PRODUCT"
    tree.links.new(tangent_normalized, tv.inputs[0])
    tree.links.new(view, tv.inputs[1])
    bv = node(tree, "ShaderNodeVectorMath", "Source bitangent dot view", "Source Eye Matcap")
    bv.operation = "DOT_PRODUCT"
    tree.links.new(bitangent, bv.inputs[0])
    tree.links.new(view, bv.inputs[1])
    nv = node(tree, "ShaderNodeVectorMath", "Source normal dot view", "Source Eye Matcap")
    nv.operation = "DOT_PRODUCT"
    tree.links.new(normal, nv.inputs[0])
    tree.links.new(view, nv.inputs[1])
    basis_sq = _math(tree, "ADD", _math(tree, "ADD", _math(tree, "MULTIPLY", tv.outputs["Value"], tv.outputs["Value"], "Eye tangent-view squared"), _math(tree, "MULTIPLY", bv.outputs["Value"], bv.outputs["Value"], "Eye bitangent-view squared"), "Eye tangent basis length xy"), _math(tree, "MULTIPLY", nv.outputs["Value"], nv.outputs["Value"], "Eye normal-view squared"), "Eye tangent basis length squared")
    basis_len = _math(tree, "SQRT", basis_sq, 0.0, "Eye tangent basis length")
    basis_inverse = _math(tree, "DIVIDE", 1.0, _math(tree, "MAXIMUM", basis_len, 1.0e-19, "Eye tangent basis floor"), "Eye tangent basis inverse")
    parallax_raw = _clamp(tree, _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", dist_sq, 0.25, "Source iris edge offset"), -5.0, "Source parallax edge scale"), 0.0, 1.0, "Source parallax raw")
    parallax_smooth = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", parallax_raw, parallax_raw, "Source parallax square"), _math(tree, "SUBTRACT", 3.0, _math(tree, "MULTIPLY", 2.0, parallax_raw, "Source parallax double"), "Source parallax cubic inner"), "Source parallax smoothstep")
    parallax_enabled = source_input.outputs["Matcap Enabled"]
    offset_x = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", tv.outputs["Value"], basis_inverse, "Normalized tangent-view"), source_input.outputs["Eye Parallax Scale"], "Source eye parallax scale X"), _math(tree, "MULTIPLY", parallax_smooth, parallax_enabled, "Eye parallax enable"), "Eye parallax X offset")
    offset_y = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", bv.outputs["Value"], basis_inverse, "Normalized bitangent-view"), source_input.outputs["Eye Parallax Scale"], "Source eye parallax scale Y"), 0.25, "Source eye parallax quarter scale"), _math(tree, "MULTIPLY", parallax_smooth, parallax_enabled, "Eye parallax enable Y"), "Eye parallax Y offset")
    parallax_uv = node(tree, "ShaderNodeCombineXYZ", "Source parallax-adjusted BaseMap UV", "Source Eye Matcap")
    tree.links.new(_math(tree, "SUBTRACT", uv_split.outputs["X"], offset_x, "Source parallax UV X"), parallax_uv.inputs["X"])
    tree.links.new(_math(tree, "SUBTRACT", uv_split.outputs["Y"], offset_y, "Source parallax UV Y"), parallax_uv.inputs["Y"])
    parallax_uv.inputs["Z"].default_value = 0.0
    for link in tuple(base_mapping.inputs["Vector"].links):
        tree.links.remove(link)
    tree.links.new(parallax_uv.outputs[0], base_mapping.inputs["Vector"])

    mat_x = _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", frac_uv[0], 2.0, "Source Matcap UV X scale"), 1.0, "Source Matcap normal X")
    mat_y = _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", frac_uv[1], 2.0, "Source Matcap UV Y scale"), 1.0, "Source Matcap normal Y")
    radial_sq = _math(tree, "ADD", _math(tree, "MULTIPLY", mat_x, mat_x, "Source Matcap normal X squared"), _math(tree, "MULTIPLY", mat_y, mat_y, "Source Matcap normal Y squared"), "Source Matcap normal radial square")
    mat_z = _math(tree, "MAXIMUM", _math(tree, "SQRT", _clamp(tree, _math(tree, "SUBTRACT", 1.0, radial_sq, "Source Matcap normal remainder"), 0.0, 1.0, "Source Matcap normal saturate"), 0.0, "Source Matcap normal square root"), 1.0e-16, "Source Matcap normal minimum Z")
    scaled_x = _math(tree, "MULTIPLY", mat_x, -parameters.normal_scale, "Source Matcap normal scale X")
    scaled_y = _math(tree, "MULTIPLY", mat_y, -parameters.normal_scale, "Source Matcap normal scale Y")
    iris_mask = _math(tree, "SUBTRACT", 1.0, _math(tree, "LESS_THAN", dist_sq, 0.25, "Source iris mask"), "Source eye-white mask")
    mask_factor = _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", iris_mask, 1.0, "Source iris normal mask"), 0.125, "Source iris normal quarter-octave")
    light_x = _math(tree, "MULTIPLY", scaled_x, mask_factor, "Source ramp normal tangent X")
    light_y = _math(tree, "MULTIPLY", scaled_y, mask_factor, "Source ramp normal bitangent Y")
    normal_z_ramp = _lerp(tree, mat_z, 1.0, iris_mask, "Source ramp normal Z")
    light_n_raw = _sum_vectors(tree, _sum_vectors(tree, _scale_vector(tree, tangent_normalized, light_x, "Source ramp tangent normal X"), _scale_vector(tree, bitangent, light_y, "Source ramp bitangent normal Y"), "Source ramp tangent frame xy"), _scale_vector(tree, normal, normal_z_ramp, "Source ramp normal Z"), "Source ramp normal")
    light_n_perturbed = _vector_node(tree, "NORMALIZE", light_n_raw, None, "Normalize source ramp normal").outputs["Vector"]
    light_n_blended = _sum_vectors(tree, _scale_vector(tree, normal, _math(tree, "SUBTRACT", 1.0, parallax_enabled, "Source matcap-off normal"), "Matcap-off source normal"), _scale_vector(tree, light_n_perturbed, parallax_enabled, "Matcap-on source normal"), "Matcap enable normal blend")
    light_n = _vector_node(tree, "NORMALIZE", light_n_blended, None, "Normalize selected eye normal").outputs["Vector"]

    matcap_full_raw = _sum_vectors(tree, _sum_vectors(tree, _scale_vector(tree, tangent_normalized, scaled_x, "Source Matcap tangent normal X"), _scale_vector(tree, bitangent, scaled_y, "Source Matcap bitangent normal Y"), "Source Matcap tangent frame xy"), _scale_vector(tree, normal, mat_z, "Source Matcap normal Z"), "Source Matcap full normal")
    matcap_full = _vector_node(tree, "NORMALIZE", matcap_full_raw, None, "Normalize source Matcap normal").outputs["Vector"]
    view_transform = node(tree, "ShaderNodeVectorTransform", "Unity world normal into camera view", "Source Eye Matcap")
    view_transform.vector_type = "NORMAL"
    view_transform.convert_from = "WORLD"
    view_transform.convert_to = "CAMERA"
    tree.links.new(matcap_full, view_transform.inputs["Vector"])
    camera_normal = _vector_node(tree, "NORMALIZE", view_transform.outputs["Vector"], None, "Normalize Matcap camera normal").outputs["Vector"]
    camera_xyz = node(tree, "ShaderNodeSeparateXYZ", "Matcap view normal components", "Source Eye Matcap")
    tree.links.new(camera_normal, camera_xyz.inputs[0])
    matcap_uv = node(tree, "ShaderNodeCombineXYZ", "Source view-space Matcap UV", "Source Eye Matcap")
    tree.links.new(_math(tree, "ADD", _math(tree, "MULTIPLY", camera_xyz.outputs["X"], 0.5, "Matcap view X half"), 0.5, "Matcap view U"), matcap_uv.inputs["X"])
    tree.links.new(_math(tree, "ADD", _math(tree, "MULTIPLY", camera_xyz.outputs["Y"], 0.5, "Matcap view Y half"), 0.5, "Matcap view V"), matcap_uv.inputs["Y"])
    matcap_uv.inputs["Z"].default_value = 0.0
    for link in tuple(matcap_mapping.inputs["Vector"].links):
        tree.links.remove(link)
    tree.links.new(matcap_uv.outputs[0], matcap_mapping.inputs["Vector"])
    return light_n, matcap_full, parallax_uv, matcap_uv


def build_eye_matcap_branch(
    tree: bpy.types.ShaderNodeTree,
    layout: Callable[[bpy.types.NodeTree], None],
    shared_state_group: bpy.types.ShaderNodeTree,
    diffuse_group: bpy.types.ShaderNodeTree,
) -> dict[str, str]:
    """Connect source UV parallax, view-space Matcap sampling, and eye highlight terms."""
    parameters = source_parameters(tree)
    if tree.get("eye_lighting_scope"):
        raise ValueError(f"Eye lighting patch already exists: {tree.name}")
    shared_state_outputs = {
        item.name: item.socket_type
        for item in shared_state_group.interface.items_tree
        if item.item_type == "SOCKET" and item.in_out == "OUTPUT"
    }
    required_state_outputs = {
        "Ambient Color": "NodeSocketColor",
        "Light Color": "NodeSocketColor",
        "CP6 Ambient Direction": "NodeSocketVector",
        "Ambient Intensity": "NodeSocketFloat",
        "Light Intensity": "NodeSocketFloat",
        "CP0 Z Ambient Floor": "NodeSocketFloat",
        "CP0 Y Lit Scale": "NodeSocketFloat",
        "CP0 W Dark Scale": "NodeSocketFloat",
        "CP1 X Dark Brightness Mix": "NodeSocketFloat",
        "CP1 Y Shadow Strength": "NodeSocketFloat",
        "CP7 X Bias": "NodeSocketFloat",
        "CP7 Y Scale": "NodeSocketFloat",
        "CP7 Z Offset": "NodeSocketFloat",
        "CP12 Y Light Color Mix": "NodeSocketFloat",
    }
    if any(shared_state_outputs.get(name) != kind for name, kind in required_state_outputs.items()):
        raise ValueError(f"Shared HGRP state group is missing required outputs: {shared_state_group.name}")
    for name, kind, value, description in (
        ("Eye Parallax Scale", "NodeSocketFloat", parameters.parallax_scale, "Source _ParallaxScale; zero disables UV displacement for a controlled comparison."),
        ("Eye Response Mix", "NodeSocketFloat", 1.0, "Eye-local A/B control: 1 selects the source Eye diffuse branch; 0 selects the Blender surface baseline. This is not a captured game uniform."),
        ("Eye Ramp CP11 W", "NodeSocketFloat", 0.0, "Explicit preview input for _CharacterParams11.w; final runtime value is not captured."),
        ("Eye Ramp CP12 X", "NodeSocketFloat", 0.0, "Explicit preview input for _CharacterParams12.x; final runtime value is not captured."),
        ("Avatar Customization Enabled", "NodeSocketFloat", parameters.avatar_customize_enabled, "Static source _AvatarCustomizeEnable value; runtime avatar override is not captured."),
        ("Eye Tint Color", "NodeSocketColor", parameters.eye_tint_color, "Static source _EyeTintColor value; runtime avatar override is not captured."),
        ("CP13 Eye Base Light Multiplier", "NodeSocketFloat", 0.0, "Constructor initial value 0.0; final runtime state is not captured and remains editable."),
        ("CP13 Eye Highlight Multiplier", "NodeSocketFloat", 0.0, "Constructor initial value 0.0; final runtime state is not captured and remains editable."),
        ("CP13 Eye Scattering Multiplier", "NodeSocketFloat", 0.0, "Constructor initial value 0.0; final runtime state is not captured and remains editable."),
        ("Eye Shadow Weight", "NodeSocketFloat", 1.0, "Explicit preview screen-shadow weight; one means lit and is not captured game shadow state."),
        ("Matcap Enabled", "NodeSocketFloat", parameters.use_matcap, "Per-material preview gate corresponding to source _MATCAP_ON and _UseMatcap."),
        ("Eye Highlight Enabled", "NodeSocketFloat", parameters.use_eye_highlight, "Per-material preview gate corresponding to source _EYE_HIGHLIGHT and _EyeHighLight."),
    ):
        _input_socket(tree, name, kind, value, description)

    group_inputs = [item for item in tree.nodes if item.type == "GROUP_INPUT"]
    if len(group_inputs) != 1:
        raise ValueError(f"Expected one eye group input node: {tree.name}")
    source_input = group_inputs[0]
    shared_state = node(tree, "ShaderNodeGroup", "Shared HGRP constructor and preview state", "HGRP Shared Preview State")
    shared_state.node_tree = shared_state_group
    base_texture, base_mapping, uv_node = _texture_and_mapping(tree, "_BaseMap")
    ramp_texture: bpy.types.Node | None = _exact_node(tree, "TEX_IMAGE", "Source diffuse ramp RGB and alpha") if parameters.use_diff_ramp == 1.0 else None
    if ramp_texture is not None and (ramp_texture.image is None or not ramp_texture.image.packed_file):
        raise ValueError(f"Source eye diffuse ramp is not packed: {tree.name}")
    matcap_texture: bpy.types.Node | None = None
    matcap_mapping: bpy.types.Node | None = None
    if parameters.use_matcap == 1.0:
        matcap_texture, matcap_mapping, _ = _texture_and_mapping(tree, "_MatcapTex")
    geometry = _exact_node(tree, "NEW_GEOMETRY", "World surface normal")
    emission = _exact_node(tree, "EMISSION", "Artist-lit NPR diffuse - shared engines")
    eye_base_alpha = _math(tree, "MULTIPLY", base_texture.outputs["Alpha"], parameters.base_color_alpha, "Source eye BaseMap alpha times BaseColor alpha")
    base_albedo = _exact_node(tree, "MIX_RGB", "Source base sample times BaseColor")
    artist_state = _exact_node(tree, "GROUP", "Artist global state - not game constants")
    if parameters.use_eye_highlight == 1.0 and artist_state.outputs.get("Direct Intensity") is None:
        raise ValueError(f"Artist preview state has no direct-intensity output: {tree.name}")
    if len(base_albedo.outputs[0].links) == 0:
        raise ValueError(f"Source eye albedo is disconnected: {tree.name}")

    response_mix = _exact_node(tree, "MIX_SHADER", "Editable native-sampling response / baseline")
    response_links = tuple(response_mix.inputs["Fac"].links)
    if len(response_links) != 1 or response_links[0].from_node != artist_state or response_links[0].from_socket.name != "Response Weight":
        raise ValueError(f"Eye response factor has unexpected source: {tree.name}")
    tree.links.remove(response_links[0])
    tree.links.new(source_input.outputs["Eye Response Mix"], response_mix.inputs["Fac"])

    uv_split = node(tree, "ShaderNodeSeparateXYZ", "Source UV0 eye-domain components", "Source Eye Matcap")
    tree.links.new(uv_node.outputs["UV"], uv_split.inputs[0])
    frac_uv = tuple(_math(tree, "FRACT", uv_split.outputs[axis], 0.0, "Source frac UV " + axis) for axis in ("X", "Y"))
    centered = tuple(_math(tree, "SUBTRACT", frac_uv[index], 0.5, "Source centered UV " + axis) for index, axis in enumerate(("X", "Y")))
    dist_sq = _math(tree, "ADD", _math(tree, "MULTIPLY", centered[0], centered[0], "Eye UV X squared"), _math(tree, "MULTIPLY", centered[1], centered[1], "Eye UV Y squared"), "Eye UV radius squared")
    inside = _math(tree, "LESS_THAN", dist_sq, 0.25, "Source iris inside mask")
    iris_mask = _math(tree, "SUBTRACT", 1.0, inside, "Source eye-white mask")
    inside_mask = inside

    white = (1.0, 1.0, 1.0, 1.0)
    if parameters.use_eye_highlight == 1.0:
        highlight_color = node(tree, "ShaderNodeRGB", "Source EyeHighLightColor", "Source Eye Highlight")
        highlight_color.outputs[0].default_value = parameters.highlight_color
        scattering_color = node(tree, "ShaderNodeRGB", "Source EyeScatteringColor", "Source Eye Highlight")
        scattering_color.outputs[0].default_value = parameters.scattering_color
        highlight_region = color_mix(tree, "MIX", iris_mask, white, highlight_color.outputs[0], "Source highlight color by eye mask", "Source Eye Highlight")
        scatter_region = color_mix(tree, "MIX", eye_base_alpha, white, scattering_color.outputs[0], "Source scattering color by base alpha", "Source Eye Highlight")
        eye_blend = color_mix(tree, "MULTIPLY", 1.0, highlight_region, scatter_region, "Source eye highlight/scattering blend", "Source Eye Highlight")
        eye_highlight_enable = color_mix(tree, "MIX", source_input.outputs["Eye Highlight Enabled"], white, eye_blend, "Source EYE_HIGHLIGHT gate", "Source Eye Highlight")
    else:
        neutral_eye_blend = node(tree, "ShaderNodeRGB", "Source EyeHighlight disabled neutral multiplier", "Source Eye Highlight")
        neutral_eye_blend.outputs[0].default_value = white
        eye_highlight_enable = neutral_eye_blend.outputs[0]

    if parameters.use_matcap == 1.0:
        if matcap_mapping is None:
            raise ValueError(f"Enabled Eye Matcap has no source mapping: {tree.name}")
        light_n, _, parallax_uv, matcap_uv = _build_matcap_geometry(
            tree,
            geometry,
            uv_node,
            uv_split,
            (frac_uv[0], frac_uv[1]),
            dist_sq,
            base_mapping,
            matcap_mapping,
            source_input,
            parameters,
        )
    else:
        parallax_uv = None
        matcap_uv = None
        light_n = _vector_node(tree, "NORMALIZE", geometry.outputs["Normal"], None, "Normalize source eye normal without Matcap").outputs["Vector"]

    camera_axis = node(tree, "ShaderNodeVectorTransform", "Fixed camera local Z axis in world", "Source Eye Matcap")
    camera_axis.vector_type = "VECTOR"
    camera_axis.convert_from = "CAMERA"
    camera_axis.convert_to = "WORLD"
    camera_axis.inputs["Vector"].default_value = (0.0, 0.0, 1.0)
    camera_forward = _vector_node(tree, "NORMALIZE", camera_axis.outputs["Vector"], None, "Normalize fixed camera-forward world axis").outputs["Vector"]
    view_ramp_alpha: bpy.types.NodeSocket | None = None
    if parameters.use_diff_ramp == 1.0:
        if ramp_texture is None or ramp_texture.image is None:
            raise ValueError(f"Enabled Eye diffuse ramp has no packed source image: {tree.name}")
        view_dot = node(tree, "ShaderNodeVectorMath", "Source eye normal dot fixed camera forward", "Source Eye Matcap")
        view_dot.operation = "DOT_PRODUCT"
        tree.links.new(light_n, view_dot.inputs[0])
        tree.links.new(camera_forward, view_dot.inputs[1])
        view_u = _clamp(tree, _math(tree, "ADD", _math(tree, "MULTIPLY", view_dot.outputs["Value"], 0.5, "Source view ramp half"), 0.5, "Source view ramp U"), 0.0, 1.0, "Source view ramp clamp")
        ramp_uv = node(tree, "ShaderNodeCombineXYZ", "Source view-ramp lookup UV", "Source Eye Matcap")
        tree.links.new(view_u, ramp_uv.inputs["X"])
        ramp_uv.inputs["Y"].default_value = 0.5
        ramp_uv.inputs["Z"].default_value = 0.0
        view_ramp = node(tree, "ShaderNodeTexImage", "Source eye view ramp alpha", "Source Eye Matcap")
        view_ramp.image = ramp_texture.image
        view_ramp.interpolation = ramp_texture.interpolation
        view_ramp.extension = "EXTEND"
        tree.links.new(ramp_uv.outputs[0], view_ramp.inputs["Vector"])
        view_ramp_alpha = view_ramp.outputs["Alpha"]
    shadow_weight = _clamp(tree, source_input.outputs["Eye Shadow Weight"], 0.0, 1.0, "Explicit eye shadow weight")
    eye_albedo, full_diffuse, npr_diffuse, alpha_premultiply, selected_view_ramp_alpha, min_ramp_alpha = _build_source_eye_diffuse(
        tree,
        source_input,
        shared_state,
        diffuse_group,
        artist_state,
        light_n,
        iris_mask,
        base_albedo.outputs[0],
        eye_highlight_enable,
        eye_base_alpha,
        ramp_texture,
        view_ramp_alpha,
        camera_forward,
        shadow_weight,
        parameters,
    )
    if parameters.use_matcap == 1.0:
        if matcap_texture is None:
            raise ValueError(f"Enabled Eye Matcap has no source texture: {tree.name}")
        matcap_mix = _lerp(tree, selected_view_ramp_alpha, min_ramp_alpha, shadow_weight, "Source Matcap ramp/shadow mix")
        cp0_z = shared_state.outputs["CP0 Z Ambient Floor"]
        matcap_intensity = _math(
            tree,
            "MULTIPLY",
            _math(tree, "ADD", _math(tree, "MULTIPLY", matcap_mix, _math(tree, "SUBTRACT", 1.0, cp0_z, "Source CP0.z complement"), "Source Matcap CP0.z mix"), cp0_z, "Source Matcap CP0.z floor"),
            _math(tree, "ADD", _math(tree, "MULTIPLY", matcap_mix, 0.5, "Source Matcap ramp half"), 0.5, "Source Matcap ramp floor"),
            "Source Matcap intensity",
        )
        matcap_texture.extension = "EXTEND"
        color_tint = node(tree, "ShaderNodeCombineColor", "Source Matcap tint alpha channel", "Source Eye Matcap")
        for channel in ("Red", "Green", "Blue"):
            color_tint.inputs[channel].default_value = parameters.matcap_color[3]
        alpha_tint = node(tree, "ShaderNodeCombineColor", "Source Matcap sample alpha channels", "Source Eye Matcap")
        tree.links.new(matcap_texture.outputs["Alpha"], alpha_tint.inputs["Red"])
        tree.links.new(matcap_texture.outputs["Alpha"], alpha_tint.inputs["Green"])
        tree.links.new(matcap_texture.outputs["Alpha"], alpha_tint.inputs["Blue"])
        tint_color = node(tree, "ShaderNodeRGB", "Source Matcap color", "Source Eye Matcap")
        tint_color.outputs[0].default_value = parameters.matcap_color
        rgb_term = color_mix(tree, "MULTIPLY", 1.0, matcap_texture.outputs["Color"], color_tint.outputs["Color"], "Source Matcap RGB times tint alpha", "Source Eye Matcap")
        alpha_term = color_mix(tree, "MULTIPLY", 1.0, tint_color.outputs[0], alpha_tint.outputs["Color"], "Source Matcap alpha times tint RGB", "Source Eye Matcap")
        source_matcap_color = color_mix(tree, "ADD", 1.0, rgb_term, alpha_term, "Source Matcap color/alpha combination", "Source Eye Matcap")
        intensity_color = _combine_color(tree, matcap_intensity, "Source Matcap scalar intensity color")
        intensity_full = color_mix(tree, "MULTIPLY", 1.0, full_diffuse, intensity_color, "Source HGRP Eye Full Diffuse times Matcap intensity", "Source Eye Matcap")
        enabled_color = _combine_color(tree, source_input.outputs["Matcap Enabled"], "Source Matcap enable color")
        matcap_contribution = color_mix(tree, "MULTIPLY", 1.0, color_mix(tree, "MULTIPLY", 1.0, source_matcap_color, intensity_full, "Matcap source color times full diffuse", "Source Eye Matcap"), enabled_color, "Matcap preview enable gate", "Source Eye Matcap")
    else:
        disabled_matcap = node(tree, "ShaderNodeRGB", "Source Matcap disabled zero contribution", "Source Eye Matcap")
        disabled_matcap.outputs[0].default_value = (0.0, 0.0, 0.0, 0.0)
        matcap_contribution = disabled_matcap.outputs[0]

    if parameters.use_eye_highlight == 1.0:
        direct_base = color_mix(tree, "MULTIPLY", 1.0, eye_albedo, source_input.outputs["CP13 Eye Base Light Multiplier"], "Source CP13 eye base direct", "Source Eye Highlight")
        highlight_strength = _math(tree, "MULTIPLY", iris_mask, source_input.outputs["CP13 Eye Highlight Multiplier"], "Source CP13 eye highlight strength")
        highlight_direct = color_mix(tree, "MULTIPLY", 1.0, highlight_color.outputs[0], _combine_color(tree, highlight_strength, "Source CP13 highlight color scale"), "Source CP13 eye highlight direct", "Source Eye Highlight")
        scattering_strength = _math(tree, "MULTIPLY", eye_base_alpha, source_input.outputs["CP13 Eye Scattering Multiplier"], "Source CP13 eye scattering strength")
        scattering_direct = color_mix(tree, "MULTIPLY", 1.0, scattering_color.outputs[0], _combine_color(tree, scattering_strength, "Source CP13 scattering color scale"), "Source CP13 scattering direct", "Source Eye Highlight")
        eye_direct_unpremultiplied = color_mix(tree, "ADD", 1.0, color_mix(tree, "ADD", 1.0, direct_base, highlight_direct, "Source eye direct base plus highlight", "Source Eye Highlight"), scattering_direct, "Source eye direct sum", "Source Eye Highlight")
        eye_direct = color_mix(tree, "MULTIPLY", 1.0, eye_direct_unpremultiplied, alpha_premultiply, "Source Eye direct premultiplied alpha", "Source Eye Highlight")
        direct_intensity = color_mix(tree, "MULTIPLY", 1.0, eye_direct, artist_state.outputs["Direct Intensity"], "Artist direct-light proxy for source eye direct", "Source Eye Highlight")
        eye_direct_gated = color_mix(tree, "MULTIPLY", 1.0, direct_intensity, _combine_color(tree, source_input.outputs["Eye Highlight Enabled"], "Source eye direct enable color"), "Source EYE_HIGHLIGHT direct gate", "Source Eye Highlight")
    else:
        disabled_highlight = node(tree, "ShaderNodeRGB", "Source EyeHighlight disabled zero contribution", "Source Eye Highlight")
        disabled_highlight.outputs[0].default_value = (0.0, 0.0, 0.0, 0.0)
        eye_direct_gated = disabled_highlight.outputs[0]
    npr_full = color_mix(tree, "MULTIPLY", 1.0, npr_diffuse, full_diffuse, "Source Eye NPR color times full diffuse", "Source Eye ComputeNPRDiffuse")
    npr_premultiplied = color_mix(tree, "MULTIPLY", 1.0, npr_full, alpha_premultiply, "Source Eye NPR premultiplied alpha", "Source Eye ComputeNPRDiffuse")
    main_lit = color_mix(tree, "ADD", 1.0, npr_premultiplied, matcap_contribution, "Source Eye mainLit before luminance desaturation", "Source Eye ComputeNPRDiffuse")
    main_lit_luminance = luminance(tree, main_lit, "Source Eye mainLit luminance")
    desaturation = _clamp(tree, _math(tree, "SUBTRACT", main_lit_luminance, 0.5, "Source Eye desaturation threshold"), 0.0, 0.5, "Source Eye desaturation amount")
    desaturation_factor = _math(tree, "ADD", _math(tree, "MULTIPLY", desaturation, desaturation, "Source Eye desaturation square"), 1.0, "Source Eye desaturation factor")
    main_lit_luminance_color = _combine_color(tree, main_lit_luminance, "Source Eye mainLit luminance color")
    term1 = color_mix(
        tree,
        "ADD",
        1.0,
        color_mix(tree, "MULTIPLY", 1.0, color_mix(tree, "SUBTRACT", 1.0, main_lit, main_lit_luminance_color, "Source Eye mainLit chroma", "Source Eye ComputeNPRDiffuse"), _combine_color(tree, desaturation_factor, "Source Eye desaturation factor color"), "Source Eye desaturation scaled chroma", "Source Eye ComputeNPRDiffuse"),
        main_lit_luminance_color,
        "Source Eye DesaturateAroundLuma result",
        "Source Eye ComputeNPRDiffuse",
    )
    final_color = color_mix(tree, "ADD", 1.0, term1, eye_direct_gated, "Source Eye mainLit plus explicit CP13 preview direct", "Source Eye Highlight")
    for link in tuple(emission.inputs["Color"].links):
        tree.links.remove(link)
    tree.links.new(final_color, emission.inputs["Color"])
    tree["eye_shared_state_group"] = shared_state_group.name
    variant_key = _eye_variant_key(parameters)
    compiled_variant = variant_key in SOURCE_COMPILED_EYE_VARIANTS
    ramp_evidence = (
        "source_compiled_texture"
        if parameters.use_diff_ramp == 1.0
        else "source_compiled_no_ramp"
        if compiled_variant
        else "ruri_reference_only"
    )
    tree["eye_lighting_scope"] = (
        "Source Eye ComputeNPRDiffuse with source-switch-selected Matcap and EyeHighlight branches; "
        f"DiffRamp evidence={ramp_evidence}. Host preview inputs remain explicit."
    )
    tree["eye_source_parameters"] = json.dumps({
        "parallax_scale": parameters.parallax_scale,
        "matcap_normal_scale": parameters.normal_scale,
        "matcap_color": parameters.matcap_color,
        "eye_highlight_color": parameters.highlight_color,
        "eye_scattering_color": parameters.scattering_color,
        "eye_tint_color": parameters.eye_tint_color,
        "avatar_customize_enabled_source": parameters.avatar_customize_enabled,
        "metallic_source": parameters.metallic,
        "base_color_alpha": parameters.base_color_alpha,
        "cp13_constructor_initial": [0.0, 0.0, 0.0],
        "cp13_preview_override": [0.0, 0.0, 0.0],
        "matcap_enabled_source": parameters.use_matcap,
        "eye_highlight_enabled_source": parameters.use_eye_highlight,
        "diffuse_ramp_enabled_source": parameters.use_diff_ramp,
        "matcap_branch_status": "source_enabled" if parameters.use_matcap == 1.0 else "source_disabled",
        "eye_highlight_branch_status": "source_enabled" if parameters.use_eye_highlight == 1.0 else "source_disabled",
        "compiled_variant_evidence": "current_compiled" if compiled_variant else "ruri_reference_only",
        "diffuse_ramp_branch_status": ramp_evidence,
        "compiled_variant_key_matcap_highlight_ramp": variant_key,
        "compiled_variant_key_order": ["_UseMatcap", "_EyeHighLight", "_UseDiffRampMap"],
        "eye_response_mix_default": 1.0,
        "eye_response_mix_default_branch": "source Eye ComputeNPRDiffuse",
        "eye_ramp_cp11_w_preview": 0.0,
        "eye_ramp_cp12_x_preview": 0.0,
    }, sort_keys=True)
    layout(tree)
    return {
        "source_identity": tree["source_identity"],
        "source_shader": tree["source_shader"],
        "source_switches": json.dumps({
            "use_matcap": parameters.use_matcap,
            "use_eye_highlight": parameters.use_eye_highlight,
            "use_diff_ramp": parameters.use_diff_ramp,
        }, sort_keys=True),
        "compiled_variant_evidence": "current_compiled" if compiled_variant else "ruri_reference_only",
        "diffuse_ramp_evidence": ramp_evidence,
        "matcap_texture": matcap_texture.image.name if matcap_texture is not None and matcap_texture.image is not None else "disabled_by_source",
        "matcap_size": json.dumps(tuple(matcap_texture.image.size)) if matcap_texture is not None and matcap_texture.image is not None else "[]",
        "parallax_uv": parallax_uv.name if parallax_uv is not None else "disabled_by_source",
        "matcap_uv": matcap_uv.name if matcap_uv is not None else "disabled_by_source",
        "lighting_output": final_color.node.name,
        "source_diffuse_group": diffuse_group.name,
        "shared_state_group": shared_state_group.name,
        "full_diffuse_output": full_diffuse.node.name,
        "cp0_z_output": shared_state.outputs["CP0 Z Ambient Floor"].node.name,
        "tangent_sign_attribute": TANGENT_SIGN_ATTRIBUTE,
    }


def source_matcap_color(
    sample: tuple[float, float, float, float],
    tint: tuple[float, float, float, float],
    intensity: float,
    full_diffuse: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Evaluate the source RGBA tint combination for numeric boundary checks."""
    if len(sample) != 4 or len(tint) != 4 or len(full_diffuse) != 3:
        raise ValueError("Matcap source and preview vectors must be RGBA, RGBA, and RGB")
    values = (*sample, *tint, intensity, *full_diffuse)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Matcap source and preview values must be finite")
    return tuple((sample[index] * tint[3] + sample[3] * tint[index]) * intensity * full_diffuse[index] for index in range(3))



