"""Validate SceneProbe material records and plan reproducible surface previews."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import NotRequired, TypedDict, cast
from endfield_shader_contract import emission_source, surface_family, uses_mro, uses_normal_map


def is_neutral_material_name(name: str) -> bool:
    """Recognize explicit placeholder materials emitted by both scene chains."""
    return name.startswith(("Geometry_Only_Neutral", "REPAIR_NEUTRAL", "Unbound_Model_Material_Neutral"))


class AssetReference(TypedDict):
    Source: str
    PathId: int
    Name: NotRequired[str]
    PassNames: NotRequired[list[str]]
    ColorSpace: NotRequired[int]


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


def resolve_export_path(path: Path) -> Path:
    """Use Windows extended paths consistently across Python and embedded Blender."""
    resolved = path.expanduser().resolve()
    value = str(resolved)
    if os.name != "nt" or len(value) < 260 or value.startswith("\\\\?\\"):
        return resolved
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


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


def validate_material(value: object) -> MaterialRecord:
    row = require_object(value, "material")
    reference_key(row)
    reference_key(row.get("Shader"))
    shader = require_object(row["Shader"], "material Shader")
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
    for field in ("Name", "PassNames"):
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
    materials: dict[tuple[str, int], MaterialRecord] = {}
    textures: dict[tuple[str, int], tuple[Path, str]] = {}
    texture_color_spaces: dict[tuple[str, int], int] = {}
    for path in paths:
        data = require_object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))
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
            candidate = resolve_export_path(path.parent / file.replace("\\", "/"))
            if candidate.is_file() and key not in textures:
                textures[key] = (candidate.parent, candidate.name)
    enriched: dict[tuple[str, int], MaterialRecord] = {}
    for key, record in materials.items():
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
            slots.append({**slot, "Texture": {**ref, **({"ColorSpace": color_space} if color_space is not None else {})}})
        enriched[key] = {**record, "Textures": slots}
    materials = enriched
    return materials, textures


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
    present = authored_properties
    if required - present:
        raise ValueError(f"Enabled surface feature lacks authored texture: material={record['Name']}, properties={sorted(required - present)}")
    properties.update(required)
    return tuple(slot for slot in record["Textures"] if slot["Property"] in properties and slot["Texture"] is not None)


def surface_limitations(record: MaterialRecord) -> tuple[str, ...]:
    flags = {"_LayerBlend": "layer_blend", "_EnableTop": "top_layer", "_UseSubsurfaceProfile": "subsurface_profile"}
    procedural = ("procedural_foliage_glow_not_reconstructed",) if has_procedural_foliage_emission(record) else ()
    return ("game_lighting_not_reconstructed", "occlusion_retained_not_applied") + procedural + tuple(
        label for flag, label in flags.items() if record["Floats"].get(flag, 0) > 0.5
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
