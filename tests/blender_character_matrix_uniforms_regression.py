"""Compare animated matrix drivers with receiver-owned native shader uniforms."""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
import math
from pathlib import Path
import sys
from typing import TypedDict

import bpy

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))

from blender_character_shader_uniforms_regression import (
    ImageResult, _configure_engine, _ensure_world, _max_difference, _render,
)
from character_shader_uniforms import (
    INPUT_MARKER, migrate_shader_uniforms, verify_shader_uniforms,
)


ROLE = 'matrix_regression_role'
COLUMN = 'matrix_regression_column'
FRAMES = (1, 10)
PIXEL_TOLERANCE = 2e-6
SCALAR_TOLERANCE = 2e-6


class Capture(TypedDict):
    image: ImageResult
    scalars: tuple[float, ...]


def _object(collection: bpy.types.Collection, name: str, data: bpy.types.ID | None,
            role: str) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    obj[ROLE] = role
    return obj


def _matrix_driver(socket: bpy.types.NodeSocket, anchor: bpy.types.Object,
                   column: int, row: int, name: str) -> None:
    driver = socket.driver_add('default_value').driver
    driver.type = 'SCRIPTED'
    driver.expression = name
    variable = driver.variables.new()
    variable.name = name
    variable.type = 'SINGLE_PROP'
    variable.targets[0].id = anchor
    variable.targets[0].data_path = f'matrix_world[{column}][{row}]'


def _material(anchor: bpy.types.Object, column: int) -> bpy.types.Material:
    material = bpy.data.materials.new(f'Matrix material {column}')
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    emission = tree.nodes.new('ShaderNodeEmission')
    matrix = tree.nodes.new('ShaderNodeCombineXYZ')
    matrix.name = 'Matrix Column'
    scale = tree.nodes.new('ShaderNodeVectorMath')
    scale.operation = 'SCALE'
    scale.inputs[3].default_value = 0.125
    offset = tree.nodes.new('ShaderNodeVectorMath')
    offset.operation = 'ADD'
    offset.inputs[1].default_value = (0.5, 0.5, 0.5)
    tree.links.new(matrix.outputs[0], scale.inputs[0])
    tree.links.new(scale.outputs[0], offset.inputs[0])
    tree.links.new(offset.outputs[0], emission.inputs['Color'])
    tree.links.new(emission.outputs[0], output.inputs['Surface'])
    for row in range(3):
        name = 'origin_component' if column == 3 else 'basis_component'
        _matrix_driver(matrix.inputs[row], anchor, column, row, name)
    if column == 0:
        hair = tree.nodes.new('ShaderNodeCombineXYZ')
        hair.name = 'Preserved Hair Frame'
        _matrix_driver(hair.inputs[0], anchor, 0, 0, 'component')
    return material


def _fixture() -> tuple[bpy.types.Scene, bpy.types.Collection]:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    _ensure_world(scene)
    scene.render.resolution_x = 32
    scene.render.resolution_y = 16
    scene.render.resolution_percentage = 100
    collection = bpy.data.collections.new('Matrix regression bundle')
    scene.collection.children.link(collection)
    camera_data = bpy.data.cameras.new('Matrix camera')
    camera_data.type = 'ORTHO'
    camera_data.ortho_scale = 4.0
    camera = _object(collection, 'Matrix camera', camera_data, 'camera')
    camera.location = (0.0, 0.0, 4.0)
    scene.camera = camera
    rig = _object(collection, 'Matrix rig', bpy.data.armatures.new('Matrix rig'), 'rig')
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bone = rig.data.edit_bones.new('Head')
    bone.head = (0.0, 0.0, 0.0)
    bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode='OBJECT')
    pose = rig.pose.bones['Head']
    pose.rotation_mode = 'XYZ'
    states = ((1, (0.23, 0.41, 0.67), (0.2, 0.3, 0.4)),
              (10, (0.57, -0.37, 1.11), (0.7, 0.8, 0.9)))
    for frame, rotation, location in states:
        pose.rotation_euler = rotation
        pose.location = location
        pose.keyframe_insert('rotation_euler', frame=frame)
        pose.keyframe_insert('location', frame=frame)
    anchor = _object(collection, 'Matrix bone anchor', None, 'anchor')
    anchor.parent = rig
    anchor.parent_type = 'BONE'
    anchor.parent_bone = 'Head'
    anchor.location = (0.1, 0.2, 0.3)
    for column in range(4):
        x = float(column - 2)
        mesh = bpy.data.meshes.new(f'Matrix quad {column}')
        mesh.from_pydata(((x, -1.0, 0.0), (x + 1.0, -1.0, 0.0),
                          (x + 1.0, 1.0, 0.0), (x, 1.0, 0.0)), (), ((0, 1, 2, 3),))
        mesh.update()
        receiver = _object(collection, f'Matrix receiver {column}', mesh, 'receiver')
        receiver[COLUMN] = column
        modifier = receiver.modifiers.new('Real skin deformation', 'ARMATURE')
        modifier.object = rig
        group = receiver.vertex_groups.new(name='Head')
        group.add(list(range(4)), 0.05, 'REPLACE')
        mesh.materials.append(_material(anchor, column))
    return scene, collection


