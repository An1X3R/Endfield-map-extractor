"""Restore source single-cluster skins lost when Blender merges nested FBX armatures.

Blender 4.4 collects a nested armature separately even when that node later becomes
an ordinary bone in the outer armature. Its mesh clusters receive vertex groups,
but no Armature modifier. Recovery uses the importer's decoded bind matrices and
exact cluster-to-bone identity; it never guesses bones from names or scene roles.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import TypedDict

import bpy
from mathutils import Matrix
from io_scene_fbx import import_fbx, parse_fbx


class NestedSkinRecovery(TypedDict):
    mesh: str
    armature: str
    bone: str
    fbx_cluster_id: int
    source_weighted_vertices: int
    previous_parent_type: str
    previous_parent_bone: str
    bind_world_matrix: list[list[float]]


def recover_nested_single_cluster(node: import_fbx.FbxImportHelperNode, mesh: import_fbx.FbxImportHelperNode, global_matrix: Matrix) -> NestedSkinRecovery:
    """Bind one explicitly merged nested skin to its existing Blender armature."""
    obj = mesh.bl_obj
    armature = node.bl_obj
    if obj is None or obj.type != "MESH" or armature is None or armature.type != "ARMATURE":
        raise RuntimeError(f"Merged nested FBX skin lacks mesh/armature objects: mesh={mesh.fbx_name}")
    pending = [node]
    weighted_clusters: list[tuple[import_fbx.FbxImportHelperNode, parse_fbx.FBXElem, tuple[int, ...], tuple[float, ...]]] = []
    while pending:
        bone = pending.pop()
        pending.extend(bone.children)
        for cluster, linked_meshes in bone.clusters:
            if mesh not in linked_meshes:
                continue
            indices = [tuple(element.props[0]) for element in cluster.elems if element.id == b"Indexes"]
            weights = [tuple(element.props[0]) for element in cluster.elems if element.id == b"Weights"]
            if indices and indices[0]:
                if len(indices) != 1 or len(weights) != 1:
                    raise RuntimeError(f"FBX cluster has invalid index/weight arrays: mesh={obj.name}")
                weighted_clusters.append((bone, cluster, indices[0], weights[0]))
    if len(weighted_clusters) != 1:
        raise RuntimeError(f"Nested skin recovery requires exactly one weighted FBX cluster: mesh={obj.name}, count={len(weighted_clusters)}")
    bone, cluster, indices, weights = weighted_clusters[0]
    vertex_count = len(obj.data.vertices)
    groups = {obj.vertex_groups[assignment.group].name for vertex in obj.data.vertices for assignment in vertex.groups if assignment.weight > 0}
    valid_source = (len(indices) == vertex_count and len(weights) == vertex_count and set(indices) == set(range(vertex_count)) and all(math.isfinite(weight) and abs(weight - 1.0) <= 1e-6 for weight in weights))
    valid_import = (bone.bl_obj == armature and bone.bl_bone in armature.data.bones and groups == {bone.bl_bone} and all(sum(assignment.weight > 0 for assignment in vertex.groups) == 1 and all(math.isfinite(assignment.weight) and assignment.weight >= 0 for assignment in vertex.groups) and abs(sum(assignment.weight for assignment in vertex.groups) - 1.0) <= 1e-4 for vertex in obj.data.vertices))
    if not valid_source or not valid_import:
        raise RuntimeError(f"Source FBX cluster and imported single-bone weights disagree: mesh={obj.name}, bone={bone.bl_bone}, groups={sorted(groups)}")
    if node not in mesh.armature_setup:
        raise RuntimeError(f"Source FBX nested bind matrix is unavailable: mesh={obj.name}")
    mesh_matrix, _ = mesh.armature_setup[node]
    world = global_matrix @ mesh_matrix
    if mesh.matrix_geom:
        world = world @ mesh.matrix_geom
    if any(not math.isfinite(value) for row in world for value in row):
        raise RuntimeError(f"Source FBX nested bind matrix is non-finite: mesh={obj.name}")
    record: NestedSkinRecovery = {"mesh": obj.name, "armature": armature.name, "bone": bone.bl_bone, "fbx_cluster_id": int(cluster.props[0]), "source_weighted_vertices": vertex_count, "previous_parent_type": obj.parent_type, "previous_parent_bone": obj.parent_bone, "bind_world_matrix": [list(row) for row in world]}
    obj.parent = armature
    obj.parent_type = "OBJECT"
    obj.parent_bone = ""
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.matrix_world = world
    modifier = obj.modifiers.new("Source FBX nested single-cluster skin", "ARMATURE")
    modifier.object = armature
    return record


def import_with_nested_single_cluster_skins(fbx_path: Path) -> tuple[set[str], tuple[NestedSkinRecovery, ...]]:
    """Import once and recover only verified skins omitted by merged nested roots."""
    helper = import_fbx.FbxImportHelperNode
    original_collect = helper.collect_armature_meshes
    original_link = helper.link_hierarchy
    nested_nodes: dict[int, import_fbx.FbxImportHelperNode] = {}
    global_matrices: list[Matrix] = []

    def collect(node: import_fbx.FbxImportHelperNode) -> None:
        original_collect(node)
        if node.is_armature and node.is_bone:
            nested_nodes[id(node)] = node

    def link(node: import_fbx.FbxImportHelperNode, fbx_template: parse_fbx.FBXElem | None, settings: import_fbx.FBXImportSettings, scene: bpy.types.Scene) -> bpy.types.Object | None:
        if node.is_armature:
            global_matrices.append(settings.global_matrix.copy())
        return original_link(node, fbx_template, settings, scene)

    helper.collect_armature_meshes = collect
    helper.link_hierarchy = link
    try:
        result = bpy.ops.import_scene.fbx(filepath=str(fbx_path), use_anim=False)
    finally:
        helper.collect_armature_meshes = original_collect
        helper.link_hierarchy = original_link
    repairs: list[NestedSkinRecovery] = []
    for node in nested_nodes.values():
        if node.meshes is None:
            raise RuntimeError(f"Merged nested FBX root lacks collected source meshes: root={node.fbx_name}")
        for mesh in sorted(node.meshes, key=lambda item: item.fbx_name):
            if mesh.bl_obj is None:
                raise RuntimeError(f"Collected nested FBX mesh lacks imported object: mesh={mesh.fbx_name}")
            if any(modifier.type == "ARMATURE" for modifier in mesh.bl_obj.modifiers):
                continue
            if not global_matrices or any(matrix != global_matrices[0] for matrix in global_matrices):
                raise RuntimeError("FBX import conversion matrix is unavailable or inconsistent")
            repairs.append(recover_nested_single_cluster(node, mesh, global_matrices[0]))
    bpy.context.view_layer.update()
    return result, tuple(repairs)
