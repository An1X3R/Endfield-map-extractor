"""Reopen an actual cached source draw and exercise its explicit snapshot refresh."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from array import array
from pathlib import Path

import bpy
from mathutils import Vector


parser = argparse.ArgumentParser()
parser.add_argument("--blend", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--repository-src", type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
args.output.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(args.repository_src))
from blender_scene_vfxrefract import refresh_vfxrefract_scene_color, _source_issues

bpy.ops.wm.open_mainfile(filepath=str(args.blend), load_ui=False, use_scripts=False)
scene = bpy.context.scene
targets = tuple(obj for obj in scene.objects if obj.get("endfield_fx_source_key", "").endswith(":10:66"))
if len(targets) != 1:
    raise ValueError("Expected one actual source draw in the validation input")
target = targets[0]
material = target.material_slots[0].material
tree = material.node_tree
record = json.loads(material["endfield_fx_source_material_json"])
samplers = tuple(node for node in tree.nodes if node.get("endfield_host_scene_color_receiver"))
source_images_before = [
    {"node": node.name, "file": bpy.path.abspath(node.image.filepath),
     "path_length": len(bpy.path.abspath(node.image.filepath)), "packed": bool(node.image.packed_file)}
    for node in tree.nodes if node.type == "TEX_IMAGE" and not node.get("endfield_host_scene_color_receiver")
]
if len(samplers) != 2 or samplers[0].image != samplers[1].image:
    raise AssertionError("Saved two-sample SceneColor graph is incomplete")
if (samplers[0].image.colorspace_settings.name != "Non-Color" or not samplers[0].image.packed_file
        or tree.nodes["Source per-channel max (not additive)"].operation != "MAXIMUM"):
    raise AssertionError("Saved SceneColor linear image or per-channel max was lost")
for label, value in (("Primary source weight", 1-record["Floats"]["_RBIntensity"]),
                     ("Masked max source weight", record["Floats"]["_RBIntensity"])):
    if abs(tree.nodes[label].inputs[3].default_value-value) > 1e-7:
        raise AssertionError("RBIntensity extrapolation was changed or clamped")
if len(target.data.vertices) != 4 or len(target.data.uv_layers) != 1 or target.data.color_attributes:
    raise AssertionError("Source mesh topology/UV/COLOR absence changed")
snapshots = tuple(json.loads(node.image["endfield_host_scene_color"]) for node in samplers)
if any(target.name not in value["excluded_draws"] for value in snapshots):
    raise AssertionError("Original SceneColor snapshot did not exclude its own receiver")
clock = tree.nodes["EF FX Source Time Seconds"]
times = []
for frame in (1, 120):
    scene.frame_set(frame)
    value = clock.outputs[0].default_value
    if abs(value-frame/24) > 1e-6:
        raise AssertionError("Saved source time driver changed after script-disabled reopen")
    times.append(value)
bad = copy.deepcopy(record)
bad["Floats"]["_UseRGBOffset"] = 1
if "_UseRGBOffset" not in _source_issues(bad, 0):
    raise AssertionError("Unsupported RGB three-sample mode was admitted")
bad["Floats"]["_UseRGBOffset"] = 0
bad["Colors"]["_RefractUVSpeed"][2] = 1
if "_RefractUVSpeed_custom1_not_available" not in _source_issues(bad, 0):
    raise AssertionError("Missing Custom1 input was silently accepted")

# A separately labeled lighting-independent host fixture makes refraction visible.
camera = scene.camera
center = sum((target.matrix_world @ Vector(corner) for corner in target.bound_box), Vector()) / 8
for obj in tuple(scene.objects):
    if obj not in (target, camera):
        bpy.data.objects.remove(obj, do_unlink=True)
behind = camera.matrix_world @ Vector((0., 0., -39.))
bpy.ops.mesh.primitive_plane_add(size=10, location=behind)
backplate = bpy.context.object
backplate.name = "HOST_VALIDATION_CHECKER_BACKDROP_NOT_GAME_GEOMETRY"
backplate.rotation_euler = camera.rotation_euler
checker_material = bpy.data.materials.new("Actual host checker radiance fixture")
checker_material.use_nodes = True
nodes = checker_material.node_tree.nodes
nodes.clear()
uv = nodes.new("ShaderNodeTexCoord")
checker = nodes.new("ShaderNodeTexChecker")
checker.inputs["Color1"].default_value = (.03, .15, .75, 1.)
checker.inputs["Color2"].default_value = (.8, .15, .04, 1.)
checker.inputs["Scale"].default_value = 160.
checker_material.node_tree.links.new(uv.outputs["Generated"], checker.inputs["Vector"])
emission = nodes.new("ShaderNodeEmission")
checker_material.node_tree.links.new(checker.outputs["Color"], emission.inputs["Color"])
out = nodes.new("ShaderNodeOutputMaterial")
checker_material.node_tree.links.new(emission.outputs[0], out.inputs["Surface"])
backplate.data.materials.append(checker_material)
scene.world = bpy.data.worlds.new("Host checker fixture World")
scene.world.use_nodes = True
scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.
scene.cycles.samples = 16
scene.cycles.use_denoising = False
renders = []
background_refresh_checks = []
for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
    scene.render.engine = engine
    checker.inputs["Color1"].default_value = (.03, .15, .75, 1.)
    for frame in (1, 120):
        scene.frame_set(frame)
        previous = (scene.camera, scene.render.filepath, scene.render.image_settings.file_format,
                    scene.render.image_settings.color_mode, scene.render.image_settings.color_depth, target.hide_render)
        image = refresh_vfxrefract_scene_color(scene, camera, (target,), args.output/f"{engine}_{frame:03d}_snapshot.exr")
        restored = (scene.camera, scene.render.filepath, scene.render.image_settings.file_format,
                    scene.render.image_settings.color_mode, scene.render.image_settings.color_depth, target.hide_render)
        if previous != restored:
            raise AssertionError("SceneColor refresh changed caller render settings or receiver visibility")
        if any(node.image != image for node in samplers):
            raise AssertionError("Snapshot refresh did not update both source samples")
        if any(node.image.packed_file is None for node in tree.nodes if node.type == "TEX_IMAGE"):
            raise AssertionError("Refresh left a source texture dependent on an external engine file read")
        target.hide_render = True
        scene.render.filepath = str(args.output/f"{engine}_{frame:03d}_background.png")
        bpy.ops.render.render(write_still=True)
        target.hide_render = False
        scene.render.filepath = str(args.output/f"{engine}_{frame:03d}_refraction.png")
        bpy.ops.render.render(write_still=True)
        renders.append({"frame": frame, "engine": engine, "time": clock.outputs[0].default_value,
                        "snapshot": json.loads(image["endfield_host_scene_color"]), "image": scene.render.filepath})
    old_snapshot = image
    old_snapshot_sha256 = json.loads(old_snapshot["endfield_host_scene_color"])["sha256"]
    old_time = clock.outputs[0].default_value
    checker.inputs["Color1"].default_value = (.05, .6, .03, 1.)
    image = refresh_vfxrefract_scene_color(scene, camera, (target,),
                                          args.output/f"{engine}_120_snapshot.exr")
    metadata = json.loads(image["endfield_host_scene_color"])
    if image == old_snapshot or metadata["sha256"] == old_snapshot_sha256:
        raise AssertionError("Overwriting the same snapshot path reused the old background")
    if clock.outputs[0].default_value != old_time or scene.frame_current != 120:
        raise AssertionError("Background-only refresh changed the source animation clock")
    if any(node.image != image for node in samplers):
        raise AssertionError("Background-only refresh did not reach both actual samples")
    changed_file = args.output/f"{engine}_120_background_changed.png"
    scene.render.filepath = str(changed_file)
    bpy.ops.render.render(write_still=True)
    compared = [bpy.data.images.load(str(path), check_existing=False) for path in
                (args.output/f"{engine}_120_refraction.png", changed_file)]
    values = [array('f', [0.]) * len(item.pixels) for item in compared]
    for item, pixels in zip(compared, values, strict=True):
        item.pixels.foreach_get(pixels)
    differences = [abs(left-right) for left,right in zip(*values, strict=True)]
    changed = sum(value != 0 for value in differences)
    if changed == 0:
        raise AssertionError(f"{engine} reused old pixels after changing the background at a fixed frame")
    background_refresh_checks.append({"engine": engine, "frame": 120, "source_time": old_time,
                                      "same_snapshot_path_overwritten": True,
                                      "old_snapshot_sha256": old_snapshot_sha256,
                                      "new_snapshot": metadata, "changed_components": changed,
                                      "max_abs_component": max(differences), "image": str(changed_file)})
    for item in compared:
        bpy.data.images.remove(item)
pixel_differences = {}
for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
    images = [bpy.data.images.load(str(args.output/f"{engine}_{frame:03d}_refraction.png"), check_existing=False)
              for frame in (1, 120)]
    pixels = [array('f', [0.]) * len(image.pixels) for image in images]
    for image, values in zip(images, pixels, strict=True):
        image.pixels.foreach_get(values)
    differences = [abs(left-right) for left, right in zip(*pixels, strict=True)]
    pixel_differences[engine] = {"changed_components": sum(value != 0 for value in differences),
                                 "max_abs_component": max(differences)}
    for image in images:
        bpy.data.images.remove(image)
for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
    if pixel_differences[engine]["changed_components"] == 0:
        raise AssertionError(f"{engine} source time changed without changing rendered pixels")
for node in tree.nodes:
    if node.type == "TEX_IMAGE" and node.image is not None and node.image.packed_file is None:
        node.image.pack()
bpy.data.orphans_purge(do_recursive=True)
saved = args.output / "source_draw_host_checker.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(saved))
bpy.ops.wm.open_mainfile(filepath=str(saved), load_ui=False, use_scripts=False)
target = next(obj for obj in bpy.context.scene.objects if obj.get("endfield_fx_source_key", "").endswith(":10:66"))
material = target.material_slots[0].material
nodes = [node for node in material.node_tree.nodes if node.type == "TEX_IMAGE"]
if len(nodes) != 4 or any(node.image is None or not node.image.packed_file for node in nodes):
    raise AssertionError("Saved reference fixture is not standalone with all four image bindings packed")
report = {"status": "same_process_two_engine_animation_and_background_refresh_verified_then_saved_reopened",
          "image_binding_and_caller_state_restoration_verified": True,
          "same_process_animation": {"BLENDER_EEVEE_NEXT": "verified_by_pixel_comparison",
                                     "CYCLES": "verified_by_pixel_comparison"},
          "render_workflow": "Refresh packed source/snapshot textures then render; no save/reopen between frames",
          "source_images_before_refresh": source_images_before,
          "fixed_frame_background_refresh": background_refresh_checks,
          "same_process_pixel_differences": pixel_differences,
          "source_artifact": str(args.blend), "source_sha256": hashlib.sha256(args.blend.read_bytes()).hexdigest(),
          "original_reopen_verified": True, "two_source_samples": True, "rb_max_not_additive": True,
          "source_vertex_count": 4, "source_vertex_color_components": 0, "uv0_retained": True,
          "clock_values": times, "refresh_restores_caller_state": True,
          "unsupported_modes_rejected": ["RGB three sample", "Custom1 speed without stream"],
          "reference_background": "Explicit host checker scene physically rendered to linear EXR; not game geometry or source appearance",
          "renders": renders, "standalone_fixture": str(saved),
          "standalone_sha256": hashlib.sha256(saved.read_bytes()).hexdigest(), "bytes": saved.stat().st_size}
(args.output/"verification.json").write_text(json.dumps(report, indent=2))
print(json.dumps({"status": report["status"], "fixture": str(saved)}))