def _receivers(objects: Sequence[bpy.types.Object]) -> tuple[bpy.types.Object, ...]:
    return tuple(sorted((obj for obj in objects if obj.get(ROLE) == 'receiver'),
                        key=lambda obj: int(obj[COLUMN])))


def _anchor(objects: Sequence[bpy.types.Object]) -> bpy.types.Object:
    values = tuple(obj for obj in objects if obj.get(ROLE) == 'anchor')
    if len(values) != 1:
        raise AssertionError(f'Expected one bone anchor, found {len(values)}')
    return values[0]


def _materials(objects: Sequence[bpy.types.Object]) -> tuple[bpy.types.Material, ...]:
    return tuple(obj.material_slots[0].material for obj in _receivers(objects))


def _source_scalars(objects: Sequence[bpy.types.Object]) -> tuple[float, ...]:
    graph = bpy.context.evaluated_depsgraph_get()
    anchor = _anchor(objects).evaluated_get(graph)
    return tuple(float(anchor.path_resolve(f'matrix_world[{int(obj[COLUMN])}][{row}]'))
                 for obj in _receivers(objects) for row in range(3))


def _driver_scalars(objects: Sequence[bpy.types.Object]) -> tuple[float, ...]:
    graph = bpy.context.evaluated_depsgraph_get()
    return tuple(float(obj.material_slots[0].material.node_tree.evaluated_get(graph)
                       .nodes['Matrix Column'].inputs[row].default_value)
                 for obj in _receivers(objects) for row in range(3))


def _native_scalars(objects: Sequence[bpy.types.Object]) -> tuple[float, ...]:
    graph = bpy.context.evaluated_depsgraph_get()
    values: list[float] = []
    for obj in _receivers(objects):
        matrix = obj.material_slots[0].material.node_tree.nodes['Matrix Column']
        evaluated = obj.evaluated_get(graph)
        for socket in matrix.inputs:
            component = socket.links[0].from_node
            if (component.bl_idname != 'ShaderNodeSeparateXYZ'
                    or socket.links[0].from_socket != component.outputs['X']
                    or len(component.inputs[0].links) != 1):
                raise AssertionError(f'Matrix input lost exact X extraction: {obj.name}/{socket.name}')
            node = component.inputs[0].links[0].from_node
            if INPUT_MARKER not in node or node.attribute_type != 'OBJECT':
                raise AssertionError(f'Matrix input lost its native receipt: {obj.name}/{socket.name}')
            if component.inputs[0].links[0].from_socket != node.outputs['Vector']:
                raise AssertionError(f'Matrix input uses an averaged scalar: {obj.name}/{socket.name}')
            values.append(float(evaluated[node.attribute_name]))
    return tuple(values)


