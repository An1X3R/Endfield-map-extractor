"""Check stamped inventory inputs and strict dynamic path-resolution semantics."""
from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest

from endfield_scene_io import SceneInputChangedError
from extractor_core.dynamic_renderers import read_dynamic_scene_renderers_for_scene
from extractor_core.effects import (
    GROUP_HASH, EffectDecodeError, _dynamic_inventory_qualifiers, _dynamic_inventory_sources,
    _require_dynamic_inventory_sources, _resolve_hashes, read_dynamic_scene_effects_for_scene,
)
from extractor_core.resources import BootstrapExtractionError
from extractor_core.projected_decals import build_path_id_index


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
