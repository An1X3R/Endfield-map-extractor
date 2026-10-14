"""Build identity-checked, source-backed character caster selections."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Literal, cast


AlphaMode = Literal["opaque", "clip", "dither", "unknown"]
Decision = Literal["accepted", "excluded"]
OPAQUE_FAMILIES = frozenset((
    "HGRP/CharacterNPR",
    "HGRP/CharacterNPR_Skin",
    "HGRP/CharacterNPR_Hair",
    "HGRP/CharacterNPR_Eye",
    "HGRP/CharacterNPR_OverlayShadow",
))


@dataclass(frozen=True, slots=True, order=True)
class SourceIdentity:
    source: str
    path_id: str

    @property
    def key(self) -> str:
        return f"{self.source}:{self.path_id}"

    def as_json(self) -> dict[str, str]:
        return {"source": self.source, "path_id": self.path_id}


@dataclass(frozen=True, slots=True)
class CasterSelection:
    """Duck-compatible selection for stage20 make_proxy_collection."""

    accepted_identities: tuple[str, ...]
    alpha_aware_identities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InputEvidence:
    role: str
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class TransformEvidence:
    identity: SourceIdentity
    path: str
    active: bool | None
    availability: str | None


@dataclass(frozen=True, slots=True)
class MaterialEvidence:
    identity: SourceIdentity
    name: str
    decision: Decision
    alpha_mode: AlphaMode
    valid_keywords: tuple[str, ...]
    source_surface_type: float | None
    render_type: str | None
    custom_render_queue: int | None
    shader_identity: SourceIdentity | None
    shader_name: str | None
    shadow_caster_disabled: bool | None
    disabled_shader_passes: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RendererEvidence:
    identity: SourceIdentity
    renderer_type: str
    game_object_identity: SourceIdentity | None
    mesh_identity: SourceIdentity | None
    path: str
    source_enabled: bool | None
    active_self: bool | None
    hierarchy_active: bool | None
    cast_shadows: int | None
    platform_cast_shadows: int | None
    decision: Decision
    reasons: tuple[str, ...]
    hierarchy: tuple[TransformEvidence, ...]
    materials: tuple[MaterialEvidence, ...]


@dataclass(frozen=True, slots=True)
class SourceCasterManifest:
    format: str
    root_identity: SourceIdentity
    resource_path: str
    inputs: tuple[InputEvidence, ...]
    selection: CasterSelection
    renderers: tuple[RendererEvidence, ...]


def _mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a JSON object with string keys")
    return cast(dict[str, object], value)


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a JSON array")
    return value


def _string(value: object, context: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{context} must be an integer, not bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    raise ValueError(f"{context} must be an integer or decimal integer string")


def _optional_bool(value: object, context: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{context} must be bool or null")
    return value


def _identity(value: object, context: str, expected_type: str | None = None) -> SourceIdentity:
    row = _mapping(value, context)
    source = _string(row.get("source"), f"{context}.source")
    path_id = str(_integer(row.get("path_id"), f"{context}.path_id"))
    object_type = row.get("type")
    if expected_type is not None and object_type != expected_type:
        raise ValueError(f"{context}.type must be {expected_type!r}; got {object_type!r}")
    return SourceIdentity(source, path_id)


def _resolved_identity(value: object, context: str, expected_type: str) -> SourceIdentity | None:
    row = _mapping(value, context)
    resolved = row.get("resolved")
    if resolved is None:
        return None
    return _identity(resolved, f"{context}.resolved", expected_type)


def _read_json(path: Path, role: str) -> tuple[object, InputEvidence, bytes]:
    resolved_path = path.resolve(strict=True)
    if not resolved_path.is_file():
        raise ValueError(f"{role} input is not a regular file: {resolved_path}")
    payload = resolved_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{role} is not valid UTF-8 JSON: {resolved_path}: {error}") from error
    return document, InputEvidence(role, str(resolved_path), digest), payload


def _root(document: dict[str, object], context: str) -> tuple[SourceIdentity, str]:
    requested = _mapping(document.get("requested_root"), f"{context}.requested_root")
    source = _string(requested.get("source"), f"{context}.requested_root.source")
    resource_path = _string(requested.get("resource_path"), f"{context}.requested_root.resource_path")
    actual = _identity(document.get("actual_root"), f"{context}.actual_root", "GameObject")
    if actual.source != source:
        raise ValueError(f"{context} requested and actual root CAB identities differ")
    return actual, resource_path


def _normal_keywords(value: object, context: str) -> frozenset[str]:
    keywords = _array(value, context)
    result: set[str] = set()
    for index, item in enumerate(keywords):
        if item == "":
            continue
        result.add(_string(item, f"{context}[{index}]").upper())
    return frozenset(result)


def _float_property_map(material: dict[str, object], context: str) -> dict[str, float]:
    saved = _mapping(material.get("m_SavedProperties"), f"{context}.m_SavedProperties")
    values = _array(saved.get("m_Floats"), f"{context}.m_SavedProperties.m_Floats")
    result: dict[str, float] = {}
    for index, item in enumerate(values):
        row = _mapping(item, f"{context}.m_SavedProperties.m_Floats[{index}]")
        name = _string(row.get("Key"), f"{context}.m_SavedProperties.m_Floats[{index}].Key")
        value = row.get("Value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{context}.m_SavedProperties.m_Floats[{index}].Value must be finite numeric")
        if name in result:
            raise ValueError(f"{context} has duplicate float property {name!r}")
        result[name] = float(value)
    return result


def _render_type_tag(material: dict[str, object], context: str) -> str | None:
    tags = _array(material.get("stringTagMap"), f"{context}.stringTagMap")
    values: list[str] = []
    for index, item in enumerate(tags):
        row = _mapping(item, f"{context}.stringTagMap[{index}]")
        key = row.get("Key")
        if isinstance(key, str) and key.casefold() == "rendertype":
            values.append(_string(row.get("Value"), f"{context}.stringTagMap[{index}].Value"))
    if len(values) > 1:
        raise ValueError(f"{context} has duplicate RenderType tags")
    return values[0] if values else None


def _alpha_mode(
    material: dict[str, object], context: str, shader_name: str | None,
) -> tuple[AlphaMode, tuple[str, ...], tuple[str, ...], float | None]:
    valid = _normal_keywords(material.get("m_ValidKeywords"), f"{context}.m_ValidKeywords")
    invalid = _normal_keywords(material.get("m_InvalidKeywords"), f"{context}.m_InvalidKeywords")
    alpha_test = "_ALPHATEST_ON"
    alpha_blend = "_ALPHABLEND_ON"
    recognized = {alpha_test, alpha_blend}
    alpha_related = {
        keyword for keyword in valid | invalid
        if "ALPHA" in keyword or "DITHER" in keyword or "TRANSPARENT" in keyword
    }
    unknown = (alpha_related - recognized) | ((valid | invalid) & {"_CHARACTER_FUR"})
    valid_keywords = tuple(sorted(valid))
    floats = _float_property_map(material, context)
    surface_type = floats.get("_SurfaceType")
    if unknown:
        return "unknown", ("unsupported_alpha_variant_keywords:" + ",".join(sorted(unknown)),), valid_keywords, surface_type
    if valid & recognized == recognized:
        return "unknown", ("conflicting_alpha_test_and_blend_keywords",), valid_keywords, surface_type
    if valid & recognized:
        mode: AlphaMode = "clip" if alpha_test in valid else "dither"
        if invalid & recognized:
            return "unknown", ("alpha_keyword_marked_invalid",), valid_keywords, surface_type
        if shader_name != "HGRP/CharacterNPR":
            return "unknown", ("alpha_proxy_shader_not_in_verified_standard_family",), valid_keywords, surface_type
        if surface_type is None:
            return "unknown", ("source_surface_type_unavailable",), valid_keywords, None
        return mode, (), valid_keywords, surface_type
    if invalid & recognized:
        return "unknown", ("alpha_keyword_marked_invalid",), valid_keywords, surface_type
    if shader_name not in OPAQUE_FAMILIES:
        return "unknown", ("opaque_shader_family_not_verified",), valid_keywords, surface_type
    if surface_type is None:
        return "unknown", ("source_surface_type_unavailable",), valid_keywords, None
    if surface_type == 0.0:
        return "opaque", (), valid_keywords, surface_type
    return "unknown", ("source_surface_type_not_opaque",), valid_keywords, surface_type


def _hierarchy(
    game_object_identity: SourceIdentity,
    transform_rows: tuple[dict[str, object], ...],
    context: str,
) -> tuple[bool | None, tuple[TransformEvidence, ...], tuple[str, ...]]:
    by_game_object: dict[SourceIdentity, list[dict[str, object]]] = {}
    by_transform: dict[SourceIdentity, dict[str, object]] = {}
    for index, row in enumerate(transform_rows):
        transform_id = _identity(row.get("object"), f"{context}.transforms[{index}].object", "Transform")
        if transform_id in by_transform:
            raise ValueError(f"{context} contains duplicate Transform identity {transform_id.key}")
        by_transform[transform_id] = row
        go_id = _identity(row.get("game_object"), f"{context}.transforms[{index}].game_object", "GameObject")
        by_game_object.setdefault(go_id, []).append(row)
    leaf_candidates = by_game_object.get(game_object_identity, [])
    if len(leaf_candidates) != 1:
        return None, (), ("transform_for_game_object_not_unique",)

    chain: list[TransformEvidence] = []
    seen: set[SourceIdentity] = set()
    cursor = leaf_candidates[0]
    while True:
        transform_id = _identity(cursor.get("object"), f"{context}.transform.object", "Transform")
        if transform_id in seen:
            return None, tuple(chain), ("transform_parent_cycle",)
        seen.add(transform_id)
        path = _string(cursor.get("path"), f"{context}.transform.path", allow_empty=True)
        active = _optional_bool(cursor.get("game_object_active"), f"{context}.transform.game_object_active")
        availability_value = cursor.get("active_availability")
        availability = None if availability_value is None else _string(availability_value, f"{context}.transform.active_availability")
        chain.append(TransformEvidence(transform_id, path, active, availability))
        if active is None or availability is None:
            return None, tuple(chain), ("transform_active_state_unavailable",)

        parent = _mapping(cursor.get("parent"), f"{context}.transform.parent")
        parent_path_id = _integer(parent.get("path_id"), f"{context}.transform.parent.path_id")
        if parent_path_id == 0:
            return all(item.active is True for item in chain), tuple(chain), ()
        resolved_parent = _resolved_identity(parent, f"{context}.transform.parent", "Transform")
        if resolved_parent is None:
            return None, tuple(chain), ("parent_transform_unresolved",)
        if _integer(parent.get("path_id"), f"{context}.transform.parent.path_id") != int(resolved_parent.path_id):
            return None, tuple(chain), ("parent_transform_path_id_mismatch",)
        cursor = by_transform.get(resolved_parent)
        if cursor is None:
            return None, tuple(chain), ("parent_transform_outside_audited_root",)


def _material_index(
    audit: dict[str, object],
    material_type_trees: list[object],
    shader_document: dict[str, object],
) -> tuple[
    dict[SourceIdentity, dict[str, object]],
    dict[SourceIdentity, tuple[SourceIdentity, str | None]],
    dict[SourceIdentity, dict[str, object]],
]:
    source_materials = _array(audit.get("materials"), "source_audit.materials")
    source_by_identity: dict[SourceIdentity, dict[str, object]] = {}
    for index, item in enumerate(source_materials):
        row = _mapping(item, f"source_audit.materials[{index}]")
        material_id = _identity(row.get("object"), f"source_audit.materials[{index}].object", "Material")
        if material_id in source_by_identity:
            raise ValueError(f"source_audit contains duplicate material identity {material_id.key}")
        source_by_identity[material_id] = row

    type_tree_by_identity: dict[SourceIdentity, dict[str, object]] = {}
    for index, item in enumerate(material_type_trees):
        row = _mapping(item, f"material_type_trees[{index}]")
        material_id = _identity(row.get("object"), f"material_type_trees[{index}].object", "Material")
        if material_id in type_tree_by_identity:
            raise ValueError(f"material TypeTrees contain duplicate material identity {material_id.key}")
        type_tree_by_identity[material_id] = row
    if set(type_tree_by_identity) != set(source_by_identity):
        missing = sorted(item.key for item in set(source_by_identity) - set(type_tree_by_identity))
        extra = sorted(item.key for item in set(type_tree_by_identity) - set(source_by_identity))
        raise ValueError(f"material TypeTree identity set differs from source audit: missing={missing}, extra={extra}")

    shader_rows = _array(shader_document.get("shaders"), "shader_identity.shaders")
    shader_for_material: dict[SourceIdentity, tuple[SourceIdentity, str | None]] = {}
    for index, item in enumerate(shader_rows):
        row = _mapping(item, f"shader_identity.shaders[{index}]")
        shader_id = _identity(row.get("shader"), f"shader_identity.shaders[{index}].shader", "Shader")
        parsed_name = row.get("parsed_form_name")
        shader_name = parsed_name if isinstance(parsed_name, str) and parsed_name else None
        shader_materials = _array(row.get("materials"), f"shader_identity.shaders[{index}].materials")
        for material_index, material_item in enumerate(shader_materials):
            material_id = _identity(material_item, f"shader_identity.shaders[{index}].materials[{material_index}]", "Material")
            if material_id in shader_for_material:
                raise ValueError(f"shader identity lists material more than once: {material_id.key}")
            shader_for_material[material_id] = (shader_id, shader_name)
    extra_shader_materials = sorted(item.key for item in set(shader_for_material) - set(source_by_identity))
    if extra_shader_materials:
        raise ValueError(f"shader identity contains materials outside source audit: {extra_shader_materials}")

    return source_by_identity, shader_for_material, type_tree_by_identity


def _material_evidence(
    material_id: SourceIdentity,
    source_row: dict[str, object] | None,
    type_tree_row: dict[str, object] | None,
    shader_info: tuple[SourceIdentity, str | None] | None,
) -> MaterialEvidence:
    context = f"material[{material_id.key}]"
    reasons: list[str] = []
    if source_row is None:
        return MaterialEvidence(
            material_id, "", "excluded", "unknown", (), None, None, None,
            None, None, None, (), ("material_missing_from_source_audit",),
        )
    object_row = _mapping(source_row.get("object"), context + ".object")
    raw_name = object_row.get("name")
    name = raw_name if isinstance(raw_name, str) else ""
    shader_ref = _mapping(source_row.get("shader"), context + ".source_shader")
    source_shader_id = _resolved_identity(shader_ref, context + ".source_shader", "Shader")
    shader_id: SourceIdentity | None = None
    shader_name: str | None = None
    if source_shader_id is None or shader_info is None:
        reasons.append("shader_identity_unresolved")
    else:
        shader_id, shader_name = shader_info
        if source_shader_id != shader_id:
            reasons.append("shader_identity_cross_source_mismatch")
        if shader_name is None:
            reasons.append("shader_name_unavailable")
    if type_tree_row is None:
        reasons.append("material_type_tree_unavailable")
        disabled: bool | None = None
        mode: AlphaMode = "unknown"
        valid_keywords: tuple[str, ...] = ()
        surface_type: float | None = None
        render_type: str | None = None
        custom_render_queue: int | None = None
        disabled_names: tuple[str, ...] = ()
    else:
        available = type_tree_row.get("available")
        if available is not True:
            reasons.append("material_type_tree_not_available")
        type_tree = type_tree_row.get("data")
        if not isinstance(type_tree, dict):
            reasons.append("material_type_tree_data_missing")
            disabled = None
            mode = "unknown"
            valid_keywords = ()
            surface_type = None
            render_type = None
            custom_render_queue = None
            disabled_names = ()
        else:
            raw_shader = _mapping(type_tree.get("m_Shader"), context + ".m_Shader")
            if source_shader_id is None:
                reasons.append("shader_reference_not_resolved_by_source_audit")
            else:
                if _integer(raw_shader.get("m_PathID"), context + ".m_Shader.m_PathID") != int(source_shader_id.path_id):
                    reasons.append("material_type_tree_shader_path_id_mismatch")
                source_file_id = _integer(shader_ref.get("file_id"), context + ".source_shader.file_id")
                if _integer(raw_shader.get("m_FileID"), context + ".m_Shader.m_FileID") != source_file_id:
                    reasons.append("material_type_tree_shader_file_id_mismatch")
            disabled_values = _array(type_tree.get("disabledShaderPasses"), context + ".disabledShaderPasses")
            disabled_names = tuple(_string(item, f"{context}.disabledShaderPasses[{index}]") for index, item in enumerate(disabled_values))
            disabled = any(item.casefold() == "shadowcaster" for item in disabled_names)
            if disabled:
                reasons.append("shadow_caster_pass_disabled")
            mode, alpha_reasons, valid_keywords, surface_type = _alpha_mode(type_tree, context, shader_name)
            reasons.extend(alpha_reasons)
            custom_queue_value = type_tree.get("m_CustomRenderQueue")
            custom_render_queue = _integer(custom_queue_value, context + ".m_CustomRenderQueue") if custom_queue_value is not None else None
            render_type = _render_type_tag(type_tree, context)
    if shader_name is None:
        mode = "unknown"
        reasons.append("unknown_shader_prevents_alpha_classification")
    decision: Decision = "accepted" if not reasons else "excluded"
    return MaterialEvidence(
        material_id, name, decision, mode, valid_keywords, surface_type, render_type,
        custom_render_queue, shader_id, shader_name, disabled, disabled_names,
        tuple(dict.fromkeys(reasons)),
    )


def _mesh_filter_data(
    visibility: dict[str, object],
    renderer_go: SourceIdentity,
    mesh_id: SourceIdentity,
    context: str,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Cross-check the static renderer's resolved MeshFilter against its TypeTree."""
    record = _mapping(visibility.get("mesh_filter"), context + ".mesh_filter")
    filter_id = _identity(record.get("object"), context + ".mesh_filter.object", "MeshFilter")
    tree = _mapping(record.get("type_tree"), context + ".mesh_filter.type_tree")
    if _identity(tree.get("object"), context + ".mesh_filter.type_tree.object", "MeshFilter") != filter_id:
        raise ValueError(f"{context} MeshFilter parser and TypeTree object identities disagree")
    if tree.get("available") is not True:
        return {}, ("mesh_filter_type_tree_unavailable",)
    data = _mapping(tree.get("data"), context + ".mesh_filter.type_tree.data")
    reasons: list[str] = []
    for field, expected, expected_type in (("game_object", renderer_go, "GameObject"), ("mesh", mesh_id, "Mesh")):
        pointer = _mapping(record.get(field), context + ".mesh_filter." + field)
        if _resolved_identity(pointer, context + ".mesh_filter." + field, expected_type) != expected:
            reasons.append("mesh_filter_" + field + "_identity_mismatch")
        tree_field = "m_GameObject" if field == "game_object" else "m_Mesh"
        raw = _mapping(data.get(tree_field), context + ".mesh_filter." + tree_field)
        for pointer_field, raw_field in (("file_id", "m_FileID"), ("path_id", "m_PathID")):
            if _integer(pointer.get(pointer_field), context + ".mesh_filter." + field + "." + pointer_field) != _integer(raw.get(raw_field), context + ".mesh_filter." + tree_field + "." + raw_field):
                reasons.append("mesh_filter_" + field + "_" + pointer_field + "_mismatch")
    return data, tuple(reasons)


