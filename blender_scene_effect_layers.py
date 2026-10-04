"""Source-color nodes for the verified VFXBaseV2 texture-role variants."""
from __future__ import annotations

import math
from pathlib import Path
from typing import NamedTuple

import bpy

from blender_scene_materials import texture_image
from endfield_scene_material_contract import (AssetReference, MaterialRecord, TextureSlot,
                                              reference_key, resolve_export_path,
                                              validate_texture_sampler, has_shader_white_texture_default)


class VfxColor(NamedTuple):
    rgb: bpy.types.NodeSocket
    alpha: bpy.types.NodeSocket
    limitations: tuple[str, ...]


class _Rgba(NamedTuple):
    rgb: bpy.types.NodeSocket
    alpha: bpy.types.NodeSocket


class _SamplerSettings(NamedTuple):
    extension: str | None
    interpolation: str
    limitations: tuple[str, ...]
    issue: str | None


class _SampleResult(NamedTuple):
    rgba: _Rgba
    limitations: tuple[str, ...]


def _sampler_settings(record: MaterialRecord, name: str,
                      reference: AssetReference) -> _SamplerSettings:
    if "TextureSampler" not in reference:
        return _SamplerSettings("REPEAT", "Linear",
                                (f"{name} source sampler settings unverified; using legacy Linear/REPEAT preview",),
                                None)
    sampler = validate_texture_sampler(reference["TextureSampler"],
                                       f"material={record['Name']}, property={name}")
    u, v = sampler["WrapU"], sampler["WrapV"]
    if (u, v) == (0, 0):
        extension = "REPEAT"
    elif (u, v) == (1, 1):
        extension = "EXTEND"
    else:
        return _SamplerSettings(None, "Linear", (), f"{name}_sampler_wrap_u{u}_v{v}")
    limitations = tuple(reason for condition, reason in (
        (sampler["FilterMode"] == 2, f"{name} trilinear mip filtering approximated by Blender Linear"),
        (sampler["Aniso"] > 1, f"{name} anisotropic filtering not reconstructed"),
        (sampler["MipBias"] != 0, f"{name} mip bias not reconstructed"),
    ) if condition)
    return _SamplerSettings(extension, "Closest" if sampler["FilterMode"] == 0 else "Linear",
                            limitations, None)


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
    if len(matches) != 1:
        raise FileNotFoundError(f"VFX texture binding missing: material={record['Name']}, property={name}")
    return matches[0]


def _uv_source_slot(record: MaterialRecord, name: str) -> TextureSlot:
    role_slots = ("_DisturbTex1", "_DisturbTex2", "_WeightTex", "_MaskTex", "_BlendTex", "_DissolveTex")
    source_name = name
    if name.startswith("_SampleTex"):
        role = record["Floats"].get(f"{name}Use")
        if type(role) not in (int, float) or int(role) != role or not 0 <= role < len(role_slots):
            raise ValueError(f"VFX sample texture role invalid: material={record['Name']}, property={name}, role={role}")
        source_name = role_slots[int(role)]
    matches = [slot for slot in record["Textures"] if slot["Property"] == source_name]
    if len(matches) != 1:
        raise ValueError(f"VFX source-role UV slot must be unique: material={record['Name']}, property={name}, role_slot={source_name}, matches={len(matches)}")
    return matches[0]


def vfx_custom1_channels(record: MaterialRecord) -> tuple[str, ...]:
    """Return the Custom1 components read by the active compiled source path."""
    floats = record["Floats"]
    if floats.get("_InParticle", 0) == 0:
        return ()
    colors = record["Colors"]
    main_speed = colors.get("_MainTexUVSpeed")
    sample_speeds = (colors.get(f"_SampleTex{index}UVSpeed") for index in range(5)
                     if floats.get(f"_UseSampleTex{index}", 0))
    main_custom_uv = isinstance(main_speed, list) and len(main_speed) == 4 and main_speed[2:] != [0, 0]
    sample_custom_uv = any(isinstance(speed, list) and len(speed) == 4 and speed[2:] != [0, 0]
                           for speed in sample_speeds)
    return tuple(channel for channel, active in (
        ("x", main_custom_uv),
        ("y", sample_custom_uv),
        ("z", floats.get("_UseDissolve", 0) != 0),
        ("w", floats.get("_UseDisturb", 0) != 0 and floats.get("_UseParticleDisturb", 0) != 0),
    ) if active)


