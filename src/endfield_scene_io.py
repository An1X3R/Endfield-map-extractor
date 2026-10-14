"""Share read-only source manifests during one explicitly owned export operation."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import ItemsView, Iterator, Mapping, ValuesView
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import threading
from typing import TYPE_CHECKING, Callable, Generic, TypeVar, TypedDict, cast
from types import MappingProxyType

if TYPE_CHECKING:
    from endfield_scene_manifest_store import ManifestSections


class ReadOnlySceneDataError(TypeError):
    """A consumer attempted to edit the shared source snapshot."""


class SceneInputChangedError(OSError):
    """A source manifest changed while its snapshot was being read."""


def _reject_edit(*arguments: object, **keywords: object) -> None:
    raise ReadOnlySceneDataError("Scene source snapshot is read-only; copy the edited record before changing it")


class _ReadOnlyObject(dict[str, object]):
    """Expose JSON objects to existing validators without allowing source mutation."""

    __slots__ = ()

    __setitem__ = _reject_edit
    __delitem__ = _reject_edit
    __ior__ = _reject_edit
    clear = _reject_edit
    pop = _reject_edit
    popitem = _reject_edit
    setdefault = _reject_edit
    update = _reject_edit


class _ReadOnlyArray(list[object]):
    """Expose JSON arrays to existing validators without allowing source mutation."""

    __slots__ = ()

    __setitem__ = _reject_edit
    __delitem__ = _reject_edit
    __iadd__ = _reject_edit
    __imul__ = _reject_edit
    append = _reject_edit
    clear = _reject_edit
    extend = _reject_edit
    insert = _reject_edit
    pop = _reject_edit
    remove = _reject_edit
    reverse = _reject_edit
    sort = _reject_edit


def _freeze_array(value: object) -> object:
    if isinstance(value, list) and not isinstance(value, _ReadOnlyArray):
        return _ReadOnlyArray(_freeze_array(item) for item in value)
    return value


def _read_only_object(value: dict[str, object]) -> _ReadOnlyObject:
    # object_hook freezes children as decoding proceeds, avoiding a second full tree.
    return _ReadOnlyObject((key, _freeze_array(item)) for key, item in value.items())


@dataclass(frozen=True)
class _FileStamp:
    device: int
    inode: int
    bytes: int
    modified_ns: int
    changed_time: int
    journal_sequence: int | None
    content_sha256: str | None


@dataclass(frozen=True)
class _Snapshot:
    stamp: _FileStamp
    value: object


@dataclass(frozen=True)
class _DerivedSnapshot:
    sources: tuple[Path, ...]
    stamps: tuple[_FileStamp, ...]
    value: object
    estimated_retained_bytes: int


T = TypeVar("T")


@dataclass(frozen=True)
class SceneSourceCacheSlot(Generic[T]):
    """Bound an immutable derived cache by recursive Python object-size estimate."""

    name: str
    value_type: type[T]
    max_entries: int
    max_bytes: int

    def __post_init__(self) -> None:
        if not self.name or self.max_entries <= 0 or self.max_bytes < 0:
            raise ValueError(f"Invalid scene source cache slot: name={self.name!r}, entries={self.max_entries}, bytes={self.max_bytes}")


class SceneIoStatistics(TypedDict):
    manifest_reads: int
    manifest_hits: int
    manifest_bytes: int
    resolution_reads: int
    resolution_hits: int
    invalidations: int
    retained_manifests: int
    retained_resolutions: int
    retained_manifest_bytes: int
    manifest_evictions: int
    resolution_evictions: int
    source_cache_hits: int
    source_cache_misses: int
    source_cache_evictions: int
    retained_source_cache_entries: int
    estimated_source_cache_bytes: int
    manifest_section_reads: int
    manifest_section_bytes: int


_MANIFEST_BYTE_BUDGET = 512 * 1024 * 1024
_MANIFEST_COUNT_BUDGET = 256
_RESOLUTION_COUNT_BUDGET = 131072
_SOURCE_CACHE_BYTE_BUDGET = 768 * 1024 * 1024
_SINGLE_PLAN_SOURCE_CACHE_BYPASS = frozenset({
    "scene-manifest-index", "path-id-index", "dynamic-scene-renderers",
})


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = (("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong),
                    ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong),
                    ("FileAttributes", wintypes.DWORD))


class SceneIoSession:
    """Own file-system snapshots and release them when the export scope exits."""

    def __init__(self, source_cache_byte_budget: int,
                 bypassed_source_cache_slots: frozenset[str]) -> None:
        if type(source_cache_byte_budget) is not int or source_cache_byte_budget <= 0:
            raise ValueError(f"Scene source cache budget must be a positive integer: bytes={source_cache_byte_budget}")
        self.source_cache_byte_budget = source_cache_byte_budget
        self.bypassed_source_cache_slots = bypassed_source_cache_slots
        self.owner = threading.get_ident()
        self.closed = False
        self.snapshots: OrderedDict[tuple[int, int] | Path, _Snapshot] = OrderedDict()
        self.versions: OrderedDict[Path, _FileStamp] = OrderedDict()
        self.resolutions: OrderedDict[str, Path] = OrderedDict()
        self.manifest_reads = 0
        self.manifest_hits = 0
        self.manifest_bytes = 0
        self.resolution_reads = 0
        self.resolution_hits = 0
        self.invalidations = 0
        self.retained_bytes = 0
        self.manifest_evictions = 0
        self.resolution_evictions = 0
        self.source_cache: OrderedDict[tuple[object, ...], _DerivedSnapshot] = OrderedDict()
        self.source_cache_slot_keys: dict[str, OrderedDict[tuple[object, ...], int]] = {}
        self.source_cache_slot_bytes: dict[str, int] = {}
        self.estimated_source_cache_bytes = 0
        self.source_cache_hits = 0
        self.source_cache_misses = 0
        self.source_cache_evictions = 0
        self.manifest_cache_root: Path | None = None
        self.manifest_section_reads = 0
        self.manifest_section_bytes = 0

    def invalidate(self) -> None:
        """Forget snapshots after resource writes; keep cumulative operation counters."""
        self._require_owner()
        self.snapshots.clear()
        self.versions.clear()
        self.resolutions.clear()
        self.source_cache.clear()
        self.source_cache_slot_keys.clear()
        self.source_cache_slot_bytes.clear()
        self.estimated_source_cache_bytes = 0
        self.retained_bytes = 0
        self.invalidations += 1

    def refresh_resource_paths(self) -> None:
        """Recheck payload paths after extraction while stamped source data stays reusable."""
        self._require_owner()
        self.resolutions.clear()

    def cached_source_value(self, slot: SceneSourceCacheSlot[T], source_paths: tuple[Path, ...],
                            qualifiers: tuple[str, ...], factory: Callable[[], T]) -> T:
        """Read stable sources and retain only admitted immutable derived values."""
        self._require_owner()
        if not source_paths:
            raise ValueError(f"Invalid scene source cache request: slot={slot.name}, sources={len(source_paths)}")
        if any(not isinstance(value, str) for value in qualifiers):
            raise TypeError(f"Scene source cache qualifiers must be strings: slot={slot.name}")
        sources = tuple(resolve_scene_read_path(path) for path in source_paths)
        stamps = tuple(_file_stamp(path) for path in sources)
        key = (slot.name, slot.value_type, sources, qualifiers)
        previous = self.source_cache.get(key)
        if previous is not None and previous.stamps != stamps:
            self._remove_source_cache_entry(key)
            previous = None
        if previous is not None:
            if not isinstance(previous.value, slot.value_type):
                raise TypeError(f"Cached scene source value has unexpected type: slot={slot.name}, expected={slot.value_type.__name__}, actual={type(previous.value).__name__}")
            self.source_cache_hits += 1
            self.source_cache.move_to_end(key)
            self.source_cache_slot_keys[slot.name].move_to_end(key)
            return previous.value
        self.source_cache_misses += 1
        value = factory()
        if not isinstance(value, slot.value_type):
            raise TypeError(f"Scene source factory returned unexpected type: slot={slot.name}, expected={slot.value_type.__name__}, actual={type(value).__name__}")
        admitted = slot.name not in self.bypassed_source_cache_slots
        frozen = _freeze_cached_value(value) if admitted else value
        after = tuple(_file_stamp(path) for path in sources)
        if after != stamps:
            self.invalidate()
            raise SceneInputChangedError(f"Scene source changed during derived read: slot={slot.name}, sources={sources}")
        if not admitted:
            return value
        retained_bytes = _estimate_cached_value_bounded(
            frozen, set(), min(slot.max_bytes, self.source_cache_byte_budget)
        )
        if retained_bytes > slot.max_bytes or retained_bytes > self.source_cache_byte_budget:
            return frozen
        same_slot = self.source_cache_slot_keys.get(slot.name)
        while same_slot is not None and (len(same_slot) >= slot.max_entries or
                self.source_cache_slot_bytes[slot.name] + retained_bytes > slot.max_bytes):
            self._remove_source_cache_entry(next(iter(same_slot)))
            same_slot = self.source_cache_slot_keys.get(slot.name)
        while self.estimated_source_cache_bytes + retained_bytes > self.source_cache_byte_budget:
            oldest = next(iter(self.source_cache))
            self._remove_source_cache_entry(oldest)
        self.source_cache[key] = _DerivedSnapshot(
            sources, stamps, frozen, retained_bytes
        )
        self.source_cache_slot_keys.setdefault(slot.name, OrderedDict())[key] = retained_bytes
        self.source_cache_slot_bytes[slot.name] = self.source_cache_slot_bytes.get(slot.name, 0) + retained_bytes
        self.estimated_source_cache_bytes += retained_bytes
        return frozen

    def _remove_source_cache_entry(self, key: tuple[object, ...]) -> None:
        removed = self.source_cache.pop(key, None)
        if removed is not None:
            slot_name = cast(str, key[0])
            same_slot = self.source_cache_slot_keys[slot_name]
            del same_slot[key]
            self.source_cache_slot_bytes[slot_name] -= removed.estimated_retained_bytes
            if not same_slot:
                del self.source_cache_slot_keys[slot_name]
                del self.source_cache_slot_bytes[slot_name]
            self.estimated_source_cache_bytes -= removed.estimated_retained_bytes
            self.source_cache_evictions += 1

    def _remember_resolution(self, lexical: str, resolved: Path) -> None:
        """Retain only direct canonical paths in the bounded operation-local LRU."""
        self._require_owner()
        if os.path.normcase(lexical) != os.path.normcase(str(resolved)):
            return
        if lexical not in self.resolutions and len(self.resolutions) == _RESOLUTION_COUNT_BUDGET:
            self.resolutions.popitem(last=False)
            self.resolution_evictions += 1
        self.resolutions[lexical] = resolved
        self.resolutions.move_to_end(lexical)

    def statistics(self) -> SceneIoStatistics:
        self._require_owner()
        return {"manifest_reads": self.manifest_reads, "manifest_hits": self.manifest_hits,
                "manifest_bytes": self.manifest_bytes, "resolution_reads": self.resolution_reads,
                "resolution_hits": self.resolution_hits, "invalidations": self.invalidations,
                "retained_manifests": len(self.snapshots), "retained_resolutions": len(self.resolutions),
                "retained_manifest_bytes": self.retained_bytes, "manifest_evictions": self.manifest_evictions,
                "resolution_evictions": self.resolution_evictions, "source_cache_hits": self.source_cache_hits,
                "source_cache_misses": self.source_cache_misses, "source_cache_evictions": self.source_cache_evictions,
                "retained_source_cache_entries": len(self.source_cache),
                "estimated_source_cache_bytes": self.estimated_source_cache_bytes,
                "manifest_section_reads": self.manifest_section_reads,
                "manifest_section_bytes": self.manifest_section_bytes}

    def _require_owner(self) -> None:
        if self.closed:
            raise RuntimeError("Scene IO session has already exited")
        if threading.get_ident() != self.owner:
            raise RuntimeError("Scene IO session cannot be shared across threads; enter a scope on the worker")

    def _close(self) -> None:
        self.invalidate()
        self.closed = True


_ACTIVE: ContextVar[SceneIoSession | None] = ContextVar("endfield_scene_io_session", default=None)


def scene_io_session() -> AbstractContextManager[SceneIoSession]:
    """Create an isolated operation scope, including when another scope is nested."""
    return _scene_io_scope(_SOURCE_CACHE_BYTE_BUDGET, frozenset())


def single_scene_plan_io_session() -> AbstractContextManager[SceneIoSession]:
    """Keep resource-check catalogs while bypassing indexes used only by planning."""
    return _scene_io_scope(_SOURCE_CACHE_BYTE_BUDGET, _SINGLE_PLAN_SOURCE_CACHE_BYPASS)


@contextmanager
def _scene_io_scope(source_cache_byte_budget: int,
                    bypassed_source_cache_slots: frozenset[str]) -> Iterator[SceneIoSession]:
    session = SceneIoSession(source_cache_byte_budget, bypassed_source_cache_slots)
    token = _ACTIVE.set(session)
    try:
        yield session
    finally:
        _ACTIVE.reset(token)
        session._close()


def _active_session() -> SceneIoSession | None:
    session = _ACTIVE.get()
    if session is not None:
        session._require_owner()
    return session


def configure_scene_manifest_cache(cache_root: Path) -> None:
    """Place derived manifest sections in the caller's validated runtime cache."""
    session = _active_session()
    if session is not None:
        resolved = cache_root.expanduser().resolve()
        if session.manifest_cache_root is not None and session.manifest_cache_root != resolved:
            raise ValueError(f"Scene IO cache root cannot change during an export: previous={session.manifest_cache_root}, requested={resolved}")
        session.manifest_cache_root = resolved


