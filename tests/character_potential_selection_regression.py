"""Compare real export decisions with an independent current-prefab inventory."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from character_runtime.character_potential_selection import potential_renderer_identities, select_potential_renderers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, action='append', required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text(encoding='utf-8-sig'))
    current = {row['character_id']: row for row in inventory['characters']}
    checks: list[dict[str, object]] = []
    for source in args.inputs:
        inputs = json.loads(source.read_text(encoding='utf-8-sig'))
        for case in inputs['selected']:
            path = Path(case['prepared_packet']['paths']['selection_report'])
            document = json.loads(path.read_text(encoding='utf-8-sig'))
            before = json.dumps(document, sort_keys=True)
            result = potential_renderer_identities(document)
            renderers = current[case['character_id']]['all_model_group_renderers']
            expected = {f"{row['identity']['cab']}:{row['identity']['path_id']}".casefold()
                        for row in renderers if '_vfxpart_' in row['name'] and row['lod_index'] == 0}
            if {identity.casefold() for identity in result} != expected:
                raise AssertionError(f'Cached selection differs from current exact prefab LODs: {case["character_id"]}')
            if json.dumps(document, sort_keys=True) != before:
                raise AssertionError('Selection mutated its source document')
            candidate = next(row for row in document['renderer_decisions'] if row['renderer_identity'] in result)
            for field, value in (('lod_index', True), ('native_shadows_only', 0), ('retained_in_scene', False)):
                changed = {**document, 'renderer_decisions': [
                    {**row, field: value} if row is candidate else row for row in document['renderer_decisions']]}
                try:
                    potential_renderer_identities(changed)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f'Invalid source selection was accepted: field={field}')
            try:
                potential_renderer_identities({**document, 'renderer_decisions': [*document['renderer_decisions'], candidate]})
            except ValueError:
                pass
            else:
                raise AssertionError('Duplicate source identity was accepted')
            checks.append({'character_id': case['character_id'], 'selection_report': str(path),
                           'selected_count': len(result), 'rows': [asdict(row) for row in select_potential_renderers(document)]})
    args.output.write_text(json.dumps({'status': 'passed', 'samples': checks}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('POTENTIAL_SOURCE_SELECTION_PASSED', len(checks), flush=True)


if __name__ == '__main__':
    main()
