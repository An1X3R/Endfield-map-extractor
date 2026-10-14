"""Audit one imported character FBX in Blender and save a rest-pose project."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Sequence, TypedDict

if TYPE_CHECKING:
    from character_fbx_nested_skin import NestedSkinRecovery

import bpy
from mathutils import Vector


WEIGHT_SUM_TOLERANCE = 1e-4
DEFORMATION_ROTATION_RADIANS = math.radians(5.0)
DEFORMATION_DISTANCE_TOLERANCE = 1e-7


class VectorRecord(TypedDict):
    x: float
    y: float
    z: float


class BoundsRecord(TypedDict):
    minimum: VectorRecord
    maximum: VectorRecord


class ModifierRecord(TypedDict):
    name: str
    target: str | None
    target_is_armature: bool
    show_viewport: bool
    use_vertex_groups: bool
    vertex_group_limit: str


class SkinWeightsRecord(TypedDict):
    has_weights: bool
    weight_assignment_count: int
    weighted_vertex_count: int
    unweighted_vertex_count: int
    finite: bool
    non_finite_weight_count: int
    negative_weight_count: int
    normalized: bool | None
    non_normalized_vertex_count: int
    minimum_vertex_weight_sum: float | None
    maximum_vertex_weight_sum: float | None
    binding_valid: bool | None
    missing_bone_groups: list[str]
    invalid_group_index_count: int
    armature_modifiers: list[ModifierRecord]
    weighted_group_vertex_counts: dict[str, int]


class ShapeKeyRecord(TypedDict):
    name: str
    value: float
    slider_min: float
    slider_max: float


class MaterialSlotRecord(TypedDict):
    material: str | None
    polygon_count: int


class MeshRecord(TypedDict):
    name: str
    vertex_count: int
    edge_count: int
    polygon_count: int
    world_bounds: BoundsRecord | None
    parent: str | None
    material_slots: list[MaterialSlotRecord]
    polygons_without_material_slot: int
    shape_keys: list[ShapeKeyRecord]
    skin_weights: SkinWeightsRecord


class BoneRecord(TypedDict):
    name: str
    parent: str | None
    use_deform: bool
    connected: bool
    head_world: VectorRecord
    tail_world: VectorRecord
    length: float


class ArmatureRecord(TypedDict):
    name: str
    bone_count: int
    world_bounds: BoundsRecord | None
    bones: list[BoneRecord]


class ImageRecord(TypedDict):
    name: str
    source: str
    filepath: str
    resolved_path: str | None
    resolved_file_exists: bool | None
    packed: bool
    width: int
    height: int
    channels: int


class MaterialImageNodeRecord(TypedDict):
    node: str
    image: str | None


class MaterialRecord(TypedDict):
    name: str
    use_nodes: bool
    image_texture_nodes: list[MaterialImageNodeRecord]


class DeformationRecord(TypedDict):
    passed: bool
    reason: str | None
    mesh: str | None
    armature: str | None
    bone: str | None
    local_rotation_axis: str | None
    rotation_degrees: float
    moved_vertex_count: int
    maximum_vertex_displacement: float
    rest_pose_restored: bool
    restore_maximum_displacement: float
    restore_distance_tolerance: float


class ValidationRecord(TypedDict):
    has_skinned_mesh: bool
    all_weights_finite: bool
    all_weighted_vertices_normalized: bool
    all_skin_groups_bind_to_active_armature_bones: bool
    deformation_test_passed: bool
    saved_blend_reopened: bool


class AuditDocument(TypedDict):
    format: str
    created_utc: str
    blender_version: str
    source_fbx: str
    imported_object_count: int
    imported_object_names: list[str]
    mesh_count: int
    meshes: list[MeshRecord]
    armatures: list[ArmatureRecord]
    materials: list[MaterialRecord]
    images: list[ImageRecord]
    deformation_test: DeformationRecord
    validation: ValidationRecord
    status: str
    output_blend: str
    output_blend_bytes: int
    saved_blend_reopened: bool
    warnings: list[str]
    visual_inspection_performed: bool
    animation_playback_tested: bool
    nested_source_skin_recoveries: tuple[NestedSkinRecovery, ...]


def parse_script_arguments(argv: Sequence[str]) -> tuple[Path, Path]:
    """Parse arguments after Blender's optional separator."""
    script_arguments = list(argv)
    if "--" in script_arguments:
        script_arguments = script_arguments[script_arguments.index("--") + 1:]
    else:
        script_arguments = script_arguments[1:]

    parser = argparse.ArgumentParser(description="Audit one character FBX with Blender's FBX importer.")
    parser.add_argument("--fbx", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args(script_arguments)
    return arguments.fbx, arguments.output_dir


def require_supported_blender() -> None:
    """Require Blender 4.4 or newer before using the supported importer API."""
    if bpy.app.version < (4, 4, 0):
        raise RuntimeError(f"Blender 4.4 or newer is required; found {bpy.app.version_string}.")


def enable_fbx_importer() -> None:
    """Enable the bundled FBX importer without downloading or installing add-ons."""
    if not hasattr(bpy.ops.import_scene, "fbx"):
        try:
            bpy.ops.preferences.addon_enable(module="io_scene_fbx")
        except RuntimeError as error:
            raise RuntimeError(f"Blender's bundled io_scene_fbx importer could not be enabled: {error}") from error
    if not hasattr(bpy.ops.import_scene, "fbx"):
        raise RuntimeError("Blender's bundled io_scene_fbx importer is unavailable; no installer was run.")


def clear_scene_objects() -> None:
    """Remove startup objects so the audit contains only the requested FBX."""
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def import_character_fbx(fbx_path: Path) -> tuple[list[bpy.types.Object], tuple[NestedSkinRecovery, ...]]:
    """Import with Blender's existing FBX operator and return scene objects."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if fbx_path.name.endswith('.geometry.json'):
        from character_geometry import import_character_geometry
        return import_character_geometry(fbx_path), ()
    from character_fbx_nested_skin import import_with_nested_single_cluster_skins
    try:
        result, repairs = import_with_nested_single_cluster_skins(fbx_path)
    except RuntimeError as error:
        raise RuntimeError(f"FBX import failed for {fbx_path}: {error}") from error
    if "FINISHED" not in result:
        raise RuntimeError(f"Blender FBX importer did not finish for {fbx_path}; operator result={sorted(result)}.")
    imported_objects = sorted(list(bpy.context.scene.objects), key=lambda obj: obj.name)
    if not imported_objects:
        raise RuntimeError(f"The FBX importer finished but created no scene objects: {fbx_path}.")
    return imported_objects, repairs


def vector_record(vector: Vector, label: str) -> VectorRecord:
    """Return finite XYZ coordinates suitable for strict JSON output."""
    components = tuple(float(component) for component in vector)
    if len(components) != 3 or not all(math.isfinite(component) for component in components):
        raise RuntimeError(f"Non-finite or non-3D coordinate encountered at {label}: {components}.")
    return {"x": components[0], "y": components[1], "z": components[2]}


def bounds_from_points(points: Sequence[Vector], label: str) -> BoundsRecord | None:
    """Compute a finite axis-aligned bound from world-space points."""
    if not points:
        return None
    coordinates = [vector_record(point, label) for point in points]
    return {
        "minimum": {
            "x": min(point["x"] for point in coordinates),
            "y": min(point["y"] for point in coordinates),
            "z": min(point["z"] for point in coordinates),
        },
        "maximum": {
            "x": max(point["x"] for point in coordinates),
            "y": max(point["y"] for point in coordinates),
            "z": max(point["z"] for point in coordinates),
        },
    }


def mesh_world_bounds(mesh_object: bpy.types.Object) -> BoundsRecord | None:
    """Return the source mesh vertex bound transformed to world space."""
    world_points = [mesh_object.matrix_world @ vertex.co for vertex in mesh_object.data.vertices]
    return bounds_from_points(world_points, f"mesh {mesh_object.name}")


def inspect_skin_weights(mesh_object: bpy.types.Object) -> tuple[SkinWeightsRecord, dict[str, int]]:
    """Inspect every vertex-group assignment and identify active bone bindings."""
    group_names = {group.index: group.name for group in mesh_object.vertex_groups}
    weight_assignment_count = 0
    weighted_vertex_count = 0
    unweighted_vertex_count = 0
    non_finite_weight_count = 0
    negative_weight_count = 0
    non_normalized_vertex_count = 0
    invalid_group_index_count = 0
    minimum_weight_sum: float | None = None
    maximum_weight_sum: float | None = None
    assigned_group_names: set[str] = set()
    weighted_group_vertex_counts: Counter[str] = Counter()
    all_finite = True

    for vertex in mesh_object.data.vertices:
        vertex_weights: list[float] = []
        vertex_has_assignment = False
        for assignment in vertex.groups:
            weight_assignment_count += 1
            weight = float(assignment.weight)
            group_name = group_names.get(assignment.group)
            if group_name is None:
                invalid_group_index_count += 1
            if not math.isfinite(weight):
                non_finite_weight_count += 1
                all_finite = False
                vertex_has_assignment = True
                if group_name is not None:
                    assigned_group_names.add(group_name)
                continue
            vertex_weights.append(weight)
            if weight < 0.0:
                negative_weight_count += 1
            if weight != 0.0:
                vertex_has_assignment = True
                if group_name is not None:
                    assigned_group_names.add(group_name)
            if weight > 0.0 and group_name is not None:
                weighted_group_vertex_counts[group_name] += 1

        if not vertex_has_assignment:
            unweighted_vertex_count += 1
            continue
        weighted_vertex_count += 1
        if len(vertex_weights) != len(vertex.groups):
            non_normalized_vertex_count += 1
            continue
        try:
            weight_sum = math.fsum(vertex_weights)
        except OverflowError:
            non_normalized_vertex_count += 1
            continue
        if not math.isfinite(weight_sum):
            non_normalized_vertex_count += 1
            continue
        minimum_weight_sum = weight_sum if minimum_weight_sum is None else min(minimum_weight_sum, weight_sum)
        maximum_weight_sum = weight_sum if maximum_weight_sum is None else max(maximum_weight_sum, weight_sum)
        if abs(weight_sum - 1.0) > WEIGHT_SUM_TOLERANCE:
            non_normalized_vertex_count += 1

    armature_modifiers: list[bpy.types.ArmatureModifier] = [
        modifier for modifier in mesh_object.modifiers if modifier.type == "ARMATURE"
    ]
    modifier_records: list[ModifierRecord] = []
    active_modifiers: list[bpy.types.ArmatureModifier] = []
    for modifier in armature_modifiers:
        target = modifier.object
        target_is_armature = target is not None and target.type == "ARMATURE"
        modifier_records.append({
            "name": modifier.name,
            "target": target.name if target is not None else None,
            "target_is_armature": target_is_armature,
            "show_viewport": modifier.show_viewport,
            "use_vertex_groups": modifier.use_vertex_groups,
            "vertex_group_limit": modifier.vertex_group,
        })
        if target_is_armature and modifier.show_viewport and modifier.use_vertex_groups:
            active_modifiers.append(modifier)

    missing_bone_groups: list[str] = []
    for group_name in sorted(assigned_group_names):
        has_active_bone = any(
            modifier.object is not None and group_name in modifier.object.data.bones and
            (not modifier.vertex_group or modifier.vertex_group == group_name)
            for modifier in active_modifiers
        )
        if not has_active_bone:
            missing_bone_groups.append(group_name)
    if invalid_group_index_count > 0:
        missing_bone_groups.append("<invalid vertex group index>")

    has_weights = weighted_vertex_count > 0
    normalized: bool | None = None
    binding_valid: bool | None = None
    if has_weights:
        normalized = all_finite and non_normalized_vertex_count == 0
        binding_valid = bool(active_modifiers) and not missing_bone_groups

    result: SkinWeightsRecord = {
        "has_weights": has_weights,
        "weight_assignment_count": weight_assignment_count,
        "weighted_vertex_count": weighted_vertex_count,
        "unweighted_vertex_count": unweighted_vertex_count,
        "finite": all_finite,
        "non_finite_weight_count": non_finite_weight_count,
        "negative_weight_count": negative_weight_count,
        "normalized": normalized,
        "non_normalized_vertex_count": non_normalized_vertex_count,
        "minimum_vertex_weight_sum": minimum_weight_sum,
        "maximum_vertex_weight_sum": maximum_weight_sum,
        "binding_valid": binding_valid,
        "missing_bone_groups": missing_bone_groups,
        "invalid_group_index_count": invalid_group_index_count,
        "armature_modifiers": modifier_records,
        "weighted_group_vertex_counts": dict(sorted(weighted_group_vertex_counts.items())),
    }
    return result, dict(weighted_group_vertex_counts)


def inspect_shape_keys(mesh_object: bpy.types.Object) -> list[ShapeKeyRecord]:
    """Report shape-key names and current slider values for one mesh."""
    shape_keys = mesh_object.data.shape_keys
    if shape_keys is None:
        return []
    return [{
        "name": key_block.name,
        "value": float(key_block.value),
        "slider_min": float(key_block.slider_min),
        "slider_max": float(key_block.slider_max),
    } for key_block in shape_keys.key_blocks]


def inspect_mesh(mesh_object: bpy.types.Object) -> tuple[MeshRecord, dict[str, int]]:
    """Collect geometry, material, shape-key, bounds, and skin data for one mesh."""
    skin_weights, group_counts = inspect_skin_weights(mesh_object)
    polygon_counts = [0 for _ in mesh_object.material_slots]
    polygons_without_material_slot = 0
    for polygon in mesh_object.data.polygons:
        material_index = polygon.material_index
        if material_index < 0 or material_index >= len(polygon_counts):
            polygons_without_material_slot += 1
        else:
            polygon_counts[material_index] += 1
    material_slots: list[MaterialSlotRecord] = []
    for index, material_slot in enumerate(mesh_object.material_slots):
        material = material_slot.material
        material_slots.append({
            "material": material.name if material is not None else None,
            "polygon_count": polygon_counts[index],
        })

    record: MeshRecord = {
        "name": mesh_object.name,
        "vertex_count": len(mesh_object.data.vertices),
        "edge_count": len(mesh_object.data.edges),
        "polygon_count": len(mesh_object.data.polygons),
        "world_bounds": mesh_world_bounds(mesh_object),
        "parent": mesh_object.parent.name if mesh_object.parent is not None else None,
        "material_slots": material_slots,
        "polygons_without_material_slot": polygons_without_material_slot,
        "shape_keys": inspect_shape_keys(mesh_object),
        "skin_weights": skin_weights,
    }
    return record, group_counts


def inspect_armature(armature: bpy.types.Object) -> ArmatureRecord:
    """Report rest-pose hierarchy and world-space bone bounds."""
    bones: list[BoneRecord] = []
    endpoints: list[Vector] = []
    for bone in sorted(armature.data.bones, key=lambda item: item.name):
        head_world = armature.matrix_world @ bone.head_local
        tail_world = armature.matrix_world @ bone.tail_local
        endpoints.extend((head_world, tail_world))
        bones.append({
            "name": bone.name,
            "parent": bone.parent.name if bone.parent is not None else None,
            "use_deform": bone.use_deform,
            "connected": bone.use_connect,
            "head_world": vector_record(head_world, f"bone {armature.name}/{bone.name} head"),
            "tail_world": vector_record(tail_world, f"bone {armature.name}/{bone.name} tail"),
            "length": float(bone.length),
        })
    return {
        "name": armature.name,
        "bone_count": len(bones),
        "world_bounds": bounds_from_points(endpoints, f"armature {armature.name}"),
        "bones": bones,
    }


def inspect_images(materials: Sequence[bpy.types.Material]) -> list[ImageRecord]:
    """Report images referenced by materials assigned to imported mesh objects."""
    images: dict[int, bpy.types.Image] = {}
    for material in materials:
        if not material.use_nodes or material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                images[node.image.as_pointer()] = node.image
    records: list[ImageRecord] = []
    for image in sorted(images.values(), key=lambda item: item.name):
        filepath = image.filepath
        resolved_path: str | None = None
        resolved_file_exists: bool | None = None
        if filepath:
            absolute_path = Path(bpy.path.abspath(filepath))
            resolved_path = str(absolute_path)
            resolved_file_exists = absolute_path.is_file()
        packed = getattr(image, "packed_file", None) is not None
        records.append({
            "name": image.name,
            "source": image.source,
            "filepath": filepath,
            "resolved_path": resolved_path,
            "resolved_file_exists": resolved_file_exists,
            "packed": packed,
            "width": int(image.size[0]),
            "height": int(image.size[1]),
            "channels": int(image.channels),
        })
    return records


def inspect_materials(materials: Sequence[bpy.types.Material]) -> list[MaterialRecord]:
    """Report materials assigned to imported mesh objects and their image nodes."""
    records: list[MaterialRecord] = []
    for material in materials:
        image_nodes: list[MaterialImageNodeRecord] = []
        if material.use_nodes and material.node_tree is not None:
            for node in material.node_tree.nodes:
                if node.type == "TEX_IMAGE":
                    image_nodes.append({
                        "node": node.name,
                        "image": node.image.name if node.image is not None else None,
                    })
        records.append({
            "name": material.name,
            "use_nodes": material.use_nodes,
            "image_texture_nodes": image_nodes,
        })
    return records


def mesh_assigned_materials(meshes: Sequence[bpy.types.Object]) -> list[bpy.types.Material]:
    """Return unique non-null materials assigned to imported mesh slots."""
    materials = {
        slot.material.as_pointer(): slot.material
        for mesh_object in meshes
        for slot in mesh_object.material_slots
        if slot.material is not None
    }
    return sorted(materials.values(), key=lambda material: material.name)


def evaluated_world_vertices(mesh_object: bpy.types.Object) -> list[Vector]:
    """Read evaluated world-space mesh vertices without retaining temporary meshes."""
    dependency_graph = bpy.context.evaluated_depsgraph_get()
    evaluated_object = mesh_object.evaluated_get(dependency_graph)
    evaluated_mesh = evaluated_object.to_mesh()
    try:
        world_matrix = evaluated_object.matrix_world.copy()
        return [world_matrix @ vertex.co for vertex in evaluated_mesh.vertices]
    finally:
        evaluated_object.to_mesh_clear()


def weighted_joint_candidate(
    armatures: Sequence[bpy.types.Object],
    meshes: Sequence[bpy.types.Object],
    group_counts: dict[str, dict[str, int]],
) -> tuple[bpy.types.Object, bpy.types.Object, str] | None:
    """Find a weighted non-root deform bone with an active matching modifier."""
    for armature in sorted(armatures, key=lambda item: item.name):
        bones = sorted(armature.data.bones, key=lambda item: item.name)
        for bone in bones:
            if bone.parent is None or not bone.use_deform:
                continue
            for mesh_object in sorted(meshes, key=lambda item: item.name):
                group_count = group_counts.get(mesh_object.name, {}).get(bone.name, 0)
                if group_count <= 0:
                    continue
                has_active_modifier = any(
                    modifier.type == "ARMATURE" and modifier.object == armature and
                    modifier.show_viewport and modifier.use_vertex_groups and
                    (not modifier.vertex_group or modifier.vertex_group == bone.name)
                    for modifier in mesh_object.modifiers
                )
                if has_active_modifier:
                    return armature, mesh_object, bone.name
    return None


def compare_vertex_sets(before: Sequence[Vector], after: Sequence[Vector]) -> tuple[int, float]:
    """Count moved evaluated vertices and return their maximum displacement."""
    if len(before) != len(after):
        raise RuntimeError(f"Evaluated mesh vertex count changed during pose test: {len(before)} -> {len(after)}.")
    moved_vertex_count = 0
    maximum_displacement = 0.0
    for index, (before_vertex, after_vertex) in enumerate(zip(before, after, strict=True)):
        displacement = (after_vertex - before_vertex).length
        if not math.isfinite(displacement):
            raise RuntimeError(f"Non-finite evaluated displacement at vertex {index}.")
        if displacement > DEFORMATION_DISTANCE_TOLERANCE:
            moved_vertex_count += 1
        maximum_displacement = max(maximum_displacement, displacement)
    return moved_vertex_count, maximum_displacement


def rest_pose_distance_tolerance(vertices: Sequence[Vector]) -> float:
    """Budget float32 transform roundoff in the evaluated world coordinate frame."""
    if not vertices:
        raise ValueError('Rest-pose verification requires evaluated vertices')
    components = tuple(float(component) for vertex in vertices for component in vertex)
    if not all(math.isfinite(value) for value in components):
        raise ValueError('Rest-pose verification received nonfinite world coordinates')
    scale = max(1.0, max(abs(value) for value in components))
    return 8.0 * (2.0 ** -23) * scale


def test_weighted_joint_deformation(
    armatures: Sequence[bpy.types.Object],
    meshes: Sequence[bpy.types.Object],
    group_counts: dict[str, dict[str, int]],
) -> DeformationRecord:
    """Rotate one weighted child bone, compare evaluated vertices, and restore it."""
    candidate = weighted_joint_candidate(armatures, meshes, group_counts)
    if candidate is None:
        return {
            "passed": False,
            "reason": "No non-root deform bone has positive weights through an active vertex-group armature modifier.",
            "mesh": None,
            "armature": None,
            "bone": None,
            "local_rotation_axis": None,
            "rotation_degrees": 5.0,
            "moved_vertex_count": 0,
            "maximum_vertex_displacement": 0.0,
            "rest_pose_restored": True,
            "restore_maximum_displacement": 0.0,
            "restore_distance_tolerance": 0.0,
        }

    armature, mesh_object, bone_name = candidate
    pose_bone = armature.pose.bones.get(bone_name)
    if pose_bone is None:
        raise RuntimeError(f"Weighted armature bone {armature.name}/{bone_name} has no pose-bone entry.")
    bpy.context.view_layer.update()
    rest_vertices = evaluated_world_vertices(mesh_object)
    original_matrix_basis = pose_bone.matrix_basis.copy()
    original_rotation_mode = pose_bone.rotation_mode
    best_axis: str | None = None
    best_moved_vertex_count = 0
    best_maximum_displacement = 0.0
    restored_vertices: list[Vector] = []
    try:
        pose_bone.rotation_mode = "XYZ"
        for axis_name, component_index in (("X", 0), ("Y", 1), ("Z", 2)):
            pose_bone.matrix_basis = original_matrix_basis
            pose_bone.rotation_mode = "XYZ"
            rotation = pose_bone.rotation_euler.copy()
            rotation[component_index] += DEFORMATION_ROTATION_RADIANS
            pose_bone.rotation_euler = rotation
            bpy.context.view_layer.update()
            posed_vertices = evaluated_world_vertices(mesh_object)
            moved_count, maximum_displacement = compare_vertex_sets(rest_vertices, posed_vertices)
            if maximum_displacement > best_maximum_displacement:
                best_axis = axis_name
                best_moved_vertex_count = moved_count
                best_maximum_displacement = maximum_displacement
            if maximum_displacement > DEFORMATION_DISTANCE_TOLERANCE:
                break
    finally:
        pose_bone.rotation_mode = original_rotation_mode
        pose_bone.matrix_basis = original_matrix_basis
        bpy.context.view_layer.update()
        restored_vertices = evaluated_world_vertices(mesh_object)

    _, restore_maximum_displacement = compare_vertex_sets(rest_vertices, restored_vertices)
    restore_tolerance = rest_pose_distance_tolerance(rest_vertices)
    rest_pose_restored = restore_maximum_displacement <= restore_tolerance
    passed = best_axis is not None and best_moved_vertex_count > 0 and rest_pose_restored
    reason = None if passed else "The weighted joint rotation did not move evaluated vertices or failed to restore the rest pose."
    return {
        "passed": passed,
        "reason": reason,
        "mesh": mesh_object.name,
        "armature": armature.name,
        "bone": bone_name,
        "local_rotation_axis": best_axis,
        "rotation_degrees": 5.0,
        "moved_vertex_count": best_moved_vertex_count,
        "maximum_vertex_displacement": best_maximum_displacement,
        "rest_pose_restored": rest_pose_restored,
        "restore_maximum_displacement": restore_maximum_displacement,
        "restore_distance_tolerance": restore_tolerance,
    }


def reopen_saved_blend(blend_path: Path, expected_object_names: set[str]) -> tuple[bool, str | None]:
    """Reopen the saved project and verify the imported scene objects survived."""
    try:
        result = bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    except RuntimeError as error:
        return False, f"Blender could not reopen {blend_path}: {error}"
    if "FINISHED" not in result:
        return False, f"Blender did not finish reopening {blend_path}; operator result={sorted(result)}."
    reopened_names = {obj.name for obj in bpy.context.scene.objects}
    missing_names = sorted(expected_object_names - reopened_names)
    if missing_names:
        return False, f"Reopened project is missing imported objects: {missing_names}."
    return True, None


def build_validation(
    mesh_records: Sequence[MeshRecord],
    deformation_test: DeformationRecord,
    saved_blend_reopened: bool,
) -> ValidationRecord:
    """Aggregate skin and deformation checks without treating static meshes as skinned."""
    skinned_meshes = [record["skin_weights"] for record in mesh_records if record["skin_weights"]["has_weights"]]
    has_skinned_mesh = bool(skinned_meshes)
    return {
        "has_skinned_mesh": has_skinned_mesh,
        "all_weights_finite": has_skinned_mesh and all(record["finite"] for record in skinned_meshes),
        "all_weighted_vertices_normalized": has_skinned_mesh and all(
            record["normalized"] is True and record["negative_weight_count"] == 0 for record in skinned_meshes
        ),
        "all_skin_groups_bind_to_active_armature_bones": has_skinned_mesh and all(
            record["binding_valid"] is True for record in skinned_meshes
        ),
        "deformation_test_passed": deformation_test["passed"],
        "saved_blend_reopened": saved_blend_reopened,
    }


def prepare_paths(fbx_argument: Path, output_argument: Path) -> tuple[Path, Path, Path]:
    """Validate input and reserve distinct output filenames without overwriting."""
    fbx_path = fbx_argument.expanduser().resolve(strict=True)
    if not fbx_path.is_file() or not (fbx_path.suffix.lower() == ".fbx" or fbx_path.name.endswith('.geometry.json')):
        raise ValueError(f"Input must be an existing FBX or portable geometry manifest: {fbx_path}")
    output_dir = output_argument.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = fbx_path.name.removesuffix('.geometry.json') if fbx_path.name.endswith('.geometry.json') else fbx_path.stem
    audit_path = output_dir / f"{stem}_character_audit.json"
    blend_path = output_dir / f"{stem}_character_audited.blend"
    existing_outputs = [str(path) for path in (audit_path, blend_path) if path.exists()]
    if existing_outputs:
        raise FileExistsError(f"Refusing to overwrite existing audit outputs: {existing_outputs}.")
    return fbx_path, audit_path, blend_path


def run_audit(fbx_path: Path, audit_path: Path, blend_path: Path) -> AuditDocument:
    """Import one FBX, inspect it, perform a reversible deformation test, and save outputs."""
    require_supported_blender()
    enable_fbx_importer()
    clear_scene_objects()
    imported_objects, nested_skin_repairs = import_character_fbx(fbx_path)
    meshes = [obj for obj in imported_objects if obj.type == "MESH"]
    armatures = [obj for obj in imported_objects if obj.type == "ARMATURE"]
    if not meshes:
        raise RuntimeError(f"The FBX contains no mesh objects: {fbx_path}.")

    mesh_records: list[MeshRecord] = []
    group_counts: dict[str, dict[str, int]] = {}
    for mesh_object in meshes:
        mesh_record, mesh_group_counts = inspect_mesh(mesh_object)
        mesh_records.append(mesh_record)
        group_counts[mesh_object.name] = mesh_group_counts
    armature_records = [inspect_armature(armature) for armature in sorted(armatures, key=lambda item: item.name)]
    materials = mesh_assigned_materials(meshes)
    material_records = inspect_materials(materials)
    image_records = inspect_images(materials)
    deformation_test = test_weighted_joint_deformation(armatures, meshes, group_counts)

    expected_object_names = {obj.name for obj in imported_objects}
    try:
        save_result = bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    except RuntimeError as error:
        raise RuntimeError(f"Could not save audited Blender project {blend_path}: {error}") from error
    if "FINISHED" not in save_result or not blend_path.is_file() or blend_path.stat().st_size <= 0:
        raise RuntimeError(f"Blender did not produce a non-empty project file at {blend_path}; operator result={sorted(save_result)}.")
    output_blend_bytes = blend_path.stat().st_size
    saved_blend_reopened, reopen_error = reopen_saved_blend(blend_path, expected_object_names)
    validation = build_validation(mesh_records, deformation_test, saved_blend_reopened)
    status = "passed" if all(validation.values()) else "failed"
    warnings = [
        f"Mesh {record['name']} has {record['skin_weights']['unweighted_vertex_count']} vertices without non-zero skin weights."
        for record in mesh_records
        if record["skin_weights"]["unweighted_vertex_count"] > 0
    ]
    if reopen_error is not None:
        warnings.append(reopen_error)
    if deformation_test["reason"] is not None:
        warnings.append(deformation_test["reason"])

    document: AuditDocument = {
        "format": "CharacterFbxAudit/1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "blender_version": bpy.app.version_string,
        "source_fbx": str(fbx_path),
        "imported_object_count": len(imported_objects),
        "imported_object_names": sorted(expected_object_names),
        "mesh_count": len(mesh_records),
        "meshes": mesh_records,
        "armatures": armature_records,
        "materials": material_records,
        "images": image_records,
        "deformation_test": deformation_test,
        "validation": validation,
        "status": status,
        "output_blend": str(blend_path),
        "output_blend_bytes": output_blend_bytes,
        "saved_blend_reopened": saved_blend_reopened,
        "warnings": warnings,
        "visual_inspection_performed": False,
        "animation_playback_tested": False,
        "nested_source_skin_recoveries": nested_skin_repairs,
    }
    audit_path.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": status,
        "mesh_count": len(mesh_records),
        "armature_count": len(armature_records),
        "deformation_test_passed": deformation_test["passed"],
        "audit_json": str(audit_path),
        "blend_file": str(blend_path),
    }, ensure_ascii=False, allow_nan=False))
    if status != "passed":
        raise RuntimeError(f"Character FBX audit failed; complete diagnostics were written to {audit_path}.")
    return document


def main(argv: Sequence[str]) -> None:
    """Run the requested one-file character audit."""
    fbx_argument, output_argument = parse_script_arguments(argv)
    fbx_path, audit_path, blend_path = prepare_paths(fbx_argument, output_argument)
    run_audit(fbx_path, audit_path, blend_path)


if __name__ == "__main__":
    main(sys.argv)
