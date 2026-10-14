"""Advance explicit source state scenarios with verified ordinary transitions.

Parameter samples apply at the beginning of each caller-supplied interval.
This is an offline boundary convention, not a claim about the game scheduler.
Unsupported selected transitions and active interruptions fail explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from animator_auxiliary_layers import number, object_row, rows
from animator_weighted_channels import float32
from character_animator_graph import _boolean, _integer, _names, _clips, ClipPointer


@dataclass(frozen=True)
class SourceMachine:
    binding_hash: int
    name: str | None
    default_state: int
    states: tuple[dict[str, object], ...]
    selectors: tuple[dict[str, object], ...]
    parameter_types: tuple[tuple[int, int], ...]
    clips: tuple[ClipPointer, ...]


@dataclass(frozen=True)
class TransitionChoice:
    source: int
    destination: int
    index: int
    duration: float
    offset: float


@dataclass(frozen=True)
class StateClock:
    state: int
    seconds: float


@dataclass(frozen=True)
class ActiveTransition:
    choice: TransitionChoice
    source: StateClock
    destination: StateClock
    progress: float


@dataclass(frozen=True)
class Schedule:
    current: StateClock
    transition: ActiveTransition | None


@dataclass(frozen=True)
class StatePort:
    state: int
    clip: ClipPointer
    sample_seconds: float
    weight: float
    write_defaults_serialized: bool


def source_machine(tree: dict[str, object], references: dict[str, object], binding_hash: int) -> SourceMachine:
    """Select a machine by its actual controller binding, without layer indices."""
    controller = object_row(tree['m_Controller'], 'controller')
    layers = tuple(object_row(object_row(row, 'layer')['data'], 'layer.data') for row in rows(controller['m_LayerArray'], 'layers'))
    matches = tuple(row for row in layers if row['m_Binding'] == binding_hash)
    if len(matches) != 1:
        raise ValueError(f'Selected source layer binding is not unique: hash={binding_hash}, matches={len(matches)}')
    layer = matches[0]
    machines = rows(controller['m_StateMachineArray'], 'machines')
    index = _integer(layer['m_StateMachineIndex'], 'machine.index')
    if not 0 <= index < len(machines):
        raise ValueError(f'Source state machine index is out of range: index={index}')
    machine = object_row(object_row(machines[index], 'machine')['data'], 'machine.data')
    if rows(machine['m_AnyStateTransitionConstantArray'], 'any_state'):
        raise ValueError('AnyState dispatch is not implemented in the explicit ordinary state scenario')
    states = tuple(object_row(object_row(row, 'state')['data'], 'state.data') for row in rows(machine['m_StateConstantArray'], 'states'))
    selectors = tuple(object_row(object_row(row, 'selector')['data'], 'selector.data') for row in rows(machine['m_SelectorStateConstantArray'], 'selectors'))
    default = _integer(machine['m_DefaultState'], 'machine.default')
    if not 0 <= default < len(states):
        raise ValueError(f'Source default state is out of range: state={default}')
    declarations = rows(object_row(object_row(controller['m_Values'], 'values')['data'], 'values.data')['m_ValueArray'], 'declarations')
    types = tuple((_integer(object_row(row, 'declaration')['m_ID'], 'parameter.id'),
                   _integer(object_row(row, 'declaration')['m_Type'], 'parameter.type')) for row in declarations)
    if len(dict(types)) != len(types):
        raise ValueError('Source parameter declarations contain duplicate hashes')
    return SourceMachine(binding_hash, _names(tree).get(binding_hash), default, states, selectors, types, _clips(tree, references))


def condition_passes(condition: dict[str, object], types: dict[int, int], parameters: dict[int, float | int | bool]) -> bool:
    """Evaluate source If/IfNot, float/int Greater/Less and int Equals/NotEqual."""
    event = _integer(condition['m_EventID'], 'condition.event')
    mode = _integer(condition['m_ConditionMode'], 'condition.mode')
    if event not in types or event not in parameters:
        raise ValueError(f'Condition requires an explicitly supplied declared parameter: hash={event}')
    value = parameters[event]
    kind = types[event]
    if kind == 4 and mode in (1, 2):
        flag = _boolean(value, 'condition.boolean')
        return flag if mode == 1 else not flag
    if kind == 9:
        raise ValueError(f'Trigger consumption is not implemented: hash={event}')
    if kind not in (1, 3) or mode not in (3, 4, 6, 7) or (mode in (6, 7) and kind != 3):
        raise ValueError(f'Unsupported source condition: hash={event}, type={kind}, mode={mode}')
    if kind == 3:
        integer_value = _integer(value, 'condition.integer')
        if not -2147483648 <= integer_value <= 2147483647:
            raise ValueError(f'Animator integer parameter exceeds int32: hash={event}, value={integer_value}')
        scalar = float32(integer_value)
    else:
        scalar = float32(number(value, 'condition.value'))
    threshold = float32(number(condition['m_EventThreshold'], 'condition.threshold'))
    if mode == 3:
        return scalar > threshold
    if mode == 4:
        return scalar < threshold
    return scalar == threshold if mode == 6 else scalar != threshold


def resolve_destination(machine: SourceMachine, destination: int, parameters: dict[int, float | int | bool], visited: tuple[int, ...]) -> int:
    """Resolve ordered serialized selector branches, rejecting cycles and gaps."""
    if destination < 30000:
        if not 0 <= destination < len(machine.states):
            raise ValueError(f'Source transition destination is out of range: destination={destination}')
        return destination
    index = destination - 30000
    if index in visited or not 0 <= index < len(machine.selectors):
        raise ValueError(f'Source selector is cyclic or absent: selector={index}, visited={visited}')
    selector = machine.selectors[index]
    for raw in rows(selector['m_TransitionConstantArray'], 'selector.transitions'):
        transition = object_row(object_row(raw, 'selector.transition')['data'], 'selector.transition.data')
        conditions = tuple(object_row(object_row(row, 'condition')['data'], 'condition.data') for row in rows(transition['m_ConditionConstantArray'], 'conditions'))
        if all(condition_passes(row, dict(machine.parameter_types), parameters) for row in conditions):
            if rows(transition['m_TransitionBlockParamConstantArray'], 'selector.block_parameters'):
                raise ValueError(f'Selector parameter blocks are not implemented: selector={index}')
            return resolve_destination(machine, _integer(transition['m_Destination'], 'destination'), parameters, visited + (index,))
    raise ValueError(f'No serialized selector branch matches the explicit parameters: selector={index}')


def select_transition(machine: SourceMachine, state_index: int, parameters: dict[int, float | int | bool]) -> TransitionChoice | None:
    """Select the first eligible source transition and validate its implemented shape."""
    if not 0 <= state_index < len(machine.states):
        raise ValueError(f'Source state is out of range: state={state_index}')
    state = machine.states[state_index]
    for index, raw in enumerate(rows(state['m_TransitionConstantArray'], 'state.transitions')):
        transition = object_row(object_row(raw, 'transition')['data'], 'transition.data')
        if _boolean(transition['m_HasExitTime'], 'transition.has_exit'):
            raise ValueError(f'Exit-time crossing is not implemented: state={state_index}, transition={index}')
        conditions = tuple(object_row(object_row(row, 'condition')['data'], 'condition.data') for row in rows(transition['m_ConditionConstantArray'], 'conditions'))
        if not conditions or not all(condition_passes(row, dict(machine.parameter_types), parameters) for row in conditions):
            continue
        destination = _integer(transition['m_DestinationState'], 'destination')
        if destination == state_index and not _boolean(transition['m_CanTransitionToSelf'], 'transition.can_self'):
            continue
        if rows(transition['m_TransitionBlockParamConstantArray'], 'transition.block_parameters'):
            raise ValueError(f'Transition parameter blocks are not implemented: state={state_index}, transition={index}')
        if not _boolean(transition['m_HasFixedDuration'], 'transition.fixed') or transition['m_BlendStyle'] != 0:
            raise ValueError(f'Only fixed-duration linear transitions are implemented: state={state_index}, transition={index}')
        duration = float32(number(transition['m_TransitionDuration'], 'transition.duration'))
        offset = float32(number(transition['m_TransitionOffset'], 'transition.offset'))
        if duration <= 0. or offset != 0.:
            raise ValueError(f'Transition duration must be positive and offset zero: duration={duration}, offset={offset}')
        return TransitionChoice(state_index, resolve_destination(machine, destination, parameters, ()), index, duration, offset)
    return None


def clip_state(machine: SourceMachine, state_index: int, parameters: dict[int, float | int | bool]) -> tuple[ClipPointer, float, bool]:
    """Validate the entered state's ordinary looping leaf and source clock factors."""
    if not 0 <= state_index < len(machine.states):
        raise ValueError(f'Entered source state is out of range: state={state_index}')
    state = machine.states[state_index]
    for field in ('m_SpeedParamID', 'm_MirrorParamID', 'm_TimeParamID'):
        if state[field] != 0:
            raise ValueError(f'Parameterized state clock/mirror is not implemented: state={state_index}, field={field}')
    offset_parameter = _integer(state['m_CycleOffsetParamID'], 'cycle_offset.parameter')
    if offset_parameter:
        if dict(machine.parameter_types).get(offset_parameter) != 1 or offset_parameter not in parameters:
            raise ValueError(f'Cycle offset requires an explicit source float parameter: hash={offset_parameter}')
        if number(parameters[offset_parameter], 'cycle_offset') != 0.:
            raise ValueError('Nonzero parameterized cycle offsets are not implemented')
    if state['m_Mirror'] is not False or state['m_Loop'] is not True or number(state['m_CycleOffset'], 'cycle_offset') != 0.:
        raise ValueError(f'Only ordinary unmirrored zero-offset looping states are implemented: state={state_index}')
    if state['m_IKOnFeet'] is not False or state['m_SyncGroupID'] != 0 or state['m_SyncGroupRole'] != 0 or rows(state['m_StateParameterConstantArray'], 'state.parameters'):
        raise ValueError(f'State IK/synchronization/parameter blocks are not implemented: state={state_index}')
    trees = rows(state['m_BlendTreeConstantArray'], 'state.trees')
    if len(trees) != 1 or rows(state['m_BlendTreeConstantIndexArray'], 'state.tree_indices') != [0]:
        raise ValueError(f'Entered state does not have one exact source tree: state={state_index}')
    nodes = rows(object_row(object_row(trees[0], 'tree')['data'], 'tree.data')['m_NodeArray'], 'nodes')
    if len(nodes) != 1:
        raise ValueError(f'Entered state is not a single sampled clip: state={state_index}')
    node = object_row(object_row(nodes[0], 'node')['data'], 'node.data')
    index = _integer(node['m_ClipID'], 'clip.index')
    if rows(node['m_ChildIndices'], 'node.children') or not 0 <= index < len(machine.clips):
        raise ValueError(f'Entered state has no exact leaf clip: state={state_index}, clip={index}')
    if node['m_Mirror'] is not False or number(node['m_CycleOffset'], 'node.cycle_offset') != 0. or number(node['m_Duration'], 'node.duration') != 1.:
        raise ValueError(f'Clip mirror/offset/duration scaling is not implemented: state={state_index}')
    speed = float32(number(state['m_Speed'], 'state.speed'))
    if speed <= 0.:
        raise ValueError(f'Only positive state speed is implemented: state={state_index}, speed={speed}')
    return machine.clips[index], speed, _boolean(state['m_WriteDefaultValues'], 'write_defaults')


