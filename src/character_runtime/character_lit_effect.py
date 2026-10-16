"""Adapt static three-map LitEffect surfaces using verified source texture inputs.

Blender PBR, roughness remapping and binary alpha clipping are approximations;
the material parameter evidence does not establish compiled game-pass parity.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import bpy

from character_node_graph import node, math_node, color_mix, input_socket

if TYPE_CHECKING:
    from rebuild_character_materials import Identity, MaterialSource, TextureSource


def validate_lit_effect_source(source: MaterialSource, textures: Mapping[Identity, TextureSource]) -> None:
    """Reject enabled branches beyond the static UV0 three-map surface adapter."""
    required_values = {
        "_BaseTextureMapCount": 0.0, "_BaseUVSet": 0.0, "_BasePbrMapUVSet": 0.0,
        "_ShadingModel": 0.0,
        "_EnableDetailMap": 0.0, "_EnableTriChannelMask": 0.0,
        "_EnableUVAnimation": 0.0, "_EnableBakedEmissive": 0.0,
        "_EnableParallaxMap": 0.0,
        "_EnableHoudiniVAT": 0.0, "_EnableInstancing": 0.0,
        "_UseDissolve": 0.0, "_UseCutOff": 0.0,
        "_UseDisturb": 0.0, "_UseVertexOffset": 0.0, "_UseVertexColorMask": 0.0,
    }
    for name, expected in required_values.items():
        if source.floats.get(name) != expected:
            raise ValueError(
                f"Unsupported static LitEffect source branch: material={source.identity}, "
                f"parameter={name}, expected={expected}, actual={source.floats.get(name)}"
            )
    alpha_test = source.floats.get("_EnableAlphaTest")
    emission_enabled = source.floats.get("_EnableEmissiveMap")
    expected_keywords = ({"_ALPHATEST_ON"} if alpha_test else set()) | ({"_EMISSIVE_MAP"} if emission_enabled else set())
    if alpha_test not in (0.0, 1.0) or emission_enabled not in (0.0, 1.0) or set(source.keywords) != expected_keywords:
        raise ValueError(f"Unsupported LitEffect keyword/alpha state: material={source.identity}, keywords={source.keywords}, alpha_test={alpha_test}")
    if alpha_test and source.floats.get("_AlphaMaskChannel") != 0.0:
        raise ValueError(f"Unsupported LitEffect active alpha mask: material={source.identity}, channel={source.floats.get('_AlphaMaskChannel')}")
    if "_EnableCommonVAT" in source.floats and source.floats["_EnableCommonVAT"] != 0.0:
        raise ValueError(f"Unsupported LitEffect CommonVAT branch: material={source.identity}")
    if emission_enabled:
        for name in ("_EnableEmissiveAnim", "_EnableEmissiveAnimSweep", "_EmissiveUVSet", "_EmissiveMaskChannel"):
            if source.floats.get(name) != 0.0:
                raise ValueError(f"Unsupported static LitEffect emission branch: material={source.identity}, parameter={name}, value={source.floats.get(name)}")
        if source.floats.get("_EmissiveType") not in (0.0, 1.0) or source.floats.get("_AlbedoAffectEmissive") not in (0.0, 1.0):
            raise ValueError(f"Unsupported LitEffect emission channel/albedo mode: material={source.identity}")
        for name in ("_EmissiveMapTilling", "_EmissiveRemap", "_EmissiveMinBrightness"):
            if name not in source.floats or source.floats[name] < 0.0:
                raise ValueError(f"Invalid LitEffect source emission parameter: material={source.identity}, parameter={name}, value={source.floats.get(name)}")
        color_names = ("_EmissiveColor",) if source.floats["_EmissiveType"] == 0.0 else ("_EmissiveColor", "_EmissiveColorG", "_EmissiveColorB", "_EmissiveColorA")
        for name in color_names:
            if name not in source.colors:
                raise ValueError(f"Missing LitEffect source emission color: material={source.identity}, parameter={name}")
    for name in ("_AlphaClipThreshold", "_RoughnessMin", "_RoughnessMax", "_Specular", "_BaseColorTintCover", "_OcclusionStrength"):
        value = source.floats.get(name)
        if value is None or not 0.0 <= value <= 1.0:
            raise ValueError(f"Invalid LitEffect source parameter: material={source.identity}, parameter={name}, value={value}")
    if source.floats["_RoughnessMin"] > source.floats["_RoughnessMax"]:
        raise ValueError(f"Inverted LitEffect roughness range: material={source.identity}")
    for name in ("_NormalScale", "_BaseColorBrighterScale"):
        if name not in source.floats or source.floats[name] < 0.0:
            raise ValueError(f"Invalid LitEffect source parameter: material={source.identity}, parameter={name}, value={source.floats.get(name)}")
    if "_BaseColor" not in source.colors:
        raise ValueError(f"Missing LitEffect source BaseColor: material={source.identity}")
    slots = {binding.name: binding for binding in source.textures}
    required_slots = {"_BaseColorMap", "_NormalMap", "_MROMap"} | ({"_EmissiveMap"} if emission_enabled else set())
    if not required_slots <= set(slots) or len(slots) != len(source.textures):
        raise ValueError(f"Static LitEffect requires its active source maps: material={source.identity}, required={sorted(required_slots)}, slots={sorted(slots)}")
    for binding in source.textures:
        texture = textures[binding.identity]
        color_space = 1 if binding.name == "_BaseColorMap" else 0
        if binding.uv_index != 0 or (binding.name in ("_BaseColorMap", "_NormalMap", "_MROMap") and texture.color_space != color_space):
            raise ValueError(f"Unsupported LitEffect texture role: material={source.identity}, slot={binding.name}, uv={binding.uv_index}, color_space={texture.color_space}")
    if textures[slots["_NormalMap"].identity].source_format != "BC5":
        raise ValueError(f"Static LitEffect requires a verified BC5 RG normal: material={source.identity}, format={textures[slots['_NormalMap'].identity].source_format}")


def connect_static_emission(
    source: MaterialSource,
    tree: bpy.types.ShaderNodeTree,
    sample: bpy.types.Node,
    surface: bpy.types.Node,
    albedo: bpy.types.NodeSocket,
) -> None:
    """Approximate static source-map channels without inventing dynamic remapping."""
    section = "LitEffect Emission - APPROX"
    tree["source_emission_scope"] = "Source EmissiveMap R/RGBA and authored HDR tints, UV tiling and albedo toggle drive Blender emission. EmissiveRemap, minimum brightness, post-exposure and compiled LitEffect intensity remain preserved but unreconstructed."
    tiling = node(tree, "ShaderNodeVectorMath", "Source emissive tiling after texture ST", section)
    tiling.operation = "SCALE"
    tiling.inputs["Scale"].default_value = source.floats["_EmissiveMapTilling"]
    tree.links.new(sample.inputs["Vector"].links[0].from_socket, tiling.inputs[0])
    tree.links.new(tiling.outputs["Vector"], sample.inputs["Vector"])
    channels = node(tree, "ShaderNodeSeparateColor", "Source emissive map channels", section)
    tree.links.new(sample.outputs["Color"], channels.inputs[0])
    contributions = [("Red", "_EmissiveColor")]
    if source.floats["_EmissiveType"] == 1.0:
        contributions.extend((("Green", "_EmissiveColorG"), ("Blue", "_EmissiveColorB"), ("Alpha", "_EmissiveColorA")))
    result: bpy.types.NodeSocket | None = None
    for channel, parameter in contributions:
        tint = node(tree, "ShaderNodeRGB", "Source " + parameter, section)
        tint.outputs[0].default_value = source.colors[parameter]
        mask = sample.outputs["Alpha"] if channel == "Alpha" else channels.outputs[channel]
        contribution = color_mix(tree, "MULTIPLY", 1.0, mask, tint.outputs[0], parameter + " times source map " + channel, section)
        result = contribution if result is None else color_mix(tree, "ADD", 1.0, result, contribution, "Sum source emissive channels", section)
    if result is None:
        raise ValueError(f"Static LitEffect emission has no source channel: material={source.identity}")
    if source.floats["_AlbedoAffectEmissive"] == 0.0:
        result = color_mix(tree, "MULTIPLY", 1.0, result, albedo, "Source emission albedo influence approximation", section)
    tree.links.new(result, surface.inputs["Emission Color"])
    surface.inputs["Emission Strength"].default_value = 1.0


def build_lit_effect_group(
    source: MaterialSource,
    tree: bpy.types.ShaderNodeTree,
    samples: Mapping[str, bpy.types.Node],
    layout: Callable[[bpy.types.NodeTree], None],
) -> bpy.types.ShaderNodeTree:
    """Keep source sampling editable and share the scene chain's BC5 decoder."""
    from blender_hgrp_surface import packed_normal_group

    tree["source_shader"] = source.shader
    tree["source_identity"] = json.dumps(source.identity)
    tree["source_floats"] = json.dumps(dict(source.floats), sort_keys=True)
    tree["source_colors"] = json.dumps(dict(source.colors), sort_keys=True)
    tree["source_adapter"] = "EndfieldStaticLitEffect/1"
    tree["source_adapter_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    tree["scope"] = "Verified source texture identities, UV0/ST and BC5 RG decode; Blender PBR, MRO roughness range, alpha clipping and static emissive-map channels approximate LitEffect. Game emission remap/minimum brightness, lighting, AO, scene effects, stencil, culling, exposure and compiled-pass parity remain unverified."
    for name, kind in (("Shader", "NodeSocketShader"), ("Coverage", "NodeSocketFloat"), ("Multiply Factor", "NodeSocketColor")):
        tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type=kind)
    for label, parameter in (("Normal Strength", "_NormalScale"), ("Roughness Min", "_RoughnessMin"), ("Roughness Max", "_RoughnessMax"), ("Specular", "_Specular"), ("Base Brightness", "_BaseColorBrighterScale"), ("Tint Cover", "_BaseColorTintCover"), ("Alpha Threshold", "_AlphaClipThreshold")):
        input_socket(tree, label, "NodeSocketFloat", source.floats[parameter], "Exact source " + parameter + "; Blender evaluation is approximate")
    input_socket(tree, "Base Tint", "NodeSocketColor", source.colors["_BaseColor"], "Exact source _BaseColor")
    entry = node(tree, "NodeGroupInput", "Editable LitEffect source parameters", "Source Parameters")
    output = node(tree, "NodeGroupOutput", "Static LitEffect outputs", "Engine Outputs")
    for binding in source.textures:
        sample = samples[binding.name]
        sample["source_slot"] = binding.name
    color = color_mix(tree, "MULTIPLY", 1.0, samples["_BaseColorMap"].outputs["Color"], entry.outputs["Base Tint"], "Source base texture and tint", "LitEffect PBR - APPROXIMATION")
    color = color_mix(tree, "MULTIPLY", 1.0, color, entry.outputs["Base Brightness"], "Source brightness approximation", "LitEffect PBR - APPROXIMATION")
    color.node.use_clamp = True
    color = color_mix(tree, "MIX", entry.outputs["Tint Cover"], color, entry.outputs["Base Tint"], "Source tint cover approximation", "LitEffect PBR - APPROXIMATION")
    surface = node(tree, "ShaderNodeBsdfPrincipled", "LitEffect Blender PBR approximation", "LitEffect PBR - APPROXIMATION")
    tree.links.new(color, surface.inputs["Base Color"])
    if source.floats["_EnableEmissiveMap"] == 1.0:
        connect_static_emission(source, tree, samples["_EmissiveMap"], surface, color)
    tree.links.new(entry.outputs["Specular"], surface.inputs["Specular IOR Level"])
    mro = node(tree, "ShaderNodeSeparateColor", "Source MRO: R metallic / G roughness / B occlusion archive", "LitEffect PBR - APPROXIMATION")
    tree.links.new(samples["_MROMap"].outputs["Color"], mro.inputs[0])
    tree.links.new(mro.outputs["Red"], surface.inputs["Metallic"])
    roughness = node(tree, "ShaderNodeMapRange", "Source roughness range approximation", "LitEffect PBR - APPROXIMATION")
    roughness.clamp = True
    tree.links.new(mro.outputs["Green"], roughness.inputs["Value"])
    tree.links.new(entry.outputs["Roughness Min"], roughness.inputs["To Min"])
    tree.links.new(entry.outputs["Roughness Max"], roughness.inputs["To Max"])
    tree.links.new(roughness.outputs["Result"], surface.inputs["Roughness"])
    packed = node(tree, "ShaderNodeGroup", "Shared scene BC5 RG normal decode", "Verified Source Texture Inputs")
    packed.node_tree = packed_normal_group()
    packed.inputs["Multiply Red By Alpha"].default_value = 0.0
    tree.links.new(samples["_NormalMap"].outputs["Color"], packed.inputs["Packed RGB"])
    tree.links.new(samples["_NormalMap"].outputs["Alpha"], packed.inputs["Packed Alpha"])
    tree.links.new(entry.outputs["Normal Strength"], packed.inputs["Normal Strength"])
    normal = node(tree, "ShaderNodeNormalMap", "Source tangent normal UV0", "Verified Source Texture Inputs")
    normal.uv_map = "UV0"
    tree.links.new(packed.outputs["Encoded Normal"], normal.inputs["Color"])
    tree.links.new(normal.outputs["Normal"], surface.inputs["Normal"])
    tree.links.new(surface.outputs[0], output.inputs["Shader"])
    if source.floats["_EnableAlphaTest"] == 1.0:
        coverage = math_node(tree, "GREATER_THAN", samples["_BaseColorMap"].outputs["Alpha"], entry.outputs["Alpha Threshold"], "Source BaseColor alpha threshold approximation", "LitEffect Alpha - APPROXIMATION")
        tree.links.new(coverage, output.inputs["Coverage"])
    else:
        output.inputs["Coverage"].default_value = 1.0
    output.inputs["Multiply Factor"].default_value = (1.0, 1.0, 1.0, 1.0)
    layout(tree)
    return tree
