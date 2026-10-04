"""Portable particle materials for the verified VFXBaseV2 texture-role variants.

Source blend/channel semantics are retained. Missing runtime screen buffers and
pipeline controls are exposed as explicit preview conditions, never as fidelity.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, cast

import bpy

from blender_scene_materials import layout_shader_node_tree
from blender_scene_effect_layers import (VfxColor, make_vfx_feature_source, make_vfx_mesh_source,
                                         vfx_feature_issues, vfx_mesh_feature_issues, vfx_custom1_channels)
from blender_scene_effect_vertex_offset import OffsetSamplerContract, validate_vertex_offset_source
from endfield_scene_material_contract import (AssetReference, MaterialRecord, reference_key, resolve_export_path,
                                              has_shader_white_texture_default)


def _link(links: bpy.types.NodeLinks, target: bpy.types.NodeSocket,
          value: bpy.types.NodeSocket | float | tuple[float, ...]) -> None:
    if isinstance(value, bpy.types.NodeSocket):
        links.new(value, target)
    else:
        target.default_value = value


def _math(tree: bpy.types.NodeTree, label: str, operation: str,
          left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeMath")
    node.operation, node.label, node.name = operation, label, label
    _link(tree.links, node.inputs[0], left)
    _link(tree.links, node.inputs[1], right)
    return node.outputs[0]


def _vector(tree: bpy.types.NodeTree, label: str, operation: str,
            left: bpy.types.NodeSocket | tuple[float, ...],
            right: bpy.types.NodeSocket | tuple[float, ...]) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation, node.label, node.name = operation, label, label
    _link(tree.links, node.inputs[0], left)
    _link(tree.links, node.inputs[1], right)
    return node.outputs[0]


def _scale(tree: bpy.types.NodeTree, label: str, rgb: bpy.types.NodeSocket,
           factor: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation, node.label, node.name = "SCALE", label, label
    tree.links.new(rgb, node.inputs[0])
    _link(tree.links, node.inputs[3], factor)
    return node.outputs[0]


def _control(tree: bpy.types.NodeTree, label: str, value: float) -> bpy.types.NodeSocket:
    node = tree.nodes.new("ShaderNodeValue")
    node.name = node.label = label
    node.outputs[0].default_value = value
    node["endfield_layout_section"] = "Explicit Preview Conditions"
    node["endfield_layout_note"] = "Editable reference state"
    return node.outputs[0]


def _composite_group() -> bpy.types.NodeTree:
    name = "Endfield FX Source Over or Additive v1"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_fx_contract") != "premultiplied_source_destination/1":
            raise ValueError(f"Conflicting FX node group: {name}")
        return existing
    group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    group["endfield_fx_contract"] = "premultiplied_source_destination/1"
    group.interface.new_socket(name="Source RGB", in_out="INPUT", socket_type="NodeSocketColor")
    group.interface.new_socket(name="Coverage", in_out="INPUT", socket_type="NodeSocketFloat")
    group.interface.new_socket(name="Output Alpha", in_out="INPUT", socket_type="NodeSocketFloat")
    group.interface.new_socket(name="Surface", in_out="OUTPUT", socket_type="NodeSocketShader")
    entry = group.nodes.new("NodeGroupInput")
    premultiplied = _scale(group, "Premultiply Source Once", entry.outputs["Source RGB"], entry.outputs["Coverage"])
    remaining = _math(group, "Destination Factor", "SUBTRACT", 1.0, entry.outputs["Output Alpha"])
    transparent = group.nodes.new("ShaderNodeBsdfTransparent")
    group.links.new(remaining, transparent.inputs[0])
    emission = group.nodes.new("ShaderNodeEmission")
    group.links.new(premultiplied, emission.inputs["Color"])
    add = group.nodes.new("ShaderNodeAddShader")
    group.links.new(transparent.outputs[0], add.inputs[0])
    group.links.new(emission.outputs[0], add.inputs[1])
    output = group.nodes.new("NodeGroupOutput")
    group.links.new(add.outputs[0], output.inputs[0])
    layout_shader_node_tree(group)
    return group


def _make_effect_material(record: MaterialRecord,
                          textures: dict[tuple[str, int], tuple[Path, str]],
                          snapshot_time: float, source_identity: object,
                          source_factory: Callable[[bpy.types.NodeTree], VfxColor]) -> bpy.types.Material:
    shader = record["Shader"].get("Name")
    if shader != "HGRP/Effect/VFXBaseV2":
        raise NotImplementedError(f"FX shader family is not implemented: shader={shader}, material={record['Name']}")
    floats = record["Floats"]
    if floats["_SurfaceType"] != 1 or floats["_SrcBlend"] != 1 or floats["_DstBlend"] != 10:
        raise NotImplementedError(f"FX blend factors are not verified: material={record['Name']}")
    if floats.get("_AlphaSrcBlend") != 1 or floats.get("_AlphaDstBlend") != 10:
        raise NotImplementedError(f"FX alpha blend factors are not verified: material={record['Name']}")
    if floats["_BlendMode"] not in (0, 1) or floats["_UseMainTexAsAlpha"] not in (0, 1):
        raise NotImplementedError(f"FX blend/channel mode is not verified: material={record['Name']}")
    if floats.get("_CullMode") not in (0, 2) or any(floats.get(name) != value for name, value in (("_ZWrite", 0), ("_ZTest", 4), ("_DisableZTest", 0))):
        raise NotImplementedError(f"FX culling/depth state is not verified: material={record['Name']}")
    slots = [slot for slot in record["Textures"] if slot["Property"] == "_MainTex"]
    if len(slots) != 1:
        raise FileNotFoundError(f"FX requires a unique MainTex binding: material={record['Name']}")
    slot = slots[0]
    ref = slot["Texture"]
    if ref is None:
        if not has_shader_white_texture_default(record, "_MainTex"):
            raise FileNotFoundError(f"FX MainTex has no verified shader default: material={record['Name']}")
        binding_identity = "serialized_shader_texture_property:_MainTex:white:2D"
    else:
        key = reference_key(ref)
        if key not in textures:
            raise FileNotFoundError(f"FX texture was not decoded: material={record['Name']}, reference={key}")
        root, filename = textures[key]
        path = resolve_export_path(root / filename)
        if not path.is_file():
            raise FileNotFoundError(path)
        if ref.get("ColorSpace") not in (0, 1):
            raise ValueError(f"FX texture source color space is missing: material={record['Name']}, reference={key}")
        binding_identity = str(path)
    identity = hashlib.sha256(json.dumps([record, binding_identity, snapshot_time, source_identity], sort_keys=True).encode()).hexdigest()[:24]
    name = "EF_FX_" + identity
    source_record_json = json.dumps(record, sort_keys=True, allow_nan=False)
    existing = bpy.data.materials.get(name)
    if existing is not None:
        previous_source_json = existing.get("endfield_fx_source_material_json")
        if previous_source_json is not None and previous_source_json != source_record_json:
            raise ValueError(f"Cached FX material source record conflicts: material={name}")
        existing["endfield_fx_source_material_json"] = source_record_json
        return existing
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.surface_render_method = "BLENDED"
    material.use_transparency_overlap = True
    material["endfield_fx_source_material"] = record["Name"]
    material["endfield_fx_source"] = record["Source"]
    material["endfield_fx_path_id"] = str(record["PathId"])
    material["endfield_fx_source_material_json"] = source_record_json
    material["endfield_fx_status"] = "static_preview_subset"
    material["endfield_fx_custom1_channels"] = json.dumps(vfx_custom1_channels(record))
    limitations = ["Game exposure and grading not captured"]
    for prop, reason in (("_UseFog", "Game fog not reconstructed"),
                         ("_UseSoftBlend", "Screen-depth soft intersection not reconstructed"),
                         ("_UseNearCameraFade", "Game near-camera fade fixed at one")):
        if floats.get(prop, 0):
            limitations.append(reason)
    tree = material.node_tree
    tree.nodes.clear()
    source = source_factory(tree)
    rgb, alpha = source.rgb, source.alpha
    if any("Weighted VFXBaseV2 controls derived" in item for item in source.limitations):
        material["endfield_fx_status"] = "source_formula_preview_partial"
    material["endfield_fx_limitations"] = json.dumps([*limitations, *source.limitations])
    excess = _vector(tree, "Exposure Threshold", "SUBTRACT", rgb, (floats["_ExpThreshold"],) * 3)
    excess = _vector(tree, "Positive Exposure Excess", "MAXIMUM", excess, (0, 0, 0))
    rgb = _vector(tree, "Exposure Gain", "ADD", rgb, _scale(tree, "Exposure Intensity", excess, floats["_ExpIntensity"]))
    rgb = _scale(tree, "Reference Exposure", rgb, _control(tree, "Exposure - Reference 1", 1.0))
    rgb = _vector(tree, "Clamp Source RGB Low", "MAXIMUM", rgb, (0, 0, 0))
    rgb = _vector(tree, "Clamp Source RGB High", "MINIMUM", rgb, (1000, 1000, 1000))
    alpha = _math(tree, "Reference Depth and Runtime Fade", "MULTIPLY", alpha,
                  _control(tree, "Screen and Runtime Fade - Reference 1", 1.0))
    alpha = _math(tree, "Opacity Lower Bound", "MAXIMUM", alpha, 0.0)
    alpha = _math(tree, "Opacity Upper Bound", "MINIMUM", alpha, 1.0)
    composite = tree.nodes.new("ShaderNodeGroup")
    composite.node_tree = _composite_group()
    composite.label = "Source Blend and Transparent Background"
    tree.links.new(rgb, composite.inputs["Source RGB"])
    tree.links.new(alpha, composite.inputs["Coverage"])
    output_alpha = _math(tree, "Source BlendMode Output Alpha", "MULTIPLY", alpha, 1 - floats["_BlendMode"])
    tree.links.new(output_alpha, composite.inputs["Output Alpha"])
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    surface = composite.outputs[0]
    if floats["_CullMode"] == 2:
        geometry = tree.nodes.new("ShaderNodeNewGeometry")
        transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
        cull = tree.nodes.new("ShaderNodeMixShader")
        cull.name = cull.label = "Source Backface Culling"
        tree.links.new(geometry.outputs["Backfacing"], cull.inputs[0])
        tree.links.new(surface, cull.inputs[1])
        tree.links.new(transparent.outputs[0], cull.inputs[2])
        surface = cull.outputs[0]
    tree.links.new(surface, output.inputs["Surface"])
    layout_shader_node_tree(tree)
    return material


def _vertex_offset_enabled(record: MaterialRecord) -> bool:
    floats = record["Floats"]
    if "_UseVertexOffset" not in floats:
        state = record.get("RenderState")
        keywords = state.get("ValidKeywords") if isinstance(state, dict) else None
        if isinstance(keywords, list) and "_USE_VERTOFFSET" in keywords:
            raise ValueError(f"FX source record omits active vertex-offset control: material={record['Name']}")
        return False
    value = floats["_UseVertexOffset"]
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"FX vertex-offset control is malformed: material={record['Name']}, value={value!r}")
    if value not in (0, 1):
        raise NotImplementedError(f"FX vertex-offset control is unsupported: material={record['Name']}, value={value}")
    return value == 1


def _vertex_offset_texture_path(reference: Mapping[str, object],
                                textures: dict[tuple[str, int], tuple[Path, str]]) -> Path:
    key = reference_key(cast(AssetReference, reference))
    if key not in textures:
        raise FileNotFoundError(f"FX vertex-offset texture was not decoded: reference={key}")
    root, filename = textures[key]
    path = resolve_export_path(root / filename)
    if not path.is_file():
        raise FileNotFoundError(f"FX vertex-offset texture file is missing: reference={key}, path={path}")
    return path


def _load_vertex_offset_image(path: Path, normalized_identity: str) -> bpy.types.Image:
    name = "EF_FX_OFFSET_" + hashlib.sha256(normalized_identity.encode()).hexdigest()[:40]
    image = bpy.data.images.get(name)
    if image is None:
        # Keep Blender's source decode path identical to the bounded research preview.
        image = bpy.data.images.load(str(path), check_existing=False)
        image.name = name
        image["endfield_fx_vertex_offset_texture_identity_json"] = normalized_identity
    elif image.get("endfield_fx_vertex_offset_texture_identity_json") != normalized_identity:
        raise ValueError(f"Conflicting cached FX vertex-offset image: image={name}, path={path}")
    if image.source != "FILE":
        raise ValueError(f"FX vertex-offset source is not a file image: image={name}, source={image.source}")
    image.pixels[0]
    if not image.has_data or image.channels < 3 or image.size[0] < 1 or image.size[1] < 1:
        raise ValueError(f"FX vertex-offset image pixels are unavailable: image={name}, path={path}")
    if image.colorspace_settings.name != "sRGB":
        raise ValueError(
            f"FX vertex-offset source decode changed: image={name}, colorspace={image.colorspace_settings.name!r}, expected='sRGB'")
    if image.packed_file is None:
        image.pack()
    if image.packed_file is None:
        raise FileNotFoundError(f"FX vertex-offset image could not be packed: image={name}, path={path}")
    return image


def _register_vertex_offset_material(material: bpy.types.Material, record: MaterialRecord,
                                     reference: Mapping[str, object], sampler: OffsetSamplerContract,
                                     image: bpy.types.Image) -> None:
    if not material.use_nodes or material.node_tree is None:
        raise ValueError(f"FX vertex-offset material has no node tree: material={material.name}")
    key = reference_key(cast(AssetReference, reference))
    identity = {"PathId": reference["PathId"], "Source": reference["Source"], "Type": "Texture2D"}
    identity_json = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    normalized_identity_json = json.dumps(
        {"PathId": key[1], "Source": key[0], "Type": "Texture2D"},
        sort_keys=True, separators=(",", ":"))
    if image.get("endfield_fx_vertex_offset_texture_identity_json") != normalized_identity_json:
        raise ValueError(f"FX vertex-offset image reference changed: material={material.name}, image={image.name}")
    material["endfield_fx_source_material_json"] = json.dumps(record, sort_keys=True, allow_nan=False)
    material["endfield_fx_vertex_offset_contract"] = "SourceFormulaPreview/1"
    material["endfield_fx_vertex_offset_texture_identity_json"] = identity_json
    material["endfield_fx_vertex_offset_sampler_json"] = json.dumps({
        "filter_mode": sampler.filter_mode,
        "wrap_u": sampler.wrap_u,
        "wrap_v": sampler.wrap_v,
        "source_color_space": sampler.source_color_space,
        "sampler_evidence": sampler.sampler_evidence,
        "evidence_reference": sampler.evidence_reference,
    }, sort_keys=True)
    material["endfield_fx_vertex_offset_pass_gate"] = 0.0
    material["endfield_fx_vertex_offset_geometry_preview"] = True
    material["endfield_fx_status"] = "source_formula_preview_partial"
    limitations_value = material.get("endfield_fx_limitations", "[]")
    if type(limitations_value) is not str:
        raise ValueError(f"FX material limitations are malformed: material={material.name}")
    limitations = json.loads(limitations_value)
    if not isinstance(limitations, list) or any(type(item) is not str for item in limitations):
        raise ValueError(f"FX material limitations are malformed: material={material.name}")
    for limitation in ("normal_pass_gate_reference_0", "vertex_offset_geometry_preview"):
        if limitation not in limitations:
            limitations.append(limitation)
    material["endfield_fx_limitations"] = json.dumps(limitations)

    nodes = [node for node in material.node_tree.nodes
             if node.get("endfield_fx_vertex_offset_node_contract") == "SourceOffsetTexture/1"]
    if len(nodes) > 1:
        raise ValueError(f"FX vertex-offset source image node is duplicated: material={material.name}, count={len(nodes)}")
    if nodes:
        node = nodes[0]
        if node.type != "TEX_IMAGE" or node.image != image:
            raise ValueError(f"FX vertex-offset source image node conflicts: material={material.name}, node={node.name}")
    else:
        node = material.node_tree.nodes.new("ShaderNodeTexImage")
        node.name = "EF_FX_VERTEX_OFFSET_SOURCE_TEXTURE"
        node.image = image
        node["endfield_fx_vertex_offset_node_contract"] = "SourceOffsetTexture/1"
        node["endfield_fx_vertex_offset_texture_identity_json"] = identity_json
        node["endfield_layout_section"] = "Vertex Offset Geometry Preview"
    node.label = "Source _OffsetTex - Geometry Preview Input"
    node.interpolation = "Linear"
    node.extension = "REPEAT" if sampler.wrap_u == "repeat" else "EXTEND"
    if node.get("endfield_fx_vertex_offset_texture_identity_json") != identity_json:
        raise ValueError(f"FX vertex-offset source node identity conflicts: material={material.name}, node={node.name}")
    layout_shader_node_tree(material.node_tree)


def _vertex_offset_inputs(record: MaterialRecord,
                          textures: dict[tuple[str, int], tuple[Path, str]]
                          ) -> tuple[Mapping[str, object], OffsetSamplerContract, bpy.types.Image]:
    """Resolve the verified geometry-stage inputs independently of renderer type."""
    reference, sampler = validate_vertex_offset_source(record)
    path = _vertex_offset_texture_path(reference, textures)
    key = reference_key(cast(AssetReference, reference))
    identity = json.dumps({"PathId": key[1], "Source": key[0], "Type": "Texture2D"},
                          sort_keys=True, separators=(",", ":"))
    return reference, sampler, _load_vertex_offset_image(path, identity)


def _color_stage_record(record: MaterialRecord) -> MaterialRecord:
    """Keep geometry controls in the source record and out of the color graph."""
    return cast(MaterialRecord, {**record, "Floats": {**record["Floats"], "_UseVertexOffset": 0}})


def make_effect_material(record: MaterialRecord,
                         textures: dict[tuple[str, int], tuple[Path, str]],
                         snapshot_time: float, vertex_streams: tuple[int, ...]) -> bpy.types.Material:
    """Build a VFXBaseV2 material from ParticleSystemRenderer stream evidence."""
    offset_enabled = _vertex_offset_enabled(record)
    offset_reference: Mapping[str, object] | None = None
    offset_sampler: OffsetSamplerContract | None = None
    offset_image: bpy.types.Image | None = None
    if offset_enabled:
        offset_reference, offset_sampler = validate_vertex_offset_source(record)
    color_record = _color_stage_record(record) if offset_enabled else record
    unsupported = vfx_feature_issues(color_record, vertex_streams)
    if unsupported:
        raise NotImplementedError(f"FX material features are not implemented: material={record['Name']}, features={unsupported}")
    if offset_enabled:
        if offset_reference is None or offset_sampler is None:
            raise ValueError(f"FX vertex-offset source validation returned incomplete data: material={record['Name']}")
        offset_reference, offset_sampler, offset_image = _vertex_offset_inputs(record, textures)
    source_identity: list[object] = ["particle", vertex_streams]
    if offset_enabled:
        source_identity.append(["vertex_offset_geometry_preview", True])
    material = _make_effect_material(
        record, textures, snapshot_time, source_identity,
        lambda tree: make_vfx_feature_source(tree, color_record, textures, snapshot_time, vertex_streams))
    if offset_enabled:
        if offset_reference is None or offset_sampler is None or offset_image is None:
            raise ValueError(f"FX vertex-offset registration data is incomplete: material={record['Name']}")
        _register_vertex_offset_material(material, record, offset_reference, offset_sampler, offset_image)
    return material


def make_mesh_effect_material(record: MaterialRecord,
                              textures: dict[tuple[str, int], tuple[Path, str]],
                              snapshot_time: float, vertex_color_components: int) -> bpy.types.Material:
    """Build a VFXBaseV2 material from MeshRenderer vertex-color evidence."""
    offset_enabled = _vertex_offset_enabled(record)
    color_record = _color_stage_record(record) if offset_enabled else record
    unsupported = vfx_mesh_feature_issues(color_record, vertex_color_components)
    if unsupported:
        raise NotImplementedError(f"FX mesh material features are not implemented: material={record['Name']}, features={unsupported}")
    inputs = _vertex_offset_inputs(record, textures) if offset_enabled else None
    identity: list[object] = ["mesh_renderer", vertex_color_components]
    if offset_enabled:
        identity.append(["vertex_offset_geometry_preview", True])
    material = _make_effect_material(
        record, textures, snapshot_time, identity,
        lambda tree: make_vfx_mesh_source(tree, color_record, textures, snapshot_time, vertex_color_components))
    if inputs is not None:
        _register_vertex_offset_material(material, record, *inputs)
    return material
