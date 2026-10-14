"""Consume the verified managed CharacterAnimationBlackboard caller boundaries.

ComplexAnimatorComponent.Tick invokes Blackboard.OnUpdate (slot 9), then
PostBlackboardTick (slot 92), then the specialized ApplyToAnimator thunk. This
module consumes explicit producer events rather than inventing their values or
the engine evaluation clock. The managed, non-IFix path is the only supported
execution path; fast-handle setup and Unity Animator evaluation remain separate.
"""
from __future__ import annotations

from dataclasses import dataclass

from animator_dynamic_lifecycle import DynamicFloatEvent, DynamicRegistry, empty_dynamic_registry, set_dynamic_float
from animator_discrete_lifecycle import set_dynamic_bool, set_dynamic_int
from animator_dynamic_publication import DynamicPublication, _hash, _validate, publish_dirty_dynamic, publish_forced_dynamic


@dataclass(frozen=True)
class ManagedBlackboardSetup:
    registry: DynamicRegistry
    existing_hashes: frozenset[int]
    dynamic_publication: DynamicPublication


@dataclass(frozen=True)
class DynamicBoolEvent:
    name_hash: int
    value: bool


@dataclass(frozen=True)
class DynamicIntEvent:
    name_hash: int
    value: int


ManagedDynamicEvent = DynamicFloatEvent | DynamicBoolEvent | DynamicIntEvent


@dataclass(frozen=True)
class ManagedBlackboardTick:
    on_update_events: tuple[ManagedDynamicEvent, ...]
    post_blackboard_events: tuple[ManagedDynamicEvent, ...]
    existing_hashes: frozenset[int]
    animator_alive: bool
    blackboard_initialized: bool


@dataclass(frozen=True)
class ManagedBlackboardTickResult:
    registry: DynamicRegistry
    publication: DynamicPublication


def setup_managed_dynamic_registration(registry: DynamicRegistry, parameter_hashes: tuple[int, ...],
                                       built_in: frozenset[int]) -> ManagedBlackboardSetup:
    """Clear registrations and replace the declaration set before forced Apply.

This is the dynamic portion of CharacterAnimationBlackboard.Setup: built-in
reset/publication is outside this result. Native ___dynamicParamCount survives
Setup; it is a registration statistic, not a list index or this registry's size.
No Animator default values or missing setter events are fabricated here.
"""
    _validate(registry.slots, registry.state, frozenset(), built_in)
    existing = frozenset(_hash(value) for value in parameter_hashes)
    cleared = empty_dynamic_registry()
    publication = publish_forced_dynamic(cleared.slots, cleared.state, existing, built_in)
    return ManagedBlackboardSetup(cleared, existing, publication)


def apply_managed_blackboard_tick(registry: DynamicRegistry, tick: ManagedBlackboardTick,
                                 built_in: frozenset[int]) -> ManagedBlackboardTickResult:
    """Replay both source caller producer groups before gated managed publication.

An uninitialized blackboard or dead Animator skips Apply only; producer events
still execute and dirty entries remain pending, matching the original caller.
Caller admission, IFix, post-tick gameplay work and engine evaluation are not
scheduled by this function.
"""
    if type(tick.animator_alive) is not bool or type(tick.blackboard_initialized) is not bool:
        raise TypeError('Captured managed Apply gates require exact bool values')
    _validate(registry.slots, registry.state, tick.existing_hashes, built_in)
    current = registry
    for event in tick.on_update_events + tick.post_blackboard_events:
        if isinstance(event, DynamicFloatEvent):
            current = set_dynamic_float(current, event.name_hash, event.value, built_in, None).registry
        elif isinstance(event, DynamicBoolEvent):
            current = set_dynamic_bool(current, event.name_hash, event.value, built_in, None).registry
        elif isinstance(event, DynamicIntEvent):
            current = set_dynamic_int(current, event.name_hash, event.value, built_in, None).registry
        else:
            raise TypeError(f'Unsupported managed dynamic event type: type={type(event).__name__}')
    publication = (publish_dirty_dynamic(current.slots, current.state, tick.existing_hashes, built_in)
                   if tick.animator_alive and tick.blackboard_initialized
                   else DynamicPublication(current.state, ()))
    return ManagedBlackboardTickResult(DynamicRegistry(current.slots, publication.state), publication)
