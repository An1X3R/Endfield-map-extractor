"""Materialize a pinned server replay only after fresh exact source verification."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from character_animation_goal_export import object_value, text_value, write_json
from character_animation_ordinary_export import OrdinarySourceIdentity, sha256, validate_source_identity


@dataclass(frozen=True)
class ReplayTemplateResult:
    configuration: Path
    input_paths: tuple[Path, ...]


def materialize_ordinary_replay(template: Path, identity: OrdinarySourceIdentity,
                               decoded_root: Path, source_audit: Path,
                               hierarchy: Path, frames: Path) -> ReplayTemplateResult:
    """Pin scenarios while hashing only the authenticated outputs of this decode."""
    template_bytes = template.read_bytes()
    request = object_value(json.loads(template_bytes), 'ordinary replay template')
    if request['format'] != 'EndfieldOrdinaryLayerReplayTemplate/1':
        raise ValueError('Expected EndfieldOrdinaryLayerReplayTemplate/1')
    validate_source_identity(object_value(request['source_identity'], 'template.source_identity'), identity)
    snapshots = object_value(request['snapshot_input_sha256'], 'template.snapshot_input_sha256')
    inputs: list[Path] = [template, Path(__file__)]
    for name, expected in snapshots.items():
        path = (template.parent / name).resolve(strict=True)
        if sha256(path) != expected:
            raise ValueError(f'Pinned server replay input changed before fresh materialization: path={path}')
        inputs.append(path)
    paths = {'source_audit': source_audit, 'selected_avatar': decoded_root / 'avatar_parsed.json',
        'source_hierarchy': hierarchy, 'source_local_frames': frames,
        'controller_type_tree': decoded_root / 'native/controller_type_tree.json',
        'controller_source_references': decoded_root / 'native/controller_source_references.json',
        'float_acl': decoded_root / 'native/float.acl'}
    expected_scenarios = object_value(request['scenario_sha256'], 'template.scenario_sha256')
    for name in ('parameter_scenario', 'state_scenario'):
        path = (template.parent / text_value(request[name], 'template.' + name)).resolve(strict=True)
        if sha256(path) != expected_scenarios[name] or snapshots.get(str(path)) != expected_scenarios[name]:
            raise ValueError(f'Server scenario has no matching immutable pinned source hash: field={name}')
        paths[name] = path
    configuration = decoded_root / 'resolved_ordinary_replay.json'
    write_json(configuration, {'format': 'EndfieldOrdinaryLayerReplay/4',
        'source_identity': request['source_identity'], 'provenance': text_value(request['provenance'], 'template.provenance'),
        'layer_selection': {'policy': 'all_source_authored_ordinary_candidates'},
        'parameter_scenario': str(paths['parameter_scenario']), 'state_scenario': str(paths['state_scenario']),
        'sampling': object_value(request['sampling'], 'template.sampling'),
        'source_sha256': {name: sha256(path) for name, path in paths.items()}})
    if hashlib.sha256(template.read_bytes()).hexdigest() != hashlib.sha256(template_bytes).hexdigest():
        raise ValueError('Server replay template changed during fresh materialization')
    return ReplayTemplateResult(configuration, tuple(dict.fromkeys(inputs)))
