"""Build a selected real prefab placement through the production ordinary material path."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import bpy
from mathutils import Vector

from blender_export_prepared_scene import build_instances, scene_signature
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_handoff import load_plan_material_catalog


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--placement-key", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    args.output.mkdir(parents=True, exist_ok=False)
    plan = load_scene_export_plan(args.plan)
    rows = [row for row in plan["prefab_geometry_instances"] if row["placement_key"] == args.placement_key]
    if not rows:
        raise ValueError(f"Selected source placement has no ready geometry: placement={args.placement_key}")
    materials, textures = load_plan_material_catalog(plan)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    collection = bpy.data.collections.new("Endfield_Prefab_Geometry_Reference")
    build_instances(rows, collection, materials, textures)
    collection["endfield_source_placements_json"] = json.dumps(plan["prefab_placements"])
    bpy.context.scene.collection.children.link(collection)
    bpy.context.view_layer.update()
    objects = tuple(collection.objects)
    if len(objects) != len(rows):
        raise ValueError(f"Prefab renderer count differs: expected={len(rows)}, actual={len(objects)}")
    if any(obj.type != "MESH" or not obj.data.polygons or not obj.data.materials for obj in objects):
        raise ValueError("Prefab renderer lacks source geometry or material slots")
    points = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    center = sum(points, Vector()) / len(points)
    radius = max((point - center).length for point in points)
    scene = bpy.context.scene
    camera_data = bpy.data.cameras.new("Source Placement Review")
    camera = bpy.data.objects.new(camera_data.name, camera_data)
    scene.collection.objects.link(camera)
    camera.location = center + Vector((1., -2., 1.)) * radius * 2
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = radius * 2.4
    camera.data.clip_end = radius * 20
    scene.camera = camera
    sun_data = bpy.data.lights.new("Review Sun", "SUN")
    sun_data.energy = 3
    sun = bpy.data.objects.new(sun_data.name, sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (0.5, -0.5, -0.5)
    scene.world.color = (0.3, 0.3, 0.3)
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 512
    scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    for image in bpy.data.images:
        if image.source == "FILE" and image.users and not image.packed_file:
            image.pack()
    bpy.context.view_layer.update()
    expected = scene_signature()
    blend = args.output / "source_prefab_snapshot.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    if scene_signature() != expected:
        raise ValueError("Prefab source geometry, transform or material slots changed on reopen")
    if any(image.source == "FILE" and image.users and not image.packed_file for image in bpy.data.images):
        raise ValueError("Reopened prefab contains unpacked source textures")
    scene = bpy.context.scene
    scene.render.filepath = str(args.output / "source_snapshot.png")
    bpy.ops.render.render(write_still=True)
    (args.output / "verification.json").write_text(json.dumps({
        "plan": str(args.plan), "placement_key": args.placement_key, "blend": str(blend),
        "source_renderer_count": len(rows), "reopen_verified": True,
        "snapshot_status": "raw_source_lod_zero_runtime_branch_visibility_and_animation_unverified",
        "source_rows": rows, "object_signatures": expected,
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
