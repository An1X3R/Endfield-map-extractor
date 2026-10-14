"""Produce exact-source cloth controls with an explicit managed caller scenario.

Clip scalar tracks and controller defaults are discovered by their serialized
hashes/types. The caller supplies all runtime gates, Setup events and admission;
this module does not infer live gameplay state or engine evaluation timing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import Path
import re
import sys
import zlib

from character_animation_parameters import (_controller_names, _integer, _number,
    _record, _rows, _text)
from character_animator_graph import _names
from character_cloth_curve_parameters import (ClothCurveInputs, ClothRuntimeInputs,
    compute_cloth_parameters)
from animator_dynamic_lifecycle import DynamicFloatEvent, empty_dynamic_registry
from animator_blackboard_caller import (ManagedBlackboardTick,
    apply_managed_blackboard_tick, setup_managed_dynamic_registration)
from animator_dynamic_publication import merge_dynamic_publication
from animator_ordinary_plan import assess_ordinary_layers, select_ordinary_routes


@dataclass(frozen=True)
class TraceSourceIdentity:
    character_id: str
    avatar_cab: str
    avatar_path_id: int
    clip_source: str
    clip_path_id: int
    controller_cab: str
    controller_path_id: int


@dataclass(frozen=True)
class DeclaredParameter:
    name_hash: int
    name: str | None
    kind: int
    default_index: int
    default_value: float | int | bool


@dataclass(frozen=True)
class SourceFloatChannel:
    track: int
    binding: int
    name_hash: int
    name: str | None
    values: tuple[float, ...]


@dataclass(frozen=True)
class CurveSource:
    field: str
    name_hash: int
    scalar_track: int | None
    scalar_binding: int | None
    default_index: int | None
    declared: bool


@dataclass(frozen=True)
class DecodedParameterSource:
    identity: TraceSourceIdentity
    times: tuple[float, ...]
    channels: tuple[SourceFloatChannel, ...]
    declarations: tuple[DeclaredParameter, ...]
    curve_sources: tuple[CurveSource, ...]
    scale_bindings: int
    scalar_start: int
    controller_scope: str
    input_hashes: tuple[tuple[Path, str], ...]


def crc(name: str) -> int:
    return zlib.crc32(name.encode('utf-8')) & 0xffffffff


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding='utf-8-sig'))


def boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f'Expected explicit bool: field={field}')
    return value


def uint32(value: object, field: str) -> int:
    result = _integer(value, field)
    if not 0 <= result <= 0xffffffff:
        raise ValueError(f'Expected uint32 hash: field={field}, value={result}')
    return result


def declarations_from_controller(tree: dict[str, object]) -> tuple[DeclaredParameter, ...]:
    """Resolve each declared type's actual serialized default array/index."""
    names = _names(tree)
    controller = _record(tree['m_Controller'], 'controller')
    values = _record(_record(controller['m_Values'], 'values')['data'], 'values.data')
    defaults = _record(_record(controller['m_DefaultValues'], 'defaults')['data'], 'defaults.data')
    result: list[DeclaredParameter] = []
    for raw in _rows(values['m_ValueArray'], 'value_array'):
        row = _record(raw, 'declaration')
        key, kind, index = uint32(row['m_ID'], 'parameter.hash'), _integer(row['m_Type'], 'parameter.type'), _integer(row['m_Index'], 'parameter.index')
        arrays = {1: 'm_FloatValues', 3: 'm_IntValues', 4: 'm_BoolValues', 9: 'm_BoolValues'}
        if kind not in arrays:
            raise ValueError(f'Unsupported declared Animator parameter type: hash={key}, type={kind}')
        array = _rows(defaults[arrays[kind]], 'default_array')
        if not 0 <= index < len(array):
            raise ValueError(f'Controller default index exceeds its typed array: hash={key}, type={kind}, index={index}')
        value = (_number(array[index], 'float.default') if kind == 1 else
                 _integer(array[index], 'int.default') if kind == 3 else boolean(array[index], 'bool.default'))
        if kind == 3 and not -0x80000000 <= value <= 0x7fffffff:
            raise ValueError(f'Controller integer default exceeds int32: hash={key}')
        result.append(DeclaredParameter(key, names.get(key), kind, index, value))
    if len({row.name_hash for row in result}) != len(result):
        raise ValueError('Controller parameter declarations contain duplicate hashes')
    return tuple(result)


