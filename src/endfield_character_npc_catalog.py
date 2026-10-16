"""Catalog independent NPC source references without claiming playable assembly."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Literal, cast

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
from endfield_character_catalog import (
    CharacterCatalogError,
    CharacterProviderIndexSource,
    CharacterResourceProvider,
    CharacterSerializedCab,
    _JSON_GROUP,
    _PROVIDER_FINGERPRINT_SCHEMA,
    _TABLE_GROUP,
    _VFS_HEADER_FINGERPRINT_SCHEMA,
    _catalog_inputs,
    _normalize_container_path,
    _prefab_path,
    _provider_details,
    _read_virtual_payload,
    _virtual_file_index,
)
from endfield_character_enemy_index import EnemyModelBinding, read_enemy_model_index


class IndependentNpcCatalogError(ValueError):
    """Independent NPC table, localization, or provider evidence is invalid."""


NpcSourceKind = Literal["prefab", "config_assembled", "unresolved"]
NpcNameTable = Literal["NpcTemplateGroupTable", "NpcTable", "EnemyTemplateDisplayInfoTable", "EnemyDisplayInfoTable"]
NpcNameStatus = Literal["resolved", "missing", "ambiguous"]
NpcConfigField = Literal["avatarMeshName", "avatarTempletName"]
NpcEntityCategory = Literal["animal", "creature", "object", "npc"]


@dataclass(frozen=True)
class IndependentNpcNameSource:
    table: NpcNameTable
    row_key: str
    field: str
    text_key: str | None
    text_id: str


@dataclass(frozen=True)
class IndependentNpcPrefabReference:
    part_name: str
    resource_path: str
    providers: tuple[CharacterResourceProvider, ...]

    @property
    def provider_found(self) -> bool:
        """Whether the exact resource path appears in the provider index."""
        return bool(self.providers)


@dataclass(frozen=True)
class IndependentNpcConfigReference:
    source_field: NpcConfigField
    source_value: str
    resource_path: str | None
    providers: tuple[CharacterResourceProvider, ...]

    @property
    def provider_found(self) -> bool:
        """Whether the exact resource path appears in the provider index."""
        return bool(self.providers)


@dataclass(frozen=True)
class IndependentNpcResourceReference:
    template_id: str
    prefab_info_path: str
    prefab_info_sha256: str | None
    prefab_parts: tuple[IndependentNpcPrefabReference, ...]
    avatar_mesh: IndependentNpcConfigReference | None
    avatar_templet: IndependentNpcConfigReference | None
    cpu_animation_template: str | None


@dataclass(frozen=True)
class IndependentNpcRecord:
    npc_id: str
    name: str | None
    name_source: IndependentNpcNameSource | None
    name_status: NpcNameStatus
    source_kind: NpcSourceKind
    resource_reference: IndependentNpcResourceReference


@dataclass(frozen=True)
class IndependentNpcCategoryEvidence:
    category: NpcEntityCategory
    source_field: Literal["avatarMeshName", "avatarTempletName", "enemyModelPrefab"] | None
    source_value: str | None


def npc_entity_category(entry: IndependentNpcRecord) -> IndependentNpcCategoryEvidence:
    """Classify the primary mesh family; shared animation templates are not taxonomy."""
    mesh = entry.resource_reference.avatar_mesh
    if mesh is None:
        enemy_parts = tuple(part for part in entry.resource_reference.prefab_parts
                            if part.resource_path.startswith("assets/beyond/dynamicassets/gameplay/actors/postmodels/enemies/"))
        if len(enemy_parts) == 1:
            return IndependentNpcCategoryEvidence("creature", "enemyModelPrefab", enemy_parts[0].resource_path)
        return IndependentNpcCategoryEvidence("npc", None, None)
    segments = mesh.source_value.replace("\\", "/").casefold().split("/")
    category: NpcEntityCategory = "npc"
    if len(segments) >= 4 and segments[:2] == ["npc", "avatarmesh"]:
        family = segments[2]
        if family == "animal":
            category = "animal"
        elif family == "monster":
            category = "creature"
        elif family == "object":
            category = "object"
    return IndependentNpcCategoryEvidence(category, mesh.source_field, mesh.source_value)


@dataclass(frozen=True)
class IndependentNpcCatalogSource:
    source_name: str
    virtual_path: str
    sha256: str
    rows: int


@dataclass(frozen=True)
class IndependentNpcCatalogSummary:
    npc_count: int
    resolved_name_count: int
    missing_name_count: int
    ambiguous_name_count: int
    prefab_count: int
    config_assembled_count: int
    unresolved_source_count: int
    missing_prefab_info_count: int
    character_linked_excluded_count: int


@dataclass(frozen=True)
class IndependentNpcCatalog:
    format: str
    source_identity: str
    entries: tuple[IndependentNpcRecord, ...]
    sources: tuple[IndependentNpcCatalogSource, ...]
    provider_index_source: CharacterProviderIndexSource
    summary: IndependentNpcCatalogSummary


@dataclass(frozen=True)
class _NpcPrefabSource:
    virtual_path: str
    sha256: str
    part_names: tuple[str, ...]
    avatar_mesh_name: str | None
    avatar_templet_name: str | None
    cpu_animation_template: str | None


@dataclass(frozen=True)
class _NpcName:
    value: str | None
    source: IndependentNpcNameSource | None
    status: NpcNameStatus


_CATALOG_CACHE_SCHEMA = "independent-npc-catalog/6"
_NPC_PROVIDER_SCHEMA = "independent-npc-provider-jsonl/1"


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise IndependentNpcCatalogError(f"Expected object with string keys: field={context}")
    return value


def _table_object(value: TableValue, context: str) -> dict[str, TableValue]:
    if not isinstance(value, dict):
        raise IndependentNpcCatalogError(f"Expected NPC table object: field={context}")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IndependentNpcCatalogError(f"Expected nonempty NPC source string: field={context}, value={value!r}")
    return value


def _optional_text(value: object, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise IndependentNpcCatalogError(f"Expected optional NPC source string: field={context}, value={value!r}")
    return value or None


def _optional_reference(value: object, context: str) -> str | None:
    text = _optional_text(value, context)
    return text.strip() if text is not None and text.strip() else None


def _table_source(
    table_name: str, virtual_path: str, payload: bytes, row_count: int,
) -> IndependentNpcCatalogSource:
    return IndependentNpcCatalogSource(
        table_name,
        virtual_path,
        hashlib.sha256(payload).hexdigest(),
        row_count,
    )


def _read_table(
    vfs_root: Path,
    table_group: vfs.VfsGroup,
    table_files: dict[str, vfs.VfsFile],
    table_name: str,
    check_cancelled: Callable[[], None],
) -> tuple[dict[str, TableValue], IndependentNpcCatalogSource]:
    check_cancelled()
    virtual_path = f"Data/TableCfg/{table_name}.bytes"
    payload = _read_virtual_payload(
        vfs_root,
        _TABLE_GROUP,
        table_group,
        table_files,
        virtual_path,
    )
    values = read_scene_table(payload, table_name)
    return values, _table_source(table_name, virtual_path, payload, len(values))


def _safe_component(value: object, context: str) -> str:
    component = _text(value, context)
    if component in (".", "..") or "/" in component or "\\" in component:
        raise IndependentNpcCatalogError(f"Unsafe NPC resource path component: field={context}, value={component!r}")
    return component


def _read_prefab_info(
    vfs_root: Path,
    json_group: vfs.VfsGroup,
    json_files: dict[str, vfs.VfsFile],
    template_id: str,
) -> _NpcPrefabSource | None:
    if template_id in (".", "..") or "/" in template_id or "\\" in template_id:
        raise IndependentNpcCatalogError(f"NPC template ID is not a safe virtual path component: {template_id!r}")
    virtual_path = f"Data/Json/NPC/PrefabInfo/{template_id}.json"
    if virtual_path.casefold() not in json_files:
        return None
    payload = _read_virtual_payload(vfs_root, _JSON_GROUP, json_group, json_files, virtual_path)
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IndependentNpcCatalogError(f"Invalid NPC PrefabInfo JSON: path={virtual_path}, error={error}") from error
    source = _object(parsed, virtual_path)
    if _text(source.get("id"), f"{virtual_path}.id") != template_id:
        raise IndependentNpcCatalogError(f"NPC PrefabInfo ID does not match its path: path={virtual_path}")
    raw_parts = source.get("partNameIdList")
    if not isinstance(raw_parts, list):
        raise IndependentNpcCatalogError(f"NPC PrefabInfo partNameIdList must be an array: path={virtual_path}")
    part_names = tuple(
        _safe_component(value, f"{virtual_path}.partNameIdList[{index}]")
        for index, value in enumerate(raw_parts)
    )
    if len(set(part_names)) != len(part_names):
        raise IndependentNpcCatalogError(f"NPC PrefabInfo repeats a part identifier: path={virtual_path}")
    return _NpcPrefabSource(
        virtual_path,
        hashlib.sha256(payload).hexdigest(),
        part_names,
        _optional_reference(source.get("avatarMeshName"), f"{virtual_path}.avatarMeshName"),
        _optional_reference(source.get("avatarTempletName"), f"{virtual_path}.avatarTempletName"),
        _optional_reference(source.get("cpuAnimationTempletName"), f"{virtual_path}.cpuAnimationTempletName"),
    )


def _configuration_resource_path(source_field: NpcConfigField, source_value: str) -> str | None:
    if source_field == "avatarMeshName":
        source_prefix = ("npc", "avatarmesh")
        family = "avatarmesh"
    else:
        source_prefix = ("npc", "avatartemplet", "entity")
        family = "avatartemplet"
    segments = tuple(source_value.replace("\\", "/").split("/"))
    normalized = tuple(segment.casefold() for segment in segments)
    if normalized[:len(source_prefix)] != source_prefix:
        return None
    tail = segments[len(source_prefix):]
    if not tail or any(segment in ("", ".", "..") or "/" in segment or "\\" in segment for segment in tail):
        return None
    directories = tuple(segment.casefold() for segment in tail[:-1])
    asset_id = tail[-1].casefold()
    parent = "/".join(("assets", "beyond", "dynamicassets", "gameplay", "npc", family, *directories))
    return f"{parent}/data_npc_{family}_{asset_id}.asset"


def _i18n_text(texts: dict[str, TableValue], text_id: int, context: str) -> str | None:
    value = texts.get(str(text_id))
    if value is None:
        return None
    if not isinstance(value, str):
        raise IndependentNpcCatalogError(f"Chinese localization value is not text: field={context}, id={text_id}")
    return value if value.strip() else None


def _resolve_name(
    npc_id: str,
    template_id: str,
    groups: dict[str, TableValue],
    text_keys: dict[str, TableValue],
    chinese_texts: dict[str, TableValue],
    npc_table_by_data_key: dict[str, tuple[tuple[str, dict[str, TableValue]], ...]],
) -> _NpcName:
    group_value = groups.get(npc_id)
    if group_value is not None:
        group = _table_object(group_value, f"NpcTemplateGroupTable[{npc_id}]")
        group_template = _optional_reference(group.get("templateId"), f"NpcTemplateGroupTable[{npc_id}].templateId")
        if group_template is not None and group_template != template_id:
            raise IndependentNpcCatalogError(
                f"NPC template differs between source tables: npc={npc_id}, "
                f"NpcInfoTable={template_id}, NpcTemplateGroupTable={group_template}"
            )
        name_key = _optional_reference(group.get("name"), f"NpcTemplateGroupTable[{npc_id}].name")
        if name_key is not None:
            key_value = text_keys.get(name_key)
            if key_value is not None:
                key_row = _table_object(key_value, f"TextTable[{name_key}]")
                text_id = key_row.get("id")
                if type(text_id) is not int:
                    raise IndependentNpcCatalogError(
                        f"NPC TextTable localization ID is not an integer: key={name_key}, value={text_id!r}"
                    )
                localized = _i18n_text(chinese_texts, text_id, f"NpcTemplateGroupTable[{npc_id}].name")
                if localized is not None:
                    return _NpcName(
                        localized,
                        IndependentNpcNameSource(
                            "NpcTemplateGroupTable", npc_id, "name", name_key, str(text_id),
                        ),
                        "resolved",
                    )

    candidates: list[tuple[str, str, int]] = []
    for row_key, row in npc_table_by_data_key.get(template_id, ()):
        name_value = _table_object(row.get("name"), f"NpcTable[{row_key}].name")
        text_id = name_value.get("id")
        if type(text_id) is not int:
            raise IndependentNpcCatalogError(
                f"NPC name localization ID is not an integer: table=NpcTable, key={row_key}, value={text_id!r}"
            )
        localized = _i18n_text(chinese_texts, text_id, f"NpcTable[{row_key}].name")
        if localized is not None:
            candidates.append((localized, row_key, text_id))
    unique_names = {candidate[0] for candidate in candidates}
    if len(unique_names) == 1:
        localized, row_key, text_id = min(candidates, key=lambda candidate: candidate[1])
        return _NpcName(
            localized,
            IndependentNpcNameSource("NpcTable", row_key, "name.id", None, str(text_id)),
            "resolved",
        )
    if len(unique_names) > 1:
        return _NpcName(None, None, "ambiguous")
    return _NpcName(None, None, "missing")


def _resolve_enemy_name(
    template_id: str,
    template_display: dict[str, TableValue],
    enemy_display: dict[str, TableValue],
    chinese_texts: dict[str, TableValue],
) -> _NpcName:
    """Resolve the canonical enemy name through exact display localization IDs."""
    candidates: list[tuple[str, IndependentNpcNameSource]] = []
    for table, values in (("EnemyTemplateDisplayInfoTable", template_display), ("EnemyDisplayInfoTable", enemy_display)):
        for row_key, value in sorted(values.items()):
            row = _table_object(value, f"{table}[{row_key}]")
            if _text(row.get("templateId"), f"{table}[{row_key}].templateId") != template_id:
                continue
            name = _table_object(row.get("name"), f"{table}[{row_key}].name")
            text_id = name.get("id")
            if type(text_id) is not int:
                raise IndependentNpcCatalogError(f"Enemy name localization ID is not an integer: table={table}, key={row_key}, value={text_id!r}")
            localized = _i18n_text(chinese_texts, text_id, f"{table}[{row_key}].name")
            if localized is not None:
                candidates.append((localized, IndependentNpcNameSource(cast(NpcNameTable, table), row_key, "name.id", None, str(text_id))))
        if candidates:
            break
    unique_names = {name for name, _source in candidates}
    if len(unique_names) == 1:
        name, source = candidates[0]
        return _NpcName(name, source, "resolved")
    return _NpcName(None, None, "ambiguous" if unique_names else "missing")


def _enemy_template_ids(
    templates: dict[str, TableValue],
    enemies: dict[str, TableValue],
) -> tuple[str, ...]:
    """Validate canonical enemy templates without treating combat variants as models."""
    template_ids: set[str] = set()
    for row_key, value in sorted(templates.items()):
        row = _table_object(value, f"EnemyTemplateTable[{row_key}]")
        template_id = _safe_component(row.get("templateId"), f"EnemyTemplateTable[{row_key}].templateId")
        if template_id != row_key:
            raise IndependentNpcCatalogError(f"Enemy template key differs from templateId: key={row_key}, templateId={template_id}")
        template_ids.add(template_id)
    for row_key, value in sorted(enemies.items()):
        row = _table_object(value, f"EnemyTable[{row_key}]")
        if _text(row.get("enemyId"), f"EnemyTable[{row_key}].enemyId") != row_key:
            raise IndependentNpcCatalogError(f"Enemy key differs from enemyId: key={row_key}")
        template_id = _text(row.get("templateId"), f"EnemyTable[{row_key}].templateId")
        if template_id not in template_ids:
            raise IndependentNpcCatalogError(f"Enemy references an undeclared template: enemy={row_key}, templateId={template_id}")
    return tuple(sorted(template_ids))


def _resolve_enemy_prefab_name(
    part_names: tuple[str, ...],
    bindings: tuple[EnemyModelBinding, ...],
    names: dict[str, _NpcName],
) -> _NpcName:
    """Join exact EnemyData model IDs to exact PrefabInfo slots, never NPC ID spelling."""
    matched = tuple(names[binding.template_id] for binding in bindings if binding.model_id in part_names)
    resolved = tuple(name for name in matched if name.status == "resolved")
    unique = {name.value for name in resolved}
    if len(unique) == 1 and not any(name.status == "ambiguous" for name in matched):
        return resolved[0]
    return _NpcName(None, None, "ambiguous" if len(unique) > 1 or any(name.status == "ambiguous" for name in matched) else "missing")


def _enemy_model_prefab_paths(
    provider_path: Path,
    bindings: tuple[EnemyModelBinding, ...],
    check_cancelled: Callable[[], None],
) -> dict[str, tuple[str, ...]]:
    """Join exact model IDs to existing enemy prefab container filenames."""
    by_filename = {binding.model_id.casefold() + ".prefab": binding.model_id for binding in bindings}
    paths: dict[str, set[str]] = {binding.model_id: set() for binding in bindings}
    with provider_path.open("rb") as stream:
        for line_number, raw in enumerate(stream, 1):
            if line_number % 1024 == 0:
                check_cancelled()
            try:
                row = _object(json.loads(raw), f"provider line {line_number}")
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise IndependentNpcCatalogError(f"Invalid enemy provider JSON: path={provider_path}, line={line_number}, error={error}") from error
            if row.get("kind") != "match":
                continue
            containers = row.get("containers")
            if not isinstance(containers, list):
                raise IndependentNpcCatalogError(f"Enemy provider containers must be an array: line={line_number}")
            for container in containers:
                path = _normalize_container_path(_text(container, f"provider line {line_number}.container"))
                model_id = by_filename.get(path.rsplit("/", 1)[-1])
                if model_id is not None and path.startswith("assets/beyond/dynamicassets/gameplay/actors/postmodels/enemies/"):
                    paths[model_id].add(path)
    return {model_id: tuple(sorted(values)) for model_id, values in paths.items()}


def _enemy_direct_prefab_records(
    entries: tuple[IndependentNpcRecord, ...],
    bindings: tuple[EnemyModelBinding, ...],
    names: dict[str, _NpcName],
    paths: dict[str, tuple[str, ...]],
    providers: dict[str, tuple[CharacterResourceProvider, ...]],
) -> tuple[IndependentNpcRecord, ...]:
    """Add canonical enemies only when no existing NPC already represents the exact model."""
    represented = {part.part_name for entry in entries for part in entry.resource_reference.prefab_parts}
    identities = {entry.npc_id for entry in entries}
    additions: list[IndependentNpcRecord] = []
    for binding in bindings:
        if binding.model_id in represented:
            continue
        resources = paths[binding.model_id]
        if not resources:
            continue
        if len(resources) != 1:
            raise IndependentNpcCatalogError(f"Enemy model matches multiple exact prefabs: template={binding.template_id}, model={binding.model_id}, paths={resources}")
        resource_path = resources[0]
        matches = providers.get(resource_path, ())
        if len(matches) != 1 or len(matches[0].serialized_cabs) != 1:
            raise IndependentNpcCatalogError(f"Enemy prefab requires one exact provider/CAB: template={binding.template_id}, path={resource_path}, providers={len(matches)}")
        npc_id = "enemy-" + binding.template_id
        if npc_id in identities:
            raise IndependentNpcCatalogError(f"Enemy identity collides with existing NPC ID: npc_id={npc_id}")
        name = names[binding.template_id]
        reference = IndependentNpcResourceReference(binding.template_id, binding.resource_path, None,
            (IndependentNpcPrefabReference(binding.model_id, resource_path, matches),), None, None, None)
        additions.append(IndependentNpcRecord(npc_id, name.value, name.source, name.status, "prefab", reference))
    return tuple(additions)


def _provider_tuple(
    path: str | None,
    providers_by_path: dict[str, tuple[CharacterResourceProvider, ...]],
) -> tuple[CharacterResourceProvider, ...]:
    if path is None:
        return ()
    return providers_by_path.get(_normalize_container_path(path), ())


def _source_kind(reference: IndependentNpcResourceReference) -> NpcSourceKind:
    if any(part.providers for part in reference.prefab_parts):
        return "prefab"
    mesh = reference.avatar_mesh
    templet = reference.avatar_templet
    if mesh is not None and templet is not None and mesh.providers and templet.providers:
        return "config_assembled"
    return "unresolved"


def _summary(
    entries: tuple[IndependentNpcRecord, ...], character_linked_excluded_count: int,
) -> IndependentNpcCatalogSummary:
    return IndependentNpcCatalogSummary(
        len(entries),
        sum(entry.name_status == "resolved" for entry in entries),
        sum(entry.name_status == "missing" for entry in entries),
        sum(entry.name_status == "ambiguous" for entry in entries),
        sum(entry.source_kind == "prefab" for entry in entries),
        sum(entry.source_kind == "config_assembled" for entry in entries),
        sum(entry.source_kind == "unresolved" for entry in entries),
        sum(entry.resource_reference.prefab_info_sha256 is None for entry in entries),
        character_linked_excluded_count,
    )


def _npc_info_with_environmental_templates(
    npc_info: dict[str, TableValue],
    environmental: dict[str, TableValue],
    linked_templates: set[str],
) -> dict[str, TableValue]:
    """Preserve source NPC IDs and add unrepresented environmental template IDs."""
    represented: set[str] = set()
    for npc_id, value in npc_info.items():
        row = _table_object(value, f"NpcInfoTable[{npc_id}]")
        if _text(row.get("npcId"), f"NpcInfoTable[{npc_id}].npcId") != npc_id:
            raise IndependentNpcCatalogError(f"NpcInfoTable key differs from npcId: key={npc_id}")
        template_id = _optional_reference(row.get("templateId"), f"NpcInfoTable[{npc_id}].templateId")
        if template_id is not None:
            represented.add(template_id)
    additions: dict[str, TableValue] = {}
    for row_key, value in sorted(environmental.items()):
        row = _table_object(value, f"GameplayAndEnvironmentalNpc[{row_key}]")
        template_id = _safe_component(row.get("dataKey"), f"GameplayAndEnvironmentalNpc[{row_key}].dataKey")
        if template_id != row_key:
            raise IndependentNpcCatalogError(
                f"Environmental NPC key differs from dataKey: key={row_key}, dataKey={template_id}"
            )
        if template_id in represented or template_id in linked_templates:
            continue
        npc_id = f"environmental-{template_id}"
        if npc_id in npc_info:
            raise IndependentNpcCatalogError(
                f"Environmental NPC identity collides with source NPC ID: npc_id={npc_id}, template_id={template_id}"
            )
        additions[npc_id] = {"npcId": npc_id, "templateId": template_id}
    return {**npc_info, **additions}


def _npc_info_with_monster_prefabs(
    npc_info: dict[str, TableValue],
    prefabs: dict[str, _NpcPrefabSource],
    linked_templates: set[str],
) -> dict[str, TableValue]:
    """Add missing exact Monster PrefabInfo templates, retaining existing source IDs."""
    represented = {
        _optional_reference(_table_object(value, f"NpcInfoTable[{npc_id}]").get("templateId"), f"NpcInfoTable[{npc_id}].templateId")
        for npc_id, value in npc_info.items()
    }
    additions: dict[str, TableValue] = {}
    for template_id, source in sorted(prefabs.items()):
        mesh = source.avatar_mesh_name
        if mesh is None or mesh.replace("\\", "/").casefold().split("/")[:3] != ["npc", "avatarmesh", "monster"]:
            continue
        if template_id in represented or template_id in linked_templates:
            continue
        npc_id = "enemy-npc-" + template_id
        if npc_id in npc_info:
            raise IndependentNpcCatalogError(f"Monster PrefabInfo identity collides with source NPC ID: npc_id={npc_id}, template_id={template_id}")
        additions[npc_id] = {"npcId": npc_id, "templateId": template_id}
    return {**npc_info, **additions}


def _read_independent_npc_catalog_sources(
    vfs_root: Path,
    provider_path: Path,
    enemy_model_index: Path | None,
    check_cancelled: Callable[[], None],
) -> IndependentNpcCatalog:
    table_group = load_group(vfs_root, _TABLE_GROUP)
    table_files = _virtual_file_index(table_group, _TABLE_GROUP)
    table_values: dict[str, dict[str, TableValue]] = {}
    table_sources: list[IndependentNpcCatalogSource] = []
    table_names = (
        "NpcInfoTable",
        "NpcTemplateGroupTable",
        "TextTable",
        "I18nTextTable_CN",
        "NpcTable",
        "NpcPrefabCorrespondingCharTable",
        "GameplayAndEnvironmentalNpc",
        "EnemyTable",
        "EnemyTemplateTable",
        "EnemyTemplateDisplayInfoTable",
        "EnemyDisplayInfoTable",
    )
    for table_name in table_names:
        values, source = _read_table(vfs_root, table_group, table_files, table_name, check_cancelled)
        table_values[table_name] = values
        table_sources.append(source)

    linked_templates: set[str] = set()
    for key, value in sorted(table_values["NpcPrefabCorrespondingCharTable"].items()):
        row = _table_object(value, f"NpcPrefabCorrespondingCharTable[{key}]")
        template_id = _text(row.get("templateId"), f"NpcPrefabCorrespondingCharTable[{key}].templateId")
        _text(row.get("correspondingCharId"), f"NpcPrefabCorrespondingCharTable[{key}].correspondingCharId")
        linked_templates.add(template_id)

    npc_info = _npc_info_with_environmental_templates(
        table_values["NpcInfoTable"], table_values["GameplayAndEnvironmentalNpc"], linked_templates,
    )

    npc_table_by_data_key_lists: dict[str, list[tuple[str, dict[str, TableValue]]]] = defaultdict(list)
    for key, value in sorted(table_values["NpcTable"].items()):
        row = _table_object(value, f"NpcTable[{key}]")
        data_key = _optional_reference(row.get("dataKey"), f"NpcTable[{key}].dataKey")
        if data_key is not None:
            npc_table_by_data_key_lists[data_key].append((key, row))
    npc_table_by_data_key = {
        key: tuple(rows) for key, rows in npc_table_by_data_key_lists.items()
    }

    json_group = load_group(vfs_root, _JSON_GROUP)
    json_files = _virtual_file_index(json_group, _JSON_GROUP)
    monster_prefabs: dict[str, _NpcPrefabSource] = {}
    for virtual_path, virtual_file in sorted(json_files.items()):
        if not virtual_path.startswith("data/json/npc/prefabinfo/") or not virtual_path.endswith(".json"):
            continue
        check_cancelled()
        payload = _read_virtual_payload(vfs_root, _JSON_GROUP, json_group, json_files, virtual_file.file_name)
        try:
            source = _object(json.loads(payload), virtual_file.file_name)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IndependentNpcCatalogError(f"Invalid NPC PrefabInfo JSON: path={virtual_file.file_name}, error={error}") from error
        mesh = _optional_reference(source.get("avatarMeshName"), f"{virtual_file.file_name}.avatarMeshName")
        if mesh is None or mesh.replace("\\", "/").casefold().split("/")[:3] != ["npc", "avatarmesh", "monster"]:
            continue
        template_id = _safe_component(virtual_file.file_name.rsplit("/", 1)[1].removesuffix(".json"), "PrefabInfo filename")
        prefab = _read_prefab_info(vfs_root, json_group, json_files, template_id)
        if prefab is not None:
            monster_prefabs[template_id] = prefab
    npc_info = _npc_info_with_monster_prefabs(npc_info, monster_prefabs, linked_templates)
    enemy_template_ids = _enemy_template_ids(table_values["EnemyTemplateTable"], table_values["EnemyTable"])
    enemy_bindings: tuple[EnemyModelBinding, ...] = ()
    enemy_names: dict[str, _NpcName] = {}
    if enemy_model_index is not None:
        index = read_enemy_model_index(enemy_model_index)
        indexed_ids = {row.template_id for row in index.bindings} | {row.template_id for row in index.unavailable}
        if indexed_ids != set(enemy_template_ids):
            raise IndependentNpcCatalogError("Enemy model index template IDs differ from the current EnemyTemplateTable")
        summary_payload = _read_virtual_payload(vfs_root, _JSON_GROUP, json_group, json_files, "Data/Json/AIConfig/EnemyTemplateDataSummary.json")
        if hashlib.sha256(summary_payload).hexdigest() != index.summary_sha256:
            raise IndependentNpcCatalogError("Enemy model index differs from the current EnemyTemplateDataSummary")
        enemy_bindings = index.bindings
        enemy_names = {template_id: _resolve_enemy_name(template_id, table_values["EnemyTemplateDisplayInfoTable"],
                       table_values["EnemyDisplayInfoTable"], table_values["I18nTextTable_CN"]) for template_id in enemy_template_ids}
        table_sources.append(IndependentNpcCatalogSource("EnemyModelIndex", "EnemyModelIndex/" + index.source_identity + ".json",
                             hashlib.sha256(enemy_model_index.read_bytes()).hexdigest(), len(indexed_ids)))
    npc_sources: dict[str, _NpcPrefabSource | None] = {}
    requested_paths: set[str] = set()
    for key, value in sorted(npc_info.items()):
        row = _table_object(value, f"NpcInfoTable[{key}]")
        npc_id = _text(row.get("npcId"), f"NpcInfoTable[{key}].npcId")
        if npc_id != key:
            raise IndependentNpcCatalogError(
                f"NpcInfoTable key differs from npcId: key={key}, npcId={npc_id}"
            )
        template_id = _optional_reference(row.get("templateId"), f"NpcInfoTable[{key}].templateId")
        if template_id is None or template_id in linked_templates:
            continue
        prefab_source = monster_prefabs.get(template_id) or _read_prefab_info(vfs_root, json_group, json_files, template_id)
        npc_sources[npc_id] = prefab_source
        if prefab_source is None:
            continue
        for part_name in prefab_source.part_names:
            requested_paths.add(_normalize_container_path(_prefab_path("npc", part_name)))
        for source_field, source_value in (
            ("avatarMeshName", prefab_source.avatar_mesh_name),
            ("avatarTempletName", prefab_source.avatar_templet_name),
        ):
            if source_value is not None:
                resource_path = _configuration_resource_path(source_field, source_value)
                if resource_path is not None:
                    requested_paths.add(_normalize_container_path(resource_path))

    enemy_prefab_paths = _enemy_model_prefab_paths(provider_path, enemy_bindings, check_cancelled) if enemy_bindings else {}
    requested_paths.update(path for paths in enemy_prefab_paths.values() for path in paths)
    try:
        providers_by_path, provider_source = _provider_details(
            provider_path,
            frozenset(requested_paths),
            check_cancelled,
        )
    except CharacterCatalogError as error:
        raise IndependentNpcCatalogError(f"NPC provider index is invalid: {error}") from error

    records: list[IndependentNpcRecord] = []
    prefab_sources: list[IndependentNpcCatalogSource] = []
    for npc_id, value in sorted(npc_info.items()):
        row = _table_object(value, f"NpcInfoTable[{npc_id}]")
        template_id = _optional_reference(row.get("templateId"), f"NpcInfoTable[{npc_id}].templateId")
        if template_id is None:
            continue
        if template_id in linked_templates:
            continue
        name = _resolve_name(
            npc_id,
            template_id,
            table_values["NpcTemplateGroupTable"],
            table_values["TextTable"],
            table_values["I18nTextTable_CN"],
            npc_table_by_data_key,
        )
        prefab_source = npc_sources[npc_id]
        if name.status == "missing" and prefab_source is not None and prefab_source.avatar_mesh_name is not None:
            family = prefab_source.avatar_mesh_name.replace("\\", "/").casefold().split("/")[:3]
            if family == ["npc", "avatarmesh", "monster"]:
                name = _resolve_enemy_prefab_name(prefab_source.part_names, enemy_bindings, enemy_names)
        prefab_path = f"Data/Json/NPC/PrefabInfo/{template_id}.json"
        prefab_parts: tuple[IndependentNpcPrefabReference, ...] = ()
        avatar_mesh: IndependentNpcConfigReference | None = None
        avatar_templet: IndependentNpcConfigReference | None = None
        prefab_sha256: str | None = None
        if prefab_source is not None:
            prefab_sha256 = prefab_source.sha256
            prefab_sources.append(IndependentNpcCatalogSource(
                "PrefabInfo", prefab_source.virtual_path, prefab_source.sha256, 1,
            ))
            prefab_parts = tuple(
                IndependentNpcPrefabReference(
                    part_name,
                    _prefab_path("npc", part_name),
                    _provider_tuple(_prefab_path("npc", part_name), providers_by_path),
                )
                for part_name in prefab_source.part_names
            )
            if prefab_source.avatar_mesh_name is not None:
                resource_path = _configuration_resource_path("avatarMeshName", prefab_source.avatar_mesh_name)
                avatar_mesh = IndependentNpcConfigReference(
                    "avatarMeshName",
                    prefab_source.avatar_mesh_name,
                    resource_path,
                    _provider_tuple(resource_path, providers_by_path),
                )
            if prefab_source.avatar_templet_name is not None:
                resource_path = _configuration_resource_path("avatarTempletName", prefab_source.avatar_templet_name)
                avatar_templet = IndependentNpcConfigReference(
                    "avatarTempletName",
                    prefab_source.avatar_templet_name,
                    resource_path,
                    _provider_tuple(resource_path, providers_by_path),
                )
        resource_reference = IndependentNpcResourceReference(
            template_id,
            prefab_path,
            prefab_sha256,
            prefab_parts,
            avatar_mesh,
            avatar_templet,
            prefab_source.cpu_animation_template if prefab_source is not None else None,
        )
        records.append(IndependentNpcRecord(
            npc_id,
            name.value,
            name.source,
            name.status,
            _source_kind(resource_reference),
            resource_reference,
        ))

    frozen_entries = tuple(records) + _enemy_direct_prefab_records(tuple(records), enemy_bindings, enemy_names, enemy_prefab_paths, providers_by_path)
    source_identity_payload = {
        "tables": [asdict(source) for source in table_sources],
        "prefab_info": [asdict(source) for source in sorted(prefab_sources, key=lambda item: item.virtual_path)],
        "provider_index_sha256": provider_source.sha256,
    }
    source_identity = hashlib.sha256(json.dumps(
        source_identity_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    character_linked_excluded_count = sum(
        1
        for value in table_values["NpcInfoTable"].values()
        if isinstance(value, dict)
        and isinstance(value.get("templateId"), str)
        and bool(value.get("templateId"))
        and value.get("templateId") in linked_templates
    )
    sources = tuple(table_sources + sorted(prefab_sources, key=lambda item: item.virtual_path))
    return IndependentNpcCatalog(
        "EndfieldIndependentNpcCatalog/1",
        source_identity,
        frozen_entries,
        sources,
        provider_source,
        _summary(frozen_entries, character_linked_excluded_count),
    )


def _cache_object(value: object, context: str) -> dict[str, object]:
    try:
        return _object(value, f"cached {context}")
    except IndependentNpcCatalogError as error:
        raise CharacterCacheError(str(error)) from error


def _cache_array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise CharacterCacheError(f"Independent NPC cache field must be an array: field={context}")
    return value


def _cache_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise CharacterCacheError(f"Independent NPC cache field must be a nonempty string: field={context}")
    return value


def _cache_optional_text(value: object, context: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise CharacterCacheError(f"Independent NPC cache field must be string or null: field={context}")
    return value


def _cache_int(value: object, context: str) -> int:
    if type(value) is not int:
        raise CharacterCacheError(f"Independent NPC cache field must be an integer: field={context}")
    return value


def _cache_sha(value: object, context: str) -> str:
    digest = _cache_text(value, context)
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise CharacterCacheError(f"Independent NPC cache field must be a lowercase SHA256: field={context}")
    return digest


def _cache_provider(value: object, context: str) -> CharacterResourceProvider:
    row = _cache_object(value, context)
    cabs: list[CharacterSerializedCab] = []
    for index, value_cab in enumerate(_cache_array(row.get("serialized_cabs"), f"{context}.serialized_cabs")):
        cab = _cache_object(value_cab, f"{context}.serialized_cabs[{index}]")
        externals = tuple(
            _cache_text(item, f"{context}.serialized_cabs[{index}].externals[]")
            for item in _cache_array(cab.get("externals"), f"{context}.serialized_cabs[{index}].externals")
        )
        cabs.append(CharacterSerializedCab(
            _cache_text(cab.get("name"), f"{context}.serialized_cabs[{index}].name"),
            externals,
        ))
    return CharacterResourceProvider(
        _cache_text(row.get("logical_name"), f"{context}.logical_name"),
        tuple(cabs),
    )


def _cache_providers(value: object, context: str) -> tuple[CharacterResourceProvider, ...]:
    return tuple(
        _cache_provider(item, f"{context}[{index}]")
        for index, item in enumerate(_cache_array(value, context))
    )


def _cache_name_source(value: object, context: str) -> IndependentNpcNameSource | None:
    if value is None:
        return None
    row = _cache_object(value, context)
    table, field = row.get("table"), row.get("field")
    if table not in ("NpcTemplateGroupTable", "NpcTable", "EnemyTemplateDisplayInfoTable", "EnemyDisplayInfoTable"):
        raise CharacterCacheError(f"Independent NPC cache name source table is unsupported: field={context}")
    if not isinstance(field, str) or not field:
        raise CharacterCacheError(f"Independent NPC cache name source field is invalid: field={context}")
    return IndependentNpcNameSource(
        cast(NpcNameTable, table),
        _cache_text(row.get("row_key"), f"{context}.row_key"),
        field,
        _cache_optional_text(row.get("text_key"), f"{context}.text_key"),
        _cache_text(row.get("text_id"), f"{context}.text_id"),
    )


def _cache_prefab_part(value: object, context: str) -> IndependentNpcPrefabReference:
    row = _cache_object(value, context)
    return IndependentNpcPrefabReference(
        _cache_text(row.get("part_name"), f"{context}.part_name"),
        _cache_text(row.get("resource_path"), f"{context}.resource_path"),
        _cache_providers(row.get("providers"), f"{context}.providers"),
    )


def _cache_config_reference(value: object, context: str) -> IndependentNpcConfigReference | None:
    if value is None:
        return None
    row = _cache_object(value, context)
    field = row.get("source_field")
    if field not in ("avatarMeshName", "avatarTempletName"):
        raise CharacterCacheError(f"Independent NPC cache config reference field is invalid: field={context}")
    return IndependentNpcConfigReference(
        "avatarMeshName" if field == "avatarMeshName" else "avatarTempletName",
        _cache_text(row.get("source_value"), f"{context}.source_value"),
        _cache_optional_text(row.get("resource_path"), f"{context}.resource_path"),
        _cache_providers(row.get("providers"), f"{context}.providers"),
    )


def _cache_resource_reference(value: object, context: str) -> IndependentNpcResourceReference:
    row = _cache_object(value, context)
    if "cpu_animation_template" not in row:
        raise CharacterCacheError(f"Independent NPC cache CPU animation reference is missing: field={context}")
    prefab_sha = row.get("prefab_info_sha256")
    if prefab_sha is not None:
        prefab_sha = _cache_sha(prefab_sha, f"{context}.prefab_info_sha256")
    parts = tuple(
        _cache_prefab_part(item, f"{context}.prefab_parts[{index}]")
        for index, item in enumerate(_cache_array(row.get("prefab_parts"), f"{context}.prefab_parts"))
    )
    return IndependentNpcResourceReference(
        _cache_text(row.get("template_id"), f"{context}.template_id"),
        _cache_text(row.get("prefab_info_path"), f"{context}.prefab_info_path"),
        prefab_sha,
        parts,
        _cache_config_reference(row.get("avatar_mesh"), f"{context}.avatar_mesh"),
        _cache_config_reference(row.get("avatar_templet"), f"{context}.avatar_templet"),
        _cache_optional_text(row.get("cpu_animation_template"), f"{context}.cpu_animation_template"),
    )


def _cache_record(value: object, context: str) -> IndependentNpcRecord:
    row = _cache_object(value, context)
    source_kind, name_status = row.get("source_kind"), row.get("name_status")
    if source_kind not in ("prefab", "config_assembled", "unresolved"):
        raise CharacterCacheError(f"Independent NPC cache source kind is invalid: field={context}")
    if name_status not in ("resolved", "missing", "ambiguous"):
        raise CharacterCacheError(f"Independent NPC cache name status is invalid: field={context}")
    name = _cache_optional_text(row.get("name"), f"{context}.name")
    name_source = _cache_name_source(row.get("name_source"), f"{context}.name_source")
    if (name_status == "resolved") != (name is not None and name_source is not None):
        raise CharacterCacheError(f"Independent NPC cache name fields disagree: field={context}")
    if name_status != "resolved" and (name is not None or name_source is not None):
        raise CharacterCacheError(f"Independent NPC cache unresolved name has a value: field={context}")
    reference = _cache_resource_reference(row.get("resource_reference"), f"{context}.resource_reference")
    calculated_kind = _source_kind(reference)
    if source_kind != calculated_kind:
        raise CharacterCacheError(
            f"Independent NPC cache source kind disagrees with provider evidence: field={context}, "
            f"stored={source_kind}, calculated={calculated_kind}"
        )
    return IndependentNpcRecord(
        _cache_text(row.get("npc_id"), f"{context}.npc_id"),
        name,
        name_source,
        "resolved" if name_status == "resolved" else "missing" if name_status == "missing" else "ambiguous",
        "prefab" if source_kind == "prefab" else "config_assembled" if source_kind == "config_assembled" else "unresolved",
        reference,
    )


def _cache_source(value: object, context: str) -> IndependentNpcCatalogSource:
    row = _cache_object(value, context)
    return IndependentNpcCatalogSource(
        _cache_text(row.get("source_name"), f"{context}.source_name"),
        _cache_text(row.get("virtual_path"), f"{context}.virtual_path"),
        _cache_sha(row.get("sha256"), f"{context}.sha256"),
        _cache_int(row.get("rows"), f"{context}.rows"),
    )


def _cache_catalog(value: object) -> IndependentNpcCatalog:
    row = _cache_object(value, "independent NPC catalog")
    if row.get("format") != "EndfieldIndependentNpcCatalog/1":
        raise CharacterCacheError(f"Unsupported independent NPC catalog format: value={row.get('format')!r}")
    source_identity = _cache_sha(row.get("source_identity"), "source_identity")
    entries = tuple(
        _cache_record(item, f"entries[{index}]")
        for index, item in enumerate(_cache_array(row.get("entries"), "entries"))
    )
    if len({entry.npc_id for entry in entries}) != len(entries):
        raise CharacterCacheError("Independent NPC catalog cache contains duplicate NPC IDs")
    sources = tuple(
        _cache_source(item, f"sources[{index}]")
        for index, item in enumerate(_cache_array(row.get("sources"), "sources"))
    )
    provider_value = _cache_object(row.get("provider_index_source"), "provider_index_source")
    provider_source = CharacterProviderIndexSource(
        _cache_text(provider_value.get("path"), "provider_index_source.path"),
        _cache_sha(provider_value.get("sha256"), "provider_index_source.sha256"),
        _cache_int(provider_value.get("bytes"), "provider_index_source.bytes"),
    )
    summary_value = _cache_object(row.get("summary"), "summary")
    summary = IndependentNpcCatalogSummary(
        _cache_int(summary_value.get("npc_count"), "summary.npc_count"),
        _cache_int(summary_value.get("resolved_name_count"), "summary.resolved_name_count"),
        _cache_int(summary_value.get("missing_name_count"), "summary.missing_name_count"),
        _cache_int(summary_value.get("ambiguous_name_count"), "summary.ambiguous_name_count"),
        _cache_int(summary_value.get("prefab_count"), "summary.prefab_count"),
        _cache_int(summary_value.get("config_assembled_count"), "summary.config_assembled_count"),
        _cache_int(summary_value.get("unresolved_source_count"), "summary.unresolved_source_count"),
        _cache_int(summary_value.get("missing_prefab_info_count"), "summary.missing_prefab_info_count"),
        _cache_int(summary_value.get("character_linked_excluded_count"), "summary.character_linked_excluded_count"),
    )
    if summary != _summary(entries, summary.character_linked_excluded_count):
        raise CharacterCacheError("Independent NPC catalog cache summary disagrees with its records")
    return IndependentNpcCatalog(
        "EndfieldIndependentNpcCatalog/1",
        source_identity,
        entries,
        sources,
        provider_source,
        summary,
    )


def read_independent_npc_catalog(
    game_root: Path,
    provider_index: Path,
    cache: CharacterDataCache,
    enemy_model_index: Path | None,
    check_cancelled: Callable[[], None],
) -> IndependentNpcCatalog:
    """Read all unlinked NPC templates and resolve exact names and provider references."""
    game, vfs_root, provider_path, source_paths = _catalog_inputs(game_root, provider_index)
    if enemy_model_index is not None:
        source_paths += (enemy_model_index,)
    provider_stamp = source_stamp(provider_path)
    cached_provider = cache.lookup_fingerprint(provider_path, _NPC_PROVIDER_SCHEMA, check_cancelled)
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
            raise CharacterSourceChangedError(f"NPC VFS group header changed during identity check: path={path}")
    provider_qualifier = cached_provider.sha256 if cached_provider is not None else "provider_sha256_pending"
    qualifiers = tuple(fingerprint.sha256 for fingerprint in header_fingerprints) + (provider_qualifier,)
    raw, _cache_hit = cache.get_or_build_json(
        "independent_npc_catalog",
        _CATALOG_CACHE_SCHEMA,
        source_paths,
        qualifiers,
        lambda: asdict(_read_independent_npc_catalog_sources(vfs_root, provider_path, enemy_model_index, check_cancelled)),
        check_cancelled,
    )
    catalog = _cache_catalog(raw)
    if Path(catalog.provider_index_source.path).resolve() != provider_path:
        raise CharacterCacheError(
            f"Independent NPC provider differs from request: expected={provider_path}, "
            f"actual={catalog.provider_index_source.path}"
        )
    if cached_provider is not None and cached_provider.sha256 != catalog.provider_index_source.sha256:
        raise CharacterCacheError(
            f"NPC provider fingerprint conflicts with catalog: path={provider_path}, "
            f"catalog_sha256={catalog.provider_index_source.sha256}, fingerprint_sha256={cached_provider.sha256}"
        )
    if cached_provider is None:
        cache.remember_fingerprint(
            provider_path,
            _NPC_PROVIDER_SCHEMA,
            CharacterSourceFingerprint(provider_stamp, catalog.provider_index_source.sha256),
        )
        cache.get_or_build_json(
            "independent_npc_catalog",
            _CATALOG_CACHE_SCHEMA,
            source_paths,
            tuple(fingerprint.sha256 for fingerprint in header_fingerprints)
            + (catalog.provider_index_source.sha256,),
            lambda: asdict(catalog),
            check_cancelled,
        )
    return catalog


def resolve_independent_npc(
    catalog: IndependentNpcCatalog,
    npc_id: str,
) -> IndependentNpcRecord:
    """Resolve one exact source-table NPC ID, rejecting absent or duplicate IDs."""
    if not npc_id or npc_id != npc_id.strip():
        raise ValueError(f"NPC ID must be a nonempty exact table key: value={npc_id!r}")
    matches = tuple(entry for entry in catalog.entries if entry.npc_id == npc_id)
    if len(matches) != 1:
        if not matches:
            raise KeyError(f"Independent NPC ID was not found: npc_id={npc_id}")
        raise IndependentNpcCatalogError(f"Independent NPC ID is duplicated: npc_id={npc_id}, count={len(matches)}")
    return matches[0]
