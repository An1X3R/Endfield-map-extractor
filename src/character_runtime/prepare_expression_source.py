"""Extract one Avatar-bound skeletal-morph asset into a portable research sidecar."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, Union


JsonValue: TypeAlias = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]
MAX_EXACT_JSON_INTEGER = 2**53 - 1
SCHEMA = "EndfieldCharacterExpressionSource/2"
MORPH_CONTAINER_MARKER = "/skeletalmorph/skeletalmorphcfg/"


@dataclass(frozen=True)
class AvatarIdentity:
    cab: str
    path_id: int
    name: str | None
    animator_rows: tuple[dict[str, JsonValue], ...]


@dataclass(frozen=True)
class ProviderCandidate:
    logical_name: str
    kind: str
    containers: tuple[str, ...]
    serialized_files: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class ProviderScan:
    lines: int
    provider_rows: int
    progress_rows: int
    failed_records: int
    complete: bool
    sha256: str
    candidates: tuple[ProviderCandidate, ...]
    avatar_providers: tuple[ProviderCandidate, ...]


def validate_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Nonfinite source scalar cannot be serialized losslessly.")
        return value
    if isinstance(value, list):
        return [validate_json(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: validate_json(item) for key, item in value.items()}
    raise TypeError(f"Unsupported decoded source value type: {type(value).__name__}.")


def read_json(path: Path) -> JsonValue:
    return validate_json(json.loads(path.read_text(encoding="utf-8-sig")))


def require_object(value: JsonValue, context: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object at {context}.")
    return value


def require_array(value: JsonValue, context: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise TypeError(f"Expected JSON array at {context}.")
    return value


def require_string(value: JsonValue, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Expected nonempty JSON string at {context}.")
    return value


def require_int(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Expected exact JSON integer at {context}.")
    return value


def canonical_hash(value: JsonValue) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def encode_integer_ids(value: JsonValue, pointer: str) -> tuple[JsonValue, tuple[str, ...]]:
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_EXACT_JSON_INTEGER:
        return str(value), (pointer,)
    if isinstance(value, list):
        children = tuple(encode_integer_ids(item, f"{pointer}/{index}") for index, item in enumerate(value))
        return [child[0] for child in children], tuple(path for child in children for path in child[1])
    if isinstance(value, dict):
        children = tuple((key, encode_integer_ids(item, f"{pointer}/{escape_pointer(key)}")) for key, item in value.items())
        return {key: child[0] for key, child in children}, tuple(path for _, child in children for path in child[1])
    return value, ()


def restore_integer_ids(value: JsonValue, pointer: str, integer_paths: frozenset[str]) -> JsonValue:
    if pointer in integer_paths:
        if not isinstance(value, str) or not value or not value.lstrip("-").isdigit():
            raise ValueError(f"Invalid decimal integer identity at {pointer}.")
        return int(value)
    if isinstance(value, list):
        return [restore_integer_ids(item, f"{pointer}/{index}", integer_paths) for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {key: restore_integer_ids(item, f"{pointer}/{escape_pointer(key)}", integer_paths) for key, item in value.items()}
    return value


def normalize_resource_path(value: str) -> str:
    return value.replace("\\", "/").strip("/")


def read_avatar_identities(source_audit: JsonValue) -> tuple[AvatarIdentity, ...]:
    audit = require_object(source_audit, "source audit")
    animator_values = require_array(audit.get("animators"), "source audit.animators")
    grouped: dict[tuple[str, int], list[dict[str, JsonValue]]] = {}
    names: dict[tuple[str, int], str | None] = {}
    for index, item in enumerate(animator_values):
        animator = require_object(item, f"source audit.animators[{index}]")
        object_identity = require_object(animator.get("object"), f"source audit.animators[{index}].object")
        avatar = require_object(animator.get("avatar"), f"source audit.animators[{index}].avatar")
        resolved_value = avatar.get("resolved")
        if resolved_value is None:
            continue
        resolved = require_object(resolved_value, f"source audit.animators[{index}].avatar.resolved")
        avatar_type = require_string(resolved.get("type"), f"source audit.animators[{index}].avatar.resolved.type")
        if avatar_type != "Avatar":
            raise ValueError(f"Animator resolved Avatar has unexpected class: animator_index={index}, type={avatar_type}.")
        cab = require_string(resolved.get("source"), f"source audit.animators[{index}].avatar.resolved.source")
        path_id = require_int(resolved.get("path_id"), f"source audit.animators[{index}].avatar.resolved.path_id")
        object_source = require_string(object_identity.get("source"), f"source audit.animators[{index}].object.source")
        object_path_id = require_int(object_identity.get("path_id"), f"source audit.animators[{index}].object.path_id")
        key = (cab, path_id)
        grouped.setdefault(key, []).append({
            "source": object_source,
            "path_id": str(object_path_id),
            "name": object_identity.get("name"),
            "avatar_file_id": require_int(avatar.get("file_id"), f"source audit.animators[{index}].avatar.file_id"),
        })
        avatar_name = resolved.get("name")
        names[key] = avatar_name if isinstance(avatar_name, str) else None
    return tuple(
        AvatarIdentity(cab, path_id, names[(cab, path_id)], tuple(rows))
        for (cab, path_id), rows in sorted(grouped.items())
    )


def select_avatar(identities: tuple[AvatarIdentity, ...], avatar_cab: str | None, avatar_path_id: int | None) -> AvatarIdentity | None:
    if (avatar_cab is None) != (avatar_path_id is None):
        raise ValueError("--avatar-cab and --avatar-path-id must be supplied together.")
    if avatar_cab is None:
        return identities[0] if len(identities) == 1 else None
    matches = tuple(identity for identity in identities if identity.cab.casefold() == avatar_cab.casefold() and identity.path_id == avatar_path_id)
    if len(matches) != 1:
        raise ValueError(f"Selected Avatar is not a unique resolved Animator Avatar: cab={avatar_cab}, path_id={avatar_path_id}, matches={len(matches)}.")
    return matches[0]


def read_provider_row(value: JsonValue, line_number: int, avatar_cab: str) -> tuple[ProviderCandidate | None, ProviderCandidate | None]:
    row = require_object(value, f"provider index line {line_number}")
    logical_name = require_string(row.get("logical_name"), f"provider index line {line_number}.logical_name")
    kind = require_string(row.get("kind"), f"provider index line {line_number}.kind")
    container_rows = tuple(require_string(item, f"provider index line {line_number}.containers") for item in require_array(row.get("containers"), f"provider index line {line_number}.containers"))
    serialized: list[tuple[str, tuple[str, ...]]] = []
    avatar_external_match = False
    avatar_serialized_match = False
    for file_index, file_value in enumerate(require_array(row.get("serialized"), f"provider index line {line_number}.serialized")):
        file_record = require_object(file_value, f"provider index line {line_number}.serialized[{file_index}]")
        cab = require_string(file_record.get("name"), f"provider index line {line_number}.serialized[{file_index}].name")
        external_rows = tuple(require_string(item, f"provider index line {line_number}.serialized[{file_index}].externals") for item in require_array(file_record.get("externals"), f"provider index line {line_number}.serialized[{file_index}].externals"))
        avatar_external_match = avatar_external_match or any(external.casefold() == avatar_cab.casefold() for external in external_rows)
        avatar_serialized_match = avatar_serialized_match or cab.casefold() == avatar_cab.casefold()
        serialized.append((cab, external_rows))
    morph_family = any(MORPH_CONTAINER_MARKER in f"/{normalize_resource_path(path).casefold()}/" for path in container_rows)
    normalized_containers = tuple(normalize_resource_path(path) for path in container_rows)
    candidate = ProviderCandidate(logical_name, kind, normalized_containers, tuple(serialized))
    morph_candidate = candidate if avatar_external_match and morph_family else None
    avatar_provider = candidate if avatar_serialized_match else None
    return morph_candidate, avatar_provider


def scan_provider_index(path: Path, avatar_cab: str) -> ProviderScan:
    digest = hashlib.sha256()
    candidates: list[ProviderCandidate] = []
    line_count = 0
    provider_row_count = 0
    progress_row_count = 0
    morph_candidates: list[ProviderCandidate] = []
    avatar_providers: list[ProviderCandidate] = []
    footer_failure_count: int | None = None
    footer_match_count: int | None = None
    with path.open("rb") as source:
        for line_count, raw_line in enumerate(source, start=1):
            digest.update(raw_line)
            line = raw_line.decode("utf-8").strip()
            if not line:
                raise ValueError(f"Provider index has an empty JSONL row: line={line_count}.")
            parsed = validate_json(json.loads(line))
            row = require_object(parsed, f"provider index line {line_count}")
            kind = require_string(row.get("kind"), f"provider index line {line_count}.kind")
            if footer_failure_count is not None:
                raise ValueError(f"Provider index contains a record after its completion footer: line={line_count}.")
            if kind == "progress":
                require_int(row.get("scanned"), f"provider index line {line_count}.scanned")
                require_int(row.get("failed"), f"provider index line {line_count}.failed")
                require_int(row.get("matches"), f"provider index line {line_count}.matches")
                require_string(row.get("utc"), f"provider index line {line_count}.utc")
                progress_row_count += 1
                continue
            if kind == "complete":
                require_int(row.get("scanned"), f"provider index line {line_count}.scanned")
                footer_failure_count = require_int(row.get("failed"), f"provider index line {line_count}.failed")
                footer_match_count = require_int(row.get("matches"), f"provider index line {line_count}.matches")
                require_string(row.get("utc"), f"provider index line {line_count}.utc")
                continue
            if kind != "match":
                raise ValueError(f"Provider index has an unsupported record kind; scan completeness is unknown: line={line_count}, kind={kind}.")
            provider_row_count += 1
            morph_candidate, avatar_provider = read_provider_row(parsed, line_count, avatar_cab)
            if morph_candidate is not None:
                morph_candidates.append(morph_candidate)
            if avatar_provider is not None:
                avatar_providers.append(avatar_provider)
    morph_identities = tuple((row.logical_name.casefold(), tuple(path.casefold() for path in row.containers)) for row in morph_candidates)
    if len(set(morph_identities)) != len(morph_identities):
        raise ValueError("Provider index contains duplicate selected morph provider identities; discovery cannot be considered unambiguous.")
    avatar_identities = tuple(row.logical_name.casefold() for row in avatar_providers)
    if len(set(avatar_identities)) != len(avatar_identities):
        raise ValueError("Provider index contains duplicate exact Avatar serialized-file providers; discovery cannot be considered unambiguous.")
    complete = footer_failure_count == 0 and footer_match_count == provider_row_count
    failed_records = footer_failure_count if footer_failure_count is not None else -1
    return ProviderScan(line_count, provider_row_count, progress_row_count, failed_records, complete, digest.hexdigest(), tuple(morph_candidates), tuple(avatar_providers))


def candidate_json(candidate: ProviderCandidate, candidate_basis: str) -> JsonValue:
    return {
        "logical_name": candidate.logical_name,
        "kind": candidate.kind,
        "container_paths": list(candidate.containers),
        "serialized_files": [
            {"cab": cab, "externals": list(externals)}
            for cab, externals in candidate.serialized_files
        ],
        "candidate_basis": candidate_basis,
    }


def serialize_avatar(identity: AvatarIdentity) -> JsonValue:
    return {
        "cab": identity.cab,
        "path_id": str(identity.path_id),
        "class_name": "Avatar",
        "name": identity.name,
        "matching_animators": list(identity.animator_rows),
    }


def resolution_document(status: str, source_audit_sha256: str, avatar: AvatarIdentity | None, scan: ProviderScan | None, selected: ProviderCandidate | None, selected_avatar_provider: ProviderCandidate | None) -> JsonValue:
    return {
        "format": "EndfieldExpressionResourceResolution/1",
        "status": status,
        "source_audit_sha256": source_audit_sha256,
        "index_scan": {
            "status": ("complete" if scan.complete else "incomplete") if scan is not None else "unknown",
            "jsonl_lines_scanned": scan.lines if scan is not None else None,
            "provider_rows_scanned": scan.provider_rows if scan is not None else None,
            "progress_records_skipped": scan.progress_rows if scan is not None else None,
            "failed_records_reported": scan.failed_records if scan is not None else None,
            "provider_index_sha256": scan.sha256 if scan is not None else None,
            "candidate_basis": "Provider index externals plus SkeletalMorph configuration container family; final object-level Avatar PPtr must match both selected Avatar CAB and PathID.",
            "avatar_provider_candidate_basis": "Provider serialized-file CAB equals the exact Animator Avatar CAB; object PathID and m_ID/m_TOS table are checked after the one selected provider is read.",
        },
        "selected_animator_avatar": serialize_avatar(avatar) if avatar is not None else None,
        "candidate_count": len(scan.candidates) if scan is not None else None,
        "candidates": [candidate_json(candidate, "Exact Avatar CAB appears in provider externals and container path belongs to the SkeletalMorph configuration family; object-level Avatar PathID is verified after extraction.") for candidate in scan.candidates] if scan is not None else [],
        "selected_candidate": candidate_json(selected, "Explicitly selected SkeletalMorph provider candidate.") if selected is not None else None,
        "avatar_provider_candidate_count": len(scan.avatar_providers) if scan is not None else None,
        "avatar_provider_candidates": [candidate_json(candidate, "Provider serialized CAB exactly matches the selected Animator Avatar CAB; Avatar PathID is verified after extraction.") for candidate in scan.avatar_providers] if scan is not None else [],
        "selected_avatar_provider": candidate_json(selected_avatar_provider, "Explicitly selected exact Avatar serialized-file provider.") if selected_avatar_provider is not None else None,
    }


def select_candidate(scan: ProviderScan, resource_path: str, logical_name: str | None) -> ProviderCandidate:
    normalized = normalize_resource_path(resource_path).casefold()
    matches = tuple(
        candidate for candidate in scan.candidates
        if any(path.casefold() == normalized for path in candidate.containers)
        and (logical_name is None or candidate.logical_name.casefold() == logical_name.casefold())
    )
    if not matches:
        raise ValueError(f"Explicit morph selection is not an Avatar-CAB candidate in the complete provider index: resource_path={resource_path}, logical_name={logical_name}.")
    if len(matches) != 1:
        raise ValueError(f"Explicit morph selection is still ambiguous; pass --logical-name: resource_path={resource_path}, matches={len(matches)}.")
    return matches[0]


def select_avatar_provider(scan: ProviderScan, logical_name: str | None) -> ProviderCandidate | None:
    if not scan.avatar_providers:
        if logical_name is not None:
            raise ValueError(f"Explicit Avatar provider was not found in the complete index: logical_name={logical_name}.")
        return None
    if logical_name is None:
        if len(scan.avatar_providers) != 1:
            return None
        return scan.avatar_providers[0]
    matches = tuple(provider for provider in scan.avatar_providers if provider.logical_name.casefold() == logical_name.casefold())
    if len(matches) != 1:
        raise ValueError(f"Explicit Avatar provider is not unique: logical_name={logical_name}, matches={len(matches)}.")
    return matches[0]


def extraction_target(root: Path, logical_name: str) -> Path:
    category, separator, relative_name = logical_name.partition("/")
    if not separator or not category or not relative_name or Path(relative_name).is_absolute() or ".." in Path(relative_name).parts:
        raise ValueError(f"Provider logical_name is not a safe bundle-relative path: {logical_name}.")
    return root / category / relative_name


def read_probe_output(probe_dir: Path) -> tuple[JsonValue, JsonValue, JsonValue, JsonValue, bytes]:
    prefix = require_object(read_json(probe_dir / "prefix.json"), "probe prefix")
    registry = require_array(read_json(probe_dir / "managed_references.json"), "probe registry")
    types = read_json(probe_dir / "ref_types.json")
    decode_receipt = require_object(read_json(probe_dir / "registry_decode_receipt.json"), "probe registry receipt")
    probe_receipt = require_object(read_json(probe_dir / "probe_receipt.json"), "probe receipt")
    raw_object = (probe_dir / "morph_asset.raw").read_bytes()
    return prefix, registry, types, {"decode": decode_receipt, "probe": probe_receipt}, raw_object


def build_control_index(prefix: JsonValue, registry: list[JsonValue]) -> tuple[list[JsonValue], int, int, int]:
    root = require_object(prefix, "asset prefix")
    data = require_object(root.get("data"), "asset prefix.data")
    names = require_array(data.get("morphMappingNames"), "asset prefix.data.morphMappingNames")
    references = require_array(data.get("morphMappingCfgs"), "asset prefix.data.morphMappingCfgs")
    if len(names) != len(references):
        raise ValueError(f"Control names and RID references have different lengths: names={len(names)}, references={len(references)}.")
    records_by_id: dict[int, tuple[int, dict[str, JsonValue]]] = {}
    for record_index, value in enumerate(registry):
        record = require_object(value, f"managed references[{record_index}]")
        rid = require_int(record.get("rid"), f"managed references[{record_index}].rid")
        if rid in records_by_id:
            raise ValueError(f"Managed reference RID is duplicated: {rid}.")
        records_by_id[rid] = (record_index, record)
    controls: list[JsonValue] = []
    for index, (name_value, reference_value) in enumerate(zip(names, references, strict=True)):
        name = require_string(name_value, f"morphMappingNames[{index}]")
        reference = require_object(reference_value, f"morphMappingCfgs[{index}]")
        rid = require_int(reference.get("rid"), f"morphMappingCfgs[{index}].rid")
        if rid not in records_by_id:
            raise ValueError(f"Control RID has no decoded registry record: index={index}, rid={rid}.")
        record_index, record = records_by_id[rid]
        payload = require_object(record.get("data"), f"managed references[{record_index}].data")
        payload_id = require_int(payload.get("id"), f"managed references[{record_index}].data.id")
        if payload_id != index:
            raise ValueError(f"Control index does not match decoded mapping payload: index={index}, payload_id={payload_id}, rid={rid}.")
        controls.append({
            "index": index,
            "name": name,
            "rid": str(rid),
            "class_name": require_string(record.get("class"), f"managed references[{record_index}].class"),
            "raw_payload_pointer": f"/raw/managed_references/{record_index}",
        })
    base_pose = require_array(data.get("basePoseConfig"), "asset prefix.data.basePoseConfig")
    bone_names = require_array(data.get("allBoneNames"), "asset prefix.data.allBoneNames")
    return controls, len(base_pose), len(bone_names), len(registry)


def build_avatar_bone_bindings(prefix: JsonValue, avatar: AvatarIdentity, avatar_probe: JsonValue) -> tuple[list[JsonValue], JsonValue, int]:
    probe = require_object(avatar_probe, "exact Avatar node map")
    if probe.get("format") != "EndfieldAvatarExactNodeMap/1" or probe.get("status") != "exact_avatar_m_id_to_tos_paths_decoded":
        raise ValueError("Exact Avatar node map has an unsupported or incomplete status.")
    if probe.get("serialized_file") != avatar.cab or require_int(probe.get("avatar_path_id"), "Avatar node map.avatar_path_id") != avatar.path_id:
        raise ValueError("Exact Avatar node map does not match the selected Animator Avatar CAB and PathID.")
    node_values = require_array(probe.get("nodes"), "Avatar node map.nodes")
    nodes_by_index: dict[int, dict[str, JsonValue]] = {}
    for node_index, node_value in enumerate(node_values):
        node = require_object(node_value, f"Avatar node map.nodes[{node_index}]")
        index = require_int(node.get("index"), f"Avatar node map.nodes[{node_index}].index")
        if index in nodes_by_index:
            raise ValueError(f"Avatar skeleton index is duplicated: {index}.")
        nodes_by_index[index] = node

    data = require_object(require_object(prefix, "asset prefix").get("data"), "asset prefix.data")
    base_pose = require_array(data.get("basePoseConfig"), "asset prefix.data.basePoseConfig")
    all_bone_names = require_array(data.get("allBoneNames"), "asset prefix.data.allBoneNames")
    if len(base_pose) != len(all_bone_names):
        raise ValueError(f"Source basePoseConfig/allBoneNames row counts differ: base_pose={len(base_pose)}, names={len(all_bone_names)}.")
    bindings: list[JsonValue] = []
    seen_bone_ids: set[int] = set()
    for row_index, (base_value, name_value) in enumerate(zip(base_pose, all_bone_names, strict=True)):
        base = require_object(base_value, f"basePoseConfig[{row_index}]")
        name = require_string(name_value, f"allBoneNames[{row_index}]")
        bone_id = require_int(base.get("boneID"), f"basePoseConfig[{row_index}].boneID")
        name_hash = require_int(base.get("boneNameHash"), f"basePoseConfig[{row_index}].boneNameHash")
        if bone_id in seen_bone_ids:
            raise ValueError(f"Source basePoseConfig boneID is duplicated: bone_id={bone_id}.")
        seen_bone_ids.add(bone_id)
        if bone_id not in nodes_by_index:
            raise ValueError(f"Source boneID is outside exact Avatar m_ID array: row={row_index}, bone_id={bone_id}, avatar_nodes={len(nodes_by_index)}.")
        node = nodes_by_index[bone_id]
        path = require_string(node.get("avatar_tos_path"), f"Avatar node map index {bone_id}.avatar_tos_path")
        terminal = require_string(node.get("terminal_name"), f"Avatar node map index {bone_id}.terminal_name")
        if path.rsplit("/", maxsplit=1)[-1] != terminal or terminal != name:
            raise ValueError(f"Exact Avatar.m_TOS terminal does not match allBoneNames: bone_id={bone_id}, expected={name}, path={path}.")
        unsigned_hash = name_hash & 0xFFFFFFFF
        crc_matches = (zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF) == unsigned_hash
        if not crc_matches:
            raise ValueError(f"Source boneNameHash does not equal UTF-8 CRC32 of its exact Avatar terminal name: row={row_index}, name={name}, hash={name_hash}.")
        bindings.append({
            "base_pose_row": row_index,
            "bone_id": bone_id,
            "all_bone_name": name,
            "bone_name_hash_signed": name_hash,
            "bone_name_hash_unsigned": unsigned_hash,
            "utf8_name_crc32_matches": True,
            "avatar_skeleton_id": require_int(node.get("avatar_skeleton_id"), f"Avatar node map index {bone_id}.avatar_skeleton_id"),
            "avatar_tos_path": path,
            "terminal_name": terminal,
            "avatar_skeleton_name_id": require_int(node.get("skeleton_name_id"), f"Avatar node map index {bone_id}.skeleton_name_id"),
        })
    return bindings, probe, len(nodes_by_index)


def build_document(
    character_id: str,
    source_audit_sha256: str,
    avatar: AvatarIdentity,
    scan: ProviderScan,
    candidate: ProviderCandidate,
    avatar_provider: ProviderCandidate | None,
    avatar_probe: JsonValue | None,
    avatar_bundle_sha256: str | None,
    avatar_extractor_receipt: JsonValue | None,
    raw_probe: tuple[JsonValue, JsonValue, JsonValue, JsonValue, bytes],
    extractor_receipt: JsonValue,
    bundle_sha256: str,
    probe_assembly_sha256: str,
    probe_source_sha256: str,
    registry_decoder_source_sha256: str,
    resources_source_sha256: str,
    unity_version: str,
) -> tuple[JsonValue, JsonValue]:
    prefix, registry, types, receipts, raw_object = raw_probe
    decode_receipt = require_object(receipts.get("decode"), "probe receipts.decode")
    probe_receipt = require_object(receipts.get("probe"), "probe receipts.probe")
    if decode_receipt.get("status") != "entire_registry_tail_decoded" or decode_receipt.get("remaining_bytes") != 0:
        raise ValueError("Complete source sidecar requires exact registry tail consumption.")
    if probe_receipt.get("status") != "exact_avatar_pointer_and_entire_registry_decoded":
        raise ValueError("Source Morph MonoBehaviour does not have a confirmed exact Avatar pointer receipt.")
    if probe_receipt.get("avatar_cab") != avatar.cab or probe_receipt.get("avatar_path_id") != avatar.path_id:
        raise ValueError("Decoded morph asset Avatar PPtr does not match the selected Animator Avatar identity.")
    if probe_receipt.get("logical_name") != candidate.logical_name:
        raise ValueError("Probe receipt logical name differs from the selected provider candidate.")

    controls, base_pose_count, bone_name_count, registry_count = build_control_index(prefix, registry)
    if avatar_provider is not None and avatar_probe is not None and avatar_bundle_sha256 is not None and avatar_extractor_receipt is not None:
        avatar_bone_bindings, avatar_node_map, avatar_node_count = build_avatar_bone_bindings(prefix, avatar, avatar_probe)
        avatar_bone_binding_status = "exact_avatar_m_id_to_tos_paths_and_source_base_pose_verified"
    elif avatar_provider is None and avatar_probe is None and avatar_bundle_sha256 is None and avatar_extractor_receipt is None:
        avatar_bone_bindings = []
        avatar_node_map = None
        avatar_node_count = 0
        avatar_bone_binding_status = "exact_avatar_provider_not_found_in_complete_provider_index"
    else:
        raise ValueError("Avatar provider, node map, and bundle hash must all be present together.")
    raw_decoded: JsonValue = {
        "asset_prefix": prefix,
        "managed_references": registry,
        "referenced_type_trees": types,
    }
    encoded_raw, integer_paths = encode_integer_ids(raw_decoded, "")
    row = next((item for item in scan.candidates if item.logical_name == candidate.logical_name and item.containers == candidate.containers), None)
    if row is None:
        raise ValueError("Selected provider candidate disappeared from the completed index scan.")
    serialized = [
        {"cab": cab, "externals": list(externals)}
        for cab, externals in candidate.serialized_files
    ]
    raw_source_canonical = {
        "asset_prefix": prefix,
        "managed_references": registry,
        "referenced_type_trees": types,
    }
    document: JsonValue = {
        "schema": SCHEMA,
        "status": "verified_raw_source_extracted",
        "character_id": character_id,
        "source": {
            "container_path": next(path for path in candidate.containers if path.casefold() == normalize_resource_path(require_string(require_object(extractor_receipt, "extractor receipt").get("selected_resource_path"), "extractor receipt.selected_resource_path")).casefold()),
            "provider_logical_name": candidate.logical_name,
            "provider_kind": candidate.kind,
            "serialized_files": serialized,
            "serialized_file": require_string(probe_receipt.get("serialized_file"), "probe receipt.serialized_file"),
            "object_path_id": str(require_int(probe_receipt.get("object_path_id"), "probe receipt.object_path_id")),
            "object_class_id": require_int(probe_receipt.get("object_class_id"), "probe receipt.object_class_id"),
            "object_bytes": require_int(probe_receipt.get("object_bytes"), "probe receipt.object_bytes"),
            "raw_object_bytes_base64": base64.b64encode(raw_object).decode("ascii"),
            "bundle_sha256": bundle_sha256,
            "raw_object_sha256": hashlib.sha256(raw_object).hexdigest(),
        },
        "selected_animator_avatar": serialize_avatar(avatar),
        "selection": {
            "binding_rule": "Selected asset must reference the exact resolved Animator Avatar CAB and PathID; name similarity is not a binding rule.",
            "candidate_count_for_avatar": len(scan.candidates),
            "avatar_provider_candidate_count": len(scan.avatar_providers),
            "candidate_index_scan_complete": True,
            "selected_asset_avatar_pointer_verified": True,
            "other_compatible_assets_are_not_selected_or_decoded": True,
        },
        "resource_resolution": {
            "provider_index_jsonl_lines_scanned": scan.lines,
            "provider_rows_scanned": scan.provider_rows,
            "progress_records_skipped": scan.progress_rows,
            "failed_records_reported": scan.failed_records,
            "provider_index_sha256": scan.sha256,
            "candidate_basis": "Avatar CAB external reference and SkeletalMorph configuration container family; exact Avatar PathID checked in the decoded object.",
            "avatar_candidates": [candidate_json(item, "Exact Avatar CAB appears in provider externals and SkeletalMorph config container family.") for item in scan.candidates],
            "avatar_bundle_candidates": [candidate_json(item, "Provider serialized CAB exactly matches the selected Animator Avatar CAB.") for item in scan.avatar_providers],
        },
        "provenance": {
            "source_audit_sha256": source_audit_sha256,
            "provider_index_scan": "complete",
            "resource_database_access": "read-only via extractor_core.resources.extract_candidate_bundles",
            "extractor_resources_py_sha256": resources_source_sha256,
            "extractor_receipt": extractor_receipt,
            "unity_version": unity_version,
            "reader_assembly_sha256": probe_assembly_sha256,
            "reader_probe_source_sha256": probe_source_sha256,
            "managed_registry_decoder_source_sha256": registry_decoder_source_sha256,
            "managed_registry_version": decode_receipt.get("registry_version"),
            "avatar_provider_logical_name": avatar_provider.logical_name if avatar_provider is not None else None,
            "avatar_bundle_sha256": avatar_bundle_sha256,
            "avatar_extractor_receipt": avatar_extractor_receipt,
            "avatar_reader_source_sha256": probe_source_sha256 if avatar_provider is not None else None,
        },
        "raw": encoded_raw,
        "numeric_encoding": {
            "scope": "/raw",
            "decimal_integer_json_pointers": list(integer_paths),
            "reason": "Preserve signed identifiers outside the exact IEEE-754 binary64 integer range.",
        },
        "control_index": controls,
        "avatar_bone_bindings_status": avatar_bone_binding_status,
        "avatar_bone_bindings": avatar_bone_bindings,
        "avatar_node_map": avatar_node_map,
        "avatar_binding_provenance": {
            "provider_logical_name": avatar_provider.logical_name if avatar_provider is not None else None,
            "bundle_sha256": avatar_bundle_sha256,
            "extractor_receipt": avatar_extractor_receipt,
            "avatar_cab": avatar.cab,
            "avatar_path_id": str(avatar.path_id),
            "mapping_rule": "basePoseConfig.boneID indexes Avatar.m_Avatar.m_AvatarSkeleton.m_ID; the uint ID resolves through Avatar.m_TOS to the exact full transform path.",
            "all_bone_names_terminal_and_crc32_checked": avatar_bone_binding_status == "exact_avatar_m_id_to_tos_paths_and_source_base_pose_verified",
        },
        "source_counts": {
            "base_pose_entries": base_pose_count,
            "all_bone_name_entries": bone_name_count,
            "mapping_controls": len(controls),
            "managed_reference_records": registry_count,
            "avatar_skeleton_node_count": avatar_node_count,
        },
        "interpretation_boundary": {
            "raw_prefix_and_reference_payloads": "Preserved decoded values; full raw serialized object bytes are included as base64 for unknown fields and exact source evidence.",
            "avatar_binding": "Exact CAB and PathID closed against source_audit Animator and source asset PPtr.",
            "bone_name_to_current_blender_rig_mapping": "not_computed_by_this_extractor",
            "unity_source_trs_to_blender_pose_basis": "not_computed_by_this_extractor",
            "morph_runtime_route_and_weight_rules": "unverified",
            "vertex_shape_data": "not_included; this sidecar contains the skeletal morph source asset only",
            "provider_reader_scope": "One explicitly selected provider bundle is opened; external CAB bundles are not loaded as a dependency closure.",
        },
        "decoded_source_canonical_sha256": canonical_hash(raw_source_canonical),
    }
    receipt: JsonValue = {
        "status": "source_avatar_binding_and_roundtrip_passed",
        "artifact": "expression_source.json",
        "schema": SCHEMA,
        "source_bundle_sha256": bundle_sha256,
        "candidate_index_scan_complete": True,
        "candidate_count_for_avatar": len(scan.candidates),
        "selected_asset_avatar_pointer_verified": True,
        "managed_reference_count": registry_count,
        "control_count": len(controls),
        "base_pose_entry_count": base_pose_count,
        "large_integer_locations_encoded": len(integer_paths),
        "full_source_object_bytes_preserved": True,
        "decoded_source_canonical_sha256": canonical_hash(raw_source_canonical),
        "source_audit_avatar_match_count": len(avatar.animator_rows),
        "avatar_bone_binding_status": avatar_bone_binding_status,
        "avatar_bone_binding_count": len(avatar_bone_bindings),
        "avatar_node_count": avatar_node_count,
        "unverified_runtime_and_coordinate_rules_remain_unasserted": True,
    }
    return document, receipt


def write_json(path: Path, value: JsonValue) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def remove_owned_work_dir(output_dir: Path, work_dir: Path) -> None:
    output_resolved = output_dir.resolve()
    work_resolved = work_dir.resolve()
    expected_work = output_resolved / "private_work"
    if work_resolved != expected_work or output_resolved not in work_resolved.parents:
        raise ValueError(f"Refusing to remove work directory outside the requested output: output={output_resolved}, work={work_resolved}.")
    if not work_resolved.is_dir():
        raise FileNotFoundError(f"Expected completed private work directory is missing: {work_resolved}.")
    shutil.rmtree(work_resolved)


def verify_sidecar(path: Path, raw_source_canonical: JsonValue, encoded_paths: tuple[str, ...]) -> JsonValue:
    reopened = require_object(read_json(path), "reopened source sidecar")
    raw = require_object(reopened.get("raw"), "reopened source sidecar.raw")
    restored = restore_integer_ids(validate_json(raw), "", frozenset(encoded_paths))
    if restored != raw_source_canonical or canonical_hash(restored) != canonical_hash(raw_source_canonical):
        raise AssertionError("Portable decoded source values failed exact integer restoration and canonical-hash round trip.")
    client_document = validate_json(json.loads(path.read_text(encoding="utf-8"), parse_int=float))
    client = require_object(client_document, "binary64 client sidecar")
    client_raw = require_object(client.get("raw"), "binary64 client sidecar.raw")
    client_restored = restore_integer_ids(validate_json(client_raw), "", frozenset(encoded_paths))
    if client_restored != raw_source_canonical:
        raise AssertionError("Binary64-parsing JSON client changed a preserved source numeric value.")
    text = path.read_text(encoding="utf-8")
    if re.search(r"[A-Za-z]:[\\/]", text):
        raise AssertionError("Portable source sidecar contains an absolute host path.")
    return {
        "status": "decoded_source_roundtrip_and_binary64_client_passed",
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "artifact_bytes": path.stat().st_size,
        "decoded_source_canonical_sha256": canonical_hash(raw_source_canonical),
        "large_integer_locations_encoded": len(encoded_paths),
        "absolute_host_paths_in_artifact": False,
    }


def import_extractor(extractor_src: Path) -> tuple[object, Path]:
    source_root = extractor_src.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Extractor source directory is missing: {source_root}.")
    sys.path.insert(0, str(source_root))
    module = importlib.import_module("extractor_core.resources")
    module_file = Path(require_string(str(getattr(module, "__file__", "")), "extractor_core.resources.__file__"))
    return module, module_file


def run(args: argparse.Namespace) -> int:
    output_dir: Path = args.output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"Refusing to reuse output directory: {output_dir}.")
    output_dir.mkdir(parents=True)
    temporary_root = Path(args.temp_root).resolve()
    temporary_root.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(temporary_root)
    os.environ["TMP"] = str(temporary_root)
    source_audit_path: Path = args.source_audit.resolve()
    source_audit_bytes = source_audit_path.read_bytes()
    source_audit_sha256 = hashlib.sha256(source_audit_bytes).hexdigest()
    source_audit = validate_json(json.loads(source_audit_bytes.decode("utf-8-sig")))
    avatar_identities = read_avatar_identities(source_audit)
    avatar = select_avatar(avatar_identities, args.avatar_cab, args.avatar_path_id)
    if avatar is None:
        status = "avatar_selection_required" if avatar_identities else "no_resolved_animator_avatar"
        report = resolution_document(status, source_audit_sha256, None, None, None, None)
        write_json(output_dir / "resource_resolution.json", report)
        print(json.dumps(report, ensure_ascii=False))
        return 0

    provider_index: Path = args.provider_index.resolve()
    if not provider_index.is_file():
        report = resolution_document("provider_index_unavailable_candidates_unknown", source_audit_sha256, avatar, None, None, None)
        write_json(output_dir / "resource_resolution.json", report)
        print(json.dumps(report, ensure_ascii=False))
        return 0

    scan = scan_provider_index(provider_index, avatar.cab)
    if not scan.complete:
        report = resolution_document("provider_index_scan_incomplete_candidates_unknown", source_audit_sha256, avatar, scan, None, None)
        write_json(output_dir / "resource_resolution.json", report)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if not scan.candidates:
        report = resolution_document("no_asset_candidate_in_complete_provider_index", source_audit_sha256, avatar, scan, None, None)
        write_json(output_dir / "resource_resolution.json", report)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if args.resource_path is None:
        report = resolution_document("asset_selection_required", source_audit_sha256, avatar, scan, None, None)
        write_json(output_dir / "resource_resolution.json", report)
        print(json.dumps(report, ensure_ascii=False))
        return 0

    candidate = select_candidate(scan, args.resource_path, args.logical_name)
    avatar_provider = select_avatar_provider(scan, args.avatar_logical_name)
    if scan.avatar_providers and avatar_provider is None:
        report = resolution_document("avatar_provider_selection_required", source_audit_sha256, avatar, scan, candidate, None)
        write_json(output_dir / "resource_resolution.json", report)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    write_json(output_dir / "resource_resolution.json", resolution_document("selected_provider_candidates_pending_exact_object_checks", source_audit_sha256, avatar, scan, candidate, avatar_provider))
    if len(candidate.containers) != 1 or normalize_resource_path(args.resource_path).casefold() not in tuple(path.casefold() for path in candidate.containers):
        raise ValueError(f"Selected provider row must map to exactly the requested resource container: logical_name={candidate.logical_name}, containers={candidate.containers}.")

    extractor_module, resources_source_path = import_extractor(args.extractor_src)
    extractor = getattr(extractor_module, "extract_candidate_bundles", None)
    if not callable(extractor):
        raise TypeError("extractor_core.resources does not expose callable extract_candidate_bundles.")
    if not args.resource_database.is_file():
        raise FileNotFoundError(f"Read-only resource database is missing: {args.resource_database}.")
    if not args.game_vfs.is_dir():
        raise FileNotFoundError(f"Game VFS directory is missing: {args.game_vfs}.")

    work_dir = output_dir / "private_work"
    work_dir.mkdir()
    extracted_root = work_dir / "candidate_bundle"
    extractor_receipt_value = extractor(args.resource_database, args.game_vfs, [candidate.logical_name], extracted_root)
    extractor_receipt_object = require_object(validate_json(extractor_receipt_value), "extractor receipt")
    bundle_path = extraction_target(extracted_root, candidate.logical_name)
    if not bundle_path.is_file():
        raise FileNotFoundError(f"Selected provider bundle was not written by the resource resolver: {candidate.logical_name}.")
    bundle_sha256 = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    probe_assembly = args.expression_reader.resolve(strict=True)
    if not probe_assembly.is_file():
        raise FileNotFoundError(f"Expression source reader assembly is not built: {probe_assembly}.")
    reader_sources = args.extractor_src.resolve().parent / "dotnet" / "CharacterTools" / "ExpressionSourceProbe"
    probe_source = reader_sources / "Program.cs"
    decoder_source = reader_sources / "RegistryDecoder.cs"
    probe_manifest = work_dir / "probe_input.json"
    selected_serialized_cabs = tuple(cab for cab, _ in candidate.serialized_files)
    if len(selected_serialized_cabs) != 1:
        raise ValueError(f"Selected provider row has ambiguous serialized CAB list: logical_name={candidate.logical_name}, cabs={selected_serialized_cabs}.")
    probe_input: JsonValue = {
        "format": "EndfieldExpressionAssetProbeInput/1",
        "bundle_path": str(bundle_path),
        "bundle_sha256": bundle_sha256,
        "logical_name": candidate.logical_name,
        "container_path": normalize_resource_path(args.resource_path),
        "serialized_file": selected_serialized_cabs[0],
        "avatar_cab": avatar.cab,
        "avatar_path_id": avatar.path_id,
        "unity_version": args.unity_version,
    }
    write_json(probe_manifest, probe_input)
    probe_dir = work_dir / "probe_output"
    environment = os.environ.copy()
    environment["TEMP"] = str(temporary_root)
    environment["TMP"] = str(temporary_root)
    result = subprocess.run(
        [str(args.dotnet), str(probe_assembly), "--input", str(probe_manifest), "--output", str(probe_dir)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Expression source reader failed with exit_code={result.returncode}: stdout={result.stdout[-4000:]}, stderr={result.stderr[-4000:]}.")
    raw_probe = read_probe_output(probe_dir)
    extractor_receipt_object["selected_resource_path"] = normalize_resource_path(args.resource_path)
    extractor_receipt_object["bundle_bytes_sha256"] = bundle_sha256
    avatar_probe: JsonValue | None = None
    avatar_bundle_sha256: str | None = None
    avatar_extractor_receipt: JsonValue | None = None
    if avatar_provider is not None:
        avatar_provider_cabs = tuple(cab for cab, _ in avatar_provider.serialized_files if cab.casefold() == avatar.cab.casefold())
        if len(avatar_provider_cabs) != 1:
            raise ValueError(f"Selected Avatar provider does not contain exactly one serialized file matching its CAB: logical_name={avatar_provider.logical_name}, matching_cabs={avatar_provider_cabs}.")
        avatar_extracted_root = work_dir / "avatar_bundle"
        avatar_extractor_receipt_value = extractor(args.resource_database, args.game_vfs, [avatar_provider.logical_name], avatar_extracted_root)
        avatar_extractor_receipt = require_object(validate_json(avatar_extractor_receipt_value), "Avatar extractor receipt")
        avatar_bundle_path = extraction_target(avatar_extracted_root, avatar_provider.logical_name)
        if not avatar_bundle_path.is_file():
            raise FileNotFoundError(f"Selected Avatar provider bundle was not written by the resource resolver: {avatar_provider.logical_name}.")
        avatar_bundle_sha256 = hashlib.sha256(avatar_bundle_path.read_bytes()).hexdigest()
        avatar_probe_manifest = work_dir / "avatar_probe_input.json"
        avatar_probe_input: JsonValue = {
            "format": "EndfieldAvatarNodeProbeInput/1",
            "bundle_path": str(avatar_bundle_path),
            "bundle_sha256": avatar_bundle_sha256,
            "logical_name": avatar_provider.logical_name,
            "serialized_file": avatar.cab,
            "avatar_path_id": avatar.path_id,
            "unity_version": args.unity_version,
        }
        write_json(avatar_probe_manifest, avatar_probe_input)
        avatar_probe_dir = work_dir / "avatar_probe_output"
        avatar_result = subprocess.run(
            [str(args.dotnet), str(probe_assembly), "--input", str(avatar_probe_manifest), "--output", str(avatar_probe_dir)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        if avatar_result.returncode != 0:
            raise RuntimeError(f"Exact Avatar node reader failed with exit_code={avatar_result.returncode}: stdout={avatar_result.stdout[-4000:]}, stderr={avatar_result.stderr[-4000:]}.")
        avatar_probe = require_object(read_json(avatar_probe_dir / "avatar_node_map.json"), "exact Avatar node map")
        avatar_extractor_receipt["selected_avatar_cab"] = avatar.cab
        avatar_extractor_receipt["selected_avatar_path_id"] = str(avatar.path_id)
        avatar_extractor_receipt["bundle_sha256"] = avatar_bundle_sha256
    raw_source_canonical: JsonValue = {
        "asset_prefix": raw_probe[0],
        "managed_references": raw_probe[1],
        "referenced_type_trees": raw_probe[2],
    }
    _, integer_paths = encode_integer_ids(raw_source_canonical, "")
    document, package_receipt = build_document(
        args.character_id,
        source_audit_sha256,
        avatar,
        scan,
        candidate,
        avatar_provider,
        avatar_probe,
        avatar_bundle_sha256,
        avatar_extractor_receipt,
        raw_probe,
        extractor_receipt_object,
        bundle_sha256,
        hashlib.sha256(probe_assembly.read_bytes()).hexdigest(),
        hashlib.sha256(probe_source.read_bytes()).hexdigest(),
        hashlib.sha256(decoder_source.read_bytes()).hexdigest(),
        hashlib.sha256(resources_source_path.read_bytes()).hexdigest(),
        args.unity_version,
    )
    output_path = output_dir / "expression_source.json"
    write_json(output_path, document)
    package_receipt_object = require_object(package_receipt, "package receipt")
    package_receipt_object.update(require_object(verify_sidecar(output_path, raw_source_canonical, integer_paths), "verification receipt"))
    write_json(output_dir / "verification.json", package_receipt)
    write_json(output_dir / "resource_resolution.json", resolution_document("verified_exact_avatar_asset_and_bone_map_extracted", source_audit_sha256, avatar, scan, candidate, avatar_provider))
    remove_owned_work_dir(output_dir, work_dir)
    print(json.dumps(package_receipt, ensure_ascii=False))
    return 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract one selected SkeletalMorph source asset bound to an explicit Animator Avatar.")
    parser.add_argument("--character-id", required=True)
    parser.add_argument("--source-audit", required=True, type=Path)
    parser.add_argument("--provider-index", required=True, type=Path)
    parser.add_argument("--resource-database", required=True, type=Path)
    parser.add_argument("--game-vfs", required=True, type=Path)
    parser.add_argument("--extractor-src", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--temp-root", required=True, type=Path)
    parser.add_argument("--unity-version", required=True)
    parser.add_argument("--expression-reader", required=True, type=Path)
    parser.add_argument("--dotnet", required=True, type=Path)
    parser.add_argument("--resource-path")
    parser.add_argument("--logical-name")
    parser.add_argument("--avatar-logical-name")
    parser.add_argument("--avatar-cab")
    parser.add_argument("--avatar-path-id", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        return run(args)
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
