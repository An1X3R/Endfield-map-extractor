"""Bind independent SkeletalMorph tables to exact prefab references and Avatar sources."""
from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
import hashlib
import math
import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from endfield_character_catalog import CharacterPrefabTemplate


class CharacterExpressionTableError(ValueError):
    """Expression table evidence is malformed or contradicts its source identity."""


@dataclass(frozen=True)
class ExpressionTableBinding:
    family: Literal["facial", "ear", "unknown"]
    reference_field: str | None
    reference_value: str | None
    resource_path: str | None
    provider_logical_name: str | None
    provider_kind: str | None
    avatar_cab: str | None
    avatar_path_id: str | None
    status: Literal["selected", "pending"]
    pending_code: str | None


@dataclass(frozen=True)
class ExpressionTablePlan:
    format: Literal["EndfieldExpressionTablePlan/1"]
    template_id: str
    character_id: str
    template_source_path: str
    template_source_sha256: str
    provider_index_sha256: str | None
    scan_status: Literal["complete", "incomplete"]
    bindings: tuple[ExpressionTableBinding, ...]


@dataclass(frozen=True)
class ExpressionTablePending:
    code: str
    message: str
    resource_path: str | None
    source_path: str | None


@dataclass(frozen=True)
class ExpressionTableSourceIdentity:
    table_identity: str
    character_id: str
    template_id: str
    reference_field: str
    reference_value: str
    resource_path: str
    provider_logical_name: str
    provider_kind: str
    serialized_file: str
    object_path_id: str
    object_class_id: int
    object_bytes: int
    bundle_sha256: str
    raw_object_sha256: str
    decoded_source_canonical_sha256: str
    avatar_cab: str
    avatar_path_id: str
    asset_name: str


@dataclass(frozen=True)
class ExpressionControlSource:
    index: int
    name: str
    rid: str
    source_class: str
    bone_count: int
    endpoint_status: Literal["source_pose_endpoint_candidate", "empty_mapping", "material_property_control", "unsupported_mapping_class"]


@dataclass(frozen=True)
class ExpressionMaterialProperty:
    """A separately typed source material mapping, never a skeletal pose endpoint."""

    control_index: int
    control_name: str
    mapping_rid: str
    parameter_rid: str
    shader_name: str
    property_name: str
    default_value: float
    renderer_mask: int
    blend_mode: int


