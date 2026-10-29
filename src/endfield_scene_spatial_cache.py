"""Persist authenticated ECS spatial records independently of export selections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from typing import Callable, TypedDict, cast

from endfield_scene_io import SceneInputChangedError, _file_stamp, resolve_scene_read_path
from endfield_scene_spatial_plan import WorldBounds
from extractor_core.vfs import VfsFile


_MAGIC = b"EFSPAT01"
_HEADER = struct.Struct("<8sIQ")
_RECORD = struct.Struct("<I64sB6d2d")
_MAX_HEADER_BYTES = 65536


class SpatialCacheCorruptError(ValueError):
    """A persisted spatial record or its authenticated envelope is invalid."""


class SpatialRecord(TypedDict):
    entity_id: int
    matrix_blob: bytes
    world_bounds: WorldBounds | None
    x: float
    z: float


@dataclass(frozen=True)
class SpatialSourceIdentity:
    backing_path: Path
    metadata: str
    cache_path: Path


@dataclass(frozen=True)
class SpatialSourceRecords:
    records: tuple[SpatialRecord, ...]
    decoded_chunk: bytes | None


def spatial_parser_identity() -> str:
    """Version records by the actual source decoder and cache implementation."""
    root = Path(__file__).resolve().parent
    paths = (Path(__file__), *(root / "extractor_core" / name for name in
                              ("instances.py", "flatbuffer.py", "lz4.py", "lz4_numba.py", "projected_decals.py")))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def spatial_source_identity(
    cache_root: Path, vfs: Path, entry: VfsFile, parser_identity: str,
) -> SpatialSourceIdentity:
    """Bind one virtual entry to its resolved backing file's robust version stamp."""
    if any(not isinstance(value, str) or len(value) != 32 or
           any(char not in "0123456789abcdefABCDEF" for char in value)
           for value in (entry.file_chunk_md5_name, entry.file_data_md5)):
        raise ValueError(f"Spatial source requires hexadecimal VFS MD5 identities: source={entry.file_name}")
    if type(entry.offset) is not int or entry.offset < 0 or type(entry.length) is not int or entry.length < 4:
        raise ValueError(f"Invalid spatial VFS range: source={entry.file_name}, offset={entry.offset}, length={entry.length}")
    backing = resolve_scene_read_path(vfs / "C3442D43" / f"{entry.file_chunk_md5_name}.chk")
    stamp = _file_stamp(backing)
    if entry.offset + entry.length > stamp.bytes:
        raise ValueError(f"Spatial VFS range exceeds backing file: source={entry.file_name}, path={backing}, bytes={stamp.bytes}")
    metadata = json.dumps({"format": "EndfieldSpatialSource/1", "parser": parser_identity,
                           "backing_path": str(backing), "stamp": asdict(stamp), "entry": asdict(entry)},
                          sort_keys=True, separators=(",", ":"), allow_nan=False)
    key = hashlib.sha256(metadata.encode("utf-8")).hexdigest()
    return SpatialSourceIdentity(backing, metadata, cache_root / "scene_spatial_v1" / key[:2] / f"{key}.bin")


def require_spatial_source_version(identity: SpatialSourceIdentity) -> None:
    """Reject changed sources even when the caller has no operation-local IO session."""
    expected = json.loads(identity.metadata)["stamp"]
    if asdict(_file_stamp(identity.backing_path)) != expected:
        raise SceneInputChangedError(f"Spatial source changed during cache read: path={identity.backing_path}")


