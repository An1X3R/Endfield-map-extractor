"""Check stamped inventory inputs and strict dynamic path-resolution semantics."""
from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest

from endfield_scene_io import (
    SceneInputChangedError,
    _estimate_cached_value_bounded,
    single_scene_plan_io_session,
)
from extractor_core.dynamic_renderers import read_dynamic_scene_renderers_for_scene
from extractor_core.effects import (
    GROUP_HASH, EffectDecodeError, _dynamic_inventory_qualifiers, _dynamic_inventory_sources,
    _require_dynamic_inventory_sources, _resolve_hashes, read_dynamic_scene_effects_for_scene,
)
from extractor_core.resources import BootstrapExtractionError
from extractor_core.projected_decals import build_path_id_index, build_path_id_index_views_cancellable


def _check_cancelled() -> None:
    return None


def _write_path_hash(path: Path, rows: tuple[tuple[bytes, str], ...]) -> None:
    records: list[bytes] = []
    strings = bytearray()
    for identifier, value in rows:
        encoded = value.encode("utf-16le")
        records.append(identifier + struct.pack("<QQ", len(strings), 0))
        strings.extend(struct.pack("<I", len(encoded)) + encoded + b"\0\0")
    path.write_bytes(struct.pack("<II", 8 + 24 * len(rows), len(rows)) + b"".join(records) + strings)