def _renderer_evidence(
    renderer: dict[str, object],
    visibility: dict[str, object],
    transform_rows: tuple[dict[str, object], ...],
    source_materials: dict[SourceIdentity, dict[str, object]],
    shader_for_material: dict[SourceIdentity, tuple[SourceIdentity, str | None]],
    material_type_trees: dict[SourceIdentity, dict[str, object]],
) -> RendererEvidence:
    context = "renderer[" + _identity(renderer.get("object"), "renderer.object").key + "]"
    renderer_id = _identity(renderer.get("object"), context + ".object")
    renderer_type = _string(_mapping(renderer.get("object"), context + ".object").get("type"), context + ".type")
    reasons: list[str] = []
    if renderer_type not in ("MeshRenderer", "SkinnedMeshRenderer"):
        reasons.append("unsupported_renderer_type")
    renderer_go = _identity(renderer.get("game_object"), context + ".game_object", "GameObject")
    visibility_go = _identity(visibility.get("game_object"), context + ".visibility.game_object", "GameObject")
    if renderer_go != visibility_go:
        reasons.append("renderer_game_object_identity_mismatch")

    flat_enabled = _optional_bool(visibility.get("enabled"), context + ".visibility.enabled")
    source_enabled = _optional_bool(renderer.get("enabled"), context + ".source_enabled")
    active_self = _optional_bool(visibility.get("active_self"), context + ".active_self")
    if source_enabled is not True:
        reasons.append("source_renderer_disabled")
    if flat_enabled is not True:
        reasons.append("renderer_visibility_disabled")
    if source_enabled != flat_enabled:
        reasons.append("enabled_state_mismatch")

    type_tree_record = _mapping(visibility.get("renderer_type_tree"), context + ".renderer_type_tree")
    if type_tree_record.get("available") is not True:
        reasons.append("renderer_type_tree_unavailable")
        renderer_data: dict[str, object] = {}
    else:
        renderer_data = _mapping(type_tree_record.get("data"), context + ".renderer_type_tree.data")
    typed_object = _mapping(type_tree_record.get("object"), context + ".renderer_type_tree.object")
    if typed_object.get("type") != renderer_type:
        reasons.append("renderer_type_identity_mismatch")
    typed_enabled = _optional_bool(renderer_data.get("m_Enabled"), context + ".m_Enabled") if renderer_data else None
    if typed_enabled != flat_enabled:
        reasons.append("renderer_type_tree_enabled_mismatch")

    cast_value: int | None = None
    platform_value: int | None = None
    raw_cast = visibility.get("cast_shadows")
    raw_platform = visibility.get("platform_specific_cast_shadows")
    try:
        cast_value = _integer(raw_cast, context + ".cast_shadows")
    except ValueError:
        reasons.append("cast_shadows_unavailable")
    if cast_value not in (1, 2, 3):
        reasons.append("cast_shadows_outside_supported_values")
    if renderer_data:
        try:
            type_cast = _integer(renderer_data.get("m_CastShadows"), context + ".m_CastShadows")
            if cast_value is not None and type_cast != cast_value:
                reasons.append("cast_shadows_type_tree_mismatch")
        except ValueError:
            reasons.append("cast_shadows_type_tree_unavailable")
        platform_tree = renderer_data.get("m_PlatformSpecificCastShadows")
        try:
            platform_mapping = _mapping(platform_tree, context + ".m_PlatformSpecificCastShadows")
            platform_value = _integer(platform_mapping.get("defaultParam"), context + ".m_PlatformSpecificCastShadows.defaultParam")
            if _integer(raw_platform, context + ".platform_specific_cast_shadows") != platform_value:
                reasons.append("platform_cast_shadows_type_tree_mismatch")
            if platform_value not in (1, 2, 3):
                reasons.append("platform_cast_shadows_outside_supported_values")
        except ValueError:
            reasons.append("platform_cast_shadows_unavailable")

    hierarchy_active, hierarchy, hierarchy_reasons = _hierarchy(renderer_go, transform_rows, context)
    reasons.extend(hierarchy_reasons)
    if hierarchy_active is not True:
        reasons.append("source_transform_hierarchy_inactive_or_unknown")
    leaf_active = hierarchy[0].active if hierarchy else None
    if active_self != leaf_active:
        reasons.append("renderer_active_self_transform_mismatch")

    renderer_path = _string(visibility.get("path"), context + ".path", allow_empty=True)
    source_path = _string(renderer.get("path"), context + ".source_path", allow_empty=True)
    if renderer_path != source_path:
        reasons.append("renderer_path_evidence_mismatch")

    mesh_value = renderer.get("mesh")
    if mesh_value is None:
        mesh_id = None
    else:
        mesh_id = _identity(mesh_value, context + ".mesh", "Mesh")
    if mesh_id is None:
        reasons.append("renderer_mesh_unresolved")
    elif renderer_data:
        mesh_data = renderer_data
        if renderer_type == "MeshRenderer":
            mesh_data, mesh_filter_reasons = _mesh_filter_data(visibility, renderer_go, mesh_id, context)
            reasons.extend(mesh_filter_reasons)
        raw_mesh = _mapping(mesh_data.get("m_Mesh"), context + ".m_Mesh") if mesh_data else {}
        if raw_mesh and _integer(raw_mesh.get("m_PathID"), context + ".m_Mesh.m_PathID") != int(mesh_id.path_id):
            reasons.append("renderer_mesh_path_id_mismatch")
    raw_game_object = _mapping(renderer_data.get("m_GameObject"), context + ".m_GameObject") if renderer_data else {}
    if raw_game_object and _integer(raw_game_object.get("m_PathID"), context + ".m_GameObject.m_PathID") != int(renderer_go.path_id):
        reasons.append("renderer_game_object_path_id_mismatch")

    audit_materials = _array(renderer.get("materials"), context + ".materials")
    raw_materials = _array(renderer_data.get("m_Materials"), context + ".m_Materials") if renderer_data else []
    if not audit_materials:
        reasons.append("renderer_has_no_material_references")
    if len(audit_materials) != len(raw_materials):
        reasons.append("renderer_material_reference_count_mismatch")
    materials: list[MaterialEvidence] = []
    for index, reference_value in enumerate(audit_materials):
        reference = _mapping(reference_value, f"{context}.materials[{index}]")
        resolved = _resolved_identity(reference, f"{context}.materials[{index}]", "Material")
        if resolved is None:
            reasons.append(f"renderer_material_unresolved:{index}")
            continue
        if index < len(raw_materials):
            raw_reference = _mapping(raw_materials[index], f"{context}.m_Materials[{index}]")
            if _integer(reference.get("path_id"), f"{context}.materials[{index}].path_id") != _integer(raw_reference.get("m_PathID"), f"{context}.m_Materials[{index}].m_PathID"):
                reasons.append(f"renderer_material_path_id_mismatch:{index}")
            if _integer(reference.get("file_id"), f"{context}.materials[{index}].file_id") != _integer(raw_reference.get("m_FileID"), f"{context}.m_Materials[{index}].m_FileID"):
                reasons.append(f"renderer_material_file_id_mismatch:{index}")
        material = _material_evidence(
            resolved,
            source_materials.get(resolved),
            material_type_trees.get(resolved),
            shader_for_material.get(resolved),
        )
        materials.append(material)
        if material.decision != "accepted":
            reasons.extend(f"material:{resolved.key}:{reason}" for reason in material.reasons)

    name_value = _mapping(renderer.get("object"), context + ".object").get("name")
    if not isinstance(name_value, str):
        reasons.append("renderer_display_name_invalid")
    decision: Decision = "accepted" if not reasons and materials else "excluded"
    return RendererEvidence(
        renderer_id,
        renderer_type,
        renderer_go,
        mesh_id,
        renderer_path,
        source_enabled,
        active_self,
        hierarchy_active,
        cast_value,
        platform_value,
        decision,
        tuple(dict.fromkeys(reasons)),
        hierarchy,
        tuple(materials),
    )


