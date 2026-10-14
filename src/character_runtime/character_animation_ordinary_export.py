"""Replay explicit source-bound ordinary layers through the existing sampler."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from animator_generic_sampling import sample_auxiliary_layers
from build_auxiliary_candidate import main as build_candidate
from character_animation_goal_export import object_value, text_value, read_json, write_json
from character_animation_parameter_trace import (TraceSourceIdentity,
    export_parameter_trace, source_input_paths)
from animator_ordinary_plan import assess_ordinary_layers, all_source_ordinary_bindings
from character_animation_state_export import export_state_layer, read_state_scenario, validate_managed_state_parameters


@dataclass(frozen=True)
class OrdinarySourceIdentity:
    character_id: str
    avatar_cab: str
    avatar_path_id: int
    clip_source: str
    clip_path_id: int
    controller_cab: str
    controller_path_id: int


@dataclass(frozen=True)
class OrdinaryExportResult:
    source_local_frames: Path
    receipt_path: Path
    input_paths: tuple[Path, ...]
    output_paths: tuple[Path, ...]


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def resolve_input(parent: Path, value: object, context: str) -> Path:
    return (parent / text_value(value, context)).resolve(strict=True)


def validate_source_identity(expected: dict[str, object], actual: OrdinarySourceIdentity) -> None:
    for name, value in asdict(actual).items():
        supplied = expected[name]
        if type(supplied) is not type(value) or supplied != value:
            raise ValueError(f'Ordinary replay source mismatch: field={name}, expected={supplied!r}, actual={value!r}')


def verify_source_hashes(expected: dict[str, object], paths: dict[str, Path]) -> None:
    for name, path in paths.items():
        digest = text_value(expected[name], 'source_sha256.' + name)
        actual = sha256(path)
        if digest.casefold() != actual:
            raise ValueError(f'Ordinary replay source hash mismatch: field={name}, path={path}, expected={digest}, actual={actual}')


def report_progress(stage: str, fraction: float, message: str) -> None:
    print(json.dumps(dict(stage=stage, fraction=fraction, message=message)), flush=True)


def check_cli_running() -> None:
    """The owning export process controls cancellation of this CLI operation."""
    return None


def read_parameter_scenario(path: Path, selected_layers: object) -> dict[str, object]:
    """Require the same explicit source-layer selection in both replay inputs."""
    scenario = object_value(read_json(path, 'parameter scenario'), 'parameter scenario')
    if scenario['format'] != 'EndfieldManagedClothParameterScenario/1':
        raise ValueError('Expected EndfieldManagedClothParameterScenario/1')
    for values, name in ((selected_layers, 'replay.layer_binding_hashes'),
                         (scenario['layer_binding_hashes'], 'scenario.layer_binding_hashes')):
        if not isinstance(values, list) or not values or any(type(value) is not int or not 0 <= value <= 0xffffffff for value in values) or len(set(values)) != len(values):
            raise ValueError(f'Expected unique nonempty uint32 source layer selection: field={name}')
    if scenario['layer_binding_hashes'] != selected_layers:
        raise ValueError('Parameter scenario selected layers differ from ordinary replay selection')
    return scenario


def generate_parameters(root: Path, decoded_root: Path, source_frames: Path,
                        identity: OrdinarySourceIdentity, scenario: Path) -> dict[str, object]:
    """Authenticate this decode's source artifacts before invoking the existing producer."""
    paths = source_input_paths(decoded_root, source_frames)
    request_path = root / 'parameter_trace_request.json'
    write_json(request_path, {
        'format': 'EndfieldAnimationParameterTraceRequest/1',
        'decoded_root': str(decoded_root),
        'source_local_frames': str(source_frames),
        'scenario': str(scenario),
        'source_identity': asdict(TraceSourceIdentity(**asdict(identity))),
        'source_sha256': {name: sha256(path) for name, path in paths.items()},
        'output': str(root / 'parameter_trace'),
    })
    return export_parameter_trace(request_path)


