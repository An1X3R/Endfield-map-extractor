"""Build editable, evidence-bound character surface approximations in a research blend.

Source CAB/PathID identities select observed shader families. Engine-specific
lighting, stencil passes, rain, and snow are not claimed to match the game.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Mapping, Sequence, cast

sys.dont_write_bytecode = True
import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from character_windows_paths import extended_path, io_path
from character_node_graph import node, math_node, color_mix, input_socket
from character_npr_nodes import ArtistLighting, make_artist_lighting, apply_diffuse_response
from character_npr_vfx import (
    CHARACTER_VFX_SHADER, MESH_EFFECT_SHADERS, UNSUPPORTED_CHARACTER_VFX_RENDERER_FLAGS,
    validate_character_vfx_shader,
)

Identity = tuple[str, int]
SUPPORTED_SHADERS = frozenset(("HGRP/CharacterNPR", "HGRP/CharacterNPR_Skin", "HGRP/CharacterNPR_Hair", "HGRP/CharacterNPR_Eye", "HGRP/CharacterNPR_OverlayShadow", "HGRP/LitEffect", *MESH_EFFECT_SHADERS))

if TYPE_CHECKING:
    from character_mesh_effect import MeshColorSource


@dataclass(frozen=True)
class TextureSource:
    identity: Identity
    name: str
    path: Path
    sha256: str
    width: int
    height: int
    color_space: int
    source_format: str


@dataclass(frozen=True)
class TextureBinding:
    name: str
    identity: Identity
    scale: tuple[float, float]
    offset: tuple[float, float]
    uv_index: int


@dataclass(frozen=True)
class MaterialSource:
    identity: Identity
    name: str
    shader: str
    floats: Mapping[str, float]
    colors: Mapping[str, tuple[float, float, float, float]]
    textures: tuple[TextureBinding, ...]
    keywords: tuple[str, ...]
    source_json: str
    tree_json: str
    null_texture_defaults: Mapping[str, str]
    shader_property_types: Mapping[str, int]
    alpha_test_keyword: str | None
    uv2_color_keyword: str | None


@dataclass(frozen=True)
class SourcePacket:
    materials: tuple[MaterialSource, ...]
    textures: Mapping[Identity, TextureSource]
    renderers: tuple[dict[str, object], ...]
    selection: tuple[dict[str, object], ...]
    renderer_pipeline: Mapping[Identity, dict[str, object]]
    input_hashes: Mapping[str, str]
    visible_materials: frozenset[Identity]


def object_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"Expected object: {label}")
    return cast(dict[str, object], value)


def array_value(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"Expected array: {label}")
    return cast(list[object], value)


def string_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"Expected nonempty string: {label}")
    return value


def int_value(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"Expected integer: {label}")
    return cast(int, value)


def float_value(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(cast(float, value))):
        raise TypeError(f"Expected finite number: {label}")
    return float(cast(float, value))


def identity(value: object, label: str) -> Identity:
    record = object_value(value, label)
    return string_value(record["source"], label + ".source").casefold(), int_value(record["path_id"], label + ".path_id")


def read_json(path: Path) -> object:
    return json.loads(io_path(path).read_text(encoding="utf-8-sig"))


def file_hash(path: Path) -> str:
    with io_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def numeric_tuple(value: object, length: int, label: str) -> tuple[float, ...]:
    values = array_value(value, label)
    if len(values) != length:
        raise ValueError(f"Expected {length} components: {label}")
    return tuple(float_value(component, label) for component in values)


def visible_material_identities(renderers: tuple[dict[str, object], ...], selection: tuple[dict[str, object], ...]) -> frozenset[Identity]:
    """Require complete source renderer selection before accepting inactive shaders."""
    renderer_map = {identity(row["object"], "renderer.object"): row for row in renderers}
    if len(renderer_map) != len(renderers):
        raise ValueError("Duplicate source renderer identity")
    seen: set[Identity] = set()
    visible: set[Identity] = set()
    for decision in selection:
        cab, path_id = string_value(decision["renderer_identity"], "renderer_identity").rsplit(":", 1)
        key = (cab.casefold(), int(path_id))
        if key not in renderer_map or key in seen:
            raise ValueError(f"Selection renderer is absent or duplicated: {key}")
        seen.add(key)
        if type(decision["visible"]) is not bool:
            raise TypeError(f"Selection visibility must be boolean: {key}")
        if decision["visible"]:
            for pointer in array_value(renderer_map[key]["materials"], "renderer.materials"):
                visible.add(identity(object_value(pointer, "material.pointer")["resolved"], "material.resolved"))
    if seen != set(renderer_map):
        raise ValueError(f"Selection does not cover every source renderer: missing={sorted(set(renderer_map) - seen)}")
    return frozenset(visible)


def optional_extra_material_identities(packet: SourcePacket) -> frozenset[Identity]:
    """Find visible extra-pass materials that are never required as a submesh base."""
    renderer_map = {identity(row["object"], "renderer.object"): row for row in packet.renderers}
    if len(renderer_map) != len(packet.renderers):
        raise ValueError("Duplicate source renderer identity")
    required: set[Identity] = set()
    visible_extras: set[Identity] = set()
    seen: set[Identity] = set()
    for decision in packet.selection:
        renderer_text = string_value(decision["renderer_identity"], "renderer_identity")
        cab, path_id = renderer_text.rsplit(":", 1)
        renderer_key = (cab.casefold(), int(path_id))
        if renderer_key not in renderer_map or renderer_key in seen:
            raise ValueError(f"Selection renderer is absent or duplicated: {renderer_key}")
        seen.add(renderer_key)
        if type(decision["visible"]) is not bool:
            raise TypeError(f"Selection visibility must be boolean: {renderer_key}")
        renderer = renderer_map[renderer_key]
        submesh_count = int_value(renderer["submesh_count"], "renderer.submesh_count")
        if submesh_count < 1:
            raise ValueError(f"Source renderer has no submeshes: {renderer_key}")
        refs = tuple(identity(object_value(pointer, "material.pointer")["resolved"], "material.resolved")
                     for pointer in array_value(renderer["materials"], "renderer.materials"))
        if len(refs) < submesh_count:
            raise ValueError(f"Source renderer has fewer materials than submeshes: {renderer_key}")
        required.update(refs[:submesh_count])
        if decision["visible"] is True:
            visible_extras.update(refs[submesh_count:])
    if seen != set(renderer_map):
        raise ValueError(f"Selection does not cover every source renderer: missing={sorted(set(renderer_map) - seen)}")
    return frozenset(visible_extras - required)


def optional_standalone_vfx_renderer_skips(
    packet: SourcePacket,
) -> tuple[dict[Identity, dict[str, object]], dict[Identity, dict[str, object]]]:
    """Identify unsupported VFX-only meshes isolated from every non-FX renderer."""
    sources = {source.identity: source for source in packet.materials}
    renderer_map = {identity(row["object"], "renderer.object"): row for row in packet.renderers}
    if len(renderer_map) != len(packet.renderers):
        raise ValueError("Duplicate source renderer identity")
    mesh_counts: dict[Identity, int] = {}
    renderer_refs: dict[Identity, tuple[Identity, ...]] = {}
    renderer_paths: dict[Identity, str] = {}
    for renderer_key, renderer in renderer_map.items():
        mesh_key = identity(renderer["mesh"], "renderer.mesh")
        mesh_counts[mesh_key] = mesh_counts.get(mesh_key, 0) + 1
        renderer_refs[renderer_key] = tuple(
            identity(object_value(pointer, "material.pointer")["resolved"], "material.resolved")
            for pointer in array_value(renderer["materials"], "renderer.materials"))
        renderer_paths[renderer_key] = string_value(renderer["path"], "renderer.path")
    dedicated: set[Identity] = set()
    non_fx_materials: set[Identity] = set()
    for renderer_key, renderer in renderer_map.items():
        refs = renderer_refs[renderer_key]
        mesh_key = identity(renderer["mesh"], "renderer.mesh")
        path = renderer_paths[renderer_key].casefold()
        is_vfx_mesh = ("_fx_" in path and mesh_counts[mesh_key] == 1
                       and int_value(renderer["submesh_count"], "renderer.submesh_count") == 1
                       and len(refs) == 1 and refs[0] in sources
                       and sources[refs[0]].shader in (CHARACTER_VFX_SHADER, "HGRP/Effect/VFXBaseV2"))
        if is_vfx_mesh:
            dedicated.add(renderer_key)
        else:
            non_fx_materials.update(refs)
    visible_fx = set()
    seen: set[Identity] = set()
    for decision in packet.selection:
        renderer_text = string_value(decision["renderer_identity"], "renderer_identity")
        cab, path_id = renderer_text.rsplit(":", 1)
        renderer_key = (cab.casefold(), int(path_id))
        if renderer_key not in renderer_map or renderer_key in seen:
            raise ValueError(f"Selection renderer is absent or duplicated: {renderer_key}")
        seen.add(renderer_key)
        if type(decision["visible"]) is not bool:
            raise TypeError(f"Selection visibility must be boolean: {renderer_key}")
        if decision["visible"] is True and renderer_key in dedicated:
            visible_fx.add(renderer_key)
    if seen != set(renderer_map):
        raise ValueError(f"Selection does not cover every source renderer: missing={sorted(set(renderer_map) - seen)}")
    unsupported_materials: dict[Identity, dict[str, object]] = {}
    for renderer_key in visible_fx:
        source = sources[renderer_refs[renderer_key][0]]
        if source.identity in non_fx_materials:
            continue
        active = tuple((name, source.floats.get(name, 0.0)) for name in UNSUPPORTED_CHARACTER_VFX_RENDERER_FLAGS
                       if source.shader == CHARACTER_VFX_SHADER and source.floats.get(name, 0.0) != 0.0)
        if active:
            invalid = tuple((name, value) for name, value in active if value != 1.0)
            if invalid:
                raise ValueError(f"Dedicated VFX source switches must be binary before optional skipping: material={source.identity}; fields={invalid}")
            features = tuple(name for name, _ in active)
        else:
            from blender_scene_effect_layers import vfx_mesh_feature_issues
            from character_mesh_effect import _scene_record
            from character_npr_vfx import character_vfx_preview_record
            record, _, _ = _scene_record(source, packet)
            if source.shader == CHARACTER_VFX_SHADER:
                # Geometry Nodes own this stage in the production surface adapter.
                surface_record = {**record, "Floats": {**record["Floats"], "_UseVertexOffset": 0.0}}
                preview = character_vfx_preview_record(surface_record)
            else:
                preview = record
            # Particle/custom-UV inputs are unsupported with either valid mesh color layout.
            # Do not infer missing streams or preempt the adapter's supported UV branches.
            without_color = frozenset(vfx_mesh_feature_issues(preview, 0))
            features = tuple(issue for issue in vfx_mesh_feature_issues(preview, 4) if issue in without_color)
            if "particle_input_on_mesh_renderer" not in features:
                continue
        reason = (f"Dedicated VFX source branch has no host implementation: material={source.name}; "
                  f"shader={source.shader}; features={features}")
        renderer_rows = tuple(key for key in dedicated if renderer_refs[key] == (source.identity,))
        selected_rows = tuple(key for key in renderer_rows if key in visible_fx)
        diagnostic = {
            "status": "skipped_unsupported_optional_effect_renderer",
            "component": "dedicated_vfx_renderer",
            "material": source.name,
            "shader": source.shader,
            "material_identity": list(source.identity),
            "reason": reason,
            "unsupported_features": list(features),
            "retained": "character_body_and_source_mesh_geometry",
            "renderer_identities": [f"{key[0]}:{key[1]}" for key in sorted(renderer_rows)],
            "renderer_paths": [renderer_paths[key] for key in sorted(renderer_rows)],
            "selected_renderer_identities": [f"{key[0]}:{key[1]}" for key in sorted(selected_rows)],
            "selected_renderer_paths": [renderer_paths[key] for key in sorted(selected_rows)],
        }
        unsupported_materials[source.identity] = diagnostic
    unsupported_renderers = {
        renderer_key: {**unsupported_materials[renderer_refs[renderer_key][0]],
                       "renderer_identity": f"{renderer_key[0]}:{renderer_key[1]}",
                       "renderer_path": renderer_paths[renderer_key]}
        for renderer_key in dedicated
        if renderer_refs[renderer_key][0] in unsupported_materials
    }
    return unsupported_materials, unsupported_renderers


def read_material_sources(
    source_dir: Path,
    material_records: Sequence[dict[str, object]],
    shader_document: Mapping[str, object],
    visible_materials: frozenset[Identity],
) -> tuple[tuple[MaterialSource, ...], Mapping[Identity, TextureSource]]:
    """Decode verified source material/texture evidence independently of a rig inventory.

    The caller validates the evidence document identity and supplies source material
    records plus exact visible references. This parser retains the same strict PNG,
    Shader pointer, source parameter, UV and visible shader checks as read_packet.
    """
    shader_names: dict[Identity, str] = {}
    shader_texture_defaults: dict[Identity, dict[str, str]] = {}
    shader_property_types: dict[Identity, Mapping[str, int]] = {}
    shader_alpha_keywords: dict[Identity, str | None] = {}
    shader_uv2_keywords: dict[Identity, str | None] = {}
    for row in array_value(shader_document["shaders"], "shaders"):
        record = object_value(row, "shader")
        ref = object_value(record["shader"], "shader.ref")
        name = string_value(ref["name"], "shader.name")
        if name == CHARACTER_VFX_SHADER:
            validate_character_vfx_shader(record)
        key = identity(ref, "shader.ref")
        if key in shader_names:
            raise ValueError(f"Duplicate Shader identity: {key}")
        shader_names[key] = name
        defaults: dict[str, str] = {}
        property_types: dict[str, int] = {}
        alpha_keyword: str | None = None
        uv2_keyword: str | None = None
        uv2_mask_layout = False
        if "property_defaults" in record:
            for raw_property in array_value(record["property_defaults"], "shader.property_defaults"):
                property_row = object_value(raw_property, "shader.property")
                property_name = string_value(property_row["m_Name"], "shader.property.m_Name")
                property_type = int_value(property_row["m_Type"], "shader.property.m_Type")
                if property_name in property_types:
                    raise ValueError(f"Duplicate source Shader property: {key}, {property_name}")
                property_types[property_name] = property_type
                if property_name == "_ExtraAlphaMask" and property_type == 4:
                    description = property_row.get("m_Description")
                    uv2_mask_layout = isinstance(description, str) and "UV1 Alpha/Root/Depth/ID" in description
                if property_name == "_EnableAlphaTest" and property_type == 2:
                    attributes = array_value(property_row.get("m_Attributes", []), "_EnableAlphaTest.m_Attributes")
                    if "Toggle(_ALPHATEST_ON)" in attributes:
                        alpha_keyword = "_ALPHATEST_ON"
                if property_name == "_UseUV2Color" and property_type == 2:
                    attributes = array_value(property_row.get("m_Attributes", []), "_UseUV2Color.m_Attributes")
                    if "Toggle(_UV2_COLOR)" in attributes:
                        uv2_keyword = "_UV2_COLOR"
                if property_type != 4:
                    continue
                texture_default = object_value(property_row["m_DefTexture"], "shader.property.m_DefTexture")
                if int_value(texture_default["m_TexDim"], "shader.property.m_TexDim") != 2:
                    continue
                if property_name in defaults:
                    raise ValueError(f"Duplicate source Shader texture property: {key}, {property_name}")
                default_name = texture_default["m_DefaultName"]
                if not isinstance(default_name, str):
                    raise TypeError(f"Expected source Shader texture default string: {key}, {property_name}")
                defaults[property_name] = default_name
        shader_texture_defaults[key] = defaults
        shader_property_types[key] = MappingProxyType(property_types)
        shader_alpha_keywords[key] = alpha_keyword
        shader_uv2_keywords[key] = uv2_keyword if uv2_mask_layout else None
    trees: dict[Identity, dict[str, object]] = {}
    for row in array_value(read_json(source_dir / "material_sources.json"), "material_sources"):
        record = object_value(row, "material_tree")
        if record["available"] is not True:
            raise ValueError(f"Material has no embedded TypeTree: {record['object']}")
        key = identity(record["object"], "material_tree.object")
        if key in trees:
            raise ValueError(f"Duplicate material TypeTree: {key}")
        trees[key] = object_value(record["data"], "material_tree.data")
    textures: dict[Identity, TextureSource] = {}
    for row in array_value(read_json(source_dir / "source_texture_artifacts.json"), "source_texture_artifacts"):
        record = object_value(row, "texture")
        ref = object_value(record["object"], "texture.object")
        png = object_value(record["png"], "texture.png")
        path = (source_dir / string_value(png["path"], "png.path").replace("\\", "/")).resolve()
        if not path.is_relative_to(source_dir.resolve()):
            raise ValueError(f"Texture escapes its source packet: {path}")
        if io_path(path).stat().st_size != int_value(png["bytes"], "png.bytes") or file_hash(path) != string_value(png["sha256"], "png.sha256").casefold():
            raise ValueError(f"Source texture identity changed: {path}")
        key = identity(ref, "texture.object")
        if key in textures:
            raise ValueError(f"Duplicate source Texture2D identity: {key}")
        textures[key] = TextureSource(key, string_value(ref["name"], "texture.name"), path, file_hash(path), int_value(record["width"], "texture.width"), int_value(record["height"], "texture.height"), int_value(record["color_space"], "texture.color_space"), string_value(record["source_format"], "texture.source_format"))
    materials: list[MaterialSource] = []
    for row in material_records:
        material = object_value(row, "material")
        key = identity(material["object"], "material.object")
        tree = trees[key]
        pointer = object_value(material["shader"], "material.shader")
        tree_shader = object_value(tree["m_Shader"], "material_tree.m_Shader")
        if int_value(tree_shader["m_PathID"], "material_tree.shader.PathID") != int_value(pointer["path_id"], "material.shader.PathID") or int_value(tree_shader["m_FileID"], "material_tree.shader.FileID") != int_value(pointer["file_id"], "material.shader.FileID"):
            raise ValueError(f"Material TypeTree and audit Shader pointers disagree: {key}")
        shader_key = identity(pointer["resolved"], "material.shader.resolved")
        shader_name = shader_names[shader_key]
        if shader_name not in SUPPORTED_SHADERS and key in visible_materials:
            raise ValueError(f"Unsupported actual source Shader referenced by visible renderer: shader={shader_name}, material={key}")
        saved = object_value(tree["m_SavedProperties"], "m_SavedProperties")
        texenvs = {string_value(object_value(entry, "texenv")["Key"], "texenv.Key"): object_value(object_value(entry, "texenv")["Value"], "texenv.Value") for entry in array_value(saved["m_TexEnvs"], "m_TexEnvs")}
        bindings: list[TextureBinding] = []
        null_texture_defaults: dict[str, str] = {}
        for raw_slot in array_value(material["textures"], "material.textures"):
            slot = object_value(raw_slot, "texture.slot")
            pointer = object_value(slot["reference"], "texture.reference")
            if int_value(pointer["path_id"], "texture.path_id") == 0:
                if pointer["resolved"] is not None or int_value(pointer["file_id"], "texture.file_id") != 0:
                    raise ValueError("Null texture pointer unexpectedly resolved")
                name = string_value(slot["slot"], "texture.slot")
                if name in shader_texture_defaults[shader_key]:
                    null_texture_defaults[name] = shader_texture_defaults[shader_key][name]
                continue
            key_texture = identity(pointer["resolved"], "texture.resolved")
            if key_texture not in textures:
                raise FileNotFoundError(f"Referenced source texture has no verified PNG: {key_texture}")
            name = string_value(slot["slot"], "texture.slot")
            uv_index = int_value(texenvs[name]["m_UVSetIndex"], "m_UVSetIndex")
            if uv_index != 0 and shader_name in SUPPORTED_SHADERS:
                raise ValueError(f"Research nodes currently require source UV0; material={key}, slot={name}, UV={uv_index}")
            bindings.append(TextureBinding(name, key_texture, cast(tuple[float, float], numeric_tuple(slot["scale"], 2, "texture.scale")), cast(tuple[float, float], numeric_tuple(slot["offset"], 2, "texture.offset")), uv_index))
        floats = {string_value(object_value(entry, "float")["name"], "float.name"): float_value(object_value(entry, "float")["value"], "float.value") for entry in array_value(material["floats"], "material.floats")}
        colors = {string_value(object_value(entry, "color")["name"], "color.name"): cast(tuple[float, float, float, float], numeric_tuple(object_value(entry, "color")["rgba"], 4, "color.rgba")) for entry in array_value(material["colors"], "material.colors")}
        keywords = tuple(string_value(keyword, "keyword") for keyword in array_value(tree["m_ValidKeywords"], "m_ValidKeywords"))
        source = MaterialSource(key, string_value(object_value(material["object"], "material.object")["name"], "material.name"), shader_name, MappingProxyType(floats), MappingProxyType(colors), tuple(bindings), keywords, json.dumps(material, ensure_ascii=True, sort_keys=True), json.dumps(tree, ensure_ascii=True, sort_keys=True), MappingProxyType(null_texture_defaults), shader_property_types[shader_key], shader_alpha_keywords[shader_key], shader_uv2_keywords[shader_key])
        if shader_name == "HGRP/LitEffect":
            from character_lit_effect import validate_lit_effect_source
            validate_lit_effect_source(source, textures)
        materials.append(source)
    if len({material.identity for material in materials}) != len(materials):
        raise ValueError("Duplicate source material identity")
    return tuple(materials), MappingProxyType(textures)


def read_packet(source_dir: Path, shader_path: Path, selection_path: Path, visibility_path: Path) -> SourcePacket:
    paths = (source_dir / "source_audit.json", source_dir / "material_sources.json", source_dir / "source_texture_artifacts.json", shader_path, selection_path, visibility_path)
    audit = object_value(read_json(paths[0]), "source_audit")
    shader = object_value(read_json(shader_path), "shader_identity")
    selection = object_value(read_json(selection_path), "selection")
    visibility = object_value(read_json(visibility_path), "renderer_visibility")
    if audit["format"] != "EndfieldCharacterSourceAudit/1" or shader["format"] != "EndfieldCharacterShaderIdentity/1" or selection["format"] not in ("CharacterLodPreviewSelection/2", "EndfieldWeaponRendererSelection/1"):
        raise ValueError("Unsupported input evidence format")
    if string_value(shader["source_audit_sha256"], "source_audit_sha256").casefold() != file_hash(paths[0]):
        raise ValueError("Shader identities refer to a different source audit")
    if selection["format"] == "CharacterLodPreviewSelection/2":
        if identity(audit["actual_root"], "actual_root")[0] != string_value(selection["lod_group_identity"], "lod_group_identity").split(":")[0].casefold():
            raise ValueError("Selection report belongs to another source container")
    else:
        root = object_value(selection["source_root_identity"], "selection.source_root_identity")
        if root.get("type") != "GameObject" or identity(root, "selection.source_root_identity") != identity(audit["actual_root"], "actual_root"):
            raise ValueError("Weapon selection belongs to another source root")
        for field, path in (("source_audit_sha256", paths[0]), ("lod_sources_sha256", source_dir / "lod_sources.json"),
                            ("renderer_visibility_sha256", visibility_path)):
            if string_value(selection[field], "weapon_selection." + field).casefold() != file_hash(path):
                raise ValueError(f"Weapon selection refers to different source evidence: field={field}, path={path}")
    if visibility["format"] != "EndfieldCharacterRendererVisibility/1" or identity(visibility["actual_root"], "visibility.actual_root") != identity(audit["actual_root"], "actual_root"):
        raise ValueError("Renderer pipeline evidence belongs to another source root")
    renderer_pipeline: dict[Identity, dict[str, object]] = {}
    for row in array_value(visibility["renderers"], "visibility.renderers"):
        record = object_value(row, "visibility.renderer")
        key = identity(record["renderer"], "visibility.renderer.identity")
        tree = object_value(record["renderer_type_tree"], "renderer_type_tree")
        if tree["available"] is not True or identity(tree["object"], "renderer_type_tree.object") != key:
            raise ValueError(f"Renderer has no identity-matched pipeline TypeTree: {key}")
        if key in renderer_pipeline:
            raise ValueError(f"Duplicate renderer pipeline identity: {key}")
        renderer_pipeline[key] = object_value(tree["data"], "renderer_type_tree.data")
    renderers = tuple(object_value(row, "renderer") for row in array_value(audit["renderers"], "renderers"))
    decisions = tuple(object_value(row, "decision") for row in array_value(selection["renderer_decisions"], "renderer_decisions"))
    visible_materials = visible_material_identities(renderers, decisions)
    material_records = tuple(object_value(row, "material") for row in array_value(audit["materials"], "materials"))
    materials, textures = read_material_sources(source_dir, material_records, shader, visible_materials)
    return SourcePacket(materials, textures, renderers, decisions, MappingProxyType(renderer_pipeline), MappingProxyType({str(path): file_hash(path) for path in paths}), visible_materials)


def source_float(source: MaterialSource, name: str) -> float:
    if name not in source.floats:
        raise ValueError(f"Required source parameter missing: material={source.identity}, field={name}")
    return source.floats[name]


def source_color(source: MaterialSource, name: str) -> tuple[float, float, float, float]:
    if name not in source.colors:
        raise ValueError(f"Required source color missing: material={source.identity}, field={name}")
    return source.colors[name]


def fur_dye_color(tree: bpy.types.ShaderNodeTree, source: MaterialSource,
                  samples: Mapping[str, bpy.types.Node], color: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Apply or verify the existing source-selected ScreenBlend without counting metadata as a connection."""
    if source.shader != "HGRP/CharacterNPR" or "_CHARACTER_FUR_DYE" not in source.keywords:
        return color
    expected = {"_FurDyeEnable": 2, "_FurDyeIntensity": 3, "_FurDyeMap": 4}
    if any(source.shader_property_types.get(name) != kind for name, kind in expected.items()):
        raise ValueError(f"Source Fur dye property/type contract is incompatible: material={source.identity}")
    enabled, intensity = source_float(source, "_FurDyeEnable"), source_float(source, "_FurDyeIntensity")
    if enabled != 1.0 or not 0.0 <= intensity <= 1.0 or "_FurDyeMap" not in samples:
        raise ValueError(f"Source Fur dye requires enabled bounded intensity and its actual map: material={source.identity}, enabled={enabled}, intensity={intensity}")
    sample = samples["_FurDyeMap"]
    if sample.type != "TEX_IMAGE" or sample.image is None or sample.image.packed_file is None:
        raise ValueError(f"Source Fur dye requires its actual packed image: material={source.identity}")
    color_space = sample.get("source_color_space")
    if (type(color_space) is not int or color_space not in (0, 1)
            or sample.image.colorspace_settings.name != ("sRGB" if color_space == 1 else "Non-Color")):
        raise ValueError(f"Source Fur dye requires its native texture color role: material={source.identity}, source={color_space}, host={sample.image.colorspace_settings.name}")
    links = tuple(sample.outputs["Color"].links)
    if links:
        if len(links) != 1:
            raise ValueError(f"Source Fur dye has ambiguous existing color consumers: material={source.identity}")
        mix = links[0].to_node
        if (mix.type != "MIX_RGB" or mix.blend_type != "SCREEN" or links[0].to_socket != mix.inputs[2]
                or mix.inputs[0].is_linked or abs(mix.inputs[0].default_value - intensity) > 1e-7
                or len(mix.inputs[1].links) != 1 or mix.inputs[1].links[0].from_socket != color
                or not mix.outputs[0].is_linked):
            raise ValueError(f"Existing source Fur dye is not the verified ScreenBlend branch: material={source.identity}")
        dyed = mix.outputs[0]
    else:
        dyed = color_mix(tree, "SCREEN", intensity, color, sample.outputs["Color"],
                         "Source Fur dye ScreenBlend host approximation", "Source Surface Channels")
    tree["source_fur_dye"] = json.dumps({"intensity": intensity, "slot": "_FurDyeMap", "node": dyed.node.name,
        "source_color_space": color_space, "host_color_space": sample.image.colorspace_settings.name,
        "reference": "Ruri_Endfield_Uber.glsl:2205-2212 Shell_DyeBlend",
        "formula": "mix(albedo,1-(1-albedo)*(1-dye),FurDyeIntensity)",
        "native_shader_semantics_verified": False,
        "scope": "Source map, ST and scalar consumed by an explicit host approximation; current compiled program equivalence is unverified."}, sort_keys=True)
    return dyed


