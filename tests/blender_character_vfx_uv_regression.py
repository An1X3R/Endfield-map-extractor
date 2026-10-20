"""Verify signed UV nodes and retained source switches in a real saved character."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                    str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
    from character_npr_vfx import character_vfx_preview_record
    from character_npr_vfx_uv import character_vfx_mesh_uv_record
    from endfield_scene_material_contract import validate_material
    from blender_scene_effect_layers import vfx_mesh_feature_issues

    digest = hashlib.sha256(args.blend.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(args.blend), load_ui=False, use_scripts=False)
    signed: list[str] = []
    inactive: list[str] = []
    missing: list[str] = []
    for group in bpy.data.node_groups:
        if group.get('source_shader') != 'HGRP/CharacterNPR_VFX' or 'source_vfx_preview_record' not in group:
            continue
        source = validate_material(json.loads(group['endfield_fx_source_material_json']))
        preview = validate_material(json.loads(group['source_vfx_preview_record']))
        mesh_names = tuple(json.loads(group['source_mesh_color_evidence'])['mesh_names'])
        character_vfx_mesh_uv_record(preview, mesh_names)
        before = json.dumps(source, sort_keys=True)
        if 'endfield_character_vfx_mesh_uv_weights' in group:
            evidence = json.loads(group['endfield_character_vfx_mesh_uv_weights'])
            for name, weights in evidence['bindings'].items():
                if preview['Colors'][name + 'UVWeights'] != weights:
                    raise AssertionError('Signed source UV weights were overwritten')
                node = group.nodes[name + ' Source UV0 Weight']
                target = group.nodes[name + ' UV Speed']
                if node.operation != 'SCALE' or node.inputs[3].default_value != weights[0]:
                    raise AssertionError('Source UV coefficient is absent from the actual graph')
                if len(node.inputs[0].links) != 1 or node.inputs[0].links[0].from_node.uv_map != 'UV0':
                    raise AssertionError('Signed source coefficient does not multiply actual UV0')
                if len(target.inputs[0].links) != 1 or target.inputs[0].links[0].from_node != node:
                    raise AssertionError('Signed UV scaling is not before the time/ST/rotation chain')
                signed.append(group.name)
        if 'source_inactive_screen_modifier' in group:
            if source['Floats']['_UsePosYAsScreenV'] != 1 or preview['Floats']['_UsePosYAsScreenV'] != 0:
                raise AssertionError('Inactive-screen source and preview distinction is missing')
            # A real named-role screen consumer must keep the unsupported modifier enabled.
            colors = {key: list(value) for key, value in source['Colors'].items()}
            colors['_MainTexUVWeights'] = [0.0, 0.0, 0.0, 1.0]
            active = character_vfx_preview_record({**source, 'Colors': colors})
            if active['Floats']['_UsePosYAsScreenV'] != 1:
                raise AssertionError('An active screen modifier was silently pruned')
            if '_UsePosYAsScreenV' not in vfx_mesh_feature_issues(active, 4):
                raise AssertionError('An active unsupported screen modifier escaped the scene validator')
            inactive.append(group.name)
        if 'endfield_character_vfx_missing_uv1_reference' in group:
            evidence = json.loads(group['endfield_character_vfx_missing_uv1_reference'])
            for name, raw in evidence.items():
                proof = json.loads(raw)
                if proof['source_uv1'] is not None or proof['status'] != 'absent_source_uv1_constant_zero_reference/1':
                    raise AssertionError('Missing native UV1 has no source absence evidence')
                obj = bpy.data.objects[name]
                if any(value != 0.0 for item in obj.data.uv_layers['UV1'].data for value in item.uv):
                    raise AssertionError('Absent native UV1 reference is not constant zero')
                missing.append(group.name)
        if json.dumps(source, sort_keys=True) != before:
            raise AssertionError('Character UV adaptation mutated the source record')
    if not signed or not inactive or not missing:
        raise AssertionError('Real saved fixture did not exercise every character UV adapter branch')
    obj = next(obj for obj in bpy.data.objects if obj.type == 'MESH' and obj.data.uv_layers.get('UV1') is not None)
    original = obj.data
    obj.data = original.copy()
    try:
        obj.data.uv_layers.remove(obj.data.uv_layers['UV1'])
        preview['Colors']['_MainTexUVWeights'] = [0.0, 1.0, 0.0, 0.0]
        try:
            character_vfx_mesh_uv_record(preview, (obj.name,))
        except ValueError as error:
            if 'layer=UV1' not in str(error):
                raise
        else:
            raise AssertionError('A missing imported UV1 was silently defaulted')
    finally:
        copy = obj.data
        obj.data = original
        bpy.data.meshes.remove(copy)
    if digest != hashlib.sha256(args.blend.read_bytes()).hexdigest():
        raise AssertionError('Real saved fixture changed during regression')
    args.output.write_text(json.dumps({'status': 'passed', 'blend': str(args.blend),
        'source_sha256': digest, 'signed_uv_groups': signed, 'inactive_screen_groups': inactive,
        'explicit_absent_uv1_groups': missing, 'missing_imported_uv1_rejected': True,
        'active_screen_modifier_retained': True}, indent=2), encoding='utf-8')
    print('CHARACTER_VFX_UV_REGRESSION_PASSED', flush=True)


if __name__ == '__main__':
    main()
