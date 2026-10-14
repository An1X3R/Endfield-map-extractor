"""Verify expression cache fingerprints against a real character source blend."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy


def _require_cache_miss(cache_root: Path, key: str, input_name: str) -> None:
    from character_expression_bake_cache import read_expression_cache

    if read_expression_cache(cache_root, key) is not None:
        raise AssertionError(f'Changed expression input unexpectedly reused the base cache: {input_name}')


def main(source_blend: Path, source_audit: Path, expression_table: Path,
         cache_root: Path, authoring_request_path: Path,
         baseline_authoring_request_path: Path, saved_hit_report_path: Path,
         result_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1] / 'src'
    runtime_root = source_root / 'character_runtime'
    sys.path.insert(0, str(source_root))
    sys.path.insert(0, str(runtime_root))
    bpy.ops.wm.open_mainfile(filepath=str(source_blend), use_scripts=False, load_ui=False)

    from character_authoring import rig_named
    from character_authoring_unweighted_leaf import restore_unweighted_avatar_leaf_bones
    from character_authoring import apply_authoring, read_authoring_request, verify_authoring
    from character_expression_bake_adapter import (_expression_cache_key,
        _validated_basic_expression_inputs)
    from character_expression_bake_cache import read_expression_cache

    armature = rig_named('Root')
    restore_unweighted_avatar_leaf_bones(armature, expression_table, source_audit)
    meshes, source_rows = _validated_basic_expression_inputs(
        'chr_0004_pelica', source_audit, armature, expression_table, 2e-6)
    base_key = _expression_cache_key('basic_mmd', 'chr_0004_pelica', armature,
                                     meshes, source_rows, 'EF Basic Expressions', 2e-6)
    if read_expression_cache(cache_root, base_key) is None:
        raise AssertionError('Base expression cache entry is missing after the miss build')

    changed_table_path = result_path.with_name('expression_source_changed.json')
    changed_table = json.loads(expression_table.read_text(encoding='utf-8-sig'))
    changed_table['cache_probe_revision'] = 1
    changed_table_path.write_text(json.dumps(changed_table, ensure_ascii=False, indent=2), encoding='utf-8')
    changed_meshes, changed_rows = _validated_basic_expression_inputs(
        'chr_0004_pelica', source_audit, armature, changed_table_path, 2e-6)
    changed_table_key = _expression_cache_key('basic_mmd', 'chr_0004_pelica', armature,
        changed_meshes, changed_rows, 'EF Basic Expressions', 2e-6)
    _require_cache_miss(cache_root, changed_table_key, 'expression_table')

    mesh = meshes[0]
    if not mesh.data.vertices:
        raise AssertionError(f'Expression source mesh has no vertices: {mesh.name}')
    original_coordinate = mesh.data.vertices[0].co.copy()
    try:
        mesh.data.vertices[0].co.x += 1e-4
        mesh.data.update()
        changed_geometry_key = _expression_cache_key('basic_mmd', 'chr_0004_pelica', armature,
            meshes, source_rows, 'EF Basic Expressions', 2e-6)
        _require_cache_miss(cache_root, changed_geometry_key, 'mesh_geometry')
    finally:
        mesh.data.vertices[0].co = original_coordinate
        mesh.data.update()

    modifier = next((modifier for obj in meshes for modifier in obj.modifiers
                     if modifier.type == 'ARMATURE'), None)
    if modifier is None:
        raise AssertionError('Expression meshes have no Armature modifier to validate')
    original_preserve_volume = bool(modifier.use_deform_preserve_volume)
    try:
        modifier.use_deform_preserve_volume = not original_preserve_volume
        changed_modifier_key = _expression_cache_key('basic_mmd', 'chr_0004_pelica', armature,
            meshes, source_rows, 'EF Basic Expressions', 2e-6)
        _require_cache_miss(cache_root, changed_modifier_key, 'armature_modifier_preserve_volume')
    finally:
        modifier.use_deform_preserve_volume = original_preserve_volume

    bone = next((item for item in armature.data.bones if item.use_deform), None)
    if bone is None:
        raise AssertionError('Expression rig has no deform bone to validate')
    original_deform = bool(bone.use_deform)
    try:
        bone.use_deform = not original_deform
        changed_bone_key = _expression_cache_key('basic_mmd', 'chr_0004_pelica', armature,
            meshes, source_rows, 'EF Basic Expressions', 2e-6)
        _require_cache_miss(cache_root, changed_bone_key, 'bone_use_deform')
    finally:
        bone.use_deform = original_deform

    restored_key = _expression_cache_key('basic_mmd', 'chr_0004_pelica', armature,
        meshes, source_rows, 'EF Basic Expressions', 2e-6)
    if restored_key != base_key:
        raise AssertionError('Restoring test mutations did not restore the original expression cache key')

    baseline_authoring = json.loads(baseline_authoring_request_path.read_text(encoding='utf-8-sig'))
    changed_authoring = json.loads(authoring_request_path.read_text(encoding='utf-8-sig'))
    baseline_animation = baseline_authoring['animations'][0]
    changed_animation = changed_authoring['animations'][0]
    if (baseline_animation['action_name'] == changed_animation['action_name']
            or baseline_animation['receipt'] != changed_animation['receipt']):
        raise AssertionError('Action metadata probe must change only the action name while retaining the real source receipt')
    request = read_authoring_request(authoring_request_path)
    authoring_report = apply_authoring(request, cache_root)
    if authoring_report['basic_expressions']['bake_cache']['status'] != 'hit':
        raise AssertionError('Basic expression cache did not hit after changing action request metadata')
    if authoring_report['independent_expression_tables'][0]['bake_cache']['status'] != 'hit':
        raise AssertionError('Independent expression cache did not hit after changing action request metadata')
    saved_hit_report = json.loads(saved_hit_report_path.read_text(encoding='utf-8'))
    if authoring_report['all_shapes'] != saved_hit_report['all_shapes']:
        raise AssertionError('Action metadata cache hit differs from the saved-and-reopened expression shapes')
    driver_audit = verify_authoring(authoring_report)
    if driver_audit['status'] != 'passed':
        raise AssertionError(f'Action metadata cache hit failed driver verification: {driver_audit}')

    animation = request.animations[0]
    clip_digest = hashlib.sha256(animation.hierarchy.read_bytes() + animation.frames.read_bytes()).hexdigest()
    result = {
        'format': 'EndfieldExpressionCacheFingerprintRegression/1',
        'status': 'passed',
        'base_key': base_key,
        'restored_key': restored_key,
        'base_cache': 'hit',
        'changed_inputs': {
            'expression_table': changed_table_key,
            'mesh_geometry': changed_geometry_key,
            'armature_modifier_preserve_volume': changed_modifier_key,
            'bone_use_deform': changed_bone_key,
        },
        'changed_input_cache_results': {
            'expression_table': 'miss',
            'mesh_geometry': 'miss',
            'armature_modifier_preserve_volume': 'miss',
            'bone_use_deform': 'miss',
        },
        'action_request_metadata_probe': {
            'status': 'hit',
            'baseline_action_name': baseline_animation['action_name'],
            'changed_action_name': changed_animation['action_name'],
            'source_receipt_unchanged': True,
            'source_clip_sha256': clip_digest,
            'different_motion_clip_tested': False,
            'saved_hit_shape_data_equal': True,
            'driver_audit': driver_audit['status'],
        },
        'scene_restored': True,
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-blend', type=Path, required=True)
    parser.add_argument('--source-audit', type=Path, required=True)
    parser.add_argument('--expression-table', type=Path, required=True)
    parser.add_argument('--cache-root', type=Path, required=True)
    parser.add_argument('--authoring-request', type=Path, required=True)
    parser.add_argument('--baseline-authoring-request', type=Path, required=True)
    parser.add_argument('--saved-hit-report', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    options = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(options.source_blend.resolve(strict=True), options.source_audit.resolve(strict=True),
         options.expression_table.resolve(strict=True), options.cache_root.resolve(strict=True),
         options.authoring_request.resolve(strict=True),
         options.baseline_authoring_request.resolve(strict=True),
         options.saved_hit_report.resolve(strict=True), options.result.resolve())
