"""Discover source-defined level maps and compose complete, uncropped H tiles."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import file_digest, sha256
import json
import math
from pathlib import Path, PurePosixPath
import re
import sqlite3

from PIL import Image

from endfield_character_bundle_inputs import prepare_providers_bundles
from endfield_character_cache import CharacterSourceChangedError, CharacterSourceStamp, source_stamp
from endfield_character_catalog import _provider_details, _read_virtual_payload, _virtual_file_index
from endfield_character_sources import CharacterSourceContext, game_source_stamps, validate_character_storage, verify_game_source
from endfield_map_overviews import load_tile
from endfield_scene_discovery import observe_helper, tool_identity
from endfield_scene_resources import resolve_path
from extractor_core.resources import BOOTSTRAP_VIRTUAL_FILES, load_group, read_virtual_file
from generate_runtime_overview import TILE_RE, choose_tile_candidates


CheckCancelled = Callable[[], None]
Progress = Callable[[str, float, str], None]
_PREFIX = "assets/beyond/dynamicassets/gameplay/ui/sprites/levelmap/levelmapchunks/"
_CONFIG_PREFIX = "data/json/uilevelmaploadconfig/"
_LEVEL = re.compile(r"^(map\d{2})_lv\d{3}$")
_GROUP = "775A31D1"
_SECTOR = 128


class LevelMapError(ValueError):
    """Source map data is incomplete, ambiguous, stale or unsafe to publish."""


@dataclass(frozen=True)
class LevelMapEntry:
    level_id: str
    map_id: str
    config_path: str
    config_sha256: str
    config_bytes: bytes
    xmin: float
    xmax: float
    zmin: float
    zmax: float
    inverse_xz: bool
    resources: tuple[str, ...]


@dataclass(frozen=True)
class LevelMapCatalog:
    catalog_id: str
    entries: tuple[LevelMapEntry, ...]
    index_stamps: tuple[CharacterSourceStamp, ...]
    game_stamps: tuple[CharacterSourceStamp, ...]


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise LevelMapError(f"Expected object: {label}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LevelMapError(f"Expected nonempty string: {label}")
    return value


def _number(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise LevelMapError(f"Expected finite number: {label}={value!r}")
    return float(value)


def _indexes(context: CharacterSourceContext) -> tuple[Path, ...]:
    return context.resource_database, context.provider_index, context.cab_inventory


def _game_stamps(context: CharacterSourceContext) -> tuple[CharacterSourceStamp, ...]:
    return tuple(CharacterSourceStamp(**row) for row in game_source_stamps(context.game_root))


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _transforms(xmin: float, xmax: float, zmin: float, zmax: float,
                width: int, height: int) -> tuple[list[list[float]], list[list[float]]]:
    sx, sz = width / (xmax - xmin), height / (zmax - zmin)
    return ([[sx, 0., -xmin * sx], [0., -sz, zmax * sz], [0., 0., 1.]],
            [[1. / sx, 0., xmin], [0., -1. / sz, zmax], [0., 0., 1.]])


def level_map_selection_grid(level_id: str, map_id: str, art_owner: str,
                             world_bounds: dict[str, object]) -> dict[str, object]:
    """Describe selection cells without altering the physical map's existing grid."""
    if not _LEVEL.fullmatch(level_id) or map_id != level_id.split("_", 1)[0]:
        raise LevelMapError(f"Selection grid map differs from source level: {level_id}, map={map_id}")
    _text(art_owner, "selectionGrid.artOwner")
    xmin, xmax, zmin, zmax = (_number(world_bounds.get(key), key) for key in ("xmin", "xmax", "zmin", "zmax"))
    columns, rows = (xmax - xmin) / _SECTOR, (zmax - zmin) / _SECTOR
    if columns <= 0 or rows <= 0 or not columns.is_integer() or not rows.is_integer():
        raise LevelMapError(f"Selection grid requires positive whole 128m cells: {level_id}")
    return {"format": "EndfieldLevelMapSelectionGrid/1", "levelId": level_id,
        "mapId": map_id, "artOwner": art_owner, "cellSizeMetres": _SECTOR,
        "origin": {"x": xmin, "z": zmin}, "columns": int(columns), "rows": int(rows),
        "indexBase": 0, "cellIdBase": 1,
        "cellIdFormula": "1 + row * columns + column",
        "cellIdOrder": "rows south-to-north; columns west-to-east",
        "rowDirection": "south-to-north", "columnDirection": "west-to-east",
        "worldBounds": {"xmin": xmin, "xmax": xmax, "zmin": zmin, "zmax": zmax}}


