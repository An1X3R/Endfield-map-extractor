"""Export real mesh-only effects, reopen them, and verify portable playback."""
from __future__ import annotations

import argparse
import json
import math
import sys
from array import array
from pathlib import Path

import bpy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-blend', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--camera', required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from blender_export_effect_animation import main as export_animation
    from blender_scene_effect_animation import animate_effect_material_time

    bpy.ops.wm.open_mainfile(filepath=str(args.source_blend), load_ui=False, use_scripts=False)
    removed: list[str] = []
    for obj in tuple(bpy.data.objects):
        if 'endfield_fx_system_source' in obj:
            removed.append(obj.name)
            bpy.data.objects.remove(obj, do_unlink=True)
    if not removed:
        raise AssertionError('Regression input must contain real particles to remove from the mesh-only slice')
    scene = bpy.context.scene
    scene.camera = scene.objects[args.camera]
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    scene.render.resolution_x = 480
    scene.render.resolution_y = 320
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    source = args.output / 'mesh_only_source.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(source))
    plan = json.loads(args.plan.read_text(encoding='utf-8-sig'))
    plan['effect_instances'] = [row for row in plan['effect_instances']
                                if row.get('component_kind') == 'mesh_renderer']
    subset_plan = args.output / 'mesh_only_plan.json'
    subset_plan.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    delivery = args.output / 'mesh_effect_animation.blend'
    sys.argv = ['blender', '--', '--source-blend', str(source), '--plan', str(subset_plan),
                '--output', str(delivery), '--frame-start', '1', '--frame-end', '72',
                '--max-systems', '1', '--camera', args.camera]
    export_animation()
    receipt = json.loads(delivery.with_suffix('.animation.json').read_text(encoding='utf-8'))
    if receipt['cache'] is not None or delivery.with_suffix('.abc').exists():
        raise AssertionError('Material-only export must not require an Alembic sidecar')
    names = receipt['mesh_uv_time_driver_materials']
    if not names or receipt['animated_systems']:
        raise AssertionError('Mesh-only regression did not exercise timed source materials')
    bpy.ops.wm.open_mainfile(filepath=str(delivery), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    timings: dict[str, list[float]] = {}
    for name in names:
        material = bpy.data.materials[name]
        animate_effect_material_time(material, scene)
        animate_effect_material_time(material, scene)
        tree = material.node_tree
        driver = next(curve.driver for curve in tree.animation_data.drivers
                      if 'EF FX Source Time Seconds' in curve.data_path)
        if sorted(variable.name for variable in driver.variables) != ['fps', 'fps_base']:
            raise AssertionError(f'Repeated activation accumulated driver variables: material={name}')
        timings[name] = []
    images: list[array] = []
    for frame in (1, 72):
        scene.frame_set(frame)
        expected = frame * scene.render.fps_base / scene.render.fps
        for name in names:
            actual = float(bpy.data.materials[name].node_tree.nodes['EF FX Source Time Seconds'].outputs[0].default_value)
            if not math.isclose(actual, expected, abs_tol=1e-5):
                raise AssertionError(f'Reopened source time mismatch: material={name}, frame={frame}, actual={actual}')
            timings[name].append(actual)
        path = args.output / f'frame_{frame:03d}.png'
        scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(str(path), check_existing=False)
        pixels = array('f', [0.0]) * len(image.pixels)
        image.pixels.foreach_get(pixels)
        images.append(pixels)
        bpy.data.images.remove(image)
    differences = [abs(left-right) for left, right in zip(images[0], images[1])]
    changed = sum(value > 1 / 255 for value in differences)
    if not changed:
        raise AssertionError('Actual source mesh effects produced identical endpoint images')
    (args.output / 'verification.json').write_text(json.dumps({
        'status': 'passed', 'source': str(args.source_blend), 'delivery': str(delivery),
        'removed_particle_objects': len(removed), 'timed_materials': timings,
        'reopened': True, 'alembic_required': False, 'changed_components': changed,
        'max_component_difference': max(differences),
        'scope': 'Real office mesh-only slice; endpoint appearance and clock playback, not full runtime parity',
    }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
