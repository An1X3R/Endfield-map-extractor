"""Compose an external runtime overview from an existing overview and H tiles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw


TILE_RE = re.compile(r"^h_(?P<map>map\d{2})_(?P<level>lv\d{3})_(?P<x>\d+)_(?P<z>\d+)$", re.IGNORECASE)


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def region_polygons(path: Path, level_id: str) -> list[list[tuple[float, float]]]:
    polygons: list[list[tuple[float, float]]] = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            try:
                import ast

                row = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                continue
        if not isinstance(row, dict) or str(row.get("levelId", "")).casefold() != level_id.casefold():
            continue
        # Mist polygons are the visible level footprint. Tier/helper records
        # describe sub-scenes and must not turn the entire inferred worldRect
        # into a painted rectangle.
        if row.get("isMist") is not True:
            continue
        for shape in row.get("shapeList") or []:
            position = shape.get("position") or {}
            px = float(position.get("x", 0.0))
            pz = float(position.get("z", 0.0))
            points = shape.get("polyLinePoints") or []
            if points:
                polygons.append([(px + float(p.get("x", 0.0)), pz + float(p.get("y", 0.0))) for p in points])
                continue
            size = shape.get("size") or {}
            sx = float(size.get("x", 0.0)) / 2.0
            sz = float(size.get("z", 0.0)) / 2.0
            polygons.append([(px - sx, pz - sz), (px + sx, pz - sz), (px + sx, pz + sz), (px - sx, pz + sz)])
    return polygons


def container_paths(scene_manifest: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    paths: dict[tuple[str, str], list[str]] = {}
    for row in scene_manifest.get("Containers") or []:
        asset = row.get("Asset") or {}
        key = (str(asset.get("Source") or ""), str(asset.get("PathId") or ""))
        container = str(row.get("Container") or "")
        if key != ("", "") and container:
            paths.setdefault(key, []).append(container)
    return paths


def tile_layer_rank(paths: list[str]) -> tuple[int, str]:
    """Prefer the normal sprite chunk over texture/state variants.

    The game ships same-named H tiles in both ``ui/sprites/levelmap`` and
    ``ui/textures/levelmap``. The former is the visible map image used by the
    established overview chain; the latter can be a green or masked state
    variant. Ordering is semantic and independent of manifest traversal order.
    """

    normalized = [path.replace("\\", "/").lower() for path in paths]
    if any("/ui/sprites/levelmap/levelmapchunks/" in path for path in normalized):
        return 3, "sprites_levelmapchunks"
    if any("/ui/textures/levelmap/levelmapchunks/" in path for path in normalized):
        return 2, "textures_levelmapchunks"
    return 0, "unknown"


def choose_tile_candidates(scene_manifest: dict[str, Any], manifest_root: Path, level_id: str) -> tuple[dict[tuple[int, int], Path], dict[str, Any]]:
    candidates: dict[tuple[int, int], list[dict[str, Any]]] = {}
    paths_by_asset = container_paths(scene_manifest)
    for row in scene_manifest.get("TextureExports") or []:
        asset = row.get("Asset") or {}
        name = str(asset.get("Name") or "")
        match = TILE_RE.match(name)
        if not match or f"{match['map'].lower()}_{match['level'].lower()}" != level_id.lower():
            continue
        if "_mist_" in name.lower() or "_tier_" in name.lower():
            continue
        path = manifest_root / str(row.get("File", "")).replace("\\", "/")
        if path.is_file():
            asset_key = (str(asset.get("Source") or ""), str(asset.get("PathId") or ""))
            container_list = paths_by_asset.get(asset_key, [])
            rank, layer = tile_layer_rank(container_list)
            candidates.setdefault((int(match["x"]), int(match["z"])), []).append(
                {"path": path, "rank": rank, "layer": layer, "containers": container_list}
            )

    selected: dict[tuple[int, int], Path] = {}
    audit: dict[str, Any] = {}
    for coordinate, values in sorted(candidates.items()):
        values.sort(
            key=lambda item: (item["rank"], str(item["path"])),
            reverse=True,
        )
        selected[coordinate] = values[0]["path"]
        audit[f"{coordinate[0]}_{coordinate[1]}"] = {
            "selected": str(values[0]["path"]),
            "selectedLayer": values[0]["layer"],
            "candidates": [
                {
                    "path": str(value["path"]),
                    "layer": value["layer"],
                    "rank": value["rank"],
                    "containers": value["containers"],
                }
                for value in values
            ],
        }
    return selected, audit


def main() -> int:
    parser = argparse.ArgumentParser(description="Compose an external Endfield runtime overview")
    parser.add_argument("--base-image", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--scene-manifest", type=Path, required=True)
    parser.add_argument("--region-records", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--map-id", default="map02")
    parser.add_argument("--level-id", required=True)
    parser.add_argument(
        "--mask-mode",
        choices=("polygon", "none"),
        default="polygon",
        help="Clip updated tiles to visible region polygons, or keep the complete H-tile grid.",
    )
    args = parser.parse_args()
    out = args.output_dir.resolve()
    if out.exists():
        raise FileExistsError(f"Refusing to overwrite: {out}")
    out.mkdir(parents=True)
    runtime = json.loads(args.runtime_manifest.read_text(encoding="utf-8"))
    spec = next(level for level in runtime["maps"][args.map_id]["levels"] if level["levelId"] == args.level_id)
    base = Image.open(args.base_image).convert("RGBA")
    width, height = base.size
    bounds = runtime["maps"][args.map_id]["worldBounds"]
    world_width = float(bounds["xmax"] - bounds["xmin"])
    world_height = float(bounds["zmax"] - bounds["zmin"])
    level_bounds = spec["worldRect"]
    scene = json.loads(args.scene_manifest.read_text(encoding="utf-8"))
    tile_rows, tile_audit = choose_tile_candidates(
        scene, args.scene_manifest.parent, args.level_id
    )
    tile_px = max(1, round(128.0 / world_width * width))
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    for (tile_x, tile_z), path in tile_rows.items():
        world_x = float(level_bounds["xmin"]) + (tile_x - 1) * 128.0
        world_z = float(level_bounds["zmin"]) + (tile_z - 1) * 128.0
        px = round((world_x - float(bounds["xmin"])) / world_width * width)
        py = round((float(bounds["zmax"]) - world_z - 128.0) / world_height * height)
        tile = Image.open(path).convert("RGBA").resize((tile_px, tile_px), Image.Resampling.LANCZOS)
        tile.putalpha(220)
        overlay.alpha_composite(tile, (px, py))
    if args.mask_mode == "polygon":
        region_mask = Image.new("L", base.size, 0)
        mask_draw = ImageDraw.Draw(region_mask)
        for polygon in region_polygons(args.region_records, args.level_id):
            pixels = [
                (
                    round((x - float(bounds["xmin"])) / world_width * width),
                    round((float(bounds["zmax"]) - z) / world_height * height),
                )
                for x, z in polygon
            ]
            if len(pixels) >= 3:
                mask_draw.polygon(pixels, fill=255)
        overlay.putalpha(ImageChops.multiply(overlay.getchannel("A"), region_mask))
    clean = Image.alpha_composite(base, overlay).convert("RGB")
    clean_path = out / "map02_overview_updated_clean.png"
    clean.save(clean_path)
    sectors = clean.convert("RGBA")
    draw = ImageDraw.Draw(sectors, "RGBA")
    grid = runtime["maps"][args.map_id]["ownershipGrid"]
    origin_x = int(float(bounds["xmin"]) // 128)
    origin_z = int(float(bounds["zmin"]) // 128)
    for row_index, row in enumerate(grid):
        for column_index, value in enumerate(row):
            if value != args.level_id:
                continue
            sx = origin_x + column_index
            sz = origin_z + row_index
            x0 = round((sx * 128 - float(bounds["xmin"])) / world_width * width)
            x1 = round(((sx + 1) * 128 - float(bounds["xmin"])) / world_width * width)
            y0 = round((float(bounds["zmax"]) - (sz + 1) * 128) / world_height * height)
            y1 = round((float(bounds["zmax"]) - sz * 128) / world_height * height)
            draw.rectangle((x0, y0, x1, y1), outline=(255, 171, 48, 255), width=5)
    sectors_path = out / "map02_overview_updated_sectors.png"
    sectors.convert("RGB").save(sectors_path)
    payload = {
        "format": "EndfieldRuntimeOverview/1",
        "mapId": args.map_id,
        "levelId": args.level_id,
        "levelWorldRect": level_bounds,
        "sourceBaseImage": str(args.base_image.resolve()),
        "sourceRuntimeManifest": str(args.runtime_manifest.resolve()),
        "sourceSceneManifest": str(args.scene_manifest.resolve()),
        "sourceRegionRecords": str(args.region_records.resolve()),
        "tileCount": len(tile_rows),
        "tileCandidateCount": sum(
            len(item["candidates"]) for item in tile_audit.values()
        ),
        "tileSelection": tile_audit,
        "regionPolygonCount": len(region_polygons(args.region_records, args.level_id)),
        "maskMode": args.mask_mode,
        "ownershipSectorCount": sum(value == args.level_id for row in grid for value in row),
        "clean": {"path": str(clean_path), "bytes": clean_path.stat().st_size, "sha256": hash_file(clean_path)},
        "sectors": {"path": str(sectors_path), "bytes": sectors_path.stat().st_size, "sha256": hash_file(sectors_path)},
        "confidence": "derived overview composition; not an official game map image",
    }
    (out / "overview_manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
