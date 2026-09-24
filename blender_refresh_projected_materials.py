"""Reuse the existing projector to refresh receiver shading on already restored decals."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blender_add_projected_decals as projector
from blender_apply_verified_geometry_repairs import mesh_digest, world_box
from endfield_scene_slot_contract import canonical_mesh_name, load_retained_mesh_slot_plans


def revalidate_cached_bindings(manifests: tuple[Path, ...], audits: tuple[Path, ...]) -> list[str]:
    """Withdraw earlier cached bindings when expanded renderer evidence disagrees."""
    plans = load_retained_mesh_slot_plans(manifests)
    reviewed = {}
    for path in audits:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if payload.get("format") == "EndfieldSceneSlotRestoration/1":
            reviewed.update({row["object"]: row for row in payload["applied"]})
    invalidated = []
    for name, row in reviewed.items():
        if not row["evidence"].startswith("cached_"):
            continue
        obj = bpy.data.objects[name]
        reference = tuple(tuple(key) for key in row["material_references"])
        source_file = str(Path(row["mesh_file"])).removeprefix("\\\\?\\").casefold()
        candidates = plans.get(canonical_mesh_name(obj["source_asset"]), ())
        sequences = {plan.material_keys for plan in candidates
                     if str(plan.mesh_file).removeprefix("\\\\?\\").casefold() == source_file}
        if sequences == {reference}:
            continue
        neutral = bpy.data.materials.get("Geometry_Only_Neutral")
        if neutral is None:
            raise ValueError("Existing neutral material is required to withdraw ambiguous bindings")
        obj.data.materials.clear()
        obj.data.materials.append(neutral)
        obj.data.polygons.foreach_set("material_index", np.zeros(len(obj.data.polygons), dtype=np.int32))
        obj["material_binding_method"] = "pending_expanded_cache_renderer_disagreement"
        invalidated.append(name)
    return invalidated


def changed_receiver_names(paths: tuple[Path, ...]) -> set[str]:
    """Select receivers from verified slot-restoration and exact-material audits."""
    names: set[str] = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if payload.get("reopen_verified") is not True:
            raise ValueError(f"Material audit was not reopened and verified: {path}")
        if payload.get("format") == "EndfieldSceneSlotRestoration/1":
            names.update(row["object"] for row in payload["applied"])
            names.update(row["object"] for row in payload["pending"] if row.get("previous_cached_binding_withdrawn"))
        elif payload.get("format") == "EndfieldExactSceneMaterialAudit/1":
            replacements = {row["replacement"] for row in payload["applied"] if row["mesh_slots"]}
            names.update(obj.name for obj in bpy.data.objects if obj.type == "MESH" and obj.get("source_asset")
                         and any(material and material.name in replacements for material in obj.data.materials))
        else:
            raise ValueError(f"Unsupported material change audit: {path}")
    absent = names - set(bpy.data.objects.keys())
    if absent:
        raise ValueError(f"Changed receiver objects absent from scene: objects={sorted(absent)}")
    return names


def decal_geometry_signature(obj: bpy.types.Object) -> str:
    """Compare world triangles independent of receiver iteration order, at 1e-5 units."""
    mesh = obj.data
    mesh.calc_loop_triangles()
    positions = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", positions)
    triangles = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    # Projected objects are unparented. Their basis is immediately current even
    # before a dependency-graph update, so batches can be checked safely.
    transform = obj.matrix_basis if obj.parent is None and not obj.constraints else obj.matrix_world
    matrix = np.array(transform, dtype=np.float64)
    world = positions.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
    corners = np.round(world[triangles.reshape(-1, 3)], 5)
    corners[corners == 0] = 0
    ordered = np.sort(np.ascontiguousarray(corners).view("V24").reshape(-1, 3), axis=1)
    faces = np.sort(np.ascontiguousarray(ordered).view("V72").reshape(-1))
    return hashlib.sha256(faces.tobytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("blend", "decal-manifest", "materials", "prior-audit", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--bounds", type=float, nargs=4, required=True)
    parser.add_argument("--changed-material-audit", type=Path, action="append")
    parser.add_argument("--binding-manifest", type=Path, action="append")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if args.output.exists() or args.audit.exists():
        raise FileExistsError("Refreshed decal output and audit must be new")
    specs = projector.read_materials(args.materials)
    xmin, xmax, zmin, zmax = args.bounds
    decals = tuple(r for r in projector.read_decals(args.decal_manifest)
                   if xmin <= r.matrix[12] < xmax and zmin <= r.matrix[14] < zmax)
    prior = json.loads(args.prior_audit.read_text(encoding="utf-8-sig"))
    bpy.ops.wm.open_mainfile(filepath=str(args.blend))
    invalidated = []
    if args.binding_manifest:
        if not args.changed_material_audit:
            raise ValueError("Binding revalidation requires verified material change audits")
        invalidated = revalidate_cached_bindings(tuple(args.binding_manifest), tuple(args.changed_material_audit))
    objects = {int(obj["projected_decal_entity_id"]): obj for obj in bpy.data.objects if "projected_decal_entity_id" in obj}
    if not objects or set(objects) - {row.entity_id for row in decals}:
        raise ValueError("Existing decal IDs do not match the selected manifest region")
    geometry = {obj.name: mesh_digest(obj) for obj in bpy.data.objects if obj.type == "MESH" and "projected_decal_entity_id" not in obj}
    previous_decal_geometry = {key: decal_geometry_signature(obj) for key, obj in objects.items()}
    receivers, grid = projector.receiver_index()
    selected_ids = set(objects)
    if args.changed_material_audit:
        names = changed_receiver_names(tuple(args.changed_material_audit))
        names.update(invalidated)
        changed = [receiver for receiver in receivers if receiver.name in names]
        low = np.array([receiver.minimum for receiver in changed], dtype=np.float64).reshape(-1, 3)
        high = np.array([receiver.maximum for receiver in changed], dtype=np.float64).reshape(-1, 3)
        basis = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
        corners = tuple(itertools.product((-0.5, 0.5), repeat=3))
        selected_ids = set()
        for decal in decals:
            if decal.entity_id in objects:
                minimum, maximum = world_box(basis @ projector.shader_matrix(decal.matrix), corners)
                if np.any(np.all(low <= maximum, axis=1) & np.all(high >= minimum, axis=1)):
                    selected_ids.add(decal.entity_id)
        print(json.dumps({"event": "changed_receiver_decals_selected", "receivers": len(changed),
                          "decals": len(selected_ids), "total_decals": len(objects)}), flush=True)
    arrays, material_cache = {}, {}
    collection = bpy.data.collections["Endfield_Projected_Decals"]
    minimum_order = min(specs[r.material_path.casefold()].floats["_DrawOrder"] for r in decals)
    refreshed = []
    pending_geometry_checks: list[int] = []
    for decal in sorted(decals, key=lambda row: (specs[row.material_path.casefold()].floats["_DrawOrder"], row.entity_id)):
        if decal.entity_id not in selected_ids:
            continue
        spec = specs[decal.material_path.casefold()]
        if spec.floats.get("_UseWaterFlow", 0) > 0 or spec.floats.get("_UseWetness", 0) > 0:
            raise ValueError(f"Deferred water decal unexpectedly exists: entity_id={decal.entity_id}")
        old = objects[decal.entity_id]
        mesh = old.data
        bpy.data.objects.remove(old, do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        bias = prior["surface_offset"] + (spec.floats["_DrawOrder"] - minimum_order) * 0.00002
        polygons, _ = projector.project_decal(decal, spec, receivers, grid, arrays, material_cache, collection, bias)
        if polygons == 0:
            raise ValueError(f"Previously restored decal lost its receiver: entity_id={decal.entity_id}")
        refreshed.append(decal.entity_id)
        pending_geometry_checks.append(decal.entity_id)
        if len(refreshed) % 100 == 0:
            # Detect a changed receiver footprint before spending time on the
            # remaining region; the final and reopened checks remain mandatory.
            batch_objects = {int(obj["projected_decal_entity_id"]): obj for obj in collection.objects}
            for entity_id in pending_geometry_checks:
                if decal_geometry_signature(batch_objects[entity_id]) != previous_decal_geometry[entity_id]:
                    raise ValueError(f"Decal geometry changed during material refresh: entity_id={entity_id}, refreshed={len(refreshed)}")
            pending_geometry_checks.clear()
            for image in bpy.data.images:
                if image.packed_file:
                    image.buffers_free()
            print(json.dumps({"event": "decal_receiver_materials_refreshed", "count": len(refreshed), "total": len(objects)}), flush=True)
    bpy.context.view_layer.update()
    current = {int(obj["projected_decal_entity_id"]): obj for obj in collection.objects}
    for key, expected in previous_decal_geometry.items():
        if decal_geometry_signature(current[key]) != expected:
            raise ValueError(f"Decal geometry changed during material refresh: entity_id={key}")
    for name, expected in geometry.items():
        if mesh_digest(bpy.data.objects[name]) != expected:
            raise ValueError(f"Receiver geometry changed during refresh: object={name}")
    unused_materials = tuple(material for material in bpy.data.materials
                             if material.get("projected_decal_material") and material.users == 0)
    print(json.dumps({"event": "refresh_geometry_verified", "unused_projected_materials": len(unused_materials)}), flush=True)
    if unused_materials:
        bpy.data.batch_remove(ids=unused_materials)
    bpy.context.scene["projected_receiver_material_status"] = "refreshed_from_current_scene_materials"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"event": "saving_material_refreshed_scene", "output": str(args.output)}), flush=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output), check_existing=False, compress=True)
    bpy.ops.wm.open_mainfile(filepath=str(args.output))
    current = {int(obj["projected_decal_entity_id"]): obj for obj in bpy.data.objects if "projected_decal_entity_id" in obj}
    if set(current) != set(previous_decal_geometry):
        raise ValueError("Reopened decal ID set changed")
    for key, expected in previous_decal_geometry.items():
        if decal_geometry_signature(current[key]) != expected:
            raise ValueError(f"Reopened decal geometry differs: entity_id={key}")
    args.audit.write_text(json.dumps({"format": "EndfieldProjectedMaterialRefresh/1", "output": str(args.output),
        "refreshed": len(refreshed), "decal_ids": sorted(refreshed), "geometry_unchanged": True, "geometry_tolerance": 0.00001,
        "changed_material_audits": [str(path) for path in (args.changed_material_audit or [])],
        "undrawn_receiver_triangles": {str(key): int(obj["undrawn_receiver_triangle_count"])
                                       for key, obj in current.items() if obj.get("undrawn_receiver_triangle_count", 0)},
        "withdrawn_ambiguous_cached_bindings": invalidated,
        "unresolved_count": bpy.context.scene["unresolved_instance_count"], "reopen_verified": True}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