def fur_surface_coordinates(tree: bpy.types.ShaderNodeTree, source: MaterialSource,
                            samples: Mapping[str, bpy.types.Node]) -> None:
    """Use the Fur semantic surface varying for its enabled packed normal sample."""
    if source.shader != "HGRP/CharacterNPR" or "_CHARACTER_FUR" not in source.keywords:
        return
    if source_float(source, "_UseBumpMap") == 0.0:
        return
    if "_BaseMap" not in samples or "_BumpMap" not in samples:
        raise ValueError(f"Enabled Fur normal mapping requires BaseMap and BumpMap: {source.identity}")
    base, bump = samples["_BaseMap"], samples["_BumpMap"]
    base_link, = base.inputs["Vector"].links
    bindings = {binding.name: binding for binding in source.textures}
    if bindings["_BaseMap"].uv_index != 0 or bindings["_BumpMap"].uv_index != 0:
        raise ValueError(f"Fur surface normal varying requires source UV0: {source.identity}")
    tree.links.new(base_link.from_socket, bump.inputs["Vector"])
    tree["source_fur_normal_uv"] = json.dumps({
        "space": "base_map_st", "base_scale": bindings["_BaseMap"].scale,
        "base_offset": bindings["_BaseMap"].offset,
        "retained_bump_scale": bindings["_BumpMap"].scale,
        "retained_bump_offset": bindings["_BumpMap"].offset,
        "sample_node": bump.name, "coordinate_node": base_link.from_node.name,
        "native_sampler_instruction_verified": False,
        "scope": "Fur semantic normal sampling uses the shared surface varying. Compiled Fur material exposes BaseMap ST but no separate BumpMap ST; the actual normal texture instruction remains unresolved.",
    }, sort_keys=True)


