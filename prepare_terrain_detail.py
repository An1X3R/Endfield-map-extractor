"""Prepare bounded source-domain terrain detail textures through the existing VFS reader."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from typing import BinaryIO

from PIL import Image

from endfield_terrain_detail_contract import DetailLayer, TerrainDetail, read_surface_metadata
from extractor_core.resources import load_group, validate_vfs_root
from extractor_core.terrain import CORE_SIZE, GROUP_HASH, PAGE_BORDER, PAGE_SIZE, _read_clear_page


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


def prepare_detail(vfs: Path, map_id: str, metadata: Path, index_path: Path,
                   tiles: tuple[tuple[int, int], ...], output: Path, texture_cache: Path) -> TerrainDetail:
    game_vfs = validate_vfs_root(vfs)
    if not re.fullmatch(r"[a-z0-9_]+", map_id):
        raise ValueError(f"Invalid terrain resource map name: {map_id}")
    for folder in (output, texture_cache):
        if folder.resolve() == game_vfs or game_vfs in folder.resolve().parents:
            raise ValueError(f"Terrain detail output must be outside the game VFS: {folder}")
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
    if len(tiles) != (x1 - x0) * (z1 - z0):
        raise ValueError("Terrain detail currently requires a fully covered rectangular tile selection")
    index = Image.open(index_path).convert("RGBA")
    if index.size != (side * 8, side * 8):
        raise ValueError(f"Unexpected source index density: size={index.size}, terrain_size={surface['size']}")
    window = index.crop((max(0, x0*8-1), max(0, index.height-z1*8-1),
                         min(index.width, x1*8+1), min(index.height, index.height-z0*8+1)))
    index_bytes = window.tobytes()
    if any(value % 4 or value // 4 >= len(surface["layers"]) for value in index_bytes):
        raise ValueError("Source terrain index is not a supported global layer-ordinal image")
    ordinals = sorted({value // 4 for value in index_bytes})
    texture_cache.mkdir(parents=True, exist_ok=True)
    files = {item.file_name: item for item in load_group(game_vfs, GROUP_HASH).iter_files()}
    handles: dict[Path, BinaryIO] = {}
    layers: list[DetailLayer] = []
    provenance = []
    try:
        for ordinal in ordinals:
            source = surface["layers"][ordinal]
            paths: dict[str, str] = {}
            for kind, expected_code, field in (("D", 108, "diffuse"), ("N", 109, "normal")):
                resource = f"Data/Terrain/PC/{map_id}/Layers/LAYER_{kind}_{source['texture_index']}.bytes"
                if resource not in files:
                    raise FileNotFoundError(f"Missing referenced terrain layer: {resource}")
                item = files[resource]
                width, height, mips, code, payload = _read_clear_page(item, game_vfs / GROUP_HASH, handles)
                if code != expected_code:
                    raise ValueError(f"Unexpected layer colour-space format: {resource}, code={code}")
                image = decode_terrain_texture(width, height, mips, code, payload).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                target = texture_cache / f"{map_id}_{kind}_{source['texture_index']}_{item.file_data_md5}.png"
                save_cached_texture(target, image)
                paths[field] = str(target.resolve())
                provenance.append({"resource": resource, "md5": item.file_data_md5, "file": str(target),
                                   "format": code, "mips_preserved_in_source": mips, "exported_mip": 0,
                                   "payload_sha256": hashlib.sha256(payload).hexdigest()})
            layers.append({**source, "diffuse": paths["diffuse"], "normal": paths["normal"]})
        size = ((x1 - x0)*CORE_SIZE, (z1 - z0)*CORE_SIZE)
        controls = Image.new("RGBA", size)
        tint = Image.new("RGBA", size)
        level = int(math.log2(side))
        for x, z in tiles:
            for kind, atlas in (("S", controls), ("T", tint)):
                resource = f"Data/Terrain/PC/{map_id}/Terrain_{level}_{x}_{z}_{kind}.bytes"
                if resource not in files:
                    raise FileNotFoundError(f"Missing terrain control page: {resource}")
                item = files[resource]
                width, height, mips, code, payload = _read_clear_page(item, game_vfs / GROUP_HASH, handles)
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
                provenance.append({"resource": resource, "md5": item.file_data_md5,
                                   "payload_sha256": hashlib.sha256(payload).hexdigest()})
    finally:
        for stream in handles.values():
            stream.close()
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
