from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import cast

from PIL import Image

from endfield_map_overviews import build_map_overviews, compose_map


SECTOR_SIZE = 128
OWNERSHIP_ORDER = "rows south-to-north; columns west-to-east"


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def image_pixels(seed: int, alpha_zero: bool) -> list[tuple[int, int, int, int]]:
    colors = [
        (seed, 40 + seed % 70, 220 - seed % 90, 0 if alpha_zero else 255),
        (seed + 1, 70 + seed % 50, 180 - seed % 80, 255),
        (seed + 2, 100 + seed % 40, 140 - seed % 60, 255),
        (seed + 3, 130 + seed % 30, 100 - seed % 40, 255),
    ]
    return colors


def write_tile(path: Path, pixels: list[tuple[int, int, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (2, 2))
    image.putdata(pixels)
    image.save(path, format="PNG")
    image.close()


def level_spec(level_id: str, bounds: dict[str, float]) -> dict[str, object]:
    return {
        "levelId": level_id,
        "worldRect": {
            "xmin": bounds["xmin"], "xmax": bounds["xmax"],
            "zmin": bounds["zmin"], "zmax": bounds["zmax"],
        },
    }


def create_map_inputs(
    root: Path,
    map_id: str,
    bounds: dict[str, float],
    ownership: list[list[str | None]],
    add_missing_owner: bool,
    add_sprite_texture_variant: bool,
) -> tuple[dict[str, object], Path, list[Path]]:
    scene_dir = root / "prepared" / map_id
    level_id = f"{map_id}_lv001"
    texture_exports: list[dict[str, object]] = []
    containers: list[dict[str, object]] = []
    source_files: list[Path] = []

    for z in (1, 2):
        for x in (1, 2):
            name = f"h_{map_id}_{level_id.split('_', 1)[1]}_{x}_{z}"
            is_sprite_override = add_sprite_texture_variant and (x, z) == (1, 1)
            pixels = image_pixels(28 + x * 17 + z * 23 + (0 if map_id == "map01" else 71), is_sprite_override)
            if is_sprite_override:
                pixels = [(230, 30, 20, 0), (220, 60, 40, 255), (210, 90, 60, 255), (200, 120, 80, 255)]
            sprite_path = scene_dir / "tiles" / f"{name}_sprite.png"
            write_tile(sprite_path, pixels)
            source_files.append(sprite_path)
            sprite_source = f"CAB-{map_id.upper()}-SPRITES"
            sprite_path_id = 1000 + (z - 1) * 2 + x
            texture_exports.append({
                "Asset": {"Source": sprite_source, "PathId": sprite_path_id, "Name": name},
                "File": sprite_path.relative_to(scene_dir).as_posix(),
            })
            layer = "sprites" if is_sprite_override else "textures"
            containers.append({
                "Container": f"assets/ui/{layer}/levelmap/levelmapchunks/{name}.png",
                "Asset": {"Source": sprite_source, "PathId": sprite_path_id, "Name": name},
            })
            if is_sprite_override:
                texture_path = scene_dir / "tiles" / f"{name}_texture_variant.png"
                write_tile(texture_path, [(255, 255, 255, 0), (200, 100, 50, 128),
                                          (215, 25, 205, 255), (205, 35, 195, 255)])
                source_files.append(texture_path)
                texture_source = f"CAB-{map_id.upper()}-TEXTURES"
                texture_path_id = 2001
                texture_exports.append({
                    "Asset": {"Source": texture_source, "PathId": texture_path_id, "Name": name},
                    "File": texture_path.relative_to(scene_dir).as_posix(),
                })
                containers.append({
                    "Container": f"assets/ui/textures/levelmap/levelmapchunks/{name}.png",
                    "Asset": {"Source": texture_source, "PathId": texture_path_id, "Name": name},
                })

    unplaced_level = f"{map_id}_lv099"
    unplaced_name = f"h_{map_id}_lv099_1_1"
    unplaced_path = scene_dir / "tiles" / f"{unplaced_name}.png"
    write_tile(unplaced_path, image_pixels(181, False))
    source_files.append(unplaced_path)
    unplaced_source = f"CAB-{map_id.upper()}-UNPLACED"
    texture_exports.append({
        "Asset": {"Source": unplaced_source, "PathId": 9901, "Name": unplaced_name},
        "File": unplaced_path.relative_to(scene_dir).as_posix(),
    })
    containers.append({
        "Container": f"assets/ui/sprites/levelmap/levelmapchunks/{unplaced_name}.png",
        "Asset": {"Source": unplaced_source, "PathId": 9901, "Name": unplaced_name},
    })

    manifest_path = scene_dir / "scene_manifest.json"
    write_json(manifest_path, {
        "Format": "EndfieldSceneProbe/1",
        "Containers": containers,
        "TextureExports": texture_exports,
    })
    source_files.append(manifest_path)

    levels = [level_spec(level_id, bounds)]
    if add_missing_owner:
        levels.append(level_spec(f"{map_id}_lv002", bounds))
    overview_spec: dict[str, object] = {
        "mapId": map_id,
        "worldBounds": bounds,
        "sectorSizeMetres": SECTOR_SIZE,
        "ownershipGridOrder": OWNERSHIP_ORDER,
        "ownershipGrid": ownership,
        "levels": levels,
        "overview": {"clean": {"pixelWidth": 4, "pixelHeight": 4}},
    }
    source_files.extend([path for path in scene_dir.rglob("*.png")])
    return overview_spec, manifest_path, source_files


def snapshot(paths: list[Path]) -> dict[str, str]:
    return {
        str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(set(paths))
    }


class MapOverviewIntegrationTests(unittest.TestCase):
    def test_builds_two_oriented_maps_without_mutating_sources_or_crossing_output_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="map_overview_integration_") as temporary:
            root = Path(temporary)
            config_dir = root / "config"
            game_root = root / "portable_game"
            cache_root = root / "external_cache"
            export_root = cache_root / "overview_run"
            config_dir.mkdir(parents=True)
            game_root.mkdir()
            cache_root.mkdir()
            game_marker = game_root / "read_only_marker.bin"
            game_marker.write_bytes(b"synthetic game marker; overview builder must not touch it\n")

            map01_bounds = {"xmin": 0.0, "xmax": 256.0, "zmin": 0.0, "zmax": 256.0}
            map02_bounds = {"xmin": 512.0, "xmax": 768.0, "zmin": -256.0, "zmax": 0.0}
            map01_level = "map01_lv001"
            map02_level = "map02_lv001"
            map01_grid = [[map01_level, map01_level], [map01_level, "map01_lv002"]]
            map02_grid = [[map02_level, map02_level], [map02_level, None]]
            map01_spec, map01_scene, map01_files = create_map_inputs(
                root, "map01", map01_bounds, map01_grid, True, True
            )
            map02_spec, map02_scene, map02_files = create_map_inputs(
                root, "map02", map02_bounds, map02_grid, False, False
            )
            manifest_path = root / "prepared" / "runtime_manifest.json"
            write_json(manifest_path, {
                "format": "EndfieldRegionMapRuntimeManifest/1",
                "maps": {"map01": map01_spec, "map02": map02_spec},
            })
            runtime_path = config_dir / "runtime_config.json"
            runtime = {
                "format": "EndfieldRuntimeConfig/1",
                "paths": {
                    "game_root": os.path.relpath(game_root, config_dir),
                    "cache_root": os.path.relpath(cache_root, config_dir),
                },
                "extraction": {
                    "runtimeManifest": {"path": os.path.relpath(manifest_path, config_dir)},
                    "mapLayerFingerprint": "synthetic-map-layer-fingerprint",
                    "maps": {
                        "map01": {"sceneProbe": {"path": os.path.relpath(map01_scene, config_dir)}},
                        "map02": {"sceneProbe": {"path": os.path.relpath(map02_scene, config_dir)}},
                    },
                },
            }
            write_json(runtime_path, runtime)
            source_files = [runtime_path, manifest_path, game_marker, *map01_files, *map02_files]
            source_before = snapshot(source_files)

            result = build_map_overviews(runtime_path, export_root)
            output_manifest_path = Path(cast(str, result["path"]))
            output_manifest_bytes = output_manifest_path.read_bytes()
            output_manifest = json.loads(output_manifest_bytes.decode("utf-8"))
            self.assertEqual(hashlib.sha256(output_manifest_bytes).hexdigest().upper(), result["sha256"])
            self.assertEqual(len(output_manifest_bytes), result["bytes"])
            self.assertEqual("EndfieldMapOverviews/1", output_manifest["format"])
            self.assertEqual("ready", output_manifest["status"])
            self.assertEqual(str(manifest_path.resolve()), output_manifest["sourceRuntimeManifest"])
            self.assertEqual(str(runtime_path.resolve()), output_manifest["sourceRuntimeConfig"])
            self.assertEqual("+Z north/up; -X west/left", output_manifest["imageOrientation"])
            self.assertEqual(2, len(output_manifest["maps"]))

            map01_result = output_manifest["maps"]["map01"]
            map02_result = output_manifest["maps"]["map02"]
            self.assertEqual(3, map01_result["placedSectors"])
            self.assertEqual(1, map01_result["missingOwnedTiles"])
            self.assertEqual(["map01_lv099"], map01_result["discoveredLevelsWithoutPlacement"])
            self.assertEqual(3, map02_result["placedSectors"])
            self.assertEqual(1, map02_result["previewOnlyTiles"])
            self.assertEqual(0, map02_result["missingOwnedTiles"])
            self.assertEqual(["map02_lv099"], map02_result["discoveredLevelsWithoutPlacement"])

            map01_audit_path = export_root / cast(str, map01_result["tileAudit"])
            map02_audit_path = export_root / cast(str, map02_result["tileAudit"])
            map01_audit = json.loads(map01_audit_path.read_text(encoding="utf-8"))
            map02_audit = json.loads(map02_audit_path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(map01_audit["missingOwnedTiles"]))
            missing_tile = map01_audit["missingOwnedTiles"][0]
            self.assertEqual({"levelId": "map01_lv002", "sector": [1.0, 1.0]}, missing_tile)
            self.assertEqual([], map02_audit["missingOwnedTiles"])
            candidate = next(row for row in map01_audit["placements"] if row["tile"] == [1, 1])
            self.assertTrue(candidate["source"].endswith("h_map01_lv001_1_1_texture_variant.png"))

            clean01 = Image.open(export_root / cast(str, map01_result["clean"])).convert("RGB")
            clean02 = Image.open(export_root / cast(str, map02_result["clean"])).convert("RGB")
            self.assertEqual((4, 4), clean01.size)
            self.assertEqual((4, 4), clean02.size)
            self.assertEqual((0, 0, 0), clean01.getpixel((0, 2)), "Transparent white RGB must not leak into the base")
            self.assertEqual((100, 50, 25), clean01.getpixel((1, 2)), "Native alpha must blend against the black background")
            self.assertEqual((0, 0, 0), clean01.getpixel((2, 0)), "Another level's owned sector must stay protected")
            self.assertNotEqual((0, 0, 0), clean02.getpixel((2, 0)), "Unowned visible border must be displayed")
            self.assertNotEqual(clean01.getpixel((0, 0)), clean01.getpixel((0, 2)), "+Z north must be at the top of the image")
            self.assertNotEqual(clean01.getpixel((0, 0)), clean01.getpixel((2, 0)), "Increasing X must move east to the right")
            self.assertNotEqual(clean02.getpixel((0, 0)), clean02.getpixel((0, 2)), "Map02 +Z north must be at the top")
            self.assertNotEqual(clean02.getpixel((0, 0)), clean02.getpixel((2, 0)), "Map02 increasing X must move right")
            clean01.close()
            clean02.close()

            source_after = snapshot(source_files)
            self.assertEqual(source_before, source_after, "Runtime and prepared image sources changed during overview generation")
            self.assertTrue(output_manifest_path.resolve().is_relative_to(cache_root.resolve()))
            output_files = [path for path in export_root.rglob("*") if path.is_file()]
            self.assertTrue(output_files)
            self.assertTrue(all(path.resolve().is_relative_to(cache_root.resolve()) for path in output_files))
            self.assertFalse(any(path.resolve().is_relative_to(game_root.resolve()) for path in output_files))
            self.assertEqual("synthetic game marker; overview builder must not touch it\n", game_marker.read_text(encoding="utf-8"))

            outside_cache = root / "outside_cache_output"
            with self.assertRaisesRegex(ValueError, "inside the selected external cache"):
                build_map_overviews(runtime_path, outside_cache)
            self.assertFalse(outside_cache.exists())
            inside_game = game_root / "forbidden_overview_output"
            with self.assertRaisesRegex(ValueError, "inside the selected external cache"):
                build_map_overviews(runtime_path, inside_game)
            self.assertFalse(inside_game.exists())
            self.assertEqual(source_before, snapshot(source_files), "Rejected output paths changed source inputs")

    def test_verified_polygon_overlay_uses_rgb_without_changing_ownership(self) -> None:
        with tempfile.TemporaryDirectory(prefix="map_overview_polygon_") as temporary:
            root = Path(temporary)
            bounds = {"xmin": 0.0, "xmax": 256.0, "zmin": 0.0, "zmax": 256.0}
            grid = [["map01_lv001", "map01_lv001"], ["map01_lv001", "map01_lv002"]]
            spec, scene, sources = create_map_inputs(root, "map01", bounds, grid, True, True)
            write_tile(scene.parent / "tiles" / "h_map01_lv099_1_1.png", [(200, 100, 50, 0)] * 4)
            spec["previewOverlays"] = [{"levelId": "map01_lv099", "worldRect": bounds,
                "opacity": 128, "polygonsXZ": [[[0, 0], [64, 0], [64, 128], [0, 128]]]}]
            spec_before = json.dumps(spec, sort_keys=True)
            source_before = snapshot(sources)
            output = root / "map01"
            result = compose_map(spec, scene, output)
            with Image.open(output / "clean.png") as image:
                self.assertEqual((100, 50, 25), image.getpixel((0, 2)))
                self.assertEqual((0, 0, 0), image.getpixel((2, 0)))
                self.assertEqual((88, 155, 95), image.getpixel((3, 3)), "Pixels outside the polygon must remain unchanged")
            self.assertEqual(3, result["placedSectors"])
            self.assertEqual([], result["discoveredLevelsWithoutPlacement"])
            self.assertEqual(spec_before, json.dumps(spec, sort_keys=True))
            self.assertEqual(source_before, snapshot(sources))


if __name__ == "__main__":
    unittest.main()
