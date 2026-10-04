"""Research preview of the source-backed VFXBaseV2 vertex-offset subset."""

from __future__ import annotations

from array import array
from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
from typing import Literal

import bpy
from mathutils import Matrix, Vector

from endfield_scene_material_contract import reference_key, validate_texture_sampler


class UnsupportedVertexOffset(ValueError):
    """The requested source controls exceed the verified preview subset."""


_VERIFIED_1472_KEYWORDS = frozenset({
    "_SAMPLE_TEX0", "_SAMPLE_TEX1", "_SAMPLE_TEX2", "_SAMPLE_TEX3",
    "_SAMPLE_TEX4", "_USE_SOFTBLEND", "_USE_VERTOFFSET",
})


@dataclass(frozen=True)
class OffsetSamplerContract:
    """Explicit source texture interpretation for LOD0 vertex sampling."""

    filter_mode: Literal["bilinear_lod0"]
    wrap_u: Literal["repeat", "clamp"]
    wrap_v: Literal["repeat", "clamp"]
    source_color_space: int
    sampler_evidence: Literal["source_verified", "research_assumption"]
    evidence_reference: str


@dataclass(frozen=True)
class _Controls:
    offset_dir: tuple[float, float, float, float]
    offset_speed: tuple[float, float, float, float]
    offset_st: tuple[float, float, float, float]
    in_particle: float
    intensity: float
    bidirectional: float
    camera_offset: float


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ValueError(f"Vertex-offset source must be an object: field={context}")
    return value


def _number(value: object, context: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"Vertex-offset source number invalid: field={context}, value={value!r}")
    return float(value)


def _vector4(value: object, context: str) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"Vertex-offset source vector invalid: field={context}, value={value!r}")
    return tuple(_number(item, f"{context}[{index}]") for index, item in enumerate(value))  # type: ignore[return-value]


def _controls(material: Mapping[str, object], texture_source: str,
              texture_path_id: int) -> _Controls:
    floats = _mapping(material.get("Floats"), "Material.Floats")
    colors = _mapping(material.get("Colors"), "Material.Colors")
    textures = material.get("Textures")
    if not isinstance(textures, list):
        raise ValueError("Vertex-offset source textures missing: field=Material.Textures")
    slots = [_mapping(item, "Material.Textures[]") for item in textures]
    offset_slots = [slot for slot in slots if slot.get("Property") == "_OffsetTex"]
    if len(offset_slots) != 1:
        raise ValueError(f"Vertex-offset source needs one _OffsetTex slot: count={len(offset_slots)}")
    slot = offset_slots[0]
    target = _mapping(slot.get("Texture"), "Material.Textures._OffsetTex.Texture")
    if (target.get("Source") != texture_source or
            target.get("PathId") != texture_path_id or
            target.get("Type") != "Texture2D"):
        raise ValueError(f"Vertex-offset texture identity mismatch: source={texture_source}, pathId={texture_path_id}, slot={target}")
    scale = slot.get("Scale")
    offset = slot.get("Offset")
    if not isinstance(scale, list) or len(scale) != 2 or not isinstance(offset, list) or len(offset) != 2:
        raise ValueError("Vertex-offset _OffsetTex scale/offset must each contain two numbers")
    return _Controls(
        _vector4(colors.get("_OffsetDir"), "Material.Colors._OffsetDir"),
        _vector4(colors.get("_OffsetSpeed"), "Material.Colors._OffsetSpeed"),
        (_number(scale[0], "_OffsetTex.Scale[0]"),
         _number(scale[1], "_OffsetTex.Scale[1]"),
         _number(offset[0], "_OffsetTex.Offset[0]"),
         _number(offset[1], "_OffsetTex.Offset[1]")),
        _number(floats.get("_InParticle"), "Material.Floats._InParticle"),
        _number(floats.get("_OffsetIntensity"), "Material.Floats._OffsetIntensity"),
        _number(floats.get("_Bi_Offset"), "Material.Floats._Bi_Offset"),
        _number(floats.get("_VertCameraOffset"), "Material.Floats._VertCameraOffset"),
    )