class _SectionedManifest(_ReadOnlyObject):
    """Read immutable top-level fields on demand from an authenticated section file."""

    def __init__(self, sections: ManifestSections, source: Path,
                 stamp: _FileStamp, session: SceneIoSession) -> None:
        dict.__init__(self, ((key, None) for key in sections.keys))
        self.sections = sections
        self.source = source
        self.stamp = stamp
        self.session = session
        self.loaded: set[str] = set()

    def __getitem__(self, key: str) -> object:
        if key not in self:
            raise KeyError(key)
        if key not in self.loaded:
            self.session._require_owner()
            payload = self.sections.read_section(key)
            value = _freeze_array(json.loads(payload, object_hook=_read_only_object))
            if _file_stamp(self.source) != self.stamp:
                raise SceneInputChangedError(f"Scene manifest changed before section read: path={self.source}, section={key}")
            dict.__setitem__(self, key, value)
            self.loaded.add(key)
            self.session.manifest_section_reads += 1
            self.session.manifest_section_bytes += len(payload)
        return dict.__getitem__(self, key)

    def get(self, key: str, *defaults: object) -> object:
        if len(defaults) > 1:
            raise TypeError(f"get expected at most 2 arguments, got {len(defaults) + 1}")
        return self[key] if key in self else (defaults[0] if defaults else None)

    def __iter__(self) -> Iterator[str]:
        # An explicit iterator also keeps dict(snapshot) on the mapping protocol.
        return dict.__iter__(self)

    def items(self) -> ItemsView[str, object]:
        return ItemsView(self)

    def values(self) -> ValuesView[object]:
        return ValuesView(self)

    def copy(self) -> dict[str, object]:
        return dict(self.items())

    def __or__(self, other: object) -> dict[str, object]:
        if not isinstance(other, dict):
            return NotImplemented
        return self.copy() | other

    def __ror__(self, other: object) -> dict[str, object]:
        if not isinstance(other, dict):
            return NotImplemented
        return other | self.copy()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, dict):
            return NotImplemented
        return self.copy() == (other.copy() if isinstance(other, _SectionedManifest) else other)

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return NotImplemented if result is NotImplemented else not result

    def __repr__(self) -> str:
        return repr(self.copy())


