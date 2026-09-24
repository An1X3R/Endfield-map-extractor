"""Join decal transforms and material IDs from the two authoritative chunk files.

The 160-byte RectDecal record matches HGRP/DecalLit's ten float4 instance
constants. Its local clipping volume is [-0.5, 0.5]^3; UV uses local X/Z.
The last word is packed runtime flags, not an ordinary floating-point value.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import flatbuffer as fb
from .lz4 import decompress_lz4_inv
from .vfs import VfsFile
from endfield_projected_decal_contract import ProjectedDecal


@dataclass(frozen=True)
class StreamingRecord:
    entity_id: int
    type_tag: int
    start: int
    end: int


def read_chunk(vfs_root: Path, entry: VfsFile) -> bytes:
    """Read and authenticate an existing streaming VFS entry without writes."""
    path = vfs_root / "C3442D43" / (entry.file_chunk_md5_name + ".chk")
    with path.open("rb") as stream:
        stream.seek(entry.offset)
        packed = stream.read(entry.length)
    digest = hashlib.md5(packed).hexdigest()
    if len(packed) != entry.length or digest.casefold() != entry.file_data_md5.casefold():
        raise ValueError(f"VFS_MD5_MISMATCH path={entry.file_name} actual={digest} expected={entry.file_data_md5}")
    expected = struct.unpack_from("<I", packed)[0]
    return decompress_lz4_inv(packed[4:], expected)


def build_path_id_index(path: Path) -> dict[bytes, tuple[str, ...]]:
    """Reuse the recovered path-ID/relative-string-offset scan, without name guesses."""
    data = path.read_bytes()
    offset, count = struct.unpack_from("<II", data)
    if offset != 8 + 24 * count or offset > len(data):
        raise ValueError(f"PATH_HASH_HEADER_INVALID path={path} offset={offset} count={count}")
    found: dict[bytes, set[str]] = defaultdict(set)
    for position in range(8, offset - 8, 8):
        identifier = data[position:position + 8]
        relative = struct.unpack_from("<Q", data, position + 8)[0]
        start = offset + relative
        if not any(identifier) or start + 6 > len(data):
            continue
        length = struct.unpack_from("<I", data, start)[0]
        end = start + 4 + length
        if not length or length % 2 or end + 2 > len(data) or data[end:end + 2] != b"\0\0":
            continue
        try:
            value = data[start + 4:end].decode("utf-16le")
        except UnicodeDecodeError:
            continue
        if value.casefold().startswith(("assets/", "data/", "packages/")):
            found[identifier].add(value)
    return {key: tuple(sorted(values)) for key, values in found.items()}


def streaming_records(data: bytes) -> dict[int, StreamingRecord]:
    """Reuse the Stage66 parallel ID/type/table record layout."""
    root = fb.u32(data, 0)
    ids, count = fb.vector_from_field(data, root, 3)
    tags, tag_count = fb.vector_from_field(data, root, 4)
    tables, table_count = fb.vector_from_field(data, root, 5)
    if count != tag_count or count != table_count:
        raise ValueError(f"STREAMING_VECTOR_MISMATCH ids={count} tags={tag_count} tables={table_count}")
    starts = [fb.table_vector_element(data, tables, i) for i in range(count)]
    ordered = sorted(set(starts))
    ends = dict(zip(ordered, ordered[1:] + [ids - 4], strict=True))
    result: dict[int, StreamingRecord] = {}
    for i, start in enumerate(starts):
        entity_id = fb.u32(data, ids + 4 * i)
        if entity_id in result or not 0 <= start < ends[start] <= len(data):
            raise ValueError(f"STREAMING_RECORD_INVALID entity_id={entity_id} start={start}")
        result[entity_id] = StreamingRecord(entity_id, data[tags + i], start, ends[start])
    return result


def rect_decal_records(data: bytes) -> dict[int, bytes]:
    """Read every type-27 column with exact entity IDs and validated strides."""
    root = fb.u32(data, 0)
    ids, id_count = fb.vector_from_field(data, root, 6)
    groups, group_count = fb.vector_from_field(data, root, 7)
    if id_count != group_count:
        raise ValueError("INIT_GROUP_COUNT_MISMATCH")
    result: dict[int, bytes] = {}
    for group_index in range(group_count):
        group = fb.table_vector_element(data, groups, group_index)
        count_address = fb.field_address(data, group, 1)
        blob_address = fb.field_address(data, group, 4)
        if count_address is None or blob_address is None:
            raise ValueError(f"INIT_GROUP_FIELD_MISSING group={group_index}")
        count = fb.u32(data, count_address)
        id_data, nids = fb.wrapped_vector(data, fb.table_vector_element(data, ids, group_index))
        desc, nd = fb.vector_from_field(data, group, 3)
        blob, length = fb.wrapped_vector(data, fb.offset_target(data, blob_address))
        if count != nids:
            raise ValueError(f"INIT_ENTITY_COUNT_MISMATCH group={group_index}")
        cursor = 0
        for i in range(nd):
            type_id, stride, reserved = struct.unpack_from("<HHI", data, desc + 8 * i)
            if reserved:
                raise ValueError(f"INIT_DESCRIPTOR_RESERVED group={group_index} type={type_id}")
            if type_id == 27:
                if stride != 160:
                    raise ValueError(f"RECT_DECAL_STRIDE_INVALID group={group_index} stride={stride}")
                for j in range(count):
                    entity_id = fb.u32(data, id_data + j * 4)
                    if entity_id in result:
                        raise ValueError(f"DUPLICATE_DECAL_ENTITY entity_id={entity_id}")
                    start = blob + cursor + j * stride
                    result[entity_id] = data[start:start + stride]
            cursor += count * stride
        if cursor != length:
            raise ValueError(f"INIT_BLOB_LENGTH_MISMATCH group={group_index} actual={cursor} expected={length}")
    return result


def decode_decal(entity_id: int, entity_base: str, source_path: str, raw: bytes,
                 streaming: bytes, record: StreamingRecord,
                 path_index: Mapping[bytes, tuple[str, ...]]) -> ProjectedDecal:
    if len(raw) != 160 or record.entity_id != entity_id or record.type_tag != 2:
        raise ValueError(f"DECAL_RECORD_CONTRACT entity_id={entity_id} length={len(raw)} tag={record.type_tag}")
    values = struct.unpack("<40f", raw)
    if not all(math.isfinite(v) for v in values[:36] + values[37:39]):
        raise ValueError(f"DECAL_NONFINITE entity_id={entity_id}")
    matrix, inverse = values[:16], values[16:32]
    residual = max(abs(sum(matrix[k * 4 + i] * inverse[j * 4 + k] for k in range(4)) - float(i == j))
                   for i in range(4) for j in range(4))
    if residual > 0.01:
        raise ValueError(f"DECAL_INVERSE_MISMATCH entity_id={entity_id} residual={residual}")
    hits: set[tuple[str, str, int]] = set()
    for offset in range(record.start, record.end - 7, 4):
        key = streaming[offset:offset + 8]
        paths = path_index.get(key, ())
        materials = [path for path in paths if path.casefold().endswith(".mat")]
        if materials:
            if len(paths) != 1:
                raise ValueError(f"DECAL_MATERIAL_HASH_AMBIGUOUS entity_id={entity_id} id={key.hex()}")
            hits.add((key.hex(), materials[0], offset))
    if len(hits) != 1:
        raise ValueError(f"DECAL_MATERIAL_COUNT entity_id={entity_id} hits={sorted(hits)}")
    material_id, material_path, material_offset = next(iter(hits))
    color = tuple(channel / 255 for channel in raw[144:148])
    return ProjectedDecal(entity_id, entity_base, source_path, matrix, inverse,
                          values[32:34], values[34:36], color, values[37], values[38],
                          struct.unpack_from("<I", raw, 156)[0], material_id, material_path,
                          material_offset, raw.hex())