def validate_vertex_offset_source(
        material: Mapping[str, object]) -> tuple[Mapping[str, object], OffsetSamplerContract]:
    """Validate the bounded 1472 VFXBaseV2 vertex-offset source contract."""
    shader = _mapping(material.get("Shader"), "Material.Shader")
    if shader.get("Name") != "HGRP/Effect/VFXBaseV2":
        raise NotImplementedError(f"Vertex-offset shader family is unsupported: shader={shader.get('Name')!r}")

    state = _mapping(material.get("RenderState"), "Material.RenderState")
    keywords = state.get("ValidKeywords")
    if not isinstance(keywords, list) or any(type(item) is not str for item in keywords):
        raise ValueError("Vertex-offset source keywords are missing or malformed: field=Material.RenderState.ValidKeywords")
    if len(keywords) != len(set(keywords)):
        raise ValueError(f"Vertex-offset source keywords contain duplicates: keywords={keywords}")
    if frozenset(keywords) != _VERIFIED_1472_KEYWORDS:
        raise NotImplementedError(f"Vertex-offset compiled variant is unsupported: keywords={keywords}")

    textures = material.get("Textures")
    if not isinstance(textures, list):
        raise ValueError("Vertex-offset source textures missing: field=Material.Textures")
    slots = [_mapping(item, "Material.Textures[]") for item in textures]
    offset_slots = [slot for slot in slots if slot.get("Property") == "_OffsetTex"]
    if len(offset_slots) != 1:
        raise ValueError(f"Vertex-offset source needs one _OffsetTex slot: count={len(offset_slots)}")
    texture_value = offset_slots[0].get("Texture")
    if texture_value is None:
        raise FileNotFoundError("Vertex-offset source has no _OffsetTex asset reference")
    reference = _mapping(texture_value, "Material.Textures._OffsetTex.Texture")
    texture_source = reference.get("Source")
    texture_path_id = reference.get("PathId")
    if (type(texture_source) is not str or not texture_source or
            type(texture_path_id) is not int or reference.get("Type") != "Texture2D"):
        raise ValueError(f"Vertex-offset source texture identity is invalid: reference={reference}")

    controls = _controls(material, texture_source, texture_path_id)
    floats = _mapping(material.get("Floats"), "Material.Floats")
    if controls.in_particle not in (0.0, 1.0):
        raise NotImplementedError(
            f"Vertex-offset source control is unsupported: field=_InParticle, value={controls.in_particle}")
    for field, expected in (("_UseVertexOffset", 1), ("_OffsetSwitchDir", 0), ("_OffsetUVSet", 0),
                            ("_UseVertexOffsetMask", 0)):
        value = _number(floats.get(field), f"Material.Floats.{field}")
        if value != expected:
            raise NotImplementedError(f"Vertex-offset source control is unsupported: field={field}, value={value}")
    if controls.bidirectional not in (0.0, 1.0) or controls.camera_offset < 0.0:
        raise NotImplementedError(
            "Vertex-offset bidirectional or camera-offset control is unsupported: "
            f"bidirectional={controls.bidirectional}, camera_offset={controls.camera_offset}")

    color_space = reference.get("ColorSpace")
    if type(color_space) is not int:
        raise ValueError(f"Vertex-offset texture ColorSpace is missing or malformed: reference={reference}")
    if color_space != 1:
        raise NotImplementedError(f"Vertex-offset texture ColorSpace is unsupported: value={color_space}")
    sampler_value = reference.get("TextureSampler")
    if sampler_value is None:
        raise ValueError(f"Vertex-offset texture sampler metadata is missing: reference={reference}")
    sampler = validate_texture_sampler(sampler_value, "Material.Textures._OffsetTex.TextureSampler")
    wrap = (sampler["WrapU"], sampler["WrapV"])
    if (sampler["FilterMode"] != 1 or wrap not in ((0, 0), (1, 1)) or
            sampler["WrapW"] != 0 or sampler["Aniso"] != 1 or sampler["MipBias"] != 0.0):
        raise NotImplementedError(f"Vertex-offset sampler is outside the verified bilinear LOD0 subset: sampler={sampler}")
    contract = OffsetSamplerContract(
        "bilinear_lod0",
        "repeat" if wrap[0] == 0 else "clamp",
        "repeat" if wrap[1] == 0 else "clamp",
        color_space,
        "source_verified",
        f"{texture_source}:{texture_path_id}:TextureSampler+ColorSpace",
    )
    return reference, contract


