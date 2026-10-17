"""Check real IK bend anatomy, frame switching and reachable target drags."""
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

from character_rig_authoring import _action_signature, apply_rig_authoring
from character_rig_ik import LIMBS, STATE, SLOTS, _pole_direction, install_limb_ik, limb_bones, snap_limb_ik


class PoseSnapshot(TypedDict):
    bones: dict[str, tuple[float, ...]]
    meshes: dict[str, array]


class LimbState(TypedDict):
    bones: dict[str, str]
    property: str


class Placement(TypedDict):
    frame: int
    mode: str
    limb: str
    pole_distance_ratio: float
    pole_anatomical_side_distance: float
    source_bend_length_ratio: float


class DragResult(TypedDict):
    frame: int
    mode: str
    limb: str
    sample: str
    endpoint_error: float
    radial_forward: float
    radial_left: float
    radial_magnitude: float


def update(rig: bpy.types.Object) -> None:
    rig.update_tag(refresh={'DATA'})
    bpy.context.view_layer.update()


def joint(rig: bpy.types.Object, name: str) -> Vector:
    update(rig)
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return evaluated.pose.bones[name].matrix.translation.copy()


def states_for(rig: bpy.types.Object) -> dict[str, LimbState]:
    """Validate required persistent native controls while ignoring extra metadata."""
    raw = json.loads(rig[STATE])
    result: dict[str, LimbState] = {}
    for limb in LIMBS:
        entry = raw.get(limb)
        if not isinstance(entry, dict) or not isinstance(entry.get('property'), str):
            raise ValueError(f'Missing IK limb property in source: {rig.name}/{limb}')
        bones = entry.get('bones')
        if not isinstance(bones, dict) or any(not isinstance(bones.get(role), str)
                                              for role in ('Upper', 'Lower', 'Target', 'Pole')):
            raise ValueError(f'Missing IK native control mapping in source: {rig.name}/{limb}')
        if any(not isinstance(name, str) or name not in rig.pose.bones for name in bones.values()):
            raise ValueError(f'IK native control bone is absent: {rig.name}/{limb}')
        result[limb] = {'bones': bones, 'property': entry['property']}
    return result


def body(rig: bpy.types.Object) -> tuple[Vector, Vector, Vector, Vector]:
    """Use rest toes for fixed handedness and posed torso for anatomical axes."""
    slots = json.loads(rig[SLOTS])
    paths = {bone.get('source_transform_path'): bone.name for bone in rig.data.bones
             if bone.get('source_transform_path')}
    names = {slot: paths[slots[str(slot)]] for slot in (0, 1, 2, 5, 6, 11, 20, 21)}
    positions = {slot: joint(rig, name) for slot, name in names.items()}
    rest = {slot: rig.data.bones[name].head_local for slot, name in names.items()}
    up = (positions[11]-positions[0]).normalized()
    left = positions[1]-positions[2]
    left = (left-up*left.dot(up)).normalized()
    forward = left.cross(up).normalized()
    rest_forward = (rest[1]-rest[2]).cross(rest[11]-rest[0])
    if rest_forward.dot(rest[20]-rest[5]+rest[21]-rest[6]) < 0.:
        forward.negate()
    return left, up, forward, (positions[1]+positions[2])*.5


def snapshot(rig: bpy.types.Object) -> PoseSnapshot:
    """Read every source pose matrix and all visible evaluated skin vertices."""
    update(rig)
    graph = bpy.context.evaluated_depsgraph_get()
    evaluated = rig.evaluated_get(graph)
    bones = {bone.name: tuple(value for row in evaluated.pose.bones[bone.name].matrix for value in row)
             for bone in rig.data.bones if bone.get('source_transform_path')}
    meshes: dict[str, array] = {}
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or not obj.get('source_geometry_path') or obj.hide_get():
            continue
        evaluated_mesh = obj.evaluated_get(graph)
        mesh = evaluated_mesh.to_mesh(preserve_all_data_layers=False, depsgraph=graph)
        try:
            values = array('f', [0.])*(len(mesh.vertices)*3)
            mesh.vertices.foreach_get('co', values)
            meshes[obj.name] = values
        finally:
            evaluated_mesh.to_mesh_clear()
    if not meshes:
        raise AssertionError('No real visible source skin was evaluated')
    return {'bones': bones, 'meshes': meshes}


def error(first: PoseSnapshot, second: PoseSnapshot) -> float:
    if first['bones'].keys() != second['bones'].keys() or first['meshes'].keys() != second['meshes'].keys():
        raise AssertionError('Source bone or visible skin identities changed')
    bone_error = max(abs(a-b) for name, values in first['bones'].items()
                     for a, b in zip(values, second['bones'][name], strict=True))
    skin_error = max(abs(a-b) for name, values in first['meshes'].items()
                     for a, b in zip(values, second['meshes'][name], strict=True))
    return max(bone_error, skin_error)


