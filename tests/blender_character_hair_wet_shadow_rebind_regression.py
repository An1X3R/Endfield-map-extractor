"""Check saved Hair wet links after the real live-shadow producer replacement."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'src' / 'character_runtime')]

from attach_character_weather_surface import linked_source
from audit_built_character import object_value, string_value
from character_hair_lighting import hair_cast_shadow_source
from character_hair_wet_profile import verify_hair_wet_before_snow
from patch_character_hgrp import by_label
from review_character_skin_lights import refresh


def main(build_receipt: Path, output: Path) -> None:
    """Use an actual saved export and reject a stale shared-control producer."""
    document = object_value(json.loads(build_receipt.read_text(encoding='utf-8')), 'build receipt')
    blend = Path(string_value(document['output'], 'built blend')).resolve(strict=True)
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False)
    refresh()
    profile = object_value(document['hair_fur_snow'], 'Hair/Fur receipt')
    bindings = profile['bindings']
    if not isinstance(bindings, list):
        raise TypeError('Hair/Fur bindings must be an array')
    checked = []
    for raw in bindings:
        row = object_value(raw, 'Hair/Fur binding')
        if row['family'] != 'HGRP/CharacterNPR_Hair':
            continue
        tree = bpy.data.node_groups[string_value(row['group'], 'Hair source group')]
        receipt = object_value(row['wet_before_snow'], 'Hair wet receipt')
        verify_hair_wet_before_snow(tree, receipt)
        target = by_label(tree, 'Hair wet view factor').inputs[1]
        live = hair_cast_shadow_source(tree)
        stale = by_label(tree, 'Explicit HGRP state and preview inputs').outputs['Cast Shadow']
        if linked_source(target) != live or live == stale or live.node.type != 'ATTRIBUTE':
            raise AssertionError('Regression requires the real rebound geometry-attribute producer')
        tree.links.new(stale, target)
        try:
            verify_hair_wet_before_snow(tree, receipt)
        except AssertionError as error:
            if str(error) != 'Saved Hair wet live link differs: Hair wet view factor/Value':
                raise
        else:
            raise AssertionError('Hair wet validation accepted the stale cast producer')
        finally:
            tree.links.new(live, target)
        verify_hair_wet_before_snow(tree, receipt)
        checked.append(tree.name)
    if not checked:
        raise AssertionError('Actual export has no compiled Hair wet bindings')
    output.write_text(json.dumps({'status': 'passed', 'blend': str(blend), 'groups': checked,
        'rebound_live_links_verified': len(checked), 'stale_producers_rejected': len(checked)}, indent=2),
        encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-receipt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.build_receipt, args.output)
