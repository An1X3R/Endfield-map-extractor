"""Check every visible source mesh against actual source-root editing transforms."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
import numpy as np
from numpy.typing import NDArray
from mathutils import Matrix


def world_vertices(obj: bpy.types.Object) -> NDArray[np.float64]:
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    values = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get('co', values)
    matrix = np.asarray(evaluated.matrix_world, dtype=np.float64)
    result = values.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
    evaluated.to_mesh_clear()
    return result


def inspect_file(path: Path) -> dict[str, object]:
    bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False)
    rigs = tuple(obj for obj in bpy.context.scene.objects if obj.type == 'ARMATURE')
    if len(rigs) != 1:
        raise ValueError(f'Expected one real character armature: file={path}; count={len(rigs)}')
    rig = rigs[0]
    roots = tuple(bone for bone in rig.pose.bones if bone.parent is None and bone.bone.use_deform)
    if len(roots) != 1:
        raise ValueError(f'Expected one deforming source root: file={path}; roots={[b.name for b in roots]}')
    root = roots[0]
    meshes = tuple(obj for obj in bpy.context.scene.objects if obj.type == 'MESH'
                   and obj.get('source_renderer_identity') and not obj.hide_render and not obj.hide_get())
    if not meshes:
        raise ValueError(f'No visible source geometry was inspected: file={path}')
    action = rig.animation_data.action if rig.animation_data else None
    frame_range = action.frame_range if action is not None else (bpy.context.scene.frame_current,) * 2
    frames = sorted({int(frame_range[0]), int((frame_range[0] + frame_range[1]) / 2), int(frame_range[1]), bpy.context.scene.frame_current})
    cases = (('translation', Matrix.Translation((.31, -.17, .23))),
             ('rotation', Matrix.Rotation(.47, 4, 'Z')),
             ('uniform_scale', Matrix.Diagonal((1.17, 1.17, 1.17, 1.))))
    results: list[dict[str, object]] = []
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        before = {obj.name: world_vertices(obj) for obj in meshes}
        basis = root.matrix_basis.copy()
        initial = rig.matrix_world @ root.matrix
        for label, transform in cases:
            root.matrix_basis = transform @ basis
            rig.update_tag()
            bpy.context.view_layer.update()
            delta = np.asarray(rig.matrix_world @ root.matrix @ initial.inverted())
            errors: dict[str, float] = {}
            for obj in meshes:
                expected = before[obj.name] @ delta[:3, :3].T + delta[:3, 3]
                error = float(np.max(np.linalg.norm(world_vertices(obj) - expected, axis=1)))
                errors[obj.name] = error
            maximum = max(errors.values())
            results.append({'frame': frame, 'transform': label, 'maximum_world_vertex_error': maximum})
            if maximum > 0.0001:
                raise AssertionError(f'Source mesh did not follow root: file={path}; frame={frame}; transform={label}; errors={errors}')
            root.matrix_basis = basis
            rig.update_tag()
            bpy.context.view_layer.update()
    return {'file': str(path), 'source_meshes': len(meshes), 'root': root.name,
            'frames': frames, 'checks': results, 'maximum_world_vertex_error': max(float(row['maximum_world_vertex_error']) for row in results)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blends', nargs='+', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    results = [inspect_file(path) for path in args.blends]
    args.output.write_text(json.dumps({'status': 'passed', 'samples': results}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REAL_CHARACTER_ROOT_MOTION_PASSED', len(results), flush=True)


if __name__ == '__main__':
    main()
