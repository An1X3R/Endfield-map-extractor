"""Read current-map DynamicSceneEffect placements from the game VFS."""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable

from . import flatbuffer as fb
from . import vfs
from .resources import iter_path_records, load_group, validate_vfs_root


GROUP_HASH = "23D53F5D"
EFFECT_STRIDE = 144
MAIN_FILE = re.compile(
    r"^Data/DynamicStreaming/PC/Scene/(map01|map02)/(fb_main_\d+_\d+_\d+\.bytes)$",
    re.IGNORECASE,
)


class EffectDecodeError(ValueError):
    """A current FX source or its path link does not match the expected layout."""


@dataclass(frozen=True)
class EffectSource:
    virtual_path: str
    data_md5: str
    byte_length: int
    grid_count: int
    effect_count: int


@dataclass(frozen=True)
class EffectRecord:
    key: str
    source_virtual_path: str
    grid_index: int
    effect_index: int
    record_offset: int
    path_hash_hex: str
    prefab_path: str
    position: tuple[float, float, float]
    rotation_euler_degrees: tuple[float, float, float]
    scale: tuple[float, float, float]


@dataclass(frozen=True)
class MapEffectInventory:
    map_id: str
    sources: tuple[EffectSource, ...]
    records: tuple[EffectRecord, ...]
    prefab_paths: tuple[str, ...]


@dataclass(frozen=True)
class _RawEffect:
    key: str
    source_virtual_path: str
    grid_index: int
    effect_index: int
    record_offset: int
    path_hash: bytes
    position: tuple[float, float, float]
    rotation_euler_degrees: tuple[float, float, float]
    scale: tuple[float, float, float]


def _vector(data: bytes, table: int, field: int, element_size: int) -> tuple[int, int]:
    if table < 4 or table + 4 > len(data):
        raise EffectDecodeError(f"FX table address is outside payload: table={table}, bytes={len(data)}")
    distance = fb.i32(data, table)
    vtable = table - distance
    if distance == 0 or vtable < 0 or vtable + 4 > len(data):
        raise EffectDecodeError(
            f"FX vtable address is invalid: table={table}, distance={distance}, bytes={len(data)}"
        )
    vtable_length = fb.u16(data, vtable)
    object_length = fb.u16(data, vtable + 2)
    if (vtable_length < 4 or vtable_length % 2 or vtable + vtable_length > len(data)
            or object_length < 4 or table + object_length > len(data)):
        raise EffectDecodeError(
            f"FX table lengths are invalid: table={table}, vtableLength={vtable_length}, "
            f"objectLength={object_length}, bytes={len(data)}"
        )
    field_entry = vtable + 4 + field * 2
    if field_entry + 2 > vtable + vtable_length:
        return 0, 0
    relative = fb.u16(data, field_entry)
    if relative == 0:
        return 0, 0
    if relative + 4 > object_length:
        raise EffectDecodeError(
            f"FX vector field exceeds table: table={table}, field={field}, "
            f"fieldOffset={relative}, objectLength={object_length}"
        )
    address = table + relative
    vector = fb.offset_target(data, address)
    if vector < address + 4 or vector + 4 > len(data):
        raise EffectDecodeError(
            f"FX vector address is invalid: table={table}, field={field}, "
            f"vector={vector}, bytes={len(data)}"
        )
    vector_data, count = vector + 4, fb.u32(data, vector)
    if vector_data + count * element_size > len(data):
        raise EffectDecodeError(
            f"FX FlatBuffer vector exceeds payload: field={field}, "
            f"offset={vector_data}, count={count}, stride={element_size}, bytes={len(data)}"
        )
    return vector_data, count


def _decode_file(data: bytes, virtual_path: str) -> tuple[int, tuple[_RawEffect, ...]]:
    if len(data) < 8:
        raise EffectDecodeError(f"FX source is too short: path={virtual_path}, bytes={len(data)}")
    try:
        root = fb.u32(data, 0)
        grid_vector, grid_count = _vector(data, root, 3, 4)
        if grid_vector == 0:
            raise EffectDecodeError(f"FX root grid field is missing: path={virtual_path}, root={root}")
        records: list[_RawEffect] = []
        for grid_index in range(grid_count):
            grid = fb.table_vector_element(data, grid_vector, grid_index)
            effect_vector, effect_count = _vector(data, grid, 8, EFFECT_STRIDE)
            for effect_index in range(effect_count):
                offset = effect_vector + effect_index * EFFECT_STRIDE
                position = struct.unpack_from("<3f", data, offset + 12)
                rotation = struct.unpack_from("<3f", data, offset + 24)
                scale = struct.unpack_from("<3f", data, offset + 36)
                if not all(math.isfinite(value) for value in (*position, *rotation, *scale)):
                    raise EffectDecodeError(
                        f"FX transform contains a non-finite value: path={virtual_path}, "
                        f"grid={grid_index}, effect={effect_index}"
                    )
                records.append(_RawEffect(
                    key=f"{virtual_path}:{grid_index}:{effect_index}",
                    source_virtual_path=virtual_path,
                    grid_index=grid_index,
                    effect_index=effect_index,
                    record_offset=offset,
                    path_hash=data[offset + 48:offset + 56],
                    position=position,
                    rotation_euler_degrees=rotation,
                    scale=scale,
                ))
    except struct.error as error:
        raise EffectDecodeError(f"FX FlatBuffer layout is invalid: path={virtual_path}, error={error}") from error
    return grid_count, tuple(records)


