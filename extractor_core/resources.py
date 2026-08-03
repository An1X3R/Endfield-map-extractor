"""Bootstrap VFS metadata and the searchable resource database from a game install."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
import json
from pathlib import Path
from typing import Any, Callable

import brotli

from . import vfs


Progress = Callable[[str, float, str, dict[str, Any] | None], None]
Cancelled = Callable[[], bool]

BOOTSTRAP_VIRTUAL_FILES = {
    "manifest": ("1CDDBF1F", "Data/Bundles/Windows/manifest.hgmmap", "manifest.hgmmap"),
    "initial_paths": (
        "3C9D9D2D",
        "Data/ExtendData/Initial/InitStringPathHash.bin",
        "InitStringPathHash.bin",
    ),
    "main_compress": (
        "D6E622F7",
        "Data/ExtendData/Main/CompressData.bin",
        "CompressData.bin",
    ),
    "main_paths": (
        "D6E622F7",
        "Data/ExtendData/Main/StringPathHash.bin",
        "StringPathHash.bin",
    ),
}
BUNDLE_GROUPS = (("7064D8E2", "main"), ("0CE8FA57", "initial"))
BUNDLE_PATTERN = re.compile(
    rb"(?:(?:i\x00n\x00i\x00t\x00i\x00a\x00l)|(?:m\x00a\x00i\x00n))"
    rb"\x00/\x00(?:[0-9a-f]\x00){24}\.\x00a\x00b\x00",
    re.I,
)


class BootstrapExtractionError(RuntimeError):
    pass


def _emit(progress: Progress | None, phase: str, value: float, message: str, **details: Any) -> None:
    if progress:
        progress(phase, value, message, details or None)


def _check_cancelled(cancelled: Cancelled | None) -> None:
    if cancelled and cancelled():
        raise BootstrapExtractionError("first-run extraction was cancelled")


def validate_vfs_root(vfs_root: Path) -> Path:
    resolved = vfs_root.expanduser().resolve()
    if not resolved.is_dir():
        raise BootstrapExtractionError(
            f"Game VFS directory is missing: {resolved}. Select the directory containing Endfield.exe."
        )
    return resolved


def load_group(vfs_root: Path, group_hash: str) -> vfs.VfsGroup:
    group_dir = validate_vfs_root(vfs_root) / group_hash
    catalog = group_dir / f"{group_hash}.blc"
    chunks = list(group_dir.glob("*.chk"))
    if not catalog.is_file() or not chunks:
        raise BootstrapExtractionError(
            f"Required VFS group {group_hash} is incomplete under {group_dir}. Verify the selected game build."
        )
    key = bytes.fromhex(vfs.DEFAULT_VFS_KEY_HEX)
    return vfs.parse_blc(
        vfs.decrypt_blc(catalog.read_bytes(), key),
        expected_group_hash=group_hash,
        expected_chunk_count=len(chunks),
        expected_chunks_length=sum(path.stat().st_size for path in chunks),
        expected_block_type=11,
    )


def read_virtual_file(vfs_root: Path, group_hash: str, virtual_path: str) -> bytes:
    group = load_group(vfs_root, group_hash)
    wanted = virtual_path.casefold()
    item = next((row for row in group.iter_files() if row.file_name.casefold() == wanted), None)
    if item is None:
        raise BootstrapExtractionError(
            f"Required game file is absent from VFS group {group_hash}: {virtual_path}"
        )
    group_dir = vfs_root / group_hash
    chunk_path = group_dir / f"{item.file_chunk_md5_name}.chk"
    if not chunk_path.is_file():
        raise BootstrapExtractionError(f"VFS chunk is missing for {virtual_path}: {chunk_path}")
    from io import BytesIO

    output = BytesIO()
    with chunk_path.open("rb") as source:
        digest = vfs.copy_virtual_file(
            source,
            output,
            item,
            group.version,
            bytes.fromhex(vfs.DEFAULT_VFS_KEY_HEX),
        )
    if digest != item.file_data_md5.upper():
        raise BootstrapExtractionError(
            f"VFS payload checksum mismatch for {virtual_path}: expected {item.file_data_md5}, got {digest}"
        )
    return output.getvalue()


def extract_bootstrap_metadata(
    vfs_root: Path,
    output_dir: Path,
    *,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> dict[str, Path]:
    root = output_dir.expanduser().resolve()
    if root.exists():
        raise FileExistsError(f"Refusing to reuse bootstrap metadata directory: {root}")
    root.mkdir(parents=True)
    results: dict[str, Path] = {}
    total = len(BOOTSTRAP_VIRTUAL_FILES)
    for index, (role, (group_hash, virtual_path, filename)) in enumerate(
        BOOTSTRAP_VIRTUAL_FILES.items(), start=1
    ):
        _check_cancelled(cancelled)
        _emit(progress, "bootstrap_metadata", (index - 1) / total, f"Extracting {role}", role=role)
        data = read_virtual_file(vfs_root, group_hash, virtual_path)
        target = root / filename
        with target.open("xb") as handle:
            handle.write(data)
        results[role] = target
    _emit(progress, "bootstrap_metadata", 1.0, "Bootstrap VFS metadata extracted")
    return results


def iter_path_records(path: Path):
    data = path.read_bytes()
    if len(data) < 8:
        raise BootstrapExtractionError("StringPathHash.bin is too short")
    strings_offset, count = struct.unpack_from("<II", data)
    records_end = 8 + count * 24
    if strings_offset != records_end or strings_offset > len(data):
        raise BootstrapExtractionError(
            f"Unexpected StringPathHash layout: offset={strings_offset}, calculated={records_end}, file={len(data)}"
        )
    position = strings_offset
    for index in range(count):
        if position + 4 > len(data):
            raise BootstrapExtractionError(f"String table ended at record {index}")
        byte_length = struct.unpack_from("<I", data, position)[0]
        end = position + 4 + byte_length
        if byte_length % 2 or end + 2 > len(data):
            raise BootstrapExtractionError(f"Invalid UTF-16 string at record {index}")
        value = data[position + 4 : end].decode("utf-16le")
        if data[end : end + 2] != b"\0\0":
            raise BootstrapExtractionError(f"Missing UTF-16 terminator at record {index}")
        opaque_record = data[8 + index * 24 : 8 + (index + 1) * 24]
        yield index, value, opaque_record, position
        position = end + 2
    if position != len(data):
        raise BootstrapExtractionError(f"String table has {len(data) - position} trailing bytes")


def build_resource_database(
    vfs_root: Path,
    manifest: Path,
    string_path_hash: Path,
    output: Path,
    *,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> dict[str, str]:
    target = output.expanduser().resolve()
    game_vfs = validate_vfs_root(vfs_root)
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite resource database: {target}")
    if target == game_vfs or game_vfs in target.parents:
        raise BootstrapExtractionError("Resource database must be outside the game installation")
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE paths (
                path_index INTEGER PRIMARY KEY, path TEXT NOT NULL, path_folded TEXT NOT NULL,
                opaque_record BLOB NOT NULL, string_offset INTEGER NOT NULL
            );
            CREATE TABLE bundles (
                logical_name TEXT PRIMARY KEY, group_hash TEXT NOT NULL, virtual_path TEXT NOT NULL,
                chunk_name TEXT NOT NULL, chunk_offset INTEGER NOT NULL, byte_length INTEGER NOT NULL,
                data_md5 TEXT NOT NULL, present_in_manifest INTEGER NOT NULL
            );
            CREATE INDEX paths_folded_idx ON paths(path_folded);
            CREATE INDEX bundles_virtual_path_idx ON bundles(virtual_path);
            """
        )
        batch = []
        path_count = 0
        for row in iter_path_records(string_path_hash):
            _check_cancelled(cancelled)
            index, value, opaque, offset = row
            batch.append((index, value, value.casefold(), opaque, offset))
            if len(batch) >= 5000:
                connection.executemany("INSERT INTO paths VALUES (?, ?, ?, ?, ?)", batch)
                batch.clear()
            path_count += 1
        if batch:
            connection.executemany("INSERT INTO paths VALUES (?, ?, ?, ?, ?)", batch)
        _emit(progress, "resource_index", 0.55, "Indexed StringPathHash", pathCount=path_count)

        manifest_clear = brotli.decompress(manifest.read_bytes())
        manifest_bundles = {
            match.group().decode("utf-16le").casefold() for match in BUNDLE_PATTERN.finditer(manifest_clear)
        }
        bundle_rows = []
        outer_names = set()
        for group_index, (group_hash, category) in enumerate(BUNDLE_GROUPS, start=1):
            _check_cancelled(cancelled)
            group = load_group(game_vfs, group_hash)
            for item in group.iter_files():
                logical_name = f"{category}/{Path(item.file_name).name}".casefold()
                outer_names.add(logical_name)
                bundle_rows.append(
                    (
                        logical_name,
                        group_hash,
                        item.file_name,
                        item.file_chunk_md5_name,
                        item.offset,
                        item.length,
                        item.file_data_md5,
                        int(logical_name in manifest_bundles),
                    )
                )
            _emit(
                progress,
                "resource_index",
                0.55 + 0.4 * group_index / len(BUNDLE_GROUPS),
                f"Indexed bundle group {group_hash}",
            )
        connection.executemany("INSERT INTO bundles VALUES (?, ?, ?, ?, ?, ?, ?, ?)", bundle_rows)
        meta = {
            "format": "EndfieldResourceDB/1",
            "path_count": str(path_count),
            "outer_bundle_count": str(len(outer_names)),
            "manifest_bundle_count": str(len(manifest_bundles)),
            "bundle_intersection_count": str(len(outer_names & manifest_bundles)),
            "bundle_missing_from_manifest": str(len(outer_names - manifest_bundles)),
            "bundle_extra_in_manifest": str(len(manifest_bundles - outer_names)),
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest().upper(),
            "string_path_hash_sha256": hashlib.sha256(string_path_hash.read_bytes()).hexdigest().upper(),
        }
        connection.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        connection.commit()
        _emit(progress, "resource_index", 1.0, "Resource database complete", output=str(target))
        return meta
    finally:
        connection.close()


