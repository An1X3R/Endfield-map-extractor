"""Render native shader uniform replacements against their real Blender drivers."""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
from typing import Literal, TypedDict, cast

import bpy


OBJECT_ROLE = 'uniform_regression_role'
MATERIAL_ROLE = 'uniform_regression_material_role'
KEY_PREFIX = 'ef_hgrp_light_'
RECEIVER_ROLE = 'receiver'
LIGHT_ROLE = 'light'
RENAMED_LIGHT_NAME = 'Uniform Regression Key Light Renamed'
ORTHO_ROLE = 'orthographic_camera'
PERSP_ROLE = 'perspective_camera'
HOST_ROLE = 'unrelated_driver_host'
BUNDLE_NAME = 'Native Shader Uniform Regression Bundle'


class DriverVariableSpec(TypedDict):
    name: str
    owner: bpy.types.ID
    id_type: str
    path: str


class UniformEntry(TypedDict):
    tree: str
    node: str
    path: str
    expression: str
    result_node: str
    variables: list[dict[str, str]]


class MigrationReport(TypedDict):
    format: str
    migrated: int
    bindings: int
    skipped: list[dict[str, str]]
    entries: list[UniformEntry]


@dataclass(frozen=True)
class RenderState:
    name: str
    frame: int
    subframe: float
    fps: int
    fps_base: float
    frame_start: int
    camera_role: Literal['orthographic_camera', 'perspective_camera']
    camera_type: Literal['ORTHO', 'PERSP']
    ortho_scale: float
    lens: float
    sensor_width: float
    resolution_x: int
    resolution_y: int
    pixel_aspect_x: float
    pixel_aspect_y: float
    light_hidden: bool
    unrelated_value: float


ImageResult = tuple[int, int, tuple[float, ...]]


def _state_cases() -> tuple[RenderState, ...]:
    return (
        RenderState('initial', 1, 0.25, 24, 1.0, 1, 'orthographic_camera', 'ORTHO', 2.0, 50.0, 36.0,
                    32, 32, 1.0, 1.0, False, 0.2),
        RenderState('clock_fps_subframe', 17, 0.5, 30, 1.001, 4, 'orthographic_camera', 'ORTHO', 2.0,
                    50.0, 36.0, 32, 32, 1.0, 1.0, False, 0.45),
        RenderState('orthographic_projection', 4, 0.75, 25, 1.0, 1, 'orthographic_camera', 'ORTHO', 4.0,
                    50.0, 36.0, 48, 32, 1.25, 1.0, False, 0.2),
        RenderState('perspective_projection', 8, 0.5, 60, 1.0, 1, 'perspective_camera', 'PERSP', 2.0,
                    75.0, 30.0, 32, 24, 1.0, 1.0, False, 0.2),
        RenderState('light_hidden', 3, 0.875, 24, 1.0, 1, 'orthographic_camera', 'ORTHO', 2.0, 50.0,
                    36.0, 32, 32, 1.0, 1.0, True, 0.2),
    )


def _ensure_world(scene: bpy.types.Scene) -> None:
    if scene.world is None:
        scene.world = bpy.data.worlds.new('Uniform Regression World')
    scene.world.use_nodes = True
    background = scene.world.node_tree.nodes.get('Background')
    if background is None:
        raise AssertionError('World node tree has no Background node')
    background.inputs['Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    background.inputs['Strength'].default_value = 0.0
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0


def _make_object(scene: bpy.types.Scene, name: str, data: bpy.types.ID | None,
                 role: str) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    obj[OBJECT_ROLE] = role
    return obj


def _driver(node: bpy.types.Node, expression: str,
            variables: tuple[DriverVariableSpec, ...]) -> None:
    curve = node.outputs[0].driver_add('default_value')
    driver = curve.driver
    driver.type = 'SCRIPTED'
    driver.expression = expression
    for specification in variables:
        variable = driver.variables.new()
        variable.name = specification['name']
        variable.type = 'SINGLE_PROP'
        target = variable.targets[0]
        target.id_type = specification['id_type']
        target.id = specification['owner']
        target.data_path = specification['path']


def _scene_variable(name: str, scene: bpy.types.Scene, path: str) -> DriverVariableSpec:
    return {'name': name, 'owner': scene, 'id_type': 'SCENE', 'path': path}


def _value(tree: bpy.types.NodeTree, label: str, scalar: float) -> bpy.types.Node:
    node = tree.nodes.new('ShaderNodeValue')
    node.label = label
    node.outputs[0].default_value = scalar
    return node


def _math(tree: bpy.types.NodeTree, operation: str,
          left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float) -> bpy.types.NodeSocket:
    node = tree.nodes.new('ShaderNodeMath')
    node.operation = operation
    for socket, value in zip(node.inputs, (left, right)):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, socket)
        else:
            socket.default_value = value
    return node.outputs[0]