def _validate_selection_grid(grid: dict[str, object]) -> dict[str, object]:
    expected = level_map_selection_grid(_text(grid.get("levelId"), "selectionGrid.levelId"),
        _text(grid.get("mapId"), "selectionGrid.mapId"), _text(grid.get("artOwner"), "selectionGrid.artOwner"),
        _object(grid.get("worldBounds"), "selectionGrid.worldBounds"))
    for key, value in expected.items():
        if isinstance(value, dict):
            actual = _object(grid.get(key), f"selectionGrid.{key}")
            if any(_number(actual.get(name), f"selectionGrid.{key}.{name}") != field for name, field in value.items()):
                raise LevelMapError(f"Selection grid contract differs from source bounds: field={key}")
        elif type(grid.get(key)) is not type(value) or grid[key] != value:
            raise LevelMapError(f"Selection grid contract differs from source bounds: field={key}")
    return expected


def selection_grid_world_bounds(grid: dict[str, object], first_column: int, last_column: int,
                                first_row: int, last_row: int) -> dict[str, float]:
    """Convert an inclusive rectangle of zero-based cells into world XZ bounds."""
    valid = _validate_selection_grid(grid)
    for label, value in (("first_column", first_column), ("last_column", last_column),
                         ("first_row", first_row), ("last_row", last_row)):
        if type(value) is not int:
            raise LevelMapError(f"Selection cell index must be an integer: {label}={value!r}")
    if not (0 <= first_column <= last_column < valid["columns"] and 0 <= first_row <= last_row < valid["rows"]):
        raise LevelMapError(f"Selection rectangle is outside grid or reversed: columns={first_column}..{last_column}, rows={first_row}..{last_row}, grid={valid['columns']}x{valid['rows']}")
    origin = _object(valid["origin"], "selectionGrid.origin")
    x, z = _number(origin["x"], "origin.x"), _number(origin["z"], "origin.z")
    return {"xmin": x + first_column * _SECTOR, "xmax": x + (last_column + 1) * _SECTOR,
            "zmin": z + first_row * _SECTOR, "zmax": z + (last_row + 1) * _SECTOR}


def with_level_map_selection_grid(entry: dict[str, object]) -> dict[str, object]:
    """Upgrade metadata explicitly while retaining the source image and transforms."""
    bounds = _object(entry.get("worldBounds"), "worldBounds")
    grid = level_map_selection_grid(_text(entry.get("levelId"), "levelId"),
        _text(entry.get("mapId"), "mapId"), _text(entry.get("artOwner"), "artOwner"), bounds)
    return {**entry, "sourceMapBounds": dict(grid["worldBounds"]), "selectionGrid": grid}


