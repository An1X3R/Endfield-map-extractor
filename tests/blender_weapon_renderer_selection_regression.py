"""Test native selection plus explicitly derived no-LOD and child-LOD fixtures."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/character_runtime'))

from audit_character_fbx import clear_scene_objects
from build_weapon_blend import select_weapon_renderers
from character_animation_binding import fields, integer, rows
from character_geometry import import_static_geometry
from preview_character import read_json_object, read_json_value
from rebuild_character_materials import read_packet


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def selection_case(source: Path, visibility: Path, output: Path, lods: list[object]) -> tuple[Path, dict[str, object]]:
    (source / 'lod_sources.json').write_text(json.dumps(lods), encoding='utf-8')
    clear_scene_objects()
    import_static_geometry(source / 'model.geometry.json')
    output.mkdir()
    selection = select_weapon_renderers(source, visibility, output)
    return selection, read_json_object(selection)


def visible_count(document: dict[str, object]) -> int:
    return sum(fields(raw, 'decision')['visible'] is True for raw in rows(document['renderer_decisions'], 'decisions'))


def rejected_case(source: Path, visibility: Path, output: Path, lods: list[object], message: str) -> str:
    try:
        selection_case(source, visibility, output, lods)
    except ValueError as error:
        require(message in str(error), f'Wrong selection rejection: {error}')
        return str(error)
    raise AssertionError(f'Invalid LOD input was accepted: {output.name}')


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-dir', 'shader-identities', 'renderer-visibility', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args(argv)
    output: Path = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / 'derived_source'
    shutil.copytree(args.source_dir, source)
    original_lods = rows(read_json_value(source / 'lod_sources.json'), 'native LODs')
    require(len(original_lods) == 1, 'This derived regression requires one native LODGroup')
    original = fields(original_lods[0], 'native LODGroup')
    audit = read_json_object(source / 'source_audit.json')
    root = fields(audit['actual_root'], 'root')
    child_owners = [fields(fields(raw, 'transform')['game_object'], 'game_object')
                    for raw in rows(audit['transforms'], 'transforms')
                    if fields(fields(raw, 'transform')['game_object'], 'game_object')['path_id'] != root['path_id']]
    require(len(child_owners) >= 2, 'The derived child-group fixture requires two source child GameObjects')
    native_selection, native = selection_case(source, args.renderer_visibility, output / 'native', original_lods)
    require(native['format'] == 'EndfieldWeaponRendererSelection/1', 'Weapon selection format is not explicit')
    require(native['source_root_identity'] == root, 'Weapon root identity changed')
    native_count = visible_count(native)
    read_packet(source, args.shader_identities, native_selection, args.renderer_visibility)

    legacy = {**native, 'format': 'CharacterLodPreviewSelection/2',
              'lod_group_identity': fields(rows(native['lod_groups'], 'groups')[0], 'group')['identity']}
    legacy_path = output / 'legacy_character_selection.json'
    legacy_path.write_text(json.dumps(legacy), encoding='utf-8')
    read_packet(source, args.shader_identities, legacy_path, args.renderer_visibility)

    no_lod_selection, no_lod = selection_case(source, args.renderer_visibility, output / 'no_lod', [])
    require(no_lod['selection_mode'] == 'native_renderer_state_without_lod' and no_lod['lod_groups'] == [],
            'No-LOD selection fabricated an LOD group')
    require(visible_count(no_lod) == len(rows(audit['renderers'], 'renderers')),
            'Derived active/enabled no-LOD source did not retain every native renderer')
    read_packet(source, args.shader_identities, no_lod_selection, args.renderer_visibility)

    child = deepcopy(original)
    child_data = fields(child['data'], 'child.data')
    child_data['m_GameObject'] = {'m_FileID': 0, 'm_PathID': child_owners[0]['path_id']}
    child_selection, child_result = selection_case(source, args.renderer_visibility, output / 'child_lod', [child])
    require(visible_count(child_result) == native_count, 'Moving the derived group to a child changed membership')
    read_packet(source, args.shader_identities, child_selection, args.renderer_visibility)

    levels = rows(child_data['m_LODs'], 'child.levels')
    require(len(levels) >= 2, 'The multi-group derived fixture requires at least two native LOD levels')
    first, second = deepcopy(child), deepcopy(child)
    fields(first['data'], 'first.data')['m_LODs'] = levels[::2]
    fields(second['data'], 'second.data')['m_LODs'] = levels[1::2]
    fields(second['data'], 'second.data')['m_GameObject'] = {'m_FileID': 0, 'm_PathID': child_owners[1]['path_id']}
    second_object = fields(second['object'], 'second.object')
    second_object['path_id'] = integer(second_object['path_id'], 'group.path_id') + 1
    multi_selection, multi = selection_case(source, args.renderer_visibility, output / 'multiple_child_lods', [first, second])
    expected = sum(len(rows(fields(rows(fields(group['data'], 'data')['m_LODs'], 'levels')[0], 'level')['renderers'], 'members'))
                   for group in (first, second))
    require(len(rows(multi['lod_groups'], 'groups')) == 2 and visible_count(multi) == expected,
            'Multiple derived groups did not select their separate exact LOD0 memberships')
    read_packet(source, args.shader_identities, multi_selection, args.renderer_visibility)

    conflicts = deepcopy(second)
    fields(conflicts['data'], 'conflict.data')['m_LODs'] = deepcopy(levels)
    conflict_error = rejected_case(source, args.renderer_visibility, output / 'conflict', [first, conflicts], 'multiple LODGroups')
    external = deepcopy(child)
    entry = fields(rows(fields(rows(fields(external['data'], 'data')['m_LODs'], 'levels')[0], 'level')['renderers'], 'renderers')[0], 'entry')
    fields(entry['renderer'], 'renderer')['m_FileID'] = 1
    external_error = rejected_case(source, args.renderer_visibility, output / 'external', [external], 'external')
    missing = deepcopy(child)
    fields(fields(missing['data'], 'data')['m_GameObject'], 'owner')['m_PathID'] = 0
    owner_error = rejected_case(source, args.renderer_visibility, output / 'missing_owner', [missing], 'absent')
    disabled = deepcopy(child)
    fields(disabled['data'], 'data')['m_Enabled'] = False
    disabled_error = rejected_case(source, args.renderer_visibility, output / 'disabled', [disabled], 'no verified current LOD')

    selected, document = selection_case(source, args.renderer_visibility, output / 'identity_checks', original_lods)
    fields(document['source_root_identity'], 'selection.root')['path_id'] = integer(root['path_id'], 'root.path_id') + 1
    selected.write_text(json.dumps(document), encoding='utf-8')
    try:
        read_packet(source, args.shader_identities, selected, args.renderer_visibility)
    except ValueError as error:
        require('another source root' in str(error), f'Unexpected root rejection: {error}')
    else:
        raise AssertionError('A different weapon source root was accepted')
    selected, _document = selection_case(source, args.renderer_visibility, output / 'hash_checks', original_lods)
    (source / 'lod_sources.json').write_text('[]', encoding='utf-8')
    try:
        read_packet(source, args.shader_identities, selected, args.renderer_visibility)
    except ValueError as error:
        require('different source evidence' in str(error), f'Unexpected evidence rejection: {error}')
    else:
        raise AssertionError('Changed LOD evidence was accepted')
    result = {'status': 'passed', 'fixture_scope': 'No-LOD, child-LOD, multi-group and invalid cases are derived algorithm fixtures; they are not native game discoveries.',
        'source': str(args.source_dir), 'native_visible': native_count, 'derived_no_lod_visible': visible_count(no_lod),
        'derived_child_lod_visible': visible_count(child_result), 'derived_multiple_group_visible': visible_count(multi),
        'legacy_character_format_accepted': True, 'root_substitution_rejected': True, 'changed_lod_bytes_rejected': True,
        'invalid_rejections': {'conflicting_membership': conflict_error, 'external_renderer': external_error,
                               'missing_owner': owner_error, 'disabled_group': disabled_error}}
    (output / 'regression.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
