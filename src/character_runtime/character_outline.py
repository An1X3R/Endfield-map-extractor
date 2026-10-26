"""Attach an editable, source-gated inverted-hull outline preview."""
from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import cast

import bpy

Identity = tuple[str, int]
SUPPORTED_OUTLINE_SHADERS = frozenset((
    "HGRP/CharacterNPR",
    "HGRP/CharacterNPR_Skin",
    "HGRP/CharacterNPR_Hair",
))
CONTRACT = "EndfieldCharacterOutlineHull/1"
SCOPE = (
    "Editable inverted-hull preview shared by Eevee and Cycles. Width conversion is an artistic "
    "local-space meter scale, not native screen-pixel equivalence. Source pass depth, stencil, "
    "render order, source offset-Z behavior, exact outline-mask channel arithmetic and average-normal "
    "baking and material mixing across source passes are not reproduced. "
    "Source alpha clipping is approximated with a host threshold and may differ from source pass semantics."
)


def _mapping(value: object, label: str) -> tuple[dict[str, object] | None, str]:
    if not isinstance(value, str):
        return None, f"{label} is missing or not text"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        return None, f"{label} is invalid JSON: {error.msg}"
    if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
        return None, f"{label} is not a string-keyed object"
    return cast(dict[str, object], parsed), ""


def _identity(value: object, label: str) -> tuple[Identity | None, str]:
    if not isinstance(value, list) or len(value) != 2:
        return None, f"{label} is not a two-item source identity"
    source, path_id = value
    if not isinstance(source, str) or type(path_id) is not int:
        return None, f"{label} has an invalid source identity"
    return (source.casefold(), path_id), ""


def _identity_key(value: Identity) -> str:
    return json.dumps(value, separators=(",", ":"))


def _canonical_metadata(value: object) -> str:
    parsed: object = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _source_texture_signature(group: bpy.types.ShaderNodeTree, slot_name: str) -> tuple[object, ...]:
    matches = tuple(
        node for node in group.nodes
        if node.type == "TEX_IMAGE" and node.get("source_slot") == slot_name
    )
    if len(matches) != 1:
        return (slot_name, "image-node-count", len(matches))
    sample = matches[0]
    image_pointer = sample.image.as_pointer() if sample.image is not None else None
    vector_links = tuple(
        link for link in group.links
        if link.to_node == sample and link.to_socket == sample.inputs["Vector"]
    )
    if len(vector_links) != 1 or vector_links[0].from_node.type != "MAPPING":
        return (slot_name, image_pointer, str(sample.extension), "mapping-unverified")
    mapping = vector_links[0].from_node
    uv_links = tuple(
        link for link in group.links
        if link.to_node == mapping and link.to_socket == mapping.inputs["Vector"]
    )
    if len(uv_links) != 1 or uv_links[0].from_node.type != "UVMAP":
        return (slot_name, image_pointer, str(sample.extension), "uv-unverified")
    uv_map = uv_links[0].from_node.uv_map
    scale = tuple(float(value) for value in mapping.inputs["Scale"].default_value[:2])
    offset = tuple(float(value) for value in mapping.inputs["Location"].default_value[:2])
    return (slot_name, image_pointer, str(sample.extension), uv_map, scale, offset)


def _source_group_signature(group: bpy.types.ShaderNodeTree) -> str:
    metadata = tuple(
        (key, _canonical_metadata(group.get(key)))
        for key in (
            "source_shader",
            "source_floats",
            "source_colors",
            "source_null_texture_defaults",
        )
    )
    textures = tuple(_source_texture_signature(group, slot) for slot in ("_BaseMap", "_OutlineMask"))
    return json.dumps((metadata, textures), separators=(",", ":"), ensure_ascii=False)


def _source_groups(material: bpy.types.Material) -> tuple[dict[str, bpy.types.ShaderNodeTree], str]:
    tree = material.node_tree
    if tree is None:
        return {}, "material has no node tree"
    groups: dict[str, bpy.types.ShaderNodeTree] = {}
    signatures: dict[str, str] = {}
    for node in tree.nodes:
        if node.type != "GROUP" or node.node_tree is None:
            continue
        raw_identity = node.node_tree.get("source_identity")
        if raw_identity is None:
            continue
        try:
            parsed_identity = json.loads(str(raw_identity))
        except json.JSONDecodeError:
            return {}, f"source identity is invalid JSON in {material.name}"
        identity, reason = _identity(parsed_identity, f"{material.name} source identity")
        if identity is None:
            return {}, reason
        key = _identity_key(identity)
        signature = _source_group_signature(node.node_tree)
        if key in signatures and signatures[key] != signature:
            return {}, f"source identity has conflicting outline inputs in {material.name}: {key}"
        groups.setdefault(key, node.node_tree)
        signatures.setdefault(key, signature)
    return groups, ""