def write_bundle_scan_index(database: Path, vfs_root: Path, output: Path) -> dict[str, str]:
    """Create the BundleScanner TSV directly from the generated resource index."""

    source = database.expanduser().resolve()
    game_vfs = validate_vfs_root(vfs_root)
    target = output.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Resource database is missing: {source}")
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite bundle scan index: {target}")
    if target == game_vfs or game_vfs in target.parents:
        raise BootstrapExtractionError("Bundle scan index must be outside the game installation")
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        rows = list(
            connection.execute(
                "SELECT logical_name,group_hash,chunk_name,chunk_offset,byte_length "
                "FROM bundles ORDER BY logical_name"
            )
        )
    finally:
        connection.close()
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write("logical_name\tchunk_path\toffset\tlength\n")
        for logical_name, group_hash, chunk_name, offset, length in rows:
            chunk_path = game_vfs / str(group_hash) / f"{chunk_name}.chk"
            if not chunk_path.is_file():
                raise BootstrapExtractionError(
                    f"Bundle scan input references a missing VFS chunk: {chunk_path}"
                )
            handle.write(f"{logical_name}\t{chunk_path}\t{int(offset)}\t{int(length)}\n")
    return {"format": "EndfieldBundleScanIndex/1", "bundle_count": str(len(rows))}


