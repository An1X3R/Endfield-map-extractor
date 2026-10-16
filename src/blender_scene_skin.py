"""Bake source scene skin poses into ordinary meshes without adding runtime rigs."""
from __future__ import annotations

from pathlib import Path
import struct

import bpy
from mathutils import Matrix, Vector

from blender_build_map02_chunk_20260727 import unity_matrix
from blender_scene_effect_meshes import _source_vertices
from blender_scene_mesh_uvs import _source_vertex_indices
from endfield_scene_skin_contract import SourceSceneSkin, load_scene_skin


def apply_scene_skin(mesh: bpy.types.Mesh, obj_path: Path, descriptor: SourceSceneSkin) -> None:
    """Map split OBJ vertices by indexed corners before applying native linear skinning."""
    payload = load_scene_skin(Path(descriptor["file"]))
    if payload.sha256 != descriptor["sha256"] or payload.vertex_count != descriptor["vertex_count"]:
        raise ValueError(f"Scene skin stream changed since planning: file={descriptor['file']}")
    source = _source_vertices(obj_path)
    if len(source) != payload.vertex_count:
        raise ValueError(f"Scene skin vertex count differs from exact source OBJ: path={obj_path}")
    mapping = _source_vertex_indices(mesh, source, obj_path)
    matrices = tuple(unity_matrix(struct.pack("<16f", *values)) for values in descriptor["bone_matrices"])
    if len(matrices) != len(payload.bind_poses):
        raise ValueError(f"Scene skin pose bone count differs: file={descriptor['file']}")
    positions: list[Vector] = []
    normal_matrices: list[Matrix] = []
    normals = tuple(normal.vector.copy() for normal in mesh.corner_normals)
    for vertex, index in zip(mesh.vertices, mapping, strict=True):
        start, end = payload.offsets[index:index + 2]
        transform = Matrix(((0.,) * 4,) * 4)
        for slot in range(start, end):
            transform += matrices[payload.indices[slot]] * payload.weights[slot]
        positions.append((transform @ vertex.co.to_4d()).to_3d())
        linear = transform.to_3x3()
        if abs(linear.determinant()) < 1e-12:
            raise ValueError(f"Scene skin blended normal transform is singular: mesh={mesh.name}, source_vertex={index}")
        normal_matrices.append(linear.inverted().transposed())
    for vertex, position in zip(mesh.vertices, positions, strict=True):
        vertex.co = position
    mesh.update()
    mesh.normals_split_custom_set(tuple((normal_matrices[loop.vertex_index] @ normal).normalized()
                                       for loop, normal in zip(mesh.loops, normals, strict=True)))
    mesh["endfield_source_skin_status"] = descriptor["status"]
    mesh["endfield_source_skin_sha256"] = payload.sha256
