"""Replay dynamic float registration and setters before explicit Animator publication.

SetFloat_Internal writes an available fast handle first, lazily registers a
managed float slot, then latches dirty and always replaces stored float32 data.
An explicit caller boundary is required; this does not schedule game callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from animator_dynamic_publication import DynamicPublication, DynamicSlot, DynamicState, _float, _hash, _validate, publish_dirty_dynamic


@dataclass(frozen=True)
class DynamicRegistry:
    slots: tuple[DynamicSlot, ...]
    state: DynamicState


@dataclass(frozen=True)
class DynamicFloatBuffer:
    hash_indices: tuple[tuple[int, int], ...]
    values: tuple[float, ...]


@dataclass(frozen=True)
class FastFloatWrite:
    name_hash: int
    buffer_index: int
    value: float


@dataclass(frozen=True)
class DynamicFloatSet:
    registry: DynamicRegistry
    buffer: DynamicFloatBuffer | None
    fast_writes: tuple[FastFloatWrite, ...]


@dataclass(frozen=True)
class DynamicFloatEvent:
    name_hash: int
    value: float


@dataclass(frozen=True)
class ManagedFloatBoundary:
    setter_events: tuple[DynamicFloatEvent, ...]
    existing_hashes: frozenset[int]


@dataclass(frozen=True)
class ManagedFloatReplay:
    registry: DynamicRegistry
    publications: tuple[DynamicPublication, ...]


def empty_dynamic_registry() -> DynamicRegistry:
    """Create an explicit empty managed registration scenario, not a live capture."""
    return DynamicRegistry((), DynamicState((), (), (), (), ()))


def _write_fast_float(buffer: DynamicFloatBuffer, name_hash: int, value: float) -> tuple[DynamicFloatBuffer, tuple[FastFloatWrite, ...]]:
    indices = dict(buffer.hash_indices)
    if len(indices) != len(buffer.hash_indices):
        raise ValueError('Captured fast float binding contains duplicate hashes')
    for key, index in buffer.hash_indices:
        _hash(key)
        if type(index) is not int or not 0 <= index < min(len(buffer.values), 0x10000):
            raise ValueError(f'Captured fast float index is outside buffer: hash={key}, index={index}')
    values = tuple(_float(item) for item in buffer.values)
    if name_hash not in indices:
        return DynamicFloatBuffer(buffer.hash_indices, values), ()
    index = indices[name_hash]
    updated = values[:index] + (value,) + values[index + 1:]
    return DynamicFloatBuffer(buffer.hash_indices, updated), (FastFloatWrite(name_hash, index, value),)


def set_dynamic_float(registry: DynamicRegistry, name_hash: int, value: float,
                      built_in: frozenset[int], buffer: DynamicFloatBuffer | None) -> DynamicFloatSet:
    """Consume a setter event; buffer=None means the native fast-valid gate is off.

The built-in virtual dispatch must already have rejected this hash. Cross-type
reuse is rejected explicitly: native masks the existing index without checking
its type and could read unrelated storage, which is outside this float contract.
"""
    _hash(name_hash)
    value = _float(value)
    dirty = _validate(registry.slots, registry.state, frozenset(), built_in)
    if name_hash in built_in:
        raise ValueError(f'Dynamic float setter cannot own a built-in field: hash={name_hash}')
    slots, state = registry.slots, registry.state
    slot = next((item for item in slots if item.name_hash == name_hash), None)
    if slot is None:
        index = len(state.floats)
        if index > 0xfff:
            raise ValueError(f'Dynamic float registration exceeds packed index domain: index={index}')
        if name_hash in dirty:
            raise ValueError(f'Unregistered float already owns a dirty entry: hash={name_hash}')
        slots += (DynamicSlot(name_hash, 0x2000 | index),)
        state = replace(state, floats=state.floats + (0.,))
        dirty[name_hash] = True
    else:
        if slot.type_then_index & 0xf000 != 0x2000:
            raise ValueError(f'Dynamic float hash already belongs to another type: hash={name_hash}')
        index = slot.type_then_index & 0xfff
        if name_hash not in dirty:
            raise ValueError(f'Registered float has no dirty entry: hash={name_hash}')
    dirty[name_hash] = dirty[name_hash] or _float(state.floats[index]) != value
    floats = state.floats[:index] + (value,) + state.floats[index + 1:]
    result = DynamicRegistry(slots, replace(state, floats=floats, dirty=tuple(dirty.items())))
    if buffer is None:
        return DynamicFloatSet(result, None, ())
    updated, writes = _write_fast_float(buffer, name_hash, value)
    return DynamicFloatSet(result, updated, writes)


def replay_managed_float_boundaries(registry: DynamicRegistry, boundaries: tuple[ManagedFloatBoundary, ...],
                                    built_in: frozenset[int]) -> ManagedFloatReplay:
    """Replay explicit ordered setter events and managed Apply boundaries.

Missing fields retain their dirty state across supplied declaration changes.
No implicit Apply, fast-mode change, engine evaluation or controller reset is
inserted. Multiple setter events in one boundary preserve a changed-then-restored
dirty latch rather than comparing only the last value with the prior frame.
"""
    publications: list[DynamicPublication] = []
    current = registry
    for boundary in boundaries:
        for event in boundary.setter_events:
            current = set_dynamic_float(current, event.name_hash, event.value, built_in, None).registry
        publication = publish_dirty_dynamic(current.slots, current.state, boundary.existing_hashes, built_in)
        current = DynamicRegistry(current.slots, publication.state)
        publications.append(publication)
    return ManagedFloatReplay(current, tuple(publications))
