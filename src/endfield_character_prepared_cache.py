"""Publish immutable prepared resources and materialize independent job copies."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from uuid import uuid4
from typing import TypedDict

from endfield_character_cache import CharacterDataCache, CharacterSourceChangedError, CharacterSourceStamp, character_bundle_closure, source_stamp


@dataclass(frozen=True)
class PreparedCacheIdentity:
    key: str
    sources: tuple[Path, ...]
    stamps: tuple[CharacterSourceStamp, ...]


class PreparedFileRecord(TypedDict):
    path: str
    bytes: int
    sha256: str


def _file_records(value: object) -> tuple[PreparedFileRecord, ...]:
    if not isinstance(value, list) or not value:
        raise TypeError('Prepared cache requires a nonempty artifact array')
    records: list[PreparedFileRecord] = []
    for row in value:
        if not isinstance(row, dict):
            raise TypeError('Prepared cache artifact must be an object')
        name, size, digest = row.get('path'), row.get('bytes'), row.get('sha256')
        if (not isinstance(name, str) or not name or '\\' in name or ':' in name
                or Path(name).is_absolute() or any(part in ('', '.', '..') for part in name.split('/'))):
            raise ValueError(f'Invalid prepared artifact path: path={name!r}')
        if type(size) is not int or size < 0 or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
            raise ValueError(f'Invalid prepared artifact size/hash: path={name}')
        records.append({'path': name, 'bytes': size, 'sha256': digest})
    return tuple(records)


def prepared_identity(cache: CharacterDataCache, game: Path, database: Path, inventory: Path,
                      provider: str, files: tuple[Path, ...], options: tuple[str, ...],
                      check_cancelled: Callable[[], None]) -> PreparedCacheIdentity:
    """Include exact VFS closure storage and hashed tools without rehashing chunks."""
    index, _ = cache.read_character_cab_index(inventory, check_cancelled)
    closures, metadata = character_bundle_closure(index, {'character': [provider]})
    if metadata['maps']['character']['unresolvedExternalCount']:
        raise ValueError('Prepared cache requires a complete selected CAB closure')
    storage: set[Path] = set()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        for name in closures['character']:
            check_cancelled()
            rows = connection.execute('SELECT group_hash,chunk_name FROM bundles WHERE logical_name = ?', (name,)).fetchall()
            if len(rows) != 1:
                raise ValueError(f'Prepared cache requires a unique VFS bundle: provider={name}, matches={len(rows)}')
            group, chunk = rows[0]
            directory = game / 'Endfield_Data/StreamingAssets/VFS' / group
            storage.update((directory / (group + '.blc'), directory / (chunk + '.chk')))
    sources = tuple(sorted({database, inventory, *files, *storage}))
    stamps = tuple(source_stamp(path) for path in sources)
    hashed = [asdict(cache.fingerprint_file(path, 'character-prepared-input/1', check_cancelled))
              for path in sources if path.suffix != '.chk']
    identity = {'format': 'EndfieldCharacterPreparedIdentity/1', 'sources': [asdict(stamp) for stamp in stamps],
                'fingerprints': hashed, 'provider': provider, 'options': options}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    verify_sources(PreparedCacheIdentity(digest, sources, stamps))
    return PreparedCacheIdentity(digest, sources, stamps)


def verify_sources(identity: PreparedCacheIdentity) -> None:
    if tuple(source_stamp(path) for path in identity.sources) != identity.stamps:
        raise CharacterSourceChangedError('Character prepared source/tool identity changed during preparation or reuse')


def _files(root: Path) -> tuple[Path, ...]:
    paths = tuple(sorted(path for path in root.rglob('*') if path.is_file()))
    if any(path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) for path in root.rglob('*')):
        raise ValueError(f'Prepared tree contains an escaping link: root={root}')
    return paths


def _digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def restore_prepared(cache: CharacterDataCache, identity: PreparedCacheIdentity, output: Path,
                     check_cancelled: Callable[[], None]) -> Path | None:
    """Verify completed artifacts before copying; shared files are never writable job links."""
    record_path = cache.cache_root / 'prepared' / (identity.key + '.json')
    if not record_path.is_file():
        return None
    record = json.loads(record_path.read_text(encoding='utf-8'))
    if not isinstance(record, dict) or record.get('key') != identity.key or record.get('format') != 'EndfieldCharacterPreparedCache/1':
        raise ValueError(f'Invalid prepared cache receipt: path={record_path}')
    root_value = record.get('root')
    if not isinstance(root_value, str) or not root_value:
        raise TypeError('Prepared cache root must be a nonempty path')
    root = Path(root_value).resolve(strict=True)
    if not root.is_relative_to((cache.cache_root / 'prepared/objects').resolve()):
        raise ValueError(f'Prepared cache root escapes owned storage: root={root}')
    rows = _file_records(record.get('files'))
    actual = _files(root)
    if [path.relative_to(root).as_posix() for path in actual] != [row['path'] for row in rows]:
        raise ValueError(f'Prepared cache file set changed: root={root}')
    for path, row in zip(actual, rows, strict=True):
        check_cancelled()
        if path.stat().st_size != row['bytes'] or _digest(path) != row['sha256']:
            raise ValueError(f'Prepared cache artifact changed: path={path}')
    verify_sources(identity)
    shutil.copytree(root, output)
    check_cancelled()
    verify_sources(identity)
    return root


def new_prepared_root(cache: CharacterDataCache) -> Path:
    """Use a permanent unique path so embedded provenance and hashes remain valid."""
    root = cache.cache_root / 'prepared/objects' / uuid4().hex
    root.parent.mkdir(parents=True, exist_ok=True)
    return root


def publish_prepared(cache: CharacterDataCache, identity: PreparedCacheIdentity, root: Path,
                     output: Path, check_cancelled: Callable[[], None]) -> None:
    rows = []
    for path in _files(root):
        check_cancelled()
        rows.append({'path': path.relative_to(root).as_posix(), 'bytes': path.stat().st_size, 'sha256': _digest(path)})
    verify_sources(identity)
    record = {'format': 'EndfieldCharacterPreparedCache/1', 'key': identity.key, 'root': str(root), 'files': rows}
    target = cache.cache_root / 'prepared' / (identity.key + '.json')
    temporary = target.with_name(target.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(record, indent=2), encoding='utf-8')
        check_cancelled()
        verify_sources(identity)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    shutil.copytree(root, output)
    check_cancelled()
    verify_sources(identity)
