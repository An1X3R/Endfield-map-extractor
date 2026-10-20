"""Discover display props and exact source configuration relationships."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
from typing import Literal
from uuid import uuid4

from endfield_character_bundle_inputs import prepare_providers_bundles
from endfield_character_cache import CharacterDataCache, CharacterSourceChangedError, CharacterSourceStamp, source_stamp
from endfield_character_catalog import (
    CharacterIdentity, CharacterResourceProvider, _cache_provider, _normalize_container_path,
    _provider_details, read_character_catalog_cached,
)
from endfield_character_sources import CharacterSourceContext, game_source_stamps, validate_character_storage, verify_game_source
from endfield_scene_discovery import tool_identity
from endfield_scene_export import run_scene_process


CheckCancelled = Callable[[], None]
Progress = Callable[[str, float, str], None]
DISPLAY_RESOURCE = 'assets/beyond/dynamicassets/gamedata/gameplayconfig/characterdisplayconfig.asset'
RESOURCE_PREFIX = 'assets/beyond/dynamicassets/gameplay/prefabs/uimodels/decoitems/'


class DisplayPropError(ValueError):
    """Display source data or an explicit selection contradicts its evidence."""


@dataclass(frozen=True)
class DisplayPropBinding:
    character_id: str
    character_name: str | None
    character_name_id: str | None
    reference_id: int
    item_index: int
    prefab_path: str
    mount_field: Literal['mountPoint', 'boneName']
    mount_point: str
    resource_paths: tuple[str, ...]
    status: Literal['resolved', 'missing_resource', 'ambiguous_resource']


@dataclass(frozen=True)
class DisplayPropEntry:
    prop_id: str
    resource_path: str
    providers: tuple[CharacterResourceProvider, ...]
    bindings: tuple[DisplayPropBinding, ...]
    geometry_status: Literal['not_verified']


@dataclass(frozen=True)
class DisplayPropCatalog:
    format: Literal['EndfieldDisplayPropCatalog/1']
    catalog_id: str
    character_catalog_identity: str
    configuration_sha256: str
    entries: tuple[DisplayPropEntry, ...]
    unresolved_bindings: tuple[DisplayPropBinding, ...]
    index_stamps: tuple[CharacterSourceStamp, ...]
    game_identity: str
    json_reader_identity: str
    unity_version: str


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DisplayPropError(f'Expected display prop object: field={field}')
    return value


def _rows(value: object, field: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise DisplayPropError(f'Expected display prop array: field={field}')
    return tuple(_object(row, field) for row in value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise DisplayPropError(f'Expected display prop text: field={field}')
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise DisplayPropError(f'Expected display prop integer: field={field}, value={value!r}')
    return value


def _read(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding='utf-8-sig'))
    return _object(value, str(path))


def display_prop_resource_paths(database: Path) -> tuple[str, ...]:
    """Scan the actual resource family without assuming a character roster."""
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        metadata = dict(connection.execute('SELECT key,value FROM meta'))
        if metadata.get('format') != 'EndfieldResourceDB/1':
            raise DisplayPropError(f'Unsupported display prop resource database: {database}')
        rows = connection.execute('SELECT path FROM paths WHERE path_folded LIKE ?', (RESOURCE_PREFIX + '%',))
        return tuple(sorted({_normalize_container_path(path) for (path,) in rows
                             if PurePosixPath(path).suffix.casefold() == '.prefab'}))


def _resource_matches(prefab: str, paths: Sequence[str]) -> tuple[str, ...]:
    normalized = _normalize_container_path(prefab)
    relative = PurePosixPath(normalized)
    if (not normalized or relative.is_absolute() or ':' in normalized
            or any(part in ('', '.', '..') for part in normalized.split('/'))):
        raise DisplayPropError(f'Unsafe display configuration prefab path: {prefab!r}')
    if relative.suffix not in ('', '.prefab'):
        raise DisplayPropError(f'Unexpected display configuration prefab suffix: {prefab!r}')
    target = normalized if relative.suffix else normalized + '.prefab'
    if '/' in target:
        exact = target if target.startswith('assets/') else RESOURCE_PREFIX + target
        return tuple(path for path in paths if _normalize_container_path(path) == exact)
    return tuple(path for path in paths if PurePosixPath(_normalize_container_path(path)).name == target)


def display_prop_bindings(document: object, paths: Sequence[str],
                          characters: Mapping[str, CharacterIdentity]) -> tuple[DisplayPropBinding, ...]:
    """Resolve managed-reference dictionary IDs before reading decoration relationships."""
    root = _object(document, 'CharacterDisplayConfig')
    metadata = _object(root.get('$animestudio'), 'configuration metadata')
    if metadata.get('container') != DISPLAY_RESOURCE:
        raise DisplayPropError('Display configuration is not bound to the expected source asset')
    data = _object(root.get('data'), 'data')
    keys = data.get('_keys')
    values = _rows(data.get('_values'), 'data._values')
    if (not isinstance(keys, list) or any(not isinstance(key, str) or not key for key in keys)
            or len(keys) != len(values) or len(set(keys)) != len(keys)):
        raise DisplayPropError('Display configuration dictionary keys are missing or ambiguous')
    references = _rows(_object(root.get('references'), 'references').get('RefIds'), 'references.RefIds')
    by_id = {_integer(row.get('rid'), 'reference.rid'): row for row in references}
    if len(by_id) != len(references):
        raise DisplayPropError('Display configuration repeats a managed-reference identity')
    result: list[DisplayPropBinding] = []
    for character_id, value in zip(keys, values, strict=True):
        rid = _integer(value.get('rid'), 'value.rid')
        if rid == -2:
            continue
        if rid not in by_id:
            raise DisplayPropError(f'Display configuration reference is absent: character={character_id}, rid={rid}')
        reference = by_id[rid]
        if reference.get('type') != {'class':'CharacterDisplayData', 'ns':'Beyond.Gameplay', 'asm':'Gameplay.Beyond'}:
            raise DisplayPropError(f'Unsupported display configuration reference type: character={character_id}')
        payload = _object(reference.get('data'), 'reference.data')
        if payload.get('exactTypeTreeDecoded') is not True or payload.get('charId') != character_id:
            raise DisplayPropError(f'Display configuration lacks exact identity/type-tree evidence: character={character_id}')
        config = _object(payload.get('decoItemConfig'), 'decoItemConfig')
        for index, item in enumerate(_rows(config.get('decoItemData'), 'decoItemData')):
            prefab = _text(item.get('prefabPath'), 'prefabPath')
            field: Literal['mountPoint', 'boneName'] = 'mountPoint' if 'mountPoint' in item else 'boneName'
            mount = _text(item.get(field), field)
            if 'mountPoint' in item and 'boneName' in item and item['boneName'] != mount:
                raise DisplayPropError(f'Display configuration mount fields disagree: character={character_id}, item={index}')
            matches = _resource_matches(prefab, paths)
            identity = characters.get(character_id)
            status: Literal['resolved', 'missing_resource', 'ambiguous_resource'] = (
                'resolved' if len(matches) == 1 else 'missing_resource' if not matches else 'ambiguous_resource')
            result.append(DisplayPropBinding(character_id, identity.name if identity else None,
                identity.name_id if identity else None, rid, index, prefab, field, mount, matches, status))
    return tuple(result)


def assemble_display_prop_entries(paths: Sequence[str], bindings: tuple[DisplayPropBinding, ...],
                                  providers: Mapping[str, tuple[CharacterResourceProvider, ...]]) -> tuple[DisplayPropEntry, ...]:
    """Keep unbound and unresolved candidates without inventing character relationships."""
    return tuple(DisplayPropEntry(sha256(path.encode()).hexdigest(), path, providers.get(path, ()),
        tuple(binding for binding in bindings if binding.status == 'resolved' and binding.resource_paths == (path,)),
        'not_verified') for path in sorted(set(paths)))


def _configuration(context: CharacterSourceContext, reader: Path, unity_version: str,
                   check_cancelled: CheckCancelled, progress: Progress) -> dict[str, object]:
    providers, _ = _provider_details(context.provider_index, frozenset((DISPLAY_RESOURCE,)), check_cancelled)
    selected = providers[DISPLAY_RESOURCE]
    if len(selected) != 1:
        raise DisplayPropError(f'Display configuration requires one provider: matches={len(selected)}')
    work = context.cache_root/'display_prop_configuration'/uuid4().hex
    work.mkdir(parents=True, exist_ok=False)
    prepare_providers_bundles(context, selected, work/'bundles', check_cancelled, progress)
    temporary = work/'temporary'
    temporary.mkdir()
    run_scene_process([str(reader), str(work/'bundles'), str(work/'json'), '--game', 'ArknightsEndfield',
        '--unity_version', unity_version, '--types', 'AssetBundle:Parse', 'ResourceManager:Parse', 'MonoBehaviour',
        '--export_type', 'JSON', '--mono_behaviour_type_tree_priority', 'SerializedFirst', '--group_assets', 'ByType'],
        work/'configuration_reader.log', {**os.environ, 'TEMP':str(temporary), 'TMP':str(temporary)}, check_cancelled)
    configs = tuple(row for path in (work/'json/MonoBehaviour').glob('*.json') for row in (_read(path),)
                    if _object(row.get('$animestudio'), 'configuration metadata').get('container') == DISPLAY_RESOURCE)
    if len(configs) != 1:
        raise DisplayPropError(f'Display configuration must decode exactly once: matches={len(configs)}, work={work}')
    return configs[0]


def read_display_prop_catalog(context: CharacterSourceContext, json_reader: Path, unity_version: str,
                              check_cancelled: CheckCancelled, progress: Progress) -> DisplayPropCatalog:
    """Reuse source-stamped indexes and exact configuration decoding in the shared cache."""
    check_cancelled()
    validate_character_storage(context.game_root, context.cache_root)
    if not unity_version.strip():
        raise DisplayPropError('Display prop catalog requires an explicit Unity version')
    inputs = (context.provider_index, context.resource_database, context.cab_inventory)
    before = tuple(source_stamp(path) for path in inputs)
    game_before = game_source_stamps(context.game_root)
    game_identity = sha256(json.dumps(game_before, sort_keys=True).encode()).hexdigest()
    reader_identity = tool_identity(json_reader)
    cache = CharacterDataCache(context.cache_root)
    characters = read_character_catalog_cached(context.game_root, context.provider_index, cache, check_cancelled)
    raw, _ = cache.get_or_build_json('display_prop_configuration', 'EndfieldDisplayPropConfiguration/1', inputs,
        (game_identity, reader_identity, unity_version),
        lambda: _configuration(context, json_reader, unity_version, check_cancelled, progress), check_cancelled)
    configuration = _object(raw, 'cached display configuration')
    config_identity = sha256(json.dumps(configuration, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def inventory() -> object:
        paths = display_prop_resource_paths(context.resource_database)
        providers, _ = _provider_details(context.provider_index, frozenset(paths), check_cancelled)
        return [{'resource_path':path, 'providers':[asdict(provider) for provider in providers[path]]} for path in paths]

    raw_inventory, _ = cache.get_or_build_json('display_prop_inventory', 'EndfieldDisplayPropInventory/1', inputs,
        (game_identity,), inventory, check_cancelled)
    providers: dict[str, tuple[CharacterResourceProvider, ...]] = {}
    for row in _rows(raw_inventory, 'display prop inventory'):
        path = _text(row.get('resource_path'), 'resource_path')
        if not path.startswith(RESOURCE_PREFIX) or not path.endswith('.prefab') or path in providers:
            raise DisplayPropError(f'Invalid or repeated cached display prop resource: {path}')
        providers[path] = tuple(_cache_provider(value, 'display prop provider') for value in _rows(row.get('providers'), 'providers'))
    bindings = display_prop_bindings(configuration, tuple(providers), {row.character_id:row for row in characters.characters})
    entries = assemble_display_prop_entries(tuple(providers), bindings, providers)
    verify_game_source(context.game_root, game_before, check_cancelled)
    if before != tuple(source_stamp(path) for path in inputs) or reader_identity != tool_identity(json_reader):
        raise CharacterSourceChangedError('Display prop source indexes or JSON reader changed while reading the catalog')
    identity = sha256(json.dumps({'configuration':config_identity, 'character_catalog':characters.source_identity,
        'indexes':[asdict(stamp) for stamp in before], 'game':game_identity,
        'reader':reader_identity, 'unity':unity_version, 'entries':[asdict(entry) for entry in entries]}, sort_keys=True).encode()).hexdigest()
    return DisplayPropCatalog('EndfieldDisplayPropCatalog/1', identity, characters.source_identity, config_identity,
        entries, tuple(binding for binding in bindings if binding.status != 'resolved'), before,
        game_identity, reader_identity, unity_version)


def public_display_prop_catalog(catalog: DisplayPropCatalog) -> dict[str, object]:
    """Expose selectors and proven relationships without private index paths or CAB identities."""
    return {'format':catalog.format, 'catalog_id':catalog.catalog_id, 'entries':[
        {'prop_id':entry.prop_id, 'resource_path':entry.resource_path,
         'provider_status':'ready' if len(entry.providers) == 1 else 'missing' if not entry.providers else 'ambiguous',
         'bindings':[{'character_id':row.character_id, 'character_name':row.character_name,
            'prefab_path':row.prefab_path, 'mount_field':row.mount_field, 'mount_point':row.mount_point,
            'item_index':row.item_index, 'evidence':'CharacterDisplayConfig.decoItemConfig.decoItemData'} for row in entry.bindings],
         'geometry_status':entry.geometry_status} for entry in catalog.entries],
        'unresolved_bindings':[asdict(row) for row in catalog.unresolved_bindings]}


def select_display_props(catalog: DisplayPropCatalog, prop_ids: tuple[str, ...]) -> tuple[DisplayPropEntry, ...]:
    """Require distinct current catalog IDs and unique resource providers."""
    if not prop_ids or len(set(prop_ids)) != len(prop_ids):
        raise DisplayPropError('Select one or more distinct display prop IDs')
    by_id = {entry.prop_id:entry for entry in catalog.entries}
    if len(by_id) != len(catalog.entries):
        raise DisplayPropError('Display prop catalog repeats an identity')
    missing = set(prop_ids) - by_id.keys()
    if missing:
        raise DisplayPropError(f'Display prop IDs are absent from the current catalog: {sorted(missing)}')
    selected = tuple(by_id[key] for key in prop_ids)
    for entry in selected:
        if len(entry.providers) != 1:
            raise DisplayPropError(f'Display prop requires one source provider: resource={entry.resource_path}, count={len(entry.providers)}')
    return selected
