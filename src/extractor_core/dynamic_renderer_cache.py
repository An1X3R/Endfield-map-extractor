"""Persist complete dynamic renderer inventories with live source-version checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from typing import BinaryIO, Callable, Literal, cast

from endfield_scene_io import SceneInputChangedError, _file_stamp, cached_scene_source_value, resolve_scene_read_path
from extract_scene_renderer_materials import RendererDraw
from .dynamic_renderers import (
    INIT_FILE, DynamicEcsReference, DynamicRendererGroup, DynamicRendererPair, DynamicRendererRecord,
    DynamicRendererSource, DynamicSceneRendererInventory, _RENDERER_INVENTORY_CACHE,
    _decode_dynamic_scene_renderers_for_scene,
)
from .effects import GROUP_HASH, MAIN_FILE, _dynamic_inventory_qualifiers, _dynamic_inventory_sources, _require_dynamic_inventory_sources
from .resources import BootstrapExtractionError, validate_vfs_root
from .scene_identity import validate_scene_id


_MAGIC = b"EFDYN002"
_HEADER = struct.Struct("<8sI4Q")
_LENGTH = struct.Struct("<I")
_STRING_COUNT = struct.Struct("<Q")
_RECORD = struct.Struct("<IBB16f20f")
_BOUNDS = struct.Struct("<6d")
_PAIR = struct.Struct("<II40s24sff")
_NULL_NAME = 0xFFFFFFFF
_MAX_JSON_BYTES = 16 * 1024 * 1024


class DynamicRendererCacheCorruptError(ValueError):
    """A persisted inventory does not match its authenticated source or schema."""


@dataclass(frozen=True)
class _Identity:
    metadata: bytes
    path: Path


def dynamic_renderer_parser_identity() -> str:
    """Bind the format to the exact cache, decoder and source-reading implementations."""
    core = Path(__file__).resolve().parent
    paths = (Path(__file__), *(core / name for name in (
        "dynamic_renderers.py", "effects.py", "resources.py", "vfs.py", "flatbuffer.py",
        "lz4.py", "scene_identity.py", "projected_decals.py")),
        core.parent / "extract_scene_renderer_materials.py", core.parent / "endfield_scene_io.py")
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _identity(cache: Path, scene: str, sources: tuple[Path, ...]) -> _Identity:
    metadata = json.dumps({"format": "DynamicSceneRendererInventory/2", "scene_id": scene,
                           "parser": dynamic_renderer_parser_identity(),
                           "sources": [{"lexical_path": str(path), "resolved_path": str(resolve_scene_read_path(path)),
                                        "stamp": asdict(_file_stamp(resolve_scene_read_path(path)))} for path in sources]},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(metadata) > _MAX_JSON_BYTES:
        raise ValueError(f"Dynamic renderer cache identity exceeds size limit: scene={scene}, bytes={len(metadata)}")
    key = hashlib.sha256(metadata).hexdigest()
    return _Identity(metadata, cache / "dynamic_renderers_v2" / key[:2] / f"{scene}_{key}.bin")


def _require_version(identity: _Identity, cache: Path, scene: str, root: Path, hash_path: Path,
                     sources: tuple[Path, ...]) -> None:
    _require_dynamic_inventory_sources(root, hash_path, sources)
    if _identity(cache, scene, sources).metadata != identity.metadata:
        raise SceneInputChangedError(f"Dynamic renderer sources changed during cache read: scene={scene}, path={identity.path}")


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache requires an object: {context}")
    return cast(dict[str, object], value)


def _required(row: dict[str, object], name: str) -> object:
    if name not in row:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache field is missing: field={name}")
    return row[name]


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache integer is invalid: value={value}, range=({minimum}, {maximum})")
    return cast(int, value)


def _index(row: dict[str, object], name: str) -> int:
    return _integer(_required(row, name), 0, 0xFFFFFFFF)


def _number(value: object) -> float:
    if type(value) not in (int, float):
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache requires a finite number: value={value}")
    try:
        number = float(cast(float, value))
    except OverflowError as error:
        raise DynamicRendererCacheCorruptError("Dynamic renderer cache number exceeds float range") from error
    if not math.isfinite(number):
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache requires a finite number: value={value}")
    return number


def _string(value: object) -> str:
    if type(value) is not str or not value:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache requires a nonempty string: value={value!r}")
    return cast(str, value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise DynamicRendererCacheCorruptError("Dynamic renderer cache requires an array")
    return cast(list[object], value)


def _vector(value: object, length: int) -> tuple[float, ...]:
    rows = _array(value)
    if len(rows) != length:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache vector length differs: expected={length}, actual={len(rows)}")
    return tuple(_number(item) for item in rows)


def _hex(value: object, length: int) -> str:
    result = _string(value)
    if len(result) != length or any(char not in "0123456789abcdefABCDEF" for char in result):
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache hex is invalid: expectedCharacters={length}, value={result}")
    return result


def _source(value: object, scene: str) -> DynamicRendererSource:
    row = _object(value, "source")
    path, kind = _string(_required(row, "virtual_path")), _required(row, "kind")
    match = MAIN_FILE.fullmatch(path) if kind == "main" else INIT_FILE.fullmatch(path) if kind == "init" else None
    length, decoded = (_integer(_required(row, name), 0, 0xFFFFFFFFFFFFFFFF)
                       for name in ("byte_length", "decoded_byte_length"))
    if match is None or match[1].casefold() != scene or decoded < 8 or length < 4 or (kind == "main" and length != decoded):
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache source identity is invalid: path={path}, kind={kind}")
    return DynamicRendererSource(path, _hex(_required(row, "data_md5"), 32), length, decoded,
                                 _hex(_required(row, "decoded_sha256"), 64), cast(Literal["main", "init"], kind))


def _source_for(row: dict[str, object], sources: dict[str, DynamicRendererSource], kind: str) -> DynamicRendererSource:
    path = _string(_required(row, "source_virtual_path"))
    source = sources.get(path)
    if source is None or source.kind != kind:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache source link is invalid: path={path}, kind={kind}")
    return source


def _reference(value: object, sources: dict[str, DynamicRendererSource]) -> DynamicEcsReference:
    row = _object(value, "reference")
    source = _source_for(row, sources, "main")
    grid, index, offset = (_index(row, name) for name in ("grid_index", "reference_index", "record_offset"))
    key = _string(_required(row, "key"))
    radius, distance = (_number(_required(row, name)) for name in ("radius", "min_distance"))
    if key != f"{source.virtual_path}:{grid}:{index}" or offset + 36 > source.decoded_byte_length or min(radius, distance) < 0:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache reference semantics differ: key={key}")
    return DynamicEcsReference(key, source.virtual_path, grid, index, offset, _index(row, "chunk_id"),
                               _index(row, "group_index"), cast(tuple[float, float, float], _vector(_required(row, "center"), 3)),
                               radius, distance, _integer(_required(row, "importance"), -0x80000000, 0x7FFFFFFF),
                               _index(row, "scene_index"))


def _group(value: object, sources: dict[str, DynamicRendererSource],
           references: dict[tuple[int, int], tuple[str, ...]]) -> DynamicRendererGroup:
    row = _object(value, "group")
    source = _source_for(row, sources, "init")
    chunk, scene_index, index, count, offset, length = (_index(row, name) for name in (
        "chunk_id", "init_scene_index", "group_index", "record_count", "blob_offset", "blob_length"))
    match = INIT_FILE.fullmatch(source.virtual_path)
    descriptors = tuple(tuple(_integer(part, 0, 0xFFFFFFFF) for part in _array(item))
                        for item in _array(_required(row, "descriptors")))
    if any(len(item) != 3 for item in descriptors):
        raise DynamicRendererCacheCorruptError("Dynamic renderer cache descriptor length differs")
    columns: dict[int, int] = {}
    for kind, stride, reserved in descriptors:
        expected = 8 + 40 * (1 << (kind - 68)) if 68 <= kind <= 74 else 4 + 32 * (1 << (kind - 8)) if 8 <= kind <= 14 else 256 if kind == 18 else stride
        if kind > 0xFFFF or not 0 < stride <= 0xFFFF or reserved or kind in columns or stride != expected:
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache descriptor is invalid: source={source.virtual_path}, kind={kind}")
        columns[kind] = stride
    ref_kinds = tuple(kind for kind in columns if 68 <= kind <= 74)
    keys = tuple(_string(item) for item in _array(_required(row, "reference_keys")))
    canonical = references.get((chunk, index))
    status = _required(row, "status")
    if (match is None or (int(match[2]), int(match[3])) != (scene_index, chunk) or canonical is None or keys != canonical
            or (length > 0 and offset < 4) or offset + length > source.decoded_byte_length or length != count * sum(columns.values())
            or status != ("renderer" if ref_kinds else "no_renderer_references")):
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache group semantics differ: source={source.virtual_path}, group={index}")
    if ref_kinds and (len(ref_kinds) != 1 or 18 not in columns or ref_kinds[0] - 60 not in columns
                     or any(columns.get(kind, stride) != stride for kind, stride in ((21, 64), *((i, 4) for i in range(6))))
                     or (any(i in columns for i in range(6)) and not all(i in columns for i in range(6)))):
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache renderer columns differ: source={source.virtual_path}, group={index}")
    return DynamicRendererGroup(source.virtual_path, chunk, scene_index, index, count,
                                cast(tuple[tuple[int, int, int], ...], descriptors), offset, length, canonical,
                                cast(Literal["renderer", "no_renderer_references"], status))


def _read_raw(stream: BinaryIO, length: int, update_digest: Callable[[bytes], None], path: Path) -> bytes:
    raw = stream.read(length)
    if len(raw) != length:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache binary data is truncated: path={path}, expectedBytes={length}")
    update_digest(raw)
    return raw


def _read_strings(stream: BinaryIO, update_digest: Callable[[bytes], None], path: Path) -> tuple[str, ...]:
    count = _STRING_COUNT.unpack(_read_raw(stream, _STRING_COUNT.size, update_digest, path))[0]
    if count > min(0xFFFFFFFF, path.stat().st_size // 5):
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache string count is invalid: path={path}, count={count}")
    strings: list[str] = []
    for _ in range(count):
        length = _LENGTH.unpack(_read_raw(stream, _LENGTH.size, update_digest, path))[0]
        if not 0 < length <= _MAX_JSON_BYTES:
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache string size is invalid: path={path}, bytes={length}")
        try:
            strings.append(_read_raw(stream, length, update_digest, path).decode("utf-8"))
        except UnicodeDecodeError as error:
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache string UTF8 is invalid: path={path}") from error
    if len(set(strings)) != len(strings):
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache string table repeats: path={path}")
    return tuple(strings)


def _read_records(stream: BinaryIO, groups: tuple[DynamicRendererGroup, ...], strings: tuple[str, ...],
                  update_digest: Callable[[bytes], None], path: Path,
                  check_cancelled: Callable[[], None]) -> tuple[DynamicRendererRecord, ...]:
    mesh_valid = tuple(value == value.casefold() and Path(value.split("##")[0]).suffix.casefold() in (".fbx", ".mesh", ".asset") for value in strings)
    material_valid = tuple(value == value.casefold() and value.endswith(".mat") for value in strings)
    records: list[DynamicRendererRecord] = []
    for group in groups:
        if group.status != "renderer":
            continue
        columns: dict[int, tuple[int, int]] = {}
        cursor = group.blob_offset
        for kind, stride, _reserved in group.descriptors:
            columns[kind] = cursor, stride
            cursor += group.record_count * stride
        reference_kind = next(kind for kind in columns if 68 <= kind <= 74)
        common_start, common_stride = columns[18]
        reference_start, reference_stride = columns[reference_kind]
        runtime_start, runtime_stride = columns[reference_kind - 60]
        capacity = (reference_stride - 8) // 40
        bounds_present = 0 in columns
        for index in range(group.record_count):
            if len(records) % 1024 == 0:
                check_cancelled()
            name_id, present, npairs, *numbers = _RECORD.unpack(_read_raw(stream, _RECORD.size, update_digest, path))
            if (present != int(bounds_present) or npairs > capacity or
                    (name_id != _NULL_NAME and name_id >= len(strings)) or not all(map(math.isfinite, numbers))):
                raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache binary record is invalid: path={path}, group={group.group_index}, record={index}")
            name = None if name_id == _NULL_NAME else strings[name_id]
            if name is not None and 21 not in columns:
                raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache unexpected name: path={path}, group={group.group_index}, record={index}")
            bounds = _BOUNDS.unpack(_read_raw(stream, _BOUNDS.size, update_digest, path)) if present else None
            if bounds is not None and (not all(map(math.isfinite, bounds)) or any(bounds[i] > bounds[i + 1] for i in (0, 2, 4))):
                raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache bounds are invalid: path={path}, group={group.group_index}, record={index}")
            reference_offset = reference_start + index * reference_stride
            pairs: list[DynamicRendererPair] = []
            for pair_index in range(npairs):
                mesh_id, material_id, reference, runtime, lod_max, lod_min = _PAIR.unpack(_read_raw(stream, _PAIR.size, update_digest, path))
                if (mesh_id >= len(strings) or material_id >= len(strings) or not mesh_valid[mesh_id] or not material_valid[material_id]
                        or not math.isfinite(lod_max) or not math.isfinite(lod_min) or lod_max < lod_min):
                    raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache binary pair is invalid: path={path}, group={group.group_index}, record={index}, pair={pair_index}")
                reference_hex = reference.hex()
                draw = RendererDraw(runtime[2], struct.unpack_from("<I", runtime, 16)[0], lod_max, lod_min)
                pairs.append(DynamicRendererPair(strings[mesh_id], strings[material_id], reference_hex[16:32], reference_hex[:16],
                                                 reference_offset + 8 + pair_index * 40, draw, reference_hex, runtime.hex()))
            records.append(DynamicRendererRecord(
                f"{group.source_virtual_path}:{group.group_index}:{index}", group.source_virtual_path, group.chunk_id,
                group.init_scene_index, group.group_index, index, group.reference_keys, name,
                tuple(numbers[:16]), bounds, tuple(numbers[16:]), common_start + index * common_stride,
                reference_offset, runtime_start + index * runtime_stride, tuple(pairs)))
    return tuple(records)


def _inventory_strings(records: tuple[DynamicRendererRecord, ...]) -> dict[str, int]:
    strings: dict[str, int] = {}
    for record in records:
        if record.name is not None:
            strings.setdefault(record.name, len(strings))
        for pair in record.pairs:
            strings.setdefault(pair.mesh_path, len(strings))
            strings.setdefault(pair.material_path, len(strings))
    return strings


def _write_strings(stream: BinaryIO, strings: dict[str, int], update_digest: Callable[[bytes], None]) -> None:
    raw = _STRING_COUNT.pack(len(strings))
    stream.write(raw)
    update_digest(raw)
    for value in strings:
        encoded = value.encode("utf-8")
        if not 0 < len(encoded) <= _MAX_JSON_BYTES:
            raise ValueError(f"Dynamic renderer cache string size is invalid: bytes={len(encoded)}")
        raw = _LENGTH.pack(len(encoded)) + encoded
        stream.write(raw)
        update_digest(raw)


def _write_record(stream: BinaryIO, record: DynamicRendererRecord, strings: dict[str, int],
                  update_digest: Callable[[bytes], None]) -> None:
    raw = _RECORD.pack(_NULL_NAME if record.name is None else strings[record.name], int(record.bounds is not None),
                       len(record.pairs), *record.matrix, *record.serialized_draw_parameters)
    stream.write(raw)
    update_digest(raw)
    if record.bounds is not None:
        raw = _BOUNDS.pack(*record.bounds)
        stream.write(raw)
        update_digest(raw)
    for pair in record.pairs:
        reference, runtime = bytes.fromhex(pair.reference_record_hex), bytes.fromhex(pair.runtime_record_hex)
        if (len(reference) != 40 or len(runtime) != 24 or reference[8:16].hex() != pair.mesh_hash_hex
                or reference[:8].hex() != pair.material_hash_hex or runtime[2] != pair.draw.submesh
                or struct.unpack_from("<I", runtime, 16)[0] != pair.draw.flags):
            raise ValueError(f"Dynamic renderer cache input pair semantics differ: key={record.key}, offset={pair.reference_offset}")
        raw = _PAIR.pack(strings[pair.mesh_path], strings[pair.material_path], reference, runtime,
                         pair.draw.lod_max_squared, pair.draw.lod_min_squared)
        stream.write(raw)
        update_digest(raw)


def _read_json(stream: BinaryIO, update_digest: Callable[[bytes], None], path: Path) -> object:
    raw_length = stream.read(_LENGTH.size)
    if len(raw_length) != _LENGTH.size:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache row header is truncated: path={path}")
    length = _LENGTH.unpack(raw_length)[0]
    if not 0 < length <= _MAX_JSON_BYTES:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache row size is invalid: path={path}, bytes={length}")
    raw = stream.read(length)
    if len(raw) != length:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache row is truncated: path={path}, expectedBytes={length}")
    update_digest(raw_length + raw)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache JSON is invalid: path={path}") from error


def _load(identity: _Identity, scene: str, check_cancelled: Callable[[], None]) -> DynamicSceneRendererInventory:
    path = identity.path
    with path.open("rb") as stream:
        header = stream.read(_HEADER.size)
        if len(header) != _HEADER.size:
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache header is truncated: path={path}")
        magic, length, nsources, nrefs, ngroups, nrecords = _HEADER.unpack(header)
        if magic != _MAGIC or length != len(identity.metadata) or sum((nsources, nrefs, ngroups, nrecords)) > path.stat().st_size // 5:
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache header is invalid: path={path}")
        metadata = stream.read(length)
        if metadata != identity.metadata:
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache source/parser identity differs: path={path}")
        digest = hashlib.sha256(header + metadata)
        sources = tuple(_source(_read_json(stream, digest.update, path), scene) for _ in range(nsources))
        source_map = {source.virtual_path: source for source in sources}
        if not sources or len(source_map) != len(sources):
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache source list is empty or repeats: path={path}")
        refs = tuple(_reference(_read_json(stream, digest.update, path), source_map) for _ in range(nrefs))
        references: dict[tuple[int, int], list[str]] = {}
        for ref in refs:
            references.setdefault((ref.chunk_id, ref.group_index), []).append(ref.key)
        if len({ref.key for ref in refs}) != len(refs):
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache reference keys repeat: path={path}")
        reference_map = {key: tuple(value) for key, value in references.items()}
        groups = tuple(_group(_read_json(stream, digest.update, path), source_map, reference_map) for _ in range(ngroups))
        group_map = {(group.source_virtual_path, group.group_index): group for group in groups}
        if (len(group_map) != len(groups) or len(groups) != len(references)
                or {(group.chunk_id, group.group_index) for group in groups} != set(references)):
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache group coverage differs: path={path}")
        if nrecords != sum(group.record_count for group in groups if group.status == "renderer"):
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache record coverage/order differs: path={path}")
        strings = _read_strings(stream, digest.update, path)
        records = _read_records(stream, groups, strings, digest.update, path, check_cancelled)
        if stream.read(32) != digest.digest() or stream.read(1):
            raise DynamicRendererCacheCorruptError(f"Dynamic renderer cache payload SHA256 differs: path={path}")
    return DynamicSceneRendererInventory(scene, sources, refs, groups, records)


def _write(identity: _Identity, inventory: DynamicSceneRendererInventory, require_version: Callable[[], None],
           check_cancelled: Callable[[], None]) -> None:
    path = identity.path
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            sections = (inventory.sources, inventory.references, inventory.groups, inventory.records)
            header = _HEADER.pack(_MAGIC, len(identity.metadata), *(len(rows) for rows in sections))
            stream.write(header + identity.metadata)
            digest = hashlib.sha256(header + identity.metadata)
            for rows in sections[:3]:
                for index, row in enumerate(rows):
                    if index % 1024 == 0:
                        check_cancelled()
                    raw = json.dumps(asdict(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
                    if len(raw) > _MAX_JSON_BYTES:
                        raise ValueError(f"Dynamic renderer cache row exceeds size limit: path={path}, bytes={len(raw)}")
                    frame = _LENGTH.pack(len(raw)) + raw
                    digest.update(frame)
                    stream.write(frame)
            strings = _inventory_strings(inventory.records)
            _write_strings(stream, strings, digest.update)
            for index, record in enumerate(inventory.records):
                if index % 1024 == 0:
                    check_cancelled()
                _write_record(stream, record, strings, digest.update)
            stream.write(digest.digest())
            stream.flush()
            os.fsync(stream.fileno())
        check_cancelled()
        require_version()
        try:
            os.rename(temporary, path)
        except FileExistsError:
            if _load(identity, inventory.scene_id, check_cancelled) != inventory:
                raise DynamicRendererCacheCorruptError(f"Concurrent dynamic renderer cache builders disagree: path={path}")
    finally:
        temporary.unlink(missing_ok=True)


def read_dynamic_scene_renderers_cached(
    vfs_root: Path, string_path_hash: Path, scene_id: str, cache_dir: Path, check_cancelled: Callable[[], None],
) -> DynamicSceneRendererInventory:
    """Reuse all source records and provenance without introducing spatial selection."""
    check_cancelled()
    scene = validate_scene_id(scene_id)
    root = validate_vfs_root(vfs_root)
    hash_path = string_path_hash.expanduser().resolve()
    if not hash_path.is_file():
        raise FileNotFoundError(f"Dynamic ECS StringPathHash source is missing: {hash_path}")
    cache = cache_dir.expanduser().resolve()
    if cache.is_relative_to(root) or cache.is_relative_to(hash_path):
        raise ValueError(f"Dynamic renderer cache must not write into the read-only game tree or metadata file: {cache}")
    sources = _dynamic_inventory_sources(root, hash_path)
    if len(sources) < 3 or not sources[1].is_file():
        raise BootstrapExtractionError(
            f"Required VFS group {GROUP_HASH} is incomplete under {root / GROUP_HASH}. Verify the selected game build."
        )

    def read() -> DynamicSceneRendererInventory:
        identity = _identity(cache, scene, sources)

        def require_version() -> None:
            _require_version(identity, cache, scene, root, hash_path, sources)

        if identity.path.is_file():
            try:
                inventory = _load(identity, scene, check_cancelled)
            except DynamicRendererCacheCorruptError as error:
                raise DynamicRendererCacheCorruptError(f"{error}, cachePath={identity.path}") from error
            require_version()
        else:
            inventory = _decode_dynamic_scene_renderers_for_scene(root, hash_path, scene, check_cancelled)
            require_version()
            _write(identity, inventory, require_version, check_cancelled)
        return inventory

    result = cached_scene_source_value(_RENDERER_INVENTORY_CACHE, sources, _dynamic_inventory_qualifiers((scene,), sources), read)
    check_cancelled()
    return result