def map_cloth_curve_sources(channels: tuple[SourceFloatChannel, ...],
                            declarations: tuple[DeclaredParameter, ...]) -> tuple[CurveSource, ...]:
    by_hash = {row.name_hash: row for row in channels}
    declared = {row.name_hash: row for row in declarations}
    if len(by_hash) != len(channels) or len(declared) != len(declarations):
        raise ValueError('Source parameter map requires unique channel/declaration hashes')
    result: list[CurveSource] = []
    for field in fields(ClothCurveInputs):
        key = crc(field.name)
        channel, parameter = by_hash.get(key), declared.get(key)
        for resolved in (channel.name if channel else None, parameter.name if parameter else None):
            if resolved is not None and resolved != field.name:
                raise ValueError(f'Native curve name collides with serialized TOS name: field={field.name}, name={resolved}')
        if parameter is not None and parameter.kind != 1:
            raise ValueError(f'Native float curve is declared with a different type: field={field.name}, type={parameter.kind}')
        result.append(CurveSource(field.name, key, channel.track if channel else None,
            channel.binding if channel else None, parameter.default_index if parameter else None, parameter is not None))
    return tuple(result)


def source_input_paths(decoded_root: Path, source_frames: Path) -> dict[str, Path]:
    native = decoded_root / 'native'
    return {'reader_request': decoded_root / 'request.json', 'source_local_frames': source_frames,
        **{name: native / filename for name, filename in (
            ('clip_audit', 'clip_audit.json'), ('scalar_bindings', 'scalar_bindings.json'),
            ('float_samples', 'float_samples.json'), ('float_header', 'float_header.json'),
            ('float_acl', 'float.acl'), ('binding_verification', 'binding_verification.json'),
            ('parameter_verification', 'animator_parameter_verification.json'),
            ('controller_type_tree', 'controller_type_tree.json'),
            ('controller_source_references', 'controller_source_references.json'),
            ('frame_domain', 'frame_domain_verification.json'))}}


