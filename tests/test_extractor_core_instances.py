"""Decode world bounds from the same FlatBuffer column layout as InitChunkData."""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extractor_core.instances import (
    InstanceSourceGenerationError,
    build_instance_database,
    iter_entities,
    validate_instance_source_generation,
)
from extractor_core.vfs import VfsChunk, VfsFile, VfsGroup


def chunk_with_components(components: tuple[tuple[int, bytes], ...]) -> bytes:
    data = bytearray(512)
    struct.pack_into("<I", data, 0, 32)
    struct.pack_into("<HH8H", data, 8, 20, 36, 0, 0, 0, 0, 0, 0, 28, 32)
    struct.pack_into("<iII", data, 32, 24, 0, 0)
    struct.pack_into("<II", data, 60, 20, 32)
    struct.pack_into("<II", data, 80, 1, 44)
    struct.pack_into("<II", data, 96, 1, 60)
    struct.pack_into("<HHH", data, 112, 6, 8, 4)
    struct.pack_into("<iI", data, 128, 16, 108)
    struct.pack_into("<HH5H", data, 140, 14, 20, 0, 8, 0, 12, 16)
    struct.pack_into("<iIII", data, 160, 20, 0, 1, 0)
    struct.pack_into("<II", data, 172, 84, 32)
    struct.pack_into("<HHH", data, 192, 6, 8, 4)
    struct.pack_into("<iI", data, 208, 16, 172)
    struct.pack_into("<II", data, 240, 1, 42)
    struct.pack_into("<I", data, 256, len(components))
    payload = b"".join(raw for _, raw in components)
    for index, (type_id, raw) in enumerate(components):
        struct.pack_into("<HHI", data, 260 + index * 8, type_id, len(raw), 0)
    struct.pack_into("<I", data, 384, len(payload))
    data[388:388 + len(payload)] = payload
    return bytes(data)


class InstanceWorldBoundsTests(unittest.TestCase):
    def test_decodes_real_flatbuffer_columns(self) -> None:
        matrix = struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -6.2, 203, 216.96, 1)
        values = (-46.0, 259.0, 283.0, 26.0, 57.0, 31.0)
        components = tuple((type_id, struct.pack("<f", value)) for type_id, value in enumerate(values))
        row, = iter_entities(chunk_with_components((*components, (18, matrix))))
        self.assertEqual(row["entity_id"], 42)
        self.assertEqual(row["world_bounds"], (-72.0, -20.0, 202.0, 316.0, 252.0, 314.0))
        row, = iter_entities(chunk_with_components(((18, matrix),)))
        self.assertIsNone(row["world_bounds"])

    def test_rejects_incomplete_or_invalid_bounds(self) -> None:
        matrix = struct.pack("<16f", *(1.0 for _ in range(16)))
        values = tuple((type_id, struct.pack("<f", float(type_id + 1))) for type_id in range(6))
        for components, message in (
            ((*values[:5], (18, matrix)), "entity 42: missing world bounds components \\[5\\]"),
            ((*values[:3], (3, struct.pack("<ff", 4, 4)), *values[4:], (18, matrix)),
             "entity 42: world bounds component 3 has stride 8"),
            ((*values[:4], (4, struct.pack("<f", float("nan"))), *values[5:], (18, matrix)),
             "entity 42: invalid world bounds component 4"),
            ((*values[:5], (5, struct.pack("<f", -1)), (18, matrix)),
             "entity 42: invalid world bounds component 5"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                list(iter_entities(chunk_with_components(tuple(components))))

    def test_source_generation_matches_current_vfs_without_writing_database(self) -> None:
        map_id = "map02"
        virtual_path = "Data/Streaming/PC/map02/Streaming/InitChunkData_-10_-1_0_0.bytes"
        matrix = struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -6.2, 203, 216.96, 1)
        values = (-46.0, 259.0, 283.0, 26.0, 57.0, 31.0)
        clear = chunk_with_components((*tuple((type_id, struct.pack("<f", value))
                                            for type_id, value in enumerate(values)), (18, matrix)))
        self.assertEqual(len(clear), 512)
        packed = struct.pack("<I", len(clear)) + b"\x33\xff\xf2" + clear
        item = VfsFile(virtual_path, "a" * 32, "b" * 32,
                       hashlib.md5(packed).hexdigest().upper(), 0, len(packed), 11, False, None)
        group = VfsGroup(1, "static", "C3442D43", 1, len(packed), 11,
                         [VfsChunk("b" * 32, "c" * 32, len(packed), 11, [item])])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = root / "Game"
            vfs_root = game_root / "Endfield_Data" / "StreamingAssets" / "VFS"
            group_dir = vfs_root / "C3442D43"
            group_dir.mkdir(parents=True)
            (group_dir / ("b" * 32 + ".chk")).write_bytes(packed)
            database_path = root / "instances.sqlite"
            with patch("extractor_core.instances.load_group", return_value=group):
                build_instance_database(map_id, vfs_root, database_path)
            original_hash = hashlib.sha256(database_path.read_bytes()).hexdigest()

            with patch("extractor_core.instances.load_group", return_value=group):
                self.assertIsNone(validate_instance_source_generation(game_root, map_id, database_path))

            self.assertEqual(hashlib.sha256(database_path.read_bytes()).hexdigest(), original_hash)

            changed_item = VfsFile(virtual_path, "a" * 32, "d" * 32, "e" * 32, 0, len(packed), 11, False, None)
            changed_group = VfsGroup(1, "static", "C3442D43", 1, len(packed), 11,
                                     [VfsChunk("d" * 32, "e" * 32, len(packed), 11, [changed_item])])
            with patch("extractor_core.instances.load_group", return_value=changed_group):
                with self.assertRaises(InstanceSourceGenerationError) as caught:
                    validate_instance_source_generation(game_root, map_id, database_path)
            self.assertEqual(caught.exception.reason, "source_changed")

    def test_legacy_instance_database_is_explicitly_unverifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "instances.sqlite"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE chunks (virtual_path TEXT PRIMARY KEY, compressed_bytes INTEGER NOT NULL, "
                "clear_bytes INTEGER NOT NULL, parsed_instances INTEGER NOT NULL);"
            )
            connection.commit()
            connection.close()
            with patch("extractor_core.instances.load_group") as load_group:
                with self.assertRaises(InstanceSourceGenerationError) as caught:
                    validate_instance_source_generation(Path(temporary), "map02", database_path)
            self.assertEqual(caught.exception.reason, "legacy")
            load_group.assert_not_called()


if __name__ == "__main__":
    unittest.main()