def read_bundle_candidate_names(results: Path) -> list[str]:
    """Return deterministic logical bundle names from a BundleScanner JSONL result."""

    source = results.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Bundle scanner results are missing: {source}")
    names: set[str] = set()
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise BootstrapExtractionError(
                    f"Bundle scanner result is invalid JSONL at line {line_number}: {source}"
                ) from error
            if row.get("kind") == "match" and isinstance(row.get("logical_name"), str):
                names.add(row["logical_name"].casefold())
    return sorted(names)


def read_bundle_candidate_names_by_map(results: Path) -> dict[str, list[str]]:
    """Group scanner-selected logical bundles by Map01/Map02 container evidence."""

    source = results.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Bundle scanner results are missing: {source}")
    names: dict[str, set[str]] = {"map01": set(), "map02": set()}
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise BootstrapExtractionError(
                    f"Bundle scanner result is invalid JSONL at line {line_number}: {source}"
                ) from error
            logical_name = row.get("logical_name")
            if row.get("kind") != "match" or not isinstance(logical_name, str):
                continue
            containers = [
                value.casefold().replace("\\", "/")
                for value in row.get("containers") or []
                if isinstance(value, str)
            ]
            for map_id in names:
                if any(f"/{map_id}/" in value or f"{map_id}_" in value for value in containers):
                    names[map_id].add(logical_name.casefold())
    return {map_id: sorted(values) for map_id, values in names.items()}