def read_decoded_parameter_source(decoded_root: Path, source_frames: Path,
                                  identity: TraceSourceIdentity,
                                  expected_hashes: dict[str, object]) -> DecodedParameterSource:
    """Validate native reader artifacts without requiring an already-final export receipt."""
    paths = source_input_paths(decoded_root, source_frames)
    hashes: dict[Path, str] = {}
    for name, path in paths.items():
        expected = _text(expected_hashes[name], 'source_sha256.' + name)
        if not re.fullmatch('[0-9a-fA-F]{64}', expected) or sha256(path) != expected.casefold():
            raise ValueError(f'Decoded parameter source bytes differ from expected identity: field={name}, path={path}')
        hashes[path] = expected.casefold()
    reader = _record(read_json(paths['reader_request']), 'reader request')
    clip = _record(read_json(paths['clip_audit']), 'clip audit')
    references = _record(read_json(paths['controller_source_references']), 'controller references')
    actual = TraceSourceIdentity(_text(reader['character_id'], 'character_id'), _text(reader['avatar_cab'], 'avatar_cab'),
        _integer(reader['avatar_path_id'], 'avatar_path_id'), _text(clip['source'], 'clip.source'),
        _integer(clip['path_id'], 'clip.path_id'), _text(references['source'], 'controller.source'),
        _integer(references['controller_path_id'], 'controller.path_id'))
    if identity != actual:
        raise ValueError(f'Decoded parameter source identity mismatch: expected={identity}, actual={actual}')
    tree = _record(read_json(paths['controller_type_tree']), 'controller tree')
    declarations = declarations_from_controller(tree)
    evidence = _record(read_json(paths['parameter_verification']), 'parameter verification')
    if evidence['status'] != 'exact_controller_clip_reference_and_tos_verified':
        raise ValueError(f'Trace currently requires the real reader exact controller/clip verification: status={evidence["status"]}')
    names, _ = _controller_names(decoded_root / 'native', reader, clip)
    if evidence['source'] != identity.controller_cab or evidence['controller_path_id'] != identity.controller_path_id:
        raise ValueError('Controller parameter evidence differs from the exact selected controller')
    scope = 'exact_controller_static_clip_reference_and_tos'
    raw_bindings = tuple(_record(row, 'binding') for row in _rows(clip['bindings'], 'bindings'))
    scalar_rows = tuple(_record(row, 'scalar') for row in _rows(read_json(paths['scalar_bindings']), 'scalar_bindings'))
    count = _integer(clip['output_track_count'], 'output_track_count')
    float_count = _integer(clip['float_curve_count'], 'float_curve_count')
    if count <= 0 or float_count <= 0 or len(scalar_rows) != float_count:
        raise ValueError('Clip transform/scalar counts are invalid')
    start = _integer(scalar_rows[0]['binding'], 'scalar_start')
    if not count * 2 <= start <= len(raw_bindings) or start + float_count != len(raw_bindings):
        raise ValueError('Actual scalar binding start disagrees with the transform/float layout')
    for index in range(count):
        position, rotation = raw_bindings[index], raw_bindings[count + index]
        if position['type'] != 'Transform' or position['attribute'] != 1 or rotation['type'] != 'Transform' or rotation['attribute'] != 2 or position['path'] != rotation['path']:
            raise ValueError(f'Actual Transform binding blocks disagree: track={index}')
    scales = raw_bindings[count * 2:start]
    if any(row['type'] != 'Transform' or row['attribute'] != 3 for row in scales) or len({row['path'] for row in scales}) != len(scales):
        raise ValueError('Intervening source bindings must be unique Transform scales')
    binding_evidence = _record(read_json(paths['binding_verification']), 'binding verification')
    if binding_evidence['scale_bindings'] != len(scales) or binding_evidence['scalar_bindings'] != float_count:
        raise ValueError('Actual scale/scalar binding layout differs from native reader verification')
    raw_frames = tuple(_record(row, 'scalar frame') for row in _rows(read_json(paths['float_samples']), 'scalar frames'))
    header = _record(read_json(paths['float_header']), 'float header')
    frame_domain = _record(read_json(paths['frame_domain']), 'frame_domain')
    selected_frames = tuple(_record(row, 'source frame') for row in _rows(read_json(source_frames), 'source frames'))
    common = _integer(frame_domain['common_frame_count'], 'common_frame_count')
    if frame_domain['status'] != 'shared_timestamp_prefix_verified' or frame_domain['raw_acl_buffers_preserved'] is not True:
        raise ValueError('Trace requires the actual reader shared timestamp-prefix/raw-buffer validation')
    if sha256(paths['float_acl']) != _text(_record(frame_domain['raw_acl_buffer_sha256'], 'raw_acl_hashes')['float'], 'float.acl.sha256').casefold():
        raise ValueError('Scalar ACL bytes differ from native shared-domain verification')
    if len(raw_frames) != header['samples'] or header['tracks'] != float_count or len(selected_frames) != common or not 0 < common <= len(raw_frames):
        raise ValueError('Raw scalar/header/shared exported frame counts disagree')
    values = tuple(tuple(_number(value, 'scalar.value') for value in _rows(row['values'], 'scalar.values')) for row in raw_frames)
    if any(len(row) != float_count for row in values):
        raise ValueError('Scalar samples differ from actual binding dimensions')
    times = tuple(_number(row['time'], 'scalar.time') for row in raw_frames)
    if any(a >= b for a, b in zip(times, times[1:])) or any(abs(times[index] - _number(row['time'], 'source.time')) > 1e-7 for index, row in enumerate(selected_frames)):
        raise ValueError('Scalar samples are not strictly ordered or do not match selected source frames')
    if abs(times[0] - _number(frame_domain['exported_start_time'], 'domain.start')) > 1e-7 or abs(times[common - 1] - _number(frame_domain['exported_end_time'], 'domain.end')) > 1e-7:
        raise ValueError('Selected scalar time domain differs from native shared-domain verification')
    channels: list[SourceFloatChannel] = []
    for track, scalar in enumerate(scalar_rows):
        raw = raw_bindings[start + track]
        if scalar['track'] != track or scalar['binding'] != start + track or scalar['attribute'] != raw['attribute'] or scalar['custom_type'] != raw['custom_type'] or raw['type'] != 'Animator':
            raise ValueError(f'Scalar row disagrees with exact GenericBinding: track={track}')
        if raw['custom_type'] != 0:
            continue
        if raw['path'] != 0 or raw['script_file_id'] != 0 or raw['script_path_id'] != 0 or raw['is_p_ptr_curve'] != 0 or raw['is_int_curve'] != 0:
            raise ValueError(f'Unsupported source-root Animator float binding: track={track}')
        key = uint32(raw['attribute'], 'float.attribute')
        if scope == 'exact_controller_static_clip_reference_and_tos' and key not in names:
            raise ValueError(f'Verified controller parameter table is missing sampled hash: hash={key}')
        channels.append(SourceFloatChannel(track, start + track, key, names.get(key), tuple(row[track] for row in values[:common])))
    curve_sources = map_cloth_curve_sources(tuple(channels), declarations)
    if any(sha256(path) != digest for path, digest in hashes.items()):
        raise ValueError('Decoded parameter sources changed during map validation')
    return DecodedParameterSource(identity, times[:common], tuple(channels), declarations,
        curve_sources, len(scales), start, scope, tuple(hashes.items()))