def _validate_indexed_mesh(mesh: bpy.types.Mesh, stream_name: str) -> None:
    if not mesh.loops or len(mesh.vertices) == 0:
        raise UnsupportedVertexOffset(f"Vertex-offset mesh must contain indexed faces: mesh={mesh.name}, stream={stream_name}")


def _uv_by_vertex(mesh: bpy.types.Mesh, layer_name: str) -> tuple[tuple[float, float], ...]:
    layer = mesh.uv_layers.get(layer_name)
    if layer is None:
        raise UnsupportedVertexOffset(f"Vertex-offset UV0 stream missing: mesh={mesh.name}, uv={layer_name}")
    _validate_indexed_mesh(mesh, "UV0")
    uvs = array("f", [0.0]) * (len(mesh.loops) * 2)
    layer.data.foreach_get("uv", uvs)
    values: list[tuple[float, float] | None] = [None] * len(mesh.vertices)
    for index, loop in enumerate(mesh.loops):
        vertex_index = loop.vertex_index
        candidate = (float(uvs[index * 2]), float(uvs[index * 2 + 1]))
        if not all(math.isfinite(item) for item in candidate):
            raise ValueError(f"Vertex-offset UV0 is non-finite: mesh={mesh.name}, vertex={vertex_index}")
        previous = values[vertex_index]
        if previous is not None and any(abs(previous[axis] - candidate[axis]) > 1e-6 for axis in range(2)):
            raise UnsupportedVertexOffset(f"Vertex-offset vertex has conflicting loop UV0: mesh={mesh.name}, vertex={vertex_index}")
        values[vertex_index] = candidate
    if any(value is None for value in values):
        raise UnsupportedVertexOffset(f"Vertex-offset mesh has loose vertices without UV0: mesh={mesh.name}")
    return tuple(value for value in values if value is not None)


def _custom1_y_by_vertex(mesh: bpy.types.Mesh, custom_name: str) -> tuple[float, ...]:
    custom = mesh.color_attributes.get(custom_name)
    if custom is None or custom.data_type != "FLOAT_COLOR" or custom.domain not in {"CORNER", "POINT"}:
        raise UnsupportedVertexOffset(f"Vertex-offset Custom1 stream missing or malformed: mesh={mesh.name}, custom={custom_name}")
    _validate_indexed_mesh(mesh, "Custom1")
    count = len(mesh.loops) if custom.domain == "CORNER" else len(mesh.vertices)
    rgba = array("f", [0.0]) * (count * 4)
    custom.data.foreach_get("color", rgba)
    values: list[float | None] = [None] * len(mesh.vertices)
    for index, loop in enumerate(mesh.loops):
        vertex_index = loop.vertex_index
        color_index = index if custom.domain == "CORNER" else vertex_index
        candidate = float(rgba[color_index * 4 + 1])
        if not math.isfinite(candidate):
            raise ValueError(f"Vertex-offset Custom1.Y is non-finite: mesh={mesh.name}, vertex={vertex_index}")
        previous = values[vertex_index]
        if previous is not None and abs(previous - candidate) > 1e-6:
            raise UnsupportedVertexOffset(f"Vertex-offset vertex has conflicting loop Custom1.Y: mesh={mesh.name}, vertex={vertex_index}")
        values[vertex_index] = candidate
    if any(value is None for value in values):
        raise UnsupportedVertexOffset(f"Vertex-offset mesh has loose vertices without Custom1.Y: mesh={mesh.name}")
    return tuple(value for value in values if value is not None)