def read_expression_material_properties(document: dict[str, object]) -> tuple[ExpressionMaterialProperty, ...]:
    """Resolve supported float properties through exact managed-reference RIDs."""
    raw = _object(document.get("raw"), "expression_source.raw")
    records: dict[str, dict[str, object]] = {}
    for value in _array(raw.get("managed_references"), "managed_references"):
        record = _object(value, "managed reference")
        rid = _decimal_id(record.get("rid"), "managed reference.rid")
        if rid in records:
            raise CharacterExpressionTableError(f"Duplicate expression managed-reference RID: rid={rid}")
        records[rid] = record
    mappings = tuple(record for record in records.values()
                     if record.get("class") == "SkeletalMorphShaderPropMappingData")
    if not mappings:
        return ()
    data = _object(_object(raw.get("asset_prefix"), "asset_prefix").get("data"), "asset_prefix.data")
    parameters = tuple(_object(value, "shaderParams[]") for value in _array(data.get("shaderParams"), "shaderParams"))
    names = tuple(_text(value, "shaderParamNames[]") for value in _array(data.get("shaderParamNames"), "shaderParamNames"))
    if len(parameters) != len(names):
        raise CharacterExpressionTableError("Expression shader parameter arrays disagree")
    parameter_names: dict[str, str] = {}
    for parameter, name in zip(parameters, names, strict=True):
        rid = _decimal_id(parameter.get("rid"), "shaderParams[].rid")
        if rid in parameter_names:
            raise CharacterExpressionTableError(f"Duplicate expression shader parameter RID: rid={rid}")
        parameter_names[rid] = name
    controls = {_decimal_id(_object(value, "control").get("rid"), "control.rid"): _object(value, "control")
                for value in _array(document.get("control_index"), "control_index")}
    if len(controls) != len(_array(document.get("control_index"), "control_index")):
        raise CharacterExpressionTableError("Duplicate expression material control RID")
    result: list[ExpressionMaterialProperty] = []
    for mapping in mappings:
        rid = _decimal_id(mapping.get("rid"), "mapping.rid")
        payload = _object(mapping.get("data"), "material mapping.data")
        parameter_rid = _decimal_id(_object(payload.get("shaderParam"), "mapping.shaderParam").get("rid"), "mapping.shaderParam.rid")
        parameter = records.get(parameter_rid)
        if parameter is None or parameter_rid not in parameter_names:
            raise CharacterExpressionTableError(f"Material mapping has no exact shader parameter: rid={rid}, parameter_rid={parameter_rid}")
        if parameter.get("class") != "SkMorphShaderParamFloat":
            continue
        values = _object(parameter.get("data"), "float parameter.data")
        property_name = _text(values.get("_paramName"), "parameter._paramName")
        suffix = "_" + property_name
        source_name = parameter_names[parameter_rid]
        if not source_name.endswith(suffix) or len(source_name) <= len(suffix):
            raise CharacterExpressionTableError(f"Shader parameter name disagrees with its exact property: name={source_name}, property={property_name}")
        shader_name = "HGRP/" + source_name[:-len(suffix)]
        if shader_name != "HGRP/CharacterNPR_Skin" or property_name not in ("_EmotionIndex", "_EmotionBlend"):
            continue
        if (_integer(payload.get("partType"), "mapping.partType") != 32
                or _integer(payload.get("vectorIndex"), "mapping.vectorIndex") != 0
                or _array(payload.get("bones"), "mapping.bones")):
            raise CharacterExpressionTableError(f"Emotion float mapping has unsupported source layout: rid={rid}")
        value = values.get("defaultValue")
        if type(value) not in (float, int) or not math.isfinite(value):
            raise CharacterExpressionTableError(f"Material property default is not finite: property={property_name}")
        control = controls.get(rid)
        if control is None:
            raise CharacterExpressionTableError(f"Material mapping is absent from the control index: rid={rid}")
        index = _integer(control.get("index"), "control.index")
        mapping_names = _array(data.get("morphMappingNames"), "morphMappingNames")
        mapping_configs = _array(data.get("morphMappingCfgs"), "morphMappingCfgs")
        if (index < 0 or index >= len(mapping_names) or len(mapping_names) != len(mapping_configs)
                or control.get("name") != mapping_names[index]
                or control.get("class_name") != mapping.get("class")
                or _object(mapping_configs[index], "morphMappingCfgs[]").get("rid") != rid):
            raise CharacterExpressionTableError(f"Material control index disagrees with raw mapping: rid={rid}")
        result.append(ExpressionMaterialProperty(
            _integer(control.get("index"), "control.index"), _text(control.get("name"), "control.name"), rid,
            parameter_rid, shader_name, property_name, float(value),
            _integer(values.get("_rendererMask"), "parameter._rendererMask"),
            _integer(values.get("blendMode"), "parameter.blendMode")))
    return tuple(result)


@dataclass(frozen=True)
class ExpressionTableResult:
    binding: ExpressionTableBinding
    status: Literal["source_verified", "pending"]
    source_identity: ExpressionTableSourceIdentity | None
    controls: tuple[ExpressionControlSource, ...]
    endpoint_readiness: Literal[
        "source_pose_endpoint_candidates_available",
        "no_nonempty_supported_controls",
        "not_verified",
    ]
    pending: tuple[ExpressionTablePending, ...]


@dataclass(frozen=True)
class ExpressionTableCatalog:
    format: Literal["EndfieldExpressionTableCatalog/1"]
    template_id: str
    character_id: str
    template_source_sha256: str
    plan: ExpressionTablePlan
    tables: tuple[ExpressionTableResult, ...]
    pending: tuple[ExpressionTablePending, ...]


@dataclass(frozen=True)
class ExpressionTableBakeManifest:
    format: Literal["EndfieldExpressionTableBakeManifest/1"]
    character_id: str
    template_id: str
    family: Literal["facial", "ear"]
    source_document_path: str
    source_identity: ExpressionTableSourceIdentity
    endpoint_control_names: tuple[str, ...]
    empty_control_names: tuple[str, ...]
    unsupported_control_names: tuple[str, ...]


