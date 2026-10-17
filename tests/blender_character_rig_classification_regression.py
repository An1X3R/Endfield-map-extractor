"""Exercise direct accessory FK controls against a real source character blend."""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
from pathlib import Path
from statistics import median
import struct
import sys

import bpy
from mathutils import Matrix

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))

from character_rig_authoring import (
    ACCESSORY_ROLES, _action_signature, _bone_role, _child_direction,
    _rest_signature, _semantic_role, apply_rig_authoring, verify_rig_authoring,
)


def source_mesh_hash() -> str:
    """Hash real source vertices, weights and shape-key coordinates."""
    digest = hashlib.sha256()
    for obj in sorted((item for item in bpy.data.objects if item.get('source_geometry_path')),
                      key=lambda item: item.name):
        digest.update(obj.name.encode('utf-8'))
        coordinates = array('f', [0.0]) * (len(obj.data.vertices) * 3)
        obj.data.vertices.foreach_get('co', coordinates)
        digest.update(coordinates.tobytes())
        for vertex in obj.data.vertices:
            for group in vertex.groups:
                digest.update(struct.pack('<IIf', vertex.index, group.group, group.weight))
        if obj.data.shape_keys is not None:
            for key in obj.data.shape_keys.key_blocks:
                key.data.foreach_get('co', coordinates)
                digest.update(coordinates.tobytes())
    return digest.hexdigest()


def evaluated_mesh_hash() -> str:
    """Hash the evaluated source meshes at the current pose."""
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    digest = hashlib.sha256()
    for obj in sorted((item for item in bpy.data.objects if item.get('source_geometry_path')),
                      key=lambda item: item.name):
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
        try:
            coordinates = array('f', [0.0]) * (len(mesh.vertices) * 3)
            mesh.vertices.foreach_get('co', coordinates)
            digest.update(obj.name.encode('utf-8'))
            digest.update(coordinates.tobytes())
        finally:
            evaluated.to_mesh_clear()
    return digest.hexdigest()


def shape_bbox_max_extent(shape: bpy.types.Object, pose_bone: bpy.types.PoseBone) -> float:
    """Measure the real widget mesh bounding box after its per-axis display scale."""
    scale = tuple(abs(float(value)) for value in pose_bone.custom_shape_scale_xyz)
    points = [tuple(float(vertex.co[axis]) * scale[axis] for axis in range(3))
              for vertex in shape.data.vertices]
    extents = tuple(max(point[axis] for point in points) - min(point[axis] for point in points)
                    for axis in range(3))
    return max(extents)


