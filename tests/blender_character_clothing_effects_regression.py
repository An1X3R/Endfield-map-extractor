"""Exercise live clothing controls on real full and simplified character exports."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/character_runtime'))
from character_material_authoring import shader_dependencies
from character_shader_uniforms import verify_shader_uniforms
from character_weather_panel import clothing_effect_controls, register


def pixels(path: Path) -> np.ndarray:
    image = bpy.data.images.load(str(path), check_existing=False)
    data = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(data)
    bpy.data.images.remove(image)
    return data.reshape((-1, 4))[:, :3]


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(arguments)
    source = args.blend.resolve(strict=True)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.open_mainfile(filepath=str(source), use_scripts=False, load_ui=False)
    register()
    scene = bpy.context.scene
    materials = tuple({slot.material.name: slot.material for obj in scene.objects
                       if obj.type in {'MESH', 'CURVES'} and not obj.hide_render
                       for slot in obj.material_slots if slot.material is not None}.values())
    controls: dict[int, bpy.types.NodeSocket] = {}
    rows: list[dict[str, object]] = []
    for material in materials:
        sockets = clothing_effect_controls(material)
        if not sockets:
            continue
        dependencies = shader_dependencies(material)
        for socket in sockets:
            if (socket.id_data.as_pointer(), socket.node.as_pointer()) not in dependencies.nodes:
                raise AssertionError(f'Panel exposes inactive effect evidence: {material.name}/{socket.node.name}')
            if socket.default_value != 1.:
                raise AssertionError(f'Fresh source effect must start at one: {material.name}')
            controls[socket.as_pointer()] = socket
        rows.append({'material': material.name, 'controls': len(sockets),
                     'simplified': bool(material.get('endfield_simple_material'))})
    if not controls:
        raise AssertionError('Character has no live source clothing effect controls')
    uniform_report = verify_shader_uniforms(materials, tuple(scene.objects), scene)
    geometry = tuple((obj.name, len(obj.data.vertices), tuple(tuple(row) for row in obj.matrix_world))
                     for obj in scene.objects if obj.type == 'MESH')
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    scene.render.resolution_x = 600
    scene.render.resolution_y = 800
    scene.render.resolution_percentage = 100
    scene.eevee.taa_render_samples = 32
    scene.render.image_settings.file_format = 'PNG'
    scene.frame_set(1)
    for label, strength in (('disabled', 0.), ('enabled', 1.)):
        for socket in controls.values():
            socket.default_value = strength
        bpy.context.view_layer.update()
        scene.render.filepath = str(args.output / (label + '.png'))
        bpy.ops.render.render(write_still=True)
    before = pixels(args.output / 'disabled.png')
    after = pixels(args.output / 'enabled.png')
    delta = np.abs(after - before)
    if not np.isfinite(delta).all() or np.count_nonzero(delta > 1. / 255.) < 30:
        raise AssertionError('Clothing effect switch has no measurable rendered contribution')
    if geometry != tuple((obj.name, len(obj.data.vertices), tuple(tuple(row) for row in obj.matrix_world))
                         for obj in scene.objects if obj.type == 'MESH'):
        raise AssertionError('Clothing controls changed source geometry or transforms')
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise AssertionError('Render verification changed the delivery file')
    result = {'status': 'passed', 'source': str(source), 'source_sha256': source_hash,
              'materials': rows, 'unique_controls': len(controls), 'uniforms': uniform_report,
              'render_max_rgb_delta': float(delta.max()),
              'render_changed_rgb_components': int(np.count_nonzero(delta > 1. / 255.)),
              'geometry_unchanged': True, 'delivery_unchanged': True, 'scripts_disabled': True}
    (args.output / 'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:])
