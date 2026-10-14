"""Replay source bool/int setter events before explicit Animator publication.

The original setters write a valid fast buffer before managed registration and
latch dirty even when a later event restores the previous value. Built-in
dispatch, allocation, IFix replacement and event timing are outside this module.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from animator_dynamic_lifecycle import DynamicRegistry
from animator_dynamic_publication import DynamicSlot, _hash, _validate


@dataclass(frozen=True)
class DynamicBoolBuffer:
    hash_indices: tuple[tuple[int, int], ...]
    values: tuple[bool, ...]


@dataclass(frozen=True)
class DynamicIntBuffer:
    hash_indices: tuple[tuple[int, int], ...]
    values: tuple[int, ...]


@dataclass(frozen=True)
class FastBoolWrite:
    name_hash: int
    buffer_index: int
    value: bool


@dataclass(frozen=True)
class FastIntWrite:
    name_hash: int
    buffer_index: int
    value: int


@dataclass(frozen=True)
class DynamicBoolSet:
    registry: DynamicRegistry
    buffer: DynamicBoolBuffer | None
    fast_writes: tuple[FastBoolWrite, ...]


@dataclass(frozen=True)
class DynamicIntSet:
    registry: DynamicRegistry
    buffer: DynamicIntBuffer | None
    fast_writes: tuple[FastIntWrite, ...]


def _bool(value: bool) -> bool:
    if type(value) is not bool:
        raise TypeError(f'Dynamic bool requires a Boolean: value={value!r}')
    return value


def _int32(value: int) -> int:
    if type(value) is not int or not -(1 << 31) <= value < 1 << 31:
        raise ValueError(f'Dynamic integer requires signed int32: value={value!r}')
    return value


def _binding_index(bindings: tuple[tuple[int, int], ...], count: int, name_hash: int) -> int | None:
    indices = dict(bindings)
    if len(indices) != len(bindings):
        raise ValueError('Captured discrete fast binding contains duplicate hashes')
    for key, index in bindings:
        _hash(key)
        if type(index) is not int or not 0 <= index < min(count, 0x10000):
            raise ValueError(f'Captured discrete fast index outside buffer: hash={key}, index={index}')
    return indices.get(name_hash)


def _existing_slot(registry: DynamicRegistry, name_hash: int, built_in: frozenset[int]) -> tuple[DynamicSlot | None, dict[int, bool]]:
    _hash(name_hash)
    dirty = _validate(registry.slots, registry.state, frozenset(), built_in)
    if name_hash in built_in:
        raise ValueError(f'Dynamic setter cannot own a built-in field: hash={name_hash}')
    slot = next((item for item in registry.slots if item.name_hash == name_hash), None)
    if (slot is None) == (name_hash in dirty):
        raise ValueError(f'Dynamic registration and dirty ownership disagree: hash={name_hash}')
    return slot, dirty


def set_dynamic_bool(registry: DynamicRegistry, name_hash: int, value: bool,
                     built_in: frozenset[int], buffer: DynamicBoolBuffer | None) -> DynamicBoolSet:
    """Consume an explicit Boolean event after built-in dispatch rejected its hash."""
    value = _bool(value)
    slot, dirty = _existing_slot(registry, name_hash, built_in)
    slots, state = registry.slots, registry.state
    if slot is None:
        index = len(state.bools)
        if index > 0xfff:
            raise ValueError(f'Dynamic bool exceeds packed index domain: index={index}')
        slots += (DynamicSlot(name_hash, index),)
        state = replace(state, bools=state.bools + (False,))
        dirty[name_hash] = True
    else:
        if slot.type_then_index & 0xf000 != 0:
            raise ValueError(f'Dynamic bool hash belongs to another type: hash={name_hash}')
        index = slot.type_then_index & 0xfff
    dirty[name_hash] = dirty[name_hash] or state.bools[index] != value
    state = replace(state, bools=state.bools[:index] + (value,) + state.bools[index + 1:], dirty=tuple(dirty.items()))
    result = DynamicRegistry(slots, state)
    if buffer is None:
        return DynamicBoolSet(result, None, ())
    values = tuple(_bool(item) for item in buffer.values)
    index = _binding_index(buffer.hash_indices, len(values), name_hash)
    if index is None:
        return DynamicBoolSet(result, DynamicBoolBuffer(buffer.hash_indices, values), ())
    updated = DynamicBoolBuffer(buffer.hash_indices, values[:index] + (value,) + values[index + 1:])
    return DynamicBoolSet(result, updated, (FastBoolWrite(name_hash, index, value),))


def set_dynamic_int(registry: DynamicRegistry, name_hash: int, value: int,
                    built_in: frozenset[int], buffer: DynamicIntBuffer | None) -> DynamicIntSet:
    """Consume an explicit signed integer event without coercion or wrapping."""
    value = _int32(value)
    slot, dirty = _existing_slot(registry, name_hash, built_in)
    slots, state = registry.slots, registry.state
    if slot is None:
        index = len(state.ints)
        if index > 0xfff:
            raise ValueError(f'Dynamic integer exceeds packed index domain: index={index}')
        slots += (DynamicSlot(name_hash, 0x1000 | index),)
        state = replace(state, ints=state.ints + (0,))
        dirty[name_hash] = True
    else:
        if slot.type_then_index & 0xf000 != 0x1000:
            raise ValueError(f'Dynamic integer hash belongs to another type: hash={name_hash}')
        index = slot.type_then_index & 0xfff
    dirty[name_hash] = dirty[name_hash] or state.ints[index] != value
    state = replace(state, ints=state.ints[:index] + (value,) + state.ints[index + 1:], dirty=tuple(dirty.items()))
    result = DynamicRegistry(slots, state)
    if buffer is None:
        return DynamicIntSet(result, None, ())
    values = tuple(_int32(item) for item in buffer.values)
    index = _binding_index(buffer.hash_indices, len(values), name_hash)
    if index is None:
        return DynamicIntSet(result, DynamicIntBuffer(buffer.hash_indices, values), ())
    updated = DynamicIntBuffer(buffer.hash_indices, values[:index] + (value,) + values[index + 1:])
    return DynamicIntSet(result, updated, (FastIntWrite(name_hash, index, value),))