def _create_fixture() -> tuple[bpy.types.Scene, bpy.types.Material, bpy.types.Object,
                                bpy.types.Object, bpy.types.Object, bpy.types.Object,
                                bpy.types.Object]:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    _ensure_world(scene)
    scene.render.resolution_percentage = 100
    scene.render.resolution_x = 32
    scene.render.resolution_y = 32
    scene.render.pixel_aspect_x = 1.0
    scene.render.pixel_aspect_y = 1.0
    scene.render.film_transparent = False

    mesh = bpy.data.meshes.new('Uniform Regression Quad Mesh')
    mesh.from_pydata(((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)),
                     (), ((0, 1, 2, 3),))
    mesh.update()
    receiver = _make_object(scene, 'Uniform Regression Receiver', mesh, RECEIVER_ROLE)

    ortho_data = bpy.data.cameras.new('Uniform Regression Ortho Camera Data')
    ortho_data.type = 'ORTHO'
    ortho_data.ortho_scale = 2.0
    ortho = _make_object(scene, 'Uniform Regression Ortho Camera', ortho_data, ORTHO_ROLE)
    ortho.location = (0.0, 0.0, 4.0)

    persp_data = bpy.data.cameras.new('Uniform Regression Perspective Camera Data')
    persp_data.type = 'PERSP'
    persp_data.lens = 50.0
    persp_data.sensor_width = 36.0
    perspective = _make_object(scene, 'Uniform Regression Perspective Camera', persp_data, PERSP_ROLE)
    perspective.location = (0.0, 0.0, 4.0)

    light_data = bpy.data.lights.new('Uniform Regression Key Light Data', 'POINT')
    light = _make_object(scene, 'Uniform Regression Key Light', light_data, LIGHT_ROLE)
    light.hide_render = False

    host = _make_object(scene, 'Uniform Regression Unrelated Host', None, HOST_ROLE)
    host['unrelated_scalar'] = 0.2

    material = bpy.data.materials.new('Uniform Regression Material')
    material[MATERIAL_ROLE] = 'original'
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    emission = tree.nodes.new('ShaderNodeEmission')
    combine = tree.nodes.new('ShaderNodeCombineColor')
    combine.mode = 'RGB'
    tree.links.new(combine.outputs['Color'], emission.inputs['Color'])
    tree.links.new(emission.outputs['Emission'], output.inputs['Surface'])

    dry = _value(tree, 'Dry constant', 0.18)
    clock = _value(tree, 'Scene clock', 0.0)
    _driver(clock, 'frame * fps_base / fps', (
        _scene_variable('fps', scene, 'render.fps'),
        _scene_variable('fps_base', scene, 'render.fps_base'),
    ))
    relative_clock = _value(tree, 'Scene clock from start', 0.0)
    _driver(relative_clock, '(frame - frame_start) / fps * fps_base', (
        _scene_variable('frame_start', scene, 'frame_start'),
        _scene_variable('fps', scene, 'render.fps'),
        _scene_variable('fps_base', scene, 'render.fps_base'),
    ))
    ortho_projection = _value(tree, 'Orthographic projection', 0.0)
    projection_variables = (
        _scene_variable('rx', scene, 'render.resolution_x'),
        _scene_variable('ry', scene, 'render.resolution_y'),
        _scene_variable('px', scene, 'render.pixel_aspect_x'),
        _scene_variable('py', scene, 'render.pixel_aspect_y'),
    )
    _driver(ortho_projection, '2*max(rx*px/(ry*py),1)/scale', projection_variables + (
        _scene_variable('scale', scene, 'camera.data.ortho_scale'),
    ))
    perspective_projection = _value(tree, 'Perspective projection', 0.0)
    _driver(perspective_projection, '2*lens*max(rx*px/(ry*py),1)/sensor', projection_variables + (
        _scene_variable('lens', scene, 'camera.data.lens'),
        _scene_variable('sensor', scene, 'camera.data.sensor_width'),
    ))
    visible = _value(tree, 'Light render visibility', 0.0)
    _driver(visible, '1-value', ({'name': 'value', 'owner': light, 'id_type': 'OBJECT',
                                  'path': 'hide_render'},))
    unrelated = _value(tree, 'Unrelated object driver', 0.0)
    _driver(unrelated, 'unrelated', ({'name': 'unrelated', 'owner': host, 'id_type': 'OBJECT',
                                      'path': '["unrelated_scalar"]'},))

    tree.links.new(_math(tree, 'ADD', dry.outputs[0], _math(tree, 'MULTIPLY', unrelated.outputs[0], 0.1)),
                   combine.inputs['Red'])
    tree.links.new(_math(tree, 'MULTIPLY', clock.outputs[0], 0.3), combine.inputs['Green'])
    blue = _math(tree, 'ADD', _math(tree, 'MULTIPLY', ortho_projection.outputs[0], 0.05),
                 _math(tree, 'MULTIPLY', perspective_projection.outputs[0], 0.02))
    blue = _math(tree, 'ADD', blue, _math(tree, 'MULTIPLY', visible.outputs[0], 0.1))
    blue = _math(tree, 'ADD', blue, _math(tree, 'MULTIPLY', relative_clock.outputs[0], 0.02))
    tree.links.new(blue, combine.inputs['Blue'])
    receiver.data.materials.append(material)
    scene.camera = ortho
    bpy.context.view_layer.update()
    return scene, material, receiver, light, ortho, perspective, host


