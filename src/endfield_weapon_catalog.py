"""Catalog exact weapon table, localization, resource and provider relationships."""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import sqlite3
import sys
from typing import Literal

from endfield_character_cache import source_stamp
from endfield_character_catalog import (
    CharacterProviderIndexSource, CharacterResourceProvider, _TABLE_GROUP,
    _normalize_container_path, _provider_details, _read_virtual_payload, _virtual_file_index,
)
from endfield_character_prepare import _fingerprint
from endfield_character_sources import game_source_stamps, validate_character_storage, verify_game_source
from extractor_core.resources import BOOTSTRAP_VIRTUAL_FILES, load_group, read_virtual_file
from extractor_core.scene_tables import TableValue, read_scene_table


class WeaponCatalogError(ValueError):
    """Weapon source fields or selected bindings contradict their declared identity."""


BindingStatus = Literal['resolved', 'missing_resource', 'ambiguous_resource', 'missing_provider', 'ambiguous_provider']
NameStatus = Literal['resolved', 'missing_item', 'missing_text']
ProviderStatus = Literal['resolved', 'missing', 'ambiguous']


@dataclass(frozen=True)
class WeaponTableSource:
    table: str
    virtual_path: str
    sha256: str
    rows: int


@dataclass(frozen=True)
class WeaponResourceIndexSource:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class WeaponResourceBinding:
    resource_path: str
    provider_status: ProviderStatus
    providers: tuple[CharacterResourceProvider, ...]


@dataclass(frozen=True)
class WeaponRefinedCandidate:
    resource: WeaponResourceBinding
    semantic_status: Literal['unverified_candidate']


@dataclass(frozen=True)
class WeaponNameSource:
    table: Literal['ItemTable']
    row_key: str
    field: Literal['name.id']
    text_table: Literal['I18nTextTable_CN']
    text_id: str


@dataclass(frozen=True)
class WeaponRecord:
    weapon_id: str
    name: str | None
    name_status: NameStatus
    name_source: WeaponNameSource | None
    rarity: int
    weapon_type: int
    model_path: str
    model_status: BindingStatus
    resources: tuple[WeaponResourceBinding, ...]
    refined_candidates: tuple[WeaponRefinedCandidate, ...]
    geometry_status: Literal['not_verified']


@dataclass(frozen=True)
class WeaponCatalogSummary:
    weapon_count: int
    resolved_names: int
    resolved_models: int
    missing_models: int
    ambiguous_models: int
    refined_candidate_weapons: int
    duplicate_name_groups: tuple[tuple[str, tuple[str, ...]], ...]
    rarity_counts: tuple[tuple[int, int], ...]
    weapon_type_counts: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class WeaponCatalog:
    format: Literal['EndfieldWeaponCatalog/1']
    source_identity: str
    sources: tuple[WeaponTableSource, ...]
    provider_index_source: CharacterProviderIndexSource
    resource_index_source: WeaponResourceIndexSource
    entries: tuple[WeaponRecord, ...]
    summary: WeaponCatalogSummary


def _record(value: TableValue, context: str) -> dict[str, TableValue]:
    if not isinstance(value, dict):
        raise WeaponCatalogError(f'Expected weapon table object: field={context}')
    return value


def _text(value: TableValue, context: str) -> str:
    if not isinstance(value, str):
        raise WeaponCatalogError(f'Expected weapon source string: field={context}, value={value!r}')
    return value


def _integer(value: TableValue, context: str) -> int:
    if type(value) is not int:
        raise WeaponCatalogError(f'Expected weapon source integer: field={context}, value={value!r}')
    return value


def _model_path(row: Mapping[str, TableValue], context: str) -> str:
    value = _text(row.get('modelPath'), context).replace('\\', '/')
    parts = value.split('/')
    if value and (value != value.strip() or value.startswith('/') or ':' in value
                  or '..' in parts or '.' in parts or not value.casefold().endswith('.prefab')):
        raise WeaponCatalogError(f'Expected relative weapon prefab path: field={context}, value={value!r}')
    return value


