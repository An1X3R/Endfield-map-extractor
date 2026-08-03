"""Authoritative Map01/Map02 instance inventory built directly from read-only VFS data."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
from pathlib import Path
from typing import Any, Callable, Iterator

from . import flatbuffer as fb
from .lz4 import decompress_lz4_inv
from .resources import BootstrapExtractionError, load_group, validate_vfs_root


Progress = Callable[[str, float, str, dict[str, Any] | None], None]
Cancelled = Callable[[], bool]
GROUP_HASH = "C3442D43"
PATH_PATTERNS = {
    "map01": re.compile(r"^Data/Streaming/PC/map01/Streaming/InitChunkData_(.+)_0_0\.bytes$", re.I),
    "map02": re.compile(
        r"^Data/Streaming/PC/map02/Streaming/InitChunkData_(?:Global|-?\d+_-?\d+)_0_0\.bytes$",
        re.I,
    ),
}


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
            matrix_type = 18 if 18 in columns else 27 if 27 in columns else None
            if matrix_type is None:
                continue
            matrix_column, matrix_stride = columns[matrix_type]
            if matrix_stride < 64:
                raise ValueError(f"group {group_index}: short matrix stride")
            matrix_address = blob_data + matrix_column + entity_index * matrix_stride
            matrix_blob = data[matrix_address : matrix_address + 64]
            matrix = struct.unpack("<16f", matrix_blob)
            name = None
            if 21 in columns:
                name_column, name_stride = columns[21]
                name_address = blob_data + name_column + entity_index * name_stride
                name = fb.parse_name(data[name_address : name_address + name_stride])
            yield {
                "entity_id": fb.u32(data, ids_data + entity_index * 4),
                "name": name,
                "base": entity_base(name),
                "matrix_type": matrix_type,
                "matrix_blob": matrix_blob,
                "x": matrix[12],
                "y": matrix[13],
                "z": matrix[14],
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
    if map_id not in PATH_PATTERNS:
        raise ValueError(f"unsupported map id: {map_id}")
    target = output.expanduser().resolve()
    game_vfs = validate_vfs_root(vfs_root)
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite instance database: {target}")
    if target == game_vfs or game_vfs in target.parents:
        raise BootstrapExtractionError("Instance database must be outside the game installation")
    target.parent.mkdir(parents=True, exist_ok=True)

    group = load_group(game_vfs, GROUP_HASH)
    pattern = PATH_PATTERNS[map_id]
    files = sorted(
        (item for item in group.iter_files() if pattern.match(item.file_name)),
        key=lambda item: item.file_name.casefold(),
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
                clear_bytes INTEGER NOT NULL, parsed_instances INTEGER NOT NULL
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
                    "INSERT INTO chunks VALUES (?,?,?,?)",
                    (item.file_name, item.length, len(clear), chunk_instances),
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
            "format": f"Endfield{map_id.title()}Instances/1",
            "map": map_id,
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