def cached_scene_source_value(slot: SceneSourceCacheSlot[T], source_paths: tuple[Path, ...],
                              qualifiers: tuple[str, ...], factory: Callable[[], T]) -> T:
    """Use a stamped operation-local cache with a miss-only retained-size estimate."""
    session = _active_session()
    if session is None:
        return factory()
    return session.cached_source_value(slot, source_paths, qualifiers, factory)


def resolve_scene_read_path(path: Path) -> Path:
    """Reuse resolution for direct paths; aliases remain live and payload stat stays live."""
    session = _active_session()
    if session is None:
        return path.expanduser().resolve()
    expanded = path.expanduser()
    lexical = os.path.abspath(expanded)
    if lexical in session.resolutions:
        session.resolution_hits += 1
        session.resolutions.move_to_end(lexical)
        return session.resolutions[lexical]
    resolved = expanded.resolve()
    session.resolution_reads += 1
    # A resolved alias must not outlive a symlink/junction retarget within this scope.
    session._remember_resolution(lexical, resolved)
    return resolved


def resolve_scene_existing_files(paths: tuple[Path, ...]) -> dict[str, Path]:
    """Resolve ordinary files with one fresh directory listing per lexical parent."""
    session = _active_session()
    groups: dict[str, tuple[Path, list[Path]]] = {}
    for path in {str(path): path for path in paths}.values():
        if "\0" in str(path):
            raise ValueError("embedded null byte")
        expanded = path.expanduser()
        groups.setdefault(str(expanded.parent), (expanded.parent, []))[1].append(path)
    resolved_files: dict[str, Path] = {}
    for parent, candidates in groups.values():
        resolved_parent = parent.resolve()
        # Alias parents and special components must retain Path.resolve semantics.
        if os.path.normcase(os.path.abspath(parent)) != os.path.normcase(str(resolved_parent)):
            for candidate in candidates:
                resolved = candidate.expanduser().resolve()
                if resolved.is_file():
                    resolved_files[str(candidate)] = resolved
            continue
        requested = {os.path.normcase(candidate.name) for candidate in candidates}
        entries: dict[str, os.DirEntry[str]] = {}
        ambiguous: set[str] = set()
        try:
            with os.scandir(resolved_parent) as directory:
                for entry in directory:
                    key = os.path.normcase(entry.name)
                    if key in requested:
                        if key in entries and entries[key].name != entry.name:
                            ambiguous.add(key)
                        entries[key] = entry
        except (FileNotFoundError, NotADirectoryError):
            continue
        for candidate in candidates:
            key = os.path.normcase(candidate.name)
            # Windows short names and DOS trailing-dot/space aliases are not entry names.
            if key in ambiguous or (os.name == "nt" and
                    ("~" in candidate.name or candidate.name.endswith((".", " ")))):
                resolved = candidate.expanduser().resolve()
                if resolved.is_file():
                    resolved_files[str(candidate)] = resolved
                continue
            entry = entries.get(key)
            if entry is None:
                continue
            reparse = (os.name == "nt" and bool(entry.stat(follow_symlinks=False).st_file_attributes
                                               & stat.FILE_ATTRIBUTE_REPARSE_POINT))
            if reparse or entry.is_symlink():
                resolved = candidate.expanduser().resolve()
                if resolved.is_file():
                    resolved_files[str(candidate)] = resolved
            elif entry.is_file(follow_symlinks=False):
                resolved = resolved_parent / entry.name
                resolved_files[str(candidate)] = resolved
                if session is not None:
                    session._remember_resolution(os.path.abspath(candidate.expanduser()), resolved)
    return resolved_files


