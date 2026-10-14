"""Bridge explicit source state scenarios to the verified native clock consumer.

Only ordinary additive leaf states after the selected Direct layers are supported.
Caller parameters, initial clock and interval phase remain explicit offline inputs.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path

from animator_auxiliary_layers import number, object_row, read_json, rows
from animator_ordinary_plan import assess_ordinary_layers
from animator_ordinary_source import ordinary_layer_mask
from animator_state_schedule import clip_state, source_machine
from build_native_state_clock_candidate import main as build_native_candidate
from character_animator_graph import _integer, _names
from character_animation_goal_export import write_json


def read_state_scenario(path: Path) -> dict[str, object]:
    scenario = object_row(read_json(path), 'state_scenario')
    if scenario['format'] != 'EndfieldNativeStateReplayScenario/1':
        raise ValueError('Expected EndfieldNativeStateReplayScenario/1')
    binding = _integer(scenario['layer_binding_hash'], 'state.layer_binding_hash')
    if not 0 <= binding <= 0xffffffff:
        raise ValueError('State layer binding hash must be uint32')
    indices = rows(scenario['state_indices'], 'state_indices')
    if not indices or any(type(value) is not int or value < 0 for value in indices) or len(set(indices)) != len(indices):
        raise ValueError('State scenario requires unique nonempty source state indices')
    initial = object_row(scenario['initial_clock'], 'initial_clock')
    if _integer(initial['state_index'], 'initial_clock.state_index') not in indices or number(initial['normalized'], 'initial_clock.normalized') < 0.:
        raise ValueError('Initial clock must reference a selected source state and be nonnegative')
    if scenario['parameter_boundary_convention'] != 'parameters_before_interval':
        raise ValueError('State scenario must explicitly supply parameters_before_interval')
    if not rows(scenario['frames'], 'state.frames'):
        raise ValueError('State scenario requires explicit per-frame parameters and speed')
    return scenario


def state_sampling_routes(tree: dict[str, object], references: dict[str, object],
                          direct_bindings: tuple[int, ...], scenario: dict[str, object],
                          hierarchy: list[object]) -> tuple[dict[str, object], ...]:
    """Resolve exact source leaf PPtrs and preserve serialized layer ordering."""
    binding = _integer(scenario['layer_binding_hash'], 'state.layer_binding_hash')
    assessments = assess_ordinary_layers(tree, references)
    by_hash = {row.binding_hash: row for row in assessments}
    if binding not in by_hash or binding in direct_bindings:
        raise ValueError('State scenario must select a distinct actual controller layer')
    if any(value not in by_hash for value in direct_bindings) or by_hash[binding].index <= max(by_hash[value].index for value in direct_bindings):
        raise ValueError('State layer interleaves selected Direct source layers; ordered mixed evaluation is unsupported')
    controller = object_row(tree['m_Controller'], 'controller')
    layer = object_row(object_row(rows(controller['m_LayerArray'], 'layers')[by_hash[binding].index], 'layer')['data'], 'layer.data')
    paths = tuple(str(object_row(row, 'hierarchy')['path']) for row in hierarchy)
    ordinary_layer_mask(layer, paths)
    if number(layer['m_DefaultWeight'], 'layer.default_weight') < 0.:
        raise ValueError('Negative source state layer weight is unsupported')
    machine = source_machine(tree, references, binding)
    names = {value: key for key, value in _names(tree).items()}
    pointers: dict[int, dict[str, object]] = {}
    for raw_sample in rows(scenario['frames'], 'state.frames'):
        sample = object_row(raw_sample, 'state.frame')
        values = object_row(sample['parameters'], 'state.parameters')
        if any(name not in names for name in values):
            raise ValueError('State parameter names must resolve through the exact controller TOS')
        parameters = {names[name]: value for name, value in values.items()}
        for state in rows(scenario['state_indices'], 'state_indices'):
            pointer, _, _ = clip_state(machine, _integer(state, 'state.index'), parameters)
            identity = asdict(pointer)
            if pointer.index in pointers and pointers[pointer.index] != identity:
                raise ValueError('Selected source states conflict on the same clip index')
            pointers[pointer.index] = identity
    if len(pointers) != 1:
        raise ValueError('Different source leaf clips require missing-channel ownership/WriteDefaults reconstruction; this replay supports same-clip source state clocks only')
    return ({'child_clips': list(pointers.values()), 'state_layer_binding_hash': binding,
             'state_indices': scenario['state_indices']},)


def validate_managed_state_parameters(managed: dict[str, object], scenario: dict[str, object],
                                      tree: dict[str, object]) -> None:
    """Check explicitly declared shared caller values, without inferring live phase."""
    links = object_row(scenario['managed_parameter_links'], 'managed_parameter_links')
    controller_names = frozenset(_names(tree).values())
    frames = rows(managed['frames'], 'managed.frames')
    states = rows(scenario['frames'], 'state.frames')
    if len(frames) != len(states):
        raise ValueError('Managed and state scenario frame counts must agree')
    for raw_managed, raw_state in zip(frames, states, strict=True):
        runtime = object_row(raw_managed, 'managed.frame')
        state = object_row(raw_state, 'state.frame')
        values = object_row(state['parameters'], 'state.parameters')
        if abs(number(runtime['time'], 'managed.time') - number(state['time'], 'state.time')) > 1e-7:
            raise ValueError('Linked managed and state scenario timestamps differ')
        for field, name in links.items():
            if not isinstance(name, str) or name not in controller_names:
                raise ValueError(f'Linked state parameter is absent from exact source controller TOS: field={field}, name={name}')
            if field not in runtime or name not in values or type(runtime[field]) is not bool or type(values[name]) is not bool:
                raise ValueError(f'Explicit shared scenario link requires both boolean inputs: field={field}, name={name}')
            if runtime[field] != values[name]:
                raise ValueError(f'Explicit managed/state scenario link differs: field={field}, name={name}, time={state["time"]}')


def export_state_layer(root: Path, scenario_path: Path, source_paths: dict[str, Path],
                       base_frames: Path, samples: Path) -> Path:
    """Apply native clock/transition ports to this fresh export's ordinary result."""
    scenario = read_state_scenario(scenario_path)
    parameters = root / 'state_parameters.json'
    write_json(parameters, {'format': 'EndfieldSourceStateScenarioParameters/1', 'frames': scenario['frames']})
    request = root / 'state_request.json'
    write_json(request, {'format': 'EndfieldNativeLeafClockScenarioRequest/1',
        **{name: str(source_paths[name]) for name in ('controller_type_tree', 'controller_source_references', 'selected_avatar', 'source_hierarchy')},
        'source_local_frames': str(base_frames), 'parameters': str(parameters),
        'clip_samples_root': str(samples), 'layer_binding_hash': scenario['layer_binding_hash'],
        'initial_clock': scenario['initial_clock'],
        'parameter_boundary_convention': scenario['parameter_boundary_convention'],
        'output': str(root / 'state_candidate')})
    build_native_candidate(request)
    candidate = root / 'state_candidate'
    receipt = object_row(read_json(candidate / 'receipt.json'), 'state.receipt')
    frames = candidate / 'source_local_frames.json'
    if hashlib.sha256(frames.read_bytes()).hexdigest() != object_row(receipt['output_sha256'], 'state.output_sha256')['source_local_frames.json']:
        raise ValueError('State candidate frames disagree with the native consumer receipt')
    for raw_trace in rows(read_json(candidate / 'state_schedule.json'), 'state.schedule'):
        trace = object_row(raw_trace, 'state.trace')
        for raw_port in rows(trace['ports'], 'state.ports'):
            if object_row(raw_port, 'state.port')['state'] not in scenario['state_indices']:
                raise ValueError('Native transition entered a source state outside explicit sampled coverage')
    return frames
