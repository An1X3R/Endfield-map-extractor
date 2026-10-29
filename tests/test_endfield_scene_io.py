from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import MappingProxyType
import unittest

from endfield_scene_io import (
    ReadOnlySceneDataError,
    SceneInputChangedError,
    SceneSourceCacheSlot,
    _SOURCE_CACHE_BYTE_BUDGET,
    _estimate_cached_value,
    _estimate_cached_value_bounded,
    cached_scene_source_value,
    configure_scene_manifest_cache,
    read_scene_manifest,
    resolve_scene_read_path,
    resolve_scene_existing_files,
    scene_io_session,
    single_scene_plan_io_session,
)
from endfield_scene_resources import describe_file


def source_document(path: Path, value: int) -> None:
    path.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Materials": [
        {"Name": "source", "Floats": {"value": value}, "Colors": {"color": [1, 2, 3, 4]}}
    ]}), encoding="utf-8")


@dataclass(frozen=True)
class FrozenCacheValue:
    providers: MappingProxyType


class SceneIoTests(unittest.TestCase):
    def test_sectioned_manifest_preserves_mapping_and_read_only_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            expected = {"Format": "EndfieldSceneProbe/1", "Materials": [{"Name": "material", "Values": [1, 2]}],
                        "Nodes": [{"Name": "unused"}], "Optional": None}
            source.write_text(json.dumps(expected), encoding="utf-8")
            for _ in range(2):
                with scene_io_session() as session:
                    configure_scene_manifest_cache(root / "cache")
                    document = read_scene_manifest(source)
                    self.assertIsInstance(document, dict)
                    self.assertEqual(list(expected), list(document))
                    self.assertEqual(0, session.statistics()["manifest_section_reads"])
                    self.assertEqual("EndfieldSceneProbe/1", document["Format"])
                    self.assertEqual(1, session.statistics()["manifest_section_reads"])
                    self.assertIs(document, read_scene_manifest(source))
                    self.assertIsNone(document.get("Optional"))
                    self.assertEqual("missing", document.get("Absent", "missing"))
                    self.assertNotIn("Absent", document)
                    with self.assertRaises(KeyError):
                        document["Absent"]
                    with self.assertRaises(ReadOnlySceneDataError):
                        document["Materials"][0]["Values"].append(3)
                    self.assertEqual(3, session.statistics()["manifest_section_reads"])
                    self.assertEqual(expected, dict(document))
                    self.assertEqual(expected, document.copy())
                    self.assertEqual(expected, dict(document.items()))
                    self.assertEqual(list(expected.values()), list(document.values()))
                    self.assertEqual(expected, json.loads(json.dumps(document)))
                    self.assertEqual(expected, document)
                    self.assertEqual(document, expected)
                    self.assertEqual({**expected, "extra": 1}, document | {"extra": 1})
                    self.assertEqual({"extra": 1, **expected}, {"extra": 1} | document)
                    self.assertEqual(4, session.statistics()["manifest_section_reads"])

    def test_sectioned_manifest_rejects_source_changes_before_lazy_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            source_document(source, 1)
            original_time = source.stat().st_mtime_ns
            with scene_io_session():
                configure_scene_manifest_cache(root / "cache")
                document = read_scene_manifest(source)
                source_document(source, 2)
                os.utime(source, ns=(original_time, original_time))
                with self.assertRaisesRegex(SceneInputChangedError, "changed before section read"):
                    document["Materials"]
                current = read_scene_manifest(source)
                self.assertEqual(2, current["Materials"][0]["Floats"]["value"])
            with scene_io_session():
                configure_scene_manifest_cache(root / "cache")
                self.assertEqual(2, read_scene_manifest(source)["Materials"][0]["Floats"]["value"])

    def test_sectioned_manifest_fresh_mapping_operations_and_scope_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            expected = {"Format": "EndfieldSceneProbe/1", "Nodes": [{"Name": "node"}]}
            source.write_text(json.dumps(expected), encoding="utf-8")
            operations = (lambda value: dict(value), lambda value: {**value},
                          lambda value: value.copy(), lambda value: value | {},
                          lambda value: {} | value, lambda value: json.loads(json.dumps(value)),
                          lambda value: dict(value.items()))
            for operation in operations:
                with scene_io_session():
                    configure_scene_manifest_cache(root / "cache")
                    self.assertEqual(expected, operation(read_scene_manifest(source)))
            with scene_io_session():
                configure_scene_manifest_cache(root / "cache")
                document = read_scene_manifest(source)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    with self.assertRaisesRegex(RuntimeError, "across threads"):
                        executor.submit(lambda: document["Nodes"]).result()
            with self.assertRaisesRegex(RuntimeError, "already exited"):
                document["Nodes"]

    def test_single_plan_scope_reuses_bounded_path_index_and_bypasses_other_large_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("first", encoding="utf-8")
            path_slot = SceneSourceCacheSlot("path-id-index", dict, 1, 320 * 1024 * 1024)
            renderer_slot = SceneSourceCacheSlot("dynamic-scene-renderers", dict, 1, 320 * 1024 * 1024)

            def build() -> dict[str, list[str]]:
                return {"items": [path.read_text(encoding="utf-8")]}

            with single_scene_plan_io_session() as session:
                first = cached_scene_source_value(path_slot, (path,), (), build)
                with self.assertRaises(ReadOnlySceneDataError):
                    first["items"].append("changed locally")
                self.assertIs(first, cached_scene_source_value(path_slot, (path,), (), build))
                unshared = cached_scene_source_value(renderer_slot, (path,), (), build)
                unshared["items"].append("changed locally")
                rebuilt = cached_scene_source_value(renderer_slot, (path,), (), build)
                self.assertEqual({"items": ["first"]}, rebuilt)
                self.assertIsNot(unshared, rebuilt)
                self.assertEqual(3, session.statistics()["source_cache_misses"])
                self.assertEqual(1, session.statistics()["source_cache_hits"])
                self.assertEqual(1, session.statistics()["retained_source_cache_entries"])
                self.assertGreater(session.statistics()["estimated_source_cache_bytes"], 0)

            self.assertEqual(0, len(session.source_cache))

            with scene_io_session() as session:
                cached = cached_scene_source_value(path_slot, (path,), (), build)
                self.assertIs(cached, cached_scene_source_value(path_slot, (path,), (), build))
                with self.assertRaises(ReadOnlySceneDataError):
                    cached["items"].append("changed locally")
                self.assertEqual(1, session.statistics()["source_cache_hits"])
                large_ceiling = SceneSourceCacheSlot("normal-large-ceiling", dict, 1,
                                                    _SOURCE_CACHE_BYTE_BUDGET + 1)
                retained = cached_scene_source_value(large_ceiling, (path,), (), build)
                self.assertIs(retained, cached_scene_source_value(large_ceiling, (path,), (), build))
                self.assertEqual(2, session.statistics()["source_cache_hits"])

    def test_single_plan_scope_preserves_small_cache_hits_and_immutable_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            source_document(path, 1)
            with single_scene_plan_io_session() as session:
                first = read_scene_manifest(path)
                self.assertIs(first, read_scene_manifest(path))
                with self.assertRaises(ReadOnlySceneDataError):
                    first["Materials"][0]["Name"] = "changed locally"
                self.assertEqual(describe_file(path), describe_file(path))
                self.assertEqual(1, session.statistics()["source_cache_hits"])
                self.assertEqual(1, session.statistics()["retained_source_cache_entries"])
                self.assertLessEqual(session.statistics()["estimated_source_cache_bytes"], _SOURCE_CACHE_BYTE_BUDGET)

    def test_single_plan_scope_retains_material_catalog_for_resource_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("material", encoding="utf-8")
            slot = SceneSourceCacheSlot("material-catalog", tuple, 4, 512 * 1024 * 1024)

            def build() -> tuple[dict[str, str], dict[str, str]]:
                return {"material": path.read_text(encoding="utf-8")}, {"texture": "texture.png"}

            with single_scene_plan_io_session() as session:
                first = cached_scene_source_value(slot, (path,), (), build)
                self.assertIs(first, cached_scene_source_value(slot, (path,), (), build))
                with self.assertRaises(ReadOnlySceneDataError):
                    first[0]["material"] = "changed locally"
                self.assertEqual(1, session.statistics()["source_cache_hits"])
                self.assertEqual(1, session.statistics()["retained_source_cache_entries"])

    def test_single_plan_bypass_rejects_source_changes_and_invalid_factory_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("first", encoding="utf-8")
            slot = SceneSourceCacheSlot("scene-manifest-index", str, 1, 256 * 1024 * 1024)

            def change_during_read() -> str:
                value = path.read_text(encoding="utf-8")
                path.write_text("other", encoding="utf-8")
                return value

            with single_scene_plan_io_session() as session:
                with self.assertRaisesRegex(SceneInputChangedError, "changed during derived read"):
                    cached_scene_source_value(slot, (path,), (), change_during_read)
                self.assertEqual(0, session.statistics()["retained_source_cache_entries"])
                with self.assertRaisesRegex(TypeError, "factory returned unexpected type"):
                    cached_scene_source_value(slot, (path,), (), path.read_bytes)
                self.assertEqual("other", cached_scene_source_value(
                    slot, (path,), (), lambda: path.read_text(encoding="utf-8")))

    def test_batch_direct_payload_seeds_only_resolution_and_keeps_presence_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mesh.obj"
            path.write_bytes(b"mesh")
            canonical = path.resolve()
            with scene_io_session() as session:
                self.assertEqual({str(path): canonical}, resolve_scene_existing_files((path,)))
                reads = session.statistics()["resolution_reads"]
                hits = session.statistics()["resolution_hits"]
                self.assertEqual(canonical, resolve_scene_read_path(path))
                self.assertEqual(reads, session.statistics()["resolution_reads"])
                self.assertEqual(hits + 1, session.statistics()["resolution_hits"])
                path.unlink()
                self.assertFalse(resolve_scene_read_path(path).is_file())
                self.assertEqual({}, resolve_scene_existing_files((path,)))

    def test_batch_payload_resolution_is_canonical_and_rechecks_live_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "First.obj"
            second = root / "second.obj"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            absent = root / "missing.obj"
            paths = (first, second, first, absent, root, root / "absent" / "mesh.obj", first / "mesh.obj")
            expected = {str(path): path.resolve() for path in paths if path.is_file()}
            with scene_io_session():
                self.assertEqual(expected, resolve_scene_existing_files(paths))
                if os.name == "nt":
                    different_case = root / "FIRST.OBJ"
                    self.assertEqual({str(different_case): different_case.resolve()},
                                     resolve_scene_existing_files((different_case,)))
                    trailing_dot = root / "First.obj."
                    self.assertEqual({str(trailing_dot): trailing_dot.resolve()},
                                     resolve_scene_existing_files((trailing_dot,)))
                first.unlink()
                absent.write_bytes(b"new payload")
                self.assertEqual({str(second): second.resolve(), str(absent): absent.resolve()},
                                 resolve_scene_existing_files(paths))

    def test_batch_payload_directory_alias_retarget_preserves_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first" / "leaf"
            second = root / "second" / "leaf"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            for target in (first, second):
                (target / "mesh.obj").write_bytes(b"mesh")
                (target.parent / "sibling.obj").write_bytes(b"sibling")
            alias = root / "alias"
            paths = (alias / "mesh.obj", alias / ".." / "sibling.obj", alias)
            with scene_io_session() as session:
                for target in (first, second):
                    if os.name == "nt":
                        result = subprocess.run(("cmd", "/c", "mklink", "/J", str(alias), str(target)),
                                                capture_output=True, text=True, check=True)
                        self.assertEqual(0, result.returncode)
                    else:
                        alias.symlink_to(target, target_is_directory=True)
                    try:
                        expected = {str(path): path.resolve() for path in paths if path.resolve().is_file()}
                        self.assertEqual(expected, resolve_scene_existing_files(paths))
                        for path in paths:
                            self.assertNotIn(os.path.abspath(path), session.resolutions)
                    finally:
                        if os.name == "nt":
                            alias.rmdir()
                        else:
                            alias.unlink()

    def test_batch_payload_file_symlink_resolves_target_and_rechecks_retarget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.obj"
            second = root / "second.obj"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            alias = root / "alias.obj"
            for target in (first, second):
                try:
                    alias.symlink_to(target)
                except OSError as error:
                    if os.name == "nt" and error.winerror == 1314:
                        self.skipTest("File symlink creation requires Windows privilege")
                    raise
                self.assertEqual({str(alias): alias.resolve()}, resolve_scene_existing_files((alias,)))
                alias.unlink()

    def test_batch_payload_relative_paths_use_the_actual_process_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mesh.obj").write_bytes(b"mesh")
            source = Path(__file__).resolve().parent.parent / "src"
            code = ("from pathlib import Path; from endfield_scene_io import resolve_scene_existing_files; "
                    "p = Path('mesh.obj'); "
                    "assert resolve_scene_existing_files((p, Path('missing.obj'))) == {str(p): p.resolve()}")
            subprocess.run((sys.executable, "-c", code), cwd=root,
                           env={**os.environ, "PYTHONPATH": str(source)}, check=True, capture_output=True)

    def test_estimator_counts_shared_records_proxy_backing_and_path_storage(self) -> None:
        path = Path(__file__)
        shared = ("provider", b"payload", 257, 0.5, None)
        providers = MappingProxyType({"container": shared})
        record = FrozenCacheValue(providers)
        graph = (record, record, path, shared)
        record_fields = vars(record)
        objects = (graph, record, record_fields, *record_fields.keys(), providers,
                   *providers.keys(), shared, *shared, path)
        unique_objects = {id(item): item for item in objects}
        expected = (sum(sys.getsizeof(item) for item in unique_objects.values())
                    + 64 + 16 * len(providers) + sys.getsizeof(str(path)))
        seen: set[int] = set()
        self.assertEqual(expected, _estimate_cached_value(graph, seen))
        self.assertEqual(set(), seen)
        covered = set(unique_objects)
        self.assertEqual(0, _estimate_cached_value(graph, covered))
        self.assertEqual(set(unique_objects), covered)
        for maximum in (expected, expected + 1):
            self.assertEqual(expected, _estimate_cached_value_bounded(graph, seen, maximum))
        for maximum in (0, expected // 2, expected - 1):
            measured = _estimate_cached_value_bounded(graph, seen, maximum)
            self.assertGreater(measured, maximum)
            self.assertLessEqual(measured, expected)
        self.assertEqual(0, _estimate_cached_value_bounded(graph, covered, 0))
        self.assertEqual(set(), seen)
        self.assertEqual(set(unique_objects), covered)
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            _estimate_cached_value_bounded(graph, seen, -1)

    def test_derived_cache_is_immutable_bounded_and_invalidated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("first", encoding="utf-8")
            slot = SceneSourceCacheSlot("test-derived", dict, 2, 2048)
            calls = 0

            def build() -> dict[str, object]:
                nonlocal calls
                calls += 1
                return {"value": path.read_text(encoding="utf-8"), "items": [1, 2]}

            with scene_io_session() as session:
                first = cached_scene_source_value(slot, (path,), (), build)
                self.assertIs(first, cached_scene_source_value(slot, (path,), (), build))
                self.assertEqual(1, calls)
                with self.assertRaises(ReadOnlySceneDataError):
                    first["items"].append(3)
                session.invalidate()
                second = cached_scene_source_value(slot, (path,), (), build)
                self.assertEqual("first", second["value"])
                self.assertEqual(2, calls)
                self.assertEqual(1, session.statistics()["source_cache_hits"])
                self.assertEqual(2, session.statistics()["source_cache_misses"])
                self.assertGreater(session.statistics()["estimated_source_cache_bytes"], 0)

    def test_derived_cache_detects_same_size_rewrite_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("first", encoding="utf-8")
            slot = SceneSourceCacheSlot("test-stamped", str, 2, 64)
            with scene_io_session() as session:
                for index in range(200):
                    with self.subTest(rewrite=index):
                        path.write_text("first", encoding="utf-8")
                        self.assertEqual("first", cached_scene_source_value(
                            slot, (path,), (), lambda: path.read_text(encoding="utf-8")))
                        stamp = path.stat()
                        path.write_text("other", encoding="utf-8")
                        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                        self.assertEqual("other", cached_scene_source_value(
                            slot, (path,), (), lambda: path.read_text(encoding="utf-8")))
                self.assertEqual(400, session.statistics()["source_cache_misses"])
                self.assertEqual(0, session.statistics()["source_cache_hits"])

    @unittest.skipUnless(os.name == "nt", "Windows write-exclusion version handle")
    def test_derived_cache_rejects_a_persistent_source_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.bin"
            path.write_bytes(b"first")
            slot = SceneSourceCacheSlot("test-active-writer", bytes, 1, 128)
            with scene_io_session():
                self.assertEqual(b"first", cached_scene_source_value(slot, (path,), (), path.read_bytes))
                with path.open("r+b", buffering=0) as writer:
                    for content in (b"other", b"third"):
                        writer.seek(0)
                        writer.write(content)
                        with self.assertRaisesRegex(SceneInputChangedError, "open for writing"):
                            cached_scene_source_value(slot, (path,), (), path.read_bytes)
                self.assertEqual(b"third", cached_scene_source_value(slot, (path,), (), path.read_bytes))

    def test_derived_cache_rejects_source_change_during_factory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("first", encoding="utf-8")
            slot = SceneSourceCacheSlot("test-changing", str, 1, 128)

            def change_during_read() -> str:
                value = path.read_text(encoding="utf-8")
                path.write_text("other", encoding="utf-8")
                return value

            with scene_io_session() as session:
                with self.assertRaisesRegex(SceneInputChangedError, "changed during derived read"):
                    cached_scene_source_value(slot, (path,), (), change_during_read)
                self.assertEqual(0, session.statistics()["retained_source_cache_entries"])
                self.assertEqual("other", cached_scene_source_value(
                    slot, (path,), (), lambda: path.read_text(encoding="utf-8")))

    def test_derived_cache_does_not_retain_values_over_slot_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("source", encoding="utf-8")
            slot = SceneSourceCacheSlot("test-large", bytes, 1, 4)
            calls = 0

            def build() -> bytes:
                nonlocal calls
                calls += 1
                return path.read_bytes()

            with scene_io_session() as session:
                self.assertEqual(b"source", cached_scene_source_value(slot, (path,), (), build))
                self.assertEqual(b"source", cached_scene_source_value(slot, (path,), (), build))
                self.assertEqual(2, calls)
                self.assertEqual(0, session.statistics()["retained_source_cache_entries"])

    def test_derived_cache_enforces_slot_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("source", encoding="utf-8")
            slot = SceneSourceCacheSlot("test-slot-budget", bytes, 4, 60)
            with scene_io_session() as session:
                cached_scene_source_value(slot, (path,), ("first",), lambda: b"one!")
                cached_scene_source_value(slot, (path,), ("second",), lambda: b"two!")
                self.assertEqual(1, session.statistics()["retained_source_cache_entries"])
                self.assertLessEqual(session.statistics()["estimated_source_cache_bytes"], 60)

    def test_derived_cache_indexes_preserve_interleaved_slot_lru_and_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.bin"
            path.write_bytes(b"first")
            slot = SceneSourceCacheSlot("test-slot-order", bytes, 2, 128)
            other = SceneSourceCacheSlot("test-other-slot", bytes, 1, 128)
            with scene_io_session() as session:
                cached_scene_source_value(slot, (path,), ("A",), lambda: path.read_bytes() + b"A")
                cached_scene_source_value(slot, (path,), ("B",), lambda: path.read_bytes() + b"B")
                cached_scene_source_value(other, (path,), (), path.read_bytes)
                cached_scene_source_value(slot, (path,), ("A",), lambda: path.read_bytes() + b"A")
                cached_scene_source_value(slot, (path,), ("C",), lambda: path.read_bytes() + b"C")
                self.assertEqual([("A",), ("C",)], [key[3] for key in session.source_cache_slot_keys[slot.name]])
                self.assertEqual(3, len(session.source_cache))
                self.assertEqual(session.estimated_source_cache_bytes, sum(session.source_cache_slot_bytes.values()))
                self.assertEqual(1, session.statistics()["source_cache_hits"])
                self.assertEqual(1, session.statistics()["source_cache_evictions"])
                path.write_bytes(b"other")
                self.assertEqual(b"otherA", cached_scene_source_value(
                    slot, (path,), ("A",), lambda: path.read_bytes() + b"A"))
                self.assertEqual(b"otherC", cached_scene_source_value(
                    slot, (path,), ("C",), lambda: path.read_bytes() + b"C"))
                self.assertEqual(session.estimated_source_cache_bytes, sum(session.source_cache_slot_bytes.values()))
                session.invalidate()
                self.assertFalse(session.source_cache_slot_keys)
                self.assertFalse(session.source_cache_slot_bytes)
                self.assertEqual(0, session.estimated_source_cache_bytes)

    def test_derived_cache_global_budget_evicts_oldest_across_slot_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.bin"
            path.write_bytes(b"source")
            first = SceneSourceCacheSlot("test-global-first", bytes, 1, _SOURCE_CACHE_BYTE_BUDGET)
            second = SceneSourceCacheSlot("test-global-second", bytes, 1, _SOURCE_CACHE_BYTE_BUDGET)
            size = _SOURCE_CACHE_BYTE_BUDGET // 2
            with scene_io_session() as session:
                cached_scene_source_value(first, (path,), (), lambda: bytes(size))
                cached_scene_source_value(second, (path,), (), lambda: bytes(size))
                self.assertEqual(1, len(session.source_cache))
                self.assertNotIn(first.name, session.source_cache_slot_keys)
                self.assertNotIn(first.name, session.source_cache_slot_bytes)
                self.assertIn(second.name, session.source_cache_slot_keys)
                self.assertEqual(1, session.statistics()["source_cache_evictions"])
                self.assertLessEqual(session.estimated_source_cache_bytes, _SOURCE_CACHE_BYTE_BUDGET)
                self.assertEqual(session.estimated_source_cache_bytes, sum(session.source_cache_slot_bytes.values()))

    def test_retained_size_estimate_counts_dataclass_mapping_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text("source", encoding="utf-8")
            slot = SceneSourceCacheSlot("test-frozen-record", FrozenCacheValue, 1, 128)
            calls = 0

            def build() -> FrozenCacheValue:
                nonlocal calls
                calls += 1
                return FrozenCacheValue(MappingProxyType({"large": "x" * 512}))

            with scene_io_session() as session:
                cached_scene_source_value(slot, (path,), (), build)
                cached_scene_source_value(slot, (path,), (), build)
                self.assertEqual(2, calls)
                self.assertEqual(0, session.statistics()["retained_source_cache_entries"])

    def test_error_exit_releases_scope_and_integrity_hash_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            source_document(path, 1)
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                with scene_io_session() as session:
                    read_scene_manifest(path)
                    before = describe_file(path)
                    stamp = path.stat()
                    source_document(path, 2)
                    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                    after = describe_file(path)
                    self.assertEqual(before["bytes"], after["bytes"])
                    self.assertNotEqual(before["sha256"], after["sha256"])
                    raise RuntimeError("cancelled")
            self.assertTrue(session.closed)
            self.assertFalse(session.snapshots)
            self.assertFalse(session.versions)
            self.assertFalse(session.resolutions)

    def test_shared_parse_rejects_nested_mutation_and_releases_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            source_document(path, 1)
            with scene_io_session() as session:
                first = read_scene_manifest(path)
                self.assertIs(first, read_scene_manifest(path))
                self.assertIsInstance(first, dict)
                materials = first["Materials"]
                with self.assertRaises(ReadOnlySceneDataError):
                    materials.append({})
                with self.assertRaises(ReadOnlySceneDataError):
                    materials[0]["Floats"]["value"] = 2
                with self.assertRaises(ReadOnlySceneDataError):
                    materials[0]["Colors"]["color"][0] = 2
                with self.assertRaises(ReadOnlySceneDataError):
                    materials[0]["Colors"].update({"other": []})
                self.assertEqual(json.loads(path.read_text()), first)
                self.assertEqual(1, session.statistics()["manifest_reads"])
                self.assertEqual(1, session.statistics()["manifest_hits"])
            self.assertTrue(session.closed)
            self.assertFalse(session.snapshots)
            self.assertFalse(session.resolutions)
            mutable = read_scene_manifest(path)
            mutable["Materials"][0]["Floats"]["value"] = 5
            self.assertEqual(1, read_scene_manifest(path)["Materials"][0]["Floats"]["value"])

    def test_file_update_explicit_invalidation_and_new_manifest_are_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            source_document(path, 1)
            with scene_io_session() as session:
                first = read_scene_manifest(path)
                stamp = path.stat()
                source_document(path, 2)
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
                second = read_scene_manifest(path)
                self.assertEqual(2, second["Materials"][0]["Floats"]["value"])
                self.assertIsNot(first, second)
                self.assertEqual(1, session.statistics()["invalidations"])
                added = Path(directory) / "supplement.json"
                source_document(added, 3)
                self.assertEqual(3, read_scene_manifest(added)["Materials"][0]["Floats"]["value"])
                session.invalidate()
                self.assertFalse(session.snapshots)
                self.assertIsNot(second, read_scene_manifest(path))
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    read_scene_manifest(path)

    def test_nested_and_thread_scopes_do_not_share_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            source_document(path, 1)
            with scene_io_session() as outer:
                original = read_scene_manifest(path)
                with scene_io_session() as inner:
                    self.assertIsNot(original, read_scene_manifest(path))
                    self.assertEqual(1, inner.statistics()["manifest_reads"])
                self.assertIs(original, read_scene_manifest(path))

                def read_worker() -> object:
                    with scene_io_session() as worker:
                        result = read_scene_manifest(path)
                        self.assertEqual(1, worker.statistics()["manifest_reads"])
                        return result

                with ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertIsNot(original, pool.submit(read_worker).result())
                    copied = copy_context()
                    with self.assertRaisesRegex(RuntimeError, "across threads"):
                        pool.submit(copied.run, read_scene_manifest, path).result()
                self.assertEqual(1, outer.statistics()["manifest_reads"])

    def test_physical_aliases_share_parse_and_lru_count_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            alias = Path(directory) / "hardlink.json"
            source_document(path, 1)
            os.link(path, alias)
            with scene_io_session() as session:
                first = read_scene_manifest(path)
                self.assertIs(first, read_scene_manifest(alias))
                if os.name == "nt":
                    self.assertIs(first, read_scene_manifest(Path("\\\\?\\" + str(path.resolve()))))
                for index in range(257):
                    extra = Path(directory) / f"{index}.json"
                    source_document(extra, index)
                    read_scene_manifest(extra)
                self.assertEqual(256, session.statistics()["retained_manifests"])
                self.assertGreater(session.statistics()["manifest_evictions"], 0)

    def test_resolution_keeps_live_payload_checks_and_original_decode_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.png"
            with scene_io_session() as session:
                resolved = resolve_scene_read_path(path)
                self.assertFalse(resolved.is_file())
                path.write_bytes(b"payload")
                self.assertTrue(resolve_scene_read_path(path).is_file())
                path.unlink()
                self.assertFalse(resolve_scene_read_path(path).is_file())
                self.assertEqual(2, session.statistics()["resolution_hits"])
                path.write_text("{bad json", encoding="utf-8")
                with self.assertRaises(json.JSONDecodeError):
                    read_scene_manifest(path)
                self.assertFalse(session.snapshots)


if __name__ == "__main__":
    unittest.main()
