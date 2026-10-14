"""Compare portable source geometry against the former FBX bridge in Blender."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from audit_character_fbx import clear_scene_objects, import_character_fbx
from preview_character import reset_static_bind_pose


@dataclass(frozen=True)
class MeshSnapshot:
    vertices: np.ndarray
    uvs: tuple[np.ndarray, ...]
    material_indices: np.ndarray
    normals: np.ndarray
    evaluated_normals: np.ndarray
    skin: dict[str, np.ndarray]


def snapshot(path: Path) -> dict[str, MeshSnapshot]:
    clear_scene_objects()
    objects, _ = import_character_fbx(path)
    meshes = tuple(obj for obj in objects if obj.type == 'MESH')
    reset_static_bind_pose(bpy.context.scene, path, meshes)
    graph = bpy.context.evaluated_depsgraph_get()
    result = {}
    for obj in meshes:
        evaluated = obj.evaluated_get(graph)
        mesh = evaluated.to_mesh()
        try:
            normal_matrix = evaluated.matrix_world.to_3x3().inverted().transposed()
            skin = {}
            for group in obj.vertex_groups:
                weights = np.zeros(len(obj.data.vertices))
                for vertex in obj.data.vertices:
                    for assignment in vertex.groups:
                        if assignment.group == group.index:
                            weights[vertex.index] = assignment.weight
                if weights.any():
                    skin[group.name] = weights
            result[obj.name] = MeshSnapshot(
                np.array([tuple(evaluated.matrix_world @ vertex.co) for vertex in mesh.vertices]),
                tuple(np.array([tuple(value.uv) for value in layer.data]) for layer in mesh.uv_layers),
                np.array([polygon.material_index for polygon in mesh.polygons]),
                np.array([tuple((normal_matrix @ normal.vector).normalized()) for normal in obj.data.corner_normals]),
                np.array([tuple((normal_matrix @ normal.vector).normalized()) for normal in mesh.corner_normals]),
                skin,
            )
        finally:
            evaluated.to_mesh_clear()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy', type=Path, required=True)
    parser.add_argument('--geometry', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    legacy = snapshot(args.legacy)
    portable = snapshot(args.geometry)
    differences = []
    for name in sorted(legacy.keys() & portable.keys()):
        old, new = legacy[name], portable[name]
        if old.vertices.shape != new.vertices.shape:
            differences.append({'mesh': name, 'legacy_shape': old.vertices.shape, 'portable_shape': new.vertices.shape})
        else:
            error = np.linalg.norm(old.vertices - new.vertices, axis=1)
            uv_ok = len(old.uvs) <= len(new.uvs) and all(a.shape == b.shape and np.allclose(a, b, atol=1e-6)
                                                                      for a, b in zip(old.uvs, new.uvs))
            skin_ok = old.skin.keys() == new.skin.keys() and all(np.allclose(old.skin[key], new.skin[key], atol=1e-6)
                                                                    for key in old.skin)
            normals_error = float(np.linalg.norm(old.normals - new.normals, axis=1).max()) if old.normals.shape == new.normals.shape else float('inf')
            evaluated_error = np.linalg.norm(old.evaluated_normals - new.evaluated_normals, axis=1)
            differences.append({'mesh': name, 'vertices': len(error), 'maximum_meters': float(error.max()),
                                'mean_meters': float(error.mean()), 'uv_match': uv_ok, 'skin_match': skin_ok,
                                'material_indices_match': np.array_equal(old.material_indices, new.material_indices),
                                'normal_error': normals_error, 'additional_uv_layers': len(new.uvs) - len(old.uvs),
                                'evaluated_normal_max_error': float(evaluated_error.max()),
                                'evaluated_normal_corners_above_002': int(np.count_nonzero(evaluated_error > .002))})
    report = {'missing': sorted(legacy.keys() - portable.keys()), 'extra': sorted(portable.keys() - legacy.keys()), 'meshes': differences}
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    if report['missing'] or report['extra'] or any(row.get('maximum_meters', float('inf')) > .0005
            or not row['uv_match'] or not row['skin_match'] or not row['material_indices_match']
            or row['normal_error'] > .002 for row in differences):
        raise AssertionError(f'Portable geometry differs from the legacy rest-pose baseline: {args.output}')
    print(json.dumps({'status': 'passed', 'meshes': len(differences), 'maximum_meters': max(row['maximum_meters'] for row in differences)}))


if __name__ == '__main__':
    main()