def normalize_source_parameter_scenario(path: Path, selected_layers: list[int]) -> dict[str, object]:
    """Resolve an explicit all-candidate policy from this export's source graph."""
    scenario = object_value(read_json(path, 'parameter scenario'), 'parameter scenario')
    if scenario['format'] != 'EndfieldManagedClothParameterScenario/2':
        raise ValueError('Automatic source selection requires EndfieldManagedClothParameterScenario/2')
    policy = object_value(scenario['layer_selection'], 'scenario.layer_selection')
    if policy['policy'] != 'all_source_authored_ordinary_candidates':
        raise ValueError('Unsupported automatic source-layer scenario selection policy')
    if 'layer_binding_hashes' in scenario:
        raise ValueError('Automatic source-layer scenario must not contain a manual binding-hash subset')
    return {key: value for key, value in scenario.items() if key != 'layer_selection'} | {
        'format': 'EndfieldManagedClothParameterScenario/1', 'layer_binding_hashes': selected_layers}


def export_ordinary_layers(
    configuration: Path,
    identity: OrdinarySourceIdentity,
    decoded_root: Path,
    source_audit: Path,
    source_hierarchy: Path,
    source_local_frames: Path,
    dotnet: Path,
    unity_version: str,
    maximum_stored_frames: int,
) -> OrdinaryExportResult:
    """Apply an authenticated parameter scenario, not a reconstructed live Animator."""
    configuration_bytes = configuration.read_bytes()
    configuration_hash = hashlib.sha256(configuration_bytes).hexdigest()
    request = object_value(json.loads(configuration_bytes), 'ordinary replay')
    version = request['format']
    if version not in ('EndfieldOrdinaryLayerReplay/1', 'EndfieldOrdinaryLayerReplay/2', 'EndfieldOrdinaryLayerReplay/3', 'EndfieldOrdinaryLayerReplay/4'):
        raise ValueError('Expected EndfieldOrdinaryLayerReplay/1, /2, /3 or /4')
    validate_source_identity(object_value(request['source_identity'], 'source_identity'), identity)
    provenance = text_value(request['provenance'], 'ordinary replay provenance')
    parameter_input = 'parameters' if version == 'EndfieldOrdinaryLayerReplay/1' else 'parameter_scenario'
    parameter_path = resolve_input(configuration.parent, request[parameter_input], parameter_input)
    source_paths = {
        parameter_input: parameter_path,
        'source_audit': source_audit,
        'selected_avatar': decoded_root / 'avatar_parsed.json',
        'source_hierarchy': source_hierarchy,
        'source_local_frames': source_local_frames,
        'controller_type_tree': decoded_root / 'native/controller_type_tree.json',
        'controller_source_references': decoded_root / 'native/controller_source_references.json',
        'float_acl': decoded_root / 'native/float.acl',
    }
    state_scenario: Path | None = None
    if version == 'EndfieldOrdinaryLayerReplay/4':
        state_scenario = resolve_input(configuration.parent, request['state_scenario'], 'state_scenario')
        source_paths['state_scenario'] = state_scenario
    source_hashes = object_value(request['source_sha256'], 'source_sha256')
    verify_source_hashes(source_hashes, source_paths)
    layer_coverage: list[dict[str, object]] | None = None
    normalized_scenario: dict[str, object] | None = None
    if version in ('EndfieldOrdinaryLayerReplay/3', 'EndfieldOrdinaryLayerReplay/4'):
        policy = object_value(request['layer_selection'], 'replay.layer_selection')
        if policy['policy'] != 'all_source_authored_ordinary_candidates':
            raise ValueError('Unsupported automatic ordinary source selection policy')
        if 'layer_binding_hashes' in request:
            raise ValueError('Automatic ordinary replay must not contain a manual binding-hash subset')
        assessments = assess_ordinary_layers(
            object_value(read_json(source_paths['controller_type_tree'], 'controller tree'), 'controller tree'),
            object_value(read_json(source_paths['controller_source_references'], 'controller references'), 'controller references'))
        selected_layers = list(all_source_ordinary_bindings(assessments))
        layer_coverage = [{'index': row.index, 'binding_hash': row.binding_hash, 'name': row.name,
                           'status': row.status, 'reasons': list(row.reasons),
                           'write_defaults_serialized': row.write_defaults_serialized} for row in assessments]
        normalized_scenario = normalize_source_parameter_scenario(parameter_path, selected_layers)
        if state_scenario is not None:
            validate_managed_state_parameters(normalized_scenario, read_state_scenario(state_scenario),
                object_value(read_json(source_paths['controller_type_tree'], 'controller tree'), 'controller tree'))
    else:
        selected_layers = request['layer_binding_hashes']
    if version == 'EndfieldOrdinaryLayerReplay/2':
        read_parameter_scenario(parameter_path, selected_layers)
    infrastructure = object_value(request['sampling'], 'sampling')
    resolved = {
        name: str(resolve_input(configuration.parent, infrastructure[name], 'sampling.' + name))
        for name in ('repository_src', 'game_root', 'resource_database', 'cab_inventory', 'sampler')
    }
    cache_root = (configuration.parent / text_value(infrastructure['cache_root'], 'sampling.cache_root')).resolve()
    prepared = infrastructure['prepared_bundle_root']
    prepared_root = None if prepared is None else str(resolve_input(configuration.parent, prepared, 'sampling.prepared_bundle_root'))
    root = decoded_root / 'ordinary_layers'
    root.mkdir(parents=True, exist_ok=False)
    trace_receipt: dict[str, object] | None = None
    trace_inputs: tuple[Path, ...] = ()
    parameters = parameter_path
    if version in ('EndfieldOrdinaryLayerReplay/2', 'EndfieldOrdinaryLayerReplay/3', 'EndfieldOrdinaryLayerReplay/4'):
        effective_scenario = parameter_path
        if normalized_scenario is not None:
            effective_scenario = root / 'resolved_parameter_scenario.json'
            write_json(effective_scenario, normalized_scenario)
            read_parameter_scenario(effective_scenario, selected_layers)
        trace_receipt = generate_parameters(root, decoded_root, source_local_frames, identity, effective_scenario)
        parameters = Path(text_value(trace_receipt['parameters'], 'generated parameters')).resolve(strict=True)
        trace_hashes = object_value(trace_receipt['input_sha256'], 'trace input hashes')
        trace_inputs = tuple(Path(path) for path in trace_hashes if not Path(path).is_relative_to(root))
    parameter_hash = sha256(parameters)
    candidate_sources = source_paths | {'parameters': parameters}
    sampling_request = {
        'format': 'EndfieldGenericAuxiliarySamplingRequest/1',
        **resolved,
        'cache_root': str(cache_root),
        'prepared_bundle_root': prepared_root,
        **{name: str(source_paths[name]) for name in ('source_audit', 'selected_avatar', 'source_hierarchy', 'controller_type_tree', 'controller_source_references')},
        'dotnet': str(dotnet),
        'unity_version': unity_version,
        'maximum_stored_frames': maximum_stored_frames,
        'layer_binding_hashes': selected_layers,
        'controller_identity': {'cab': identity.controller_cab, 'path_id': identity.controller_path_id},
        'controller_input_sha256': {name: source_hashes[name] for name in ('controller_type_tree', 'controller_source_references')},
        'output': str(root / 'sampling'),
    }
    if state_scenario is not None:
        sampling_request |= {'format': 'EndfieldGenericAuxiliarySamplingRequest/2',
                             'state_scenario': str(state_scenario)}
    sampling_request_path = root / 'sampling_request.json'
    write_json(sampling_request_path, sampling_request)
    sampled = sample_auxiliary_layers(sampling_request_path, check_cli_running, report_progress)
    candidate_request = {
        'format': 'EndfieldOrdinarySourceLayersRequest/1',
        **{name: str(candidate_sources[name]) for name in ('controller_type_tree', 'controller_source_references', 'source_hierarchy', 'source_local_frames', 'parameters', 'selected_avatar')},
        'clip_samples_root': sampled['clip_samples_root'],
        'clip_execution_settings': sampled['clip_execution_settings'],
        'layer_binding_hashes': selected_layers,
        'output': str(root / 'candidate'),
    }
    candidate_request_path = root / 'candidate_request.json'
    write_json(candidate_request_path, candidate_request)
    build_candidate(candidate_request_path)
    candidate_receipt_path = root / 'candidate/receipt.json'
    candidate = object_value(read_json(candidate_receipt_path, 'ordinary candidate receipt'), 'ordinary candidate receipt')
    frames = root / 'candidate/source_local_frames.json'
    if candidate['output_sha256'] != sha256(frames):
        raise ValueError('Ordinary candidate frames disagree with the evaluator receipt')
    state_inputs: tuple[Path, ...] = ()
    if state_scenario is not None:
        frames = export_state_layer(root, state_scenario, source_paths, frames,
            Path(text_value(sampled['clip_samples_root'], 'sampled clips')).resolve(strict=True))
        state_inputs = (state_scenario, *(Path(__file__).with_name(name) for name in (
            'character_animation_state_export.py', 'build_native_state_clock_candidate.py',
            'animator_state_schedule.py', 'animator_state_clock.py', 'animator_state_pose.py')))
    verify_source_hashes(source_hashes, source_paths)
    if sha256(parameters) != parameter_hash:
        raise ValueError('Ordinary replay parameter bytes changed during evaluation')
    if trace_receipt is not None:
        for path, digest in object_value(trace_receipt['input_sha256'], 'trace input hashes').items():
            if sha256(Path(path)) != digest:
                raise ValueError(f'Automatic parameter trace source changed during ordinary evaluation: path={path}')
    if sha256(configuration) != configuration_hash:
        raise ValueError(f'Ordinary replay configuration changed during evaluation: path={configuration}')
    receipt_path = root / 'receipt.json'
    write_json(receipt_path, {
        'format': 'EndfieldOrdinaryLayerReplayReceipt/1',
        'status': 'explicit_source_scenario_applied',
        'source_identity': asdict(identity),
        'provenance': provenance,
        'configuration': str(configuration),
        'configuration_sha256': configuration_hash,
        'source_sha256': {name: sha256(path) for name, path in source_paths.items()},
        'request_format': version,
        'parameters': str(parameters),
        'parameters_sha256': parameter_hash,
        'parameter_trace_receipt': str(root / 'parameter_trace/receipt.json') if trace_receipt is not None else None,
        'parameter_trace_status': trace_receipt['status'] if trace_receipt is not None else 'prepared_trace_supplied',
        'input_source_local_frames': str(source_local_frames),
        'source_local_frames': str(frames),
        'source_local_frames_sha256': sha256(frames),
        'frames': candidate['frames'],
        'layer_binding_hashes': candidate['layer_binding_hashes'],
        'source_layer_coverage': layer_coverage,
        'source_layer_selection': request['layer_selection'] if version in ('EndfieldOrdinaryLayerReplay/3', 'EndfieldOrdinaryLayerReplay/4') else 'explicit_binding_hash_subset',
        'sampling_receipt': str(root / 'sampling/receipt.json'),
        'candidate_receipt': str(candidate_receipt_path),
        'state_candidate_receipt': str(root / 'state_candidate/receipt.json') if state_scenario is not None else None,
        'pre_state_source_local_frames': str(root / 'candidate/source_local_frames.json') if state_scenario is not None else None,
        'runtime_animator_reconstructed': False,
        'evaluation_order': 'decoded source -> existing source-goal solver -> source-ordered Direct layers' + (' -> source-ordered native leaf clock/transition layer' if state_scenario is not None else ''),
        'validation_boundary': 'This explicit offline order is not asserted to be native Animator/IK scheduling. Source scenario identity, exact input bytes, selected clips and frame clocks are verified; live runtime events, layer gates and WriteDefaults remain unresolved.',
    })
    return OrdinaryExportResult(
        frames,
        receipt_path,
        tuple(dict.fromkeys((configuration, parameter_path, Path(__file__), Path(__file__).with_name('animator_ordinary_plan.py'), Path(__file__).with_name('animator_generic_sampling.py'), Path(__file__).with_name('build_auxiliary_candidate.py'), *trace_inputs, *state_inputs))),
        tuple(sorted(path for path in root.rglob('*') if path.is_file())),
    )
