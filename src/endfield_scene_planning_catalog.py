"""Load globally validated material metadata on demand for bounded scene planning."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import MappingProxyType
from typing import TypedDict, cast

from endfield_scene_io import SceneInputChangedError, _file_stamp, _freeze_cached_value
from endfield_scene_material_contract import (
    MaterialRecord, _build_catalog, reference_key, resolve_export_path, validate_material,
)
from endfield_scene_mesh_uv_contract import required_uv_channels


AssetKey = tuple[str, int]
_FORMAT = "EndfieldPlanningCatalog/1"
_MAX_METADATA_BYTES = 1024 * 1024


class PlanningCatalogCorruptError(ValueError):
    """A persisted catalog identity, key index or selected row is invalid."""


class _Metadata(TypedDict):
    format: str
    identity: str
    material_count: int
    material_keys_sha256: str
    texture_count: int
    texture_keys_sha256: str
    requires_extra_uv: bool


@dataclass(frozen=True)
class PlanningCatalog:
    materials: Mapping[AssetKey, MaterialRecord]
    textures: Mapping[AssetKey, tuple[Path, str]]
    requires_extra_uv: bool


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=True).encode("ascii")


def _reference(key: AssetKey) -> str:
    return _json_bytes(key).decode("ascii")


def _key(reference: str, path: Path) -> AssetKey:
    decoded: object = json.loads(reference)
    if (not isinstance(decoded, list) or len(decoded) != 2 or not isinstance(decoded[0], str)
            or not decoded[0] or type(decoded[1]) is not int):
        raise PlanningCatalogCorruptError(f"Planning catalog AssetRef is invalid: path={path}, reference={reference}")
    key = cast(AssetKey, (decoded[0], decoded[1]))
    if key[0] != key[0].casefold() or _reference(key) != reference:
        raise PlanningCatalogCorruptError(f"Planning catalog AssetRef is not canonical: path={path}, reference={reference}")
    return key


def _keys_digest(references: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for reference in references:
        digest.update(reference.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _row_digest(reference: str, payload: bytes) -> str:
    return hashlib.sha256(reference.encode("ascii") + b"\0" + payload).hexdigest()


def _connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)


def _read_row(path: Path, table: str, key: AssetKey) -> bytes:
    reference = _reference(key)
    try:
        with closing(_connection(path)) as database:
            row = database.execute(f"SELECT payload,sha256 FROM {table} WHERE reference=?", (reference,)).fetchone()
    except sqlite3.DatabaseError as error:
        raise PlanningCatalogCorruptError(f"Planning catalog row cannot be read: path={path}, table={table}, reference={reference}") from error
    if row is None or not isinstance(row[0], bytes) or not isinstance(row[1], str):
        raise PlanningCatalogCorruptError(f"Planning catalog row is missing or invalid: path={path}, table={table}, reference={reference}")
    payload, digest = cast(tuple[bytes, str], row)
    if _row_digest(reference, payload) != digest:
        raise PlanningCatalogCorruptError(f"Planning catalog row SHA256 differs: path={path}, table={table}, reference={reference}")
    return payload


@dataclass(frozen=True, eq=False)
class _Materials(Mapping[AssetKey, MaterialRecord]):
    path: Path
    references: tuple[AssetKey, ...]
    key_set: frozenset[AssetKey]
    _records: dict[AssetKey, MaterialRecord] = field(init=False, default_factory=dict, repr=False)

    def __getitem__(self, key: AssetKey) -> MaterialRecord:
        if key not in self.key_set:
            raise KeyError(key)
        if key not in self._records:
            payload = _read_row(self.path, "materials", key)
            record = validate_material(json.loads(payload))
            if reference_key(record) != key:
                raise PlanningCatalogCorruptError(f"Planning material identity differs: path={self.path}, reference={key}")
            self._records[key] = _freeze_cached_value(record)
        return self._records[key]

    def __contains__(self, key: object) -> bool:
        return key in self.key_set

    def __iter__(self) -> Iterator[AssetKey]:
        return iter(self.references)

    def __len__(self) -> int:
        return len(self.references)


@dataclass(frozen=True, eq=False)
class _Textures(Mapping[AssetKey, tuple[Path, str]]):
    path: Path
    references: tuple[AssetKey, ...]
    key_set: frozenset[AssetKey]
    _candidates: dict[AssetKey, tuple[tuple[Path, str], ...]] = field(init=False, default_factory=dict, repr=False)

    def __getitem__(self, key: AssetKey) -> tuple[Path, str]:
        if key not in self.key_set:
            raise KeyError(key)
        if key not in self._candidates:
            decoded: object = json.loads(_read_row(self.path, "textures", key))
            if not isinstance(decoded, list) or not decoded:
                raise PlanningCatalogCorruptError(f"Planning texture candidates require a nonempty array: path={self.path}, reference={key}")
            candidates: list[tuple[Path, str]] = []
            for row in decoded:
                if (not isinstance(row, list) or len(row) != 2
                        or any(not isinstance(value, str) or not value for value in row)):
                    raise PlanningCatalogCorruptError(f"Planning texture candidate is invalid: path={self.path}, reference={key}")
                candidates.append((Path(row[0]), row[1]))
            self._candidates[key] = tuple(candidates)
        for directory, filename in self._candidates[key]:
            resolved = resolve_export_path(directory / filename)
            if resolved.is_file():
                return resolved.parent, resolved.name
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        if key not in self.key_set:
            return False
        try:
            self[cast(AssetKey, key)]
        except KeyError:
            return False
        return True

    def __iter__(self) -> Iterator[AssetKey]:
        return (key for key in self.references if key in self)

    def __len__(self) -> int:
        return sum(1 for _ in self)


def _identity(paths: tuple[Path, ...]) -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("endfield_scene_material_contract.py", "endfield_scene_mesh_uv_contract.py",
                 "endfield_shader_contract.py", "endfield_scene_planning_catalog.py"):
        digest.update(name.encode("ascii"))
        digest.update((root / name).read_bytes())
    return _json_bytes({"format": _FORMAT, "parser": digest.hexdigest(),
                        "sources": [{"path": str(path), "stamp": asdict(_file_stamp(path))} for path in paths]}).decode("ascii")


def _load(path: Path, identity: str) -> PlanningCatalog:
    try:
        with closing(_connection(path)) as database:
            page_count = database.execute("PRAGMA page_count").fetchone()[0]
            page_size = database.execute("PRAGMA page_size").fetchone()[0]
            if path.stat().st_size != page_count * page_size:
                raise PlanningCatalogCorruptError(f"Planning catalog file size differs from SQLite layout: path={path}")
            metadata_row = database.execute("SELECT payload,sha256 FROM metadata WHERE id=1").fetchone()
            material_refs = tuple(row[0] for row in database.execute("SELECT reference FROM materials ORDER BY reference"))
            texture_refs = tuple(row[0] for row in database.execute("SELECT reference FROM textures ORDER BY reference"))
    except sqlite3.DatabaseError as error:
        raise PlanningCatalogCorruptError(f"Planning catalog index cannot be read: path={path}") from error
    if (metadata_row is None or not isinstance(metadata_row[0], bytes) or not isinstance(metadata_row[1], str)
            or len(metadata_row[0]) > _MAX_METADATA_BYTES
            or hashlib.sha256(metadata_row[0]).hexdigest() != metadata_row[1]):
        raise PlanningCatalogCorruptError(f"Planning catalog metadata SHA256 differs: path={path}")
    decoded: object = json.loads(metadata_row[0])
    if not isinstance(decoded, dict):
        raise PlanningCatalogCorruptError(f"Planning catalog metadata requires an object: path={path}")
    metadata = cast(dict[str, object], decoded)
    if (metadata.get("format") != _FORMAT or metadata.get("identity") != identity
            or type(metadata.get("requires_extra_uv")) is not bool):
        raise PlanningCatalogCorruptError(f"Planning catalog source identity or UV summary differs: path={path}")
    keys: list[tuple[AssetKey, ...]] = []
    for table, references in (("material", material_refs), ("texture", texture_refs)):
        if (any(not isinstance(reference, str) or not reference.isascii() for reference in references)
                or type(metadata.get(f"{table}_count")) is not int
                or metadata[f"{table}_count"] != len(references)
                or metadata.get(f"{table}_keys_sha256") != _keys_digest(references)):
            raise PlanningCatalogCorruptError(f"Planning catalog key index differs: path={path}, table={table}")
        keys.append(tuple(_key(reference, path) for reference in references))
    materials, textures = keys
    return PlanningCatalog(_Materials(path, materials, frozenset(materials)),
                           _Textures(path, textures, frozenset(textures)), cast(bool, metadata["requires_extra_uv"]))


def _write(paths: tuple[Path, ...], path: Path, identity: str) -> None:
    materials, textures = _build_catalog(paths)
    requires_extra_uv = any(required_uv_channels(record) for record in materials.values())
    material_refs = tuple(sorted(_reference(key) for key in materials))
    texture_refs = tuple(sorted(_reference(key) for key in textures))
    metadata: _Metadata = {"format": _FORMAT, "identity": identity,
                           "material_count": len(material_refs), "material_keys_sha256": _keys_digest(material_refs),
                           "texture_count": len(texture_refs), "texture_keys_sha256": _keys_digest(texture_refs),
                           "requires_extra_uv": requires_extra_uv}
    payload = _json_bytes(metadata)
    if len(payload) > _MAX_METADATA_BYTES:
        raise ValueError(f"Planning catalog metadata exceeds limit: path={path}, bytes={len(payload)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with closing(sqlite3.connect(temporary)) as database:
            database.executescript("CREATE TABLE metadata(id INTEGER PRIMARY KEY,payload BLOB NOT NULL,sha256 TEXT NOT NULL);"
                                   "CREATE TABLE materials(reference TEXT PRIMARY KEY,payload BLOB NOT NULL,sha256 TEXT NOT NULL);"
                                   "CREATE TABLE textures(reference TEXT PRIMARY KEY,payload BLOB NOT NULL,sha256 TEXT NOT NULL);")
            database.execute("INSERT INTO metadata VALUES(1,?,?)", (payload, hashlib.sha256(payload).hexdigest()))
            for key, record in materials.items():
                reference, row = _reference(key), _json_bytes(record)
                database.execute("INSERT INTO materials VALUES(?,?,?)", (reference, row, _row_digest(reference, row)))
            for key, candidates in textures.items():
                reference, row = _reference(key), _json_bytes(tuple((str(folder), filename) for folder, filename in candidates))
                database.execute("INSERT INTO textures VALUES(?,?,?)", (reference, row, _row_digest(reference, row)))
            database.commit()
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        if _identity(paths) != identity:
            raise SceneInputChangedError(f"Planning catalog sources changed during creation: paths={paths}")
        try:
            if os.name == "nt":
                os.rename(temporary, path)
            else:
                os.replace(temporary, path)
        except FileExistsError:
            published = _load(path, identity)
            if (published.requires_extra_uv != requires_extra_uv
                    or tuple(_reference(key) for key in published.materials) != material_refs
                    or cast(_Textures, published.textures).references != tuple(_key(reference, path) for reference in texture_refs)):
                raise PlanningCatalogCorruptError(f"Concurrent planning catalog builders disagree: path={path}")
    finally:
        temporary.unlink(missing_ok=True)


def load_planning_catalog(paths: tuple[Path, ...], cache_root: Path) -> PlanningCatalog:
    """Read validated keys and decode only records requested during this operation."""
    if not paths:
        return PlanningCatalog(MappingProxyType({}), MappingProxyType({}), False)
    resolved = tuple(resolve_export_path(path) for path in paths)
    identity = _identity(resolved)
    key = hashlib.sha256(identity.encode("ascii")).hexdigest()
    path = cache_root.expanduser().resolve() / "scene_planning_catalog_v1" / key[:2] / f"{key}.sqlite"
    if not path.exists():
        _write(resolved, path, identity)
    catalog = _load(path, identity)
    if _identity(resolved) != identity:
        raise SceneInputChangedError(f"Planning catalog sources changed during load: paths={resolved}")
    return catalog
