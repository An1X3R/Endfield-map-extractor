"""Check actual default-ramp and dither prop exports after saving and reopening."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy


def check_default_ramp(path: Path) -> int:
    bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False)
    nodes = [node for tree in bpy.data.node_groups for node in tree.nodes
             if node.get('source_default_evidence') ==
             'Null material texture pointer and exact Shader property default white']
    if not nodes or any(tuple(node.outputs[0].default_value) != (1, 1, 1, 1) for node in nodes):
        raise AssertionError('Verified default-white diffuse ramp is absent or changed')
    return len(nodes)


def check_dither(path: Path) -> int:
    bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False)
    groups = [tree for tree in bpy.data.node_groups
              if tree.get('source_shader') == 'HGRP/Effect/VFXDitherAlpha']
    if not groups:
        raise AssertionError('Source dither group did not survive saving')
    for tree in groups:
        uv = [node.uv_map for node in tree.nodes if node.type == 'UVMAP']
        if 'UV0' not in uv or 'UVMap' in uv:
            raise AssertionError(f'Dither sampler does not use imported UV0: {uv}')
        for width, height, percentage in ((512, 512, 100), (800, 600, 50)):
            scene = bpy.context.scene
            scene.render.resolution_x, scene.render.resolution_y = width, height
            scene.render.resolution_percentage = percentage
            scene.frame_set(scene.frame_current + 1)
            bpy.context.view_layer.update()
            for axis, size in (('X', width), ('Y', height)):
                node = tree.nodes[f'Dither Raster Resolution {axis}']
                if abs(node.outputs[0].default_value - size * percentage / 100) > 0.001:
                    raise AssertionError(f'Dither raster driver lost after grouping/save: {axis}')
    return len(groups)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--npr-blend', type=Path, required=True)
    parser.add_argument('--dither-blend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    report = {'status': 'passed', 'default_white_nodes': check_default_ramp(args.npr_blend),
              'dither_groups': check_dither(args.dither_blend), 'raster_resize_and_reopen': 'passed'}
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