def _offset_inputs_by_vertex(
        mesh: bpy.types.Mesh, layer_name: str,
        in_particle: float) -> tuple[tuple[tuple[float, float], float], ...]:
    uvs = _uv_by_vertex(mesh, layer_name)
    if in_particle == 0.0:
        custom_y_values = (0.0,) * len(uvs)
    elif in_particle == 1.0:
        custom_y_values = _custom1_y_by_vertex(mesh, "EF_FX_CUSTOM1")
    else:
        raise UnsupportedVertexOffset(f"Vertex-offset _InParticle must be zero or one: value={in_particle}")
    return tuple((uv, custom_y) for uv, custom_y in zip(uvs, custom_y_values, strict=True))


def _srgb_to_linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _coordinate(index: int, extent: int, wrap: Literal["repeat", "clamp"]) -> int:
    return index % extent if wrap == "repeat" else min(max(index, 0), extent - 1)


def _sample_red(pixels: array, width: int, height: int, u: float, v: float,
                sampler: OffsetSamplerContract) -> float:
    x = u * width - 0.5
    y = v * height - 0.5
    left = math.floor(x)
    bottom = math.floor(y)
    tx = x - left
    ty = y - bottom

    def texel(dx: int, dy: int) -> float:
        col = _coordinate(left + dx, width, sampler.wrap_u)
        row = _coordinate(bottom + dy, height, sampler.wrap_v)
        return _srgb_to_linear(float(pixels[(row * width + col) * 4]))

    lower = texel(0, 0) * (1.0 - tx) + texel(1, 0) * tx
    upper = texel(0, 1) * (1.0 - tx) + texel(1, 1) * tx
    return lower * (1.0 - ty) + upper * ty


def _vertex_offset_preview_context_issues(
        material: Mapping[str, object], controls: _Controls,
        image: bpy.types.Image, sampler: OffsetSamplerContract,
        texture_source: str, texture_path_id: int, object_to_world: Matrix,
        camera_world: Vector, time_seconds: float, pass_gate: float) -> tuple[str, ...]:
    floats = _mapping(material.get("Floats"), "Material.Floats")
    shader = _mapping(material.get("Shader"), "Material.Shader")
    state = _mapping(material.get("RenderState"), "Material.RenderState")
    keywords = state.get("ValidKeywords")
    issues: list[str] = []
    if shader.get("Name") != "HGRP/Effect/VFXBaseV2":
        issues.append("shader_not_vfxbasev2")
    if not isinstance(keywords, list) or any(type(item) is not str for item in keywords):
        issues.append("material_keywords_invalid")
    elif frozenset(keywords) != _VERIFIED_1472_KEYWORDS:
        issues.append("compiled_vertex_variant_not_verified")
    if controls.in_particle not in (0.0, 1.0):
        issues.append(f"unsupported__InParticle={controls.in_particle!r}")
    for field, expected in (("_UseVertexOffset", 1), ("_OffsetSwitchDir", 0), ("_OffsetUVSet", 0),
                            ("_UseVertexOffsetMask", 0)):
        if floats.get(field) != expected:
            issues.append(f"unsupported_{field}={floats.get(field)!r}")
    if controls.bidirectional not in (0.0, 1.0) or controls.camera_offset < 0.0:
        issues.append("unsupported_bidirectional_or_camera_offset")
    if (sampler.filter_mode != "bilinear_lod0" or sampler.wrap_u not in ("repeat", "clamp") or
            sampler.wrap_v not in ("repeat", "clamp") or sampler.source_color_space != 1 or
            sampler.sampler_evidence not in ("source_verified", "research_assumption") or
            not sampler.evidence_reference):
        issues.append("sampler_contract_invalid_or_source_colorspace_not_srgb")
    if type(texture_source) is not str or not texture_source or type(texture_path_id) is not int:
        issues.append("texture_identity_invalid")
    if image.size[0] >= 1 and image.size[1] >= 1:
        image.pixels[0]
    if not image.has_data or image.channels < 3 or image.size[0] < 1 or image.size[1] < 1:
        issues.append("texture_pixels_unavailable")
    if len(object_to_world) != 4 or any(len(row) != 4 for row in object_to_world) or not all(
            math.isfinite(float(object_to_world[row][column])) for row in range(4) for column in range(4)):
        issues.append("object_matrix_invalid")
    else:
        try:
            object_to_world.to_3x3().inverted()
        except ValueError:
            issues.append("object_matrix_noninvertible")
    if len(camera_world) != 3 or not all(math.isfinite(float(value)) for value in camera_world):
        issues.append("reference_camera_invalid")
    if type(time_seconds) not in (int, float) or not math.isfinite(float(time_seconds)):
        issues.append("source_time_invalid")
    if type(pass_gate) not in (int, float) or pass_gate not in (0, 1):
        issues.append("pass_gate_must_be_explicit_zero_or_one")
    return tuple(issues)


