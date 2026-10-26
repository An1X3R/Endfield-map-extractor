"""Exercise Cycles transparency setup on an actual exported layered Fur mesh."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'src' / 'character_runtime')]
from character_fur_source_shell import activate_source_fur_shell


def run(source: Path) -> None:
    bpy.ops.wm.open_mainfile(filepath=str(source.resolve(strict=True)), use_scripts=False)
    shells = tuple(obj for obj in bpy.context.scene.objects if obj.get('source_fur_shell'))
    if not shells:
        raise ValueError('Regression requires real exported source Fur shells')
    bpy.context.scene.cycles.transparent_max_bounces = 8
    for shell in shells:
        receipt = json.loads(shell['source_fur_shell'])
        active = activate_source_fur_shell({**receipt, 'status': 'built'})
        expected = 2 * receipt['layer_count'] + 8
        if bpy.context.scene.cycles.transparent_max_bounces < expected:
            raise AssertionError('Layered Fur retains an insufficient Cycles transparency budget')
        if active['cycles_transparent_bounces_required'] != expected:
            raise AssertionError('Fur receipt lost the layer-derived transparency requirement')
        bpy.context.scene.cycles.transparent_max_bounces = expected + 16
        activate_source_fur_shell({**active, 'status': 'built'})
        if bpy.context.scene.cycles.transparent_max_bounces != expected + 16:
            raise AssertionError('Fur activation reduced an existing artist transparency budget')
    print(json.dumps({'status': 'passed', 'actual_shells_checked': len(shells)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    run(args.source)
