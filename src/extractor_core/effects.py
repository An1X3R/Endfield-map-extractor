"""Read current-map DynamicSceneEffect placements from the game VFS."""

from __future__ import annotations

import math
import os
import re
import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable

from endfield_scene_io import (SceneInputChangedError, SceneSourceCacheSlot, cached_scene_source_value,
                               resolve_scene_read_path)

from . import flatbuffer as fb
from . import vfs
from .instances import GROUP_HASH as STATIC_GROUP_HASH, _instance_pattern, iter_entities
from .lz4 import decompress_lz4_inv
from .projected_decals import build_path_id_index_views_cancellable
from .resources import iter_path_records, load_group, validate_vfs_root
from .scene_identity import validate_scene_id


GROUP_HASH = "23D53F5D"
EFFECT_STRIDE = 144
MAIN_FILE = re.compile(
    r"^Data/DynamicStreaming/PC/Scene/([^/]+)/(fb_main_\d+_\d+_\d+\.bytes)$",
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


_EFFECT_INVENTORY_CACHE = SceneSourceCacheSlot(
    "dynamic-scene-effects", dict, 4, 64 * 1024 * 1024
)
_PATH_RECORD_VALIDATION_CACHE = SceneSourceCacheSlot(
    "path-id-record-validation", bytes, 4, 64
)


def _dynamic_inventory_sources(vfs_root: Path, string_path_hash: Path) -> tuple[Path, ...]:
    """Stamp the whole group because BLC validation includes every physical chunk."""
    group_dir = vfs_root / GROUP_HASH
    chunks = tuple(sorted(group_dir.glob("*.chk")))
    return (string_path_hash, group_dir / f"{GROUP_HASH}.blc", *chunks)


def _static_scene_source(vfs_root: Path, scene_id: str) -> tuple[vfs.VfsGroup, vfs.VfsFile]:
    """Bind absent dynamic payloads to an exact, independently indexed static chunk."""
    group = load_group(vfs_root, STATIC_GROUP_HASH)
    pattern = _instance_pattern(scene_id)
    files = sorted((item for item in group.iter_files() if pattern.fullmatch(item.file_name)),
                   key=lambda item: item.file_name.casefold())
    if not files or len({item.file_name.casefold() for item in files}) != len(files):
        raise EffectDecodeError(f"Static-only scene requires unique InitChunkData sources: scene={scene_id}")
    return group, files[0]


def _scene_inventory_sources(vfs_root: Path, string_path_hash: Path,
                             scene_ids: tuple[str, ...]) -> tuple[Path, ...]:
    """Include exact static evidence only for scenes without dynamic main files."""
    sources = _dynamic_inventory_sources(vfs_root, string_path_hash)
    group = load_group(vfs_root, GROUP_HASH)
    main_scenes = {match[1].casefold() for item in group.iter_files()
                   if (match := MAIN_FILE.fullmatch(item.file_name)) is not None}
    extra: list[Path] = []
    for scene in scene_ids:
        if scene not in main_scenes:
            _, item = _static_scene_source(vfs_root, scene)
            extra.extend((vfs_root / STATIC_GROUP_HASH / f"{STATIC_GROUP_HASH}.blc",
                          vfs_root / STATIC_GROUP_HASH / f"{item.file_chunk_md5_name}.chk"))
    return (*sources, *dict.fromkeys(extra))


def _require_scene_inventory_sources(vfs_root: Path, string_path_hash: Path, scene_ids: tuple[str, ...],
                                     expected: tuple[Path, ...]) -> None:
    if _scene_inventory_sources(vfs_root, string_path_hash, scene_ids) != expected:
        raise SceneInputChangedError(f"Scene inventory source membership changed during read: scenes={scene_ids}")


def _require_static_only_scene(vfs_root: Path, scene_id: str, check_cancelled: Callable[[], None]) -> None:
    """Require absent dynamic payloads and a checksum-verified, decoded static root."""
    check_cancelled()
    group = load_group(vfs_root, GROUP_HASH)
    prefix = f"data/dynamicstreaming/pc/scene/{scene_id}/"
    metadata = {prefix + "fbstreamarea.bytes", prefix + "fb_version.bytes"}
    files = tuple(item for item in group.iter_files() if item.file_name.casefold().startswith(prefix))
    if any(item.file_name.casefold() not in metadata for item in files):
        raise EffectDecodeError(
            f"Dynamic scene has payloads without main sources: scene={scene_id}, "
            f"paths={tuple(item.file_name for item in files)}")
    if len({item.file_name.casefold() for item in files}) != len(files):
        raise EffectDecodeError(f"Duplicate static-only scene metadata paths: scene={scene_id}")
    for item in files:
        check_cancelled()
        _verified_inventory_payload(vfs_root, group, item)
    static_group, static_item = _static_scene_source(vfs_root, scene_id)
    packed = _verified_inventory_payload(vfs_root, static_group, static_item)
    if len(packed) < 4:
        raise EffectDecodeError(f"Static InitChunkData compression header missing: path={static_item.file_name}")
    try:
        clear = decompress_lz4_inv(packed[4:], struct.unpack_from("<I", packed)[0])
        for index, _ in enumerate(iter_entities(clear)):
            if index % 1024 == 0:
                check_cancelled()
    except (ValueError, struct.error) as error:
        raise EffectDecodeError(f"Static InitChunkData evidence decode failed: path={static_item.file_name}, error={error}") from error


def _verified_inventory_payload(vfs_root: Path, group: vfs.VfsGroup, item: vfs.VfsFile) -> bytes:
    output = BytesIO()
    with (vfs_root / group.group_hash / f"{item.file_chunk_md5_name}.chk").open("rb") as source:
        digest = vfs.copy_virtual_file(source, output, item, group.version, bytes.fromhex(vfs.DEFAULT_VFS_KEY_HEX))
    if digest != item.file_data_md5.upper() or not item.length:
        raise EffectDecodeError(f"Scene evidence VFS checksum/length differs: path={item.file_name}, expectedMd5={item.file_data_md5}, actualMd5={digest}")
    return output.getvalue()


def _dynamic_inventory_qualifiers(scene_ids: tuple[str, ...], sources: tuple[Path, ...]) -> tuple[str, ...]:
    """Keep lexical group membership even when multiple paths resolve to one target."""
    return (*scene_ids, *(str(path) for path in sources))


def _require_dynamic_inventory_sources(
    vfs_root: Path, string_path_hash: Path, expected: tuple[Path, ...],
) -> None:
    """Reject chunk membership changes during an inventory decode."""
    actual = _dynamic_inventory_sources(vfs_root, string_path_hash)
    if actual != expected:
        raise SceneInputChangedError(
            f"Dynamic scene group membership changed during read: group={GROUP_HASH}, "
            f"expectedSources={expected}, actualSources={actual}"
        )


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
    string_path_hash: Path, wanted: frozenset[bytes], check_cancelled: Callable[[], None],
    path_prefixes: tuple[str, ...],
) -> dict[bytes, str]:
    with string_path_hash.open("rb") as source:
        header = source.read(8)
        byte_length = os.fstat(source.fileno()).st_size
    if len(header) < 8:
        raise EffectDecodeError(f"StringPathHash is too short: path={string_path_hash}, bytes={len(header)}")
    strings_offset, count = struct.unpack_from("<II", header, 0)
    if strings_offset != 8 + count * 24 or strings_offset > byte_length:
        raise EffectDecodeError(
            f"StringPathHash layout differs: path={string_path_hash}, "
            f"offset={strings_offset}, count={count}, bytes={byte_length}"
        )
    check_cancelled()
    path_views = (build_path_id_index_views_cancellable(string_path_hash, check_cancelled)
                  if wanted else None)
    resolved_path = resolve_scene_read_path(string_path_hash)

    if path_views is None or not path_views.path_records_valid:
        def validate_path_records() -> bytes:
            for record_index, _value, _opaque_record, _string_at in iter_path_records(resolved_path):
                if record_index % 8192 == 0:
                    check_cancelled()
            return b""

        cached_scene_source_value(_PATH_RECORD_VALIDATION_CACHE, (resolved_path,), (), validate_path_records)
    check_cancelled()
    found: dict[bytes, set[str]] = {identifier: set() for identifier in wanted}
    for wanted_index, identifier in enumerate(wanted):
        if wanted_index % 8192 == 0:
            check_cancelled()
        strict_paths = path_views.strict_paths(identifier) if path_views is not None else ()
        for value in strict_paths:
            if value.casefold().startswith(path_prefixes):
                found[identifier].add(value)
    unresolved = sorted(identifier.hex() for identifier, values in found.items() if not values)
    ambiguous = sorted(identifier.hex() for identifier, values in found.items() if len(values) > 1)
    if unresolved or ambiguous:
        raise EffectDecodeError(
            f"Source path resolution failed: path={string_path_hash}, "
            f"unresolvedCount={len(unresolved)}, unresolvedSample={unresolved[:8]}, "
            f"ambiguousCount={len(ambiguous)}, ambiguousSample={ambiguous[:8]}"
        )
    return {identifier: next(iter(values)) for identifier, values in found.items()}


