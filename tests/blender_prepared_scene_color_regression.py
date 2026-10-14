"""Exercise the real prepared-scene entrypoint with source-backed refraction draws."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import runpy
import sys

import bpy


parser = argparse.ArgumentParser()
parser.add_argument("--plan", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--repository-src", type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
args.output.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(args.repository_src))
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_contract import load_catalog, reference_key
from blender_scene_vfxrefract import verify_scene_vfxrefract, refresh_vfxrefract_scene_color

plan = load_scene_export_plan(args.plan)
settings = plan["scene_color_snapshot"]
materials, _ = load_catalog(tuple(Path(p) for p in plan["scene_manifests"]))
expected_draws = tuple(row["placement_key"] for row in plan["direct_effect_renderers"]
    if materials[reference_key(row["material_asset"])]["Shader"].get("Name") == "HGRP/Effect/VFXRefract")
if not expected_draws:
    raise ValueError("The integration fixture must contain actual planned VFXRefract draws")
blend = args.output / "scene.blend"
audit_path = args.output / "scene_audit.json"
sys.argv = ["blender", "--", "--plan", str(args.plan), "--output", str(blend), "--audit", str(audit_path)]
runpy.run_path(str(args.repository_src / "blender_export_prepared_scene.py"), run_name="__main__")
audit = json.loads(audit_path.read_text(encoding="utf-8"))
scene = bpy.context.scene
snapshot = audit["scene_color_snapshot"]
verify_scene_vfxrefract(scene, snapshot)
if not audit["reopen_verified"] or snapshot["status"] != "offline_camera_snapshot_refraction":
    raise AssertionError("Normal prepared export did not save/reopen completed snapshot refraction")
targets = tuple(scene.objects[name] for name in snapshot["receiver_objects"])
if set(expected_draws) != {obj["endfield_fx_placement_key"] for obj in targets}:
    raise AssertionError("Prepared export lost or added source refraction placements")
if scene.render.engine != settings["engine"]:
    raise AssertionError("Prepared export overwrote the explicit renderer")
if [scene.render.resolution_x, scene.render.resolution_y] != settings["resolution"]:
    raise AssertionError("Prepared export changed the explicit snapshot resolution")
if abs(scene.camera.data.lens - settings["lens_mm"]) > 1e-5:
    raise AssertionError("Prepared export changed the caller's camera lens")
if max(abs(scene.camera.matrix_world[r][c] - settings["camera_matrix_world"][r * 4 + c])
       for r in range(4) for c in range(4)) > 1e-4:
    raise AssertionError("Prepared export changed the caller's camera pose")
if any(row.get("material_status") == "awaiting_host_scene_color" for row in audit["effects"]):
    raise AssertionError("Audit retained temporary material status after finalization")
for obj in targets:
    material = obj.material_slots[0].material
    if abs(material.node_tree.nodes["EF FX Source Time Seconds"].outputs[0].default_value - settings["time_seconds"]) > 1e-5:
        raise AssertionError("Refraction shader time differs from the export request")
scene.render.filepath = str(args.output / "reopened_render.png")
bpy.ops.render.render(write_still=True)
render = Path(scene.render.filepath)
if not render.is_file() or render.stat().st_size == 0:
    raise AssertionError("Reopened prepared scene did not render")
scene.camera.data.type = "ORTHO"
try:
    verify_scene_vfxrefract(scene, snapshot)
except ValueError:
    pass
else:
    raise AssertionError("Changed camera projection was accepted as the original snapshot")
scene.camera.data.type = "PERSP"
scene.render.resolution_x += 1
try:
    verify_scene_vfxrefract(scene, snapshot)
except ValueError:
    pass
else:
    raise AssertionError("Changed output dimensions were accepted as the original snapshot")
scene.render.resolution_x -= 1
scene.camera.data.lens *= .99
refreshed = refresh_vfxrefract_scene_color(scene, scene.camera, targets, args.output / "camera_refresh.exr")
updated = json.loads(scene["endfield_scene_color_snapshot"])
if updated["snapshot_metadata"] != refreshed["endfield_host_scene_color"] or updated["snapshot_metadata"] == snapshot["snapshot_metadata"]:
    raise AssertionError("Refreshing a moved camera left stale scene metadata")
verify_scene_vfxrefract(scene, updated)
(args.output / "verification.json").write_text(json.dumps({
    "status": "passed", "plan": str(args.plan), "blend": str(blend),
    "plan_sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest(),
    "blend_sha256": hashlib.sha256(blend.read_bytes()).hexdigest(),
    "planned_receivers": list(expected_draws), "engine": scene.render.engine,
    "snapshot": snapshot, "render": str(render),
    "camera_projection_and_resolution_changes_rejected": True,
    "camera_refresh_scene_metadata_verified": True,
    "scope": "Actual planner rows through the normal prepared builder, save/reopen and render. Camera-bound host reconstruction, not native live buffers."
}, indent=2), encoding="utf-8")
