"""Validation for portable terrain detail source paths."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endfield_scene_io import read_scene_manifest, scene_io_session
from prepare_terrain_detail import (
    _detail_resources_path, _existing_detail_result, _scene_sources, terrain_detail_source_paths,
)


class TerrainDetailSourceTests(unittest.TestCase):
    def test_global_source_assets_are_exact_and_map_scoped(self) -> None:
        for map_id in ("map01", "map02"):
            self.assertEqual(terrain_detail_source_paths(map_id), (
                f"assets/beyond/dynamicassets/scenes/pc/{map_id}/terrain/hgterrainsurfacesdata.asset",
                f"assets/beyond/dynamicassets/scenes/pc/{map_id}/terrain/splatindexmap.tga",
            ))
        for map_id in ("", "../map02", "map02/terrain"):
            with self.assertRaisesRegex(ValueError, "Invalid terrain resource map name"):
                terrain_detail_source_paths(map_id)

    def test_exact_terrain_refs_ignore_unrelated_export_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "terrain.bundle"
            index = root / "index.png"
            bundle.write_bytes(b"terrain metadata bundle")
            index.write_bytes(b"terrain index texture")
            manifest = root / "scene_manifest.json"
            manifest.write_text(json.dumps({
                "Format": "EndfieldSceneProbe/1",
                "Containers": [
                    {"Container": "assets/beyond/dynamicassets/scenes/pc/map01/terrain/hgterrainsurfacesdata.asset",
                     "Asset": {"Source": "terrain.bundle", "PathId": 41, "Type": "MonoBehaviour"}},
                    {"Container": "assets/beyond/dynamicassets/scenes/pc/map01/terrain/splatindexmap.tga",
                     "Asset": {"Source": "terrain.bundle", "PathId": 42, "Type": "Texture2D"}},
                ],
                "SerializedFiles": [
                    {"Name": "terrain.bundle", "OriginalPath": "terrain.bundle"},
                    {"Name": "unrelated.bundle", "OriginalPath": "unused/\x00unrelated.bundle"},
                ],
                "TextureExports": [
                    {"Asset": {"Source": "terrain.bundle", "PathId": 42}, "File": "index.png"},
                    {"Asset": {"Source": "elsewhere", "PathId": 43}, "File": "unused/\x00texture.png"},
                ],
            }), encoding="utf-8")
            with scene_io_session() as session:
                read_scene_manifest(manifest)
                metadata_path, index_path = _scene_sources((manifest,), "map01")

                self.assertEqual(1, session.statistics()["manifest_reads"])
                self.assertEqual(1, session.statistics()["manifest_hits"])
            self.assertEqual(metadata_path, bundle)
            self.assertEqual(index_path, index)

    def test_reused_partial_detail_plan_keeps_existing_pending(self) -> None:
        pending = {"entity_id": -1, "entity_base": "terrain", "component": "terrain_detail",
                   "reason": "source_detail_incomplete", "detail": "missing source page"}
        self.assertEqual(_existing_detail_result(["one.json"], [pending]),
                         {"status": "partial", "details": ["one.json"], "pending": [pending],
                          "reused_plan": True})
        self.assertEqual(_existing_detail_result([], [pending])["status"], "partial")
        self.assertIsNone(_existing_detail_result([], []))

    def test_missing_legacy_field_is_explicitly_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            surface = Path(directory)
            self.assertIsNone(_detail_resources_path(surface, {}))
            self.assertEqual(_detail_resources_path(surface, {"detail_resources": "pages.json"}),
                             surface / "pages.json")
            with self.assertRaisesRegex(ValueError, "escapes its surface cache"):
                _detail_resources_path(surface, {"detail_resources": "../pages.json"})


if __name__ == "__main__":
    unittest.main()
