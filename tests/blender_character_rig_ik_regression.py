"""Verify optional native limb IK against real source bones and evaluated skin."""
from __future__ import annotations

import argparse
from array import array
import json
import math
from pathlib import Path
import sys
from typing import TypedDict

import bpy
from mathutils import Matrix, Vector

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))

from character_rig_authoring import _action_signature, _constraint_signature, apply_rig_authoring
from character_rig_ik import LIMBS, STATE, install_limb_ik, limb_bones, register, snap_limb_ik


class PoseSnapshot(TypedDict):
    bones: dict[str, tuple[float, ...]]
    meshes: dict[str, array]


def original_rest(rig: bpy.types.Object, names: tuple[str, ...]) -> tuple[tuple[object, ...], ...]:
    """Capture original rest transforms, parent bindings and deform flags."""
    return tuple((name, rig.data.bones[name].parent.name if rig.data.bones[name].parent else None,
                  tuple(rig.data.bones[name].head_local), tuple(rig.data.bones[name].tail_local),
                  tuple(value for row in rig.data.bones[name].matrix_local for value in row),
                  rig.data.bones[name].use_deform, rig.data.bones[name].get('source_transform_path'))
                 for name in names)


def snapshot(rig: bpy.types.Object, names: tuple[str, ...]) -> PoseSnapshot:
    """Read actual source pose matrices and evaluated visible source skin vertices."""
    rig.update_tag(refresh={'DATA'})
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated_rig = rig.evaluated_get(depsgraph)
    bones = {name: tuple(float(value) for row in evaluated_rig.pose.bones[name].matrix for value in row)
             for name in names}
    meshes: dict[str, array] = {}
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or not obj.get('source_geometry_path') or obj.hide_get():
            continue
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
        try:
            coordinates = array('f', [0.0]) * (len(mesh.vertices) * 3)
            mesh.vertices.foreach_get('co', coordinates)
            meshes[obj.name] = coordinates
        finally:
            evaluated.to_mesh_clear()
    if not meshes:
        raise AssertionError('The regression source has no visible source-identified skin meshes')
    return {'bones': bones, 'meshes': meshes}


def difference(first: PoseSnapshot, second: PoseSnapshot) -> dict[str, float]:
    """Compare complete source snapshots with matching bone and mesh identities."""
    if first['bones'].keys() != second['bones'].keys() or first['meshes'].keys() != second['meshes'].keys():
        raise AssertionError('Source bone or visible skin identity changed')
    bone_error = max(abs(left - right) for name, values in first['bones'].items()
                     for left, right in zip(values, second['bones'][name], strict=True))
    mesh_error = max(abs(left - right) for name, values in first['meshes'].items()
                     for left, right in zip(values, second['meshes'][name], strict=True))
    return {'source_bone_matrix_maximum': bone_error, 'source_skin_vertex_maximum': mesh_error}


def require_preserved(first: PoseSnapshot, second: PoseSnapshot, label: str) -> dict[str, float]:
    """Fail immediately when source pose or deformed skin exceeds float precision."""
    errors = difference(first, second)
    if max(errors.values()) > 0.0001:
        raise AssertionError(f'{label}: source pose or skin changed: {errors}')
    return errors


def evaluated_joint(rig: bpy.types.Object, name: str) -> Vector:
    """Return the evaluated original bone center in armature space."""
    rig.update_tag(refresh={'DATA'})
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return evaluated.pose.bones[name].matrix.translation.copy()


def evaluated_world_matrix(rig: bpy.types.Object, name: str) -> Matrix:
    """Read a native source or target bone's evaluated world transform."""
    rig.update_tag(refresh={'DATA'})
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return evaluated.matrix_world @ evaluated.pose.bones[name].matrix


def resource_signature(rig: bpy.types.Object) -> tuple[object, ...]:
    """Capture resource ownership before a deliberately invalid real setup request."""
    return (tuple(sorted(rig.data.bones.keys())), tuple(sorted(bpy.data.objects.keys())),
            tuple(sorted(bpy.data.meshes.keys())), tuple(sorted(rig.data.collections.keys())),
            _constraint_signature(rig), tuple(sorted(rig.keys())), rig.get(STATE),
            tuple((curve.data_path, curve.array_index) for curve in rig.animation_data.drivers))


