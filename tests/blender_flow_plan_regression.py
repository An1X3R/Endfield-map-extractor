"""Compare real prepared-scene exports with and without an authored flow recipe."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_effect_flow_preview import apply_flow_presentation
from endfield_effect_presentation import read_flow_presentation


def source_geometry(scene: bpy.types.Scene) -> dict[str, str]:
    records: dict[str, str] = {}
    for obj in scene.objects:
        if obj.type != "MESH" or "endfield_flow_preview_scope" in obj:
            continue
        digest = hashlib.sha256()
        for row in obj.matrix_world:
            digest.update(struct.pack("<4f", *row))
        for vertex in obj.data.vertices:
            digest.update(struct.pack("<3f", *vertex.co))
        for polygon in obj.data.polygons:
            digest.update(struct.pack("<" + "I" * len(polygon.vertices), *polygon.vertices))
        records[obj.name] = digest.hexdigest()
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    args.output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.open_mainfile(filepath=str(args.baseline), load_ui=False, use_scripts=False)
    baseline = source_geometry(bpy.context.scene)
    original_names = set(bpy.context.scene.objects.keys())
    if not baseline:
        raise AssertionError("Real source geometry is required for this regression")
    bpy.ops.wm.open_mainfile(filepath=str(args.candidate), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    if source_geometry(scene) != baseline:
        raise AssertionError("Flow presentation changed original geometry or transforms")
    collection = bpy.data.collections["Endfield_Authored_Flow_Presentation"]
    recipe = read_flow_presentation(json.loads(collection["endfield_flow_recipe_json"]))
    authored_names = {row["name"] for row in recipe["ribbons"]}
    if set(scene.objects.keys()) != original_names | authored_names:
        raise AssertionError("Flow presentation removed source objects or added unexpected objects")
    before = (len(bpy.data.objects), len(bpy.data.materials), len(bpy.data.collections))
    try:
        apply_flow_presentation(scene, recipe)
    except ValueError as error:
        if "collide" not in str(error):
            raise
    else:
        raise AssertionError("Duplicate application must fail before mutation")
    if before != (len(bpy.data.objects), len(bpy.data.materials), len(bpy.data.collections)):
        raise AssertionError("Duplicate recipe changed Blender data")
    material = collection.objects[0].data.materials[0]
    clock = material.node_tree.nodes["EF FX Source Time Seconds"]
    times: dict[int, float] = {}
    for frame in (24, 48, 96):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        seconds = clock.outputs[0].default_value
        expected = frame * scene.render.fps_base / scene.render.fps
        if abs(seconds - expected) > 1e-5:
            raise AssertionError(f"Reopened native driver is invalid at frame={frame}: {seconds} != {expected}")
        times[frame] = seconds
    center = sum((Vector(row["center"]) for row in recipe["ribbons"]), Vector()) / len(recipe["ribbons"])
    radius = max(max(row["radii"]) for row in recipe["ribbons"])
    camera = scene.camera
    camera.location = center + Vector((radius * 1.6, -radius * 2, radius))
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.type = "PERSP"
    camera.data.lens = 36
    camera.data.clip_end = radius * 20
    scene.render.resolution_x, scene.render.resolution_y = 480, 320
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.eevee.taa_render_samples = 16
    scene.cycles.samples = 16
    renders: dict[str, list[str]] = {}
    for engine in ("BLENDER_EEVEE_NEXT", "CYCLES"):
        scene.render.engine = engine
        hashes: list[str] = []
        for frame in (24, 48):
            scene.frame_set(frame)
            path = args.output / f"{engine}_{frame}.png"
            scene.render.filepath = str(path)
            bpy.ops.render.render(write_still=True)
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        if len(set(hashes)) != 2:
            raise AssertionError(f"No rendered motion in {engine}")
        renders[engine] = hashes
    (args.output / "receipt.json").write_text(json.dumps({
        "status": "passed", "baseline": str(args.baseline), "candidate": str(args.candidate),
        "source_meshes_unchanged": len(baseline), "source_objects_preserved": len(original_names),
        "authored_ribbons": sorted(authored_names), "reopened_without_scripts": True,
        "duplicate_application_fails_without_mutation": True, "native_clock_seconds": times,
        "renders": renders, "scope": "Real prepared-scene integration; authored approximation, not native parity"
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
