"""Verify sampled affine children retain source-parent edits after save and reopen."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import cast

import bpy
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_character_animation_curve_regression import matrix_error, source_transform


def world_vertices(obj: bpy.types.Object) -> tuple[Vector, ...]:
    graph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(graph)
    return tuple(evaluated.matrix_world @ vertex.co for vertex in evaluated.data.vertices)


def check_root_edits(rig: bpy.types.Object, mesh: bpy.types.Object) -> dict[str, float]:
    bpy.context.scene.frame_set(3)
    root = rig.pose.bones['Root']
    basis = root.matrix_basis.copy()
    root_matrix = root.matrix.copy()
    child_matrix = rig.pose.bones['Affine child'].matrix.copy()
    vertices = world_vertices(mesh)
    transforms = (
        ('translation', Matrix.Translation((.23, -.17, .31))),
        ('rotation', Matrix.Rotation(.42, 4, 'Z')),
        ('nonuniform_scale', Matrix.Diagonal((1.2, .8, 1.1, 1.))),
    )
    errors: dict[str, float] = {}
    for label, delta in transforms:
        root.matrix_basis = delta @ basis
        bpy.context.view_layer.update()
        actual_delta = root.matrix @ root_matrix.inverted()
        bone_error = matrix_error(rig.pose.bones['Affine child'].matrix, actual_delta @ child_matrix)
        world_delta = rig.matrix_world @ actual_delta @ rig.matrix_world.inverted()
        vertex_error = max((actual - world_delta @ original).length
                           for actual, original in zip(world_vertices(mesh), vertices, strict=True))
        errors[label] = max(bone_error, vertex_error)
        if errors[label] > 2e-4:
            raise AssertionError(f'Affine child ignored its root edit: mode={label}, error={errors[label]}')
    root.matrix_basis = basis
    bpy.context.view_layer.update()
    return errors


def main(source_dir: Path, output: Path) -> None:
    sys.path.insert(0, str(source_dir / 'character_runtime'))
    from character_animation_affine import AffineTarget, bind_affine_parent, validate_affine_targets
    from character_animation_binding import attach_animation, validate_attached_animation

    output.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    rig = bpy.data.objects.new('Affine parent regression', bpy.data.armatures.new('Regression skeleton'))
    scene.collection.objects.link(rig)
    host = bpy.data.objects.new('Transformed rig host', None)
    scene.collection.objects.link(host)
    host.matrix_world = Matrix.LocRotScale(Vector((1., -2., .5)), Quaternion(Vector((0., 0., 1.)), .23),
                                          Vector((1.25, .8, 1.4)))
    rig.parent = host
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    root = rig.data.edit_bones.new('Root')
    root.head, root.tail = (0., 0., 0.), (0., 1., 0.)
    child = rig.data.edit_bones.new('Affine child')
    child.head, child.tail = (.1, .8, .2), (.1, 1.2, .2)
    child.roll = .63
    child.parent = root
    bpy.ops.object.mode_set(mode='OBJECT')
    for name, path in (('Root', 'Avatar/Root'), ('Affine child', 'Avatar/Root/Child')):
        rig.data.bones[name]['source_transform_path'] = path
    data = bpy.data.meshes.new('Rigid attachment geometry')
    data.from_pydata([(.1, .8, .2), (.2, .8, .2), (.1, .9, .2)], [], [(0, 1, 2)])
    mesh = bpy.data.objects.new('Rigid attachment', data)
    scene.collection.objects.link(mesh)
    mesh.parent = rig
    mesh.vertex_groups.new(name='Affine child').add([0, 1, 2], 1., 'REPLACE')
    mesh.modifiers.new('Source skin', 'ARMATURE').object = rig
    hierarchy = output / 'source_hierarchy.json'
    frames_path = output / 'source_local_frames.json'
    rest_root = source_transform((0., 0., 0.), Quaternion(), (1., 1., 1.))
    rest_child = source_transform((.1, .8, .2), Quaternion(), (1., 1., 1.))
    hierarchy.write_text(json.dumps([
        {'index': 0, 'path': 'Avatar/Root', 'parent_index': -1, 'rest_local': rest_root},
        {'index': 1, 'path': 'Avatar/Root/Child', 'parent_index': 0, 'rest_local': rest_child},
    ]), encoding='utf-8')
    frames: list[dict[str, object]] = []
    for index in range(6):
        frames.append({'time': index / 30., 'locals': [
            source_transform((.03 * index, -.02 * index, .01 * index),
                             Quaternion(Vector((0., 0., 1.)), .1 * index), (1.4, .7, 1.1)),
            source_transform((.1, .8, .2), Quaternion(Vector((0., 0., 1.)), math.radians(35. * index)),
                             (1.8, .65, 1.15)),
        ]})
    frames_path.write_text(json.dumps(frames), encoding='utf-8')
    binding = attach_animation(rig, hierarchy, frames_path, 'Parent-relative affine source', 30., 2e-4)
    helpers = cast(list[dict[str, object]], binding['affine_targets'])
    if len(helpers) != 1 or helpers[0]['bone_name'] != 'Affine child':
        raise AssertionError(f'Fixture must exercise one child affine target: {helpers}')
    before = check_root_edits(rig, mesh)
    scene.frame_set(1)
    blend = output / 'affine_parent_regression.blend'
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False)
    rig = bpy.data.objects['Affine parent regression']
    validation = validate_attached_animation(rig, binding, tuple(range(1, 7)), 2e-4)
    after = check_root_edits(rig, bpy.data.objects['Rigid attachment'])
    objects = cast(list[dict[str, object]], helpers[0]['objects'])
    helper = AffineTarget('Affine child', bpy.data.objects[str(objects[0]['object'])],
                          bpy.data.objects[str(objects[1]['object'])], str(helpers[0]['constraint']))
    helper.left.parent_type = 'OBJECT'
    try:
        validate_affine_targets(rig, list(helpers), 6)
    except ValueError:
        pass
    else:
        raise AssertionError('Saved affine target validation accepted the detached legacy parent')
    bind_affine_parent(rig, helper)
    validate_affine_targets(rig, list(helpers), 6)
    report = {'status': 'passed', 'source_matrix_error': validation['maximum_matrix_error'],
              'root_edit_errors': before, 'reopened_root_edit_errors': after,
              'detached_parent_rejected': True, 'affine_targets': helpers}
    (output / 'validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('AFFINE_PARENT_FOLLOW_VERIFIED', json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--src', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.src, args.output)
