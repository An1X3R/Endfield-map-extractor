"""Catalog localized character identities and exact prefab provider bindings.

The catalog records table and prefab relationships only. Provider presence does
not prove that a prefab's geometry, rig, or animation has been decoded.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import time
from collections.abc import Callable
from typing import Literal

from extractor_core import vfs
from extractor_core.resources import load_group
from extractor_core.scene_tables import TableValue, read_scene_table
from endfield_character_cache import (
    CharacterCacheError,
    CharacterDataCache,
    CharacterSourceChangedError,
    CharacterSourceFingerprint,
    source_stamp,
)


class CharacterCatalogError(ValueError):
    """Required character identity, prefab, or provider evidence is invalid."""


@dataclass(frozen=True)
class CharacterIdentity:
    character_id: str
    name: str
    english_name: str
    name_id: str


@dataclass(frozen=True)
class CharacterTableSource:
    virtual_path: str
    sha256: str
    rows: int


@dataclass(frozen=True)
class CharacterPrefabInfoSource:
    virtual_path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class CharacterProviderIndexSource:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class CharacterSerializedCab:
    name: str
    externals: tuple[str, ...]


@dataclass(frozen=True)
class CharacterResourceProvider:
    logical_name: str
    serialized_cabs: tuple[CharacterSerializedCab, ...]


@dataclass(frozen=True)
class CharacterPrefabVariant:
    variant: Literal["characters", "npc"]
    resource_path: str
    provider_status: Literal["found", "not_found"]
    providers: tuple[CharacterResourceProvider, ...]


@dataclass(frozen=True)
class CharacterPrefabPart:
    part_name: str
    variants: tuple[CharacterPrefabVariant, ...]


@dataclass(frozen=True)
class CharacterPrefabReferenceField:
    source_field: str
    value: str | None


@dataclass(frozen=True)
class CharacterPrefabTemplate:
    template_id: str
    corresponding_character_id: str
    source: CharacterPrefabInfoSource
    part_name_id_list: tuple[str, ...]
    reference_fields: tuple[CharacterPrefabReferenceField, ...]
    parts: tuple[CharacterPrefabPart, ...]
    geometry_status: Literal["not_verified"]


@dataclass(frozen=True)
class CharacterModelBinding:
    character_id: str
    status: Literal["bound", "no_template"]
    templates: tuple[CharacterPrefabTemplate, ...]
    geometry_status: Literal["not_verified", "not_available"]


@dataclass(frozen=True)
class CharacterModelSummary:
    bound_character_count: int
    no_template_character_count: int
    template_count: int
    part_count: int
    variant_count: int
    provider_match_count: int
    geometry_verification: Literal["not_performed"]


@dataclass(frozen=True)
class CharacterCatalog:
    format: str
    source_identity: str
    characters: tuple[CharacterIdentity, ...]
    sources: tuple[CharacterTableSource, ...]
    provider_index_source: CharacterProviderIndexSource
    model_bindings: tuple[CharacterModelBinding, ...]
    model_summary: CharacterModelSummary


@dataclass(frozen=True)
class CharacterCatalogRunReceipt:
    format: str
    status: Literal["written"]
    started_utc: str
    elapsed_seconds: float
    output_path: str
    output_sha256: str
    provider_index_path: str
    provider_index_sha256: str
    character_count: int
    bound_character_count: int
    no_template_character_count: int
    geometry_verification: Literal["not_performed"]


@dataclass(frozen=True)
class _PrefabSourceRecord:
    template_id: str
    corresponding_character_id: str
    source: CharacterPrefabInfoSource
    part_names: tuple[str, ...]
    reference_fields: tuple[CharacterPrefabReferenceField, ...]


_TABLE_GROUP = "42A8FCA6"
_JSON_GROUP = "775A31D1"
_VFS_KEY = bytes.fromhex(vfs.DEFAULT_VFS_KEY_HEX)
_REFERENCE_FIELDS = (
    "abilityTempletName",
    "avatarMeshName",
    "avatarTempletName",
    "cpuAnimationTempletName",
    "earMorphAvatarName",
    "facialMorphAvatarName",
)
_CATALOG_CACHE_SCHEMA = "character-catalog-cache/2"
_PROVIDER_FINGERPRINT_SCHEMA = "character-provider-jsonl/1"
_VFS_HEADER_FINGERPRINT_SCHEMA = "character-vfs-group-header/1"


def _never_cancelled() -> None:
    return None


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CharacterCatalogError(f"Expected source object with string keys: field={context}")
    return value


def _table_object(value: TableValue, context: str) -> dict[str, TableValue]:
    if not isinstance(value, dict):
        raise CharacterCatalogError(f"Expected character source object: field={context}")
    return value


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CharacterCatalogError(f"Expected nonempty source string: field={context}, value={value!r}")
    return value


def _optional_source_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CharacterCatalogError(f"Expected optional source string: field={context}, value={value!r}")
    return value


def _virtual_file_index(group: vfs.VfsGroup, group_hash: str) -> dict[str, vfs.VfsFile]:
    indexed: dict[str, vfs.VfsFile] = {}
    for item in group.iter_files():
        key = item.file_name.casefold()
        if key in indexed:
            raise CharacterCatalogError(
                f"VFS group contains duplicate case-insensitive file paths: group={group_hash}, path={item.file_name}"
            )
        indexed[key] = item
    return indexed


def _read_virtual_payload(
    vfs_root: Path,
    group_hash: str,
    group: vfs.VfsGroup,
    files: dict[str, vfs.VfsFile],
    virtual_path: str,
) -> bytes:
    item = files.get(virtual_path.casefold())
    if item is None:
        raise FileNotFoundError(f"Required virtual file is absent: group={group_hash}, path={virtual_path}")
    chunk_path = vfs_root / group_hash / f"{item.file_chunk_md5_name}.chk"
    if not chunk_path.is_file():
        raise FileNotFoundError(f"VFS chunk is missing: path={chunk_path}, virtual_file={virtual_path}")
    output = BytesIO()
    with chunk_path.open("rb") as source:
        digest = vfs.copy_virtual_file(source, output, item, group.version, _VFS_KEY)
    if digest.upper() != item.file_data_md5.upper():
        raise CharacterCatalogError(
            f"VFS payload checksum mismatch: path={virtual_path}, expected={item.file_data_md5}, actual={digest}"
        )
    return output.getvalue()


def assemble_character_identities(
    characters: dict[str, TableValue], texts: dict[str, TableValue],
) -> tuple[CharacterIdentity, ...]:
    """Join exact source IDs to Chinese text; retain distinct IDs with equal names."""
    identities: list[CharacterIdentity] = []
    for key, value in sorted(characters.items()):
        row = _table_object(value, f"CharacterTable[{key}]")
        character_id = _string(row.get("charId"), f"CharacterTable[{key}].charId")
        if key != character_id:
            raise CharacterCatalogError(
                f"Character identity disagrees with table key: key={key}, charId={character_id}"
            )
        localized = _table_object(row.get("name"), f"CharacterTable[{key}].name")
        identifier = localized.get("id")
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            raise CharacterCatalogError(f"Invalid character localization ID: character={key}, id={identifier!r}")
        name = _string(texts.get(str(identifier)), f"I18nTextTable_CN[{identifier}] for {key}")
        english_name = _string(row.get("engName"), f"CharacterTable[{key}].engName")
        identities.append(CharacterIdentity(character_id, name, english_name, str(identifier)))
    if not identities:
        raise CharacterCatalogError("CharacterTable contains no character identities")
    return tuple(identities)


def _safe_component(value: object, context: str) -> str:
    component = _string(value, context)
    if component in (".", "..") or "/" in component or "\\" in component:
        raise CharacterCatalogError(f"Expected a single prefab part identifier: field={context}, value={component!r}")
    return component


def _read_prefab_info(
    vfs_root: Path,
    json_group: vfs.VfsGroup,
    json_files: dict[str, vfs.VfsFile],
    template_id: str,
    corresponding_character_id: str,
) -> _PrefabSourceRecord:
    if template_id in (".", "..") or "/" in template_id or "\\" in template_id:
        raise CharacterCatalogError(f"Prefab template ID is not a safe virtual path component: {template_id!r}")
    virtual_path = f"Data/Json/NPC/PrefabInfo/{template_id}.json"
    payload = _read_virtual_payload(vfs_root, _JSON_GROUP, json_group, json_files, virtual_path)
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CharacterCatalogError(f"Invalid PrefabInfo JSON: path={virtual_path}, error={error}") from error
    source = _object(parsed, virtual_path)
    actual_template_id = _string(source.get("id"), f"{virtual_path}.id")
    if actual_template_id != template_id:
        raise CharacterCatalogError(
            f"PrefabInfo template identity mismatch: path={virtual_path}, expected={template_id}, actual={actual_template_id}"
        )
    _validate_prefab_character_binding(source, corresponding_character_id, virtual_path)
    raw_parts = source.get("partNameIdList")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise CharacterCatalogError(f"PrefabInfo partNameIdList must be a nonempty array: path={virtual_path}")
    part_names = tuple(
        _safe_component(part, f"{virtual_path}.partNameIdList[{index}]")
        for index, part in enumerate(raw_parts)
    )
    if len(set(part_names)) != len(part_names):
        raise CharacterCatalogError(f"PrefabInfo repeats a part identifier: path={virtual_path}")
    references: list[CharacterPrefabReferenceField] = []
    for field_name in _REFERENCE_FIELDS:
        if field_name in source:
            references.append(
                CharacterPrefabReferenceField(
                    field_name,
                    _optional_source_string(source[field_name], f"{virtual_path}.{field_name}"),
                )
            )
    return _PrefabSourceRecord(
        template_id,
        corresponding_character_id,
        CharacterPrefabInfoSource(virtual_path, sha256(payload).hexdigest(), len(payload)),
        part_names,
        tuple(references),
    )


def _validate_prefab_character_binding(source: dict[str, object], character_id: str, virtual_path: str) -> None:
    """Validate the optional prefab duplicate of the authoritative character-table binding."""
    if "correspondingCharId" not in source:
        return
    actual = _string(source["correspondingCharId"], f"{virtual_path}.correspondingCharId")
    if actual != character_id:
        raise CharacterCatalogError(
            f"PrefabInfo character binding mismatch: path={virtual_path}, expected={character_id}, actual={actual}"
        )


def _normalize_container_path(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _provider_details(
    provider_index: Path,
    requested_paths: frozenset[str],
    check_cancelled: Callable[[], None],
) -> tuple[dict[str, tuple[CharacterResourceProvider, ...]], CharacterProviderIndexSource]:
    providers_by_path: dict[str, dict[str, set[CharacterSerializedCab]]] = {
        path: {} for path in requested_paths
    }
    digest = sha256()
    total_bytes = 0
    last_record: dict[str, object] = {}
    try:
        stream = provider_index.open("rb")
    except OSError as error:
        raise OSError(f"Cannot open provider index: path={provider_index}: {error}") from error
    with stream:
        for line_number, raw_line in enumerate(stream, 1):
            if line_number % 1024 == 0:
                check_cancelled()
            digest.update(raw_line)
            total_bytes += len(raw_line)
            try:
                decoded = raw_line.decode("utf-8-sig")
                parsed = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CharacterCatalogError(
                    f"Invalid provider index JSONL record: path={provider_index}, line={line_number}, error={error}"
                ) from error
            row = _object(parsed, f"provider index line {line_number}")
            last_record = row
            if row.get("kind") != "match":
                continue
            logical_name = _string(row.get("logical_name"), f"provider index line {line_number}.logical_name")
            containers = row.get("containers")
            if not isinstance(containers, list):
                raise CharacterCatalogError(
                    f"Provider containers must be an array: path={provider_index}, line={line_number}"
                )
            matching_paths: set[str] = set()
            for container in containers:
                container_path = _normalize_container_path(
                    _string(container, f"provider index line {line_number}.containers[]")
                )
                if container_path in requested_paths:
                    matching_paths.add(container_path)
            if not matching_paths:
                continue
            serialized = row.get("serialized")
            if not isinstance(serialized, list):
                raise CharacterCatalogError(
                    f"Provider serialized CAB records must be an array: path={provider_index}, line={line_number}"
                )
            cab_records: set[CharacterSerializedCab] = set()
            for cab_index, cab_value in enumerate(serialized):
                cab = _object(cab_value, f"provider index line {line_number}.serialized[{cab_index}]")
                cab_name = _string(cab.get("name"), f"provider index line {line_number}.serialized[{cab_index}].name")
                raw_externals = cab.get("externals")
                if not isinstance(raw_externals, list):
                    raise CharacterCatalogError(
                        f"Provider CAB externals must be an array: path={provider_index}, line={line_number}, cab={cab_name}"
                    )
                externals = tuple(sorted({
                    _string(external, f"provider index line {line_number}.serialized[{cab_index}].externals[]")
                    for external in raw_externals
                }))
                cab_records.add(CharacterSerializedCab(cab_name, externals))
            normalized_provider = logical_name.casefold()
            for container_path in matching_paths:
                providers_by_path[container_path].setdefault(normalized_provider, set()).update(cab_records)
    failed = last_record.get("failed")
    if last_record.get("kind") != "complete" or type(failed) is not int or failed != 0:
        raise CharacterCatalogError(
            f"Provider index is incomplete or has failed records: path={provider_index}, terminal={last_record}"
        )
    frozen: dict[str, tuple[CharacterResourceProvider, ...]] = {}
    for path, providers in providers_by_path.items():
        frozen[path] = tuple(
            CharacterResourceProvider(
                logical_name,
                tuple(sorted(cabs, key=lambda cab: (cab.name, cab.externals))),
            )
            for logical_name, cabs in sorted(providers.items())
        )
    return frozen, CharacterProviderIndexSource(str(provider_index), digest.hexdigest(), total_bytes)


def _prefab_path(variant: Literal["characters", "npc"], part_name: str) -> str:
    filename = part_name if part_name.casefold().endswith(".prefab") else f"{part_name}.prefab"
    return f"assets/beyond/dynamicassets/gameplay/actors/postmodels/{variant}/{filename}"


def _model_bindings(
    identities: tuple[CharacterIdentity, ...],
    mappings: dict[str, list[_PrefabSourceRecord]],
    providers: dict[str, tuple[CharacterResourceProvider, ...]],
) -> tuple[CharacterModelBinding, ...]:
    bindings: list[CharacterModelBinding] = []
    for identity in identities:
        templates: list[CharacterPrefabTemplate] = []
        for record in sorted(mappings.get(identity.character_id, []), key=lambda entry: entry.template_id):
            parts: list[CharacterPrefabPart] = []
            for part_name in record.part_names:
                variants: list[CharacterPrefabVariant] = []
                for variant in ("characters", "npc"):
                    resource_path = _prefab_path(variant, part_name)
                    matches = providers[_normalize_container_path(resource_path)]
                    variants.append(
                        CharacterPrefabVariant(
                            variant,
                            resource_path,
                            "found" if matches else "not_found",
                            matches,
                        )
                    )
                parts.append(CharacterPrefabPart(part_name, tuple(variants)))
            templates.append(
                CharacterPrefabTemplate(
                    record.template_id,
                    record.corresponding_character_id,
                    record.source,
                    record.part_names,
                    record.reference_fields,
                    tuple(parts),
                    "not_verified",
                )
            )
        status: Literal["bound", "no_template"] = "bound" if templates else "no_template"
        geometry_status: Literal["not_verified", "not_available"] = (
            "not_verified" if templates else "not_available"
        )
        bindings.append(CharacterModelBinding(identity.character_id, status, tuple(templates), geometry_status))
    return tuple(bindings)


def _summary(bindings: tuple[CharacterModelBinding, ...]) -> CharacterModelSummary:
    bound = tuple(binding for binding in bindings if binding.status == "bound")
    unbound = tuple(binding for binding in bindings if binding.status == "no_template")
    templates = tuple(template for binding in bound for template in binding.templates)
    parts = tuple(part for template in templates for part in template.parts)
    variants = tuple(variant for part in parts for variant in part.variants)
    providers = tuple(provider for variant in variants for provider in variant.providers)
    return CharacterModelSummary(
        len(bound),
        len(unbound),
        len(templates),
        len(parts),
        len(variants),
        len(providers),
        "not_performed",
    )


def _catalog_source_paths(game: Path, vfs_root: Path, provider_path: Path) -> tuple[Path, ...]:
    sources: list[Path] = [game / "Endfield.exe"]
    for group_hash in (_TABLE_GROUP, _JSON_GROUP):
        group_dir = vfs_root / group_hash
        catalog_path = group_dir / f"{group_hash}.blc"
        chunks = tuple(sorted(group_dir.glob("*.chk"), key=lambda path: path.name.casefold()))
        if not catalog_path.is_file() or not chunks:
            raise FileNotFoundError(
                f"Character catalog VFS group is incomplete: group={group_hash}, directory={group_dir}"
            )
        sources.extend((catalog_path, *chunks))
    sources.append(provider_path)
    return tuple(sources)


def _catalog_inputs(game_root: Path, provider_index: Path) -> tuple[Path, Path, Path, tuple[Path, ...]]:
    game = game_root.expanduser().resolve()
    vfs_root = game / "Endfield_Data" / "StreamingAssets" / "VFS"
    if not (game / "Endfield.exe").is_file():
        raise FileNotFoundError(f"Character catalog requires the directory containing Endfield.exe: path={game}")
    if not vfs_root.is_dir():
        raise FileNotFoundError(f"Character catalog VFS directory is missing: path={vfs_root}")
    provider_path = provider_index.expanduser().resolve()
    if not provider_path.is_file():
        raise FileNotFoundError(f"Character catalog provider index is missing: path={provider_path}")
    return game, vfs_root, provider_path, _catalog_source_paths(game, vfs_root, provider_path)


def _read_character_catalog_sources(
    vfs_root: Path, provider_path: Path, check_cancelled: Callable[[], None],
) -> CharacterCatalog:
    table_group = load_group(vfs_root, _TABLE_GROUP)
    table_files = _virtual_file_index(table_group, _TABLE_GROUP)
    table_names = ("CharacterTable", "I18nTextTable_CN", "NpcPrefabCorrespondingCharTable")
    decoded: dict[str, dict[str, TableValue]] = {}
    sources: list[CharacterTableSource] = []
    for table_name in table_names:
        check_cancelled()
        virtual_path = f"Data/TableCfg/{table_name}.bytes"
        payload = _read_virtual_payload(vfs_root, _TABLE_GROUP, table_group, table_files, virtual_path)
        values = read_scene_table(payload, table_name)
        decoded[table_name] = values
        sources.append(CharacterTableSource(virtual_path, sha256(payload).hexdigest(), len(values)))

    identities = assemble_character_identities(decoded["CharacterTable"], decoded["I18nTextTable_CN"])
    known_character_ids = {identity.character_id for identity in identities}
    json_group = load_group(vfs_root, _JSON_GROUP)
    json_files = _virtual_file_index(json_group, _JSON_GROUP)
    prefab_records: dict[str, list[_PrefabSourceRecord]] = {}
    prefab_sources: list[CharacterPrefabInfoSource] = []
    for key, value in sorted(decoded["NpcPrefabCorrespondingCharTable"].items()):
        check_cancelled()
        row = _table_object(value, f"NpcPrefabCorrespondingCharTable[{key}]")
        template_id = _string(row.get("templateId"), f"NpcPrefabCorrespondingCharTable[{key}].templateId")
        corresponding_character_id = _string(
            row.get("correspondingCharId"),
            f"NpcPrefabCorrespondingCharTable[{key}].correspondingCharId",
        )
        if key != template_id:
            raise CharacterCatalogError(
                f"Prefab table key disagrees with templateId: key={key}, templateId={template_id}"
            )
        if corresponding_character_id not in known_character_ids:
            raise CharacterCatalogError(
                f"Prefab template references an unknown character: template={template_id}, "
                f"character={corresponding_character_id}"
            )
        record = _read_prefab_info(
            vfs_root,
            json_group,
            json_files,
            template_id,
            corresponding_character_id,
        )
        prefab_records.setdefault(corresponding_character_id, []).append(record)
        prefab_sources.append(record.source)

    requested_paths = frozenset(
        _normalize_container_path(_prefab_path(variant, part_name))
        for record in (record for records in prefab_records.values() for record in records)
        for part_name in record.part_names
        for variant in ("characters", "npc")
    )
    provider_details, provider_source = _provider_details(provider_path, requested_paths, check_cancelled)
    bindings = _model_bindings(identities, prefab_records, provider_details)
    source_identity_payload = {
        "tables": [asdict(source) for source in sources],
        "prefab_info": [asdict(source) for source in sorted(prefab_sources, key=lambda item: item.virtual_path)],
        "provider_index_sha256": provider_source.sha256,
    }
    source_identity = sha256(
        json.dumps(source_identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CharacterCatalog(
        "EndfieldCharacterCatalog/2",
        source_identity,
        identities,
        tuple(sources),
        provider_source,
        bindings,
        _summary(bindings),
    )


def read_character_catalog(game_root: Path, provider_index: Path) -> CharacterCatalog:
    """Read character sources directly, preserving the original two-path entry point."""
    _game, vfs_root, provider_path, _sources = _catalog_inputs(game_root, provider_index)
    return _read_character_catalog_sources(vfs_root, provider_path, _never_cancelled)


def read_character_catalog_cached(
    game_root: Path,
    provider_index: Path,
    cache: CharacterDataCache,
    check_cancelled: Callable[[], None],
) -> CharacterCatalog:
    """Read a catalog through a persistent source-stamped cache owned by the caller."""
    game, vfs_root, provider_path, source_paths = _catalog_inputs(game_root, provider_index)
    provider_stamp = source_stamp(provider_path)
    cached_provider = cache.lookup_fingerprint(provider_path, _PROVIDER_FINGERPRINT_SCHEMA, check_cancelled)
    group_headers = tuple(
        vfs_root / group_hash / f"{group_hash}.blc"
        for group_hash in (_TABLE_GROUP, _JSON_GROUP)
    )
    header_fingerprints = tuple(
        cache.fingerprint_file(path, _VFS_HEADER_FINGERPRINT_SCHEMA, check_cancelled)
        for path in group_headers
    )
    for path, fingerprint in zip(group_headers, header_fingerprints, strict=True):
        if source_stamp(path) != fingerprint.stamp:
            raise CharacterSourceChangedError(f"Character VFS group header changed during identity check: path={path}")
    provider_qualifier = (
        cached_provider.sha256 if cached_provider is not None else "provider_sha256_pending"
    )
    qualifiers = tuple(fingerprint.sha256 for fingerprint in header_fingerprints) + (provider_qualifier,)
    raw, _cache_hit = cache.get_or_build_json(
        "character_catalog",
        _CATALOG_CACHE_SCHEMA,
        source_paths,
        qualifiers,
        lambda: asdict(_read_character_catalog_sources(vfs_root, provider_path, check_cancelled)),
        check_cancelled,
    )
    catalog = _character_catalog_from_record(raw)
    if Path(catalog.provider_index_source.path).resolve() != provider_path:
        raise CharacterCacheError(
            f"Cached catalog provider source differs from request: expected={provider_path}, actual={catalog.provider_index_source.path}"
        )
    if cached_provider is not None and cached_provider.sha256 != catalog.provider_index_source.sha256:
        raise CharacterCacheError(
            f"Provider fingerprint conflicts with cached catalog: path={provider_path}, "
            f"catalog_sha256={catalog.provider_index_source.sha256}, fingerprint_sha256={cached_provider.sha256}"
        )
    if cached_provider is None:
        cache.remember_fingerprint(
            provider_path,
            _PROVIDER_FINGERPRINT_SCHEMA,
            CharacterSourceFingerprint(provider_stamp, catalog.provider_index_source.sha256),
        )
        cache.get_or_build_json(
            "character_catalog",
            _CATALOG_CACHE_SCHEMA,
            source_paths,
            tuple(fingerprint.sha256 for fingerprint in header_fingerprints)
            + (catalog.provider_index_source.sha256,),
            lambda: asdict(catalog),
            check_cancelled,
        )
    return catalog


def _cache_object(value: object, context: str) -> dict[str, object]:
    return _object(value, f"cached {context}")


def _cache_array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise CharacterCacheError(f"Character catalog cache field must be an array: field={context}")
    return value


def _cache_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise CharacterCacheError(f"Character catalog cache field must be a nonempty string: field={context}")
    return value


def _cache_int(value: object, context: str) -> int:
    if type(value) is not int:
        raise CharacterCacheError(f"Character catalog cache field must be an integer: field={context}")
    return value


def _cache_sha256(value: object, context: str) -> str:
    digest = _cache_text(value, context)
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise CharacterCacheError(f"Character catalog cache field is not a lowercase SHA256: field={context}")
    return digest


def _cache_cab(value: object, context: str) -> CharacterSerializedCab:
    row = _cache_object(value, context)
    externals = tuple(_cache_text(item, f"{context}.externals[]") for item in _cache_array(row.get("externals"), f"{context}.externals"))
    return CharacterSerializedCab(_cache_text(row.get("name"), f"{context}.name"), externals)


def _cache_provider(value: object, context: str) -> CharacterResourceProvider:
    row = _cache_object(value, context)
    cabs = tuple(_cache_cab(item, f"{context}.serialized_cabs[]")
                 for item in _cache_array(row.get("serialized_cabs"), f"{context}.serialized_cabs"))
    return CharacterResourceProvider(_cache_text(row.get("logical_name"), f"{context}.logical_name"), cabs)


def _cache_variant(value: object, context: str) -> CharacterPrefabVariant:
    row = _cache_object(value, context)
    variant, status = row.get("variant"), row.get("provider_status")
    if variant not in ("characters", "npc") or status not in ("found", "not_found"):
        raise CharacterCacheError(f"Character catalog cache variant is invalid: field={context}")
    providers = tuple(_cache_provider(item, f"{context}.providers[]")
                      for item in _cache_array(row.get("providers"), f"{context}.providers"))
    return CharacterPrefabVariant(
        "characters" if variant == "characters" else "npc",
        _cache_text(row.get("resource_path"), f"{context}.resource_path"),
        "found" if status == "found" else "not_found",
        providers,
    )


def _cache_part(value: object, context: str) -> CharacterPrefabPart:
    row = _cache_object(value, context)
    variants = tuple(_cache_variant(item, f"{context}.variants[]")
                     for item in _cache_array(row.get("variants"), f"{context}.variants"))
    return CharacterPrefabPart(_cache_text(row.get("part_name"), f"{context}.part_name"), variants)


def _cache_template(value: object, context: str) -> CharacterPrefabTemplate:
    row = _cache_object(value, context)
    source_value = _cache_object(row.get("source"), f"{context}.source")
    source = CharacterPrefabInfoSource(
        _cache_text(source_value.get("virtual_path"), f"{context}.source.virtual_path"),
        _cache_sha256(source_value.get("sha256"), f"{context}.source.sha256"),
        _cache_int(source_value.get("bytes"), f"{context}.source.bytes"),
    )
    part_names = tuple(_cache_text(item, f"{context}.part_name_id_list[]")
                       for item in _cache_array(row.get("part_name_id_list"), f"{context}.part_name_id_list"))
    references: list[CharacterPrefabReferenceField] = []
    for index, value_reference in enumerate(_cache_array(row.get("reference_fields"), f"{context}.reference_fields")):
        reference = _cache_object(value_reference, f"{context}.reference_fields[{index}]")
        field_value = reference.get("value")
        if field_value is not None and not isinstance(field_value, str):
            raise CharacterCacheError(f"Character catalog cached reference must be string or null: field={context}")
        references.append(CharacterPrefabReferenceField(
            _cache_text(reference.get("source_field"), f"{context}.reference_fields[{index}].source_field"),
            field_value,
        ))
    parts = tuple(_cache_part(item, f"{context}.parts[]")
                  for item in _cache_array(row.get("parts"), f"{context}.parts"))
    if row.get("geometry_status") != "not_verified":
        raise CharacterCacheError(f"Character catalog cached template geometry status is invalid: field={context}")
    return CharacterPrefabTemplate(
        _cache_text(row.get("template_id"), f"{context}.template_id"),
        _cache_text(row.get("corresponding_character_id"), f"{context}.corresponding_character_id"),
        source,
        part_names,
        tuple(references),
        parts,
        "not_verified",
    )


def _cache_binding(value: object, context: str) -> CharacterModelBinding:
    row = _cache_object(value, context)
    status, geometry_status = row.get("status"), row.get("geometry_status")
    if status not in ("bound", "no_template") or geometry_status not in ("not_verified", "not_available"):
        raise CharacterCacheError(f"Character catalog cached binding status is invalid: field={context}")
    templates = tuple(_cache_template(item, f"{context}.templates[]")
                      for item in _cache_array(row.get("templates"), f"{context}.templates"))
    return CharacterModelBinding(
        _cache_text(row.get("character_id"), f"{context}.character_id"),
        "bound" if status == "bound" else "no_template",
        templates,
        "not_verified" if geometry_status == "not_verified" else "not_available",
    )


def _character_catalog_from_record(value: object) -> CharacterCatalog:
    row = _cache_object(value, "character catalog")
    if row.get("format") != "EndfieldCharacterCatalog/2":
        raise CharacterCacheError(f"Unsupported cached character catalog format: value={row.get('format')!r}")
    identity = _cache_sha256(row.get("source_identity"), "source_identity")
    characters: list[CharacterIdentity] = []
    for index, value_character in enumerate(_cache_array(row.get("characters"), "characters")):
        character = _cache_object(value_character, f"characters[{index}]")
        characters.append(CharacterIdentity(
            _cache_text(character.get("character_id"), f"characters[{index}].character_id"),
            _cache_text(character.get("name"), f"characters[{index}].name"),
            _cache_text(character.get("english_name"), f"characters[{index}].english_name"),
            _cache_text(character.get("name_id"), f"characters[{index}].name_id"),
        ))
    table_sources: list[CharacterTableSource] = []
    for index, value_source in enumerate(_cache_array(row.get("sources"), "sources")):
        source = _cache_object(value_source, f"sources[{index}]")
        table_sources.append(CharacterTableSource(
            _cache_text(source.get("virtual_path"), f"sources[{index}].virtual_path"),
            _cache_sha256(source.get("sha256"), f"sources[{index}].sha256"),
            _cache_int(source.get("rows"), f"sources[{index}].rows"),
        ))
    provider_source_value = _cache_object(row.get("provider_index_source"), "provider_index_source")
    provider_source = CharacterProviderIndexSource(
        _cache_text(provider_source_value.get("path"), "provider_index_source.path"),
        _cache_sha256(provider_source_value.get("sha256"), "provider_index_source.sha256"),
        _cache_int(provider_source_value.get("bytes"), "provider_index_source.bytes"),
    )
    bindings = tuple(_cache_binding(item, "model_bindings[]")
                     for item in _cache_array(row.get("model_bindings"), "model_bindings"))
    summary_value = _cache_object(row.get("model_summary"), "model_summary")
    if summary_value.get("geometry_verification") != "not_performed":
        raise CharacterCacheError("Cached character catalog geometry verification status is invalid")
    summary = CharacterModelSummary(
        _cache_int(summary_value.get("bound_character_count"), "model_summary.bound_character_count"),
        _cache_int(summary_value.get("no_template_character_count"), "model_summary.no_template_character_count"),
        _cache_int(summary_value.get("template_count"), "model_summary.template_count"),
        _cache_int(summary_value.get("part_count"), "model_summary.part_count"),
        _cache_int(summary_value.get("variant_count"), "model_summary.variant_count"),
        _cache_int(summary_value.get("provider_match_count"), "model_summary.provider_match_count"),
        "not_performed",
    )
    return CharacterCatalog(
        "EndfieldCharacterCatalog/2", identity, tuple(characters), tuple(table_sources), provider_source, bindings, summary,
    )


def _write_json_new(path: Path, value: object) -> bytes:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(encoded)
    return encoded


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-root", required=True, type=Path)
    parser.add_argument("--provider-index", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path)
    options = parser.parse_args()
    output = options.output.expanduser().resolve()
    game = options.game_root.expanduser().resolve()
    provider_index = options.provider_index.expanduser().resolve()
    receipt_path = output.with_name(f"{output.stem}_receipt.json")
    if output.is_relative_to(game) or receipt_path.is_relative_to(game):
        raise CharacterCatalogError(f"Character catalog output must be outside the game installation: path={output}")
    if output == receipt_path:
        raise CharacterCatalogError(f"Character catalog and receipt paths collide: path={output}")
    for path in (output, receipt_path):
        if path.exists():
            raise FileExistsError(f"Character catalog output already exists: path={path}")

    cache_root = (
        options.cache_dir.expanduser().resolve()
        if options.cache_dir is not None
        else output.parent / ".endfield_character_cache"
    )
    blocked_cache_roots = {game, provider_index.parent.resolve()}
    if any(cache_root == directory or cache_root.is_relative_to(directory) for directory in blocked_cache_roots):
        raise CharacterCatalogError(
            f"Character cache must be outside game and provider input directories: path={cache_root}"
        )

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    catalog = read_character_catalog_cached(game, provider_index, CharacterDataCache(cache_root), _never_cancelled)
    output.parent.mkdir(parents=True, exist_ok=True)
    output_bytes = _write_json_new(output, asdict(catalog))
    elapsed_seconds = time.perf_counter() - started
    bound_count = catalog.model_summary.bound_character_count
    no_template_count = catalog.model_summary.no_template_character_count
    receipt = CharacterCatalogRunReceipt(
        "EndfieldCharacterCatalogRunReceipt/1",
        "written",
        started_at,
        elapsed_seconds,
        str(output),
        sha256(output_bytes).hexdigest(),
        catalog.provider_index_source.path,
        catalog.provider_index_source.sha256,
        len(catalog.characters),
        bound_count,
        no_template_count,
        "not_performed",
    )
    _write_json_new(receipt_path, asdict(receipt))
    print(json.dumps(asdict(receipt), ensure_ascii=False))


if __name__ == "__main__":
    main()
