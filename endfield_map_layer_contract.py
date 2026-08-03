"""Reusable contracts and helpers for Endfield map-layer datasets.

The module is dependency-free and never reads the game installation directly.
It normalizes already extracted evidence into deterministic, sector-addressable
records suitable for a later local API or desktop UI.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


MANIFEST_FORMAT = "EndfieldRegionMapLayerManifest/1"
DATASET_FORMAT = "EndfieldMapLayerDataset/1"
VALIDATION_FORMAT = "EndfieldMapLayerValidation/1"
GENERATOR_VERSION = "stage119-v1"
SECTOR_SIZE = 128

DECIMAL_INTEGER_PATTERN = re.compile(r"^-?[0-9]+$")
PATH_ID_FIELD_NAMES = frozenset(
    {
        "pathId",
        "PathId",
        "transformPathId",
        "parentTransformPathId",
        "texturePathId",
    }
)

CATEGORIES = (
    "building",
    "prop",
    "road",
    "vegetation",
    "terrain-structure",
    "unknown",
)

_RECORD_REQUIRED_FIELDS: Mapping[str, tuple[str, ...]] = {
    "instance": ("instanceId", "assetId", "positionUnity", "anchorSector", "category", "bindingStatus"),
    "water": ("waterId", "geometry", "sectorKeys", "waterType", "bindingStatus"),
    "effect": ("effectId", "anchorId", "fullSystemGroup", "positionUnity", "sectorKeys", "bindingStatus"),
    "effect-anchor": (
        "anchorId",
        "memberEffectIds",
        "prototypeMemberIds",
        "anchorSelectionPolicy",
        "bindingStatus",
    ),
    "effect-prototype-member": (
        "prototypeMemberId",
        "prototypeId",
        "memberIndex",
        "localPositionUnity",
    ),
    "effect-prototype": ("prototypeId", "name", "memberIds", "rootAsset"),
    "light": ("lightId", "anchorId", "type", "bindingStatus"),
}

_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "vegetation",
        (
            "vegetation",
            "tree",
            "grass",
            "plant",
            "bush",
            "shrub",
            "flower",
            "bamboo",
            "vine",
            "farmod",
        ),
    ),
    (
        "road",
        (
            "road",
            "bridge",
            "rail",
            "track",
            "highway",
            "street",
            "tystair",
        ),
    ),
    (
        "terrain-structure",
        (
            "terrain",
            "cliff",
            "mountain",
            "ground",
            "slope",
            "rock",
            "landscape",
            "quarry",
        ),
    ),
    (
        "building",
        (
            "building",
            "house",
            "tower",
            "factory",
            "warehouse",
            "roof",
            "wall",
            "window",
            "door",
            "floor",
            "module",
            "city",
            "architecture",
        ),
    ),
    (
        "prop",
        (
            "prop",
            "crate",
            "barrel",
            "lamp",
            "sign",
            "bench",
            "fence",
            "pipe",
            "machine",
            "vehicle",
            "container",
            "decal",
            "statue",
        ),
    ),
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(*parts: Any, prefix: str | None = None, length: int = 24) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x1f")
    token = digest.hexdigest()[:length]
    return f"{prefix}:{token}" if prefix else token


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().upper()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest().upper()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise FileExistsError(temporary)
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def write_jsonl_gzip(path: Path, rows: Iterable[Mapping[str, Any]]) -> tuple[int, str, int]:
    """Write deterministic gzip JSONL and return count, SHA256, and bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise FileExistsError(temporary)
    count = 0
    with temporary.open("xb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as text:
                for row in rows:
                    text.write(canonical_json(dict(row)))
                    text.write("\n")
                    count += 1
    os.replace(temporary, path)
    return count, sha256_file(path), path.stat().st_size


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_new_output_root(
    path: Path,
    *,
    export_root: Path | None = None,
    game_root: Path | None = None,
) -> Path:
    resolved = path.expanduser().resolve()
    configured_export = export_root or (
        Path(os.environ["ENDFIELD_EXPORT_ROOT"]) if os.environ.get("ENDFIELD_EXPORT_ROOT") else None
    )
    if configured_export is None:
        raise ValueError(
            "Export root is not configured. Pass --export-root or set ENDFIELD_EXPORT_ROOT."
        )
    export_root = configured_export.expanduser().resolve()
    configured_game = game_root or (
        Path(os.environ["ENDFIELD_GAME_ROOT"]) if os.environ.get("ENDFIELD_GAME_ROOT") else None
    )
    if configured_game is not None:
        game_root = configured_game.expanduser().resolve()
        try:
            resolved.relative_to(game_root)
        except ValueError:
            pass
        else:
            raise ValueError("Output root cannot be inside the game installation")
    try:
        resolved.relative_to(export_root)
    except ValueError as exc:
        raise ValueError(f"Output root must be inside {export_root}") from exc
    if resolved.exists():
        raise FileExistsError(f"Refusing to reuse an existing output root: {resolved}")
    resolved.mkdir(parents=True)
    return resolved


def parse_matrix_blob(blob: bytes | bytearray | memoryview) -> list[float]:
    data = bytes(blob)
    if len(data) != 64:
        raise ValueError(f"Expected a 64-byte float matrix, got {len(data)} bytes")
    values = [float(value) for value in struct.unpack("<16f", data)]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Matrix contains a non-finite value")
    return values


def classify_entity(entity_base: str) -> tuple[str, str | None]:
    lowered = str(entity_base or "").casefold()
    for category, tokens in _CATEGORY_RULES:
        for token in tokens:
            if token in lowered:
                return category, token
    return "unknown", None


def classify_effect(value: str) -> str:
    lowered = str(value or "").casefold()
    if "waterfall" in lowered:
        return "waterfall"
    if "steam" in lowered:
        return "steam"
    if "smoke" in lowered:
        return "smoke"
    if "fire" in lowered:
        return "fire"
    if any(token in lowered for token in ("energy", "zhenlie", "fangyuzhenlie", "flow")):
        return "energy-flow"
    if any(token in lowered for token in ("lightbeam", "dragoneye", "light", "pulse")):
        return "light-pulse"
    return "particle"


def infer_source_level(map_id: str, *values: Any) -> str | None:
    joined = " ".join(str(value or "") for value in values).casefold().replace("\\", "/")
    patterns = (
        rf"{re.escape(map_id.casefold())}[/_]?lv0*(\d{{1,3}})",
        r"(?:^|[/_])lv0*(\d{1,3})(?:[/_]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, joined)
        if match:
            return f"{map_id}_lv{int(match.group(1)):03d}"
    return None


def sector_key(sector_x: int, sector_z: int) -> str:
    return f"sector:{int(sector_x)}:{int(sector_z)}"


def sector_filename(sector_x: int, sector_z: int) -> str:
    return f"s{int(sector_x):+05d}_{int(sector_z):+05d}.jsonl.gz"


def sector_for_point(x: float, z: float) -> tuple[int, int]:
    if not math.isfinite(x) or not math.isfinite(z):
        raise ValueError("World coordinates must be finite")
    return math.floor(x / SECTOR_SIZE), math.floor(z / SECTOR_SIZE)


def sector_keys_for_bounds(bounds: Mapping[str, float]) -> list[str]:
    xmin = float(bounds["xmin"])
    xmax = float(bounds["xmax"])
    zmin = float(bounds["zmin"])
    zmax = float(bounds["zmax"])
    if not all(math.isfinite(value) for value in (xmin, xmax, zmin, zmax)):
        raise ValueError("Bounds must be finite")
    if xmax < xmin or zmax < zmin:
        raise ValueError("Bounds are inverted")
    if xmax == xmin and zmax == zmin:
        sx, sz = sector_for_point(xmin, zmin)
        return [sector_key(sx, sz)]
    sx0 = math.floor(xmin / SECTOR_SIZE)
    sz0 = math.floor(zmin / SECTOR_SIZE)
    sx1 = math.ceil(xmax / SECTOR_SIZE) - 1 if xmax > xmin else sx0
    sz1 = math.ceil(zmax / SECTOR_SIZE) - 1 if zmax > zmin else sz0
    return [
        sector_key(sx, sz)
        for sz in range(sz0, sz1 + 1)
        for sx in range(sx0, sx1 + 1)
    ]


def bounds_from_minmax(minimum: Sequence[float], maximum: Sequence[float]) -> dict[str, float]:
    if len(minimum) < 3 or len(maximum) < 3:
        raise ValueError("3D bounds require three-component min/max values")
    return {
        "xmin": float(minimum[0]),
        "xmax": float(maximum[0]),
        "ymin": float(minimum[1]),
        "ymax": float(maximum[1]),
        "zmin": float(minimum[2]),
        "zmax": float(maximum[2]),
    }


def union_bounds(rows: Iterable[Mapping[str, float]]) -> dict[str, float] | None:
    values = list(rows)
    if not values:
        return None
    return {
        "xmin": min(float(row["xmin"]) for row in values),
        "xmax": max(float(row["xmax"]) for row in values),
        "ymin": min(float(row["ymin"]) for row in values),
        "ymax": max(float(row["ymax"]) for row in values),
        "zmin": min(float(row["zmin"]) for row in values),
        "zmax": max(float(row["zmax"]) for row in values),
    }


def convex_hull(points: Iterable[Sequence[float]]) -> list[list[float]]:
    unique = sorted({(float(point[0]), float(point[1])) for point in points})
    if len(unique) <= 1:
        return [[x, z] for x, z in unique]

    def cross(origin: tuple[float, float], left: tuple[float, float], right: tuple[float, float]) -> float:
        return (left[0] - origin[0]) * (right[1] - origin[1]) - (left[1] - origin[1]) * (right[0] - origin[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    hull = lower[:-1] + upper[:-1]
    if hull and hull[0] != hull[-1]:
        hull.append(hull[0])
    return [[x, z] for x, z in hull]


@dataclass(frozen=True)
class SectorResolution:
    map_id: str
    domain_id: str
    sector_x: int
    sector_z: int
    key: str
    level_id: str | None
    coverage: str
    unity_bounds_xz: dict[str, float]
    blender_bounds_xy: dict[str, float]


class RuntimeResolver:
    def __init__(self, manifest: Mapping[str, Any]):
        if manifest.get("format") != "EndfieldRegionMapRuntimeManifest/1":
            raise ValueError("Unsupported runtime manifest format")
        if manifest.get("resolverVersion") != "endfield-sector128-v1":
            raise ValueError("Unsupported resolver version")
        self.manifest = dict(manifest)
        self.maps: Mapping[str, Any] = self.manifest["maps"]

    @classmethod
    def from_path(cls, path: Path) -> "RuntimeResolver":
        return cls(load_json(path))

    def map(self, map_id: str) -> Mapping[str, Any]:
        try:
            return self.maps[map_id]
        except KeyError as exc:
            raise ValueError(f"Unknown map: {map_id}") from exc

    def level_from_id_num(self, map_id: str, id_num: int | None) -> str | None:
        if id_num is None:
            return None
        for level in self.map(map_id).get("levels") or []:
            if int(level.get("idNum")) == int(id_num):
                return str(level["levelId"])
        return None

    def resolve_sector(self, map_id: str, sector_x: int, sector_z: int) -> SectorResolution:
        spec = self.map(map_id)
        bounds = spec["worldBounds"]
        origin_x = math.floor(float(bounds["xmin"]) / SECTOR_SIZE)
        origin_z = math.floor(float(bounds["zmin"]) / SECTOR_SIZE)
        column = int(sector_x) - origin_x
        row = int(sector_z) - origin_z
        grid = spec["ownershipGrid"]
        level_id = None
        if 0 <= row < len(grid) and 0 <= column < len(grid[row]):
            level_id = grid[row][column]
        world = {
            "xmin": float(sector_x * SECTOR_SIZE),
            "xmax": float((sector_x + 1) * SECTOR_SIZE),
            "zmin": float(sector_z * SECTOR_SIZE),
            "zmax": float((sector_z + 1) * SECTOR_SIZE),
        }
        return SectorResolution(
            map_id=map_id,
            domain_id=str(spec["domainId"]),
            sector_x=int(sector_x),
            sector_z=int(sector_z),
            key=sector_key(sector_x, sector_z),
            level_id=str(level_id) if level_id else None,
            coverage="mapped" if level_id else "unmapped",
            unity_bounds_xz=world,
            blender_bounds_xy={
                "xmin": world["xmin"],
                "xmax": world["xmax"],
                "ymin": -world["zmax"],
                "ymax": -world["zmin"],
            },
        )

    def resolve_point(self, map_id: str, x: float, z: float) -> SectorResolution:
        sector_x, sector_z = sector_for_point(float(x), float(z))
        return self.resolve_sector(map_id, sector_x, sector_z)

    def levels_for_sector_keys(self, map_id: str, keys: Iterable[str]) -> list[str]:
        levels = set()
        for key in keys:
            prefix, sector_x, sector_z = key.split(":")
            if prefix != "sector":
                raise ValueError(f"Invalid sector key: {key}")
            resolved = self.resolve_sector(map_id, int(sector_x), int(sector_z))
            if resolved.level_id:
                levels.add(resolved.level_id)
        return sorted(levels)

    @staticmethod
    def unity_to_blender_position(position: Sequence[float]) -> dict[str, float]:
        if len(position) < 3:
            raise ValueError("Position requires three components")
        x, y, z = (float(position[0]), float(position[1]), float(position[2]))
        return {"x": x, "y": -z, "z": y}

    @staticmethod
    def unity_to_blender_direction(direction: Sequence[float]) -> dict[str, float]:
        if len(direction) < 3:
            raise ValueError("Direction requires three components")
        x, y, z = (float(direction[0]), float(direction[1]), float(direction[2]))
        if not all(math.isfinite(value) for value in (x, y, z)):
            raise ValueError("Direction contains a non-finite value")
        return {"x": x, "y": -z, "z": y}


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("format") != MANIFEST_FORMAT:
        raise ValueError("Invalid layer manifest format")
    if manifest.get("resolverVersion") != "endfield-sector128-v1":
        raise ValueError("Invalid layer manifest resolverVersion")
    maps = manifest.get("maps")
    if not isinstance(maps, Mapping) or not maps:
        raise ValueError("Layer manifest maps must be a non-empty object")
    if not isinstance(manifest.get("coordinateConvention"), Mapping):
        raise ValueError("Layer manifest lacks coordinateConvention")
    required_layers = {"instances", "water", "effects", "lights", "roads"}
    for map_id, spec in maps.items():
        if spec.get("mapId") != map_id:
            raise ValueError(f"Map key/id mismatch: {map_id}")
        layers = spec.get("layers")
        if not isinstance(layers, Mapping):
            raise ValueError(f"Map {map_id} lacks layers")
        missing = required_layers.difference(layers)
        if missing:
            raise ValueError(f"Map {map_id} lacks layers: {sorted(missing)}")
        for layer_name, layer in layers.items():
            if layer.get("mapId") != map_id or layer.get("layer") != layer_name:
                raise ValueError(f"Layer key/id mismatch: {map_id}/{layer_name}")
            if not layer.get("indexPath"):
                raise ValueError(f"Layer {map_id}/{layer_name} lacks indexPath")


def validate_browser_safe_path_ids(value: Any, *, context: str = "root") -> None:
    """Reject Unity PathIDs that would be rounded by JavaScript JSON parsing."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            child_context = f"{context}.{key}"
            if key in PATH_ID_FIELD_NAMES and child is not None:
                if not isinstance(child, str) or DECIMAL_INTEGER_PATTERN.fullmatch(child) is None:
                    raise ValueError(f"Unity PathID must be a decimal string at {child_context}: {child!r}")
            validate_browser_safe_path_ids(child, context=child_context)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            validate_browser_safe_path_ids(child, context=f"{context}[{index}]")


def validate_dataset_index(index: Mapping[str, Any]) -> None:
    """Validate the stable, browser-facing subset shared by all layer indexes."""

    if index.get("format") != DATASET_FORMAT:
        raise ValueError("Invalid dataset index format")
    layer = str(index.get("layer") or "")
    if layer not in {"instances", "water", "effects", "lights", "roads"}:
        raise ValueError(f"Unsupported dataset layer: {layer}")
    map_id = str(index.get("mapId") or "")
    if not map_id or not str(index.get("datasetId") or "").startswith(f"{map_id}.{layer}."):
        raise ValueError(f"Dataset id does not match map/layer: {index.get('datasetId')}")
    if index.get("generatorVersion") != GENERATOR_VERSION:
        raise ValueError(f"Unsupported generator version: {index.get('generatorVersion')}")
    if not isinstance(index.get("scan"), Mapping):
        raise ValueError(f"Dataset {map_id}/{layer} lacks scan metadata")
    for field in ("recordCount", "coverageCount", "duplicateCount", "pendingCount"):
        if not isinstance(index.get(field), int) or int(index[field]) < 0:
            raise ValueError(f"Dataset {map_id}/{layer} has invalid {field}")
    validate_browser_safe_path_ids(index, context=f"index:{map_id}/{layer}")

    if layer == "instances":
        required = {
            "assetTable",
            "categoryCounts",
            "assetResolutionCounts",
            "assetResolutionInstanceCounts",
            "assetPendingCount",
            "assetPendingInstanceCount",
            "shards",
        }
    elif layer == "effects":
        required = {"records", "anchors", "prototypes", "prototypeMembers", "anchorSelectionPolicy"}
    elif layer == "water":
        required = {"records", "configs", "geometryTypeCounts"}
    elif layer == "lights":
        required = {"records"}
    else:
        required = {"viewOf", "filter", "sectorKeys", "roadShards"}
        if index.get("recordEncoding") != "view-reference":
            raise ValueError("Road indexes must use view-reference encoding")
        road_shards = index.get("roadShards")
        if not isinstance(road_shards, list):
            raise ValueError("Road indexes must expose roadShards")
        road_sector_keys = [str(row.get("sectorKey")) for row in road_shards if isinstance(row, Mapping)]
        if road_sector_keys != list(index.get("sectorKeys") or []):
            raise ValueError("Road sectorKeys must exactly match roadShards")
        if any(int(row.get("roadRecordCount") or 0) <= 0 for row in road_shards if isinstance(row, Mapping)):
            raise ValueError("Every roadShards entry must contain at least one road record")
    missing = sorted(field for field in required if field not in index)
    if missing:
        raise ValueError(f"Dataset {map_id}/{layer} lacks fields: {missing}")


def validate_layer_record(record: Mapping[str, Any]) -> None:
    record_type = str(record.get("recordType") or "")
    required = _RECORD_REQUIRED_FIELDS.get(record_type)
    if required is None:
        raise ValueError(f"Unsupported layer recordType: {record_type}")
    if not record.get("mapId"):
        raise ValueError("Layer record lacks mapId")
    missing = [field for field in required if field not in record]
    if missing:
        raise ValueError(f"Layer record {record_type} lacks fields: {missing}")
    if "bindingStatus" in required and record.get("bindingStatus") not in {"mapped", "candidate", "unmapped"}:
        raise ValueError(f"Invalid bindingStatus: {record.get('bindingStatus')}")
    for key in record.get("sectorKeys") or []:
        if not re.fullmatch(r"sector:-?\d+:-?\d+", str(key)):
            raise ValueError(f"Invalid sector key: {key}")
    sector = record.get("anchorSector")
    if sector is not None:
        expected = sector_key(int(sector["sectorX"]), int(sector["sectorZ"]))
        if sector.get("key") != expected:
            raise ValueError(f"Anchor sector key mismatch: {sector.get('key')} != {expected}")
    validate_browser_safe_path_ids(record, context=f"record:{record_type}")
