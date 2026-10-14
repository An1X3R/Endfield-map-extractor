"""Source shallow-water coverage with explicit Blender depth/background substitutes."""
from __future__ import annotations

import bpy

from blender_scene_effect_layers import _math
from blender_scene_materials import layout_shader_node_tree
from endfield_water_transport import WaterEdgeBlend


def water_edge_blend_group() -> bpy.types.NodeTree:
    """Expose the source terminal coverage independently of host ray reconstruction."""
    name = "EF_Water_Source_Edge_Blend_v1"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_contract") != "SourceWaterEdgeBlend/1":
            raise ValueError(f"Water edge group conflicts with an existing node group: {name}")
        return existing
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    tree["endfield_contract"] = "SourceWaterEdgeBlend/1"
    for label in ("Depth", "View Distance", "View Normal Cosine", "Distance Edge Blend Factor"):
        tree.interface.new_socket(name=label, in_out="INPUT", socket_type="NodeSocketFloat")
    tree.interface.new_socket(name="Coverage", in_out="OUTPUT", socket_type="NodeSocketFloat")
    entry = tree.nodes.new("NodeGroupInput")
    source = entry.outputs
    distance = _math(tree, "Source camera distance scale", "MULTIPLY", source["View Distance"], 0.05000000074505806)
    weight = _math(tree, "Source distance clamp", "MINIMUM", distance, 1.0)
    cosine = _math(tree, "Source absolute facing", "ABSOLUTE", source["View Normal Cosine"], 0.0)
    difference = _math(tree, "Source facing difference", "SUBTRACT", cosine, 1.0)
    angle = _math(tree, "Source angular blend", "ADD", 1.0,
                  _math(tree, "Source weighted facing", "MULTIPLY", difference, weight))
    depth = _math(tree, "Source edge depth cap", "MINIMUM", source["Depth"], 10000.0)
    coverage = _math(tree, "Source edge factor", "MULTIPLY", source["Distance Edge Blend Factor"],
                     _math(tree, "Source facing depth", "MULTIPLY", depth, angle))
    coverage = _math(tree, "Source coverage maximum", "MINIMUM", 1.0,
                     _math(tree, "Source coverage minimum", "MAXIMUM", coverage, 0.0))
    output = tree.nodes.new("NodeGroupOutput")
    tree.links.new(coverage, output.inputs["Coverage"])
    for node in tree.nodes:
        node["endfield_layout_section"] = "01 Source Shallow Water Coverage"
    layout_shader_node_tree(tree)
    return tree


def connect_water_edge_blend(material: bpy.types.Material, normal: bpy.types.NodeSocket,
                             depth: bpy.types.NodeSocket, settings: WaterEdgeBlend) -> bpy.types.Node:
    """Blend the existing surface with unattenuated host refracted background.

    Depth remains the measured vertical host depth, not the native screen-hit ray.
    Camera distance is Blender's camera distance, including its orthographic policy.
    The source upload's final subtraction slot is zero; no artistic substitute is used.
    """
    tree = material.node_tree
    if tree is None or normal.node.id_data != tree or depth.node.id_data != tree:
        raise ValueError("Water edge normal and depth must belong to the material node tree")
    if material.get("endfield_edge_blend_contract") is not None:
        raise ValueError("Water material already has source edge coverage")
    outputs = [node for node in tree.nodes if node.type == "OUTPUT_MATERIAL" and node.is_active_output]
    refractions = [node for node in tree.nodes if node.type == "BSDF_REFRACTION"
                   and node.label == "Single boundary: source transmission applied once"]
    if len(outputs) != 1 or len(refractions) != 1 or not outputs[0].inputs["Surface"].is_linked:
        raise ValueError("Water edge blend requires one connected source transport material output")
    previous = outputs[0].inputs["Surface"].links[0].from_socket
    start = set(tree.nodes)
    coverage = tree.nodes.new("ShaderNodeGroup")
    coverage.node_tree = water_edge_blend_group()
    coverage.name = "EF_Water_Source_Edge_Coverage"
    coverage.inputs["Distance Edge Blend Factor"].default_value = settings.distance_edge_blend_factor
    tree.links.new(depth, coverage.inputs["Depth"])
    camera = tree.nodes.new("ShaderNodeCameraData")
    camera.label = "Host camera distance / native orthographic policy differs"
    tree.links.new(camera.outputs["View Distance"], coverage.inputs["View Distance"])
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    dot = tree.nodes.new("ShaderNodeVectorMath")
    dot.operation = "DOT_PRODUCT"
    tree.links.new(geometry.outputs["Incoming"], dot.inputs[0])
    tree.links.new(normal, dot.inputs[1])
    tree.links.new(dot.outputs["Value"], coverage.inputs["View Normal Cosine"])
    background = tree.nodes.new("ShaderNodeBsdfRefraction")
    background.label = "Host refracted background before source water coverage"
    background.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    for label in ("IOR", "Roughness"):
        background.inputs[label].default_value = refractions[0].inputs[label].default_value
    tree.links.new(normal, background.inputs["Normal"])
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.label = "Source shallow-water background / restored surface"
    tree.links.new(coverage.outputs["Coverage"], mix.inputs[0])
    tree.links.new(background.outputs[0], mix.inputs[1])
    tree.links.new(previous, mix.inputs[2])
    shadow = tree.nodes.new("ShaderNodeLightPath")
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    adapter = tree.nodes.new("ShaderNodeMixShader")
    adapter.label = "Preserve open-water shadow transport"
    tree.links.new(shadow.outputs["Is Shadow Ray"], adapter.inputs[0])
    tree.links.new(mix.outputs[0], adapter.inputs[1])
    tree.links.new(transparent.outputs[0], adapter.inputs[2])
    tree.links.new(adapter.outputs[0], outputs[0].inputs["Surface"])
    for node in set(tree.nodes) - start:
        node["endfield_layout_section"] = "09 Source Edge Coverage"
    material["endfield_edge_blend_contract"] = "SourceWaterEdgeBlend/1"
    material["endfield_edge_blend_boundary"] = (
        "Source coverage and serialized DistanceEdgeBlendFactor; measured vertical depth, "
        "Blender camera distance and unattenuated host refraction replace native screen-depth/color sampling.")
    layout_shader_node_tree(tree)
    return coverage
