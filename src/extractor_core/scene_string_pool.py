"""Read the versioned pooled strings referenced by current LevelConfig records."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import struct


class SceneStringPoolError(ValueError):
    """A source string pool violates its declared header or payload bounds."""


def _extended_length(data: bytes, position: int, length: int) -> tuple[int, int]:
    if length != 15:
        return position, length
    while position < len(data):
        value = data[position]
        position += 1
        length += value
        if value != 255:
            return position, length
    raise SceneStringPoolError("Truncated string-pool LZ4 length")


def _pool_lz4(data: bytes, expected: int) -> bytes:
    """Decode bounded standard tokens, including source blocks ending in a match."""
    output = bytearray()
    position = 0
    while position < len(data):
        token = data[position]
        position += 1
        position, literals = _extended_length(data, position, token >> 4)
        if position + literals > len(data) or len(output) + literals > expected:
            raise SceneStringPoolError(f"String-pool LZ4 literal exceeds bounds: offset={position}, length={literals}")
        output.extend(data[position:position + literals])
        position += literals
        if position == len(data):
            break
        if position + 2 > len(data):
            raise SceneStringPoolError("Truncated string-pool LZ4 match offset")
        distance = int.from_bytes(data[position:position + 2], "little")
        position += 2
        position, length = _extended_length(data, position, token & 15)
        length += 4
        if distance < 1 or distance > len(output) or len(output) + length > expected:
            raise SceneStringPoolError(f"String-pool LZ4 match exceeds bounds: offset={position}, distance={distance}, length={length}")
        while length:
            block = output[-distance:][:length]
            output.extend(block)
            length -= len(block)
    if len(output) != expected:
        raise SceneStringPoolError(f"String-pool LZ4 size differs: expected={expected}, actual={len(output)}")
    return bytes(output)


def _pool_strings(data: bytes, path: str) -> tuple[str, ...]:
    if len(data) < 24:
        raise SceneStringPoolError(f"Truncated string-pool header: path={path}")
    magic, version, codec, count, clear_size, packed_size = struct.unpack_from("<4s5i", data)
    if (magic != b"USPC" or version != 1 or codec != 1 or not 1 <= count <= 10000
            or not 4 + count * 8 <= clear_size <= 16 * 1024 * 1024 or packed_size != len(data) - 24):
        raise SceneStringPoolError(f"Unsupported string-pool header: path={path}, header={(magic, version, codec, count, clear_size, packed_size)}")
    clear = _pool_lz4(data[24:], clear_size)
    if struct.unpack_from("<i", clear)[0] != count:
        raise SceneStringPoolError(f"String-pool count differs: path={path}")
    offsets = struct.unpack_from(f"<{count}i", clear, 4)
    base = 4 + count * 4
    if offsets[0] != 0 or any(first >= second for first, second in zip(offsets, offsets[1:])):
        raise SceneStringPoolError(f"String-pool offsets are not strictly ordered: path={path}")
    result: list[str] = []
    for offset, end in zip(offsets, (*offsets[1:], len(clear) - base)):
        if offset < 0 or end > len(clear) - base or end - offset < 4:
            raise SceneStringPoolError(f"String-pool entry exceeds payload: path={path}, offset={offset}, end={end}")
        length = struct.unpack_from("<i", clear, base + offset)[0]
        if length < 0 or length != end - offset - 4:
            raise SceneStringPoolError(f"String-pool entry length differs: path={path}, offset={offset}, length={length}")
        result.append(clear[base + offset + 4:base + end].decode("utf-8"))
    return tuple(result)


def read_scene_string_pool(read: Callable[[str], bytes]) -> Mapping[int, str]:
    """Decode source pool zero's declared final index and all contiguous USPC pools."""
    prefix = "Data/Json/StringPool/StringPool"
    first = read(prefix + "0.byte")
    if len(first) < 4:
        raise SceneStringPoolError("Truncated initial string-pool inventory")
    last = struct.unpack_from("<i", first)[0]
    if not 0 <= last < 1024:
        raise SceneStringPoolError(f"Invalid final string-pool index: index={last}")
    result: dict[int, str] = {}
    for index in range(last + 1):
        path = f"{prefix}{index}.byte"
        values = _pool_strings(first[4:] if index == 0 else read(path), path)
        if index < last and len(values) != 10000:
            raise SceneStringPoolError(f"Non-final string pool requires 10000 entries: path={path}, count={len(values)}")
        result.update((index * 10000 + offset, value) for offset, value in enumerate(values))
    return result
