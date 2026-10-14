"""Exercise persistent manifest sections with real source and cache files."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from endfield_scene_manifest_store import ManifestSectionStoreCorruptError, open_manifest_sections


class ManifestSectionStoreTests(unittest.TestCase):
    def test_cold_and_warm_roundtrip_without_source_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            original = {"Format": "EndfieldSceneProbe/1", "中文": [1.25, -0.0, {"flag": True}], "empty": None}
            source.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8-sig")
            cold = open_manifest_sections(source, root / "cache", "version-one")
            self.assertEqual(tuple(original), cold.keys)
            self.assertEqual(original, {key: json.loads(cold.read_section(key)) for key in cold.keys})
            self.assertEqual(b"null", cold.read_section("empty"))
            with self.assertRaises(KeyError):
                cold.read_section("missing")
            source.unlink()
            warm = open_manifest_sections(source, root / "cache", "version-one")
            self.assertEqual(cold.path, warm.path)
            self.assertEqual(original, {key: json.loads(warm.read_section(key)) for key in warm.keys})

    def test_source_changes_use_separate_identity_and_keep_duplicate_key_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            source.write_text('{"duplicate":1,"later":null,"duplicate":2}', encoding="utf-8")
            first = open_manifest_sections(source, root / "cache", "first")
            self.assertEqual(("duplicate", "later"), first.keys)
            self.assertEqual(2, json.loads(first.read_section("duplicate")))
            source.write_text('{"duplicate":3}', encoding="utf-8")
            second = open_manifest_sections(source, root / "cache", "second")
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(3, json.loads(second.read_section("duplicate")))
            self.assertEqual(2, json.loads(first.read_section("duplicate")))

    def test_empty_object_and_invalid_source_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            source.write_text("{}", encoding="utf-8")
            self.assertEqual((), open_manifest_sections(source, root / "cache", "empty").keys)
            source.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                open_manifest_sections(source, root / "cache", "array")
            source.write_text('{"bad":', encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                open_manifest_sections(source, root / "cache", "malformed")

    def test_payload_corruption_fails_on_access_without_rebuilding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            source.write_text('{"first":1,"second":2}', encoding="utf-8")
            store = open_manifest_sections(source, root / "cache", "version")
            raw = store.path.read_bytes()
            store.path.write_bytes(raw[:-1] + b"3")
            source.unlink()
            reopened = open_manifest_sections(source, root / "cache", "version")
            self.assertEqual(b"1", reopened.read_section("first"))
            with self.assertRaisesRegex(ManifestSectionStoreCorruptError, "SHA256"):
                reopened.read_section("second")
            self.assertEqual(raw[:-1] + b"3", store.path.read_bytes())

    def test_truncation_and_invalid_ranges_fail_during_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            source.write_text('{"section":[1,2]}', encoding="utf-8")
            store = open_manifest_sections(source, root / "cache", "version")
            raw = store.path.read_bytes()
            for damaged in (raw[:5], raw[:-1], raw + b"extra"):
                with self.subTest(bytes=len(damaged)):
                    store.path.write_bytes(damaged)
                    with self.assertRaises(ManifestSectionStoreCorruptError):
                        open_manifest_sections(source, root / "cache", "version")
            header = struct.Struct("<8sI32s")
            magic, metadata_bytes, _ = header.unpack(raw[:header.size])
            metadata = json.loads(raw[header.size:header.size + metadata_bytes])
            metadata["sections"][0]["offset"] = 1
            replacement = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
            store.path.write_bytes(header.pack(magic, len(replacement), hashlib.sha256(replacement).digest())
                                   + replacement + raw[header.size + metadata_bytes:])
            with self.assertRaisesRegex(ManifestSectionStoreCorruptError, "range"):
                open_manifest_sections(source, root / "cache", "version")

    def test_metadata_name_bitflip_is_rejected_before_section_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            source.write_text('{"first":1,"second":2}', encoding="utf-8")
            store = open_manifest_sections(source, root / "cache", "version")
            raw = store.path.read_bytes()
            self.assertIn(b'"name":"first"', raw)
            store.path.write_bytes(raw.replace(b'"name":"first"', b'"name":"third"', 1))
            with self.assertRaisesRegex(ManifestSectionStoreCorruptError, "metadata SHA256"):
                open_manifest_sections(source, root / "cache", "version")

    def test_lone_surrogates_roundtrip_and_regular_unicode_stays_compact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            original = {"中文": "中文", "surrogate": "\ud800", "\udfff": ["\ud800", "normal"]}
            source.write_text(json.dumps(original, ensure_ascii=True), encoding="utf-8")
            store = open_manifest_sections(source, root / "cache", "version")
            self.assertEqual('"中文"'.encode("utf-8"), store.read_section("中文"))
            self.assertEqual(b'"\\ud800"', store.read_section("surrogate"))
            source.unlink()
            warm = open_manifest_sections(source, root / "cache", "version")
            self.assertEqual(original, {key: json.loads(warm.read_section(key)) for key in warm.keys})

    def test_concurrent_publication_produces_complete_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.json"
            original = {"rows": list(range(2048)), "null": None}
            source.write_text(json.dumps(original), encoding="utf-8")

            def read_store(index: int) -> dict[str, object]:
                store = open_manifest_sections(source, root / "cache", "shared")
                return {key: json.loads(store.read_section(key)) for key in store.keys}

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = tuple(executor.map(read_store, range(8)))
            self.assertEqual((original,) * 8, results)
            self.assertEqual([], list((root / "cache").rglob("*.tmp")))