def verify_reversed_feet(rig: bpy.types.Object) -> dict[str, object]:
    """Rotate both real FK feet 180 degrees without changing torso or pole anatomy."""
    bpy.context.scene.frame_set(1)
    before = snapshot(rig)
    slots = json.loads(rig[SLOTS])
    paths = {bone.get('source_transform_path'): bone.name for bone in rig.data.bones
             if bone.get('source_transform_path')}
    names = {slot: paths[slots[str(slot)]] for slot in (5, 6, 20, 21)}
    feet = tuple(rig.pose.bones[names[slot]] for slot in (5, 6))
    bases = tuple(bone.matrix_basis.copy() for bone in feet)
    _, up, forward, _ = body(rig)
    original_toes = {slot: joint(rig, name) for slot, name in names.items()}
    original_directions = {
        limb: _pole_direction(rig, limb, tuple(rig.pose.bones[name].matrix.copy()
                                              for name in limb_bones(rig, limb)))
        for limb in LIMBS}
    direction_errors: dict[str, float] = {}
    try:
        for bone in feet:
            original = bone.matrix.copy()
            rotated = Matrix.Rotation(math.pi, 4, up) @ original
            rotated.translation = original.translation
            bone.matrix = rotated
            update(rig)
        rotated_toes = {slot: joint(rig, name) for slot, name in names.items()}
        original_forward = sum((original_toes[toe]-original_toes[foot]).dot(forward)
                               for foot, toe in ((5, 20), (6, 21)))
        rotated_forward = sum((rotated_toes[toe]-rotated_toes[foot]).dot(forward)
                              for foot, toe in ((5, 20), (6, 21)))
        if original_forward <= 0. or rotated_forward >= -original_forward*.9:
            raise AssertionError(f'Actual source toes did not reverse: before={original_forward}, after={rotated_forward}')
        for limb in LIMBS:
            matrices = tuple(rig.pose.bones[name].matrix.copy() for name in limb_bones(rig, limb))
            direction_errors[limb] = (_pole_direction(rig, limb, matrices)-original_directions[limb]).length
            if direction_errors[limb] > .0001:
                raise AssertionError(f'Rotating real FK feet changed pole anatomy: {limb}/{direction_errors[limb]}')
    finally:
        for bone, basis in zip(feet, bases, strict=True):
            bone.matrix_basis = basis
        update(rig)
    restored_error = error(before, snapshot(rig))
    if restored_error > .0001:
        raise AssertionError(f'Resetting real FK feet changed source pose or skin: {restored_error}')
    return {'rotation_radians': math.pi, 'original_toe_forward': original_forward,
            'rotated_toe_forward': rotated_forward, 'pole_direction_vector_errors': direction_errors,
            'source_reset_error': restored_error}