def _resolve_hashes(
    string_path_hash: Path, wanted: frozenset[bytes], check_cancelled: Callable[[], None]
) -> dict[bytes, str]:
    data = string_path_hash.read_bytes()
    if len(data) < 8:
        raise EffectDecodeError(f"StringPathHash is too short: path={string_path_hash}, bytes={len(data)}")
    strings_offset, count = struct.unpack_from("<II", data, 0)
    if strings_offset != 8 + count * 24 or strings_offset > len(data):
        raise EffectDecodeError(
            f"StringPathHash layout differs: path={string_path_hash}, "
            f"offset={strings_offset}, count={count}, bytes={len(data)}"
        )
    by_offset: dict[int, set[bytes]] = {}
    for word_index, position in enumerate(range(8, strings_offset - 8, 8)):
        if word_index % 8192 == 0:
            check_cancelled()
        identifier = data[position:position + 8]
        if identifier not in wanted:
            continue
        relative_offset = struct.unpack_from("<Q", data, position + 8)[0]
        by_offset.setdefault(strings_offset + relative_offset, set()).add(identifier)
    del data
    found: dict[bytes, set[str]] = {identifier: set() for identifier in wanted}
    for record_index, value, _opaque_record, string_at in iter_path_records(string_path_hash):
        if record_index % 8192 == 0:
            check_cancelled()
        for identifier in by_offset.get(string_at, ()):
            if value.casefold().startswith(("assets/", "data/")):
                found[identifier].add(value)
    unresolved = sorted(identifier.hex() for identifier, values in found.items() if not values)
    ambiguous = sorted(identifier.hex() for identifier, values in found.items() if len(values) > 1)
    if unresolved or ambiguous:
        raise EffectDecodeError(
            f"FX prefab path resolution failed: path={string_path_hash}, "
            f"unresolvedCount={len(unresolved)}, unresolvedSample={unresolved[:8]}, "
            f"ambiguousCount={len(ambiguous)}, ambiguousSample={ambiguous[:8]}"
        )
    return {identifier: next(iter(values)) for identifier, values in found.items()}


def read_dynamic_scene_effects(
    vfs_root: Path, string_path_hash: Path, check_cancelled: Callable[[], None]
) -> dict[str, MapEffectInventory]:
    """Return all current Map01/Map02 fb_main effect source records and paths."""
    game_vfs = validate_vfs_root(vfs_root)
    source_paths = string_path_hash.expanduser().resolve()
    if not source_paths.is_file():
        raise FileNotFoundError(f"FX StringPathHash source is missing: {source_paths}")
    group = load_group(game_vfs, GROUP_HASH)
    candidates: list[tuple[str, vfs.VfsFile]] = []
    for item in group.iter_files():
        if not item.file_name.casefold().startswith(
            ("data/dynamicstreaming/pc/scene/map01/fb_main_",
             "data/dynamicstreaming/pc/scene/map02/fb_main_")
        ):
            continue
        match = MAIN_FILE.fullmatch(item.file_name)
        if match is None:
            raise EffectDecodeError(f"FX main filename differs from current layout: {item.file_name}")
        candidates.append((match.group(1).casefold(), item))
    candidates.sort(key=lambda pair: pair[1].file_name.casefold())
    if not candidates or {map_id for map_id, _ in candidates} != {"map01", "map02"}:
        raise EffectDecodeError(
            f"FX main sources for both maps are required: group={GROUP_HASH}, "
            f"mapIds={sorted({map_id for map_id, _ in candidates})}"
        )
    raw: dict[str, list[_RawEffect]] = {"map01": [], "map02": []}
    sources: dict[str, list[EffectSource]] = {"map01": [], "map02": []}
    seen_paths: set[str] = set()
    for map_id, item in candidates:
        check_cancelled()
        if item.file_name.casefold() in seen_paths:
            raise EffectDecodeError(f"Duplicate FX VFS source path: {item.file_name}")
        seen_paths.add(item.file_name.casefold())
        chunk_path = game_vfs / GROUP_HASH / f"{item.file_chunk_md5_name}.chk"
        output = BytesIO()
        with chunk_path.open("rb") as source:
            digest = vfs.copy_virtual_file(
                source, output, item, group.version, bytes.fromhex(vfs.DEFAULT_VFS_KEY_HEX)
            )
        if digest != item.file_data_md5.upper():
            raise EffectDecodeError(
                f"FX VFS checksum mismatch: path={item.file_name}, "
                f"expectedMd5={item.file_data_md5}, actualMd5={digest}"
            )
        payload = output.getvalue()
        grid_count, file_records = _decode_file(payload, item.file_name)
        raw[map_id].extend(file_records)
        sources[map_id].append(EffectSource(
            item.file_name, digest, len(payload), grid_count, len(file_records)
        ))
    wanted = frozenset(row.path_hash for records in raw.values() for row in records)
    resolved = _resolve_hashes(source_paths, wanted, check_cancelled)
    result: dict[str, MapEffectInventory] = {}
    for map_id in ("map01", "map02"):
        rows = tuple(EffectRecord(
            row.key, row.source_virtual_path, row.grid_index, row.effect_index,
            row.record_offset, row.path_hash.hex(), resolved[row.path_hash],
            row.position, row.rotation_euler_degrees, row.scale,
        ) for row in raw[map_id])
        result[map_id] = MapEffectInventory(
            map_id, tuple(sources[map_id]), rows,
            tuple(sorted({row.prefab_path for row in rows}, key=str.casefold)),
        )
    return result