def _decode_dynamic_scene_effects(
    vfs_root: Path,
    string_path_hash: Path,
    scene_ids: tuple[str, ...],
    check_cancelled: Callable[[], None],
) -> dict[str, MapEffectInventory]:
    """Decode current fb_main effect records for each requested scene."""
    game_vfs = validate_vfs_root(vfs_root)
    source_paths = string_path_hash.expanduser().resolve()
    if not source_paths.is_file():
        raise FileNotFoundError(f"FX StringPathHash source is missing: {source_paths}")
    group = load_group(game_vfs, GROUP_HASH)
    candidates: list[tuple[str, vfs.VfsFile]] = []
    prefixes = {
        scene_id: f"data/dynamicstreaming/pc/scene/{scene_id}/fb_main_"
        for scene_id in scene_ids
    }
    for item in group.iter_files():
        folded_path = item.file_name.casefold()
        scene_id = next(
            (candidate_id for candidate_id, prefix in prefixes.items()
             if folded_path.startswith(prefix)),
            None,
        )
        if scene_id is None:
            continue
        match = MAIN_FILE.fullmatch(item.file_name)
        if match is None:
            raise EffectDecodeError(f"FX main filename differs from current layout: {item.file_name}")
        matched_scene_id = validate_scene_id(match.group(1).casefold())
        if matched_scene_id != scene_id:
            raise EffectDecodeError(
                f"FX main scene path differs from requested scene: path={item.file_name}, "
                f"requestedScene={scene_id}, actualScene={matched_scene_id}"
            )
        candidates.append((matched_scene_id, item))
    candidates.sort(key=lambda pair: pair[1].file_name.casefold())
    found_scene_ids = {scene_id for scene_id, _ in candidates}
    for scene_id in set(scene_ids) - found_scene_ids:
        _require_static_only_scene(game_vfs, scene_id, check_cancelled)
    raw: dict[str, list[_RawEffect]] = {scene_id: [] for scene_id in scene_ids}
    sources: dict[str, list[EffectSource]] = {scene_id: [] for scene_id in scene_ids}
    seen_paths: set[str] = set()
    for scene_id, item in candidates:
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
        raw[scene_id].extend(file_records)
        sources[scene_id].append(EffectSource(
            item.file_name, digest, len(payload), grid_count, len(file_records)
        ))
    wanted = frozenset(row.path_hash for records in raw.values() for row in records)
    resolved = _resolve_hashes(source_paths, wanted, check_cancelled, ("assets/", "data/"))
    result: dict[str, MapEffectInventory] = {}
    for scene_id in scene_ids:
        rows = tuple(EffectRecord(
            row.key, row.source_virtual_path, row.grid_index, row.effect_index,
            row.record_offset, row.path_hash.hex(), resolved[row.path_hash],
            row.position, row.rotation_euler_degrees, row.scale,
        ) for row in raw[scene_id])
        result[scene_id] = MapEffectInventory(
            scene_id, tuple(sources[scene_id]), rows,
            tuple(sorted({row.prefab_path for row in rows}, key=str.casefold)),
        )
    return result