def _objects_by_role(scene: bpy.types.Scene) -> dict[str, bpy.types.Object]:
    result: dict[str, bpy.types.Object] = {}
    for obj in scene.objects:
        role = obj.get(OBJECT_ROLE)
        if isinstance(role, str):
            if role in result:
                raise AssertionError(f'Duplicate fixture object role: {role}')
            result[role] = obj
    required = {RECEIVER_ROLE, LIGHT_ROLE, ORTHO_ROLE, PERSP_ROLE, HOST_ROLE}
    if not required.issubset(result):
        raise AssertionError(f'Regression fixture objects are missing: {sorted(required - result.keys())}')
    return result


def _apply_state(scene: bpy.types.Scene, state: RenderState,
                 objects: dict[str, bpy.types.Object]) -> None:
    scene.frame_start = state.frame_start
    scene.render.fps = state.fps
    scene.render.fps_base = state.fps_base
    scene.render.resolution_x = state.resolution_x
    scene.render.resolution_y = state.resolution_y
    scene.render.pixel_aspect_x = state.pixel_aspect_x
    scene.render.pixel_aspect_y = state.pixel_aspect_y
    camera = objects[state.camera_role]
    camera.data.type = state.camera_type
    camera.data.ortho_scale = state.ortho_scale
    camera.data.lens = state.lens
    camera.data.sensor_width = state.sensor_width
    scene.camera = camera
    objects[LIGHT_ROLE].hide_render = state.light_hidden
    objects[HOST_ROLE]['unrelated_scalar'] = state.unrelated_value
    objects[HOST_ROLE].update_tag()
    scene.frame_set(state.frame, subframe=state.subframe)
    bpy.context.view_layer.update()


def _configure_engine(scene: bpy.types.Scene, engine: str) -> None:
    scene.render.engine = engine
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.image_settings.color_depth = '32'
    if engine == 'CYCLES':
        scene.cycles.device = 'CPU'
        scene.cycles.samples = 1
        scene.cycles.use_denoising = False
    elif engine == 'BLENDER_EEVEE_NEXT':
        scene.eevee.taa_render_samples = 1
    else:
        raise ValueError(f'Unsupported regression render engine: {engine}')


def _render(scene: bpy.types.Scene, target_path: Path) -> ImageResult:
    bpy.context.view_layer.update()
    scene.render.filepath = str(target_path)
    result = bpy.ops.render.render(write_still=True)
    if result != {'FINISHED'}:
        raise AssertionError(f'Real {scene.render.engine} render did not finish: {result}')
    if not target_path.is_file():
        raise AssertionError(f'{scene.render.engine} render file is missing: {target_path}')
    image = bpy.data.images.load(str(target_path), check_existing=False)
    if image is None or image.size[0] <= 0 or image.size[1] <= 0:
        raise AssertionError(f'{scene.render.engine} render produced no Render Result')
    pixels = tuple(float(value) for value in image.pixels[:])
    if not pixels or not all(math.isfinite(value) for value in pixels):
        raise AssertionError(f'{scene.render.engine} render produced empty or non-finite pixels')
    result_image = int(image.size[0]), int(image.size[1]), pixels
    bpy.data.images.remove(image)
    return result_image


def _capture(scene: bpy.types.Scene, objects: dict[str, bpy.types.Object], engine: str,
             states: tuple[RenderState, ...], output: Path, phase: str) -> dict[str, ImageResult]:
    _configure_engine(scene, engine)
    results: dict[str, ImageResult] = {}
    for state in states:
        _apply_state(scene, state, objects)
        results[state.name] = _render(scene, output / (phase + '_' + state.name + '.exr'))
    return results


