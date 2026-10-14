"""Read native scene Light components from paired Streaming and Init chunks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
import gzip
import hashlib
import json
import math
from pathlib import Path
import struct
import tempfile

from . import flatbuffer as fb
from .projected_decals import build_path_id_index, read_chunk, streaming_records
from .scene_identity import validate_scene_id


_GROUP = "C3442D43"
_TRANSFORM = 1
_LIGHT = 131072
_ADDITIONAL = 33554432
_LENS_FLARE = 536870912
_SIZES = {1: 1, 2: 4, 3: 4, 4: 4, 9: 12, 10: 16, 15: 8}


@dataclass(frozen=True)
class LightProperty:
    encoded_id: int
    data_type: int
    raw_hex: str


@dataclass(frozen=True)
class NativeLight:
    map_id: str
    entity_id: int
    name: str
    position: tuple[float, float, float]
    euler_zxy: tuple[float, float, float]
    scale: tuple[float, float, float]
    light_type: int
    shape: int
    color: tuple[float, float, float, float]
    intensity: float
    range: float
    spot_angle: float
    inner_spot_angle: float
    linear_length: float
    color_temperature: float
    use_color_temperature: bool
    enable_obb: bool
    character_only: bool
    falloff_exponent: float
    cookie_path: str | None
    component_mask: int
    light_properties: tuple[LightProperty, ...]
    additional_properties: tuple[LightProperty, ...]
    source_init_paths: tuple[str, ...]
    source_streaming_paths: tuple[str, ...]
    baseline_sha256: str


@dataclass(frozen=True)
class NativeLightInventory:
    map_id: str
    rows: tuple[NativeLight, ...]
    source_sha256: tuple[tuple[str, str], ...]
    physical_record_count: int


def preview_temperature_rgb(kelvin: float) -> tuple[tuple[float, float, float], bool]:
    """Evaluate the bundled MaterialX Planckian-locus RGB approximation for a preview."""
    if not math.isfinite(kelvin) or kelvin <= 0:
        raise ValueError(f"Native Light color temperature invalid: kelvin={kelvin}")
    bounded = min(25000.0, max(1667.0, kelvin))
    t = 1000.0 / bounded
    if bounded < 4000.0:
        x = -0.2661239 * t**3 - 0.2343580 * t**2 + 0.8776956 * t + 0.179910
    else:
        x = -3.0258469 * t**3 + 2.1070379 * t**2 + 0.2226347 * t + 0.240390
    if bounded < 2222.0:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif bounded < 4000.0:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    if y <= 0.0:
        raise ValueError(f"Native Light color temperature chromaticity invalid: kelvin={kelvin}, y={y}")
    xyz = (x / y, 1.0, (1.0 - x - y) / y)
    rgb = (
        max(0.0, 3.2406 * xyz[0] - 1.5372 * xyz[1] - 0.4986 * xyz[2]),
        max(0.0, -0.9689 * xyz[0] + 1.8758 * xyz[1] + 0.0415 * xyz[2]),
        max(0.0, 0.0557 * xyz[0] - 0.2040 * xyz[1] + 1.0570 * xyz[2]),
    )
    return rgb, kelvin != bounded


def _properties(data: bytes, table: int, context: str) -> dict[int, tuple[int, bytes]]:
    ids, id_count = fb.vector_from_field(data, table, 3)
    types, type_count = fb.vector_from_field(data, table, 4)
    values, value_count = fb.vector_from_field(data, table, 5)
    if id_count != type_count or id_count != value_count:
        raise ValueError(f"Light property vector lengths differ: {context}, counts={(id_count, type_count, value_count)}")
    result: dict[int, tuple[int, bytes]] = {}
    for index in range(id_count):
        encoded_id = fb.u32(data, ids + 4 * index)
        data_type = data[types + index]
        if encoded_id in result or data_type not in _SIZES:
            raise ValueError(f"Invalid Light property: {context}, id={encoded_id:#x}, type={data_type}")
        start = fb.offset_target(data, values + 4 * index)
        end = start + _SIZES[data_type]
        if start < 0 or end > len(data):
            raise ValueError(f"Light property exceeds chunk: {context}, id={encoded_id:#x}, offset={start}")
        result[encoded_id] = data_type, data[start:end]
    return result


def _native_record(data: bytes, table: int, context: str) -> tuple[str, int, int, dict[int, dict[int, tuple[int, bytes]]]]:
    name_field = fb.field_address(data, table, 0)
    header_field = fb.field_address(data, table, 1)
    mask_field = fb.field_address(data, table, 3)
    if name_field is None or header_field is None or mask_field is None or fb.field_address(data, table, 2) is not None:
        raise ValueError(f"Native Light record header differs from verified layout: {context}")
    name_start = fb.offset_target(data, name_field)
    length = fb.u32(data, name_start)
    if name_start + 4 + length > len(data):
        raise ValueError(f"Native Light name exceeds chunk: {context}")
    name = data[name_start + 4:name_start + 4 + length].decode("utf-8")
    header = fb.u32(data, header_field)
    mask = struct.unpack_from("<Q", data, mask_field)[0]
    vector, count = fb.vector_from_field(data, table, 4)
    components: dict[int, dict[int, tuple[int, bytes]]] = {}
    for index in range(count):
        component = fb.table_vector_element(data, vector, index)
        bit_field = fb.field_address(data, component, 0)
        if bit_field is None:
            raise ValueError(f"Native Light component bit missing: {context}, index={index}")
        bit = struct.unpack_from("<Q", data, bit_field)[0]
        if bit in components or bit & mask == 0:
            raise ValueError(f"Native Light component bit invalid: {context}, bit={bit:#x}")
        components[bit] = _properties(data, component, f"{context}, bit={bit:#x}")
    if sum(components) != mask:
        raise ValueError(f"Native Light component mask differs: {context}, mask={mask:#x}, bits={tuple(components)}")
    return name, header, mask, components


def _required(props: dict[int, tuple[int, bytes]], encoded_id: int, data_type: int, context: str) -> bytes:
    value = props.get(encoded_id)
    if value is None or value[0] != data_type:
        raise ValueError(f"Native Light field missing or type differs: {context}, id={encoded_id:#x}, expectedType={data_type}")
    return value[1]


def _float(props: dict[int, tuple[int, bytes]], encoded_id: int, context: str) -> float:
    value = struct.unpack("<f", _required(props, encoded_id, 4, context))[0]
    if not math.isfinite(value):
        raise ValueError(f"Native Light field is nonfinite: {context}, id={encoded_id:#x}")
    return value


def _int(props: dict[int, tuple[int, bytes]], encoded_id: int, context: str) -> int:
    return struct.unpack("<i", _required(props, encoded_id, 2, context))[0]


def _bool(props: dict[int, tuple[int, bytes]], encoded_id: int, context: str) -> bool:
    value = _required(props, encoded_id, 1, context)[0]
    if value not in (0, 1):
        raise ValueError(f"Native Light boolean invalid: {context}, id={encoded_id:#x}, value={value}")
    return bool(value)


def _vector(props: dict[int, tuple[int, bytes]], encoded_id: int, data_type: int,
            context: str) -> tuple[float, ...]:
    value = struct.unpack("<" + ("fff" if data_type == 9 else "ffff"),
                          _required(props, encoded_id, data_type, context))
    if not all(math.isfinite(item) for item in value):
        raise ValueError(f"Native Light vector is nonfinite: {context}, id={encoded_id:#x}")
    return value


def _raw_items(props: dict[int, tuple[int, bytes]]) -> tuple[LightProperty, ...]:
    return tuple(LightProperty(key, data_type, raw.hex())
                 for key, (data_type, raw) in sorted(props.items()))


def _fingerprint(name: str, mask: int, components: dict[int, dict[int, tuple[int, bytes]]]) -> str:
    digest = hashlib.sha256()
    digest.update(name.encode("utf-8"))
    digest.update(struct.pack("<Q", mask))
    for bit, props in sorted(components.items()):
        digest.update(struct.pack("<Q", bit))
        for key, (data_type, raw) in sorted(props.items()):
            digest.update(struct.pack("<IB", key, data_type))
            digest.update(raw)
    return digest.hexdigest()


def _light_row(map_id: str, entity_id: int, name: str, mask: int,
               components: dict[int, dict[int, tuple[int, bytes]]], cookie_path: str | None,
               init_path: str, streaming_path: str) -> NativeLight:
    context = f"map={map_id}, entity={entity_id}, init={init_path}"
    if _TRANSFORM not in components or _LIGHT not in components or _ADDITIONAL not in components:
        raise ValueError(f"Native Light required components missing: {context}, bits={tuple(components)}")
    transform, light, additional = (components[bit] for bit in (_TRANSFORM, _LIGHT, _ADDITIONAL))
    if len(transform) != 3 or len(light) != 60 or len(additional) != 21:
        raise ValueError(f"Native Light property schema differs: {context}, counts={(len(transform), len(light), len(additional))}")
    return NativeLight(
        map_id, entity_id, name,
        _vector(transform, 0x00000000, 9, context),
        _vector(transform, 0x00010000, 9, context),
        _vector(transform, 0x00020000, 9, context),
        _int(light, 0x11000000, context),
        _int(light, 0x11010000, context),
        _vector(light, 0x11020000, 10, context),
        _float(light, 0x11030000, context),
        _float(light, 0x11040000, context),
        _float(light, 0x11050000, context),
        _float(light, 0x11070000, context),
        _float(light, 0x11060000, context),
        _float(light, 0x11150000, context),
        _bool(light, 0x11160000, context),
        _bool(light, 0x111a0000, context),
        _bool(additional, 0x19100000, context),
        _float(additional, 0x19120000, context),
        cookie_path, mask, _raw_items(light), _raw_items(additional),
        (init_path,), (streaming_path,), _fingerprint(name, mask, components),
    )


def read_native_lights(vfs_root: Path, path_hash: Path, map_id: str,
                       check_cancelled: Callable[[], None] | None) -> NativeLightInventory:
    """Read map-local native lights; every selected source is verified against its Init partner."""
    from .resources import load_group

    validate_scene_id(map_id)
    entries = {entry.file_name: entry for entry in load_group(vfs_root, _GROUP).iter_files()}
    source_sha256: dict[str, str] = {}
    paths = build_path_id_index(path_hash)
    rows: dict[int, NativeLight] = {}
    physical_count = 0
    prefix = f"Data/Streaming/PC/{map_id}/Streaming/StreamingChunkData_"
    for streaming_path, entry in sorted(entries.items()):
        if not streaming_path.startswith(prefix) or not streaming_path.endswith(".bytes"):
            continue
        if check_cancelled is not None:
            check_cancelled()
        stream = read_chunk(vfs_root, entry)
        stream_records = streaming_records(stream)
        hits = []
        for entity_id, record in stream_records.items():
            if record.type_tag != 1:
                continue
            mask_field = fb.field_address(stream, record.start, 3)
            if mask_field is None:
                raise ValueError(f"Streaming component mask missing: path={streaming_path}, entity={entity_id}")
            mask = struct.unpack_from("<Q", stream, mask_field)[0]
            if mask & _LIGHT:
                hits.append((entity_id, record))
        if not hits:
            continue
        init_path = streaming_path.replace("/Streaming/StreamingChunkData_", "/Streaming/InitChunkData_")
        if init_path not in entries:
            raise FileNotFoundError(f"Native Light Init partner missing: {init_path}")
        init = read_chunk(vfs_root, entries[init_path])
        init_records = streaming_records(init)
        source_sha256[streaming_path] = hashlib.sha256(stream).hexdigest()
        source_sha256[init_path] = hashlib.sha256(init).hexdigest()
        for entity_id, stream_record in hits:
            context = f"path={streaming_path}, entity={entity_id}"
            init_record = init_records.get(entity_id)
            if init_record is None or init_record.type_tag != 1:
                raise ValueError(f"Native Light Init record missing: {context}, init={init_path}")
            stream_name, stream_header, stream_mask, stream_components = _native_record(stream, stream_record.start, context)
            name, init_header, init_mask, components = _native_record(init, init_record.start, context)
            if not name or stream_name or stream_header != init_header or stream_mask != init_mask:
                raise ValueError(f"Native Light source pair differs: {context}, init={init_path}")
            if _LENS_FLARE in components and not (init_mask & _LENS_FLARE):
                raise ValueError(f"Native Light flare mask differs: {context}")
            cookie_props = stream_components[_LIGHT]
            if any(key != 0x11220000 for key in cookie_props):
                raise ValueError(f"Unexpected Streaming Light override: {context}, ids={tuple(cookie_props)}")
            cookie_path: str | None = None
            if cookie_props:
                raw = _required(cookie_props, 0x11220000, 15, context)
                matches = paths.get(raw, ())
                if len(matches) != 1:
                    raise ValueError(f"Native Light Cookie path unresolved: {context}, hash={raw.hex()}, matches={matches}")
                cookie_path = matches[0]
            candidate = _light_row(map_id, entity_id, name, init_mask, components,
                                   cookie_path, init_path, streaming_path)
            existing = rows.get(entity_id)
            if existing is not None:
                if existing.baseline_sha256 != candidate.baseline_sha256 or existing.cookie_path != candidate.cookie_path:
                    raise ValueError(f"Native Light duplicate conflicts: {context}, previous={existing.source_init_paths}")
                candidate = replace(
                    existing,
                    source_init_paths=tuple(sorted(set(existing.source_init_paths + (init_path,)))),
                    source_streaming_paths=tuple(sorted(set(existing.source_streaming_paths + (streaming_path,)))),
                )
            rows[entity_id] = candidate
            physical_count += 1
    return NativeLightInventory(map_id, tuple(rows[key] for key in sorted(rows)),
                                tuple(sorted(source_sha256.items())), physical_count)


def _cache_fingerprint(vfs_root: Path, path_hash: Path, map_id: str) -> str:
    from .resources import load_group

    catalog = vfs_root / _GROUP / f"{_GROUP}.blc"
    group = load_group(vfs_root, _GROUP)
    digest = hashlib.sha256()
    for source in (catalog, path_hash, Path(__file__)):
        digest.update(source.read_bytes())
    prefix = f"Data/Streaming/PC/{map_id}/Streaming/"
    chunks = {entry.file_chunk_md5_name for entry in group.iter_files()
              if entry.file_name.startswith(prefix) and
              ("/InitChunkData_" in entry.file_name or "/StreamingChunkData_" in entry.file_name)}
    for name in sorted(chunks):
        chunk = vfs_root / _GROUP / f"{name}.chk"
        info = chunk.stat()
        digest.update(f"{name}:{info.st_size}:{info.st_mtime_ns}\n".encode("ascii"))
    return digest.hexdigest()


def _cache_properties(value: object, context: str) -> tuple[LightProperty, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Native Light cache properties must be an array: {context}")
    result: list[LightProperty] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"Native Light cache property must be an object: {context}[{index}]")
        encoded_id, data_type, raw_hex = (item.get("encoded_id"), item.get("data_type"), item.get("raw_hex"))
        if type(encoded_id) is not int or type(data_type) is not int or data_type not in _SIZES or type(raw_hex) is not str:
            raise ValueError(f"Native Light cache property invalid: {context}[{index}]")
        try:
            raw = bytes.fromhex(raw_hex)
        except ValueError as error:
            raise ValueError(f"Native Light cache property hex invalid: {context}[{index}]") from error
        if len(raw) != _SIZES[data_type]:
            raise ValueError(f"Native Light cache property size invalid: {context}[{index}]")
        result.append(LightProperty(encoded_id, data_type, raw_hex))
    if len({item.encoded_id for item in result}) != len(result):
        raise ValueError(f"Native Light cache properties duplicate: {context}")
    return tuple(result)


def _cache_strings(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(type(item) is not str or not item for item in value):
        raise ValueError(f"Native Light cache source paths invalid: {context}")
    return tuple(value)


def _cache_row(value: object, map_id: str) -> NativeLight:
    if not isinstance(value, dict):
        raise ValueError("Native Light cache row must be an object")
    context = f"map={map_id}, entity={value.get('entity_id')}"
    def required(name: str) -> object:
        if name not in value:
            raise ValueError(f"Native Light cache field missing: {context}, field={name}")
        return value[name]
    def number(name: str) -> float:
        item = required(name)
        if type(item) not in (int, float) or not math.isfinite(float(item)):
            raise ValueError(f"Native Light cache number invalid: {context}, field={name}")
        return float(item)
    def integer(name: str) -> int:
        item = required(name)
        if type(item) is not int:
            raise ValueError(f"Native Light cache integer invalid: {context}, field={name}")
        return item
    def boolean(name: str) -> bool:
        item = required(name)
        if type(item) is not bool:
            raise ValueError(f"Native Light cache boolean invalid: {context}, field={name}")
        return item
    def vector(name: str, length: int) -> tuple[float, ...]:
        item = required(name)
        if not isinstance(item, list) or len(item) != length or any(type(part) not in (int, float) or not math.isfinite(float(part)) for part in item):
            raise ValueError(f"Native Light cache vector invalid: {context}, field={name}")
        return tuple(float(part) for part in item)
    if required("map_id") != map_id or type(required("name")) is not str or not required("name"):
        raise ValueError(f"Native Light cache identity invalid: {context}")
    cookie = required("cookie_path")
    if cookie is not None and (type(cookie) is not str or not cookie):
        raise ValueError(f"Native Light cache Cookie invalid: {context}")
    digest = required("baseline_sha256")
    if type(digest) is not str or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"Native Light cache fingerprint invalid: {context}")
    return NativeLight(
        map_id, integer("entity_id"), required("name"),
        vector("position", 3), vector("euler_zxy", 3), vector("scale", 3),
        integer("light_type"), integer("shape"), vector("color", 4),
        number("intensity"), number("range"), number("spot_angle"),
        number("inner_spot_angle"), number("linear_length"), number("color_temperature"),
        boolean("use_color_temperature"), boolean("enable_obb"), boolean("character_only"),
        number("falloff_exponent"), cookie, integer("component_mask"),
        _cache_properties(required("light_properties"), f"{context}.light_properties"),
        _cache_properties(required("additional_properties"), f"{context}.additional_properties"),
        _cache_strings(required("source_init_paths"), f"{context}.source_init_paths"),
        _cache_strings(required("source_streaming_paths"), f"{context}.source_streaming_paths"), digest,
    )


def read_native_lights_cached(vfs_root: Path, path_hash: Path, map_id: str, cache_dir: Path,
                              check_cancelled: Callable[[], None] | None) -> NativeLightInventory:
    """Reuse an inventory only while its catalog, path index, code and chunk metadata match."""
    validate_scene_id(map_id)
    resolved_cache = cache_dir.resolve()
    if resolved_cache.is_relative_to(vfs_root.resolve()) or resolved_cache.is_relative_to(path_hash.resolve().parent):
        raise ValueError(f"Native Light cache must not write into read-only game or metadata trees: {resolved_cache}")
    fingerprint = _cache_fingerprint(vfs_root, path_hash, map_id)
    path = resolved_cache / f"native_lights_{map_id}_{fingerprint[:20]}.json.gz"
    if path.is_file():
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError, UnicodeDecodeError) as error:
            raise ValueError(f"Native Light cache cannot be decoded: path={path}") from error
        if not isinstance(payload, dict) or payload.get("format") != "NativeLightInventoryCache/1" or payload.get("fingerprint") != fingerprint or payload.get("map_id") != map_id:
            raise ValueError(f"Native Light cache identity differs: path={path}")
        raw_rows, raw_sources, raw_physical = (payload.get("rows"), payload.get("source_sha256"), payload.get("physical_record_count"))
        if not isinstance(raw_rows, list) or not isinstance(raw_sources, list) or type(raw_physical) is not int:
            raise ValueError(f"Native Light cache body invalid: path={path}")
        rows = tuple(_cache_row(value, map_id) for value in raw_rows)
        if len({row.entity_id for row in rows}) != len(rows):
            raise ValueError(f"Native Light cache duplicate entity: path={path}")
        sources: list[tuple[str, str]] = []
        for item in raw_sources:
            if (not isinstance(item, list) or len(item) != 2 or type(item[0]) is not str or
                    type(item[1]) is not str or len(item[1]) != 64 or
                    any(char not in "0123456789abcdef" for char in item[1])):
                raise ValueError(f"Native Light cache source fingerprint invalid: path={path}, item={item!r}")
            sources.append((item[0], item[1]))
        if any(source not in dict(sources) for row in rows for source in row.source_init_paths + row.source_streaming_paths):
            raise ValueError(f"Native Light cache row source missing: path={path}")
        return NativeLightInventory(map_id, rows, tuple(sources), raw_physical)
    inventory = read_native_lights(vfs_root, path_hash, map_id, check_cancelled)
    payload = {"format": "NativeLightInventoryCache/1", "fingerprint": fingerprint,
               "map_id": map_id, "rows": [asdict(row) for row in inventory.rows],
               "source_sha256": inventory.source_sha256,
               "physical_record_count": inventory.physical_record_count}
    resolved_cache.mkdir(parents=True, exist_ok=True)
    encoded = gzip.compress(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    with tempfile.NamedTemporaryFile(dir=resolved_cache, prefix="native_lights_", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
    temporary.replace(path)
    return inventory
