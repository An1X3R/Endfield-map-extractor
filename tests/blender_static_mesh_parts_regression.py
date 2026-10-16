"""Verify current-source static mesh parts through Blender save, reopen and render."""
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
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--entity-id", type=int, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    plan = load_scene_export_plan(args.plan)
    rows = [row for row in plan["instances"] if "mesh_part_key" in row]
    selected = [row for row in rows if row["entity_id"] == args.entity_id]
    if len(selected) < 2:
        raise ValueError(f"Expected a multipart source entity: entity_id={args.entity_id}")
    materials, textures = load_plan_material_catalog(plan)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    collection = bpy.data.collections.new("Source Static Mesh Parts")
    scene.collection.children.link(collection)
    build_instances(rows, collection, materials, textures)
    bpy.context.view_layer.update()
    objects = {obj["endfield_static_mesh_part_key"]: obj for obj in collection.objects}
    if len(objects) != len(rows):
        raise ValueError("Saved object candidates do not preserve unique static mesh parts")
    for row in rows:
        obj = objects[row["mesh_part_key"]]
        if (obj.type != "MESH" or not obj.data.polygons or not obj.data.materials
                or obj.get("entity_id") != str(row["entity_id"])
                or "endfield_dynamic_geometry_source_key" in obj):
            raise ValueError(f"Static mesh part lost geometry, material or identity: part={row['mesh_part_key']}")
        evidence = json.loads(obj["endfield_static_mesh_part_source_json"])
        if evidence["source_draw_indices"] != row["source_draw_indices"]:
            raise ValueError(f"Static part lost source draw evidence: part={row['mesh_part_key']}")
    points = [objects[row["mesh_part_key"]].matrix_world @ Vector(corner)
              for row in selected for corner in objects[row["mesh_part_key"]].bound_box]
    lower = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    upper = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    center = (lower + upper) * .5
    span = max(upper - lower)
    camera = bpy.data.objects.new("Source Tree Review", bpy.data.cameras.new("Source Tree Review"))
    scene.collection.objects.link(camera)
    camera.location = center + Vector((span, -span * 1.7, span * .7))
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = span * 1.3
    camera.data.clip_end = span * 20
    scene.camera = camera
    sun = bpy.data.objects.new("Review Sun", bpy.data.lights.new("Review Sun", "SUN"))
    scene.collection.objects.link(sun)
    sun.data.energy = 2.5
    sun.rotation_euler = (.5, -.5, -.5)
    world = bpy.data.worlds.new("Review World")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (.25, .3, .35, 1.)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = .5
    scene.world = world
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.eevee.taa_render_samples = 32
    scene.cycles.samples = 32
    scene.render.resolution_x = scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    for image in bpy.data.images:
        if image.source == "FILE" and image.users and not image.packed_file:
            image.pack()
    args.output.mkdir(parents=True, exist_ok=False)
    blend = args.output / "static_mesh_parts.blend"
    bpy.context.view_layer.update()
    expected = scene_signature()
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    if scene_signature() != expected:
        raise ValueError("Source static mesh parts changed on save/reopen")
    if any(image.source == "FILE" and image.users and not image.packed_file for image in bpy.data.images):
        raise ValueError("Static mesh part sample contains an unpacked source texture")
    scene = bpy.context.scene
    for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
        scene.render.engine = engine
        scene.render.filepath = str(args.output / f"{engine.lower()}.png")
        bpy.ops.render.render(write_still=True)
    (args.output / "verification.json").write_text(json.dumps({
        "source_plan": str(args.plan), "sample_entity_id": args.entity_id,
        "source_entities": len({row["entity_id"] for row in rows}), "mesh_parts": len(rows),
        "source_part_keys": sorted(objects), "reopen_verified": True,
        "packed_source_images": True, "engines": ["EEVEE", "Cycles"],
        "view_boundary": "One source entity framed under diagnostic neutral light; source shaders and world placements preserved",
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
