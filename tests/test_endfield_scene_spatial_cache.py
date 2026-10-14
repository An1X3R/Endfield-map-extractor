"""Exercise persisted spatial selection against authenticated on-disk ECS chunks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from typing import cast
from collections.abc import Callable, Iterable, Mapping

from endfield_scene_export_plan import Bounds, _CachedSpatialSelection, _cached_spatial_selection
from endfield_scene_io import SceneInputChangedError, scene_io_session
from endfield_scene_spatial_cache import (
    SpatialCacheCorruptError, SpatialSourceIdentity, SpatialSourceRecords, read_spatial_source_records,
    spatial_parser_identity, spatial_source_identity,
)
from extractor_core.instances import iter_entities
from extractor_core.projected_decals import read_chunk
from extractor_core.vfs import VfsFile
from endfield_scene_spatial_plan import select_static_source_rows
from tests.test_extractor_core_instances import chunk_with_components


def _matrix(x: float, z: float) -> bytes:
    return struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, 0, z, 1)


def _write_source(root: Path, name: str, x: float, bounds: tuple[float, ...] | None) -> VfsFile:
    components = tuple((index, struct.pack("<f", value)) for index, value in enumerate(bounds)) if bounds is not None else ()
    chunk = chunk_with_components((*components, (18, _matrix(x, x))))
    return _write_chunk(root, name, chunk)


def _write_chunk(root: Path, name: str, chunk: bytes) -> VfsFile:
    standard_token = min(len(chunk), 15) << 4
    token = next(value for value in range(256) if
                 ((((value & 3) | ((value >> 2) & 12)) << 4) | ((value >> 2) & 3) | ((value >> 4) & 12)) == standard_token)
    remaining = len(chunk) - 15
    extension = bytes((255,)) * (remaining // 255) + bytes((remaining % 255,))
    packed = struct.pack("<I", len(chunk)) + bytes((token,)) + extension + chunk
    digest = hashlib.md5(packed).hexdigest()
    target = root / "C3442D43" / f"{digest}.chk"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(packed)
    return VfsFile(name, "0" * 16, digest, digest, 0, len(packed), 0, False, None)


def _duplicate_chunk(last_matrix: bytes, last_components: tuple[tuple[int, bytes], ...]) -> bytes:
    components = tuple((index, struct.pack("<f", value)) for index, value in enumerate((2, 0, 2, 1, 1, 1)))
    first = chunk_with_components((*components, (18, _matrix(2.0, 2.0))))
    second = chunk_with_components((*last_components, (18, last_matrix)))
    data = bytearray(first[:512] + first + second)
    first_base, second_base = 512, 512 + len(first)
    struct.pack_into("<III", data, 80, 2, first_base + 128 - 84, second_base + 128 - 88)
    struct.pack_into("<III", data, 96, 2, first_base + 160 - 100, second_base + 160 - 104)
    return bytes(data)


_ENTITIES = cast(Callable[[bytes], Iterable[Mapping[str, object]]], iter_entities)


def _read(identity: SpatialSourceIdentity, root: Path, entry: VfsFile) -> SpatialSourceRecords:
    return read_spatial_source_records(identity, root, entry, read_chunk, _ENTITIES, None)


class PersistentSpatialCacheTests(unittest.TestCase):
    def test_roundtrip_preserves_exact_selection_and_returns_cold_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = _write_source(root, "near", -20.0, (-1, 10, -1, 3, 20, 3))
            identity = spatial_source_identity(root / "cache", root, entry, spatial_parser_identity())
            cold = _read(identity, root, entry)
            warm = _read(identity, root, entry)
            self.assertEqual(cold.records, warm.records)
            self.assertEqual(read_chunk(root, entry), cold.decoded_chunk)
            self.assertIsNone(warm.decoded_chunk)
            row, = warm.records
            self.assertEqual(_matrix(-20.0, -20.0), row["matrix_blob"])
            self.assertEqual((-4.0, 2.0, -10.0, 30.0, -4.0, 2.0), row["world_bounds"])

    def test_selection_reads_only_selected_and_decal_chunks_after_first_fill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "instances.db"
            db.write_bytes(b"unchanged index")
            near = _write_source(root, "near", 2.0, (2, 0, 2, 1, 1, 1))
            distant = _write_source(root, "distant", 102.0, (102, 0, 102, 1, 1, 1))
            decal = _write_source(root, "decal", 202.0, (202, 0, 202, 1, 1, 1))
            entries = {entry.file_name: entry for entry in (near, distant, decal)}
            rows = {name: ((42, "Building", _matrix(x, x), name, 0, 0, "building"),)
                    for name, x in (("near", 2.0), ("distant", 102.0))}
            reads: list[str] = []
            parses: list[int] = []

            def counted_read(vfs: Path, entry: VfsFile) -> bytes:
                reads.append(entry.file_name)
                return read_chunk(vfs, entry)

            def counted_entities(chunk: bytes) -> Iterable[Mapping[str, object]]:
                parses.append(len(chunk))
                return iter_entities(chunk)

            def columns(chunk: bytes) -> dict[int, dict[int, bytes]]:
                # These real fixture chunks contain no renderer components.
                return {cast(int, row["entity_id"]): {} for row in iter_entities(chunk)}

            def select(bounds: Bounds) -> _CachedSpatialSelection:
                return _cached_spatial_selection(db, root / "cache", root, entries, rows,
                                                 frozenset({"decal"}), bounds, bounds, {"all"}, {},
                                                 frozenset({"building"}), False, False, True, [],
                                                 counted_read, columns, counted_entities, None)

            near_bounds: Bounds = {"xmin": 0, "xmax": 10, "zmin": 0, "zmax": 10}
            far_bounds: Bounds = {"xmin": 100, "xmax": 110, "zmin": 100, "zmax": 110}
            cold = select(near_bounds)
            self.assertEqual(["decal", "distant", "near"], reads)
            self.assertEqual(2, len(parses))
            reads.clear()
            parses.clear()
            warm = select(near_bounds)
            self.assertEqual(cold, warm)
            self.assertEqual(["decal", "near"], reads)
            self.assertEqual([], parses)
            reads.clear()
            other = select(far_bounds)
            self.assertEqual(["decal", "distant"], reads)
            self.assertEqual(["decal", "distant"], [source.source for source in other.sources])
            self.assertEqual({"distant", "decal"}, {source.source for source in other.sources if source.source_sha256})
            reads.clear()
            with scene_io_session():
                first = select(far_bounds)
                select(far_bounds)
                self.assertEqual(["decal", "distant"], reads)
                source = root / "C3442D43" / f"{distant.file_chunk_md5_name}.chk"
                source.write_bytes(source.read_bytes()[:-1])
                with self.assertRaisesRegex(ValueError, "range exceeds backing"):
                    select(far_bounds)
                self.assertEqual(first, other)

    def test_corruption_fails_without_rebuilding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = _write_source(root, "source", 2.0, (2, 0, 2, 1, 1, 1))
            identity = spatial_source_identity(root / "cache", root, entry, spatial_parser_identity())
            _read(identity, root, entry)
            data = identity.cache_path.read_bytes()
            identity.cache_path.write_bytes(data[:-1] + bytes((data[-1] ^ 1,)))
            with self.assertRaisesRegex(SpatialCacheCorruptError, "SHA256 differs"):
                _read(identity, root, entry)
            identity.cache_path.write_bytes(data[:-1])
            with self.assertRaisesRegex(SpatialCacheCorruptError, "layout is invalid"):
                _read(identity, root, entry)

    def test_source_changes_during_fill_fail_and_publish_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = _write_source(root, "source", 2.0, (2, 0, 2, 1, 1, 1))
            identity = spatial_source_identity(root / "cache", root, entry, spatial_parser_identity())

            def changing_entities(chunk: bytes) -> Iterable[Mapping[str, object]]:
                identity.backing_path.write_bytes(identity.backing_path.read_bytes() + b"changed")
                return iter_entities(chunk)

            with self.assertRaisesRegex(SceneInputChangedError, "changed during cache read"):
                read_spatial_source_records(identity, root, entry, read_chunk, changing_entities, None)
            self.assertFalse(identity.cache_path.exists())
            changed = spatial_source_identity(root / "cache", root, entry, spatial_parser_identity())
            self.assertNotEqual(identity.cache_path, changed.cache_path)
            versioned = spatial_source_identity(root / "cache", root, entry, "new-parser")
            self.assertNotEqual(changed.cache_path, versioned.cache_path)

    def test_concurrent_builders_publish_one_valid_complete_record_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = _write_source(root, "source", 2.0, (2, 0, 2, 1, 1, 1))
            identity = spatial_source_identity(root / "cache", root, entry, spatial_parser_identity())
            with ThreadPoolExecutor(max_workers=4) as workers:
                values = list(workers.map(lambda _: _read(identity, root, entry), range(12)))
            self.assertTrue(all(value.records == values[0].records for value in values))
            self.assertEqual([], list(identity.cache_path.parent.glob("*.tmp")))
            self.assertEqual(1, len(list(identity.cache_path.parent.glob("*.bin"))))

    def test_distant_static_bounds_and_matrix_evidence_remain_required_on_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bounds: Bounds = {"xmin": 0, "xmax": 10, "zmin": 0, "zmax": 10}
            for source_bounds, indexed_matrix, message in (
                (None, _matrix(102.0, 102.0), "no source world bounds"),
                ((102, 0, 102, 1, 1, 1), _matrix(999.0, 999.0), "differs from instance index"),
            ):
                entry = _write_source(root, "distant", 102.0, source_bounds)
                identity = spatial_source_identity(root / "cache", root, entry, spatial_parser_identity())
                _read(identity, root, entry)
                warm = _read(identity, root, entry)
                self.assertIsNone(warm.decoded_chunk)
                rows = ((42, "Distant", indexed_matrix, "distant", 0, 0, "building"),)
                with self.assertRaisesRegex(ValueError, message):
                    select_static_source_rows("distant", rows, warm.records, bounds, bounds,
                                              frozenset({"building"}), (), False)

    def test_nonfinite_matrix_is_rejected_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = _write_source(root, "source", float("inf"), (2, 0, 2, 1, 1, 1))
            identity = spatial_source_identity(root / "cache", root, entry, spatial_parser_identity())
            with self.assertRaisesRegex(SpatialCacheCorruptError, "finite float32"):
                _read(identity, root, entry)
            self.assertFalse(identity.cache_path.exists())

    def test_source_entry_fields_and_cache_identity_cannot_be_reused_after_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = _write_source(root, "source", 2.0, (2, 0, 2, 1, 1, 1))
            parser = spatial_parser_identity()
            identity = spatial_source_identity(root / "cache", root, entry, parser)
            _read(identity, root, entry)
            changed = VfsFile("other", entry.file_name_hash, entry.file_chunk_md5_name,
                              entry.file_data_md5, entry.offset, entry.length, entry.block_type,
                              entry.use_encrypt, entry.iv_seed)
            alternate = spatial_source_identity(root / "cache", root, changed, parser)
            self.assertNotEqual(identity.cache_path, alternate.cache_path)
            alternate.cache_path.parent.mkdir(parents=True, exist_ok=True)
            alternate.cache_path.write_bytes(identity.cache_path.read_bytes())
            with self.assertRaisesRegex(SpatialCacheCorruptError, "identity differs"):
                _read(alternate, root, changed)

    def test_duplicate_source_ids_preserve_last_record_selection_and_failure_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bounds: Bounds = {"xmin": 0, "xmax": 10, "zmin": 0, "zmax": 10}
            components = tuple((index, struct.pack("<f", value)) for index, value in enumerate((2, 0, 2, 1, 1, 1)))
            indexed_rows = ((42, "Building", _matrix(2.0, 2.0), "source", 0, 0, "building"),)
            for matrix, last_components, expected_error in (
                (_matrix(2.0, 2.0), components, None),
                (_matrix(102.0, 102.0), components, "differs from instance index"),
                (_matrix(2.0, 2.0), (), "no source world bounds"),
            ):
                with self.subTest(expected_error=expected_error):
                    chunk = _duplicate_chunk(matrix, last_components)
                    decoded = tuple(iter_entities(chunk))
                    self.assertEqual([42, 42], [row["entity_id"] for row in decoded])
                    self.assertEqual([0, 1], [row["group_index"] for row in decoded])
                    entry = _write_chunk(root, "source", chunk)
                    identity = spatial_source_identity(root / "cache", root, entry, spatial_parser_identity())
                    cold = _read(identity, root, entry)
                    warm = _read(identity, root, entry)
                    self.assertEqual(cold.records, warm.records)
                    self.assertEqual(1, len(warm.records))
                    self.assertEqual(decoded[-1]["matrix_blob"], warm.records[0]["matrix_blob"])
                    self.assertEqual(decoded[-1]["world_bounds"], warm.records[0]["world_bounds"])
                    self.assertIsNone(warm.decoded_chunk)
                    if expected_error is None:
                        direct = select_static_source_rows("source", indexed_rows, decoded, bounds, bounds,
                                                           frozenset({"building"}), (), False)
                        cached = select_static_source_rows("source", indexed_rows, warm.records, bounds, bounds,
                                                           frozenset({"building"}), (), False)
                        self.assertEqual(direct, cached)
                        self.assertEqual(indexed_rows, cached.selected_rows)
                    else:
                        for records in (decoded, cold.records, warm.records):
                            with self.assertRaisesRegex(ValueError, expected_error):
                                select_static_source_rows("source", indexed_rows, records, bounds, bounds,
                                                          frozenset({"building"}), (), False)


if __name__ == "__main__":
    unittest.main()
