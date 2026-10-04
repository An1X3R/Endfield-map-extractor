"""Apply source-domain detail layers to an existing bounded scene without mesh changes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_restore_scene_material_slots import geometry_signature
from blender_scene_materials import layout_shader_node_tree
from blender_scene_terrain import adapt_scene_terrain
from blender_terrain_detail import apply_scene_detail


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "detail", "output", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if args.output.exists():
        raise FileExistsError(args.output)
    bpy.ops.wm.open_mainfile(filepath=str(args.source), load_ui=False)
    before = geometry_signature()
    adapt_scene_terrain(bpy.context.scene)
    changes = apply_scene_detail(bpy.context.scene, args.detail)
    if not changes or geometry_signature() != before:
        raise AssertionError("Terrain detail reconstruction changed geometry or UVs")
    for row in changes:
        layout_shader_node_tree(bpy.data.materials[row["material"]].node_tree)
    for group in bpy.data.node_groups:
        if group.get("endfield_contract") == "HGTerrainDetail/1":
            layout_shader_node_tree(group)
    for image in bpy.data.images:
        if image.source == "FILE" and image.users and not image.packed_file:
            image.pack()
    scene = bpy.context.scene
    scene["endfield_terrain_stage"] = "Source detail layers, height blend, tint, roughness and world detail normals; smooth triplanar adaptation"
    bpy.context.preferences.filepaths.save_version = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output), compress=True, check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(args.output), load_ui=False)
    if geometry_signature() != before:
        raise AssertionError("Saved terrain detail reconstruction changed geometry or UVs")
    repeated = apply_scene_detail(bpy.context.scene, args.detail)
    if any(row["status"] != "already_adapted" for row in repeated):
        raise AssertionError("Terrain detail reconstruction is not idempotent")
    normals = adapt_scene_terrain(bpy.context.scene)
    mesh_normals = json.loads(bpy.context.scene["endfield_terrain_mesh_normal_audit"])
    if any(row["status"] != "already_adapted" for row in mesh_normals):
        raise AssertionError("Saved terrain source-normal bindings changed")
    unbound = [image.name for image in bpy.data.images if image.source == "FILE" and image.users and not image.packed_file]
    if unbound:
        raise AssertionError(f"Terrain detail scene has unpacked textures: {unbound}")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"source": str(args.source), "output": str(args.output),
        "detail": str(args.detail), "materials": changes, "base_normal_contracts": normals,
        "source_mesh_normals": mesh_normals,
        "reopened_geometry_uv_signature": before, "idempotence_verified": True,
        "all_used_file_textures_packed": True, "bytes": args.output.stat().st_size}, indent=2), encoding="utf-8")
    print(json.dumps({"event": "terrain_detail_saved_verified", "output": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