def _record(value: Mapping[str, object], label: str) -> SpatialRecord:
    entity_id = value.get("entity_id")
    matrix = value.get("matrix_blob")
    if type(entity_id) is not int or not 0 <= entity_id <= 0xFFFFFFFF:
        raise SpatialCacheCorruptError(f"Spatial entity ID must be uint32: {label}, entity_id={entity_id}")
    if not isinstance(matrix, bytes) or len(matrix) != 64:
        raise SpatialCacheCorruptError(f"Spatial matrix requires 16 finite float32 values: {label}, entity_id={entity_id}")
    numbers = struct.unpack("<16f", matrix)
    if not all(map(math.isfinite, numbers)):
        raise SpatialCacheCorruptError(f"Spatial matrix requires 16 finite float32 values: {label}, entity_id={entity_id}")
    x, z = value.get("x"), value.get("z")
    if any(type(v) not in (int, float) or not math.isfinite(cast(float, v)) for v in (x, z)) or (x, z) != (numbers[12], numbers[14]):
        raise SpatialCacheCorruptError(f"Spatial origin differs from matrix: {label}, entity_id={entity_id}")
    bounds = value.get("world_bounds")
    if "world_bounds" not in value or (bounds is not None and (
            not isinstance(bounds, (tuple, list)) or len(bounds) != 6 or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds) or
            any(bounds[index] > bounds[index + 1] for index in (0, 2, 4)))):
        raise SpatialCacheCorruptError(f"Spatial bounds require six finite ordered values or null: {label}, entity_id={entity_id}")
    return {"entity_id": entity_id, "matrix_blob": matrix,
            "world_bounds": None if bounds is None else cast(WorldBounds, tuple(float(v) for v in bounds)),
            "x": float(cast(float, x)), "z": float(cast(float, z))}


def _load(identity: SpatialSourceIdentity) -> tuple[SpatialRecord, ...]:
    path = identity.cache_path
    size = path.stat().st_size
    with path.open("rb") as stream:
        raw = stream.read(_HEADER.size)
        if len(raw) != _HEADER.size:
            raise SpatialCacheCorruptError(f"Spatial cache header is truncated: path={path}, bytes={size}")
        magic, metadata_bytes, count = _HEADER.unpack(raw)
        if magic != _MAGIC or not 0 < metadata_bytes <= _MAX_HEADER_BYTES or size != _HEADER.size + metadata_bytes + count * _RECORD.size + 32:
            raise SpatialCacheCorruptError(f"Spatial cache layout is invalid: path={path}, bytes={size}, records={count}")
        try:
            actual = json.loads(stream.read(metadata_bytes))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SpatialCacheCorruptError(f"Spatial cache metadata is invalid: path={path}") from error
        expected = json.loads(identity.metadata)
        if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected.items()):
            raise SpatialCacheCorruptError(f"Spatial cache source identity differs: path={path}")
        payload = stream.read(count * _RECORD.size)
        if len(payload) != count * _RECORD.size:
            raise SpatialCacheCorruptError(f"Spatial cache record is truncated: path={path}, index={len(payload) // _RECORD.size}")
        if stream.read(32) != hashlib.sha256(payload).digest() or stream.read(1):
            raise SpatialCacheCorruptError(f"Spatial cache payload SHA256 differs: path={path}")
    return _records_from_payload(payload, path)


