"""Portable sweep core previews from verified HGRP/Lit compiled variants."""
from __future__ import annotations

import math
import json
from typing import NamedTuple

import bpy

from endfield_scene_material_contract import MaterialRecord
from endfield_shader_contract import has_verified_lit_alpha_clip


class LitSweep(NamedTuple):
    speed: float
    interval: float
    width: float
    falloff: float
    height_offset: float
    random: float
    albedo_scale: float
    albedo_affect: float


def lit_sweep_values(record: MaterialRecord) -> LitSweep:
    """Require every serialized value used by the verified sweep expression."""
    names = ("_EmissiveSweepSpeed", "_EmissiveSweepInterval", "_EmissiveSweepWidth",
             "_EmissiveSweepFalloff", "_EmissiveSweepHeightOffset", "_EmissiveSweepRandom",
             "_EmissiveSweepAlbedoScale", "_AlbedoAffectEmissive")
    floats = record["Floats"]
    missing = tuple(name for name in names if name not in floats)
    if missing:
        raise ValueError(f"Lit emission sweep lacks serialized fields: material={record['Name']}, fields={missing}")
    values = tuple(float(floats[name]) for name in names)
    if not all(math.isfinite(value) for value in values) or values[1] <= 0 or values[2] <= 0:
        raise ValueError(f"Invalid Lit emission sweep values: material={record['Name']}, values={dict(zip(names, values))}")
    return LitSweep(*values)


def _math(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks, operation: str,
          left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float,
          label: str) -> bpy.types.NodeSocket:
    node = nodes.new("ShaderNodeMath")
    node.operation = operation
    node.label = label
    for index, value in enumerate((left, right)):
        if isinstance(value, (float, int)):
            node.inputs[index].default_value = value
        else:
            links.new(value, node.inputs[index])
    return node.outputs[0]


def _scene_time(nodes: bpy.types.Nodes, source_field: str) -> bpy.types.NodeSocket:
    scene = bpy.context.scene
    if scene is None:
        raise ValueError("Lit emission sweep requires an active Blender scene for preview time")
    node = nodes.new("ShaderNodeValue")
    node.label = f"{source_field}: Blender seconds proxy; game phase unavailable"
    curve = node.outputs[0].driver_add("default_value")
    curve.driver.expression = "frame * fps_base / fps"
    # Preserve Blender's continuous frame input, including render subframes.
    for name, path in (("fps", "render.fps"), ("fps_base", "render.fps_base")):
        variable = curve.driver.variables.new()
        variable.name = name
        variable.targets[0].id_type = "SCENE"
        variable.targets[0].id = scene
        variable.targets[0].data_path = path
    return node.outputs[0]


def _interaction_preview_gate(nodes: bpy.types.Nodes) -> bpy.types.NodeSocket:
    """Expose the native 0/1 hint gate as an explicit Blender preview control."""
    scene = bpy.context.scene
    if scene is None:
        raise ValueError("Lit emission sweep requires an active scene for the interaction preview gate")
    property_name = "endfield_interaction_sweep_preview"
    if property_name not in scene:
        scene[property_name] = 1.0
        scene.id_properties_ui(property_name).update(
            min=0.0, max=1.0,
            description="Interactive hint sweep preview: 0 off, 1 on. Authored preview setting; game interaction state is unavailable.")
    value = scene[property_name]
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"Invalid interaction sweep preview strength: value={value}; expected finite 0..1")
    node = nodes.new("ShaderNodeValue")
    node.label = "Interactive hint preview: 0 off / 1 on; game state unknown"
    curve = node.outputs[0].driver_add("default_value")
    variable = curve.driver.variables.new()
    variable.name = "preview_gate"
    variable.targets[0].id_type = "SCENE"
    variable.targets[0].id = scene
    variable.targets[0].data_path = f'["{property_name}"]'
    curve.driver.expression = "preview_gate"
    return node.outputs[0]


def _emissive_uv_time(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks) -> bpy.types.NodeSocket:
    """Match VFXParams0.w's native fmod(Time.y, 1024) with an explicit clock proxy."""
    time = _scene_time(nodes, "_Time.y")
    return _math(nodes, links, "MODULO", time, 1024.0,
                 "_VFXParams0.w: native time remainder over 1024 seconds")


