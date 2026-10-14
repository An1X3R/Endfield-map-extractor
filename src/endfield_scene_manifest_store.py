"""Persist independently authenticated JSON sections for lazy scene-manifest reads."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
from types import MappingProxyType
from typing import Mapping, TypedDict, cast


_MAGIC = b"EFMSEC02"
_HEADER = struct.Struct("<8sI32s")
_FORMAT = "EndfieldManifestSections/2"
_MAX_METADATA_BYTES = 1024 * 1024


class ManifestSectionStoreCorruptError(ValueError):
    """A persisted section index or requested payload failed validation."""


@dataclass(frozen=True)
class _Section:
    offset: int
    length: int
    sha256: str


class _SectionMetadata(TypedDict):
    name: str
    offset: int
    length: int
    sha256: str


@dataclass(frozen=True)
class ManifestSections:
    """Read individual sections without opening the original source manifest."""

    path: Path
    keys: tuple[str, ...]
    _payload_offset: int
    _file_bytes: int
    _sections: Mapping[str, _Section]

    def read_section(self, key: str) -> bytes:
        """Return the exact compact JSON bytes after checking their digest."""
        section = self._sections[key]
        with self.path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != self._file_bytes:
                raise ManifestSectionStoreCorruptError(
                    f"Manifest section store size changed: path={self.path}, section={key}"
                )
            stream.seek(self._payload_offset + section.offset)
            payload = stream.read(section.length)
        if len(payload) != section.length or hashlib.sha256(payload).hexdigest() != section.sha256:
            raise ManifestSectionStoreCorruptError(
                f"Manifest section SHA256 differs: path={self.path}, section={key}, bytes={len(payload)}"
            )
        return payload


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ManifestSectionStoreCorruptError(f"Manifest section metadata requires an object: {label}")
    return cast(dict[str, object], value)


def _load(path: Path, source_identity: str) -> ManifestSections:
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        raw_header = stream.read(_HEADER.size)
        if len(raw_header) != _HEADER.size:
            raise ManifestSectionStoreCorruptError(f"Manifest section header is truncated: path={path}, bytes={size}")
        magic, metadata_bytes, metadata_sha256 = _HEADER.unpack(raw_header)
        if magic != _MAGIC or not 0 < metadata_bytes <= _MAX_METADATA_BYTES:
            raise ManifestSectionStoreCorruptError(f"Manifest section header is invalid: path={path}, bytes={size}")
        raw_metadata = stream.read(metadata_bytes)
        if len(raw_metadata) != metadata_bytes:
            raise ManifestSectionStoreCorruptError(f"Manifest section metadata is truncated: path={path}")
        if hashlib.sha256(raw_metadata).digest() != metadata_sha256:
            raise ManifestSectionStoreCorruptError(f"Manifest section metadata SHA256 differs: path={path}")
        try:
            decoded: object = json.loads(raw_metadata)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ManifestSectionStoreCorruptError(f"Manifest section metadata is invalid JSON: path={path}") from error
    metadata = _object(decoded, str(path))
    if metadata.get("format") != _FORMAT or metadata.get("source_identity") != source_identity:
        raise ManifestSectionStoreCorruptError(f"Manifest section source identity differs: path={path}")
    payload_bytes = metadata.get("payload_bytes")
    rows = metadata.get("sections")
    payload_offset = _HEADER.size + metadata_bytes
    if type(payload_bytes) is not int or payload_bytes < 0 or size != payload_offset + payload_bytes:
        raise ManifestSectionStoreCorruptError(f"Manifest section file size is invalid: path={path}, bytes={size}")
    if not isinstance(rows, list):
        raise ManifestSectionStoreCorruptError(f"Manifest section metadata requires sections array: path={path}")
    sections: dict[str, _Section] = {}
    end = 0
    for value in rows:
        row = _object(value, str(path))
        name, offset, length, digest = (row.get(field) for field in ("name", "offset", "length", "sha256"))
        if (not isinstance(name, str) or name in sections or type(offset) is not int or offset != end
                or type(length) is not int or length <= 0 or offset + length > payload_bytes
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ManifestSectionStoreCorruptError(f"Manifest section range or digest is invalid: path={path}, section={name}")
        sections[name] = _Section(offset, length, digest)
        end += length
    if end != payload_bytes:
        raise ManifestSectionStoreCorruptError(f"Manifest section ranges do not cover payload: path={path}, bytes={payload_bytes}")
    return ManifestSections(path, tuple(sections), payload_offset, size, MappingProxyType(sections))


def _write(source: Path, path: Path, source_identity: str) -> None:
    decoded: object = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(decoded, dict):
        raise ValueError(f"Manifest section source requires a JSON object: path={source}")
    data = cast(dict[str, object], decoded)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, payload_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".payload.tmp", dir=path.parent)
    payload_path = Path(payload_name)
    publication_path: Path | None = None
    try:
        rows: list[_SectionMetadata] = []
        offset = 0
        with os.fdopen(descriptor, "wb") as stream:
            for name, value in data.items():
                # Escape lone surrogates while keeping ordinary Unicode compact.
                payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=True).encode(
                    "utf-8", errors="backslashreplace"
                )
                rows.append({"name": name, "offset": offset, "length": len(payload),
                             "sha256": hashlib.sha256(payload).hexdigest()})
                stream.write(payload)
                offset += len(payload)
        metadata = json.dumps({"format": _FORMAT, "source_identity": source_identity,
                               "payload_bytes": offset, "sections": rows}, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False).encode("utf-8", errors="backslashreplace")
        if len(metadata) > _MAX_METADATA_BYTES:
            raise ValueError(f"Manifest section metadata exceeds limit: source={source}, bytes={len(metadata)}")
        publication_descriptor, publication_name = tempfile.mkstemp(
            prefix=f"{path.stem}.", suffix=".tmp", dir=path.parent
        )
        publication_path = Path(publication_name)
        with os.fdopen(publication_descriptor, "wb") as stream:
            stream.write(_HEADER.pack(_MAGIC, len(metadata), hashlib.sha256(metadata).digest()))
            stream.write(metadata)
            with payload_path.open("rb") as payload_stream:
                shutil.copyfileobj(payload_stream, stream, 1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Windows publishes exclusively because readers can prevent replacement.
            if os.name == "nt":
                os.rename(publication_path, path)
            else:
                os.replace(publication_path, path)
        except FileExistsError:
            published = _load(path, source_identity)
            expected = {row["name"]: _Section(row["offset"], row["length"], row["sha256"]) for row in rows}
            if published.keys != tuple(expected) or published._sections != expected:
                raise ManifestSectionStoreCorruptError(f"Concurrent manifest section builders disagree: path={path}")
            for name in published.keys:
                published.read_section(name)
    finally:
        payload_path.unlink(missing_ok=True)
        if publication_path is not None:
            publication_path.unlink(missing_ok=True)


def open_manifest_sections(source: Path, cache_root: Path, source_identity: str) -> ManifestSections:
    """Open a versioned store; callers must verify source stamps around this call."""
    if not isinstance(source_identity, str) or not source_identity:
        raise ValueError("Manifest section source identity requires a nonempty string")
    key = hashlib.sha256(source_identity.encode("utf-8", errors="surrogatepass")).hexdigest()
    path = cache_root / "scene_manifest_sections_v2" / key[:2] / f"{key}.bin"
    if not path.exists():
        _write(source, path, source_identity)
    return _load(path, source_identity)