def _records_from_payload(payload: bytes, path: Path) -> tuple[SpatialRecord, ...]:
    """Validate packed numeric columns in native code before materializing records."""
    if not payload:
        return ()
    import numpy as np

    dtype = np.dtype([("id", "<u4"), ("matrix", "V64"), ("present", "u1"),
                      ("bounds", "<f8", (6,)), ("origin", "<f8", (2,))])
    if dtype.itemsize != _RECORD.size or len(payload) % _RECORD.size:
        raise SpatialCacheCorruptError(f"Spatial cache record layout is invalid: path={path}")
    rows = np.frombuffer(payload, dtype=dtype)
    matrices = np.ndarray((len(rows), 16), dtype="<f4", buffer=payload,
                          offset=4, strides=(_RECORD.size, 4))
    present = rows["present"]
    bounds = rows["bounds"]
    origin = rows["origin"]
    invalid_marker = np.flatnonzero((present > 1) | ((present == 0) & np.any(bounds != 0, axis=1)))
    if invalid_marker.size:
        raise SpatialCacheCorruptError(
            f"Spatial cache null-bounds marker is invalid: path={path}, index={int(invalid_marker[0])}")
    invalid_matrix = np.flatnonzero(~np.all(np.isfinite(matrices), axis=1))
    if invalid_matrix.size:
        raise SpatialCacheCorruptError(
            f"Spatial matrix requires 16 finite float32 values: {path}, entity_id={int(rows['id'][invalid_matrix[0]])}")
    invalid_origin = np.flatnonzero(~np.all(np.isfinite(origin), axis=1)
                                    | (origin[:, 0] != matrices[:, 12]) | (origin[:, 1] != matrices[:, 14]))
    if invalid_origin.size:
        raise SpatialCacheCorruptError(
            f"Spatial origin differs from matrix: {path}, entity_id={int(rows['id'][invalid_origin[0]])}")
    invalid_bounds = np.flatnonzero((present == 1) & (
        ~np.all(np.isfinite(bounds), axis=1) | np.any(bounds[:, ::2] > bounds[:, 1::2], axis=1)))
    if invalid_bounds.size:
        raise SpatialCacheCorruptError(
            f"Spatial bounds require six finite ordered values or null: {path}, entity_id={int(rows['id'][invalid_bounds[0]])}")
    identifiers, counts = np.unique(rows["id"], return_counts=True)
    duplicates = identifiers[counts > 1]
    if duplicates.size:
        raise SpatialCacheCorruptError(f"Spatial cache entity ID repeats: path={path}, entity_id={int(duplicates[0])}")
    return tuple({"entity_id": entity_id, "matrix_blob": matrix,
                  "world_bounds": cast(WorldBounds, tuple(values[:6])) if marker else None,
                  "x": values[6], "z": values[7]}
                 for entity_id, matrix, marker, *values in _RECORD.iter_unpack(payload))


def _write(identity: SpatialSourceIdentity, records: tuple[SpatialRecord, ...]) -> None:
    path = identity.cache_path
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = identity.metadata.encode("utf-8")
    if len(metadata) > _MAX_HEADER_BYTES:
        raise ValueError(f"Spatial source metadata exceeds header limit: source={identity.backing_path}, bytes={len(metadata)}")
    descriptor, name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_HEADER.pack(_MAGIC, len(metadata), len(records)))
            stream.write(metadata)
            digest = hashlib.sha256()
            for record in records:
                bounds = record["world_bounds"]
                raw = _RECORD.pack(record["entity_id"], record["matrix_blob"], int(bounds is not None),
                                   *(bounds if bounds is not None else (0.0,) * 6), record["x"], record["z"])
                stream.write(raw)
                digest.update(raw)
            stream.write(digest.digest())
            stream.flush()
            os.fsync(stream.fileno())
        require_spatial_source_version(identity)
        try:
            # Windows publishes exclusively; POSIX atomically replaces the same source version.
            os.rename(temporary, path)
        except FileExistsError:
            if _load(identity) != records:
                raise SpatialCacheCorruptError(f"Concurrent spatial cache builders disagree: path={path}")
    finally:
        temporary.unlink(missing_ok=True)


def read_spatial_source_records(
    identity: SpatialSourceIdentity, vfs: Path, entry: VfsFile,
    read_chunk: Callable[[Path, VfsFile], bytes],
    iter_entities: Callable[[bytes], Iterable[Mapping[str, object]]],
    check_cancelled: Callable[[], None] | None,
) -> SpatialSourceRecords:
    """Lazily fill absent records and retain only this source's cold decoded chunk."""
    if identity.cache_path.exists():
        records = _load(identity)
        require_spatial_source_version(identity)
        return SpatialSourceRecords(records, None)
    chunk = read_chunk(vfs, entry)
    records_by_id: dict[int, SpatialRecord] = {}
    for index, value in enumerate(iter_entities(chunk)):
        if check_cancelled is not None and index % 8192 == 0:
            check_cancelled()
        record = _record(value, entry.file_name)
        # Preserve the selector's source-local last-record-wins contract.
        records_by_id[record["entity_id"]] = record
    records = tuple(records_by_id.values())
    require_spatial_source_version(identity)
    _write(identity, records)
    return SpatialSourceRecords(records, chunk)
