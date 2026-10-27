from __future__ import annotations

import struct
import unittest

from endfield_scene_spatial_plan import WorldBounds, select_static_source_rows


def matrix_at(pivot: tuple[float, float, float]) -> bytes:
    return struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, *pivot, 1)


def spatial_record(entity_id: int, matrix: bytes, footprint: WorldBounds) -> dict[str, object]:
    x, y, z = struct.unpack_from("<3f", matrix, 48)
    return {"entity_id": entity_id, "matrix_blob": matrix, "world_bounds": footprint, "x": x, "y": y, "z": z}


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
            ((-2.0, 0.0, 10.0, 20.0, -2.0, 0.0),), True, None,
        )

        self.assertEqual(rows, actual.selected_rows)
        self.assertEqual(frozenset({1, 2}), actual.visible_static_ids)
        self.assertEqual(frozenset({2}), actual.bottom_candidate_ids)
        self.assertEqual(frozenset({1, 2, 3}), actual.surface_candidate_ids)
        self.assertEqual(2, actual.selected_static_count)
        self.assertEqual(1, actual.footprint_extras)
        without_effects = select_static_source_rows(
            "source", rows, records, bounds, selection, frozenset({"building"}),
            ((-2.0, 0.0, 10.0, 20.0, -2.0, 0.0),), False, None,
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
                frozenset({"building"}), (), False, None,
            )

    def test_global_assembly_uses_one_intersecting_member_owner_across_batches(self) -> None:
        source = "Data/Streaming/PC/map02/Streaming/InitChunkData_Global_0_123.bytes"
        matrix = matrix_at((30.0, 5.0, 30.0))
        rows = tuple((i, "Part", matrix, source, 0, i, "prop") for i in (1, 2, 3))
        records = (
            spatial_record(1, matrix, (12.0, 18.0, 0.0, 10.0, 12.0, 18.0)),
            spatial_record(2, matrix, (4.0, 8.0, 0.0, 10.0, 4.0, 8.0)),
            spatial_record(3, matrix, (25.0, 40.0, 0.0, 10.0, 25.0, 40.0)),
        )
        selection = {"xmin": 0.0, "xmax": 20.0, "zmin": 0.0, "zmax": 20.0}
        left = {**selection, "xmax": 10.0}
        right = {**selection, "xmin": 10.0}
        water = ((12.0, 18.0, 0.0, 10.0, 12.0, 18.0),)
        first = select_static_source_rows(source, rows, records, left, selection, frozenset({"prop"}), water, True, None)
        second = select_static_source_rows(source, rows[::-1], records, right, selection, frozenset({"prop"}), water, True, None)

        self.assertEqual(frozenset({1, 2, 3}), first.visible_static_ids)
        self.assertEqual(frozenset(), second.visible_static_ids)
        self.assertEqual(3, first.footprint_extras)
        self.assertEqual(frozenset(), first.bottom_candidate_ids)
        self.assertEqual(frozenset({1}), second.bottom_candidate_ids)
        self.assertEqual(frozenset({2}), first.surface_candidate_ids)
        self.assertEqual(frozenset({1}), second.surface_candidate_ids)

    def test_global_assembly_prefers_its_pivot_inside_selection(self) -> None:
        source = "Data/Streaming/PC/map02/Streaming/InitChunkData_Global_0_123.bytes"
        matrix = matrix_at((15.0, 5.0, 15.0))
        rows = tuple((i, "Part", matrix, source, 0, i, "building") for i in (1, 2))
        records = (
            spatial_record(1, matrix, (1.0, 3.0, 0.0, 10.0, 1.0, 3.0)),
            spatial_record(2, matrix, (25.0, 30.0, 0.0, 10.0, 25.0, 30.0)),
        )
        selection = {"xmin": 0.0, "xmax": 20.0, "zmin": 0.0, "zmax": 20.0}
        left = select_static_source_rows(source, rows, records, {**selection, "xmax": 10.0}, selection,
                                         frozenset({"building"}), (), False, None)
        right = select_static_source_rows(source, rows, records, {**selection, "xmin": 10.0}, selection,
                                          frozenset({"building"}), (), False, None)
        self.assertEqual(frozenset(), left.visible_static_ids)
        self.assertEqual(frozenset({1, 2}), right.visible_static_ids)

    def test_global_assembly_does_not_own_union_gaps_even_when_pivot_is_inside(self) -> None:
        source = "Data/Streaming/PC/map02/Streaming/InitChunkData_Global_0_123.bytes"
        matrix = matrix_at((10.0, 5.0, 10.0))
        rows = tuple((i, "Part", matrix, source, 0, i, "prop") for i in (1, 2))
        records = (
            spatial_record(1, matrix, (0.0, 3.0, 0.0, 10.0, 0.0, 3.0)),
            spatial_record(2, matrix, (17.0, 20.0, 0.0, 10.0, 17.0, 20.0)),
        )
        selection = {"xmin": 5.0, "xmax": 15.0, "zmin": 5.0, "zmax": 15.0}
        actual = select_static_source_rows(source, rows, records, selection, selection, frozenset({"prop"}), (), False, None)
        self.assertEqual((), actual.selected_rows)

    def test_assembly_scope_excludes_base_groups_non_global_origin_and_different_pivots(self) -> None:
        global_source = "Data/Streaming/PC/map02/Streaming/InitChunkData_Global_0_123.bytes"
        pivot = (30.0, 5.0, 30.0)
        selection = {"xmin": 10.0, "xmax": 20.0, "zmin": 10.0, "zmax": 20.0}
        cases = (
            (global_source.replace("_123.bytes", "_0.bytes"), pivot, pivot, "Part", "prop"),
            (global_source.replace("Global", "1_1"), pivot, pivot, "Part", "prop"),
            (global_source, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), "Part", "prop"),
            (global_source, pivot, (30.0, 6.0, 30.0), "Part", "prop"),
            (global_source, pivot, (30.0, 5.000001, 30.0), "Part", "prop"),
            (global_source, pivot, pivot, "HLOD0_Cluster", "prop"),
            (global_source, pivot, pivot, "Part", "unknown"),
        )
        for source, first_pivot, second_pivot, second_name, second_category in cases:
            with self.subTest(source=source, pivot=second_pivot, name=second_name, category=second_category):
                first_matrix, second_matrix = matrix_at(first_pivot), matrix_at(second_pivot)
                rows = ((1, "Part", first_matrix, source, 0, 0, "prop"),
                        (2, second_name, second_matrix, source, 0, 1, second_category))
                records = (spatial_record(1, first_matrix, (12.0, 15.0, 0.0, 10.0, 12.0, 15.0)),
                           spatial_record(2, second_matrix, (25.0, 30.0, 0.0, 10.0, 25.0, 30.0)))
                actual = select_static_source_rows(source, rows, records, selection, selection,
                                                   frozenset({"prop", "unknown"}), (), False, None)
                self.assertEqual(frozenset({1}), actual.visible_static_ids)

    def test_explicit_level_scope_keeps_mixed_source_members_and_water_context(self) -> None:
        source = "Data/Streaming/PC/map02/Streaming/InitChunkData_Global_0_123.bytes"
        selected, foreign = (10 << 27) + 1, (8 << 27) + 2
        matrix = matrix_at((15.0, 5.0, 15.0))
        rows = tuple((eid, "Part", matrix, source, 0, index, "prop")
                     for index, eid in enumerate((selected, foreign)))
        footprint = (12.0, 18.0, 0.0, 10.0, 12.0, 18.0)
        records = tuple(spatial_record(eid, matrix, footprint) for eid in (selected, foreign))
        bounds = {"xmin": 10.0, "xmax": 20.0, "zmin": 10.0, "zmax": 20.0}
        scoped = select_static_source_rows(source, rows, records, bounds, bounds, frozenset({"prop"}),
                                           (footprint,), True, 10)
        unrestricted = select_static_source_rows(source, rows, records, bounds, bounds, frozenset({"prop"}),
                                                 (footprint,), True, None)
        self.assertEqual(frozenset({selected}), scoped.visible_static_ids)
        self.assertEqual(frozenset({foreign}), scoped.excluded_level_ids)
        self.assertEqual(unrestricted.selected_rows, scoped.selected_rows)
        self.assertEqual(unrestricted.bottom_candidate_ids, scoped.bottom_candidate_ids)
        self.assertEqual(unrestricted.surface_candidate_ids, scoped.surface_candidate_ids)


if __name__ == "__main__":
    unittest.main()
