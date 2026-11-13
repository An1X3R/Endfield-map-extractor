"""Verify unweighted controls against real skin bind anchors and native limb IK."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Matrix

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/character_runtime'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from character_geometry import _matrix, _nodes, _worlds, import_character_geometry, read_geometry_payload
from character_rig_authoring import apply_rig_authoring, read_humanoid_bone_map
from character_rig_ik import install_limb_ik, limb_bones
from blender_character_rig_ik_regression import difference, require_preserved, snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geometry', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    document, _ = read_geometry_payload(args.geometry)
    nodes = _nodes(document['nodes'])
    worlds = _worlds(nodes)
    weighted = {bone['path'] for mesh in document['meshes'] for bone in mesh['bones']}
    objects = import_character_geometry(args.geometry)
    rig = next(obj for obj in objects if obj.type == 'ARMATURE')
    slots = read_humanoid_bone_map(args.geometry.parent / 'humanoid_bone_map.json', rig['source_transform_path'])
    apply_rig_authoring(rig, slots)
    source_names = tuple(rig.data.bones.keys())
    by_path = {bone.get('source_transform_path'): bone.name for bone in rig.data.bones}
    anchors: list[dict[str, object]] = []
    for slot in (14, 15, 16, 17):
        path = slots[slot]
        if path in weighted:
            continue
        children = tuple(node for node in nodes if node.parent == path and node.path in weighted
                         and node.local.translation.length < 1e-5)
        if not children:
            raise AssertionError(f'Fixture has no direct weighted anchor for arm slot {slot}')
        child = children[0]
        mesh, reference = next((mesh, bone) for mesh in document['meshes']
                               for bone in mesh['bones'] if bone['path'] == child.path)
        native = worlds[mesh['path']] @ _matrix(reference['inverse_bind_matrix_column_major']).inverted()
        expected = native @ child.local.inverted()
        actual = rig.data.bones[by_path[path]].matrix_local
        error = (actual.translation - expected.translation).length
        angle = actual.to_quaternion().rotation_difference(expected.to_quaternion()).angle
        if error > 5e-5 or min(angle, abs(math.tau - angle)) > .001:
            raise AssertionError(f'Unweighted control differs from direct skin evidence: {path}, {error}, {angle}')
        anchors.append({'slot': slot, 'anchor': child.name, 'position_error_meters': error})
    if not anchors:
        raise AssertionError('Fixture does not exercise any unweighted arm controls')
    baseline = snapshot(rig, source_names)
    fk: dict[str, float] = {}
    for limb in ('LeftArm', 'RightArm'):
        upper, lower, end = limb_bones(rig, limb)
        for name in (upper, lower, end):
            bone = rig.pose.bones[name]
            bone.matrix_basis = Matrix.Rotation(.3, 4, 'Z')
            movement = difference(baseline, snapshot(rig, source_names))['source_skin_vertex_maximum']
            if movement < .001:
                raise AssertionError(f'FK control fails to deform real skin: {name}, {movement}')
            fk[name] = movement
            bone.matrix_basis = Matrix.Identity(4)
            require_preserved(baseline, snapshot(rig, source_names), 'Reset FK ' + name)
    ik: dict[str, dict[str, float]] = {}
    for limb in ('LeftArm', 'RightArm', 'LeftLeg', 'RightLeg'):
        state = install_limb_ik(rig, limb)
        require_preserved(baseline, snapshot(rig, source_names), 'Neutral IK ' + limb)
        upper, _, end = limb_bones(rig, limb)
        target = rig.pose.bones[state['bones']['Target']]
        original = target.matrix.copy()
        moved = original.copy()
        origin = rig.pose.bones[upper].head.copy()
        moved.translation = origin + (original.translation - origin) * .85
        target.matrix = moved
        current = snapshot(rig, source_names)
        endpoint_error = (rig.pose.bones[end].head - target.matrix.translation).length
        displacement = difference(baseline, current)['source_skin_vertex_maximum']
        if endpoint_error > .001 or displacement < .001:
            raise AssertionError(f'IK control misses its skin/endpoint: {limb}, {endpoint_error}, {displacement}')
        target.matrix = original
        require_preserved(baseline, snapshot(rig, source_names), 'Restore IK target ' + limb)
        orientation = rig.pose.bones[state['bones']['EndOutput']]
        before = rig.pose.bones[end].matrix.to_quaternion()
        orientation.matrix_basis = Matrix.Rotation(.3, 4, 'X')
        snapshot(rig, source_names)
        angle = before.rotation_difference(rig.pose.bones[end].matrix.to_quaternion()).angle
        angle = min(angle, abs(math.tau - angle))
        if abs(angle - .3) > .001:
            raise AssertionError(f'IK end rotation failed: {limb}, {angle}')
        orientation.matrix_basis = Matrix.Identity(4)
        rig[state['property']] = 0.0
        require_preserved(baseline, snapshot(rig, source_names), 'Disable IK ' + limb)
        ik[limb] = {'endpoint_error_meters': endpoint_error, 'skin_displacement_meters': displacement,
                    'end_rotation_radians': angle}
    result = {'status': 'passed', 'anchors': anchors, 'fk_skin_movement': fk, 'ik': ik}
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
