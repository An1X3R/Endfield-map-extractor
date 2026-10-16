"""Exercise the native preview switch on a real animated character and both renderers."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/character_runtime'))

from character_viewport_preview import PROPERTY, attach_viewport_preview, verify_viewport_preview
from character_authoring_validation import character_invariants, shape_signatures


def render(output: Path, name: str, engine: str) -> tuple[float, ...]:
    scene = bpy.context.scene
    scene.frame_set(1)
    scene.render.engine = engine
    scene.render.resolution_x = 256
    scene.render.resolution_y = 256
    scene.render.resolution_percentage = 100
    scene.eevee.taa_render_samples = 8
    scene.cycles.samples = 16
    scene.cycles.seed = 1
    scene.cycles.use_animated_seed = False
    scene.cycles.use_denoising = False
    scene.render.image_settings.file_format = 'PNG'
    scene.render.filepath = str(output / (name + '.png'))
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(scene.render.filepath, check_existing=False)
    pixels = tuple(image.pixels)
    bpy.data.images.remove(image)
    return pixels


def benchmark() -> float:
    scene = bpy.context.scene
    for frame in (2, 3, 4):
        scene.frame_set(frame)
    samples: list[float] = []
    for frame in range(7, 61, 5):
        started = time.perf_counter()
        scene.frame_set(frame)
        bpy.context.evaluated_depsgraph_get()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def main(source: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(source), use_scripts=False, load_ui=False)
    names = tuple(obj.name for obj in bpy.context.scene.objects if obj.type == 'MESH')
    scene = bpy.context.scene
    scene.frame_set(1)
    invariant = character_invariants(names)
    shapes = shape_signatures(names)
    original_modifiers = {(obj.name, mod.name): (mod.show_viewport, mod.show_render)
                          for obj in scene.objects for mod in obj.modifiers}
    engines = ('BLENDER_EEVEE_NEXT', 'CYCLES')
    reference = {engine: render(output, 'original_' + engine, engine) for engine in engines}
    rig, = (obj for obj in scene.objects if obj.type == 'ARMATURE')
    report = attach_viewport_preview(rig)
    if not report['modifiers']:
        raise AssertionError('Fixture has no native preview modifiers to exercise')
    if attach_viewport_preview(rig) != report:
        raise AssertionError('Attaching the preview switch is not idempotent')
    target = output / 'character_editable.blend'
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    scene.render.resolution_x, scene.render.resolution_y = 480, 720
    rig[PROPERTY] = False
    rig.update_tag()
    bpy.context.view_layer.update()
    bpy.ops.object.select_all(action='DESELECT')
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'PROPERTIES':
                area.spaces.active.context = 'OBJECT'
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    rig = bpy.data.objects[report['armature']]
    if bool(rig[PROPERTY]):
        raise AssertionError('Saved editing mode was not preserved')
    checks = verify_viewport_preview(report)
    timings: dict[str, float] = {}
    differences: dict[str, float] = {}
    for mode in (True, False, True):
        rig[PROPERTY] = mode
        rig.update_tag()
        bpy.context.view_layer.update()
        verify_viewport_preview(report)
        timings[str(mode)] = benchmark()
        bpy.context.scene.frame_set(1)
        if character_invariants(names) != invariant or shape_signatures(names) != shapes:
            raise AssertionError('Viewport mode changed source geometry, skin, rest pose, shapes or materials')
        owned = {(row['object'], row['modifier']) for row in report['modifiers']}
        for obj in bpy.context.scene.objects:
            for mod in obj.modifiers:
                expected = original_modifiers[(obj.name, mod.name)]
                if mod.show_render != expected[1] or ((obj.name, mod.name) not in owned and mod.show_viewport != expected[0]):
                    raise AssertionError(f'Preview switch changed an unrelated modifier: {obj.name}/{mod.name}')
        if str(mode) + '_CYCLES' in differences:
            continue
        for engine in engines:
            pixels = render(output, str(mode) + '_' + engine, engine)
            difference = max(abs(a-b) for a, b in zip(reference[engine], pixels, strict=True))
            differences[str(mode) + '_' + engine] = difference
            if difference > 1. / 255.:
                raise AssertionError(f'Viewport switch changed a render: mode={mode}, engine={engine}, max={difference}')
    if hashlib.sha256(source.read_bytes()).hexdigest() != original_hash:
        raise AssertionError('Input sample was modified')
    result = {'status': 'passed', 'preview': report, 'reopen': checks,
              'scripts_enabled_on_reopen': False, 'frame_evaluation_median_seconds': timings,
              'render_max_pixel_difference': differences, 'blend': str(target)}
    (output / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.source.resolve(strict=True), args.output.resolve())
