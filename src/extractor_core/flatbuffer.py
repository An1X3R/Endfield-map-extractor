"""Minimal FlatBuffer reader for Endfield InitChunkData entity columns."""

from __future__ import annotations

import struct


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def i32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def table_fields(data: bytes, table: int) -> list[int]:
    vtable = table - i32(data, table)
    size = u16(data, vtable)
    return [u16(data, vtable + 4 + index * 2) for index in range((size - 4) // 2)]


def field_address(data: bytes, table: int, field_index: int) -> int | None:
    fields = table_fields(data, table)
    if field_index >= len(fields) or not fields[field_index]:
        return None
    return table + fields[field_index]


def offset_target(data: bytes, address: int) -> int:
    return address + u32(data, address)


def vector_from_field(data: bytes, table: int, field_index: int) -> tuple[int, int]:
    address = field_address(data, table, field_index)
    if address is None:
        return 0, 0
    vector = offset_target(data, address)
    return vector + 4, u32(data, vector)


def table_vector_element(data: bytes, vector_data: int, index: int) -> int:
    address = vector_data + index * 4
    return offset_target(data, address)


def wrapped_vector(data: bytes, wrapper_table: int) -> tuple[int, int]:
    return vector_from_field(data, wrapper_table, 0)


def parse_name(raw: bytes) -> str | None:
    name = raw.split(b"\0", 1)[0]
    return name.decode("utf-8", errors="replace") if name else None