def make_environment(layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    tree = bpy.data.node_groups.new("EF Character Environment - APPROXIMATION", "ShaderNodeTree")
    for name, value, description in (("Wetness", 0.0, "Artist control; original _WetEffectIntensity binding is unverified"), ("Rain Wetness", 0.0, "Uniform wetness approximation; no animated rain streak simulation"), ("Water Height Meters", -1000.0, "World Blender Z corresponds to Unity world Y in this audited meter scene"), ("Submersion Strength", 0.0, "Approximate height-mask intensity"), ("Wet Darkening", 0.25, "Artist preview coefficient, not a recovered game constant"), ("Wet Roughness", 0.12, "Artist preview roughness, not a recovered game constant")):
        tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type="NodeSocketFloat")
        control = node(tree, "ShaderNodeValue", name + " (editable)", "Approximate Environment Controls")
        control.outputs[0].default_value = value
        control["evidence_note"] = description
        output = tree.nodes.get("Environment Outputs")
        if output is None:
            output = node(tree, "NodeGroupOutput", "Approximation - no snow formula", "Approximate Environment Controls")
            output.name = "Environment Outputs"
        tree.links.new(control.outputs[0], output.inputs[name])
    tree["scope"] = "Wetness preview only; source global/material bindings are unverified. Snow is intentionally absent."
    layout(tree)
    return tree


def decode_normal(tree: bpy.types.NodeTree, sample: bpy.types.Node, strength: bpy.types.NodeSocket, multiply_alpha: bool, label: str) -> bpy.types.NodeSocket:
    section = "Packed Tangent Normal - Source Channels"
    channels = node(tree, "ShaderNodeSeparateColor", label + " RG / BA", section)
    tree.links.new(sample.outputs["Color"], channels.inputs[0])
    red = math_node(tree, "MULTIPLY", channels.outputs["Red"], sample.outputs["Alpha"], "Unity R times A", section) if multiply_alpha else channels.outputs["Red"]
    components = [math_node(tree, "SUBTRACT", math_node(tree, "MULTIPLY", channel, 2.0, "2 * packed channel", section), 1.0, "Signed tangent channel", section) for channel in (red, channels.outputs["Green"])]
    if not multiply_alpha:
        components = [math_node(tree, "MULTIPLY", component, strength, "Hair RG strength before Z", section) for component in components]
    xx = math_node(tree, "MULTIPLY", components[0], components[0], "X squared", section)
    yy = math_node(tree, "MULTIPLY", components[1], components[1], "Y squared", section)
    xy = math_node(tree, "ADD", xx, yy, "X squared + Y squared", section)
    remaining = math_node(tree, "MAXIMUM", math_node(tree, "SUBTRACT", 1.0, xy, "1 - X squared - Y squared", section), 0.0, "Clamp nonnegative Z squared", section)
    z = math_node(tree, "SQRT", remaining, 0.0, "Reconstruct positive Z", section)
    if multiply_alpha:
        components = [math_node(tree, "MULTIPLY", component, strength, "Unity strength after Z", section) for component in components]
    combine = node(tree, "ShaderNodeCombineColor", "Encoded reconstructed tangent normal", section)
    for index, component in enumerate((*components, z)):
        encoded = math_node(tree, "ADD", math_node(tree, "MULTIPLY", component, 0.5, "Encode signed normal", section), 0.5, "Normal to RGB", section)
        tree.links.new(encoded, combine.inputs[index])
    normal = node(tree, "ShaderNodeNormalMap", "Shared Eevee / Cycles tangent normal", section)
    normal.uv_map = "UV0"
    tree.links.new(combine.outputs[0], normal.inputs["Color"])
    return normal.outputs[0]


def texture_colorspace(binding: TextureBinding, texture: TextureSource) -> str:
    if texture.color_space not in (0, 1):
        raise ValueError(f"Unsupported source texture color space: {texture.identity}, {texture.color_space}")
    color_role = binding.name in {"_BaseMap", "_BaseColorMap", "_EmissionMap", "_EmissiveMap", "_MatcapTex", "_EmotionMap", "_ShadowLutTex", "_FurDyeMap", "_VFXSpecialBlendTex", "_VFXSpecialMainTex", "_ParallaxTex"}
    return "sRGB" if color_role and texture.color_space == 1 else "Non-Color"


def fur_coverage_sample(tree: bpy.types.ShaderNodeTree, original: bpy.types.Node,
                        slot: str) -> bpy.types.Node:
    """Share the packed image without changing the canonical source-slot sampler."""
    if original.type != "TEX_IMAGE" or original.image is None or original.image.packed_file is None:
        raise ValueError(f"Fur coverage requires its actual packed source image: {slot}")
    sample = node(tree, "ShaderNodeTexImage", "Fur coverage sampling " + slot, "Approx Fur Coverage")
    sample.image = original.image
    sample.interpolation = original.interpolation
    sample.extension = original.extension
    sample.projection = original.projection
    sample.projection_blend = original.projection_blend
    sample["source_coverage_slot"] = slot
    sample["source_identity"] = original["source_identity"]
    sample["source_sha256"] = original["source_sha256"]
    return sample


def fur_shell_coverage(tree: bpy.types.ShaderNodeTree, source: MaterialSource,
                       samples: Mapping[str, bpy.types.Node]) -> bpy.types.NodeSocket:
    """Interpret the reference shell fragment mask with actual source UVs and inputs."""
    section = "Approx Fur Coverage"
    names = ("_FurDirMapEnable", "_FurFlowBaseStrength", "_FurNoise", "_FurCutoffStart",
             "_FurCutoffEnd", "_FurSharpen", "_FurEdgeFade")
    values = {name: source_float(source, name) for name in names}
    if (values["_FurDirMapEnable"] not in (0.0, 1.0)
            or not 0.0 <= values["_FurCutoffStart"] <= values["_FurCutoffEnd"] <= 1.0
            or not 0.0 <= values["_FurSharpen"] <= 1.0):
        raise ValueError(f"Unsupported source Fur coverage parameters: material={source.identity}, values={values}")
    bindings = {binding.name: binding for binding in source.textures}
    if "_FurMap" not in samples or "_BaseMap" not in samples:
        raise ValueError(f"Source Fur coverage requires actual FurMap and BaseMap bindings: {source.identity}")
    fur = bindings["_FurMap"]
    base = bindings["_BaseMap"]
    if fur.uv_index != 0 or base.uv_index != 0:
        raise ValueError(f"Source Fur reference requires UV0 for BaseMap and FurMap: {source.identity}")
    uv = node(tree, "ShaderNodeUVMap", "Source shell layer from imported TEXCOORD1.x", section)
    uv.uv_map = "UV1"
    split = node(tree, "ShaderNodeSeparateXYZ", "Unmodified source shell layer", section)
    tree.links.new(uv.outputs[0], split.inputs[0])
    layer = split.outputs["X"]
    noise = math_node(tree, "MULTIPLY", layer, 12.9898 + 78.233, "Shell noise dot", section)
    noise = math_node(tree, "SINE", noise, 0.0, "Shell noise sine", section)
    noise = math_node(tree, "MULTIPLY", noise, 43758.5469, "Shell noise hash", section)
    noise = math_node(tree, "FRACT", noise, 0.0, "Shell noise fraction", section)
    noise = math_node(tree, "SUBTRACT", math_node(tree, "MULTIPLY", noise, 2.0, "Signed noise scale", section), 1.0, "Signed shell noise", section)
    noise = math_node(tree, "MULTIPLY", noise, values["_FurNoise"] * 0.05, "Source Fur noise amplitude", section)
    if "_FurDirMap" in samples:
        direction = fur_coverage_sample(tree, samples["_FurDirMap"], "_FurDirMap")
        raw_uv = node(tree, "ShaderNodeUVMap", "Source direction uses pre-BaseMap UV0", section)
        raw_uv.uv_map = "UV0"
        tree.links.new(raw_uv.outputs[0], direction.inputs["Vector"])
        channels = node(tree, "ShaderNodeSeparateColor", "Source direction RG and coverage B", section)
        tree.links.new(direction.outputs["Color"], channels.inputs[0])
        direction_xy: tuple[bpy.types.NodeSocket | float, bpy.types.NodeSocket | float] = (channels.outputs["Red"], channels.outputs["Green"])
        direction_blue: bpy.types.NodeSocket | float = channels.outputs["Blue"]
    elif source.null_texture_defaults.get("_FurDirMap") == "bump" and values["_FurDirMapEnable"] == 0.0:
        direction_xy, direction_blue = (0.5, 0.5), 1.0
    else:
        raise ValueError(f"Source Fur direction image/default is unproven for coverage: {source.identity}")
    base_vector = samples["_BaseMap"].inputs["Vector"].links
    if len(base_vector) != 1:
        raise ValueError(f"Source Fur BaseMap coordinate input is ambiguous: {source.identity}")
    base_uv = node(tree, "ShaderNodeSeparateXYZ", "Actual transformed BaseMap UV for Fur sampling", section)
    tree.links.new(base_vector[0].from_socket, base_uv.inputs[0])
    mapped = node(tree, "ShaderNodeCombineXYZ", "Reference FurMap coordinates use ST.x on both axes", section)
    for axis, component, offset in zip(("X", "Y"), direction_xy, fur.offset, strict=True):
        flow = math_node(tree, "SUBTRACT", math_node(tree, "MULTIPLY", component, 2.0, "Direction signed scale " + axis, section), 1.0, "Direction signed offset " + axis, section)
        flow = math_node(tree, "MULTIPLY", flow, values["_FurDirMapEnable"] * values["_FurFlowBaseStrength"], "Source flow strength " + axis, section)
        flow = math_node(tree, "ADD", flow, noise, "Direction plus shell noise " + axis, section)
        flow = math_node(tree, "MULTIPLY", layer, flow, "Layer-scaled Fur offset " + axis, section)
        coordinate = math_node(tree, "SUBTRACT", base_uv.outputs[axis], flow, "Offset actual BaseMap UV " + axis, section)
        coordinate = math_node(tree, "ADD", math_node(tree, "MULTIPLY", coordinate, fur.scale[0], "Reference Fur tiling " + axis, section), offset, "Reference Fur offset " + axis, section)
        tree.links.new(coordinate, mapped.inputs[axis])
    fur_sample = fur_coverage_sample(tree, samples["_FurMap"], "_FurMap")
    tree.links.new(mapped.outputs[0], fur_sample.inputs["Vector"])
    fur_channels = node(tree, "ShaderNodeSeparateColor", "Actual FurMap red coverage sample", section)
    tree.links.new(fur_sample.outputs["Color"], fur_channels.inputs[0])
    cutoff = math_node(tree, "ADD", math_node(tree, "MULTIPLY", layer, values["_FurCutoffEnd"] - values["_FurCutoffStart"], "Layer cutoff range", section), values["_FurCutoffStart"], "Source layer cutoff", section)
    sharp = math_node(tree, "SQRT", cutoff, 0.0, "Square-root shell cutoff", section)
    sharp = math_node(tree, "ADD", math_node(tree, "MULTIPLY", cutoff, 1.0 - values["_FurSharpen"], "Unsharpened cutoff weight", section), math_node(tree, "MULTIPLY", sharp, values["_FurSharpen"], "Sharpened cutoff weight", section), "Source sharpened cutoff", section)
    low = math_node(tree, "MAXIMUM", math_node(tree, "SUBTRACT", sharp, 0.25, "Lower cutoff bound", section), 0.0, "Clamp lower cutoff", section)
    high = math_node(tree, "MINIMUM", math_node(tree, "ADD", sharp, 0.25, "Upper cutoff bound", section), 1.0, "Clamp upper cutoff", section)
    density = math_node(tree, "MULTIPLY", fur_channels.outputs["Red"], direction_blue, "Direction B times FurMap R", section)
    raw = math_node(tree, "DIVIDE", math_node(tree, "SUBTRACT", density, low, "Coverage above lower cutoff", section), math_node(tree, "SUBTRACT", high, low, "Coverage cutoff width", section), "Normalized shell coverage", section)
    raw = math_node(tree, "MINIMUM", math_node(tree, "MAXIMUM", raw, 0.0, "Clamp coverage low", section), 1.0, "Clamp coverage high", section)
    smooth = math_node(tree, "MULTIPLY", math_node(tree, "MULTIPLY", raw, raw, "Coverage squared", section), math_node(tree, "SUBTRACT", 3.0, math_node(tree, "MULTIPLY", raw, 2.0, "Coverage doubled", section), "Smoothstep factor", section), "Smooth shell coverage", section)
    base_layer = math_node(tree, "SUBTRACT", 1.0, math_node(tree, "GREATER_THAN", layer, 0.01, "Non-base shell layer", section), "Source base-layer preservation", section)
    smooth = math_node(tree, "ADD", smooth, math_node(tree, "MULTIPLY", base_layer, math_node(tree, "SUBTRACT", 1.0, smooth, "Base layer uncovered fraction", section), "Preserve base-layer coverage", section), "Coverage including base shell", section)
    geometry = node(tree, "ShaderNodeNewGeometry", "Host shading normal and view direction", section)
    facing = node(tree, "ShaderNodeVectorMath", "Host normal dot incoming view", section)
    facing.operation = "DOT_PRODUCT"
    tree.links.new(geometry.outputs["Normal"], facing.inputs[0])
    tree.links.new(geometry.outputs["Incoming"], facing.inputs[1])
    cube = math_node(tree, "POWER", layer, 3.0, "Shell layer cubed", section)
    edge = math_node(tree, "SUBTRACT", math_node(tree, "ADD", math_node(tree, "SUBTRACT", 1.0, cube, "Layer edge retention", section), facing.outputs["Value"], "View-facing shell edge", section), values["_FurEdgeFade"], "Source shell edge fade", section)
    alpha = math_node(tree, "MINIMUM", math_node(tree, "MAXIMUM", math_node(tree, "MULTIPLY", smooth, edge, "Coverage times edge fade", section), 0.0, "Clamp shell alpha low", section), 1.0, "Clamp shell alpha high", section)
    alpha = math_node(tree, "ADD", math_node(tree, "MULTIPLY", math_node(tree, "CEIL", layer, 0.0, "Reference shell ceil", section), math_node(tree, "SUBTRACT", alpha, 1.0, "Shell alpha minus one", section), "Non-base shell alpha", section), 1.0, "Reference shell alpha", section)
    alpha = math_node(tree, "MULTIPLY", alpha, math_node(tree, "SUBTRACT", 1.0, math_node(tree, "LESS_THAN", alpha, 0.003, "Reference Fur fragment clip", section), "Keep surviving shell fragments", section), "Host Fur shell coverage", section)
    tree["source_fur_shell_coverage"] = json.dumps({"status": "source_inputs_host_coverage_approximation", "reference": "Ruri_Endfield_Uber.glsl:2215-2233,2349-2350 Shell_SampleSurface", "uv_layer": "UV1", "layer_component": "X", "parameters": values, "fur_map_identity": fur.identity, "fur_map_scale": fur.scale, "fur_map_offset": fur.offset, "direction_default": source.null_texture_defaults.get("_FurDirMap"), "native_shader_semantics_verified": False, "scope": "Reference fragment arithmetic on retained source shells; Blender shading normal/view, implicit texture derivatives, hash precision and dithering are host approximations. No native vertex expansion or depth-prepass equivalence is claimed."}, sort_keys=True)
    return alpha


