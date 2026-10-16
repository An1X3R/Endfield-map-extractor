"""Validate SceneProbe material records and plan reproducible surface previews."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from endfield_scene_io import (SceneSourceCacheSlot, cached_scene_source_value,
                               read_scene_manifest, resolve_scene_read_path, resolve_scene_existing_files)
from typing import NotRequired, TypedDict, cast
from endfield_shader_contract import emission_source, surface_family, uses_mro, uses_normal_map, has_verified_lit_top_layer


_MATERIAL_CATALOG_CACHE = SceneSourceCacheSlot(
    "material-catalog", tuple, 4, 512 * 1024 * 1024
)


def is_neutral_material_name(name: str) -> bool:
    """Recognize explicit placeholder materials emitted by both scene chains."""
    return name.startswith(("Geometry_Only_Neutral", "REPAIR_NEUTRAL", "Unbound_Model_Material_Neutral"))


class TextureSampler(TypedDict):
    FilterMode: int
    WrapU: int
    WrapV: int
    WrapW: int
    Aniso: int
    MipBias: float


class ShaderTextureDefault(TypedDict):
    Property: str
    Name: str
    Dimension: int


class AssetReference(TypedDict):
    Source: str
    PathId: int
    Name: NotRequired[str]
    PassNames: NotRequired[list[str]]
    ColorSpace: NotRequired[int]
    TextureSampler: NotRequired[TextureSampler]
    TextureDefaults: NotRequired[list[ShaderTextureDefault]]


class TextureSlot(TypedDict):
    Property: str
    Texture: AssetReference | None
    Scale: list[float]
    Offset: list[float]


class MaterialRenderState(TypedDict):
    DisabledShaderPasses: list[str]
    ValidKeywords: list[str]
    CustomRenderQueue: int


class MaterialRecord(TypedDict):
    Name: str
    Source: str
    PathId: int
    Shader: AssetReference
    Textures: list[TextureSlot]
    Floats: dict[str, float]
    Colors: dict[str, list[float]]
    RenderState: NotRequired[MaterialRenderState]


def validate_shader_texture_defaults(value: object, context: str) -> list[ShaderTextureDefault]:
    """Retain explicit source defaults without inventing omitted texture properties."""
    if not isinstance(value, list):
        raise ValueError(f"Shader texture defaults must be an array: field={context}")
    result: list[ShaderTextureDefault] = []
    names: set[str] = set()
    for item in value:
        row = require_object(item, context)
        prop, name, dimension = row.get("Property"), row.get("Name"), row.get("Dimension")
        if not isinstance(prop, str) or not prop or prop in names:
            raise ValueError(f"Invalid or duplicate shader texture default: field={context}, property={prop}")
        if not isinstance(name, str) or type(dimension) is not int:
            raise ValueError(f"Shader texture default requires Name/string and Dimension/int: field={context}, property={prop}")
        names.add(prop)
        result.append({"Property": prop, "Name": name, "Dimension": dimension})
    return result


def shader_constant_texture_default(record: MaterialRecord, property_name: str) -> tuple[float, float, float, float] | None:
    """Decode white and the consumed height red; grey alpha is not established."""
    defaults = record["Shader"].get("TextureDefaults")
    if defaults is None:
        return None
    for row in validate_shader_texture_defaults(defaults, f"material={record['Name']}"):
        if row["Property"] == property_name and row["Dimension"] == 2:
            if row["Name"] == "white":
                return (1.0, 1.0, 1.0, 1.0)
            if property_name == "_BaseHeightMap" and row["Name"] in ("grey", "gray"):
                # ShaderLab grey is sRGB; linearGrey is a distinct built-in.
                linear_grey = ((0.5 + 0.055) / 1.055) ** 2.4
                # Only height red is consumed; the other components are host placeholders.
                return (linear_grey, linear_grey, linear_grey, 1.0)
    return None


def has_shader_white_texture_default(record: MaterialRecord, property_name: str) -> bool:
    """Recognize only an explicitly declared two-dimensional white source texture."""
    return shader_constant_texture_default(record, property_name) == (1.0, 1.0, 1.0, 1.0)


def mesh_material_requires_uv1(material: MaterialRecord) -> bool:
    """Report source UV1 use by the main and enabled VFXBaseV2 sample textures."""
    floats, colors = material["Floats"], material["Colors"]
    return any(
        isinstance(weights := colors.get(f"{name}UVWeights"), list) and
        len(weights) == 4 and weights[1] != 0
        for name in ("_MainTex", *(f"_SampleTex{index}" for index in range(5)
                                   if floats.get(f"_UseSampleTex{index}", 0) != 0))
    )


def resolve_export_path(path: Path) -> Path:
    """Use Windows extended paths consistently across Python and embedded Blender."""
    return _extended_export_path(resolve_scene_read_path(path))


def _extended_export_path(resolved: Path) -> Path:
    value = str(resolved)
    if os.name != "nt" or len(value) < 260 or value.startswith("\\\\?\\"):
        return resolved
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def resolve_existing_export_files(paths: tuple[Path, ...]) -> dict[str, Path]:
    """Batch existing payload resolution without retaining directory snapshots."""
    return {path: _extended_export_path(resolved)
            for path, resolved in resolve_scene_existing_files(paths).items()}


def require_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError(f"Expected object: {context}")
    return cast(dict[str, object], value)


def reference_key(value: object) -> tuple[str, int]:
    row = require_object(value, "asset reference")
    if not isinstance(row.get("Source"), str) or not row["Source"]:
        raise ValueError("Asset reference requires a nonempty Source")
    if type(row.get("PathId")) is not int:
        raise ValueError("Asset reference requires an integer PathId")
    return cast(str, row["Source"]).casefold(), cast(int, row["PathId"])


def number_vector(value: object, size: int, context: str) -> None:
    if not isinstance(value, list) or len(value) != size or any(
        type(v) not in (int, float) or not math.isfinite(v) for v in value
    ):
        raise ValueError(f"Expected {size} finite numbers: {context}")


def validate_texture_sampler(value: object, context: str) -> TextureSampler:
    """Read serialized sampler state without inventing absent axis settings."""
    row = require_object(value, context)
    for field, allowed in (("FilterMode", range(3)), ("WrapU", range(4)),
                           ("WrapV", range(4)), ("WrapW", range(4)), ("Aniso", range(17))):
        if type(row.get(field)) is not int or row[field] not in allowed:
            raise ValueError(f"Invalid texture sampler field: context={context}, field={field}, value={row.get(field)!r}")
    bias = row.get("MipBias")
    if type(bias) not in (int, float) or not math.isfinite(bias):
        raise ValueError(f"Invalid texture sampler mip bias: context={context}, value={bias!r}")
    return cast(TextureSampler, {**{field: row[field] for field in
                                 ("FilterMode", "WrapU", "WrapV", "WrapW", "Aniso")},
                                "MipBias": float(bias)})


def validate_material(value: object) -> MaterialRecord:
    row = require_object(value, "material")
    reference_key(row)
    reference_key(row.get("Shader"))
    shader = require_object(row["Shader"], "material Shader")
    if "TextureDefaults" in shader:
        validate_shader_texture_defaults(shader["TextureDefaults"], "material Shader.TextureDefaults")
    if "PassNames" in shader and (not isinstance(shader["PassNames"], list) or
            not all(isinstance(name, str) for name in shader["PassNames"])):
        raise ValueError("Shader PassNames must be a string array")
    if "RenderState" in row:
        state = require_object(row["RenderState"], "material RenderState")
        for field in ("DisabledShaderPasses", "ValidKeywords"):
            if not isinstance(state.get(field), list) or not all(isinstance(name, str) for name in state[field]):
                raise ValueError(f"Material RenderState requires string array: field={field}")
        if type(state.get("CustomRenderQueue")) is not int:
            raise ValueError("Material RenderState requires integer CustomRenderQueue")
    if not isinstance(row.get("Name"), str) or not row["Name"]:
        raise ValueError("Material requires a nonempty Name")
    floats = require_object(row.get("Floats"), "material Floats")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in floats.values()):
        raise ValueError(f"Material floats must be finite: {row['Name']}")
    for name, color in require_object(row.get("Colors"), "material Colors").items():
        number_vector(color, 4, name)
    slots = row.get("Textures")
    if not isinstance(slots, list):
        raise ValueError("Material requires Textures array")
    names: set[str] = set()
    for value in slots:
        slot = require_object(value, "texture slot")
        name = slot.get("Property")
        if not isinstance(name, str) or name in names:
            raise ValueError(f"Invalid or duplicate texture property: {name}")
        names.add(name)
        if "Texture" not in slot:
            raise ValueError(f"Texture reference field missing: {name}")
        if slot["Texture"] is not None:
            reference_key(slot["Texture"])
            if "TextureSampler" in slot["Texture"]:
                validate_texture_sampler(slot["Texture"]["TextureSampler"], f"material={row['Name']}, property={name}")
            if "ColorSpace" in slot["Texture"] and (
                type(slot["Texture"]["ColorSpace"]) is not int or slot["Texture"]["ColorSpace"] not in (0, 1)
            ):
                raise ValueError(f"Material texture ColorSpace must be 0 or 1: material={row['Name']}, property={name}, value={slot['Texture']['ColorSpace']}")
        number_vector(slot.get("Scale"), 2, name)
        number_vector(slot.get("Offset"), 2, name)
    normalized = cast(MaterialRecord, row)
    return {**normalized, "Textures": [
        {**slot, "Texture": None} if slot["Texture"] is not None and slot["Texture"]["PathId"] == 0 else slot
        for slot in normalized["Textures"]
    ]}


def merge_material_evidence(left: MaterialRecord, right: MaterialRecord) -> MaterialRecord:
    """Enrich identical source records without treating absent metadata as disagreement."""
    def signature(record: MaterialRecord) -> tuple[object, ...]:
        return (reference_key(record), record["Name"], reference_key(record["Shader"]),
                record["Floats"], record["Colors"], sorted(
                    (slot["Property"], reference_key(slot["Texture"]) if slot["Texture"] else None,
                     slot["Scale"], slot["Offset"]) for slot in record["Textures"]))

    if signature(left) != signature(right):
        raise ValueError(f"Conflicting material records: reference={reference_key(right)}")
    for field in ("Name", "PassNames", "TextureDefaults"):
        if field in left["Shader"] and field in right["Shader"] and left["Shader"][field] != right["Shader"][field]:
            raise ValueError(f"Conflicting shader metadata: reference={reference_key(right)}, field={field}")
    if "RenderState" in left and "RenderState" in right and left["RenderState"] != right["RenderState"]:
        raise ValueError(f"Conflicting material render state: reference={reference_key(right)}")
    result: MaterialRecord = {**left, "Shader": {**left["Shader"], **right["Shader"]}}
    if "RenderState" in right:
        result = {**result, "RenderState": right["RenderState"]}
    return result


def load_catalog(paths: tuple[Path, ...]) -> tuple[
    dict[tuple[str, int], MaterialRecord], dict[tuple[str, int], tuple[Path, str]]
]:
    if not paths:
        return {}, {}
    resolved = tuple(resolve_export_path(path) for path in paths)
    materials, texture_candidates = cached_scene_source_value(
        _MATERIAL_CATALOG_CACHE, resolved, (),
        lambda: _build_catalog(resolved),
    )
    textures: dict[tuple[str, int], tuple[Path, str]] = {}
    pending = texture_candidates
    candidate_index = 0
    while pending:
        paths = {key: candidates[candidate_index][0] / candidates[candidate_index][1]
                 for key, candidates in pending.items()}
        existing = resolve_existing_export_files(tuple(paths.values()))
        for key, path in paths.items():
            candidate = existing.get(str(path))
            if candidate is not None:
                textures[key] = (candidate.parent, candidate.name)
        candidate_index += 1
        pending = {key: candidates for key, candidates in pending.items()
                   if key not in textures and candidate_index < len(candidates)}
    return materials, textures


def _build_catalog(paths: tuple[Path, ...]) -> tuple[
    dict[tuple[str, int], MaterialRecord],
    dict[tuple[str, int], list[tuple[Path, str]]],
]:
    materials: dict[tuple[str, int], MaterialRecord] = {}
    texture_candidates: dict[tuple[str, int], list[tuple[Path, str]]] = {}
    texture_color_spaces: dict[tuple[str, int], int] = {}
    texture_samplers: dict[tuple[str, int], TextureSampler] = {}
    for path in paths:
        path = resolve_export_path(path)
        data = require_object(read_scene_manifest(path), str(path))
        if data.get("Format") != "EndfieldSceneProbe/1":
            raise ValueError(f"Unsupported SceneProbe format: {path}")
        for field in ("Materials", "TextureExports"):
            if not isinstance(data.get(field), list):
                raise ValueError(f"SceneProbe requires {field}: {path}")
        for value in cast(list[object], data["Materials"]):
            record = validate_material(value)
            key = reference_key(record)
            materials[key] = merge_material_evidence(materials[key], record) if key in materials else record
        for value in cast(list[object], data["TextureExports"]):
            row = require_object(value, "texture export")
            asset = require_object(row.get("Asset"), "texture export Asset")
            key = reference_key(asset)
            if "TextureSampler" in asset:
                sampler = validate_texture_sampler(asset["TextureSampler"], f"texture export={key}")
                if key in texture_samplers and texture_samplers[key] != sampler:
                    raise ValueError(f"Conflicting texture sampler: reference={key}, values={texture_samplers[key]}/{sampler}")
                texture_samplers[key] = sampler
            color_space = asset.get("ColorSpace")
            if color_space is not None:
                if type(color_space) is not int or color_space not in (0, 1):
                    raise ValueError(f"Texture export ColorSpace must be 0 or 1: reference={key}, value={color_space}")
                if key in texture_color_spaces and texture_color_spaces[key] != color_space:
                    raise ValueError(f"Conflicting texture ColorSpace: reference={key}, values={texture_color_spaces[key]}/{color_space}")
                texture_color_spaces[key] = color_space
            file = row.get("File")
            if file is None:
                continue
            if not isinstance(file, str) or not file:
                raise ValueError(f"Invalid texture filename: reference={key}, manifest={path}")
            # Existing exported manifests may intentionally reference shared absolute caches.
            candidate = path.parent / file.replace("\\", "/")
            texture_candidates.setdefault(key, []).append((candidate.parent, candidate.name))
    shader_defaults: dict[tuple[str, int], list[ShaderTextureDefault]] = {}
    for record in materials.values():
        defaults = record["Shader"].get("TextureDefaults")
        if defaults is None:
            continue
        shader_key = reference_key(record["Shader"])
        if shader_key in shader_defaults and shader_defaults[shader_key] != defaults:
            raise ValueError(f"Conflicting serialized shader texture defaults: shader={shader_key}")
        shader_defaults[shader_key] = defaults
    enriched: dict[tuple[str, int], MaterialRecord] = {}
    for key, record in materials.items():
        shader_key = reference_key(record["Shader"])
        if "TextureDefaults" not in record["Shader"] and shader_key in shader_defaults:
            record = {**record, "Shader": {**record["Shader"], "TextureDefaults": shader_defaults[shader_key]}}
        slots: list[TextureSlot] = []
        for slot in record["Textures"]:
            ref = slot["Texture"]
            if ref is None:
                slots.append({**slot})
                continue
            ref_key = reference_key(ref)
            color_space = texture_color_spaces.get(ref_key)
            if color_space is not None and "ColorSpace" in ref and ref["ColorSpace"] != color_space:
                raise ValueError(f"Material/export texture ColorSpace conflict: material={record['Name']}, property={slot['Property']}, reference={ref_key}, values={ref['ColorSpace']}/{color_space}")
            sampler = texture_samplers.get(ref_key)
            if (sampler is not None and "TextureSampler" in ref and
                    validate_texture_sampler(ref["TextureSampler"], f"material texture={ref_key}") != sampler):
                raise ValueError(f"Material/export texture sampler conflict: material={record['Name']}, property={slot['Property']}, reference={ref_key}")
            slots.append({**slot, "Texture": {**ref,
                **({"ColorSpace": color_space} if color_space is not None else {}),
                **({"TextureSampler": sampler} if sampler is not None else {})}})
        enriched[key] = {**record, "Textures": slots}
    materials = enriched
    return materials, texture_candidates


def has_procedural_foliage_emission(record: MaterialRecord) -> bool:
    """Identify the authored procedural foliage input contract, not an asset name."""
    properties = {slot["Property"] for slot in record["Textures"]}
    enabled = record["Floats"].get("_EnableEmissiveMap", 0) > 0.5 or record["Floats"].get("_EnableBakedEmissive", 0) > 0.5
    return enabled and "_FoliageGlowNoise" in properties and "_EmissiveMap" not in properties


def supported_slots(record: MaterialRecord) -> tuple[TextureSlot, ...]:
    floats = record["Floats"]
    family = surface_family(record)
    authored_properties = {slot["Property"] for slot in record["Textures"]}
    if {"_FogDensity", "_CubEdgeFade"} <= floats.keys() and "_FogColor" in record["Colors"] and "_NoiseTex3D" in authored_properties:
        raise ValueError(f"Deferred volume fog requires scene-volume evaluation, not static surface slots: material={record['Name']}")
    properties = {"_BaseColorMap", "_BaseMap", "_MainTex", "_NormalMap", "_BumpMap", "_MROMap"}
    if not uses_mro(record):
        properties.remove("_MROMap")
    if not uses_normal_map(record):
        properties.remove("_NormalMap")
    required: set[str] = set()
    defaults: set[str] = set()
    if has_verified_lit_top_layer(record):
        required.update(("_NormalMap", "_Layer1BaseMap", "_Layer1BumpMap"))
        if floats["_LayerBlendHeight"] != 0:
            required.add("_BaseHeightMap")
        bound = {slot["Property"] for slot in record["Textures"] if slot["Texture"] is not None}
        defaults = {name for name in required if shader_constant_texture_default(record, name) is not None}
        if required - bound - defaults:
            raise ValueError(f"Top layer requires bound textures or verified shader defaults: material={record['Name']}, properties={sorted(required - bound - defaults)}")
    if floats.get("_EnableTriChannelMask", 0) > 0.5:
        required.add("_MaskMap")
    if (floats.get("_EnableEmissiveMap", 0) > 0.5 or floats.get("_EnableBakedEmissive", 0) > 0.5) and not has_procedural_foliage_emission(record):
        if family in ("lit", "trunk", "transparent"):
            source = emission_source(record)
            source_property = {"emissive_map": "_EmissiveMap", "base_alpha": "_BaseColorMap",
                               "normal_blue": "_NormalMap", "normal_alpha": "_NormalMap",
                               "mro_alpha": "_MROMap"}.get(source)
            if source_property is not None:
                required.add(source_property)
        elif family in ("leaf", "card"):
            pass
        else:
            required.add("_EmissiveMap")
            channel = floats.get("_EmissiveMaskChannel")
            if channel is not None and channel not in (0, 1, 2, 3) and any(
                slot["Property"] == "_EmissiveMap" and slot["Texture"] is not None for slot in record["Textures"]
            ):
                raise ValueError(f"Unsupported emissive mask selection: material={record['Name']}, channel={channel}; shader semantics require verification")
    # Serialized null references are authored empty slots, not missing exports.
    present = authored_properties | defaults
    if required - present:
        raise ValueError(f"Enabled surface feature lacks authored texture: material={record['Name']}, properties={sorted(required - present)}")
    properties.update(required)
    return tuple(slot for slot in record["Textures"] if slot["Property"] in properties and slot["Texture"] is not None)


def top_layer_runtime_limitations(record: MaterialRecord) -> tuple[str, ...]:
    """Describe unresolved program inputs independently of audited source UV streams."""
    if not has_verified_lit_top_layer(record):
        return ()
    keywords = (record.get("RenderState") or {}).get("ValidKeywords", [])
    if "_TERRAIN_BLEND" in keywords:
        return ("terrain_blend_source_screen_depth_not_reconstructed",
                "terrain_blend_source_camera_constants_not_reconstructed",
                "terrain_blend_source_virtual_texture_sampling_not_reconstructed")
    return ("top_layer_current_program_equivalence_not_verified",)


def surface_limitations(record: MaterialRecord) -> tuple[str, ...]:
    flags = {"_LayerBlend": "layer_blend", "_EnableTop": "top_layer", "_UseSubsurfaceProfile": "subsurface_profile"}
    procedural = ("procedural_foliage_glow_not_reconstructed",) if has_procedural_foliage_emission(record) else ()
    top = has_verified_lit_top_layer(record)
    partial = top_layer_runtime_limitations(record)
    return ("game_lighting_not_reconstructed", "occlusion_retained_not_applied") + procedural + partial + tuple(
        label for flag, label in flags.items() if record["Floats"].get(flag, 0) > 0.5 and not (top and flag == "_LayerBlend")
    )


def load_slot_bindings(path: Path) -> dict[str, str]:
    payload = require_object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))
    if payload.get("format") != "EndfieldExactMaterialSlots/1" or not isinstance(payload.get("bindings"), list):
        raise ValueError(f"Bindings require EndfieldExactMaterialSlots/1 and bindings array: {path}")
    result: dict[str, str] = {}
    for value in cast(list[object], payload["bindings"]):
        row = require_object(value, "slot binding")
        container, reference = row.get("container"), row.get("source_reference")
        if not isinstance(container, str) or not container or not isinstance(reference, str):
            raise ValueError(f"Binding requires container and source_reference strings: {path}")
        parts = reference.rsplit(":", 1)
        if len(parts) != 2 or not parts[0] or not parts[1].lstrip("-").isdigit():
            raise ValueError(f"Binding reference requires Source:PathId: {reference}")
        key = container.replace("\\", "/").casefold()
        if key in result:
            raise ValueError(f"Duplicate material container binding: {container}")
        result[key] = reference.casefold()
    return result
