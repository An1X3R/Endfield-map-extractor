from __future__ import annotations

import unittest

from endfield_scene_spatial_plan import select_static_source_rows


class SceneSpatialSelectionTests(unittest.TestCase):
    def test_static_ownership_water_bottom_and_surface_candidates(self) -> None:
        bounds = {"xmin": 0.0, "xmax": 10.0, "zmin": 0.0, "zmax": 10.0}
        selection = {"xmin": 0.0, "xmax": 10.0, "zmin": 0.0, "zmax": 10.0}
        rows = (
            (1, "Building", b"matrix-1", "source", 0, 0, "building"),
            (2, "Crossing", b"matrix-2", "source", 0, 1, "building"),
            (3, "Surface", b"matrix-3", "source", 0, 2, "unknown"),
        )
        records = (
            {"entity_id": 1, "matrix_blob": b"matrix-1", "world_bounds": (1.0, 2.0, 3.0, 4.0, 1.0, 2.0), "x": 1.5, "z": 1.5},
            {"entity_id": 2, "matrix_blob": b"matrix-2", "world_bounds": (-5.0, 2.0, 5.0, 30.0, -5.0, 2.0), "x": -20.0, "z": 0.0},
            {"entity_id": 3, "matrix_blob": b"matrix-3", "world_bounds": (5.0, 25.0, 0.0, 30.0, 5.0, 25.0), "x": 20.0, "z": 20.0},
        )

        actual = select_static_source_rows(
            "source", rows, records, bounds, selection, frozenset({"building"}),
            ((-2.0, 0.0, 10.0, 20.0, -2.0, 0.0),), True,
        )

        self.assertEqual(rows, actual.selected_rows)
        self.assertEqual(frozenset({1, 2}), actual.visible_static_ids)
        self.assertEqual(frozenset({2}), actual.bottom_candidate_ids)
        self.assertEqual(frozenset({1, 2, 3}), actual.surface_candidate_ids)
        self.assertEqual(2, actual.selected_static_count)
        self.assertEqual(1, actual.footprint_extras)
        without_effects = select_static_source_rows(
            "source", rows, records, bounds, selection, frozenset({"building"}),
            ((-2.0, 0.0, 10.0, 20.0, -2.0, 0.0),), False,
        )
        self.assertEqual(frozenset(), without_effects.surface_candidate_ids)

    def test_matrix_mismatch_fails_with_source_identity(self) -> None:
        row = (17, "Building", b"indexed-matrix", "source", 0, 0, "building")
        record = {"entity_id": 17, "matrix_blob": b"changed-matrix", "world_bounds": (0.0, 1.0, 0.0, 1.0, 0.0, 1.0), "x": 0.5, "z": 0.5}

        with self.assertRaisesRegex(ValueError, "entity_id=17, source=source"):
            select_static_source_rows(
                "source", (row,), (record,),
                {"xmin": 0.0, "xmax": 1.0, "zmin": 0.0, "zmax": 1.0},
                {"xmin": 0.0, "xmax": 1.0, "zmin": 0.0, "zmax": 1.0},
                frozenset({"building"}), (), False,
            )


if __name__ == "__main__":
    unittest.main()
