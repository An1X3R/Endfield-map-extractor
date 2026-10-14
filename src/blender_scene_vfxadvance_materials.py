"""Build the verified mask-only VFXAdvance mesh material as a partial preview."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import bpy

from blender_scene_effect_layers import _component, _math, _sample, _scale, _uv, _vec, _xy
from blender_scene_materials import layout_shader_node_tree
from endfield_scene_material_contract import MaterialRecord
from endfield_scene_vfxadvance_contract import (
    VFXADVANCE_BLEND_DISSOLVE_DISTURB_EVIDENCE_VARIANT,
    VFXADVANCE_MASK_ONLY_EVIDENCE_VARIANT,
    vfxadvance_blend_dissolve_disturb_issues,
    vfxadvance_mask_only_issues,
)


def _clamp_source_rgb(tree: bpy.types.NodeTree, rgb: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Clamp source VFXAdvance output color to the compiled [0, 1000] range."""
    lower = _vec(tree, "Source RGB Clamp Lower Bound 0", "MAXIMUM", rgb, (0.0, 0.0, 0.0))
    return _vec(tree, "Source RGB Clamp Upper Bound 1000", "MINIMUM",
                lower, (1000.0, 1000.0, 1000.0))


def _coverage_group() -> bpy.types.NodeTree:
    """Return the source-mask coverage group with an explicit host depth socket."""
    name = "Endfield VFXAdvance Mask Coverage Partial"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_fx_contract") != "mask_sample_alpha_main_alpha_vertex_alpha_tint_rgba_host_depth/2":
            raise ValueError(f"Conflicting VFXAdvance coverage node group: name={name}")
        return existing
    group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    group["endfield_fx_contract"] = "mask_sample_alpha_main_alpha_vertex_alpha_tint_rgba_host_depth/2"
    for socket_name in ("Mask Sample Alpha", "Main Alpha", "Vertex Alpha", "Tint Alpha"):
        group.interface.new_socket(name=socket_name, in_out="INPUT", socket_type="NodeSocketFloat")
    host_depth = group.interface.new_socket(
        name="HOST_DEPTH_VISIBILITY", in_out="INPUT", socket_type="NodeSocketFloat")
    host_depth.default_value = 1.0
    host_depth.min_value = 0.0
    host_depth.max_value = 1.0
    group.interface.new_socket(name="Coverage", in_out="OUTPUT", socket_type="NodeSocketFloat")
    inputs = group.nodes.new("NodeGroupInput")
    coverage = inputs.outputs["Mask Sample Alpha"]
    for socket_name in ("Main Alpha", "Vertex Alpha", "Tint Alpha", "HOST_DEPTH_VISIBILITY"):
        coverage = _math(group, f"Coverage x {socket_name}", "MULTIPLY", coverage,
                         inputs.outputs[socket_name])
    output = group.nodes.new("NodeGroupOutput")
    group.links.new(coverage, output.inputs["Coverage"])
    layout_shader_node_tree(group)
    return group


