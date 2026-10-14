"""Verify editable Sun inputs on an existing water candidate and after reopen."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from blender_water_transport import connect_water_transport_sun


def validate(scene: bpy.types.Scene, transport: bpy.types.Node, light: bpy.types.Object) -> dict[str, object]:
    scene.frame_set(scene.frame_current + 1)
    bpy.context.view_layer.update()
    phase = transport.inputs['Phase Cosine'].links[0].from_node
    direction = phase.inputs[1].links[0].from_node
    actual = tuple(socket.default_value for socket in direction.inputs)
    expected = tuple(light.matrix_world.to_quaternion() @ Vector((0, 0, -1)))
    error = max(abs(a - b) for a, b in zip(actual, expected, strict=True))
    if error > 1e-5:
        raise AssertionError(f'Live direction mismatch: actual={actual}, expected={expected}, error={error}')
    radiance = tuple(transport.inputs['HOST Direct Radiance'].default_value[:3])
    expected_radiance = tuple(channel * light.data.energy * (not light.hide_render) for channel in light.data.color)
    color_error = max(abs(a - b) for a, b in zip(radiance, expected_radiance, strict=True))
    if color_error > 1e-5:
        raise AssertionError(f'Live radiance mismatch: actual={radiance}, expected={expected_radiance}')
    paths = {socket.path_from_id() + '.default_value' for socket in direction.inputs}
    paths.add(transport.inputs['HOST Direct Radiance'].path_from_id() + '.default_value')
    drivers = tuple(curve for curve in transport.id_data.animation_data.drivers if curve.data_path in paths)
    if len(drivers) != 6 or any(not curve.driver.is_valid for curve in drivers):
        raise AssertionError('Six built-in water light drivers must remain valid')
    return {'direction':actual, 'expected_direction':expected, 'maximum_direction_error':error,
            'radiance':radiance, 'maximum_radiance_error':color_error, 'valid_drivers':len(drivers)}


def main(source: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(source), use_scripts=False)
    scene = bpy.context.scene
    water = next(obj for obj in scene.objects if obj.name.startswith('WATER_') and obj.get('entity_id'))
    material = water.data.materials[0]
    transport = next(node for node in material.node_tree.nodes
                     if node.type == 'GROUP' and node.node_tree.get('endfield_contract') == 'SourceWaterTransport/1')
    sun = next(obj for obj in scene.objects if obj.type == 'LIGHT' and obj.data.type == 'SUN')
    source_matrix = sun.matrix_world.copy()
    original_energy, original_color = sun.data.energy, tuple(sun.data.color)
    initial_radiance = tuple(transport.inputs['HOST Direct Radiance'].default_value[:3])
    connect_water_transport_sun(transport, sun, 1.0)
    checks = [validate(scene, transport, sun)]
    if max(abs(a - b) for a, b in zip(initial_radiance, checks[0]['radiance'], strict=True)) > 1e-6:
        raise AssertionError('The candidate host-light baseline changed on binding')
    parent = bpy.data.objects.new('Live Sun Parent Verification', None)
    scene.collection.objects.link(parent)
    parent.rotation_euler = (0.3, -0.7, 0.2)
    sun.parent = parent
    sun.rotation_euler = (1.1, 0.4, -0.9)
    sun.data.color, sun.data.energy = (0.2, 0.7, 0.9), 3.0
    checks.append(validate(scene, transport, sun))
    sun.rotation_mode = 'QUATERNION'
    sun.rotation_quaternion = Vector((0.3, 0.5, -0.8)).to_track_quat('-Z', 'Y')
    checks.append(validate(scene, transport, sun))
    sun.hide_render = True
    checks.append(validate(scene, transport, sun))
    sun.hide_render = False
    sun.parent = None
    bpy.data.objects.remove(parent, do_unlink=True)
    sun.matrix_world = source_matrix
    sun.data.energy, sun.data.color = original_energy, original_color
    checks.append(validate(scene, transport, sun))
    scene.render.resolution_x, scene.render.resolution_y = 320, 240
    scene.render.resolution_percentage = 100
    scene.cycles.samples = 32
    scene.render.image_settings.file_format = 'PNG'
    renders = []
    for engine in ('BLENDER_EEVEE_NEXT', 'CYCLES'):
        scene.render.engine = engine
        for name, color, energy in (('source_host', original_color, original_energy),
                                    ('edited_host', (0.05, 0.8, 1.0), 5.0)):
            sun.data.color, sun.data.energy = color, energy
            checks.append(validate(scene, transport, sun))
            path = output / f'{name}_{engine}.png'
            scene.render.filepath = str(path)
            bpy.ops.render.render(write_still=True)
            renders.append({'engine':engine, 'state':name, 'path':str(path)})
    sun.data.energy, sun.data.color = original_energy, original_color
    validate(scene, transport, sun)
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    scene.frame_set(1)
    bpy.context.preferences.filepaths.save_version = 0
    artifact = output / 'river_live_sun.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(artifact))
    bpy.ops.wm.open_mainfile(filepath=str(artifact), use_scripts=False)
    scene = bpy.context.scene
    transport = next(node for mat in bpy.data.materials if mat.use_nodes for node in mat.node_tree.nodes
                     if node.get('endfield_direction_contract') == 'LiveHostSunForwardBlenderZUp/1')
    sun = bpy.data.objects[transport['endfield_host_sun']]
    sun.rotation_mode = 'XYZ'
    sun.rotation_euler = (0.8, 0.2, -0.4)
    sun.data.color, sun.data.energy = (0.9, 0.1, 0.4), 2.5
    checks.append(validate(scene, transport, sun))
    if digest != hashlib.sha256(source.read_bytes()).hexdigest():
        raise AssertionError('The source candidate changed during validation')
    receipt = {'status':'live_host_sun_verified_saved_reopened_without_auto_scripts', 'source':str(source),
               'source_sha256':digest, 'artifact':str(artifact), 'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),
               'checks':checks, 'renders':renders, 'source_assets_modified':False, 'initial_host_radiance_preserved':True,
               'boundary':'Explicit selected Blender Sun and energy scale; no native runtime light selection, atmosphere radiance or screen-buffer parity.',
               'default_export_changed':False}
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print('WATER_LIVE_SUN_VERIFIED')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.source, args.output_dir)