def vertex_offset_preview_issues(material: Mapping[str, object], mesh: bpy.types.Mesh,
                                 image: bpy.types.Image, sampler: OffsetSamplerContract,
                                 texture_source: str, texture_path_id: int,
                                 object_to_world: Matrix, camera_world: Vector,
                                 time_seconds: float, pass_gate: float) -> tuple[str, ...]:
    """Check the bounded source formula without claiming native sampler parity."""
    controls = _controls(material, texture_source, texture_path_id)
    issues = list(_vertex_offset_preview_context_issues(
        material, controls, image, sampler, texture_source, texture_path_id,
        object_to_world, camera_world, time_seconds, pass_gate))
    if not issues:
        try:
            _offset_inputs_by_vertex(mesh, "UVMap", controls.in_particle)
        except UnsupportedVertexOffset as error:
            issues.append(str(error))
    return tuple(issues)


def apply_vertex_offset_research_preview(material: Mapping[str, object], source_mesh: bpy.types.Mesh,
                                         image: bpy.types.Image, sampler: OffsetSamplerContract,
                                         texture_source: str, texture_path_id: int,
                                         object_to_world: Matrix, camera_world: Vector,
                                         time_seconds: float, pass_gate: float) -> bpy.types.Mesh:
    """Copy and displace every indexed vertex, preserving all source mesh layers."""
    issues = vertex_offset_preview_issues(material, source_mesh, image, sampler, texture_source,
                                          texture_path_id, object_to_world, camera_world,
                                          time_seconds, pass_gate)
    if issues:
        raise UnsupportedVertexOffset(f"Vertex-offset research preview unsupported: issues={issues}")
    controls = _controls(material, texture_source, texture_path_id)
    vertex_streams = _offset_inputs_by_vertex(source_mesh, "UVMap", controls.in_particle)
    width, height = int(image.size[0]), int(image.size[1])
    pixels = array("f", [0.0]) * (width * height * 4)
    image.pixels.foreach_get(pixels)
    direction_local = Vector((controls.offset_dir[0], -controls.offset_dir[2], controls.offset_dir[1]))
    direction_world = object_to_world.to_3x3() @ direction_local
    world_to_local = object_to_world.to_3x3().inverted()
    copy = source_mesh.copy()
    try:
        for index, vertex in enumerate(copy.vertices):
            (u, v), custom_y = vertex_streams[index]
            sample_u = ((u * controls.offset_st[0] + controls.offset_st[2]) +
                        controls.offset_speed[0] * time_seconds + controls.offset_speed[2] * custom_y)
            sample_v = ((v * controls.offset_st[1] + controls.offset_st[3]) +
                        controls.offset_speed[1] * time_seconds + controls.offset_speed[3] * custom_y)
            red = _sample_red(pixels, width, height, sample_u, sample_v, sampler)
            scalar = ((controls.intensity + controls.offset_dir[3] * custom_y) *
                      (red * (1.0 + controls.bidirectional) - controls.bidirectional))
            base_world = object_to_world @ vertex.co
            to_camera = camera_world - base_world
            distance = to_camera.length
            if distance <= 1e-12:
                raise UnsupportedVertexOffset(f"Vertex-offset camera coincides with vertex: mesh={source_mesh.name}, vertex={index}")
            camera_push = (to_camera / distance) * (min(max(distance - 1.0, 0.0), controls.camera_offset) + 0.001)
            world_delta = (direction_world * scalar + camera_push) * (1.0 - pass_gate)
            vertex.co += world_to_local @ world_delta
        copy.update()
        copy["endfield_fx_vertex_offset_scope"] = "source_formula_preview_partial"
        copy["endfield_fx_sampler_evidence"] = sampler.sampler_evidence
        copy["endfield_fx_sampler_reference"] = sampler.evidence_reference
        copy["endfield_fx_pass_gate"] = float(pass_gate)
        return copy
    except Exception:
        bpy.data.meshes.remove(copy)
        raise


