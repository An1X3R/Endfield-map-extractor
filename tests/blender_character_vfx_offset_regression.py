"""Exercise real saved normal-offset geometry and reject source or graph tampering."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
import bmesh


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                    str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
    from audit_character_materials import source_geometry_digest_with_modifiers
    from character_npr_vfx import character_vfx_preview_record
    from character_vfx_vertex_offset import (
        source_modifiers_without_offsets, verify_vertex_offsets, verify_offset_surface_bindings,
    )
    from character_windows_paths import extended_path

    report = json.loads((args.build_dir / 'build_receipt.json').read_text(encoding='utf-8'))
    receipt = report['vertex_offsets']
    if not receipt:
        raise AssertionError('Regression requires a real source with enabled vertex offset')
    bpy.ops.wm.open_mainfile(filepath=extended_path(args.build_dir / 'weapon.blend'), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    baseline = report['geometry_digests']
    evidence = verify_vertex_offsets(receipt, scene)
    if evidence != report['vertex_offset_validation']:
        raise AssertionError('Saved multi-frame source-bound offset evidence changed')
    if not all(row['observed_time_variation'] for row in evidence['frames']):
        raise AssertionError('Real animated source did not vary across three sampled frames')

    def immutable_source() -> dict[str, str]:
        return source_geometry_digest_with_modifiers(tuple(baseline), source_modifiers_without_offsets)

    if immutable_source() != baseline:
        raise AssertionError('Installed non-destructive offset changed the original source hash')
    row = receipt[0]
    obj = scene.objects[row['object']]
    modifier = obj.modifiers[row['modifier']]
    tree = modifier.node_group
    checks: list[str] = []

    for field in ('vertex', 'uv', 'visibility', 'unrelated_modifier'):
        if field == 'vertex':
            old = obj.data.vertices[0].co.copy()
            obj.data.vertices[0].co.x += 0.01
        elif field == 'uv':
            old = obj.data.uv_layers['UV0'].data[0].uv.copy()
            obj.data.uv_layers['UV0'].data[0].uv.x += 0.01
        elif field == 'visibility':
            old = obj.hide_render
            obj.hide_render = not old
        else:
            extra = obj.modifiers.new('Unauthorized source modifier', 'SMOOTH')
        if immutable_source() == baseline:
            raise AssertionError(f'Immutable source tamper was ignored: {field}')
        if field == 'vertex':
            obj.data.vertices[0].co = old
        elif field == 'uv':
            obj.data.uv_layers['UV0'].data[0].uv = old
        elif field == 'visibility':
            obj.hide_render = old
        else:
            obj.modifiers.remove(extra)
        checks.append(field + '_tamper_rejected')

    for field in ('enable', 'time_speed', 'source_identity', 'intensity_override'):
        if field == 'enable':
            modifier.show_render = False
        elif field == 'time_speed':
            target = tree.nodes['Source Offset Speed'].inputs[0]
            old = tuple(target.default_value)
            target.default_value = (1.0, 0.0, 0.0)
        elif field == 'source_identity':
            old = tree['source_identity']
            tree['source_identity'] = '["wrong",1]'
        else:
            socket = next(item for item in tree.interface.items_tree if item.name == 'Source Offset Intensity')
            old = modifier.get(socket.identifier, socket.default_value)
            modifier[socket.identifier] = 0.0
        try:
            verify_vertex_offsets(receipt, scene)
        except ValueError:
            checks.append(field + '_tamper_rejected')
        else:
            raise AssertionError(f'Offset tamper was ignored: {field}')
        if field == 'enable':
            modifier.show_render = True
        elif field == 'time_speed':
            target.default_value = old
        elif field == 'source_identity':
            tree['source_identity'] = old
        else:
            modifier[socket.identifier] = old

    group = next(group for group in bpy.data.node_groups if group.get('source_vertex_offset_geometry_contract'))
    source = json.loads(group['endfield_fx_source_material_json'])
    if source['Floats']['_UseVertexOffset'] != 1.0:
        raise AssertionError('Original source offset switch was overwritten')
    try:
        character_vfx_preview_record(source)
    except ValueError:
        checks.append('material_only_offset_rejected')
    else:
        raise AssertionError('Material-only offset silently lost the required geometry stage')
    if immutable_source() != baseline or verify_vertex_offsets(receipt, scene) != evidence:
        raise AssertionError('Regression did not restore the original saved geometry and controls')
    verify_offset_surface_bindings(receipt, scene)
    slot = obj.material_slots[row['source_slots'][0]]
    saved_material = slot.material
    slot.material = bpy.data.materials.new('Derived wrong source material')
    try:
        verify_offset_surface_bindings(receipt, scene)
    except ValueError:
        checks.append('surface_material_substitution_rejected')
    else:
        raise AssertionError('Offset material source substitution was ignored')
    slot.material = saved_material

    # This is a derived boundary test, not an additional native game discovery.
    original_data = obj.data
    derived_data = original_data.copy()
    obj.data = derived_data
    bm = bmesh.new()
    try:
        bm.from_mesh(derived_data)
        vertices = [bm.verts.new(value) for value in ((4, 0, 0), (5, 0, 0), (4, 1, 0))]
        face = bm.faces.new(vertices)
        unrelated_slot = max(row['source_slots']) + 1
        face.material_index = unrelated_slot
        bm.to_mesh(derived_data)
    finally:
        bm.free()
    while len(derived_data.materials) <= unrelated_slot:
        derived_data.materials.append(saved_material)
    derived_result = verify_vertex_offsets(receipt, scene)
    checks.append('derived_unrelated_surface_vertices_unchanged')
    obj.data = original_data
    bpy.data.meshes.remove(derived_data)
    result = {'status': 'passed', 'checks': checks, 'real_source_evidence': evidence,
              'derived_disconnected_surface_test': derived_result,
              'immutable_source_digest_preserved': True, 'saved_blend_modified': False}
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'checks': len(checks)}))


if __name__ == '__main__':
    main()
