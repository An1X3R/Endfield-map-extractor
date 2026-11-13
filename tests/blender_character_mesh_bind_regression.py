"""Compare real per-mesh skin binds with evaluated Blender deformation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Matrix
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/character_runtime'))
from character_geometry import (
    _array, _nodes, _worlds, geometry_bind_layout, import_character_geometry,
    read_geometry_payload, has_source_bind_constraint,
)
from preview_character import require_static_pose_scene


def inspect_deformation(geometry: Path) -> list[dict[str, object]]:
    document, payload = read_geometry_payload(geometry)
    meshes = tuple(document['meshes'])
    binds, mesh_worlds, overrides = geometry_bind_layout(_worlds(_nodes(document['nodes'])), meshes)
    objects = import_character_geometry(geometry)
    rig = next(obj for obj in objects if obj.type == 'ARMATURE')
    by_path = {bone.get('source_transform_path'): bone.name for bone in rig.data.bones
               if isinstance(bone.get('source_transform_path'), str)}
    aliases = tuple(bone for bone in rig.pose.bones if bone.bone.get('source_bind_target'))
    if not aliases or not all(has_source_bind_constraint(rig, bone) for bone in aliases):
        raise AssertionError('A real mesh-specific bind sample is required')
    require_static_pose_scene(bpy.context.scene)
    aliases[0].constraints[0].mute = True
    try:
        require_static_pose_scene(bpy.context.scene)
    except RuntimeError:
        pass
    else:
        raise AssertionError('A modified source bind constraint must remain a static-pose blocker')
    finally:
        aliases[0].constraints[0].mute = False
    mesh_objects = {obj['source_geometry_path']: obj for obj in objects if obj.type == 'MESH'}
    results: list[dict[str, object]] = []
    rest_positions: dict[str, np.ndarray] = {}
    targets = {bone.bone['source_bind_target'] for bone in aliases}
    for scenario, angle, translation in (('rest', 0.0, (0.0, 0.0, 0.0)),
                                         ('joint_rotation', .35, (0.0, 0.0, 0.0)),
                                         ('moved_rig', .35, (.42, -.23, .17))):
        for name in targets:
            rig.pose.bones[name].matrix_basis = Matrix.Rotation(angle, 4, 'Z')
        rig.location = translation
        bpy.context.view_layer.update()
        graph = bpy.context.evaluated_depsgraph_get()
        evaluated_rig = rig.evaluated_get(graph)
        for row in meshes:
            path = row['path']
            if path not in overrides:
                continue
            positions = _array(row['positions'], payload, 3, 'float32')
            homogeneous = np.column_stack((positions, np.ones(len(positions))))
            offsets = _array(row['skin']['offsets'], payload, 1, 'uint32').ravel()
            indices = _array(row['skin']['indices'], payload, 1, 'int32').ravel()
            weights = _array(row['skin']['weights'], payload, 1, 'float32').ravel()
            vertex_indices = np.repeat(np.arange(len(positions)), np.diff(offsets))
            expected = np.zeros((len(positions), 3), dtype=np.float64)
            sums = np.zeros(len(positions), dtype=np.float64)
            for index, bone in enumerate(row['bones']):
                selected = (indices == index) & (weights > 0)
                vertex_ids = vertex_indices[selected]
                bind = overrides[path].get(bone['path'], binds[bone['path']])
                posed = evaluated_rig.pose.bones[by_path[bone['path']]].matrix
                transform = np.asarray(rig.matrix_world @ posed @ bind.inverted() @ mesh_worlds[path])
                transformed = homogeneous[vertex_ids] @ transform.T
                np.add.at(expected, vertex_ids, transformed[:, :3] * weights[selected, None])
                np.add.at(sums, vertex_ids, weights[selected])
            expected = np.divide(expected, sums[:, None], out=expected, where=sums[:, None] > 0)
            evaluated = mesh_objects[path].evaluated_get(graph)
            evaluated_mesh = evaluated.to_mesh()
            try:
                actual = np.array([tuple(evaluated.matrix_world @ vertex.co) for vertex in evaluated_mesh.vertices])
            finally:
                evaluated.to_mesh_clear()
            difference = np.linalg.norm(actual[sums > 0] - expected[sums > 0], axis=1)
            maximum = float(difference.max())
            # Blender orthonormalizes edit-bone rest matrices; allow at most 0.05 mm.
            if maximum > 5e-5:
                raise AssertionError(f'Source skin differs: scenario={scenario}, mesh={row["name"]}, meters={maximum}')
            if scenario == 'rest':
                rest_positions[path] = actual.copy()
            movement = float(np.linalg.norm(actual - rest_positions[path], axis=1).max())
            if scenario != 'rest' and movement < 1e-4:
                raise AssertionError(f'Test pose did not deform the real mesh: {scenario}/{row["name"]}')
            results.append({'scenario': scenario, 'mesh': row['name'], 'maximum_meters': maximum,
                            'motion_from_rest_meters': movement})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geometry', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    result = {'status': 'passed', 'source': str(args.geometry), 'checks': inspect_deformation(args.geometry)}
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
