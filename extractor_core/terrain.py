"""Read-only Map01/Map02 terrain extraction used by the first-run coordinator."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import struct
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .lz4 import decompress_lz4_inv
from .resources import BootstrapExtractionError, load_group, validate_vfs_root


Progress = Callable[[str, float, str, dict[str, Any] | None], None]
Cancelled = Callable[[], bool]
GROUP_HASH = "F84BF5E6"
PAGE_SIZE = 132
PAGE_BORDER = 2
CORE_SIZE = 128


def _emit(progress: Progress | None, phase: str, value: float, message: str, **details: Any) -> None:
    if progress:
        progress(phase, value, message, details or None)


def _check_cancelled(cancelled: Cancelled | None) -> None:
    if cancelled and cancelled():
        raise BootstrapExtractionError("first-run terrain extraction was cancelled")


def _rgb565(value: int) -> tuple[int, int, int]:
    red = (value >> 11) & 31
    green = (value >> 5) & 63
    blue = value & 31
    return (
        (red * 255 + 15) // 31,
        (green * 255 + 31) // 63,
        (blue * 255 + 15) // 31,
    )


def _alpha_palette(alpha0: int, alpha1: int) -> list[int]:
    if alpha0 > alpha1:
        return [alpha0, alpha1, *[((7 - index) * alpha0 + index * alpha1) // 7 for index in range(1, 7)]]
    return [
        alpha0,
        alpha1,
        *[((5 - index) * alpha0 + index * alpha1) // 5 for index in range(1, 5)],
        0,
        255,
    ]


def decode_dxt5(payload: bytes, width: int, height: int) -> bytes:
    blocks_x = (width + 3) // 4
    blocks_y = (height + 3) // 4
    expected = blocks_x * blocks_y * 16
    if len(payload) != expected:
        raise ValueError(f"DXT5 size mismatch: {len(payload)} != {expected}")
    output = bytearray(width * height * 4)
    offset = 0
    for block_y in range(blocks_y):
        for block_x in range(blocks_x):
            block = payload[offset : offset + 16]
            offset += 16
            alphas = _alpha_palette(block[0], block[1])
            alpha_bits = int.from_bytes(block[2:8], "little")
            color0, color1, color_bits = struct.unpack_from("<HHI", block, 8)
            rgb0 = _rgb565(color0)
            rgb1 = _rgb565(color1)
            colors = [
                rgb0,
                rgb1,
                tuple((2 * rgb0[index] + rgb1[index]) // 3 for index in range(3)),
                tuple((rgb0[index] + 2 * rgb1[index]) // 3 for index in range(3)),
            ]
            for pixel_y in range(4):
                y = block_y * 4 + pixel_y
                if y >= height:
                    continue
                for pixel_x in range(4):
                    x = block_x * 4 + pixel_x
                    if x >= width:
                        continue
                    pixel = pixel_y * 4 + pixel_x
                    color = colors[(color_bits >> (pixel * 2)) & 3]
                    alpha = alphas[(alpha_bits >> (pixel * 3)) & 7]
                    destination = (y * width + x) * 4
                    output[destination : destination + 4] = bytes((*color, alpha))
    return bytes(output)


def _read_clear_page(item: Any, group_dir: Path, handles: dict[Path, Any]) -> tuple[int, int, int, int, bytes]:
    chunk_path = group_dir / f"{item.file_chunk_md5_name}.chk"
    stream = handles.get(chunk_path)
    if stream is None:
        stream = chunk_path.open("rb")
        handles[chunk_path] = stream
    stream.seek(item.offset)
    packed = stream.read(item.length)
    if len(packed) != item.length or hashlib.md5(packed).hexdigest().upper() != item.file_data_md5.upper():
        raise BootstrapExtractionError(f"Terrain VFS validation failed: {item.file_name}")
    if len(packed) < 4:
        raise BootstrapExtractionError(f"Terrain payload is too short: {item.file_name}")
    clear_size = struct.unpack_from("<I", packed)[0]
    clear = decompress_lz4_inv(packed[4:], clear_size)
    if len(clear) < 20:
        raise BootstrapExtractionError(f"Terrain page is too short: {item.file_name}")
    magic, version, width, height, field_one, format_code, payload_size = struct.unpack_from(
        "<4sIHHHHI", clear
    )
    if magic != b"TRET" or version != 1 or payload_size + 20 != len(clear):
        raise BootstrapExtractionError(f"Unexpected TRET header: {item.file_name}")
    return width, height, field_one, format_code, clear[20:]


def _valid_height(value: int) -> bool:
    return 0 < value < 32768


def _bilinear_height(values: tuple[int, ...], u: float, v: float) -> int:
    u = max(0.0, min(64.0, u))
    v = max(0.0, min(64.0, v))
    x0 = min(63, math.floor(u))
    z0 = min(63, math.floor(v))
    fraction_x = u - x0
    fraction_z = v - z0
    corners = (
        values[z0 * 65 + x0],
        values[z0 * 65 + x0 + 1],
        values[(z0 + 1) * 65 + x0],
        values[(z0 + 1) * 65 + x0 + 1],
    )
    if not all(_valid_height(value) for value in corners):
        nearest = values[round(v) * 65 + round(u)]
        return nearest if _valid_height(nearest) else 32768
    value = (
        (corners[0] * (1.0 - fraction_x) + corners[1] * fraction_x) * (1.0 - fraction_z)
        + (corners[2] * (1.0 - fraction_x) + corners[3] * fraction_x) * fraction_z
    )
    return max(0, min(32767, round(value)))


def _map01_sample(
    heights: dict[tuple[int, int, int], tuple[int, ...]],
    fine_x: int,
    fine_z: int,
    local_x: int,
    local_z: int,
) -> tuple[int, tuple[int, int, int] | None]:
    for level in range(5, 0, -1):
        factor = 2 ** (5 - level)
        source = (level, fine_x // factor, fine_z // factor)
        values = heights.get(source)
        if values is None:
            continue
        u = ((fine_x % factor) * 64.0 + local_x) / factor
        v = ((fine_z % factor) * 64.0 + local_z) / factor
        value = _bilinear_height(values, u, v)
        if _valid_height(value):
            return value, source
    return 32768, None


def _save_manifest(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _extract_map01(
    vfs_root: Path,
    output_dir: Path,
    progress: Progress | None,
    cancelled: Cancelled | None,
) -> dict[str, Any]:
    pattern = re.compile(
        r"^Data/Terrain/PC/map01/Terrain_(?P<level>[1-5])_(?P<x>\d+)_(?P<z>\d+)_(?P<kind>[AHN])\.bytes$",
        re.I,
    )
    group = load_group(vfs_root, GROUP_HASH)
    group_dir = vfs_root / GROUP_HASH
    items: dict[tuple[int, int, int, str], Any] = {}
    for item in group.iter_files():
        match = pattern.match(item.file_name)
        if match:
            items[(int(match["level"]), int(match["x"]), int(match["z"]), match["kind"].upper())] = item
    if not items:
        raise BootstrapExtractionError("No Map01 terrain pages were found in the selected game build")

    handles: dict[Path, Any] = {}
    heights: dict[tuple[int, int, int], tuple[int, ...]] = {}
    albedo: dict[tuple[int, int, int], Image.Image] = {}
    normal: dict[tuple[int, int, int], Image.Image] = {}
    try:
        rows = sorted(items.items())
        for index, ((level, x, z, kind), item) in enumerate(rows, start=1):
            _check_cancelled(cancelled)
            width, height, _field, format_code, payload = _read_clear_page(item, group_dir, handles)
            if kind == "H":
                if (width, height, format_code, len(payload)) != (65, 65, 6, 65 * 65 * 2):
                    raise BootstrapExtractionError(f"Unexpected Map01 height page: {item.file_name}")
                heights[(level, x, z)] = struct.unpack("<4225H", payload)
            else:
                if (width, height, format_code) != (PAGE_SIZE, PAGE_SIZE, 101):
                    raise BootstrapExtractionError(f"Unexpected Map01 {kind} page: {item.file_name}")
                image = Image.frombytes("RGBA", (width, height), decode_dxt5(payload, width, height)).convert("RGB")
                core = image.crop((PAGE_BORDER, PAGE_BORDER, PAGE_BORDER + CORE_SIZE, PAGE_BORDER + CORE_SIZE))
                (albedo if kind == "A" else normal)[(level, x, z)] = core
            if index % 50 == 0 or index == len(rows):
                _emit(
                    progress,
                    "map01_terrain",
                    0.42 * index / len(rows),
                    f"Decoded Map01 terrain page {index}/{len(rows)}",
                )
    finally:
        for stream in handles.values():
            stream.close()

    selected: dict[tuple[int, int], tuple[int, int, int]] = {}
    fallback_counts: dict[str, int] = {}
    for fine_z in range(32):
        for fine_x in range(32):
            _check_cancelled(cancelled)
            source_counts: dict[tuple[int, int, int], int] = {}
            for local_z in range(65):
                for local_x in range(65):
                    _value, source = _map01_sample(heights, fine_x, fine_z, local_x, local_z)
                    if source is not None:
                        source_counts[source] = source_counts.get(source, 0) + 1
            if source_counts:
                source = max(source_counts, key=source_counts.get)
                selected[(fine_x, fine_z)] = source
                fallback_counts[str(source[0])] = fallback_counts.get(str(source[0]), 0) + 1
        _emit(progress, "map01_terrain", 0.42 + 0.18 * (fine_z + 1) / 32, "Resolved Map01 terrain fallback coverage")

    database = output_dir / "terrain.sqlite"
    connection = sqlite3.connect(database)
    source_rows: list[tuple[int, int, tuple[int, int, int]]] = []
    try:
        connection.execute(
            "CREATE TABLE tiles (x INTEGER,z INTEGER,width INTEGER,height INTEGER,heights BLOB,"
            "source_level INTEGER,source_x INTEGER,source_z INTEGER,PRIMARY KEY(x,z))"
        )
        for row_index, ((fine_x, fine_z), source) in enumerate(
            sorted(selected.items(), key=lambda row: (row[0][1], row[0][0])), start=1
        ):
            _check_cancelled(cancelled)
            resampled = [
                _map01_sample(heights, fine_x, fine_z, local_x, local_z)[0]
                for local_z in range(65)
                for local_x in range(65)
            ]
            connection.execute(
                "INSERT INTO tiles VALUES (?,?,?,?,?,?,?,?)",
                (fine_x, fine_z, 65, 65, struct.pack("<4225H", *resampled), *source),
            )
            source_rows.append((fine_x, fine_z, source))
            if row_index % 64 == 0:
                connection.commit()
        connection.commit()
    finally:
        connection.close()

    albedo_atlas = Image.new("RGB", (4096, 4096), (32, 32, 32))
    normal_atlas = Image.new("RGB", (4096, 4096), (128, 128, 255))
    for row_index, (fine_x, fine_z, source) in enumerate(source_rows, start=1):
        level, _source_x, _source_z = source
        factor = 2 ** (5 - level)
        sub_x = fine_x % factor
        sub_z = fine_z % factor
        crop = (
            round(sub_x * CORE_SIZE / factor),
            round(sub_z * CORE_SIZE / factor),
            round((sub_x + 1) * CORE_SIZE / factor),
            round((sub_z + 1) * CORE_SIZE / factor),
        )
        for atlas, pages in ((albedo_atlas, albedo), (normal_atlas, normal)):
            page = pages.get(source)
            if page is not None:
                atlas.paste(
                    page.crop(crop).resize((CORE_SIZE, CORE_SIZE), Image.Resampling.BILINEAR),
                    (fine_x * CORE_SIZE, fine_z * CORE_SIZE),
                )
        if row_index % 64 == 0 or row_index == len(source_rows):
            _emit(
                progress,
                "map01_terrain",
                0.6 + 0.34 * row_index / max(1, len(source_rows)),
                f"Built Map01 terrain atlas tile {row_index}/{len(source_rows)}",
            )

    albedo_name = "map01_terrain_pyramid_baked_albedo.png"
    normal_name = "map01_terrain_pyramid_baked_normal.png"
    albedo_atlas.save(output_dir / albedo_name, optimize=True)
    normal_atlas.save(output_dir / normal_name, optimize=True)
    manifest = {
        "format": "EndfieldTerrainSurfaceAtlas/2",
        "read_only_game_input": True,
        "map": "map01",
        "bounds": None,
        "terrain_position": [-1024.0, 0.0, -1024.0],
        "height_scale": 512.0,
        "height_denominator": 32768.0,
        "terrain_size": 2048.0,
        "height_formula": "terrain_position_y + uint16_height * height_scale / height_denominator; values >= 32768 are no-data",
        "tile_world_origin": [-1024.0, -1024.0],
        "tile_world_size": 64.0,
        "world_tile_count": 32,
        "source_page_size": PAGE_SIZE,
        "source_page_border": PAGE_BORDER,
        "atlas_core_pixels_per_tile": CORE_SIZE,
        "atlas_size": [4096, 4096],
        "atlas_tile_origin": [0, 0],
        "atlas_tile_count": [32, 32],
        "atlas_world_bounds": [-1024.0, 1024.0, -1024.0, 1024.0],
        "atlas_row_direction": "Unity +Z maps to PNG top-to-bottom rows",
        "albedo": albedo_name,
        "normal": normal_name,
        "height_database": database.name,
        "active_tiles": len(selected),
        "source_tile_counts_by_level": {
            str(level): sum(1 for key in heights if key[0] == level) for level in range(1, 6)
        },
        "selected_fine_tile_counts_by_source_level": fallback_counts,
        "missing_fine_tiles": [
            [x, z] for z in range(32) for x in range(32) if (x, z) not in selected
        ],
        "missing_channels": [],
        "source_formats": {
            "A": "GraphicsFormat.RGBA_DXT5_UNorm (101)",
            "N": "GraphicsFormat.RGBA_DXT5_UNorm (101)",
        },
    }
    _save_manifest(output_dir / "terrain_surface.json", manifest)
    _emit(progress, "map01_terrain", 1.0, "Map01 terrain extraction complete")
    return manifest


def _overlaps_map02(tile_x: int, tile_z: int, bounds: tuple[float, float, float, float] | None) -> bool:
    if bounds is None:
        return True
    origin_x = -2048.0 + tile_x * 64.0
    origin_z = -2048.0 + tile_z * 64.0
    xmin, xmax, zmin, zmax = bounds
    return origin_x < xmax and origin_x + 64.0 > xmin and origin_z < zmax and origin_z + 64.0 > zmin


def _extract_map02(
    vfs_root: Path,
    output_dir: Path,
    bounds: tuple[float, float, float, float] | None,
    progress: Progress | None,
    cancelled: Cancelled | None,
) -> dict[str, Any]:
    pattern = re.compile(r"^Data/Terrain/PC/map02/Terrain_6_(\d+)_(\d+)_([AHN])\.bytes$", re.I)
    group = load_group(vfs_root, GROUP_HASH)
    group_dir = vfs_root / GROUP_HASH
    items: dict[tuple[int, int, str], Any] = {}
    active: set[tuple[int, int]] = set()
    for item in group.iter_files():
        match = pattern.match(item.file_name)
        if not match:
            continue
        x, z, kind = int(match.group(1)), int(match.group(2)), match.group(3).upper()
        if not (0 <= x < 64 and 0 <= z < 64) or not _overlaps_map02(x, z, bounds):
            continue
        items[(x, z, kind)] = item
        if kind == "H":
            active.add((x, z))
    if not active:
        raise BootstrapExtractionError("No Map02 L6 terrain pages matched the requested bounds")

    min_x = min(x for x, _z in active)
    max_x = max(x for x, _z in active)
    min_z = min(z for _x, z in active)
    max_z = max(z for _x, z in active)
    width = (max_x - min_x + 1) * CORE_SIZE
    height = (max_z - min_z + 1) * CORE_SIZE
    albedo_atlas = Image.new("RGB", (width, height), (32, 32, 32))
    normal_atlas = Image.new("RGB", (width, height), (128, 128, 255))
    database = output_dir / "terrain.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE tiles (x INTEGER,z INTEGER,width INTEGER,height INTEGER,heights BLOB,PRIMARY KEY(x,z))"
    )
    handles: dict[Path, Any] = {}
    missing_channels: list[list[Any]] = []
    try:
        ordered = sorted(active, key=lambda pair: (pair[1], pair[0]))
        for index, (x, z) in enumerate(ordered, start=1):
            _check_cancelled(cancelled)
            page_width, page_height, _field, format_code, heights = _read_clear_page(
                items[(x, z, "H")], group_dir, handles
            )
            if format_code != 6 or len(heights) != page_width * page_height * 2:
                raise BootstrapExtractionError(f"Unexpected Map02 height page at {x},{z}")
            connection.execute("INSERT INTO tiles VALUES (?,?,?,?,?)", (x, z, page_width, page_height, heights))
            for kind, atlas in (("A", albedo_atlas), ("N", normal_atlas)):
                item = items.get((x, z, kind))
                if item is None:
                    missing_channels.append([x, z, kind])
                    continue
                image_width, image_height, _one, image_format, payload = _read_clear_page(
                    item, group_dir, handles
                )
                if (image_width, image_height, image_format) != (PAGE_SIZE, PAGE_SIZE, 101):
                    raise BootstrapExtractionError(f"Unexpected Map02 {kind} page at {x},{z}")
                page = Image.frombytes(
                    "RGBA", (image_width, image_height), decode_dxt5(payload, image_width, image_height)
                ).convert("RGB")
                atlas.paste(
                    page.crop((PAGE_BORDER, PAGE_BORDER, PAGE_BORDER + CORE_SIZE, PAGE_BORDER + CORE_SIZE)),
                    ((x - min_x) * CORE_SIZE, (z - min_z) * CORE_SIZE),
                )
            if index % 50 == 0:
                connection.commit()
            _emit(
                progress,
                "map02_terrain",
                0.88 * index / len(ordered),
                f"Extracted Map02 terrain tile {index}/{len(ordered)}",
            )
        connection.commit()
    finally:
        connection.close()
        for stream in handles.values():
            stream.close()

    albedo_name = "map02_terrain_baked_albedo.png"
    normal_name = "map02_terrain_baked_normal.png"
    albedo_atlas.save(output_dir / albedo_name, optimize=True)
    normal_atlas.save(output_dir / normal_name, optimize=True)
    manifest = {
        "format": "EndfieldTerrainSurfaceAtlas/1",
        "read_only_game_input": True,
        "map": "map02",
        "bounds": list(bounds) if bounds is not None else None,
        "terrain_position": [-2048.0, 0.0, -2048.0],
        "height_scale": 730.0,
        "terrain_size": 4096.0,
        "height_formula": "terrain_position_y + uint16_height * height_scale / 32768; values >= 32768 are no-data",
        "tile_world_origin": [-2048.0, -2048.0],
        "tile_world_size": 64.0,
        "world_tile_count": 64,
        "source_page_size": PAGE_SIZE,
        "source_page_border": PAGE_BORDER,
        "atlas_core_pixels_per_tile": CORE_SIZE,
        "atlas_size": [width, height],
        "atlas_tile_origin": [min_x, min_z],
        "atlas_tile_count": [max_x - min_x + 1, max_z - min_z + 1],
        "atlas_world_bounds": [
            -2048.0 + min_x * 64.0,
            -2048.0 + (max_x + 1) * 64.0,
            -2048.0 + min_z * 64.0,
            -2048.0 + (max_z + 1) * 64.0,
        ],
        "atlas_row_direction": "Unity +Z maps to PNG top-to-bottom rows",
        "albedo": albedo_name,
        "normal": normal_name,
        "height_database": database.name,
        "active_tiles": len(active),
        "missing_channels": missing_channels,
        "source_formats": {
            "A": "GraphicsFormat.RGBA_DXT5_UNorm (101)",
            "N": "GraphicsFormat.RGBA_DXT5_UNorm (101)",
        },
    }
    _save_manifest(output_dir / "terrain_surface.json", manifest)
    _emit(progress, "map02_terrain", 1.0, "Map02 terrain extraction complete")
    return manifest


def extract_terrain_surface(
    map_id: str,
    vfs_root: Path,
    output_dir: Path,
    *,
    bounds: tuple[float, float, float, float] | None = None,
    progress: Progress | None = None,
    cancelled: Cancelled | None = None,
) -> dict[str, Any]:
    """Extract terrain payload directly from the read-only VFS into a new directory."""

    game_vfs = validate_vfs_root(vfs_root)
    root = output_dir.expanduser().resolve()
    if root.exists():
        raise FileExistsError(f"Refusing to reuse terrain output directory: {root}")
    if root == game_vfs or game_vfs in root.parents:
        raise BootstrapExtractionError("Terrain output must be outside the game installation")
    if bounds is not None and (bounds[1] <= bounds[0] or bounds[3] <= bounds[2]):
        raise ValueError("Terrain bounds must be a non-empty half-open rectangle")
    root.mkdir(parents=True)
    if map_id == "map01":
        if bounds is not None:
            raise ValueError("Map01 first-run terrain extraction currently produces the full atlas")
        return _extract_map01(game_vfs, root, progress, cancelled)
    if map_id == "map02":
        return _extract_map02(game_vfs, root, bounds, progress, cancelled)
    raise ValueError(f"Unsupported map id: {map_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Endfield terrain from a read-only VFS")
    parser.add_argument("map_id", choices=("map01", "map02"))
    parser.add_argument("vfs_root", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--xmin", type=float)
    parser.add_argument("--xmax", type=float)
    parser.add_argument("--zmin", type=float)
    parser.add_argument("--zmax", type=float)
    args = parser.parse_args()
    values = (args.xmin, args.xmax, args.zmin, args.zmax)
    if any(value is not None for value in values) and not all(value is not None for value in values):
        parser.error("Specify all four bounds or none")
    bounds = tuple(float(value) for value in values) if all(value is not None for value in values) else None
    result = extract_terrain_surface(args.map_id, args.vfs_root, args.output_dir, bounds=bounds)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
