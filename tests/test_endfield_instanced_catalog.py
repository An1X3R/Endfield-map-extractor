"""Check physical identity, source binding and prepared-cache integrity."""
from __future__ import annotations

import json
from pathlib import Path
import struct
import tempfile
import unittest

from endfield_instanced_catalog import (
    InstancedSceneError, _absolute_inputs, _art_owner, _assemble_catalog,
    _prepared_from_marker, _validate_catalog,
)
from endfield_scene_io import SceneInputChangedError
from endfield_scene_resources import describe_file
from extractor_core.scene_tables import SceneTableError, read_scene_table


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
        self.assertEqual(_art_owner(b"\x12\x03" + struct.pack("<i", len(reference)) + reference, "config"), "indie_test")
        with self.assertRaisesRegex(InstancedSceneError, "art reference length"):
            _art_owner(b"\x12\x03" + struct.pack("<i", -1), "config")
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
        self.assertFalse(scenes["indie_unregistered"]["export_capability"]["ready"])
        self.assertTrue(all(value is None for value in scenes["indie_one"]["component_counts"].values()))
        self.assertEqual(len({group["name"] for group in catalog["groups"]}), len(catalog["groups"]))
        self.assertEqual(_validate_catalog(catalog), catalog)
        invalid = {**catalog, "scenes": [catalog["scenes"][0], catalog["scenes"][0]]}
        with self.assertRaisesRegex(InstancedSceneError, "Duplicate cached physical scene"):
            _validate_catalog(invalid)

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