def _file_stamp(path: Path) -> _FileStamp:
    value = path.stat()
    if os.name == "nt":
        return _windows_file_stamp(path, value)
    return _FileStamp(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                      value.st_ctime_ns, None, None)


def _windows_file_stamp(path: Path, value: os.stat_result) -> _FileStamp:
    """Use a journal sequence, or exact bytes on explicitly non-journal filesystems."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create_file.restype = wintypes.HANDLE
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    get_info.restype = wintypes.BOOL
    control = kernel32.DeviceIoControl
    control.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    control.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    # Data-read access enforces sharing; attribute-only handles do not exclude existing writers.
    # USN reasons can coalesce until a writer closes, so stamping must reject active writers.
    handle = create_file(str(path), 0x0081, 0x0001 | 0x0004, None, 3, 0x0080, None)
    if handle == wintypes.HANDLE(-1).value:
        error_code = ctypes.get_last_error()
        if error_code == 32:
            raise SceneInputChangedError(f"Scene source is open for writing during version check: path={path}")
        error = ctypes.WinError(error_code)
        error.filename = str(path)
        raise error
    try:
        info = _FileBasicInfo()
        if not get_info(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            error.filename = str(path)
            raise error
        buffer = ctypes.create_string_buffer(65536)
        returned = wintypes.DWORD()
        sequence: int | None = None
        fingerprint: str | None = None
        if control(handle, 0x000900EB, None, 0, buffer, len(buffer), ctypes.byref(returned), None):
            if returned.value < 8:
                raise SceneInputChangedError(f"Scene source USN header is truncated: path={path}, bytes={returned.value}")
            record_length, major, _minor = struct.unpack_from("<IHH", buffer)
            offsets = {2: 24, 3: 40}
            if (major not in offsets or record_length > returned.value
                    or record_length < offsets[major] + 8):
                raise SceneInputChangedError(
                    f"Scene source USN layout is unsupported: path={path}, version={major}, "
                    f"recordBytes={record_length}, returnedBytes={returned.value}"
                )
            sequence = struct.unpack_from("<q", buffer, offsets[major])[0]
            if sequence < 0:
                raise SceneInputChangedError(f"Scene source USN is negative: path={path}, sequence={sequence}")
            if sequence == 0:
                sequence = None
        else:
            error_code = ctypes.get_last_error()
            # INVALID_FUNCTION, NOT_SUPPORTED and JOURNAL_NOT_ACTIVE select exact-byte versioning.
            if error_code not in (1, 50, 1179):
                error = ctypes.WinError(error_code)
                error.filename = str(path)
                raise error
        if sequence is None:
            # Keep the write-exclusion handle open throughout the exact content read.
            with path.open("rb") as source:
                fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
        return _FileStamp(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                          int(info.ChangeTime), sequence, fingerprint)
    finally:
        if not close_handle(handle):
            error = ctypes.WinError(ctypes.get_last_error())
            error.filename = str(path)
            raise error


def _freeze_cached_value(value: T) -> T:
    if isinstance(value, dict) and not isinstance(value, _ReadOnlyObject):
        return cast(T, _ReadOnlyObject((key, _freeze_cached_value(item)) for key, item in value.items()))
    if isinstance(value, list) and not isinstance(value, _ReadOnlyArray):
        return cast(T, _ReadOnlyArray(_freeze_cached_value(item) for item in value))
    if isinstance(value, set):
        return cast(T, frozenset(_freeze_cached_value(item) for item in value))
    if type(value) is tuple:
        return cast(T, tuple(_freeze_cached_value(item) for item in value))
    if isinstance(value, frozenset):
        return cast(T, frozenset(_freeze_cached_value(item) for item in value))
    return value


def _estimate_cached_value(value: object, seen: set[int]) -> int:
    """Estimate retained Python objects; native payloads and allocator arenas are excluded."""
    return _estimate_cached_value_bounded(value, seen, None)


def _estimate_cached_value_bounded(value: object, seen: set[int], maximum: int | None) -> int:
    """Count exactly for admissible values; stop once a value cannot fit its cache."""
    if maximum is not None and maximum < 0:
        raise ValueError(f"Scene cache size limit cannot be negative: maximum={maximum}")
    pending: list[object] = [value]
    owned_seen = set(seen)
    scalar_types = (str, bytes, int, float, bool, type(None))
    size = 0
    while pending:
        if maximum is not None and size > maximum:
            return size
        item = pending.pop()
        identity = id(item)
        if identity in owned_seen:
            continue
        owned_seen.add(identity)
        size += sys.getsizeof(item)
        # Source catalogs mostly contain scalars; avoid ABC/dataclass inspection for them.
        if type(item) in scalar_types:
            continue
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
        elif isinstance(item, Mapping):
            if isinstance(item, MappingProxyType):
                size += 64 + 16 * len(item)
            pending.extend(item.keys())
            pending.extend(item.values())
        elif is_dataclass(item) and not isinstance(item, type):
            try:
                pending.append(vars(item))
            except TypeError:
                pending.extend(getattr(item, field.name) for field in fields(item))
        elif isinstance(item, Path):
            size += sys.getsizeof(str(item))
    return size


def read_scene_manifest(path: Path) -> object:
    """Read a fresh version or reuse its immutable parse; integrity hashes remain external."""
    session = _active_session()
    if session is None:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    resolved = resolve_scene_read_path(path)
    before = _file_stamp(resolved)
    # Extended Windows paths and normal paths can identify the same physical file.
    key: tuple[int, int] | Path = (before.device, before.inode) if before.inode else resolved
    previous = session.snapshots.get(key)
    version = session.versions.get(resolved)
    if ((version is not None and version != before)
            or (previous is not None and previous.stamp != before)):
        session.invalidate()
        previous = None
    session.versions[resolved] = before
    session.versions.move_to_end(resolved)
    if len(session.versions) > _MANIFEST_COUNT_BUDGET * 2:
        session.versions.popitem(last=False)
    if previous is not None and previous.stamp == before:
        session.manifest_hits += 1
        session.snapshots.move_to_end(key)
        return previous.value
    if session.manifest_cache_root is None:
        value = json.loads(resolved.read_text(encoding="utf-8-sig"), object_hook=_read_only_object)
        value = _freeze_array(value)
    else:
        from endfield_scene_manifest_store import open_manifest_sections

        identity = json.dumps({"source": str(resolved), "stamp": asdict(before)},
                              sort_keys=True, separators=(",", ":"))
        sections = open_manifest_sections(resolved, session.manifest_cache_root, identity)
        value = _SectionedManifest(sections, resolved, before, session)
    if _file_stamp(resolved) != before:
        raise SceneInputChangedError(f"Scene manifest changed during read: path={resolved}")
    if before.bytes <= _MANIFEST_BYTE_BUDGET:
        while session.snapshots and (session.retained_bytes + before.bytes > _MANIFEST_BYTE_BUDGET
                                    or len(session.snapshots) >= _MANIFEST_COUNT_BUDGET):
            _, evicted = session.snapshots.popitem(last=False)
            session.retained_bytes -= evicted.stamp.bytes
            session.manifest_evictions += 1
        session.snapshots[key] = _Snapshot(before, value)
        session.retained_bytes += before.bytes
    session.manifest_reads += 1
    session.manifest_bytes += before.bytes
    return value
