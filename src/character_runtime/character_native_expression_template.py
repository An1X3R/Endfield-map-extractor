"""Authenticate fixed facial evidence before binding it to a fresh authored scene."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROLES = ('source_blend', 'source_build_receipt', 'source_audit', 'expression_table',
         'body_receipt', 'mesh_channel_probe', 'emotion_probe')
BODY_FIELDS = ('character_id', 'avatar_cab', 'avatar_path_id', 'clip_source', 'clip_path_id',
               'animator_root_path', 'resource_path')
AUDIT_FIELDS = ('requested_root', 'actual_root', 'source_containers', 'animators', 'transforms', 'renderers', 'meshes')


def record(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError('Expected object: ' + context)
    return value


def text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError('Expected nonempty text: ' + context)
    return value


def read(path: Path) -> dict[str, object]:
    return record(json.loads(path.read_text(encoding='utf-8-sig')), str(path))


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_inputs(snapshot: dict[str, object]) -> None:
    if snapshot['format'] != 'EndfieldCharacterNativeExpressionSnapshot/1':
        raise ValueError('Unsupported native expression snapshot')
    for name, expected in record(snapshot['input_sha256'], 'pinned inputs').items():
        if digest(Path(name)) != expected:
            raise ValueError('Native expression configuration or fixed evidence changed: ' + name)


def references(parent: Path, manifest: dict[str, object]) -> dict[str, Path]:
    sources = record(manifest['sources'], 'fixed expression sources')
    result: dict[str, Path] = {}
    for role in ROLES:
        ref = record(sources[role], role)
        path = (parent / text(ref['path'], role)).resolve(strict=True)
        if digest(path) != ref['sha256']:
            raise ValueError('Fixed expression evidence changed: ' + role)
        result[role] = path
    return result


def body_identity(row: dict[str, object]) -> dict[str, object]:
    if row['format'] != 'EndfieldAnimationExportReceipt/1' or row['status'] != 'decoded_bound_and_merged_candidate':
        raise ValueError('Expression replay requires a verified selected-Avatar body receipt')
    return {key: row[key] for key in BODY_FIELDS}


def verify_compatible_sources(old: dict[str, Path], fresh: dict[str, Path], expected: dict[str, object]) -> None:
    old_build, new_build = read(old['source_build_receipt']), read(fresh['source_build_receipt'])
    for sources, build in ((old, old_build), (fresh, new_build)):
        if (build['format'] != 'EndfieldCharacterAuthoring/1' or build['character_id'] != expected['character_id']
                or build['blend_sha256'] != digest(sources['source_blend'])
                or record(build['identity'], 'authoring identity')['source_audit_sha256'] != digest(sources['source_audit'])):
            raise ValueError('Expression template source build/blend/audit identity mismatch')
    if body_identity(read(old['body_receipt'])) != expected or body_identity(read(fresh['body_receipt'])) != expected:
        raise ValueError('Fresh body Avatar/clip/root identity differs from the configured expression template')
    old_audit, new_audit = read(old['source_audit']), read(fresh['source_audit'])
    for name in AUDIT_FIELDS:
        if old_audit[name] != new_audit[name]:
            raise ValueError('Fresh expression source differs from fixed audit evidence: ' + name)
    old_table, new_table = read(old['expression_table']), read(fresh['expression_table'])
    for name in ('schema', 'character_id', 'selected_animator_avatar', 'raw', 'avatar_node_map', 'avatar_bone_bindings'):
        if old_table[name] != new_table[name]:
            raise ValueError('Fresh facial table/Avatar bindings differ from fixed evidence: ' + name)
    for name in ('container_path', 'serialized_file', 'object_path_id', 'bundle_sha256', 'raw_object_sha256'):
        if record(old_table['source'], 'old table source')[name] != record(new_table['source'], 'fresh table source')[name]:
            raise ValueError('Fresh expression table source identity changed: ' + name)


def materialize_expression_authoring(template_path: Path, authoring_path: Path, loaded_blend: Path) -> Path:
    template = read(template_path)
    if template['format'] != 'EndfieldNativeExpressionReplayTemplate/1':
        raise ValueError('Unsupported expression replay template bridge')
    snapshot = record(template['snapshot'], 'expression snapshot')
    verify_inputs(snapshot)
    raw_fresh = record(template['fresh_sources'], 'fresh expression sources')
    fresh = {key: (template_path.parent / text(raw_fresh[key], key)).resolve(strict=True)
             for key in ('source_blend', 'source_build_receipt', 'source_audit', 'expression_table', 'body_receipt')}
    if fresh['source_blend'] != loaded_blend.resolve(strict=True):
        raise ValueError('Fresh expression template blend differs from the loaded scene')
    output = (template_path.parent / text(template['output'], 'expression output')).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifests = snapshot['templates']
    if not isinstance(manifests, list) or not manifests:
        raise ValueError('Expected explicit expression templates')
    materialized: list[str] = []
    for index, path_value in enumerate(manifests):
        source_path = Path(text(path_value, 'fixed template')).resolve(strict=True)
        source = read(source_path)
        old = references(source_path.parent, source)
        verify_compatible_sources(old, fresh, record(snapshot['source_identity'], 'expected source identity'))
        inputs = {**old, **fresh}
        manifest = {**source, 'armature': text(template['armature'], 'fresh armature'),
            'sources': {key: {'path': str(path), 'sha256': digest(path)} for key, path in inputs.items()},
            'output': str(output / ('replay_' + str(index)))}
        target = output / ('manifest_' + str(index) + '.json')
        target.write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding='utf-8')
        materialized.append(str(target))
    verify_inputs(snapshot)
    request = read(authoring_path)
    if request['character_id'] != record(snapshot['source_identity'], 'expected identity')['character_id']:
        raise ValueError('Fresh authoring character differs from the configured replay')
    target = output / 'authoring_request.json'
    target.write_text(json.dumps({**request, 'native_expression_replays': materialized}, indent=2, allow_nan=False), encoding='utf-8')
    receipt = {'format': 'EndfieldNativeExpressionTemplateReceipt/1', 'status': 'fixed_evidence_authenticated_fresh_sources_bound',
        'replay_id': snapshot['replay_id'], 'source_identity': snapshot['source_identity'], 'provenance': snapshot['provenance'],
        'fixed_input_sha256': snapshot['input_sha256'], 'fresh_input_sha256': {str(path): digest(path) for path in fresh.values()},
        'manifests': [{'path': name, 'sha256': digest(Path(name))} for name in materialized],
        'scope': 'Fixed facial pose/table evidence and compatible fresh body source; explicit offline snapshot weights, no live tracker.'}
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2, allow_nan=False), encoding='utf-8')
    return target
