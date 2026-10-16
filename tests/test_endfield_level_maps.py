from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from endfield_level_maps import (LevelMapError, compose_level_map, level_map_selection_grid,
    parse_level_map_config, read_level_map_manifest, resolve_level_map_image, selection_grid_world_bounds)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def config_payload() -> bytes:
    return json.dumps({"basic": {"worldRectLeftBottom": {"x": -128., "y": 384.},
        "worldRectRightTop": {"x": 128., "y": 640.}, "needInverseXZ": False}}).encode()


def prepare_fixture(root: Path) -> tuple[Path, tuple[str, ...]]:
    exports: list[dict[str, object]] = []
    containers: list[dict[str, object]] = []
    resources: list[str] = []
    for x in (1, 2):
        for z in (1, 2):
            name = f"h_map03_lv017_{x}_{z}"
            resource = f"assets/ui/sprites/levelmap/levelmapchunks/{name}.png"
            path = root / f"{name}.png"
            with Image.new("RGBA", (2, 2), (x * 40, z * 60, 90, 0 if (x, z) == (1, 2) else 128)) as image:
                image.save(path)
            asset = {"Source": "CAB-map", "PathId": x * 10 + z, "Name": name}
            exports.append({"Asset": asset, "File": path.name})
            containers.append({"Asset": asset, "Container": resource})
            resources.append(resource)
    manifest = root / "scene_manifest.json"
    write_json(manifest, {"TextureExports": exports, "Containers": containers})
    return manifest, tuple(resources)


class LevelMapIntegrationTests(unittest.TestCase):
    def test_source_aligned_selection_rectangles_and_invalid_inputs(self) -> None:
        grid = level_map_selection_grid("map02_lv010", "map02", "map02",
            {"xmin": -128., "xmax": 1792., "zmin": 384., "zmax": 1664.})
        self.assertEqual((grid["columns"], grid["rows"]), (15, 10))
        self.assertEqual(selection_grid_world_bounds(grid, 0, 0, 0, 0),
            {"xmin": -128., "xmax": 0., "zmin": 384., "zmax": 512.})
        self.assertEqual(selection_grid_world_bounds(grid, 14, 14, 9, 9),
            {"xmin": 1664., "xmax": 1792., "zmin": 1536., "zmax": 1664.})
        self.assertEqual(selection_grid_world_bounds(grid, 2, 4, 1, 2),
            {"xmin": 128., "xmax": 512., "zmin": 512., "zmax": 768.})
        self.assertEqual(selection_grid_world_bounds(grid, 0, 14, 0, 9), grid["worldBounds"])
        for rectangle in ((-1, 0, 0, 0), (0, 15, 0, 0), (0, 0, 0, 10), (2, 1, 0, 0), (0, 0, 2, 1), (True, 1, 0, 0), (0., 1, 0, 0)):
            with self.subTest(rectangle=rectangle), self.assertRaises(LevelMapError):
                selection_grid_world_bounds(grid, *rectangle)
        with self.assertRaisesRegex(LevelMapError, "origin"):
            selection_grid_world_bounds({**grid, "origin": {"x": 128., "z": 384.}}, 0, 0, 0, 0)

    def test_full_native_pixels_north_up_source_origin_and_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene, resources = prepare_fixture(root)
            entry = parse_level_map_config("map03_lv017", "Data/Json/UILevelMapLoadConfig/map03_lv017.json", config_payload(), resources)
            metadata = compose_level_map(entry, scene, root / "map.png")
            with Image.open(root / "map.png") as image:
                self.assertEqual(image.size, (4, 4))
                self.assertEqual(image.getpixel((0, 0)), (40, 120, 90, 0))
                self.assertEqual(image.getpixel((3, 3)), (80, 60, 90, 128))
            self.assertEqual(metadata["worldToPixel"], [[.015625, 0., 2.], [0., -.015625, 10.], [0., 0., 1.]])
            self.assertEqual(metadata["pixelToWorld"], [[64., 0., -128.], [0., -64., 640.], [0., 0., 1.]])
            registry = root / "level_maps.json"
            write_json(registry, {"format": "EndfieldLevelMaps/1", "entries": [metadata]})
            self.assertEqual(resolve_level_map_image(registry, root, "map03_lv017"), root / "map.png")
            with self.assertRaises(KeyError):
                resolve_level_map_image(registry, root, "map02_lv010")
            (root / "map.png").write_bytes(b"corrupt")
            with self.assertRaisesRegex(LevelMapError, "size changed"):
                resolve_level_map_image(registry, root, "map03_lv017")

    def test_missing_tile_is_not_a_successful_partial_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene, resources = prepare_fixture(root)
            (root / "h_map03_lv017_2_1.png").unlink()
            entry = parse_level_map_config("map03_lv017", "config.json", config_payload(), resources)
            with self.assertRaisesRegex(LevelMapError, r"missing=\[\(2, 1\)\]"):
                compose_level_map(entry, scene, root / "map.png")
            self.assertFalse((root / "map.png").exists())

    def test_registry_rejects_escaping_image_and_inconsistent_transform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene, resources = prepare_fixture(root)
            entry = parse_level_map_config("map03_lv017", "config.json", config_payload(), resources)
            metadata = compose_level_map(entry, scene, root / "map.png")
            registry = root / "level_maps.json"
            metadata["image"]["relativePath"] = "../map.png"
            write_json(registry, {"format": "EndfieldLevelMaps/1", "entries": [metadata]})
            with self.assertRaisesRegex(LevelMapError, "Unsafe"):
                read_level_map_manifest(registry, root)
            metadata["image"]["relativePath"] = "map.png"
            metadata["worldToPixel"][0][2] = 0.
            write_json(registry, {"format": "EndfieldLevelMaps/1", "entries": [metadata]})
            with self.assertRaisesRegex(LevelMapError, "transform differs"):
                read_level_map_manifest(registry, root)


if __name__ == "__main__":
    unittest.main()
