"""Shared HGRP decoding and Blender lighting adapters for both render engines."""
from __future__ import annotations

import bpy

from endfield_scene_material_contract import MaterialRecord
from endfield_shader_contract import surface_family, uses_mro, normal_layout


def packed_normal_group() -> bpy.types.ShaderNodeTree:
    name = "EF_HGRP_Packed_Normal_v1"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_contract") != "HGRPPackedNormal/1":
            raise ValueError(f"Conflicting packed normal group: {name}")
        return existing
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    for label, kind in (("Packed RGB", "NodeSocketColor"), ("Packed Alpha", "NodeSocketFloat"),
                        ("Multiply Red By Alpha", "NodeSocketFloat"), ("Normal Strength", "NodeSocketFloat")):
        tree.interface.new_socket(name=label, in_out="INPUT", socket_type=kind)
    tree.interface.new_socket(name="Encoded Normal", in_out="OUTPUT", socket_type="NodeSocketColor")
    tree.interface.new_socket(name="Blue", in_out="OUTPUT", socket_type="NodeSocketFloat")
    nodes, links = tree.nodes, tree.links
    entry = nodes.new("NodeGroupInput")
    output = nodes.new("NodeGroupOutput")
    split = nodes.new("ShaderNodeSeparateColor")
    links.new(entry.outputs["Packed RGB"], split.inputs["Color"])
    factor = nodes.new("ShaderNodeMixRGB")
    links.new(entry.outputs["Multiply Red By Alpha"], factor.inputs[0])
    factor.inputs[1].default_value = (1, 1, 1, 1)
    links.new(entry.outputs["Packed Alpha"], factor.inputs[2])
    red = nodes.new("ShaderNodeMath")
    red.operation = "MULTIPLY"
    links.new(split.outputs["Red"], red.inputs[0])
    links.new(factor.outputs[0], red.inputs[1])
    signed = []
    for channel in (red.outputs[0], split.outputs["Green"]):
        node = nodes.new("ShaderNodeMath")
        node.operation = "MULTIPLY_ADD"
        node.inputs[1].default_value = 2
        node.inputs[2].default_value = -1
        links.new(channel, node.inputs[0])
        signed.append(node.outputs[0])
    xy = nodes.new("ShaderNodeCombineXYZ")
    links.new(signed[0], xy.inputs[0])
    links.new(signed[1], xy.inputs[1])
    squared = nodes.new("ShaderNodeVectorMath")
    squared.operation = "DOT_PRODUCT"
    links.new(xy.outputs[0], squared.inputs[0])
    links.new(xy.outputs[0], squared.inputs[1])
    remaining = nodes.new("ShaderNodeMath")
    remaining.operation = "SUBTRACT"
    remaining.use_clamp = True
    remaining.inputs[0].default_value = 1
    links.new(squared.outputs["Value"], remaining.inputs[1])
    z = nodes.new("ShaderNodeMath")
    z.operation = "SQRT"
    links.new(remaining.outputs[0], z.inputs[0])
    encoded = nodes.new("ShaderNodeCombineColor")
    for index, component in enumerate((*signed, z.outputs[0])):
        scaled = component
        if index < 2:
            strength = nodes.new("ShaderNodeMath")
            strength.operation = "MULTIPLY"
            links.new(component, strength.inputs[0])
            links.new(entry.outputs["Normal Strength"], strength.inputs[1])
            scaled = strength.outputs[0]
        node = nodes.new("ShaderNodeMath")
        node.operation = "MULTIPLY_ADD"
        node.inputs[1].default_value = 0.5
        node.inputs[2].default_value = 0.5
        links.new(scaled, node.inputs[0])
        links.new(node.outputs[0], encoded.inputs[index])
    links.new(encoded.outputs[0], output.inputs["Encoded Normal"])
    links.new(split.outputs["Blue"], output.inputs["Blue"])
    tree["endfield_contract"] = "HGRPPackedNormal/1"
    return tree


