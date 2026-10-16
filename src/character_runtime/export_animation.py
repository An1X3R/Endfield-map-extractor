"""Export one explicit real clip through existing ACL and Avatar solvers."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import cast

from character_animation_goal_export import export_source_goals
from character_animator_graph import export_controller_graph


@dataclass(frozen=True)
class AnimationRequest:
    format: str
    character_id: str
    avatar_bundle: str
    avatar_cab: str
    avatar_path_id: int
    clip_bundle: str
    resource_path: str
    source_audit: str
    animator_root_path: str
    controller_bundle: str | None
    controller_selection_receipt: str | None
    unity_version: str
    maximum_stored_frames: int
    output: str
    dotnet: str
    reader: str
    ordinary_layer_replay: str | None
    ordinary_layer_replay_template: str | None
    default_clothing_sampling: str | None
    finalik_replay: str | None
    finalik_replay_template: str | None


def require_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f'Expected JSON object: {context}')
    return cast(dict[str, object], value)


def read_request(path: Path) -> AnimationRequest:
    raw = require_object(json.loads(path.read_text(encoding='utf-8-sig')), 'animation request')
    values: dict[str, object] = {}
    for name in AnimationRequest.__dataclass_fields__:
        value = raw.get(name) if name in ('controller_selection_receipt', 'ordinary_layer_replay', 'ordinary_layer_replay_template', 'default_clothing_sampling', 'finalik_replay', 'finalik_replay_template') else raw[name]
        if name in ('controller_bundle', 'controller_selection_receipt', 'ordinary_layer_replay', 'ordinary_layer_replay_template', 'default_clothing_sampling', 'finalik_replay', 'finalik_replay_template') and value is None:
            values[name] = None
        elif name in ('avatar_path_id', 'maximum_stored_frames'):
            if type(value) is not int:
                raise TypeError(f'Expected integer: {name}')
            values[name] = value
        elif not isinstance(value, str) or not value:
            raise TypeError(f'Expected nonempty string: {name}')
        else:
            values[name] = value
    request = AnimationRequest(**values)
    path_fields = ('avatar_bundle', 'clip_bundle', 'source_audit', 'output', 'dotnet', 'reader')
    normalized = asdict(request)
    for name in path_fields:
        normalized[name] = str((path.parent / Path(getattr(request, name))).resolve())
    if request.controller_bundle is not None:
        normalized['controller_bundle'] = str((path.parent / Path(request.controller_bundle)).resolve())
    if request.controller_selection_receipt is not None:
        normalized['controller_selection_receipt'] = str((path.parent / Path(request.controller_selection_receipt)).resolve(strict=True))
    if request.ordinary_layer_replay is not None:
        normalized['ordinary_layer_replay'] = str((path.parent / Path(request.ordinary_layer_replay)).resolve())
    if request.ordinary_layer_replay_template is not None:
        normalized['ordinary_layer_replay_template'] = str((path.parent / Path(request.ordinary_layer_replay_template)).resolve())
    if request.default_clothing_sampling is not None:
        normalized['default_clothing_sampling'] = str((path.parent / Path(request.default_clothing_sampling)).resolve())
    if request.finalik_replay is not None:
        normalized['finalik_replay'] = str((path.parent / Path(request.finalik_replay)).resolve())
    if request.finalik_replay_template is not None:
        normalized['finalik_replay_template'] = str((path.parent / Path(request.finalik_replay_template)).resolve())
    request = AnimationRequest(**normalized)
    if request.format != 'EndfieldAnimationExportRequest/1' or request.maximum_stored_frames < 2 or request.avatar_path_id == 0:
        raise ValueError('Invalid animation request format/frame budget/Avatar PathID')
    for name in ('avatar_bundle', 'clip_bundle', 'source_audit', 'dotnet', 'reader'):
        if not Path(getattr(request, name)).is_file():
            raise FileNotFoundError(getattr(request, name))
    if request.controller_bundle is not None and not Path(request.controller_bundle).is_file():
        raise FileNotFoundError(request.controller_bundle)
    if request.controller_selection_receipt is not None and request.controller_bundle is None:
        raise ValueError('controller_selection_receipt requires its exact controller_bundle')
    if request.ordinary_layer_replay is not None:
        if request.controller_bundle is None:
            raise ValueError('ordinary_layer_replay requires an exact controller_bundle')
        if not Path(request.ordinary_layer_replay).is_file():
            raise FileNotFoundError(request.ordinary_layer_replay)
    if request.ordinary_layer_replay_template is not None:
        if request.ordinary_layer_replay is not None:
            raise ValueError('Select exactly one prepared ordinary replay or fresh server replay template')
        if request.controller_bundle is None:
            raise ValueError('ordinary_layer_replay_template requires an exact controller_bundle')
        if not Path(request.ordinary_layer_replay_template).is_file():
            raise FileNotFoundError(request.ordinary_layer_replay_template)
    if request.finalik_replay is not None and not Path(request.finalik_replay).is_file():
        raise FileNotFoundError(request.finalik_replay)
    if request.default_clothing_sampling is not None:
        if request.controller_bundle is None:
            raise ValueError('default_clothing_sampling requires an exact controller_bundle')
        if request.ordinary_layer_replay is not None or request.ordinary_layer_replay_template is not None:
            raise ValueError('Select either default clothing or an explicit ordinary replay')
        if not Path(request.default_clothing_sampling).is_file():
            raise FileNotFoundError(request.default_clothing_sampling)
    if request.finalik_replay_template is not None:
        if request.finalik_replay is not None:
            raise ValueError('Select exactly one fixed FinalIK replay or authenticated fresh template')
        if not Path(request.finalik_replay_template).is_file():
            raise FileNotFoundError(request.finalik_replay_template)
    return request


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def export_animation(request_path: Path) -> dict[str, object]:
    request = read_request(request_path)
    output = Path(request.output)
    output.mkdir(parents=True, exist_ok=False)
    temp = output / 'temp'
    temp.mkdir()
    normalized_request = output / 'request.json'
    normalized_request.write_text(json.dumps(asdict(request), indent=2), encoding='utf-8')
    environment = dict(os.environ)
    environment.update(TEMP=str(temp), TMP=str(temp), DOTNET_CLI_HOME=str(temp), DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1',
                       DOTNET_GENERATE_ASPNET_CERTIFICATE='false', DOTNET_CLI_TELEMETRY_OPTOUT='1')
    log = output / 'reader.log'
    command = (request.dotnet, request.reader, str(normalized_request))
    started = time.perf_counter()
    with log.open('wb') as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, env=environment, check=False)
    if result.returncode != 0:
        failure = {'format': 'EndfieldAnimationExportReceipt/1', 'status': 'failed', 'exit_code': result.returncode, 'log': str(log), 'command': command}
        (output / 'receipt.json').write_text(json.dumps(failure, indent=2), encoding='utf-8')
        raise RuntimeError(f'Animation reader failed with exit {result.returncode}; inspect {log}')
    merged = output / 'humanoid/merged'
    merged_receipt = require_object(json.loads((merged / 'receipt.json').read_text()), 'merged receipt')
    clip = require_object(json.loads((output / 'native/clip_audit.json').read_text()), 'clip audit')
    sample_header = require_object(json.loads((output / 'native/transform_header.json').read_text()), 'sample header')
    time_domain_path = merged / 'frame_domain_verification.json'
    time_domain = require_object(json.loads(time_domain_path.read_text(encoding='utf-8-sig')), 'frame-domain verification')
    hierarchy_path = merged / 'source_hierarchy.json'
    base_frames_path = merged / 'source_local_frames.json'
    base_unapplied_path = merged / 'unapplied_scalar_channels.json'
    goal_export = export_source_goals(output, request.character_id, request.avatar_cab, request.avatar_path_id,
                                      request.animator_root_path, hierarchy_path, base_frames_path,
                                      base_unapplied_path, time_domain_path)
    raw_hashes = require_object(time_domain['raw_acl_buffer_sha256'], 'raw ACL buffer hashes')
    raw_acl_paths = {'transform': output / 'native/transform.acl', 'root_motion': output / 'native/root_motion.acl', 'float': output / 'native/float.acl'}
    for buffer_name, path in raw_acl_paths.items():
        expected_hash = raw_hashes[buffer_name]
        if not isinstance(expected_hash, str) or file_hash(path).casefold() != expected_hash.casefold():
            raise RuntimeError(f'Raw ACL buffer changed after frame-domain verification: {buffer_name}')
    inputs = [Path(request.avatar_bundle), Path(request.clip_bundle), Path(request.source_audit), Path(request.reader), normalized_request]
    if request.controller_bundle is not None:
        inputs.append(Path(request.controller_bundle))
    if request.controller_selection_receipt is not None:
        inputs.append(Path(request.controller_selection_receipt))
    inputs.extend(goal_export.input_paths)
    parameter_verification = output / 'native/animator_parameter_verification.json'
    controller_graph = export_controller_graph(output / 'native')
    outputs = [hierarchy_path, base_frames_path, base_unapplied_path, parameter_verification,
               time_domain_path, *raw_acl_paths.values(), *goal_export.output_paths]
    if controller_graph is not None:
        outputs.extend((controller_graph, output / 'native/controller_type_tree.json', output / 'native/controller_source_references.json'))
        inputs.append(Path(__file__).with_name('character_animator_graph.py'))
    effective_frames = goal_export.source_local_frames
    ordinary_receipt: Path | None = None
    default_clothing_status: dict[str, object] | None = None
    if (request.ordinary_layer_replay is not None or request.ordinary_layer_replay_template is not None
            or request.default_clothing_sampling is not None):
        from character_animation_ordinary_export import OrdinarySourceIdentity, export_ordinary_layers
        if controller_graph is None:
            raise ValueError('Ordinary replay requires a decoded controller graph')
        controller_source = require_object(json.loads((output / 'native/controller_source_references.json').read_text(encoding='utf-8-sig')), 'controller source')
        identity = OrdinarySourceIdentity(request.character_id, request.avatar_cab, request.avatar_path_id,
                                          clip['source'], clip['path_id'], controller_source['source'], controller_source['controller_path_id'])
        ordinary_configuration = Path(request.ordinary_layer_replay) if request.ordinary_layer_replay is not None else None
        if request.ordinary_layer_replay_template is not None:
            from character_animation_replay_template import materialize_ordinary_replay
            materialized = materialize_ordinary_replay(Path(request.ordinary_layer_replay_template), identity,
                output, Path(request.source_audit), hierarchy_path, effective_frames)
            ordinary_configuration = materialized.configuration
            inputs.extend(materialized.input_paths)
            outputs.append(materialized.configuration)
        if request.default_clothing_sampling is not None:
            from character_animation_default_clothing import build_default_clothing_replay
            sampling_path = Path(request.default_clothing_sampling)
            sampling = require_object(json.loads(sampling_path.read_text(encoding='utf-8-sig')), 'default clothing sampling')
            generated = build_default_clothing_replay(output, Path(request.source_audit), effective_frames, identity, sampling)
            ordinary_configuration = generated.configuration_path
            default_clothing_status = {'status': generated.status, 'reason': generated.reason,
                                       'layer_binding_hashes': list(generated.layer_binding_hashes)}
            inputs.extend((sampling_path, *(Path(__file__).with_name(name) for name in (
                'character_animation_default_clothing.py', 'character_animation_parameter_trace.py',
                'character_cloth_curve_parameters.py'))))
            if generated.configuration_path is not None:
                outputs.extend((generated.configuration_path, generated.parameters_path))
        if ordinary_configuration is None and request.default_clothing_sampling is None:
            raise ValueError('Ordinary replay has no authenticated configuration')
        if ordinary_configuration is not None:
            ordinary = export_ordinary_layers(ordinary_configuration, identity, output,
                                              Path(request.source_audit), hierarchy_path, effective_frames,
                                              Path(request.dotnet), request.unity_version, request.maximum_stored_frames)
            effective_frames = ordinary.source_local_frames
            ordinary_receipt = ordinary.receipt_path
            inputs.extend(ordinary.input_paths)
            outputs.extend(ordinary.output_paths)
            if default_clothing_status is not None:
                default_clothing_status['status'] = 'applied'
    pre_finalik_frames = effective_frames
    finalik_receipt: Path | None = None
    if request.finalik_replay is not None or request.finalik_replay_template is not None:
        from character_animation_finalik_export import FinalIKSourceIdentity, export_finalik_replay
        finalik_identity = FinalIKSourceIdentity(request.character_id, request.avatar_cab, request.avatar_path_id,
                                                clip['source'], clip['path_id'], request.animator_root_path, request.resource_path)
        finalik_configuration = Path(request.finalik_replay) if request.finalik_replay is not None else None
        if request.finalik_replay_template is not None:
            from character_animation_finalik_template import materialize_finalik_replay
            materialized = materialize_finalik_replay(Path(request.finalik_replay_template), finalik_identity,
                output, Path(request.source_audit), hierarchy_path, effective_frames)
            finalik_configuration = materialized.configuration
            inputs.extend(materialized.input_paths)
            outputs.append(materialized.configuration)
        if finalik_configuration is None:
            raise ValueError('FinalIK replay has no authenticated configuration')
        finalik = export_finalik_replay(finalik_configuration, finalik_identity, output,
                                        Path(request.source_audit), hierarchy_path, effective_frames)
        effective_frames = finalik.source_local_frames
        finalik_receipt = finalik.receipt_path
        inputs.extend(finalik.input_paths)
        outputs.extend(finalik.output_paths)
    unique_inputs = list(dict.fromkeys(inputs))
    unique_outputs = list(dict.fromkeys(outputs))
    goal_boundary = ('The candidate uses exact selected-Avatar hand/foot goal tracks and HumanBoneMass slot routes. Animator translation and hips correction are constrained by both foot reach bounds; per-frame endpoint residuals and remaining mass-center error are recorded in the source-goal receipt. Native IK weights, joint limits, twist distribution, and game-runtime parity remain unverified.'
                     if goal_export.status.startswith('applied_') else
                     'No hand/foot goal channels were present; the merged source-local frames and unapplied scalar channel list remain unchanged.')
    receipt = {'format': 'EndfieldAnimationExportReceipt/1', 'status': 'decoded_bound_and_merged_candidate', 'character_id': request.character_id, 'resource_path': request.resource_path, 'clip_name': clip['m_name'], 'clip_source': clip['source'], 'clip_path_id': clip['path_id'], 'avatar_cab': request.avatar_cab, 'avatar_path_id': request.avatar_path_id, 'animator_root_path': request.animator_root_path, 'sample_rate': sample_header['sample_rate'], 'frames': merged_receipt['frames'], 'time_domain': time_domain, 'time_domain_verification': str(time_domain_path), 'hierarchy_nodes': merged_receipt['hierarchy_nodes'], 'source_hierarchy': str(hierarchy_path), 'base_source_local_frames': str(base_frames_path), 'source_local_frames': str(goal_export.source_local_frames), 'unapplied_channels': str(goal_export.unapplied_channels), 'source_goal_ik': str(goal_export.receipt_path), 'source_goal_ik_status': goal_export.status, 'merged_receipt': str(merged / 'receipt.json'), 'inputs': [{'path': str(path), 'sha256': file_hash(path)} for path in unique_inputs], 'outputs': [{'path': str(path), 'sha256': file_hash(path)} for path in unique_outputs], 'elapsed_seconds': time.perf_counter() - started, 'game_and_cached_inputs_read_only': True, 'command': command, 'log': str(log), 'validation_boundary': 'Actual stored clip frames and exact Avatar paths verified. ' + goal_boundary + ' Blender visual parity requires the final binder/sample audit.'}
    receipt['source_local_frames'] = str(effective_frames)
    if default_clothing_status is not None:
        receipt['default_clothing'] = default_clothing_status
    if ordinary_receipt is not None:
        receipt['source_goal_local_frames'] = str(goal_export.source_local_frames)
        receipt['ordinary_layer_replay'] = str(ordinary_receipt)
        receipt['validation_boundary'] += ' Explicit ordinary source layers were applied after source goals; this order does not reconstruct native runtime scheduling. See ordinary_layer_replay for the selected scenario and remaining limits.'
    if finalik_receipt is not None:
        receipt['pre_finalik_source_local_frames'] = str(pre_finalik_frames)
        receipt['finalik_replay'] = str(finalik_receipt)
        finalik_details = require_object(json.loads(finalik_receipt.read_text(encoding='utf-8')), 'FinalIK replay receipt')
        if finalik_details['status'] == 'explicit_grounder_two_leg_pose_replay_applied':
            receipt['validation_boundary'] += ' The explicit Grounder pelvis/two-leg/foot-post segment was applied after the effective source-goal/ordinary pose. Exact source references are snapshot-authenticated; ground query results, weights, availability and histories remain external scenario inputs. Grounding terrain/smoothing, spine/body rotation and native global scheduling were not inferred. See finalik_replay for the bounded consumer.'
        else:
            receipt['validation_boundary'] += ' Explicit Animation0/direct hierarchy FinalIK calls were applied after the effective source-goal/ordinary pose; terrain, live weights/history and native global scheduling were not inferred. See finalik_replay for snapshot identity and scenario inputs.'
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt))
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    arguments = parser.parse_args()
    export_animation(arguments.request)