def build_source_caster_manifest(
    source_audit_path: Path,
    renderer_visibility_path: Path,
    shader_identity_path: Path,
    material_type_trees_path: Path,
) -> SourceCasterManifest:
    """Build a stage20-compatible selection and a complete evidence manifest."""
    audit_value, audit_input, _ = _read_json(source_audit_path, "source_audit")
    visibility_value, visibility_input, _ = _read_json(renderer_visibility_path, "renderer_visibility")
    shader_value, shader_input, _ = _read_json(shader_identity_path, "shader_identity")
    materials_value, materials_input, _ = _read_json(material_type_trees_path, "material_type_trees")
    audit = _mapping(audit_value, "source_audit")
    visibility = _mapping(visibility_value, "renderer_visibility")
    shader_document = _mapping(shader_value, "shader_identity")
    material_type_trees = _array(materials_value, "material_type_trees")

    if audit.get("format") != "EndfieldCharacterSourceAudit/1":
        raise ValueError(f"Unsupported source audit format in {audit_input.path}: {audit.get('format')!r}")
    if visibility.get("format") != "EndfieldCharacterRendererVisibility/1":
        raise ValueError(f"Unsupported renderer visibility format in {visibility_input.path}: {visibility.get('format')!r}")
    if shader_document.get("format") != "EndfieldCharacterShaderIdentity/1":
        raise ValueError(f"Unsupported shader identity format in {shader_input.path}: {shader_document.get('format')!r}")

    root_identity, resource_path = _root(audit, "source_audit")
    visibility_root, visibility_resource_path = _root(visibility, "renderer_visibility")
    shader_requested = _mapping(shader_document.get("requested_root"), "shader_identity.requested_root")
    shader_resource_path = _string(shader_requested.get("resource_path"), "shader_identity.requested_root.resource_path")
    shader_source = _string(shader_requested.get("source"), "shader_identity.requested_root.source")
    if visibility_root != root_identity or visibility_resource_path != resource_path:
        raise ValueError("renderer visibility root does not match source audit root")
    if shader_source != root_identity.source or shader_resource_path != resource_path:
        raise ValueError("shader identity requested root does not match source audit root")
    audit_digest = audit_input.sha256.casefold()
    shader_audit_digest = _string(shader_document.get("source_audit_sha256"), "shader_identity.source_audit_sha256").casefold()
    if shader_audit_digest != audit_digest:
        raise ValueError("shader identity was generated from different source_audit bytes")
    _string(shader_document.get("source_audit"), "shader_identity.source_audit")

    audit_renderer_rows = _array(audit.get("renderers"), "source_audit.renderers")
    visibility_rows = _array(visibility.get("renderers"), "renderer_visibility.renderers")
    audit_by_identity: dict[SourceIdentity, dict[str, object]] = {}
    for index, item in enumerate(audit_renderer_rows):
        row = _mapping(item, f"source_audit.renderers[{index}]")
        renderer_id = _identity(row.get("object"), f"source_audit.renderers[{index}].object")
        if renderer_id in audit_by_identity:
            raise ValueError(f"source audit contains duplicate renderer identity {renderer_id.key}")
        audit_by_identity[renderer_id] = row
    visibility_by_identity: dict[SourceIdentity, dict[str, object]] = {}
    for index, item in enumerate(visibility_rows):
        row = _mapping(item, f"renderer_visibility.renderers[{index}]")
        renderer_id = _identity(row.get("renderer"), f"renderer_visibility.renderers[{index}].renderer")
        if renderer_id in visibility_by_identity:
            raise ValueError(f"renderer visibility contains duplicate renderer identity {renderer_id.key}")
        typed = _mapping(row.get("renderer_type_tree"), f"renderer_visibility.renderers[{index}].renderer_type_tree")
        typed_id = _identity(typed.get("object"), f"renderer_visibility.renderers[{index}].renderer_type_tree.object")
        if renderer_id != typed_id:
            raise ValueError(f"visibility renderer and its TypeTree identity differ: {renderer_id.key}")
        visibility_by_identity[renderer_id] = row
    if set(audit_by_identity) != set(visibility_by_identity):
        missing = sorted(item.key for item in set(audit_by_identity) - set(visibility_by_identity))
        extra = sorted(item.key for item in set(visibility_by_identity) - set(audit_by_identity))
        raise ValueError(f"source audit and visibility renderer identities differ: missing={missing}, extra={extra}")

    source_materials, shader_for_material, type_tree_by_identity = _material_index(audit, material_type_trees, shader_document)
    transform_rows = tuple(_mapping(item, f"source_audit.transforms[{index}]") for index, item in enumerate(_array(audit.get("transforms"), "source_audit.transforms")))
    renderer_evidence = tuple(
        _renderer_evidence(
            audit_by_identity[renderer_id],
            visibility_by_identity[renderer_id],
            transform_rows,
            source_materials,
            shader_for_material,
            type_tree_by_identity,
        )
        for renderer_id in audit_by_identity
    )
    accepted = tuple(row.identity.key for row in renderer_evidence if row.decision == "accepted")
    alpha_aware = tuple(
        row.identity.key for row in renderer_evidence
        if row.decision == "accepted" and any(material.alpha_mode in ("clip", "dither") for material in row.materials)
    )
    return SourceCasterManifest(
        "EndfieldSourceCasterManifest/1",
        root_identity,
        resource_path,
        (audit_input, visibility_input, shader_input, materials_input),
        CasterSelection(accepted, alpha_aware),
        renderer_evidence,
    )