def level_map_source_context(runtime_path: Path) -> CharacterSourceContext:
    """Reuse prepared scene indexes without requiring character initialization."""
    runtime = _object(json.loads(runtime_path.read_text(encoding="utf-8-sig")), "scene runtime")
    if runtime.get("format") != "EndfieldRuntimeConfig/1":
        raise LevelMapError(f"Unsupported scene runtime: {runtime_path}")
    paths = _object(runtime.get("paths"), "runtime.paths")
    extraction = _object(runtime.get("extraction"), "runtime.extraction")
    discovery = _object(extraction.get("sceneDiscovery"), "runtime.extraction.sceneDiscovery")
    if discovery.get("status") != "completed":
        raise LevelMapError("Level maps require completed scene source discovery")

    def descriptor(name: str) -> Path:
        row = _object(discovery.get(name), f"sceneDiscovery.{name}")
        path = resolve_path(row.get("path"), runtime_path.parent)
        if path.stat().st_size != row.get("bytes") or _digest(path).casefold() != _text(row.get("sha256"), name).casefold():
            raise LevelMapError(f"Prepared scene source descriptor changed: {name}={path}")
        return path

    context = CharacterSourceContext(resolve_path(paths.get("game_root"), runtime_path.parent),
        resolve_path(paths.get("blender_exe"), runtime_path.parent),
        resolve_path(paths.get("cache_root"), runtime_path.parent),
        resolve_path(paths.get("webui_output_root"), runtime_path.parent),
        descriptor("scannerResults"), resolve_path(extraction.get("resourceDatabase"), runtime_path.parent),
        descriptor("cabInventory"))
    validate_character_storage(context.game_root, context.cache_root)
    with closing(sqlite3.connect(context.resource_database.as_uri() + "?mode=ro", uri=True)) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM meta"))
    vfs = context.game_root / "Endfield_Data/StreamingAssets/VFS"
    for role, field in (("manifest", "manifest_sha256"), ("main_paths", "string_path_hash_sha256")):
        group, virtual_path, _ = BOOTSTRAP_VIRTUAL_FILES[role]
        actual = sha256(read_virtual_file(vfs, group, virtual_path)).hexdigest()
        if _text(metadata.get(field), field).casefold() != actual:
            raise LevelMapError(f"Scene resource database belongs to different game data: field={field}, expected={metadata.get(field)}, actual={actual}")
    return context


def parse_level_map_config(level_id: str, config_path: str, payload: bytes,
                           resources: tuple[str, ...]) -> LevelMapEntry:
    """Use the source world rectangle; region polygons never define tile origins."""
    match = _LEVEL.fullmatch(level_id)
    if match is None:
        raise LevelMapError(f"Invalid source level identifier: {level_id}")
    config = _object(json.loads(payload), config_path)
    basic = _object(config.get("basic"), f"{config_path}.basic")
    lower = _object(basic.get("worldRectLeftBottom"), "worldRectLeftBottom")
    upper = _object(basic.get("worldRectRightTop"), "worldRectRightTop")
    xmin, zmin = (_number(lower.get(axis), f"worldRectLeftBottom.{axis}") for axis in ("x", "y"))
    xmax, zmax = (_number(upper.get(axis), f"worldRectRightTop.{axis}") for axis in ("x", "y"))
    inverse = basic.get("needInverseXZ")
    if type(inverse) is not bool or xmax <= xmin or zmax <= zmin:
        raise LevelMapError(f"Invalid world rectangle or needInverseXZ: level={level_id}")
    if any(not ((high - low) / _SECTOR).is_integer() for low, high in ((xmin, xmax), (zmin, zmax))):
        raise LevelMapError(f"World rectangle does not contain whole 128m H tiles: level={level_id}")
    return LevelMapEntry(level_id, match[1], config_path, sha256(payload).hexdigest(), payload,
                         xmin, xmax, zmin, zmax, inverse, resources)


