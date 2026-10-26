"""Verify saved character offsets tolerate layout frames and reject shader tampering."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Sequence

import bpy

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from character_vfx_vertex_offset import verify_offset_surface_bindings, verify_vertex_offsets
from character_windows_paths import extended_path


def _reject(receipt: Sequence[dict[str, object]], scene: bpy.types.Scene, label: str) -> None:
    try:
        verify_vertex_offsets(receipt, scene)
    except ValueError as error:
        if 'graph, source identity or enable state changed' not in str(error):
            raise
    else:
        raise AssertionError(f'Saved vertex-offset audit ignored {label}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    report = json.loads((args.build_dir / 'build_receipt.json').read_text(encoding='utf-8'))
    path = Path(report['output'])
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if original_hash != report['sha256']:
        raise AssertionError('Actual saved character differs from its build receipt')
    receipt = report['vertex_offsets']
    if not receipt:
        raise AssertionError('Actual saved character has no source-bound vertex offset')
    bpy.ops.wm.open_mainfile(filepath=extended_path(path), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    evidence = verify_vertex_offsets(receipt, scene)
    verify_offset_surface_bindings(receipt, scene)
    if evidence != report['vertex_offset_validation']:
        raise AssertionError('Saved multi-frame vertex-offset evidence differs from the build')
    frames = sum(node.type == 'FRAME' for row in receipt
                 for node in scene.objects[row['object']].modifiers[row['modifier']].node_group.nodes)
    if not frames:
        raise AssertionError('Actual saved fixture does not exercise post-build authoring layout frames')
    row = receipt[0]
    modifier = scene.objects[row['object']].modifiers[row['modifier']]
    tree = modifier.node_group
    layout = tree.nodes.new('NodeFrame')
    layout.label = 'Regression layout frame without evaluation semantics'
    try:
        if verify_vertex_offsets(receipt, scene) != evidence:
            raise AssertionError('A socket-free layout frame changed offset evidence')
    finally:
        tree.nodes.remove(layout)
    checks = ['layout_frame_ignored']
    for attribute in ('show_render', 'show_viewport'):
        original = getattr(modifier, attribute)
        setattr(modifier, attribute, False)
        try:
            _reject(receipt, scene, attribute)
        finally:
            setattr(modifier, attribute, original)
        checks.append(attribute + '_tamper_rejected')
    original_identity = tree['source_identity']
    tree['source_identity'] = '["wrong",1]'
    try:
        _reject(receipt, scene, 'source identity')
    finally:
        tree['source_identity'] = original_identity
    checks.append('source_identity_tamper_rejected')
    extra = tree.nodes.new('ShaderNodeMath')
    try:
        _reject(receipt, scene, 'an additional computational node')
    finally:
        tree.nodes.remove(extra)
    checks.append('computational_node_tamper_rejected')
    target = next(socket for node in tree.nodes if node.type == 'MATH' for socket in node.inputs
                  if not socket.is_linked and socket.type == 'VALUE')
    original_value = target.default_value
    target.default_value += .125
    try:
        _reject(receipt, scene, 'computational constant')
    finally:
        target.default_value = original_value
    checks.append('computational_constant_tamper_rejected')
    if verify_vertex_offsets(receipt, scene) != evidence:
        raise AssertionError('Regression failed to restore actual source offset evidence')
    if hashlib.sha256(path.read_bytes()).hexdigest() != original_hash:
        raise AssertionError('Regression changed the input character blend')
    result = {'status': 'passed', 'rendered': False, 'saved_blend_modified': False,
              'source_blend': str(path), 'layout_frames': frames, 'checks': checks,
              'real_source_evidence': evidence}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('CHARACTER_VERTEX_OFFSET_REOPEN_REGRESSION_PASSED', len(receipt), flush=True)


if __name__ == '__main__':
    main()
