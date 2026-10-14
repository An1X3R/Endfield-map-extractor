"""Bind a server-pinned direct-limb or Grounder scenario to an authenticated fresh pose."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from character_animation_finalik_export import FinalIKSourceIdentity
from character_animation_goal_export import file_hash, object_value, text_value, write_json


@dataclass(frozen=True)
class FinalIKTemplateResult:
    configuration: Path
    input_paths: tuple[Path, ...]


def materialize_finalik_replay(template: Path, identity: FinalIKSourceIdentity, decoded_root: Path,
                               source_audit: Path, hierarchy: Path, frames: Path) -> FinalIKTemplateResult:
    payload = template.read_bytes()
    row = object_value(json.loads(payload), 'FinalIK template')
    version = row['format']
    if version not in ('EndfieldFinalIKReplayTemplate/1', 'EndfieldFinalIKReplayTemplate/2'):
        raise ValueError('Expected EndfieldFinalIKReplayTemplate/1 or /2')
    expected = object_value(row['source_identity'], 'FinalIK template identity')
    for key, actual in asdict(identity).items():
        if type(expected[key]) is not type(actual) or expected[key] != actual:
            raise ValueError('Configured FinalIK source identity differs from fresh decode: ' + key)
    if file_hash(frames) != row['expected_input_source_local_frames_sha256']:
        raise ValueError('Fresh pre-FinalIK pose differs from the configured expected input SHA256')
    pins = object_value(row['snapshot_input_sha256'], 'FinalIK snapshot inputs')
    inputs: list[Path] = [template, Path(__file__)]
    for name, digest in pins.items():
        path = (template.parent / name).resolve(strict=True)
        if file_hash(path) != digest:
            raise ValueError('FinalIK server configuration or explicit scenario changed: ' + name)
        inputs.append(path)
    paths = {'source_audit': source_audit, 'selected_avatar': decoded_root / 'avatar_parsed.json',
        'source_hierarchy': hierarchy, 'input_source_local_frames': frames,
        'frame_domain_verification': decoded_root / 'humanoid/merged/frame_domain_verification.json'}
    for key in ('scenario', 'source_component_inventory'):
        path = (template.parent / text_value(row[key], key)).resolve(strict=True)
        if pins.get(str(path)) != file_hash(path):
            raise ValueError('FinalIK scenario/source inventory has no matching pinned SHA256: ' + key)
        paths[key] = path
    configuration = decoded_root / 'resolved_finalik_replay.json'
    scenario = object_value(json.loads(paths['scenario'].read_bytes()), 'FinalIK template scenario')
    scenario_format = ('EndfieldFinalIKScenario/1' if version == 'EndfieldFinalIKReplayTemplate/1'
                       else 'EndfieldGrounderScenario/1')
    if scenario['format'] != scenario_format:
        raise ValueError('FinalIK template requires ' + scenario_format)
    replay_format = ('EndfieldFinalIKReplay/1' if version == 'EndfieldFinalIKReplayTemplate/1'
                     else 'EndfieldFinalIKReplay/2')
    write_json(configuration, {'format': replay_format, 'source_identity': expected,
        'scenario': str(paths['scenario']), 'source_component_inventory': str(paths['source_component_inventory']),
        'provenance': text_value(row['provenance'], 'FinalIK provenance'),
        'source_sha256': {key: file_hash(path) for key, path in paths.items()}})
    if template.read_bytes() != payload:
        raise ValueError('FinalIK template changed during fresh materialization')
    return FinalIKTemplateResult(configuration, tuple(dict.fromkeys(inputs)))
