from __future__ import annotations

import unittest
from dataclasses import replace

from endfield_scene_lights_plan import (build_light_plan,
                                        build_water_transport_directional_dependency,
                                        validate_light_plan_rows)
from extractor_core.lights import LightProperty, NativeLight, NativeLightInventory


class WaterTransportLightDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        init_path = "Data/Streaming/PC/map01/Streaming/InitChunkData_0_0_0_0.bytes"
        stream_path = init_path.replace("/InitChunkData_", "/StreamingChunkData_")
        properties = tuple(LightProperty(index, 1, "00") for index in range(60))
        additional = tuple(LightProperty(index, 1, "00") for index in range(21))
        light = NativeLight(
            "map01", 90982682, "Sun", (20.0, 30.0, 40.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0),
            1, 0, (1.0, 1.0, 1.0, 1.0), 2.0, 10.0, 45.0, 20.0, 0.0, 6500.0,
            False, False, False, 1.0, None, 131072, properties, additional,
            (init_path,), (stream_path,), "0" * 64,
        )
        self.light = light
        self.inventory = NativeLightInventory(
            "map01", (light,), ((init_path, "1" * 64), (stream_path, "2" * 64)), 1)

    def test_non_anchor_batch_can_carry_exact_directional_sun_dependency(self) -> None:
        selection = {"xmin": 0.0, "xmax": 256.0, "zmin": 0.0, "zmax": 128.0}
        non_anchor_batch = {"xmin": 128.0, "xmax": 256.0, "zmin": 0.0, "zmax": 128.0}
        selected = build_light_plan(self.inventory, non_anchor_batch, selection)
        self.assertEqual(selected.rows, ())

        dependency = build_water_transport_directional_dependency(self.inventory, "map01", 90982682)
        self.assertEqual(len(dependency.rows), 1)
        self.assertEqual(dependency.rows[0]["entity_id"], 90982682)
        self.assertEqual(dependency.rows[0]["light_type"], 1)
        self.assertEqual(dependency.rows[0]["selection_scope"], "water_transport_dependency")
        self.assertEqual(validate_light_plan_rows(
            list(dependency.rows), "map01", dict(dependency.source_sha256)), dependency.rows)

    def test_dependency_rejects_missing_identity_and_non_sun(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one source row"):
            build_water_transport_directional_dependency(self.inventory, "map01", 42)
        point = replace(self.light, light_type=0)
        point_inventory = NativeLightInventory(
            "map01", (point,), self.inventory.source_sha256, 1)
        with self.assertRaisesRegex(ValueError, "directional Sun"):
            build_water_transport_directional_dependency(point_inventory, "map01", 90982682)


if __name__ == "__main__":
    unittest.main()
