"""Validation for portable terrain detail source paths."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from prepare_terrain_detail import _detail_resources_path, _existing_detail_result


class TerrainDetailSourceTests(unittest.TestCase):
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