def read_bundle_closure_names_by_map(
    matches: Path, cab_inventory: Path
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Expand map candidate bundles through serialized-file external references."""

    roots = read_bundle_candidate_names_by_map(matches)
    cab_path = cab_inventory.expanduser().resolve()
    if not cab_path.is_file():
        raise FileNotFoundError(f"Bundle CAB inventory is missing: {cab_path}")

    bundle_serialized: dict[str, set[str]] = {}
    bundle_externals: dict[str, set[str]] = {}
    providers: dict[str, set[str]] = {}

    def normalized_file_name(value: str) -> str:
        return value.replace("\\", "/").rsplit("/", 1)[-1].casefold()

    with cab_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise BootstrapExtractionError(
                    f"CAB inventory is invalid JSONL at line {line_number}: {cab_path}"
                ) from error
            if row.get("kind") != "cab" or not isinstance(row.get("logical_name"), str):
                continue
            logical_name = row["logical_name"].casefold()
            serialized_names = bundle_serialized.setdefault(logical_name, set())
            external_names = bundle_externals.setdefault(logical_name, set())
            for serialized in row.get("serialized") or []:
                if not isinstance(serialized, dict):
                    continue
                name = serialized.get("name")
                if isinstance(name, str) and name:
                    normalized = normalized_file_name(name)
                    serialized_names.add(normalized)
                    providers.setdefault(normalized, set()).add(logical_name)
                for external in serialized.get("externals") or []:
                    if isinstance(external, str) and external:
                        external_names.add(normalized_file_name(external))

    closures: dict[str, list[str]] = {}
    unresolved_by_map: dict[str, list[str]] = {}
    for map_id, root_names in roots.items():
        selected = set(root_names)
        queue = list(root_names)
        unresolved: set[str] = set()
        while queue:
            logical_name = queue.pop()
            for external in bundle_externals.get(logical_name, set()):
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

    metadata = {
        "format": "EndfieldAutomaticBundleClosure/1",
        "providerSerializedFileCount": len(providers),
        "bundleInventoryCount": len(bundle_serialized),
        "maps": {
            map_id: {
                "rootBundleCount": len(roots[map_id]),
                "closureBundleCount": len(closures[map_id]),
                "unresolvedExternalCount": len(unresolved_by_map[map_id]),
                "unresolvedExternalSample": unresolved_by_map[map_id][:32],
            }
            for map_id in ("map01", "map02")
        },
    }
    return closures, metadata


def extract_candidate_bundles(
    database: Path,
    vfs_root: Path,
    logical_names: list[str],
    output_dir: Path,
    *,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> dict[str, str]:
    """Extract only scanner-selected bundles into an external first-run cache."""

    source = database.expanduser().resolve()
    game_vfs = validate_vfs_root(vfs_root)
    root = output_dir.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Resource database is missing: {source}")
    if root.exists():
        raise FileExistsError(f"Refusing to reuse candidate bundle directory: {root}")
    if root == game_vfs or game_vfs in root.parents:
        raise BootstrapExtractionError("Candidate bundle output must be outside the game installation")
    root.mkdir(parents=True)
    wanted = {name.casefold() for name in logical_names}
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        rows = list(
            connection.execute(
                "SELECT logical_name,group_hash,virtual_path FROM bundles ORDER BY logical_name"
            )
        )
    finally:
        connection.close()
    selected = [row for row in rows if str(row[0]).casefold() in wanted]
    missing = sorted(wanted - {str(row[0]).casefold() for row in selected})
    if missing:
        raise BootstrapExtractionError(
            "Bundle scanner selected names absent from the generated resource index: "
            + ", ".join(missing[:8])
        )
    groups: dict[str, tuple[vfs.VfsGroup, dict[str, vfs.VfsFile]]] = {}
    key = bytes.fromhex(vfs.DEFAULT_VFS_KEY_HEX)
    extracted = 0
    total_bytes = 0
    for index, (logical_name, group_hash, virtual_path) in enumerate(selected, start=1):
        _check_cancelled(cancelled)
        group_key = str(group_hash)
        if group_key not in groups:
            group = load_group(game_vfs, group_key)
            groups[group_key] = (
                group,
                {item.file_name.casefold(): item for item in group.iter_files()},
            )
        group, by_virtual_path = groups[group_key]
        item = by_virtual_path.get(str(virtual_path).casefold())
        if item is None:
            raise BootstrapExtractionError(
                f"Bundle resource disappeared from VFS catalog: {virtual_path}"
            )
        category, _, file_name = str(logical_name).partition("/")
        target_dir = root / (category or "bundles")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (file_name or f"{item.file_data_md5}.ab")
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite candidate bundle: {target}")
        chunk_path = game_vfs / group_key / f"{item.file_chunk_md5_name}.chk"
        with chunk_path.open("rb") as input_stream, target.open("xb") as output_stream:
            digest = vfs.copy_virtual_file(input_stream, output_stream, item, group.version, key)
        if digest != item.file_data_md5.upper():
            raise BootstrapExtractionError(
                f"Candidate bundle checksum mismatch for {logical_name}: {digest}"
            )
        extracted += 1
        total_bytes += target.stat().st_size
        _emit(
            progress,
            "scene_candidate_bundles",
            index / max(1, len(selected)),
            f"Extracted scene candidate bundle {index}/{len(selected)}",
            logicalName=logical_name,
        )
    return {
        "format": "EndfieldCandidateBundleSet/1",
        "requested_count": str(len(wanted)),
        "extracted_count": str(extracted),
        "bytes": str(total_bytes),
    }
