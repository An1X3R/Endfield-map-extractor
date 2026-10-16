"""Render retained stair/ruins contact and verify host sampling against source BVH."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from numpy.typing import NDArray

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_terrain_blend import apply_scene_terrain_contact, CONTRACT
from blender_scene_terrain import atlas_pixels
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_handoff import load_plan_material_catalog


def geometry_digest(obj: bpy.types.Object) -> str:
    mesh = obj.data
    vertices = np.empty(len(mesh.vertices)*3, dtype=np.float32)
    normals = np.empty(len(mesh.loops)*3, dtype=np.float32)
    mesh.vertices.foreach_get("co", vertices)
    mesh.corner_normals.foreach_get("vector", normals)
    buffers = [vertices.tobytes(), normals.tobytes()]
    for layer in mesh.uv_layers:
        uv = np.empty(len(mesh.loops)*2, dtype=np.float32)
        layer.data.foreach_get("uv", uv)
        buffers.append(layer.name.encode()+uv.tobytes())
    return hashlib.sha256(b"".join(buffers)).hexdigest()


def aim(camera: bpy.types.Object, target: Vector, offset: tuple[float, float, float]) -> None:
    camera.location = target + Vector(offset)
    camera.rotation_euler = (target-camera.location).to_track_quat("-Z", "Y").to_euler()


def render(scene: bpy.types.Scene, path: Path) -> NDArray[np.float32]:
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(str(path), check_existing=False)
    result = atlas_pixels(image).copy()
    bpy.data.images.remove(image)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--")+1:])
    args.output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.open_mainfile(filepath=str(args.source), load_ui=False, use_scripts=False)
    records, _ = load_plan_material_catalog(load_scene_export_plan(args.plan))
    ids = {"1364575161", "1129528628"}
    for obj in tuple(bpy.data.objects):
        if "unity_tile" not in obj and str(obj.get("entity_id")) not in ids:
            bpy.data.objects.remove(obj, do_unlink=True)
    scene = bpy.context.scene
    receivers = tuple(obj for obj in scene.objects if str(obj.get("entity_id")) in ids)
    if len(receivers) != 2:
        raise AssertionError("Real source stair and ruins receivers are required")
    original = {obj.name:geometry_digest(obj) for obj in receivers}
    audit = apply_scene_terrain_contact(scene, records, args.output/"heightfield")
    if audit["status"] != "host_heightfield_contact_attached":
        raise AssertionError(audit)
    if any(geometry_digest(obj) != original[obj.name] or obj.modifiers for obj in receivers):
        raise AssertionError("Contact changed source geometry, UVs or corner normals")
    group = next(tree for tree in bpy.data.node_groups if tree.get("endfield_terrain_contact_contract") == CONTRACT)
    for material in {slot.material for obj in receivers for slot in obj.material_slots if slot.material}:
        if material.get("endfield_terrain_contact_contract") and not material.get("endfield_terrain_contact_boundary"):
            raise AssertionError("Host material lost its explicit native-equivalence boundary")
    scene.world = bpy.data.worlds.new("Contact Regression World")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (.25, .25, .25, 1)
    scene.view_settings.view_transform = "Standard"
    scene.render.use_compositing = False
    scene.render.resolution_percentage = 100
    scene.render.resolution_x, scene.render.resolution_y = 768, 576
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.cycles.samples = 16
    scene.cycles.use_denoising = True
    camera = bpy.data.objects.new("Contact Review Camera", bpy.data.cameras.new("Contact Review Camera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera.data.lens = 45
    camera.data.clip_end = 2000
    light = bpy.data.objects.new("Contact Key", bpy.data.lights.new("Contact Key", "SUN"))
    scene.collection.objects.link(light)
    light.rotation_euler = (.4, -.5, -.3)
    light.data.energy = 2
    frames = []
    for receiver in receivers:
        name = "stair" if str(receiver["entity_id"]) == "1364575161" else "ruins"
        points = [receiver.matrix_world @ Vector(point) for point in receiver.bound_box]
        target = sum(points, Vector())/8
        width = max(max(point[axis] for point in points)-min(point[axis] for point in points) for axis in (0, 1))
        receiver_materials = {slot.material for slot in receiver.material_slots if slot.material and slot.material.get("endfield_terrain_contact_contract")}
        for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
            scene.render.engine = engine
            aim(camera, target, (width*.8, -width*.85, width*.85))
            for strength, label in ((0., "before"), (1., "after")):
                for material in receiver_materials:
                    material.node_tree.nodes["Host Terrain Contact Controls"].inputs["Blend Strength"].default_value = strength
                path = args.output/f"{name}_{engine.lower()}_{label}.png"
                render(scene, path)
                frames.append(str(path))
        camera.data.type = "ORTHO"
        camera.data.ortho_scale = width*1.3
        aim(camera, target, (-width*.8, width*.7, width*1.2))
        path = args.output/f"{name}_cycles_reverse_ortho.png"
        render(scene, path)
        frames.append(str(path))
        camera.data.type = "PERSP"
    blend = args.output/"terrain_contact_actual.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    if any(geometry_digest(scene.objects[name]) != digest for name, digest in original.items()):
        raise AssertionError("Source geometry changed after save/reopen")
    group = next(tree for tree in bpy.data.node_groups if tree.get("endfield_terrain_contact_contract") == CONTRACT)
    planes = [node for node in group.nodes if node.type == "TEX_IMAGE" and node.image.get("endfield_terrain_contact_contract") == CONTRACT]
    if len(planes) != 2 or any(not node.image.packed_file for node in planes):
        raise AssertionError("Heightfield coefficients did not survive packed save/reopen")
    vertices, faces = [], []
    for obj in scene.objects:
        if obj.type == "MESH" and "unity_tile" in obj:
            offset = len(vertices)
            vertices.extend(obj.matrix_world@vertex.co for vertex in obj.data.vertices)
            obj.data.calc_loop_triangles()
            faces.extend(tuple(index+offset for index in triangle.vertices) for triangle in obj.data.loop_triangles)
    bvh = BVHTree.FromPolygons(vertices, faces, all_triangles=True)
    xmin, xmax, ymin, ymax = json.loads(group["endfield_terrain_contact_bounds"])
    codes = atlas_pixels(planes[0].image)[:, :, 3]
    valid_rows = np.argwhere(codes > .5)
    hole_rows = np.argwhere(codes < .5)
    iy, ix = map(int, valid_rows[len(valid_rows)//2])
    hy, hx = map(int, hole_rows[len(hole_rows)//2])
    for obj in scene.objects:
        if obj.type == "MESH":
            obj.hide_render = True
    bpy.ops.mesh.primitive_plane_add(size=2)
    probe = bpy.context.object
    material = bpy.data.materials.new("Exact Sampling Probe")
    material.use_nodes = True
    probe.data.materials.append(material)
    tree = material.node_tree
    entry = tree.nodes.new("ShaderNodeGroup")
    probe_group = group.copy()
    probe_group.name = "Exact Source Contact Sampling Probe"
    entry.node_tree = probe_group
    constant_position = probe_group.nodes.new("ShaderNodeCombineXYZ")
    position = next(node for node in probe_group.nodes if node.type == "NEW_GEOMETRY")
    for link in tuple(position.outputs["Position"].links):
        probe_group.links.new(constant_position.outputs[0], link.to_socket)
    for name, value in (("Contact Width", .2), ("Falloff", .77), ("Blend Strength", 1.), ("Normal Strength", 1.), ("Normal Object Offset", 0.)):
        entry.inputs[name].default_value = value
    rgb = tree.nodes.new("ShaderNodeCombineXYZ")
    difference = tree.nodes.new("ShaderNodeMath")
    difference.operation = "SUBTRACT"
    tree.links.new(entry.outputs["Height"], difference.inputs[0])
    tree.links.new(difference.outputs[0], rgb.inputs[0])
    for name, index in (("Valid", 1), ("Terrain Weight", 2)):
        tree.links.new(entry.outputs[name], rgb.inputs[index])
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(rgb.outputs[0], emission.inputs["Color"])
    tree.links.new(emission.outputs[0], tree.nodes.get("Material Output").inputs["Surface"])
    scene.render.resolution_x = scene.render.resolution_y = 8
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    camera = scene.camera
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 1
    checks = []
    for name, x, y in (("valid_lower", xmin+ix+.23, ymin+iy+.17), ("valid_upper", xmin+ix+.73, ymin+iy+.81),
                     ("hole", xmin+hx+.5, ymin+hy+.5), ("outside", xmin-.75, ymin+.5)):
        hit = bvh.ray_cast(Vector((x, y, 1000)), Vector((0, 0, -1)))[0]
        z = hit.z+.08 if hit is not None else 300.
        probe.location = (x, y, z)
        camera.location = (x, y, z+10)
        camera.rotation_euler = (0, 0, 0)
        for index, value in enumerate((x, y, z)):
            constant_position.inputs[index].default_value = value
        difference.inputs[1].default_value = hit.z if hit is not None else 0.
        for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
            scene.render.engine = engine
            pixels = render(scene, args.output/f"numeric_{name}_{engine.lower()}.exr")
            inside = xmin <= x < xmax and ymin <= y < ymax and hit is not None
            actual = pixels[4, 4, :3]
            if abs(float(actual[1])-float(inside)) > 1e-4:
                raise AssertionError(f"Coverage mismatch: case={name}, engine={engine}, value={actual}")
            height_error = abs(float(actual[0])) if inside else 0.
            weight = 1-min((abs(z-hit.z)/.2)**.77, 1) if inside else 0.
            weight_error = abs(float(actual[2])-weight)
            if height_error > 3e-4 or weight_error > .006:
                raise AssertionError(f"Retained terrain sampling mismatch: case={name}, engine={engine}, height={height_error}, weight={weight_error}")
            checks.append({"case": name, "engine": engine, "height_max_error": height_error,
                           "weight_max_error": weight_error, "pixels": 64})
    (args.output/"terrain_contact_regression.json").write_text(json.dumps({"audit": audit, "geometry_unchanged": True,
        "save_reopen_without_scripts": True, "frames": frames, "sampling_checks": checks,
        "source_materials": [{"material": m.name, "width": m["endfield_terrain_contact_source_width"]} for m in bpy.data.materials if m.get("endfield_terrain_contact_contract")]}, indent=2), encoding="utf-8")
    print(json.dumps({"status": "passed", "output": str(args.output), "sampling_checks": checks}))


main()
