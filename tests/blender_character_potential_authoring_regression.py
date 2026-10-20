"""Verify a real authoring helper contract without weakening source visibility checks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
               str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from character_runtime.character_potential_build import verify_potential_appearance


def rejects(report: dict[str, object], message: str) -> None:
    try:
        verify_potential_appearance(report, bpy.context.scene)
    except ValueError as error:
        if message not in str(error):
            raise
    else:
        raise AssertionError(f'Potential audit accepted tampering: {message}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    bpy.ops.wm.open_mainfile(filepath=str(args.blend), use_scripts=False, load_ui=False)
    owners = tuple(obj for obj in bpy.context.scene.objects if 'endfield_potential_build_report' in obj)
    if len(owners) != 1:
        raise AssertionError('Regression requires one real installed potential controller')
    owner = owners[0]
    report = json.loads(owner['endfield_potential_build_report'])
    verification = verify_potential_appearance(report, bpy.context.scene)
    if not owner.hide_get():
        raise AssertionError('Regression requires the actual hidden authoring helper state')
    sources = tuple(bpy.context.scene.objects[name] for name in report['unrelated_visibility']
                    if bpy.context.scene.objects[name].type == 'MESH'
                    and bpy.context.scene.objects[name].get('source_geometry_path') is not None)
    if not sources:
        raise AssertionError('Regression requires a real unrelated source renderer')
    source = sources[0]
    original = source.hide_render
    source.hide_render = not original
    try:
        rejects(report, 'unrelated source object visibility')
    finally:
        source.hide_render = original
    parent = owner.parent
    owner.parent = None
    try:
        rejects(report, 'invalid authoring helper identity')
    finally:
        owner.parent = parent
    original = owner.hide_render
    owner.hide_render = not original
    try:
        rejects(report, 'unrelated source object visibility')
    finally:
        owner.hide_render = original
    verify_potential_appearance(report, bpy.context.scene)
    args.output.write_text(json.dumps({'status': 'passed', 'verification': verification,
        'source_visibility_tamper_rejected': source.name, 'helper_parent_tamper_rejected': True,
        'helper_render_visibility_tamper_rejected': True, 'original_states_restored': True}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
