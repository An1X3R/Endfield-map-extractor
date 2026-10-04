"""Export a bounded random review sample from an already restored scene.

The original instance node groups, meshes, materials and packed textures are
retained. Only point selection changes, using the existing repair workflow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Euler, Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_apply_verified_geometry_repairs import mesh_digest, world_box
from blender_terrain_atlas import crop_terrain_atlases
from blender_scene_terrain import adapt_scene_terrain, adapt_scene_terrain_mesh_normals


def sample_mesh_digest(obj: bpy.types.Object) -> str:
    digest = hashlib.sha256(mesh_digest(obj).encode())
    indices = np.empty(len(obj.data.polygons), dtype=np.int32)
    obj.data.polygons.foreach_get("material_index", indices)
    digest.update(indices.tobytes())
    digest.update(json.dumps([material.name if material else None for material in obj.data.materials]).encode())
    for layer in obj.data.uv_layers:
        values = np.empty(len(layer.data) * 2, dtype=np.float32)
        layer.data.foreach_get("uv", values)
        digest.update(layer.name.encode())
        digest.update(values.tobytes())
    return digest.hexdigest()


def source_template(obj: bpy.types.Object) -> bpy.types.Object:
    sources = {node.inputs["Object"].default_value
               for modifier in obj.modifiers if modifier.type == "NODES" and modifier.node_group
               for node in modifier.node_group.nodes
               if node.type == "OBJECT_INFO" and node.inputs["Object"].default_value}
    if len(sources) != 1:
        raise ValueError(f"Sample requires exactly one template per instancer: object={obj.name}")
    return next(iter(sources))


def point_rows(obj: bpy.types.Object, indices: list[int]) -> list[tuple[tuple[float, ...], ...]]:
    return [(tuple(obj.data.vertices[index].co),
             tuple(obj.data.attributes["ef_rotation"].data[index].vector),
             tuple(obj.data.attributes["ef_scale"].data[index].vector)) for index in indices]


def selected_instancer(obj: bpy.types.Object, indices: list[int]) -> bpy.types.Object:
    """Reuse the verified geometry-repair point-removal method."""
    expected = point_rows(obj, indices)
    result = obj.copy()
    result.data = obj.data.copy()
    selected = set(indices)
    mesh = bmesh.new()
    mesh.from_mesh(result.data)
    mesh.verts.ensure_lookup_table()
    bmesh.ops.delete(mesh, geom=[vertex for vertex in mesh.verts if vertex.index not in selected], context="VERTS")
    mesh.to_mesh(result.data)
    mesh.free()
    result["instance_count"] = len(indices)
    if "verified_entity_ids_json" in obj:
        entity_ids = json.loads(obj["verified_entity_ids_json"])
        if not isinstance(entity_ids, list) or len(entity_ids) != len(obj.data.vertices) or any(type(value) is not int for value in entity_ids):
            raise ValueError(f"Verified instance IDs do not match source points: object={obj.name}")
        result["verified_entity_ids_json"] = json.dumps([entity_ids[index] for index in indices])
    if point_rows(result, list(range(len(indices)))) != expected:
        raise ValueError(f"Sample changed instance transforms or ordering: object={obj.name}")
    return result


def material_images(materials: set[bpy.types.Material]) -> set[bpy.types.Image]:
    images: set[bpy.types.Image] = set()
    visited: set[bpy.types.NodeTree] = set()
    pending = [material.node_tree for material in materials if material.use_nodes]
    while pending:
        tree = pending.pop()
        if tree in visited:
            continue
        visited.add(tree)
        for node in tree.nodes:
            if node.type == "TEX_IMAGE" and node.image:
                images.add(node.image)
            elif node.type == "GROUP" and node.node_tree:
                pending.append(node.node_tree)
    return images


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("blend", "output", "audit", "preview"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--cell-size", type=float, required=True)
    parser.add_argument("--max-instances", type=int, required=True)
    parser.add_argument("--max-image-mib", type=int, required=True)
    parser.add_argument("--cell", type=int, nargs=2, help="Restrict verification to one Blender XY cell while retaining all sample budgets")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    for path in (args.output, args.audit, args.preview):
        if path.exists():
            raise FileExistsError(path)
    if args.cell_size <= 0 or args.max_instances < 30 or args.max_image_mib <= 0:
        raise ValueError("Sample limits must be positive and allow at least 30 instances")
    bpy.ops.wm.open_mainfile(filepath=str(args.blend))
    original = bpy.context.scene
    instancers = [obj for obj in original.objects if obj.type == "MESH" and "instance_count" in obj]
    templates = {obj.name: source_template(obj) for obj in instancers}
    objects = {obj.name: obj for obj in instancers}
    cells: dict[tuple[int, int], dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    point_references: list[tuple[str, int]] = []
    instance_boxes: list[tuple[np.ndarray, np.ndarray]] = []
    for obj in instancers:
        corners = tuple(tuple(corner) for corner in templates[obj.name].bound_box)
        for vertex in obj.data.vertices:
            position = obj.matrix_world @ vertex.co
            cell = tuple(int(np.floor(position[i] / args.cell_size)) for i in (0, 1))
            cells[cell][obj.name].append(vertex.index)
            rotation = Euler(obj.data.attributes["ef_rotation"].data[vertex.index].vector, 'XYZ')
            scale = obj.data.attributes["ef_scale"].data[vertex.index].vector
            matrix = obj.matrix_world @ Matrix.LocRotScale(vertex.co, rotation, scale)
            point_references.append((obj.name, vertex.index))
            instance_boxes.append(world_box(matrix, corners))
    instance_low = np.array([low for low, _ in instance_boxes], dtype=np.float64)
    instance_high = np.array([high for _, high in instance_boxes], dtype=np.float64)
    candidates = [cell for cell, entries in cells.items() if 30 <= sum(map(len, entries.values())) <= args.max_instances
                  and sum(len(templates[name].data.polygons) * len(indices) for name, indices in entries.items()) <= 1500000]
    if args.cell is not None:
        candidates = [cell for cell in candidates if cell == tuple(args.cell)]
    random.Random(args.seed).shuffle(candidates)
    surfaces = [obj for obj in original.objects if obj.type == "MESH"
                and ("unity_tile" in obj or "projected_decal_entity_id" in obj
                     or ("entity_id" in obj and obj.get("source_asset")
                         and "instance_count" not in obj and not obj.hide_render))]
    boxes = {obj.name: world_box(obj.matrix_world, tuple(tuple(corner) for corner in obj.bound_box)) for obj in surfaces}
    choice = None
    minimum_image_bytes: int | None = None
    rejected: Counter[str] = Counter()
    for cell in candidates:
        low = np.array(cell, dtype=np.float64) * args.cell_size
        high = low + args.cell_size
        touching = np.flatnonzero(np.all(instance_low[:, :2] < high, axis=1)
                                  & np.all(instance_high[:, :2] >= low, axis=1))
        entries: dict[str, list[int]] = defaultdict(list)
        for index in touching:
            name, point_index = point_references[index]
            entries[name].append(point_index)
        if not 30 <= len(touching) <= args.max_instances or sum(len(templates[name].data.polygons) * len(indices)
                                                     for name, indices in entries.items()) > 1500000:
            rejected['overlap_geometry_budget'] += 1
            continue
        receivers = [obj for obj in surfaces if np.all(boxes[obj.name][0][:2] < high)
                     and np.all(boxes[obj.name][1][:2] >= low) and len(obj.data.polygons)]
        if not any("unity_tile" in obj for obj in receivers):
            rejected['no_terrain'] += 1
            continue
        content_low = instance_low[touching].min(axis=0)
        content_high = instance_high[touching].max(axis=0)
        direct = [obj for obj in receivers if "entity_id" in obj and obj.get("source_asset")]
        if len(touching) + len(direct) > args.max_instances:
            rejected['direct_geometry_budget'] += 1
            continue
        if direct:
            content_low = np.minimum(content_low, np.array([boxes[obj.name][0] for obj in direct]).min(axis=0))
            content_high = np.maximum(content_high, np.array([boxes[obj.name][1] for obj in direct]).max(axis=0))
        extent = content_high - content_low
        if (min(extent[:2]) < args.cell_size * 0.5 or max(extent[:2]) > args.cell_size * 4
                or extent[2] > args.cell_size * 3 or extent[2] > max(extent[:2]) * 0.8):
            rejected['noncompact_geometry'] += 1
            continue
        terrain = [obj for obj in receivers if "unity_tile" in obj]
        terrain_area = sum(float(np.prod(np.maximum(0, np.minimum(boxes[obj.name][1][:2], high)
                                                     - np.maximum(boxes[obj.name][0][:2], low)))) for obj in terrain)
        if terrain_area < args.cell_size ** 2 * 0.75:
            rejected['insufficient_terrain_footprint'] += 1
            continue
        if content_low[2] - max(boxes[obj.name][1][2] for obj in terrain) > args.cell_size * 0.5:
            rejected['terrain_below_sample'] += 1
            continue
        sources = {templates[name] for name in entries}
        materials = {material for obj in (*receivers, *sources) for material in obj.data.materials if material}
        images = material_images(materials)
        unpacked = [image.name for image in images if image.source == "FILE" and not image.packed_file]
        if unpacked:
            raise ValueError(f"Selected scene textures are not packed: images={unpacked}")
        image_bytes = sum(image.packed_file.size for image in images if image.packed_file)
        minimum_image_bytes = image_bytes if minimum_image_bytes is None else min(minimum_image_bytes, image_bytes)
        if image_bytes <= args.max_image_mib * 1024 ** 2:
            choice = (cell, entries, receivers, sources, images, image_bytes, content_low, content_high)
            break
        rejected['packed_image_budget'] += 1
    if choice is None:
        raise ValueError(f"No populated sample fits the review bounds and budgets: candidates={len(candidates)}, rejected={dict(rejected)}, minimum_candidate_image_bytes={minimum_image_bytes}")
    cell, entries, receivers, sources, images, image_bytes, content_low, content_high = choice
    scene = bpy.data.scenes.new(args.region + "_Random_Review")
    bpy.context.window.scene = scene
    source_collection = bpy.data.collections.new("Sample_Sources")
    instance_collection = bpy.data.collections.new("Sample_Instances")
    surface_collection = bpy.data.collections.new("Sample_Terrain_And_Decals")
    for collection in (source_collection, instance_collection, surface_collection):
        scene.collection.children.link(collection)
    source_collection.hide_render = True
    for source in sources:
        source_collection.objects.link(source)
        source.hide_set(True)
    selected = []
    for name, indices in entries.items():
        obj = selected_instancer(objects[name], indices)
        instance_collection.objects.link(obj)
        selected.append(obj)
    for obj in receivers:
        surface_collection.objects.link(obj)
        obj.hide_set(False)
    bpy.context.view_layer.update()
    corners = [tuple(item.matrix_world @ Vector(corner))
               for item in bpy.context.evaluated_depsgraph_get().object_instances if item.object.type == "MESH"
               and (item.is_instance or ("entity_id" in item.object.original and item.object.original.get("source_asset")))
               for corner in item.object.bound_box]
    if not corners:
        raise ValueError("Selected sample has no evaluated mesh instances")
    points = np.array(corners, dtype=np.float64)
    center = Vector((points.min(axis=0) + points.max(axis=0)) * 0.5)
    camera_data = bpy.data.cameras.new("Sample_Camera")
    camera = bpy.data.objects.new("Sample_Camera", camera_data)
    scene.collection.objects.link(camera)
    radius = max(float(np.max(points.max(axis=0) - points.min(axis=0))), args.cell_size)
    camera.location = center + Vector((0.75, -0.95, 1.15)).normalized() * radius * 2
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera_data.type = "ORTHO"
    rotation = np.array(camera.rotation_euler.to_matrix())
    framed = (points - np.array(center)) @ rotation
    extent = framed.max(axis=0) - framed.min(axis=0)
    camera_data.ortho_scale = max(float(extent[0]), float(extent[1]) * 960 / 720) * 1.15
    camera_data.clip_end = 2000
    scene.camera = camera
    sun_data = bpy.data.lights.new("Sample_Sun", "SUN")
    sun_data.energy = 2.0
    sun_data.angle = 0.15
    sun = bpy.data.objects.new("Sample_Sun", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (0.4, -0.5, -0.3)
    scene.world = bpy.data.worlds.new("Sample_World")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.3, 0.3, 0.3, 1)
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.6
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 8
    scene.cycles.use_denoising = True
    scene.render.resolution_x = 960
    scene.render.resolution_y = 720
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(args.preview)
    scene["sample_source"] = str(args.blend)
    scene["sample_seed"] = args.seed
    scene["sample_region"] = args.region
    scene["sample_unity_bounds"] = json.dumps([cell[0] * args.cell_size, (cell[0] + 1) * args.cell_size,
                                               -(cell[1] + 1) * args.cell_size, -cell[1] * args.cell_size])
    scene["sample_selection"] = ("requested compact cell" if args.cell is not None else "random populated compact cell") + "; all intersecting instance bounds retained; bounded horizontal and vertical extents; terrain coverage; original geometry retained"
    scene["sample_content_bounds"] = json.dumps([content_low.tolist(), content_high.tolist()])
    bpy.context.view_layer.update()
    terrain_crops = crop_terrain_atlases(scene)
    terrain_normals = adapt_scene_terrain(scene)
    sample_materials = {material for obj in scene.objects if obj.type == "MESH"
                        for material in obj.data.materials if material}
    sample_images = material_images(sample_materials)
    image_bytes = sum(image.packed_file.size for image in sample_images if image.packed_file)
    if image_bytes > args.max_image_mib * 1024 ** 2:
        raise ValueError(f"Density-preserving sample exceeds image budget: bytes={image_bytes}, limit_mib={args.max_image_mib}")
    expected = {obj.name: sample_mesh_digest(obj) for obj in scene.objects if obj.type == "MESH"}
    expected_instances = sum(int(obj["instance_count"]) for obj in selected)
    direct_meshes = sum("entity_id" in obj and bool(obj.get("source_asset")) for obj in receivers)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.preview.parent.mkdir(parents=True, exist_ok=True)
    bpy.data.libraries.write(str(args.output), {scene}, path_remap="ABSOLUTE", fake_user=False, compress=True)
    bpy.ops.wm.open_mainfile(filepath=str(args.output))
    scene = bpy.context.scene
    actual = {obj.name: sample_mesh_digest(obj) for obj in scene.objects if obj.type == "MESH"}
    if actual != expected:
        raise ValueError("Reopened sample changed mesh geometry, UVs or instance transforms")
    reopened_normals = adapt_scene_terrain_mesh_normals(scene)
    if any(row["status"] != "already_adapted" for row in reopened_normals):
        raise ValueError("Reopened sample changed terrain source-normal bindings")
    actual_instances = sum(int(obj["instance_count"]) for obj in scene.objects if "instance_count" in obj)
    if actual_instances != expected_instances:
        raise ValueError("Reopened sample instance count changed")
    loaded_materials = {material for obj in scene.objects if obj.type == "MESH"
                        for material in obj.data.materials if material}
    loaded_images = material_images(loaded_materials)
    if any(not image.packed_file for image in loaded_images):
        raise ValueError("Reopened sample lost packed textures")
    image_bytes = sum(image.packed_file.size for image in loaded_images)
    if len(bpy.data.scenes) != 1:
        raise ValueError("Sample unexpectedly retained additional scenes")
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"
                area.spaces.active.shading.type = "MATERIAL"
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output), compress=True, check_existing=False)
    bpy.ops.render.render(write_still=True)
    args.audit.write_text(json.dumps({"format": "EndfieldRandomSceneSample/1", "region": args.region,
        "input": str(args.blend), "output": str(args.output), "preview": str(args.preview),
        "seed": args.seed, "unity_bounds": json.loads(scene["sample_unity_bounds"]),
        "content_bounds": json.loads(scene["sample_content_bounds"]),
        "rejected_before_selection": dict(rejected),
        "instances": actual_instances, "templates": len(sources), "terrain_and_decals": len(receivers),
        "direct_meshes": direct_meshes,
        "packed_images": len(loaded_images), "packed_image_bytes": image_bytes,
        "terrain_atlas_crops": terrain_crops,
        "terrain_normal_adaptation": terrain_normals,
        "terrain_mesh_normal_reopen_verification": reopened_normals,
        "bytes": args.output.stat().st_size, "geometry_reopen_verified": True,
        "selection_scope": scene["sample_selection"]}, indent=2), encoding="utf-8")
    print(json.dumps({"event": "random_scene_sample_verified", "region": args.region,
                      "instances": actual_instances, "bytes": args.output.stat().st_size}), flush=True)


if __name__ == "__main__":
    main()
