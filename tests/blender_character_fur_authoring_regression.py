"""Verify relocated Fur weather consumers and reject disconnected real materials."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'src' / 'character_runtime'))

from audit_built_character import object_value, string_value
from character_fur_curve_weather import verify_fur_curve_snow
from character_windows_paths import extended_path


def main(build_receipt: Path, output: Path) -> None:
    receipt = object_value(json.loads(build_receipt.read_text(encoding='utf-8')), 'build receipt')
    bpy.ops.wm.open_mainfile(filepath=extended_path(Path(string_value(receipt['output'], 'blend'))), use_scripts=False)
    prepared = object_value(receipt['fur_curves_snow'], 'prepared Fur')
    entries = prepared['entries']
    if not isinstance(entries, list) or not entries:
        raise ValueError('Regression requires actual exported Fur weather entries')
    checked = 0
    for entry in entries:
        weather = object_value(object_value(entry, 'Fur entry')['curve_weather'], 'Fur weather')
        verify_fur_curve_snow(weather)
        tree = bpy.data.materials[string_value(weather['material'], 'material')].node_tree
        instance = tree.nodes[string_value(weather['instance'], 'weather instance')]
        if not instance.get('endfield_authoring_parameters'):
            raise AssertionError('Regression requires a real repackaged source weather instance')
        surface = tree.nodes[string_value(weather['surface'], 'surface')]
        wrong_source = tree.nodes.new('ShaderNodeValue')
        wrong_source.outputs[0].default_value = 0.123
        for label in ('Base Color', 'Roughness', 'Metallic'):
            link, = surface.inputs[label].links
            original = link.from_socket
            tree.links.new(wrong_source.outputs[0], surface.inputs[label])
            try:
                try:
                    verify_fur_curve_snow(weather)
                except AssertionError as error:
                    if str(error) != 'Saved Fur snow live consumer is disconnected: ' + label:
                        raise
                else:
                    raise AssertionError(f'Disconnected Fur consumer was accepted: {label}')
            finally:
                tree.links.new(original, surface.inputs[label])
            verify_fur_curve_snow(weather)
            checked += 1
        tree.nodes.remove(wrong_source)
    output.write_text(json.dumps({'status': 'passed', 'disconnected_consumers_rejected': checked,
                                  'source_blend_saved': False}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-receipt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.build_receipt, args.output)
