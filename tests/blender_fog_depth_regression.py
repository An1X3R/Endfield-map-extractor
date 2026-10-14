"""Verify live depth attributes, source fog wiring, and save/reopen in real Blender."""
import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Euler, Matrix

parser = argparse.ArgumentParser()
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_fog_depth import attach_fog_depth, DEPTH_GAP_ATTRIBUTE

args.output.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.open_mainfile(filepath=str(args.source), load_ui=False, use_scripts=False)
template = next(m for m in bpy.data.materials
    if m.get("endfield_fx_fog_softblend_status") == "constant_one_screen_depth_unavailable")
scene = bpy.context.scene
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
camera = bpy.data.objects.new("DepthRegressionCamera", bpy.data.cameras.new("DepthRegressionCamera"))
scene.collection.objects.link(camera)
scene.camera = camera
camera.data.clip_start = 2.
camera.data.clip_end = 10.
casters = bpy.data.collections.new("ExplicitDepthCasters")
scene.collection.children.link(casters)


def plane(name: str, z: float, size: float, collection: bpy.types.Collection) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(-size, -size, z), (size, -size, z), (size, size, z), (-size, size, z)], [], [(0, 1, 2, 3)])
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def gaps(obj: bpy.types.Object) -> list[float]:
    bpy.context.view_layer.update()
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    values = [value.value for value in mesh.attributes[DEPTH_GAP_ATTRIBUTE].data]
    evaluated.to_mesh_clear()
    return values


receiver = plane("Receiver", -5., 4., scene.collection)
receiver.data.materials.append(template)
unbound = plane("UnboundSharedMaterial", -5., 1., scene.collection)
unbound.data.materials.append(template)
override = template.copy()
override["regression_override_marker"] = "object_slot"
receiver.material_slots[0].link = "OBJECT"
receiver.material_slots[0].material = override
bound_material = attach_fog_depth(camera, (receiver,), casters, 2)[0]
if unbound.material_slots[0].material != template:
    raise AssertionError("Unbound material owner was modified")
if receiver.material_slots[0].material == template:
    raise AssertionError("Bound receiver did not get an isolated material")
if receiver.material_slots[0].material.get("regression_override_marker") != "object_slot":
    raise AssertionError("Effective object material override was not preserved")
before_duplicate = (len(bpy.data.materials), len(bpy.data.node_groups), len(receiver.modifiers))
try:
    attach_fog_depth(camera, (receiver,), casters, 2)
except ValueError as error:
    if "unbound source soft-blend material" not in str(error):
        raise
else:
    raise AssertionError("Duplicate live depth attachment was accepted")
if before_duplicate != (len(bpy.data.materials), len(bpy.data.node_groups), len(receiver.modifiers)):
    raise AssertionError("Rejected duplicate attachment mutated Blender data")
wall = plane("DepthWall", -5.5, 100., casters)
near_wall = plane("BeforeNearClip", -1., 100., casters)
rows = []


def check(name: str, expected: float) -> None:
    values = gaps(receiver)
    error = max(abs(value - expected) for value in values)
    if error > 2.e-5:
        raise AssertionError(f"{name}: expected={expected}, range={min(values), max(values)}, error={error}")
    rows.append({"case": name, "vertices": len(values), "expected": expected, "max_error": error})


check("perspective_off_axis_and_near_clip", .5)
transform = Matrix.Translation((13., -9., 4.)) @ Euler((.7, -.3, .4)).to_matrix().to_4x4()
camera.matrix_world = transform
receiver.matrix_world = transform @ Matrix.Diagonal((1.7, .4, 1., 1.))
wall.matrix_world = transform
near_wall.matrix_world = transform
check("rotated_camera_nonuniform_receiver", .5)
for obj in (camera, receiver, wall, near_wall):
    obj.matrix_world = Matrix.Identity(4)
for vertex in receiver.data.vertices:
    vertex.co.z = -9.5
receiver.data.update()
for vertex in wall.data.vertices:
    vertex.co.z = -9.8
wall.data.update()
check("off_axis_far_hit", .3)
for vertex in wall.data.vertices:
    vertex.co.z = -10.3
wall.data.update()
check("beyond_far_is_miss", .5)
for vertex in receiver.data.vertices:
    vertex.co.z = -9.75
receiver.data.update()
check("miss_near_far_plane", .25)
camera.data.clip_end = 8.
for vertex in receiver.data.vertices:
    vertex.co.z = -7.75
receiver.data.update()
check("live_far_clip_driver", .25)
for vertex in wall.data.vertices:
    vertex.co.z = -7.5
wall.data.update()
check("occluder_before_fog", -.25)
for vertex in receiver.data.vertices:
    vertex.co.z = 0.
receiver.data.update()
check("camera_plane_is_finite_zero", 0.)
for vertex in receiver.data.vertices:
    vertex.co.z = -7.
receiver.data.update()
check("restored_front_fog", .5)
bpy.context.preferences.filepaths.save_version = 0
artifact = args.output / "fog_depth_regression.blend"
bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
bpy.ops.wm.save_as_mainfile(filepath=str(artifact), compress=True)
bpy.ops.wm.open_mainfile(filepath=str(artifact), load_ui=False, use_scripts=False)
scene = bpy.context.scene
receiver = bpy.data.objects["Receiver"]
camera = scene.camera
check("saved_reopened_no_scripts", .5)
camera.data.clip_end = 7.25
check("reopened_clip_driver", .25)
material = bpy.data.materials[bound_material]
factor = material.node_tree.nodes["Source Soft Depth Saturate"]
if not factor.use_clamp or factor.inputs[1].default_value != 1.:
    raise AssertionError("Source soft-distance division/clamp was lost")
(args.output / "regression.json").write_text(json.dumps({"status": "passed", "cases": rows,
    "source_soft_distance": 1., "shared_material_preserved": True, "use_scripts": False,
    "object_override_preserved": True, "duplicate_rejected_without_mutation": True,
    "artifact": str(artifact)}, indent=2), encoding="utf-8")
print(json.dumps({"status": "passed", "cases": len(rows), "artifact": str(artifact)}))
