"""Prepare current-source potential overlays and display-effect dependencies."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
import argparse
import json
import os
from pathlib import Path
import sqlite3
import shutil

from character_runtime.character_potential_source import display_effects, mount_ancestry, read_document, record, text
from endfield_character_cache import CharacterDataCache, character_bundle_closure, source_stamp
from endfield_character_prepare import _fingerprint, _logical_bundle_name
from endfield_character_selection import CharacterSelection, resolve_character_selection
from endfield_character_catalog import read_character_catalog_cached
from endfield_character_sources import CharacterSourceContext, game_source_stamps, validate_character_storage, verify_game_source
from endfield_scene_discovery import observe_helper, tool_identity
from endfield_scene_export import run_scene_process
from extractor_core.resources import extract_candidate_bundles


DISPLAY_RESOURCE = 'assets/beyond/dynamicassets/gamedata/gameplayconfig/characterdisplayconfig.asset'
MANIFEST_FORMAT = 'EndfieldCharacterPotential/1'
CheckCancelled = Callable[[], None]
Progress = Callable[[str, float, str], None]


def _identity(path: Path, check_cancelled: CheckCancelled) -> dict[str, str | int]:
    fingerprint = _fingerprint(path, check_cancelled)
    return {'path': fingerprint.path, 'bytes': fingerprint.bytes, 'sha256': fingerprint.sha256}


def _providers(path: Path, resources: tuple[str, ...], check_cancelled: CheckCancelled) -> dict[str, str]:
    matches: dict[str, set[str]] = {resource.casefold(): set() for resource in resources}
    terminal: dict[str, object] = {}
    terminal_line = 0
    line_number = 0
    with path.open(encoding='utf-8-sig') as stream:
        for line_number, line in enumerate(stream, start=1):
            if line_number % 1024 == 0:
                check_cancelled()
            row = record(json.loads(line), f'provider line {line_number}')
            if row.get('kind') == 'complete':
                terminal = dict(row)
                terminal_line = line_number
            elif row.get('kind') == 'error':
                raise ValueError(f'Potential provider index records an error: line={line_number}, record={row}')
            elif row.get('kind') == 'match':
                containers = row.get('containers')
                if not isinstance(containers, list) or any(type(item) is not str for item in containers):
                    raise ValueError(f'Potential provider containers are invalid: line={line_number}')
                for container in containers:
                    key = container.replace('\\', '/').casefold()
                    if key in matches:
                        matches[key].add(_logical_bundle_name(text(row.get('logical_name'), 'logical_name'), 'provider'))
    if (terminal_line != line_number or terminal.get('kind') != 'complete' or terminal.get('failed') != 0
            or type(terminal.get('scanned')) is not int or terminal['scanned'] <= 0):
        raise ValueError(f'Potential provider scan is incomplete: source={path}, terminal={terminal}')
    if any(len(found) != 1 for found in matches.values()):
        raise ValueError(f'Potential source resource providers must resolve once: matches={matches}')
    return {resource: next(iter(found)) for resource, found in matches.items()}


def _extract(source: CharacterSourceContext, names: tuple[str, ...], output: Path,
             check_cancelled: CheckCancelled, progress: Progress) -> None:
    with sqlite3.connect(source.resource_database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        sizes = tuple(connection.execute('SELECT byte_length FROM bundles WHERE logical_name=?', (name,)).fetchone() for name in names)
    if any(row is None or type(row[0]) is not int or row[0] < 0 for row in sizes):
        raise ValueError('Potential extraction roots contain absent or invalid resource-database records')
    total_bytes = sum(row[0] for row in sizes if row is not None)
    if len(names) > 256 or total_bytes > 256_000_000:
        raise ValueError(f'Potential source dependency budget exceeded: bundles={len(names)}, bytes={total_bytes}')
    free_bytes = shutil.disk_usage(output.parent).free
    required = total_bytes * 8 + 128_000_000
    if free_bytes < required:
        raise OSError(f'Potential preparation has insufficient free space: output={output}, free_bytes={free_bytes}, required_bytes={required}')
    def cancelled() -> bool:
        check_cancelled()
        return False

    def emit(phase: str, fraction: float, message: str, details: dict[str, object] | None) -> None:
        progress('potential_extract', fraction, message)

    extract_candidate_bundles(source.resource_database, source.game_root / 'Endfield_Data/StreamingAssets/VFS',
                              list(names), output, progress=emit, cancelled=cancelled)


def prepare_character_potential(source: CharacterSourceContext, selection: CharacterSelection,
                                json_reader: Path, scene_probe: Path, output: Path,
                                check_cancelled: CheckCancelled, progress: Progress) -> Path:
    """Discover selected display configuration and produce a portable build manifest."""
    check_cancelled()
    target = output.resolve()
    validate_character_storage(source.game_root, target)
    validate_character_storage(source.game_root, source.cache_root)
    for input_path in (source.provider_index, source.resource_database, source.cab_inventory, json_reader, scene_probe):
        if not input_path.is_file():
            raise FileNotFoundError(f'Potential preparation input is missing: path={input_path}')
        if target == input_path.resolve().parent or target.is_relative_to(input_path.resolve().parent):
            raise ValueError(f'Potential preparation output overlaps an input directory: output={target}, input={input_path}')
    before_game = game_source_stamps(source.game_root)
    before_indexes = tuple(source_stamp(path) for path in (source.provider_index, source.resource_database, source.cab_inventory))
    cache = CharacterDataCache(source.cache_root / 'potential')
    catalog = read_character_catalog_cached(source.game_root, source.provider_index, cache, check_cancelled)
    resolve_character_selection(catalog, selection)
    providers = _providers(source.provider_index, (DISPLAY_RESOURCE, selection.resource_path), check_cancelled)
    if providers[selection.resource_path.casefold()] != selection.logical_provider:
        raise ValueError('Potential mount provider differs from the selected character provider')
    target.mkdir(parents=True, exist_ok=False)
    progress('potential_config', .1, 'Reading current source potential configuration and mount hierarchy')
    inputs = target / 'configuration_bundles'
    _extract(source, tuple(sorted(set(providers.values()))), inputs, check_cancelled, progress)
    json_output = target / 'configuration_json'
    command = [str(json_reader.resolve()), str(inputs), str(json_output), '--game', 'ArknightsEndfield',
               '--unity_version', '2021.3.34f5', '--types', 'AssetBundle:Parse', 'ResourceManager:Parse',
               'GameObject', 'Transform', 'MonoBehaviour', '--export_type', 'JSON',
               '--mono_behaviour_type_tree_priority', 'SerializedFirst', '--group_assets', 'ByType']
    temporary = target / 'temporary'
    temporary.mkdir()
    run_scene_process(command, target / 'configuration_reader.log',
                      {**os.environ, 'TEMP': str(temporary), 'TMP': str(temporary)}, check_cancelled)
    configs = tuple(document for path in (json_output / 'MonoBehaviour').glob('*.json')
                    for document in (read_document(path),)
                    if record(document.get('$animestudio'), 'metadata').get('container') == DISPLAY_RESOURCE)
    if len(configs) != 1:
        raise ValueError(f'Potential display config must resolve exactly once: matches={len(configs)}')
    effects = display_effects(configs[0], selection.character_id)
    mounts = tuple(mount_ancestry(json_output, selection.resource_path, effect.mount_point) for effect in effects)
    character_roots = tuple(document for path in (json_output / 'GameObject').glob('*.json')
                            for document in (read_document(path),)
                            if record(document.get('$animestudio'), 'metadata').get('container') == selection.resource_path
                            and document.get('m_Name') == selection.part_name
                            and record(record(document.get('m_Transform'), 'm_Transform').get('m_Father'), 'm_Father').get('m_PathID') == 0)
    if len(character_roots) != 1:
        raise ValueError(f'Potential source character root must resolve once: part={selection.part_name}, matches={len(character_roots)}')
    character_root = character_roots[0]
    resources = tuple(f'assets/beyond/dynamicassets/gameplay/effects/prefabs/{effect.name.casefold()}.prefab' for effect in effects)
    roots = _providers(source.provider_index, resources, check_cancelled) if resources else {}
    scene_manifest: Path | None = None
    closure: tuple[str, ...] = ()
    unresolved: object = ()
    unresolved_count = 0
    if roots:
        cab_index, _cab_fingerprint = cache.read_character_cab_index(source.cab_inventory, check_cancelled)
        closures, metadata = character_bundle_closure(cab_index, {'potential': sorted(set(roots.values()))})
        closure = tuple(closures['potential'])
        unresolved = metadata['maps']['potential']['unresolvedExternalSample']
        unresolved_count = metadata['maps']['potential']['unresolvedExternalCount']
        with sqlite3.connect(source.resource_database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
            sizes = tuple(connection.execute('SELECT byte_length FROM bundles WHERE logical_name=?', (name,)).fetchone() for name in closure)
        if any(row is None or type(row[0]) is not int for row in sizes):
            raise ValueError('Potential effect closure contains absent resource-database records')
        total_bytes = sum(row[0] for row in sizes if row is not None)
        if len(closure) > 256 or total_bytes > 256_000_000:
            raise ValueError(f'Potential source dependency budget exceeded: bundles={len(closure)}, bytes={total_bytes}')
        progress('potential_effects', .4, 'Extracting exact source display effect dependencies')
        bundles = target / 'effect_bundles'
        _extract(source, closure, bundles, check_cancelled, progress)
        probe_output = target / 'effects'
        observe_helper([str(scene_probe.resolve()), str(bundles), str(probe_output)],
                       target / 'effect_probe.log', target / 'temporary', check_cancelled,
                       lambda row: progress('potential_effects', .7, 'Preparing display effect geometry and materials'))
        scene_manifest = probe_output / 'scene_manifest.json'
        scene = read_document(scene_manifest)
        if scene.get('Format') != 'EndfieldSceneProbe/1':
            raise ValueError(f'Potential effect probe returned an unsupported manifest: path={scene_manifest}')
    verify_game_source(source.game_root, before_game, check_cancelled)
    if before_indexes != tuple(source_stamp(path) for path in (source.provider_index, source.resource_database, source.cab_inventory)):
        raise ValueError('Potential source indexes changed during preparation')
    manifest = {
        'format': MANIFEST_FORMAT, 'status': 'source_prepared_requires_blender',
        'selection': asdict(selection), 'display_configuration': _identity(next(path for path in (json_output / 'MonoBehaviour').glob('*.json')
            if record(read_document(path).get('$animestudio'), 'metadata').get('container') == DISPLAY_RESOURCE), check_cancelled),
        'source_root': {'name': selection.part_name, 'source': text(record(character_root.get('$animestudio'), 'metadata').get('sourceFile'), 'sourceFile')},
        'source_indexes': [_identity(path, check_cancelled) for path in (source.provider_index, source.resource_database, source.cab_inventory)],
        'game_stamps': before_game, 'json_reader_identity': tool_identity(json_reader), 'scene_probe_identity': tool_identity(scene_probe),
        'scene_manifest': scene_manifest.relative_to(target).as_posix() if scene_manifest else None,
        'effect_root_bundles': roots, 'effect_closure_bundles': closure,
        'unresolved_external_count': unresolved_count, 'unresolved_external_sample': unresolved,
        'effects': [{'effect': asdict(effect), 'ancestry': [asdict(node) for node in chain]}
                    for effect, chain in zip(effects, mounts, strict=True)],
        'artifacts': [_identity(path, check_cancelled) for folder in (target / 'configuration_bundles', target / 'effects')
                      if folder.exists() for path in sorted(folder.rglob('*')) if path.is_file()],
        'scope': 'Source selected display configuration and dependencies; Blender builds fixed emitter snapshots with explicit pending features.',
    }
    result = target / 'potential_manifest.json'
    verify_game_source(source.game_root, before_game, check_cancelled)
    if before_indexes != tuple(source_stamp(path) for path in (source.provider_index, source.resource_database, source.cab_inventory)):
        raise ValueError('Potential source indexes changed while recording artifacts')
    result.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    progress('potential_completed', 1., 'Potential source manifest prepared')
    return result


def main() -> None:
    """Accept the public export request without importing Blender's runtime modules."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    request_path = args.request.resolve(strict=True)
    request = read_document(request_path)
    if request.get('format') != 'EndfieldCharacterExportRequest/2':
        raise ValueError('Potential preparation requires EndfieldCharacterExportRequest/2')

    def source_path(field: str) -> Path:
        return (request_path.parent / text(request.get(field), field)).resolve(strict=True)

    catalog_path = source_path('catalog')
    catalog = read_document(catalog_path)
    provider_source = record(catalog.get('provider_index_source'), 'catalog.provider_index_source')
    provider_path = (catalog_path.parent / text(provider_source.get('path'), 'catalog.provider_index_source.path')).resolve(strict=True)
    if request.get('unity_version') != '2021.3.34f5':
        raise ValueError(f'Potential reader requires the supported explicit Unity version: value={request.get("unity_version")!r}')
    variant = request.get('variant')
    if variant not in ('characters', 'npc'):
        raise ValueError(f'Potential request requires an explicit character variant: variant={variant!r}')
    cache_value = request.get('cache_dir')
    cache_root = ((request_path.parent / text(cache_value, 'cache_dir')).resolve() if cache_value is not None
                  else args.output.resolve().parent / '.potential_source_cache')
    source = CharacterSourceContext(source_path('game_root'), source_path('blender'), cache_root,
                                    args.output.resolve().parent, provider_path, source_path('database'), source_path('cab_inventory'))
    selection = CharacterSelection(text(catalog.get('source_identity'), 'catalog.source_identity'),
        text(request.get('character_id'), 'character_id'), text(request.get('template_id'), 'template_id'),
        text(request.get('part_name'), 'part_name'), 'characters' if variant == 'characters' else 'npc',
        text(request.get('resource_path'), 'resource_path'), text(request.get('provider'), 'provider'))

    def check_cancelled() -> None:
        return None

    def progress(stage: str, fraction: float, message: str) -> None:
        print(json.dumps({'format': 'EndfieldPotentialPreparationProgress/1', 'stage': stage,
                          'fraction': fraction, 'message': message}), flush=True)

    result = prepare_character_potential(source, selection, source_path('potential_json_reader'),
                                          source_path('potential_scene_probe'), args.output, check_cancelled, progress)
    print(json.dumps({'format': MANIFEST_FORMAT, 'manifest': str(result)}), flush=True)


if __name__ == '__main__':
    main()