def linear_progress(progress: float, seconds: float, duration: float) -> tuple[float, float]:
    """Match the native fixed-duration progress add and linear clamp, with no exit remainder."""
    if any(not math.isfinite(value) for value in (progress, seconds, duration)) or progress < 0. or seconds < 0. or duration <= 0.:
        raise ValueError('Transition progress requires finite nonnegative time and positive duration')
    updated = float32(float32(progress) + float32(float32(seconds) / float32(duration)))
    return updated, min(1., max(0., updated))


def advance_schedule(machine: SourceMachine, schedule: Schedule, parameters: dict[int, float | int | bool], seconds: float) -> Schedule:
    """Consume one explicit interval; fail if an unsupported interruption becomes eligible."""
    delta = float32(number(seconds, 'interval.seconds'))
    if delta < 0.:
        raise ValueError('State scenario intervals must be nonnegative')
    transition = schedule.transition
    clocks = (schedule.current,) if transition is None else (schedule.current, transition.source, transition.destination)
    if any(not math.isfinite(clock.seconds) or clock.seconds < 0. for clock in clocks):
        raise ValueError('State scenario clocks must be finite and nonnegative')
    if transition is None:
        choice = select_transition(machine, schedule.current.state, parameters)
        if choice is not None:
            transition = ActiveTransition(choice, schedule.current, StateClock(choice.destination, 0.), 0.)
    else:
        for clock in (transition.source, transition.destination):
            eligible = select_transition(machine, clock.state, parameters)
            if eligible is not None and eligible != transition.choice:
                raise ValueError(f'Active transition interruption is not implemented: source={clock.state}, choice={eligible}')
    def advance(clock: StateClock) -> StateClock:
        _, speed, _ = clip_state(machine, clock.state, parameters)
        return StateClock(clock.state, float32(clock.seconds + float32(delta * speed)))
    if transition is None:
        return Schedule(advance(schedule.current), None)
    source, destination = advance(transition.source), advance(transition.destination)
    progress, weight = linear_progress(transition.progress, delta, transition.choice.duration)
    if weight == 1.:
        return Schedule(destination, None)
    return Schedule(source, ActiveTransition(transition.choice, source, destination, progress))


def state_ports(machine: SourceMachine, schedule: Schedule, parameters: dict[int, float | int | bool], clip_durations: dict[int, float]) -> tuple[StatePort, ...]:
    """Expose source/destination pose ports for the verified ordinary mixer."""
    transition = schedule.transition
    clocks = ((schedule.current, 1.),) if transition is None else ((transition.source, float32(1. - min(1., transition.progress))), (transition.destination, min(1., transition.progress)))
    result: list[StatePort] = []
    for clock, weight in clocks:
        clip, _, write_defaults = clip_state(machine, clock.state, parameters)
        if clip.index not in clip_durations:
            raise ValueError(f'Entered state clip has no validated source duration: clip={clip.index}')
        duration = number(clip_durations[clip.index], 'clip.duration')
        if duration <= 0. or not math.isfinite(clock.seconds) or clock.seconds < 0.:
            raise ValueError('Clip duration must be positive and source clock nonnegative')
        result.append(StatePort(clock.state, clip, clock.seconds % duration, weight, write_defaults))
    return tuple(result)
