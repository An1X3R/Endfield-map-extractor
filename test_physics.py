"""Focused validation tests for serialized PhysicsManager gravity values."""

from __future__ import annotations

import math
import unittest
from pathlib import Path

from extractor_core.physics import PhysicsSettingsError, _finite_gravity


class PhysicsGravityValidationTests(unittest.TestCase):
    def test_reads_finite_vector_and_ignores_unrelated_type_tree_fields(self) -> None:
        gravity = _finite_gravity(
            {"x": 0, "y": -9.8100004196167, "z": 0, "m_Extra": "ignored"},
            Path("globalgamemanagers"),
            10,
        )

        self.assertEqual((0.0, -9.8100004196167, 0.0), gravity)

    def test_rejects_missing_or_non_numeric_components(self) -> None:
        for value in ({"x": 0.0, "y": -9.81}, {"x": 0.0, "y": "-9.81", "z": 0.0}):
            with self.subTest(value=value):
                with self.assertRaises(PhysicsSettingsError):
                    _finite_gravity(value, Path("globalgamemanagers"), 10)

    def test_rejects_non_finite_components_and_booleans(self) -> None:
        for value in (
            {"x": 0.0, "y": math.nan, "z": 0.0},
            {"x": 0.0, "y": math.inf, "z": 0.0},
            {"x": 0.0, "y": False, "z": 0.0},
        ):
            with self.subTest(value=value):
                with self.assertRaises(PhysicsSettingsError):
                    _finite_gravity(value, Path("globalgamemanagers"), 10)


if __name__ == "__main__":
    unittest.main()
