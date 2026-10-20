"""Measure a real clip's editable contact candidate against explicit capsule proxies."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blender_sleeve_contact import ContactCapsule, ContactSettings, add_sleeve_contact


def points(mesh: bpy.types.Object) -> np.ndarray:
    evaluated = mesh.evaluated_get(bpy.context.evaluated_depsgraph_get())
    values = np.empty(len(evaluated.data.vertices) * 3, dtype=np.float64)
    evaluated.data.vertices.foreach_get("co", values)
    return values.reshape((-1, 3))


def source_hash(mesh: bpy.types.Object, groups: int) -> str:
    digest = hashlib.sha256()
    for vertex in mesh.data.vertices:
        digest.update(struct.pack("<3f", *vertex.co))
        for value in vertex.groups:
            if value.group < groups:
                digest.update(struct.pack("<IIf", vertex.index, value.group, value.weight))
    return digest.hexdigest()


def matrix_values(armature: bpy.types.Object, capsules: tuple[ContactCapsule, ...]) -> np.ndarray:
    return np.asarray([tuple(value for row in armature.pose.bones[capsule.bone].matrix for value in row)
                       for capsule in capsules])


def clearance(mesh: bpy.types.Object, armature: bpy.types.Object, capsule: ContactCapsule,
               values: np.ndarray) -> np.ndarray:
    matrix = mesh.matrix_world.inverted() @ armature.matrix_world @ armature.pose.bones[capsule.bone].matrix
    start = np.asarray(matrix @ Vector(capsule.start))
    end = np.asarray(matrix @ Vector(capsule.end))
    axis = end - start
    fraction = np.clip((values - start) @ axis / (axis @ axis), 0.0, 1.0)
    return np.linalg.norm(values - start - fraction[:, None] * axis, axis=1) - capsule.radius


def triangle_overlap_count(values: np.ndarray, left: tuple[tuple[int, ...], ...],
                            right: tuple[tuple[int, ...], ...]) -> int:
    trees = []
    for faces in (left, right):
        indices = sorted({index for face in faces for index in face})
        remap = {index: offset for offset, index in enumerate(indices)}
        trees.append(BVHTree.FromPolygons([Vector(values[index]) for index in indices],
                     [tuple(remap[index] for index in face) for face in faces], all_triangles=True))
    return len(trees[0].overlap(trees[1]))


def render_pair(scene: bpy.types.Scene, armature: bpy.types.Object, capsule: ContactCapsule,
                 modifier: bpy.types.NodesModifier, frame: int, output: Path) -> None:
    scene.frame_set(frame)
    matrix = armature.matrix_world @ armature.pose.bones[capsule.bone].matrix
    target = matrix @ Vector((0.015, 0.0, 0.0))
    scene.camera.location = matrix @ Vector((0.015, -0.24, 0.18))
    scene.camera.rotation_euler = (target - scene.camera.location).to_track_quat("-Z", "Y").to_euler()
    for state in ("before", "after"):
        modifier.show_render = state == "after"
        scene.render.filepath = str(output / f"{capsule.name}_{frame:03d}_{state}.png")
        bpy.ops.render.render(write_still=True)
    modifier.show_render = True


def main() -> None:
    config_path, output = (Path(value) for value in sys.argv[sys.argv.index("--") + 1:])
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    mesh = bpy.data.objects[config["mesh"]]
    armature = bpy.data.objects[config["armature"]]
    capsules = tuple(ContactCapsule(row["name"], row["bone"], tuple(row["start"]), tuple(row["end"]),
                                     float(row["radius"]), tuple(row["source_groups"])) for row in config["capsules"])
    settings = ContactSettings(**config["settings"])
    group_count = len(mesh.vertex_groups)
    original = source_hash(mesh, group_count)
    weights = []
    for capsule in capsules:
        indices = {mesh.vertex_groups[name].index for name in capsule.source_groups}
        weights.append(np.asarray([sum(value.weight for value in vertex.groups if value.group in indices)
                                   for vertex in mesh.data.vertices]))
    selected = tuple(np.flatnonzero(value >= settings.full_weight) for value in weights)
    untouched = np.flatnonzero(np.max(weights, axis=0) == 0)
    mesh.data.calc_loop_triangles()
    sleeve_faces = []
    hand_faces = []
    for capsule, weight in zip(capsules, weights, strict=True):
        bones = (armature.data.bones[capsule.bone], *armature.data.bones[capsule.bone].children_recursive)
        indices = {mesh.vertex_groups[bone.name].index for bone in bones if bone.name in mesh.vertex_groups}
        hand = np.asarray([sum(value.weight for value in vertex.groups if value.group in indices)
                           for vertex in mesh.data.vertices]) >= 0.5
        hand &= weight == 0
        sleeve_faces.append(tuple(tuple(face.vertices) for face in mesh.data.loop_triangles
                                  if all(weight[index] >= settings.full_weight for index in face.vertices)))
        hand_faces.append(tuple(tuple(face.vertices) for face in mesh.data.loop_triangles
                                if all(hand[index] for index in face.vertices)))
        if not sleeve_faces[-1] or not hand_faces[-1]:
            raise AssertionError(f"Real sleeve/hand triangle partition is empty: capsule={capsule.name}")
    baseline: list[np.ndarray] = []
    poses: list[np.ndarray] = []
    scene = bpy.context.scene
    if (scene.frame_start, scene.frame_end) != (1, 61):
        raise AssertionError(f"Bounded real sample must contain61frames: range={scene.frame_start},{scene.frame_end}")
    for frame in range(1, 62):
        scene.frame_set(frame)
        baseline.append(points(mesh))
        poses.append(matrix_values(armature, capsules))
    collection = bpy.data.collections.new("Editable Contact Proxies")
    scene.collection.children.link(collection)
    modifier = add_sleeve_contact(mesh, armature, capsules, settings, collection, "EF Optional Sleeve Contact")
    rows: list[dict[str, float | int | str]] = []
    maximum_displacement = 0.0
    maximum_unselected = 0.0
    maximum_pose = 0.0
    worst_after = 1e6
    for frame in range(1, 62):
        scene.frame_set(frame)
        corrected = points(mesh)
        delta = np.linalg.norm(corrected - baseline[frame - 1], axis=1)
        maximum_displacement = max(maximum_displacement, float(np.max(delta)))
        maximum_unselected = max(maximum_unselected, float(np.max(delta[untouched])))
        maximum_pose = max(maximum_pose, float(np.max(np.abs(matrix_values(armature, capsules) - poses[frame - 1]))))
        for capsule, indices, sleeve, hand in zip(capsules, selected, sleeve_faces, hand_faces, strict=True):
            before = clearance(mesh, armature, capsule, baseline[frame - 1][indices])
            after = clearance(mesh, armature, capsule, corrected[indices])
            worst_after = min(worst_after, float(np.min(after)))
            row: dict[str, float | int | str] = {"frame": frame, "capsule": capsule.name, "selected_vertices": len(indices),
                         "before_minimum_clearance_m": float(np.min(before)),
                         "after_minimum_clearance_m": float(np.min(after)),
                         "before_penetrating_vertices": int(np.sum(before < 0)),
                         "after_penetrating_vertices": int(np.sum(after < -1e-5))}
            row["hand_triangles"] = len(hand)
            row["sleeve_triangles"] = len(sleeve)
            row["before_hand_sleeve_triangle_overlaps"] = triangle_overlap_count(baseline[frame - 1], sleeve, hand)
            row["after_hand_sleeve_triangle_overlaps"] = triangle_overlap_count(corrected, sleeve, hand)
            rows.append(row)
    if worst_after < settings.margin - 1e-5 or maximum_pose > 1e-7 or maximum_unselected > 1e-7:
        raise AssertionError(f"Contact invariant failed: clearance={worst_after}, pose={maximum_pose}, outside={maximum_unselected}")
    if maximum_displacement > settings.maximum_displacement + 1e-5 or maximum_displacement <= 1e-5:
        raise AssertionError(f"Contact displacement is ineffective or unbounded: maximum={maximum_displacement}")
    if source_hash(mesh, group_count) != original:
        raise AssertionError("Contact candidate changed source coordinates or original skin weights")
    scene.frame_set(26)
    saved = output / "editable_sleeve_contact.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(saved))
    bpy.ops.wm.open_mainfile(filepath=str(saved), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    mesh = bpy.data.objects[config["mesh"]]
    armature = bpy.data.objects[config["armature"]]
    modifier = mesh.modifiers["EF Optional Sleeve Contact"]
    reopen_clearance = min(float(np.min(clearance(mesh, armature, capsule, points(mesh)[indices])))
                           for capsule, indices in zip(capsules, selected, strict=True))
    if reopen_clearance < settings.margin - 1e-5:
        raise AssertionError(f"Reopened capsule correction changed: clearance={reopen_clearance}")
    for obj in scene.objects:
        if obj.type == "MESH" and obj != mesh:
            obj.hide_render = True
    scene.use_nodes = False
    bpy.ops.object.camera_add()
    scene.camera = bpy.context.object
    scene.camera.data.type = "ORTHO"
    scene.camera.data.ortho_scale = 0.22
    bpy.ops.object.light_add(type="AREA", location=(0.0, -1.0, 2.0))
    bpy.context.object.data.energy = 120.0
    bpy.context.object.data.size = 1.5
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 320
    scene.render.resolution_y = 320
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    for capsule in capsules:
        for frame in (19, 26, 27):
            render_pair(scene, armature, capsule, modifier, frame, output)
    (output / "audit.json").write_text(json.dumps({
        "status": "opt_in_blender_contact_candidate_not_native_parity", "frames": 61,
        "config": str(config_path), "source_geometry_and_original_weights_sha256": original,
        "maximum_vertex_displacement_m": maximum_displacement,
        "maximum_unselected_displacement_m": maximum_unselected, "maximum_hand_pose_matrix_error": maximum_pose,
        "worst_corrected_proxy_clearance_m": worst_after, "reopen_proxy_clearance_m": reopen_clearance,
        "blend": str(saved), "rows": rows,
        "limitations": "Authored hand-bone capsule proxies and sleeve-weight falloff; proxy clearance does not prove full skin separation or reconstruct native dynamics. Triangle overlaps cover only faces fully inside the explicit sleeve-weight and descendant-hand partitions. Before/after renders use real posed geometry and source materials."
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