def _scalar_delta(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != 12 or len(right) != 12 or not all(math.isfinite(value) for value in left + right):
        raise AssertionError('Matrix regression requires twelve finite components')
    return max(abs(first - second) for first, second in zip(left, right))


def _capture(scene: bpy.types.Scene, objects: Sequence[bpy.types.Object], output: Path,
             phase: str, scalars: Callable[[Sequence[bpy.types.Object]], tuple[float, ...]]) -> dict[int, Capture]:
    result: dict[int, Capture] = {}
    for frame in FRAMES:
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        values = scalars(objects)
        delta = _scalar_delta(values, _source_scalars(objects))
        if delta > SCALAR_TOLERANCE:
            raise AssertionError(f'Matrix scalar differs from evaluated bone anchor: phase={phase}, frame={frame}, delta={delta}')
        result[frame] = {'image': _render(scene, output / f'{phase}_{frame}.exr'), 'scalars': values}
    return result


def _parity(left: dict[int, Capture], right: dict[int, Capture]) -> dict[str, float]:
    result: dict[str, float] = {}
    for frame in FRAMES:
        pixel_delta = _max_difference(left[frame]['image'], right[frame]['image'])
        scalar_delta = _scalar_delta(left[frame]['scalars'], right[frame]['scalars'])
        if pixel_delta > PIXEL_TOLERANCE or scalar_delta > SCALAR_TOLERANCE:
            raise AssertionError(f'Animated matrix parity failed: frame={frame}, pixels={pixel_delta}, scalars={scalar_delta}')
        result[f'frame_{frame}_pixels'] = pixel_delta
        result[f'frame_{frame}_scalars'] = scalar_delta
    return result


def main(output: Path, engine: str) -> None:
    output = output.resolve() / engine.lower()
    output.mkdir(parents=True, exist_ok=True)
    scene, collection = _fixture()
    _configure_engine(scene, engine)
    objects = tuple(collection.objects)
    baseline = _capture(scene, objects, output, 'driver', _driver_scalars)
    if _scalar_delta(baseline[1]['scalars'], baseline[10]['scalars']) < 0.01:
        raise AssertionError('The animated bone anchor did not move enough to test stale values')
    migration = migrate_shader_uniforms(_materials(objects), objects, scene)
    if migration['migrated'] != 12 or migration['bindings'] != 12:
        raise AssertionError(f'Unexpected matrix migration count: {migration}')
    preserved_tree = _materials(objects)[0].node_tree
    preserved = tuple(preserved_tree.animation_data.drivers)
    if (len(preserved) != 1 or preserved[0].driver.expression != 'component'
            or preserved[0].data_path != preserved_tree.nodes['Preserved Hair Frame'].inputs[0]
            .path_from_id('default_value') or preserved[0].driver.variables[0].targets[0].id != _anchor(objects)):
        raise AssertionError('Migration changed the explicit hair-frame driver contract')
    _anchor(objects).name = 'Renamed animated bone anchor'
    verify_shader_uniforms(_materials(objects), objects, scene)
    if migrate_shader_uniforms(_materials(objects), objects, scene)['migrated'] != 0:
        raise AssertionError('Matrix migration is not idempotent')
    native = _capture(scene, objects, output, 'native', _native_scalars)
    parity = _parity(baseline, native)
    native_path = output / 'matrix_uniform_native.blend'
    library_path = output / 'matrix_uniform_library.blend'
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(native_path), check_existing=False)
    bpy.data.libraries.write(str(library_path), {collection}, fake_user=True)
    bpy.ops.wm.open_mainfile(filepath=str(native_path), load_ui=False)
    scene = bpy.context.scene
    collection = bpy.data.collections['Matrix regression bundle']
    objects = tuple(collection.objects)
    verify_shader_uniforms(_materials(objects), objects, scene)
    reopened = _capture(scene, objects, output, 'reopened', _native_scalars)
    reopen_parity = _parity(baseline, reopened)
    source = _receivers(objects)[0]
    shared = bpy.data.objects.new('Additional shared-material receiver', source.data.copy())
    collection.objects.link(shared)
    shared.hide_render = True
    shared[ROLE] = 'shared_receiver'
    rebound = migrate_shader_uniforms(_materials(objects), tuple(scene.objects), scene)
    if rebound['migrated'] != 0 or rebound['bindings'] != 3:
        raise AssertionError(f'Shared material receiver was not bound exactly once: {rebound}')
    verify_shader_uniforms(_materials(objects), tuple(scene.objects), scene)
    with bpy.data.libraries.load(str(library_path), link=False) as (data_from, data_to):
        data_to.collections = list(data_from.collections)
    appended = data_to.collections[0]
    scene.collection.children.link(appended)
    appended_objects = tuple(appended.objects)
    appended_anchor = _anchor(appended_objects)
    if appended_anchor == _anchor(objects):
        raise AssertionError('Append reused the original anchor')
    for obj in _receivers(appended_objects):
        for curve in obj.animation_data.drivers:
            if curve.data_path.startswith('["ef_matrix_') and curve.driver.variables[0].targets[0].id != appended_anchor:
                raise AssertionError(f'Append lost the receiver driver anchor remap: {obj.name}/{curve.data_path}')
    verify_shader_uniforms(_materials(appended_objects), appended_objects, scene)
    for frame in FRAMES:
        scene.frame_set(frame)
        if _scalar_delta(_native_scalars(appended_objects), _source_scalars(appended_objects)) > SCALAR_TOLERANCE:
            raise AssertionError(f'Appended receiver has stale animated scalars: frame={frame}')
    report = {'status': 'passed', 'engine': engine, 'animated_frames': list(FRAMES),
              'matrix_inputs': migration['migrated'], 'legacy_vs_native': parity,
              'save_reopen': reopen_parity, 'idempotence': True, 'anchor_rename': True,
              'shared_receiver_bindings': rebound['bindings'], 'append_anchor_remap': True,
              'fixture': 'Real animated armature, skinned quads and bone-parented matrix anchor'}
    (output / 'matrix_uniform_render_regression.json').write_text(
        json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    print('MATRIX_UNIFORM_RENDER_REGRESSION_PASSED', json.dumps(report, allow_nan=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--engine', choices=('CYCLES', 'BLENDER_EEVEE_NEXT'), required=True)
    arguments = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(arguments.output, arguments.engine)