def uv2_alpha_mask(tree: bpy.types.ShaderNodeTree, source: MaterialSource,
                   samples: Mapping[str, bpy.types.Node]) -> bpy.types.NodeSocket:
    """Interpret the source UV1 Alpha/Root/Depth/ID texture's first channel as coverage."""
    enabled = source_float(source, "_UseUV2Color")
    if (source.uv2_color_keyword != "_UV2_COLOR" or enabled != 1.0
            or source.shader_property_types.get("_ExtraAlphaMask") != 4
            or "_ExtraAlphaMask" not in samples):
        raise ValueError(f"Source UV2 alpha requires its enabled Shader toggle and actual mask: material={source.identity}, enabled={enabled}")
    binding = next(item for item in source.textures if item.name == "_ExtraAlphaMask")
    original = samples["_ExtraAlphaMask"]
    sample = node(tree, "ShaderNodeTexImage", "Source UV1 alpha mask host sampling", "Approx UV1 Alpha Mask")
    sample.image = original.image
    sample.interpolation = original.interpolation
    sample.extension = original.extension
    sample["source_coverage_slot"] = "_ExtraAlphaMask"
    sample["source_identity"] = original["source_identity"]
    sample["source_sha256"] = original["source_sha256"]
    uv = node(tree, "ShaderNodeUVMap", "Source UV2 Color feature uses UV1", "Approx UV1 Alpha Mask")
    uv.uv_map = "UV1"
    mapping = node(tree, "ShaderNodeMapping", "Source ExtraAlphaMask scale and offset", "Approx UV1 Alpha Mask")
    mapping.vector_type = "POINT"
    mapping.inputs["Scale"].default_value = (*binding.scale, 1.0)
    mapping.inputs["Location"].default_value = (*binding.offset, 0.0)
    tree.links.new(uv.outputs[0], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs[0], sample.inputs["Vector"])
    channels = node(tree, "ShaderNodeSeparateColor", "UV1 Alpha/Root/Depth/ID first channel", "Approx UV1 Alpha Mask")
    tree.links.new(sample.outputs["Color"], channels.inputs[0])
    tree["source_uv2_alpha_mask"] = json.dumps({"status": "source_contract_host_coverage_approximation",
        "keyword": source.uv2_color_keyword, "property": "_UseUV2Color", "value": enabled,
        "slot": "_ExtraAlphaMask", "image": sample.image.name, "uv_layer": "UV1", "channel": "Red",
        "scale": binding.scale, "offset": binding.offset, "native_shader_semantics_verified": False,
        "scope": "Source Shader property describes UV1 Alpha/Root/Depth/ID; the first channel supplies a host alpha mask multiplied before source alpha thresholding. Native program channel arithmetic and sampling equivalence are unverified."}, sort_keys=True)
    return channels.outputs["Red"]


def load_images(packet: SourcePacket) -> Mapping[tuple[Identity, str, str], bpy.types.Image]:
    images: dict[tuple[Identity, str, str], bpy.types.Image] = {}
    for source in packet.materials:
        for binding in source.textures:
            texture = packet.textures[binding.identity]
            colorspace = texture_colorspace(binding, texture)
            alpha_mode = "CHANNEL_PACKED"
            if source.shader in MESH_EFFECT_SHADERS:
                colorspace = "Non-Color" if texture.color_space == 0 else "sRGB"
                alpha_mode = "STRAIGHT"
            key = (texture.identity, colorspace, alpha_mode)
            if key not in images:
                image = bpy.data.images.load(extended_path(texture.path), check_existing=False)
                image.name = "EFCHAR " + texture.name + " " + colorspace
                image.colorspace_settings.name = colorspace
                image.alpha_mode = alpha_mode
                if tuple(image.size) != (texture.width, texture.height):
                    raise ValueError(f"Decoded image dimensions differ from source: {texture.path}")
                image.pack()
                image.use_fake_user = True
                image["source_retention"] = "Declared source material input; retained even when its source feature is inactive in the host preview."
                images[key] = image
    return MappingProxyType(images)


def image_node(tree: bpy.types.NodeTree, binding: TextureBinding, texture: TextureSource, images: Mapping[tuple[Identity, str, str], bpy.types.Image]) -> bpy.types.Node:
    image = images[(texture.identity, texture_colorspace(binding, texture), "CHANNEL_PACKED")]
    result = node(tree, "ShaderNodeTexImage", binding.name + " | " + texture.source_format, "Verified Source Texture Inputs")
    result.image = image
    result["source_identity"] = json.dumps(texture.identity)
    result["source_sha256"] = texture.sha256
    result["source_color_space"] = texture.color_space
    result["source_slot"] = binding.name
    uv = node(tree, "ShaderNodeUVMap", "Source UV0", "Verified Source Texture Inputs")
    uv.uv_map = "UV0"
    mapping = node(tree, "ShaderNodeMapping", "Source slot scale and offset", "Verified Source Texture Inputs")
    mapping.vector_type = "POINT"
    mapping.inputs["Scale"].default_value = (*binding.scale, 1.0)
    mapping.inputs["Location"].default_value = (*binding.offset, 0.0)
    tree.links.new(uv.outputs[0], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs[0], result.inputs["Vector"])
    return result


def source_base_sample(tree: bpy.types.NodeTree, source: MaterialSource, samples: Mapping[str, bpy.types.Node]) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket]:
    """Use a source image or the exact referenced Shader's verified white default."""
    sample = samples.get("_BaseMap")
    if sample is not None:
        return sample.outputs["Color"], sample.outputs["Alpha"]
    if source.null_texture_defaults.get("_BaseMap") != "white":
        raise ValueError(f"Source shader requires a verified _BaseMap or an exact white default: {source.identity}")
    color = node(tree, "ShaderNodeRGB", "Exact Shader default _BaseMap: white", "Verified Source Shader Defaults")
    color.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)
    alpha = node(tree, "ShaderNodeValue", "Exact Shader default _BaseMap alpha: one", "Verified Source Shader Defaults")
    alpha.outputs[0].default_value = 1.0
    return color.outputs[0], alpha.outputs[0]


def archived_material_group(source: MaterialSource, packet: SourcePacket, images: Mapping[tuple[Identity, str, str], bpy.types.Image], layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    """Retain inactive source evidence without pretending to evaluate its shader."""
    if source.identity in packet.visible_materials or source.shader in SUPPORTED_SHADERS:
        raise ValueError(f"Archive group requires an unsupported inactive source shader: {source.identity}")
    tree = bpy.data.node_groups.new("EF Archived Character " + source.name, "ShaderNodeTree")
    tree["source_shader"] = source.shader
    tree["source_identity"] = json.dumps(source.identity)
    tree["source_shader_property_types"] = json.dumps(dict(source.shader_property_types), sort_keys=True)
    tree["source_floats"] = json.dumps(dict(source.floats), sort_keys=True)
    tree["source_colors"] = json.dumps(dict(source.colors), sort_keys=True)
    tree["archived_unsupported_shader"] = True
    tree["scope"] = "All source references are inactive in the audited selection. Texture/parameter archive only; transparent carrier is not a reconstructed shader."
    for name, kind in (("Shader", "NodeSocketShader"), ("Coverage", "NodeSocketFloat"), ("Multiply Factor", "NodeSocketColor")):
        tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type=kind)
    output = node(tree, "NodeGroupOutput", "Inactive unsupported shader archive", "Archived Source Evidence")
    transparent = node(tree, "ShaderNodeBsdfTransparent", "Non-rendering archive carrier", "Archived Source Evidence")
    tree.links.new(transparent.outputs[0], output.inputs["Shader"])
    output.inputs["Coverage"].default_value = 0.0
    output.inputs["Multiply Factor"].default_value = (1.0, 1.0, 1.0, 1.0)
    for binding in source.textures:
        texture = packet.textures[binding.identity]
        sample = node(tree, "ShaderNodeTexImage", binding.name + " | archived, inactive", "Archived Source Evidence")
        sample.image = images[(texture.identity, texture_colorspace(binding, texture), "CHANNEL_PACKED")]
        sample["source_identity"] = json.dumps(texture.identity)
        sample["source_sha256"] = texture.sha256
        sample["source_slot"] = binding.name
        sample["source_uv_index"] = binding.uv_index
        sample["source_scale"] = binding.scale
        sample["source_offset"] = binding.offset
    layout(tree)
    return tree


