"""Authoritative scene instance inventory built directly from read-only VFS data."""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import struct
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from . import flatbuffer as fb, vfs
from .scene_identity import validate_scene_id
from .lz4 import decompress_lz4_inv
from .resources import BootstrapExtractionError, load_group, validate_vfs_root


Progress = Callable[[str, float, str, dict[str, Any] | None], None]
Cancelled = Callable[[], bool]
GROUP_HASH = "C3442D43"
_SOURCE_COLUMNS = frozenset({"source_chunk", "source_data_md5", "source_offset"})
PATH_PATTERNS = {
    "map01": re.compile(r"^Data/Streaming/PC/map01/Streaming/InitChunkData_(.+)_0_-?\d+\.bytes$", re.I),
    "map02": re.compile(
        r"^Data/Streaming/PC/map02/Streaming/InitChunkData_(?:Global|-?\d+_-?\d+)_0_-?\d+\.bytes$",
        re.I,
    ),
}


class InstanceSourceGenerationError(ValueError):
    """An instance database cannot be proven to match the selected game source."""

    def __init__(self, reason: Literal["legacy", "source_changed", "database_invalid", "source_unavailable"],
                 message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _instance_pattern(map_id: str) -> re.Pattern[str]:
    pattern = PATH_PATTERNS.get(map_id)
    if pattern is not None:
        return pattern
    return re.compile(
        rf"^Data/Streaming/PC/{re.escape(map_id)}/Streaming/InitChunkData_(?:Global|-?\d+_-?\d+)_0_-?\d+\.bytes$",
        re.I,
    )


def _instance_source_order(item: vfs.VfsFile) -> tuple[int, str]:
    """Prefer base-group records while retaining unique entities in other groups."""
    name = item.file_name.casefold()
    return (0 if name.endswith("_0_0.bytes") else 1, name)


def _emit(progress: Progress | None, phase: str, value: float, message: str, **details: Any) -> None:
    if progress:
        progress(phase, value, message, details or None)


def _check_cancelled(cancelled: Cancelled | None) -> None:
    if cancelled and cancelled():
        raise BootstrapExtractionError("first-run extraction was cancelled")


def entity_base(name: str | None) -> str | None:
    if not name:
        return None
    value = name.split("#", 1)[0]
    value = re.sub(r" \(\d+\)", "", value)
    return re.sub(r"_ECSMerged$", "", value, flags=re.IGNORECASE)


def iter_entities(data: bytes) -> Iterator[dict[str, Any]]:
    root = fb.u32(data, 0)
    if len(fb.table_fields(data, root)) != 8:
        raise ValueError("expected 8 root fields")
    ids_vector, id_group_count = fb.vector_from_field(data, root, 6)
    data_vector, data_group_count = fb.vector_from_field(data, root, 7)
    if id_group_count != data_group_count:
        raise ValueError("parallel group count mismatch")

    for group_index in range(data_group_count):
        ids_wrapper = fb.table_vector_element(data, ids_vector, group_index)
        ids_data, id_count = fb.wrapped_vector(data, ids_wrapper)
        group = fb.table_vector_element(data, data_vector, group_index)
        count_address = fb.field_address(data, group, 1)
        if count_address is None:
            raise ValueError(f"group {group_index}: missing count")
        count = fb.u32(data, count_address)
        if count != id_count:
            raise ValueError(f"group {group_index}: id count mismatch")
        desc_data, desc_count = fb.vector_from_field(data, group, 3)
        descriptors = []
        for index in range(desc_count):
            type_id, stride, reserved = struct.unpack_from("<HHI", data, desc_data + index * 8)
            if reserved:
                raise ValueError(f"group {group_index}: descriptor reserved value")
            descriptors.append((type_id, stride))
        wrapper_address = fb.field_address(data, group, 4)
        if wrapper_address is None:
            raise ValueError(f"group {group_index}: missing blob")
        blob_data, blob_length = fb.wrapped_vector(data, fb.offset_target(data, wrapper_address))
        expected = count * sum(stride for _, stride in descriptors)
        if blob_length != expected:
            raise ValueError(f"group {group_index}: blob length mismatch")
        columns = {}
        cursor = 0
        for type_id, stride in descriptors:
            columns[type_id] = (cursor, stride)
            cursor += count * stride

        for entity_index in range(count):
            entity_id = fb.u32(data, ids_data + entity_index * 4)
            matrix_type = 18 if 18 in columns else 27 if 27 in columns else None
            if matrix_type is None:
                continue
            matrix_column, matrix_stride = columns[matrix_type]
            if matrix_stride < 64:
                raise ValueError(f"group {group_index}: short matrix stride")
            matrix_address = blob_data + matrix_column + entity_index * matrix_stride
            matrix_blob = data[matrix_address : matrix_address + 64]
            matrix = struct.unpack("<16f", matrix_blob)
            bounds_components = set(range(6)) & columns.keys()
            world_bounds = None
            if bounds_components:
                if len(bounds_components) != 6:
                    missing = sorted(set(range(6)) - bounds_components)
                    raise ValueError(f"entity {entity_id}: missing world bounds components {missing}")
                values = []
                for type_id in range(6):
                    column, stride = columns[type_id]
                    if stride != 4:
                        raise ValueError(f"entity {entity_id}: world bounds component {type_id} has stride {stride}, expected 4")
                    value = struct.unpack_from("<f", data, blob_data + column + entity_index * stride)[0]
                    if not math.isfinite(value) or (type_id >= 3 and value < 0):
                        raise ValueError(f"entity {entity_id}: invalid world bounds component {type_id}: {value}")
                    values.append(value)
                cx, cy, cz, ex, ey, ez = values
                world_bounds = (cx - ex, cx + ex, cy - ey, cy + ey, cz - ez, cz + ez)
                if not all(math.isfinite(value) for value in world_bounds):
                    raise ValueError(f"entity {entity_id}: world bounds exceed finite range")
            name = None
            if 21 in columns:
                name_column, name_stride = columns[21]
                name_address = blob_data + name_column + entity_index * name_stride
                name = fb.parse_name(data[name_address : name_address + name_stride])
            yield {
                "entity_id": entity_id,
                "name": name,
                "base": entity_base(name),
                "matrix_type": matrix_type,
                "matrix_blob": matrix_blob,
                "x": matrix[12],
                "y": matrix[13],
                "z": matrix[14],
                "world_bounds": world_bounds,
                "group_index": group_index,
                "entity_index": entity_index,
            }


def build_instance_database(
    map_id: str,
    vfs_root: Path,
    output: Path,
    *,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> dict[str, str]:
    validate_scene_id(map_id)
    target = output.expanduser().resolve()
    game_vfs = validate_vfs_root(vfs_root)
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite instance database: {target}")
    if target == game_vfs or game_vfs in target.parents:
        raise BootstrapExtractionError("Instance database must be outside the game installation")
    target.parent.mkdir(parents=True, exist_ok=True)

    group = load_group(game_vfs, GROUP_HASH)
    pattern = _instance_pattern(map_id)
    files = sorted(
        (item for item in group.iter_files() if pattern.match(item.file_name)),
        key=_instance_source_order,
    )
    if not files:
        raise BootstrapExtractionError(f"No {map_id} InitChunkData records were found in VFS group {GROUP_HASH}")
    group_dir = game_vfs / GROUP_HASH
    connection = sqlite3.connect(target)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE chunks (
                virtual_path TEXT PRIMARY KEY, compressed_bytes INTEGER NOT NULL,
                clear_bytes INTEGER NOT NULL, parsed_instances INTEGER NOT NULL,
                source_chunk TEXT NOT NULL, source_data_md5 TEXT NOT NULL,
                source_offset INTEGER NOT NULL
            );
            CREATE TABLE instances (
                entity_id INTEGER PRIMARY KEY, name TEXT, entity_base TEXT,
                matrix_type INTEGER NOT NULL, matrix_col_major BLOB NOT NULL,
                x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL,
                source_path TEXT NOT NULL, group_index INTEGER NOT NULL, entity_index INTEGER NOT NULL
            );
            CREATE TABLE duplicates (
                entity_id INTEGER NOT NULL, source_path TEXT NOT NULL, same_payload INTEGER NOT NULL
            );
            CREATE INDEX instances_base_idx ON instances(entity_base);
            CREATE INDEX instances_xz_idx ON instances(x,z);
            """
        )
        inserted = duplicate_count = compressed_total = clear_total = 0
        current_chunk: Path | None = None
        current_handle = None
        try:
            for file_index, item in enumerate(files, start=1):
                _check_cancelled(cancelled)
                chunk_path = group_dir / f"{item.file_chunk_md5_name}.chk"
                if current_chunk != chunk_path:
                    if current_handle is not None:
                        current_handle.close()
                    current_handle = chunk_path.open("rb")
                    current_chunk = chunk_path
                current_handle.seek(item.offset)
                packed = current_handle.read(item.length)
                if len(packed) != item.length or hashlib.md5(packed).hexdigest().upper() != item.file_data_md5.upper():
                    raise BootstrapExtractionError(f"VFS validation failed: {item.file_name}")
                expected = struct.unpack_from("<I", packed)[0]
                clear = decompress_lz4_inv(packed[4:], expected)
                chunk_instances = 0
                for row in iter_entities(clear):
                    existing = connection.execute(
                        "SELECT name,matrix_col_major FROM instances WHERE entity_id=?", (row["entity_id"],)
                    ).fetchone()
                    if existing is not None:
                        same = existing[0] == row["name"] and existing[1] == row["matrix_blob"]
                        connection.execute(
                            "INSERT INTO duplicates VALUES (?,?,?)",
                            (row["entity_id"], item.file_name, int(same)),
                        )
                        duplicate_count += 1
                        continue
                    connection.execute(
                        "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            row["entity_id"], row["name"], row["base"], row["matrix_type"],
                            row["matrix_blob"], row["x"], row["y"], row["z"], item.file_name,
                            row["group_index"], row["entity_index"],
                        ),
                    )
                    inserted += 1
                    chunk_instances += 1
                connection.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                    (item.file_name, item.length, len(clear), chunk_instances,
                     item.file_chunk_md5_name.upper(), item.file_data_md5.upper(), item.offset),
                )
                connection.commit()
                compressed_total += item.length
                clear_total += len(clear)
                _emit(
                    progress,
                    f"{map_id}_instances",
                    file_index / len(files),
                    f"Indexed {map_id} InitChunkData {file_index}/{len(files)}",
                    files=file_index,
                    totalFiles=len(files),
                    instances=inserted,
                    duplicates=duplicate_count,
                )
        finally:
            if current_handle is not None:
                current_handle.close()
        meta = {
            "format": f"Endfield{map_id.title()}Instances/2",
            "map": map_id,
            "source_vfs_group": GROUP_HASH,
            "source_vfs_version": str(group.version),
            "source_file_count": str(len(files)),
            "instance_count": str(inserted),
            "duplicate_count": str(duplicate_count),
            "compressed_bytes": str(compressed_total),
            "clear_bytes_processed": str(clear_total),
        }
        connection.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
        connection.commit()
        return meta
    finally:
        connection.close()


def validate_instance_source_generation(game_root: Path, map_id: str, instances_path: Path) -> None:
    """Prove an indexed instance database matches current InitChunkData VFS records."""
    validate_scene_id(map_id)
    database_path = instances_path.expanduser().resolve()
    if not database_path.is_file():
        raise InstanceSourceGenerationError(
            "database_invalid", f"Instance database is missing: path={database_path}"
        )
    uri = f"{database_path.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {"meta", "chunks"}.issubset(tables):
                raise InstanceSourceGenerationError(
                    "database_invalid", f"Instance database lacks required tables: path={database_path}"
                )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
            if not _SOURCE_COLUMNS.issubset(columns):
                raise InstanceSourceGenerationError(
                    "legacy",
                    f"Unverifiable legacy instance database: source-generation fields are absent; path={database_path}",
                )
            meta = dict(connection.execute("SELECT key,value FROM meta"))
            required_meta = {"format", "map", "source_vfs_group", "source_vfs_version", "source_file_count"}
            if not required_meta.issubset(meta):
                raise InstanceSourceGenerationError(
                    "legacy",
                    f"Unverifiable legacy instance database: source-generation metadata is absent; path={database_path}",
                )
            if any(type(meta[key]) is not str for key in required_meta):
                raise InstanceSourceGenerationError(
                    "database_invalid", f"Invalid source-generation metadata types: path={database_path}"
                )
            expected_format = f"Endfield{map_id.title()}Instances/2"
            if meta["format"] != expected_format:
                if meta["format"] == f"Endfield{map_id.title()}Instances/1":
                    raise InstanceSourceGenerationError(
                        "legacy",
                        f"Unverifiable legacy instance database: format={meta['format']}, path={database_path}",
                    )
                raise InstanceSourceGenerationError(
                    "database_invalid",
                    f"Unsupported instance database format: expected={expected_format}, "
                    f"actual={meta['format']}, path={database_path}",
                )
            if meta["map"] != map_id or meta["source_vfs_group"].upper() != GROUP_HASH:
                raise InstanceSourceGenerationError(
                    "source_changed",
                    f"Instance database source identity differs: map={map_id}, database_map={meta['map']}, "
                    f"database_group={meta['source_vfs_group']}, path={database_path}",
                )
            try:
                stored_version = int(meta["source_vfs_version"])
                stored_file_count = int(meta["source_file_count"])
            except ValueError as exc:
                raise InstanceSourceGenerationError(
                    "database_invalid", f"Invalid source-generation metadata: path={database_path}"
                ) from exc
            if stored_file_count < 1:
                raise InstanceSourceGenerationError(
                    "database_invalid", f"Instance database has invalid source file count: path={database_path}"
                )
            rows = list(connection.execute(
                "SELECT virtual_path,compressed_bytes,source_chunk,source_data_md5,source_offset FROM chunks"
            ))
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise InstanceSourceGenerationError(
            "database_invalid", f"Cannot read instance source-generation database: path={database_path}, error={exc}"
        ) from exc

    vfs_root = game_root.expanduser().resolve() / "Endfield_Data" / "StreamingAssets" / "VFS"
    try:
        group = load_group(vfs_root, GROUP_HASH)
    except (BootstrapExtractionError, vfs.ProbeError, OSError) as exc:
        raise InstanceSourceGenerationError(
            "source_unavailable", f"Cannot read current InitChunkData VFS records: root={vfs_root}, error={exc}"
        ) from exc
    if group.version != stored_version:
        raise InstanceSourceGenerationError(
            "source_changed",
            f"Instance database VFS version differs: map={map_id}, database={stored_version}, current={group.version}",
        )

    pattern = _instance_pattern(map_id)
    current: dict[str, vfs.VfsFile] = {}
    for item in group.iter_files():
        key = item.file_name.casefold()
        if not pattern.fullmatch(item.file_name):
            continue
        if key in current:
            raise InstanceSourceGenerationError(
                "source_changed", f"Current VFS has ambiguous InitChunkData path: map={map_id}, path={item.file_name}"
            )
        current[key] = item
    indexed: dict[str, tuple[str, int, str, str, int]] = {}
    for path, length, chunk_name, data_md5, offset in rows:
        if not isinstance(path, str) or not isinstance(chunk_name, str) or not isinstance(data_md5, str):
            raise InstanceSourceGenerationError(
                "database_invalid", f"Invalid source-generation row types: map={map_id}, path={database_path}"
            )
        key = path.casefold()
        if key in indexed:
            raise InstanceSourceGenerationError(
                "database_invalid", f"Duplicate source-generation path: map={map_id}, path={path}"
            )
        if type(length) is not int or type(offset) is not int or length < 1 or offset < 0:
            raise InstanceSourceGenerationError(
                "database_invalid", f"Invalid source-generation range: map={map_id}, path={path}"
            )
        indexed[key] = (path, length, chunk_name.upper(), data_md5.upper(), offset)

    if len(rows) != stored_file_count or set(indexed) != set(current):
        raise InstanceSourceGenerationError(
            "source_changed",
            f"Instance database InitChunkData set differs: map={map_id}, database_files={len(indexed)}, "
            f"database_meta_files={stored_file_count}, current_files={len(current)}",
        )
    for key, item in current.items():
        row = indexed[key]
        expected = (item.file_name, item.length, item.file_chunk_md5_name.upper(),
                    item.file_data_md5.upper(), item.offset)
        if row != expected:
            raise InstanceSourceGenerationError(
                "source_changed",
                f"Instance database InitChunkData source differs: map={map_id}, path={item.file_name}, "
                f"database={row[2:]}, current={expected[2:]}",
            )


def build_unresolved_asset_database(instances: Path, output: Path) -> dict[str, str]:
    """Create a truthful bootstrap asset table; geometry resolution remains unresolved."""

    source = instances.expanduser().resolve()
    target = output.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Instance database is missing: {source}")
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite asset database: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    output_db = sqlite3.connect(target)
    try:
        output_db.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE resolutions (
                entity_base TEXT PRIMARY KEY, instance_count INTEGER NOT NULL, method TEXT NOT NULL,
                container_path TEXT, logical_name TEXT, root_cab TEXT, model_asset_name TEXT
            );
            """
        )
        rows = list(
            source_db.execute(
                "SELECT entity_base, COUNT(*) FROM instances WHERE entity_base IS NOT NULL "
                "AND entity_base != '' GROUP BY entity_base ORDER BY entity_base"
            )
        )
        output_db.executemany(
            "INSERT INTO resolutions VALUES (?,?,'unresolved',NULL,NULL,NULL,NULL)", rows
        )
        instance_count = sum(int(row[1]) for row in rows)
        meta = {
            "format": "EndfieldBootstrapAssetResolutions/1",
            "resolution_status": "unresolved",
            "asset_count": str(len(rows)),
            "instance_count": str(instance_count),
            "limitations": "No geometry/material closure has been claimed by the bootstrap index.",
        }
        output_db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
        output_db.commit()
        return meta
    finally:
        source_db.close()
        output_db.close()
