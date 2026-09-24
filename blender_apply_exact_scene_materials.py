"""Apply a reviewed library only to existing exact material-reference slots."""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from endfield_scene_material_contract import load_slot_bindings
from blender_hgrp_surface import configure_scene_transmission


def geometry_digest() -> str:
    digest = hashlib.sha256()
    for obj in sorted(bpy.data.objects, key=lambda item: item.name):
        digest.update(json.dumps((obj.name, [list(row) for row in obj.matrix_world])).encode())
    for mesh in sorted(bpy.data.meshes, key=lambda item: item.name):
        digest.update(mesh.name.encode())
        for data, field, kind, size in ((mesh.vertices, "co", "f", 3), (mesh.loops, "vertex_index", "i", 1),
                                        (mesh.polygons, "material_index", "i", 1)):
            values = array.array(kind, [0]) * (len(data) * size)
            data.foreach_get(field, values)
            digest.update(values.tobytes())
        for layer in mesh.uv_layers:
            digest.update(layer.name.encode())
            values = array.array("f", [0]) * (len(layer.data) * 2)
            layer.data.foreach_get("uv", values)
            digest.update(values.tobytes())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("blend", "library", "bindings", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    options = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if options.output.exists() or options.audit.exists():
        raise FileExistsError("Scene material output and audit must be new paths")
    bindings = load_slot_bindings(options.bindings)
    bpy.ops.wm.open_mainfile(filepath=str(options.blend))
    before = geometry_digest()
    original = tuple(bpy.data.materials)
    with bpy.data.libraries.load(str(options.library), link=False) as (source, target):
        target.materials = source.materials
    library = {m["endfield_material_source"].casefold(): m for m in target.materials
               if m and m.get("endfield_surface_contract") in ("EndfieldStaticSurface/1", "EndfieldHGRPSurface/2")}
    if any(material.get("endfield_refraction_adapter") for material in library.values()):
        configure_scene_transmission(bpy.context.scene)
    applied = []
    pending = []
    for material in original:
        if material.get("projected_decal_material"):
            continue
        container = str(material.get("ecs_material_path", "")).replace("\\", "/").casefold()
        reference = str(material.get("endfield_material_source", "")) or bindings.get(container)
        if reference is None:
            if container:
                pending.append({"material": material.name, "container": container, "reason": "not_in_verified_library"})
            continue
        if reference.casefold() not in library:
            if reference == bindings.get(container):
                raise ValueError(f"Bound material absent from library: container={container}, reference={reference}")
            continue
        replacement = library[reference.casefold()].copy()
        for key in ("ecs_material_path", "ecs_material_id"):
            if key in material:
                replacement[key] = material[key]
        replacement["material_status"] = "exact_reference_static_surface_approximation"
        slots = 0
        for mesh in bpy.data.meshes:
            for index, current in enumerate(mesh.materials):
                if current == material:
                    mesh.materials[index] = replacement
                    slots += 1
        applied.append({"old_material": material.name, "replacement": replacement.name, "mesh_slots": slots})
    if not any(row["mesh_slots"] for row in applied):
        raise ValueError("No exact-reference mesh slots matched; scene was not saved")
    bpy.context.scene["surface_material_status"] = "partial_exact_slots_static_approximation"
    bpy.context.scene["surface_material_pending"] = "neutral merged slots, terrain, projected receiver refresh, unhandled shader features"
    if geometry_digest() != before:
        raise ValueError("Material application changed geometry, transforms, UVs or face slot indices")
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.audit.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(options.output), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(options.output))
    if geometry_digest() != before:
        raise ValueError("Reopened geometry differs after material application")
    for row in applied:
        material = bpy.data.materials[row["replacement"]]
        actual_slots = sum(current == material for mesh in bpy.data.meshes for current in mesh.materials)
        if actual_slots != row["mesh_slots"]:
            raise ValueError(f"Reopened slot assignment differs: material={material.name}, expected={row['mesh_slots']}, actual={actual_slots}")
        if row["mesh_slots"] and material.get("material_status") != "exact_reference_static_surface_approximation":
            raise ValueError("Reopened material binding lost its status")
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and (node.image is None or node.image.packed_file is None):
                raise ValueError(f"Reopened material texture is not packed: material={material.name}, node={node.name}")
    options.audit.write_text(json.dumps({"format": "EndfieldExactSceneMaterialAudit/1", "input": str(options.blend),
        "output": str(options.output), "geometry_uv_transform_sha256": before, "reopen_verified": True,
        "applied": applied, "pending_exact_slots": pending, "scope": "partial_exact_slots_only"}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
