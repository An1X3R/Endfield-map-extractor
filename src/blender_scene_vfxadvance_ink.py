"""Preview source ink textures while exposing absent runtime simulation inputs."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import bpy

from blender_scene_effect_layers import (
    VfxColor, _component, _disturb, _math, _normal_disturb, _sample, _scale, _value, _vec,
)
from blender_scene_materials import layout_shader_node_tree
from blender_scene_vfxadvance_materials import _clamp_source_rgb, _named_uv
from endfield_scene_material_contract import MaterialRecord
from endfield_scene_vfxadvance_ink_contract import (
    INK_DISSOLVE_UV, INK_EVIDENCE_VARIANTS, INK_UV_PROPERTIES, vfxadvance_ink_texture_issues,
)


def _clamp(tree: bpy.types.NodeTree, label: str, value: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeClamp")
    node.name = node.label = label
    tree.links.new(value, node.inputs["Value"])
    return node.outputs["Result"]


def _host_value(tree: bpy.types.NodeTree, name: str, value: float) -> bpy.types.NodeSocket:
    socket = _value(tree, name, value)
    socket.node["endfield_fx_host_parameter"] = name
    socket.node["endfield_layout_section"] = "Editable Host Inputs"
    return socket


def _ink_uv(tree: bpy.types.NodeTree, record: MaterialRecord,
            specification: tuple[str, str, str, str], source_uv: bpy.types.NodeSocket,
            seconds: float, displacement: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    texture, weights, speed, rotation = specification
    weighted = _scale(tree, f"{texture} Source UV0 Weight", source_uv, record["Colors"][weights][0])
    return _named_uv(tree, record, texture, speed, rotation, weighted, seconds, displacement)


def _dissolve(tree: bpy.types.NodeTree, record: MaterialRecord, color: VfxColor,
              red: bpy.types.NodeSocket) -> VfxColor:
    """Apply source dissolve coverage and the separately weighted emissive edge."""
    floats = record["Floats"]
    host = _host_value(tree, "HOST_DISSOLVE_SCHEDULE", 0.0)
    host.node["endfield_fx_source_term"] = "net TEXCOORD1.z multiplied by particle selector"
    schedule = _math(tree, "Ink Dissolve Schedule", "ADD", host, floats["_DissolveScheduleOffset"])
    threshold = _math(tree, "Ink Dissolve Threshold", "SUBTRACT",
                      _math(tree, "Ink Dissolve Schedule Scale", "MULTIPLY", schedule, 2.02), 1.01)
    delta = _math(tree, "Ink Dissolve Delta", "SUBTRACT", red, threshold)
    amount = _clamp(tree, "Ink Dissolve Coverage", _math(
        tree, "Ink Dissolve Sharpness", "MULTIPLY", delta, floats["_DissolveEdgeSharp"]))
    edge = _clamp(tree, "Ink Dissolve Edge", _math(
        tree, "Ink Dissolve Edge Sharpness", "MULTIPLY",
        _math(tree, "Ink Dissolve Edge Distance", "SUBTRACT", floats["_EdgeDistance"], delta),
        floats["_DissolveEdgeSharp"]))
    edge_rgb = _scale(tree, "Ink Dissolve Edge Tint", _vec(
        tree, "Ink Source Edge RGB", "ADD", tuple(record["Colors"]["_EdgeColor"][:3]), (0., 0., 0.)),
        floats["_TintColorIntensity"])
    edge_rgb = _scale(tree, "Ink Source Edge Premultiply", edge_rgb, edge)
    rgb = _vec(tree, "Ink Dissolve RGB", "ADD",
        _scale(tree, "Ink Color Outside Edge", color.rgb,
               _math(tree, "Ink One Minus Edge", "SUBTRACT", 1., edge)),
        _scale(tree, "Ink Edge Mix", edge_rgb, edge))
    alpha = _math(tree, "Ink Dissolve Alpha", "MULTIPLY", color.alpha, amount)
    return VfxColor(rgb, alpha, (*color.limitations,
        "partial_host_dissolve: HOST_DISSOLVE_SCHEDULE is the unavailable net TEXCOORD1.z term; its editable neutral preview value is 0"))


def _ink_color(tree: bpy.types.NodeTree, record: MaterialRecord,
               textures: dict[tuple[str, int], tuple[Path, str]], seconds: float) -> VfxColor:
    """Reconstruct named static texture stages independently of runtime ink storage."""
    floats, colors = record["Floats"], record["Colors"]
    # Only Main and Mask have channel switches in these compiled branches.
    sampling_record: MaterialRecord = {**record, "Floats": {**floats,
        "_UseBlendTexAsAlpha": 0., "_UseDissolveTexAsAlpha": 0., "_UseDisturbTex1AsAlpha": 0.}}
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.name = uv.label = "Source UV0"
    uv.uv_map = "UVMap"
    ink = tree.nodes.new("ShaderNodeCombineXYZ")
    ink.name = ink.label = "HOST_INK_UV_OFFSET"
    ink["endfield_fx_host_parameter"] = "HOST_INK_UV_OFFSET"
    ink["endfield_layout_section"] = "Editable Host Inputs"
    ink["endfield_fx_source_term"] = "final additive UV displacement from the absent world-XZ runtime ink texture"
    ink.inputs["X"].default_value = ink.inputs["Y"].default_value = ink.inputs["Z"].default_value = 0.
    disturb_ink = _scale(tree, "Ink Disturb Simulation Weight", ink.outputs[0], floats["_DisturbTex1UseInkSimulation"])
    disturb_uv = _ink_uv(tree, record, INK_UV_PROPERTIES[3], uv.outputs[0], seconds, disturb_ink)
    disturb = _sample(tree, sampling_record, "_DisturbTex1", textures, disturb_uv)
    if floats["_DisturbTex1Normal"]:
        offset = _normal_disturb(tree, "Ink Static Disturb", disturb.rgba,
                                floats["_DisturbUIntensity1"], 1., floats["_Bi_Disturb"])
    else:
        offset = _disturb(tree, "Ink Static Disturb", disturb.rgba,
                         floats["_DisturbUIntensity1"], floats["_DisturbVIntensity1"], 1., floats["_Bi_Disturb"])
    sampled: dict[str, VfxColor] = {}
    specifications = INK_UV_PROPERTIES[:3]
    if "_USE_DISSOLVE" in record["RenderState"]["ValidKeywords"]:
        specifications = (*specifications, INK_DISSOLVE_UV)
    limitations = list(disturb.limitations)
    for specification in specifications:
        name = specification[0]
        simulation = ink.outputs[0]
        if name == "_MaskTex":
            simulation = _scale(tree, "Ink Mask Simulation Weight", simulation, floats["_MaskTexUseInkSimulation"])
        static_offset = _scale(tree, f"{name} Static Disturbance", offset, floats[f"{name}UseDisturb"])
        displacement = _vec(tree, f"{name} Ink Plus Static Disturbance", "ADD", simulation, static_offset)
        coordinates = _ink_uv(tree, record, specification, uv.outputs[0], seconds, displacement)
        result = _sample(tree, sampling_record, name, textures, coordinates)
        sampled[name] = VfxColor(result.rgba.rgb, result.rgba.alpha, result.limitations)
        limitations.extend(result.limitations)
    main, mask, blend = (sampled[name] for name in ("_MainTex", "_MaskTex", "_BlendTex"))
    tint = colors["_TintColor"]
    rgb = _vec(tree, "Ink Main Tint", "MULTIPLY", main.rgb, tuple(tint[:3]))
    rgb = _scale(tree, "Ink Main Intensity", rgb, floats["_TintColorIntensity"])
    rgb = _vec(tree, "Ink Base RGB", "MULTIPLY", rgb, mask.rgb)
    alpha = _math(tree, "Ink Main Tint Alpha", "MULTIPLY", main.alpha, tint[3] * floats["_TintColorAlpha"])
    alpha = _math(tree, "Ink Base Alpha", "MULTIPLY", alpha, mask.alpha)
    blend_tint = colors["_BlendTint"]
    blend_presence = _clamp(tree, "Ink Blend Presence", _math(
        tree, "Ink Blend Tint Alpha", "MULTIPLY",
        _math(tree, "Ink Blend Alpha Sum", "ADD", alpha, blend.alpha), blend_tint[3]))
    blend_rgb = _vec(tree, "Ink Blend RGB Tint", "MULTIPLY", blend.rgb, tuple(blend_tint[:3]))
    rgb = _vec(tree, "Ink Main Plus Blend", "ADD", rgb,
               _scale(tree, "Ink Blend Contribution", blend_rgb, blend_presence))
    color = VfxColor(rgb, alpha, tuple(limitations))
    if "_DissolveTex" in sampled:
        color = _dissolve(tree, record, color,
                           _component(tree, "Ink Dissolve Red", sampled["_DissolveTex"].rgb, "X"))
    exposure = _host_value(tree, "HOST_POST_EXPOSURE", 1.)
    visibility = _host_value(tree, "HOST_EFFECT_VISIBILITY", 1.)
    rgb = _clamp_source_rgb(tree, _scale(tree, "Ink Host Exposure", color.rgb, exposure))
    alpha = _clamp(tree, "Ink Source Coverage", _math(tree, "Ink Host Visibility", "MULTIPLY", color.alpha, visibility))
    return VfxColor(rgb, alpha, color.limitations)


def make_vfxadvance_ink_material(record: MaterialRecord,
                                 textures: dict[tuple[str, int], tuple[Path, str]],
                                 snapshot_time: float, vertex_color_components: int) -> bpy.types.Material:
    """Export the static appearance with editable, explicitly partial runtime controls."""
    issues = vfxadvance_ink_texture_issues(record, vertex_color_components)
    if issues:
        raise NotImplementedError(f"VFXAdvance ink texture contract is unsupported: material={record['Name']}, issues={issues}")
    if not math.isfinite(snapshot_time):
        raise ValueError(f"VFXAdvance ink time must be finite: material={record['Name']}, value={snapshot_time}")
    identity = hashlib.sha256(json.dumps([record, snapshot_time, "ink_texture_partial/1"],
                                       sort_keys=True, allow_nan=False).encode()).hexdigest()[:24]
    name = f"EF_FX_ADVANCE_INK_{identity}"
    existing = bpy.data.materials.get(name)
    if existing is not None:
        return existing
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.surface_render_method = "BLENDED"
    material.use_transparency_overlap = True
    material.use_backface_culling = record["Floats"]["_CullMode"] == 2
    tree = material.node_tree
    if tree is None:
        raise RuntimeError(f"Blender did not create an ink node tree: material={name}")
    tree.nodes.clear()
    color = _ink_color(tree, record, textures, snapshot_time)
    alpha = color.alpha
    cull = record["Floats"]["_CullMode"]
    if cull != 0:
        geometry = tree.nodes.new("ShaderNodeNewGeometry")
        side = geometry.outputs["Backfacing"]
        if cull == 2:
            side = _math(tree, "Ink Front Face", "SUBTRACT", 1., side)
        alpha = _math(tree, "Ink Source Face Coverage", "MULTIPLY", alpha, side)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = emission.label = "Source Ink Texture Color"
    tree.links.new(color.rgb, emission.inputs["Color"])
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.name = mix.label = "Source Ink Texture Coverage"
    tree.links.new(alpha, mix.inputs[0])
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(mix.outputs[0], output.inputs["Surface"])
    limitations = [*color.limitations,
        "partial_host_ink: HOST_INK_UV_OFFSET is an editable final UV offset; neutral (0,0) replaces the absent world-XZ runtime simulation texture, bounds and facing weight",
        "partial_host_visibility: game activation and renderer fade are not captured; HOST_EFFECT_VISIBILITY is explicitly enabled at 1",
        "partial_host_exposure: game exposure and color grading are unavailable; HOST_POST_EXPOSURE is an editable multiplier set to 1",
        "Source MeshRenderer has no COLOR stream; explicit white input makes both main and blend vertex attenuation neutral",
        "Game motion-vector targets and native transparent compositing are represented by Blender transparent/emission mixing",
    ]
    if "_USE_FOG" in record["RenderState"]["ValidKeywords"]:
        limitations.append("Native volumetric fog buffers are unavailable and omitted from the texture preview")
    material["endfield_fx_source_material"] = record["Name"]
    material["endfield_fx_source"] = record["Source"]
    material["endfield_fx_path_id"] = str(record["PathId"])
    material["endfield_fx_source_material_json"] = json.dumps(record, sort_keys=True, allow_nan=False)
    material["endfield_fx_shader_variant_observed"] = INK_EVIDENCE_VARIANTS[frozenset(record["RenderState"]["ValidKeywords"])]
    material["endfield_fx_branch"] = "vfxadvance_ink_texture_partial"
    material["endfield_fx_status"] = "source_formula_preview_partial"
    material["endfield_fx_limitations"] = json.dumps(limitations)
    layout_shader_node_tree(tree)
    return material