def material_group(source: MaterialSource, packet: SourcePacket, environment: bpy.types.ShaderNodeTree, images: Mapping[tuple[Identity, str, str], bpy.types.Image], lighting: bpy.types.ShaderNodeTree, layout: Callable[[bpy.types.NodeTree], None], color_source: MeshColorSource | None) -> bpy.types.ShaderNodeTree:
    if source.shader not in SUPPORTED_SHADERS:
        return archived_material_group(source, packet, images, layout)
    if source.shader == "HGRP/LitEffect":
        from character_lit_effect import build_lit_effect_group
        tree = bpy.data.node_groups.new("EF Static LitEffect " + source.name, "ShaderNodeTree")
        samples = {binding.name: image_node(tree, binding, packet.textures[binding.identity], images) for binding in source.textures}
        return build_lit_effect_group(source, tree, samples, layout)
    if source.shader in MESH_EFFECT_SHADERS:
        from character_mesh_effect import material_group as build_mesh_effect_group
        return build_mesh_effect_group(source, packet, images, color_source, layout)
    family = source.shader.split("/")[1]
    tree = bpy.data.node_groups.new("EF Character " + source.name, "ShaderNodeTree")
    tree["source_shader"] = source.shader
    tree["source_identity"] = json.dumps(source.identity)
    tree["source_shader_property_types"] = json.dumps(dict(source.shader_property_types), sort_keys=True)
    tree["source_floats"] = json.dumps(dict(source.floats), sort_keys=True)
    tree["source_colors"] = json.dumps(dict(source.colors), sort_keys=True)
    input_socket(tree, "Base Tint", "NodeSocketColor", source_color(source, "_BaseColor"), "Exact source _BaseColor")
    input_socket(tree, "Approx Pass Opacity", "NodeSocketFloat", 1.0, "Editable Blender opacity multiplier; source depth/stencil behavior is not reconstructed")
    for name, kind in (("Shader", "NodeSocketShader"), ("Coverage", "NodeSocketFloat"), ("Multiply Factor", "NodeSocketColor")):
        tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type=kind)
    entry = node(tree, "NodeGroupInput", "Editable Source and Preview Parameters", "Editable Parameters")
    output = node(tree, "NodeGroupOutput", family + " outputs", "Engine Outputs")
    samples = {binding.name: image_node(tree, binding, packet.textures[binding.identity], images) for binding in source.textures}
    base_color, base_alpha = source_base_sample(tree, source, samples)
    fur_surface_coordinates(tree, source, samples)
    if source.null_texture_defaults:
        tree["source_null_texture_defaults"] = json.dumps({
            slot: {
                "slot": slot,
                "default": default,
                "evidence": "Exact material-referenced Shader property TypeTree",
                "scope": "Unity null texture pointer uses the verified shader default; no source image was extracted",
            }
            for slot, default in source.null_texture_defaults.items()
        }, sort_keys=True)
    tint_alpha = source_color(source, "_BaseColor")[3]
    input_socket(tree, "Base Tint Alpha", "NodeSocketFloat", tint_alpha, "Exact source _BaseColor.a; Blender color sockets do not expose alpha as a separate output")
    if family == "CharacterNPR_OverlayShadow":
        input_socket(tree, "Use Gray As Alpha", "NodeSocketFloat", source_float(source, "_UseGrayAsAlpha"), "Source coverage lerps texture alpha to texture red")
        split = node(tree, "ShaderNodeSeparateColor", "Overlay source red mask", "Overlay Multiply - APPROXIMATION")
        tree.links.new(base_color, split.inputs[0])
        coverage = color_mix(tree, "MIX", entry.outputs["Use Gray As Alpha"], base_alpha, split.outputs["Red"], "Source alpha / red coverage", "Overlay Multiply - APPROXIMATION")
        rgb = color_mix(tree, "MIX", entry.outputs["Use Gray As Alpha"], base_color, (1.0, 1.0, 1.0, 1.0), "Gray-mask branch uses white before tint", "Overlay Multiply - APPROXIMATION")
        color = color_mix(tree, "MULTIPLY", 1.0, rgb, entry.outputs["Base Tint"], "Source overlay tint", "Overlay Multiply - APPROXIMATION")
        alpha = math_node(tree, "MULTIPLY", coverage, entry.outputs["Base Tint Alpha"], "Source coverage times tint alpha", "Overlay Multiply - APPROXIMATION")
        alpha = math_node(tree, "MULTIPLY", alpha, entry.outputs["Approx Pass Opacity"], "Editable overlay coverage", "Overlay Multiply - APPROXIMATION")
        factor = color_mix(tree, "MIX", math_node(tree, "MULTIPLY", alpha, entry.outputs["Base Tint Alpha"], "Source multiply intensity", "Overlay Multiply - APPROXIMATION"), (1.0, 1.0, 1.0, 1.0), color, "Source multiply framebuffer factor - no stencil/fog", "Overlay Multiply - APPROXIMATION")
        emission = node(tree, "ShaderNodeEmission", "Unlit source shadow tint approximation", "Overlay Multiply - APPROXIMATION")
        tree.links.new(color, emission.inputs["Color"])
        tree.links.new(emission.outputs[0], output.inputs["Shader"])
        tree.links.new(alpha, output.inputs["Coverage"])
        tree.links.new(factor, output.inputs["Multiply Factor"])
    else:
        for name, parameter in (("Smoothness", "_Smoothness"), ("Metallic", "_Metallic"), ("Specular", "_Specular")):
            input_socket(tree, name, "NodeSocketFloat", source_float(source, parameter), "Exact source " + parameter + "; Principled lighting is an approximation")
        input_socket(tree, "Preview SSS", "NodeSocketFloat", 0.0, "Optional artist skin approximation; no source scattering formula recovered")
        input_socket(tree, "Preview Hair Anisotropy", "NodeSocketFloat", 0.5 if family == "CharacterNPR_Hair" else 0.0, "Artist anisotropy approximation; source strand dual-normal lighting is not matched")
        input_socket(tree, "Preview Eye Coat", "NodeSocketFloat", 0.0, "Optional artist eye approximation; source matcap/parallax lighting is not matched")
        color = color_mix(tree, "MULTIPLY", 1.0, base_color, entry.outputs["Base Tint"], "Source base sample times BaseColor", "Source Surface Channels")
        color = fur_dye_color(tree, source, samples, color)
        from character_parallax_preview import apply_parallax_preview
        color = apply_parallax_preview(tree, source, samples, color, entry)
        if family == "CharacterNPR" and "_CUSTOMIZE_AVATAR" in source.keywords:
            customize_enabled = source_float(source, "_AvatarCustomizeEnable")
            if customize_enabled not in (0.0, 1.0):
                raise ValueError(f"Unsupported source Avatar customization toggle: material={source.identity}, value={customize_enabled}")
            customize_colors = (
                ("Customize Base Color", "_CustomizeBaseColor"),
                ("Customize Base Tint Color", "_CustomizeBaseTintColor"),
                ("Customize Add Tint Color", "_CustomizeAddTintColor"),
            )
            for label, parameter in customize_colors:
                input_socket(tree, label, "NodeSocketColor", source_color(source, parameter), "Exact source " + parameter)
            input_socket(tree, "Avatar Customize Enabled", "NodeSocketFloat", customize_enabled, "Exact source _AvatarCustomizeEnable toggle")
            tree["source_avatar_customization"] = json.dumps({
                "keyword": "_CUSTOMIZE_AVATAR",
                "enabled": customize_enabled,
                "formula": "sampled=BaseMap*BaseColor; tint=mix(AddTint,BaseTint,sampled.g); tint=mix(tint,CustomizeBaseColor,sampled.b); result=sampled.r*tint",
                "scope": "Generic CharacterNPR only; source sample assignment in the extracted compiled branch is absent, so BaseMap sampling dataflow is not fully verified.",
            }, sort_keys=True)
            channels = node(tree, "ShaderNodeSeparateColor", "Avatar customization masks after BaseMap 脳 BaseColor", "Source Avatar Customization")
            tree.links.new(color, channels.inputs[0])
            tint = color_mix(tree, "MIX", channels.outputs["Green"], entry.outputs["Customize Add Tint Color"], entry.outputs["Customize Base Tint Color"], "Source green mask: AddTint to BaseTint", "Source Avatar Customization")
            tint = color_mix(tree, "MIX", channels.outputs["Blue"], tint, entry.outputs["Customize Base Color"], "Source blue mask: tint to BaseColor", "Source Avatar Customization")
            red = node(tree, "ShaderNodeCombineColor", "Repeat source red mask across RGB", "Source Avatar Customization")
            for channel in ("Red", "Green", "Blue"):
                tree.links.new(channels.outputs["Red"], red.inputs[channel])
            customized = color_mix(tree, "MULTIPLY", 1.0, red.outputs[0], tint, "Source red mask times customized tint", "Source Avatar Customization")
            color = color_mix(tree, "MIX", entry.outputs["Avatar Customize Enabled"], color, customized, "Source Avatar customization toggle", "Source Avatar Customization")
        roughness = math_node(tree, "SUBTRACT", 1.0, entry.outputs["Smoothness"], "Roughness = 1 - source smoothness", "Source Surface Channels")
        metallic = entry.outputs["Metallic"]
        specular = entry.outputs["Specular"]
        if source.floats.get("_UseMetallicGlossMap") == 1.0:
            if "_MetallicGlossMap" not in samples:
                raise ValueError(f"Enabled source MetallicGlossMap missing: {source.identity}")
            packed = samples["_MetallicGlossMap"]
            channels = node(tree, "ShaderNodeSeparateColor", "R metallic / G specular / B AO / A smoothness", "Source Surface Channels")
            tree.links.new(packed.outputs["Color"], channels.inputs[0])
            roughness = math_node(tree, "SUBTRACT", 1.0, packed.outputs["Alpha"], "Source packed alpha to roughness", "Source Surface Channels")
            metallic, specular = channels.outputs["Red"], channels.outputs["Green"]
            color = color_mix(tree, "MULTIPLY", 1.0, color, channels.outputs["Blue"], "AO applied to diffuse - Blender approximation", "Source Surface Channels")
        environment_node = node(tree, "ShaderNodeGroup", "Wetness controls - APPROXIMATION", "Approximate Environment")
        environment_node.node_tree = environment
        geometry = node(tree, "ShaderNodeNewGeometry", "Audited meter world position", "Approximate Environment")
        xyz = node(tree, "ShaderNodeSeparateXYZ", "Blender world Z = Unity height", "Approximate Environment")
        tree.links.new(geometry.outputs["Position"], xyz.inputs[0])
        height = math_node(tree, "SUBTRACT", environment_node.outputs["Water Height Meters"], xyz.outputs["Z"], "Water height minus position", "Approximate Environment")
        mask = node(tree, "ShaderNodeMapRange", "Height mask smoothstep - binding unverified", "Approximate Environment")
        mask.interpolation_type = "SMOOTHSTEP"
        mask.clamp = True
        mask.inputs["From Min"].default_value = -0.2
        mask.inputs["From Max"].default_value = 0.15
        tree.links.new(height, mask.inputs["Value"])
        submersion = math_node(tree, "MULTIPLY", mask.outputs["Result"], environment_node.outputs["Submersion Strength"], "Approximate water coverage", "Approximate Environment")
        wetness = math_node(tree, "MAXIMUM", environment_node.outputs["Wetness"], environment_node.outputs["Rain Wetness"], "Approximate wetness max rain", "Approximate Environment")
        wetness = math_node(tree, "MINIMUM", math_node(tree, "MAXIMUM", wetness, submersion, "Approximate wetness max water", "Approximate Environment"), 1.0, "Clamp preview wetness", "Approximate Environment")
        if source.floats.get("_DisableRainEffectOnMaterial") == 1.0:
            wetness = math_node(tree, "MULTIPLY", wetness, 0.0, "Source material disables environment effect", "Approximate Environment")
        dark = math_node(tree, "SUBTRACT", 1.0, math_node(tree, "MULTIPLY", wetness, environment_node.outputs["Wet Darkening"], "Artist wet darkening", "Approximate Environment"), "Artist wet albedo scale", "Approximate Environment")
        color = color_mix(tree, "MULTIPLY", 1.0, color, dark, "Approximate wet surface color", "Approximate Environment")
        roughness = color_mix(tree, "MIX", wetness, roughness, environment_node.outputs["Wet Roughness"], "Approximate wet roughness", "Approximate Environment")
        bsdf = node(tree, "ShaderNodeBsdfPrincipled", family + " shared Eevee / Cycles surface", "Shared Engine Surface - APPROXIMATION")
        for socket, source_socket in (("Base Color", color), ("Roughness", roughness), ("Metallic", metallic), ("Specular IOR Level", specular)):
            tree.links.new(source_socket, bsdf.inputs[socket])
        if family == "CharacterNPR_Skin":
            tree.links.new(entry.outputs["Preview SSS"], bsdf.inputs["Subsurface Weight"])
        if family == "CharacterNPR_Hair":
            tree.links.new(entry.outputs["Preview Hair Anisotropy"], bsdf.inputs["Anisotropic"])
        if family == "CharacterNPR_Eye":
            tree.links.new(entry.outputs["Preview Eye Coat"], bsdf.inputs["Coat Weight"])
        if source.floats.get("_UseBumpMap") == 1.0 or "_NORMALMAP" in source.keywords:
            normal_slot = "_SplitNormalMap" if family == "CharacterNPR_Hair" and "_SPECULAR_NORMALMAP" in source.keywords else "_BumpMap"
            input_socket(tree, "Normal Strength", "NodeSocketFloat", source_float(source, "_BumpScale"), "Exact source _BumpScale")
            if normal_slot in samples:
                tree.links.new(decode_normal(tree, samples[normal_slot], entry.outputs["Normal Strength"], normal_slot == "_BumpMap", normal_slot), bsdf.inputs["Normal"])
            elif normal_slot == "_BumpMap" and source.null_texture_defaults.get(normal_slot) == "bump":
                # The exact source Shader declares Unity's neutral bump default.
                neutral = node(tree, "ShaderNodeNewGeometry", "Verified source null bump default - surface normal", "Source Surface Channels")
                tree.links.new(neutral.outputs["Normal"], bsdf.inputs["Normal"])
                tree["source_null_normal_default"] = json.dumps({"slot": normal_slot, "default": "bump", "evidence": "Exact material-referenced Shader property TypeTree", "scope": "Neutral source default, not a replacement for a missing non-null texture"}, sort_keys=True)
            else:
                raise ValueError(f"Enabled source normal texture missing or default unverified: material={source.identity}, slot={normal_slot}")
        if "_EMISSION" in source.keywords:
            emission_sample = samples.get("_EmissionMap")
            if emission_sample is not None:
                emission_map = emission_sample.outputs["Color"]
            elif source.null_texture_defaults.get("_EmissionMap") == "black":
                default_node = node(tree, "ShaderNodeRGB", "Exact Shader default _EmissionMap: black", "Verified Source Shader Defaults")
                default_node.outputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
                default_node["source_shader_texture_default"] = json.dumps({
                    "slot": "_EmissionMap",
                    "default": "black",
                    "evidence": "Exact material-referenced Shader property TypeTree",
                    "scope": "Unity null texture pointer uses the verified Shader default; no source image was extracted",
                }, sort_keys=True)
                tree["source_null_emission_default"] = json.dumps({
                    "slot": "_EmissionMap",
                    "default": "black",
                    "evidence": "Exact material-referenced Shader property TypeTree",
                    "scope": "Unity null texture pointer uses the verified Shader default; no source image was extracted",
                }, sort_keys=True)
                emission_map = default_node.outputs[0]
            else:
                raise ValueError(
                    f"Enabled source emission texture missing or default unverified: "
                    f"material={source.identity}, slot=_EmissionMap, "
                    f"shader_default={source.null_texture_defaults.get('_EmissionMap')!r}"
                )
            input_socket(tree, "Emission Tint", "NodeSocketColor", source_color(source, "_EmissionColor"), "Exact source _EmissionColor")
            input_socket(tree, "Emission Brightness", "NodeSocketFloat", source_float(source, "_EmissionBrightness"), "Exact source _EmissionBrightness; game exposure is not matched")
            emission = color_mix(tree, "MULTIPLY", 1.0, emission_map, entry.outputs["Emission Tint"], "Source emission texture times color", "Source Emission")
            tree.links.new(emission, bsdf.inputs["Emission Color"])
            tree.links.new(entry.outputs["Emission Brightness"], bsdf.inputs["Emission Strength"])
        alpha = math_node(tree, "MULTIPLY", base_alpha, entry.outputs["Base Tint Alpha"], "Source base alpha times tint alpha", "Source Alpha Rules")
        if source.shader == "HGRP/CharacterNPR" and "_UV2_COLOR" in source.keywords:
            alpha = math_node(tree, "MULTIPLY", alpha, uv2_alpha_mask(tree, source, samples),
                              "Source base alpha times UV1 alpha mask", "Source Alpha Rules")
        feature_alpha_test = False
        if (source.shader == "HGRP/CharacterNPR" and "_ALPHATEST_ON" in source.keywords
                and source_float(source, "_AlphaClip") != 1.0):
            enabled = source_float(source, "_EnableAlphaTest")
            if (source.alpha_test_keyword != "_ALPHATEST_ON" or enabled != 1.0
                    or source.shader_property_types.get("_AlphaClipThreshold") != 3):
                raise ValueError(f"Source alpha-test keyword requires its explicit enabled Shader toggle/threshold contract: material={source.identity}, enabled={enabled}")
            feature_alpha_test = True
            tree["source_alpha_test_feature"] = json.dumps({"keyword": source.alpha_test_keyword,
                "property": "_EnableAlphaTest", "value": enabled, "threshold": source_float(source, "_AlphaClipThreshold"),
                "legacy_alpha_clip": source_float(source, "_AlphaClip"), "native_shader_semantics_verified": False,
                "scope": "Explicit Shader Toggle attribute, active material keyword and enabled source value select the host BaseMap alpha threshold; current native shader program equivalence is unverified."}, sort_keys=True)
        if source.shader == "HGRP/CharacterNPR" and "_CHARACTER_FUR" in source.keywords:
            alpha = fur_shell_coverage(tree, source, samples)
        elif feature_alpha_test or source_float(source, "_AlphaClip") == 1.0:
            input_socket(tree, "Alpha Clip Threshold", "NodeSocketFloat", source_float(source, "_AlphaClipThreshold"), "Exact source alpha clip threshold")
            alpha = math_node(tree, "SUBTRACT", 1.0, math_node(tree, "LESS_THAN", alpha, entry.outputs["Alpha Clip Threshold"], "Discard alpha below source threshold", "Source Alpha Rules"), "Keep alpha at or above source threshold", "Source Alpha Rules")
        elif source_float(source, "_SurfaceType") == 0.0:
            alpha = math_node(tree, "MULTIPLY", entry.outputs["Base Tint Alpha"], 0.0, "Opaque source surface ignores texture alpha", "Source Alpha Rules")
            alpha = math_node(tree, "ADD", alpha, 1.0, "Opaque coverage = one", "Source Alpha Rules")
        elif source_float(source, "_SurfaceType") != 1.0:
            raise ValueError(f"Unsupported source surface type: {source.identity}")
        alpha = math_node(tree, "MULTIPLY", alpha, entry.outputs["Approx Pass Opacity"], "Editable surface coverage", "Source Alpha Rules")
        tree.links.new(alpha, output.inputs["Coverage"])
        lit_shader = apply_diffuse_response(tree, source.shader, source.floats, samples, color, bsdf, lighting, source.null_texture_defaults)
        from character_special_vfx import attach_special_vfx, source_from_material, MissingSpecialVfxTextureError
        try:
            special_source = source_from_material(source, samples)
        except MissingSpecialVfxTextureError as error:
            diagnostic = {"status": "skipped_missing_resource", "component": "character_special_vfx",
                          "material": source.name, "identity": source.identity, "reason": str(error),
                          "retained": "base_surface"}
            tree["skipped_optional_resource"] = json.dumps(diagnostic, sort_keys=True)
            print(json.dumps({"level": "WARNING", "event": "character_resource_skipped", **diagnostic}), flush=True)
            special_source = None
        if special_source is not None:
            special_uv = node(tree, "ShaderNodeUVMap", "Special VFX source UV0", "Approx Character Special VFX")
            special_uv.uv_map = "UV0"
            special_geometry = node(tree, "ShaderNodeNewGeometry", "Special VFX geometric frame", "Approx Character Special VFX")
            source_alpha = math_node(tree, "MULTIPLY", base_alpha, entry.outputs["Base Tint Alpha"], "Special VFX unmodified source alpha", "Approx Character Special VFX")
            premultiply = math_node(tree, "ADD", math_node(tree, "MULTIPLY", math_node(tree, "SUBTRACT", source_alpha, 1.0, "Special VFX alpha minus one", "Approx Character Special VFX"), source_float(source, "_AlphaPremultiply"), "Special VFX selected alpha premultiply", "Approx Character Special VFX"), 1.0, "Special VFX source alpha multiplier", "Approx Character Special VFX")
            special_normal = special_geometry.outputs["Normal"]
            if special_source.floats["_VFXFresnelUseNormalMap"] == 1.0:
                links = tuple(bsdf.inputs["Normal"].links)
                if len(links) != 1:
                    raise ValueError(f"Special VFX mapped Fresnel requires a connected BSDF normal: {source.identity}")
                special_normal = links[0].from_socket
            lit_shader = attach_special_vfx(tree, special_source, lit_shader, special_uv.outputs[0],
                special_normal, special_geometry.outputs["Incoming"], premultiply, bpy.context.scene)
        tree.links.new(lit_shader, output.inputs["Shader"])
        tree.links.new(color, output.inputs["Multiply Factor"])
        from character_fur_transmission import attach_fur_transmission
        attach_fur_transmission(tree, source)
    for sample in samples.values():
        if not sample.outputs["Color"].is_linked and not sample.outputs["Alpha"].is_linked:
            sample.label = sample.label + " | preserved, inactive"
            sample["endfield_layout_note"] = "Retained; formula unknown."
    layout(tree)
    return tree