def make_vfxadvance_mask_material(record: MaterialRecord,
                                 textures: dict[tuple[str, int], tuple[Path, str]],
                                 snapshot_time: float,
                                 vertex_color_components: int) -> bpy.types.Material:
    """Build the source mask and underground feature branch for UV0 and RGBA streams."""
    issues = vfxadvance_mask_only_issues(record, vertex_color_components)
    if issues:
        raise NotImplementedError(
            "VFXAdvance mask and underground features are outside the supported source contract: "
            f"material={record['Name']}, issues={issues}")
    if type(snapshot_time) not in (int, float) or not math.isfinite(snapshot_time):
        raise ValueError(f"VFXAdvance snapshot time must be finite: material={record['Name']}, value={snapshot_time}")

    source_json = json.dumps(record, sort_keys=True, allow_nan=False)
    identity = hashlib.sha256(json.dumps(
        [record, snapshot_time, vertex_color_components, "vfxadvance_mask_only"],
        sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()[:24]
    name = f"EF_FX_ADVANCE_MASK_{identity}"
    existing = bpy.data.materials.get(name)
    if existing is not None:
        if (existing.get("endfield_fx_source_material_json") != source_json
                or existing.get("endfield_fx_shader_variant_observed") != VFXADVANCE_MASK_ONLY_EVIDENCE_VARIANT
                or existing.get("endfield_fx_branch") != "vfxadvance_mask_only"
                or not existing.use_nodes or existing.node_tree is None):
            raise ValueError(f"Cached VFXAdvance material conflicts with source identity: material={name}")
        return existing

    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.surface_render_method = "BLENDED"
    material.use_transparency_overlap = True
    material["endfield_fx_source_material"] = record["Name"]
    material["endfield_fx_source"] = record["Source"]
    material["endfield_fx_path_id"] = str(record["PathId"])
    material["endfield_fx_source_material_json"] = source_json
    material["endfield_fx_shader_variant_observed"] = VFXADVANCE_MASK_ONLY_EVIDENCE_VARIANT
    material["endfield_fx_branch"] = "vfxadvance_mask_only"
    material["endfield_fx_status"] = "source_formula_preview_partial"

    tree = material.node_tree
    if tree is None:
        raise RuntimeError(f"Blender did not create a node tree: material={name}")
    tree.nodes.clear()
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.name = uv.label = "Source UV0"
    uv.uv_map = "UVMap"
    time = tree.nodes.new("ShaderNodeValue")
    time.name = time.label = "EF FX Source Time Seconds"
    time.outputs[0].default_value = snapshot_time
    time["endfield_layout_section"] = "Source Time"
    zero = _vec(tree, "No VFXAdvance UV Disturbance", "ADD", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    main_uv = _uv(tree, record, "_MainTex", uv.outputs[0], snapshot_time, zero, 0.0)
    mask_uv = _uv(tree, record, "_MaskTex", uv.outputs[0], snapshot_time, zero, 0.0)
    main_sample = _sample(tree, record, "_MainTex", textures, main_uv)
    mask_sample = _sample(tree, record, "_MaskTex", textures, mask_uv)
    main, mask = main_sample.rgba, mask_sample.rgba
    material["endfield_fx_limitations"] = json.dumps([
        "partial_host_depth: HOST_DEPTH_VISIBILITY replaces native screen-depth underground fade and defaults to 1",
        "Compiled motion-vector render target 1 is omitted",
        "Native post-exposure and render-target composition state are not reproduced",
        "Source alpha blending is represented with Blender transparent/emission mixing",
        *main_sample.limitations, *mask_sample.limitations,
    ])

    vertex_color = tree.nodes.new("ShaderNodeVertexColor")
    vertex_color.name = vertex_color.label = "Source Mesh RGBA Vertex Color"
    vertex_color.layer_name = "EF_FX_COLOR"
    vertex_color["endfield_layout_section"] = "Source Mesh Streams"
    tint = record["Colors"]["_TintColor"]
    rgb = _vec(tree, "MainTex x Source Tint", "MULTIPLY", main.rgb, tuple(tint[:3]))
    rgb = _vec(tree, "Source Vertex RGB", "MULTIPLY", rgb, vertex_color.outputs["Color"])
    rgb = _scale(tree, "Source Tint Intensity", rgb, record["Floats"]["_TintColorIntensity"])
    rgb = _clamp_source_rgb(tree, rgb)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = emission.label = "Source Tint Intensity"
    emission.inputs["Strength"].default_value = 1.0
    tree.links.new(rgb, emission.inputs["Color"])

    coverage = tree.nodes.new("ShaderNodeGroup")
    coverage.node_tree = _coverage_group()
    coverage.name = coverage.label = "HOST_DEPTH_VISIBILITY (partial_host_depth)"
    coverage["endfield_fx_host_depth_parameter"] = "HOST_DEPTH_VISIBILITY"
    coverage.inputs["Mask Sample Alpha"].default_value = 1.0
    tint_alpha = _math(tree, "Source _TintColor.a x _TintColorAlpha", "MULTIPLY",
                       tint[3], record["Floats"]["_TintColorAlpha"])
    tree.links.new(tint_alpha, coverage.inputs["Tint Alpha"])
    tree.links.new(mask.alpha, coverage.inputs["Mask Sample Alpha"])
    tree.links.new(main.alpha, coverage.inputs["Main Alpha"])
    tree.links.new(vertex_color.outputs["Alpha"], coverage.inputs["Vertex Alpha"])

    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    transparent.name = transparent.label = "Source Transparent Coverage"
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.name = mix.label = "Source Mask Coverage Preview"
    tree.links.new(coverage.outputs["Coverage"], mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(mix.outputs[0], output.inputs["Surface"])
    layout_shader_node_tree(tree)
    return material


def _named_uv(tree: bpy.types.NodeTree, record: MaterialRecord, texture_name: str,
              speed_name: str, rotation_name: str, base_uv: bpy.types.NodeSocket,
              snapshot_time: float, disturbance: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Build UV coordinates where source texture and UV property names differ."""
    slot = next(slot for slot in record["Textures"] if slot["Property"] == texture_name)
    colors = record["Colors"]
    speed = colors[speed_name]
    rotation = colors[rotation_name]
    time_node = tree.nodes.get("EF FX Source Time Seconds")
    if time_node is None:
        seconds = tree.nodes.new("ShaderNodeValue")
        seconds.name = seconds.label = "EF FX Source Time Seconds"
        seconds.outputs[0].default_value = snapshot_time
        time = seconds.outputs[0]
    else:
        time = time_node.outputs[0]
    offset_x = _math(tree, f"{texture_name} Time Offset X", "MULTIPLY", time, speed[0])
    offset_y = _math(tree, f"{texture_name} Time Offset Y", "MULTIPLY", time, speed[1])
    offset = tree.nodes.new("ShaderNodeCombineXYZ")
    offset.name = offset.label = f"{texture_name} Time Offset"
    tree.links.new(offset_x, offset.inputs["X"])
    tree.links.new(offset_y, offset.inputs["Y"])
    moving = _vec(tree, f"{texture_name} UV Speed", "ADD", base_uv, offset.outputs[0])
    centered = _vec(tree, f"{texture_name} UV Center", "SUBTRACT", moving, (0.5, 0.5, 0.0))
    x = _component(tree, f"{texture_name} UV X", centered, "X")
    y = _component(tree, f"{texture_name} UV Y", centered, "Y")
    rx = _math(tree, f"{texture_name} Rotate X", "ADD",
               _math(tree, f"{texture_name} Rotate XX", "MULTIPLY", x, rotation[0]),
               _math(tree, f"{texture_name} Rotate XY", "MULTIPLY", y, rotation[2]))
    ry = _math(tree, f"{texture_name} Rotate Y", "ADD",
               _math(tree, f"{texture_name} Rotate YX", "MULTIPLY", x, rotation[1]),
               _math(tree, f"{texture_name} Rotate YY", "MULTIPLY", y, rotation[3]))
    rotated = _vec(tree, f"{texture_name} UV Restore Center", "ADD",
                   _xy(tree, f"{texture_name} Rotated UV", rx, ry), (0.5, 0.5, 0.0))
    mapped = _vec(tree, f"{texture_name} UV Scale", "MULTIPLY",
                  rotated, (slot["Scale"][0], slot["Scale"][1], 1.0))
    mapped = _vec(tree, f"{texture_name} UV Offset", "ADD", mapped,
                  (slot["Offset"][0], slot["Offset"][1], 0.0))
    return _vec(tree, f"{texture_name} UV Disturbance", "ADD", mapped, disturbance)


def _blend_dissolve_coverage_group() -> bpy.types.NodeTree:
    """Return explicit dissolve and host-depth coverage controls."""
    name = "Endfield VFXAdvance Blend Dissolve Disturb Coverage Partial"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_fx_contract") != "main_alpha_x_dissolve_x_host_depth/1":
            raise ValueError(f"Conflicting VFXAdvance coverage node group: name={name}")
        return existing
    group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    group["endfield_fx_contract"] = "main_alpha_x_dissolve_x_host_depth/1"
    for socket_name in ("Main Alpha", "Dissolve Coverage"):
        group.interface.new_socket(name=socket_name, in_out="INPUT", socket_type="NodeSocketFloat")
    host_depth = group.interface.new_socket(
        name="HOST_DEPTH_VISIBILITY", in_out="INPUT", socket_type="NodeSocketFloat")
    host_depth.default_value = 1.0
    host_depth.min_value = 0.0
    host_depth.max_value = 1.0
    group.interface.new_socket(name="Coverage", in_out="OUTPUT", socket_type="NodeSocketFloat")
    inputs = group.nodes.new("NodeGroupInput")
    alpha = _math(group, "Main Alpha x Dissolve Coverage", "MULTIPLY",
                  inputs.outputs["Main Alpha"], inputs.outputs["Dissolve Coverage"])
    coverage = _math(group, "Coverage x HOST_DEPTH_VISIBILITY", "MULTIPLY",
                     alpha, inputs.outputs["HOST_DEPTH_VISIBILITY"])
    output = group.nodes.new("NodeGroupOutput")
    group.links.new(coverage, output.inputs["Coverage"])
    layout_shader_node_tree(group)
    return group


def make_vfxadvance_blend_dissolve_disturb_material(
        record: MaterialRecord, textures: dict[tuple[str, int], tuple[Path, str]],
        snapshot_time: float, vertex_color_components: int) -> bpy.types.Material:
    """Build the source blend, dissolve, and scalar-disturb VFXAdvance branch."""
    issues = vfxadvance_blend_dissolve_disturb_issues(record, vertex_color_components)
    if issues:
        raise NotImplementedError(
            "VFXAdvance blend, dissolve, disturb, and underground features are outside the supported source contract: "
            f"material={record['Name']}, issues={issues}")
    if type(snapshot_time) not in (int, float) or not math.isfinite(snapshot_time):
        raise ValueError(f"VFXAdvance snapshot time must be finite: material={record['Name']}, value={snapshot_time}")

    source_json = json.dumps(record, sort_keys=True, allow_nan=False)
    identity = hashlib.sha256(json.dumps(
        [record, snapshot_time, vertex_color_components, "vfxadvance_blend_dissolve_disturb"],
        sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()[:24]
    name = f"EF_FX_ADVANCE_BLEND_DISSOLVE_DISTURB_{identity}"
    existing = bpy.data.materials.get(name)
    if existing is not None:
        if (existing.get("endfield_fx_source_material_json") != source_json
                or existing.get("endfield_fx_shader_variant_observed") != VFXADVANCE_BLEND_DISSOLVE_DISTURB_EVIDENCE_VARIANT
                or existing.get("endfield_fx_branch") != "vfxadvance_blend_dissolve_disturb"
                or not existing.use_nodes or existing.node_tree is None):
            raise ValueError(f"Cached VFXAdvance material conflicts with source identity: material={name}")
        return existing

    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.surface_render_method = "BLENDED"
    material.use_transparency_overlap = True
    material["endfield_fx_source_material"] = record["Name"]
    material["endfield_fx_source"] = record["Source"]
    material["endfield_fx_path_id"] = str(record["PathId"])
    material["endfield_fx_source_material_json"] = source_json
    material["endfield_fx_shader_variant_observed"] = VFXADVANCE_BLEND_DISSOLVE_DISTURB_EVIDENCE_VARIANT
    material["endfield_fx_branch"] = "vfxadvance_blend_dissolve_disturb"
    material["endfield_fx_status"] = "source_formula_preview_partial"

    tree = material.node_tree
    if tree is None:
        raise RuntimeError(f"Blender did not create a node tree: material={name}")
    tree.nodes.clear()
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.name = uv.label = "Source UV0"
    uv.uv_map = "UVMap"
    time = tree.nodes.new("ShaderNodeValue")
    time.name = time.label = "EF FX Source Time Seconds"
    time.outputs[0].default_value = snapshot_time
    time["endfield_layout_section"] = "Source Time"
    zero = _vec(tree, "No VFXAdvance UV Disturbance", "ADD",
                (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))

    disturb_uv = _named_uv(tree, record, "_DisturbTex1", "_DisturbUVSpeed1",
                           "_DisturbUVRotateMat1", uv.outputs[0], snapshot_time, zero)
    disturb_result = _sample(tree, record, "_DisturbTex1", textures, disturb_uv)
    disturb_red = _component(tree, "DisturbTex1 Red Scalar", disturb_result.rgba.rgb, "X")
    disturb_u = _math(tree, "Disturb U Offset", "MULTIPLY",
                      disturb_red, record["Floats"]["_DisturbUIntensity1"])
    disturb_v = _math(tree, "Disturb V Offset", "MULTIPLY",
                      disturb_red, record["Floats"]["_DisturbVIntensity1"])
    disturbance = tree.nodes.new("ShaderNodeCombineXYZ")
    disturbance.name = disturbance.label = "Source DisturbTex1 Scalar Offset"
    tree.links.new(disturb_u, disturbance.inputs["X"])
    tree.links.new(disturb_v, disturbance.inputs["Y"])
    disturbance["endfield_layout_section"] = "Source Disturb"

    main_disturbance = disturbance.outputs[0] if record["Floats"]["_MainTexUseDisturb"] else zero
    blend_disturbance = disturbance.outputs[0] if record["Floats"]["_BlendTexUseDisturb"] else zero
    dissolve_disturbance = disturbance.outputs[0] if record["Floats"]["_DissolveTexUseDisturb"] else zero
    main_uv = _uv(tree, record, "_MainTex", uv.outputs[0], snapshot_time,
                  main_disturbance, 0.0)
    blend_uv = _uv(tree, record, "_BlendTex", uv.outputs[0], snapshot_time,
                   blend_disturbance, 0.0)
    dissolve_uv = _named_uv(tree, record, "_DissolveTex", "_DissolveUVSpeed",
                            "_DissolveUVRotateMat", uv.outputs[0], snapshot_time,
                            dissolve_disturbance)
    main_result = _sample(tree, record, "_MainTex", textures, main_uv)
    blend_result = _sample(tree, record, "_BlendTex", textures, blend_uv)
    dissolve_result = _sample(tree, record, "_DissolveTex", textures, dissolve_uv)

    vertex_color = tree.nodes.new("ShaderNodeVertexColor")
    vertex_color.name = vertex_color.label = "Source Mesh RGBA Vertex Color"
    vertex_color.layer_name = "EF_FX_COLOR"
    vertex_color["endfield_layout_section"] = "Source Mesh Streams"
    tint = record["Colors"]["_TintColor"]
    tint_intensity = record["Floats"]["_TintColorIntensity"]
    main_rgb = _vec(tree, "MainTex x Source Tint", "MULTIPLY",
                    main_result.rgba.rgb, tuple(tint[:3]))
    main_rgb = _vec(tree, "MainTex x Source Vertex RGB", "MULTIPLY",
                    main_rgb, vertex_color.outputs["Color"])
    main_rgb = _scale(tree, "Source Main Tint Intensity", main_rgb, tint_intensity)
    main_alpha = _math(tree, "MainTex Alpha x Vertex Alpha", "MULTIPLY",
                       main_result.rgba.alpha, vertex_color.outputs["Alpha"])
    tint_alpha = _math(tree, "Source _TintColor.a x _TintColorAlpha", "MULTIPLY",
                       tint[3], record["Floats"]["_TintColorAlpha"])
    main_alpha = _math(tree, "Main Alpha x Source Tint RGBA Alpha", "MULTIPLY",
                       main_alpha, tint_alpha)

    blend_tint = record["Colors"]["_BlendTint"]
    blend_rgb = _vec(tree, "BlendTex x Source Blend Tint", "MULTIPLY",
                     blend_result.rgba.rgb, tuple(blend_tint[:3]))
    blend_rgb = _vec(tree, "BlendTex x Source Vertex RGB", "MULTIPLY",
                     blend_rgb, vertex_color.outputs["Color"])
    blend_alpha_factor = _math(tree, "Blend Tint Alpha x Vertex Alpha", "MULTIPLY",
                               blend_tint[3], vertex_color.outputs["Alpha"])
    blend_presence = _math(tree, "Blend Coverage Source Sum", "ADD",
                           main_alpha, blend_result.rgba.alpha)
    blend_presence = _math(tree, "Blend Coverage Tint", "MULTIPLY",
                           blend_presence, blend_alpha_factor)
    blend_presence_clamp = tree.nodes.new("ShaderNodeClamp")
    blend_presence_clamp.name = blend_presence_clamp.label = "Blend Coverage 0 to 1"
    tree.links.new(blend_presence, blend_presence_clamp.inputs["Value"])
    blend_rgb = _scale(tree, "Blend Layer Source Coverage", blend_rgb,
                       blend_presence_clamp.outputs["Result"])
    combined_rgb = _vec(tree, "Main and Blend Source Sum", "ADD", main_rgb, blend_rgb)

    dissolve_red = _component(tree, "DissolveTex Red", dissolve_result.rgba.rgb, "X")
    dissolve_offset = record["Floats"]["_DissolveScheduleOffset"]
    host_dissolve_schedule = tree.nodes.new("ShaderNodeValue")
    host_dissolve_schedule.name = host_dissolve_schedule.label = (
        "HOST_DISSOLVE_SCHEDULE (partial compiled varying term)")
    host_dissolve_schedule.outputs[0].default_value = 0.0
    host_dissolve_schedule["endfield_fx_host_parameter"] = "HOST_DISSOLVE_SCHEDULE"
    host_dissolve_schedule["endfield_fx_source_term"] = "_5.z * _30._m4"
    host_dissolve_schedule["endfield_fx_default_is_unverified"] = True
    host_dissolve_schedule["endfield_layout_section"] = "Host Schedule Input"
    schedule = _math(tree, "Source Dissolve Schedule plus HOST_DISSOLVE_SCHEDULE",
                     "ADD", dissolve_offset, host_dissolve_schedule.outputs[0])
    schedule_scaled = _math(tree, "Source Dissolve Schedule x 2.02", "MULTIPLY",
                            schedule, 2.02)
    dissolve_threshold = _math(tree, "Source Dissolve Threshold Offset", "SUBTRACT",
                               schedule_scaled, 1.01)
    dissolve_measure = _math(tree, "Dissolve Red Minus Schedule Threshold", "SUBTRACT",
                             dissolve_red, dissolve_threshold)
    sharp = record["Floats"]["_DissolveEdgeSharp"]
    dissolve_coverage = _math(tree, "Dissolve Coverage", "MULTIPLY",
                              dissolve_measure, sharp)
    clamp_coverage = tree.nodes.new("ShaderNodeClamp")
    clamp_coverage.name = clamp_coverage.label = "Dissolve Coverage 0 to 1"
    tree.links.new(dissolve_coverage, clamp_coverage.inputs["Value"])
    edge_measure = _math(tree, "Dissolve Edge Distance Minus Measure", "SUBTRACT",
                         record["Floats"]["_EdgeDistance"], dissolve_measure)
    edge_coverage = _math(tree, "Dissolve Edge Coverage", "MULTIPLY", edge_measure, sharp)
    clamp_edge = tree.nodes.new("ShaderNodeClamp")
    clamp_edge.name = clamp_edge.label = "Dissolve Edge Coverage 0 to 1"
    tree.links.new(edge_coverage, clamp_edge.inputs["Value"])

    edge_color = record["Colors"]["_EdgeColor"]
    edge_tint = tree.nodes.new("ShaderNodeRGB")
    edge_tint.name = edge_tint.label = "Source Dissolve Edge Tint"
    edge_tint.outputs[0].default_value = tuple(edge_color)
    edge_tint["endfield_layout_section"] = "Source Dissolve"
    edge_rgb = _scale(tree, "Source Dissolve Edge Tint Intensity", edge_tint.outputs[0],
                      _math(tree, "Dissolve Edge x Tint Intensity", "MULTIPLY",
                            clamp_edge.outputs["Result"], tint_intensity))
    one_minus_edge = _math(tree, "One Minus Dissolve Edge", "SUBTRACT",
                           1.0, clamp_edge.outputs["Result"])
    base_part = _scale(tree, "Main Blend Outside Dissolve Edge", combined_rgb, one_minus_edge)
    edge_part = _scale(tree, "Dissolve Edge Source Mix", edge_rgb, clamp_edge.outputs["Result"])
    output_rgb = _vec(tree, "VFXAdvance Source Color", "ADD", base_part, edge_part)
    output_rgb = _clamp_source_rgb(tree, output_rgb)

    coverage = tree.nodes.new("ShaderNodeGroup")
    coverage.node_tree = _blend_dissolve_coverage_group()
    coverage.name = coverage.label = "HOST_DEPTH_VISIBILITY (partial_host_depth)"
    coverage["endfield_fx_host_depth_parameter"] = "HOST_DEPTH_VISIBILITY"
    coverage.inputs["HOST_DEPTH_VISIBILITY"].default_value = 1.0
    tree.links.new(main_alpha, coverage.inputs["Main Alpha"])
    tree.links.new(clamp_coverage.outputs["Result"], coverage.inputs["Dissolve Coverage"])

    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = emission.label = "Source VFXAdvance Color"
    tree.links.new(output_rgb, emission.inputs["Color"])
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    transparent.name = transparent.label = "Source Transparent Coverage"
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.name = mix.label = "Source Dissolve and Host Depth Coverage"
    tree.links.new(coverage.outputs["Coverage"], mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(mix.outputs[0], output.inputs["Surface"])

    limitations = [
        "partial_host_depth: HOST_DEPTH_VISIBILITY replaces the compiled scene-depth underground fade and ground-plane visibility; default is 1",
        "Compiled motion-vector render target 1 is omitted",
        "Native post-exposure and render-target composition state are not reproduced",
        "partial_host_dissolve_schedule: the compiled fragment adds _5.z * _30._m4 to _DissolveScheduleOffset; source runtime value is unavailable, and HOST_DISSOLVE_SCHEDULE is an explicit net additive term defaulting to 0",
        "DisturbTex1 normal-map and ink-simulation modes are outside this branch; source material selects the scalar red-channel path",
        "Blend alpha output follows the compiled main-alpha and dissolve path; Unity transparent compositing is represented by Blender transparent/emission mixing",
        *disturb_result.limitations, *main_result.limitations,
        *blend_result.limitations, *dissolve_result.limitations,
    ]
    material["endfield_fx_limitations"] = json.dumps(limitations)
    layout_shader_node_tree(tree)
    return material