def _max_difference(left: ImageResult, right: ImageResult) -> float:
    if left[:2] != right[:2] or len(left[2]) != len(right[2]):
        raise AssertionError(f'Render dimensions changed: left={left[:2]}, right={right[:2]}')
    return max(abs(first - second) for first, second in zip(left[2], right[2]))


def _mean_pixel_difference(left: ImageResult, right: ImageResult) -> float:
    left_pixels = len(left[2]) // 4
    right_pixels = len(right[2]) // 4
    if left_pixels == 0 or right_pixels == 0:
        raise AssertionError('Render produced no complete RGBA pixels')
    left_mean = tuple(sum(left[2][channel::4]) / left_pixels for channel in range(4))
    right_mean = tuple(sum(right[2][channel::4]) / right_pixels for channel in range(4))
    return max(abs(first - second) for first, second in zip(left_mean, right_mean))


def _value_node(material: bpy.types.Material, label: str) -> bpy.types.Node:
    matches = tuple(node for node in material.node_tree.nodes
                    if node.bl_idname == 'ShaderNodeValue' and node.label == label)
    if len(matches) != 1:
        raise AssertionError(f'Expected one Value node labelled {label!r}, found {len(matches)}')
    return matches[0]


def _target_rows(socket: bpy.types.NodeSocket) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted((link.to_node.name, link.to_socket.identifier, link.to_socket.name)
                        for link in socket.links))


def _driver_curve(material: bpy.types.Material, node: bpy.types.Node) -> bpy.types.FCurve:
    tree = material.node_tree
    path = node.outputs[0].path_from_id('default_value')
    matches = tuple(curve for curve in tree.animation_data.drivers
                    if curve.data_path == path) if tree.animation_data else ()
    if len(matches) != 1:
        raise AssertionError(f'Expected one preserved driver for {material.name}/{node.label}: {len(matches)}')
    return matches[0]


def _source_values(scene: bpy.types.Scene, material: bpy.types.Material,
                   objects: dict[str, bpy.types.Object]) -> dict[str, float]:
    frame = float(scene.frame_current_final)
    fps = float(scene.render.fps)
    fps_base = float(scene.render.fps_base)
    ratio = max((scene.render.resolution_x * scene.render.pixel_aspect_x)
                / (scene.render.resolution_y * scene.render.pixel_aspect_y), 1.0)
    camera = scene.camera.data
    expected = {
        'Dry constant': 0.18,
        'Scene clock': frame * fps_base / fps,
        'Scene clock from start': (frame - scene.frame_start) / fps * fps_base,
        'Orthographic projection': 2.0 * ratio / camera.ortho_scale,
        'Perspective projection': 2.0 * camera.lens * ratio / camera.sensor_width,
        'Light render visibility': float(not objects[LIGHT_ROLE].hide_render),
        'Unrelated object driver': float(objects[HOST_ROLE]['unrelated_scalar']),
    }
    for label, value in expected.items():
        actual = float(_value_node(material, label).outputs[0].default_value)
        if not math.isclose(actual, value, rel_tol=1e-6, abs_tol=1e-6):
            raise AssertionError(f'Original Blender driver numeric value differs: {label}, expected={value}, actual={actual}')
    return expected


def _graph_signature(materials: tuple[bpy.types.Material, ...]) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    for material in sorted(materials, key=lambda item: item.name):
        tree = material.node_tree
        nodes = tuple(sorted((node.name, node.bl_idname, str(node.get('endfield_shader_uniform', '')))
                             for node in tree.nodes))
        links = tuple(sorted((link.from_node.name, link.from_socket.identifier,
                              link.to_node.name, link.to_socket.identifier) for link in tree.links))
        drivers = tuple(sorted((curve.data_path, curve.array_index, curve.driver.expression,
                                tuple((variable.name, variable.type,
                                       tuple((target.id.name if target.id else '', target.data_path)
                                             for target in variable.targets))
                                      for variable in curve.driver.variables))
                             for curve in tree.animation_data.drivers)) if tree.animation_data else ()
        rows.append((material.name, nodes, links, drivers))
    return tuple(rows)


def _all_receivers(scene: bpy.types.Scene) -> tuple[bpy.types.Object, ...]:
    return tuple(obj for obj in scene.objects if obj.type == 'MESH' and any(
        slot.material is not None and slot.material.get(MATERIAL_ROLE) in ('original', 'copy')
        for slot in obj.material_slots))