def _texture_info(
    group: bpy.types.ShaderNodeTree,
    slot_name: str,
) -> tuple[dict[str, object] | None, str]:
    matches = tuple(
        node for node in group.nodes
        if node.type == "TEX_IMAGE" and node.get("source_slot") == slot_name
    )
    if len(matches) != 1:
        return None, f"source texture slot {slot_name} has {len(matches)} image nodes"
    sample = matches[0]
    image = sample.image
    if image is None or image.size[0] <= 0 or image.size[1] <= 0 or image.packed_file is None:
        return None, f"source texture slot {slot_name} has no packed image"
    vector_links = tuple(
        link for link in group.links
        if link.to_node == sample and link.to_socket == sample.inputs["Vector"]
    )
    if len(vector_links) != 1 or vector_links[0].from_node.type != "MAPPING":
        return None, f"source texture slot {slot_name} has no verified UV mapping"
    mapping = vector_links[0].from_node
    uv_links = tuple(
        link for link in group.links
        if link.to_node == mapping and link.to_socket == mapping.inputs["Vector"]
    )
    if len(uv_links) != 1 or uv_links[0].from_node.type != "UVMAP":
        return None, f"source texture slot {slot_name} has no named UV layer"
    uv_map = str(uv_links[0].from_node.uv_map)
    if uv_map != "UV0":
        return None, f"source texture slot {slot_name} uses unsupported UV layer {uv_map}"
    scale = tuple(float(value) for value in mapping.inputs["Scale"].default_value[:2])
    offset = tuple(float(value) for value in mapping.inputs["Location"].default_value[:2])
    if any(not math.isfinite(value) for value in (*scale, *offset)):
        return None, f"source texture slot {slot_name} has non-finite UV mapping"
    return {
        "image": image,
        "uv_map": uv_map,
        "scale": cast(tuple[float, float], scale),
        "offset": cast(tuple[float, float], offset),
        "extension": str(sample.extension),
    }, ""


def _number(values: dict[str, object], name: str) -> tuple[float | None, str]:
    value = values.get(name)
    if type(value) not in (int, float) or not math.isfinite(float(cast(float, value))):
        return None, f"source property {name} is missing or non-finite"
    return float(cast(float, value)), ""


def _color(values: dict[str, object], name: str) -> tuple[tuple[float, float, float, float] | None, str]:
    value = values.get(name)
    if not isinstance(value, list) or len(value) != 4:
        return None, f"source color {name} is missing or not RGBA"
    if any(type(component) not in (int, float) or not math.isfinite(float(cast(float, component))) for component in value):
        return None, f"source color {name} has non-finite components"
    return cast(tuple[float, float, float, float], tuple(float(component) for component in value)), ""


def _texture_signature(texture: dict[str, object] | None) -> tuple[object, ...] | None:
    if texture is None:
        return None
    image = cast(bpy.types.Image, texture["image"])
    return (
        image.as_pointer(),
        texture["uv_map"],
        texture["scale"],
        texture["offset"],
        texture["extension"],
    )


