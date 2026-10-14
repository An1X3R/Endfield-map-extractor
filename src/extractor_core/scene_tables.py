"""Read the self-describing binary tables used by the scene directory.

Offsets are absolute within the table. Readers return an advanced offset rather
than modifying caller state, and malformed or unsupported values fail explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import TypeAlias


TableValue: TypeAlias = str | int | float | bool | None | list["TableValue"] | dict[str, "TableValue"]


class SceneTableError(ValueError):
    """A scene table violates its declared binary layout."""


@dataclass(frozen=True)
class _Type:
    kind: int
    first: int | None
    second: int | None
    first_hash: int | None
    second_hash: int | None


Fields: TypeAlias = tuple[tuple[str, _Type], ...]


def _range(data: bytes, offset: int, size: int) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise SceneTableError(f"Table offset outside payload: offset={offset}, size={size}, bytes={len(data)}")


def _number(data: bytes, offset: int, code: str, alignment: int) -> tuple[int | float, int]:
    position = (offset + alignment - 1) & -alignment
    size = struct.calcsize(code)
    _range(data, position, size)
    return struct.unpack_from(code, data, position)[0], position + size


def _integer(data: bytes, offset: int) -> tuple[int, int]:
    value, position = _number(data, offset, "<i", 1)
    return int(value), position


def _string(data: bytes, offset: int) -> tuple[str, int]:
    _range(data, offset, 1)
    end = data.find(b"\0", offset)
    if end < 0:
        raise SceneTableError(f"Unterminated table string: offset={offset}")
    return data[offset:end].decode("utf-8"), end + 1


def _kind(data: bytes, offset: int) -> tuple[int, int]:
    _range(data, offset, 1)
    kind = data[offset]
    if kind > 10:
        raise SceneTableError(f"Unsupported table type: offset={offset}, kind={kind}")
    return kind, offset + 1


def _hash(data: bytes, offset: int, kind: int) -> tuple[int | None, int]:
    if kind not in (6, 8):
        return None, offset
    return _integer(data, (offset + 3) & -4)


def _descriptor(data: bytes, offset: int, kind: int) -> tuple[_Type, int]:
    if kind in (6, 8):
        reference, position = _hash(data, offset, kind)
        return _Type(kind, None, None, reference, None), position
    if kind == 9:
        item, position = _kind(data, offset)
        reference, position = _hash(data, position, item)
        return _Type(kind, item, None, reference, None), position
    if kind == 10:
        key, position = _kind(data, offset)
        value, position = _kind(data, position)
        key_hash, position = _hash(data, position, key)
        value_hash, position = _hash(data, position, value)
        return _Type(kind, key, value, key_hash, value_hash), position
    return _Type(kind, None, None, None, None), offset


def _count(data: bytes, offset: int) -> tuple[int, int]:
    count, position = _integer(data, offset)
    if count < 0 or count > len(data):
        raise SceneTableError(f"Invalid table collection count: offset={offset}, count={count}")
    return count, position


def _definitions(data: bytes, offset: int) -> dict[int, Fields]:
    count, position = _count(data, offset)
    beans: dict[int, Fields] = {}
    identifiers: set[int] = set()
    for _ in range(count):
        kind, position = _kind(data, position)
        identifier, position = _integer(data, (position + 3) & -4)
        _, position = _string(data, position)
        entries, position = _count(data, (position + 3) & -4)
        if identifier in identifiers:
            raise SceneTableError(f"Duplicate table type identifier: identifier={identifier}")
        identifiers.add(identifier)
        fields: list[tuple[str, _Type]] = []
        for _ in range(entries):
            name, position = _string(data, position)
            if kind == 6:
                _, position = _integer(data, (position + 3) & -4)
            elif kind == 8:
                field_kind, position = _kind(data, position)
                specification, position = _descriptor(data, position, field_kind)
                if any(existing == name for existing, _ in fields):
                    raise SceneTableError(f"Duplicate table field: type={identifier}, field={name}")
                fields.append((name, specification))
            else:
                raise SceneTableError(f"Unsupported table definition: kind={kind}, identifier={identifier}")
        if kind == 8:
            beans[identifier] = tuple(fields)
    return beans


def _fields(beans: dict[int, Fields], identifier: int | None) -> Fields:
    if identifier is None or identifier not in beans:
        raise SceneTableError(f"Table references an undefined bean: identifier={identifier}")
    return beans[identifier]


def _bean(data: bytes, offset: int, fields: Fields, beans: dict[int, Fields], depth: int) -> tuple[TableValue, int]:
    result: dict[str, TableValue] = {}
    position = offset
    for index, (name, specification) in enumerate(fields):
        value, position = _value(data, position, specification, beans, depth + 1)
        result[name] = value
        if specification.kind == 0 and index + 1 < len(fields) and fields[index + 1][1].kind != 0:
            position = (position + 3) & -4
    return result, position


def _mapping(data: bytes, offset: int, specification: _Type, beans: dict[int, Fields], depth: int) -> TableValue:
    count, position = _count(data, offset)
    _range(data, position, count * 8)
    position += count * 8
    key_type = _Type(int(specification.first), None, None, specification.first_hash, None)
    value_type = _Type(int(specification.second), None, None, specification.second_hash, None)
    result: dict[str, TableValue] = {}
    for _ in range(count):
        key, position = _value(data, position, key_type, beans, depth + 1)
        if not isinstance(key, (str, int)) or isinstance(key, bool):
            raise SceneTableError(f"Unsupported table map key: offset={position}, type={type(key).__name__}")
        value, position = _value(data, position, value_type, beans, depth + 1)
        name = str(key)
        if name in result:
            raise SceneTableError(f"Duplicate table map key: key={name}")
        result[name] = value
        if value_type.kind == 0:
            position = (position + 3) & -4
    return result


def _value(data: bytes, offset: int, specification: _Type, beans: dict[int, Fields], depth: int) -> tuple[TableValue, int]:
    if depth > 64:
        raise SceneTableError(f"Cyclic or excessively nested table value: offset={offset}, depth={depth}")
    kind = specification.kind
    if kind in (0, 1):
        _range(data, offset, 1)
        if kind == 0 and data[offset] not in (0, 1):
            raise SceneTableError(f"Invalid table boolean: offset={offset}, value={data[offset]}")
        return (bool(data[offset]) if kind == 0 else data[offset]), offset + 1
    numeric = {2: ("<i", 1), 3: ("<q", 8), 4: ("<f", 1), 5: ("<d", 8), 6: ("<i", 1)}
    if kind in numeric:
        code, alignment = numeric[kind]
        return _number(data, offset, code, alignment)
    pointer, position = _integer(data, offset)
    if pointer == -1:
        return ("" if kind == 7 else None), position
    _range(data, pointer, 1)
    if kind == 7:
        return _string(data, pointer)[0], position
    if kind == 8:
        return _bean(data, pointer, _fields(beans, specification.first_hash), beans, depth)[0], position
    if kind == 9:
        count, item_position = _count(data, pointer)
        item_type = _Type(int(specification.first), None, None, specification.first_hash, None)
        items: list[TableValue] = []
        for _ in range(count):
            item, item_position = _value(data, item_position, item_type, beans, depth + 1)
            items.append(item)
        return items, position
    if kind == 10:
        return _mapping(data, pointer, specification, beans, depth), position
    raise SceneTableError(f"Unsupported table value: offset={offset}, kind={kind}")


def read_scene_table(data: bytes, expected_name: str) -> dict[str, TableValue]:
    """Read a named root map, retaining required and extra source fields unchanged."""
    _range(data, 0, 12)
    definitions, root, values = struct.unpack_from("<iii", data)
    beans = _definitions(data, definitions)
    kind, position = _kind(data, root)
    name, position = _string(data, position)
    if name != expected_name or kind != 10:
        raise SceneTableError(f"Unexpected scene table root: expected={expected_name}, actual={name}, kind={kind}")
    specification, _ = _descriptor(data, position, kind)
    result = _mapping(data, values, specification, beans, 0)
    if not isinstance(result, dict):
        raise SceneTableError(f"Scene table root is not a map: table={expected_name}")
    return result
