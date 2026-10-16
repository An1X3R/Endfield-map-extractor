"""Prepare bounded source-domain terrain detail textures through the existing VFS reader."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import sqlite3
import os
from collections.abc import Callable
from typing import BinaryIO, cast
from uuid import uuid4

from PIL import Image

from endfield_scene_io import read_scene_manifest
from endfield_scene_export_plan import Pending
from endfield_terrain_detail_contract import DetailLayer, TerrainDetail, read_surface_metadata
from extractor_core.resources import load_group, validate_vfs_root
from extractor_core.terrain import CORE_SIZE, GROUP_HASH, PAGE_BORDER, PAGE_SIZE, _read_clear_page


TerrainPage = tuple[int, int, int, int, bytes, str, str]


def decode_terrain_texture(width: int, height: int, mips: int, code: int, payload: bytes) -> Image.Image:
    """Use Pillow's existing BCn decoder; return native top-down payload rows."""
    if code not in (100, 101, 108, 109) or min(width, height, mips) < 1:
        raise ValueError(f"Unsupported compressed terrain texture: size={width,height}, mips={mips}, format={code}")
    lengths = [((max(1, width >> level) + 3) // 4) * ((max(1, height >> level) + 3) // 4) * 16
               for level in range(mips)]
    if len(payload) != sum(lengths):
        raise ValueError(f"Terrain mip chain length mismatch: actual={len(payload)}, expected={sum(lengths)}, format={code}")
    return Image.frombytes("RGBA", (width, height), payload[:lengths[0]], "bcn", 7 if code in (108, 109) else 3)


def save_cached_texture(path: Path, image: Image.Image) -> None:
    """Share only byte-identical decoded texture payloads between review regions."""
    if path.exists():
        with Image.open(path) as existing:
            if existing.mode != image.mode or existing.size != image.size or existing.tobytes() != image.tobytes():
                raise ValueError(f"Terrain texture cache disagrees with source: {path}")
    else:
        with path.open("xb") as stream:
            image.save(stream, format="PNG")


def _prepare_detail(map_id: str, metadata: Path, index_path: Path,
                    tiles: tuple[tuple[int, int], ...], output: Path, texture_cache: Path,
                    read_page: Callable[[str], TerrainPage]) -> TerrainDetail:
    if not re.fullmatch(r"[a-z0-9_]+", map_id):
        raise ValueError(f"Invalid terrain resource map name: {map_id}")
    if output.exists():
        raise FileExistsError(output)
    if not tiles or len(set(tiles)) != len(tiles):
        raise ValueError("Terrain detail requires distinct source tiles")
    surface = read_surface_metadata(metadata)
    side = surface["size"] // 64
    if any(not (0 <= x < side and 0 <= z < side) for x, z in tiles):
        raise ValueError(f"Terrain tile outside source extent: tiles={tiles}, side={side}")
    x0, x1 = min(x for x, _ in tiles), max(x for x, _ in tiles) + 1
    z0, z1 = min(z for _, z in tiles), max(z for _, z in tiles) + 1
    with Image.open(index_path) as source_index:
        index = source_index.convert("RGBA")
    if index.size != (side * 8, side * 8):
        raise ValueError(f"Unexpected source index density: size={index.size}, terrain_size={surface['size']}")
    window = index.crop((max(0, x0*8-1), max(0, index.height-z1*8-1),
                         min(index.width, x1*8+1), min(index.height, index.height-z0*8+1)))
    index_bytes = window.tobytes()
    if any(value % 4 or value // 4 >= len(surface["layers"]) for value in index_bytes):
        raise ValueError("Source terrain index is not a supported global layer-ordinal image")
    ordinals = sorted({value // 4 for value in index_bytes})
    texture_cache.mkdir(parents=True, exist_ok=True)
    layers: list[DetailLayer] = []
    provenance = []
    for ordinal in ordinals:
        source = surface["layers"][ordinal]
        paths: dict[str, str] = {}
        for kind, expected_code, field in (("D", 108, "diffuse"), ("N", 109, "normal")):
            resource = f"Data/Terrain/PC/{map_id}/Layers/LAYER_{kind}_{source['texture_index']}.bytes"
            width, height, mips, code, payload, source_md5, payload_sha256 = read_page(resource)
            if code != expected_code:
                raise ValueError(f"Unexpected layer colour-space format: {resource}, code={code}")
            image = decode_terrain_texture(width, height, mips, code, payload).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            target = texture_cache / f"{map_id}_{kind}_{source['texture_index']}_{source_md5}.png"
            save_cached_texture(target, image)
            paths[field] = str(target.resolve())
            provenance.append({"resource": resource, "md5": source_md5, "file": str(target),
                               "format": code, "mips_preserved_in_source": mips, "exported_mip": 0,
                               "payload_sha256": payload_sha256})
        layers.append({**source, "diffuse": paths["diffuse"], "normal": paths["normal"]})
    size = ((x1 - x0)*CORE_SIZE, (z1 - z0)*CORE_SIZE)
    controls = Image.new("RGBA", size)
    tint = Image.new("RGBA", size)
    level = int(math.log2(side))
    for x, z in tiles:
        for kind, atlas in (("S", controls), ("T", tint)):
            resource = f"Data/Terrain/PC/{map_id}/Terrain_{level}_{x}_{z}_{kind}.bytes"
            width, height, mips, code, payload, source_md5, payload_sha256 = read_page(resource)
            if (width, height, mips, code) != (PAGE_SIZE, PAGE_SIZE, 1, 8 if kind == "S" else 100):
                raise ValueError(f"Unexpected control page header: {resource}, header={width,height,mips,code}")
            if kind == "S":
                if len(payload) != width * height * 4:
                    raise ValueError(f"Invalid uncompressed control page size: {resource}")
                image = Image.frombytes("RGBA", (width,height), payload)
            else:
                image = decode_terrain_texture(width,height,mips,code,payload)
            core = image.crop((PAGE_BORDER,PAGE_BORDER,PAGE_BORDER+CORE_SIZE,PAGE_BORDER+CORE_SIZE))
            atlas.paste(core, ((x-x0)*CORE_SIZE,(z-z0)*CORE_SIZE))
            provenance.append({"resource": resource, "md5": source_md5,
                               "payload_sha256": payload_sha256})
    output.mkdir(parents=True)
    controls.save(output / "control.png")
    tint.save(output / "tint.png")
    index.save(output / "index.png")
    px, _, pz = surface["position"]
    result: TerrainDetail = {"format": "EndfieldTerrainDetail/1", "map": map_id,
        "position": surface["position"], "size": surface["size"], "tiles": [list(tile) for tile in tiles],
        "bounds": [px+x0*64,px+x1*64,pz+z0*64,pz+z1*64], "index": "index.png",
        "control": "control.png", "tint": "tint.png", "layers": layers,
        "source_metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
        "source_index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "projection": "source_domain_smooth_triplanar_v1"}
    (output / "terrain_detail.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (output / "source_validation.json").write_text(json.dumps({"resources": provenance,
        "orientation": {"layer_d_n": "Native BC7 rows flipped to match Texture2DConverter PNG orientation; use direct Unity UV",
                        "control_tint": "Native page rows retained; sample with V=1-localZ",
                        "index": "SceneProbe PNG retained; source-domain Y-flip corresponds to direct positive world V"},
        "limitations": ["Source-domain UV scale and smooth triplanar are Blender adaptations",
                        "Runtime stochastic projection, C cliff layers and POM are not reconstructed"]}, indent=2), encoding="utf-8")
    return result


def prepare_detail(vfs: Path, map_id: str, metadata: Path, index_path: Path,
                   tiles: tuple[tuple[int, int], ...], output: Path, texture_cache: Path) -> TerrainDetail:
    """Prepare the original VFS-backed research entry point."""
    group_dir = validate_vfs_root(vfs) / GROUP_HASH
    group = load_group(vfs, GROUP_HASH)
    pages = {item.file_name.casefold(): item for item in group.iter_files()}
    handles: dict[Path, BinaryIO] = {}

    def read_page(name: str) -> TerrainPage:
        item = pages.get(name.casefold())
        if item is None:
            raise FileNotFoundError(f"Terrain VFS page is missing: {name}")
        width, height, mips, code, payload = _read_clear_page(item, group_dir, handles)
        return width, height, mips, code, payload, item.file_data_md5, hashlib.sha256(payload).hexdigest()

    try:
        return _prepare_detail(map_id, metadata, index_path, tiles, output, texture_cache, read_page)
    finally:
        for stream in handles.values():
            stream.close()


def prepare_cached_detail(map_id: str, metadata: Path, index_path: Path,
                          tiles: tuple[tuple[int, int], ...], resources_manifest: Path,
                          output: Path, texture_cache: Path) -> TerrainDetail:
    """Decode first-run preserved pages without reopening the game VFS."""
    source = json.loads(resources_manifest.read_text(encoding="utf-8-sig"))
    if not isinstance(source, dict) or source.get("format") != "EndfieldTerrainDetailResources/1" or source.get("map") != map_id:
        raise ValueError(f"Invalid preserved terrain resources: {resources_manifest}")
    rows = source.get("resources")
    if not isinstance(rows, list):
        raise ValueError(f"Missing preserved terrain page records: {resources_manifest}")
    pages: dict[str, dict[str, object]] = {}
    for value in rows:
        if not isinstance(value, dict) or not isinstance(value.get("resource"), str):
            raise ValueError(f"Invalid preserved terrain page record: {resources_manifest}")
        name = value["resource"].casefold()
        if name in pages:
            raise ValueError(f"Repeated preserved terrain page: {name}")
        pages[name] = value

    def read_page(name: str) -> TerrainPage:
        row = pages.get(name.casefold())
        if row is None:
            raise FileNotFoundError(f"Preserved terrain page is missing: {name}")
        relative = row.get("file")
        if not isinstance(relative, str):
            raise ValueError(f"Preserved terrain page has no file: {name}")
        path = (resources_manifest.parent / relative).resolve()
        if not path.is_relative_to(resources_manifest.parent.resolve()):
            raise ValueError(f"Preserved terrain page escapes its surface cache: {path}")
        raw = path.read_bytes()
        if len(raw) < 20:
            raise ValueError(f"Preserved terrain page is truncated: {path}")
        magic, version, width, height, mips, code, length = struct.unpack_from("<4sIHHHHI", raw)
        payload = raw[20:]
        if magic != b"TRET" or version != 1 or len(payload) != length:
            raise ValueError(f"Invalid preserved terrain page header: {path}")
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        if (width, height, mips, code, length, payload_sha256) != (
                row.get("width"), row.get("height"), row.get("mip_count"), row.get("format_code"),
                row.get("payload_bytes"), row.get("payload_sha256")):
            raise ValueError(f"Preserved terrain page disagrees with manifest: {path}")
        source_md5 = row.get("source_md5")
        if not isinstance(source_md5, str) or not re.fullmatch(r"[0-9A-Fa-f]{32}", source_md5):
            raise ValueError(f"Invalid preserved terrain source digest: {path}")
        return width, height, mips, code, payload, source_md5, payload_sha256

    return _prepare_detail(map_id, metadata, index_path, tiles, output, texture_cache, read_page)


def terrain_detail_source_paths(map_id: str) -> tuple[str, str]:
    """Declare the exact global assets required by selected terrain surfaces."""
    if not re.fullmatch(r"[a-z0-9_]+", map_id):
        raise ValueError(f"Invalid terrain resource map name: {map_id}")
    prefix = f"assets/beyond/dynamicassets/scenes/pc/{map_id}/terrain/"
    return prefix + "hgterrainsurfacesdata.asset", prefix + "splatindexmap.tga"


def _scene_sources(manifests: tuple[Path, ...], map_id: str) -> tuple[Path, Path]:
    """Resolve exact source metadata bundle and SplatIndexMap export by AssetRef."""
    metadata_name, index_name = terrain_detail_source_paths(map_id)
    metadata_refs: set[tuple[str, int]] = set()
    index_refs: set[tuple[str, int]] = set()
    serialized: dict[str, list[tuple[Path, str]]] = {}
    textures: dict[tuple[str, int], list[tuple[Path, str]]] = {}
    for manifest in manifests:
        data = read_scene_manifest(manifest)
        if not isinstance(data, dict) or data.get("Format") != "EndfieldSceneProbe/1":
            raise ValueError(f"Invalid SceneProbe terrain source manifest: {manifest}")
        for value in data.get("Containers", []):
            if not isinstance(value, dict) or not isinstance(value.get("Asset"), dict):
                continue
            name = str(value.get("Container", "")).replace("\\", "/").casefold()
            asset = value["Asset"]
            key = (asset.get("Source"), asset.get("PathId"))
            if not isinstance(key[0], str) or type(key[1]) is not int:
                continue
            if name == metadata_name and asset.get("Type") == "MonoBehaviour":
                metadata_refs.add(cast(tuple[str, int], key))
            elif name == index_name and asset.get("Type") == "Texture2D":
                index_refs.add(cast(tuple[str, int], key))
        for value in data.get("SerializedFiles", []):
            if not isinstance(value, dict) or not isinstance(value.get("Name"), str):
                continue
            original = value.get("OriginalPath")
            if not isinstance(original, str) or not original:
                continue
            serialized.setdefault(value["Name"], []).append((manifest, original))
        for value in data.get("TextureExports", []):
            if not isinstance(value, dict) or not isinstance(value.get("Asset"), dict):
                continue
            asset = value["Asset"]
            key = (asset.get("Source"), asset.get("PathId"))
            if not isinstance(key[0], str) or type(key[1]) is not int or not isinstance(value.get("File"), str):
                continue
            textures.setdefault(cast(tuple[str, int], key), []).append(
                (manifest, value["File"].replace("\\", "/")))
    if len(metadata_refs) != 1 or len(index_refs) != 1:
        raise ValueError(f"Expected one exact terrain metadata and index AssetRef for {map_id}: "
                         f"metadata={sorted(metadata_refs)}, index={sorted(index_refs)}")
    metadata_ref = next(iter(metadata_refs))
    index_ref = next(iter(index_refs))

    def resolve_export(manifest: Path, value: str) -> Path:
        candidate = Path(value)
        candidate = candidate if candidate.is_absolute() else manifest.parent / candidate
        return candidate.resolve()

    bundles = sorted({resolve_export(manifest, value)
                      for manifest, value in serialized.get(metadata_ref[0], [])})
    indices = sorted({resolve_export(manifest, value)
                      for manifest, value in textures.get(index_ref, [])})
    if not bundles or any(not path.is_file() for path in bundles) or not indices or any(not path.is_file() for path in indices):
        raise FileNotFoundError(f"Terrain source bundle or index export is missing: map={map_id}, "
                                f"bundles={bundles}, indices={indices}")
    for label, paths in (("bundle", bundles), ("SplatIndexMap", indices)):
        hashes = set()
        for path in paths:
            with path.open("rb") as stream:
                hashes.add(hashlib.file_digest(stream, "sha256").digest())
        if len(hashes) != 1:
            raise ValueError(f"Exact terrain {label} AssetRef has conflicting files: {paths}")
    return bundles[0], indices[0]


def _source_metadata(runtime_path: Path, map_id: str, bundle: Path, root: Path,
                     check_cancelled: Callable[[], None]) -> Path:
    """Dump one already prepared bundle through the first-run SceneProbe helper."""
    from endfield_scene_export import run_scene_process
    from endfield_scene_resources import read_json, resolve_path

    state = read_json(runtime_path.parent / "first_run_state.json")
    steps = cast(dict[str, object], state["steps"])
    helper = cast(dict[str, object], cast(dict[str, object], steps["helper_dependencies"])["outputs"])
    outputs = cast(dict[str, object], helper["outputs"])
    probe = resolve_path(cast(dict[str, object], outputs["sceneProbe"])["path"], runtime_path.parent)
    if not probe.is_file():
        raise FileNotFoundError(f"Prepared SceneProbe helper is missing: {probe}")
    with bundle.open("rb") as stream:
        bundle_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    folder = root / "metadata" / f"{map_id}_{bundle_hash[:24]}"
    manifest = folder / "scene_manifest.json"
    if not manifest.is_file():
        if folder.exists():
            raise ValueError(f"Partial terrain metadata cache requires inspection: {folder}")
        folder.parent.mkdir(parents=True, exist_ok=True)
        work = root / "work" / uuid4().hex
        work.mkdir(parents=True)
        environment = {**os.environ, "TEMP": str(work), "TMP": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
        output = work / "probe"
        run_scene_process([str(probe), str(bundle), str(output), "--include-unreferenced", "--manifest-only",
                           "--dump-metadata"], work / "probe.log", environment, check_cancelled)
        if not (output / "scene_manifest.json").is_file():
            raise FileNotFoundError(f"SceneProbe did not export terrain metadata: {output}")
        os.replace(output, folder)
    data = read_json(manifest)
    expected = f"assets/beyond/dynamicassets/scenes/pc/{map_id}/terrain/hgterrainsurfacesdata.asset"
    references = {cast(tuple[str, int], (asset["Source"], asset["PathId"]))
                  for row in cast(list[dict[str, object]], data["Containers"])
                  if str(row.get("Container", "")).replace("\\", "/").casefold() == expected
                  for asset in [cast(dict[str, object], row["Asset"])]}
    matches = []
    for row in cast(list[dict[str, object]], data["MetadataExports"]):
        asset = cast(dict[str, object], row["Asset"])
        if (asset.get("Source"), asset.get("PathId")) in references and isinstance(row.get("File"), str):
            matches.append((folder / cast(str, row["File"]).replace("\\", "/")).resolve())
    if len(references) != 1 or len(matches) != 1 or not matches[0].is_file():
        raise ValueError(f"SceneProbe did not identify unique HGTerrainSurfacesData metadata: {manifest}")
    return matches[0]


def _selected_tiles(surface: Path, bounds: dict[str, float]) -> tuple[tuple[int, int], ...]:
    data = json.loads((surface / "terrain_surface.json").read_text(encoding="utf-8-sig"))
    origin = data["tile_world_origin"]
    size = float(data["tile_world_size"])
    database = (surface / data["height_database"]).resolve()
    if not database.is_relative_to(surface.resolve()):
        raise ValueError(f"Terrain height database escapes its surface cache: {database}")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT x,z FROM tiles ORDER BY z,x").fetchall()
    return tuple((x, z) for x, z in rows if origin[0] + x * size < bounds["xmax"]
                 and origin[0] + (x + 1) * size > bounds["xmin"]
                 and origin[1] + z * size < bounds["zmax"]
                 and origin[1] + (z + 1) * size > bounds["zmin"])


def _detail_resources_path(surface: Path, info: dict[str, object]) -> Path | None:
    value = info.get("detail_resources")
    if value is None:
        return None
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError(f"Invalid terrain detail resource path in {surface}: {value}")
    path = (surface / value).resolve()
    if not path.is_relative_to(surface.resolve()):
        raise ValueError(f"Terrain detail resource path escapes its surface cache: {path}")
    return path


def _existing_detail_result(details: list[str], pending: list[Pending]) -> dict[str, object] | None:
    unresolved = [row for row in pending if row["component"] == "terrain_detail"]
    if not details and not unresolved:
        return None
    return {"status": "partial" if unresolved else "ready", "details": details,
            "pending": unresolved, "reused_plan": True}


def prepare_scene_terrain_details(runtime_path: Path, plan_path: Path,
                                  check_cancelled: Callable[[], None],
                                  progress: Callable[[float, str], None]) -> dict[str, object]:
    """Prepare selected cached terrain pages and atomically attach exact detail paths."""
    from endfield_scene_export_plan import load_scene_export_plan
    from endfield_scene_resources import read_json, resource_cache_root
    from endfield_terrain_detail_contract import load_terrain_detail

    runtime = read_json(runtime_path)
    plan = load_scene_export_plan(plan_path)
    existing = _existing_detail_result(plan.get("terrain_details", []), plan["pending"])
    if existing is not None:
        return existing
    surfaces = tuple(Path(value) for value in plan["terrain_surfaces"])
    if not surfaces:
        return {"status": "not_selected", "details": [], "pending": []}
    root = resource_cache_root(runtime_path, runtime) / "terrain_details"
    root.mkdir(parents=True, exist_ok=True)
    manifests = tuple(Path(value) for value in plan["scene_manifests"])
    map_id = plan["map_id"]
    check_cancelled()
    bundle, index = _scene_sources(manifests, map_id)
    metadata = _source_metadata(runtime_path, map_id, bundle, root, check_cancelled)
    source = read_surface_metadata(metadata)
    bounds = plan["bounds"]
    ready: list[str] = []
    pending: list[dict[str, object]] = []
    for number, surface in enumerate(surfaces):
        check_cancelled()
        progress(number / len(surfaces), f"Preparing terrain detail surface {number + 1}/{len(surfaces)}")
        info = read_json(surface / "terrain_surface.json")
        tiles = _selected_tiles(surface, bounds)
        if not tiles:
            continue
        if info.get("map") != map_id or info.get("terrain_size") != source["size"] or info.get("terrain_position") != source["position"]:
            raise ValueError(f"Terrain source metadata disagrees with first-run height surface: {surface}")
        resources = _detail_resources_path(surface, info)
        if resources is None or not resources.is_file():
            pending.append({"entity_id": -1, "entity_base": "terrain", "component": "terrain_detail",
                            "reason": "source_detail_incomplete", "detail": f"Missing preserved terrain pages: {resources}"})
            continue
        retained = read_json(resources)
        level = int(math.log2(source["size"] // 64))
        available = {tuple(row) for row in cast(list[list[int]], retained["source_tiles"])}
        missing = sorted((level, x, z) for x, z in tiles if (level, x, z) not in available)
        if missing:
            pending.append({"entity_id": -1, "entity_base": "terrain", "component": "terrain_detail",
                            "reason": "source_detail_incomplete",
                            "detail": f"No matching finest-level S/T source pages for {surface}: tiles={missing}"})
            continue
        selected_addresses = {(level, x, z) for x, z in tiles}
        incomplete = [row for row in cast(list[list[object]], retained.get("missing_pages", []))
                      if isinstance(row, list) and len(row) == 4 and tuple(row[:3]) in selected_addresses
                      and row[3] in ("S", "T")]
        if incomplete:
            pending.append({"entity_id": -1, "entity_base": "terrain", "component": "terrain_detail",
                            "reason": "source_detail_incomplete",
                            "detail": f"Selected S/T pages were absent from first-run source: {surface}, pages={incomplete}"})
            continue
        identity = hashlib.sha256(json.dumps({"version": 1, "surface": str(surface.resolve()),
            "tiles": tiles, "metadata": hashlib.sha256(metadata.read_bytes()).hexdigest(),
            "index": hashlib.sha256(index.read_bytes()).hexdigest(),
            "resources": hashlib.sha256(resources.read_bytes()).hexdigest()}, sort_keys=True).encode()).hexdigest()
        output = root / identity
        detail_path = output / "terrain_detail.json"
        if output.exists():
            detail = load_terrain_detail(detail_path)
            if detail["tiles"] != [list(tile) for tile in tiles]:
                raise ValueError(f"Cached terrain detail selected different source tiles: {detail_path}")
        else:
            work = root / "work" / uuid4().hex
            work.parent.mkdir(exist_ok=True)
            prepare_cached_detail(map_id, metadata, index, tiles, resources, work, root / "textures")
            detail = read_json(work / "terrain_detail.json")
            detail["source_surface"] = str((surface / "terrain_surface.json").resolve())
            (work / "terrain_detail.json").write_text(json.dumps(detail, indent=2), encoding="utf-8")
            os.replace(work, output)
            load_terrain_detail(detail_path)
        ready.append(str(detail_path))
    updated = dict(plan)
    updated["terrain_details"] = ready
    updated["pending"] = [*plan["pending"], *pending]
    updated["summary"] = {**plan["summary"], "pending": len(updated["pending"])}
    temporary = plan_path.with_name(plan_path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(updated, stream, indent=2)
    os.replace(temporary, plan_path)
    load_scene_export_plan(plan_path)
    progress(1.0, f"Prepared {len(ready)} terrain detail surfaces; {len(pending)} pending")
    return {"status": "partial" if pending else "ready", "details": ready, "pending": pending}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("vfs", "metadata", "index", "output", "texture-cache"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--map", required=True)
    parser.add_argument("--tile", nargs=2, type=int, action="append", required=True)
    args = parser.parse_args()
    result = prepare_detail(args.vfs, args.map, args.metadata, args.index,
                            tuple(tuple(tile) for tile in args.tile), args.output, args.texture_cache)
    print(json.dumps({"event": "terrain_detail_prepared", "map": result["map"], "layers": len(result["layers"])}))


if __name__ == "__main__":
    main()
