"""Verify one real Dynamic ECS multipart record through production Blender import."""
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    plan = load_scene_export_plan(args.plan)
    rows = [row for row in plan["dynamic_geometry_instances"] if row["source_key"] == args.source_key]
    if len(rows) != 2 or any("mesh_part_key" not in row for row in rows):
        raise ValueError(f"Expected one source record with two exact mesh parts: source_key={args.source_key}")
    materials, textures = load_plan_material_catalog(plan)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    collection = bpy.data.collections.new("Source Dynamic Mesh Parts")
    scene.collection.children.link(collection)
    build_instances(rows, collection, materials, textures)
    bpy.context.view_layer.update()
    objects = {obj["endfield_dynamic_mesh_part_key"]: obj for obj in collection.objects}
    if len(objects) != len(rows):
        raise ValueError("Dynamic source part identities were merged or lost")
    evidence = []
    for row in rows:
        obj = objects[row["mesh_part_key"]]
        raw_lines = Path(row["mesh_file"]).read_text(encoding="utf-8").splitlines()
        source_faces = sum(line.startswith("f ") for line in raw_lines)
        source_uv = sum(line.startswith("vt ") for line in raw_lines)
        source = json.loads(obj["endfield_dynamic_geometry_source_json"])
        if (obj.type != "MESH" or not obj.data.vertices or len(obj.data.polygons) != source_faces
                or not obj.data.materials or obj.get("endfield_dynamic_geometry_source_key") != args.source_key
                or "entity_id" in obj or source != row):
            raise ValueError(f"Dynamic part lost geometry, material, original identity or draw proof: part={row['mesh_part_key']}")
        if source_uv and (not obj.data.uv_layers or any(len(layer.data) != len(obj.data.loops) for layer in obj.data.uv_layers)):
            raise ValueError(f"Dynamic part lost source UVs: part={row['mesh_part_key']}")
        expected_refs = [ref for index, ref in enumerate(row["material_refs"]) if index in row["selected_submeshes"]]
        if json.loads(obj["endfield_material_refs"]) != row["material_refs"] or len(obj.data.materials) != len(expected_refs):
            raise ValueError(f"Dynamic part material bindings differ from the production plan: part={row['mesh_part_key']}")
        evidence.append({"mesh_part_key": row["mesh_part_key"], "source_key": row["source_key"],
                         "source_mesh": row["mesh_asset"], "source_material_refs": row["material_refs"],
                         "source_draw_indices": row["source_draw_indices"],
                         "source_draw_count": len(row["source_renderer_pairs"]),
                         "vertices": len(obj.data.vertices), "polygons": len(obj.data.polygons),
                         "source_obj_faces": source_faces, "source_obj_uvs": source_uv,
                         "uv_layers": [layer.name for layer in obj.data.uv_layers],
                         "material_slots": len(obj.data.materials)})
    points = [obj.matrix_world @ Vector(corner) for obj in objects.values() for corner in obj.bound_box]
    lower = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    upper = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    center, span = (lower + upper) * .5, max(upper - lower)
    camera = bpy.data.objects.new("Source Tree Review", bpy.data.cameras.new("Source Tree Review"))
    scene.collection.objects.link(camera)
    camera.location = center + Vector((span, -span * 1.7, span * .7))
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.type, camera.data.ortho_scale, camera.data.clip_end = "ORTHO", span * 1.3, span * 20
    scene.camera = camera
    sun = bpy.data.objects.new("Review Sun", bpy.data.lights.new("Review Sun", "SUN"))
    scene.collection.objects.link(sun)
    sun.data.energy, sun.rotation_euler = 2.5, (.5, -.5, -.5)
    world = bpy.data.worlds.new("Review World")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (.25, .3, .35, 1.)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = .5
    scene.world = world
    scene.render.engine, scene.eevee.taa_render_samples = "BLENDER_EEVEE_NEXT", 32
    scene.render.resolution_x = scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    for image in bpy.data.images:
        if image.source == "FILE" and image.users and not image.packed_file:
            image.pack()
    bpy.context.view_layer.update()
    expected = scene_signature()
    first = objects[rows[0]["mesh_part_key"]]
    original = first["endfield_dynamic_mesh_part_key"]
    first["endfield_dynamic_mesh_part_key"] = original + ":damaged"
    if scene_signature() == expected:
        raise ValueError("Dynamic source part identity is missing from save/reopen signatures")
    first["endfield_dynamic_mesh_part_key"] = original
    args.output.mkdir(parents=True, exist_ok=False)
    blend = args.output / "dynamic_mesh_parts.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    if scene_signature() != expected:
        raise ValueError("Dynamic geometry, UVs, material slots or source identity changed on save/reopen")
    if any(image.source == "FILE" and image.users and not image.packed_file for image in bpy.data.images):
        raise ValueError("Dynamic source sample contains an unpacked source texture")
    scene = bpy.context.scene
    scene.render.filepath = str(args.output / "source_tree.png")
    bpy.ops.render.render(write_still=True)
    (args.output / "verification.json").write_text(json.dumps({
        "source_plan": str(args.plan), "source_key": args.source_key, "source_records": 1, "mesh_parts": len(rows),
        "parts": evidence, "reopen_verified": True, "part_identity_signature_verified": True,
        "packed_source_images": True, "engine": "EEVEE", "blend": str(blend),
        "view_boundary": "One authored base-LOD source record under diagnostic neutral light; runtime activation and camera LOD unverified",
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