def apply_material_vertex_offset(material: bpy.types.Material, source_mesh: bpy.types.Mesh,
                                 world: Matrix, camera_world: Vector,
                                 age: float) -> bpy.types.Mesh:
    """Apply a material's packed source offset texture to a copied particle mesh."""
    source_json = material.get("endfield_fx_source_material_json")
    if source_json is None:
        if (material.get("endfield_fx_vertex_offset_contract") is not None or
                material.get("endfield_fx_vertex_offset_geometry_preview") is not None or
                material.get("endfield_source_vertex_offset_control") == 1 or
                material.get("endfield_vertex_offset_stage") is not None):
            raise ValueError(f"FX vertex-offset source record is missing: material={material.name}")
        return source_mesh
    if type(source_json) is not str:
        raise ValueError(f"FX material source record is malformed: material={material.name}")
    source = _mapping(json.loads(source_json), "Material.source JSON")
    floats = _mapping(source.get("Floats"), "Material.Floats")
    if "_UseVertexOffset" not in floats:
        state_value = source.get("RenderState")
        state = _mapping(state_value, "Material.RenderState") if state_value is not None else {}
        keywords = state.get("ValidKeywords")
        if isinstance(keywords, list) and "_USE_VERTOFFSET" in keywords:
            raise ValueError(f"FX source record omits enabled vertex-offset control: material={material.name}")
        return source_mesh
    enabled = _number(floats.get("_UseVertexOffset"), "Material.Floats._UseVertexOffset")
    if enabled == 0:
        return source_mesh
    if enabled != 1:
        raise NotImplementedError(
            f"FX material vertex-offset control is unsupported: material={material.name}, value={enabled}")

    reference, sampler = validate_vertex_offset_source(source)
    texture_source = reference["Source"]
    texture_path_id = reference["PathId"]
    texture_identity = json.dumps({
        "PathId": texture_path_id,
        "Source": texture_source,
        "Type": "Texture2D",
    }, sort_keys=True, separators=(",", ":"))
    texture_key = reference_key(_mapping(reference, "Material.Textures._OffsetTex.Texture"))
    image_identity = json.dumps({
        "PathId": texture_key[1],
        "Source": texture_key[0],
        "Type": "Texture2D",
    }, sort_keys=True, separators=(",", ":"))
    if material.get("endfield_fx_vertex_offset_contract") != "SourceFormulaPreview/1":
        raise ValueError(f"FX vertex-offset preview metadata is missing: material={material.name}")
    if material.get("endfield_fx_vertex_offset_texture_identity_json") != texture_identity:
        raise ValueError(f"FX vertex-offset texture identity changed: material={material.name}")
    sampler_json = material.get("endfield_fx_vertex_offset_sampler_json")
    if type(sampler_json) is not str or json.loads(sampler_json) != {
            "filter_mode": sampler.filter_mode,
            "wrap_u": sampler.wrap_u,
            "wrap_v": sampler.wrap_v,
            "source_color_space": sampler.source_color_space,
            "sampler_evidence": sampler.sampler_evidence,
            "evidence_reference": sampler.evidence_reference,
    }:
        raise ValueError(f"FX vertex-offset sampler metadata changed: material={material.name}")
    pass_gate = material.get("endfield_fx_vertex_offset_pass_gate")
    if type(pass_gate) not in (int, float) or pass_gate != 0:
        raise ValueError(f"FX vertex-offset reference pass gate must be zero: material={material.name}, value={pass_gate!r}")
    if material.get("endfield_fx_vertex_offset_geometry_preview") is not True:
        raise ValueError(f"FX vertex-offset geometry-preview marker is missing: material={material.name}")
    limitations_value = material.get("endfield_fx_limitations")
    if type(limitations_value) is not str:
        raise ValueError(f"FX material limitations are missing: material={material.name}")
    limitations = json.loads(limitations_value)
    if (not isinstance(limitations, list) or
            "normal_pass_gate_reference_0" not in limitations or
            "vertex_offset_geometry_preview" not in limitations):
        raise ValueError(f"FX vertex-offset preview limitations are incomplete: material={material.name}")

    if not material.use_nodes or material.node_tree is None:
        raise ValueError(f"FX vertex-offset material has no node tree: material={material.name}")
    matching_nodes = [node for node in material.node_tree.nodes
                      if node.type == "TEX_IMAGE" and
                      node.get("endfield_fx_vertex_offset_node_contract") == "SourceOffsetTexture/1"]
    if len(matching_nodes) != 1:
        raise ValueError(
            f"FX vertex-offset image node must be unique: material={material.name}, count={len(matching_nodes)}")
    node = matching_nodes[0]
    if node.get("endfield_fx_vertex_offset_texture_identity_json") != texture_identity:
        raise ValueError(f"FX vertex-offset image node identity changed: material={material.name}")
    image = node.image
    if image is None or image.get("endfield_fx_vertex_offset_texture_identity_json") != image_identity:
        raise ValueError(f"FX vertex-offset image datablock is missing or mismatched: material={material.name}")
    if image.packed_file is None:
        raise FileNotFoundError(f"FX vertex-offset texture is not packed for portable delivery: material={material.name}, image={image.name}")
    image.pixels[0]
    if not image.has_data or image.channels < 3 or image.size[0] < 1 or image.size[1] < 1:
        raise ValueError(f"FX vertex-offset texture pixels are unavailable: material={material.name}, image={image.name}")
    if image.colorspace_settings.name != "sRGB":
        raise ValueError(
            f"FX vertex-offset image color space changed: material={material.name}, image={image.name}, "
            f"value={image.colorspace_settings.name!r}, expected='sRGB'")

    if not source_mesh.vertices:
        controls = _controls(source, texture_source, texture_path_id)
        issues = _vertex_offset_preview_context_issues(
            source, controls, image, sampler, texture_source, texture_path_id,
            world, camera_world, age, 0.0)
        if issues:
            raise UnsupportedVertexOffset(f"Vertex-offset preview unsupported for empty mesh: issues={issues}")
        return source_mesh
    return apply_vertex_offset_research_preview(
        source, source_mesh, image, sampler, texture_source, texture_path_id,
        world, camera_world, age, 0.0)
