"""Check physical identity, source binding and prepared-cache integrity."""
from __future__ import annotations

import json
from pathlib import Path
import struct
import tempfile
import unittest

from lz4.block import compress

from endfield_instanced_catalog import (
    InstancedSceneError, _absolute_inputs, _art_owner, _assemble_catalog,
    _chunk_source_evidence,
    _prepared_from_marker, _validate_catalog,
)
from endfield_scene_io import SceneInputChangedError, _file_stamp
from endfield_scene_resources import describe_file
from extractor_core.scene_tables import SceneTableError, read_scene_table
from extractor_core.scene_string_pool import SceneStringPoolError, _pool_lz4, read_scene_string_pool
from extractor_core.vfs import VfsFile


def string_table(name: str, rows: tuple[tuple[str, str], ...]) -> bytes:
    """Create a native map-of-strings table without replacing the real reader."""
    root = b"\x0a" + name.encode() + b"\0\x07\x07"
    values_at = 16 + len(root)
    data = bytearray(struct.pack("<iii", 12, 16, values_at) + struct.pack("<i", 0) + root)
    data.extend(struct.pack("<i", len(rows)) + b"\0" * (8 * len(rows)))
    pointers_at = len(data)
    data.extend(b"\0" * (8 * len(rows)))
    for index, pair in enumerate(rows):
        for field, text in enumerate(pair):
            struct.pack_into("<i", data, pointers_at + index * 8 + field * 4, len(data))
            data.extend(text.encode() + b"\0")
    return bytes(data)


