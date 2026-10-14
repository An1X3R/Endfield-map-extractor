"""Store compact, content-addressed character expression bake results."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import zlib


CACHE_FORMAT = "EndfieldCharacterExpressionBakeCache/1"
CACHE_DIRECTORY = "character_expression_bakes"
CACHE_MAGIC = b"EFEXPCACHE\x01"
CACHE_MAX_HEADER_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class CachedShapeKey:
    object_name: str
    shape_key_name: str
    relative_key_name: str
    vertex_count: int
    coordinate_block: bytes
    coordinate_sha256: str
    driver_property: str | None
    value: float
    slider_min: float
    slider_max: float
    vertex_group: str
    interpolation: str
    mute: bool


@dataclass(frozen=True)
class CachedController:
    object_name: str
    controls_format: str
    properties: tuple[tuple[str, float], ...]
    display_type: str
    display_size: float


@dataclass(frozen=True)
class CachedExpressionBake:
    receipt: dict[str, object]
    shape_keys: tuple[CachedShapeKey, ...]
    controllers: tuple[CachedController, ...]


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"Expression cache requires an object: {context}")
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"Expression cache requires an array: {context}")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"Expression cache requires nonempty text: {context}")
    return value


def _text_or_empty(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Expression cache requires text: {context}")
    return value


def _integer(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"Expression cache requires a nonnegative integer: {context}")
    return value


def _number(value: object, context: str) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"Expression cache requires a finite number: {context}")
    number = float(value)
    if not -float("inf") < number < float("inf"):
        raise ValueError(f"Expression cache requires a finite number: {context}")
    return number


def _digest(value: object, context: str) -> str:
    digest = _text(value, context)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"Expression cache requires a lowercase SHA-256 digest: {context}")
    return digest


def _cache_path(cache_root: Path, cache_key: str) -> Path:
    _digest(cache_key, "cache key")
    return cache_root / CACHE_DIRECTORY / f"{cache_key}.efc"


def expression_cache_key(inputs: dict[str, object]) -> str:
    """Hash a fully specified, animation-independent expression input record."""
    return hashlib.sha256(_canonical_json(inputs)).hexdigest()


def _header_and_blocks(cache_key: str, entry: CachedExpressionBake) -> tuple[bytes, tuple[bytes, ...]]:
    _digest(cache_key, "cache key")
    _object(entry.receipt, "receipt")
    shape_rows: list[dict[str, object]] = []
    coordinate_blocks: list[bytes] = []
    for index, shape in enumerate(entry.shape_keys):
        if shape.vertex_count < 0:
            raise ValueError(f"Expression cache vertex count is negative: {shape.object_name}/{shape.shape_key_name}")
        expected_size = shape.vertex_count * 12
        if len(shape.coordinate_block) == 0 and expected_size != 0:
            raise ValueError(f"Expression cache coordinate block is empty: {shape.object_name}/{shape.shape_key_name}")
        try:
            raw = zlib.decompress(shape.coordinate_block)
        except zlib.error as error:
            raise ValueError(f"Expression cache coordinate block is invalid: item={index}, error={error}") from error
        if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != shape.coordinate_sha256:
            raise ValueError(f"Expression cache coordinate block failed its size or digest check: {shape.object_name}/{shape.shape_key_name}")
        row = {
            "object_name": shape.object_name,
            "shape_key_name": shape.shape_key_name,
            "relative_key_name": shape.relative_key_name,
            "vertex_count": shape.vertex_count,
            "coordinate_size": len(shape.coordinate_block),
            "coordinate_sha256": shape.coordinate_sha256,
            "coordinate_block_sha256": hashlib.sha256(shape.coordinate_block).hexdigest(),
            "driver_property": shape.driver_property,
            "value": shape.value,
            "slider_min": shape.slider_min,
            "slider_max": shape.slider_max,
            "vertex_group": shape.vertex_group,
            "interpolation": shape.interpolation,
            "mute": shape.mute,
        }
        shape_rows.append(row)
        coordinate_blocks.append(shape.coordinate_block)
    controller_rows = []
    for controller in entry.controllers:
        controller_rows.append({
            "object_name": controller.object_name,
            "controls_format": controller.controls_format,
            "properties": [[name, value] for name, value in controller.properties],
            "display_type": controller.display_type,
            "display_size": controller.display_size,
        })
    header = {
        "format": CACHE_FORMAT,
        "cache_key": cache_key,
        "receipt": entry.receipt,
        "shape_keys": shape_rows,
        "controllers": controller_rows,
    }
    return _canonical_json(header), tuple(coordinate_blocks)


def encode_expression_cache(cache_key: str, entry: CachedExpressionBake) -> bytes:
    """Encode one cache entry with integrity checks for metadata and coordinate blocks."""
    header, blocks = _header_and_blocks(cache_key, entry)
    if len(header) > CACHE_MAX_HEADER_BYTES:
        raise ValueError(f"Expression cache metadata exceeds the supported size: {len(header)} bytes")
    return CACHE_MAGIC + hashlib.sha256(header).digest() + struct.pack("<I", len(header)) + header + b"".join(blocks)


def _decode_controller(value: object, context: str) -> CachedController:
    row = _object(value, context)
    properties: list[tuple[str, float]] = []
    for index, raw in enumerate(_array(row.get("properties"), f"{context}.properties")):
        property_row = _array(raw, f"{context}.properties[{index}]")
        if len(property_row) != 2:
            raise ValueError(f"Expression cache controller property needs a name and value: {context}[{index}]")
        properties.append((_text(property_row[0], f"{context}.property_name"),
                           _number(property_row[1], f"{context}.property_value")))
    if len({name for name, _ in properties}) != len(properties):
        raise ValueError(f"Expression cache controller has duplicate properties: {context}")
    return CachedController(
        _text(row.get("object_name"), f"{context}.object_name"),
        _text(row.get("controls_format"), f"{context}.controls_format"),
        tuple(properties),
        _text(row.get("display_type"), f"{context}.display_type"),
        _number(row.get("display_size"), f"{context}.display_size"),
    )


def decode_expression_cache(payload: bytes, expected_key: str) -> CachedExpressionBake:
    """Decode and verify a cache entry before Blender scene mutation begins."""
    _digest(expected_key, "expected cache key")
    prefix_size = len(CACHE_MAGIC) + 32 + 4
    if len(payload) < prefix_size or payload[:len(CACHE_MAGIC)] != CACHE_MAGIC:
        raise ValueError(f"Expression cache header is missing or unsupported: key={expected_key}")
    cursor = len(CACHE_MAGIC)
    expected_header_sha = payload[cursor:cursor + 32]
    cursor += 32
    header_size = struct.unpack_from("<I", payload, cursor)[0]
    cursor += 4
    if header_size == 0 or header_size > CACHE_MAX_HEADER_BYTES or cursor + header_size > len(payload):
        raise ValueError(f"Expression cache metadata length is invalid: key={expected_key}, size={header_size}")
    header_bytes = payload[cursor:cursor + header_size]
    cursor += header_size
    if hashlib.sha256(header_bytes).digest() != expected_header_sha:
        raise ValueError(f"Expression cache metadata digest mismatch: key={expected_key}")
    try:
        header = _object(json.loads(header_bytes.decode("utf-8")), "header")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Expression cache metadata is unreadable: key={expected_key}, error={error}") from error
    if header.get("format") != CACHE_FORMAT or header.get("cache_key") != expected_key:
        raise ValueError(f"Expression cache identity mismatch: key={expected_key}")
    receipt = _object(header.get("receipt"), "receipt")
    shapes: list[CachedShapeKey] = []
    for index, raw in enumerate(_array(header.get("shape_keys"), "shape_keys")):
        context = f"shape_keys[{index}]"
        row = _object(raw, context)
        object_name = _text(row.get("object_name"), f"{context}.object_name")
        shape_name = _text(row.get("shape_key_name"), f"{context}.shape_key_name")
        vertex_count = _integer(row.get("vertex_count"), f"{context}.vertex_count")
        coordinate_size = _integer(row.get("coordinate_size"), f"{context}.coordinate_size")
        raw_digest = _digest(row.get("coordinate_sha256"), f"{context}.coordinate_sha256")
        block_digest = _digest(row.get("coordinate_block_sha256"), f"{context}.coordinate_block_sha256")
        end = cursor + coordinate_size
        if end > len(payload):
            raise ValueError(f"Expression cache coordinate block is truncated: {object_name}/{shape_name}")
        block = payload[cursor:end]
        cursor = end
        if hashlib.sha256(block).hexdigest() != block_digest:
            raise ValueError(f"Expression cache compressed coordinate digest mismatch: {object_name}/{shape_name}")
        try:
            coordinates = zlib.decompress(block)
        except zlib.error as error:
            raise ValueError(f"Expression cache coordinate block cannot be decompressed: {object_name}/{shape_name}, error={error}") from error
        if len(coordinates) != vertex_count * 12 or hashlib.sha256(coordinates).hexdigest() != raw_digest:
            raise ValueError(f"Expression cache coordinate data failed validation: {object_name}/{shape_name}")
        driver_property = row.get("driver_property")
        if driver_property is not None and not isinstance(driver_property, str):
            raise TypeError(f"Expression cache driver property must be text or null: {context}")
        mute = row.get("mute")
        if type(mute) is not bool:
            raise TypeError(f"Expression cache mute value must be boolean: {context}")
        shapes.append(CachedShapeKey(
            object_name, shape_name,
            _text(row.get("relative_key_name"), f"{context}.relative_key_name"),
            vertex_count, block, raw_digest, driver_property,
            _number(row.get("value"), f"{context}.value"),
            _number(row.get("slider_min"), f"{context}.slider_min"),
            _number(row.get("slider_max"), f"{context}.slider_max"),
            _text_or_empty(row.get("vertex_group"), f"{context}.vertex_group"),
            _text(row.get("interpolation"), f"{context}.interpolation"), mute,
        ))
    controllers = tuple(_decode_controller(value, f"controllers[{index}]") for index, value in enumerate(
        _array(header.get("controllers"), "controllers")))
    if cursor != len(payload):
        raise ValueError(f"Expression cache has trailing data: key={expected_key}, bytes={len(payload) - cursor}")
    if len({(shape.object_name, shape.shape_key_name) for shape in shapes}) != len(shapes):
        raise ValueError(f"Expression cache has duplicate shape-key records: key={expected_key}")
    if len({controller.object_name for controller in controllers}) != len(controllers):
        raise ValueError(f"Expression cache has duplicate controller records: key={expected_key}")
    return CachedExpressionBake(receipt, tuple(shapes), controllers)


def _same_expression_result(left: CachedExpressionBake, right: CachedExpressionBake) -> bool:
    left_receipt = json.loads(_canonical_json(left.receipt).decode("utf-8"))
    right_receipt = json.loads(_canonical_json(right.receipt).decode("utf-8"))
    for receipt in (left_receipt, right_receipt):
        for field in ("source_table", "source_document", "source_audit"):
            source = receipt.get(field)
            if isinstance(source, dict):
                for metadata in ("file", "sha256", "bytes"):
                    source.pop(metadata, None)
    return (left.shape_keys == right.shape_keys
            and left.controllers == right.controllers
            and _canonical_json(left_receipt) == _canonical_json(right_receipt))


def read_expression_cache(cache_root: Path, cache_key: str) -> CachedExpressionBake | None:
    """Read a cache entry, distinguishing a miss from a damaged existing file."""
    path = _cache_path(cache_root, cache_key)
    if not path.exists():
        return None
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise OSError(f"Unable to read expression cache file {path}: {error}") from error
    try:
        return decode_expression_cache(payload, cache_key)
    except (TypeError, ValueError, struct.error) as error:
        raise ValueError(f"Expression cache is corrupt and was left untouched: path={path}, reason={error}") from error


def write_expression_cache(cache_root: Path, cache_key: str, entry: CachedExpressionBake) -> None:
    """Publish one complete cache entry atomically without mutating scene inputs."""
    payload = encode_expression_cache(cache_key, entry)
    path = _cache_path(cache_root, cache_key)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        if path.exists():
            existing = path.read_bytes()
            existing_entry = decode_expression_cache(existing, cache_key)
            if existing != payload and not _same_expression_result(existing_entry, entry):
                raise FileExistsError(f"Refusing to replace an existing expression cache entry: {path}")
            return
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{cache_key}.", suffix=".tmp",
                                         dir=directory, delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            existing = path.read_bytes()
            existing_entry = decode_expression_cache(existing, cache_key)
            if existing != payload and not _same_expression_result(existing_entry, entry):
                raise FileExistsError(f"Expression cache key appeared while writing with different content: {path}")
            return
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
