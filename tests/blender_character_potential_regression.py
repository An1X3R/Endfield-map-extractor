"""Validate actual potential-overlay deliveries without automatic script execution."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from character_runtime.character_potential_controls import PROPERTY, STATE, install_overlay_control, resolve_renderers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blends', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    results: list[dict[str, object]] = []
    for path in args.blends:
        bpy.ops.wm.open_mainfile(filepath=str(path.resolve(strict=True)), load_ui=False, use_scripts=False)
        scene = bpy.context.scene
        owners = tuple(obj for obj in scene.objects if STATE in obj)
        if len(owners) != 1:
            raise AssertionError(f'Expected one appearance control in {path}, found {len(owners)}')
        owner = owners[0]
        state = json.loads(owner[STATE])
        if state['format'] != 'EndfieldPotentialOverlay/1':
            raise AssertionError(f'Unexpected overlay state in {path}')
        identities = tuple(state['renderers'])
        objects = resolve_renderers(scene, identities)
        unrelated = {obj.name: (obj.hide_render, obj.hide_viewport, obj.hide_get()) for obj in scene.objects
                     if obj not in objects and 'potential_mount_source' not in obj}
        controlled = (*objects, *(obj for obj in scene.objects if 'potential_mount_source' in obj))
        for value in (False, True, False, True):
            owner[PROPERTY] = value
            owner.update_tag()
            scene.frame_set(scene.frame_current)
            bpy.context.view_layer.update()
            for obj in controlled:
                if obj.hide_render != (not value) or obj.hide_viewport != (not value) or obj.hide_get():
                    raise AssertionError(f'Visibility mismatch: file={path}, object={obj.name}, enabled={value}')
            for name, visibility in unrelated.items():
                obj = scene.objects[name]
                if visibility != (obj.hide_render, obj.hide_viewport, obj.hide_get()):
                    raise AssertionError(f'Unrelated visibility changed: file={path}, object={name}')
        try:
            install_overlay_control(scene, owner, identities)
        except ValueError as error:
            if 'already installed' not in str(error):
                raise
        else:
            raise AssertionError('Existing control was overwritten')
        scene.render.engine = 'CYCLES'
        scene.cycles.samples = 16
        scene.render.resolution_percentage = 50
        scene.render.filepath = str(args.output.parent / (path.stem + '_cycles.png'))
        bpy.ops.render.render(write_still=True)
        results.append({'file': str(path), 'overlay_renderers': len(objects),
                        'display_objects': len(controlled)-len(objects),
                        'reopened_without_autoexec': True, 'visibility_roundtrip': True,
                        'unrelated_visibility_preserved': True, 'cycles_render': scene.render.filepath})
    args.output.write_text(json.dumps({'status': 'passed', 'samples': results}, indent=2), encoding='utf-8')
    print('POTENTIAL_REOPEN_REGRESSION_PASSED', len(results), flush=True)


if __name__ == '__main__':
    main()
