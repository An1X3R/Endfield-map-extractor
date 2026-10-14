"""Bake verified facial bone targets into mesh-local, rig-compatible shape keys.

This Blender adapter requires the audited bind initialization. It never bakes
body animation, material expressions, modifiers other than skin, or control
mixing semantics into the target. Existing shape keys and skin weights remain.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import bpy
from mathutils import Matrix, Vector


@dataclass(frozen=True)
class FacialBoneTarget:
    bone_name: str
    matrix_basis: Matrix


@dataclass(frozen=True)
class BakedExpression:
    object_name: str
    shape_key_name: str
    moved_vertices: int
    maximum_delta: float


def evaluated_local_vertices(obj: bpy.types.Object) -> tuple[Vector, ...]:
    """Read skin evaluation in mesh-local space without retaining temporary data."""
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        if len(mesh.vertices) != len(obj.data.vertices):
            raise ValueError(f'Expression evaluation changed vertex topology: {obj.name}')
        return tuple(vertex.co.copy() for vertex in mesh.vertices)
    finally:
        evaluated.to_mesh_clear()


def evaluated_skin_vertices(obj: bpy.types.Object) -> tuple[Vector, ...]:
    """Exclude downstream shading geometry without baking it into expressions."""
    downstream = tuple(mod for mod in obj.modifiers if mod.type != 'ARMATURE' and mod.show_viewport)
    try:
        for modifier in downstream:
            modifier.show_viewport = False
        return evaluated_local_vertices(obj)
    finally:
        for modifier in downstream:
            modifier.show_viewport = True
        bpy.context.view_layer.update()


def bake_facial_target(
    armature: bpy.types.Object,
    meshes: tuple[bpy.types.Object, ...],
    targets: tuple[FacialBoneTarget, ...],
    control_name: str,
    tolerance: float,
) -> tuple[BakedExpression, ...]:
    """Bake one source endpoint; leave all bones and shape-key values neutral."""
    if armature.type != 'ARMATURE' or armature.data.pose_position != 'POSE':
        raise ValueError('Expression baking requires an enabled source armature')
    if not meshes or not targets or not control_name.strip() or not math.isfinite(tolerance) or tolerance <= 0.:
        raise ValueError('Expression baking requires meshes, targets, a name, and positive finite tolerance')
    if armature.animation_data or armature.constraints or any(bone.constraints for bone in armature.pose.bones):
        raise ValueError('Expression baking requires a static rig without animation or constraints')
    identity = Matrix.Identity(4)
    if any(max(abs(bone.matrix_basis[i][j] - identity[i][j]) for i in range(4) for j in range(4)) > tolerance
           for bone in armature.pose.bones):
        raise ValueError('Expression baking requires the audited identity bind pose')
    if len({target.bone_name for target in targets}) != len(targets):
        raise ValueError('Expression bone targets contain duplicate names')
    for target in targets:
        if target.bone_name not in armature.pose.bones or any(not math.isfinite(v) for row in target.matrix_basis for v in row):
            raise ValueError(f'Expression bone target is missing or nonfinite: {target.bone_name}')
    for obj in meshes:
        if obj.type != 'MESH' or obj.data.users != 1:
            raise ValueError(f'Expression baking requires uniquely owned source mesh data: {obj.name}')
        active = tuple(mod for mod in obj.modifiers if mod.show_viewport and mod.type == 'ARMATURE')
        if len(active) != 1 or active[0].type != 'ARMATURE' or active[0].object != armature:
            raise ValueError(f'Expression baking requires only the source skin modifier: {obj.name}, modifiers={[(mod.name, mod.type) for mod in active]}')
        keys = obj.data.shape_keys
        if keys and (not keys.use_relative or keys.animation_data or control_name in keys.key_blocks
                     or any(abs(key.value) > tolerance for key in keys.key_blocks)):
            raise ValueError(f'Expression shape keys must be neutral, relative, unanimated, and uniquely named: {obj.name}/{control_name}')
    neutral = {obj.name: evaluated_skin_vertices(obj) for obj in meshes}
    try:
        for target in targets:
            armature.pose.bones[target.bone_name].matrix_basis = target.matrix_basis.copy()
        posed = {obj.name: evaluated_skin_vertices(obj) for obj in meshes}
    finally:
        for target in targets:
            armature.pose.bones[target.bone_name].matrix_basis = identity
        bpy.context.view_layer.update()
    result = []
    for obj in meshes:
        deltas = tuple(new - old for new, old in zip(posed[obj.name], neutral[obj.name], strict=True))
        maximum = max(delta.length for delta in deltas)
        if maximum <= tolerance:
            continue
        if obj.data.shape_keys is None:
            obj.shape_key_add(name='Basis', from_mix=False)
        basis = obj.data.shape_keys.reference_key
        key = obj.shape_key_add(name=control_name, from_mix=False)
        key.relative_key = basis
        for point, original, delta in zip(key.data, basis.data, deltas, strict=True):
            point.co = original.co + delta
        key.value = 1.
        try:
            baked = evaluated_skin_vertices(obj)
            error = max((actual - expected).length for actual, expected in zip(baked, posed[obj.name], strict=True))
            if error > tolerance:
                raise ValueError(f'Expression shape endpoint differs from source bone evaluation: {obj.name}/{control_name}, error={error}, tolerance={tolerance}')
        finally:
            key.value = 0.
        result.append(BakedExpression(obj.name, key.name, sum(delta.length > tolerance for delta in deltas), maximum))
    return tuple(result)
