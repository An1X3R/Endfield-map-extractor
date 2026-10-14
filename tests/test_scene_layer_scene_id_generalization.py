from __future__ import annotations

import unittest
from pathlib import Path

from endfield_scene_water_plan import build_water_geometry_plan
from extractor_core.lights import read_native_lights, read_native_lights_cached


class SceneLayerSceneIdTests(unittest.TestCase):
    def test_native_light_readers_reject_invalid_scene_ids_before_io(self) -> None:
        missing_path = Path("missing")
        with self.assertRaisesRegex(ValueError, "Invalid scene ID"):
            read_native_lights(missing_path, missing_path, "indie-dg005", None)
        with self.assertRaisesRegex(ValueError, "Invalid scene ID"):
            read_native_lights_cached(missing_path, missing_path, "indie-dg005", missing_path, None)

    def test_indie_scene_without_global_water_chunk_keeps_explicit_failure(self) -> None:
        bounds = {"xmin": 0.0, "xmax": 1.0, "zmin": 0.0, "zmax": 1.0}
        with self.assertRaisesRegex(
            FileNotFoundError, "WATER_GLOBAL_INIT_MISSING map_id=indie_dg005"
        ):
            build_water_geometry_plan(
                Path("unused"), {}, {}, "indie_dg005", bounds, bounds, (), None
            )

    def test_water_plan_rejects_invalid_scene_ids_before_chunk_scan(self) -> None:
        bounds = {"xmin": 0.0, "xmax": 1.0, "zmin": 0.0, "zmax": 1.0}
        with self.assertRaisesRegex(ValueError, "Invalid scene ID"):
            build_water_geometry_plan(
                Path("unused"), {}, {}, "indie-dg005", bounds, bounds, (), None
            )


if __name__ == "__main__":
    unittest.main()
