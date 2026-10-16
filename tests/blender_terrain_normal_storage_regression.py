"""Verify native terrain normal fan encoding against a retained real scene plan."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy
import numpy as np
from numpy.typing import NDArray

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_build_map02_chunk_20260727 import build_terrain
from blender_scene_terrain import adapt_scene_terrain, adapt_scene_terrain_mesh_normals
from blender_terrain_atlas import crop_terrain_atlases


def geometry_digest(mesh: bpy.types.Mesh) -> str:
    """Include source positions, connectivity and every UV layer, excluding shading."""
    coordinates = np.empty(len(mesh.vertices)*3, dtype=np.float32)
    vertices = np.empty(len(mesh.loops), dtype=np.int32)
    edges = np.empty(len(mesh.edges)*2, dtype=np.int32)
    mesh.vertices.foreach_get("co", coordinates)
    mesh.loops.foreach_get("vertex_index", vertices)
    mesh.edges.foreach_get("vertices", edges)
    buffers = [coordinates.tobytes(), vertices.tobytes(), edges.tobytes(),
               json.dumps([list(face.loop_indices) for face in mesh.polygons]).encode()]
    for layer in mesh.uv_layers:
        uv = np.empty(len(mesh.loops)*2, dtype=np.float32)
        layer.data.foreach_get("uv", uv)
        buffers.append(layer.name.encode()+uv.tobytes())
    return hashlib.sha256(b"".join(buffers)).hexdigest()


def corner_normals(mesh: bpy.types.Mesh) -> NDArray[np.float32]:
    values = np.empty(len(mesh.loops)*3, dtype=np.float32)
    mesh.corner_normals.foreach_get("vector", values)
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--")+1:])
    args.output.mkdir(parents=True, exist_ok=False)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    collection = bpy.data.collections.new("Source Terrain Regression")
    scene = bpy.context.scene
    scene.collection.children.link(collection)
    bounds = tuple(plan["bounds"][key] for key in ("xmin", "xmax", "zmin", "zmax"))
    for value in plan["terrain_surfaces"]:
        surface = Path(value)
        manifest = json.loads((surface / "terrain_surface.json").read_text(encoding="utf-8"))
        build_terrain(surface / manifest["height_database"], collection, bounds, surface, False)
    objects = tuple(collection.objects)
    original = {obj.name: geometry_digest(obj.data) for obj in objects}
    if not objects:
        raise AssertionError("The real plan produced no terrain")
    adapt_scene_terrain(scene)
    initial = json.loads(scene["endfield_terrain_mesh_normal_audit"])
    partitions = {obj.name: json.loads(obj.data["endfield_terrain_normal_fan_partition"])
                  for obj in objects if "endfield_terrain_normal_fan_partition" in obj.data}
    if not partitions:
        raise AssertionError("The source plan did not exercise the native zero-normal fan defect")
    normals = {obj.name: corner_normals(obj.data) for obj in objects}
    repeated = adapt_scene_terrain_mesh_normals(scene)
    if any(row["status"] != "already_adapted" for row in repeated):
        raise AssertionError(repeated)
    if any(not np.array_equal(normals[obj.name], corner_normals(obj.data)) for obj in objects):
        raise AssertionError("Repeated adaptation changed stored normals")
    crops = crop_terrain_atlases(scene)
    cropped = adapt_scene_terrain_mesh_normals(scene)
    if not crops:
        raise AssertionError("The source plan did not exercise the production crop path")
    if any(geometry_digest(obj.data) != original[obj.name] for obj in objects):
        raise AssertionError("Normal adaptation or cropping changed source geometry or UVs")
    for obj in objects:
        partition_edges = set(partitions.get(obj.name, {}).get("edges", []))
        if partition_edges and not all(obj.data.edges[index].use_edge_sharp for index in partition_edges):
            raise AssertionError("Native encoding fan partitions were lost after cropping")
    before_save = {obj.name: hashlib.sha256(corner_normals(obj.data).tobytes()).hexdigest() for obj in objects}
    blend = args.output / "terrain-normal-storage.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
    reopened = adapt_scene_terrain_mesh_normals(bpy.context.scene)
    for name, digest in original.items():
        mesh = bpy.data.objects[name].data
        if geometry_digest(mesh) != digest:
            raise AssertionError(f"Serialized terrain geometry or UVs changed: {name}")
        if hashlib.sha256(corner_normals(mesh).tobytes()).hexdigest() != before_save[name]:
            raise AssertionError(f"Serialized native normal encoding changed: {name}")
    result = {"blender_version": bpy.app.version_string, "plan": str(args.plan.resolve()),
              "geometry_sha256": original, "initial": initial, "repeated": repeated,
              "crops": crops, "cropped": cropped, "reopened": reopened, "partitions": partitions,
              "geometry_and_uv_unchanged": True, "stored_normals_persisted": True}
    (args.output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