def _check_original_drivers(material: bpy.types.Material) -> None:
    unrelated = _value_node(material, 'Unrelated object driver')
    curve = _driver_curve(material, unrelated)
    driver = curve.driver
    if driver.expression != 'unrelated' or len(driver.variables) != 1:
        raise AssertionError('The unrelated object-property driver changed')
    target = driver.variables[0].targets[0]
    if target.id is None or target.id.get(OBJECT_ROLE) != HOST_ROLE or target.data_path != '["unrelated_scalar"]':
        raise AssertionError('The unrelated driver target changed')


def _make_material_copy(receiver: bpy.types.Object, material: bpy.types.Material,
                        light: bpy.types.Object, key: str) -> tuple[bpy.types.Object, bpy.types.Material]:
    copied_material = material.copy()
    copied_material.name = 'Uniform Regression Material Copy'
    copied_material[MATERIAL_ROLE] = 'copy'
    copied_object = receiver.copy()
    copied_object.data = receiver.data.copy()
    copied_object.name = 'Uniform Regression Receiver Copy'
    copied_object[OBJECT_ROLE] = 'receiver_copy'
    copied_object.data.materials.clear()
    copied_object.data.materials.append(copied_material)
    copied_object.hide_render = True
    receiver.users_collection[0].objects.link(copied_object)
    if copied_material.node_tree == material.node_tree:
        raise AssertionError('Material.copy did not create an independent shader graph')
    if copied_object.get(key) != light:
        raise AssertionError(f'Copied receiver lost its light ID pointer: {key}')
    return copied_object, copied_material


def _save_bundle(scene: bpy.types.Scene, objects: dict[str, bpy.types.Object]) -> None:
    bundle = bpy.data.collections.new(BUNDLE_NAME)
    scene.collection.children.link(bundle)
    for role in (RECEIVER_ROLE, LIGHT_ROLE, ORTHO_ROLE, PERSP_ROLE, HOST_ROLE):
        bundle.objects.link(objects[role])


def _collision_object(scene: bpy.types.Scene, name: str) -> bpy.types.Object:
    collision = bpy.data.objects.new(name, None)
    scene.collection.objects.link(collision)
    collision.hide_render = True
    return collision


def _append_bundle(library_path: Path, scene: bpy.types.Scene) -> bpy.types.Collection:
    with bpy.data.libraries.load(str(library_path), link=False) as (data_from, data_to):
        if BUNDLE_NAME not in data_from.collections:
            raise AssertionError(f'Native fixture blend has no append bundle: {library_path}')
        data_to.collections = [BUNDLE_NAME]
    if len(data_to.collections) != 1 or data_to.collections[0] is None:
        raise AssertionError('Blender did not append exactly one native regression collection')
    collection = data_to.collections[0]
    scene.collection.children.link(collection)
    return collection


def _report_receipt_entries(material: bpy.types.Material) -> tuple[UniformEntry, ...]:
    entries: list[UniformEntry] = []
    for node in material.node_tree.nodes:
        raw = node.get('endfield_shader_uniform')
        if not isinstance(raw, str):
            continue
        document = json.loads(raw)
        if (not isinstance(document, dict) or not isinstance(document.get('variables'), list)
                or not all(isinstance(row, dict) for row in document['variables'])):
            raise AssertionError(f'Invalid migrated Value-node receipt: {material.name}/{node.name}')
        entries.append(cast(UniformEntry, document))
    return tuple(entries)


