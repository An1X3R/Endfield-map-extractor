"""Build and reopen a real source potential layer without touching the input blend."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
               str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from character_runtime.character_potential_build import install_potential_appearance, verify_potential_appearance
from character_runtime.character_potential_controls import PROPERTY
from character_runtime.character_effect_attachment import MARKER, validate_effect_attachment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    parser.add_argument('--renderer-selection', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    if args.output.exists():
        raise FileExistsError(args.output)
    before = hashlib.sha256(args.blend.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(args.blend.resolve()), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    rigs = tuple(obj for obj in scene.objects if obj.type == 'ARMATURE')
    if len(rigs) != 1:
        raise ValueError(f'Smoke test requires one character armature: count={len(rigs)}')
    owner = bpy.data.objects.new('Potential appearance', None)
    scene.collection.objects.link(owner)
    selection = json.loads(args.renderer_selection.read_text(encoding='utf-8-sig'))
    report = install_potential_appearance(scene, owner, rigs[0], selection, args.manifest, 'base')
    controlled = tuple(obj for obj in scene.objects if obj.get('potential_mount_source')
                       or obj.name in report['applied']['overlay_objects'])
    if not controlled or not any(obj.get('potential_mount_source') for obj in controlled):
        raise AssertionError(f'Real display source produced no mounted objects: report={report}')
    controlled_names = tuple(obj.name for obj in controlled)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output.resolve()))
    bpy.ops.wm.open_mainfile(filepath=str(args.output.resolve()), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    owner = scene.objects['Potential appearance']
    verification = verify_potential_appearance(report, scene)
    objects = tuple(scene.objects[name] for name in controlled_names)
    for enabled in (True, False, True, False):
        owner[PROPERTY] = enabled
        owner.update_tag()
        scene.frame_set(scene.frame_current)
        bpy.context.view_layer.update()
        if any(obj.hide_render != (not enabled) or obj.hide_viewport != (not enabled) for obj in objects):
            raise AssertionError('Saved potential overlay and display visibility drivers differ')
    for anchor in (obj for obj in scene.objects if MARKER in obj):
        rig = scene.objects[anchor['effect_attachment_rig']]
        snapshots = tuple(scene.objects[name] for name in json.loads(anchor['effect_snapshot_objects']))
        validate_effect_attachment(anchor, rig, snapshots)
    if before != hashlib.sha256(args.blend.read_bytes()).hexdigest():
        raise AssertionError('Input character blend changed')
    args.output.with_suffix('.json').write_text(json.dumps({'status': 'saved_reopened_visibility_and_mounts_verified',
        'controlled_objects': len(objects), 'verification': verification, 'report': report}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('POTENTIAL_BUILD_SMOKE_PASSED', len(objects), flush=True)


if __name__ == '__main__':
    main()