def source_curves_at_frame(source: DecodedParameterSource, frame: int, gate: bool,
                            absent_values: dict[str, object]) -> ClothCurveInputs:
    """Use exact clip/default values; an enabled valid-parameter gate returns zero for absent fields."""
    channels = {row.name_hash: row for row in source.channels}
    declarations = {row.name_hash: row for row in source.declarations}
    values: dict[str, float] = {}
    if any(key not in {row.field for row in source.curve_sources if not row.declared} for key in absent_values):
        raise ValueError('Explicit absent reads may name only actual undeclared native curve fields')
    for origin in source.curve_sources:
        if not origin.declared:
            if gate:
                if origin.field in absent_values:
                    raise ValueError(f'Enabled controller gate conflicts with explicit absent read: field={origin.field}')
                values[origin.field] = 0.
            elif origin.field in absent_values:
                values[origin.field] = _number(absent_values[origin.field], 'explicit absent float read')
            else:
                raise ValueError(f'Gate-disabled absent Animator.GetFloat is unresolved; supply an explicit scenario read: field={origin.field}')
        elif origin.name_hash in channels:
            values[origin.field] = channels[origin.name_hash].values[frame]
        else:
            values[origin.field] = float(declarations[origin.name_hash].default_value)
    return ClothCurveInputs(**values)