def widget_geometry_probe(rig: bpy.types.Object, controls: list[dict[str, object]],
                          slot_paths: dict[int, str]) -> dict[str, object]:
    """Check real source-joint directions and compact widget bounds on the sample rig."""
    toe_paths = {slot_paths[slot] for slot in (20, 21) if slot in slot_paths}
    controls_by_bone = {str(row['bone']): row for row in controls}
    bones_by_path = {str(bone.get('source_transform_path')): bone
                     for bone in rig.data.bones
                     if isinstance(bone.get('source_transform_path'), str)}
    mapped_bones = {slot: bones_by_path[path] for slot, path in slot_paths.items()
                    if path in bones_by_path}
    direct_avatar_slot_pairs = (
        (7, 8), (8, 9), (10, 11),
        (12, 14), (14, 16), (16, 18),
        (13, 15), (15, 17), (17, 19),
        (1, 3), (3, 5), (5, 20),
        (2, 4), (4, 6), (6, 21),
    )
    avatar_pairs: list[dict[str, object]] = []
    for parent_slot, child_slot in direct_avatar_slot_pairs:
        if parent_slot not in mapped_bones or child_slot not in mapped_bones:
            raise AssertionError(f'Real Avatar mapping is missing direct slots {parent_slot}->{child_slot}')
        parent = mapped_bones[parent_slot]
        child = mapped_bones[child_slot]
        if child.parent != parent:
            raise AssertionError(f'Avatar slots are not direct source joints: {parent_slot}->{child_slot}')
        control = controls_by_bone.get(parent.name)
        if control is None:
            raise AssertionError(f'Mapped Avatar parent has no source FK control: {parent.name}')
        axis, widget_length = _child_direction(parent, str(control['role']))
        source_vector = parent.matrix_local.inverted().to_3x3() @ (child.head_local - parent.head_local)
        source_spacing = float(source_vector.length)
        alignment = float(axis.dot(source_vector.normalized()))
        if alignment < 0.999 or widget_length > source_spacing * 1.001:
            raise AssertionError(f'Widget differs from exact Avatar joint direction: {parent.name}->{child.name}, dot={alignment}, length={widget_length}, spacing={source_spacing}')
        avatar_pairs.append({'parent_slot': parent_slot, 'child_slot': child_slot,
                             'parent': parent.name, 'child': child.name,
                             'direction_dot': alignment, 'source_spacing': source_spacing})

    face_bones = [rig.data.bones[str(row['bone'])] for row in controls
                  if row['role'] == 'Face']
    face_coordinates = [bone.head_local for bone in face_bones]
    face_extent = (max(max(point[axis] for point in face_coordinates)
                       - min(point[axis] for point in face_coordinates)
                       for axis in range(3)) if len(face_coordinates) > 1 else 0.0)
    nearest_face_spacings: list[float] = []
    for bone in face_bones:
        distances = [float((other.head_local - bone.head_local).length)
                     for other in face_bones if other != bone
                     and float((other.head_local - bone.head_local).length) > 1e-5]
        if distances:
            nearest_face_spacings.append(min(distances))

    terminal_dots: list[float] = []
    hand_chain_dots: list[float] = []
    accessory_rows: list[dict[str, object]] = []
    for row in controls:
        bone = rig.data.bones[str(row['bone'])]
        role = str(row['role'])
        axis, widget_length = _child_direction(bone, role)

        if role == 'Hands':
            hand_children = [child for child in bone.children
                             if controls_by_bone.get(child.name, {}).get('role') == 'Hands']
            if len(hand_children) == 1:
                child = hand_children[0]
                source_vector = bone.matrix_local.inverted().to_3x3() @ (child.head_local - bone.head_local)
                alignment = float(axis.dot(source_vector.normalized()))
                if alignment < 0.999:
                    raise AssertionError(f'Finger widget differs from direct source child: {bone.name}->{child.name}, dot={alignment}')
                hand_chain_dots.append(alignment)

        if bone.parent is not None and not bone.children and (role == 'Hands' or row['path'] in toe_paths):
            parent_vector = bone.matrix_local.inverted().to_3x3() @ (bone.head_local - bone.parent.head_local)
            parent_distance = float(parent_vector.length)
            if parent_distance > 1e-5:
                terminal_alignment = float(axis.dot(parent_vector.normalized()))
                if terminal_alignment < 0.999 or abs(widget_length - parent_distance) > parent_distance * 1e-3:
                    raise AssertionError(f'Terminal control differs from parent-to-bone spacing: {bone.name}, dot={terminal_alignment}, length={widget_length}, spacing={parent_distance}')
                terminal_dots.append(terminal_alignment)

        if role not in ('Face', 'Clothing'):
            continue
        pose_bone = rig.pose.bones[bone.name]
        shape = pose_bone.custom_shape
        if shape is None:
            raise AssertionError(f'Face or clothing widget is missing: {bone.name}')
        accessory_rows.append({'bone': bone.name, 'role': role,
                               'bbox_max_extent': shape_bbox_max_extent(shape, pose_bone)})

    if not avatar_pairs:
        raise AssertionError('Real source rig has no exact Avatar joint pairs to validate')
    if not terminal_dots:
        raise AssertionError('Real source rig has no terminal finger or toe controls to validate')
    if not hand_chain_dots:
        raise AssertionError('Real source rig has no nonterminal finger controls to validate')
    if not accessory_rows:
        raise AssertionError('Real source rig has no face or clothing controls to validate')

    category_geometry: dict[str, dict[str, float | int]] = {}
    bbox_reduction: dict[str, dict[str, float | int]] = {}
    legacy_bbox_summaries = {
        'Face': (84, 0.062051866203546524, 0.09122811770066619),
        'Clothing': (67, 0.12593663856387227, 0.1545896945520573),
    }
    for role in ('Face', 'Clothing'):
        role_rows = [row for row in accessory_rows if row['role'] == role]
        legacy_count, legacy_median, legacy_max = legacy_bbox_summaries[role]
        if len(role_rows) != legacy_count:
            raise AssertionError(f'Unexpected {role} controls in the Pelica sample: {len(role_rows)}')
        role_extents = sorted(float(row['bbox_max_extent']) for row in role_rows)
        current_median = float(median(role_extents))
        current_max = role_extents[-1]
        reduction_limit = 0.25 if role == 'Face' else 0.50
        if current_median >= legacy_median * reduction_limit or current_max >= legacy_max * reduction_limit:
            raise AssertionError(f'{role} widget bounds did not shrink from the legacy preview: median={current_median}, max={current_max}')
        category_geometry[role] = {'count': len(role_rows),
                                   'bbox_median': current_median,
                                   'bbox_max': current_max}
        bbox_reduction[role] = {
            'legacy_median': legacy_median,
            'current_median': current_median,
            'median_reduction_percent': 100.0 * (1.0 - current_median / legacy_median),
            'legacy_max': legacy_max,
            'current_max': current_max,
            'max_reduction_percent': 100.0 * (1.0 - current_max / legacy_max),
        }

    face_rows = [row for row in accessory_rows if row['role'] == 'Face']
    if not face_rows:
        raise AssertionError('Pelica regression sample has no classified face controls')
    face_bbox_max = max(float(row['bbox_max_extent']) for row in face_rows)
    face_bbox_median = float(median(float(row['bbox_max_extent']) for row in face_rows))
    if face_extent > 1e-5 and face_bbox_max > face_extent * 0.05:
        raise AssertionError(f'Face widgets exceed five percent of the actual face-joint span: bbox={face_bbox_max}, span={face_extent}')
    nearest_face_median = float(median(nearest_face_spacings)) if nearest_face_spacings else 0.0
    if nearest_face_median > 1e-5 and face_bbox_median > nearest_face_median * 0.40:
        raise AssertionError(f'Median face widget exceeds the local joint-spacing bound: bbox={face_bbox_median}, spacing={nearest_face_median}')

    return {
        'avatar_mapped_joint_pairs': avatar_pairs,
        'avatar_joint_pair_count': len(avatar_pairs),
        'avatar_direction_min_dot': min(float(row['direction_dot']) for row in avatar_pairs),
        'terminal_control_count': len(terminal_dots),
        'terminal_direction_min_dot': min(terminal_dots),
        'nonterminal_finger_control_count': len(hand_chain_dots),
        'nonterminal_finger_direction_min_dot': min(hand_chain_dots),
        'face_joint_span': face_extent,
        'face_nearest_joint_spacing_median': nearest_face_median,
        'face_bbox_median': face_bbox_median,
        'face_bbox_max': face_bbox_max,
        'face_bbox_max_span_ratio': face_bbox_max / face_extent if face_extent > 1e-5 else 0.0,
        'bbox_by_role': category_geometry,
        'legacy_to_current_bbox': bbox_reduction,
        'face_clothing_widgets': accessory_rows,
    }


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--legacy-report', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    old_report = json.loads(args.legacy_report.read_text(encoding='utf-8'))
    legacy_check = verify_rig_authoring(old_report)
    rig = bpy.data.objects[str(old_report['armature'])]
    slots = {int(row['slot']): str(row['path']) for row in old_report['mapped_slots']}
    assert all(_bone_role(slot) == 'Face' for slot in (22, 23, 24))
    assert _semantic_role('character/Root/tail_base_a_01_jnt_ctrl')[0] == 'Tail'
    assert _semantic_role('character/Root/hair_base/ornament_base')[0] == 'Accessories'
    assert _semantic_role('character/Root/unresolved_source_jnt')[0] == 'Source Details'
    mesh_before = source_mesh_hash()
    rest_before = _rest_signature(rig)
    action_before = _action_signature(rig)
    frames = (1, 31, 61)
    before: dict[str, str] = {}
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        before[str(frame)] = evaluated_mesh_hash()
    report = apply_rig_authoring(rig, slots)
    geometry_probe = widget_geometry_probe(rig, report['source_fk_controls'], slots)
    repeated = apply_rig_authoring(rig, None)
    assert report['source_fk_controls'] == repeated['source_fk_controls']
    repeated_geometry = widget_geometry_probe(rig, repeated['source_fk_controls'], slots)
    assert geometry_probe == repeated_geometry
    assert mesh_before == source_mesh_hash()
    assert rest_before == _rest_signature(rig)
    assert action_before == _action_signature(rig)
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        assert before[str(frame)] == evaluated_mesh_hash()
    assert not rig.data.collections['Source Details'].is_visible
    assert all(not name.lower().startswith('corrective')
               for name in report['bone_collections']['Hands'])

    weighted: set[str] = set()
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or not obj.get('source_geometry_path'):
            continue
        groups = {group.index: group.name for group in obj.vertex_groups}
        weighted.update(groups[group.group] for vertex in obj.data.vertices
                        for group in vertex.groups if group.weight > 0.0)
    movement: dict[str, str] = {}
    for role in ACCESSORY_ROLES:
        candidates = [row for row in report['source_fk_controls']
                      if row['role'] == role and row['bone'] in weighted]
        if not candidates:
            continue
        bone = rig.pose.bones[str(candidates[0]['bone'])]
        original = bone.matrix_basis.copy()
        pose_before = evaluated_mesh_hash()
        bone.matrix_basis = original @ Matrix.Rotation(0.22, 4, 'X')
        assert evaluated_mesh_hash() != pose_before, role
        bone.matrix_basis = original
        bpy.context.scene.frame_set(bpy.context.scene.frame_current)
        assert evaluated_mesh_hash() == pose_before, role
        movement[role] = bone.name
    assert {'Hair', 'Face', 'Ears', 'Clothing', 'Accessories'} <= movement.keys()
    verify_rig_authoring(repeated)
    saved = output / 'accessory_fk_regression.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(saved))
    bpy.ops.wm.open_mainfile(filepath=str(saved))
    rig = bpy.data.objects[str(repeated['armature'])]
    reopened = verify_rig_authoring(repeated)
    assert mesh_before == source_mesh_hash()
    assert rest_before == _rest_signature(rig)
    assert action_before == _action_signature(rig)
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        assert before[str(frame)] == evaluated_mesh_hash()
    result = {'status': 'verified_after_reopen', 'legacy_report': legacy_check,
              'current_report': reopened, 'movement': movement,
              'widget_geometry': {key: value for key, value in geometry_probe.items()
                                  if key != 'face_clothing_widgets'},
              'category_counts': {role: len(names) for role, names in report['bone_collections'].items()},
              'source_mesh_hash': mesh_before, 'rest_signature': rest_before,
              'action_signature': action_before, 'frames': before}
    (output / 'classification_report.json').write_text(json.dumps(repeated, indent=2), encoding='utf-8')
    (output / 'verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    (output / 'widget_geometry_probe.json').write_text(json.dumps(geometry_probe, indent=2), encoding='utf-8')
    print(json.dumps(result, sort_keys=True))


main(sys.argv[sys.argv.index('--') + 1:])
