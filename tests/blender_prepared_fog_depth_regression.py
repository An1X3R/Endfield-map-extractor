"""Verify a real prepared-scene depth export after reopen and render both engines."""
import argparse
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_fog_depth import apply_scene_fog_depth, DEPTH_GAP_ATTRIBUTE
from blender_export_prepared_scene import scene_signature

parser = argparse.ArgumentParser()
for name in ("source", "audit", "output"):
    parser.add_argument("--" + name, type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
args.output.mkdir(parents=True, exist_ok=True)
worker_audit = json.loads(args.audit.read_text(encoding="utf-8"))
audit = worker_audit["fog_depth"]
if audit["status"] != "host_camera_depth_attached" or not audit["receivers"]:
    raise AssertionError("The prepared-scene worker did not attach any source fog")
bpy.ops.wm.open_mainfile(filepath=str(args.source), load_ui=False, use_scripts=False)
scene = bpy.context.scene
camera = scene.camera
if camera.name != audit["camera"]:
    raise AssertionError("The exported camera binding changed")
receiver = bpy.data.objects[audit["receivers"][0]]
used_materials = {slot.material.name for obj in scene.objects if obj.type == "MESH"
                  for slot in obj.material_slots if slot.material is not None}
if any(row["material"] not in used_materials for row in worker_audit["material_pending"]):
    raise AssertionError("Pending resources include overridden, unused data materials")
original_signature = scene_signature()[receiver.name]
effective_material = receiver.material_slots[0].material
receiver.material_slots[0].link = "DATA"
if scene_signature()[receiver.name] == original_signature:
    raise AssertionError("Worker signature failed to detect a lost object material override")
receiver.material_slots[0].link = "OBJECT"
receiver.material_slots[0].material = effective_material
if scene_signature()[receiver.name] != original_signature:
    raise AssertionError("Restored material override did not restore the worker signature")
modifier = next(item for item in receiver.modifiers if item.type == "NODES")
group = modifier.node_group
casters = next(node for node in group.nodes if node.bl_idname == "GeometryNodeCollectionInfo").inputs["Collection"].default_value
if len(casters.all_objects) != audit["opaque_casters"]:
    raise AssertionError("The prepared-scene caster membership changed")
counts = (len(bpy.data.materials), len(bpy.data.node_groups), len(bpy.data.collections), len(receiver.modifiers))
try:
    apply_scene_fog_depth(scene, audit["subdivision_level"])
except ValueError as error:
    if "unbound source soft-blend material" not in str(error):
        raise
else:
    raise AssertionError("Duplicate scene-wide attachment was accepted")
if counts != (len(bpy.data.materials), len(bpy.data.node_groups), len(bpy.data.collections), len(receiver.modifiers)):
    raise AssertionError("Duplicate scene-wide attachment changed datablocks")
empty = bpy.data.scenes.new("NoSourceFogRegression")
if apply_scene_fog_depth(empty, 1)["status"] != "no_source_enabled_fog":
    raise AssertionError("Empty scenes should not invent fog owners")
bpy.data.scenes.remove(empty)


def verify_depth(label: str) -> dict[str, str | int | float]:
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    vertices = []
    faces = []
    for obj in casters.all_objects:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        offset = len(vertices)
        vertices.extend(evaluated.matrix_world @ vertex.co for vertex in mesh.vertices)
        faces.extend(tuple(offset + index for index in face.vertices) for face in mesh.polygons)
        evaluated.to_mesh_clear()
    tree = BVHTree.FromPolygons(vertices, faces)
    evaluated = receiver.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    attribute = mesh.attributes[DEPTH_GAP_ATTRIBUTE]
    origin = camera.matrix_world.translation
    forward = camera.matrix_world.to_3x3() @ Vector((0., 0., -1.))
    errors = []
    values = [item.value for item in attribute.data]
    if not all(math.isfinite(value) for value in values):
        raise AssertionError("Saved fog depth has nonfinite values")
    for index in range(0, len(mesh.vertices), max(1, len(mesh.vertices) // 32)):
        point = evaluated.matrix_world @ mesh.vertices[index].co
        delta = point - origin
        depth = delta.dot(forward)
        expected = 0.
        if depth > 0.:
            start = origin + delta * (camera.data.clip_start / depth)
            length = delta.length * (camera.data.clip_end - camera.data.clip_start) / depth
            hit = tree.ray_cast(start, delta.normalized(), length)[0]
            expected = (hit - point).dot(forward) if hit is not None else camera.data.clip_end - depth
        errors.append(abs(values[index] - expected))
    evaluated.to_mesh_clear()
    if max(errors) > .002:
        raise AssertionError(f"Independent world BVH mismatch: case={label}, max_error={max(errors)}")
    return {"case": label, "compared_points": len(errors), "max_error": max(errors),
            "gap_min": min(values), "gap_max": max(values)}


rows = [verify_depth("prepared_saved_reopened")]
target = sum((receiver.matrix_world @ vertex.co for vertex in receiver.data.vertices), Vector()) / len(receiver.data.vertices)
front = (receiver.matrix_world.to_3x3().inverted().transposed() @ receiver.data.polygons[0].normal).normalized()
camera.location = target + front * 3. + Vector((0., 0., .6))
camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()
camera.data.lens = 14.
camera.data.clip_start = .1
camera.data.clip_end = 250.
rows.append(verify_depth("moved_camera_and_live_clip_drivers"))
scene.render.resolution_x = 512
scene.render.resolution_y = 288
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.eevee.taa_render_samples = 16
scene.cycles.samples = 16
for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
    scene.render.engine = engine
    scene.render.filepath = str(args.output / (engine + ".png"))
    bpy.ops.render.render(write_still=True)
(args.output / "receipt.json").write_text(json.dumps({
    "status": "prepared_export_reopened_depth_and_dual_engine_render_verified",
    "source": str(args.source), "source_audit": str(args.audit), "comparisons": rows,
    "duplicate_rejected_without_mutation": True, "empty_scene_unchanged": True,
    "use_scripts": False, "boundary": audit["boundary"]}, indent=2), encoding="utf-8")
print(json.dumps({"status": "passed", "comparisons": rows}))