def manifest_as_json(manifest: SourceCasterManifest) -> dict[str, object]:
    """Return a fresh JSON-compatible audit without mutating the manifest."""
    return {
        "format": manifest.format,
        "status": "candidate_selection_only",
        "scope": "Source-backed identity and alpha-state selection only; no proxy geometry, downstream alpha binding, or rendered shadow is verified.",
        "root": {"identity": manifest.root_identity.as_json(), "resource_path": manifest.resource_path},
        "inputs": [
            {"role": row.role, "path": row.path, "sha256": row.sha256}
            for row in manifest.inputs
        ],
        "selection": {
            "accepted_identities": list(manifest.selection.accepted_identities),
            "alpha_aware_identities": list(manifest.selection.alpha_aware_identities),
        },
        "summary": {
            "renderer_count": len(manifest.renderers),
            "accepted_count": len(manifest.selection.accepted_identities),
            "alpha_aware_count": len(manifest.selection.alpha_aware_identities),
            "excluded_count": sum(row.decision == "excluded" for row in manifest.renderers),
        },
        "identity_validation": {
            "renderer_join": "source_audit object identity equals renderer_visibility.renderer identity; identity is source CAB plus decimal PathID",
            "material_join": "renderer material resolved identity equals source_audit material identity and material TypeTree object identity",
            "shader_join": "source_audit material shader resolved identity equals shader_identity shader identity and shader TypeTree PPtr path/file IDs",
            "root_join": "requested resource root and actual root CAB/PathID must agree across source audit and visibility; shader request root and source audit byte SHA256 must agree",
            "hierarchy": "renderer GameObject must resolve to exactly one Transform; every parent Transform identity must resolve inside the audited transform set and every active flag must be available",
        },
        "renderers": [
            {
                "renderer_identity": row.identity.key,
                "renderer_object": row.identity.as_json(),
                "renderer_type": row.renderer_type,
                "game_object_identity": row.game_object_identity.key if row.game_object_identity else None,
                "mesh_identity": row.mesh_identity.key if row.mesh_identity else None,
                "path": row.path,
                "source_enabled": row.source_enabled,
                "active_self": row.active_self,
                "hierarchy_active": row.hierarchy_active,
                "cast_shadows": row.cast_shadows,
                "platform_cast_shadows": row.platform_cast_shadows,
                "decision": row.decision,
                "reasons": list(row.reasons),
                "hierarchy": [
                    {
                        "transform_identity": item.identity.key,
                        "transform_object": item.identity.as_json(),
                        "path": item.path,
                        "active": item.active,
                        "availability": item.availability,
                    }
                    for item in row.hierarchy
                ],
                "materials": [
                    {
                        "material_identity": item.identity.key,
                        "material_object": item.identity.as_json(),
                        "name": item.name,
                        "decision": item.decision,
                        "alpha_mode": item.alpha_mode,
                        "valid_keywords": list(item.valid_keywords),
                        "source_surface_type": item.source_surface_type,
                        "render_type": item.render_type,
                        "custom_render_queue": item.custom_render_queue,
                        "shader_identity": item.shader_identity.key if item.shader_identity else None,
                        "shader_name": item.shader_name,
                        "shadow_caster_disabled": item.shadow_caster_disabled,
                        "disabled_shader_passes": list(item.disabled_shader_passes),
                        "reasons": list(item.reasons),
                    }
                    for item in row.materials
                ],
            }
            for row in manifest.renderers
        ],
    }


def write_source_caster_manifest(output_path: Path, manifest: SourceCasterManifest) -> None:
    """Write a deterministic UTF-8 JSON audit to an explicit output path."""
    destination = output_path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest_as_json(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
