"""Publish managed dynamic channels in captured dictionary enumeration order.

This models CharacterAnimationBlackboard._ApplyDynamicParams, after generated
built-in publication. Fast-handle writes, live ownership discovery, Animator
evaluation order and engine setter internals remain separate inputs.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import struct

ParameterValue = bool | int | float


@dataclass(frozen=True)
class DynamicSlot:
    name_hash: int
    type_then_index: int


@dataclass(frozen=True)
class DynamicState:
    bools: tuple[bool, ...]
    ints: tuple[int, ...]
    floats: tuple[float, ...]
    triggers: tuple[bool, ...]
    dirty: tuple[tuple[int, bool], ...]


@dataclass(frozen=True)
class DynamicWrite:
    name_hash: int
    operation: str
    value: ParameterValue


@dataclass(frozen=True)
class DynamicPublication:
    state: DynamicState
    writes: tuple[DynamicWrite, ...]


def _hash(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 0xffffffff:
        raise ValueError(f'Dynamic parameter hash must be uint32: hash={value!r}')
    return value


def _float(value: float) -> float:
    if type(value) is not float or not math.isfinite(value) or abs(value) > 3.4028234663852886e38:
        raise ValueError(f'Dynamic float must fit the finite float32 domain: value={value!r}')
    return struct.unpack('<f', struct.pack('<f', value))[0]


def _validate(slots: tuple[DynamicSlot, ...], state: DynamicState,
              existing: frozenset[int], built_in: frozenset[int]) -> dict[int, bool]:
    for value in existing | built_in:
        _hash(value)
    if any(type(value) is not bool for value in state.bools + state.triggers):
        raise TypeError('Dynamic bool and trigger storage requires bool values')
    if any(type(value) is not int or not -0x80000000 <= value <= 0x7fffffff for value in state.ints):
        raise ValueError('Dynamic integer storage requires signed int32 values')
    for value in state.floats:
        _float(value)
    dirty = dict(state.dirty)
    if len(dirty) != len(state.dirty) or any(type(value) is not bool for value in dirty.values()):
        raise ValueError('Dynamic dirty storage requires unique hashes and bool values')
    for value in dirty:
        _hash(value)
    seen: set[int] = set()
    for slot in slots:
        _hash(slot.name_hash)
        if slot.name_hash in seen or slot.name_hash in built_in:
            raise ValueError(f'Dynamic parameter ownership conflicts: hash={slot.name_hash}')
        seen.add(slot.name_hash)
        packed = slot.type_then_index
        if type(packed) is not int or not 0 <= packed <= 0x3fff:
            raise ValueError(f'Unsupported dynamic type/index encoding: packed={packed!r}')
        kind, index = packed & 0xf000, packed & 0xfff
        values = {0: state.bools, 0x1000: state.ints, 0x2000: state.floats, 0x3000: state.triggers}[kind]
        if index >= len(values):
            raise ValueError(f'Dynamic typed index is outside storage: hash={slot.name_hash}, type={kind}, index={index}')
    return dirty


def _publish(slots: tuple[DynamicSlot, ...], state: DynamicState,
             existing: frozenset[int], selected: frozenset[int],
             dirty: dict[int, bool]) -> DynamicPublication:
    writes: list[DynamicWrite] = []
    for slot in slots:
        if slot.name_hash not in selected or slot.name_hash not in existing:
            continue
        kind, index = slot.type_then_index & 0xf000, slot.type_then_index & 0xfff
        if kind == 0:
            operation, value = 'set_bool', state.bools[index]
        elif kind == 0x1000:
            operation, value = 'set_int', state.ints[index]
        elif kind == 0x2000:
            operation, value = 'set_float', _float(state.floats[index])
        else:
            value = state.triggers[index]
            operation = 'set_trigger' if value else 'reset_trigger'
        writes.append(DynamicWrite(slot.name_hash, operation, value))
        dirty[slot.name_hash] = False
    result = DynamicState(state.bools, state.ints, state.floats, state.triggers,
                          tuple(dirty.items()))
    return DynamicPublication(result, tuple(writes))


def publish_dirty_dynamic(slots: tuple[DynamicSlot, ...], state: DynamicState,
                          existing: frozenset[int], built_in: frozenset[int]) -> DynamicPublication:
    """Clear only successfully published dirty fields; absent parameters retain dirty."""
    dirty = _validate(slots, state, existing, built_in)
    for slot in slots:
        if slot.name_hash not in dirty:
            raise ValueError(f'Dynamic dirty entry is missing: hash={slot.name_hash}')
    return _publish(slots, state, existing, frozenset(key for key, value in dirty.items() if value), dirty)


def publish_forced_dynamic(slots: tuple[DynamicSlot, ...], state: DynamicState,
                           existing: frozenset[int], built_in: frozenset[int]) -> DynamicPublication:
    """Force bypasses dirty selection but still respects the Animator exist set."""
    dirty = _validate(slots, state, existing, built_in)
    return _publish(slots, state, existing, frozenset(slot.name_hash for slot in slots), dirty)


def merge_dynamic_publication(parameters: Mapping[str, ParameterValue],
                              names: Mapping[int, str], publication: DynamicPublication) -> dict[str, ParameterValue]:
    """Apply actual writes over an already published snapshot without adding defaults."""
    result = dict(parameters)
    for write in publication.writes:
        if write.name_hash not in names or not isinstance(names[write.name_hash], str):
            raise ValueError(f'Published dynamic field lacks an exact source name: hash={write.name_hash}')
        result[names[write.name_hash]] = write.value
    return result