def _single_weight_sample(record: MaterialRecord) -> int | None:
    """Select the verified one-hot weight stage after source disturbance stages."""
    floats = record["Floats"]
    if (any(floats.get(name) != 1 for name in ("_UseWeightTex", "_UseDissolve", "_DissolveUseWeight", "_UseDisturb")) or
            floats.get("_Bi_Disturb") != 0 or floats.get("_DisturbUseWeight") not in (0, 1)):
        return None
    active = tuple(index for index in range(5) if floats.get(f"_UseSampleTex{index}") == 1)
    weights = tuple(index for index in active if floats.get(f"_SampleTex{index}Use") == 2)
    if len(weights) != 1:
        return None
    selected = weights[0]
    if selected not in (1, 2) or tuple(floats.get(f"_SampleTex{index}Use") for index in range(selected)) != tuple(range(selected)):
        return None
    if [floats.get(f"_SampleTex{selected}UseWeight{role}", 0) for role in range(6)] != [0, 0, 1, 0, 0, 0]:
        return None
    if any(floats.get(f"_SampleTex{index}UseWeight2", 0) != 0 for index in active if index != selected):
        return None
    return selected


def _material_feature_issues(record: MaterialRecord) -> tuple[str, ...]:
    """Identify shader controls outside the verified source-color graph."""
    floats, colors = record["Floats"], record["Colors"]
    weight_sample = _single_weight_sample(record)
    issues: list[str] = []
    if record["Shader"].get("Name") != "HGRP/Effect/VFXBaseV2":
        issues.append("shader_family")
    if floats.get("_InParticle") not in (0, 1):
        issues.append("particle_shader_mode")
    excluded = (
        "_UseFresnel", "_UseBright", "_UseLighting", "_UseVertexOffset",
        "_UseVATColorTex", "_EnableHoudiniVAT", "_UseVFXPortal", "_UseTrail",
        "_UseAnimBreathing", "_UseEdgeColor", "_UseScreenUV", "_UsePolarUV",
        "_UsePosYAsScreenV", "_UseParticleDisturb", "_UseWaterBlend",
    )
    issues.extend(name for name in excluded if floats.get(name, 0) != 0)
    if (floats.get("_DissolveUseWeight", 0) != 0 or floats.get("_Bi_Disturb", 0) != 0) and weight_sample is None:
        issues.append("weighted_or_bidirectional_dissolve_disturb")
    if floats.get("_DisturbTex1Normal", 0) != 0 or floats.get("_DisturbTex2Normal", 0) != 0:
        issues.append("normal_channel_disturb")
    if floats.get("_UseDisturb", 0) and not floats.get("_UseSampleTex0", 0):
        issues.append("disturb_without_sample_texture")
    roles = [int(floats.get(f"_SampleTex{index}Use", 0)) for index in range(5)
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
    if floats.get("_UseBlend", 0) and 4 not in roles:
        issues.append("blend_without_verified_source")
    if 4 in roles and floats.get("_UseBlend", 0) != 1:
        issues.append("blend_role_without_enabled_blend")
    if 4 in roles:
        blend_tint = colors.get("_BlendTint")
        if (not isinstance(blend_tint, list) or len(blend_tint) != 4 or
                any(type(value) not in (int, float) or not math.isfinite(value) for value in blend_tint)):
            issues.append("blend_tint")
    enabled_samples = [index for index in range(6) if floats.get(f"_UseSampleTex{index}", 0)]
    if enabled_samples != list(range(len(enabled_samples))):
        issues.append("noncontiguous_sample_keywords")
    if floats.get("_MainUVSet", 0) != 0:
        issues.append("main_uv_set")
    for index in range(7):
        name = "_MainTex" if index == 6 else f"_SampleTex{index}"
        enabled = index == 6 or floats.get(f"_UseSampleTex{index}", 0) != 0
        if not enabled:
            continue
        if index != 6 and index >= 5:
            issues.append(f"sample_texture_{index}")
            continue
        bindings = [slot for slot in record["Textures"] if slot["Property"] == name]
        if (len(bindings) != 1 or
                (bindings[0]["Texture"] is None and not has_shader_white_texture_default(record, name))):
            issues.append(f"missing_{name}")
        source_slots = [slot for slot in record["Textures"]
                        if slot["Property"] == name and slot["Texture"] is not None]
        if len(source_slots) == 1:
            sampler_issue = _sampler_settings(record, name, source_slots[0]["Texture"]).issue
            if sampler_issue is not None:
                issues.append(sampler_issue)
        if colors.get(f"{name}UVWeights") != [1, 0, 0, 0]:
            issues.append(f"{name}_custom_uv")
        speed = colors.get(f"{name}UVSpeed")
        if (not isinstance(speed, list) or len(speed) != 4 or
                any(type(value) not in (int, float) or not math.isfinite(value) for value in speed)):
            issues.append(f"{name}_uv_speed")
        # The source shader multiplies custom UV speed by _InParticle.
        # Non-particle materials retain these coefficients but contribute zero.
        rotation = colors.get(f"{name}UVRotateMat")
        if not isinstance(rotation, list) or len(rotation) != 4:
            issues.append(f"{name}_uv_rotation")
        if index < 5:
            role = floats.get(f"{name}Use", 0)
            if role not in (0, 1, 3, 4, 5) and index != weight_sample:
                issues.append(f"{name}_role")
            weights = [floats.get(f"{name}UseWeight{r}", 0) for r in range(6)]
            if role in (0, 1, 3, 4, 5) and weights != [int(r == role) for r in range(6)]:
                issues.append(f"{name}_role_weights")
            if floats.get(f"{name}UseDisturb", 0) not in (0, 1):
                issues.append(f"{name}_disturb_mode")
    if floats.get("_MainTexUseDisturb", 0) not in (0, 1):
        issues.append("main_disturb_mode")
    return tuple(issues)


def _particle_texcoord_channels(vertex_streams: tuple[int, ...]) -> dict[int, tuple[int, ...]] | None:
    """Resolve the verified Unity 2021.3 stream subset into scalar TEXCOORD slots."""
    # Position/Normal/Tangent/Color are separate attributes; the remaining streams pack in order.
    widths = {0: 0, 1: 0, 2: 0, 3: 0, 4: 2, 5: 2, 34: 4, 38: 4}
    if len(set(vertex_streams)) != len(vertex_streams) or any(stream not in widths for stream in vertex_streams):
        return None
    cursor = 0
    channels: dict[int, tuple[int, ...]] = {}
    for stream in vertex_streams:
        width = widths[stream]
        channels[stream] = tuple(range(cursor, cursor + width))
        cursor += width
    return channels


def vfx_feature_issues(record: MaterialRecord, vertex_streams: tuple[int, ...]) -> tuple[str, ...]:
    """Validate source controls and a real ParticleSystemRenderer vertex layout."""
    issues: list[str] = []
    channels = _particle_texcoord_channels(vertex_streams)
    if (channels is None or 0 not in channels or channels.get(4) != (0, 1) or
            (record["Floats"].get("_DisableVertColor", 0) == 0 and 3 not in channels)):
        issues.append("particle_vertex_layout")
    # Supported graph branches consume Custom1 from TEXCOORD1, never unused trailing Custom2.
    if vfx_custom1_channels(record) and (channels is None or channels.get(34) != (4, 5, 6, 7)):
        issues.append("custom1_vertex_stream_missing")
    return (*issues, *_material_feature_issues(record))


def vfx_mesh_feature_issues(record: MaterialRecord, vertex_color_components: int) -> tuple[str, ...]:
    """Validate source controls against MeshRenderer color evidence."""
    issues: list[str] = []
    if vertex_color_components not in (0, 4):
        issues.append("mesh_vertex_color_components")
    if record["Floats"].get("_InParticle", 0) != 0:
        issues.append("particle_input_on_mesh_renderer")
    if record["Floats"].get("_DisableVertColor", 0) == 0 and vertex_color_components == 0:
        issues.append("mesh_vertex_color_missing")
    source_issues = list(_material_feature_issues(record))
    if (record["Floats"].get("_InParticle", 0) == 0 and
            record["Colors"].get("_SampleTex1UVWeights") == [0, 1, 0, 0] and
            "_SampleTex1_custom_uv" in source_issues):
        source_issues.remove("_SampleTex1_custom_uv")
    return (*issues, *source_issues)


def _uv(tree: bpy.types.NodeTree, record: MaterialRecord, name: str,
        base_uv: bpy.types.NodeSocket, snapshot_time: float,
        disturbance: bpy.types.NodeSocket,
        custom_speed: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    slot = _uv_source_slot(record, name)
    colors = record["Colors"]
    speed, rotation = colors[f"{name}UVSpeed"], colors[f"{name}UVRotateMat"]
    time_node = tree.nodes.get("EF FX Source Time Seconds")
    if time_node is None:
        seconds = _value(tree, "EF FX Source Time Seconds", snapshot_time)
    else:
        seconds = time_node.outputs[0]
    speed_x = _math(tree, f"{name} Speed X", "MULTIPLY", seconds, speed[0])
    speed_y = _math(tree, f"{name} Speed Y", "MULTIPLY", seconds, speed[1])
    if speed[2] != 0:
        speed_x = _math(tree, f"{name} Custom Speed X", "ADD", speed_x,
                        _math(tree, f"{name} Custom X", "MULTIPLY", custom_speed, speed[2]))
    if speed[3] != 0:
        speed_y = _math(tree, f"{name} Custom Speed Y", "ADD", speed_y,
                        _math(tree, f"{name} Custom Y", "MULTIPLY", custom_speed, speed[3]))
    offset_uv = _xy(tree, f"{name} Time Offset", speed_x, speed_y)
    moving = _vec(tree, f"{name} UV Speed", "ADD", base_uv, offset_uv)
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
            uv: bpy.types.NodeSocket) -> _SampleResult:
    slot = _slot(record, name)
    reference = slot["Texture"]
    if reference is None and has_shader_white_texture_default(record, name):
        rgb = tree.nodes.new("ShaderNodeRGB")
        rgb.name = rgb.label = f"Source {name} - Shader Default White"
        rgb.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)
        rgb["endfield_layout_section"] = "Source Texture and UV"
        rgb["endfield_source_default"] = "serialized_shader_texture_property:white:2D"
        return _SampleResult(_Rgba(rgb.outputs[0], _value(tree, f"{name} Default Alpha", 1.0)), ())
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
    sampler = _sampler_settings(record, name, reference)
    if sampler.issue is not None or sampler.extension is None:
        raise NotImplementedError(
            f"VFX source sampler unsupported: material={record['Name']}, property={name}, issue={sampler.issue}")
    node = tree.nodes.new("ShaderNodeTexImage")
    node.name = node.label = f"Source {name}"
    node.image = texture_image(path, "Non-Color" if color_space == 0 else "sRGB", "STRAIGHT")
    node.extension = sampler.extension
    node.interpolation = sampler.interpolation
    tree.links.new(uv, node.inputs["Vector"])
    node["endfield_layout_section"] = "Source Texture and UV"
    if record["Floats"].get(f"_Use{name[1:]}AsAlpha", 0) == 0:
        return _SampleResult(_Rgba(node.outputs["Color"], node.outputs["Alpha"]), sampler.limitations)
    red = _component(tree, f"{name} Red as Alpha", node.outputs["Color"], "X")
    white = _vec(tree, f"{name} White RGB", "ADD", (1.0, 1.0, 1.0), (0.0, 0.0, 0.0))
    return _SampleResult(_Rgba(white, red), sampler.limitations)


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


