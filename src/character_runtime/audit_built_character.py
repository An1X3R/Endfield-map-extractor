"""Verify one saved character without rendering, saving, or enabling automatic scripts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy


def object_value(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Expected an object: {context}')
    return value


def string_value(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f'Expected a nonempty string: {context}')
    return value


def main(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from character_windows_paths import extended_path, io_path

    if io_path(args.output).exists():
        raise FileExistsError(args.output)
    sys.path.insert(0, str(args.repository_src))
    from audit_character_materials import source_geometry_digest
    from attach_character_live_shadows import SHADOW_MODIFIER
    from preview_character import verify_saved_static_bind_pose
    from review_character_skin_lights import refresh
    from review_character_live_shadows import shadow_fields, field_summary
    from character_late_materials import verify_late_materials

    receipt = object_value(json.loads(io_path(args.build_receipt).read_text(encoding='utf-8')), 'build receipt')
    if receipt['format'] != 'EndfieldCharacterBuild/1' or receipt['status'] != 'built_not_reopened':
        raise ValueError('Expected a completed build-only character receipt')
    if receipt['source_geometry_digest_basis'] != 'source_local_parent_chain/1':
        raise ValueError('Expected stable local/parent-chain source geometry fingerprints')
    blend = Path(string_value(receipt['output'], 'output')).resolve(strict=True)
    expected_hash = string_value(receipt['sha256'], 'sha256')
    digest = hashlib.sha256(io_path(blend).read_bytes()).hexdigest()
    if digest != expected_hash:
        raise AssertionError(f'Built file differs from its receipt: {blend}')
    bpy.ops.wm.open_mainfile(filepath=extended_path(blend), use_scripts=False)
    refresh()
    workspace_audit = None
    if 'authoring_workspace' in receipt:
        from character_authoring_workspace import verify_authoring_workspace
        workspace_audit = verify_authoring_workspace(object_value(receipt['authoring_workspace'], 'authoring workspace'))
    from character_emotion_atlas import verify_emotion_atlases
    emotion_atlas_audit = verify_emotion_atlases(receipt['emotion_atlas']) if 'emotion_atlas' in receipt else None
    late_materials_audit = (verify_late_materials(receipt['late_materials'])
                            if 'late_materials' in receipt else None)
    from character_wetfilm_profile import verify_profile_wetfilm
    wetfilm_audit = (verify_profile_wetfilm(receipt['standard_wetfilm'])
                     if 'standard_wetfilm' in receipt else None)
    from character_standard_snow_profile import verify_profile_standard_snow
    standard_snow_audit = (verify_profile_standard_snow(object_value(receipt['standard_snow'], 'standard snow'))
                           if 'standard_snow' in receipt else None)
    from character_skin_weather_profile import verify_profile_skin_rain
    skin_rain_audit = (verify_profile_skin_rain(receipt['skin_rain'])
                       if 'skin_rain' in receipt else None)
    from character_hair_fur_snow_profile import verify_profile_hair_fur_snow
    hair_fur_snow_audit = (verify_profile_hair_fur_snow(object_value(receipt['hair_fur_snow'], 'Hair/Fur snow'))
                           if 'hair_fur_snow' in receipt else None)
    from character_fur_curves_profile import verify_profile_fur_curves
    fur_curves_snow_audit = verify_profile_fur_curves(
        object_value(receipt.get('fur_curves_snow', {'status': 'not_requested'}), 'Fur Curves snow'))
    special_lights = object_value(receipt['special_lights'], 'special lights')
    special_lights_audit = None
    if special_lights.get('status') == 'not_applicable':
        from review_character_hgrp import visible_source_group_names
        visible_groups = visible_source_group_names()
        skins = tuple(tree.name for tree in bpy.data.node_groups
                      if tree.name in visible_groups and tree.get('source_shader') == 'HGRP/CharacterNPR_Skin')
        if skins or special_lights.get('visible_skin_groups') != [] or any(
                special_lights.get(key) != [] for key in ('shadows', 'specular_only', 'ramped')):
            raise AssertionError('Skin lights declared not applicable despite source Skin groups or bindings')
        special_lights_audit = {'status': 'not_applicable', 'visible_source_skin_groups': 0}
    original_geometry = object_value(receipt['source_geometry_before_live_shadows'], 'source geometry')
    names = tuple(original_geometry)
    if not names or any(name not in bpy.data.objects or bpy.data.objects[name].type != 'MESH' for name in names):
        raise AssertionError('Saved character is missing a source mesh')
    visibility = {name: [bpy.data.objects[name].hide_render, bpy.data.objects[name].hide_viewport, bpy.data.objects[name].hide_get()] for name in names}
    if visibility != receipt['source_visibility']:
        raise AssertionError('Source visibility differs after scripts-disabled reopening')
    transforms = {name: [[list(row) for row in bpy.data.objects[name].matrix_basis],
                         [list(row) for row in bpy.data.objects[name].matrix_parent_inverse],
                         bpy.data.objects[name].parent.name if bpy.data.objects[name].parent else ''] for name in names}
    if transforms != receipt['source_transforms']:
        raise AssertionError('Source local transform or parenting differs after reopening')
    receivers_raw = receipt['receiver_names']
    if not isinstance(receivers_raw, list):
        raise TypeError('Receiver names must be an array')
    receivers = tuple(string_value(name, 'receiver name') for name in receivers_raw)
    fields = shadow_fields(receivers)
    field_hash = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    if field_hash != receipt['shadow_fields_sha256'] or field_summary(fields) != receipt['shadow_point_counts']:
        raise AssertionError('Evaluated shadow fields differ after reopening')
    expected_images_raw = receipt['packed_images']
    if not isinstance(expected_images_raw, list):
        raise TypeError('Packed images must be an array')
    expected_images = tuple(object_value(row, 'packed image') for row in expected_images_raw)
    images = tuple(image for image in bpy.data.images if image.name.startswith('EFCHAR '))
    if len(images) != len(expected_images):
        raise AssertionError('Saved source texture role count differs')
    for row in expected_images:
        name = string_value(row['name'], 'image name')
        image = bpy.data.images.get(name)
        if image is None or image.packed_file is None:
            raise AssertionError(f'Source texture is missing or unpacked: {name}')
        image.pixels[0]
        if not image.has_data or hashlib.sha256(image.packed_file.data).hexdigest() != row['sha256']:
            raise AssertionError(f'Packed source texture bytes differ: {name}')
        if list(image.size) != row['dimensions'] or image.colorspace_settings.name != row['color_space'] or image.alpha_mode != row['alpha_mode']:
            raise AssertionError(f'Source texture interpretation differs: {name}')
    invalid = tuple((tree.name, curve.data_path) for tree in bpy.data.node_groups if tree.animation_data
                    for curve in tree.animation_data.drivers if not curve.driver.is_valid)
    if invalid:
        raise AssertionError(f'Invalid live node drivers after reopening: {invalid}')
    rig_name = string_value(receipt['armature'], 'armature')
    head_name = string_value(receipt['head_bone'], 'head bone')
    rig = bpy.data.objects.get(rig_name)
    if rig is None or rig.type != 'ARMATURE' or rig.data.pose_position != 'POSE' or head_name not in rig.pose.bones:
        raise AssertionError('Saved editable armature or actual head bone is missing')
    pose_path = Path(string_value(receipt['static_bind_pose_audit'], 'static bind pose audit'))
    pose = object_value(json.loads(io_path(pose_path).read_text(encoding='utf-8')), 'static bind pose audit')
    authoring = receipt.get('authoring')
    authoring_audit = None
    if authoring is None:
        verify_saved_static_bind_pose(pose)
    else:
        from character_authoring import verify_authoring
        from mathutils import Matrix
        authoring_audit = verify_authoring(object_value(authoring, 'authoring'))
        action = rig.animation_data.action if rig.animation_data else None
        saved_basis = {bone.name: bone.matrix_basis.copy() for bone in rig.pose.bones}
        try:
            if rig.animation_data:
                rig.animation_data.action = None
            for bone in rig.pose.bones:
                bone.matrix_basis = Matrix.Identity(4)
            refresh()
            verify_saved_static_bind_pose(pose)
        finally:
            if rig.animation_data:
                rig.animation_data.action = action
            for bone in rig.pose.bones:
                bone.matrix_basis = saved_basis[bone.name]
            refresh()
    for name in receivers:
        obj = bpy.data.objects[name]
        modifier = obj.modifiers.get(SHADOW_MODIFIER)
        if modifier is None:
            raise AssertionError(f'Missing live shadow receiver modifier: {name}')
        obj.modifiers.remove(modifier)
    refresh()
    after = source_geometry_digest(names)
    if after != original_geometry:
        changed = tuple(name for name in names if after[name] != original_geometry[name])
        raise AssertionError(f'Saved geometry, UVs, weights or shape keys differ: {changed}')
    report = {'format': 'EndfieldCharacterBuildReopenAudit/1', 'status': 'passed', 'blend': str(blend),
              'blend_sha256': digest, 'source_renderers': len(names), 'packed_image_roles': len(images),
              'source_geometry_and_skin_preserved': True, 'source_visibility_preserved': True,
              'live_shadow_fields_equal': True, 'live_drivers_valid': True, 'editable_pose_preserved': True,
              'late_materials_reachable_after_reopen': late_materials_audit,
              'emotion_atlas': emotion_atlas_audit,
              'standard_wetfilm': wetfilm_audit,
              'standard_snow': standard_snow_audit,
              'skin_rain': skin_rain_audit,
              'hair_fur_snow': hair_fur_snow_audit,
              'fur_curves_snow': fur_curves_snow_audit,
              'special_lights': special_lights_audit,
              'scripts_enabled_on_reopen': False, 'render_count': 0, 'save_count': 0,
              'authoring': authoring_audit,
              'authoring_workspace': workspace_audit,
              'build_receipt_sha256': hashlib.sha256(io_path(args.build_receipt).read_bytes()).hexdigest()}
    io_path(args.output).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'source_renderers': len(names), 'packed_image_roles': len(images)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('build-receipt', 'repository-src', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    main(parser.parse_args(sys.argv[sys.argv.index('--') + 1:]))