def _color_multiply(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks,
                    left: bpy.types.NodeSocket, right: bpy.types.NodeSocket,
                    label: str) -> bpy.types.NodeSocket:
    node = nodes.new("ShaderNodeMixRGB")
    node.blend_type = "MULTIPLY"
    node.inputs[0].default_value = 1.0
    node.label = label
    links.new(left, node.inputs[1])
    links.new(right, node.inputs[2])
    return node.outputs[0]


def _sweep_intensity(material: bpy.types.Material, record: MaterialRecord,
                     albedo: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Build the shared compiled camera-Y sweep intensity."""
    values = lit_sweep_values(record)
    nodes, links = material.node_tree.nodes, material.node_tree.links
    geometry = nodes.new("ShaderNodeNewGeometry")
    object_info = nodes.new("ShaderNodeObjectInfo")
    object_info.name = "EF_SweepObjectOrigin"
    relative = nodes.new("ShaderNodeVectorMath")
    relative.operation = "SUBTRACT"
    relative.label = "World position relative to object origin"
    links.new(geometry.outputs["Position"], relative.inputs[0])
    links.new(object_info.outputs["Location"], relative.inputs[1])
    view = nodes.new("ShaderNodeVectorTransform")
    view.vector_type = "VECTOR"
    view.convert_from = "WORLD"
    view.convert_to = "CAMERA"
    view.label = "ViewMatrix rotation; preview camera proxy"
    links.new(relative.outputs["Vector"], view.inputs["Vector"])
    camera_position = nodes.new("ShaderNodeSeparateXYZ")
    links.new(view.outputs["Vector"], camera_position.inputs[0])
    time = _scene_time(nodes, "_TimeParameters.w")
    phase_time = time
    if values.random != 0.0:
        origin = nodes.new("ShaderNodeSeparateXYZ")
        links.new(object_info.outputs["Location"], origin.inputs[0])
        offset = _math(nodes, links, "MULTIPLY", origin.outputs["X"], values.random,
                       "Sweep random by object world X")
        phase_time = _math(nodes, links, "ADD", time, offset, "Sweep randomized time")
    cycles = _math(nodes, links, "DIVIDE", phase_time, values.interval, "Sweep cycle count")
    truncated = _math(nodes, links, "TRUNC", cycles, 0.0, "Sweep truncation")
    elapsed = _math(nodes, links, "SUBTRACT", phase_time,
                    _math(nodes, links, "MULTIPLY", truncated, values.interval,
                          "Completed sweep cycles"), "Time within sweep cycle")
    centered = _math(nodes, links, "SUBTRACT", elapsed, values.interval * 0.3,
                     "Sweep cycle center")
    height = _math(nodes, links, "ADD",
                   _math(nodes, links, "MULTIPLY", centered, values.speed,
                         "Sweep travel speed"), values.height_offset, "Sweep height offset")
    distance = _math(nodes, links, "ABSOLUTE",
                     _math(nodes, links, "SUBTRACT", camera_position.outputs["Y"], height,
                           "Distance from sweep band"), 0.0, "Sweep absolute distance")
    normalized = _math(nodes, links, "MINIMUM",
                       _math(nodes, links, "DIVIDE", distance, values.width,
                             "Sweep width"), 1.0, "Sweep width clamp")
    band = _math(nodes, links, "MINIMUM",
                 _math(nodes, links, "MAXIMUM",
                       _math(nodes, links, "MULTIPLY",
                             _math(nodes, links, "SUBTRACT", 1.0, normalized,
                                   "Sweep inverse distance"), values.falloff,
                             "Sweep falloff"), 0.0, "Sweep lower clamp"),
                 1.0, "Sweep upper clamp")
    squared = _math(nodes, links, "MULTIPLY", band, band, "Squared sweep mask")

    mean = nodes.new("ShaderNodeVectorMath")
    mean.operation = "DOT_PRODUCT"
    mean.label = "HGRP mean albedo RGB"
    mean.inputs[1].default_value = (0.3330000042915344, 0.3330000042915344,
                                    0.3330000042915344)
    links.new(albedo, mean.inputs[0])
    brightness = _math(nodes, links, "MAXIMUM",
                       _math(nodes, links, "MULTIPLY",
                             _math(nodes, links, "ADD", 0.2,
                                   _math(nodes, links, "MULTIPLY",
                                         _math(nodes, links, "SUBTRACT", mean.outputs["Value"], 0.2,
                                               "Albedo above sweep floor"), values.albedo_scale,
                                         "Sweep albedo scale"), "Sweep albedo floor"),
                             5.0, "Sweep albedo brightness"), 0.0, "Sweep brightness clamp")
    intensity = _math(nodes, links, "MULTIPLY", squared, brightness,
                      "HGRP sweep emission intensity")
    return _math(nodes, links, "MULTIPLY", intensity, _interaction_preview_gate(nodes),
                 "Interactive hint preview gate; static emission is preserved")


def preserve_projected_sweep_origin(material: bpy.types.Material) -> None:
    """Read each projected triangle's receiver origin instead of the decal origin."""
    if material.get("endfield_emissive_motion_status") not in (
            "source_formula_preview_runtime_unresolved", "blender_approximation_runtime_unresolved"):
        return
    nodes, links = material.node_tree.nodes, material.node_tree.links
    origin = nodes.get("EF_SweepObjectOrigin")
    if origin is None or origin.type != "OBJECT_INFO":
        raise ValueError(f"Projected sweep lacks its receiver-origin node: material={material.name}")
    attribute = nodes.new("ShaderNodeAttribute")
    attribute.attribute_type = "GEOMETRY"
    attribute.attribute_name = "endfield_receiver_world_origin"
    attribute.label = "Sweep origin of the original decal receiver"
    for link in tuple(origin.outputs["Location"].links):
        target = link.to_socket
        links.remove(link)
        links.new(attribute.outputs["Vector"], target)
    nodes.remove(origin)
    material["endfield_emissive_coordinate_status"] = "view_y_relative_to_projected_receiver_origin_blender_camera_proxy"


def mark_lit_sweep_approximation(material: bpy.types.Material, record: MaterialRecord) -> None:
    """Separate the reused Lit core from evidence about the selected source variant."""
    state = record.get("RenderState")
    keywords = sorted(state["ValidKeywords"]) if state is not None else []
    features = [keyword for keyword in keywords if keyword not in (
        "_EMISSIVE_ANIM_SWEEP", "_EMISSIVE_MAP", "_EMISSIVE_NOMAP", "_EMISSIVE_MASK")]
    material["endfield_emissive_motion_status"] = "blender_approximation_runtime_unresolved"
    material["endfield_emissive_motion_reason"] = (
        "shared_lit_sweep_preview_variant_surface_and_draw_inputs_unverified")
    material["endfield_emissive_motion_keywords"] = json.dumps(keywords)
    material["endfield_emissive_approximation_features"] = json.dumps(features)
    material["endfield_emissive_source_pass"] = "unverified_variant_shared_lit_core"
    material["endfield_emissive_albedo_status"] = "authored_material_albedo_shared_lit_core_approximation"
    material["endfield_emissive_albedo_enable_status"] = "source_values_shared_lit_core_approximation"
    if record["Floats"]["_EmissiveMaskChannel"] in (1, 2, 3):
        material["endfield_emissive_remap_status"] = "shared_lit_packed_mask_remap_approximation"
    values = lit_sweep_values(record)
    material["endfield_emissive_sweep_source_values"] = json.dumps(values._asdict())
    material["endfield_emissive_preview_status"] = (
        "authored_sweep_values_blender_camera_and_clock_variant_formula_unverified")


def apply_lit_sweep(material: bpy.types.Material, record: MaterialRecord,
                    emission_color: bpy.types.NodeSocket,
                    albedo: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Build the camera-Y sweep core shared by 1063/1064 and alpha-tested 343."""
    nodes, links = material.node_tree.nodes, material.node_tree.links
    intensity = _sweep_intensity(material, record, albedo)
    intensity_color = nodes.new("ShaderNodeCombineColor")
    intensity_color.label = "Sweep RGB intensity"
    for input_socket in intensity_color.inputs[:3]:
        links.new(intensity, input_socket)
    result = _color_multiply(nodes, links, emission_color,
                             intensity_color.outputs[0], "EmissiveColor times sweep")
    return _apply_sweep_albedo(material, record, result, albedo)


def _apply_sweep_albedo(material: bpy.types.Material, record: MaterialRecord,
                        result: bpy.types.NodeSocket, albedo: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    nodes, links = material.node_tree.nodes, material.node_tree.links
    values = lit_sweep_values(record)
    influence = nodes.new("ShaderNodeMixRGB")
    influence.label = "AlbedoAffectEmissive: 1 ignores albedo"
    influence.inputs[0].default_value = values.albedo_affect
    influence.inputs[2].default_value = (1.0, 1.0, 1.0, 1.0)
    links.new(albedo, influence.inputs[1])
    result = _color_multiply(nodes, links, result, influence.outputs[0],
                             "HGRP emission albedo influence")
    material["endfield_emissive_motion_status"] = "source_formula_preview_runtime_unresolved"
    alpha_core = has_verified_lit_alpha_clip(record)
    material["endfield_emissive_motion_reason"] = (
        "gbuffer_sweep_core_preview_draw_color_fade_and_exposure_runtime_unresolved" if alpha_core
        else "reflection_pass_formula_preview_main_pass_instance_inputs_unavailable")
    material["endfield_emissive_source_pass"] = "HGBuffer" if alpha_core else "RayTracingReflection"
    material["endfield_emissive_interaction_gate_status"] = "native_enable_disable_0_1_with_explicit_scene_preview_control"
    material["endfield_emissive_coordinate_status"] = "view_y_relative_to_object_origin_blender_camera_proxy"
    material["endfield_emissive_global_time_status"] = "game_global_phase_unavailable_blender_scene_seconds"
    material["endfield_emissive_sweep_clock_source"] = "HGRPTimeManager.gameplayTime"
    material["endfield_emissive_exposure_status"] = "game_global_post_exposure_unavailable_blender_color_management"
    material["endfield_emissive_albedo_status"] = (
        "verified_343_sweep_core_albedo_affect_and_scale" if alpha_core
        else "verified_1063_1064_albedo_affect_and_sweep_scale")
    return result


def apply_lit_mask_sweep(material: bpy.types.Material, record: MaterialRecord,
                         mask: bpy.types.NodeSocket, albedo: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Build variant 1062 packed-mask remapping and tint-alpha sweep weighting."""
    nodes, links = material.node_tree.nodes, material.node_tree.links
    tint = record["Colors"].get("_EmissiveColor")
    enable = record["Floats"].get("_EnableEmissiveMap")
    if (not isinstance(tint, list) or len(tint) != 4 or
            any(not math.isfinite(value) for value in tint) or
            type(enable) not in (int, float) or not math.isfinite(enable)):
        raise ValueError(f"Masked sweep requires finite RGBA tint and emissive enable: material={record['Name']}")
    if record["Floats"].get("_EmissiveMaskChannel") not in (1, 2, 3):
        raise ValueError(f"Unverified packed sweep mask selector: material={record['Name']}")
    remapped = _math(nodes, links, "MINIMUM",
                     _math(nodes, links, "MAXIMUM",
                           _math(nodes, links, "SUBTRACT",
                                 _math(nodes, links, "MULTIPLY", mask, 1.1111111640930176,
                                       "Packed emission mask scale"), 0.055555559694767,
                                 "Packed emission mask offset"), 0.0, "Packed mask lower clamp"),
                     1.0, "Packed mask upper clamp")
    intensity = _sweep_intensity(material, record, albedo)
    weight = _math(nodes, links, "ADD", 1.0 - tint[3],
                   _math(nodes, links, "MULTIPLY", intensity, tint[3], "Tint alpha sweep weight"),
                   "Packed emission static and animated mix")
    factor = _math(nodes, links, "MULTIPLY",
                   _math(nodes, links, "MULTIPLY", remapped, weight, "Packed mask times sweep"),
                   float(enable), "Source emission enable")
    emission = nodes.new("ShaderNodeVectorMath")
    emission.operation = "SCALE"
    emission.label = "Packed-mask authored emission color"
    emission.inputs[0].default_value = tint[:3]
    links.new(factor, emission.inputs["Scale"])
    result = _apply_sweep_albedo(material, record, emission.outputs[0], albedo)
    material["endfield_emissive_albedo_status"] = "verified_1062_albedo_affect_and_tint_alpha_sweep"
    material["endfield_emissive_remap_status"] = "compiled_1062_packed_mask_remap"
    material["endfield_emissive_sweep_weight"] = tint[3]
    return result


def apply_lit_map_sweep(material: bpy.types.Material, record: MaterialRecord,
                        texture: bpy.types.Node, albedo: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Build variants 1059/1060 with independently weighted RGBA emission bands."""
    nodes, links = material.node_tree.nodes, material.node_tree.links
    floats = record["Floats"]
    if floats.get("_EmissiveUVSet") != 0 or floats.get("_EmissiveType") not in (0, 1):
        raise ValueError(f"Unverified mapped sweep UV/channel mode: material={record['Name']}")
    speed = record["Colors"].get("_EmissiveSpeed")
    tiling = floats.get("_EmissiveMapTilling")
    if (not isinstance(speed, list) or len(speed) != 4 or
            any(not math.isfinite(value) for value in speed) or
            type(tiling) not in (int, float) or not math.isfinite(tiling)):
        raise ValueError(f"Mapped sweep requires finite source UV speed and tiling: material={record['Name']}")
    bindings = [slot for slot in record["Textures"] if slot["Property"] == "_EmissiveMap"]
    if len(bindings) != 1:
        raise ValueError(f"Mapped sweep requires one emissive texture binding: material={record['Name']}")
    slot = bindings[0]
    uv = nodes.new("ShaderNodeTexCoord")
    mapping = nodes.new("ShaderNodeMapping")
    mapping.label = "Emissive UV0 source scale and offset"
    mapping.inputs["Scale"].default_value[:2] = slot["Scale"]
    mapping.inputs["Location"].default_value[:2] = slot["Offset"]
    links.new(uv.outputs["UV"], mapping.inputs["Vector"])
    time = _emissive_uv_time(nodes, links)
    movement = nodes.new("ShaderNodeCombineXYZ")
    for axis, rate in zip(("X", "Y"), speed[:2]):
        links.new(_math(nodes, links, "MULTIPLY", time, rate, f"Emissive UV {axis} speed"), movement.inputs[axis])
    offset = nodes.new("ShaderNodeVectorMath")
    offset.operation = "ADD"
    offset.label = "Emissive source ST plus time movement"
    links.new(mapping.outputs["Vector"], offset.inputs[0])
    links.new(movement.outputs[0], offset.inputs[1])
    scale = nodes.new("ShaderNodeVectorMath")
    scale.operation = "SCALE"
    scale.label = "Emissive tiling after ST and movement"
    links.new(offset.outputs[0], scale.inputs[0])
    scale.inputs["Scale"].default_value = tiling
    links.new(scale.outputs[0], texture.inputs["Vector"])
    intensity = _sweep_intensity(material, record, albedo)
    separate = nodes.new("ShaderNodeSeparateColor")
    links.new(texture.outputs["Color"], separate.inputs["Color"])
    accumulated = None
    for channel, name in (("Red", "_EmissiveColor"), ("Green", "_EmissiveColorG"),
                          ("Blue", "_EmissiveColorB"), ("Alpha", "_EmissiveColorA")):
        color = record["Colors"].get(name)
        if not isinstance(color, list) or len(color) != 4 or any(not math.isfinite(value) for value in color):
            raise ValueError(f"Mapped sweep requires finite RGBA tint: material={record['Name']}, field={name}")
        mask = texture.outputs["Alpha"] if channel == "Alpha" else separate.outputs[channel]
        weight = _math(nodes, links, "ADD", 1.0 - color[3],
                       _math(nodes, links, "MULTIPLY", intensity, color[3], f"{channel} sweep weight"),
                       f"{channel} static and animated mix")
        factor = _math(nodes, links, "MULTIPLY", mask, weight, f"{channel} emission mask")
        factor = _math(nodes, links, "MULTIPLY", factor,
                       1.0 if channel == "Red" else float(floats["_EmissiveType"]), f"{channel} source channel enable")
        contribution = nodes.new("ShaderNodeVectorMath")
        contribution.operation = "SCALE"
        contribution.label = f"{channel} authored emission color"
        contribution.inputs[0].default_value = color[:3]
        links.new(factor, contribution.inputs["Scale"])
        if accumulated is None:
            accumulated = contribution.outputs[0]
        else:
            addition = nodes.new("ShaderNodeVectorMath")
            addition.operation = "ADD"
            links.new(accumulated, addition.inputs[0])
            links.new(contribution.outputs[0], addition.inputs[1])
            accumulated = addition.outputs[0]
    for operation, bound in (("MAXIMUM", 0.0), ("MINIMUM", 1000.0)):
        clamp = nodes.new("ShaderNodeVectorMath")
        clamp.operation = operation
        clamp.label = "Source mapped emission clamp"
        links.new(accumulated, clamp.inputs[0])
        clamp.inputs[1].default_value = (bound, bound, bound)
        accumulated = clamp.outputs[0]
    result = _apply_sweep_albedo(material, record, accumulated, albedo)
    material["endfield_emissive_albedo_status"] = "verified_1059_1060_albedo_affect_and_per_channel_sweep"
    material["endfield_emissive_uv_status"] = "source_uv0_st_speed_tiling_native_time_mod1024_blender_time_proxy"
    return result
