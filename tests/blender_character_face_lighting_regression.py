"""Render real character faces under independent lamps in both Blender engines."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy
import numpy as np
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from character_material_options import parse_character_material_options
from character_material_variant import apply_material_variant, verify_material_variant
from character_simple_materials import HEAD_SHADOW_FAMILIES, simple_material_surfaces, source_head_shadow_mode
from character_weather_panel import weather_groups, weather_socket
from blender_character_simple_materials_regression import scene_digest


def render_crop(scene: bpy.types.Scene, destination: Path) -> tuple[float, float, float]:
    """Measure the central facial area from the actual saved render."""
    scene.render.filepath = str(destination)
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(str(destination), check_existing=False)
    width, height = image.size
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    values = pixels.reshape(height, width, 4)[int(height*.40):int(height*.55), int(width*.40):int(width*.60), :3]
    means = tuple(float(value) for value in values.mean(axis=(0, 1)))
    bpy.data.images.remove(image)
    return means


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--weather', required=True, choices=('simple', 'full'))
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    args.output.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    scene.frame_set(1)
    before = scene_digest(scene)
    report = apply_material_variant(scene, parse_character_material_options({
        'mode': 'simplified', 'lighting': 'simple',
        'weather': {'quality': args.weather, 'effects': ['wet', 'snow', 'rain']}}), [])
    verify_material_variant(report)
    assert before == scene_digest(scene), 'Material conversion changed geometry, rig or visibility'
    faces = tuple(surface for surface in simple_material_surfaces(scene) if surface.family == 'face')
    assert faces, 'Fixture has no source SDF face'
    for surface in faces:
        assert surface.channels['surface_closure'] == surface.bsdf.name
        assert 'unlit_node' not in surface.channels
        assert surface.channels.inputs['Face Forward'].is_linked
        assert surface.channels.inputs['Face Brightness'].is_linked
        assert surface.channels.inputs['Face Normal Detail'].is_linked
    shadow_owners = 0
    for obj in scene.objects:
        if obj.type != 'MESH':
            continue
        mode = source_head_shadow_mode(obj)
        for slot in obj.material_slots:
            material = slot.material
            if material is None or not material.get('endfield_simple_material'):
                continue
            families = {node['semantic_family'] for node in material.node_tree.nodes
                        if node.get('endfield_simple_channels')}
            if material.get('overlay_scope'):
                families = {'overlay'}
            if not families or not families.issubset(HEAD_SHADOW_FAMILIES):
                continue
            disabled = material.get('source_shadow_casting') == 'disabled_by_unanimous_source_renderers'
            assert disabled == (mode == 0), f'Head shadow policy differs: {obj.name}/{material.name}/{mode}'
            if disabled:
                output = next(node for node in material.node_tree.nodes
                              if node.type == 'OUTPUT_MATERIAL' and node.is_active_output)
                gate = output.inputs['Surface'].links[0].from_node
                assert gate.type == 'MIX_SHADER'
                assert gate.inputs[0].links[0].from_socket.name == 'Is Shadow Ray'
                assert gate.inputs[2].links[0].from_node.type == 'BSDF_TRANSPARENT'
                shadow_owners += 1
    owners = tuple(obj for obj in scene.objects if obj.type == 'MESH' and not obj.hide_render
                   and any(slot.material == surface.material for slot in obj.material_slots for surface in faces))
    assert owners, 'Fixture has no visible source SDF face mesh'
    corners = tuple(obj.matrix_world @ Vector(corner) for obj in owners for corner in obj.bound_box)
    low = Vector(tuple(min(corner[axis] for corner in corners) for axis in range(3)))
    high = Vector(tuple(max(corner[axis] for corner in corners) for axis in range(3)))
    center = (low + high)*.5
    height = max(high.z-low.z, high.x-low.x)*2.5
    camera = scene.camera
    camera.location = center + camera.matrix_world.to_quaternion() @ Vector((0., 0., height*4.))
    camera.data.type = 'ORTHO'
    camera.data.ortho_scale = height
    camera.data.dof.use_dof = False
    scene.render.resolution_x = 480
    scene.render.resolution_y = 480
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    scene.eevee.taa_render_samples = 64
    scene.cycles.samples = 24
    scene.cycles.use_denoising = True
    lamp_data = bpy.data.lights.new('Face response test area', 'AREA')
    lamp_data.energy = 55.*height*height
    lamp_data.shape = 'DISK'
    lamp_data.size = height
    lamp = bpy.data.objects.new(lamp_data.name, lamp_data)
    scene.collection.objects.link(lamp)
    lamp.location = center + Vector((height*.8, -height, height*.6))
    lamp.rotation_euler = (center-lamp.location).to_track_quat('-Z', 'Y').to_euler()
    groups = {group.name: group for surface in simple_material_surfaces(scene)
              for group in weather_groups(surface.material)}
    for group in groups.values():
        for name in ('User Effect X', 'User Effect Z', 'User Effect W'):
            weather_socket(group, name).default_value = 0.
    results: dict[str, tuple[float, float, float]] = {}
    for engine in ('BLENDER_EEVEE_NEXT', 'CYCLES'):
        scene.render.engine = engine
        for name, energy in (('off', 0.), ('on', 55.*height*height)):
            lamp_data.energy = energy
            results[engine+'_'+name] = render_crop(scene, args.output/f'{engine}_{name}.png')
        difference = np.mean(results[engine+'_on']) - np.mean(results[engine+'_off'])
        assert difference > .01, f'Face ignores added lamp in {engine}: difference={difference}'
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    for name, color in (('red', (1., .02, .02)), ('blue', (.02, .02, 1.))):
        lamp_data.color = color
        results[name] = render_crop(scene, args.output/f'{name}.png')
    assert results['red'][0] > results['blue'][0] + .01, results
    assert results['blue'][2] > results['red'][2] + .01, results
    lamp_data.color = (1., 1., 1.)
    for surface in faces:
        surface.material.node_tree.nodes['EF Simple Face Brightness'].outputs[0].default_value = .6
    results['brightness_06'] = render_crop(scene, args.output/'brightness_06.png')
    assert np.mean(results['brightness_06']) < np.mean(results['BLENDER_EEVEE_NEXT_on']) - .01
    for surface in faces:
        surface.material.node_tree.nodes['EF Simple Face Brightness'].outputs[0].default_value = 1.
    for group in groups.values():
        weather_socket(group, 'User Effect Z').default_value = .8
        weather_socket(group, 'User Effect X').default_value = 1.
    results['wet_rain'] = render_crop(scene, args.output/'wet_rain.png')
    for group in groups.values():
        weather_socket(group, 'User Effect Z').default_value = 0.
        weather_socket(group, 'User Effect X').default_value = 0.
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output/'face_lighting_review.blend'))
    (args.output/'verification.json').write_text(json.dumps({
        'status': 'passed', 'faces': len(faces), 'weather': args.weather,
        'source_no_cast_head_slots': shadow_owners,
        'geometry_rig_actions_preserved': True, 'face_crop_rgb': results,
        'limits': report['limits']}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('FACE_LIGHTING_PASSED', args.weather, flush=True)


if __name__ == '__main__':
    main()
