"""Discover and augment runtime map level metadata from extracted evidence.

The public WebUI manifest is intentionally static, while game-derived overview
images and level records live outside the repository. This module bridges that
boundary: it discovers level IDs from the same BundleScanner/SceneProbe outputs
used by the main extraction path, then derives conservative sector ownership
from MapRegionTable evidence. Unknown levels are never guessed from names alone.
"""

from __future__ import annotations

import ast
import copy
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


LEVEL_RE = re.compile(r"\b(?P<map>map\d{2})_(?P<level>lv\d{3})\b", re.IGNORECASE)
H_TILE_RE = re.compile(r"^h_(?P<map>map\d{2})_(?P<level>lv\d{3})_(?P<x>\d+)_(?P<z>\d+)$", re.IGNORECASE)
SECTOR_SIZE = 128


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _iter_records(path: Path) -> Iterable[dict[str, Any]]:
    """Read JSONL or Python-literal log records without trusting headers."""

    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        text = line.strip()
        if not text or not text.startswith(("{", "[")):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            try:
                value = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                continue
        if isinstance(value, dict):
            yield value


def _level_from_text(value: Any) -> str | None:
    match = LEVEL_RE.search(str(value or ""))
    if not match:
        return None
    return f"{match.group('map').lower()}_{match.group('level').lower()}"


def discover_level_tiles(scene_manifest: Path) -> dict[str, dict[str, Any]]:
    """Discover exact H tile coordinates from SceneProbe texture exports."""

    manifest = load_json(scene_manifest)
    result: dict[str, dict[str, Any]] = defaultdict(lambda: {"tiles": {}, "textureCount": 0})
    for row in manifest.get("TextureExports") or []:
        asset = row.get("Asset") or {}
        name = str(asset.get("Name") or "")
        match = H_TILE_RE.match(name)
        if not match or row.get("File") is None:
            continue
        level = f"{match['map'].lower()}_{match['level'].lower()}"
        # Mist/tier tiles are separate UI layers, not ownership H tiles.
        if "_mist_" in name.lower() or "_tier_" in name.lower():
            continue
        result[level]["tiles"][(int(match["x"]), int(match["z"]))] = {
            "asset": asset,
            "file": row.get("File"),
        }
        result[level]["textureCount"] += 1
    return dict(result)


def discover_level_ids_from_bundle_scan(bundle_scan: Path) -> set[str]:
    """Discover level IDs from container paths in BundleScanner JSONL."""

    levels: set[str] = set()
    for row in _iter_records(bundle_scan):
        if row.get("kind") != "match":
            continue
        for value in row.get("containers") or []:
            level = _level_from_text(value)
            if level:
                levels.add(level)
    return levels


def _shape_points(shape: Mapping[str, Any]) -> list[tuple[float, float]]:
    position = shape.get("position") or {}
    px = float(position.get("x", 0.0))
    pz = float(position.get("z", 0.0))
    points = shape.get("polyLinePoints") or []
    if points:
        return [(px + float(point.get("x", 0.0)), pz + float(point.get("y", 0.0))) for point in points]
    size = shape.get("size") or {}
    sx = float(size.get("x", 0.0)) / 2.0
    sz = float(size.get("z", 0.0)) / 2.0
    return [(px - sx, pz - sz), (px + sx, pz - sz), (px + sx, pz + sz), (px - sx, pz + sz)]


def region_bounds(region_records: Path) -> dict[str, dict[str, float]]:
    """Return conservative XZ envelopes for level records."""

    points: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in _iter_records(region_records):
        level = _level_from_text(row.get("levelId"))
        if not level:
            continue
        for shape in row.get("shapeList") or []:
            points[level].extend(_shape_points(shape))
    return {
        level: {
            "xmin": min(point[0] for point in values),
            "xmax": max(point[0] for point in values),
            "zmin": min(point[1] for point in values),
            "zmax": max(point[1] for point in values),
        }
        for level, values in points.items()
        if values
    }


def _align_down(value: float) -> int:
    return math.floor(value / SECTOR_SIZE) * SECTOR_SIZE


def _align_up(value: float) -> int:
    return math.ceil(value / SECTOR_SIZE) * SECTOR_SIZE


def _tile_grid(tile_info: Mapping[str, Any]) -> tuple[int, int]:
    tiles = tile_info.get("tiles") or {}
    if not tiles:
        return 0, 0
    return max(x for x, _ in tiles), max(z for _, z in tiles)


