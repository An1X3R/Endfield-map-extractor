"""Source foam coverage with editable Blender surface shading.

The source mask uses field B and small-wave alpha independently of UseFlowMap.
Blender supplies camera distance and surface illumination; native screen-space
lighting and the camera-relative sector-atlas edge blend are not reproduced.
"""
from __future__ import annotations

import json

import bpy

from blender_scene_materials import layout_shader_node_tree
from blender_scene_water_waves import (
    _image, _math, _mix_float, _node, _number, _record, sample_water_flow_field,
)
from endfield_scene_water_materials import WaterMaterialRow


def _clamp(tree: bpy.types.NodeTree, name: str,
           value: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    return _math(tree, name + "_Upper", "MINIMUM",
                 _math(tree, name + "_Lower", "MAXIMUM", value, 0.0), 1.0)


def _small_alpha(tree: bpy.types.NodeTree, use_flow_map: bool) -> bpy.types.NodeSocket:
    if not use_flow_map:
        node = tree.nodes.get("EF_Wave_Small_Sample")
        if node is None or node.type != "TEX_IMAGE":
            raise ValueError("Water foam requires the existing small-wave texture sample")
        return node.outputs["Alpha"]
    first = tree.nodes.get("EF_Wave_Small_0_Sample")
    second = tree.nodes.get("EF_Wave_Small_1_Sample")
    weight = tree.nodes.get("EF_Wave_Small_1_Triangle")
    if first is None or second is None or weight is None:
        raise ValueError("Water foam requires the existing dual-phase small-wave samples")
    return _mix_float(tree, "Foam_Small_Alpha", weight.outputs[0],
                      first.outputs["Alpha"], second.outputs["Alpha"])


def reconnect_source_water_foam(material: bpy.types.Material) -> None:
    """Retain the source mask when the underlying portable/transport surface changes."""
    tree = material.node_tree
    if tree is None:
        raise ValueError("Water foam requires a node material")
    foam = tree.nodes.get("EF_Water_Foam_Surface")
    if foam is None:
        if material.get("endfield_water_foam_status") == "source_mask_host_surface":
            raise ValueError("Water foam surface was removed after its source mask was built")
        return
    outputs = [node for node in tree.nodes if node.type == "OUTPUT_MATERIAL"]
    if not outputs or any(not node.inputs["Surface"].is_linked for node in outputs):
        raise ValueError("Water foam requires connected material surfaces for all render targets")
    if len({node.target for node in outputs}) != len(outputs):
        raise ValueError("Water foam requires one material output per render target")
    mask = tree.nodes.get("EF_Water_Foam_Coverage")
    if mask is None:
        raise ValueError("Water foam surface has no source coverage output")
    for output in outputs:
        target = output.inputs["Surface"]
        source = target.links[0].from_socket
        name = "EF_Water_Foam_Mix" + ("" if output.target == "ALL" else "_" + output.target)
        mix = tree.nodes.get(name)
        if mix is None:
            mix = tree.nodes.new("ShaderNodeMixShader")
            mix.name = name
            mix.label = "Source foam coverage / " + output.target
            mix["endfield_layout_section"] = "08 Source Foam"
        tree.links.new(mask.outputs[0], mix.inputs[0])
        tree.links.new(foam.outputs["BSDF"], mix.inputs[2])
        if source.node != mix:
            tree.links.new(source, mix.inputs[1])
            tree.links.new(mix.outputs[0], target)
    layout_shader_node_tree(tree)


def build_source_water_foam(material: bpy.types.Material, spec: WaterMaterialRow) -> str:
    """Build source coverage, or explicitly report absent legacy-cache resources."""
    tree = material.node_tree
    if tree is None or tree.nodes.get("EF_Water_Foam_Surface") is not None:
        raise ValueError("Water foam requires a node material without an existing foam branch")
    config = _record(json.loads(spec.config_source_json), "config")
    appearance = _record(config.get("Appearance"), "Appearance")
    opacity = _number(appearance.get("FoamOpacity"), "FoamOpacity")
    if opacity < 0:
        raise ValueError("Source FoamOpacity must be nonnegative")
    if opacity == 0:
        material["endfield_water_foam_status"] = "source_opacity_zero"
        return "source_opacity_zero"
    if spec.flow_map is None or not spec.sector_complete:
        material["endfield_water_foam_status"] = "pending_source_field_resources"
        material["endfield_water_foam_pending"] = "Reprepare water resources to resolve the source foam field and sectors; UseFlowMap does not disable foam."
        return "pending_source_field_resources"
    if len(spec.sectors) > 20:
        raise ValueError(f"Water foam sector sampler budget exceeded: entity={spec.entity_id}, sectors={len(spec.sectors)}")
    fade = _number(appearance.get("FoamFadeDistance"), "FoamFadeDistance")
    offset = _number(appearance.get("FoamShapeOffset"), "FoamShapeOffset")
    roughness = _number(appearance.get("FoamRoughness"), "FoamRoughness")
    color = _record(appearance.get("FoamColorG"), "FoamColorG")
    rgb = tuple(_number(color.get(channel), "FoamColorG." + channel) for channel in "RGB")
    if fade <= 0 or not 0 <= roughness <= 1 or any(value < 0 for value in rgb):
        raise ValueError(f"Invalid source foam fade, roughness or color: entity={spec.entity_id}")
    start = set(tree.nodes)
    field = tree.nodes.get("EF_Wave_Flow_RG")
    if field is None:
        world = tree.nodes.get("EF_Wave_World_XZ")
        if world is None:
            raise ValueError("Water foam requires the existing source world coordinates")
        files = {spec.flow_map.file, *(sector.texture.file for sector in spec.sectors)}
        images = {file: _image(file) for file in sorted(files)}
        global_config = _record(json.loads(spec.global_source_json), "global")
        sampled, _ = sample_water_flow_field(tree, spec, global_config, world.outputs[0], images)
        field = _node(tree, "ShaderNodeSeparateColor", "Foam_Field_RGB")
        field.mode = "RGB"
        tree.links.new(sampled, field.inputs["Color"])
    alpha = _small_alpha(tree, spec.flow.use_flow_map)
    shape = _clamp(tree, "Foam_Shape", _math(tree, "Foam_Shape_Offset", "SUBTRACT", alpha, offset))
    camera = _node(tree, "ShaderNodeCameraData", "Foam_Host_Camera")
    distance = _clamp(tree, "Foam_Distance", _math(tree, "Foam_Distance_Ratio", "DIVIDE", camera.outputs["View Distance"], fade))
    visible = _math(tree, "Foam_Distance_Fade", "SUBTRACT", 1.0, distance)
    geometry = _node(tree, "ShaderNodeNewGeometry", "Foam_Geometric_Normal")
    dot = _node(tree, "ShaderNodeVectorMath", "Foam_View_Dot_Normal")
    dot.operation = "DOT_PRODUCT"
    tree.links.new(geometry.outputs["Normal"], dot.inputs[0])
    tree.links.new(geometry.outputs["Incoming"], dot.inputs[1])
    facing = _math(tree, "Foam_Facing", "MAXIMUM", _clamp(tree, "Foam_View", dot.outputs["Value"]), 0.5)
    mask = _math(tree, "Foam_Field_Shape", "MULTIPLY", field.outputs["Blue"], shape)
    for name, value in (("Opacity", opacity), ("Distance", visible), ("Facing", facing)):
        mask = _math(tree, "Foam_Coverage_" + name, "MULTIPLY", mask, value)
    coverage = tree.nodes.new("NodeReroute")
    coverage.name = "EF_Water_Foam_Coverage"
    tree.links.new(_clamp(tree, "Foam_Host_Coverage", mask), coverage.inputs[0])
    foam = tree.nodes.new("ShaderNodeBsdfPrincipled")
    foam.name = "EF_Water_Foam_Surface"
    foam.label = "Source foam color / Blender lighting"
    foam.inputs["Base Color"].default_value = (*rgb, 1.0)
    foam.inputs["Roughness"].default_value = roughness
    for node in set(tree.nodes) - start:
        node["endfield_layout_section"] = "08 Source Foam"
    material["endfield_water_foam_status"] = "source_mask_host_surface"
    material["endfield_water_foam_boundary"] = "Source field-B/wave-alpha mask and authored foam color; Blender camera distance and Principled illumination. Native GBuffer lighting, anonymous descriptor binding and camera-relative atlas edge fade remain unverified."
    reconnect_source_water_foam(material)
    return "source_mask_host_surface"
