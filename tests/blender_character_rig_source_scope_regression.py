"""Verify original source signatures beside a real added potential display layer."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
               str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from character_potential_build import verify_potential_appearance
from character_potential_controls import PROPERTY
from character_rig_authoring import _registered_source_geometry, verify_rig_authoring
from character_rig_authoring import _potential_source_visibility
from audit_character_materials import source_geometry_digest, source_geometry_digest_with_visibility


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-receipt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    if args.output.exists():
        raise FileExistsError(args.output)
    receipt = json.loads(args.build_receipt.read_text(encoding='utf-8'))
    blend = Path(receipt['output'])
    before = hashlib.sha256(blend.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    potential = receipt['appearance_report']['potential']
    verify_potential_appearance(potential, scene)
    owner = scene.objects[potential['applied']['owner']]
    initial = owner[PROPERTY]
    rigs = receipt['authoring_workspace']['rigs']
    try:
        checks = [verify_rig_authoring(report) for report in rigs]
        registered = frozenset(obj for report in rigs for obj in _registered_source_geometry(report))
        display_names = tuple(name for effect in potential['applied']['display_effects']
                              for name in effect['objects'])
        additions = tuple(scene.objects[name] for name in display_names
                          if scene.objects[name].type == 'MESH' and scene.objects[name] not in registered)
        if not additions:
            raise AssertionError('Scope regression requires actual added potential display meshes')
        display = additions[0]
        previous_source_path = display.get('source_geometry_path')
        try:
            display['source_geometry_path'] = 'scope-regression-added-display'
            verify_rig_authoring(rigs[0])
        finally:
            if previous_source_path is None:
                del display['source_geometry_path']
            else:
                display['source_geometry_path'] = previous_source_path
        target = next(obj for obj in registered if obj.name not in potential['applied']['overlay_objects'])
        names = tuple(obj.name for obj in registered)
        canonical_visibility = _potential_source_visibility(tuple(registered))
        if source_geometry_digest(names) != source_geometry_digest_with_visibility(names, {}):
            raise AssertionError('Actual-visibility digest compatibility changed')
        canonical_digest = source_geometry_digest_with_visibility(names, canonical_visibility)
        original_coordinate = target.data.vertices[0].co.copy()
        try:
            target.data.vertices[0].co.x += 0.125
            if source_geometry_digest_with_visibility(names, canonical_visibility) == canonical_digest:
                raise AssertionError('Canonical geometry digest accepted source coordinate corruption')
        finally:
            target.data.vertices[0].co = original_coordinate
        original_mesh = target.data
        original_mesh_name = original_mesh.name
        changed_mesh = original_mesh.copy()
        changed_mesh.vertices.add(1)
        original_mesh.name = original_mesh_name + ' Scope Regression Backup'
        changed_mesh.name = original_mesh_name
        target.data = changed_mesh
        try:
            try:
                verify_rig_authoring(rigs[0])
            except AssertionError as error:
                if 'Source geometry signature changed' not in str(error):
                    raise
            else:
                raise AssertionError('Registered source vertex-layout corruption was not rejected')
        finally:
            target.data = original_mesh
            bpy.data.meshes.remove(changed_mesh)
            original_mesh.name = original_mesh_name
        previous_visibility = target.hide_render
        try:
            target.hide_render = not previous_visibility
            if source_geometry_digest_with_visibility(names, canonical_visibility) == canonical_digest:
                raise AssertionError('Canonical geometry digest accepted unrelated source visibility corruption')
            try:
                verify_rig_authoring(rigs[0])
            except (AssertionError, ValueError) as error:
                if ('Source geometry signature changed' not in str(error)
                        and 'unrelated source object visibility' not in str(error)):
                    raise
            else:
                raise AssertionError('Registered source visibility corruption was not rejected')
        finally:
            target.hide_render = previous_visibility
        verify_rig_authoring(rigs[0])
        if source_geometry_digest_with_visibility(names, canonical_visibility) != canonical_digest:
            raise AssertionError('Canonical geometry digest did not restore after source corruption checks')
    finally:
        owner[PROPERTY] = initial
        owner.update_tag()
        scene.frame_set(scene.frame_current)
        bpy.context.view_layer.update()
    verify_potential_appearance(potential, scene)
    if hashlib.sha256(blend.read_bytes()).hexdigest() != before:
        raise AssertionError('Scope regression modified the source blend')
    args.output.write_text(json.dumps({'status': 'verified_original_signature_and_rejected_source_corruption',
        'rigs': checks, 'added_display_meshes': [obj.name for obj in additions],
        'added_source_tag_scope_verified': True,
        'source_digest_coordinate_and_visibility_corruption_rejected': True,
        'potential_initial_state_restored': True, 'blend_sha256_unchanged': before}, indent=2), encoding='utf-8')
    print('RIG_SOURCE_SCOPE_REGRESSION_PASSED', len(additions), flush=True)


if __name__ == '__main__':
    main()
