"""Keep optional host-context limitations explicit without changing source shaders."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bmesh
import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_terrain_blend import apply_scene_terrain_contact
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_handoff import load_plan_material_catalog


def connections(material: bpy.types.Material) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(sorted((link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier)
                        for link in material.node_tree.links))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--")+1:])
    args.output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.open_mainfile(filepath=str(args.source), load_ui=False, use_scripts=False)
    records, _ = load_plan_material_catalog(load_scene_export_plan(args.plan))
    for obj in tuple(bpy.data.objects):
        if "unity_tile" not in obj and str(obj.get("entity_id")) != "1364575161":
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.orphans_purge(do_recursive=True)
    scene = bpy.context.scene
    terrain = tuple(obj for obj in scene.objects if "unity_tile" in obj)
    receiver = next(obj for obj in scene.objects if str(obj.get("entity_id")) == "1364575161")
    material = receiver.material_slots[0].material
    original = connections(material)
    for obj in terrain:
        obj.hide_render = True
    rows = [apply_scene_terrain_contact(scene, records, args.output/"absent")]
    if rows[-1]["status"] != "source_enabled_terrain_context_absent" or connections(material) != original:
        raise AssertionError("Absent terrain changed the source shader or lost its explicit status")
    for obj in terrain:
        obj.hide_render = False
    target = terrain[0]
    source_material = target.data.materials[0]
    alternate = source_material.copy()
    surface = Path(source_material["endfield_terrain_surface_source"])
    second = args.output/"second_surface.json"
    second.write_bytes(surface.read_bytes())
    alternate["endfield_terrain_surface_source"] = str(second)
    target.data.materials[0] = alternate
    rows.append(apply_scene_terrain_contact(scene, records, args.output/"multiple"))
    if (rows[-1]["status"] != "unsupported_retained_heightfield_context"
            or "one explicit surface atlas" not in rows[-1]["reason"] or connections(material) != original):
        raise AssertionError("Multiple atlases changed the source shader or lost their explicit reason")
    target.data.materials[0] = source_material
    original_mesh = target.data
    target.data = original_mesh.copy()
    mesh = bmesh.new()
    mesh.from_mesh(target.data)
    bmesh.ops.triangulate(mesh, faces=list(mesh.faces))
    mesh.to_mesh(target.data)
    mesh.free()
    rows.append(apply_scene_terrain_contact(scene, records, args.output/"nonquad"))
    if (rows[-1]["status"] != "unsupported_retained_heightfield_context"
            or "quads" not in rows[-1]["reason"] or connections(material) != original):
        raise AssertionError("Nonquad context changed the source shader or lost its explicit reason")
    target.data = original_mesh
    rows.append(apply_scene_terrain_contact(scene, records, args.output/"supported"))
    if rows[-1]["status"] != "host_heightfield_contact_attached" or "endfield_terrain_contact_context_reason" in material:
        raise AssertionError("Supported context failed to bind or retained a stale unsupported reason")
    (args.output/"terrain_contact_context_regression.json").write_text(json.dumps({"status": "passed", "cases": rows}, indent=2), encoding="utf-8")
    print(json.dumps({"status": "passed", "cases": [row["status"] for row in rows]}))


main()
