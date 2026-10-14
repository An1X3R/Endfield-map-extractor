"""Index exact controller layers and custom pose-time routes without inventing runtime state."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import cast
import zlib


class AnimatorGraphError(ValueError):
    """A serialized controller graph has contradictory or malformed references."""


@dataclass(frozen=True)
class Parameter:
    hash: int
    name: str | None


@dataclass(frozen=True)
class ClipPointer:
    index: int
    file_id: int
    cab: str
    path_id: int


@dataclass(frozen=True)
class DirectRoute:
    layer: int
    state: int
    tree: int
    node: int
    child_nodes: tuple[int, ...]
    weights: tuple[Parameter, ...]
    pose_times: tuple[Parameter, ...]
    use_pose_time: bool
    normalize_weights: bool
    child_clips: tuple[ClipPointer | None, ...]


@dataclass(frozen=True)
class Layer:
    index: int
    name: Parameter
    state_machine: int
    default_weight_serialized: float
    blend_mode_serialized: int
    default_state_serialized: int
    state_count: int
    state_transition_count: int
    any_state_transition_count: int
    selector_count: int
    ik_pass: bool
    source_pointer: str


def _obj(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AnimatorGraphError(f'Expected object: {field}')
    return cast(dict[str, object], value)


def _array(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise AnimatorGraphError(f'Expected array: {field}')
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise AnimatorGraphError(f'Expected integer: {field}, value={value!r}')
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise AnimatorGraphError(f'Expected boolean: {field}, value={value!r}')
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AnimatorGraphError(f'Expected nonempty string: {field}')
    return value


def _data(value: object, field: str) -> dict[str, object]:
    return _obj(_obj(value, field)['data'], field + '.data')


def _index(value: object, length: int, field: str) -> int:
    index = _integer(value, field)
    if not 0 <= index < length:
        raise AnimatorGraphError(f'Index out of range: {field}, index={index}, length={length}')
    return index


def _names(tree: dict[str, object]) -> dict[int, str]:
    names: dict[int, str] = {}
    for value in _array(tree['m_TOSData'], 'm_TOSData'):
        if not isinstance(value, str):
            raise AnimatorGraphError('Expected string: TOS entry')
        name = value
        key = zlib.crc32(name.encode('utf-8'))
        if key in names and names[key] != name:
            raise AnimatorGraphError(f'Controller TOS collision: hash={key}')
        names[key] = name
    return names


def _parameter(value: object, names: dict[int, str]) -> Parameter:
    key = _integer(value, 'parameter hash')
    return Parameter(key, names.get(key))


def _clips(tree: dict[str, object], references: dict[str, object]) -> tuple[ClipPointer, ...]:
    source = _text(references['source'], 'controller CAB')
    externals = tuple(_text(value, 'external CAB') for value in _array(references['external_cabs'], 'external_cabs'))
    result: list[ClipPointer] = []
    for index, value in enumerate(_array(tree['m_AnimationClips'], 'm_AnimationClips')):
        row = _obj(value, 'clip pointer')
        file_id = _integer(row['m_FileID'], 'm_FileID')
        if not 0 <= file_id <= len(externals):
            raise AnimatorGraphError(f'Clip external reference out of range: clip={index}, file_id={file_id}')
        result.append(ClipPointer(index, file_id, source if file_id == 0 else externals[file_id - 1],
                                  _integer(row['m_PathID'], 'm_PathID')))
    return tuple(result)


def _direct_routes(layer_index: int, state_index: int, state: dict[str, object],
                   names: dict[int, str], clips: tuple[ClipPointer, ...]) -> tuple[DirectRoute, ...]:
    result: list[DirectRoute] = []
    for tree_index, value in enumerate(_array(state['m_BlendTreeConstantArray'], 'blend trees')):
        nodes = tuple(_data(item, 'blend node') for item in _array(_data(value, 'blend tree')['m_NodeArray'], 'nodes'))
        for node_index, node in enumerate(nodes):
            children = tuple(_index(item, len(nodes), 'child node') for item in _array(node['m_ChildIndices'], 'children'))
            if _integer(node['m_BlendType'], 'blend type') != 4:
                continue
            direct = _data(node['m_BlendDirectData'], 'direct blend')
            weights = tuple(_parameter(item, names) for item in _array(direct['m_ChildBlendEventIDArray'], 'weights'))
            times = tuple(_parameter(item, names) for item in _array(direct['m_ChildPoseTimeEventIDArray'], 'pose times'))
            use_time = _boolean(direct['m_UsePoseTimeValues'], 'use pose times')
            if len(weights) != len(children) or (use_time and len(times) != len(children)):
                raise AnimatorGraphError(f'Direct blend channel count differs from children: layer={layer_index}, state={state_index}, node={node_index}')
            child_clips: list[ClipPointer | None] = []
            for child in children:
                clip_index = _integer(nodes[child]['m_ClipID'], 'm_ClipID')
                child_clips.append(None if clip_index == 0xffffffff else clips[_index(clip_index, len(clips), 'child clip')])
            result.append(DirectRoute(layer_index, state_index, tree_index, node_index, children, weights, times,
                                      use_time, _boolean(direct['m_NormalizedBlendValues'], 'normalized weights'), tuple(child_clips)))
    return tuple(result)


def index_controller_graph(tree: dict[str, object], references: dict[str, object]) -> dict[str, object]:
    """Retain exact clip identity and custom direct controls; serialized defaults are not live state."""
    names = _names(tree)
    clips = _clips(tree, references)
    controller = _obj(tree['m_Controller'], 'm_Controller')
    machines = tuple(_data(value, 'state machine') for value in _array(controller['m_StateMachineArray'], 'state machines'))
    layers: list[Layer] = []
    routes: list[DirectRoute] = []
    for index, value in enumerate(_array(controller['m_LayerArray'], 'layers')):
        layer = _data(value, 'layer')
        machine_index = _index(layer['m_StateMachineIndex'], len(machines), 'layer state machine')
        machine = machines[machine_index]
        states = tuple(_data(item, 'state') for item in _array(machine['m_StateConstantArray'], 'states'))
        weight = layer['m_DefaultWeight']
        if type(weight) not in (int, float) or not math.isfinite(weight):
            raise AnimatorGraphError(f'Invalid layer default weight: layer={index}, value={weight!r}')
        layers.append(Layer(index, _parameter(layer['m_Binding'], names), machine_index, float(weight),
                            _integer(layer['(int&)m_LayerBlendingMode'], 'blend mode'),
                            _integer(machine['m_DefaultState'], 'default state'), len(states),
                            sum(len(_array(state['m_TransitionConstantArray'], 'transitions')) for state in states),
                            len(_array(machine['m_AnyStateTransitionConstantArray'], 'any-state transitions')),
                            len(_array(machine['m_SelectorStateConstantArray'], 'selectors')),
                            _boolean(layer['m_IKPass'], 'IK pass'), f'/m_Controller/m_LayerArray/{index}/data'))
        for state_index, state in enumerate(states):
            routes.extend(_direct_routes(index, state_index, state, names, clips))
    return {'format': 'EndfieldAnimatorGraphSource/1', 'controller': _text(tree['m_Name'], 'controller name'),
            'source': _text(references['source'], 'controller CAB'),
            'controller_path_id': _integer(references['controller_path_id'], 'controller PathID'),
            'layers': [asdict(layer) for layer in layers], 'direct_routes': [asdict(route) for route in routes],
            'clips': [asdict(clip) for clip in clips], 'runtime_state_reconstructed': False,
            'boundary': 'Source layer masks, transitions, selectors and 1D/2D/sequence data remain in the hashed raw graph. Direct pose-time parameters are game extensions. No layer evaluation, runtime weights, state transitions, montage dispatch or native auxiliary deformation is claimed.'}


def export_controller_graph(native: Path) -> Path | None:
    verification = _obj(json.loads((native / 'animator_parameter_verification.json').read_text(encoding='utf-8-sig')), 'parameter verification')
    if verification['status'] != 'exact_controller_clip_reference_and_tos_verified':
        return None
    source = native / 'controller_type_tree.json'
    references = native / 'controller_source_references.json'
    tree = _obj(json.loads(source.read_text(encoding='utf-8-sig')), 'controller tree')
    identities = _obj(json.loads(references.read_text(encoding='utf-8-sig')), 'source references')
    if identities['source'] != verification['source'] or identities['controller_path_id'] != verification['controller_path_id']:
        raise AnimatorGraphError('Controller graph identities differ from the exact selected-clip verification')
    result = index_controller_graph(tree, identities)
    result['input_sha256'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, references)}
    output = native / 'controller_graph_source.json'
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    return output