def remap_roughness(material: bpy.types.Material, source: bpy.types.NodeSocket,
                    target: bpy.types.NodeSocket, record: MaterialRecord) -> None:
    node = material.node_tree.nodes.new("ShaderNodeMapRange")
    node.label = "HGRP authored roughness range"
    node.clamp = True
    node.inputs["To Min"].default_value = record["Floats"].get("_RoughnessMin", 0.0)
    node.inputs["To Max"].default_value = record["Floats"].get("_RoughnessMax", 1.0)
    material.node_tree.links.new(source, node.inputs["Value"])
    material.node_tree.links.new(node.outputs["Result"], target)


def apply_authored_albedo(material: bpy.types.Material, bsdf: bpy.types.Node,
                          record: MaterialRecord) -> None:
    nodes, links = material.node_tree.nodes, material.node_tree.links
    base = bsdf.inputs["Base Color"]
    if not base.links:
        return
    incoming = base.links[0].from_socket
    brightness = nodes.new("ShaderNodeMixRGB")
    brightness.label = "HGRP BaseColorBrighterScale (saturate)"
    brightness.blend_type = "MULTIPLY"
    brightness.use_clamp = True
    brightness.inputs[0].default_value = 1
    value = record["Floats"].get("_BaseColorBrighterScale", 1.0)
    brightness.inputs[2].default_value = (value, value, value, 1)
    links.new(incoming, brightness.inputs[1])
    cover = nodes.new("ShaderNodeMixRGB")
    cover.label = "HGRP BaseColorTintCover"
    cover.inputs[0].default_value = record["Floats"].get("_BaseColorTintCover", 0.0)
    cover.inputs[2].default_value = record["Colors"].get("_BaseColor", [1, 1, 1, 1])
    links.new(brightness.outputs[0], cover.inputs[1])
    links.new(cover.outputs[0], base)


def apply_engine_surface(material: bpy.types.Material, bsdf: bpy.types.Node,
                         record: MaterialRecord) -> None:
    """Keep one editable surface, with explicit engine outputs for transmission."""
    family = surface_family(record)
    material["endfield_shader_family"] = family
    material["endfield_surface_contract"] = "EndfieldHGRPSurface/2"
    material["endfield_render_pipeline_status"] = "verified_texture_decode_blender_lighting_adapter"
    material["endfield_surface_fidelity"] = "verified_core_with_explicit_blender_approximations"
    material["endfield_supported_engines"] = "CYCLES,BLENDER_EEVEE_NEXT"
    material["endfield_normal_layout"] = normal_layout(record)
    material["endfield_mro_interpretation"] = (
        "Active separate MRO: R metallic, G authored-range roughness, B occlusion retained"
        if uses_mro(record) else "Separate MRO inactive; packed normal B when enabled, otherwise authored roughness"
    )
    if family in ("leaf", "card"):
        add_foliage_transmission(material, bsdf, record)
    if family != "transparent" or record["Floats"].get("_UseRefraction", 0) <= 0.5:
        return
    # Screen colour/depth sampling has no portable shader-node equivalent.
    # Use the engines' native scene transmission instead of reusing alpha as glass.
    bsdf.inputs["Transmission Weight"].default_value = max(0.0, min(1.0, record["Floats"].get("_RefractionContribution", 1.0)))
    bsdf.inputs["IOR"].default_value = 1.45
    material.use_raytrace_refraction = True
    material.surface_render_method = "DITHERED"
    material.thickness_mode = "SLAB"
    outputs = [n for n in material.node_tree.nodes if n.type == "OUTPUT_MATERIAL"]
    if len(outputs) != 1:
        raise ValueError(f"Expected one source surface output: material={material.name}")
    outputs[0].target = "CYCLES"
    outputs[0].name = "EF_Output_Cycles"
    eevee = material.node_tree.nodes.new("ShaderNodeOutputMaterial")
    eevee.target = "EEVEE"
    eevee.name = "EF_Output_Eevee"
    eevee.inputs["Thickness"].default_value = max(0.0, record["Floats"].get("_RefractThickness", 0.01))
    material.node_tree.links.new(bsdf.outputs["BSDF"], eevee.inputs["Surface"])
    material["endfield_refraction_adapter"] = "native transmission; IOR=1.45 is a Blender approximation; authored screen distortion is not an IOR"