class InstancedSceneCatalogTests(unittest.TestCase):
    def test_chunk_source_identity_uses_existing_vfs_and_file_stamp_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group = root / "C3442D43"
            group.mkdir()
            source = group / ("c" * 32 + ".chk")
            source.write_bytes(b"first")
            item = VfsFile("Data/Streaming/PC/indie_one/Streaming/InitChunkData_0_0_0_0.bytes",
                           "a" * 32, "c" * 32, "b" * 32, 0, 5, 0, False, None)
            first = _chunk_source_evidence(root, "C3442D43", (item,), {source: _file_stamp(source)})
            source.write_bytes(b"other")
            second = _chunk_source_evidence(root, "C3442D43", (item,), {source: _file_stamp(source)})
            self.assertNotEqual(first, second)
            self.assertEqual(first[0]["data_md5"], "B" * 32)
            self.assertEqual(first[0]["virtual_path"], item.file_name)

    def test_current_pool_wire_format_and_rejected_corruption(self) -> None:
        entries = tuple(struct.pack("<i", len(value)) + value for value in
                        (b"indie_test/indie_test_art_streaming.asset", b"", b"another"))
        offsets = (0, len(entries[0]), len(entries[0]) + len(entries[1]))
        clear = struct.pack("<4i", 3, *offsets) + b"".join(entries)
        packed = compress(clear, store_size=False)
        header = struct.pack("<4s5i", b"USPC", 1, 1, 3, len(clear), len(packed))
        source = struct.pack("<i", 0) + header + packed
        result = read_scene_string_pool({"Data/Json/StringPool/StringPool0.byte": source}.__getitem__)
        self.assertEqual(result, {0: "indie_test/indie_test_art_streaming.asset", 1: "", 2: "another"})
        self.assertEqual(_art_owner(b"\x12\x03" + struct.pack("<i", 0), "pooled", result), "indie_test")
        for damaged in (source[:-1], source[:8] + struct.pack("<i", 99) + source[12:]):
            with self.assertRaisesRegex(SceneStringPoolError, "header"):
                read_scene_string_pool({"Data/Json/StringPool/StringPool0.byte": damaged}.__getitem__)
        invalid_clear = clear[:4] + struct.pack("<i", -1) + clear[8:]
        invalid_packed = compress(invalid_clear, store_size=False)
        invalid = struct.pack("<i4s5i", 0, b"USPC", 1, 1, 3, len(invalid_clear), len(invalid_packed)) + invalid_packed
        with self.assertRaisesRegex(SceneStringPoolError, "offsets"):
            read_scene_string_pool({"Data/Json/StringPool/StringPool0.byte": invalid}.__getitem__)

    def test_source_pool_terminal_lz4_match_and_strict_bounds(self) -> None:
        self.assertEqual(_pool_lz4(b"\x13a\x01\x00", 8), b"aaaaaaaa")
        self.assertEqual(_pool_lz4(b"\x13a\x01\x00\x30bbb", 11), b"aaaaaaaabbb")
        with self.assertRaisesRegex(SceneStringPoolError, "match exceeds bounds"):
            _pool_lz4(b"\x13a\x00\x00", 8)
        with self.assertRaisesRegex(SceneStringPoolError, "match exceeds bounds"):
            _pool_lz4(b"\x13a\x01\x00", 7)
        with self.assertRaisesRegex(SceneStringPoolError, "Truncated"):
            _pool_lz4(b"\xf0\xff", 8)

    def test_native_table_roundtrip_and_wrong_root_offset_duplicate_failures(self) -> None:
        payload = string_table("Names", (("office", "总桩办公室"), ("room", "地下空间")))
        self.assertEqual(read_scene_table(payload, "Names"), {"office": "总桩办公室", "room": "地下空间"})
        with self.assertRaisesRegex(SceneTableError, "Unexpected scene table root"):
            read_scene_table(payload, "Wrong")
        truncated = struct.pack("<iii", 12, 16, 999999) + payload[12:]
        with self.assertRaisesRegex(SceneTableError, "offset outside payload"):
            read_scene_table(truncated, "Names")
        with self.assertRaisesRegex(SceneTableError, "Duplicate"):
            read_scene_table(string_table("Names", (("same", "one"), ("same", "two"))), "Names")

    def test_art_root_reference_and_relocated_inputs_preserve_original(self) -> None:
        reference = b"indie_test/indie_test_art_streaming.asset"
        self.assertEqual(_art_owner(b"\x12\x03" + struct.pack("<i", len(reference)) + reference, "config", None), "indie_test")
        with self.assertRaisesRegex(InstancedSceneError, "art reference length"):
            _art_owner(b"\x12\x03" + struct.pack("<i", -1), "config", None)
        self.assertEqual(_art_owner(b"\x12\x03" + struct.pack("<i", 35744), "pooled", {35744: reference.decode()}), "indie_test")
        with self.assertRaisesRegex(InstancedSceneError, "string ID is absent"):
            _art_owner(b"\x12\x03" + struct.pack("<i", 35744), "pooled", {})
        source = {"format": "EndfieldRuntimeConfig/1", "map": {"instances": "instances.sqlite", "terrainSurfaces": ["terrain"]}}
        with tempfile.TemporaryDirectory() as temporary:
            copied = _absolute_inputs(source, Path(temporary), "")
            self.assertEqual(copied["map"]["instances"], str(Path(temporary) / "instances.sqlite"))
            self.assertEqual(copied["map"]["terrainSurfaces"], [str(Path(temporary) / "terrain")])
        self.assertEqual(source["map"]["instances"], "instances.sqlite")

    def test_physical_dedup_primary_scene_binding_null_counts_and_unique_group_labels(self) -> None:
        owners = {"logical_one": "indie_one", "logical_alias": "indie_one", "logical_two": "indie_two", "map01_lv001": "map01"}
        descriptions = {key: {"showName": {"id": identifier, "text": ""}} for key, identifier in
                        (("logical_one", 1), ("logical_alias", 1), ("logical_two", 2))}
        dungeons = {"dungeon_one": {"sceneId": "logical_one", "levelId": "logical_two", "dungeonCategory": "category_one", "dungeonName": {"id": 1, "text": ""}},
                    "dungeon_two": {"sceneId": "", "levelId": "logical_two", "dungeonCategory": "category_two", "dungeonName": {"id": 2, "text": ""}}}
        types = {name: {"enemyInfoTitle": {"id": 3, "text": ""}} for name in ("category_one", "category_two")}
        catalog = _assemble_catalog(owners, {"indie_one": 4, "indie_two": 2, "indie_unregistered": 1, "devonly_gym": 1},
                                    descriptions, dungeons, types, {"1": "Office", "2": "Room", "3": "Shared title"}, {}, "a" * 64)
        self.assertEqual(len(catalog["scenes"]), 3)
        scenes = {row["scene_id"]: row for row in catalog["scenes"]}
        self.assertEqual(scenes["indie_one"]["logical_level_ids"], ["logical_alias", "logical_one"])
        self.assertEqual(scenes["indie_one"]["category_ids"], ["category_one"])
        self.assertEqual(scenes["indie_two"]["category_ids"], ["category_two"])
        self.assertTrue(scenes["indie_unregistered"]["export_capability"]["ready"])
        self.assertEqual(scenes["indie_unregistered"]["logical_level_ids"], [])
        self.assertEqual(scenes["indie_unregistered"]["name"], "indie_unregistered")
        self.assertTrue(any("without a logical level registration" in reason
                            for reason in scenes["indie_unregistered"]["limitations"]))
        self.assertTrue(all(value is None for value in scenes["indie_one"]["component_counts"].values()))
        self.assertEqual(len({group["name"] for group in catalog["groups"]}), len(catalog["groups"]))
        self.assertEqual(_validate_catalog(catalog), catalog)
        invalid = {**catalog, "scenes": [catalog["scenes"][0], catalog["scenes"][0]]}
        with self.assertRaisesRegex(InstancedSceneError, "Duplicate cached physical scene"):
            _validate_catalog(invalid)

    def test_dungeon_uses_explicit_art_owner_before_matching_physical_root(self) -> None:
        owners = {"indie_layer": "indie_owner", "indie_owner": "indie_owner", "indie_other": "indie_other"}
        dungeons = {"shared": {"sceneId": "indie_layer", "levelId": "indie_other",
                                "dungeonCategory": "story", "dungeonName": {"id": 1, "text": ""}},
                    "unregistered": {"sceneId": "indie_unregistered", "levelId": "",
                                     "dungeonCategory": "story", "dungeonName": {"id": 2, "text": ""}}}
        types = {"story": {"enemyInfoTitle": {"id": 3, "text": ""}}}
        roots = {"indie_layer": 2, "indie_owner": 4, "indie_other": 1, "indie_unregistered": 1}
        catalog = _assemble_catalog(owners, roots, {}, dungeons, types,
                                    {"1": "Shared art", "2": "Raw scene", "3": "Story"}, {}, "b" * 64)
        scenes = {row["scene_id"]: row for row in catalog["scenes"]}
        self.assertEqual(scenes["indie_owner"]["name"], "Shared art")
        self.assertEqual(scenes["indie_owner"]["category_ids"], ["story"])
        self.assertEqual(scenes["indie_owner"]["logical_level_ids"], ["indie_layer", "indie_owner"])
        self.assertEqual(scenes["indie_layer"]["category_ids"], [])
        self.assertEqual(scenes["indie_other"]["category_ids"], [])
        self.assertTrue(scenes["indie_layer"]["export_capability"]["ready"])
        self.assertTrue(any("does not include that owner's sources" in reason
                            for reason in scenes["indie_layer"]["limitations"]))
        self.assertIn({"table": "LevelConfig", "key": "indie_layer", "field": "_art_owner",
                       "value": "indie_owner", "i18n_id": None}, scenes["indie_layer"]["source_evidence"])
        self.assertTrue(any(row["table"] == "DungeonTable" and row["value"] == "indie_layer"
                            for row in scenes["indie_owner"]["source_evidence"]))
        self.assertEqual(scenes["indie_unregistered"]["name"], "Raw scene")
        self.assertEqual(scenes["indie_unregistered"]["category_ids"], ["story"])
        self.assertEqual(_validate_catalog(catalog), catalog)

    def test_prepared_marker_reuse_cancellation_and_missing_changed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {}
            for key in ("runtime", "state", "instances", "assets"):
                path = root / key
                path.write_bytes(key.encode())
                files[key] = describe_file(path)
            marker = root / "complete.json"
            marker.write_text(json.dumps({"format": "PreparedInstancedScene/1", "scene_id": "indie_one",
                                         "source_identity": "a" * 64, "input_hash": "inputs", "files": files,
                                         "bounds": {"xmin": 0, "xmax": 128, "zmin": 0, "zmax": 128},
                                         "limitations": ["whole scene"], "resource_status": "deferred_exact_preflight"}), encoding="utf-8")
            result = _prepared_from_marker(marker, "indie_one", "a" * 64, "inputs", lambda: None)
            self.assertIsNotNone(result)
            self.assertEqual(result.runtime_path, root / "runtime")
            self.assertEqual(result.limitations, ("whole scene",))
            self.assertIsNone(_prepared_from_marker(marker, "indie_one", "a" * 64, "changed inputs", lambda: None))

            original = json.loads(marker.read_text(encoding="utf-8"))
            invalid_bounds = (
                {"xmin": 0.5, "xmax": 128, "zmin": 0, "zmax": 128},
                {"xmin": float("nan"), "xmax": 128, "zmin": 0, "zmax": 128},
                {"xmin": 10 ** 400, "xmax": 128, "zmin": 0, "zmax": 128},
            )
            for bounds in invalid_bounds:
                marker.write_text(json.dumps({**original, "bounds": bounds}), encoding="utf-8")
                with self.assertRaisesRegex(InstancedSceneError, "bounds"):
                    _prepared_from_marker(marker, "indie_one", "a" * 64, "inputs", lambda: None)
            marker.write_text(json.dumps(original), encoding="utf-8")

            def cancel() -> None:
                raise InterruptedError("cancelled cache hit")

            with self.assertRaisesRegex(InterruptedError, "cancelled cache hit"):
                _prepared_from_marker(marker, "indie_one", "a" * 64, "inputs", cancel)
            (root / "instances").write_bytes(b"changed")
            with self.assertRaisesRegex(SceneInputChangedError, "artifact changed"):
                _prepared_from_marker(marker, "indie_one", "a" * 64, "inputs", lambda: None)
            (root / "instances").unlink()
            with self.assertRaisesRegex(FileNotFoundError, "input is missing"):
                _prepared_from_marker(marker, "indie_one", "a" * 64, "inputs", lambda: None)


if __name__ == "__main__":
    unittest.main()