_REFERENCE_FAMILIES: tuple[tuple[str, Literal["facial", "ear"]], ...] = (
    ("facialMorphAvatarName", "facial"),
    ("earMorphAvatarName", "ear"),
)
_MAPPING_CLASS = "SkeletalMorphMappingData"


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CharacterExpressionTableError(f"Expected JSON object: field={context}")
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise CharacterExpressionTableError(f"Expected JSON array: field={context}")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CharacterExpressionTableError(f"Expected nonempty string: field={context}, value={value!r}")
    return value


def _optional_text(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _text(value, context)


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CharacterExpressionTableError(f"Expected exact integer: field={context}, value={value!r}")
    return value


def _decimal_id(value: object, context: str) -> str:
    if isinstance(value, bool):
        raise CharacterExpressionTableError(f"Expected decimal source identity: field={context}, value={value!r}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        return str(int(value, 10))
    raise CharacterExpressionTableError(f"Expected decimal source identity: field={context}, value={value!r}")


def _sha256(value: object, context: str) -> str:
    digest = _text(value, context)
    if re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        raise CharacterExpressionTableError(f"Expected SHA-256 hex digest: field={context}, value={digest!r}")
    return digest.lower()


def _normalize_resource_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    return "/".join(part for part in normalized.split("/") if part).casefold()


def _expected_resource_path(reference_value: str, family: Literal["facial", "ear"]) -> str | None:
    components = reference_value.split("/")
    expected_family = "facialmorph" if family == "facial" else "earmorph"
    if len(components) != 4 or components[0].casefold() != expected_family or components[1].casefold() != "avatar":
        return None
    actor_class, actor_name = components[2:]
    if re.fullmatch(r"[A-Za-z0-9_-]+", actor_class) is None or re.fullmatch(r"[A-Za-z0-9_-]+", actor_name) is None:
        return None
    morph_prefix = "facemorph" if family == "facial" else "earmorph"
    return (
        "assets/beyond/dynamicassets/gamedata/skeletalmorph/skeletalmorphcfg/"
        f"{actor_class.casefold()}/data_{morph_prefix}_avatar_{actor_name.casefold()}.asset"
    )


def _candidate_rows(resolution_document: object) -> tuple[dict[str, object], ...]:
    resolution = _object(resolution_document, "resource_resolution")
    if resolution.get("format") != "EndfieldExpressionResourceResolution/1":
        raise CharacterExpressionTableError("Unsupported expression resource-resolution format")
    return tuple(_object(value, f"resource_resolution.candidates[{index}]") for index, value in enumerate(
        _array(resolution.get("candidates"), "resource_resolution.candidates")))


def _avatar_identity(resolution: dict[str, object]) -> tuple[str, str]:
    avatar = _object(resolution.get("selected_animator_avatar"), "resource_resolution.selected_animator_avatar")
    if avatar.get("class_name") != "Avatar":
        raise CharacterExpressionTableError("Selected Animator identity is not an Avatar")
    return (
        _text(avatar.get("cab"), "selected_animator_avatar.cab"),
        _decimal_id(avatar.get("path_id"), "selected_animator_avatar.path_id"),
    )


def _candidate_matches_avatar(candidate: dict[str, object], avatar_cab: str) -> bool:
    serialized_rows = _array(candidate.get("serialized_files"), "candidate.serialized_files")
    external_cabs: set[str] = set()
    for index, value in enumerate(serialized_rows):
        serialized = _object(value, f"candidate.serialized_files[{index}]")
        external_cabs.update(
            _text(external, f"candidate.serialized_files[{index}].externals[]").casefold()
            for external in _array(serialized.get("externals"), f"candidate.serialized_files[{index}].externals")
        )
    return avatar_cab.casefold() in external_cabs


def _matching_candidates(candidates: tuple[dict[str, object], ...], resource_path: str, avatar_cab: str) -> tuple[dict[str, object], ...]:
    matches: list[dict[str, object]] = []
    wanted = _normalize_resource_path(resource_path)
    for candidate_index, candidate in enumerate(candidates):
        paths = _array(candidate.get("container_paths"), f"candidate[{candidate_index}].container_paths")
        if not any(_normalize_resource_path(_text(path, f"candidate[{candidate_index}].container_paths[]")) == wanted for path in paths):
            continue
        if not _candidate_matches_avatar(candidate, avatar_cab):
            raise CharacterExpressionTableError(
                f"Exact morph path candidate lacks the selected Avatar CAB external: path={resource_path}, provider={candidate.get('logical_name')}"
            )
        matches.append(candidate)
    return tuple(matches)


def plan_expression_tables(template: CharacterPrefabTemplate, resolution_document: object) -> ExpressionTablePlan:
    """Plan facial and ear sources from exact PrefabInfo references and provider evidence."""
    resolution = _object(resolution_document, "resource_resolution")
    if resolution.get("format") != "EndfieldExpressionResourceResolution/1":
        raise CharacterExpressionTableError("Unsupported expression resource-resolution format")
    scan = _object(resolution.get("index_scan"), "resource_resolution.index_scan")
    scan_status_value = scan.get("status")
    if scan_status_value not in ("complete", "incomplete"):
        raise CharacterExpressionTableError(f"Invalid provider-index scan status: {scan_status_value!r}")
    scan_status: Literal["complete", "incomplete"] = scan_status_value
    provider_index_sha256 = _optional_text(scan.get("provider_index_sha256"), "index_scan.provider_index_sha256")
    if provider_index_sha256 is not None:
        provider_index_sha256 = _sha256(provider_index_sha256, "index_scan.provider_index_sha256")
    candidates = _candidate_rows(resolution)
    avatar_cab, avatar_path_id = _avatar_identity(resolution)
    fields: dict[str, str | None] = {}
    for field in template.reference_fields:
        if field.source_field in fields:
            raise CharacterExpressionTableError(f"PrefabInfo contains a duplicate reference field: field={field.source_field}")
        fields[field.source_field] = field.value
    bindings: list[ExpressionTableBinding] = []
    bound_paths: set[str] = set()
    for reference_field, family in _REFERENCE_FAMILIES:
        reference_value = fields.get(reference_field)
        if reference_value is None or reference_value == "":
            bindings.append(ExpressionTableBinding(family, reference_field, None, None, None, None,
                                                   avatar_cab, avatar_path_id, "pending", "missing_prefab_reference"))
            continue
        resource_path = _expected_resource_path(reference_value, family)
        if resource_path is None:
            bindings.append(ExpressionTableBinding(family, reference_field, reference_value, None, None, None,
                                                   avatar_cab, avatar_path_id, "pending", "unsupported_prefab_reference_format"))
            continue
        bound_paths.add(_normalize_resource_path(resource_path))
        if scan_status != "complete":
            bindings.append(ExpressionTableBinding(family, reference_field, reference_value, resource_path, None, None,
                                                   avatar_cab, avatar_path_id, "pending", "provider_index_scan_incomplete"))
            continue
        matches = _matching_candidates(candidates, resource_path, avatar_cab)
        if not matches:
            bindings.append(ExpressionTableBinding(family, reference_field, reference_value, resource_path, None, None,
                                                   avatar_cab, avatar_path_id, "pending", "referenced_table_provider_missing"))
            continue
        if len(matches) != 1:
            bindings.append(ExpressionTableBinding(family, reference_field, reference_value, resource_path, None, None,
                                                   avatar_cab, avatar_path_id, "pending", "referenced_table_provider_ambiguous"))
            continue
        candidate = matches[0]
        bindings.append(ExpressionTableBinding(
            family,
            reference_field,
            reference_value,
            resource_path,
            _text(candidate.get("logical_name"), "candidate.logical_name"),
            _text(candidate.get("kind"), "candidate.kind"),
            avatar_cab,
            avatar_path_id,
            "selected",
            None,
        ))

    if scan_status == "complete":
        for candidate_index, candidate in enumerate(candidates):
            logical_name = _text(candidate.get("logical_name"), f"candidate[{candidate_index}].logical_name")
            for path_index, path_value in enumerate(_array(candidate.get("container_paths"), f"candidate[{candidate_index}].container_paths")):
                path = _text(path_value, f"candidate[{candidate_index}].container_paths[{path_index}]")
                if "/skeletalmorphcfg/" not in f"/{_normalize_resource_path(path)}/":
                    continue
                if _normalize_resource_path(path) in bound_paths:
                    continue
                bindings.append(ExpressionTableBinding("unknown", None, None, path, logical_name,
                                                       _optional_text(candidate.get("kind"), f"candidate[{candidate_index}].kind"),
                                                       avatar_cab, avatar_path_id, "pending", "unbound_morph_candidate"))

    return ExpressionTablePlan(
        "EndfieldExpressionTablePlan/1",
        template.template_id,
        template.corresponding_character_id,
        template.source.virtual_path,
        _sha256(template.source.sha256, "template.source.sha256"),
        provider_index_sha256,
        scan_status,
        tuple(bindings),
    )


def _source_identity(binding: ExpressionTableBinding, plan: ExpressionTablePlan, document: dict[str, object]) -> ExpressionTableSourceIdentity:
    if binding.status != "selected" or binding.resource_path is None or binding.provider_logical_name is None:
        raise CharacterExpressionTableError("Cannot validate source sidecar for a pending expression binding")
    source = _object(document.get("source"), "expression_source.source")
    actual_path = _text(source.get("container_path"), "expression_source.source.container_path")
    if _normalize_resource_path(actual_path) != _normalize_resource_path(binding.resource_path):
        raise CharacterExpressionTableError(f"Expression source path does not match its PrefabInfo reference: expected={binding.resource_path}, actual={actual_path}")
    provider_name = _text(source.get("provider_logical_name"), "expression_source.source.provider_logical_name")
    if provider_name.casefold() != binding.provider_logical_name.casefold():
        raise CharacterExpressionTableError(f"Expression provider differs from the indexed binding: expected={binding.provider_logical_name}, actual={provider_name}")
    avatar = _object(document.get("selected_animator_avatar"), "expression_source.selected_animator_avatar")
    avatar_cab = _text(avatar.get("cab"), "expression_source.selected_animator_avatar.cab")
    avatar_path_id = _decimal_id(avatar.get("path_id"), "expression_source.selected_animator_avatar.path_id")
    if avatar.get("class_name") != "Avatar" or avatar_cab.casefold() != (binding.avatar_cab or "").casefold() or avatar_path_id != binding.avatar_path_id:
        raise CharacterExpressionTableError("Expression source sidecar Avatar CAB/PathID does not match the selected Animator Avatar")
    selection = _object(document.get("selection"), "expression_source.selection")
    if selection.get("selected_asset_avatar_pointer_verified") is not True or selection.get("candidate_index_scan_complete") is not True:
        raise CharacterExpressionTableError("Expression source sidecar lacks complete provider scan and exact Avatar pointer verification")
    serialized_file = _text(source.get("serialized_file"), "expression_source.source.serialized_file")
    serialized_rows = _array(source.get("serialized_files"), "expression_source.source.serialized_files")
    serialized_cabs = tuple(
        _text(_object(row, f"expression_source.source.serialized_files[{index}]").get("cab"),
              f"expression_source.source.serialized_files[{index}].cab")
        for index, row in enumerate(serialized_rows)
    )
    if not any(cab.casefold() == serialized_file.casefold() for cab in serialized_cabs):
        raise CharacterExpressionTableError("Expression source serialized file is absent from its provider index identity")
    raw_object_base64 = _text(source.get("raw_object_bytes_base64"), "expression_source.source.raw_object_bytes_base64")
    try:
        raw_object = base64.b64decode(raw_object_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise CharacterExpressionTableError("Expression source raw object is not valid base64") from error
    raw_object_sha256 = hashlib.sha256(raw_object).hexdigest()
    declared_raw_hash = _sha256(source.get("raw_object_sha256"), "expression_source.source.raw_object_sha256")
    object_bytes = _integer(source.get("object_bytes"), "expression_source.source.object_bytes")
    if len(raw_object) != object_bytes or raw_object_sha256 != declared_raw_hash:
        raise CharacterExpressionTableError("Expression source raw object size/hash does not match its sidecar declaration")
    provenance = _object(document.get("provenance"), "expression_source.provenance")
    source_audit_sha256 = _sha256(provenance.get("source_audit_sha256"), "expression_source.provenance.source_audit_sha256")
    avatar_provenance = _object(document.get("avatar_binding_provenance"), "expression_source.avatar_binding_provenance")
    if avatar_provenance.get("avatar_cab", "").casefold() != avatar_cab.casefold() or _decimal_id(
        avatar_provenance.get("avatar_path_id"), "avatar_binding_provenance.avatar_path_id"
    ) != avatar_path_id:
        raise CharacterExpressionTableError("Expression Avatar binding provenance disagrees with the selected Avatar identity")
    if document.get("avatar_bone_bindings_status") not in (
        "exact_avatar_m_id_to_tos_paths_and_source_base_pose_verified",
        "exact_avatar_provider_not_found_in_complete_provider_index",
    ):
        raise CharacterExpressionTableError("Expression sidecar has an unsupported Avatar bone-binding status")
    serialized_files_text = f"{serialized_file}|{source.get('object_path_id')}|{source.get('object_class_id')}|{object_bytes}"
    identity_material = "|".join((
        plan.template_id,
        binding.reference_field or "",
        binding.reference_value or "",
        _normalize_resource_path(binding.resource_path),
        provider_name.casefold(),
        serialized_files_text,
        avatar_cab.casefold(),
        avatar_path_id,
        source_audit_sha256,
        declared_raw_hash,
    ))
    table_identity = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
    asset_prefix = _object(_object(document.get("raw"), "expression_source.raw").get("asset_prefix"), "expression_source.raw.asset_prefix")
    return ExpressionTableSourceIdentity(
        table_identity,
        _text(document.get("character_id"), "expression_source.character_id"),
        plan.template_id,
        binding.reference_field or "",
        binding.reference_value or "",
        binding.resource_path,
        provider_name,
        _text(source.get("provider_kind"), "expression_source.source.provider_kind"),
        serialized_file,
        _decimal_id(source.get("object_path_id"), "expression_source.source.object_path_id"),
        _integer(source.get("object_class_id"), "expression_source.source.object_class_id"),
        object_bytes,
        _sha256(source.get("bundle_sha256"), "expression_source.source.bundle_sha256"),
        declared_raw_hash,
        _sha256(document.get("decoded_source_canonical_sha256"), "expression_source.decoded_source_canonical_sha256"),
        avatar_cab,
        avatar_path_id,
        _text(asset_prefix.get("m_Name"), "expression_source.raw.asset_prefix.m_Name"),
    )


def _control_sources(document: dict[str, object]) -> tuple[tuple[ExpressionControlSource, ...], int]:
    raw = _object(document.get("raw"), "expression_source.raw")
    asset_prefix = _object(raw.get("asset_prefix"), "expression_source.raw.asset_prefix")
    data = _object(asset_prefix.get("data"), "expression_source.raw.asset_prefix.data")
    names = tuple(_text(value, f"morphMappingNames[{index}]") for index, value in enumerate(
        _array(data.get("morphMappingNames"), "asset_prefix.data.morphMappingNames")))
    configs = tuple(_object(value, f"morphMappingCfgs[{index}]") for index, value in enumerate(
        _array(data.get("morphMappingCfgs"), "asset_prefix.data.morphMappingCfgs")))
    rows = tuple(_object(value, f"control_index[{index}]") for index, value in enumerate(
        _array(document.get("control_index"), "expression_source.control_index")))
    managed = tuple(_object(value, f"managed_references[{index}]") for index, value in enumerate(
        _array(raw.get("managed_references"), "expression_source.raw.managed_references")))
    if len(names) != len(configs) or len(names) != len(rows):
        raise CharacterExpressionTableError(
            f"Expression control arrays disagree: names={len(names)}, configs={len(configs)}, index={len(rows)}"
        )
    by_rid: dict[str, dict[str, object]] = {}
    for index, record in enumerate(managed):
        rid = _decimal_id(record.get("rid"), f"managed_references[{index}].rid")
        if rid in by_rid:
            raise CharacterExpressionTableError(f"Duplicate expression managed-reference RID: rid={rid}")
        by_rid[rid] = record
    bone_bindings: dict[int, dict[str, object]] = {}
    for index, binding_value in enumerate(_array(document.get("avatar_bone_bindings"), "avatar_bone_bindings")):
        binding = _object(binding_value, f"avatar_bone_bindings[{index}]")
        bone_id = _integer(binding.get("bone_id"), f"avatar_bone_bindings[{index}].bone_id")
        if bone_id in bone_bindings:
            raise CharacterExpressionTableError(f"Duplicate exact Avatar bone binding: bone_id={bone_id}")
        bone_bindings[bone_id] = binding

    material_rids = frozenset(row.mapping_rid for row in read_expression_material_properties(document))
    controls: list[ExpressionControlSource] = []
    supported_count = 0
    for index, (name, config, row) in enumerate(zip(names, configs, rows, strict=True)):
        if _integer(row.get("index"), f"control_index[{index}].index") != index or _text(row.get("name"), f"control_index[{index}].name") != name:
            raise CharacterExpressionTableError(f"Expression control index/name differs from raw arrays: index={index}")
        config_rid = _decimal_id(config.get("rid"), f"morphMappingCfgs[{index}].rid")
        control_rid = _decimal_id(row.get("rid"), f"control_index[{index}].rid")
        if config_rid != control_rid:
            raise CharacterExpressionTableError(f"Expression control RID differs from raw mapping config: index={index}")
        record = by_rid.get(control_rid)
        if record is None:
            raise CharacterExpressionTableError(f"Expression control RID has no decoded managed reference: index={index}, rid={control_rid}")
        class_name = _text(row.get("class_name"), f"control_index[{index}].class_name")
        if _text(record.get("class"), f"managed reference class for {control_rid}") != class_name:
            raise CharacterExpressionTableError(f"Expression control class differs from its managed reference: index={index}")
        payload = _object(record.get("data"), f"managed_references[{control_rid}].data")
        if _integer(payload.get("id"), f"managed_references[{control_rid}].data.id") != index:
            raise CharacterExpressionTableError(f"Expression control ID differs from its raw array index: index={index}")
        if class_name != _MAPPING_CLASS:
            disposition = "material_property_control" if control_rid in material_rids else "unsupported_mapping_class"
            controls.append(ExpressionControlSource(index, name, control_rid, class_name, 0, disposition))
            continue
        bones = tuple(_object(value, f"managed_references[{control_rid}].data.bones[]") for value in _array(
            payload.get("bones"), f"managed_references[{control_rid}].data.bones"))
        if not bones:
            controls.append(ExpressionControlSource(index, name, control_rid, class_name, 0, "empty_mapping"))
            continue
        for bone_index, bone in enumerate(bones):
            bone_id = _integer(bone.get("boneID"), f"control[{index}].bones[{bone_index}].boneID")
            name_hash = _integer(bone.get("boneNameHash"), f"control[{index}].bones[{bone_index}].boneNameHash") & 0xFFFFFFFF
            binding = bone_bindings.get(bone_id)
            if binding is None:
                raise CharacterExpressionTableError(f"Expression control bone is absent from exact Avatar source bindings: index={index}, bone_id={bone_id}")
            if _integer(binding.get("bone_name_hash_unsigned"), f"Avatar binding[{bone_id}].bone_name_hash_unsigned") != name_hash:
                raise CharacterExpressionTableError(f"Expression control bone-name hash differs from exact Avatar binding: index={index}, bone_id={bone_id}")
        supported_count += 1
        controls.append(ExpressionControlSource(index, name, control_rid, class_name, len(bones), "source_pose_endpoint_candidate"))
    return tuple(controls), supported_count


def build_expression_table_catalog(
    template: CharacterPrefabTemplate,
    resolution_document: object,
    source_documents: tuple[object, ...],
) -> ExpressionTableCatalog:
    """Join verified source sidecars to the exact table plan and retain every partial."""
    plan = plan_expression_tables(template, resolution_document)
    documents = tuple(_object(value, f"expression source sidecars[{index}]") for index, value in enumerate(source_documents))
    sidecars_by_path: dict[str, list[dict[str, object]]] = {}
    for index, document in enumerate(documents):
        source = _object(document.get("source"), f"expression source sidecars[{index}].source")
        path = _text(source.get("container_path"), f"expression source sidecars[{index}].source.container_path")
        sidecars_by_path.setdefault(_normalize_resource_path(path), []).append(document)

    consumed_paths: set[str] = set()
    pending_all: list[ExpressionTablePending] = []
    table_results: list[ExpressionTableResult] = []
    for binding in plan.bindings:
        binding_pending: list[ExpressionTablePending] = []
        if binding.status == "pending":
            pending = ExpressionTablePending(
                binding.pending_code or "binding_pending",
                f"Expression table binding could not be selected: field={binding.reference_field}, value={binding.reference_value}",
                binding.resource_path,
                None,
            )
            pending_all.append(pending)
            table_results.append(ExpressionTableResult(binding, "pending", None, (), "not_verified", (pending,)))
            continue
        if binding.resource_path is None:
            raise CharacterExpressionTableError("Selected expression table binding has no resource path")
        normalized = _normalize_resource_path(binding.resource_path)
        candidates = sidecars_by_path.get(normalized, [])
        if not candidates:
            pending = ExpressionTablePending("missing_table_sidecar", "Selected provider candidate has no extracted source sidecar", binding.resource_path, None)
            pending_all.append(pending)
            table_results.append(ExpressionTableResult(binding, "pending", None, (), "not_verified", (pending,)))
            continue
        if len(candidates) != 1:
            pending = ExpressionTablePending("duplicate_source_sidecars", "Multiple source sidecars claim the same exact SkeletalMorph path", binding.resource_path, None)
            pending_all.append(pending)
            table_results.append(ExpressionTableResult(binding, "pending", None, (), "not_verified", (pending,)))
            consumed_paths.add(normalized)
            continue
        document = candidates[0]
        consumed_paths.add(normalized)
        if document.get("schema") != "EndfieldCharacterExpressionSource/2" or document.get("status") != "verified_raw_source_extracted":
            raise CharacterExpressionTableError(f"Expression source sidecar has an unsupported schema or status: path={binding.resource_path}")
        if _text(document.get("character_id"), "expression_source.character_id") != template.corresponding_character_id:
            raise CharacterExpressionTableError(f"Expression sidecar character differs from the selected prefab template: expected={template.corresponding_character_id}")
        source_identity = _source_identity(binding, plan, document)
        controls, supported_count = _control_sources(document)
        endpoint_readiness: Literal[
            "source_pose_endpoint_candidates_available",
            "no_nonempty_supported_controls",
            "not_verified",
        ] = "source_pose_endpoint_candidates_available" if supported_count else "no_nonempty_supported_controls"
        unsupported_controls = tuple(control for control in controls if control.endpoint_status == "unsupported_mapping_class")
        for control in unsupported_controls:
            pending_all.append(ExpressionTablePending(
                "unsupported_mapping_class",
                f"Control is preserved but its managed class has no editable pose-endpoint binder: {control.name} ({control.source_class})",
                binding.resource_path,
                None,
            ))
        table_results.append(ExpressionTableResult(binding, "source_verified", source_identity, controls,
                                                  endpoint_readiness, tuple(
                                                      ExpressionTablePending("unsupported_mapping_class",
                                                          f"Control is preserved but its managed class has no editable pose-endpoint binder: {control.name} ({control.source_class})",
                                                          binding.resource_path, None)
                                                      for control in unsupported_controls)))

    for normalized_path, unmatched_documents in sidecars_by_path.items():
        if normalized_path in consumed_paths:
            continue
        for document in unmatched_documents:
            source = _object(document.get("source"), "unbound expression sidecar.source")
            source_path = _text(source.get("container_path"), "unbound expression sidecar.source.container_path")
            pending_all.append(ExpressionTablePending("unbound_source_sidecar", "Source sidecar does not match any selected PrefabInfo SkeletalMorph reference", None, source_path))

    return ExpressionTableCatalog(
        "EndfieldExpressionTableCatalog/1",
        template.template_id,
        template.corresponding_character_id,
        _sha256(template.source.sha256, "template.source.sha256"),
        plan,
        tuple(table_results),
        tuple(pending_all),
    )


def make_expression_table_bake_manifest(
    result: ExpressionTableResult,
    source_document_path: str,
) -> ExpressionTableBakeManifest:
    """Create a strict endpoint-baker manifest from one verified source table."""
    if result.status != "source_verified" or result.source_identity is None:
        raise CharacterExpressionTableError("Cannot create an endpoint-baker manifest for an unverified expression table")
    family = result.binding.family
    if family not in ("facial", "ear"):
        raise CharacterExpressionTableError(f"Cannot bake an unbound expression family: family={family}")
    path = _text(source_document_path, "source_document_path")
    endpoint_names = tuple(control.name for control in result.controls
                           if control.endpoint_status == "source_pose_endpoint_candidate")
    empty_names = tuple(control.name for control in result.controls
                        if control.endpoint_status == "empty_mapping")
    unsupported_names = tuple(control.name for control in result.controls
                              if control.endpoint_status in ("unsupported_mapping_class", "material_property_control"))
    if len(set(endpoint_names + empty_names + unsupported_names)) != len(result.controls):
        raise CharacterExpressionTableError("Expression source controls contain duplicate names or an unknown endpoint status")
    return ExpressionTableBakeManifest(
        "EndfieldExpressionTableBakeManifest/1",
        result.source_identity.character_id,
        result.source_identity.template_id,
        family,
        path,
        result.source_identity,
        endpoint_names,
        empty_names,
        unsupported_names,
    )
