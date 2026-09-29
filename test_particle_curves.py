"""Analytical checks for the particle curve contract and explicit limitations."""
from __future__ import annotations

import unittest

from extractor_core.particle_curves import (
    CurveKey, ParticleCurve, ParticleCurveError, ParticleMinMaxCurve,
    UnsupportedParticleCurve, evaluate_minmax_curve, evaluate_particle_curve,
    parse_minmax_curve, parse_particle_curve,
)


def key(time: float, value: float, slope: float) -> CurveKey:
    return CurveKey(time, value, slope, slope, 0, 1 / 3, 1 / 3)


def curve(keys: tuple[CurveKey, ...]) -> ParticleCurve:
    return ParticleCurve(keys, 2, 2, 4)


class ParticleCurveTests(unittest.TestCase):
    def test_cubic_and_non_unit_time_domain_match_analytic_functions(self) -> None:
        cubic = curve((key(0, 0, 0), key(1, 1, 3)))
        linear = curve((key(2, 3, 2), key(4, 7, 2)))
        for time in (0, 0.1, 0.25, 0.6, 1):
            self.assertAlmostEqual(evaluate_particle_curve(cubic, time), time ** 3)
            self.assertAlmostEqual(evaluate_particle_curve(linear, 2 + time * 2), 3 + time * 4)

    def test_loop_wrap_preserves_non_unit_domain_and_exact_keys(self) -> None:
        linear = curve((key(2, 3, 2), key(4, 7, 2)))
        for time, expected in ((1, 5), (-1, 5), (5, 5), (6, 3), (2, 3), (4, 7)):
            self.assertAlmostEqual(evaluate_particle_curve(linear, time), expected)

    def test_two_curves_keep_random_factor_and_one_shared_multiplier(self) -> None:
        lower = curve((key(0, 1, 1), key(1, 2, 1)))
        upper = curve((key(0, 3, 1), key(1, 4, 1)))
        value = ParticleMinMaxCurve(2, 5, 99, upper, lower)
        for factor in (0, 0.25, 0.75, 1):
            self.assertAlmostEqual(evaluate_minmax_curve(value, 0.4, factor), 5 * (1.4 + 2 * factor))

    def test_constants_do_not_require_inactive_curve_keys(self) -> None:
        empty = curve(())
        self.assertEqual(evaluate_minmax_curve(ParticleMinMaxCurve(0, 7, 2, empty, empty), 0.5, 0.1), 7)
        self.assertEqual(evaluate_minmax_curve(ParticleMinMaxCurve(3, 4, 15, empty, empty), 0.5, 0), 15)
        self.assertEqual(evaluate_minmax_curve(ParticleMinMaxCurve(3, 4, 15, empty, empty), 0.5, 1), 4)

    def test_step_retains_left_value_until_exact_right_key(self) -> None:
        value = curve((key(0, 2, float("inf")), key(1, 5, float("inf"))))
        self.assertEqual(evaluate_particle_curve(value, 0.99999), 2)
        self.assertEqual(evaluate_particle_curve(value, 1), 5)

    def test_unsupported_evaluation_never_substitutes_linear_or_clamped_values(self) -> None:
        weighted = curve((CurveKey(0, 0, 0, 0, 2, 0.1, 0.2), key(1, 1, 0)))
        with self.assertRaisesRegex(UnsupportedParticleCurve, "Weighted"):
            evaluate_particle_curve(weighted, 0.5)
        with self.assertRaisesRegex(UnsupportedParticleCurve, "extrapolation"):
            evaluate_particle_curve(ParticleCurve((key(0.2, 0, 1), key(0.8, 1, 1)), 0, 0, 4), 0)
        with self.assertRaisesRegex(UnsupportedParticleCurve, "extrapolation"):
            evaluate_particle_curve(curve((key(0.5, 3, 0),)), 0)
        with self.assertRaisesRegex(ParticleCurveError, "without keys"):
            evaluate_particle_curve(curve(()), 0.5)
        with self.assertRaisesRegex(ParticleCurveError, "Random factor"):
            evaluate_minmax_curve(ParticleMinMaxCurve(0, 1, 1, curve(()), curve(())), 0, 1.1)

    def test_serialized_boundary_preserves_infinity_and_rejects_malformed_data(self) -> None:
        row: dict[str, object] = {
            "Time": 0, "Value": 2, "InSlope": "-Infinity", "OutSlope": "Infinity",
            "WeightedMode": 0, "InWeight": 0, "OutWeight": 0,
        }
        encoded: dict[str, object] = {"Keys": [row], "PreInfinity": 2, "PostInfinity": 2, "RotationOrder": 4}
        parsed = parse_particle_curve(encoded, "test")
        self.assertEqual(parsed.keys[0].out_slope, float("inf"))
        self.assertEqual(evaluate_particle_curve(parsed, 0), 2)
        with self.assertRaisesRegex(ParticleCurveError, "finite number"):
            parse_particle_curve({**encoded, "Keys": [{**row, "Value": True}]}, "test")
        with self.assertRaisesRegex(ParticleCurveError, "finite number"):
            parse_particle_curve({**encoded, "Keys": [{**row, "InSlope": "NaN"}]}, "test")
        with self.assertRaisesRegex(ParticleCurveError, "increase strictly"):
            parse_particle_curve({**encoded, "Keys": [row, row]}, "test")
        with self.assertRaisesRegex(ParticleCurveError, "Required curve field"):
            parse_particle_curve({"Keys": []}, "test")
        with self.assertRaisesRegex(UnsupportedParticleCurve, "Unknown particle curve mode"):
            parse_minmax_curve({"MinMaxState": 4}, "test")


if __name__ == "__main__":
    unittest.main()