def add_foliage_transmission(material: bpy.types.Material, bsdf: bpy.types.Node,
                             record: MaterialRecord) -> None:
    nodes, links = material.node_tree.nodes, material.node_tree.links
    normal_texture = next((n for n in nodes if n.type == "TEX_IMAGE" and n.label == "_NormalMap"), None)
    strength = nodes.new("ShaderNodeMath")
    strength.operation = "MULTIPLY_ADD"
    strength.label = "HGRP thin-leaf transmission plus subsurface proxy"
    strength.inputs[1].default_value = record["Floats"].get("_Transmission", 0.0)
    strength.inputs[2].default_value = record["Floats"].get("_SubsurfaceIntensity", 0.0)
    if normal_texture is not None:
        inverse = nodes.new("ShaderNodeMath")
        inverse.operation = "SUBTRACT"
        inverse.inputs[0].default_value = 1.0
        links.new(normal_texture.outputs["Alpha"], inverse.inputs[1])
        links.new(inverse.outputs[0], strength.inputs[0])
    else:
        strength.inputs[0].default_value = 1.0
    strength.use_clamp = True
    coverage = nodes.new("ShaderNodeMath")
    coverage.operation = "MULTIPLY"
    coverage.name = "EF_FoliageCoverage"
    links.new(strength.outputs[0], coverage.inputs[0])
    translucent = nodes.new("ShaderNodeBsdfTranslucent")
    translucent.name = "EF_FoliageTranslucent"
    blend = nodes.new("ShaderNodeMixShader")
    blend.label = "Portable thin-leaf lighting approximation"
    links.new(coverage.outputs[0], blend.inputs[0])
    links.new(bsdf.outputs[0], blend.inputs[1])
    links.new(translucent.outputs[0], blend.inputs[2])
    output = material.node_tree.get_output_node("ALL")
    links.new(blend.outputs[0], output.inputs["Surface"])
    refresh_surface_adapters(material)
    material["endfield_foliage_adapter"] = "Translucent BSDF approximates authored transmission and subsurface; game AO/wrap lighting and separate diffuse normals remain unimplemented"


def refresh_surface_adapters(material: bpy.types.Material) -> None:
    """Keep portable lighting branches attached after a decal changes the core."""
    nodes, links = material.node_tree.nodes, material.node_tree.links
    coverage = nodes.get("EF_FoliageCoverage")
    if coverage is None:
        return
    bsdfs = [n for n in nodes if n.type == "BSDF_PRINCIPLED"]
    if len(bsdfs) != 1:
        raise ValueError(f"Foliage adapter requires exactly one surface core: {material.name}")
    translucent = nodes["EF_FoliageTranslucent"]
    for source, destination in ((bsdfs[0].inputs["Alpha"], coverage.inputs[1]),
                                (bsdfs[0].inputs["Base Color"], translucent.inputs["Color"])):
        for link in list(destination.links):
            links.remove(link)
        if source.is_linked:
            links.new(source.links[0].from_socket, destination)
        else:
            destination.default_value = source.default_value


def configure_scene_transmission(scene: bpy.types.Scene) -> None:
    """Configure scene resources required by Eevee's material transmission."""
    scene.eevee.use_raytracing = True
    scene.eevee.ray_tracing_method = "SCREEN"
    scene.cycles.transmission_bounces = max(8, scene.cycles.transmission_bounces)
    scene.cycles.transparent_max_bounces = max(16, scene.cycles.transparent_max_bounces)
    scene["endfield_pipeline_contract"] = "EndfieldBlenderDualEngine/1"
    scene["endfield_pipeline_limitations"] = "Eevee screen rays depend on camera visibility and probes; game exposure, GI and screen-space buffers are not copied"