def produce_parameter_trace(source: DecodedParameterSource, tree: dict[str, object],
                            references: dict[str, object], scenario: dict[str, object]) -> dict[str, object]:
    """Replay explicit source-input events, then reuse native-verified cloth arithmetic."""
    if scenario['format'] != 'EndfieldManagedClothParameterScenario/1':
        raise ValueError('Expected EndfieldManagedClothParameterScenario/1')
    provenance = _text(scenario['provenance'], 'scenario.provenance')
    if _integer(scenario['cloth_combine_layer_index'], 'cloth_combine_layer_index') >= 0:
        raise ValueError('This source arithmetic supports only the explicit ClothCombine-absent branch')
    if scenario['initial_dynamic_snapshot'] != 'serialized_controller_float_defaults':
        raise ValueError('Supported explicit initial dynamic snapshot is serialized_controller_float_defaults')
    raw_builtin = _rows(scenario['built_in_hashes'], 'built_in_hashes')
    built_in = frozenset(uint32(value, 'built_in_hash') for value in raw_builtin)
    if len(built_in) != len(raw_builtin):
        raise ValueError('Explicit built-in ownership hashes must be unique')
    _text(scenario['built_in_provenance'], 'built_in_provenance')
    declared = {row.name_hash: row for row in source.declarations}
    sources: list[tuple[str, int, str]] = []
    for raw in _rows(scenario['dynamic_sources'], 'dynamic_sources'):
        row = _record(raw, 'dynamic_source')
        name, phase = _text(row['name'], 'source.name'), _text(row['phase'], 'source.phase')
        key = crc(name)
        if name not in {origin.field for origin in source.curve_sources} or key not in declared or declared[key].kind != 1 or declared[key].name != name or key in built_in:
            raise ValueError(f'Dynamic source must be an exact declared non-builtin native float curve: name={name}')
        if phase not in ('on_update', 'post_blackboard'):
            raise ValueError(f'Unknown explicit producer phase: phase={phase}')
        sources.append((name, key, phase))
    if len({row[0] for row in sources}) != len(sources):
        raise ValueError('Dynamic source selection must be unique')
    setup_frames = tuple(_integer(value, 'setup.frame_index') for value in _rows(scenario['setup_frame_indices'], 'setup_frame_indices'))
    if not setup_frames or setup_frames[0] != 0 or any(not 0 <= value < len(source.times) for value in setup_frames) or tuple(sorted(set(setup_frames))) != setup_frames:
        raise ValueError('Explicit empty registry scenario requires unique ordered Setup indices beginning at frame zero')
    frame_rows = _rows(scenario['frames'], 'scenario.frames')
    if len(frame_rows) != len(source.times):
        raise ValueError('Explicit runtime frame count differs from source scalar domain')
    hashes = tuple(uint32(value, 'layer_binding_hash') for value in _rows(scenario['layer_binding_hashes'], 'layer_binding_hashes'))
    routes = select_ordinary_routes(assess_ordinary_layers(tree, references), hashes)
    required: dict[str, int] = {}
    for route in routes:
        for spec in _rows(_record(route, 'route')['weights'], 'weights') + _rows(_record(route, 'route')['pose_times'], 'pose_times'):
            parameter = _record(spec, 'route.parameter')
            key = uint32(parameter['hash'], 'route.hash')
            if key == 0xffffffff:
                continue
            name = _text(parameter['name'], 'route.parameter.name')
            if crc(name) != key or key not in declared or declared[key].kind != 1:
                raise ValueError(f'Ordinary route requires an exact declared source float: name={name}, hash={key}')
            required[name] = key
    registry = empty_dynamic_registry()
    snapshot = {name: float(declared[key].default_value) for name, key, _ in sources}
    frames: list[object] = []
    publications: list[object] = []
    setup_events: list[object] = []
    for index, (time, raw) in enumerate(zip(source.times, frame_rows, strict=True)):
        runtime = _record(raw, 'runtime.frame')
        if abs(_number(runtime['time'], 'runtime.time') - time) > 1e-7:
            raise ValueError(f'Runtime scenario time differs from exact source domain: frame={index}')
        frame_provenance = _text(runtime['provenance'], 'runtime.provenance')
        if index in setup_frames:
            previous_count = len(registry.slots)
            setup = setup_managed_dynamic_registration(registry, tuple(declared), built_in)
            registry = setup.registry
            setup_events.append({'frame_index': index, 'removed_slots': previous_count,
                'declaration_count': len(setup.existing_hashes), 'dynamic_forced_writes': len(setup.dynamic_publication.writes)})
        curve = source_curves_at_frame(source, index, boolean(runtime['read_float_gate'], 'read_float_gate'),
            _record(runtime['absent_float_reads'], 'absent_float_reads'))
        events = {phase: tuple(DynamicFloatEvent(key, float(getattr(curve, name))) for name, key, selected in sources if selected == phase)
                  for phase in ('on_update', 'post_blackboard')}
        tick = ManagedBlackboardTick(events['on_update'], events['post_blackboard'], frozenset(declared),
            boolean(runtime['animator_alive'], 'animator_alive'), boolean(runtime['blackboard_initialized'], 'blackboard_initialized'))
        result = apply_managed_blackboard_tick(registry, tick, built_in)
        registry = result.registry
        snapshot = merge_dynamic_publication(snapshot, {key: name for name, key, _ in sources}, result.publication)
        values = asdict(curve) | snapshot
        parameters = compute_cloth_parameters(ClothCurveInputs(**values), ClothRuntimeInputs(
            _number(runtime['transition_offset'], 'transition_offset'), boolean(runtime['styled_body_add'], 'styled_body_add')))
        channels = {row.name_hash: row for row in source.channels}
        for name, key in required.items():
            if name not in parameters:
                parameters[name] = channels[key].values[index] if key in channels else float(declared[key].default_value)
        frames.append({'time': time, 'parameters': parameters,
            'provenance': provenance + '; ' + frame_provenance})
        publications.append({'frame_index': index, 'time': time, 'writes': [asdict(row) for row in result.publication.writes],
            'on_update_events': [asdict(row) for row in events['on_update']], 'post_blackboard_events': [asdict(row) for row in events['post_blackboard']],
            'snapshot': dict(snapshot), 'dirty': list(registry.state.dirty), 'registry_slots': len(registry.slots)})
    return {'format': 'EndfieldClothParameterTrace/1', 'status': 'native_managed_caller_exact_source_explicit_scenario',
        'source_identity': asdict(source.identity), 'frames': frames,
        'curve_sources': [asdict(row) for row in source.curve_sources], 'declarations': [asdict(row) for row in source.declarations],
        'scalar_channels': [{key: value for key, value in asdict(row).items() if key != 'values'} for row in source.channels],
        'source_scalar_layout': {'scalar_start': source.scalar_start, 'scale_bindings': source.scale_bindings},
        'controller_scope': source.controller_scope, 'setup_events': setup_events, 'publications': publications,
        'scenario': scenario, 'runtime_animator_reconstructed': False,
        'boundary': 'Explicit source scalar/default reads -> supplied OnUpdate/PostBlackboard source-input events -> native-verified managed publication -> native-verified ClothCombine-absent scalar arithmetic. This replay order matches the bounded Stage67 source-input consumer; it does not reconstruct gameplay producer bodies, live clip/state ownership, C-output publication, fast handles, IK feedback, or Unity evaluation scheduling.'}