def _level_spec(
    level_id: str,
    tile_info: Mapping[str, Any],
    region: Mapping[str, float],
    existing: Mapping[str, Any] | None,
    existing_levels: Iterable[Mapping[str, Any]],
    map_bounds: Mapping[str, float],
) -> dict[str, Any]:
    if existing is not None:
        return copy.deepcopy(dict(existing))
    tile_x, tile_z = _tile_grid(tile_info)
    if not tile_x or not tile_z:
        raise ValueError(f"Cannot infer H tile grid for {level_id}")
    xmin = _align_down(float(region["xmin"]))
    zmin = _align_down(float(region["zmin"]))
    width = max(tile_x * SECTOR_SIZE, _align_up(float(region["xmax"])) - xmin)
    height = max(tile_z * SECTOR_SIZE, _align_up(float(region["zmax"])) - zmin)
    xmax = xmin + width
    zmax = zmin + height
    if xmin < float(map_bounds["xmin"]):
        xmax += float(map_bounds["xmin"]) - xmin
        xmin = int(map_bounds["xmin"])
    if zmin < float(map_bounds["zmin"]):
        zmax += float(map_bounds["zmin"]) - zmin
        zmin = int(map_bounds["zmin"])
    xmax = min(xmax, float(map_bounds["xmax"]))
    zmax = min(zmax, float(map_bounds["zmax"]))
    used_ids = [int(item.get("idNum")) for item in existing_levels if item.get("idNum") is not None]
    inferred_id = max(used_ids or [0]) + 1
    return {
        "levelId": level_id,
        "idNum": inferred_id,
        "idNumSource": "inferred_runtime_discovery",
        "worldRect": {"xmin": xmin, "xmax": int(xmax), "zmin": zmin, "zmax": int(zmax)},
        "hTileGrid": {"x": tile_x, "z": tile_z},
        "ownershipSectorCount": 0,
        "discovery": {
            "textureCount": int(tile_info.get("textureCount", 0)),
            "regionEnvelope": dict(region),
        },
    }


def augment_runtime_manifest(
    base_manifest: Mapping[str, Any],
    *,
    scene_manifest: Path,
    region_records: Path,
    map_id: str = "map02",
) -> dict[str, Any]:
    """Add newly discovered levels and fill only currently-unmapped sectors."""

    manifest = copy.deepcopy(dict(base_manifest))
    map_spec = manifest["maps"][map_id]
    tile_levels = discover_level_tiles(scene_manifest)
    bounds_by_level = region_bounds(region_records)
    existing_levels = {str(row["levelId"]): row for row in map_spec.get("levels") or []}
    discovered = sorted(set(tile_levels) | set(bounds_by_level))
    added: list[str] = []
    for level_id in discovered:
        if not level_id.startswith(f"{map_id}_"):
            continue
        if level_id not in bounds_by_level:
            continue
        spec = _level_spec(
            level_id,
            tile_levels.get(level_id, {"tiles": {}, "textureCount": 0}),
            bounds_by_level[level_id],
            existing_levels.get(level_id),
            map_spec.get("levels") or [],
            map_spec["worldBounds"],
        )
        if level_id not in existing_levels:
            map_spec.setdefault("levels", []).append(spec)
            existing_levels[level_id] = spec
            added.append(level_id)

        region = bounds_by_level[level_id]
        grid = map_spec["ownershipGrid"]
        origin_x = math.floor(float(map_spec["worldBounds"]["xmin"]) / SECTOR_SIZE)
        origin_z = math.floor(float(map_spec["worldBounds"]["zmin"]) / SECTOR_SIZE)
        count = 0
        for row_index in range(len(grid)):
            sector_z = origin_z + row_index
            for column_index in range(len(grid[row_index])):
                if grid[row_index][column_index] is not None:
                    continue
                sector_x = origin_x + column_index
                if (
                    sector_x * SECTOR_SIZE < float(region["xmax"])
                    and (sector_x + 1) * SECTOR_SIZE > float(region["xmin"])
                    and sector_z * SECTOR_SIZE < float(region["zmax"])
                    and (sector_z + 1) * SECTOR_SIZE > float(region["zmin"])
                ):
                    grid[row_index][column_index] = level_id
                    count += 1
        existing_levels[level_id]["ownershipSectorCount"] = sum(
            1 for row in grid for value in row if value == level_id
        )
        existing_levels[level_id].setdefault("discovery", {})["newlyMappedSectorCount"] = count
    manifest.setdefault("sourceEvidence", {})["runtimeDiscovery"] = "BundleScanner/SceneProbe H tiles + MapRegionTable evidence"
    manifest["sourceEvidence"]["runtimeDiscoveredLevels"] = ",".join(added) if added else "none"
    return manifest
