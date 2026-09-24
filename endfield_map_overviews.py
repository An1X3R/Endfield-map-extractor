"""Build WebUI overviews from prepared H tiles and the runtime sector contract."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import cast

from PIL import Image, ImageChops, ImageDraw

from endfield_scene_material_contract import require_object
from generate_runtime_overview import TILE_RE, choose_tile_candidates, container_paths


FORMAT = "EndfieldMapOverviews/1"
LAYOUT_PATH = Path(__file__).resolve().parent / "webui" / "src" / "data" / "region_map_overview_layout.json"


def read_object(path: Path) -> dict[str, object]:
    return require_object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))


def input_path(value: object, parent: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Overview input requires a non-empty path")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else parent / path).resolve()


def standard_tiles(scene: dict[str, object], root: Path, level: str) -> dict[tuple[int, int], Path]:
    """Use the normal map texture layer, whose alpha defines the visible footprint."""
    containers = container_paths(scene)
    allowed = {key for key, paths in containers.items() if any(
        "/ui/textures/levelmap/levelmapchunks/" in value.replace("\\", "/").lower() for value in paths
    )}
    exports = cast(list[dict[str, object]], scene["TextureExports"])
    selected = []
    for row in exports:
        asset = require_object(row["Asset"], "texture asset")
        if (str(asset.get("Source", "")), str(asset.get("PathId", ""))) in allowed:
            selected.append(row)
    tiles, _ = choose_tile_candidates({**scene, "TextureExports": selected}, root, level)
    return tiles


def load_tile(path: Path, root: Path, size: tuple[int, int]) -> Image.Image:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"H tile escapes the prepared scene directory: {path}")
    with Image.open(path) as source:
        return source.convert("RGBA").resize(size, Image.Resampling.LANCZOS)


def compose_map(spec: dict[str, object], scene_path: Path, output: Path) -> dict[str, object]:
    """Extend image coverage without changing ownership or the sector export contract."""
    scene = read_object(scene_path)
    bounds = require_object(spec["worldBounds"], "world bounds")
    xmin, xmax, zmin, zmax = (float(cast(float, bounds[k])) for k in ("xmin", "xmax", "zmin", "zmax"))
    size = int(cast(int, spec["sectorSizeMetres"]))
    if size != 128 or spec["ownershipGridOrder"] != "rows south-to-north; columns west-to-east":
        raise ValueError("Unsupported overview sector orientation or size")
    overview = require_object(spec["overview"], "overview")
    dimensions = require_object(overview["clean"], "clean overview dimensions")
    width, height = int(cast(int, dimensions["pixelWidth"])), int(cast(int, dimensions["pixelHeight"]))
    if not 0 < width <= 8192 or not 0 < height <= 8192 or xmax <= xmin or zmax <= zmin:
        raise ValueError("Invalid overview image or world dimensions")
    grid = spec["ownershipGrid"]
    if not isinstance(grid, list) or len(grid) != (zmax - zmin) / size:
        raise ValueError("Overview ownership rows do not match world bounds")
    levels = spec["levels"]
    if not isinstance(levels, list):
        raise ValueError("Overview levels must be a list")
    specs = {str(require_object(row, "level")["levelId"]): require_object(row, "level") for row in levels}
    for row in grid:
        if not isinstance(row, list) or len(row) != (xmax - xmin) / size:
            raise ValueError("Overview ownership columns do not match world bounds")
        if any(level is not None and level not in specs for level in row):
            raise ValueError("Overview ownership references an unknown level")
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    placements: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []
    owned: set[tuple[int, int]] = set()

    def pixels(world_x: float, world_z: float) -> tuple[int, int, int, int]:
        return (round((world_x - xmin) / (xmax - xmin) * width),
                round((zmax - world_z - size) / (zmax - zmin) * height),
                round((world_x + size - xmin) / (xmax - xmin) * width),
                round((zmax - world_z) / (zmax - zmin) * height))

    for level, level_spec in specs.items():
        rect = require_object(level_spec["worldRect"], "level world rectangle")
        tiles = standard_tiles(scene, scene_path.parent, level)
        for (tx, tz), path in sorted(tiles.items()):
            world_x = float(cast(float, rect["xmin"])) + (tx - 1) * size
            world_z = float(cast(float, rect["zmin"])) + (tz - 1) * size
            column, row_index = (world_x - xmin) / size, (world_z - zmin) / size
            if not column.is_integer() or not row_index.is_integer():
                raise ValueError(f"Level is not aligned to H tiles: {level}")
            column, row_index = int(column), int(row_index)
            if not (0 <= row_index < len(grid) and 0 <= column < len(grid[row_index])):
                continue
            owner = grid[row_index][column]
            if owner is not None and owner != level:
                continue
            x0, y0, x1, y1 = pixels(world_x, world_z)
            tile = load_tile(path, scene_path.parent, (x1 - x0, y1 - y0))
            if owner == level:
                owned.add((column, row_index))
            if owner is None and tile.getchannel("A").getbbox() is None:
                tile.close()
                continue
            image.alpha_composite(tile, (x0, y0))
            tile.close()
            placements.append({"levelId": level, "tile": [tx, tz], "sector": [world_x / size, world_z / size],
                "source": str(path), "pixels": [x0, y0, x1, y1], "coverage": "owned" if owner else "preview_only"})
    for row_index, row in enumerate(grid):
        for column, level in enumerate(row):
            if level is not None and (column, row_index) not in owned:
                missing.append({"levelId": level, "sector": [xmin / size + column, zmin / size + row_index]})

    overlays = spec.get("previewOverlays", [])
    if not isinstance(overlays, list):
        raise ValueError("Preview overlays must be a list")
    overlay_audits = []
    placed_overlay_levels: set[str] = set()
    for value in overlays:
        overlay_spec = require_object(value, "preview overlay")
        level = str(overlay_spec["levelId"])
        rect = require_object(overlay_spec["worldRect"], "overlay world rectangle")
        opacity = int(cast(int, overlay_spec["opacity"]))
        if not 0 <= opacity <= 255:
            raise ValueError(f"Invalid overview opacity: {level}")
        polygons = overlay_spec["polygonsXZ"]
        if not isinstance(polygons, list) or not polygons:
            raise ValueError(f"Preview overlay needs verified visibility polygons: {level}")
        tiles, tile_audit = choose_tile_candidates(scene, scene_path.parent, level)
        if not tiles:
            overlay_audits.append({"levelId": level, "status": "missing_source_tiles"})
            continue
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        mask = Image.new("L", image.size, 0)
        painter = ImageDraw.Draw(mask)
        for polygon in polygons:
            if not isinstance(polygon, list) or len(polygon) < 3:
                raise ValueError(f"Invalid visibility polygon: {level}")
            points = [(round((float(point[0]) - xmin) / (xmax - xmin) * width),
                       round((zmax - float(point[1])) / (zmax - zmin) * height)) for point in polygon]
            painter.polygon(points, fill=255)
        for (tx, tz), path in sorted(tiles.items()):
            world_x = float(cast(float, rect["xmin"])) + (tx - 1) * size
            world_z = float(cast(float, rect["zmin"])) + (tz - 1) * size
            x0, y0, x1, y1 = pixels(world_x, world_z)
            tile = load_tile(path, scene_path.parent, (x1 - x0, y1 - y0))
            # This reviewed overlay uses RGB plus MapRegion polygons, not texture alpha.
            tile.putalpha(opacity)
            layer.alpha_composite(tile, (x0, y0))
            tile.close()
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
        image.alpha_composite(layer)
        layer.close()
        mask.close()
        placed_overlay_levels.add(level)
        overlay_audits.append({"levelId": level, "status": "placed", "tiles": len(tiles), "tileCandidates": tile_audit})
    if not placements and not placed_overlay_levels:
        raise ValueError(f"No source H tiles match the current layout: {scene_path}")
    output.mkdir(parents=True, exist_ok=False)
    background = Image.new("RGBA", image.size, (0, 0, 0, 255))
    background.alpha_composite(image)
    image.close()
    image = background
    image.convert("RGB").save(output / "clean.png")
    overlay = image.copy()
    draw = ImageDraw.Draw(overlay)
    for record in placements:
        if record["coverage"] != "owned":
            continue
        x0, y0, x1, y1 = cast(list[int], record["pixels"])
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(80, 110, 100, 180), width=1)
    overlay.convert("RGB").save(output / "sectors.png")
    image.close()
    overlay.close()
    exports = cast(list[dict[str, object]], scene["TextureExports"])
    discovered: set[str] = set()
    for row in exports:
        asset = require_object(row["Asset"], "texture asset")
        match = TILE_RE.match(str(asset.get("Name", "")))
        if match:
            discovered.add(f"{match['map'].lower()}_{match['level'].lower()}")
    unplaced = sorted(discovered - specs.keys() - placed_overlay_levels)
    audit = {"placements": placements, "missingOwnedTiles": missing,
             "discoveredLevelsWithoutPlacement": unplaced, "overlays": overlay_audits}
    (output / "tiles.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"clean": f"{output.name}/clean.png", "sectors": f"{output.name}/sectors.png",
            "tileAudit": f"{output.name}/tiles.json", "placedSectors": len(owned),
            "previewOnlyTiles": sum(row["coverage"] == "preview_only" for row in placements),
            "missingOwnedTiles": len(missing), "discoveredLevelsWithoutPlacement": unplaced,
            "pixelWidth": width, "pixelHeight": height}


def build_map_overviews(runtime_path: Path, output: Path) -> dict[str, object]:
    """Create a new cache without modifying the runtime configuration or source assets."""
    runtime = read_object(runtime_path)
    if runtime.get("format") != "EndfieldRuntimeConfig/1":
        raise ValueError(f"Unsupported runtime config: {runtime_path}")
    paths = require_object(runtime["paths"], "runtime paths")
    game = input_path(paths["game_root"], runtime_path.parent)
    cache = input_path(paths["cache_root"], runtime_path.parent)
    output = output.resolve()
    if not output.is_relative_to(cache) or output.is_relative_to(game):
        raise ValueError("Overview output must stay inside the selected external cache")
    if output.exists():
        raise FileExistsError(f"Overview output must be new: {output}")
    extraction = require_object(runtime["extraction"], "runtime extraction")
    descriptor = require_object(extraction["runtimeManifest"], "runtime manifest")
    manifest_path = input_path(descriptor["path"], runtime_path.parent)
    manifest = read_object(manifest_path)
    if manifest.get("format") != "EndfieldRegionMapRuntimeManifest/1":
        raise ValueError(f"Unsupported map layout: {manifest_path}")
    map_specs = require_object(manifest["maps"], "map layouts")
    preview_layout = read_object(LAYOUT_PATH)
    if preview_layout.get("format") != "EndfieldMapOverviewLayout/1":
        raise ValueError(f"Unsupported preview layout: {LAYOUT_PATH}")
    preview_maps = require_object(preview_layout["maps"], "preview maps")
    sources = require_object(extraction["maps"], "prepared maps")
    output.mkdir(parents=True)
    maps: dict[str, object] = {}
    for map_id in ("map01", "map02"):
        entry = require_object(sources[map_id], map_id)
        probe = require_object(entry["sceneProbe"], "SceneProbe")
        scene_path = input_path(probe["path"], runtime_path.parent)
        preview_spec = require_object(preview_maps[map_id], "preview layout")
        spec = {**require_object(map_specs[map_id], map_id), "previewOverlays": preview_spec["overlays"]}
        maps[map_id] = compose_map(spec, scene_path, output / map_id)
    target = output / "overview_manifest.json"
    target.write_text(json.dumps({"format": FORMAT, "status": "ready", "maps": maps,
        "sourceRuntimeManifest": str(manifest_path), "sourceRuntimeConfig": str(runtime_path),
        "previewLayoutSha256": hashlib.sha256(LAYOUT_PATH.read_bytes()).hexdigest().upper(),
        "compositionPolicy": "Native-alpha texture tiles; owned sectors protected; unowned visible tiles are preview-only; reviewed polygon overlays",
        "datasetFingerprint": extraction["mapLayerFingerprint"],
        "imageOrientation": "+Z north/up; -X west/left"}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest().upper(), "bytes": target.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_map_overviews(args.runtime_config.resolve(), args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