def verify_missing_widgets(rig: bpy.types.Object) -> str:
    """A missing owned widget collection must fail before any source mutation."""
    widget = next(item for item in bpy.data.collections
                  if item.get('endfield_rig_authoring_armature') == rig.name
                  and item.get('endfield_rig_authoring_role') == 'Rig Widgets')
    before = resource_signature(rig)
    widget['endfield_rig_authoring_role'] = 'Unavailable Regression Widgets'
    try:
        try:
            install_limb_ik(rig, 'LeftArm')
        except ValueError as error:
            if 'existing rig authoring widget collection' not in str(error):
                raise
            message = str(error)
        else:
            raise AssertionError('Missing widget collection was accepted by IK installation')
    finally:
        widget['endfield_rig_authoring_role'] = 'Rig Widgets'
    if before != resource_signature(rig):
        raise AssertionError('Missing-widget preflight changed source or authoring resources')
    return message


def verify_ambiguous_paths(rig: bpy.types.Object) -> str:
    """Duplicate real source paths must fail explicitly before editing the rig."""
    mapped = tuple(bone for bone in rig.data.bones if bone.get('source_transform_path'))
    bone = mapped[0]
    original = bone['source_transform_path']
    before = resource_signature(rig)
    bone['source_transform_path'] = mapped[1]['source_transform_path']
    try:
        try:
            install_limb_ik(rig, 'LeftArm')
        except ValueError as error:
            if 'Ambiguous source transform path' not in str(error):
                raise
            message = str(error)
        else:
            raise AssertionError('Ambiguous source paths were accepted by IK installation')
    finally:
        bone['source_transform_path'] = original
    if before != resource_signature(rig):
        raise AssertionError('Ambiguous-path preflight changed source or authoring resources')
    return message


