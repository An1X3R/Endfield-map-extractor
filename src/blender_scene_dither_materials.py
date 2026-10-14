"""Unlit VFXDitherAlpha preview from compiled HGBuffer/DepthOnly variants 142/418."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import bpy

from blender_scene_effect_layers import _component, _math, _sample, _scale, _uv, _value, _vec
from blender_scene_materials import layout_shader_node_tree
from endfield_scene_material_contract import MaterialRecord


SHADER = "HGRP/Effect/VFXDitherAlpha"


def validate_dither_mesh(record: MaterialRecord, vertex_color_components: int) -> None:
    """Require the actual unlit UV0 variant; inactive feature controls do not alter it."""
    if record["Shader"].get("Name") != SHADER:
        raise ValueError(f"Dither shader family differs: material={record['Name']}")
    state = record.get("RenderState")
    if state is None or state.get("ValidKeywords") != []:
        raise NotImplementedError(f"Dither compiled feature variant is not verified: material={record['Name']}")
    disabled = state.get("DisabledShaderPasses")
    if not isinstance(disabled, list) or "DepthOnly" in disabled or "HGBuffer" in disabled:
        raise NotImplementedError(f"Dither requires source HGBuffer and DepthOnly passes: material={record['Name']}")
    if state.get("CustomRenderQueue") != 2000:
        raise NotImplementedError(f"Dither source queue is not verified: material={record['Name']}")
    floats, colors = record["Floats"], record["Colors"]
    for name in ("_InParticle", "_DisableVertColor", "_TintColorIntensity", "_TintColorAlpha",
                 "_UseMainTexAsAlpha", "_IgnorePostExposure", "_DitherTilling", "_DitherAlphaExp",
                 "_DitherAlphaMode", "_DitherAlphaEdge", "_UseLighting", "_UseNearCameraFade",
                 "_CullMode", "_SurfaceType"):
        value = floats.get(name)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"Dither source control is missing or non-finite: material={record['Name']}, property={name}")
    for name in ("_InParticle", "_DisableVertColor", "_UseMainTexAsAlpha", "_IgnorePostExposure", "_DitherAlphaMode"):
        if floats[name] not in (0, 1):
            raise NotImplementedError(f"Dither selector is not verified: material={record['Name']}, property={name}")
    if floats["_SurfaceType"] != 0 or floats["_UseLighting"] != 0 or floats["_UseNearCameraFade"] != 0:
        raise NotImplementedError(f"Dither requires unlit opaque source without near-camera fade: material={record['Name']}")
    if floats["_CullMode"] not in (0, 1, 2) or floats["_DitherAlphaExp"] <= 0:
        raise NotImplementedError(f"Dither culling/exponent is not verified: material={record['Name']}")
    for name in ("_TintColor", "_MainTexUVWeights", "_MainTexUVSpeed", "_MainTexUVRotateMat"):
        value = colors.get(name)
        if not isinstance(value, list) or len(value) != 4 or any(type(item) not in (int, float) or not math.isfinite(item) for item in value):
            raise ValueError(f"Dither source vector is missing or invalid: material={record['Name']}, property={name}")
    if colors["_MainTexUVWeights"] != [1, 0, 0, 0] or colors["_MainTexUVSpeed"][2:] != [0, 0]:
        raise NotImplementedError(f"Dither requires actual UV0 without particle custom inputs: material={record['Name']}")
    if type(vertex_color_components) is not int or vertex_color_components not in (0, 3, 4):
        raise ValueError(f"Dither mesh COLOR component count is invalid: material={record['Name']}, components={vertex_color_components}")


def _pixel_dimension(tree: bpy.types.NodeTree, axis: str) -> bpy.types.NodeSocket:
    """Bind full-frame raster dimensions, including Blender resolution percentage."""
    scene = bpy.context.scene
    node = tree.nodes.new("ShaderNodeValue")
    node.name = node.label = f"Dither Raster Resolution {axis.upper()}"
    node.outputs[0].default_value = getattr(scene.render, f"resolution_{axis}") * scene.render.resolution_percentage / 100
    driver = node.outputs[0].driver_add("default_value").driver
    driver.type = "SCRIPTED"
    for name in (f"resolution_{axis}", "resolution_percentage"):
        variable = driver.variables.new()
        variable.name, variable.type = name, "SINGLE_PROP"
        variable.targets[0].id_type = "SCENE"
        variable.targets[0].id, variable.targets[0].data_path = scene, f"render.{name}"
    driver.expression = f"resolution_{axis} * resolution_percentage / 100"
    return node.outputs[0]


def make_dither_mesh_material(record: MaterialRecord, textures: dict[tuple[str, int], tuple[Path, str]],
                              snapshot_time: float, vertex_color_components: int) -> bpy.types.Material:
    """Combine source depth discard and unlit GBuffer color into a marked surface preview."""
    validate_dither_mesh(record, vertex_color_components)
    if type(snapshot_time) not in (int, float) or not math.isfinite(snapshot_time):
        raise ValueError(f"Dither source time is invalid: material={record['Name']}")
    identity = hashlib.sha256(json.dumps([record, snapshot_time, vertex_color_components], sort_keys=True, allow_nan=False).encode()).hexdigest()[:24]
    name = "EF_FX_DITHER_" + identity
    previous = bpy.data.materials.get(name)
    if previous is not None:
        return previous
    material = bpy.data.materials.new(name)
    material.use_nodes, material.surface_render_method = True, "DITHERED"
    material.use_transparency_overlap = True
    tree = material.node_tree
    tree.nodes.clear()
    floats, colors = record["Floats"], record["Colors"]
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.uv_map = "UVMap"
    zero = _vec(tree, "Dither No UV Disturbance", "ADD", (0., 0., 0.), (0., 0., 0.))
    sample_uv = _uv(tree, record, "_MainTex", uv.outputs[0], snapshot_time, zero, 0.)
    sample = _sample(tree, record, "_MainTex", textures, sample_uv)
    tint = colors["_TintColor"]
    rgb = _scale(tree, "Dither Source Tint Intensity", _vec(tree, "Dither Source Tint RGB", "MULTIPLY", sample.rgba.rgb, tuple(tint[:3])), floats["_TintColorIntensity"])
    alpha = _math(tree, "Dither Source Tint Alpha", "MULTIPLY", sample.rgba.alpha, tint[3] * floats["_TintColorAlpha"])
    limitations = [*sample.limitations,
        "Compiled VFXDitherAlpha HGBuffer RGB and DepthOnly discard are combined into one Blender surface; Unity equal-depth/stencil and deferred GBuffer pipeline are approximated",
        "Game exposure is an explicit reference of one; game grading is not captured",
        "Source TAA noise phase is an editable zero reference; Blender raster sample positions and game TAA differ",
        "Runtime instance vertex-color attenuation and disappearance are explicit zero references; source streams are unchanged"]
    if floats["_DisableVertColor"] == 0:
        if vertex_color_components == 0:
            vertex = tree.nodes.new("ShaderNodeRGB")
            vertex.name = vertex.label = "MeshRenderer Missing COLOR Reference White"
            vertex.outputs[0].default_value = (1., 1., 1., 1.)
            vertex_rgb, vertex_alpha = vertex.outputs[0], 1.
            limitations.append("MeshRenderer missing COLOR uses an explicit white RGBA reference; this game runtime binding is unverified and no source color stream is synthesized")
        else:
            vertex = tree.nodes.new("ShaderNodeVertexColor")
            vertex.layer_name = "EF_FX_COLOR"
            vertex_rgb, vertex_alpha = vertex.outputs["Color"], vertex.outputs["Alpha"]
        rgb = _vec(tree, "Dither Source Vertex RGB", "MULTIPLY", rgb, vertex_rgb)
        alpha = _math(tree, "Dither Source Vertex Alpha", "MULTIPLY", alpha, vertex_alpha)
    exposure = _value(tree, "Dither Game Exposure Reference One", 1.)
    rgb = _scale(tree, "Dither Source Exposure", rgb, _math(tree, "Dither Exposure Selector", "ADD", 1 - floats["_IgnorePostExposure"], _math(tree, "Dither Weighted Exposure", "MULTIPLY", exposure, floats["_IgnorePostExposure"])))
    rgb = _vec(tree, "Dither Clamp RGB Low", "MAXIMUM", rgb, (0., 0., 0.))
    rgb = _vec(tree, "Dither Source RGB", "MINIMUM", rgb, (1000., 1000., 1000.))
    attenuation = _value(tree, "Dither Runtime Vertex Attenuation Reference Zero", 0.)
    remaining = _math(tree, "Dither Remaining Vertex Attenuation", "SUBTRACT", 1., attenuation)
    rgb = _scale(tree, "Dither Runtime Vertex RGB", rgb, remaining)
    alpha = _math(tree, "Dither Runtime Vertex Alpha", "MULTIPLY", alpha, remaining)
    disappear = _value(tree, "Dither Runtime Disappearance Reference Zero", 0.)
    alpha = _math(tree, "Dither Runtime Alpha", "MULTIPLY", alpha, _math(tree, "Dither Remaining Instance", "SUBTRACT", 1., disappear))
    alpha = _math(tree, "Dither Clamp Alpha Low", "MAXIMUM", alpha, 0.)
    alpha = _math(tree, "Dither Clamp Alpha High", "MINIMUM", alpha, 1.)
    coverage = _math(tree, "Dither Source Coverage", "POWER", alpha, floats["_DitherAlphaExp"])
    coordinate = tree.nodes.new("ShaderNodeTexCoord")
    x = _math(tree, "Dither Pixel X", "MULTIPLY", _component(tree, "Dither Window X", coordinate.outputs["Window"], "X"), _pixel_dimension(tree, "x"))
    y = _math(tree, "Dither Pixel Y", "MULTIPLY", _math(tree, "Dither Source Window Y", "SUBTRACT", 1., _component(tree, "Dither Window Y", coordinate.outputs["Window"], "Y")), _pixel_dimension(tree, "y"))
    phase = _value(tree, "Dither TAA Phase Reference Zero", 0.)
    x = _math(tree, "Dither Tiled X", "ADD", _math(tree, "Dither Scale X", "MULTIPLY", x, floats["_DitherTilling"]), phase)
    y = _math(tree, "Dither Tiled Y", "ADD", _math(tree, "Dither Scale Y", "MULTIPLY", y, floats["_DitherTilling"]), phase)
    dot = _math(tree, "Dither IGN Dot", "ADD", _math(tree, "Dither IGN X", "MULTIPLY", x, 0.0671105608344078), _math(tree, "Dither IGN Y", "MULTIPLY", y, 0.005837149918079376))
    noise = _math(tree, "Dither Source IGN", "FRACT", _math(tree, "Dither IGN Multiplier", "MULTIPLY", _math(tree, "Dither IGN Inner Fraction", "FRACT", dot, 0.), 52.98291778564453), 0.)
    threshold = (_math(tree, "Dither Edge Threshold", "SUBTRACT", _math(tree, "Dither Edge Noise", "MULTIPLY", _math(tree, "Dither Edge Coverage", "MULTIPLY", coverage, .2), noise), floats["_DitherAlphaEdge"])
                 if floats["_DitherAlphaMode"] == 1 else _math(tree, "Dither Alpha Threshold", "SUBTRACT", coverage, noise))
    keep = _math(tree, "Dither Source Keep", "SUBTRACT", 1., _math(tree, "Dither Source Discard", "LESS_THAN", threshold, 0.))
    if floats["_CullMode"] != 0:
        geometry = tree.nodes.new("ShaderNodeNewGeometry")
        face = geometry.outputs["Backfacing"] if floats["_CullMode"] == 1 else _math(tree, "Dither Keep Front Face", "SUBTRACT", 1., geometry.outputs["Backfacing"])
        keep = _math(tree, "Dither Source Face Coverage", "MULTIPLY", keep, face)
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(rgb, emission.inputs["Color"])
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.name = mix.label = "Dither Source Depth Coverage and HGBuffer RGB"
    tree.links.new(keep, mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(mix.outputs[0], output.inputs["Surface"])
    material["endfield_fx_source_material"] = record["Name"]
    material["endfield_fx_source"] = record["Source"]
    material["endfield_fx_path_id"] = str(record["PathId"])
    material["endfield_fx_source_material_json"] = json.dumps(record, sort_keys=True, allow_nan=False)
    material["endfield_fx_status"] = "source_formula_preview_partial"
    material["endfield_fx_limitations"] = json.dumps(limitations)
    material["endfield_fx_custom1_channels"] = "[]"
    material["endfield_fx_dither_shader"] = SHADER
    material["endfield_fx_dither_contract"] = "unlit_hgbuffer_depth_ign/1"
    layout_shader_node_tree(tree)
    return material