def verify(rig: bpy.types.Object, origin: str, output: Path) -> dict[str, object]:
    """Exercise frame1 controls directly and resnap controls at every test frame."""
    states = states_for(rig)
    action = _action_signature(rig)
    for state in states.values():
        rig[state['property']] = 0.
    reversed_feet = verify_reversed_feet(rig)
    baselines: dict[int, PoseSnapshot] = {}
    for frame in (1, 18, 31):
        bpy.context.scene.frame_set(frame)
        baselines[frame] = snapshot(rig)
    bpy.context.scene.frame_set(1)
    neutral_errors: dict[str, float] = {}
    for limb in LIMBS:
        snap_limb_ik(rig, limb)
        neutral_errors[limb] = error(baselines[1], snapshot(rig))
        if neutral_errors[limb] > .0001:
            raise AssertionError(f'{origin}/{limb}: frame1 snap changed source skin: {neutral_errors[limb]}')
    for state in states.values():
        rig[state['property']] = 0.
    bases = {name: rig.pose.bones[name].matrix_basis.copy()
             for state in states.values() for name in state['bones'].values()}
    initial_angles = {limb: rig.pose.bones[state['bones']['Lower']].constraints['EF Native Editing IK'].pole_angle
                      for limb, state in states.items()}
    samples: list[DragResult] = []
    placements: list[Placement] = []
    for frame in (1, 18, 31):
        for mode in ('frame1_controls_toggle', 'snap_current_fk'):
            for limb, state in states.items():
                for item in states.values():
                    rig[item['property']] = 0.
                for name, value in bases.items():
                    rig.pose.bones[name].matrix_basis = value
                for name, item in states.items():
                    rig.pose.bones[item['bones']['Lower']].constraints['EF Native Editing IK'].pole_angle = initial_angles[name]
                bpy.context.scene.frame_set(frame)
                left, up, forward, center = body(rig)
                names = limb_bones(rig, limb)
                a, b, c = (joint(rig, name) for name in names)
                length = (b-a).length+(c-b).length
                axis = (c-a).normalized()
                original_bend = (b-a-axis*(b-a).dot(axis)).length/length
                if mode == 'snap_current_fk':
                    snap_limb_ik(rig, limb)
                else:
                    rig[state['property']] = 1.
                    update(rig)
                pole_position = joint(rig, state['bones']['Pole'])
                side = 1. if limb.startswith('Left') else -1.
                placement: Placement = {'frame': frame, 'mode': mode, 'limb': limb,
                                        'pole_distance_ratio': (pole_position-b).length/length,
                                        'pole_anatomical_side_distance': side*(pole_position-center).dot(left),
                                        'source_bend_length_ratio': original_bend}
                if mode == 'snap_current_fk':
                    if abs(placement['pole_distance_ratio']-.3) > .0001:
                        raise AssertionError(f'{origin}: pole is too far from source joint: {placement}')
                    if limb.endswith('Arm') and placement['pole_anatomical_side_distance'] < .01:
                        raise AssertionError(f'{origin}: elbow pole crossed the body midline: {placement}')
                placements.append(placement)
                target = rig.pose.bones[state['bones']['Target']]
                original = target.matrix.copy()
                drags = (('inward80', .8, Vector()), ('inward95', .95, Vector()),
                         ('forward', .85, forward*.15), ('backward', .85, -forward*.15),
                         ('left', .85, left*.15), ('right', .85, -left*.15),
                         ('raised', .75, up*.2))
                for label, fraction, offset in drags:
                    moved = original.copy()
                    requested = a+(c-a)*fraction+offset*length
                    moved.translation = requested
                    target.matrix = moved
                    source = tuple(joint(rig, name) for name in names)
                    drag_axis = (requested-source[0]).normalized()
                    radial = source[1]-source[0]-drag_axis*(source[1]-source[0]).dot(drag_axis)
                    row: DragResult = {'frame': frame, 'mode': mode, 'limb': limb, 'sample': label,
                                       'endpoint_error': (source[2]-requested).length,
                                       'radial_forward': radial.dot(forward), 'radial_left': radial.dot(left),
                                       'radial_magnitude': radial.length}
                    if row['endpoint_error'] > .001:
                        raise AssertionError(f'{origin}: actual source endpoint missed a reachable target: {row}')
                    if limb.endswith('Leg') and row['radial_forward'] < row['radial_magnitude']*.7:
                        raise AssertionError(f'{origin}: actual source knee bends sideways or backwards: {row}')
                    if limb.endswith('Arm') and row['radial_forward'] > row['radial_magnitude']*.2:
                        raise AssertionError(f'{origin}: actual source elbow bends forwards: {row}')
                    samples.append(row)
    for state in states.values():
        rig[state['property']] = 0.
    restored: dict[int, float] = {}
    for frame in (1, 18, 31):
        bpy.context.scene.frame_set(frame)
        restored[frame] = error(baselines[frame], snapshot(rig))
        if restored[frame] > .0001:
            raise AssertionError(f'{origin}: FK did not restore original source skin at frame {frame}')
    if _action_signature(rig) != action:
        raise AssertionError(f'{origin}: source Action was modified')
    bpy.context.scene.frame_set(1)
    for limb in LIMBS:
        snap_limb_ik(rig, limb)
    for state in states.values():
        rig[state['property']] = 0.
    saved = output/(origin+'_fixed_frame1.blend')
    bpy.ops.wm.save_as_mainfile(filepath=str(saved))
    return {'status': 'verified', 'origin': origin, 'saved_frame': 1, 'blend': str(saved),
            'reversed_real_fk_feet': reversed_feet,
            'frame1_neutral_source_errors': neutral_errors, 'source_fk_restore_errors': restored,
            'placements': placements, 'samples': samples,
            'maximum_endpoint_error': max(row['endpoint_error'] for row in samples)}


def open_rig(path: Path) -> bpy.types.Object:
    """Open a real fixture without running embedded scripts or guessing a rig."""
    if not path.is_file():
        raise FileNotFoundError(f'IK interaction fixture does not exist: {path}')
    bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False)
    rigs = tuple(obj for obj in bpy.data.objects if obj.type == 'ARMATURE')
    if len(rigs) != 1:
        raise ValueError(f'IK interaction fixture requires exactly one armature: {path}, count={len(rigs)}')
    return rigs[0]


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delivered-blend', required=True, type=Path)
    parser.add_argument('--source-blend', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    arguments = parser.parse_args(argv)
    output = arguments.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    delivered = verify(open_rig(arguments.delivered_blend), 'delivered', output)
    (output/'delivered_interactions.json').write_text(json.dumps(delivered, indent=2), encoding='utf-8')
    rig = open_rig(arguments.source_blend)
    bpy.context.scene.frame_set(1)
    apply_rig_authoring(rig, None)
    before = snapshot(rig)
    install_errors: dict[str, float] = {}
    for limb in LIMBS:
        install_limb_ik(rig, limb)
        install_errors[limb] = error(before, snapshot(rig))
        if install_errors[limb] > .0001:
            raise AssertionError(f'Fresh frame1 IK installation changed source pose: {limb}/{install_errors[limb]}')
    fresh = verify(rig, 'fresh', output)
    fresh['frame1_install_source_errors'] = install_errors
    (output/'fresh_interactions.json').write_text(json.dumps(fresh, indent=2), encoding='utf-8')
    summary = {'status': 'verified', 'drag_count': 336,
               'delivered_maximum_endpoint_error': delivered['maximum_endpoint_error'],
               'fresh_maximum_endpoint_error': fresh['maximum_endpoint_error'],
               'fresh_frame1_install_errors': install_errors}
    (output/'verification.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary))


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--')+1:])
