"""Bake verified facial bone targets into mesh-local, rig-compatible shape keys.

This Blender adapter requires the audited bind initialization. It never bakes
body animation, material expressions, modifiers other than skin, or control
mixing semantics into the target. Existing shape keys and skin weights remain.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
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


@dataclass(frozen=True)
class ExpressionDriverControl:
    label: str
    keys: tuple[bpy.types.ShapeKey, ...]


@dataclass(frozen=True)
class ExpressionDriverCheck:
    label: str
    requested: float
    driven_key_count: int
    maximum_driver_error: float


@contextmanager
def isolated_pose_evaluation(armature: bpy.types.Object) -> Iterator[None]:
    """Evaluate static source bones without unrelated scene dependencies."""
    scene = bpy.data.scenes.new('Source expression pose validation')
    objects = {armature}
    parent = armature.parent
    while parent is not None:
        objects.add(parent)
        parent = parent.parent
    try:
        for obj in objects:
            scene.collection.objects.link(obj)
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
            yield
    finally:
        bpy.data.scenes.remove(scene)


def _evaluated_vertices(obj: bpy.types.Object, graph: bpy.types.Depsgraph) -> tuple[Vector, ...]:
    """Read one mesh from an already evaluated dependency graph."""
    evaluated = obj.evaluated_get(graph)
    mesh = evaluated.to_mesh()
    try:
        if len(mesh.vertices) != len(obj.data.vertices):
            raise ValueError(f'Expression evaluation changed vertex topology: {obj.name}')
        return tuple(vertex.co.copy() for vertex in mesh.vertices)
    finally:
        evaluated.to_mesh_clear()


def evaluated_local_vertices(obj: bpy.types.Object) -> tuple[Vector, ...]:
    """Read skin evaluation in mesh-local space without retaining temporary data."""
    bpy.context.view_layer.update()
    return _evaluated_vertices(obj, bpy.context.evaluated_depsgraph_get())


def evaluated_skin_vertices(obj: bpy.types.Object) -> tuple[Vector, ...]:
    """Exclude downstream shading geometry without baking it into expressions."""
    return evaluated_skin_batch((obj,))[obj.name]


def evaluated_skin_batch(meshes: tuple[bpy.types.Object, ...]) -> dict[str, tuple[Vector, ...]]:
    """Read one skin state with a single update for every selected mesh."""
    if not meshes:
        return {}
    downstream = tuple(mod for obj in meshes for mod in obj.modifiers
                       if mod.type != 'ARMATURE' and mod.show_viewport)
    try:
        for modifier in downstream:
            modifier.show_viewport = False
        bpy.context.view_layer.update()
        graph = bpy.context.evaluated_depsgraph_get()
        return {obj.name: _evaluated_vertices(obj, graph) for obj in meshes}
    finally:
        for modifier in downstream:
            modifier.show_viewport = True
        if downstream:
            bpy.context.view_layer.update()


def validate_expression_drivers(controller: bpy.types.Object,
                                controls: tuple[ExpressionDriverControl, ...],
                                tolerance: float) -> tuple[ExpressionDriverCheck, ...]:
    """Verify exact driver bindings and batch their evaluated control states."""
    if not controls:
        return ()
    original = {control.label: float(controller[control.label]) for control in controls}
    key_owners: dict[int, bpy.types.Key] = {}
    for control in controls:
        for key in control.keys:
            keys = key.id_data
            key_owners[keys.as_pointer()] = keys
            path = key.path_from_id('value')
            curves = tuple(curve for curve in keys.animation_data.drivers if curve.data_path == path) if keys.animation_data else ()
            if len(curves) != 1:
                raise AssertionError(f'Expected one expression shape-key driver: {keys.name}/{key.name}, count={len(curves)}')
            driver = curves[0].driver
            if driver.type != 'SCRIPTED' or driver.expression != 'v' or len(driver.variables) != 1:
                raise AssertionError(f'Unexpected expression shape-key driver: {keys.name}/{key.name}')
            variable = driver.variables[0]
            target = variable.targets[0]
            if (variable.name != 'v' or variable.type != 'SINGLE_PROP'
                    or target.id != controller or target.data_path != f'["{control.label}"]'):
                raise AssertionError(f'Expression shape-key driver does not resolve to its exact control: {keys.name}/{key.name}/{control.label}')
    checks: dict[str, list[ExpressionDriverCheck]] = {control.label: [] for control in controls}
    try:
        states = tuple(tuple(value for _control in controls) for value in (0.0, 0.5, 1.0))
        distinct = tuple((index + 1) / (len(controls) + 1) for index in range(len(controls)))
        for values in (*states, distinct):
            for control, value in zip(controls, values, strict=True):
                controller[control.label] = value
            controller.update_tag()
            for keys in key_owners.values():
                keys.update_tag()
            bpy.context.view_layer.update()
            for control, value in zip(controls, values, strict=True):
                errors = tuple(abs(float(key.value) - value) for key in control.keys)
                maximum = max(errors, default=0.0)
                if maximum > tolerance:
                    raise AssertionError(f'Expression shape-key driver did not follow its controller: controller={controller.name}, control={control.label}, requested={value}, maximum_error={maximum}, tolerance={tolerance}')
                if values is not distinct:
                    checks[control.label].append(ExpressionDriverCheck(control.label, value, len(errors), maximum))
    finally:
        for label, value in original.items():
            controller[label] = value
        controller.update_tag()
        for keys in key_owners.values():
            keys.update_tag()
        bpy.context.view_layer.update()
    return tuple(check for control in controls for check in checks[control.label])


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
    neutral = evaluated_skin_batch(meshes)
    try:
        for target in targets:
            armature.pose.bones[target.bone_name].matrix_basis = target.matrix_basis.copy()
        posed = evaluated_skin_batch(meshes)
    finally:
        for target in targets:
            armature.pose.bones[target.bone_name].matrix_basis = identity
        # The endpoint read below evaluates the neutral rig and new key together.
    result: list[BakedExpression] = []
    created: list[tuple[bpy.types.Object, bpy.types.ShapeKey]] = []
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
        created.append((obj, key))
        result.append(BakedExpression(obj.name, key.name, sum(delta.length > tolerance for delta in deltas), maximum))
    try:
        for _obj, key in created:
            key.value = 1.
        baked = evaluated_skin_batch(tuple(obj for obj, _key in created))
        for obj, _key in created:
            error = max((actual - expected).length for actual, expected in zip(baked[obj.name], posed[obj.name], strict=True))
            if error > tolerance:
                raise ValueError(f'Expression shape endpoint differs from source bone evaluation: {obj.name}/{control_name}, error={error}, tolerance={tolerance}')
    finally:
        for _obj, key in created:
            key.value = 0.
        bpy.context.view_layer.update()
    return tuple(result)
