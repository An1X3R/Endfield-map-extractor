"""Exercise compact character materials on a real retained Blender delivery."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from character_simple_materials import SimpleMaterialOptions, apply_simple_character_materials, bind_simple_face_light, simple_material_surfaces


def geometry_digest(mesh: bpy.types.Mesh) -> str:
    """Include mesh positions, connectivity, UVs and retained shape-key positions."""
    vertices = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    loops = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.vertices.foreach_get('co', vertices)
    mesh.loops.foreach_get('vertex_index', loops)
    parts = [vertices.tobytes(), loops.tobytes()]
    for layer in mesh.uv_layers:
        uv = np.empty(len(mesh.loops) * 2, dtype=np.float32)
        layer.data.foreach_get('uv', uv)
        parts.extend((layer.name.encode(), uv.tobytes()))
    if mesh.shape_keys is not None:
        for key in mesh.shape_keys.key_blocks:
            positions = np.empty(len(key.data) * 3, dtype=np.float32)
            key.data.foreach_get('co', positions)
            parts.extend((key.name.encode(), positions.tobytes()))
    return hashlib.sha256(b''.join(parts)).hexdigest()


def scene_digest(scene: bpy.types.Scene) -> str:
    """Ignore materials while retaining geometry, object transforms, rig and actions."""
    objects = []
    for obj in scene.objects:
        row = {'name': obj.name, 'type': obj.type,
            'matrix': [list(value) for value in obj.matrix_world],
            'parent': None if obj.parent is None else obj.parent.name,
            'hidden': [obj.hide_render, obj.hide_viewport, obj.hide_get()]}
        if obj.type == 'MESH':
            row['geometry'] = geometry_digest(obj.data)
        if obj.type == 'ARMATURE':
            row['bones'] = [(bone.name, list(bone.head_local), list(bone.tail_local)) for bone in obj.data.bones]
            row['pose'] = [(bone.name, [list(value) for value in bone.matrix_basis]) for bone in obj.pose.bones]
        objects.append(row)
    actions = [(action.name, action.as_pointer(), action.users) for action in bpy.data.actions]
    return hashlib.sha256(json.dumps([objects, actions], sort_keys=True).encode()).hexdigest()


def verify_expression_drivers(scene: bpy.types.Scene) -> int:
    """Change the real expression controller and observe copied live value nodes."""
    controllers = tuple(obj for obj in scene.objects if obj.get('expression_material_controls') is not None)
    if not controllers:
        return 0
    if len(controllers) != 1:
        raise AssertionError('The integration fixture has ambiguous material expression controllers')
    controller = controllers[0]
    initial = {key: controller[key] for key in ('_EmotionIndex', '_EmotionBlend')}
    values = {'_EmotionIndex': 2., '_EmotionBlend': .7}
    drivers = []
    materials = {surface.material.name: surface.material for surface in simple_material_surfaces(scene)}
    for material in materials.values():
        tree = material.node_tree
        if tree.animation_data is None:
            continue
        for curve in tree.animation_data.drivers:
            if any(target.id == controller for variable in curve.driver.variables for target in variable.targets):
                drivers.append((tree, curve))
    if not drivers:
        raise AssertionError('Source material expressions have no copied compact consumers')
    frame = scene.frame_current
    try:
        for key, value in values.items():
            controller[key] = value
        controller.update_tag()
        scene.frame_set(frame + 1)
        bpy.context.view_layer.update()
        for tree, curve in drivers:
            if not curve.driver.is_valid:
                raise AssertionError('Copied material expression driver is invalid: ' + curve.data_path)
            target = curve.driver.variables[0].targets[0]
            property_name = json.loads(target.data_path[1:-1])
            actual = tree.path_resolve(curve.data_path)
            if abs(actual - values[property_name]) > 1e-6:
                raise AssertionError(f'Copied expression route did not evaluate: {property_name}={actual}')
    finally:
        for key, value in initial.items():
            controller[key] = value
        controller.update_tag()
        scene.frame_set(frame)
        bpy.context.view_layer.update()
    return len(drivers)


def verify_sun_routes(scene: bpy.types.Scene) -> int:
    """Evaluate the real designated Sun direction, color and intensity routes."""
    sun = bpy.data.objects.get('EF HGRP Main Sun')
    if sun is None:
        raise AssertionError('The real character fixture is missing its designated main Sun')
    count = bind_simple_face_light(scene, sun)
    materials = {surface.material.name: surface.material for surface in simple_material_surfaces(scene)}
    if count == 0 and not any(item.get('endfield_simple_sun_color')
                              for material in materials.values() for item in material.node_tree.nodes):
        raise AssertionError('Compact materials have no designated Sun direction or color consumers')
    lights = [(material.node_tree, item) for material in materials.values()
              for item in material.node_tree.nodes if item.get('endfield_simple_face_light')]
    initial_rotation = sun.rotation_euler.copy()
    initial_color = tuple(sun.data.color)
    initial_energy = sun.data.energy
    frame = scene.frame_current
    owners = {material.name: next(obj for obj in scene.objects
                                 if obj.type == 'MESH' and any(slot.material == material for slot in obj.material_slots))
              for material in materials.values()}
    visibility = {obj.name: (obj.hide_render, obj.hide_viewport, obj.hide_get()) for obj in owners.values()}
    try:
        # Hidden LOD graphs do not evaluate until an owning object participates.
        for obj in owners.values():
            obj.hide_render = False
            obj.hide_viewport = False
            obj.hide_set(False)
        sun.rotation_euler.z += .4
        sun.data.color = (.8, .9, 1.)
        sun.data.energy = initial_energy * .5
        sun.update_tag()
        scene.frame_set(frame + 1)
        bpy.context.view_layer.update()
        for _, item in lights:
            actual = tuple(socket.default_value for socket in item.inputs)
            expected = tuple(sun.matrix_world[axis][2] for axis in range(3))
            if any(abs(a - b) > 1e-6 for a, b in zip(actual, expected, strict=True)):
                raise AssertionError('The compact SDF did not follow its designated Sun transform')
        for material in materials.values():
            for item in material.node_tree.nodes:
                if item.get('endfield_simple_flat_surface') and abs(item.inputs['Strength'].default_value - .5) > 1e-6:
                    raise AssertionError('Flat face/eye diffuse did not follow relative Sun energy')
                if item.get('endfield_simple_sun_color') and any(abs(a - b) > 1e-6 for a, b in zip(item.outputs[0].default_value[:3], sun.data.color, strict=True)):
                    raise AssertionError('Flat face/eye diffuse did not follow Sun color')
    finally:
        for name, state in visibility.items():
            obj = scene.objects[name]
            obj.hide_render, obj.hide_viewport = state[:2]
            obj.hide_set(state[2])
        sun.rotation_euler = initial_rotation
        sun.data.color = initial_color
        sun.data.energy = initial_energy
        sun.update_tag()
        scene.frame_set(frame)
        bpy.context.view_layer.update()
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    args.output.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    before = scene_digest(scene)
    report = apply_simple_character_materials(scene, SimpleMaterialOptions((.5, -.6, .65), .18))
    if scene_digest(scene) != before:
        raise AssertionError('Material conversion changed geometry, rig, actions, transforms or visibility')
    surfaces = simple_material_surfaces(scene)
    for surface in surfaces:
        for item in surface.material.node_tree.nodes:
            if item.type == 'GROUP' and any(value.type == 'TEX_IMAGE' for value in item.node_tree.nodes):
                raise AssertionError('A compact calculation group hides image samplers')
            if item.type == 'GROUP' and item.node_tree.get('source_shader') is not None:
                raise AssertionError('A compact material retains the full source shader graph')
    expression_drivers = verify_expression_drivers(scene)
    sun_routes = verify_sun_routes(scene)
    if scene_digest(scene) != before:
        raise AssertionError('Expression verification did not restore the original character state')
    document = {'materials': asdict(report), 'geometry_rig_actions_preserved': True,
        'live_expression_routes': expression_drivers, 'live_sun_routes': sun_routes, 'surface_hooks': len(surfaces)}
    (args.output / 'simple_material_regression.json').write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output / 'simple_material_regression.blend'))
    print(json.dumps({'status': 'passed', 'materials': len(report.materials),
        'surfaces': len(surfaces), 'expression_routes': expression_drivers}))


if __name__ == '__main__':
    main()
