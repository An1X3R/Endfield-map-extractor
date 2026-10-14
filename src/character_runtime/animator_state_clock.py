"""Advance ordinary leaf clocks with verified source state-evaluation semantics.

The original StateEvaluate derives normalized increments from the source leaf
duration, writes clip seconds, and consumes transition-reset state. This module
supports that path and serialized immediate-transition behavior; it does not
infer external delta time, global speed, runtime layer enable or event order.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from animator_auxiliary_layers import number, object_row, rows
from animator_state_schedule import (SourceMachine, TransitionChoice, StatePort,
    clip_state, select_transition, linear_progress)
from animator_weighted_channels import float32
from character_animator_graph import ClipPointer, _boolean, _integer


@dataclass(frozen=True)
class MachineClockPolicy:
    binding_hash: int
    immediate_transition: bool


@dataclass(frozen=True)
class LeafClockSource:
    state: int
    clip: ClipPointer
    duration: float
    speed: float
    write_defaults_serialized: bool


@dataclass(frozen=True)
class ClockReset:
    offset: float
    remainder_seconds: float


@dataclass(frozen=True)
class LeafClockEvaluation:
    normalized: float
    previous_normalized: float
    duration_seconds: float
    weighted_clip_duration: float
    clip_time_seconds: float
    previous_clip_time_seconds: float
    initialization_pending: bool
    reset_remainder_seconds: float


@dataclass(frozen=True)
class NativeStateClock:
    state: int
    normalized: float


@dataclass(frozen=True)
class NativeClockTransition:
    choice: TransitionChoice
    source: NativeStateClock
    destination: NativeStateClock
    progress: float


@dataclass(frozen=True)
class NativeClockSchedule:
    current: NativeStateClock
    transition: NativeClockTransition | None


def source_clock_policy(tree: dict[str, object], binding_hash: int) -> MachineClockPolicy:
    """Read the actual machine flag; reject unverified synchronized duration graphs."""
    controller = object_row(tree['m_Controller'], 'controller')
    layers = tuple(object_row(object_row(row,'layer')['data'],'layer.data') for row in rows(controller['m_LayerArray'],'layers'))
    matches = tuple(row for row in layers if row['m_Binding']==binding_hash)
    if len(matches)!=1:
        raise ValueError(f'Clock policy requires a unique source layer binding: hash={binding_hash}')
    layer = matches[0]
    if number(layer['m_DefaultWeight'],'layer.default_weight')!=1. or layer['m_SyncedLayerAffectsTiming'] is not False:
        raise ValueError('Native leaf clock supports one unsynchronized source layer with unit serialized weight')
    machines = rows(controller['m_StateMachineArray'],'machines')
    index = _integer(layer['m_StateMachineIndex'],'machine.index')
    if not 0<=index<len(machines):
        raise ValueError(f'Clock policy machine index is out of range: index={index}')
    machine = object_row(object_row(machines[index],'machine')['data'],'machine.data')
    if _integer(machine['m_SynchronizedLayerCount'],'machine.synchronized_layer_count')!=1:
        raise ValueError('Multiple synchronized source layer duration accumulation is not implemented')
    return MachineClockPolicy(binding_hash,_boolean(machine['m_ImmediateTransition'],'machine.immediate_transition'))


def leaf_clock_source(machine: SourceMachine, state: int, parameters: dict[int, float | int | bool],
                      clip_durations: dict[int, float]) -> LeafClockSource:
    """Join the entered serialized leaf to its exact validated source duration."""
    clip,speed,write_defaults = clip_state(machine,state,parameters)
    if clip.index not in clip_durations:
        raise ValueError(f'Entered leaf clip has no verified source duration: clip={clip.index}')
    duration = float32(number(clip_durations[clip.index],'clip.duration'))
    if duration<=0.:
        raise ValueError(f'Native source leaf duration must be positive: clip={clip.index}, duration={duration}')
    return LeafClockSource(state,clip,duration,speed,write_defaults)


def evaluate_leaf_clock(source: LeafClockSource, policy: MachineClockPolicy, normalized: float,
                        seconds: float, global_speed: float, reset: ClockReset | None) -> LeafClockEvaluation:
    """Match full native leaf-clock arithmetic and source immediate/deferred reset."""
    previous = float32(number(normalized,'clock.normalized'))
    delta = float32(number(seconds,'clock.delta_seconds'))
    multiplier = float32(number(global_speed,'clock.global_speed'))
    duration = float32(number(source.duration,'source.duration'))
    source_speed = float32(number(source.speed,'source.speed'))
    if previous<0. or delta<0. or multiplier<0. or duration<=0. or source_speed<=0.:
        raise ValueError('Native leaf clock requires nonnegative clock/delta/global speed and positive source speed/duration')
    speed = float32(source_speed*multiplier)
    effective_duration = float32(duration/speed) if speed!=0. else math.inf
    increment = float32(float32(speed*delta)/duration)
    if reset is not None:
        offset = float32(number(reset.offset,'reset.offset'))
        remainder = float32(number(reset.remainder_seconds,'reset.remainder_seconds'))
        previous = float32(float32(remainder/effective_duration)+offset)
        if policy.immediate_transition:
            current = float32(previous+increment)
        else:
            current = previous
            previous = float32(current-increment)
    else:
        current = float32(previous+increment)
    return LeafClockEvaluation(current,previous,effective_duration,duration,
        float32(duration*current),float32(duration*previous),False,0.)


def advance_native_clock_schedule(machine: SourceMachine, policy: MachineClockPolicy,
                                  schedule: NativeClockSchedule, parameters: dict[int, float | int | bool],
                                  clip_durations: dict[int, float], seconds: float, global_speed: float) -> NativeClockSchedule:
    """Follow the verified current-advance -> condition-select -> destination-evaluate order."""
    if policy.binding_hash!=machine.binding_hash:
        raise ValueError('Native clock policy belongs to a different source machine binding')
    delta = number(seconds,'clock.delta_seconds')
    source_point = schedule.current if schedule.transition is None else schedule.transition.source
    source = leaf_clock_source(machine,source_point.state,parameters,clip_durations)
    advanced = evaluate_leaf_clock(source,policy,source_point.normalized,delta,global_speed,None)
    source_point = NativeStateClock(source.state,advanced.normalized)
    transition = schedule.transition
    reset: ClockReset | None = None
    if transition is None:
        choice = select_transition(machine,source.state,parameters)
        if choice is None:
            return NativeClockSchedule(source_point,None)
        destination = NativeStateClock(choice.destination,0.)
        transition = NativeClockTransition(choice,source_point,destination,0.)
        reset = ClockReset(choice.offset,0.)
    else:
        for clock in (transition.source,transition.destination):
            eligible = select_transition(machine,clock.state,parameters)
            if eligible is not None and eligible!=transition.choice:
                raise ValueError(f'Native clock schedule interruption is not implemented: choice={eligible}')
    if float32(number(global_speed,'clock.global_speed'))!=1.:
        raise ValueError('Fixed transition progress currently requires explicit unit global speed; nonunit native tick progress dispatch is not verified')
    target = leaf_clock_source(machine,transition.destination.state,parameters,clip_durations)
    evaluated = evaluate_leaf_clock(target,policy,transition.destination.normalized,delta,global_speed,reset)
    destination = NativeStateClock(target.state,evaluated.normalized)
    progress,weight = linear_progress(transition.progress,delta,transition.choice.duration)
    if weight==1.:
        return NativeClockSchedule(destination,None)
    return NativeClockSchedule(source_point,NativeClockTransition(transition.choice,source_point,destination,progress))


def native_clock_ports(machine: SourceMachine, schedule: NativeClockSchedule,
                       parameters: dict[int, float | int | bool], clip_durations: dict[int, float]) -> tuple[StatePort, ...]:
    """Expose native unwrapped clip seconds; the existing consumer validates stored coverage."""
    transition = schedule.transition
    clocks = ((schedule.current,1.),) if transition is None else ((transition.source,float32(1.-transition.progress)),(transition.destination,transition.progress))
    result: list[StatePort] = []
    for clock,weight in clocks:
        source = leaf_clock_source(machine,clock.state,parameters,clip_durations)
        if not math.isfinite(clock.normalized) or clock.normalized<0.:
            raise ValueError('Native source state clock must be finite and nonnegative')
        result.append(StatePort(source.state,source.clip,float32(clock.normalized*source.duration),weight,source.write_defaults_serialized))
    return tuple(result)
