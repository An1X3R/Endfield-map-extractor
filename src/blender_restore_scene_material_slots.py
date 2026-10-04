"""Restore neutral submesh slots through the preserved builder import/join pipeline."""
from __future__ import annotations

import argparse
import array
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import ModuleType

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_scene_materials import create_material
from blender_hgrp_surface import configure_scene_transmission
from endfield_scene_material_contract import load_catalog, reference_key, supported_slots, surface_limitations
from endfield_scene_material_contract import is_neutral_material_name
from endfield_scene_slot_contract import SlotPlan, load_slot_plans, load_retained_mesh_slot_plans, canonical_mesh_name
from endfield_scene_slot_contract import AmbiguousSubmeshSlots, static_submesh_slots
from endfield_scene_slot_contract import RuntimeDraw, RuntimeSlotMapping, runtime_submesh_slots, load_runtime_draw_plans


class GeometryCorrespondenceError(ValueError):
    """The retained scene mesh does not match the referenced source export."""


def geometry_signature() -> str:
    digest = hashlib.sha256()
    for obj in sorted(bpy.data.objects, key=lambda item: item.name):
        digest.update(json.dumps((obj.name, [list(row) for row in obj.matrix_world])).encode())
    for mesh in sorted(bpy.data.meshes, key=lambda item: item.name):
        digest.update(mesh.name.encode())
        for data, field, kind, size in ((mesh.vertices, "co", "f", 3), (mesh.loops, "vertex_index", "i", 1)):
            values = array.array(kind, [0]) * (len(data) * size)
            data.foreach_get(field, values)
            digest.update(values.tobytes())
        for layer in mesh.uv_layers:
            digest.update(layer.name.encode())
            values = array.array("f", [0]) * (len(layer.data) * 2)
            layer.data.foreach_get("uv", values)
            digest.update(values.tobytes())
    return digest.hexdigest()


def source_slot_indices(builder: ModuleType, plan: SlotPlan, target: bpy.types.Mesh,
                        draw_variants: tuple[tuple[RuntimeDraw, ...], ...]) -> tuple[np.ndarray, RuntimeSlotMapping | None]:
    """Reuse original import and ordered Join, then require identical geometry and UVs."""
    original_meshes = set(bpy.data.meshes)
    imported = builder.import_obj(plan.mesh_file)
    temporary = []
    source = None
    runtime_mapping = None
    try:
        try:
            references = tuple(f"{source}:{identifier}" for source, identifier in plan.material_keys)
            if draw_variants:
                mappings = {runtime_submesh_slots(len(imported), references, draws) for draws in draw_variants}
                if len(mappings) != 1:
                    raise AmbiguousSubmeshSlots("Native instance variants disagree on selected submesh roles")
                runtime_mapping = next(iter(mappings))
                slots = runtime_mapping
            else:
                slots = static_submesh_slots(len(imported), references)
        except AmbiguousSubmeshSlots as error:
            raise GeometryCorrespondenceError(str(error)) from error
        for index, obj in enumerate(imported):
            material = bpy.data.materials.new("SLOT_PROBE_" + str(index))
            temporary.append(material)
            obj.data.materials.clear()
            obj.data.materials.append(material)
        source = builder.join_submeshes(imported)
        mesh = source.data
        if (len(mesh.vertices), len(mesh.loops), len(mesh.polygons)) != (len(target.vertices), len(target.loops), len(target.polygons)):
            raise GeometryCorrespondenceError("Source vertex, loop or polygon counts differ")
        for left, right, field, size, dtype in ((mesh.vertices, target.vertices, "co", 3, np.float32),
                                               (mesh.loops, target.loops, "vertex_index", 1, np.int32)):
            a, b = np.empty(len(left) * size, dtype=dtype), np.empty(len(right) * size, dtype=dtype)
            left.foreach_get(field, a)
            right.foreach_get(field, b)
            if not np.allclose(a, b, rtol=0, atol=0.00001):
                raise GeometryCorrespondenceError(f"Source geometry differs: field={field}")
        if len(mesh.uv_layers) != len(target.uv_layers):
            raise GeometryCorrespondenceError("Source UV layer count differs")
        for left, right in zip(mesh.uv_layers, target.uv_layers, strict=True):
            a, b = np.empty(len(left.data) * 2, dtype=np.float32), np.empty(len(right.data) * 2, dtype=np.float32)
            left.data.foreach_get("uv", a)
            right.data.foreach_get("uv", b)
            if not np.allclose(a, b, rtol=0, atol=0.00001):
                raise GeometryCorrespondenceError("Source UV coordinates differ")
        # The legacy builder uses unique temporary slots to survive Blender Join deduplication.
        pointers = {material.as_pointer(): index for index, material in enumerate(temporary)}
        mapping = np.array([slots.indices[pointers[m.as_pointer()]] for m in mesh.materials], dtype=np.int32)
        indices = np.empty(len(mesh.polygons), dtype=np.int32)
        mesh.polygons.foreach_get("material_index", indices)
        return mapping[indices], runtime_mapping
    finally:
        for obj in ([source] if source is not None else imported):
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in set(bpy.data.meshes) - original_meshes:
            if mesh.users:
                raise RuntimeError(f"Temporary imported mesh is still in use: {mesh.name}")
            bpy.data.meshes.remove(mesh)
        for material in temporary:
            bpy.data.materials.remove(material)