def _outline_appearance_signature(config: dict[str, object]) -> str:
    values = {
        key: config[key] for key in (
            "source_color", "base_color", "outline_tint", "tint_enabled", "brightness",
            "saturation", "use_tint_controls", "mask_enabled", "alpha_test_enabled", "alpha_threshold",
        )
    }
    values["base_texture"] = _texture_signature(cast(dict[str, object] | None, config["base_texture"]))
    values["mask_texture"] = _texture_signature(cast(dict[str, object] | None, config["mask_texture"]))
    return json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _outline_config(
    group: bpy.types.ShaderNodeTree,
    mesh: bpy.types.Object,
    material_name: str,
) -> tuple[dict[str, object] | None, str]:
    shader_value = group.get("source_shader")
    if not isinstance(shader_value, str) or shader_value not in SUPPORTED_OUTLINE_SHADERS:
        return None, f"source shader is not outline-capable: {shader_value}"
    floats, reason = _mapping(group.get("source_floats"), f"{material_name} source floats")
    if floats is None:
        return None, reason
    colors, reason = _mapping(group.get("source_colors"), f"{material_name} source colors")
    if colors is None:
        return None, reason
    enabled, reason = _number(floats, "_EnableOutline")
    if enabled is None:
        return None, reason
    if enabled != 1.0:
        return None, f"source _EnableOutline is disabled for {material_name}"
    width, reason = _number(floats, "_OutlineWidth")
    if width is None:
        return None, reason
    if width <= 0.0:
        return None, f"source _OutlineWidth is not positive for {material_name}"
    base_color, reason = _color(colors, "_BaseColor")
    if base_color is None:
        return None, reason
    base_texture, base_texture_reason = _texture_info(group, "_BaseMap")
    if base_texture is None:
        defaults, default_reason = _mapping(group.get("source_null_texture_defaults"), f"{material_name} verified texture defaults")
        if defaults is None or defaults.get("_BaseMap") != "white":
            return None, base_texture_reason if defaults is not None else default_reason
    mask_enabled, reason = _number(floats, "_EnableOutlineMask")
    if mask_enabled is None:
        return None, reason
    mask_texture: dict[str, object] | None = None
    if mask_enabled == 1.0:
        if not mesh.data.uv_layers.get("UV0"):
            return None, f"required source outline mask has no UV0 on {mesh.name}"
        mask_texture, reason = _texture_info(group, "_OutlineMask")
        if mask_texture is None:
            return None, f"required source outline mask is unavailable for {material_name}: {reason}"
    elif mask_enabled != 0.0:
        return None, f"source _EnableOutlineMask has unsupported value for {material_name}: {mask_enabled}"
    outline_tint, tint_reason = _color(colors, "_OutlineTintColor")
    tint_enabled, tint_enabled_reason = _number(floats, "_OutlineTintEnable")
    if tint_enabled is None and "_OutlineTintEnable" in floats:
        return None, tint_enabled_reason
    if tint_enabled is not None and tint_enabled not in (0.0, 1.0):
        return None, f"source _OutlineTintEnable has unsupported value for {material_name}: {tint_enabled}"
    if tint_enabled == 1.0 and outline_tint is None:
        return None, tint_reason if "_OutlineTintColor" in colors else "source tint is enabled but _OutlineTintColor is missing"
    brightness, brightness_reason = _number(floats, "_OutlineColorBrightness")
    saturation, saturation_reason = _number(floats, "_OutlineColorSaturation")
    use_tint_controls = tint_enabled is None or tint_enabled == 0.0
    if use_tint_controls and (brightness is None or saturation is None):
        return None, brightness_reason if brightness is None else saturation_reason
    offset_z, offset_reason = _number(floats, "_OutlineOffsetZ")
    average_normal, normal_reason = _number(floats, "_OutlineAverageNormal")
    alpha_test, reason = _number(floats, "_EnableAlphaTest")
    if alpha_test is None:
        return None, reason
    if alpha_test not in (0.0, 1.0):
        return None, f"source _EnableAlphaTest has unsupported value for {material_name}: {alpha_test}"
    alpha_threshold, reason = _number(floats, "_AlphaClipThreshold")
    if alpha_threshold is None:
        return None, reason
    return {
        "shader": shader_value,
        "source_group": group,
        "source_width": width,
        "source_offset_z": offset_z,
        "source_average_normal": average_normal,
        "source_color": outline_tint if tint_enabled == 1.0 and outline_tint is not None else (1.0, 1.0, 1.0, 1.0),
        "base_color": base_color,
        "outline_tint": outline_tint,
        "tint_enabled": tint_enabled,
        "brightness": brightness,
        "saturation": saturation,
        "use_tint_controls": use_tint_controls,
        "base_texture": base_texture,
        "base_texture_reason": base_texture_reason,
        "mask_enabled": mask_enabled == 1.0,
        "mask_texture": mask_texture,
        "offset_reason": offset_reason,
        "normal_reason": normal_reason,
        "alpha_test_enabled": alpha_test == 1.0,
        "alpha_threshold": alpha_threshold,
    }, ""


