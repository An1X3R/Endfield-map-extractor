"""Compare restrained full-shell relief in both engines using a real saved character."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'src'), str(REPO / 'src/character_runtime')]
from audit_character_materials import source_geometry_digest
from blender_scene_materials import layout_shader_node_tree
from character_fur_appearance import FurSurfaceDetail, apply_fur_surface_detail, verify_fur_appearance
from character_fur_source_shell import verify_source_fur_shell


def packed_images() -> dict[str, str]:
    return {image.name: hashlib.sha256(image.packed_file.data).hexdigest()
            for image in bpy.data.images if image.packed_file is not None}


def render(path: Path, engine: str) -> np.ndarray:
    scene = bpy.context.scene
    scene.render.engine = engine
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(str(path), check_existing=False)
    pixels = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(pixels)
    bpy.data.images.remove(image)
    return pixels


parser = argparse.ArgumentParser()
parser.add_argument('--source', required=True, type=Path)
parser.add_argument('--output', required=True, type=Path)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
args.output.mkdir(exist_ok=False, parents=True)
bpy.ops.wm.open_mainfile(filepath=str(args.source), use_scripts=False, load_ui=False)
shell, = (obj for obj in bpy.data.objects if obj.get('fur_artist_appearance') and not obj.hide_render)
source = json.loads(shell['source_fur_shell'])
shell_name = shell.name
digest = source_geometry_digest((shell.name, source['source_mesh']))
images = packed_images()
tree = bpy.data.node_groups[source['source_group']]
emission = tree.nodes[source['special_vfx']['emission_node']]
emission_links = tuple((link.from_node.name, link.from_socket.name, link.to_socket.name)
                       for name in ('Color', 'Strength') for link in emission.inputs[name].links)
assert not any(obj.type == 'CURVES' and not obj.hide_render for obj in bpy.context.scene.objects)
scene = bpy.context.scene
scene.frame_set(6)
scene.render.resolution_x = 768
scene.render.resolution_y = 768
scene.cycles.samples = 24
scene.cycles.use_denoising = True
scene.cycles.transparent_max_bounces = 48
engines = ('BLENDER_EEVEE_NEXT', 'CYCLES')
before = {engine: render(args.output / f'before_{engine}.png', engine) for engine in engines}
detail = apply_fur_surface_detail(shell, FurSurfaceDetail(.35, .18, .0005))
layout_shader_node_tree(tree)
assert digest == source_geometry_digest((shell.name, source['source_mesh']))
assert images == packed_images()
assert emission_links == tuple((link.from_node.name, link.from_socket.name, link.to_socket.name)
                              for name in ('Color', 'Strength') for link in emission.inputs[name].links)
verify_fur_appearance(shell)
verify_source_fur_shell(source)
after = {engine: render(args.output / f'after_{engine}.png', engine) for engine in engines}
deltas = {engine: float(np.abs(before[engine] - after[engine]).mean()) for engine in engines}
assert all(0.000001 < value < .04 for value in deltas.values()), deltas
scene.frame_set(66)
animated = render(args.output / 'after_EEVEE_frame066.png', 'BLENDER_EEVEE_NEXT')
temporal_delta = float(np.abs(animated - after['BLENDER_EEVEE_NEXT']).max())
assert temporal_delta > 1 / 255, temporal_delta
scene.frame_set(6)
scene.render.engine = 'CYCLES'
scene.render.filepath = str(args.output / 'reopened_CYCLES.png')
bpy.context.preferences.filepaths.save_version = 0
output = args.output / '艾尔黛拉_完整材质_炭绒细化.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(output), compress=False)
bpy.ops.wm.open_mainfile(filepath=str(output), use_scripts=False, load_ui=False)
verify_fur_appearance(bpy.data.objects[shell_name])
verify_source_fur_shell(source)
assert images == packed_images()
assert digest == source_geometry_digest((shell_name, source['source_mesh']))
assert json.loads(bpy.data.objects[shell_name]['fur_surface_detail']) == detail
reopened = render(args.output / 'reopened_CYCLES.png', 'CYCLES')
reopen_error = float(np.abs(reopened - after['CYCLES']).mean())
assert reopen_error < .002, reopen_error
report = {'output': str(output), 'source': str(args.source), 'detail': detail,
          'material_mode': 'full', 'no_added_strands': True, 'geometry_unchanged': True,
          'packed_images_unchanged': True, 'emission_links_unchanged': True,
          'mean_render_changes': deltas, 'temporal_delta': temporal_delta,
          'reopened_cycles_mean_error': reopen_error, 'saved_engine': 'CYCLES'}
(args.output / 'receipt.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print('FUR_SURFACE_DETAIL_PASSED', json.dumps(report, ensure_ascii=True), flush=True)
