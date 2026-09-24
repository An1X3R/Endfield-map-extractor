"""Validate source terrain layers and the portable detail-material manifest."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import TypedDict


class TerrainLayer(TypedDict):
    ordinal: int
    texture_index: int
    splat_st: list[float]
    packed_info: list[float]
    diffuse_scale: list[float]
    mask_offset: list[float]
    mask_scale: list[float]
    pom: list[float]


class TerrainSurface(TypedDict):
    position: list[float]
    size: int
    layers: list[TerrainLayer]


class DetailLayer(TerrainLayer):
    diffuse: str
    normal: str


class TerrainDetail(TypedDict):
    format: str
    map: str
    position: list[float]
    size: int
    bounds: list[float]
    tiles: list[list[int]]
    index: str
    control: str
    tint: str
    layers: list[DetailLayer]
    source_metadata_sha256: str
    source_index_sha256: str
    projection: str


def source_vector(section: str, name: str, dimensions: int) -> list[float]:
    axes = "xyzw"[:dimensions]
    expression = rf"Vector{dimensions}f {re.escape(name)}\s+" + r"\s+".join(
        rf"float {axis} = ([^\s]+)" for axis in axes)
    matches = re.findall(expression, section)
    if len(matches) != 1:
        raise ValueError(f"Expected one terrain source vector: field={name}, matches={len(matches)}")
    values = [float(item) for item in matches[0]]
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"Nonfinite terrain source vector: field={name}")
    return values


def read_surface_metadata(path: Path) -> TerrainSurface:
    text = path.read_text(encoding="utf-8")
    if 'string m_Name = "HGTerrainSurfacesData"' not in text:
        raise ValueError(f"Expected HGTerrainSurfacesData metadata: {path}")
    sizes = re.findall(r"\bint terrainSize = (\d+)", text)
    if len(sizes) != 1:
        raise ValueError(f"Missing unique terrain size: {path}")
    size = int(sizes[0])
    if size < 64 or size % 64 or not math.log2(size / 64).is_integer():
        raise ValueError(f"Unsupported terrain extent: size={size}, path={path}")
    if "SplatLayerData layerData" not in text or "int splatTexCount" not in text:
        raise ValueError(f"Missing terrain layer section: {path}")
    section = text.split("SplatLayerData layerData", 1)[1].split("int splatTexCount", 1)[0]
    count_match = re.search(r"int size = (\d+)", section)
    if count_match is None:
        raise ValueError(f"Missing terrain layer count: {path}")
    count = int(count_match.group(1))
    rows = section.split("SplatLayerData data")[1:]
    if len(rows) != count:
        raise ValueError(f"Terrain layer count mismatch: expected={count}, actual={len(rows)}")
    layers: list[TerrainLayer] = []
    for ordinal, row in enumerate(rows):
        st = source_vector(row, "splatST", 4)
        if st[0] <= 0 or st[1] < 0 or not st[1].is_integer():
            raise ValueError(f"Invalid source layer tiling or texture index: ordinal={ordinal}, st={st}")
        layers.append({"ordinal": ordinal, "texture_index": int(st[1]), "splat_st": st,
                       "packed_info": source_vector(row, "packedSplatInfo", 4),
                       "diffuse_scale": source_vector(row, "diffuseRemapScale", 4),
                       "mask_offset": source_vector(row, "maskMapRemapOffset", 4),
                       "mask_scale": source_vector(row, "maskMapRemapScale", 4),
                       "pom": source_vector(row, "pomParams", 4)})
    return {"position": source_vector(text, "terrainPosition", 3), "size": size, "layers": layers}


def finite_vector(value: object, size: int, label: str) -> list[float]:
    if (not isinstance(value, list) or len(value) != size
            or any(type(item) not in (int, float) or not math.isfinite(item) for item in value)):
        raise ValueError(f"Terrain detail requires a finite {size}-vector: {label}")
    return [float(item) for item in value]


def source_file(value: object, root: Path, label: str) -> str:
    if not isinstance(value, str) or not value or not (root / value).is_file():
        raise ValueError(f"Terrain detail texture is missing: field={label}, path={value}, root={root}")
    return value


def load_terrain_detail(path: Path) -> TerrainDetail:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("format") != "EndfieldTerrainDetail/1":
        raise ValueError(f"Unsupported terrain detail manifest: {path}")
    if raw.get("projection") != "source_domain_smooth_triplanar_v1":
        raise ValueError(f"Unsupported terrain projection: {raw.get('projection')}")
    if not isinstance(raw.get("map"), str) or not re.fullmatch(r"[a-z0-9_]+", raw["map"]):
        raise ValueError(f"Invalid terrain map identity: {path}")
    if (type(raw.get("size")) is not int or raw["size"] < 64 or raw["size"] % 64
            or not math.log2(raw["size"] / 64).is_integer()):
        raise ValueError(f"Invalid terrain size: {path}")
    position = finite_vector(raw.get("position"), 3, "position")
    bounds = finite_vector(raw.get("bounds"), 4, "bounds")
    if bounds[0] >= bounds[1] or bounds[2] >= bounds[3]:
        raise ValueError(f"Empty terrain detail bounds: {path}")
    tiles = raw.get("tiles")
    if not isinstance(tiles, list) or not tiles or any(not isinstance(tile, list) or len(tile) != 2
            or any(type(index) is not int or index < 0 for index in tile) for tile in tiles):
        raise ValueError(f"Invalid terrain detail tile addresses: {path}")
    side = raw["size"] // 64
    x0, x1 = min(tile[0] for tile in tiles), max(tile[0] for tile in tiles) + 1
    z0, z1 = min(tile[1] for tile in tiles), max(tile[1] for tile in tiles) + 1
    if (len({tuple(tile) for tile in tiles}) != len(tiles) or max(x1, z1) > side
            or len(tiles) != (x1-x0)*(z1-z0)):
        raise ValueError(f"Terrain detail tiles must form a distinct in-bounds rectangle: {path}")
    expected_bounds = [position[0]+x0*64, position[0]+x1*64, position[2]+z0*64, position[2]+z1*64]
    if bounds != expected_bounds:
        raise ValueError(f"Terrain detail bounds disagree with source tiles: actual={bounds}, expected={expected_bounds}")
    raw_layers = raw.get("layers")
    if not isinstance(raw_layers, list) or not raw_layers:
        raise ValueError(f"Missing terrain layers: {path}")
    layers: list[DetailLayer] = []
    for row in raw_layers:
        if not isinstance(row, dict) or any(type(row.get(key)) is not int or row[key] < 0
                                           for key in ("ordinal", "texture_index")):
            raise ValueError(f"Invalid terrain layer identity: {row}")
        if row["ordinal"] > 63:
            raise ValueError(f"Terrain layer ordinal exceeds the source index encoding: {row['ordinal']}")
        splat_st = finite_vector(row.get("splat_st"), 4, "splat_st")
        if splat_st[0] <= 0 or splat_st[1] != row["texture_index"]:
            raise ValueError(f"Terrain layer tiling/index mismatch: ordinal={row['ordinal']}, st={splat_st}, texture_index={row['texture_index']}")
        layers.append({"ordinal": row["ordinal"], "texture_index": row["texture_index"],
                       "splat_st": splat_st,
                       "packed_info": finite_vector(row.get("packed_info"), 4, "packed_info"),
                       "diffuse_scale": finite_vector(row.get("diffuse_scale"), 4, "diffuse_scale"),
                       "mask_offset": finite_vector(row.get("mask_offset"), 4, "mask_offset"),
                       "mask_scale": finite_vector(row.get("mask_scale"), 4, "mask_scale"),
                       "pom": finite_vector(row.get("pom"), 4, "pom"),
                       "diffuse": source_file(row.get("diffuse"), path.parent, "diffuse"),
                       "normal": source_file(row.get("normal"), path.parent, "normal")})
    if len({row["ordinal"] for row in layers}) != len(layers):
        raise ValueError(f"Duplicate terrain layer ordinals: {path}")
    for key in ("source_metadata_sha256", "source_index_sha256"):
        if not isinstance(raw.get(key), str) or not re.fullmatch("[a-f0-9]{64}", raw[key]):
            raise ValueError(f"Missing terrain source fingerprint: {key}")
    return {"format": raw["format"], "map": raw["map"], "position": position,
            "size": raw["size"], "bounds": bounds, "tiles": tiles,
            "index": source_file(raw.get("index"), path.parent, "index"),
            "control": source_file(raw.get("control"), path.parent, "control"),
            "tint": source_file(raw.get("tint"), path.parent, "tint"), "layers": layers,
            "source_metadata_sha256": raw["source_metadata_sha256"],
            "source_index_sha256": raw["source_index_sha256"], "projection": raw["projection"]}