def obj_group_slot_indices(plan: SlotPlan, target: bpy.types.Mesh,
                           draw_variants: tuple[tuple[RuntimeDraw, ...], ...]) -> tuple[np.ndarray, RuntimeSlotMapping | None, str]:
    """Match original OBJ global indices before assigning its verified face groups."""
    vertices: list[tuple[float, float, float]] = []
    texture_coordinates: list[tuple[float, float]] = []
    faces: list[tuple[tuple[int, int], ...]] = []
    groups: list[int] = []
    group_name = ""
    with plan.mesh_file.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.startswith("v "):
                parts = line.split()
                if len(parts) < 4:
                    raise ValueError(f"Invalid OBJ vertex: path={plan.mesh_file}, line={line_number}")
                vertices.append(tuple(float(value) for value in parts[1:4]))
            elif line.startswith("vt "):
                parts = line.split()
                if len(parts) < 3:
                    raise GeometryCorrespondenceError(f"OBJ has no two-component UV contract: path={plan.mesh_file}, line={line_number}")
                texture_coordinates.append(tuple(float(value) for value in parts[1:3]))
            elif line.startswith("g "):
                group_name = line.strip()[2:]
            elif line.startswith("f "):
                match = re.fullmatch(re.escape(plan.mesh_name) + r"_(\d+)", group_name, flags=re.I)
                if match is None:
                    raise GeometryCorrespondenceError(f"OBJ face lacks an exact numbered source group: path={plan.mesh_file}, line={line_number}, group={group_name}")
                face = []
                for token in line.split()[1:]:
                    parts = token.split("/")
                    if not parts[0].lstrip("-").isdigit() or (len(parts) > 1 and parts[1] and not parts[1].lstrip("-").isdigit()):
                        raise ValueError(f"Invalid OBJ face index syntax: path={plan.mesh_file}, line={line_number}, token={token}")
                    vertex_number = int(parts[0])
                    uv_number = int(parts[1]) if len(parts) > 1 and parts[1] else None
                    if vertex_number == 0 or uv_number == 0:
                        raise ValueError(f"OBJ face indices are one-based: path={plan.mesh_file}, line={line_number}, token={token}")
                    if uv_number is None or vertex_number < 0 or uv_number < 0:
                        raise GeometryCorrespondenceError(f"OBJ face requires positive vertex and UV indices: path={plan.mesh_file}, line={line_number}")
                    vertex, uv = vertex_number-1, uv_number-1
                    if vertex >= len(vertices) or uv >= len(texture_coordinates):
                        raise ValueError(f"OBJ face index exceeds preceding data: path={plan.mesh_file}, line={line_number}")
                    face.append((vertex, uv))
                if len(face) < 3:
                    raise ValueError(f"OBJ face has fewer than three corners: path={plan.mesh_file}, line={line_number}")
                faces.append(tuple(face))
                groups.append(int(match.group(1)))
    if not vertices or not faces or not texture_coordinates:
        raise GeometryCorrespondenceError(f"OBJ lacks the global face-group geometry/UV contract: path={plan.mesh_file}")
    if not np.isfinite(vertices).all() or not np.isfinite(texture_coordinates).all():
        raise ValueError(f"OBJ contains nonfinite geometry or UV data: path={plan.mesh_file}")
    count = max(groups) + 1
    if sorted(set(groups)) != list(range(count)) or len(target.uv_layers) != 1:
        raise GeometryCorrespondenceError("OBJ groups or retained UV layer count differ")
    if (len(vertices), len(faces)) != (len(target.vertices), len(target.polygons)):
        raise GeometryCorrespondenceError("OBJ global vertex or face counts differ")
    source = np.asarray(vertices, dtype=np.float32)
    actual = np.empty((len(target.vertices), 3), dtype=np.float32)
    target.vertices.foreach_get("co", actual.ravel())
    axis = np.column_stack((source[:, 0], -source[:, 2], source[:, 1]))
    layouts = {"obj_original_x": axis, "obj_unmirrored_x": axis * (-1, 1, 1)}
    matching = [name for name, coordinates in layouts.items()
                if np.allclose(actual, coordinates, rtol=0, atol=0.00001)]
    if len(matching) != 1:
        raise GeometryCorrespondenceError(f"OBJ coordinate layout is not unique: matched={matching}")
    uv_layer = target.uv_layers[0]
    for polygon, face in zip(target.polygons, faces, strict=True):
        if tuple(polygon.vertices) != tuple(vertex for vertex, _ in face):
            raise GeometryCorrespondenceError(f"OBJ face vertex order differs: face={polygon.index}")
        for loop_index, (_, uv_index) in zip(polygon.loop_indices, face, strict=True):
            actual_uv = uv_layer.data[loop_index].uv
            if not np.allclose(actual_uv, texture_coordinates[uv_index], rtol=0, atol=0.00001):
                raise GeometryCorrespondenceError(f"OBJ UV differs: face={polygon.index}, loop={loop_index}")
    references = tuple(f"{source}:{identifier}" for source, identifier in plan.material_keys)
    try:
        if draw_variants:
            mappings = {runtime_submesh_slots(count, references, draws) for draws in draw_variants}
            if len(mappings) != 1:
                raise AmbiguousSubmeshSlots("Native instance variants disagree on selected submesh roles")
            roles = next(iter(mappings))
            slots = roles
        else:
            roles = None
            slots = static_submesh_slots(count, references)
    except AmbiguousSubmeshSlots as error:
        raise GeometryCorrespondenceError(str(error)) from error
    return np.asarray([slots.indices[group] for group in groups], dtype=np.int32), roles, matching[0]


