"""Check temporal identity of the bounded particle preview emission schedule."""
from __future__ import annotations

import random
import unittest

from extractor_core.particle_snapshot import _birth_times


def constant(value: float, minimum: float, mode: int) -> dict[str, object]:
    curve = {"Keys": [], "PreInfinity": 2, "PostInfinity": 2, "RotationOrder": 4}
    return {"MinMaxState": mode, "Scalar": value, "MinScalar": minimum,
            "MaxCurve": curve, "MinCurve": curve}


def system(delay: float, looping: bool) -> dict[str, object]:
    return {"LengthInSec": 1.0, "StartDelay": constant(delay, delay, 0), "Looping": looping}


def emission(rate: float, bursts: list[dict[str, object]]) -> dict[str, object]:
    return {"RateOverTime": constant(rate, rate / 2, 0),
            "RateOverDistance": constant(0, 0, 0),
            "BurstCount": len(bursts), "Bursts": bursts}


def burst(time: float, probability: float) -> dict[str, object]:
    return {"Time": time, "Probability": probability, "CycleCount": 2,
            "RepeatInterval": 0.1, "CountCurve": constant(9, 2, 3)}


class ParticleTimingTests(unittest.TestCase):
    def test_constant_rate_births_match_integrated_rate(self) -> None:
        births = _birth_times(system(0, False), emission(10, []), 0.95, 1000, random.Random(17))
        self.assertEqual(len(births), 9)
        for index, time in enumerate(births):
            self.assertAlmostEqual(time, (index + 1) / 10)

    def test_old_births_are_identical_at_every_later_sample(self) -> None:
        source = emission(137, [burst(0.37, 0.6), burst(0, 0.8)])
        source["RateOverTime"] = constant(137, 31, 3)
        authored_system = system(0.013, True)
        complete = _birth_times(authored_system, source, 2.171, 10000, random.Random(13))
        for age in (0, 0.012, 0.013, 0.033, 0.104, 0.387, 0.923, 1.013, 1.583):
            current = _birth_times(authored_system, source, age, 10000, random.Random(13))
            self.assertEqual(current, tuple(time for time in complete if time <= age), age)

    def test_burst_at_loop_boundary_and_start_delay(self) -> None:
        source = emission(0, [{**burst(0, 1), "CycleCount": 1, "CountCurve": constant(1, 1, 0)}])
        authored_system = system(0.25, True)
        for age, expected in ((0, ()), (0.24, ()), (0.25, (0.25,)), (1.25, (0.25, 1.25))):
            self.assertEqual(_birth_times(authored_system, source, age, 1000, random.Random(1)), expected)


if __name__ == "__main__":
    unittest.main()
