"""Verify fixed effect mounts preserve snapshots and obey exact source follow flags."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Quaternion, Vector


def matrix_error(left: Matrix, right: Matrix) -> float:
    return max(abs(left[row][column] - right[row][column])
               for row in range(4) for column in range(4))


def expected_anchor(rig: bpy.types.Object, rotation: int, scale: int) -> Matrix:
    world = rig.matrix_world @ rig.pose.bones['Mount'].matrix
    if rotation == 1 and scale == 1:
        return world
    location, quaternion, scaling = world.decompose()
    return Matrix.LocRotScale(location, quaternion if rotation == 1 else Quaternion(),
                              scaling if scale == 1 else Vector((1., 1., 1.)))


def check_motion(rig: bpy.types.Object, cases: list[tuple[str, str, int, int]],
                 source_worlds: dict[str, Matrix], bind_anchors: dict[str, Matrix]) -> dict[str, float]:
    transforms = (
        ('translation', Matrix.Translation((.23, -.17, .31))),
        ('rotation', Matrix.Rotation(.6, 4, 'Z')),
        ('nonuniform_scale', Matrix.Diagonal((1.3, .8, 1.1, 1.))),
    )
    errors: dict[str, float] = {}
    for label, matrix in transforms:
        rig.pose.bones['Root'].matrix_basis = matrix
        bpy.context.view_layer.update()
        for anchor_name, obj_name, rotation, scale in cases:
            target = expected_anchor(rig, rotation, scale)
            anchor = bpy.data.objects[anchor_name]
            obj = bpy.data.objects[obj_name]
            expected = target @ bind_anchors[anchor_name].inverted() @ source_worlds[obj_name]
            error = max(matrix_error(anchor.matrix_world, target), matrix_error(obj.matrix_world, expected))
            errors[label + '_' + str(rotation) + str(scale)] = error
            if error > 2e-5:
                raise AssertionError(f'Effect mount source flags differ: mode={label}, rotation={rotation}, scale={scale}, error={error}')
    rig.pose.bones['Root'].matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()
    return errors


def main(source_dir: Path, output: Path) -> None:
    sys.path.insert(0, str(source_dir / 'character_runtime'))
    from character_effect_attachment import attach_effect_snapshot, validate_effect_attachment

    output.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    rig = bpy.data.objects.new('Effect mount regression', bpy.data.armatures.new('Source skeleton'))
    scene.collection.objects.link(rig)
    rig.matrix_world = Matrix.Translation((.2, -.1, .4)) @ Matrix.Rotation(.4, 4, 'X')
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    root = rig.data.edit_bones.new('Root')
    root.head, root.tail = (0., 0., 0.), (0., .8, 0.)
    mount = rig.data.edit_bones.new('Mount')
    mount.head, mount.tail = (.1, .8, .2), (.1, 1.1, .2)
    mount.parent = root
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.data.bones['Mount']['source_transform_path'] = 'Avatar/Root/Mount'
    source_worlds: dict[str, Matrix] = {}
    bind_anchors: dict[str, Matrix] = {}
    cases: list[tuple[str, str, int, int]] = []
    maximum_initial_error = 0.
    for rotation, scale in ((1, 0), (1, 1), (0, 0), (0, 1)):
        label = f'R{rotation}S{scale}'
        mesh = bpy.data.meshes.new(label + ' snapshot geometry')
        mesh.from_pydata([(0., 0., 0.), (.1, 0., 0.), (0., .1, 0.)], [], [(0, 1, 2)])
        obj = bpy.data.objects.new(label + ' snapshot', mesh)
        scene.collection.objects.link(obj)
        obj.matrix_world = Matrix.LocRotScale(Vector((.3, 1.1, .7)), Quaternion(Vector((0., 0., 1.)), .2),
                                              Vector((.7, 1.2, .9)))
        bpy.context.view_layer.update()
        original = obj.matrix_world.copy()
        reference = 'Avatar/Root/Mount' if scale == 0 else 'Mount'
        anchor = attach_effect_snapshot(rig, reference, rotation, scale, (obj,))
        maximum_initial_error = max(maximum_initial_error, matrix_error(original, obj.matrix_world))
        source_worlds[obj.name] = original
        bind_anchors[anchor.name] = expected_anchor(rig, rotation, scale)
        cases.append((anchor.name, obj.name, rotation, scale))
    errors = check_motion(rig, cases, source_worlds, bind_anchors)
    blend = output / 'effect_attachment_regression.blend'
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False)
    rig = bpy.data.objects['Effect mount regression']
    reopened_error = 0.
    for anchor_name, obj_name, rotation, scale in cases:
        anchor = bpy.data.objects[anchor_name]
        obj = bpy.data.objects[obj_name]
        validate_effect_attachment(anchor, rig, (obj,))
        reopened_error = max(reopened_error, matrix_error(source_worlds[obj_name], obj.matrix_world))
    if max(maximum_initial_error, reopened_error) > 2e-5:
        raise AssertionError(f'Effect snapshot keep-world or saved-reopen validation failed: initial={maximum_initial_error}, reopened={reopened_error}')
    reopened_motion = check_motion(rig, cases, source_worlds, bind_anchors)
    report = {'status': 'passed', 'source_flag_combinations': 4, 'initial_world_matrix_error': maximum_initial_error,
              'root_edit_errors': errors, 'reopened_world_matrix_error': reopened_error,
              'reopened_root_edit_errors': reopened_motion}
    (output / 'validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('EFFECT_ATTACHMENT_VERIFIED', json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--src', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.src, args.output)