def matched_slot_indices(builder: ModuleType, plan: SlotPlan, target: bpy.types.Mesh,
                         draw_variants: tuple[tuple[RuntimeDraw, ...], ...]) -> tuple[np.ndarray, RuntimeSlotMapping | None, str, str | None]:
    """Require every matching source contract to agree on all face roles."""
    matches: list[tuple[np.ndarray, RuntimeSlotMapping | None, str]] = []
    mismatches: list[str] = []
    try:
        indices, roles, layout = obj_group_slot_indices(plan, target, draw_variants)
        matches.append((indices, roles, layout))
    except GeometryCorrespondenceError as error:
        mismatches.append(f"OBJ global match: {error}")
    try:
        indices, roles = source_slot_indices(builder, plan, target, draw_variants)
        matches.append((indices, roles, "joined_import"))
    except GeometryCorrespondenceError as error:
        mismatches.append(f"joined import match: {error}")
    if not matches:
        raise GeometryCorrespondenceError("; ".join(mismatches))
    selected_indices, selected_roles, layout = matches[0]
    if any(not np.array_equal(selected_indices, values) or selected_roles != role
           for values, role, _ in matches[1:]):
        raise GeometryCorrespondenceError(f"Source contracts disagree on face roles: mesh={plan.mesh_name}, layouts={[row[2] for row in matches]}")
    comparison = "; ".join(mismatches) if mismatches else "OBJ global and joined import matched the same face roles"
    return selected_indices, selected_roles, layout, comparison


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("blend", "builder", "renderers", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--manifest", action="append", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if args.output.exists() or args.audit.exists():
        raise FileExistsError("Material restoration requires new output and audit paths")
    spec = importlib.util.spec_from_file_location("preserved_scene_builder", args.builder)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load preserved builder: {args.builder}")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    paths = tuple(args.manifest)
    materials, textures = load_catalog(paths)
    plans, plan_pending = load_slot_plans(args.renderers, paths)
    runtime_plans = load_runtime_draw_plans(args.renderers)
    renderer_payload = json.loads(args.renderers.read_text(encoding="utf-8-sig"))
    selected_bases = renderer_payload.get("selected_bases")
    if selected_bases is not None and (not isinstance(selected_bases, list) or not all(isinstance(value, str) for value in selected_bases)):
        raise ValueError("Renderer selected_bases requires a string array")
    retained_plans = load_retained_mesh_slot_plans(paths)
    bpy.ops.wm.open_mainfile(filepath=str(args.blend))
    before = geometry_signature()
    source_bases: dict[str, set[str]] = defaultdict(set)
    source_origins: dict[str, list[tuple[str, tuple[float, ...]]]] = defaultdict(list)
    for instancer in bpy.data.objects:
        if (instancer.type == "MESH" and instancer.get("entity_base") and "entity_id" in instancer
                and instancer.get("source_asset") and "instance_count" not in instancer):
            base = str(instancer["entity_base"])
            source_bases[instancer.name].add(base)
            source_origins[instancer.name].append((base, tuple(instancer.matrix_world.translation)))
        if instancer.type != "MESH" or not instancer.get("entity_base") or not instancer.get("instance_count"):
            continue
        for modifier in instancer.modifiers:
            if modifier.type == "NODES" and modifier.node_group:
                for node in modifier.node_group.nodes:
                    if node.type == "OBJECT_INFO" and node.inputs["Object"].default_value:
                        source_name = node.inputs["Object"].default_value.name
                        base = str(instancer["entity_base"])
                        source_bases[source_name].add(base)
                        source_origins[source_name].extend((base, tuple(instancer.matrix_world @ vertex.co))
                                                           for vertex in instancer.data.vertices)
    targets = [obj for obj in bpy.data.objects if obj.type == "MESH" and obj.get("source_asset")
               and source_bases.get(obj.name) and
               (selected_bases is None or source_bases[obj.name].intersection(selected_bases)) and
               ((len(obj.data.materials) == 1 and obj.data.materials[0]
                 and is_neutral_material_name(obj.data.materials[0].name))
                or str(obj.get("material_binding_method", "")).startswith("cached_"))]
    previously_neutral = {obj.name for obj in targets if len(obj.data.materials) == 1 and obj.data.materials[0]
                          and is_neutral_material_name(obj.data.materials[0].name)}
    cache: dict[tuple[str, int], bpy.types.Material] = {}
    applied, pending = [], []
    delivery_scene = bpy.context.scene
    import_scene = bpy.data.scenes.new("Endfield_Material_Slot_Import")
    bpy.context.window.scene = import_scene
    for index, obj in enumerate(targets):
        # Runtime mesh aliases are accepted only after the retained geometry and UVs agree.
        candidates = [plan for base in sorted(source_bases[obj.name]) for plan in plans.get(base, ())]
        matched = []
        mismatches = []
        for candidate in candidates:
            try:
                candidate_indices, roles, layout, contract_comparison = matched_slot_indices(builder, candidate, obj.data,
                    runtime_plans.get((candidate.base, candidate.mesh_name, candidate.material_paths), ()))
            except GeometryCorrespondenceError as error:
                mismatches.append({"mesh": candidate.mesh_name, "detail": str(error)})
                continue
            matched.append((candidate, candidate_indices, roles, layout, contract_comparison))
        if not candidates and not any(base in plan_pending for base in source_bases[obj.name]):
            for candidate in retained_plans.get(canonical_mesh_name(obj["source_asset"]), ()):
                try:
                    candidate_indices, roles, layout, contract_comparison = matched_slot_indices(builder, candidate, obj.data, ())
                except GeometryCorrespondenceError as error:
                    mismatches.append({"mesh": candidate.mesh_name, "detail": str(error)})
                    continue
                matched.append((candidate, candidate_indices, roles, layout, contract_comparison))
        signatures = {(candidate.material_keys, values.tobytes(), roles, layout)
                      for candidate, values, roles, layout, _ in matched}
        if len(signatures) != 1:
            reason = ("conflicting_geometry_matched_material_sequences" if matched else
                      "source_geometry_mismatch" if mismatches else "missing_renderer_evidence")
            pending.append({"object": obj.name, "reason": reason, "geometry_candidates": mismatches})
            continue
        plan, indices, roles, layout, contract_comparison = matched[0]
        if layout == "obj_original_x":
            coordinate_fidelity = "legacy_obj_x_orientation_unverified_materials_only"
        elif layout == "obj_unmirrored_x":
            coordinate_fidelity = "sceneprobe_unmirrored_layout_verified"
        else:
            coordinate_fidelity = "joined_import_existing_coordinate_contract"
        missing = [key for key in plan.material_keys if key not in materials]
        if missing:
            pending.append({"object": obj.name, "reason": "material_record_missing", "references": missing})
            continue
        records = [materials[key] for key in plan.material_keys]
        if any(record["Name"].casefold().startswith(("m_fx_", "m_water", "m_shadow", "m_fog", "m_volume")) for record in records):
            pending.append({"object": obj.name, "reason": "deferred_effect_water_or_helper_material"})
            continue
        try:
            slots = [supported_slots(record) for record in records]
        except ValueError as error:
            pending.append({"object": obj.name, "reason": "unsupported_surface_feature_input", "detail": str(error)})
            continue
        missing_textures = [slot["Texture"] for group in slots for slot in group if reference_key(slot["Texture"]) not in textures]
        if missing_textures:
            pending.append({"object": obj.name, "reason": "required_texture_missing", "references": missing_textures})
            continue
        assigned = []
        for key, record, textures_used in zip(plan.material_keys, records, slots, strict=True):
            if key not in cache:
                material = create_material(record, textures, True)
                material["endfield_surface_limitations"] = json.dumps(surface_limitations(record))
                material["endfield_unhandled_texture_properties"] = json.dumps([slot["Property"] for slot in record["Textures"] if slot["Texture"] and slot not in textures_used])
                material["endfield_shader_reference"] = json.dumps(record["Shader"])
                cache[key] = material
                for node in material.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image:
                        node.image.buffers_free()
            assigned.append(cache[key])
        if roles is not None and roles.undrawn_submeshes:
            undrawn = bpy.data.materials.get("Endfield_Runtime_Undrawn_Submesh")
            if undrawn is None:
                undrawn = bpy.data.materials.new("Endfield_Runtime_Undrawn_Submesh")
                undrawn.use_nodes = True
                undrawn.node_tree.nodes.clear()
                transparent = undrawn.node_tree.nodes.new("ShaderNodeBsdfTransparent")
                output = undrawn.node_tree.nodes.new("ShaderNodeOutputMaterial")
                undrawn.node_tree.links.new(transparent.outputs[0], output.inputs["Surface"])
                undrawn.diffuse_color = (0, 0, 0, 0)
                undrawn["endfield_runtime_role"] = "no_non_shadow_draw_for_submesh"
            if undrawn.get("endfield_runtime_role") != "no_non_shadow_draw_for_submesh":
                raise ValueError("Runtime undrawn material name collides with another material")
            assigned.append(undrawn)
        obj.data.materials.clear()
        for material in assigned:
            obj.data.materials.append(material)
        obj.data.polygons.foreach_set("material_index", indices)
        obj["material_binding_method"] = plan.evidence + "_geometry_uv_verified"
        obj["material_binding_coordinate_layout"] = layout
        obj["material_binding_coordinate_fidelity"] = coordinate_fidelity
        obj["material_source_references"] = json.dumps(plan.material_keys)
        if plan.material_paths:
            obj["ecs_material_paths"] = json.dumps(plan.material_paths)
        if roles is not None:
            obj["runtime_submesh_roles"] = json.dumps(roles._asdict())
        applied.append({"object": obj.name, "base": plan.base, "mesh_file": str(plan.mesh_file),
                        "entity_bases": sorted(source_bases[obj.name]), "previously_neutral": obj.name in previously_neutral,
                        "material_references": plan.material_keys, "material_names": [m.name for m in assigned], "evidence": plan.evidence,
                        "coordinate_layout": layout, "coordinate_fidelity": coordinate_fidelity,
                        "source_contract_comparison": contract_comparison,
                        "runtime_submesh_roles": roles._asdict() if roles is not None else None,
                        "face_counts": dict(Counter(int(v) for v in indices))})
        if len(applied) % 25 == 0:
            print(json.dumps({"event": "material_templates_restored", "applied": len(applied), "visited": index + 1, "total": len(targets)}), flush=True)
    bpy.context.window.scene = delivery_scene
    bpy.data.scenes.remove(import_scene)
    for row in pending:
        row["entity_bases"] = sorted(source_bases[row["object"]])
        row["instance_origins"] = source_origins[row["object"]]
        if row["object"] not in previously_neutral:
            obj = bpy.data.objects[row["object"]]
            neutral = bpy.data.materials.get("Geometry_Only_Neutral")
            if neutral is None:
                raise ValueError("Existing neutral material is required to withdraw unverified cached bindings")
            obj.data.materials.clear()
            obj.data.materials.append(neutral)
            obj.data.polygons.foreach_set("material_index", np.zeros(len(obj.data.polygons), dtype=np.int32))
            obj["material_binding_method"] = "pending_current_renderer_revalidation"
            row["previous_cached_binding_withdrawn"] = True
    if geometry_signature() != before:
        raise ValueError("Material restoration altered retained geometry, UVs or object transforms")
    bpy.context.scene["surface_material_status"] = "current_renderer_submesh_slots_restored_partial_static_shading"
    bpy.context.scene["surface_material_restored_templates"] = bpy.context.scene.get("surface_material_restored_templates", 0) + sum(row["previously_neutral"] for row in applied)
    bpy.context.scene["surface_material_pending_templates"] = len(pending)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    if any(material.get("endfield_refraction_adapter") for material in bpy.data.materials):
        configure_scene_transmission(bpy.context.scene)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output), check_existing=False, compress=True)
    bpy.ops.wm.open_mainfile(filepath=str(args.output))
    if geometry_signature() != before:
        raise ValueError("Reopened material restoration changed retained geometry")
    for row in applied:
        obj = bpy.data.objects[row["object"]]
        if [m.name for m in obj.data.materials] != row["material_names"]:
            raise ValueError(f"Reopened material slot order differs: object={obj.name}")
        actual = Counter(p.material_index for p in obj.data.polygons)
        if dict(actual) != row["face_counts"]:
            raise ValueError(f"Reopened submesh face counts differ: object={obj.name}")
    args.audit.write_text(json.dumps({"format": "EndfieldSceneSlotRestoration/1", "input": str(args.blend), "output": str(args.output),
        "geometry_uv_transform_sha256": before, "reopen_verified": True, "target_templates": len(targets),
        "material_count": len(cache), "applied": applied, "pending": pending,
        "pending_counts": dict(Counter(row["reason"] for row in pending)),
        "remaining_scope": ["terrain", "projected_receiver_material_refresh", "unhandled_shader_features"]}, indent=2), encoding="utf-8")
    print(json.dumps({"event": "scene_material_restoration_verified", "applied": len(applied), "pending": len(pending)}), flush=True)


if __name__ == "__main__":
    main()
