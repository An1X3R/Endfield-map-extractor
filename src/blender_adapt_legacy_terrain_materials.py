"""Transfer reviewed legacy terrain materials and atlas UVs without replacing geometry."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_apply_verified_geometry_repairs import mesh_digest
from blender_scene_terrain import adapt_scene_terrain


def fill_retained_atlas_uvs(surface: Path) -> list[dict[str, str]]:
    """Complete height-only tiles using an existing, independently checked atlas binding."""
    manifest_path = surface / "terrain_surface.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "EndfieldTerrainSurfaceAtlas/1" or "atlas_world_bounds" in manifest:
        raise ValueError(f"Expected legacy full-map atlas metadata: path={manifest_path}")
    if manifest["atlas_row_direction"] != "Unity +Z maps to PNG top-to-bottom rows":
        raise ValueError(f"Unsupported terrain atlas orientation: path={manifest_path}")
    origin = np.array(manifest["tile_world_origin"], dtype=np.float64)
    tile_size = float(manifest["tile_world_size"])
    extent = tile_size * int(manifest["world_tile_count"])
    materials = [material for material in bpy.data.materials
                 if material.get("endfield_terrain_surface_source")
                 and Path(material["endfield_terrain_surface_source"]).resolve() == manifest_path.resolve()]
    if len(materials) != 1 or origin.shape != (2,) or tile_size != 64 or extent <= 0:
        raise ValueError(f"Retained terrain atlas is not uniquely supported: path={manifest_path}, materials={len(materials)}")
    material = materials[0]
    for channel in ("albedo", "normal"):
        images = [node.image for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image
                  and Path(node.image.filepath).name == manifest[channel]]
        if len(images) != 1 or not images[0].packed_file:
            raise ValueError(f"Retained atlas image missing or unpacked: channel={channel}")
        if bytes(images[0].packed_file.data) != (surface / manifest[channel]).read_bytes():
            raise ValueError(f"Retained atlas image differs from source: channel={channel}")
    terrain = [obj for obj in bpy.data.objects if obj.type == "MESH" and "unity_tile" in obj]
    checked = 0
    candidates = []
    with closing(sqlite3.connect((surface / manifest["height_database"]).resolve().as_uri() + "?mode=ro", uri=True)) as database:
        for obj in terrain:
            mesh = obj.data
            if material not in tuple(mesh.materials) and not any(
                    item and item.name.startswith("Terrain_HeightOnly_NotTextured") for item in mesh.materials):
                continue
            coordinates = np.array([tuple(obj.matrix_world @ vertex.co) for vertex in mesh.vertices])
            x, z = (int(value) for value in str(obj["unity_tile"]).split(","))
            row = database.execute("SELECT width,height,heights FROM tiles WHERE x=? AND z=?", (x, z)).fetchone()
            if row is None:
                raise ValueError(f"Terrain height source missing: tile={x},{z}")
            width, height, payload = row
            grid = np.frombuffer(payload, dtype="<u2").reshape(height, width)
            local = (coordinates[:, :2] * (1, -1) - origin - np.array((x, z)) * tile_size)
            indices = np.rint(local).astype(np.int64)
            if (not np.allclose(local, indices, rtol=0, atol=0.00001) or np.any(indices < 0)
                    or np.any(indices[:, 0] >= width) or np.any(indices[:, 1] >= height)):
                raise ValueError(f"Retained terrain vertices differ from source grid: object={obj.name}")
            if not np.allclose(coordinates[:, 2], grid[indices[:, 1], indices[:, 0]] / 64.0, rtol=0, atol=0.00001):
                raise ValueError(f"Retained terrain heights differ from source: object={obj.name}")
            vertex_uv = (coordinates[:, :2] * (1, -1) - origin) / extent
            vertex_uv[:, 1] = 1 - vertex_uv[:, 1]
            values = vertex_uv[[loop.vertex_index for loop in mesh.loops]].astype(np.float32).reshape(-1)
            if np.min(values) < 0 or np.max(values) > 1:
                raise ValueError(f"Terrain atlas UV outside coverage: object={obj.name}")
            uv = mesh.uv_layers.get("TerrainWorldUV")
            if material in tuple(mesh.materials):
                if uv is None:
                    raise ValueError(f"Retained textured tile lacks atlas UV: object={obj.name}")
                actual = np.empty(len(values), dtype=np.float32)
                uv.data.foreach_get("uv", actual)
                if not np.allclose(actual, values, rtol=0, atol=0.000001):
                    raise ValueError(f"Retained atlas UV disagrees with metadata: object={obj.name}")
                checked += 1
            else:
                if mesh.uv_layers:
                    raise ValueError(f"Height-only tile has unexpected UV data: object={obj.name}")
                candidates.append((obj, values, hashlib.sha256(payload).hexdigest()))
    if not checked or not candidates:
        raise ValueError(f"Atlas completion requires verified neighbors and missing UV tiles: checked={checked}, candidates={len(candidates)}")
    result = []
    for obj, values, height_digest in candidates:
        uv = obj.data.uv_layers.new(name="TerrainWorldUV")
        uv.data.foreach_set("uv", values)
        obj.data.materials.clear()
        obj.data.materials.append(material)
        result.append({"object": obj.name, "tile": str(obj["unity_tile"]), "material": material.name,
                       "height_sha256": height_digest, "surface": str(manifest_path)})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("blend", "builder", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--surface", type=Path, action="append", required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if args.output.exists() or args.audit.exists():
        raise FileExistsError("Terrain material output and audit must be new")
    spec = importlib.util.spec_from_file_location("legacy_terrain_builder", args.builder)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load terrain builder: {args.builder}")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    bpy.ops.wm.open_mainfile(filepath=str(args.blend))
    before = {o.name: mesh_digest(o) for o in bpy.data.objects if o.type == "MESH"}
    targets = {str(o["unity_tile"]): o for o in bpy.data.objects if o.type == "MESH" and "unity_tile" in o}
    if not targets:
        raise ValueError("Scene has no retained terrain tile references")
    applied = []
    seen: set[str] = set()
    mismatched = []
    for surface in args.surface:
        manifest = json.loads((surface / "terrain_surface.json").read_text(encoding="utf-8"))
        if manifest.get("format") != "EndfieldTerrainSurfaceAtlas/1":
            raise ValueError(f"Unsupported terrain surface format: {surface}")
        xmin, xmax, zmin, zmax = manifest["atlas_world_bounds"]
        wanted = []
        for key, obj in targets.items():
            coordinates = np.array([tuple(v.co) for v in obj.data.vertices])
            if not len(coordinates):
                continue
            center = coordinates.mean(axis=0)
            if xmin <= center[0] < xmax and zmin <= -center[1] < zmax:
                wanted.append(key)
        if not wanted:
            continue
        if seen.intersection(wanted):
            raise ValueError(f"Overlapping terrain atlas assignments: surface={surface}")
        collection = bpy.data.collections.new("TEMP_Legacy_Terrain_Material_Transfer")
        bpy.context.scene.collection.children.link(collection)
        meshes_before = set(bpy.data.meshes)
        builder.build_terrain(surface / manifest["height_database"], collection,
                              tuple(manifest["atlas_world_bounds"]), surface, True,
                              {tuple(int(v) for v in key.split(",")) for key in wanted})
        for source in tuple(collection.objects):
            key = str(source["unity_tile"])
            if key not in wanted:
                continue
            target = targets[key]
            same = (len(source.data.vertices), len(source.data.loops)) == (len(target.data.vertices), len(target.data.loops))
            if same:
                for a, b, field, size, dtype in ((source.data.vertices, target.data.vertices, "co", 3, np.float32),
                                               (source.data.loops, target.data.loops, "vertex_index", 1, np.int32)):
                    left, right = np.empty(len(a) * size, dtype=dtype), np.empty(len(b) * size, dtype=dtype)
                    a.foreach_get(field, left)
                    b.foreach_get(field, right)
                    same = same and bool(np.allclose(left, right, rtol=0, atol=0.00001))
            if not same:
                mismatched.append({"tile": key, "surface": str(surface), "reason": "height_or_topology_mismatch"})
                continue
            if target.data.uv_layers.get("TerrainWorldUV") is not None:
                raise ValueError(f"Terrain UV already exists: tile={key}")
            values = np.empty(len(source.data.loops) * 2, dtype=np.float32)
            source.data.uv_layers["TerrainWorldUV"].data.foreach_get("uv", values)
            if np.min(values) < -0.00001 or np.max(values) > 1.00001:
                raise ValueError(f"Terrain atlas UV outside coverage: tile={key}")
            uv = target.data.uv_layers.new(name="TerrainWorldUV")
            uv.data.foreach_set("uv", values)
            target.data.materials.clear()
            target.data.materials.append(source.data.materials[0])
            applied.append({"tile": key, "object": target.name, "material": source.data.materials[0].name,
                            "atlas": str(surface), "uv_min": float(np.min(values)), "uv_max": float(np.max(values))})
            seen.add(key)
        for source in tuple(collection.objects):
            bpy.data.objects.remove(source, do_unlink=True)
        bpy.data.collections.remove(collection)
        for mesh in set(bpy.data.meshes) - meshes_before:
            if mesh.users:
                raise RuntimeError(f"Temporary terrain mesh still used: {mesh.name}")
            bpy.data.meshes.remove(mesh)
        for image in bpy.data.images:
            if image.packed_file:
                image.buffers_free()
    if not applied:
        raise ValueError("No terrain tiles matched the legacy surface inputs")
    normal_adaptation = adapt_scene_terrain(bpy.context.scene)
    for name, digest in before.items():
        if mesh_digest(bpy.data.objects[name]) != digest:
            raise ValueError(f"Legacy terrain material transfer changed geometry: object={name}")
    bpy.context.scene["terrain_material_status"] = "source_atlas_albedo_world_normal_applied_with_exact_geometry_check"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(args.output))
    for name, digest in before.items():
        if mesh_digest(bpy.data.objects[name]) != digest:
            raise ValueError(f"Reopened terrain material transfer changed geometry: object={name}")
    for row in applied:
        obj = bpy.data.objects[row["object"]]
        if obj.data.materials[0].name != row["material"] or "TerrainWorldUV" not in obj.data.uv_layers:
            raise ValueError(f"Reopened terrain material or UV missing: object={obj.name}")
    args.audit.write_text(json.dumps({"format": "EndfieldLegacyTerrainMaterialTransfer/1", "output": str(args.output),
        "applied": applied, "pending_tiles": sorted(set(targets) - seen), "mismatched": mismatched,
        "original_geometry_verified": True, "reopen_verified": True, "builder": str(args.builder),
        "terrain_normal_adaptation": normal_adaptation}, indent=2), encoding="utf-8")
    print(json.dumps({"event": "terrain_material_transfer_verified", "applied": len(applied), "pending": len(targets) - len(seen)}), flush=True)


if __name__ == "__main__":
    main()
