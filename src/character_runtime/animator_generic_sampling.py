"""Sample explicit source-bound ordinary Direct children with the existing ACL reader.

This produces reusable exact clip samples, not a live Animator or a parameter
scenario. Layer selection is by serialized binding hash, never by layer index.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from animator_auxiliary_layers import number, object_row, read_json, rows
from animator_ordinary_plan import (OrdinaryLayerAssessment, assess_ordinary_layers,
    select_ordinary_routes, validate_sampled_clip_pointers)
from animator_selected_avatar_bindings import load_selected_avatar_clip, selected_avatar_bindings
from build_auxiliary_candidate import validate_clip_execution_settings
from character_animator_graph import _integer as integer

CheckCancelled = Callable[[], None]
Progress = Callable[[str, float, str], None]
LOOP_FIELDS = ('m_LoopTime', 'm_LoopBlend', 'm_LoopBlendOrientation',
               'm_LoopBlendPositionY', 'm_LoopBlendPositionXZ', 'm_CycleOffset')


@dataclass(frozen=True)
class AuxiliaryTarget:
    index: int
    cab: str
    path_id: int
    logical_provider: str


@dataclass(frozen=True)
class AuxiliarySamplingPlan:
    assessments: tuple[OrdinaryLayerAssessment, ...]
    routes: tuple[dict[str, object], ...]
    targets: tuple[AuxiliaryTarget, ...]


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Expected nonempty text: field={field}')
    return value


def request_file_path(base: Path, value: object, field: str, strict: bool) -> Path:
    path = Path(text(value, field)).expanduser()
    return (path if path.is_absolute() else base / path).resolve(strict=strict)


def logical_bundle(value: object) -> str:
    name = text(value, 'logical_provider')
    parts = name.split('/')
    if len(parts) != 2 or any(part in ('', '.', '..') or '\\' in part or ':' in part for part in parts):
        raise ValueError(f'Expected safe category/file bundle identity: logical_provider={name}')
    return name


def source_sampling_plan(tree: dict[str, object], references: dict[str, object],
                         binding_hashes: tuple[int, ...],
                         providers: tuple[tuple[str, tuple[str, ...]], ...]) -> AuxiliarySamplingPlan:
    """Select authored Direct leaves and reject ambiguous source/provider identities."""
    if any(type(value) is not int or not 0 <= value <= 0xffffffff for value in binding_hashes):
        raise ValueError('Layer binding selection requires uint32 hashes')
    assessments = assess_ordinary_layers(tree, references)
    routes = tuple(object_row(value, 'route') for value in select_ordinary_routes(assessments, binding_hashes))
    return sampling_plan_from_routes(assessments, routes, providers)


def sampling_plan_from_routes(assessments: tuple[OrdinaryLayerAssessment, ...],
                              routes: tuple[dict[str, object], ...],
                              providers: tuple[tuple[str, tuple[str, ...]], ...]) -> AuxiliarySamplingPlan:
    """Prepare exact clip providers for already validated source routes."""
    by_cab: dict[str, tuple[str, ...]] = {}
    for cab, names in providers:
        key = text(cab, 'provider.cab').casefold()
        if key in by_cab:
            raise ValueError(f'Duplicate normalized CAB inventory entry: cab={cab}')
        by_cab[key] = names
    targets: dict[int, AuxiliaryTarget] = {}
    for route in routes:
        for raw in rows(route['child_clips'], 'child_clips'):
            clip = object_row(raw, 'clip')
            index = integer(clip['index'], 'clip.index')
            cab = text(clip['cab'], 'clip.cab')
            path_id = integer(clip['path_id'], 'clip.path_id')
            if index < 0 or not re.fullmatch(r'CAB-[0-9a-fA-F]{32}', cab) or not -(1 << 63) <= path_id < (1 << 63) or path_id == 0:
                raise ValueError(f'Invalid exact clip identity: index={index}, cab={cab}, path_id={path_id}')
            choices = by_cab.get(cab.casefold(), ())
            if len(choices) != 1:
                raise ValueError(f'Exact clip CAB needs one provider: cab={cab}, providers={choices}')
            target = AuxiliaryTarget(index, cab, path_id, logical_bundle(choices[0]))
            if index in targets and targets[index] != target:
                raise ValueError(f'Conflicting controller clip identity: index={index}')
            targets[index] = target
    return AuxiliarySamplingPlan(assessments, routes, tuple(targets.values()))


def write_json(path: Path, record: object) -> None:
    with path.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, indent=2, allow_nan=False)


def verified_reused_bundle(database: Path, prepared_root: Path, logical: str) -> Path:
    """Compare reused bytes with the current resource database payload checksum."""
    root = prepared_root.resolve(strict=True)
    path = (root / logical_bundle(logical)).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(root):
        raise ValueError(f'Prepared bundle escapes selected directory or is not a file: logical={logical}')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        matches = connection.execute('SELECT data_md5,byte_length FROM bundles WHERE lower(logical_name)=?', (logical.casefold(),)).fetchall()
    if len(matches) != 1:
        raise ValueError(f'Prepared bundle requires one current resource row: logical={logical}, rows={len(matches)}')
    expected, length = matches[0]
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'md5').hexdigest()
    if digest.casefold() != str(expected).casefold() or path.stat().st_size != length:
        raise ValueError(f'Prepared bundle differs from current indexed VFS payload: logical={logical}, expected_md5={expected}, actual_md5={digest}')
    return path


def sample_auxiliary_layers(request_path: Path, check_cancelled: CheckCancelled,
                            progress: Progress) -> dict[str, object]:
    """Prepare exact roots, execute the configured sampler and publish verified samples."""
    request_path = request_path.resolve(strict=True)
    request = object_row(read_json(request_path), 'request')
    version = request.get('format')
    if version not in ('EndfieldGenericAuxiliarySamplingRequest/1', 'EndfieldGenericAuxiliarySamplingRequest/2'):
        raise ValueError('Expected EndfieldGenericAuxiliarySamplingRequest/1 or /2')
    path_fields = ('repository_src', 'game_root', 'cache_root', 'resource_database', 'cab_inventory',
                   'controller_type_tree', 'controller_source_references', 'source_audit',
                   'selected_avatar', 'source_hierarchy', 'dotnet', 'sampler')
    paths = {field: request_file_path(request_path.parent, request[field], field, field != 'cache_root') for field in path_fields}
    if version == 'EndfieldGenericAuxiliarySamplingRequest/2':
        paths['state_scenario'] = request_file_path(request_path.parent, request['state_scenario'], 'state_scenario', True)
    for field in path_fields:
        if field not in ('repository_src', 'game_root', 'cache_root') and not paths[field].is_file():
            raise ValueError(f'Sampling input must be a file: field={field}, path={paths[field]}')
    for field in ('repository_src', 'game_root'):
        if not paths[field].is_dir():
            raise ValueError(f'Sampling input must be a directory: field={field}, path={paths[field]}')
    output = request_file_path(request_path.parent, request['output'], 'output', False)
    prepared_value = request['prepared_bundle_root']
    prepared = None if prepared_value is None else request_file_path(request_path.parent, prepared_value, 'prepared_bundle_root', True)
    if output.exists() or any(path.is_relative_to(output) for path in paths.values()) or (prepared is not None and prepared.is_relative_to(output)):
        raise ValueError(f'Sampling needs a new output directory separate from its inputs: output={output}')
    maximum = integer(request['maximum_stored_frames'], 'maximum_stored_frames')
    if not 1 <= maximum <= 100000:
        raise ValueError('Explicit stored frame budget must be in 1..100000')
    expected = object_row(request['controller_identity'], 'controller_identity')
    references = object_row(read_json(paths['controller_source_references']), 'references')
    if (text(references['source'], 'controller.source').casefold() != text(expected['cab'], 'identity.cab').casefold()
            or integer(references['controller_path_id'], 'controller.path_id') != integer(expected['path_id'], 'identity.path_id')):
        raise ValueError('Selected controller CAB/PathID disagrees with exact source references')
    hashes = object_row(request['controller_input_sha256'], 'controller_input_sha256')
    for field in ('controller_type_tree', 'controller_source_references'):
        digest = text(hashes[field], field + '.sha256')
        if not re.fullmatch('[0-9a-fA-F]{64}', digest) or sha256(paths[field]) != digest.lower():
            raise ValueError(f'Selected controller source bytes changed: field={field}')
    sys.path.insert(0, str(paths['repository_src']))
    from endfield_character_cache import CharacterDataCache, source_stamp
    from endfield_character_sources import game_source_stamps, validate_character_storage, verify_game_source
    from endfield_scene_export import run_scene_process
    from extractor_core.resources import extract_candidate_bundles
    for target in (paths['cache_root'], output):
        validate_character_storage(paths['game_root'], target)
    check_cancelled()
    game_generation = game_source_stamps(paths['game_root'])
    immutable = tuple(path for field, path in paths.items() if field not in ('repository_src', 'game_root', 'cache_root'))
    immutable += tuple(sorted(paths['sampler'].parent.glob('*.dll')))
    immutable += (request_path.resolve(strict=True),)
    module_root = Path(__file__).parent
    immutable += tuple(module_root / name for name in ('animator_generic_sampling.py',
        'animator_ordinary_plan.py', 'character_animator_graph.py', 'animator_selected_avatar_bindings.py',
        'animator_clip_references.py', 'build_auxiliary_candidate.py', 'animator_auxiliary_layers.py',
        'animator_ordinary_source.py', 'animator_clip_delta.py', 'animator_ordinary_direct.py',
        'animator_ordinary_layers.py', 'animator_direct_parameters.py', 'animator_weighted_channels.py'))
    if version == 'EndfieldGenericAuxiliarySamplingRequest/2':
        immutable += tuple(module_root / name for name in ('character_animation_state_export.py',
            'animator_state_schedule.py', 'animator_state_clock.py', 'animator_state_pose.py', 'build_native_state_clock_candidate.py'))
    immutable = tuple(dict.fromkeys(immutable))
    stamps = {path: source_stamp(path) for path in immutable}
    input_hashes = {str(path): sha256(path) for path in immutable}
    for field in ('controller_type_tree', 'controller_source_references'):
        if input_hashes[str(paths[field])] != text(hashes[field], field + '.sha256').lower():
            raise ValueError(f'Selected controller source changed before planning: field={field}')
    index, fingerprint = CharacterDataCache(paths['cache_root']).read_character_cab_index(paths['cab_inventory'], check_cancelled)
    bindings = tuple(integer(value, 'layer_binding_hash') for value in rows(request['layer_binding_hashes'], 'layer_binding_hashes'))
    plan = source_sampling_plan(object_row(read_json(paths['controller_type_tree']), 'tree'), references, bindings, index.providers)
    direct_indices = {target.index for target in plan.targets}
    state_indices: set[int] = set()
    if version == 'EndfieldGenericAuxiliarySamplingRequest/2':
        from character_animation_state_export import read_state_scenario, state_sampling_routes
        state_routes = state_sampling_routes(object_row(read_json(paths['controller_type_tree']), 'tree'),
            references, bindings, read_state_scenario(paths['state_scenario']), rows(read_json(paths['source_hierarchy']), 'hierarchy'))
        state_indices = {integer(object_row(clip, 'state.clip')['index'], 'clip.index')
            for route in state_routes for clip in rows(route['child_clips'], 'state.clips')}
        if state_indices & direct_indices:
            raise ValueError('Looping state clips cannot share ordinary Direct execution settings')
        plan = sampling_plan_from_routes(plan.assessments, plan.routes + state_routes, index.providers)
    check_cancelled()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'source_plan.json', {'format': 'EndfieldGenericAuxiliarySourcePlan/1',
        'controller_identity': expected, 'layer_binding_hashes': list(bindings),
        'cab_inventory_sha256': fingerprint.sha256, **asdict(plan)})
    logical_names = tuple(dict.fromkeys(target.logical_provider for target in plan.targets))
    progress('auxiliary_source', .15, 'Selected exact source Direct child objects')
    bundle_paths: dict[str, Path] = {}
    missing: list[str] = []
    for name in logical_names:
        if prepared is not None and (prepared / name).exists():
            bundle_paths[name] = verified_reused_bundle(paths['resource_database'], prepared, name)
        else:
            missing.append(name)
    def cancelled() -> bool:
        check_cancelled()
        return False
    if missing:
        extract_candidate_bundles(paths['resource_database'], paths['game_root'] / 'Endfield_Data/StreamingAssets/VFS',
                                  missing, output / 'bundles', progress=None, cancelled=cancelled)
        bundle_paths.update({name: output / 'bundles' / name for name in missing})
    bundle_stamps = {path: source_stamp(path) for path in bundle_paths.values()}
    bundle_hashes = {str(path): sha256(path) for path in bundle_stamps}
    targets = [{**asdict(target), 'bundle': str(bundle_paths[target.logical_provider])} for target in plan.targets]
    sampler_request = {'format': 'EndfieldAuxiliaryClipSampling/1',
        'unity_version': text(request['unity_version'], 'unity_version'), 'maximum_stored_frames': maximum,
        'source_audit': str(paths['source_audit']), 'output': str(output / 'sampled'), 'targets': targets}
    write_json(output / 'sampling_request.json', sampler_request)
    temporary = output / 'temp'
    temporary.mkdir()
    environment = dict(os.environ, TEMP=str(temporary), TMP=str(temporary), DOTNET_CLI_HOME=str(temporary))
    progress('auxiliary_decode', .35, 'Decoding selected exact clips with the configured ACL sampler')
    run_scene_process([str(paths['dotnet']), str(paths['sampler']), str(output / 'sampling_request.json')],
                      output / 'sampling.log', environment, check_cancelled)
    samples = output / 'sampled'
    validate_sampled_clip_pointers(list(plan.routes), samples)
    avatar = object_row(read_json(paths['selected_avatar']), 'avatar')
    hierarchy = rows(read_json(paths['source_hierarchy']), 'hierarchy')
    clips: list[object] = []
    settings: list[object] = []
    for target in plan.targets:
        check_cancelled()
        source = object_row(read_json(samples / str(target.index) / 'clip_source.json'), 'clip.source')
        bindings_result = selected_avatar_bindings(source, avatar, hierarchy)
        decoded = load_selected_avatar_clip(samples / str(target.index), avatar, hierarchy)
        settings.append({key: source[key] for key in ('cab', 'pathId', 'rawDataSha256', *LOOP_FIELDS)} | {'index': target.index})
        clips.append({**asdict(target), 'name': source['m_Name'], 'raw_data_sha256': source['rawDataSha256'],
            'frames': len(decoded.times), 'mapped_tracks': len(decoded.paths),
            'unmapped_tracks_skipped': sum(row.avatar_index == -1 for row in bindings_result),
            'full_reader_binding_validation': read_json(samples / str(target.index) / 'verification_status.json')})
    execution_settings = {'format': 'EndfieldAuxiliaryClipExecutionSettings/1', 'clips': settings}
    direct_settings = {'format': 'EndfieldAuxiliaryClipExecutionSettings/1',
        'clips': [row for row in settings if object_row(row, 'setting')['index'] in direct_indices]}
    validate_clip_execution_settings(direct_settings, samples, direct_indices)
    for row in settings:
        setting = object_row(row, 'setting')
        if setting['index'] in state_indices and (setting['m_LoopTime'] is not True or
                any(setting[field] is not False for field in LOOP_FIELDS[1:5]) or number(setting['m_CycleOffset'], 'clip.cycle_offset') != 0.):
            raise ValueError(f'Native state clip loop correction or offset is unsupported: index={setting["index"]}')
    write_json(output / 'clip_execution_settings.json', execution_settings)
    if state_indices:
        write_json(output / 'direct_clip_execution_settings.json', direct_settings)
    for path, stamp in {**stamps, **bundle_stamps}.items():
        if source_stamp(path) != stamp or sha256(path) != (input_hashes | bundle_hashes)[str(path)]:
            raise ValueError(f'Sampling input changed before publication: path={path}')
    verify_game_source(paths['game_root'], game_generation, check_cancelled)
    receipt = {'format': 'EndfieldGenericAuxiliarySamplingReceipt/1', 'status': 'exact_children_sampled_selected_avatar_validated',
        'controller_identity': expected, 'layer_binding_hashes': list(request['layer_binding_hashes']),
        'source_plan': str(output / 'source_plan.json'), 'clip_samples_root': str(samples),
        'clip_execution_settings': str(output / ('direct_clip_execution_settings.json' if state_indices else 'clip_execution_settings.json')), 'clips': clips,
        'input_sha256': input_hashes | bundle_hashes,
        'output_sha256': {str(path): sha256(path) for path in sorted(output.rglob('*')) if path.is_file()},
        'runtime_animator_reconstructed': False,
        'pending': ['Explicit source parameter frames and layer weights are required by the ordinary consumer',
                    'This adapter does not choose effective controller, live states, clocks, or Animator/IK evaluation ordering']}
    write_json(output / 'receipt.json', receipt)
    progress('auxiliary_ready', 1., 'Exact auxiliary samples and execution settings verified')
    return receipt


def check_running() -> None:
    return None


def print_progress(stage: str, fraction: float, message: str) -> None:
    print(json.dumps({'stage': stage, 'fraction': fraction, 'message': message}), flush=True)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise ValueError('Expected one EndfieldGenericAuxiliarySamplingRequest/1 file')
    sample_auxiliary_layers(Path(sys.argv[1]), check_running, print_progress)
