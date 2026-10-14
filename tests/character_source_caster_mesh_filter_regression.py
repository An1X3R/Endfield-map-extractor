"""Verify actual static-renderer MeshFilter evidence and reject altered source references."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from source_caster_manifest import SourceIdentity, _array, _identity, _mapping, build_source_caster_manifest


@dataclass(frozen=True, slots=True)
class AlteredReference:
    path: tuple[str, ...]
    value: object
    reason: str


def altered_visibility(document: dict[str, object], identity: SourceIdentity, case: AlteredReference) -> dict[str, object]:
    result = deepcopy(document)
    matches = tuple(_mapping(value, 'renderer') for value in _array(result.get('renderers'), 'renderers')
                    if _identity(_mapping(value, 'renderer').get('renderer'), 'renderer.renderer') == identity)
    if len(matches) != 1:
        raise AssertionError('Expected exactly one source renderer for altered evidence')
    target = matches[0]
    for field in case.path[:-1]:
        target = _mapping(target.get(field), field)
    target[case.path[-1]] = case.value
    return result


def main(source_audit: Path, visibility: Path, shader_identities: Path, material_sources: Path, output: Path) -> None:
    manifest = build_source_caster_manifest(source_audit, visibility, shader_identities, material_sources)
    static_rows = tuple(row for row in manifest.renderers if row.renderer_type == 'MeshRenderer')
    if not static_rows:
        raise AssertionError('Actual evidence has no static source renderer')
    if any(reason.startswith(('mesh_filter_', 'renderer_mesh_')) for row in static_rows for reason in row.reasons):
        raise AssertionError('Actual source MeshFilter cross-check failed')
    selected = static_rows[0]
    document = _mapping(json.loads(visibility.read_text(encoding='utf-8-sig')), 'visibility')
    cases = (
        AlteredReference(('mesh_filter', 'mesh', 'resolved', 'source'), 'CAB-reference-deliberately-altered', 'mesh_filter_mesh_identity_mismatch'),
        AlteredReference(('mesh_filter', 'type_tree', 'data', 'm_Mesh', 'm_FileID'), -1, 'mesh_filter_mesh_file_id_mismatch'),
        AlteredReference(('mesh_filter', 'type_tree', 'data', 'm_Mesh', 'm_PathID'), 0, 'mesh_filter_mesh_path_id_mismatch'),
        AlteredReference(('mesh_filter', 'game_object', 'resolved', 'path_id'), 0, 'mesh_filter_game_object_identity_mismatch'),
        AlteredReference(('mesh_filter', 'type_tree', 'data', 'm_GameObject', 'm_FileID'), -1, 'mesh_filter_game_object_file_id_mismatch'),
        AlteredReference(('mesh_filter', 'type_tree', 'data', 'm_GameObject', 'm_PathID'), 0, 'mesh_filter_game_object_path_id_mismatch'),
        AlteredReference(('mesh_filter', 'type_tree', 'available'), False, 'mesh_filter_type_tree_unavailable'),
    )
    work = output.with_suffix('.evidence')
    work.mkdir(parents=True, exist_ok=False)
    for index, case in enumerate(cases):
        changed = work / f'altered_{index}.json'
        changed.write_text(json.dumps(altered_visibility(document, selected.identity, case)), encoding='utf-8')
        altered = build_source_caster_manifest(source_audit, changed, shader_identities, material_sources)
        row = next(row for row in altered.renderers if row.identity == selected.identity)
        if row.decision != 'excluded' or case.reason not in row.reasons or row.identity.key in altered.selection.accepted_identities:
            raise AssertionError(f'Altered source reference was not rejected: {case.reason}, actual={row.reasons}')
    missing = work / 'missing_mesh_filter.json'
    missing.write_text(json.dumps(altered_visibility(document, selected.identity, AlteredReference(('mesh_filter',), None, ''))), encoding='utf-8')
    try:
        build_source_caster_manifest(source_audit, missing, shader_identities, material_sources)
    except ValueError as error:
        if '.mesh_filter must be a JSON object' not in str(error):
            raise AssertionError('Missing MeshFilter failed for an unrelated reason') from error
    else:
        raise AssertionError('Missing MeshFilter evidence was accepted')
    output.write_text(json.dumps({'status': 'passed', 'renderer_count': len(manifest.renderers),
                                 'static_renderer_count': len(static_rows), 'selected_renderer': selected.identity.key,
                                 'altered_references_rejected': [case.reason for case in cases],
                                 'missing_mesh_filter_rejected': True}, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'static_renderers': len(static_rows), 'negative_cases': len(cases) + 1, 'audit': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-audit', 'renderer-visibility', 'shader-identities', 'material-sources', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    main(args.source_audit, args.renderer_visibility, args.shader_identities, args.material_sources, args.output)