def _check_migration(material: bpy.types.Material, before_links: dict[str, tuple[tuple[str, str, str], ...]],
                     migration: MigrationReport, receivers: tuple[bpy.types.Object, ...],
                     scene: bpy.types.Scene,
                     verify: Callable[[Sequence[bpy.types.Material], Sequence[bpy.types.Object], bpy.types.Scene],
                                      dict[str, str | int]],
                     resolve: Callable[[bpy.types.NodeTree, bpy.types.NodeSocket], bpy.types.NodeSocket]) -> str:
    tree = material.node_tree
    if migration.get('format') != 'EndfieldShaderUniforms/1' or migration.get('migrated') != 5:
        raise AssertionError(f'Expected five supported Value drivers to migrate: {migration}')
    entries = _report_receipt_entries(material)
    if len(entries) != 5:
        raise AssertionError(f'Expected five durable node receipts, found {len(entries)}')
    labels = ('Scene clock', 'Scene clock from start', 'Orthographic projection',
              'Perspective projection', 'Light render visibility')
    light_keys: list[str] = []
    for label in labels:
        source = _value_node(material, label)
        if not isinstance(source.get('endfield_shader_uniform'), str) or source.outputs[0].links:
            raise AssertionError(f'Migrated evidence Value node is missing or reconnected: {label}')
        path = source.outputs[0].path_from_id('default_value')
        if tree.animation_data and any(curve.data_path == path for curve in tree.animation_data.drivers):
            raise AssertionError(f'Legacy shader output driver remains: {label}')
        replacement = resolve(tree, source.outputs[0])
        if _target_rows(replacement) != before_links[label]:
            raise AssertionError(f'Native replacement changed shader/material links: {label}')
        receipt = json.loads(source['endfield_shader_uniform'])
        if label == 'Light render visibility':
            variables = receipt['variables']
            object_rows = tuple(row for row in variables if row['kind'] == 'OBJECT')
            if len(object_rows) != 1:
                raise AssertionError(f'Light visibility receipt has no unique OBJECT binding: {object_rows}')
            key = object_rows[0]['key']
            if not key.startswith(KEY_PREFIX) or object_rows[0]['path'] != '[' + json.dumps(key) + '].hide_render':
                raise AssertionError(f'Light receiver pointer path is malformed: {object_rows[0]}')
            light_keys.append(key)
            for receiver in receivers:
                pointer = receiver.get(key)
                if not isinstance(pointer, bpy.types.Object) or pointer.get(OBJECT_ROLE) != LIGHT_ROLE:
                    raise AssertionError(f'Light ID pointer is missing from a receiving object: {receiver.name}/{key}')
            if key in tree:
                raise AssertionError(f'Light ID pointer was stored on a ShaderNodeTree: {tree.name}/{key}')
    if len(light_keys) != 1:
        raise AssertionError('Expected exactly one unique additional-light receiver pointer')
    _check_original_drivers(material)
    if verify((material,), receivers, scene).get('status') != 'verified':
        raise AssertionError('Native shader uniform receipt validation did not pass')
    return light_keys[0]


