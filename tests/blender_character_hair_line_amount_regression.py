"""Verify real Whiten Hair preserves zero line amount and rejects negatives."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from character_hair_lighting import HairShaderFrame, line_map_binding_from_material, source_parameters
from patch_character_eye_lighting import exact_source_record


def main(blend: Path, output: Path) -> None:
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False)
    frame = HairShaderFrame((-1., 0., 0.), (0., 0., 1.), (0., -1., 0.),
                            (0., 0., 1.), 'fbx_root_frame_proxy_unverified')
    groups = tuple(tree for tree in bpy.data.node_groups
                   if tree.get('source_shader') == 'HGRP/CharacterNPR_Hair' and tree.get('hair_lighting_patch'))
    if not groups:
        raise AssertionError('Saved real Whiten source has no integrated Hair groups')
    rows: list[dict[str, object]] = []
    for tree in groups:
        _, native = exact_source_record(tree)
        parameters = source_parameters(tree, frame, line_map_binding_from_material(native))
        if parameters.line_amount != 0.0 or not parameters.use_line_map:
            raise AssertionError(f'Whiten source zero line amount or LineMap selection changed: {tree.name}')
        periods = tuple(item for item in tree.nodes if item.label == 'HGRP source procedural line period')
        if len(periods) != 1 or periods[0].inputs[1].is_linked or periods[0].inputs[1].default_value != 0.0:
            raise AssertionError(f'Whiten shader graph failed to preserve the exact zero source line amount: {tree.name}')
        try:
            replace(parameters, line_amount=-0.1).validate()
        except ValueError as error:
            if str(error) != 'Source _LineAmount must be nonnegative':
                raise AssertionError(f'Negative line amount failed for an unexpected reason: {tree.name}, error={error}') from error
        else:
            raise AssertionError(f'Negative source line amount was accepted: {tree.name}')
        rows.append({'group': tree.name, 'line_amount': parameters.line_amount,
                     'source_zero_in_graph': True, 'negative_value_rejected': True,
                     'line_map_path_id': parameters.line_map_path_id})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'status': 'passed', 'source_blend': str(blend), 'groups': rows}, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'groups': len(rows), 'audit': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.blend, args.output)