def discover_level_maps(context: CharacterSourceContext, check_cancelled: CheckCancelled) -> LevelMapCatalog:
    """Join current normal H resource paths to exact VFS load configurations."""
    check_cancelled()
    before = tuple(source_stamp(path) for path in _indexes(context))
    game_before = _game_stamps(context)
    by_level: dict[str, list[str]] = {}
    with closing(sqlite3.connect(context.resource_database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM meta"))
        if metadata.get("format") != "EndfieldResourceDB/1":
            raise LevelMapError(f"Unsupported resource database: {context.resource_database}")
        for (raw_path,) in connection.execute("SELECT path FROM paths WHERE path_folded LIKE ?", (_PREFIX + "%",)):
            path = raw_path.replace("\\", "/").casefold()
            match = TILE_RE.fullmatch(PurePosixPath(path).stem)
            if match and PurePosixPath(path).suffix == ".png":
                by_level.setdefault(f"{match['map']}_{match['level']}", []).append(path)
    if not by_level:
        raise LevelMapError("Current resource database contains no normal sprite H tiles")
    vfs = context.game_root / "Endfield_Data/StreamingAssets/VFS"
    group = load_group(vfs, _GROUP)
    files = _virtual_file_index(group, _GROUP)
    entries: list[LevelMapEntry] = []
    for level, resources in sorted(by_level.items()):
        check_cancelled()
        config_path = f"{_CONFIG_PREFIX}{level}.json"
        payload = _read_virtual_payload(vfs, _GROUP, group, files, config_path)
        entries.append(parse_level_map_config(level, files[config_path].file_name, payload, tuple(sorted(set(resources)))))
    verify_game_source(context.game_root, [asdict(stamp) for stamp in game_before], check_cancelled)
    if tuple(source_stamp(path) for path in _indexes(context)) != before:
        raise CharacterSourceChangedError("Level map indexes changed during discovery")
    identity = sha256(json.dumps({"indexes": [asdict(stamp) for stamp in before],
        "game": [asdict(stamp) for stamp in game_before],
        "levels": [(entry.level_id, entry.config_sha256, entry.resources) for entry in entries]}, sort_keys=True).encode()).hexdigest()
    return LevelMapCatalog(identity, tuple(entries), before, game_before)


def public_level_map_catalog(catalog: LevelMapCatalog) -> dict[str, object]:
    """Expose discovered source maps separately from completed image artifacts."""
    return {"format": "EndfieldLevelMapCatalog/1", "catalogId": catalog.catalog_id, "entries": [
        with_level_map_selection_grid({"levelId": entry.level_id, "mapId": entry.map_id, "artOwner": entry.map_id,
         "worldBounds": {"xmin": entry.xmin, "xmax": entry.xmax, "zmin": entry.zmin, "zmax": entry.zmax},
         "sourceConfig": {"virtualPath": entry.config_path, "sha256": entry.config_sha256},
         "tileCount": len(entry.resources), "needInverseXZ": entry.inverse_xz,
         "status": "unsupported_inverse_xz" if entry.inverse_xz else "discovered"})
        for entry in catalog.entries]}


def compose_level_map(entry: LevelMapEntry, scene_path: Path, output: Path) -> dict[str, object]:
    """Preserve native RGBA pixels across the full source rectangle, without masks."""
    if entry.inverse_xz:
        raise LevelMapError(f"Source requires unimplemented inverse-XZ map orientation: {entry.level_id}")
    scene = _object(json.loads(scene_path.read_text(encoding="utf-8-sig")), "scene manifest")
    tiles, audit = choose_tile_candidates(scene, scene_path.parent, entry.level_id)
    columns, rows = int((entry.xmax - entry.xmin) / _SECTOR), int((entry.zmax - entry.zmin) / _SECTOR)
    expected = {(x, z) for x in range(1, columns + 1) for z in range(1, rows + 1)}
    if set(tiles) != expected:
        raise LevelMapError(f"Incomplete H tile grid: level={entry.level_id}, missing={sorted(expected-set(tiles))}, extra={sorted(set(tiles)-expected)}")
    if any(row["selectedLayer"] != "sprites_levelmapchunks" or len(row["candidates"]) != 1 for row in audit.values()):
        raise LevelMapError(f"Normal sprite H tiles are ambiguous or missing: level={entry.level_id}")
    first = tiles[(1, 1)]
    with Image.open(first) as source:
        tile_size = source.size
    width, height = tile_size[0] * columns, tile_size[1] * rows
    if width * height > 150_000_000:
        raise LevelMapError(f"Native map exceeds bounded composition size: level={entry.level_id}, pixels={width*height}")
    placements: list[dict[str, object]] = []
    with Image.new("RGBA", (width, height), (0, 0, 0, 0)) as image:
        for (x, z), path in sorted(tiles.items()):
            with Image.open(path) as original:
                if original.size != tile_size or original.mode != "RGBA":
                    raise LevelMapError(f"H tile dimensions or mode differ: path={path}, size={original.size}, mode={original.mode}")
            pixel_x, pixel_y = (x - 1) * tile_size[0], (rows - z) * tile_size[1]
            with load_tile(path, scene_path.parent, tile_size) as tile:
                image.paste(tile, (pixel_x, pixel_y))
            placements.append({"x": x, "z": z, "pixelX": pixel_x, "pixelY": pixel_y,
                "worldX": entry.xmin + (x - 1) * _SECTOR, "worldZ": entry.zmin + (z - 1) * _SECTOR,
                "sha256": _digest(path), "sourceFile": path.relative_to(scene_path.parent).as_posix()})
        if output.exists():
            raise FileExistsError(output)
        image.save(output, format="PNG")
        alpha = image.getchannel("A").getextrema()
    world_to_pixel, pixel_to_world = _transforms(entry.xmin, entry.xmax, entry.zmin, entry.zmax, width, height)
    return with_level_map_selection_grid({"levelId": entry.level_id, "mapId": entry.map_id, "artOwner": entry.map_id,
        "worldBounds": {"xmin": entry.xmin, "xmax": entry.xmax, "zmin": entry.zmin, "zmax": entry.zmax},
        "sourceConfig": {"virtualPath": entry.config_path, "sha256": entry.config_sha256},
        "tileSizeMetres": _SECTOR, "tilePixelWidth": tile_size[0], "tilePixelHeight": tile_size[1],
        "columns": columns, "rows": rows, "tileCount": len(placements), "needInverseXZ": False,
        "tileLayer": "sprites_levelmapchunks", "cropping": "none", "alpha": "native",
        "pixelOrigin": "northwest", "tileOrder": "columns west-to-east; rows south-to-north",
        "worldToPixel": world_to_pixel, "pixelToWorld": pixel_to_world,
        "image": {"relativePath": output.name, "pixelWidth": width, "pixelHeight": height,
                  "mode": "RGBA", "alphaExtrema": alpha, "bytes": output.stat().st_size, "sha256": _digest(output)},
        "tiles": placements})


def _image_path(image: dict[str, object], output_root: Path) -> Path:
    relative = PurePosixPath(_text(image.get("relativePath"), "image.relativePath"))
    if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative) or ":" in str(relative):
        raise LevelMapError(f"Unsafe level map image path: {relative}")
    path = (output_root / relative).resolve()
    if not path.is_relative_to(output_root.resolve()) or not path.is_file():
        raise LevelMapError(f"Level map image is missing or escapes output root: {relative}")
    return path


def read_level_map_manifest(manifest_path: Path, output_root: Path) -> dict[str, object]:
    """Validate the completed map registry without leaking local source paths."""
    manifest = _object(json.loads(manifest_path.read_text(encoding="utf-8-sig")), "level map manifest")
    if manifest.get("format") != "EndfieldLevelMaps/1" or not isinstance(manifest.get("entries"), list):
        raise LevelMapError(f"Unsupported level map manifest: {manifest_path}")
    identifiers: set[str] = set()
    for raw in manifest["entries"]:
        entry = _object(raw, "level map entry")
        level = _text(entry.get("levelId"), "levelId")
        if not _LEVEL.fullmatch(level) or level in identifiers:
            raise LevelMapError(f"Invalid or duplicate completed level map: {level}")
        identifiers.add(level)
        if _text(entry.get("mapId"), "mapId") != level.split("_", 1)[0]:
            raise LevelMapError(f"Map owner differs from level ID: {level}")
        _text(entry.get("artOwner"), "artOwner")
        bounds = _object(entry.get("worldBounds"), "worldBounds")
        xmin, xmax, zmin, zmax = (_number(bounds.get(key), key) for key in ("xmin", "xmax", "zmin", "zmax"))
        if xmax <= xmin or zmax <= zmin:
            raise LevelMapError(f"Invalid completed map bounds: {level}")
        source_bounds = _object(entry.get("sourceMapBounds"), "sourceMapBounds")
        if any(_number(source_bounds.get(key), key) != bounds[key] for key in ("xmin", "xmax", "zmin", "zmax")):
            raise LevelMapError(f"Source map bounds differ from map world bounds: {level}")
        grid = _validate_selection_grid(_object(entry.get("selectionGrid"), "selectionGrid"))
        expected_grid = level_map_selection_grid(level, entry["mapId"], entry["artOwner"], bounds)
        if grid != expected_grid:
            raise LevelMapError(f"Selection grid owner or bounds differ from map: {level}")
        config = _object(entry.get("sourceConfig"), "sourceConfig")
        _text(config.get("virtualPath"), "sourceConfig.virtualPath")
        digest = _text(config.get("sha256"), "sourceConfig.sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise LevelMapError(f"Invalid source config digest: {level}")
        image = _object(entry.get("image"), "image")
        if not re.fullmatch(r"[0-9a-f]{64}", _text(image.get("sha256"), "image.sha256")):
            raise LevelMapError(f"Invalid image digest: {level}")
        for dimension in ("pixelWidth", "pixelHeight"):
            if type(image.get(dimension)) is not int or image[dimension] <= 0:
                raise LevelMapError(f"Invalid image dimension: {level}.{dimension}")
        path = _image_path(image, output_root)
        if type(image.get("bytes")) is not int or path.stat().st_size != image["bytes"]:
            raise LevelMapError(f"Completed map image size changed: {level}")
        for key in ("worldToPixel", "pixelToWorld"):
            matrix = entry.get(key)
            if not isinstance(matrix, list) or len(matrix) != 3:
                raise LevelMapError(f"Expected 3x3 transform: {level}.{key}")
            for row in matrix:
                if not isinstance(row, list) or len(row) != 3:
                    raise LevelMapError(f"Expected 3x3 transform: {level}.{key}")
                for value in row:
                    _number(value, key)
        expected = _transforms(xmin, xmax, zmin, zmax, image["pixelWidth"], image["pixelHeight"])
        for key, matrix in zip(("worldToPixel", "pixelToWorld"), expected, strict=True):
            if any(not math.isclose(value, target, rel_tol=1e-10, abs_tol=1e-10)
                   for row, targets in zip(entry[key], matrix, strict=True)
                   for value, target in zip(row, targets, strict=True)):
                raise LevelMapError(f"Map transform differs from source bounds and image dimensions: {level}.{key}")
    return manifest


def resolve_level_map_image(manifest_path: Path, output_root: Path, level_id: str) -> Path:
    """Resolve only a registered, intact PNG inside the configured output root."""
    manifest = read_level_map_manifest(manifest_path, output_root)
    for raw in manifest["entries"]:
        entry = _object(raw, "level map entry")
        if entry["levelId"] != level_id:
            continue
        image = _object(entry["image"], "image")
        path = _image_path(image, output_root)
        if _digest(path) != image.get("sha256"):
            raise LevelMapError(f"Completed map image digest changed: {level_id}")
        with Image.open(path) as pixels:
            if pixels.format != "PNG" or pixels.mode != "RGBA" or pixels.size != (image.get("pixelWidth"), image.get("pixelHeight")):
                raise LevelMapError(f"Completed map PNG dimensions or mode changed: {level_id}")
        return path
    raise KeyError(f"Level map is not prepared: {level_id}")


def export_level_map(context: CharacterSourceContext, catalog: LevelMapCatalog, level_id: str,
                     probe_executable: Path, output_root: Path, check_cancelled: CheckCancelled,
                     progress: Progress) -> Path:
    """Extract exact provider closures, decode and register one dynamically selected map."""
    selected = [entry for entry in catalog.entries if entry.level_id == level_id]
    if len(selected) != 1:
        raise LevelMapError(f"Expected one discovered source map: level={level_id}, count={len(selected)}")
    entry = selected[0]
    if not probe_executable.is_file():
        raise FileNotFoundError(f"Scene texture reader is missing: {probe_executable}")
    if entry.inverse_xz:
        raise LevelMapError(f"Source requires unimplemented inverse-XZ map orientation: {level_id}")
    validate_character_storage(context.game_root, output_root)
    if tuple(source_stamp(path) for path in _indexes(context)) != catalog.index_stamps:
        raise CharacterSourceChangedError("Level map catalog indexes are stale; rediscover current maps")
    verify_game_source(context.game_root, [asdict(stamp) for stamp in catalog.game_stamps], check_cancelled)
    registry_path = output_root / "level_maps.json"
    registry = read_level_map_manifest(registry_path, output_root) if registry_path.exists() else {"format": "EndfieldLevelMaps/1", "entries": []}
    if any(_object(row, "entry")["levelId"] == level_id for row in registry["entries"]):
        raise LevelMapError(f"Map is already registered; select a new output root: {level_id}")
    providers, _ = _provider_details(context.provider_index, frozenset(entry.resources), check_cancelled)
    ambiguous = {path: len(values) for path, values in providers.items() if len(values) != 1}
    if ambiguous:
        raise LevelMapError(f"Normal H tile providers are missing or ambiguous: {ambiguous}")
    output = output_root / level_id
    output.mkdir(parents=True, exist_ok=False)
    work = output / "source"
    work.mkdir()
    prepare_providers_bundles(context, tuple({provider.logical_name: provider for values in providers.values()
        for provider in values}.values()), work / "bundles", check_cancelled, progress)
    probe = work / "probe"
    progress("decoding", .5, "Decoding original normal H map tiles")
    observe_helper([str(probe_executable), str(work / "bundles"), str(probe), "--include-unreferenced",
        "--texture-workers", "1", "--texture-memory-mib", "32"], work / "probe.log", work / "temp",
        check_cancelled, lambda row: check_cancelled())
    config = output / "load_config.source.json"
    config.write_bytes(entry.config_bytes)
    metadata = compose_level_map(entry, probe / "scene_manifest.json", output / f"{level_id}.png")
    metadata["image"]["relativePath"] = f"{level_id}/{level_id}.png"
    metadata["catalogId"] = catalog.catalog_id
    metadata["sourceIndexes"] = [{**asdict(stamp), "path": Path(stamp.path).name} for stamp in catalog.index_stamps]
    metadata["sourceReaderIdentity"] = tool_identity(probe_executable)
    metadata["sourceGameIdentity"] = sha256(json.dumps([asdict(stamp) for stamp in catalog.game_stamps], sort_keys=True).encode()).hexdigest()
    verify_game_source(context.game_root, [asdict(stamp) for stamp in catalog.game_stamps], check_cancelled)
    if tuple(source_stamp(path) for path in _indexes(context)) != catalog.index_stamps:
        raise CharacterSourceChangedError("Level map indexes changed during export")
    updated = {**registry, "entries": [*registry["entries"], metadata]}
    temporary = registry_path.with_suffix(".json.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(updated, stream, ensure_ascii=False, indent=2)
    read_level_map_manifest(temporary, output_root)
    resolve_level_map_image(temporary, output_root, level_id)
    temporary.replace(registry_path)
    progress("completed", 1., "Complete source level map registered")
    return registry_path
