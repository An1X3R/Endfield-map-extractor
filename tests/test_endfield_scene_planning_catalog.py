"""Exercise demand-driven planning metadata against real manifests and SQLite files."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from endfield_scene_io import ReadOnlySceneDataError
from endfield_scene_material_contract import load_catalog
from endfield_scene_planning_catalog import PlanningCatalogCorruptError, load_planning_catalog
from tests.test_scene_materials import material_record


def _manifest(path: Path, materials: list[dict[str, object]], textures: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Materials": materials,
                               "TextureExports": textures}), encoding="utf-8")


class PlanningCatalogTests(unittest.TestCase):
    def test_cold_warm_equivalence_and_read_only_selected_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            (root / "texture.png").write_bytes(b"texture")
            _manifest(manifest, [material_record()], [{"Asset": {"Source": "CAB-texture", "PathId": 3},
                                                       "File": "texture.png"}])
            expected_materials, expected_textures = load_catalog((manifest,))
            for _ in range(2):
                catalog = load_planning_catalog((manifest,), root / "cache")
                self.assertFalse(catalog.requires_extra_uv)
                self.assertEqual(tuple(expected_materials), tuple(catalog.materials))
                self.assertEqual(expected_materials, dict(catalog.materials))
                self.assertEqual(expected_textures, dict(catalog.textures))
                self.assertEqual(expected_materials, catalog.materials)
                self.assertEqual(expected_textures, catalog.textures)
                record = catalog.materials[("cab-material", 1)]
                with self.assertRaises(ReadOnlySceneDataError):
                    record["Floats"]["changed"] = 1.0
                with self.assertRaises(ReadOnlySceneDataError):
                    record["Textures"][0]["Offset"].append(2.0)
                with self.assertRaises(KeyError):
                    catalog.materials[("missing", 1)]

    def test_warm_catalog_does_not_decode_unrequested_material_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            first = material_record()
            second = {**first, "PathId": 2, "Name": "second"}
            _manifest(manifest, [first, second], [])
            load_planning_catalog((manifest,), root / "cache")
            store, = (root / "cache").rglob("*.sqlite")
            with closing(sqlite3.connect(store)) as database:
                database.execute("UPDATE materials SET payload=? WHERE reference=?", (b'{}', '["cab-material",2]'))
                database.commit()
            warm = load_planning_catalog((manifest,), root / "cache")
            self.assertIn(("cab-material", 2), warm.materials)
            self.assertEqual(2, len(warm.materials))
            self.assertEqual("surface", warm.materials[("cab-material", 1)]["Name"])
            with self.assertRaisesRegex(PlanningCatalogCorruptError, "row SHA256"):
                warm.materials[("cab-material", 2)]

    def test_texture_candidates_remain_live_and_preserve_first_existing_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            asset = {"Source": "CAB-texture", "PathId": 3}
            _manifest(manifest, [], [{"Asset": asset, "File": "first.png"}, {"Asset": asset, "File": "second.png"}])
            first, second = root / "first.png", root / "second.png"
            second.write_bytes(b"second")
            catalog = load_planning_catalog((manifest,), root / "cache")
            key = ("cab-texture", 3)
            self.assertEqual((root, "second.png"), catalog.textures[key])
            first.write_bytes(b"first")
            self.assertEqual((root, "first.png"), catalog.textures[key])
            first.unlink()
            second.unlink()
            self.assertNotIn(key, catalog.textures)
            self.assertEqual((), tuple(catalog.textures))
            self.assertEqual(0, len(catalog.textures))
            with self.assertRaises(KeyError):
                catalog.textures[key]
            first.write_bytes(b"restored")
            self.assertEqual((root, "first.png"), catalog.textures[key])

    def test_first_texture_winner_does_not_resolve_invalid_later_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            asset = {"Source": "CAB-texture", "PathId": 3}
            _manifest(manifest, [], [{"Asset": asset, "File": "first.png"}, {"Asset": asset, "File": "invalid\0.png"}])
            first = root / "first.png"
            first.write_bytes(b"first")
            catalog = load_planning_catalog((manifest,), root / "cache")
            self.assertEqual((root, "first.png"), catalog.textures[("cab-texture", 3)])
            first.unlink()
            with self.assertRaisesRegex(ValueError, "null"):
                catalog.textures[("cab-texture", 3)]

    def test_global_enrichment_and_uv_summary_match_full_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.json", root / "second.json"
            record = {**material_record(),
                      "Shader": {"Source": "CAB-93483c2cb844b6b343af0950ad4630ad", "PathId": 5324015590718682574},
                      "Floats": {"_BaseTextureMapCount": 2, "_LayerBlend": 1, "_LayerBlendType": 2,
                                 "_BaseUVSet": 1, "_BasePbrMapUVSet": 0, "_LayerBlendUVType": 2, "_LayerBlendHeight": 1},
                      "RenderState": {"DisabledShaderPasses": [], "CustomRenderQueue": 2000,
                                      "ValidKeywords": ["_LAYERBLEND_TOP", "_TWO_BASEMAP"]}}
            enriched = {**record, "Shader": {**record["Shader"], "TextureDefaults": [
                {"Property": "_LayerBlendMaskMap", "Name": "white", "Dimension": 2}]}}
            _manifest(first, [record], [])
            _manifest(second, [enriched], [{"Asset": {"Source": "CAB-texture", "PathId": 3, "ColorSpace": 1}, "File": None}])
            expected, _ = load_catalog((first, second))
            catalog = load_planning_catalog((first, second), root / "cache")
            self.assertTrue(catalog.requires_extra_uv)
            self.assertEqual(expected, dict(catalog.materials))
            self.assertEqual(1, catalog.materials[("cab-material", 1)]["Textures"][0]["Texture"]["ColorSpace"])

    def test_changed_same_size_source_with_restored_time_uses_new_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            record = material_record()
            _manifest(manifest, [record], [])
            first = load_planning_catalog((manifest,), root / "cache")
            stamp = manifest.stat()
            _manifest(manifest, [{**record, "Name": "SURFACE"}], [])
            os.utime(manifest, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertEqual(stamp.st_size, manifest.stat().st_size)
            second = load_planning_catalog((manifest,), root / "cache")
            self.assertEqual("surface", first.materials[("cab-material", 1)]["Name"])
            self.assertEqual("SURFACE", second.materials[("cab-material", 1)]["Name"])
            self.assertEqual(2, len(tuple((root / "cache").rglob("*.sqlite"))))

    def test_metadata_and_key_index_corruption_fail_without_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            _manifest(manifest, [material_record()], [])
            load_planning_catalog((manifest,), root / "cache")
            store, = (root / "cache").rglob("*.sqlite")
            original = store.read_bytes()
            with closing(sqlite3.connect(store)) as database:
                database.execute("UPDATE metadata SET payload=?", (b'{}',))
                database.commit()
            with self.assertRaisesRegex(PlanningCatalogCorruptError, "metadata SHA256"):
                load_planning_catalog((manifest,), root / "cache")
            store.write_bytes(original)
            with closing(sqlite3.connect(store)) as database:
                database.execute("DELETE FROM materials")
                database.commit()
            with self.assertRaisesRegex(PlanningCatalogCorruptError, "key index"):
                load_planning_catalog((manifest,), root / "cache")
            store.write_bytes(original[:50])
            with self.assertRaises(PlanningCatalogCorruptError):
                load_planning_catalog((manifest,), root / "cache")
            store.write_bytes(original + b"extra")
            with self.assertRaisesRegex(PlanningCatalogCorruptError, "file size"):
                load_planning_catalog((manifest,), root / "cache")

    def test_cold_validation_rejects_conflicts_and_empty_catalog_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.json", root / "second.json"
            _manifest(first, [material_record()], [])
            _manifest(second, [{**material_record(), "Name": "conflict"}], [])
            with self.assertRaisesRegex(ValueError, "Conflicting material"):
                load_planning_catalog((first, second), root / "cache")
            empty = load_planning_catalog((), root / "cache")
            self.assertFalse(empty.requires_extra_uv)
            self.assertEqual({}, empty.materials)
            self.assertEqual({}, empty.textures)

    def test_concurrent_creation_has_no_open_connection_or_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            _manifest(manifest, [material_record()], [])

            def read_catalog(index: int) -> str:
                catalog = load_planning_catalog((manifest,), root / "cache")
                return catalog.materials[("cab-material", 1)]["Name"]

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = tuple(executor.map(read_catalog, range(8)))
            self.assertEqual(("surface",) * 8, results)
            self.assertEqual([], list((root / "cache").rglob("*.tmp")))
            store, = (root / "cache").rglob("*.sqlite")
            store.unlink()
