"""Persist immutable character source indexes under an explicitly owned cache root."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import TypedDict
from uuid import uuid4

from extractor_core.resources import BundleClosureMetadata


class CharacterCacheError(RuntimeError):
    """A character cache entry is malformed or cannot be written safely."""


class CharacterSourceChangedError(OSError):
    """A character source changed while it was being read or cached."""


CheckCancelled = Callable[[], None]
JsonBuilder = Callable[[], object]


@dataclass(frozen=True)
class CharacterSourceStamp:
    path: str
    device: int
    inode: int
    bytes: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class CharacterSourceFingerprint:
    stamp: CharacterSourceStamp
    sha256: str


@dataclass(frozen=True)
class CharacterCabIndex:
    bundle_serialized: tuple[tuple[str, tuple[str, ...]], ...]
    bundle_externals: tuple[tuple[str, tuple[str, ...]], ...]
    providers: tuple[tuple[str, tuple[str, ...]], ...]


class _ClosureCounts(TypedDict):
    rootBundleCount: int
    closureBundleCount: int
    unresolvedExternalCount: int
    unresolvedExternalSample: list[str]


def source_stamp(path: Path) -> CharacterSourceStamp:
    """Return the canonical path and all stable filesystem identity fields."""
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Character cache source file is missing: path={source}")
    stat = source.stat()
    return CharacterSourceStamp(
        str(source), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
    )


def _stamp_from_record(value: object, context: str) -> CharacterSourceStamp:
    if not isinstance(value, dict):
        raise CharacterCacheError(f"Character cache source stamp must be an object: field={context}")
    path = value.get("path")
    integers = tuple(value.get(key) for key in ("device", "inode", "bytes", "modified_ns", "changed_ns"))
    if not isinstance(path, str) or not path or any(type(item) is not int for item in integers):
        raise CharacterCacheError(f"Character cache source stamp is invalid: field={context}")
    return CharacterSourceStamp(path, integers[0], integers[1], integers[2], integers[3], integers[4])


def _json_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CharacterCacheError(f"Character cache record must be an object: field={context}")
    return value


def _read_json(path: Path, context: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CharacterCacheError(f"Cannot read character cache record: context={context}, path={path}, error={error}") from error
    return _json_object(value, context)


def _write_json_atomic(path: Path, value: object, context: str) -> None:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise CharacterCacheError(f"Character cache value is not JSON serializable: context={context}, error={error}") from error
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _identity_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _normalized_file_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _cab_index_from_record(value: object) -> CharacterCabIndex:
    row = _json_object(value, "CAB index")
    bundles_value = row.get("bundles")
    providers_value = row.get("providers")
    if not isinstance(bundles_value, list) or not isinstance(providers_value, list):
        raise CharacterCacheError("Character CAB cache requires bundle and provider arrays")
    serialized_rows: list[tuple[str, tuple[str, ...]]] = []
    external_rows: list[tuple[str, tuple[str, ...]]] = []
    for position, value_row in enumerate(bundles_value):
        bundle = _json_object(value_row, f"CAB index bundles[{position}]")
        name, serialized, externals = bundle.get("logical_name"), bundle.get("serialized"), bundle.get("externals")
        if (not isinstance(name, str) or not name or not isinstance(serialized, list)
                or not isinstance(externals, list)
                or any(not isinstance(item, str) for item in (*serialized, *externals))):
            raise CharacterCacheError(f"Character CAB cache bundle is malformed: index={position}")
        serialized_rows.append((name, tuple(serialized)))
        external_rows.append((name, tuple(externals)))
    provider_rows: list[tuple[str, tuple[str, ...]]] = []
    for position, value_row in enumerate(providers_value):
        provider = _json_object(value_row, f"CAB index providers[{position}]")
        name, logical_names = provider.get("file_name"), provider.get("logical_names")
        if (not isinstance(name, str) or not name or not isinstance(logical_names, list)
                or any(not isinstance(item, str) for item in logical_names)):
            raise CharacterCacheError(f"Character CAB cache provider is malformed: index={position}")
        provider_rows.append((name, tuple(logical_names)))
    return CharacterCabIndex(tuple(serialized_rows), tuple(external_rows), tuple(provider_rows))


def _parse_character_cab_index(path: Path, check_cancelled: CheckCancelled) -> object:
    bundle_serialized: dict[str, set[str]] = {}
    bundle_externals: dict[str, set[str]] = {}
    providers: dict[str, set[str]] = {}
    try:
        stream = path.open("r", encoding="utf-8-sig")
    except OSError as error:
        raise OSError(f"Cannot open character CAB inventory: path={path}: {error}") from error
    with stream:
        for line_number, line in enumerate(stream, 1):
            if line_number % 1024 == 0:
                check_cancelled()
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise CharacterCacheError(
                    f"Character CAB inventory is invalid JSONL: path={path}, line={line_number}, error={error}"
                ) from error
            if not isinstance(row, dict):
                raise CharacterCacheError(f"Character CAB record must be an object: path={path}, line={line_number}")
            logical_name = row.get("logical_name")
            if row.get("kind") != "cab" or not isinstance(logical_name, str):
                continue
            normalized_logical_name = logical_name.casefold()
            serialized_names = bundle_serialized.setdefault(normalized_logical_name, set())
            external_names = bundle_externals.setdefault(normalized_logical_name, set())
            serialized_value = row.get("serialized") or []
            if not isinstance(serialized_value, list):
                raise CharacterCacheError(
                    f"Character CAB serialized entries must be an array: path={path}, line={line_number}"
                )
            for serialized in serialized_value:
                if not isinstance(serialized, dict):
                    continue
                name = serialized.get("name")
                if isinstance(name, str) and name:
                    normalized = _normalized_file_name(name)
                    serialized_names.add(normalized)
                    providers.setdefault(normalized, set()).add(normalized_logical_name)
                raw_externals = serialized.get("externals") or []
                if not isinstance(raw_externals, list):
                    raise CharacterCacheError(
                        f"Character CAB externals must be an array: path={path}, line={line_number}"
                    )
                for external in raw_externals:
                    if isinstance(external, str) and external:
                        external_names.add(_normalized_file_name(external))
    check_cancelled()
    return {
        "bundles": [
            {"logical_name": name, "serialized": sorted(bundle_serialized[name]),
             "externals": sorted(bundle_externals[name])}
            for name in sorted(bundle_serialized)
        ],
        "providers": [
            {"file_name": name, "logical_names": sorted(providers[name])}
            for name in sorted(providers)
        ],
    }


class CharacterDataCache:
    """Share pure JSON indexes across explicit callers and separate worker processes."""

    def __init__(self, cache_root: Path) -> None:
        self.cache_root = cache_root.expanduser().resolve()

    def lookup_fingerprint(
        self, path: Path, schema: str, check_cancelled: CheckCancelled,
    ) -> CharacterSourceFingerprint | None:
        source = path.expanduser().resolve()
        stamp = source_stamp(source)
        check_cancelled()
        cache_path = self._fingerprint_path(source, schema)
        if not cache_path.is_file():
            return None
        record = _read_json(cache_path, "source fingerprint")
        if record.get("format") != "EndfieldCharacterSourceFingerprint/1" or record.get("schema") != schema:
            raise CharacterCacheError(f"Unsupported character source fingerprint record: path={cache_path}")
        recorded_stamp = _stamp_from_record(record.get("source"), "source fingerprint.source")
        if recorded_stamp != stamp:
            return None
        digest = record.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise CharacterCacheError(f"Character source fingerprint has invalid SHA256: path={cache_path}")
        if source_stamp(source) != stamp:
            raise CharacterSourceChangedError(f"Character input changed during cache lookup: path={source}")
        return CharacterSourceFingerprint(stamp, digest)

    def fingerprint_file(
        self, path: Path, schema: str, check_cancelled: CheckCancelled,
    ) -> CharacterSourceFingerprint:
        source = path.expanduser().resolve()
        cached = self.lookup_fingerprint(source, schema, check_cancelled)
        if cached is not None:
            return cached
        before = source_stamp(source)
        digest = hashlib.sha256()
        total = 0
        try:
            with source.open("rb") as stream:
                while chunk := stream.read(4 * 1024 * 1024):
                    check_cancelled()
                    digest.update(chunk)
                    total += len(chunk)
        except OSError as error:
            raise OSError(f"Cannot hash character source: path={source}: {error}") from error
        after = source_stamp(source)
        if before != after or total != after.bytes:
            raise CharacterSourceChangedError(f"Character input changed while hashing: path={source}")
        result = CharacterSourceFingerprint(before, digest.hexdigest())
        self.remember_fingerprint(source, schema, result)
        return result

    def remember_fingerprint(
        self, path: Path, schema: str, fingerprint: CharacterSourceFingerprint,
    ) -> None:
        source = path.expanduser().resolve()
        current = source_stamp(source)
        if current != fingerprint.stamp or current.path != str(source):
            raise CharacterSourceChangedError(f"Character input changed before fingerprint cache write: path={source}")
        record = {
            "format": "EndfieldCharacterSourceFingerprint/1",
            "schema": schema,
            "source": asdict(fingerprint.stamp),
            "sha256": fingerprint.sha256,
        }
        _write_json_atomic(self._fingerprint_path(source, schema), record, "source fingerprint")

    def get_or_build_json(
        self,
        namespace: str,
        schema: str,
        source_paths: tuple[Path, ...],
        qualifiers: tuple[str, ...],
        builder: JsonBuilder,
        check_cancelled: CheckCancelled,
    ) -> tuple[object, bool]:
        if not namespace or re.fullmatch(r"[a-zA-Z0-9_-]+", namespace) is None:
            raise ValueError(f"Invalid character cache namespace: value={namespace!r}")
        if not schema or any(not isinstance(value, str) for value in qualifiers):
            raise ValueError(f"Invalid character cache identity: namespace={namespace}, schema={schema!r}")
        if not source_paths:
            raise ValueError(f"Character cache entry requires source files: namespace={namespace}")
        sources = tuple(source_stamp(path) for path in source_paths)
        identity = {
            "format": "EndfieldCharacterDataCacheEntry/1",
            "namespace": namespace,
            "schema": schema,
            "sources": [asdict(stamp) for stamp in sources],
            "qualifiers": list(qualifiers),
        }
        key = hashlib.sha256(_identity_bytes(identity)).hexdigest()
        cache_path = self.cache_root / "entries" / namespace / f"{key}.json"
        check_cancelled()
        if cache_path.is_file():
            record = _read_json(cache_path, f"{namespace} cache entry")
            if record.get("identity") != identity or "value" not in record:
                raise CharacterCacheError(f"Character cache identity mismatch: namespace={namespace}, path={cache_path}")
            if tuple(source_stamp(path) for path in source_paths) != sources:
                raise CharacterSourceChangedError(f"Character inputs changed during cache read: namespace={namespace}")
            check_cancelled()
            return record["value"], True
        check_cancelled()
        value = builder()
        check_cancelled()
        after = tuple(source_stamp(path) for path in source_paths)
        if after != sources:
            raise CharacterSourceChangedError(
                f"Character inputs changed during derived read: namespace={namespace}, sources={[stamp.path for stamp in sources]}"
            )
        try:
            normalized_value = json.loads(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError) as error:
            raise CharacterCacheError(
                f"Character cache value is not JSON serializable: namespace={namespace}, error={error}"
            ) from error
        _write_json_atomic(
            cache_path,
            {"identity": identity, "value": normalized_value},
            f"{namespace} cache entry",
        )
        return normalized_value, False

    def read_character_cab_index(
        self, cab_inventory: Path, check_cancelled: CheckCancelled,
    ) -> tuple[CharacterCabIndex, CharacterSourceFingerprint]:
        source = cab_inventory.expanduser().resolve()
        fingerprint = self.fingerprint_file(source, "character-cab-jsonl/1", check_cancelled)
        if source_stamp(source) != fingerprint.stamp:
            raise CharacterSourceChangedError(f"Character CAB changed before index read: path={source}")
        raw, _cache_hit = self.get_or_build_json(
            "character_cab_index", "character-cab-index/1", (source,), (fingerprint.sha256,),
            lambda: _parse_character_cab_index(source, check_cancelled), check_cancelled,
        )
        if source_stamp(source) != fingerprint.stamp:
            raise CharacterSourceChangedError(f"Character CAB changed after index read: path={source}")
        return _cab_index_from_record(raw), fingerprint

    def _fingerprint_path(self, source: Path, schema: str) -> Path:
        key = hashlib.sha256(_identity_bytes((str(source), schema))).hexdigest()
        return self.cache_root / "fingerprints" / f"{key}.json"


def character_bundle_closure(
    index: CharacterCabIndex, roots: dict[str, list[str]],
) -> tuple[dict[str, list[str]], BundleClosureMetadata]:
    """Resolve roots from a cached immutable CAB index with bootstrap-compatible semantics."""
    bundle_serialized = dict(index.bundle_serialized)
    bundle_externals = dict(index.bundle_externals)
    providers = dict(index.providers)
    closures: dict[str, list[str]] = {}
    unresolved_by_map: dict[str, list[str]] = {}
    for map_id, root_names in roots.items():
        missing_roots = sorted(set(root_names) - bundle_serialized.keys())
        if missing_roots:
            raise CharacterCacheError(f"Bundle roots are absent from CAB inventory: group={map_id}, roots={missing_roots}")
        selected = set(root_names)
        queue = list(root_names)
        unresolved: set[str] = set()
        while queue:
            logical_name = queue.pop()
            for external in bundle_externals.get(logical_name, ()):
                external_providers = providers.get(external)
                if not external_providers:
                    unresolved.add(external)
                    continue
                for provider in external_providers:
                    if provider not in selected:
                        selected.add(provider)
                        queue.append(provider)
        closures[map_id] = sorted(selected)
        unresolved_by_map[map_id] = sorted(unresolved)
    metadata: BundleClosureMetadata = {
        "format": "EndfieldAutomaticBundleClosure/1",
        "providerSerializedFileCount": len(providers),
        "bundleInventoryCount": len(bundle_serialized),
        "maps": {
            map_id: _ClosureCounts(
                rootBundleCount=len(roots[map_id]),
                closureBundleCount=len(closures[map_id]),
                unresolvedExternalCount=len(unresolved_by_map[map_id]),
                unresolvedExternalSample=unresolved_by_map[map_id][:32],
            )
            for map_id in roots
        },
    }
    return closures, metadata