def verify_owned_name_collision(rig: bpy.types.Object) -> str:
    """A real reserved-name collision must preserve pre-existing native constraints."""
    bone_name = 'EF IK LeftArm :: Upper'
    constraint_name = 'EF IK LeftArm :: Source Output'
    source_name = limb_bones(rig, 'LeftArm')[0]
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bone = rig.data.edit_bones.new(bone_name)
    bone.head, bone.tail, bone.use_deform = (0.0, 0.0, 0.0), (0.0, 0.1, 0.0), False
    bpy.ops.object.mode_set(mode='OBJECT')
    constraint = rig.pose.bones[source_name].constraints.new('LIMIT_ROTATION')
    constraint.name, constraint.influence = constraint_name, 0.0
    before = resource_signature(rig)
    try:
        try:
            install_limb_ik(rig, 'LeftArm')
        except ValueError as error:
            message = str(error)
        else:
            raise AssertionError('Existing generated bone name was accepted by IK installation')
        if before != resource_signature(rig):
            raise AssertionError('Failed IK installation deleted pre-existing source constraints or other resources')
    finally:
        existing = rig.pose.bones[source_name].constraints.get(constraint_name)
        if existing is not None:
            rig.pose.bones[source_name].constraints.remove(existing)
        bpy.ops.object.mode_set(mode='EDIT')
        rig.data.edit_bones.remove(rig.data.edit_bones[bone_name])
        bpy.ops.object.mode_set(mode='OBJECT')
    return message


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    rig = next(obj for obj in bpy.data.objects if obj.type == 'ARMATURE')
    source_names = tuple(sorted(bone.name for bone in rig.data.bones))
    source_rest = original_rest(rig, source_names)
    action = _action_signature(rig)
    frames = (1, 18, 31, 61, 120)
    baseline: dict[int, PoseSnapshot] = {}
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        baseline[frame] = snapshot(rig, source_names)
    bpy.context.scene.frame_set(18)
    apply_rig_authoring(rig, None)
    baseline_check = require_preserved(baseline[18], snapshot(rig, source_names), 'FK authoring setup')
    preflight = {'missing_widget_collection': verify_missing_widgets(rig),
                 'ambiguous_source_paths': verify_ambiguous_paths(rig),
                 'preserved_existing_name_collision': verify_owned_name_collision(rig)}
    neutral: dict[str, dict[str, float]] = {}
    for limb in LIMBS:
        install_limb_ik(rig, limb)
        neutral[limb] = require_preserved(baseline[18], snapshot(rig, source_names), 'Neutral installation ' + limb)
    register()
    mesh = bpy.data.objects[next(iter(baseline[18]['meshes']))]
    bpy.ops.object.select_all(action='DESELECT')
    mesh.select_set(True)
    bpy.context.view_layer.objects.active = mesh
    selection_result = bpy.ops.endfield.select_ik_control(rig_name=rig.name, limb='LeftArm', control='Target')
    selected_target = json.loads(rig[STATE])['LeftArm']['bones']['Target']
    if (selection_result != {'FINISHED'} or bpy.context.object != rig or rig.mode != 'POSE'
            or rig.data.bones.active.name != selected_target
            or [bone.name for bone in rig.data.bones if bone.select] != [selected_target]):
        raise AssertionError('Native IK selection did not activate exactly the requested target from mesh state')
    require_preserved(baseline[18], snapshot(rig, source_names), 'Native target selection from mesh state')

    movement: dict[str, dict[str, float]] = {}
    target_rotation: dict[str, dict[str, float]] = {}
    independent_rotation: dict[str, dict[str, float]] = {}
    for limb in LIMBS:
        state = json.loads(rig[STATE])[limb]
        upper_name, lower_name, end_name = limb_bones(rig, limb)
        upper = evaluated_joint(rig, upper_name)
        lower = evaluated_joint(rig, lower_name)
        end = evaluated_joint(rig, end_name)
        length = (lower - upper).length + (end - lower).length
        target = rig.pose.bones[state['bones']['Target']]
        pole = rig.pose.bones[state['bones']['Pole']]
        target_original, pole_original = target.matrix.copy(), pole.matrix.copy()
        moved = target.matrix.copy()
        moved.translation = upper + (end - upper) * 0.83
        target.matrix = moved
        after_target = snapshot(rig, source_names)
        target_error = (evaluated_joint(rig, end_name) - target.matrix.translation).length
        if target_error > length * 0.003:
            diagnostic = {'target': list(target.matrix.translation), 'source_end': list(evaluated_joint(rig, end_name)),
                          'evaluated_target': list(evaluated_joint(rig, target.name)),
                          'target_basis': [list(row) for row in target.matrix_basis],
                          'solver_constraints': [(item.name, item.type, item.influence, item.mute, item.is_valid)
                                                 for item in rig.pose.bones[state['bones']['Lower']].constraints],
                          'solver_settings': {name: getattr(rig.pose.bones[state['bones']['Lower']].constraints[0], name)
                                              for name in ('use_location', 'use_rotation', 'use_tail', 'ik_type', 'chain_count', 'weight')},
                          'solver_tail': list(rig.pose.bones[state['bones']['Lower']].tail),
                          'outputs': {name: [constraint.influence for constraint in rig.pose.bones[name].constraints]
                                      for name in (upper_name, lower_name)},
                          'property': rig[state['property']]}
            raise AssertionError(f'{limb}: original source endpoint misses IK target: {target_error}, limb_length={length}, diagnostic={diagnostic}')
        skin_movement = difference(baseline[18], after_target)['source_skin_vertex_maximum']
        if skin_movement < 0.0001:
            raise AssertionError(f'{limb}: IK target moved but original source skin did not deform')
        bend_before = evaluated_joint(rig, lower_name)
        axis = (target.matrix.translation - upper).normalized()
        radial = pole.matrix.translation - upper
        rotated = Matrix.Rotation(0.8, 3, axis) @ radial
        moved_pole = pole.matrix.copy()
        moved_pole.translation = upper + rotated
        pole.matrix = moved_pole
        bend_after = evaluated_joint(rig, lower_name)
        pole_displacement = (bend_after - bend_before).length
        pole_endpoint_error = (evaluated_joint(rig, end_name) - target.matrix.translation).length
        if pole_displacement < length * 0.02 or pole_endpoint_error > length * 0.003:
            raise AssertionError(f'{limb}: pole failed to steer source bend while preserving endpoint: bend={pole_displacement}, endpoint={pole_endpoint_error}')
        movement[limb] = {'target_endpoint_error': target_error, 'pole_endpoint_error': pole_endpoint_error,
                          'source_joint_pole_displacement': pole_displacement, 'source_skin_displacement': skin_movement}
        target.matrix, pole.matrix = target_original, pole_original
        require_preserved(baseline[18], snapshot(rig, source_names), 'Reset target and pole ' + limb)
        source_rotation_before = evaluated_world_matrix(rig, end_name).to_quaternion()
        rotated_target = Matrix.Rotation(0.2, 4, 'Z') @ target_original
        rotated_target.translation = target_original.translation
        target.matrix = rotated_target
        rotated_pose = snapshot(rig, source_names)
        source_rotation = evaluated_world_matrix(rig, end_name).to_quaternion()
        desired_rotation = evaluated_world_matrix(rig, target.name).to_quaternion()
        rotation_error = source_rotation.rotation_difference(desired_rotation).angle
        actual_rotation = source_rotation_before.rotation_difference(source_rotation).angle
        rotation_endpoint_error = (evaluated_joint(rig, end_name) - target.matrix.translation).length
        rotation_skin = difference(baseline[18], rotated_pose)['source_skin_vertex_maximum']
        if rotation_error > 0.001 or actual_rotation < 0.19 or rotation_endpoint_error > length * 0.003 or rotation_skin < 0.0001:
            raise AssertionError(f'{limb}: source hand/foot did not follow target rotation: rotation_error={rotation_error}, actual_rotation={actual_rotation}, endpoint_error={rotation_endpoint_error}, skin={rotation_skin}')
        target_rotation[limb] = {'source_world_rotation_error': rotation_error, 'source_rotation_radians': actual_rotation,
                                 'source_endpoint_error': rotation_endpoint_error, 'source_skin_displacement': rotation_skin}
        target.matrix = target_original
        require_preserved(baseline[18], snapshot(rig, source_names), 'Reset target rotation ' + limb)
        orientation = rig.pose.bones[state['bones']['EndOutput']]
        if (orientation.custom_shape is None or not orientation.get('endfield_ik_rotation_control')
                or not all(orientation.lock_location) or not all(orientation.lock_scale)):
            raise AssertionError(f'{limb}: wrist/ankle rotation control is not exposed safely')
        basis_before = orientation.matrix_basis.copy()
        chain_before = tuple(evaluated_world_matrix(rig, name) for name in (upper_name, lower_name))
        selection = bpy.ops.endfield.select_ik_control(rig_name=rig.name, limb=limb, control='EndOutput')
        if selection != {'FINISHED'} or rig.data.bones.active.name != orientation.name:
            raise AssertionError(f'{limb}: wrist/ankle rotation selection failed')
        orientation.matrix_basis = basis_before @ Matrix.Rotation(0.3, 4, 'X')
        rotated = snapshot(rig, source_names)
        rotation = evaluated_world_matrix(rig, end_name).to_quaternion()
        expected = evaluated_world_matrix(rig, orientation.name).to_quaternion()
        angle = source_rotation_before.rotation_difference(rotation).angle
        angle = min(angle, math.tau-angle)
        rotation_error = rotation.rotation_difference(expected).angle
        chain_error = max(abs(before[i][j] - evaluated_world_matrix(rig, name)[i][j])
                          for name, before in zip((upper_name, lower_name), chain_before, strict=True)
                          for i in range(4) for j in range(4))
        skin_error = difference(baseline[18], rotated)['source_skin_vertex_maximum']
        if not 0.29 < angle < 0.31 or rotation_error > 0.001 or chain_error > 0.0001 or skin_error < 0.0001:
            raise AssertionError(f'{limb}: independent wrist/ankle rotation failed: angle={angle}, rotation={rotation_error}, chain={chain_error}, skin={skin_error}')
        independent_rotation[limb] = {'angle': angle, 'rotation_error': rotation_error,
                                      'upper_lower_chain_error': chain_error, 'skin_displacement': skin_error}
        orientation.matrix_basis = basis_before
        require_preserved(baseline[18], snapshot(rig, source_names), 'Reset independent end rotation ' + limb)

    states = json.loads(rig[STATE])
    for state in states.values():
        rig[state['property']] = 0.0
    restored: dict[int, dict[str, float]] = {}
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        restored[frame] = require_preserved(baseline[frame], snapshot(rig, source_names), 'Source FK at frame ' + str(frame))
    if original_rest(rig, source_names) != source_rest or _action_signature(rig) != action:
        raise AssertionError('IK installation changed source rest matrices, bone identities or original Action')
    bpy.context.scene.frame_set(31)
    snapped: dict[str, dict[str, float]] = {}
    snap_movement: dict[str, float] = {}
    for limb in LIMBS:
        result = bpy.ops.endfield.snap_character_ik(rig_name=rig.name, limb=limb)
        if result != {'FINISHED'}:
            raise AssertionError(f'{limb}: native snap operator did not finish: {result}')
        snapped[limb] = require_preserved(baseline[31], snapshot(rig, source_names), 'Snap current source FK ' + limb)
        target = rig.pose.bones[states[limb]['bones']['Target']]
        original = target.matrix.copy()
        upper_name, _, end_name = limb_bones(rig, limb)
        upper = evaluated_joint(rig, upper_name)
        moved = original.copy()
        moved.translation = upper + (original.translation - upper) * 0.87
        target.matrix = moved
        error = (evaluated_joint(rig, end_name) - target.matrix.translation).length
        skin = difference(baseline[31], snapshot(rig, source_names))['source_skin_vertex_maximum']
        if error > 0.001 or skin < 0.0001:
            raise AssertionError(f'{limb}: snapped IK failed to drive actual source endpoint or skin: endpoint={error}, skin={skin}')
        snap_movement[limb] = error
        target.matrix = original
        require_preserved(baseline[31], snapshot(rig, source_names), 'Reset snapped source FK ' + limb)
    for state in states.values():
        rig[state['property']] = 0.0
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        require_preserved(baseline[frame], snapshot(rig, source_names), 'Source FK after snapping at frame ' + str(frame))
    bpy.context.scene.frame_set(18)
    for limb in LIMBS:
        snap_limb_ik(rig, limb)
    saved_pose = snapshot(rig, source_names)
    require_preserved(baseline[18], saved_pose, 'Neutral before native IK save')
    saved = output / 'pelica_native_limb_ik.blend'
    rig_name = rig.name
    bpy.ops.wm.save_as_mainfile(filepath=str(saved))
    bpy.context.preferences.filepaths.use_scripts_auto_execute = False
    bpy.ops.wm.open_mainfile(filepath=str(saved), use_scripts=False)
    rig = bpy.data.objects[rig_name]
    reopened = require_preserved(saved_pose, snapshot(rig, source_names), 'Native IK reopen with scripts disabled')
    state = json.loads(rig[STATE])['LeftArm']
    target = rig.pose.bones[state['bones']['Target']]
    original = target.matrix.copy()
    moved = original.copy()
    source_upper = evaluated_joint(rig, limb_bones(rig, 'LeftArm')[0])
    moved.translation = source_upper + (original.translation - source_upper) * 0.9 + Vector((0.005, 0.0, 0.005))
    target.matrix = moved
    end_name = limb_bones(rig, 'LeftArm')[2]
    reopened_error = (evaluated_joint(rig, end_name) - target.matrix.translation).length
    reopened_movement = difference(saved_pose, snapshot(rig, source_names))['source_skin_vertex_maximum']
    if reopened_error > 0.001 or reopened_movement < 0.0001:
        raise AssertionError(f'Native IK did not evaluate after reopen with scripts disabled: endpoint={reopened_error}, skin={reopened_movement}')
    target.matrix = original
    require_preserved(saved_pose, snapshot(rig, source_names), 'Native IK reset after reopen')
    result = {'status': 'verified', 'blend': str(saved), 'source_bones': len(source_names),
              'visible_source_meshes': sorted(baseline[18]['meshes']),
              'visible_source_vertex_count': sum(len(values) // 3 for values in baseline[18]['meshes'].values()),
              'preflight_harmless_failures': preflight, 'frame31_snap': snapped,
              'frame31_snap_target_endpoint_errors': snap_movement,
              'fk_authoring': baseline_check, 'neutral_installations': neutral, 'movement': movement,
              'target_rotation': target_rotation,
              'independent_wrist_ankle_rotation': independent_rotation,
              'native_target_selection_from_mesh': selected_target,
              'source_fk_restored_frames': restored, 'reopen_scripts_disabled': reopened,
              'reopen_target_endpoint_error': reopened_error, 'reopen_source_skin_displacement': reopened_movement,
              'source_action_unchanged': _action_signature(rig) == action,
              'source_rest_unchanged': original_rest(rig, source_names) == source_rest}
    (output / 'verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