def _source_material_slots(
    mesh: bpy.types.Object,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    configured: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for slot_index, slot in enumerate(mesh.material_slots):
        material = slot.material
        if material is None:
            skipped.append({"slot": slot_index, "reason": "material slot is empty"})
            continue
        raw_stack = material.get("source_material_stack")
        if not isinstance(raw_stack, str):
            skipped.append({"slot": slot_index, "material": material.name, "reason": "source material stack is absent"})
            continue
        try:
            stack = json.loads(raw_stack)
        except json.JSONDecodeError as error:
            skipped.append({"slot": slot_index, "material": material.name, "reason": f"source material stack is invalid JSON: {error.msg}"})
            continue
        if not isinstance(stack, list) or not stack:
            skipped.append({"slot": slot_index, "material": material.name, "reason": "source material stack is empty or invalid"})
            continue
        groups, group_reason = _source_groups(material)
        if group_reason:
            skipped.append({"slot": slot_index, "material": material.name, "reason": group_reason})
            continue
        eligible: list[dict[str, object]] = []
        slot_reasons: list[str] = []
        for index, raw_identity in enumerate(stack):
            identity, identity_reason = _identity(raw_identity, f"{material.name} source pass {index}")
            if identity is None:
                slot_reasons.append(identity_reason)
                continue
            group = groups.get(_identity_key(identity))
            if group is None:
                slot_reasons.append(f"source material group is absent: {_identity_key(identity)}")
                continue
            config, config_reason = _outline_config(group, mesh, material.name)
            if config is not None:
                eligible.append(config)
            elif "_EnableOutline is disabled" not in config_reason and "source shader is not outline-capable" not in config_reason:
                slot_reasons.append(config_reason)
        if slot_reasons:
            skipped.append({"slot": slot_index, "material": material.name, "reason": "; ".join(slot_reasons)})
        if eligible:
            selected = eligible[0]
            widths = tuple(float(cast(float, item["source_width"])) for item in eligible)
            appearance_signatures = tuple(_outline_appearance_signature(item) for item in eligible)
            configured.append({
                "slot": slot_index,
                "material": material.name,
                "source": selected,
                "source_pass_count": len(eligible),
                "source_widths": list(widths),
                "source_pass_appearance_match": len(set(appearance_signatures)) == 1,
            })
    return configured, skipped


def _shader_node(tree: bpy.types.NodeTree, node_type: str, name: str, location: tuple[int, int]) -> bpy.types.Node:
    node = tree.nodes.new(node_type)
    node.name = name
    node.label = name
    node.location = location
    return node


def _texture_sample(
    tree: bpy.types.NodeTree,
    texture: dict[str, object],
    slot_name: str,
    location: tuple[int, int],
) -> bpy.types.Node:
    uv = _shader_node(tree, "ShaderNodeUVMap", f"Source {slot_name} UV0", location)
    uv.uv_map = cast(str, texture["uv_map"])
    mapping = _shader_node(tree, "ShaderNodeMapping", f"Source {slot_name} ST", (location[0] + 180, location[1]))
    mapping.vector_type = "POINT"
    mapping.inputs["Scale"].default_value = (*cast(tuple[float, float], texture["scale"]), 1.0)
    mapping.inputs["Location"].default_value = (*cast(tuple[float, float], texture["offset"]), 0.0)
    sample = _shader_node(tree, "ShaderNodeTexImage", f"Source {slot_name} | packed", (location[0] + 390, location[1]))
    sample.image = cast(bpy.types.Image, texture["image"])
    sample.extension = cast(str, texture["extension"])
    tree.links.new(uv.outputs[0], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs[0], sample.inputs["Vector"])
    return sample


def _multiply_color(
    tree: bpy.types.NodeTree,
    first: bpy.types.NodeSocket,
    second: bpy.types.NodeSocket,
    name: str,
    location: tuple[int, int],
) -> bpy.types.NodeSocket:
    multiply = _shader_node(tree, "ShaderNodeMixRGB", name, location)
    multiply.blend_type = "MULTIPLY"
    multiply.inputs[0].default_value = 1.0
    tree.links.new(first, multiply.inputs[1])
    tree.links.new(second, multiply.inputs[2])
    return multiply.outputs[0]


def _outline_material(
    mesh_name: str,
    slot_index: int,
    config: dict[str, object],
) -> bpy.types.Material:
    shader = cast(str, config["shader"])
    source_group = cast(bpy.types.ShaderNodeTree, config["source_group"])
    name = f"EF Outline Approx {mesh_name} slot {slot_index} {shader}"
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.surface_render_method = "BLENDED"
    material.use_transparency_overlap = True
    material.use_backface_culling = False
    material["outline_candidate_contract"] = CONTRACT
    material["outline_candidate_scope"] = SCOPE
    material["source_shader"] = shader
    material["source_outline_parameters"] = json.dumps({
        key: config[key] for key in (
            "source_width", "source_offset_z", "source_average_normal", "source_color",
            "outline_tint", "tint_enabled", "use_tint_controls", "brightness", "saturation",
            "mask_enabled", "alpha_test_enabled", "alpha_threshold",
        )
    }, sort_keys=True)
    material["outline_mask_host_composition"] = (
        "Source RGB luminance drives outline coverage and source RGB multiplies outline tint; "
        "native channel semantics are not recovered."
    )
    tree = material.node_tree
    if tree is None:
        raise RuntimeError(f"New outline material has no node tree: {name}")
    tree.nodes.clear()
    output = _shader_node(tree, "ShaderNodeOutputMaterial", "Outline Output", (1100, 150))
    geometry = _shader_node(tree, "ShaderNodeNewGeometry", "Hull Backfacing", (700, -350))
    transparent = _shader_node(tree, "ShaderNodeBsdfTransparent", "Transparent Hull Interior", (700, 250))
    emission = _shader_node(tree, "ShaderNodeEmission", "Editable Source-derived Outline", (700, 0))
    emission_color: bpy.types.NodeSocket = _shader_node(
        tree, "ShaderNodeRGB", "Outline Color Tint (editable)", (0, 0)
    ).outputs[0]
    tint_node = tree.nodes.get("Outline Color Tint (editable)")
    if tint_node is None:
        raise RuntimeError(f"Outline tint node was not created: {name}")
    tint_node.outputs[0].default_value = cast(tuple[float, float, float, float], config["source_color"])
    base_texture = cast(dict[str, object] | None, config["base_texture"])
    base_color = cast(tuple[float, float, float, float], config["base_color"])
    base_sample: bpy.types.Node | None = None
    if base_texture is not None:
        base_sample = _texture_sample(tree, base_texture, "BaseMap", (-900, 450))
        base_alpha = base_sample.outputs["Alpha"]
    else:
        alpha = _shader_node(tree, "ShaderNodeValue", "Verified white BaseMap alpha", (-250, 450))
        alpha.outputs[0].default_value = 1.0
        base_alpha = alpha.outputs[0]
    if bool(config["use_tint_controls"]):
        base_tint = _shader_node(tree, "ShaderNodeRGB", "Source BaseColor (editable)", (-900, 150))
        base_tint.outputs[0].default_value = base_color
        if base_texture is None:
            emission_color = base_tint.outputs[0]
        else:
            if base_sample is None:
                raise RuntimeError(f"Verified source BaseMap sample is unavailable: {name}")
            sampled = _multiply_color(tree, base_sample.outputs["Color"], base_tint.outputs[0], "Source BaseMap × BaseColor", (-250, 350))
            emission_color = sampled
    else:
        emission_color = tint_node.outputs[0]
    source_coverage: bpy.types.NodeSocket = base_alpha
    if bool(config["alpha_test_enabled"]):
        alpha_gate = _shader_node(tree, "ShaderNodeMath", "Approximate source alpha threshold", (220, -650))
        alpha_gate.operation = "GREATER_THAN"
        alpha_gate.inputs[1].default_value = cast(float, config["alpha_threshold"])
        tree.links.new(base_alpha, alpha_gate.inputs[0])
        source_coverage = alpha_gate.outputs[0]
    if bool(config["use_tint_controls"]):
        hue = _shader_node(tree, "ShaderNodeHueSaturation", "Source Brightness and Saturation (approximate)", (450, 350))
        hue.inputs["Hue"].default_value = 0.5
        hue.inputs["Saturation"].default_value = cast(float, config["saturation"])
        hue.inputs["Value"].default_value = cast(float, config["brightness"])
        hue.inputs["Fac"].default_value = 1.0
        tree.links.new(emission_color, hue.inputs["Color"])
        emission_color = hue.outputs[0]
    tree.links.new(emission_color, emission.inputs["Color"])
    if bool(config["mask_enabled"]):
        mask_texture = cast(dict[str, object], config["mask_texture"])
        mask_sample = _texture_sample(tree, mask_texture, "OutlineMask", (-900, -450))
        mask_luminance = _shader_node(tree, "ShaderNodeRGBToBW", "Outline mask RGB luminance (approximate)", (-250, -350))
        tree.links.new(mask_sample.outputs["Color"], mask_luminance.inputs["Color"])
        mask_influence = _shader_node(tree, "ShaderNodeValue", "Outline Mask Influence (editable)", (180, -250))
        mask_influence.outputs[0].default_value = 1.0
        mask_delta = _shader_node(tree, "ShaderNodeMath", "Mask luminance minus full coverage", (180, -450))
        mask_delta.operation = "SUBTRACT"
        mask_delta.inputs[1].default_value = 1.0
        tree.links.new(mask_luminance.outputs[0], mask_delta.inputs[0])
        scaled_delta = _shader_node(tree, "ShaderNodeMath", "Editable mask influence", (430, -350))
        scaled_delta.operation = "MULTIPLY"
        tree.links.new(mask_delta.outputs[0], scaled_delta.inputs[0])
        tree.links.new(mask_influence.outputs[0], scaled_delta.inputs[1])
        mask_coverage = _shader_node(tree, "ShaderNodeMath", "Source outline mask coverage", (680, -300))
        mask_coverage.operation = "ADD"
        mask_coverage.inputs[1].default_value = 1.0
        tree.links.new(scaled_delta.outputs[0], mask_coverage.inputs[0])
        combined_coverage = _shader_node(tree, "ShaderNodeMath", "Mask × source alpha coverage", (900, -300))
        combined_coverage.operation = "MULTIPLY"
        tree.links.new(mask_coverage.outputs[0], combined_coverage.inputs[0])
        tree.links.new(source_coverage, combined_coverage.inputs[1])
        source_coverage = combined_coverage.outputs[0]
    backface_coverage = _shader_node(tree, "ShaderNodeMath", "Backfacing × source coverage", (1100, -100))
    backface_coverage.operation = "MULTIPLY"
    tree.links.new(geometry.outputs["Backfacing"], backface_coverage.inputs[0])
    tree.links.new(source_coverage, backface_coverage.inputs[1])
    mix = _shader_node(tree, "ShaderNodeMixShader", "Backface Outline and Transparent Interior", (1100, 220))
    tree.links.new(backface_coverage.outputs[0], mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs["Surface"])
    tree["source_identity"] = source_group.get("source_identity")
    return material


def _transparent_material() -> bpy.types.Material:
    name = "EF Outline Approx Transparent Source Slot"
    material = bpy.data.materials.get(name)
    if material is not None:
        if material.get("outline_candidate_contract") != CONTRACT:
            raise ValueError(f"Existing material name belongs to another asset: {name}")
        return material
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.surface_render_method = "BLENDED"
    material.use_transparency_overlap = True
    material["outline_candidate_contract"] = CONTRACT
    tree = material.node_tree
    if tree is None:
        raise RuntimeError(f"New transparent outline material has no node tree: {name}")
    tree.nodes.clear()
    output = _shader_node(tree, "ShaderNodeOutputMaterial", "Transparent Output", (300, 0))
    transparent = _shader_node(tree, "ShaderNodeBsdfTransparent", "No outline for this source slot", (0, 0))
    tree.links.new(transparent.outputs[0], output.inputs["Surface"])
    return material


def _outline_geometry_group() -> bpy.types.GeometryNodeTree:
    name = "EF Character Outline Hull - APPROXIMATION"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("outline_candidate_contract") != CONTRACT:
            raise ValueError(f"Existing geometry group name belongs to another asset: {name}")
        return cast(bpy.types.GeometryNodeTree, existing)
    tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
    tree["outline_candidate_contract"] = CONTRACT
    tree["outline_candidate_scope"] = SCOPE
    geometry_in = tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    source_width = tree.interface.new_socket(name="Source Outline Width", in_out="INPUT", socket_type="NodeSocketFloat")
    source_width.default_value = 0.0
    source_width.min_value = 0.0
    source_width.max_value = 2.0
    meters_per_width = tree.interface.new_socket(name="Artistic Meters per Source Width", in_out="INPUT", socket_type="NodeSocketFloat")
    meters_per_width.default_value = 0.0
    meters_per_width.min_value = 0.0
    geometry_out = tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    entry = _shader_node(tree, "NodeGroupInput", "Editable Width Controls", (-700, 300))
    output = _shader_node(tree, "NodeGroupOutput", "Inverted Hull", (800, 300))
    multiply = _shader_node(tree, "ShaderNodeMath", "Source width × artistic meter scale", (-400, 0))
    multiply.operation = "MULTIPLY"
    tree.links.new(entry.outputs[source_width.identifier], multiply.inputs[0])
    tree.links.new(entry.outputs[meters_per_width.identifier], multiply.inputs[1])
    normal = _shader_node(tree, "GeometryNodeInputNormal", "Evaluated point normal", (-400, -250))
    offset = _shader_node(tree, "ShaderNodeVectorMath", "Normal expansion", (-100, -120))
    offset.operation = "SCALE"
    tree.links.new(normal.outputs["Normal"], offset.inputs[0])
    tree.links.new(multiply.outputs[0], offset.inputs[3])
    set_position = _shader_node(tree, "GeometryNodeSetPosition", "Post-deformation hull expansion", (100, 300))
    tree.links.new(entry.outputs[geometry_in.identifier], set_position.inputs["Geometry"])
    tree.links.new(offset.outputs[0], set_position.inputs["Offset"])
    tree.links.new(set_position.outputs["Geometry"], output.inputs[geometry_out.identifier])
    return tree


def _mesh_outline_row(
    scene: bpy.types.Scene,
    mesh: bpy.types.Object,
    meters_per_source_width: float,
    transparent: bpy.types.Material,
    geometry_group: bpy.types.GeometryNodeTree,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    raw_pipeline = mesh.get("source_renderer_pipeline_json")
    pipeline, reason = _mapping(raw_pipeline, f"{mesh.name} renderer pipeline")
    if pipeline is None:
        return None, {"object": mesh.name, "status": "skipped", "reason": reason}
    renderer_enabled = pipeline.get("m_EnableCharacterOutline")
    if type(renderer_enabled) is not bool:
        return None, {"object": mesh.name, "status": "skipped", "reason": "renderer m_EnableCharacterOutline is absent or not boolean"}
    if not renderer_enabled:
        return None, {"object": mesh.name, "status": "skipped", "reason": "renderer m_EnableCharacterOutline is false"}
    if mesh.hide_render:
        return None, {"object": mesh.name, "status": "skipped", "reason": "source renderer is hidden from render"}
    if any(obj.get("outline_candidate_source") == mesh.name for obj in scene.objects):
        return None, {"object": mesh.name, "status": "skipped", "reason": "source renderer already has an outline candidate"}
    configured, skipped = _source_material_slots(mesh)
    if not configured:
        return None, {"object": mesh.name, "status": "skipped", "reason": "no supported source material slot has a usable outline", "slots": skipped}
    if not mesh.data.uv_layers.get("UV0") and any(bool(row["source"]["mask_enabled"]) or row["source"]["base_texture"] is not None for row in configured):
        return None, {"object": mesh.name, "status": "skipped", "reason": "source texture inputs require missing UV0", "slots": skipped}
    widths = tuple(
        float(width)
        for row in configured
        for width in cast(list[object], row["source_widths"])
    )
    source_width = max(widths)
    shell = mesh.copy()
    shell.data = mesh.data
    shell.name = "EF Outline Approx " + mesh.name
    for collection in mesh.users_collection:
        collection.objects.link(shell)
    if not mesh.users_collection:
        bpy.context.scene.collection.objects.link(shell)
    world_matrix = mesh.matrix_world.copy()
    shell.parent = mesh.parent
    shell.matrix_parent_inverse = mesh.matrix_parent_inverse.copy()
    shell.matrix_world = world_matrix
    shell.hide_render = mesh.hide_render
    shell.hide_viewport = mesh.hide_viewport
    # Ink shells must not cast shadows or alter indirect surface lighting.
    shell.visible_shadow = False
    shell.visible_diffuse = False
    shell.visible_glossy = False
    shell.visible_transmission = False
    shell.visible_volume_scatter = False
    for slot in shell.material_slots:
        slot.link = "OBJECT"
        slot.material = transparent
    material_rows: list[dict[str, object]] = []
    for row in configured:
        slot_index = int(cast(int, row["slot"]))
        config = cast(dict[str, object], row["source"])
        outline_material = _outline_material(mesh.name, slot_index, config)
        shell.material_slots[slot_index].material = outline_material
        material_rows.append({
            "slot": slot_index,
            "material": row["material"],
            "source_shader": config["shader"],
            "source_width": config["source_width"],
            "source_widths": row["source_widths"],
            "source_offset_z": config["source_offset_z"],
            "source_average_normal": config["source_average_normal"],
            "mask_enabled": config["mask_enabled"],
            "mask_status": "source UV0/ST image sampled" if config["mask_enabled"] else "disabled in source",
            "alpha_test_enabled": config["alpha_test_enabled"],
            "alpha_threshold": config["alpha_threshold"],
            "alpha_clip_status": "host greater-than threshold approximation" if config["alpha_test_enabled"] else "disabled in source",
            "source_pass_count": row["source_pass_count"],
            "source_pass_appearance_match": row["source_pass_appearance_match"],
            "source_pass_style_status": (
                "all enabled passes share source color and texture inputs"
                if row["source_pass_appearance_match"]
                else "first enabled pass supplies color and masks; source pass blending is not reproduced"
            ),
            "host_width_mode": "maximum enabled source width shared by this renderer",
            "host_color_mode": (
                "source BaseMap × BaseColor with brightness/saturation controls"
                if config["use_tint_controls"]
                else "source _OutlineTintColor direct"
            ),
        })
    modifier = shell.modifiers.new("EF Source Outline Hull - APPROXIMATION", "NODES")
    modifier.node_group = geometry_group
    source_width_socket = next(
        item for item in geometry_group.interface.items_tree
        if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == "Source Outline Width"
    )
    meters_socket = next(
        item for item in geometry_group.interface.items_tree
        if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == "Artistic Meters per Source Width"
    )
    modifier[source_width_socket.identifier] = source_width
    modifier[meters_socket.identifier] = meters_per_source_width
    shell["outline_candidate_source"] = mesh.name
    shell["outline_candidate_contract"] = CONTRACT
    shell["outline_candidate_scope"] = SCOPE
    row_result = {
        "object": mesh.name,
        "outline_object": shell.name,
        "status": "installed",
        "renderer_enable": renderer_enabled,
        "source_widths": list(widths),
        "shared_source_width": source_width,
        "artistic_meters_per_source_width": meters_per_source_width,
        "host_width_m": source_width * meters_per_source_width,
        "width_note": "When enabled source passes differ in width, the maximum width is shared across the renderer; this avoids material-boundary geometry seams.",
        "materials": material_rows,
        "skipped_slots": skipped,
    }
    return shell, row_result


def attach_character_outline_hulls(
    scene: bpy.types.Scene,
    artistic_meters_per_source_width: float,
) -> dict[str, object]:
    """Add a source-gated editable hull copy to eligible visible mesh renderers."""
    source_meshes = tuple(
        obj for obj in scene.objects
        if obj.type == "MESH" and obj.get("source_renderer_pipeline_json") is not None
        and obj.get("outline_candidate_contract") is None
    )
    return attach_selected_outline_hulls(scene, source_meshes, artistic_meters_per_source_width)


def attach_selected_outline_hulls(
    scene: bpy.types.Scene,
    source_meshes: Sequence[bpy.types.Object],
    artistic_meters_per_source_width: float,
) -> dict[str, object]:
    """Install outlines only on the caller's explicit source-renderer selection."""
    if not math.isfinite(artistic_meters_per_source_width) or artistic_meters_per_source_width <= 0.0:
        raise ValueError(f"Artistic meters per source width must be positive and finite: {artistic_meters_per_source_width}")
    if not source_meshes:
        raise ValueError("Scene has no source renderer metadata to inspect for outline eligibility")
    if len({obj.as_pointer() for obj in source_meshes}) != len(source_meshes):
        raise ValueError("Outline source selection contains duplicate objects")
    for obj in source_meshes:
        if obj.type != "MESH" or scene.objects.get(obj.name) != obj or obj.get("outline_candidate_contract") is not None:
            raise ValueError(f"Outline selection must contain original scene mesh objects: {obj.name}")
    transparent = _transparent_material()
    geometry_group = _outline_geometry_group()
    rows: list[dict[str, object]] = []
    for mesh in source_meshes:
        _, row = _mesh_outline_row(scene, mesh, artistic_meters_per_source_width, transparent, geometry_group)
        rows.append(row)
    installed = sum(row["status"] == "installed" for row in rows)
    if installed == 0:
        raise ValueError(f"No source outline hulls were installed; candidates={rows}")
    return {
        "format": CONTRACT,
        "status": "candidate_installed",
        "scope": SCOPE,
        "artistic_meters_per_source_width": artistic_meters_per_source_width,
        "installed_count": installed,
        "rows": rows,
    }