def _make_vfx_feature_source(tree: bpy.types.NodeTree, record: MaterialRecord,
                             textures: dict[tuple[str, int], tuple[Path, str]],
                             snapshot_time: float) -> VfxColor:
    """Build the shader's pre-exposure RGBA from verified texture-role controls."""
    if type(snapshot_time) not in (int, float) or not math.isfinite(snapshot_time):
        raise ValueError(f"VFX snapshot time must be finite: material={record['Name']}, value={snapshot_time}")
    floats, colors = record["Floats"], record["Colors"]
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.uv_map = "UVMap"
    uv1 = None
    if record["Colors"].get("_SampleTex1UVWeights") == [0, 1, 0, 0] and floats.get("_InParticle", 0) == 0:
        uv1 = tree.nodes.new("ShaderNodeUVMap")
        uv1.uv_map = "EF_FX_UV1"
    zero = _vec(tree, "No UV Disturbance", "ADD", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    channels = vfx_custom1_channels(record)
    custom = None
    if channels:
        custom = tree.nodes.new("ShaderNodeAttribute")
        custom.name = custom.label = "Particle Custom1 XYZW"
        custom.attribute_name = "EF_FX_CUSTOM1"
        custom["endfield_layout_section"] = "Particle Vertex Streams"
    custom_x: bpy.types.NodeSocket | float = 0.0
    custom_y: bpy.types.NodeSocket | float = 0.0
    if custom is not None and "x" in channels:
        custom_x = _component(tree, "Custom1 X for Main UV", custom.outputs["Color"], "X")
    if custom is not None and "y" in channels:
        custom_y = _component(tree, "Custom1 Y for Sample UV", custom.outputs["Color"], "Y")
    disturb_weight: bpy.types.NodeSocket | float = 0.0 if floats.get("_UseParticleDisturb", 0) and floats.get("_InParticle", 0) == 0 else 1.0
    if "w" in channels:
        if custom is None:
            raise ValueError(f"VFX Custom1 node missing: material={record['Name']}, channel=w")
        disturb_weight = _math(tree, "Particle Disturb Weight", "MULTIPLY",
                                custom.outputs["Alpha"], floats["_InParticle"])
    disturbance = zero
    role_samples: dict[int, list[_Rgba]] = {0: [], 1: [], 2: [], 3: [], 4: [], 5: []}
    sampling_limitations: list[str] = []
    source_weight: bpy.types.NodeSocket | None = None
    for index in range(5):
        if floats.get(f"_UseSampleTex{index}", 0) == 0:
            continue
        name = f"_SampleTex{index}"
        role = int(floats[f"{name}Use"])
        source_uv = uv1.outputs[0] if index == 1 and uv1 is not None else uv.outputs[0]
        sampled_uv = _uv(tree, record, name, source_uv, snapshot_time,
                         disturbance if floats.get(f"{name}UseDisturb", 0) else zero,
                         custom_y)
        sample_result = _sample(tree, record, name, textures, sampled_uv)
        sampled = sample_result.rgba
        sampling_limitations.extend(sample_result.limitations)
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
        elif role == 2:
            source_weight = _component(tree, "Source Weight Red", sampled.rgb, "X")
            amount = floats["_DisturbUseWeight"] * floats["_WeightTexIntensity"] * floats[f"{name}UseWeight2"]
            factor = _math(tree, "Weighted Disturb Factor", "ADD", 1.0 - amount,
                           _math(tree, "Weighted Disturb Red", "MULTIPLY", source_weight, amount))
            disturbance = _scale(tree, "Weighted Disturbance", disturbance, factor)
    main_uv = _uv(tree, record, "_MainTex", uv.outputs[0], snapshot_time,
                  disturbance if floats.get("_MainTexUseDisturb", 0) else zero,
                  custom_x)
    main_result = _sample(tree, record, "_MainTex", textures, main_uv)
    main = main_result.rgba
    sampling_limitations.extend(main_result.limitations)
    tint = colors["_TintColor"]
    rgb = _scale(tree, "Source Tint Intensity",
                 _vec(tree, "Source Tint RGB", "MULTIPLY", main.rgb, tuple(tint[:3])),
                 floats["_TintColorIntensity"])
    alpha = _math(tree, "Source Tint Alpha", "MULTIPLY", main.alpha,
                  tint[3] * floats["_TintColorAlpha"])
    if floats.get("_DisableVertColor", 0) == 0:
        vertex = tree.nodes.new("ShaderNodeVertexColor")
        vertex.name = vertex.label = "Source Vertex Color"
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
    if role_samples[4]:
        blend = _Rgba(_vec(tree, "Blend Base RGB", "ADD", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
                      _value(tree, "Blend Base Alpha", 0.0))
        for index, sample in enumerate(role_samples[4]):
            blend = _add_rgba(tree, f"Blend Layer {index}", blend, sample)
        blend_tint = colors["_BlendTint"]
        blend_factor = _math(tree, "Blend Alpha Sum", "ADD", alpha, blend.alpha)
        blend_factor = _math(tree, "Blend Tint Alpha", "MULTIPLY", blend_factor, blend_tint[3])
        blend_factor = _math(tree, "Blend Clamp Low", "MAXIMUM", blend_factor, 0.0)
        blend_factor = _math(tree, "Blend Clamp High", "MINIMUM", blend_factor, 1.0)
        blend_rgb = _vec(tree, "Blend Tint RGB", "MULTIPLY", blend.rgb, tuple(blend_tint[:3]))
        rgb = _vec(tree, "Add Blend RGB", "ADD", rgb,
                   _scale(tree, "Blend RGB Contribution", blend_rgb, blend_factor))
    limitations = sampling_limitations
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
        if floats.get("_DissolveUseWeight", 0):
            if source_weight is None:
                raise ValueError(f"VFX weighted dissolve has no source weight sample: material={record['Name']}")
            amount = floats["_WeightTexIntensity"] * floats["_DissolveUseWeight"]
            factor = _math(tree, "Weighted Dissolve Factor", "ADD", 1.0 - amount,
                           _math(tree, "Weighted Dissolve Red", "MULTIPLY", source_weight, amount))
            schedule = _math(tree, "Weighted Dissolve Schedule", "ADD", -1.0,
                             _math(tree, "Weighted Dissolve Interpolation", "MULTIPLY",
                                   _math(tree, "Dissolve Schedule Plus One", "ADD", schedule, 1.0), factor))
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
    if source_weight is not None:
        limitations.append("Weighted VFXBaseV2 controls derived from compiled shader formulas; game exposure and depth remain uncaptured")
    return VfxColor(rgb, alpha, tuple(limitations))


def make_vfx_feature_source(tree: bpy.types.NodeTree, record: MaterialRecord,
                            textures: dict[tuple[str, int], tuple[Path, str]],
                            snapshot_time: float, vertex_streams: tuple[int, ...]) -> VfxColor:
    issues = vfx_feature_issues(record, vertex_streams)
    if issues:
        raise NotImplementedError(f"VFX source controls unsupported: material={record['Name']}, issues={issues}")
    source = _make_vfx_feature_source(tree, record, textures, snapshot_time)
    if (record["Floats"].get("_DisableVertColor", 0) == 0 and
            record["Floats"].get("_DisableParticleInstanceColor", 0) == 0):
        return VfxColor(source.rgb, source.alpha,
                        (*source.limitations, "Runtime renderer vertex-color attenuation is not captured"))
    return source


def make_vfx_mesh_source(tree: bpy.types.NodeTree, record: MaterialRecord,
                         textures: dict[tuple[str, int], tuple[Path, str]],
                         snapshot_time: float, vertex_color_components: int) -> VfxColor:
    issues = vfx_mesh_feature_issues(record, vertex_color_components)
    if issues:
        raise NotImplementedError(f"VFX mesh source controls unsupported: material={record['Name']}, issues={issues}")
    source = _make_vfx_feature_source(tree, record, textures, snapshot_time)
    if record["Floats"].get("_DisableVertColor", 0) == 0:
        return VfxColor(source.rgb, source.alpha,
                        (*source.limitations, "Runtime renderer vertex-color attenuation is not captured"))
    return source
