"""Source-color nodes for the verified VFXBaseV2 texture-role variants."""
from __future__ import annotations

import math
from pathlib import Path
from typing import NamedTuple

import bpy

from blender_scene_materials import texture_image
from endfield_scene_material_contract import MaterialRecord, TextureSlot, reference_key, resolve_export_path


class VfxColor(NamedTuple):
    rgb: bpy.types.NodeSocket
    alpha: bpy.types.NodeSocket
    limitations: tuple[str, ...]


class _Rgba(NamedTuple):
    rgb: bpy.types.NodeSocket
    alpha: bpy.types.NodeSocket


def _link(tree: bpy.types.NodeTree, target: bpy.types.NodeSocket,
          value: bpy.types.NodeSocket | float | tuple[float, ...]) -> None:
    if isinstance(value, bpy.types.NodeSocket):
        tree.links.new(value, target)
    else:
        target.default_value = value


def _math(tree: bpy.types.NodeTree, label: str, operation: str,
          left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeMath")
    node.name = node.label = label
    node.operation = operation
    _link(tree, node.inputs[0], left)
    _link(tree, node.inputs[1], right)
    return node.outputs[0]


def _vec(tree: bpy.types.NodeTree, label: str, operation: str,
         left: bpy.types.NodeSocket | tuple[float, ...],
         right: bpy.types.NodeSocket | tuple[float, ...]) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.name = node.label = label
    node.operation = operation
    _link(tree, node.inputs[0], left)
    _link(tree, node.inputs[1], right)
    return node.outputs[0]


def _scale(tree: bpy.types.NodeTree, label: str, vector: bpy.types.NodeSocket,
           factor: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.name = node.label = label
    node.operation = "SCALE"
    tree.links.new(vector, node.inputs[0])
    _link(tree, node.inputs[3], factor)
    return node.outputs[0]


def _value(tree: bpy.types.NodeTree, label: str, number: float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeValue")
    node.name = node.label = label
    node.outputs[0].default_value = number
    node["endfield_layout_section"] = "Explicit Preview Conditions"
    return node.outputs[0]


def _component(tree: bpy.types.NodeTree, label: str, vector: bpy.types.NodeSocket,
               channel: str) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeSeparateXYZ")
    node.name = node.label = label
    tree.links.new(vector, node.inputs[0])
    return node.outputs[channel]


def _xy(tree: bpy.types.NodeTree, label: str, x: bpy.types.NodeSocket,
        y: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeCombineXYZ")
    node.name = node.label = label
    tree.links.new(x, node.inputs["X"])
    tree.links.new(y, node.inputs["Y"])
    return node.outputs[0]


def _slot(record: MaterialRecord, name: str) -> TextureSlot:
    matches = [slot for slot in record["Textures"] if slot["Property"] == name]
    if len(matches) != 1 or matches[0]["Texture"] is None:
        raise FileNotFoundError(f"VFX texture binding missing: material={record['Name']}, property={name}")
    return matches[0]


def vfx_custom1_channels(record: MaterialRecord) -> tuple[str, ...]:
    """Return the Custom1 components read by the active compiled source path."""
    floats = record["Floats"]
    if floats.get("_InParticle", 0) == 0:
        return ()
    return tuple(channel for channel, active in (
        ("z", floats.get("_UseDissolve", 0) != 0),
        ("w", floats.get("_UseDisturb", 0) != 0 and floats.get("_DisturbUseWeight", 0) != 0),
    ) if active)


def vfx_feature_issues(record: MaterialRecord, vertex_streams: tuple[int, ...]) -> tuple[str, ...]:
    """Identify material controls outside the source-color graph's verified domain."""
    floats, colors = record["Floats"], record["Colors"]
    issues: list[str] = []
    if record["Shader"].get("Name") != "HGRP/Effect/VFXBaseV2":
        issues.append("shader_family")
    if vertex_streams not in ((0, 1, 3, 4), (0, 1, 3, 4, 5, 34)):
        issues.append("particle_vertex_layout")
    if vfx_custom1_channels(record) and vertex_streams != (0, 1, 3, 4, 5, 34):
        issues.append("custom1_vertex_stream_missing")
    if floats.get("_InParticle") not in (0, 1):
        issues.append("particle_shader_mode")
    excluded = (
        "_UseBlend", "_UseFresnel", "_UseBright", "_UseLighting", "_UseVertexOffset",
        "_UseVATColorTex", "_EnableHoudiniVAT", "_UseVFXPortal", "_UseTrail",
        "_UseAnimBreathing", "_UseEdgeColor", "_UseScreenUV", "_UsePolarUV",
        "_UsePosYAsScreenV", "_UseParticleDisturb", "_UseWaterBlend",
    )
    issues.extend(name for name in excluded if floats.get(name, 0) != 0)
    if floats.get("_DissolveUseWeight", 0) != 0 or floats.get("_Bi_Disturb", 0) != 0:
        issues.append("weighted_or_bidirectional_dissolve_disturb")
    if floats.get("_DisturbTex1Normal", 0) != 0 or floats.get("_DisturbTex2Normal", 0) != 0:
        issues.append("normal_channel_disturb")
    if floats.get("_UseDisturb", 0) and not floats.get("_UseSampleTex0", 0):
        issues.append("disturb_without_sample_texture")
    roles = [int(floats.get(f"_SampleTex{index}Use", 0)) for index in range(4)
             if floats.get(f"_UseSampleTex{index}", 0)]
    if roles != sorted(roles):
        issues.append("sample_role_order")
    if floats.get("_UseDisturb", 0) and 0 not in roles:
        issues.append("disturb_without_verified_source")
    if floats.get("_UseDisturb", 0) and floats.get("_UseDisturb2", 0) and 1 not in roles:
        issues.append("disturb2_without_verified_source")
    if floats.get("_UseDissolve", 0) and 5 not in roles:
        issues.append("dissolve_without_verified_source")
    if floats.get("_UseMask", 0) and 3 not in roles:
        issues.append("mask_without_verified_source")
    enabled_samples = [index for index in range(4) if floats.get(f"_UseSampleTex{index}", 0)]
    if enabled_samples != list(range(len(enabled_samples))):
        issues.append("noncontiguous_sample_keywords")
    if floats.get("_MainUVSet", 0) != 0:
        issues.append("main_uv_set")
    for index in range(7):
        name = "_MainTex" if index == 6 else f"_SampleTex{index}"
        enabled = index == 6 or floats.get(f"_UseSampleTex{index}", 0) != 0
        if not enabled:
            continue
        if index != 6 and index >= 4:
            issues.append(f"sample_texture_{index}")
            continue
        if name not in {slot["Property"] for slot in record["Textures"] if slot["Texture"] is not None}:
            issues.append(f"missing_{name}")
        if colors.get(f"{name}UVWeights") != [1, 0, 0, 0]:
            issues.append(f"{name}_custom_uv")
        speed = colors.get(f"{name}UVSpeed")
        if not isinstance(speed, list) or len(speed) != 4 or speed[2:] != [0, 0]:
            issues.append(f"{name}_uv_speed")
        rotation = colors.get(f"{name}UVRotateMat")
        if not isinstance(rotation, list) or len(rotation) != 4:
            issues.append(f"{name}_uv_rotation")
        if index < 4:
            role = floats.get(f"{name}Use", 0)
            if role not in (0, 1, 3, 5):
                issues.append(f"{name}_role")
            weights = [floats.get(f"{name}UseWeight{r}", 0) for r in range(6)]
            if role in (0, 1, 3, 5) and weights != [int(r == role) for r in range(6)]:
                issues.append(f"{name}_role_weights")
            if floats.get(f"{name}UseDisturb", 0) not in (0, 1):
                issues.append(f"{name}_disturb_mode")
    if floats.get("_MainTexUseDisturb", 0) not in (0, 1):
        issues.append("main_disturb_mode")
    return tuple(issues)


def _uv(tree: bpy.types.NodeTree, record: MaterialRecord, name: str,
        base_uv: bpy.types.NodeSocket, snapshot_time: float,
        disturbance: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    slot = _slot(record, name)
    colors = record["Colors"]
    speed, rotation = colors[f"{name}UVSpeed"], colors[f"{name}UVRotateMat"]
    moving = _vec(tree, f"{name} UV Speed", "ADD", base_uv,
                  (speed[0] * snapshot_time, speed[1] * snapshot_time, 0.0))
    centered = _vec(tree, f"{name} UV Center", "SUBTRACT", moving, (0.5, 0.5, 0.0))
    x = _component(tree, f"{name} UV X", centered, "X")
    y = _component(tree, f"{name} UV Y", centered, "Y")
    rx = _math(tree, f"{name} Rotate X", "ADD",
               _math(tree, f"{name} Rotate XX", "MULTIPLY", x, rotation[0]),
               _math(tree, f"{name} Rotate XY", "MULTIPLY", y, rotation[2]))
    ry = _math(tree, f"{name} Rotate Y", "ADD",
               _math(tree, f"{name} Rotate YX", "MULTIPLY", x, rotation[1]),
               _math(tree, f"{name} Rotate YY", "MULTIPLY", y, rotation[3]))
    rotated = _vec(tree, f"{name} UV Restore Center", "ADD",
                   _xy(tree, f"{name} Rotated UV", rx, ry), (0.5, 0.5, 0.0))
    scale = slot["Scale"]
    offset = slot["Offset"]
    mapped = _vec(tree, f"{name} UV Scale", "MULTIPLY", rotated, (scale[0], scale[1], 1.0))
    mapped = _vec(tree, f"{name} UV Offset", "ADD", mapped, (offset[0], offset[1], 0.0))
    return _vec(tree, f"{name} Distorted UV", "ADD", mapped, disturbance)


def _sample(tree: bpy.types.NodeTree, record: MaterialRecord, name: str,
            textures: dict[tuple[str, int], tuple[Path, str]],
            uv: bpy.types.NodeSocket) -> _Rgba:
    slot = _slot(record, name)
    reference = slot["Texture"]
    if reference is None:
        raise FileNotFoundError(f"VFX texture binding missing: material={record['Name']}, property={name}")
    key = reference_key(reference)
    if key not in textures:
        raise FileNotFoundError(f"VFX texture was not decoded: material={record['Name']}, property={name}, reference={key}")
    root, filename = textures[key]
    path = resolve_export_path(root / filename)
    if not path.is_file():
        raise FileNotFoundError(f"VFX exported texture missing: material={record['Name']}, property={name}, path={path}")
    color_space = reference.get("ColorSpace")
    if color_space not in (0, 1):
        raise ValueError(f"VFX texture color space missing: material={record['Name']}, property={name}, reference={key}")
    node = tree.nodes.new("ShaderNodeTexImage")
    node.name = node.label = f"Source {name}"
    node.image = texture_image(path, "Non-Color" if color_space == 0 else "sRGB", "STRAIGHT")
    node.extension = "REPEAT"
    tree.links.new(uv, node.inputs["Vector"])
    node["endfield_layout_section"] = "Source Texture and UV"
    if record["Floats"].get(f"_Use{name[1:]}AsAlpha", 0) == 0:
        return _Rgba(node.outputs["Color"], node.outputs["Alpha"])
    red = _component(tree, f"{name} Red as Alpha", node.outputs["Color"], "X")
    white = _vec(tree, f"{name} White RGB", "ADD", (1.0, 1.0, 1.0), (0.0, 0.0, 0.0))
    return _Rgba(white, red)


def _add_rgba(tree: bpy.types.NodeTree, label: str, left: _Rgba,
              right: _Rgba) -> _Rgba:
    return _Rgba(_vec(tree, f"{label} RGB", "ADD", left.rgb, right.rgb),
                 _math(tree, f"{label} Alpha", "ADD", left.alpha, right.alpha))


def _disturb(tree: bpy.types.NodeTree, label: str, sample: _Rgba,
             u: float, v: float, weight: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    red = _component(tree, f"{label} Red", sample.rgb, "X")
    weighted = _math(tree, f"{label} Particle Weight", "MULTIPLY", red, weight)
    return _scale(tree, f"{label} UV Offset", _vec(tree, f"{label} Intensities", "ADD",
                                                     (u, v, 0.0), (0.0, 0.0, 0.0)), weighted)


def make_vfx_feature_source(tree: bpy.types.NodeTree, record: MaterialRecord,
                            textures: dict[tuple[str, int], tuple[Path, str]],
                            snapshot_time: float, vertex_streams: tuple[int, ...]) -> VfxColor:
    """Build the shader's pre-exposure RGBA from verified texture-role controls."""
    issues = vfx_feature_issues(record, vertex_streams)
    if issues:
        raise NotImplementedError(f"VFX source controls unsupported: material={record['Name']}, issues={issues}")
    if type(snapshot_time) not in (int, float) or not math.isfinite(snapshot_time):
        raise ValueError(f"VFX snapshot time must be finite: material={record['Name']}, value={snapshot_time}")
    floats, colors = record["Floats"], record["Colors"]
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.uv_map = "UVMap"
    zero = _vec(tree, "No UV Disturbance", "ADD", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    channels = vfx_custom1_channels(record)
    custom = None
    if channels:
        custom = tree.nodes.new("ShaderNodeAttribute")
        custom.name = custom.label = "Particle Custom1 XYZW"
        custom.attribute_name = "EF_FX_CUSTOM1"
        custom["endfield_layout_section"] = "Particle Vertex Streams"
    disturb_weight: bpy.types.NodeSocket | float = 1.0
    if "w" in channels:
        if custom is None:
            raise ValueError(f"VFX Custom1 node missing: material={record['Name']}, channel=w")
        disturb_weight = _math(tree, "Particle Disturb Weight", "MULTIPLY",
                                custom.outputs["Alpha"], floats["_InParticle"])
    disturbance = zero
    role_samples: dict[int, list[_Rgba]] = {0: [], 1: [], 3: [], 5: []}
    for index in range(4):
        if floats.get(f"_UseSampleTex{index}", 0) == 0:
            continue
        name = f"_SampleTex{index}"
        role = int(floats[f"{name}Use"])
        sampled_uv = _uv(tree, record, name, uv.outputs[0], snapshot_time,
                         disturbance if floats.get(f"{name}UseDisturb", 0) else zero)
        sampled = _sample(tree, record, name, textures, sampled_uv)
        role_samples[role].append(sampled)
        if role == 0 and floats.get("_UseDisturb", 0):
            disturbance = _vec(tree, "Disturb 1 Accumulation", "ADD", disturbance,
                               _disturb(tree, "Disturb 1", sampled,
                                        floats.get("_DisturbUIntensity1", 0),
                                        floats.get("_DisturbVIntensity1", 0), disturb_weight))
        elif role == 1 and floats.get("_UseDisturb", 0) and floats.get("_UseDisturb2", 0):
            disturbance = _vec(tree, "Disturb 2 Accumulation", "ADD", disturbance,
                               _disturb(tree, "Disturb 2", sampled,
                                        floats.get("_DisturbUIntensity2", 0),
                                        floats.get("_DisturbVIntensity2", 0), disturb_weight))
    main_uv = _uv(tree, record, "_MainTex", uv.outputs[0], snapshot_time,
                  disturbance if floats.get("_MainTexUseDisturb", 0) else zero)
    main = _sample(tree, record, "_MainTex", textures, main_uv)
    tint = colors["_TintColor"]
    rgb = _scale(tree, "Source Tint Intensity",
                 _vec(tree, "Source Tint RGB", "MULTIPLY", main.rgb, tuple(tint[:3])),
                 floats["_TintColorIntensity"])
    alpha = _math(tree, "Source Tint Alpha", "MULTIPLY", main.alpha,
                  tint[3] * floats["_TintColorAlpha"])
    if floats.get("_DisableVertColor", 0) == 0:
        vertex = tree.nodes.new("ShaderNodeVertexColor")
        vertex.name = vertex.label = "Particle Initial and Lifetime Color"
        vertex.layer_name = "EF_FX_COLOR"
        rgb = _vec(tree, "Particle Color", "MULTIPLY", rgb, vertex.outputs["Color"])
        alpha = _math(tree, "Particle Opacity", "MULTIPLY", alpha, vertex.outputs["Alpha"])
    mask = _Rgba(_vec(tree, "Mask Base RGB", "ADD", (1.0 - floats.get("_UseMask", 0),) * 3,
                      (0.0, 0.0, 0.0)),
                 _value(tree, "Mask Base Alpha", 1.0 - floats.get("_UseMask", 0)))
    for index, sample in enumerate(role_samples[3]):
        mask = _add_rgba(tree, f"Mask Layer {index}", mask, sample)
    rgb = _vec(tree, "Apply Mask RGB", "MULTIPLY", rgb, mask.rgb)
    alpha = _math(tree, "Apply Mask Alpha", "MULTIPLY", alpha, mask.alpha)
    limitations: list[str] = []
    if floats.get("_UseDissolve", 0):
        if "z" in channels:
            if custom is None:
                raise ValueError(f"VFX Custom1 node missing: material={record['Name']}, channel=z")
            custom_z: bpy.types.NodeSocket | float = _math(
                tree, "Particle Dissolve Schedule", "MULTIPLY",
                _component(tree, "Custom1 Z", custom.outputs["Color"], "Z"),
                floats["_InParticle"])
        else:
            custom_z = 0.0
        dissolve = _value(tree, "Dissolve Base", 0.0)
        for index, sample in enumerate(role_samples[5]):
            dissolve = _math(tree, f"Dissolve Layer {index}", "ADD", dissolve,
                             _component(tree, f"Dissolve Red {index}", sample.rgb, "X"))
        schedule = _math(tree, "Dissolve Schedule", "ADD",
                         floats.get("_DissolveScheduleOffset", 0), custom_z)
        schedule = _math(tree, "Dissolve Schedule 2.02", "MULTIPLY", schedule, 2.02)
        schedule = _math(tree, "Dissolve Schedule Shift", "SUBTRACT", schedule, 1.01)
        delta = _math(tree, "Dissolve Delta", "SUBTRACT", dissolve, schedule)
        factor = _math(tree, "Dissolve Edge Sharpness", "MULTIPLY", delta,
                       floats["_DissolveEdgeSharp"])
        factor = _math(tree, "Dissolve Clamp Low", "MAXIMUM", factor, 0.0)
        factor = _math(tree, "Dissolve Clamp High", "MINIMUM", factor, 1.0)
        alpha = _math(tree, "Dissolve Alpha", "MULTIPLY", alpha, factor)
        edge = _math(tree, "Emissive Edge Distance", "SUBTRACT",
                     floats.get("_DissolveEmissiveEdge", 0), delta)
        edge = _math(tree, "Emissive Edge Sharpness", "MULTIPLY", edge,
                     floats["_DissolveEdgeSharp"])
        edge = _math(tree, "Emissive Edge Low", "MAXIMUM", edge, 0.0)
        edge = _math(tree, "Emissive Edge High", "MINIMUM", edge, 1.0)
        emissive = colors["_DissolveEmissiveColor"]
        target = _scale(tree, "Dissolve Emissive Tint",
                        _vec(tree, "Dissolve Emissive RGB", "ADD", tuple(emissive[:3]),
                             (0.0, 0.0, 0.0)), floats["_TintColorIntensity"])
        target = _scale(tree, "Dissolve Emissive Edge Premultiply", target, edge)
        rgb = _vec(tree, "Dissolve Emissive Mix", "ADD",
                   _scale(tree, "Dissolve Base RGB", rgb,
                          _math(tree, "One Minus Emissive Edge", "SUBTRACT", 1.0, edge)),
                   _scale(tree, "Dissolve Edge RGB", target, edge))
    return VfxColor(rgb, alpha, tuple(limitations))
