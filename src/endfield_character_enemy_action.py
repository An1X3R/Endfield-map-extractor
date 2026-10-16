"""Decode an exact enemy controller clip and reuse the calibrated prefab Action binder."""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import shutil

from endfield_character_cache import CharacterDataCache, source_stamp
from endfield_character_npc_animation import npc_avatar_source_transforms
from endfield_character_npc_assembly import NpcSourceObject
from endfield_character_npc_prefab_action import verify_prefab_action_delivery
from endfield_character_selection import CharacterSelection
from endfield_character_sources import CharacterSourceContext, game_source_stamps, verify_game_source
from endfield_scene_export import run_scene_process
from endfield_scene_resources import write_index
from extractor_core.resources import extract_candidate_bundles
from webui_character_contract import json_object, required_text


def _sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _read(path: Path) -> dict[str, object]:
    return json_object(json.loads(path.read_text(encoding='utf-8-sig')), str(path))


def _evidence(path: Path) -> dict[str, str]:
    return {'path': str(path.resolve(strict=True)), 'sha256': _sha(path)}


def _copy_source(row: dict[str, object], output: Path, name: str) -> Path:
    source = Path(required_text(row.get('source_json'), name + '.source_json')).resolve(strict=True)
    expected = required_text(row.get('source_json_sha256'), name + '.source_json_sha256')
    if _sha(source) != expected:
        raise ValueError(f'Enemy animation source JSON changed: field={name}, path={source}')
    destination = output / (name + '.json')
    shutil.copyfile(source, destination)
    if _sha(destination) != expected:
        raise ValueError(f'Enemy animation evidence copy differs: path={destination}')
    return destination