class DynamicInventoryTests(unittest.TestCase):
    def test_path_index_preserves_overlapping_candidates_and_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "StringPathHash.bin"
            strings = bytearray(b"\0" * 8)
            offsets: list[int] = []
            for text in ("assets/first.asset", "packages/second.asset", "data/third.asset"):
                offsets.append(len(strings))
                encoded = text.encode("utf-16le")
                strings.extend(struct.pack("<I", len(encoded)) + encoded + b"\0\0")
            identifier = 0xFEDCBA9876543210
            path.write_bytes(struct.pack("<II6Q", 56, 2, identifier, offsets[0], offsets[1],
                                         identifier, offsets[2], 0) + strings)
            result = build_path_id_index(path)
            self.assertEqual({identifier.to_bytes(8, "little"): ("assets/first.asset", "data/third.asset"),
                              offsets[0].to_bytes(8, "little"): ("packages/second.asset",)}, result)
            self.assertEqual([identifier.to_bytes(8, "little"), offsets[0].to_bytes(8, "little")], list(result))

    def test_group_inputs_include_metadata_and_every_chunk_membership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            group = root / GROUP_HASH
            group.mkdir()
            path_hash = root / "StringPathHash.bin"
            first, second = group / "A.chk", group / "B.chk"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            sources = _dynamic_inventory_sources(root, path_hash)
            self.assertEqual((path_hash, group / f"{GROUP_HASH}.blc", first, second), sources)
            qualifiers = _dynamic_inventory_qualifiers(("map01",), sources)
            self.assertEqual(("map01", *(str(path) for path in sources)), qualifiers)
            alias_directory = group / "alias"
            alias_directory.mkdir()
            same_target_path = alias_directory / ".." / first.name
            self.assertEqual(first.resolve(), same_target_path.resolve())
            self.assertNotEqual(_dynamic_inventory_qualifiers(("map01",), (first,)),
                                _dynamic_inventory_qualifiers(("map01",), (same_target_path,)))
            renamed = group / "C.chk"
            second.rename(renamed)
            renamed_sources = _dynamic_inventory_sources(root, path_hash)
            self.assertNotEqual(qualifiers, _dynamic_inventory_qualifiers(("map01",), renamed_sources))
            renamed.rename(second)
            self.assertEqual(qualifiers, _dynamic_inventory_qualifiers(("map01",), sources))
            _require_dynamic_inventory_sources(root, path_hash, sources)
            second.unlink()
            with self.assertRaisesRegex(SceneInputChangedError, "membership changed"):
                _require_dynamic_inventory_sources(root, path_hash, sources)
            second.write_bytes(b"second")
            (group / "C.chk").write_bytes(b"new")
            with self.assertRaisesRegex(SceneInputChangedError, "membership changed"):
                _require_dynamic_inventory_sources(root, path_hash, sources)

    def test_resolver_preserves_prefix_ambiguity_and_complete_table_validation(self) -> None:
        identifier = bytes.fromhex("1234567890abcdef")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "StringPathHash.bin"
            _write_path_hash(path, ((identifier, "packages/example.asset"),))
            resolved = _resolve_hashes(path, frozenset((identifier,)), _check_cancelled,
                                       ("assets/", "data/", "packages/"))
            self.assertEqual({identifier: "packages/example.asset"}, resolved)
            with self.assertRaisesRegex(EffectDecodeError, "unresolvedCount=1"):
                _resolve_hashes(path, frozenset((identifier,)), _check_cancelled, ("assets/", "data/"))
            _write_path_hash(path, ((identifier, "assets/a.prefab"), (identifier, "assets/b.prefab")))
            with self.assertRaisesRegex(EffectDecodeError, "ambiguousCount=1"):
                _resolve_hashes(path, frozenset((identifier,)), _check_cancelled, ("assets/", "data/"))
            _write_path_hash(path, ((identifier, "assets/a.prefab"),))
            with path.open("ab") as handle:
                handle.write(b"trailing")
            with self.assertRaisesRegex(BootstrapExtractionError, "trailing bytes"):
                _resolve_hashes(path, frozenset((identifier,)), _check_cancelled, ("assets/", "data/"))

    def test_effect_resolver_rejects_valid_string_at_interior_offset(self) -> None:
        identifier = bytes.fromhex("1234567890abcdef")
        interior_path = "assets/interior.prefab"
        encoded_path = interior_path.encode("utf-16le")
        embedded_record = struct.pack("<I", len(encoded_path)) + encoded_path + b"\0\0"
        record_payload = "x".encode("utf-16le") + embedded_record + "tail".encode("utf-16le")
        string_table = struct.pack("<I", len(record_payload)) + record_payload + b"\0\0"
        path_offset = 8 + 24

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "StringPathHash.bin"
            path.write_bytes(
                struct.pack("<II", path_offset, 1)
                + identifier
                + struct.pack("<QQ", 6, 0)
                + string_table
            )
            self.assertIn(interior_path, build_path_id_index(path)[identifier])
            with self.assertRaisesRegex(EffectDecodeError, "unresolvedCount=1"):
                _resolve_hashes(path, frozenset((identifier,)), _check_cancelled, ("assets/", "data/"))

    def test_resolver_reuses_stamped_index_with_original_path_spelling(self) -> None:
        first_id = bytes.fromhex("1020304050607080")
        second_id = bytes.fromhex("2030405060708090")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "StringPathHash.bin"
            _write_path_hash(path, ((first_id, "Assets/Upper/First.prefab"),
                                    (second_id, "packages/Second.prefab")))
            with single_scene_plan_io_session() as session:
                index = build_path_id_index(path)
                self.assertEqual(("Assets/Upper/First.prefab",), index[first_id])
                all_paths = _resolve_hashes(path, frozenset((first_id, second_id)), _check_cancelled,
                                            ("assets/", "data/", "packages/"))
                self.assertEqual({first_id: "Assets/Upper/First.prefab",
                                  second_id: "packages/Second.prefab"}, all_paths)
                asset_paths = _resolve_hashes(path, frozenset((first_id,)), _check_cancelled,
                                              ("assets/", "data/"))
                self.assertEqual({first_id: "Assets/Upper/First.prefab"}, asset_paths)
                _write_path_hash(path, ((first_id, "Data/Changed.prefab"),
                                        (second_id, "packages/Second.prefab")))
                updated_index = build_path_id_index(path)
                self.assertEqual(("Data/Changed.prefab",), updated_index[first_id])
                updated = _resolve_hashes(path, frozenset((first_id,)), _check_cancelled,
                                          ("assets/", "data/"))
                self.assertEqual({first_id: "Data/Changed.prefab"}, updated)
                self.assertGreaterEqual(session.statistics()["source_cache_hits"], 3)
            self.assertEqual(0, len(session.source_cache))

    def test_path_index_size_estimate_matches_general_cache_walk(self) -> None:
        interior_id = bytes.fromhex("1020304050607080")
        strict_id = bytes.fromhex("2030405060708090")
        embedded_path = "assets/InteriorCandidate.prefab"
        encoded_path = embedded_path.encode("utf-16le")
        embedded_record = struct.pack("<I", len(encoded_path)) + encoded_path + b"\0\0"
        record_payload = "x".encode("utf-16le") + embedded_record + "tail".encode("utf-16le")
        first_string = struct.pack("<I", len(record_payload)) + record_payload + b"\0\0"
        second_path = "data/StrictRecord.prefab"
        encoded_second = second_path.encode("utf-16le")
        second_string = struct.pack("<I", len(encoded_second)) + encoded_second + b"\0\0"
        strings_offset = 8 + 2 * 24
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "StringPathHash.bin"
            path.write_bytes(
                struct.pack("<II", strings_offset, 2)
                + interior_id + struct.pack("<QQ", 6, 0)
                + strict_id + struct.pack("<QQ", len(first_string), 0)
                + first_string + second_string
            )
            with single_scene_plan_io_session() as session:
                views = build_path_id_index_views_cancellable(path, _check_cancelled)
                self.assertIn(embedded_path, views.permissive()[interior_id])
                self.assertEqual((), views.strict_paths(interior_id))
                self.assertEqual((second_path,), views.strict_paths(strict_id))
                expected_size = _estimate_cached_value_bounded(views, set(), None)
                self.assertEqual(expected_size, session.statistics()["estimated_source_cache_bytes"])
            self.assertEqual(0, len(session.source_cache))

    def test_cancelled_inventory_request_stops_before_reading_inputs(self) -> None:
        def cancelled() -> None:
            raise InterruptedError("inventory cancelled")

        root = Path("missing-input")
        for reader in (read_dynamic_scene_effects_for_scene, read_dynamic_scene_renderers_for_scene):
            with self.subTest(reader=reader.__name__):
                with self.assertRaisesRegex(InterruptedError, "inventory cancelled"):
                    reader(root, root / "StringPathHash.bin", "map01", cancelled)


if __name__ == "__main__":
    unittest.main()