def weapon_resource_paths(basic: Mapping[str, TableValue], indexed_paths: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Resolve exact table path suffixes while preserving missing or ambiguous resource matches."""
    resources: dict[str, set[str]] = {}
    for resource in indexed_paths:
        if not isinstance(resource, str) or not resource:
            raise WeaponCatalogError('Resource index contains an invalid path')
        resources.setdefault(_normalize_container_path(resource), set()).add(resource)
    result: dict[str, tuple[str, ...]] = {}
    for key, raw in basic.items():
        row = _record(raw, f'WeaponBasicTable[{key}]')
        model = _model_path(row, f'WeaponBasicTable[{key}].modelPath')
        suffix = '/' + model.casefold()
        result[key] = tuple(sorted({min(originals) for path, originals in resources.items()
                                    if model and (path == model.casefold() or path.endswith(suffix))}))
    return result


def _resource(path: str, providers: Mapping[str, tuple[CharacterResourceProvider, ...]]) -> WeaponResourceBinding:
    matches = providers.get(_normalize_container_path(path), ())
    status: ProviderStatus = 'resolved' if len(matches) == 1 else 'missing' if not matches else 'ambiguous'
    return WeaponResourceBinding(path, status, matches)


def _binding_status(resources: tuple[WeaponResourceBinding, ...]) -> BindingStatus:
    if not resources:
        return 'missing_resource'
    if len(resources) != 1:
        return 'ambiguous_resource'
    status = resources[0].provider_status
    return 'resolved' if status == 'resolved' else 'missing_provider' if status == 'missing' else 'ambiguous_provider'


def _name(weapon_id: str, items: Mapping[str, TableValue], texts: Mapping[str, TableValue]) -> tuple[str | None, NameStatus, WeaponNameSource | None]:
    if weapon_id not in items:
        return None, 'missing_item', None
    row = _record(items[weapon_id], f'ItemTable[{weapon_id}]')
    if _text(row.get('id'), f'ItemTable[{weapon_id}].id') != weapon_id:
        raise WeaponCatalogError(f'Item identity disagrees with weapon key: weapon={weapon_id}')
    localized = _record(row.get('name'), f'ItemTable[{weapon_id}].name')
    text_id = _integer(localized.get('id'), f'ItemTable[{weapon_id}].name.id')
    source = WeaponNameSource('ItemTable', weapon_id, 'name.id', 'I18nTextTable_CN', str(text_id))
    value = texts.get(str(text_id))
    if value is None or value == '':
        return None, 'missing_text', source
    return _text(value, f'I18nTextTable_CN[{text_id}]'), 'resolved', source


def assemble_weapon_records(basic: Mapping[str, TableValue], items: Mapping[str, TableValue], texts: Mapping[str, TableValue], indexed_paths: Sequence[str], providers: Mapping[str, tuple[CharacterResourceProvider, ...]]) -> tuple[WeaponRecord, ...]:
    """Join by source IDs, retaining ASCII names, duplicate names and unresolved model rows."""
    matches = weapon_resource_paths(basic, indexed_paths)
    path_index = {_normalize_container_path(path): path for path in indexed_paths}
    entries: list[WeaponRecord] = []
    for key, raw in sorted(basic.items()):
        row = _record(raw, f'WeaponBasicTable[{key}]')
        weapon_id = _text(row.get('weaponId'), f'WeaponBasicTable[{key}].weaponId')
        if not key or weapon_id != key:
            raise WeaponCatalogError(f'Weapon identity disagrees with table key: key={key!r}, weaponId={weapon_id!r}')
        name, name_status, name_source = _name(weapon_id, items, texts)
        model = _model_path(row, f'WeaponBasicTable[{key}].modelPath')
        resources = tuple(_resource(path, providers) for path in matches[key])
        candidates: dict[str, WeaponRefinedCandidate] = {}
        for resource in resources:
            path = PurePosixPath(resource.resource_path)
            candidate = str(path.with_name(path.stem + '_refined' + path.suffix))
            indexed = path_index.get(_normalize_container_path(candidate))
            if indexed is not None:
                candidates[_normalize_container_path(indexed)] = WeaponRefinedCandidate(_resource(indexed, providers), 'unverified_candidate')
        entries.append(WeaponRecord(weapon_id, name, name_status, name_source,
            _integer(row.get('rarity'), f'WeaponBasicTable[{key}].rarity'),
            _integer(row.get('weaponType'), f'WeaponBasicTable[{key}].weaponType'), model,
            _binding_status(resources), resources, tuple(candidates[key] for key in sorted(candidates)), 'not_verified'))
    if not entries:
        raise WeaponCatalogError('WeaponBasicTable contains no weapon records')
    return tuple(entries)


def resolve_weapon_model(entries: Sequence[WeaponRecord], weapon_id: str) -> WeaponResourceBinding:
    """Reject unavailable or ambiguous model bindings only when a weapon is selected."""
    matches = tuple(row for row in entries if row.weapon_id == weapon_id)
    if len(matches) != 1 or matches[0].model_status != 'resolved':
        raise WeaponCatalogError(f'Weapon model selection is unresolved or ambiguous: weapon={weapon_id}, matches={len(matches)}, statuses={[row.model_status for row in matches]}')
    return matches[0].resources[0]


def _summary(entries: tuple[WeaponRecord, ...]) -> WeaponCatalogSummary:
    names: dict[str, list[str]] = {}
    for row in entries:
        if row.name is not None:
            names.setdefault(row.name, []).append(row.weapon_id)
    return WeaponCatalogSummary(len(entries), sum(row.name_status == 'resolved' for row in entries),
        sum(row.model_status == 'resolved' for row in entries),
        sum(row.model_status in ('missing_resource', 'missing_provider') for row in entries),
        sum(row.model_status in ('ambiguous_resource', 'ambiguous_provider') for row in entries),
        sum(bool(row.refined_candidates) for row in entries),
        tuple((name, tuple(ids)) for name, ids in sorted(names.items()) if len(ids) > 1),
        tuple(sorted(Counter(row.rarity for row in entries).items())),
        tuple(sorted(Counter(row.weapon_type for row in entries).items())))


def read_weapon_catalog(game_root: Path, provider_index: Path, database: Path, check_cancelled: Callable[[], None]) -> WeaponCatalog:
    """Read source bytes through existing verified VFS, table and provider readers."""
    game = game_root.resolve(strict=True)
    provider = provider_index.resolve(strict=True)
    resource_db = database.resolve(strict=True)
    before_game = game_source_stamps(game)
    before_indexes = tuple(source_stamp(path) for path in (provider, resource_db))
    vfs_root = game / 'Endfield_Data/StreamingAssets/VFS'
    group = load_group(vfs_root, _TABLE_GROUP)
    files = _virtual_file_index(group, _TABLE_GROUP)
    decoded: dict[str, dict[str, TableValue]] = {}
    sources: list[WeaponTableSource] = []
    for name in ('WeaponBasicTable', 'ItemTable', 'I18nTextTable_CN'):
        check_cancelled()
        path = f'Data/TableCfg/{name}.bytes'
        payload = _read_virtual_payload(vfs_root, _TABLE_GROUP, group, files, path)
        values = read_scene_table(payload, name)
        decoded[name] = values
        sources.append(WeaponTableSource(name, path, sha256(payload).hexdigest(), len(values)))
    with sqlite3.connect(resource_db.as_uri() + '?mode=ro', uri=True) as connection:
        metadata = dict(connection.execute('SELECT key, value FROM meta'))
        if metadata.get('format') != 'EndfieldResourceDB/1':
            raise WeaponCatalogError(f'Unsupported weapon resource database: path={resource_db}, format={metadata.get("format")!r}')
        for field, role in (('manifest_sha256', 'manifest'), ('string_path_hash_sha256', 'main_paths')):
            check_cancelled()
            group_hash, virtual_path, _filename = BOOTSTRAP_VIRTUAL_FILES[role]
            actual = sha256(read_virtual_file(vfs_root, group_hash, virtual_path)).hexdigest()
            if metadata.get(field, '').casefold() != actual:
                raise WeaponCatalogError(f'Weapon resource database belongs to different game bytes: database={resource_db}, field={field}, expected={metadata.get(field)!r}, actual={actual}')
        indexed = tuple(row[0] for row in connection.execute("SELECT path FROM paths WHERE path_folded LIKE '%.prefab' ORDER BY path_index"))
    basic = decoded['WeaponBasicTable']
    resources = weapon_resource_paths(basic, indexed)
    paths = frozenset(_normalize_container_path(path) for matches in resources.values() for path in matches)
    candidate_paths = frozenset(str(PurePosixPath(path).with_name(PurePosixPath(path).stem + '_refined.prefab')) for path in paths)
    indexed_folded = frozenset(_normalize_container_path(path) for path in indexed)
    providers, provider_source = _provider_details(provider, paths | (candidate_paths & indexed_folded), check_cancelled)
    fingerprint = _fingerprint(resource_db, check_cancelled)
    resource_source = WeaponResourceIndexSource(fingerprint.path, fingerprint.sha256, fingerprint.bytes)
    entries = assemble_weapon_records(basic, decoded['ItemTable'], decoded['I18nTextTable_CN'], indexed, providers)
    identity = sha256(json.dumps({'format': 'EndfieldWeaponCatalog/1', 'tables': [asdict(row) for row in sources],
        'provider_sha256': provider_source.sha256, 'resource_database_sha256': resource_source.sha256},
        sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    verify_game_source(game, before_game, check_cancelled)
    if before_indexes != tuple(source_stamp(path) for path in (provider, resource_db)):
        raise WeaponCatalogError('Weapon resource indexes changed during catalog reading')
    return WeaponCatalog('EndfieldWeaponCatalog/1', identity, tuple(sources), provider_source, resource_source, entries, _summary(entries))


def _not_cancelled() -> None:
    return None


def main(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('game-root', 'provider-index', 'database', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(list(argv))
    output = args.output.resolve()
    validate_character_storage(args.game_root, output.parent)
    if output.exists():
        raise FileExistsError(f'Weapon catalog output already exists: {output}')
    catalog = read_weapon_catalog(args.game_root, args.provider_index, args.database, _not_cancelled)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(asdict(catalog), ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'written', 'output': str(output), 'source_identity': catalog.source_identity,
                      'summary': asdict(catalog.summary)}, ensure_ascii=True))


if __name__ == '__main__':
    main(sys.argv[1:])