def _read_dynamic_scene_effects(
    vfs_root: Path, string_path_hash: Path, scene_ids: tuple[str, ...],
    check_cancelled: Callable[[], None],
) -> dict[str, MapEffectInventory]:
    """Reuse immutable scene inventories while every group input remains unchanged."""
    check_cancelled()
    root = validate_vfs_root(vfs_root)
    hash_path = string_path_hash.expanduser().resolve()
    if not hash_path.is_file():
        raise FileNotFoundError(f"FX StringPathHash source is missing: {hash_path}")
    sources = _scene_inventory_sources(root, hash_path, scene_ids)

    def decode() -> dict[str, MapEffectInventory]:
        result = _decode_dynamic_scene_effects(root, hash_path, scene_ids, check_cancelled)
        _require_scene_inventory_sources(root, hash_path, scene_ids, sources)
        return result

    qualifiers = _dynamic_inventory_qualifiers(scene_ids, sources)
    result = cached_scene_source_value(_EFFECT_INVENTORY_CACHE, sources, qualifiers, decode)
    check_cancelled()
    return dict(result)


def read_dynamic_scene_effects(
    vfs_root: Path, string_path_hash: Path, check_cancelled: Callable[[], None]
) -> dict[str, MapEffectInventory]:
    """Return all current Map01/Map02 fb_main effect source records and paths."""
    return _read_dynamic_scene_effects(
        vfs_root, string_path_hash, ("map01", "map02"), check_cancelled
    )


def read_dynamic_scene_effects_for_scene(
    vfs_root: Path,
    string_path_hash: Path,
    scene_id: str,
    check_cancelled: Callable[[], None],
) -> MapEffectInventory:
    """Return the current fb_main effect records and paths for one validated scene."""
    validated_scene_id = validate_scene_id(scene_id)
    return _read_dynamic_scene_effects(
        vfs_root, string_path_hash, (validated_scene_id,), check_cancelled
    )[validated_scene_id]