def surface_stack(sources: tuple[MaterialSource, ...], groups: Mapping[Identity, bpy.types.ShaderNodeTree], layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.Material:
    overlay_count = sum(source.shader == "HGRP/CharacterNPR_OverlayShadow" for source in sources)
    if overlay_count and overlay_count != len(sources):
        raise ValueError("Mixed surface and framebuffer-multiply passes require an explicit receiver composition")
    key = hashlib.sha256(json.dumps([source.identity for source in sources]).encode()).hexdigest()[:12]
    material = bpy.data.materials.new("EFCHAR " + sources[0].name + (" + source passes " + key if len(sources) > 1 else ""))
    material.use_nodes = True
    material.surface_render_method = "DITHERED"
    material.use_transparency_overlap = False
    if sources[0].shader in SUPPORTED_SHADERS:
        tint_name = "_TintColor" if sources[0].shader in MESH_EFFECT_SHADERS else "_BaseColor"
        material.diffuse_color = source_color(sources[0], tint_name)
    vfx_sources = tuple(source for source in sources if source.shader in MESH_EFFECT_SHADERS)
    if vfx_sources:
        surface_types = {source_float(source, "_SurfaceType") for source in vfx_sources}
        if len(surface_types) != 1:
            raise ValueError(f"A Blender material stack cannot preserve conflicting VFX surface modes: identities={[source.identity for source in vfx_sources]}, surface_types={sorted(surface_types)}")
        surface_type = next(iter(surface_types))
        if surface_type not in (0.0, 1.0):
            raise ValueError(f"Unsupported VFX source surface type in character material stack: surface_type={surface_type}")
        material.surface_render_method = "BLENDED" if surface_type == 1.0 else "DITHERED"
        material.use_transparency_overlap = True
        render_states: list[dict[str, object]] = []
        for source in vfx_sources:
            group = groups[source.identity]
            for property_name in ("source_floats", "source_render_state"):
                if property_name not in group:
                    raise ValueError(f"VFX source metadata is missing from its character group: material={source.identity}, property={property_name}")
            render_states.append({
                "identity": source.identity,
                "floats": group["source_floats"],
                "render_state": group["source_render_state"],
            })
        material["source_vfx_render_state"] = json.dumps(render_states, sort_keys=True)
        depth_writers = tuple(source for source in vfx_sources
                              if source.shader == CHARACTER_VFX_SHADER and source_float(source, "_ZWrite") == 1.0)
        if surface_type == 1.0 and depth_writers:
            # A single host surface cannot reproduce separate native depth draws.
            material.use_transparency_overlap = False
            material["source_vfx_depth_approximation"] = json.dumps({
                "format": "EndfieldCharacterVfxDepth/1",
                "source_depth_writers": [source.identity for source in depth_writers],
                "source_zwrite": 1.0,
                "host_transparency_overlap": False,
                "native_depth_pass_verified": False,
                "scope": "Blender non-overlapping surface approximation for explicit CharacterNPR_VFX depth writers; separate native depth and inter-object ordering are not reproduced.",
            }, sort_keys=True)
    material["source_shader"] = sources[0].shader
    material["source_material_stack"] = json.dumps([source.identity for source in sources])
    material["fidelity"] = "Editable source-sampling approximation; game lighting, stencil and render queues are not reproduced"
    if any(source.shader not in SUPPORTED_SHADERS for source in sources):
        material["archived_unsupported_shader"] = True
        material["fidelity"] = "Inactive unsupported source shader archive; transparent carrier is not reconstructed shading"
    tree = material.node_tree
    tree.nodes.clear()
    output = node(tree, "ShaderNodeOutputMaterial", "Shared Eevee / Cycles output", "Engine Outputs")
    transparent = node(tree, "ShaderNodeBsdfTransparent", "Transparent outside source coverage", "Source Alpha Rules")
    previous: bpy.types.NodeSocket = transparent.outputs[0]
    overlay_factor: bpy.types.NodeSocket | None = None
    overlay_alpha: bpy.types.NodeSocket | None = None
    for index, source in enumerate(sources):
        group = node(tree, "ShaderNodeGroup", source.shader + " | source pass " + str(index), "Ordered Source Material Passes")
        group.node_tree = groups[source.identity]
        if index == 0:
            control = 1.0
        else:
            enabled = node(tree, "ShaderNodeValue", "Approx Extra Pass " + str(index) + " Enable (0 / 1)", "Extra Pass Controls - APPROXIMATION")
            enabled.outputs[0].default_value = 1.0
            enabled["evidence_note"] = "Native material order retained on last submesh; single-surface composition replaces repeated draws"
            control = enabled.outputs[0]
        coverage = math_node(tree, "MULTIPLY", group.outputs["Coverage"], control, "Source pass coverage times enable", "Ordered Source Material Passes")
        if index > 0 and "source_fx_layer" in group.node_tree:
            tree.links.new(previous, group.inputs["Previous Shader"])
            mix = node(tree, "ShaderNodeMixShader", "Optional transparent FX over retained source surface", "Ordered Source Material Passes")
            tree.links.new(control, mix.inputs[0])
            tree.links.new(previous, mix.inputs[1])
            tree.links.new(group.outputs["Layer Shader"], mix.inputs[2])
            previous = mix.outputs[0]
        elif source.shader == "HGRP/CharacterNPR_OverlayShadow":
            factor = color_mix(tree, "MIX", control, (1.0, 1.0, 1.0, 1.0), group.outputs["Multiply Factor"], "Optional source multiply overlay", "Overlay Composition - APPROXIMATION")
            overlay_factor = factor if overlay_factor is None else color_mix(tree, "MULTIPLY", 1.0, overlay_factor, factor, "Multiply ordered source overlay factors", "Overlay Composition - APPROXIMATION")
            overlay_alpha = coverage if overlay_alpha is None else math_node(tree, "SUBTRACT", 1.0, math_node(tree, "MULTIPLY", math_node(tree, "SUBTRACT", 1.0, overlay_alpha, "Remaining prior coverage", "Overlay Composition - APPROXIMATION"), math_node(tree, "SUBTRACT", 1.0, coverage, "Remaining next coverage", "Overlay Composition - APPROXIMATION"), "Combined uncovered fraction", "Overlay Composition - APPROXIMATION"), "Combined overlay coverage", "Overlay Composition - APPROXIMATION")
        else:
            mix = node(tree, "ShaderNodeMixShader", "Ordered source surface-over approximation", "Ordered Source Material Passes")
            tree.links.new(coverage, mix.inputs[0])
            tree.links.new(previous, mix.inputs[1])
            tree.links.new(group.outputs["Shader"], mix.inputs[2])
            previous = mix.outputs[0]
    if overlay_factor is not None and overlay_alpha is not None:
        shadow = node(tree, "ShaderNodeBsdfTransparent", "Source overlay multiplicative transmission", "Overlay Composition - APPROXIMATION")
        tree.links.new(overlay_factor, shadow.inputs["Color"])
        previous = shadow.outputs[0]
        material.surface_render_method = "BLENDED"
        material["overlay_scope"] = "Tinted transmission bridge for source multiply factors; native screen-space depth/stencil and receiver filtering remain unimplemented"
    tree.links.new(previous, output.inputs["Surface"])
    layout(tree)
    return material


def geometry_signature(objects: Sequence[bpy.types.Object]) -> dict[str, tuple[int, int, int, int]]:
    return {obj.name: (len(obj.data.vertices), len(obj.data.polygons), len(obj.vertex_groups), len(obj.data.shape_keys.key_blocks) if obj.data.shape_keys is not None else 0) for obj in objects}


def bind_sources(packet: SourcePacket, groups: Mapping[Identity, bpy.types.ShaderNodeTree], skipped_material_passes: Mapping[Identity, dict[str, object]], skipped_effect_renderers: Mapping[Identity, dict[str, object]], layout: Callable[[bpy.types.NodeTree], None]) -> tuple[list[dict[str, object]], tuple[bpy.types.Object, ...]]:
    sources = {source.identity: source for source in packet.materials}
    renderer_map = {identity(renderer["object"], "renderer.object"): renderer for renderer in packet.renderers}
    stacks: dict[tuple[Identity, ...], bpy.types.Material] = {}
    rows: list[dict[str, object]] = []
    bound: list[bpy.types.Object] = []
    for decision in packet.selection:
        renderer_key_text = string_value(decision["renderer_identity"], "renderer_identity")
        cab, path_id = renderer_key_text.rsplit(":", 1)
        renderer_key = (cab.casefold(), int(path_id))
        renderer = renderer_map[renderer_key]
        pipeline = packet.renderer_pipeline[renderer_key]
        name = string_value(decision["fbx_object_name"], "fbx_object_name")
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            raise ValueError(f"Audited renderer mesh not present in blend: {renderer_key}, {name}")
        if obj.hide_render != (decision["visible"] is False):
            raise ValueError(f"Audited source visibility changed: {name}")
        submesh_count = int_value(renderer["submesh_count"], "renderer.submesh_count")
        if submesh_count < 1 or len(obj.material_slots) != submesh_count:
            raise ValueError(f"FBX/source submesh slot count mismatch: {name}, FBX={len(obj.material_slots)}, source={submesh_count}")
        refs = tuple(identity(object_value(pointer, "material.pointer")["resolved"], "material.resolved") for pointer in array_value(renderer["materials"], "renderer.materials"))
        if len(refs) < submesh_count:
            raise ValueError(f"Source renderer has fewer materials than submeshes: {name}")
        obj["source_renderer_identity"] = renderer_key_text
        obj["source_renderer_pipeline_json"] = json.dumps(pipeline, sort_keys=True)
        text = bpy.data.texts.new("SOURCE RENDERER " + renderer_key_text + ".json")
        text.write(json.dumps(pipeline, sort_keys=True))
        pipeline_names = ("m_CastShadows", "m_ReceiveShadows", "m_RenderingLayerMask", "m_EnableCharacterOutline", "m_EnablePerRendererLighting", "m_PerRendererLightingOffset", "m_PerRendererLightingAnchor", "m_LightModeMask", "m_ProbeAnchor", "m_SortingOrder", "m_RendererSortingFudge")
        native_pipeline = {field: pipeline[field] for field in pipeline_names if field in pipeline}
        effect_skip = skipped_effect_renderers.get(renderer_key)
        if effect_skip is not None:
            if len(refs) != 1 or refs[0] not in sources:
                raise ValueError(f"Optional effect skip no longer identifies one source material: {renderer_key}")
            if tuple(effect_skip["material_identity"]) != refs[0]:
                raise ValueError(f"Optional effect skip material identity changed: {renderer_key}")
            obj.hide_render = True
            obj["skipped_optional_effect_renderer_json"] = json.dumps(effect_skip, sort_keys=True)
            rows.append({"renderer_identity": renderer_key_text, "object": name,
                         "source_materials": [list(ref) for ref in refs], "bound_material_stacks": [],
                         "submesh_count": submesh_count, "extra_pass_count": len(refs) - submesh_count,
                         "connected_extra_pass_count": 0, "skipped_optional_material_passes": [],
                         "skipped_optional_effect_renderer": effect_skip,
                         "visible": decision["visible"], "effective_visible": False,
                         "native_pipeline_fields": native_pipeline,
                         "unavailable_pipeline_fields": [field for field in pipeline_names if field not in pipeline],
                         "composition": "Unsupported dedicated optional VFX renderer hidden; source mesh and material evidence retained"})
            bound.append(obj)
            continue
        if not obj.data.uv_layers or obj.data.uv_layers[0].name != "UV0":
            raise ValueError(f"Research source binding requires audited UV0: {name}")
        bound_refs = tuple(ref for ref in refs if ref not in skipped_material_passes)
        if decision["visible"] is True and any("source_fur_shell_coverage" in groups[ref] for ref in bound_refs):
            layer = obj.data.uv_layers.get("UV1")
            if layer is None or len(layer.data) != len(obj.data.loops) or not layer.data:
                raise ValueError(f"Source Fur coverage requires imported TEXCOORD1 as UV1: {name}")
            if any(not math.isfinite(corner.uv.x) or not 0.0 <= corner.uv.x <= 1.0 for corner in layer.data):
                raise ValueError(f"Source Fur shell layer UV1.x is not finite within [0,1]: {name}")
        if any("source_uv2_alpha_mask" in groups[ref] for ref in bound_refs) and "UV1" not in obj.data.uv_layers:
            raise ValueError(f"Source UV2 alpha feature requires imported UV1: {name}")
        for ref in bound_refs:
            special_record = groups[ref].get("source_special_vfx")
            if special_record is None:
                continue
            main_uv = json.loads(special_record).get("host_inputs", {}).get("main_uv", {})
            if main_uv.get("route") == "source_uv1":
                layer = obj.data.uv_layers.get("UV1")
                if (layer is None or not layer.data or len(layer.data) != len(obj.data.loops)
                        or any(not math.isfinite(value) for corner in layer.data for value in corner.uv)):
                    raise ValueError(f"Special VFX main UV2 requires actual finite imported UV1: object={name}, material={ref}")
        bound_stacks: list[list[list[str | int]]] = []
        for index in range(submesh_count):
            source_slot_refs = refs[index:] if index == submesh_count - 1 else (refs[index],)
            slot_refs = tuple(ref for ref in source_slot_refs if ref not in skipped_material_passes)
            if not slot_refs:
                raise ValueError(f"Optional material-pass filtering removed a required source submesh: renderer={renderer_key}, submesh={index}")
            if slot_refs not in stacks:
                stacks[slot_refs] = surface_stack(tuple(sources[ref] for ref in slot_refs), groups, layout)
            obj.material_slots[index].link = "OBJECT"
            obj.material_slots[index].material = stacks[slot_refs]
            bound_stacks.append([list(ref) for ref in slot_refs])
        renderer_skips = [skipped_material_passes[ref] for ref in dict.fromkeys(refs) if ref in skipped_material_passes]
        composition = "Single-surface approximation preserving native material order; extra materials apply to final source submesh"
        if renderer_skips:
            composition += "; unsupported optional extra passes omitted while original source references and TypeTrees remain archived"
        rows.append({"renderer_identity": renderer_key_text, "object": name,
                     "source_materials": [list(ref) for ref in refs], "bound_material_stacks": bound_stacks,
                     "submesh_count": submesh_count, "extra_pass_count": len(refs) - submesh_count,
                     "connected_extra_pass_count": max(0, len(bound_stacks[-1]) - 1),
                     "skipped_optional_material_passes": renderer_skips,
                     "visible": decision["visible"], "native_pipeline_fields": native_pipeline,
                     "unavailable_pipeline_fields": [name for name in pipeline_names if name not in pipeline],
                     "composition": composition})
        bound.append(obj)
    return rows, tuple(bound)


def configure_render_view(engine: str, direction: Vector, center: Vector, extent: float, width: int, height: int, samples: int) -> None:
    """Set the actual preview camera and engine controls without invoking rendering."""
    scene = bpy.context.scene
    scene.render.engine = engine
    scene.render.resolution_x, scene.render.resolution_y = width, height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = False
    scene.camera.location = center + direction.normalized() * extent * 4.0
    scene.camera.rotation_euler = (center - scene.camera.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera.data.ortho_scale = extent * 1.18 * max(1.0, height / width)
    if engine == "CYCLES":
        scene.cycles.device = "CPU"
        scene.cycles.samples = samples
        scene.cycles.use_denoising = True
    elif engine == "BLENDER_EEVEE_NEXT":
        scene.eevee.taa_render_samples = samples
    else:
        raise ValueError(f"Unsupported verification engine: {engine}")


def render_view(output: Path, name: str, engine: str, direction: Vector, center: Vector, extent: float, width: int, height: int, samples: int) -> dict[str, object]:
    configure_render_view(engine, direction, center, extent, width, height, samples)
    bpy.context.scene.render.filepath = extended_path(output / name)
    bpy.ops.render.render(write_still=True)
    image = output / name
    if not io_path(image).is_file() or io_path(image).stat().st_size == 0:
        raise ValueError(f"Render produced no image: {image}")
    return {"path": str(image), "engine": engine, "width": width, "height": height, "samples": samples, "bytes": io_path(image).stat().st_size, "sha256": file_hash(image)}


def render_material_comparisons(output: Path, center: Vector, extent: float, width: int, height: int, samples: int, environment: bpy.types.ShaderNodeTree, lighting: bpy.types.ShaderNodeTree, settings: ArtistLighting) -> list[dict[str, object]]:
    renders = [render_view(output, "front_eevee.png", "BLENDER_EEVEE_NEXT", Vector((0.0, -1.0, 0.05)), center, extent, width, height, samples), render_view(output, "side_eevee.png", "BLENDER_EEVEE_NEXT", Vector((1.0, -0.05, 0.05)), center, extent, width, height, samples), render_view(output, "front_cycles_smoke.png", "CYCLES", Vector((0.0, -1.0, 0.05)), center, extent, 192, 256, min(samples, 16))]
    controls = {item.label: item.outputs[0] for item in lighting.nodes if item.type == "VALUE"}
    controls["Response Weight"].default_value = 0.0
    renders.append(render_view(output, "front_npr_disabled_eevee.png", "BLENDER_EEVEE_NEXT", Vector((0.0, -1.0, 0.05)), center, extent, width, height, samples))
    controls["Response Weight"].default_value = settings.response_weight
    controls["Light Direction X"].default_value = -settings.direction[0]
    renders.append(render_view(output, "front_light_reversed_eevee.png", "BLENDER_EEVEE_NEXT", Vector((0.0, -1.0, 0.05)), center, extent, width, height, samples))
    controls["Light Direction X"].default_value = settings.direction[0]
    controls["Shadow LUT Weight"].default_value = 0.0
    renders.append(render_view(output, "front_lut_disabled_eevee.png", "BLENDER_EEVEE_NEXT", Vector((0.0, -1.0, 0.05)), center, extent, width, height, samples))
    controls["Shadow LUT Weight"].default_value = 1.0
    controls["Direct Intensity"].default_value = settings.direct_intensity * 0.5
    renders.append(render_view(output, "front_half_direct_eevee.png", "BLENDER_EEVEE_NEXT", Vector((0.0, -1.0, 0.05)), center, extent, width, height, samples))
    controls["Direct Intensity"].default_value = settings.direct_intensity
    wet_control = next(node for node in environment.nodes if node.type == "VALUE" and node.label == "Wetness (editable)")
    wet_control.outputs[0].default_value = 1.0
    renders.append(render_view(output, "front_wet_approx_eevee.png", "BLENDER_EEVEE_NEXT", Vector((0.0, -1.0, 0.05)), center, extent, width, height, samples))
    wet_control.outputs[0].default_value = 0.0
    renders.append(render_view(output, "front_final_eevee.png", "BLENDER_EEVEE_NEXT", Vector((0.0, -1.0, 0.05)), center, extent, width, height, samples))
    return renders


@dataclass(frozen=True)
class LoadedMaterialBuild:
    environment: bpy.types.ShaderNodeTree
    lighting: bpy.types.ShaderNodeTree
    image_cache: Mapping[tuple[Identity, str, str], bpy.types.Image]
    groups: Mapping[Identity, bpy.types.ShaderNodeTree]
    skipped_optional_material_passes: tuple[dict[str, object], ...]
    skipped_optional_effect_renderers: tuple[dict[str, object], ...]
    renderer_rows: tuple[dict[str, object], ...]
    bound_objects: tuple[bpy.types.Object, ...]
    center: Vector
    extent: float


def build_loaded_materials(packet: SourcePacket, lighting_settings: ArtistLighting, layout_shader_node_tree: Callable[[bpy.types.NodeTree], None], skipped_effect_materials: Mapping[Identity, dict[str, object]], skipped_effect_renderers: Mapping[Identity, dict[str, object]]) -> LoadedMaterialBuild:
    """Load, pack and bind source materials without rendering or saving a blend."""
    environment = make_environment(layout_shader_node_tree)
    lighting = make_artist_lighting(lighting_settings, layout_shader_node_tree)
    image_cache = load_images(packet)
    from character_mesh_effect import collect_character_mesh_color_sources
    color_sources = collect_character_mesh_color_sources(packet)
    optional_extra_identities = optional_extra_material_identities(packet)
    if set(skipped_effect_materials).intersection(optional_extra_identities):
        raise ValueError("A standalone optional VFX material cannot also be an optional extra pass")
    for diagnostic in skipped_effect_materials.values():
        print(json.dumps({"level": "WARNING", "event": "character_optional_effect_renderer_skipped",
                          **diagnostic}, sort_keys=True), flush=True)
    groups: dict[Identity, bpy.types.ShaderNodeTree] = {}
    skipped_optional_material_passes: dict[Identity, dict[str, object]] = {}
    for source in packet.materials:
        if source.identity in skipped_effect_materials:
            continue
        try:
            groups[source.identity] = material_group(source, packet, environment, image_cache, lighting,
                layout_shader_node_tree, color_sources.get(source.identity))
        except NotImplementedError as error:
            if source.identity not in optional_extra_identities:
                raise
            diagnostic = {
                "status": "skipped_unsupported_optional_material_pass",
                "component": "optional_source_material_pass",
                "material": source.name,
                "shader": source.shader,
                "identity": list(source.identity),
                "reason": str(error),
                "retained": "base_and_supported_material_passes",
                "source_material_datablock": "SOURCE MATERIAL " + source.name + ".json",
                "source_typetree_datablock": "SOURCE TYPETREE " + source.name + ".json",
            }
            skipped_optional_material_passes[source.identity] = diagnostic
            print(json.dumps({"level": "WARNING", "event": "character_optional_material_pass_skipped",
                              **diagnostic}, sort_keys=True), flush=True)
    for source in packet.materials:
        text = bpy.data.texts.new("SOURCE MATERIAL " + source.name + ".json")
        text.write(source.source_json)
        tree = bpy.data.texts.new("SOURCE TYPETREE " + source.name + ".json")
        tree.write(source.tree_json)
        if source.identity in skipped_optional_material_passes or source.identity in skipped_effect_materials:
            continue
        material = surface_stack((source,), groups, layout_shader_node_tree)
        material.use_fake_user = True
    mesh_objects = tuple(obj for obj in bpy.data.objects if obj.type == "MESH" and not obj.name.startswith("PREVIEW_ONLY_"))
    before = geometry_signature(mesh_objects)
    renderer_rows, bound_objects = bind_sources(packet, groups, skipped_optional_material_passes,
                                                skipped_effect_renderers, layout_shader_node_tree)
    if geometry_signature(mesh_objects) != before:
        raise ValueError("Material rebuild changed source mesh, shape keys, or vertex-group counts")
    visible = tuple(obj for obj in bound_objects if not obj.hide_render)
    if not visible:
        raise ValueError("No audited source renderer is visible")
    minimum = Vector((min((obj.matrix_world @ Vector(corner))[axis] for obj in visible for corner in obj.bound_box) for axis in range(3)))
    maximum = Vector((max((obj.matrix_world @ Vector(corner))[axis] for obj in visible for corner in obj.bound_box) for axis in range(3)))
    center, extent = (minimum + maximum) * 0.5, max(maximum - minimum)
    scene = bpy.context.scene
    scene.world.use_nodes = True
    scene.world.node_tree.nodes.get("Background").inputs["Color"].default_value = (0.045, 0.045, 0.045, 1.0)
    scene.world.node_tree.nodes.get("Background").inputs["Strength"].default_value = 0.35
    scene.view_settings.view_transform = "AgX"
    ground = bpy.data.objects.get("PREVIEW_ONLY_Ground")
    if ground is not None:
        ground.hide_render = True
    for light in (obj for obj in bpy.data.objects if obj.type == "LIGHT"):
        light.data.use_shadow = True
    return LoadedMaterialBuild(environment, lighting, image_cache, MappingProxyType(groups),
        tuple(skipped_optional_material_passes.values()), tuple(skipped_effect_materials.values()),
        tuple(renderer_rows), bound_objects, center, extent)


def build_material_sample(argv: Sequence[str], render_samples: Callable[[Path, Vector, float, int, int, int, bpy.types.ShaderNodeTree, bpy.types.ShaderNodeTree, ArtistLighting], list[dict[str, object]]]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("blend", "source-dir", "shader-identities", "preview-report", "renderer-visibility", "ruri-glsl", "scene-source-dir", "output-dir"):
        parser.add_argument("--" + name, required=True, type=Path)
    for name in ("width", "height", "samples"):
        parser.add_argument("--" + name, required=True, type=int)
    parser.add_argument("--npr-light-direction", required=True, type=float, nargs=3)
    for name in ("npr-direct-intensity", "npr-ambient-intensity", "npr-ramp-bias", "npr-wrap", "npr-weight"):
        parser.add_argument("--" + name, required=True, type=float)
    args = parser.parse_args(list(argv))
    if min(args.width, args.height, args.samples) < 1:
        raise ValueError("Render dimensions and samples must be positive")
    output = args.output_dir.resolve()
    if io_path(output).exists():
        raise FileExistsError(f"Research output already exists: {output}")
    if not io_path(args.scene_source_dir / "blender_scene_materials.py").is_file():
        raise FileNotFoundError("Required production node layout helper is absent")
    if not io_path(args.ruri_glsl).is_file():
        raise FileNotFoundError(f"Required pinned shader reference is absent: {args.ruri_glsl}")
    sys.path.insert(0, str(args.scene_source_dir.resolve()))
    from blender_scene_materials import layout_shader_node_tree
    packet = read_packet(args.source_dir, args.shader_identities, args.preview_report, args.renderer_visibility)
    bpy.ops.wm.open_mainfile(filepath=extended_path(args.blend.resolve()))
    io_path(output).mkdir(parents=True)
    lighting_settings = ArtistLighting(tuple(args.npr_light_direction), args.npr_direct_intensity, args.npr_ambient_intensity, args.npr_ramp_bias, args.npr_wrap, args.npr_weight)
    built = build_loaded_materials(packet, lighting_settings, layout_shader_node_tree, {}, {})
    environment, lighting, image_cache = built.environment, built.lighting, built.image_cache
    renderer_rows, center, extent = built.renderer_rows, built.center, built.extent
    visible = tuple(obj for obj in built.bound_objects if not obj.hide_render)
    renders = render_samples(output, center, extent, args.width, args.height, args.samples, environment, lighting, lighting_settings)
    blend = output / "character_materials.blend"
    bpy.ops.wm.save_as_mainfile(filepath=extended_path(blend))
    expected_images = {texture.identity for source in packet.materials for texture in source.textures}
    actual_images = {identity for identity, _colorspace, _alpha_mode in image_cache}
    if actual_images != expected_images or any(image.packed_file is None for image in image_cache.values()):
        raise ValueError("Referenced source image packing is incomplete")
    report = {"format": "EndfieldCharacterMaterialRebuild/1", "status": "approximation_smoke_passed", "game_material_fidelity_complete": False, "source_blend": str(args.blend.resolve()), "source_blend_sha256": file_hash(args.blend), "input_hashes": dict(packet.input_hashes), "script_sha256": file_hash(Path(__file__)), "layout_helper_sha256": file_hash(args.scene_source_dir / "blender_scene_materials.py"), "reference_glsl": str(args.ruri_glsl.resolve()), "reference_glsl_sha256": file_hash(args.ruri_glsl), "source_materials": len(packet.materials), "actual_shader_families": sorted({source.shader for source in packet.materials}), "verified_texture_identities": len(actual_images), "packed_image_roles": len(image_cache), "source_geometry_counts_preserved": True, "visible_renderer_count": len(visible), "renderers": renderer_rows, "extra_material_passes_connected": sum(cast(int, row["extra_pass_count"]) for row in renderer_rows), "source_parameters": "All original float/color/texture fields and complete material and renderer TypeTrees retained in blend Text datablocks; active basic parameters exposed on source material groups", "pipeline_support": {"integrated": ["Exact five source Shader identities and CAB/PathID material/texture bindings", "Base tint, UV0/ST sampling, RG/R*A-G packed normal channels, metallic/gloss/spec/AO and keyword-gated emission", "Source opaque/transparent alpha distinction and source alpha-clip threshold", "Ordered extra source material references retained as adjustable Blender composition", "Native LOD0/active/enabled/shadows-only visibility inherited from the audited preview", "Connected source 32-slice skin shadow LUT sampling and source-enabled diffuse ramp RGB/alpha with local chroma normalization; shared artist lighting composition"], "preserved_not_integrated": ["SpecRampMap, native hair strand lobes and skin SDF lighting", "Character light loop types, main/additional-light parameters and game exposure", "Native rendering-layer masks including 258; no equivalent HGRP light filtering implemented", "Per-renderer lighting enable, offset, anchor, light-mode mask and probe anchor", "Stencil/depth, render queues, fog and screen-space overlay multiply/culling", "Global wet/rain binding and response; controls remain artist approximations", "Global snow consumption formula is unknown; no fabricated snow nodes"]}, "control_entry_points": {"source_parameters": "Select mesh material; top-level EF Character <source material name> group inputs; Tab into group for verified texture channels", "artist_lighting": lighting.name + " internal Value nodes (Light Direction X/Y/Z, Direct Intensity, Ambient Intensity, Ramp Bias, Wrap, Response Weight, Shadow LUT Weight)", "wetness": environment.name + " internal Value nodes (Wetness, Rain Wetness, Water Height Meters, Submersion Strength, Wet Darkening, Wet Roughness)", "extra_passes": "Composite EFCHAR material top-level Approx Extra Pass 1 Enable (0 / 1) Value node", "complete_source_parameters": "Blend Text datablocks SOURCE MATERIAL / SOURCE TYPETREE / SOURCE RENDERER"}, "renders": renders, "output_blend": str(blend), "output_blend_bytes": io_path(blend).stat().st_size, "output_blend_sha256": file_hash(blend), "limitations": ["Source ramp/LUT sampling and local chroma arithmetic are connected; their diffuse composition and shared artist light are approximate, with unknown game global uniforms, screen shadows, SDF, matcap/parallax, light loops and outlines unimplemented", "Hair uses source split-normal RG for diffuse normal; source BA specular normal and exact strand lobes are preserved but not reconstructed", "Additional native material passes use adjustable single-surface composition on the final submesh; stencil, depth, fog and render queue semantics are not replicated", "OverlayShadow uses original red/alpha mask and source tint with transparency; framebuffer multiply is approximated by an unlit translucent surface", "Wetness controls are explicitly approximate artist inputs; original wet/rain fields are preserved without an unverified binding", "Snow has no recovered consumption formula and no artificial snow control or node", "Standard source Toggle(_ALPHATEST_ON), active keyword and explicit _EnableAlphaTest=1 select host BaseMap alpha thresholding; legacy _AlphaClip remains supported. Current native shader program equivalence is unverified", "General source textures require UV0; Fur shell coverage reads source UV1.x as the layer, and explicit _UV2_COLOR uses the source UV1 Alpha/Root/Depth/ID red mask with its original ST before alpha thresholding. Missing required inputs or unsupported UV indices fail explicitly"]}
    report["source_extra_material_passes"] = sum(cast(int, row["extra_pass_count"]) for row in renderer_rows)
    report["extra_material_passes_connected"] = sum(cast(int, row["connected_extra_pass_count"]) for row in renderer_rows)
    report["source_material_identities"] = [list(source.identity) for source in packet.materials]
    support = cast(dict[str, object], report["pipeline_support"])
    cast(list[str], support["integrated"])[3] = "Supported ordered extra source material references are adjustable; unsupported optional passes remain archived in source metadata"
    cast(list[str], report["limitations"]).append("Unsupported optional material passes are omitted from the bound stack and listed separately in the build receipt")
    report["material_support"] = {"visible_supported": [{"identity": list(source.identity), "shader": source.shader} for source in packet.materials if source.identity in packet.visible_materials and source.identity in built.groups], "inactive_unsupported": [{"identity": list(source.identity), "shader": source.shader, "status": "archived_unsupported_shader"} for source in packet.materials if source.shader not in SUPPORTED_SHADERS]}
    report["skipped_optional_material_passes"] = list(built.skipped_optional_material_passes)
    report["research_modules"] = {name: file_hash(Path(__file__).with_name(name)) for name in ("character_npr_nodes.py", "character_node_graph.py")}
    report["artist_lighting"] = {"direction": list(lighting_settings.direction), "direct_intensity": lighting_settings.direct_intensity, "ambient_intensity": lighting_settings.ambient_intensity, "ramp_bias": lighting_settings.ramp_bias, "wrap": lighting_settings.wrap, "response_weight": lighting_settings.response_weight, "shadow_lut_weight": 1.0}
    report["npr_evidence"] = {"shadow_lut_lines": [1385, 1397], "diffuse_ramp_lines": [1985, 2000], "skin_ramp_lines": [1684, 1705], "local_chroma_normalization_lines": [1460, 1492], "transfer_reference": "https://raw.githubusercontent.com/Unity-Technologies/Graphics/2021.3/staging/Packages/com.unity.render-pipelines.core/ShaderLibrary/Color.hlsl", "transfer_boundary": "Unity 2021.3 reference piecewise transfer; HGRP/Substance helper binary not recovered", "binding_boundary": "Source flags choose native LUT/ramp branches. Geometric surface N, artist bias/wrap/direct/ambient replace unbound SDF/global/light-loop state. Source AO and approximate wetness feed the preview albedo."}
    io_path(output / "material_rebuild_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({"status": report["status"], "blend": str(blend), "materials": len(packet.materials), "textures": len(actual_images), "extra_passes": report["extra_material_passes_connected"]}))


def main(argv: Sequence[str]) -> None:
    build_material_sample(argv, render_material_comparisons)


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1:])