def _run_regression(output_root: Path, engine: str) -> dict[str, object]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
    from character_shader_uniforms import (migrate_shader_uniforms, resolve_shader_uniform_socket,
                                           verify_shader_uniforms)

    output = output_root.resolve() / engine.lower()
    output.mkdir(parents=True, exist_ok=True)
    baseline_path = output / 'shader_uniform_driver_baseline.blend'
    native_path = output / 'shader_uniform_native.blend'
    append_path = output / 'shader_uniform_appended_collision.blend'
    scene, material, receiver, light, _, _, _ = _create_fixture()
    objects = _objects_by_role(scene)
    states = _state_cases()
    before_links = {label: _target_rows(_value_node(material, label).outputs[0]) for label in (
        'Scene clock', 'Scene clock from start', 'Orthographic projection',
        'Perspective projection', 'Light render visibility')}
    baseline_values: dict[str, dict[str, float]] = {}
    for state in states:
        _apply_state(scene, state, objects)
        baseline_values[state.name] = _source_values(scene, material, objects)
    _apply_state(scene, states[0], objects)
    _configure_engine(scene, engine)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(baseline_path))
    baseline = _capture(scene, objects, engine, states, output, 'driver')
    for name in ('clock_fps_subframe', 'orthographic_projection', 'perspective_projection', 'light_hidden'):
        delta = _mean_pixel_difference(baseline['initial'], baseline[name])
        if delta <= 1e-4:
            raise AssertionError(f'Source driver fixture did not visibly respond to {name}: delta={delta}')

    _apply_state(scene, states[0], objects)
    migration = migrate_shader_uniforms((material,), (receiver,), scene)
    migration_report = cast(MigrationReport, migration)
    if migration['migrated'] != 5 or migration['bindings'] < 1:
        raise AssertionError(f'Supported uniform migration count is wrong: {migration_report}')
    light_key = _check_migration(material, before_links, migration_report, (receiver,), scene,
                                 verify_shader_uniforms, resolve_shader_uniform_socket)
    light.name = RENAMED_LIGHT_NAME
    if receiver.get(light_key) != light:
        raise AssertionError('Renaming the Blender light broke its receiving-object ID pointer')
    before_repeat = _graph_signature((material,))
    repeated = migrate_shader_uniforms((material,), (receiver,), scene)
    if repeated['migrated'] != 0 or repeated['bindings'] != 0:
        raise AssertionError(f'Repeated migration was not idempotent: {repeated}')
    if _graph_signature((material,)) != before_repeat:
        raise AssertionError('Repeated migration changed the shader graph')
    native = _capture(scene, objects, engine, states, output, 'native')
    parity: dict[str, float] = {}
    for state in states:
        delta = _max_difference(baseline[state.name], native[state.name])
        parity[state.name] = delta
        if delta > 2e-4:
            raise AssertionError(f'Real render differs after native migration: engine={engine}, '
                                 f'state={state.name}, max_pixel_delta={delta}')

    copy_object, copy_material = _make_material_copy(receiver, material, light, light_key)
    all_materials = (material, copy_material)
    all_receivers = (receiver, copy_object)
    _check_original_drivers(copy_material)
    if verify_shader_uniforms(all_materials, all_receivers, scene).get('status') != 'verified':
        raise AssertionError('Copied material/object native pointer bindings did not verify')
    source_signature = _graph_signature(all_materials)
    _save_bundle(scene, objects)
    _apply_state(scene, states[0], objects)
    bpy.ops.wm.save_as_mainfile(filepath=str(native_path))
    bpy.ops.wm.open_mainfile(filepath=str(native_path), use_scripts=False, load_ui=False)

    scene = bpy.context.scene
    objects = _objects_by_role(scene)
    receiver = objects[RECEIVER_ROLE]
    light = objects[LIGHT_ROLE]
    materials = tuple(material for material in bpy.data.materials
                      if material.get(MATERIAL_ROLE) in ('original', 'copy'))
    if len(materials) != 2:
        raise AssertionError(f'Save/reopen lost a copied material: {len(materials)}')
    material = next(item for item in materials if item.get(MATERIAL_ROLE) == 'original')
    copy_material = next(item for item in materials if item.get(MATERIAL_ROLE) == 'copy')
    receivers = _all_receivers(scene)
    if len(receivers) != 2:
        raise AssertionError(f'Save/reopen lost a copied receiver: {len(receivers)}')
    if _graph_signature((material, copy_material)) != source_signature:
        raise AssertionError('Save/reopen changed material graph links, node receipts, or unrelated drivers')
    _check_original_drivers(material)
    verify_shader_uniforms((material, copy_material), receivers, scene)
    before_repeat = _graph_signature((material, copy_material))
    repeated = migrate_shader_uniforms((material, copy_material), receivers, scene)
    if repeated['migrated'] != 0 or repeated['bindings'] != 0:
        raise AssertionError(f'Saved-file migration was not idempotent: {repeated}')
    if _graph_signature((material, copy_material)) != before_repeat:
        raise AssertionError('Saved-file idempotence check changed the graph')
    reopened_native = _capture(scene, objects, engine, (states[0], states[-1]), output, 'reopened')
    reopen_parity = {state.name: _max_difference(baseline[state.name], reopened_native[state.name])
                     for state in (states[0], states[-1])}
    if any(value > 2e-4 for value in reopen_parity.values()):
        raise AssertionError(f'Saved/reopened render differs from the original driver output: {reopen_parity}')

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    _ensure_world(scene)
    _collision_object(scene, 'Uniform Regression Receiver')
    collision_light = _collision_object(scene, RENAMED_LIGHT_NAME)
    _collision_object(scene, 'Uniform Regression Ortho Camera')
    _collision_object(scene, 'Uniform Regression Perspective Camera')
    collision_host = _collision_object(scene, 'Uniform Regression Unrelated Host')
    appended_collection = _append_bundle(native_path, scene)
    appended_objects = tuple(appended_collection.objects)
    appended_roles = {obj[OBJECT_ROLE]: obj for obj in appended_objects if OBJECT_ROLE in obj}
    if not {RECEIVER_ROLE, LIGHT_ROLE, ORTHO_ROLE, PERSP_ROLE, HOST_ROLE}.issubset(appended_roles):
        raise AssertionError(f'Append omitted fixture objects: {sorted(appended_roles)}')
    appended_receiver = appended_roles[RECEIVER_ROLE]
    appended_light = appended_roles[LIGHT_ROLE]
    appended_host = appended_roles[HOST_ROLE]
    appended_material = appended_receiver.material_slots[0].material
    if appended_material is None or appended_material.get(MATERIAL_ROLE) != 'original':
        raise AssertionError('Append did not retain the original migrated material')
    if appended_light == collision_light:
        raise AssertionError('Name collision routed a native bridge pointer to a destination object')
    appended_entries = _report_receipt_entries(appended_material)
    appended_light_rows = tuple(row for entry in appended_entries for row in entry['variables']
                                if row['kind'] == 'OBJECT')
    if len(appended_light_rows) != 1:
        raise AssertionError(f'Appended material lost its light binding receipt: {appended_light_rows}')
    appended_key = appended_light_rows[0]['key']
    if appended_receiver.get(appended_key) != appended_light or appended_receiver.get(appended_key) == collision_light:
        raise AssertionError('Appended per-receiver ID pointer did not remap to the appended light')
    unrelated_curve = _driver_curve(appended_material, _value_node(appended_material, 'Unrelated object driver'))
    unrelated_target = unrelated_curve.driver.variables[0].targets[0].id
    if unrelated_target != appended_host or unrelated_target == collision_host:
        raise AssertionError('Unrelated object driver target did not remap with the appended material')
    appended_cameras = {obj[OBJECT_ROLE]: obj for obj in appended_objects if obj.type == 'CAMERA'}
    append_roles = {RECEIVER_ROLE: appended_receiver, LIGHT_ROLE: appended_light,
                    ORTHO_ROLE: appended_cameras[ORTHO_ROLE], PERSP_ROLE: appended_cameras[PERSP_ROLE],
                    HOST_ROLE: appended_host}
    scene.camera = appended_cameras[ORTHO_ROLE]
    bpy.context.view_layer.update()
    if verify_shader_uniforms((appended_material,), (appended_receiver,), scene).get('status') != 'verified':
        raise AssertionError('Appended native shader uniforms failed graph/pointer validation')
    _apply_state(scene, states[0], append_roles)
    _configure_engine(scene, engine)
    appended_before_save = _render(scene, output / 'appended_initial.exr')
    append_delta = _max_difference(native['initial'], appended_before_save)
    if append_delta > 2e-4:
        raise AssertionError(f'Appended collision fixture changed rendered pixels: {append_delta}')
    bpy.ops.wm.save_as_mainfile(filepath=str(append_path))
    bpy.ops.wm.open_mainfile(filepath=str(append_path), use_scripts=False, load_ui=False)

    scene = bpy.context.scene
    appended_collection = bpy.data.collections.get(BUNDLE_NAME)
    if appended_collection is None:
        raise AssertionError('Save/reopen lost the appended regression collection')
    appended_objects = tuple(appended_collection.objects)
    appended_roles = {obj[OBJECT_ROLE]: obj for obj in appended_objects if OBJECT_ROLE in obj}
    appended_receiver = appended_roles[RECEIVER_ROLE]
    appended_light = appended_roles[LIGHT_ROLE]
    appended_host = appended_roles[HOST_ROLE]
    appended_material = appended_receiver.material_slots[0].material
    if appended_material is None:
        raise AssertionError('Save/reopen lost the appended material')
    appended_entries = _report_receipt_entries(appended_material)
    appended_key = next(row['key'] for entry in appended_entries for row in entry['variables']
                        if row['kind'] == 'OBJECT')
    if appended_receiver.get(appended_key) != appended_light:
        raise AssertionError('Save/reopen lost the appended receiver-to-light ID pointer')
    verify_shader_uniforms((appended_material,), (appended_receiver,), scene)
    scene_objects = _objects_by_role(scene)
    _apply_state(scene, states[0], scene_objects)
    appended_after_reopen = _render(scene, output / 'appended_reopened_initial.exr')
    append_reopen_delta = _max_difference(native['initial'], appended_after_reopen)
    if append_reopen_delta > 2e-4:
        raise AssertionError(f'Appended save/reopen changed rendered pixels: {append_reopen_delta}')

    return {
        'status': 'passed',
        'blender_version': bpy.app.version_string,
        'engine': engine,
        'device': 'CPU' if engine == 'CYCLES' else 'EEVEE',
        'fixture': 'Synthetic plane, material-only emission shader, real scene/camera/light RNA drivers',
        'states': [state.name for state in states],
        'source_driver_values': baseline_values,
        'migrated_value_drivers': migration['migrated'],
        'receiver_bindings': migration['bindings'],
        'unrelated_driver_preserved': True,
        'dry_constant_preserved': True,
        'material_links_preserved': True,
        'migration_idempotent': True,
        'save_reopen_verified': True,
        'copy_verified': True,
        'light_rename_verified': True,
        'append_name_collision_pointer_remap_verified': True,
        'legacy_vs_native_max_pixel_delta': parity,
        'save_reopen_max_pixel_delta': reopen_parity,
        'append_max_pixel_delta': append_delta,
        'append_save_reopen_max_pixel_delta': append_reopen_delta,
        'files': {'driver_baseline': str(baseline_path), 'native': str(native_path),
                  'appended_collision': str(append_path)},
    }


def main(output: Path, engine: str) -> None:
    report = _run_regression(output, engine)
    report_path = output.resolve() / engine.lower() / 'shader_uniform_render_regression.json'
    report_path.write_text(json.dumps(report, allow_nan=False, ensure_ascii=False, indent=2), encoding='utf-8')
    print('SHADER_UNIFORM_RENDER_REGRESSION_PASSED', json.dumps(report, allow_nan=False, ensure_ascii=False),
          flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--engine', choices=('CYCLES', 'BLENDER_EEVEE_NEXT'), required=True)
    arguments = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(arguments.output, arguments.engine)