def run_enemy_prefab_action(
    context: CharacterSourceContext, selection: CharacterSelection,
    selection_document: dict[str, object], payload: Path, output_dir: Path,
    defaults: dict[str, object], tools: dict[str, Path], options: dict[str, object],
    check_cancelled: Callable[[], None], progress: Callable[[str, float, str], None],
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Keep controller membership, source Avatar and saved-frame checks on the shared chain."""
    expected_selection = {'format': 'EndfieldEnemyAnimationSource/1',
                          'source_identity': selection.catalog_identity, 'npc_id': selection.character_id,
                          'template_id': selection.template_id, 'part_name': selection.part_name}
    if any(selection_document.get(key) != value for key, value in expected_selection.items()):
        raise ValueError('Enemy action selection differs from the exact prefab source')
    actions = selection_document.get('actions')
    if not isinstance(actions, list) or len(actions) != 1:
        raise ValueError('Enemy Action requires exactly one authenticated controller clip')
    action = json_object(actions[0], 'selected enemy action')
    if action.get('action_id') != options.get('action_id'):
        raise ValueError('Enemy action_id differs from its selected controller clip')
    frames = options.get('maximum_stored_frames')
    if type(frames) is not int or not 2 <= frames <= 18000:
        raise ValueError('Enemy maximum_stored_frames must be an integer from 2 to 18000')
    controller = json_object(selection_document.get('controller'), 'enemy controller')
    clip_kind = required_text(action.get('controller_clip_kind'), 'action.controller_clip_kind')
    if clip_kind not in ('base', 'override'):
        raise ValueError(f'Unsupported enemy controller clip kind: {clip_kind}')
    if clip_kind == 'base' and controller.get('type') == 'AnimatorOverrideController':
        controller = json_object(selection_document.get('base_controller'), 'base controller clip owner')
    avatar = json_object(selection_document.get('selected_avatar'), 'enemy Avatar')
    controller_type = required_text(controller.get('type'), 'controller.type')
    if controller_type not in ('AnimatorController', 'AnimatorOverrideController'):
        raise ValueError(f'Unsupported enemy controller type: {controller_type}')
    static_inputs = {'source_blend': payload / 'build/character.blend',
                     'static_source_audit': payload / 'prepared/static_probe/source_audit.json',
                     'static_build_receipt': payload / 'build/build_receipt.json',
                     'preparation_receipt': payload / 'prepared/preparation_receipt.json'}
    input_paths = (*static_inputs.values(), context.provider_index, context.resource_database,
                   context.cab_inventory, *tools.values(), Path(__file__))
    stamps = tuple(source_stamp(path) for path in input_paths)
    game_before = game_source_stamps(context.game_root)
    output = output_dir / 'enemy_animation'
    output.mkdir(parents=True, exist_ok=False)
    temporary = output / 'temp'
    temporary.mkdir()
    environment = {**os.environ, 'TEMP': str(temporary), 'TMP': str(temporary), 'PYTHONDONTWRITEBYTECODE': '1'}
    selection_path = output / 'selection_source.json'
    write_index(selection_path, selection_document)
    source_paths = {'selection_source': selection_path,
                    'controller_source': _copy_source(controller, output, 'controller_source'),
                    'avatar_source': _copy_source(avatar, output, 'avatar_source'),
                    'clip_source': _copy_source(action, output, 'clip_source')}
    cache = CharacterDataCache(context.cache_root)
    cab_index, _ = cache.read_character_cab_index(context.cab_inventory, check_cancelled)
    avatar_cab = required_text(avatar.get('cab'), 'selected_avatar.cab')
    avatar_providers = dict(cab_index.providers).get(avatar_cab.casefold(), ())
    if len(avatar_providers) != 1:
        raise ValueError(f'Enemy Avatar needs one exact CAB provider: cab={avatar_cab}, providers={avatar_providers}')
    avatar_provider = avatar_providers[0]
    controller_provider = required_text(controller.get('logical_provider'), 'controller.logical_provider')
    clip_provider = required_text(action.get('logical_provider'), 'action.logical_provider')
    providers = {avatar_provider, controller_provider, clip_provider}
    base_controller = selection_document.get('base_controller')
    if base_controller is not None:
        base = json_object(base_controller, 'base_controller')
        source_paths['base_controller_source'] = _copy_source(base, output, 'base_controller_source')
        providers.add(required_text(base.get('logical_provider'), 'base_controller.logical_provider'))
    progress('extracting', .77, 'Preparing the exact enemy controller clip and Avatar')
    bundles = output / 'bundles'
    extract_candidate_bundles(context.resource_database, context.game_root / 'Endfield_Data/StreamingAssets/VFS',
                              sorted(providers), bundles)
    check_cancelled()
    avatar_value = _read(source_paths['avatar_source'])
    avatar_object = NpcSourceObject(source_paths['avatar_source'], avatar_value,
                                   json_object(avatar_value.get('$animestudio'), 'Avatar metadata'))
    source = {'format': 'EndfieldEnemyAnimationSource/1', 'runtime_animator_reconstructed': False,
              'character_id': selection.character_id, 'catalog_source_identity': selection.catalog_identity,
              'controller_bundle': str(bundles / controller_provider),
              'controller_clip_kind': clip_kind,
              'controller_clip_index': action['controller_clip_index'], 'original_clip': action['original_clip'],
              'static_source_audit': _evidence(static_inputs['static_source_audit']),
              'preparation_receipt': _evidence(static_inputs['preparation_receipt']),
              **{field: _evidence(path) for field, path in source_paths.items()},
              'transforms': npc_avatar_source_transforms(avatar_object, 'NPC_Root')}
    if base_controller is not None:
        source['base_controller_bundle'] = str(bundles / required_text(
            json_object(base_controller, 'base_controller').get('logical_provider'), 'base_controller.logical_provider'))
    source_path = output / 'enemy_source.json'
    write_index(source_path, source)
    animation_request = {'format': 'EndfieldAnimationExportRequest/1', 'character_id': selection.character_id,
        'avatar_bundle': str(bundles / avatar_provider), 'avatar_cab': avatar_cab, 'avatar_path_id': int(str(avatar['path_id'])),
        'clip_bundle': str(bundles / clip_provider), 'resource_path': action['resource_path'],
        'source_audit': str(source_path), 'animator_root_path': 'NPC_Root', 'controller_bundle': None,
        'unity_version': defaults['unity_version'], 'maximum_stored_frames': frames,
        'output': str(output / 'decoded'), 'dotnet': str(tools['dotnet']), 'reader': str(tools['reader'])}
    request_path = output / 'animation_request.json'
    write_index(request_path, animation_request)
    run_scene_process([str(defaults['python']), str(tools['animation_exporter']), '--request', str(request_path)],
                      output / 'animation.log', environment, check_cancelled)
    animation_receipt_path = output / 'decoded/receipt.json'
    decoded = _read(animation_receipt_path)
    expected = {'status': 'decoded_bound_and_merged_candidate', 'character_id': selection.character_id,
                'clip_path_id': int(str(action['clip_path_id'])), 'avatar_path_id': int(str(avatar['path_id'])),
                'resource_path': action['resource_path']}
    if (any(decoded.get(key) != value for key, value in expected.items())
            or str(decoded.get('clip_source')).casefold() != str(action['clip_cab']).casefold()
            or str(decoded.get('avatar_cab')).casefold() != avatar_cab.casefold()):
        raise ValueError('Decoded enemy animation differs from the selected source clip or Avatar')
    npc_receipt = {'format': 'EndfieldNpcAnimationReceipt/1', 'status': 'exact_enemy_controller_clip_exported',
        'npc_id': selection.character_id, 'template_id': selection.template_id, 'part_name': selection.part_name,
        'catalog_source_identity': selection.catalog_identity, 'state_name': action['state_name'],
        'action_id': action['action_id'], 'avatar_identity_matches': True, 'runtime_animator_reconstructed': False,
        'source': _evidence(source_path), 'animation_receipt': _evidence(animation_receipt_path)}
    npc_receipt_path = output / 'enemy_animation_receipt.json'
    write_index(npc_receipt_path, npc_receipt)
    inputs = {**static_inputs, 'animation_receipt': animation_receipt_path, 'npc_animation_receipt': npc_receipt_path}
    build_output = output_dir / 'enemy_action_build'
    build_request = output / 'build_request.json'
    write_index(build_request, {'format': 'EndfieldPrefabNpcActionBuild/1',
                               **{key: str(path) for key, path in inputs.items()}, 'output': str(build_output)})
    progress('building', .87, 'Attaching the enemy Action to the original prefab rig')
    run_scene_process([str(context.blender), '--background', '--factory-startup', '--python-exit-code', '1',
                       '--python', str(tools['prefab_action_builder']), '--', '--request', str(build_request)],
                      output / 'build.log', environment, check_cancelled)
    files = verify_prefab_action_delivery(_read(build_output / 'receipt.json'), build_output, output_dir, selection,
                                         required_text(action.get('state_name'), 'state_name'), frames, inputs)
    verify_game_source(context.game_root, game_before, check_cancelled)
    if tuple(source_stamp(path) for path in input_paths) != stamps:
        raise ValueError('Enemy animation source indexes, static artifacts or tools changed during export')
    return files, [{'code': 'enemy_controller_clip_scope', 'message':
        '所选敌人控制器片段已绑定原始骨架并逐帧保存重开；控制器过渡、实时行为决策和运行时 IK 未重建。'}]