def export_parameter_trace(request_path: Path) -> dict[str, object]:
    """Produce a new hash-linked trace without reading historical prepared traces."""
    request_path = request_path.resolve(strict=True)
    request_hash = sha256(request_path)
    module_root = Path(__file__).parent
    module_hashes = {module_root / name: sha256(module_root / name) for name in ('character_animation_parameter_trace.py',
        'character_animation_parameters.py', 'character_cloth_curve_parameters.py', 'animator_blackboard_caller.py',
        'animator_dynamic_lifecycle.py', 'animator_dynamic_publication.py', 'animator_discrete_lifecycle.py',
        'animator_ordinary_plan.py', 'character_animator_graph.py')}
    request = _record(read_json(request_path), 'request')
    if request['format'] != 'EndfieldAnimationParameterTraceRequest/1':
        raise ValueError('Expected EndfieldAnimationParameterTraceRequest/1')
    def path(field: str, strict: bool) -> Path:
        return (request_path.parent / _text(request[field], field)).resolve(strict=strict)
    decoded_root, source_frames, scenario_path, output = path('decoded_root', True), path('source_local_frames', True), path('scenario', True), path('output', False)
    if output.exists() or any(value.is_relative_to(output) for value in (request_path, decoded_root, source_frames, scenario_path)):
        raise ValueError('Parameter trace output must be new and separate from its inputs')
    raw_identity = _record(request['source_identity'], 'source_identity')
    identity = TraceSourceIdentity(*(_integer(raw_identity[field.name], field.name) if field.name.endswith('_path_id')
        else _text(raw_identity[field.name], field.name) for field in fields(TraceSourceIdentity)))
    scenario_hash = sha256(scenario_path)
    scenario = _record(read_json(scenario_path), 'scenario')
    source = read_decoded_parameter_source(decoded_root, source_frames, identity, _record(request['source_sha256'], 'source_sha256'))
    tree = _record(read_json(decoded_root / 'native/controller_type_tree.json'), 'tree')
    references = _record(read_json(decoded_root / 'native/controller_source_references.json'), 'references')
    trace = produce_parameter_trace(source, tree, references, scenario)
    hashes = dict(source.input_hashes) | {request_path: request_hash, scenario_path: scenario_hash} | module_hashes
    if any(sha256(value) != digest for value, digest in hashes.items()):
        raise ValueError('Parameter trace request/scenario/source changed during evaluation')
    output.mkdir(parents=True, exist_ok=False)
    trace['input_sha256'] = {str(value): digest for value, digest in hashes.items()}
    trace_path = output / 'parameters.json'
    trace_path.write_text(json.dumps(trace, indent=2, allow_nan=False), encoding='utf-8')
    receipt = {'format': 'EndfieldAnimationParameterTraceReceipt/1', 'status': 'exact_source_trace_produced',
        'parameters': str(trace_path), 'parameters_sha256': sha256(trace_path), 'frames': len(source.times),
        'source_identity': asdict(identity), 'input_sha256': trace['input_sha256'],
        'publication_boundaries_with_writes': sum(bool(_record(row, 'publication')['writes']) for row in _rows(trace['publications'], 'publications')),
        'runtime_animator_reconstructed': False, 'boundary': trace['boundary']}
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    return receipt


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise ValueError('Expected one EndfieldAnimationParameterTraceRequest/1 file')
    print(json.dumps(export_parameter_trace(Path(sys.argv[1]))))
