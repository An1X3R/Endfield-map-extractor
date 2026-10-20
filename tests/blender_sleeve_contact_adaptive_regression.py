"""Select bounded clip-specific contact controls from real hand/sleeve intersections."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
from typing import NamedTuple

import bpy
import numpy as np
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_sleeve_contact import (
    ContactCapsule, ContactFrame, ContactSettings, add_sleeve_contact, bake_sleeve_contact_controls,
)
from blender_sleeve_contact_regression import (
    matrix_values, points, render_pair, source_hash, triangle_overlap_count,
)


class Candidate(NamedTuple):
    radius: float
    strength: float
    overlaps: int
    maximum_displacement: float
    mean_displacement: float


def corrected_points(values: np.ndarray, matrix: np.ndarray, capsule: ContactCapsule,
                     weights: np.ndarray, settings: ContactSettings,
                     radius: float, strength: float) -> np.ndarray:
    """Evaluate the exact capsule node arithmetic for a bounded candidate search."""
    endpoints = np.asarray(((*capsule.start, 1.0), (*capsule.end, 1.0))) @ matrix.T
    start, end = endpoints[:, :3]
    segment = end - start
    relative = values - start
    fraction = np.clip(relative @ segment / (segment @ segment), 0.0, 1.0)
    radial = relative - fraction[:, None] * segment
    length = np.linalg.norm(radial, axis=1)
    direction = np.divide(radial, length[:, None], out=np.zeros_like(radial), where=length[:, None] > 0)
    depth = np.clip(radius + settings.margin - length, 0.0, settings.maximum_displacement)
    factor = np.clip(weights / settings.full_weight, 0.0, 1.0)
    factor = factor ** 3 * (factor * (factor * 6.0 - 15.0) + 10.0)
    return values + direction * (depth * factor * settings.strength * strength)[:, None]


def main() -> None:
    config_path, output = (Path(value) for value in sys.argv[sys.argv.index("--") + 1:])
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    mesh = bpy.data.objects[config["mesh"]]
    armature = bpy.data.objects[config["armature"]]
    scene = bpy.context.scene
    if (scene.frame_start, scene.frame_end) != (1, 61):
        raise AssertionError("Adaptive regression requires the explicit61frame source clip")
    capsules = tuple(ContactCapsule(row["name"], row["bone"], tuple(row["start"]), tuple(row["end"]),
                                    row["radius"], tuple(row["source_groups"])) for row in config["capsules"])
    settings = ContactSettings(**config["settings"])
    radii = tuple(float(value) for value in config["candidate_radii"])
    strengths = tuple(float(value) for value in config["candidate_strengths"])
    if (not radii or not strengths or len(radii) * len(strengths) > 12
            or any(not math.isfinite(value) or value <= 0 for value in radii)
            or any(not math.isfinite(value) or not 0 < value <= 1 for value in strengths)):
        raise ValueError("Candidate grid must contain at most12finite positive radius/strength pairs")
    source_blend = Path(bpy.data.filepath)
    source_sha = hashlib.sha256(source_blend.read_bytes()).hexdigest()
    group_count = len(mesh.vertex_groups)
    original_hash = source_hash(mesh, group_count)
    source_action = armature.animation_data.action.name
    weights = []
    sleeve_faces = []
    hand_faces = []
    mesh.data.calc_loop_triangles()
    for capsule in capsules:
        indices = {mesh.vertex_groups[name].index for name in capsule.source_groups}
        weight = np.asarray([sum(group.weight for group in vertex.groups if group.group in indices)
                             for vertex in mesh.data.vertices])
        weights.append(weight)
        bone = armature.data.bones[capsule.bone]
        names = {item.name for item in (bone, *bone.children_recursive)}
        indices = {group.index for group in mesh.vertex_groups if group.name in names}
        hand = np.asarray([sum(group.weight for group in vertex.groups if group.group in indices)
                           for vertex in mesh.data.vertices]) >= 0.5
        hand &= weight == 0
        sleeve_faces.append(tuple(tuple(face.vertices) for face in mesh.data.loop_triangles
                                  if all(weight[index] >= settings.full_weight for index in face.vertices)))
        hand_faces.append(tuple(tuple(face.vertices) for face in mesh.data.loop_triangles
                                if all(hand[index] for index in face.vertices)))
        if not sleeve_faces[-1] or not hand_faces[-1]:
            raise AssertionError(f"Source contact partition is empty: capsule={capsule.name}")
    if any(np.any((left > 0) & (right > 0))
           for index, left in enumerate(weights) for right in weights[index + 1:]):
        raise AssertionError("Independent side search requires disjoint source weight groups")
    untouched = np.flatnonzero(np.max(weights, axis=0) == 0)
    baseline = []
    poses = []
    results = []
    selected = []
    for frame in range(1, 62):
        scene.frame_set(frame)
        values = points(mesh)
        baseline.append(values)
        poses.append(matrix_values(armature, capsules))
        for capsule, weight, sleeve, hand in zip(capsules, weights, sleeve_faces, hand_faces, strict=True):
            before = triangle_overlap_count(values, sleeve, hand)
            matrix = np.asarray(mesh.matrix_world.inverted() @ armature.matrix_world
                                @ armature.pose.bones[capsule.bone].matrix)
            candidates = [Candidate(capsule.radius, 0.0, before, 0.0, 0.0)]
            for radius in radii:
                for strength in strengths:
                    corrected = corrected_points(values, matrix, capsule, weight, settings, radius, strength)
                    displacement = np.linalg.norm(corrected - values, axis=1)
                    candidates.append(Candidate(radius, strength,
                        triangle_overlap_count(corrected, sleeve, hand),
                        float(np.max(displacement)), float(np.mean(displacement[weight > 0]))))
            valid = [candidate for candidate in candidates if candidate.overlaps <= before]
            best = min(candidate.overlaps for candidate in valid)
            tolerance = math.floor(before * config["overlap_slack_fraction"])
            finalists = [candidate for candidate in valid if candidate.overlaps <= best + tolerance]
            chosen = min(finalists, key=lambda candidate: (
                candidate.maximum_displacement, candidate.mean_displacement, candidate.overlaps))
            selected.append(ContactFrame(frame, capsule.name, chosen.radius, chosen.strength))
            results.append({"frame": frame, "capsule": capsule.name, "before": before,
                            "selected": chosen._asdict(), "candidates": [item._asdict() for item in candidates]})
        if frame % 10 == 0:
            print(json.dumps({"search_frame": frame}), flush=True)
    (output / "selection.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    collection = bpy.data.collections.new("Clip Specific Contact Proxies")
    scene.collection.children.link(collection)
    modifier = add_sleeve_contact(mesh, armature, capsules, settings, collection, "EF Optional Sleeve Contact")
    identity = json.dumps({"source_blend_sha256": source_sha, "source_action": source_action,
                           "source_frames": [1, 61]}, sort_keys=True)
    bake_sleeve_contact_controls(modifier, tuple(selected), identity)
    rows = []
    maximum_displacement = 0.0
    maximum_unselected = 0.0
    maximum_pose = 0.0
    maximum_prediction_error = 0.0
    corrected_frames = []
    for frame in range(1, 62):
        scene.frame_set(frame)
        corrected = points(mesh)
        corrected_frames.append(corrected)
        delta = np.linalg.norm(corrected - baseline[frame - 1], axis=1)
        maximum_displacement = max(maximum_displacement, float(np.max(delta)))
        maximum_unselected = max(maximum_unselected, float(np.max(delta[untouched])))
        maximum_pose = max(maximum_pose, float(np.max(np.abs(matrix_values(armature, capsules) - poses[frame - 1]))))
        predicted = baseline[frame - 1].copy()
        for side, (capsule, weight, sleeve, hand) in enumerate(zip(capsules, weights, sleeve_faces, hand_faces, strict=True)):
            sample = selected[(frame - 1) * len(capsules) + side]
            row = results[(frame - 1) * len(capsules) + side]
            matrix = np.asarray(mesh.matrix_world.inverted() @ armature.matrix_world
                                @ armature.pose.bones[capsule.bone].matrix)
            predicted = corrected_points(predicted, matrix, capsule, weight, settings, sample.radius, sample.strength)
            after = triangle_overlap_count(corrected, sleeve, hand)
            if after > row["before"]:
                raise AssertionError(f"Actual Blender contact regressed: frame={frame}, side={capsule.name}, before={row['before']}, after={after}")
            rows.append({"frame": frame, "capsule": capsule.name, "before": row["before"], "after": after,
                         "radius": sample.radius, "strength": sample.strength,
                         "maximum_displacement_m": float(np.max(delta[weight > 0])),
                         "mean_displacement_m": float(np.mean(delta[weight > 0]))})
        maximum_prediction_error = max(maximum_prediction_error, float(np.max(np.abs(predicted - corrected))))
    if (maximum_unselected != 0.0 or maximum_pose != 0.0 or maximum_prediction_error > 2e-6
            or maximum_displacement > settings.maximum_displacement + 1e-6
            or source_hash(mesh, group_count) != original_hash):
        raise AssertionError(f"Adaptive invariants failed: unselected={maximum_unselected}, pose={maximum_pose}, prediction={maximum_prediction_error}, displacement={maximum_displacement}")
    correction_steps = [float(np.max(np.linalg.norm(
        (corrected_frames[index] - baseline[index])
        - (corrected_frames[index - 1] - baseline[index - 1]), axis=1))) for index in range(1, 61)]
    scene.frame_set(29)
    saved = output / "editable_clip_contact.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(saved))
    bpy.ops.wm.open_mainfile(filepath=str(saved), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    mesh = bpy.data.objects[config["mesh"]]
    armature = bpy.data.objects[config["armature"]]
    modifier = mesh.modifiers["EF Optional Sleeve Contact"]
    reopen_error = 0.0
    for frame in range(1, 62):
        scene.frame_set(frame)
        reopen_error = max(reopen_error, float(np.max(np.abs(points(mesh) - corrected_frames[frame - 1]))))
    if reopen_error > 1e-7 or source_hash(mesh, group_count) != original_hash:
        raise AssertionError(f"Reopened contact curves changed: coordinate_error={reopen_error}")
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
        for frame in (19, 26, 27, 28, 29):
            render_pair(scene, armature, capsule, modifier, frame, output)
    receipt = {
        "status": "tested_clip_only_opt_in_blender_approximation",
        "source": str(source_blend), "source_blend_sha256": source_sha,
        "source_action": source_action, "config": str(config_path), "frames": [1, 61],
        "source_geometry_and_original_weights_sha256": original_hash,
        "blend": str(saved), "rows": rows,
        "before_total": sum(row["before"] for row in rows),
        "after_total": sum(row["after"] for row in rows),
        "regressed_samples": sum(row["after"] > row["before"] for row in rows),
        "disabled_samples": sum(row["strength"] == 0 for row in rows),
        "maximum_vertex_displacement_m": maximum_displacement,
        "maximum_unselected_displacement_m": maximum_unselected,
        "maximum_hand_pose_matrix_error": maximum_pose,
        "maximum_numpy_blender_coordinate_error_m": maximum_prediction_error,
        "maximum_reopen_coordinate_error_m": reopen_error,
        "maximum_adjacent_frame_correction_change_m": max(correction_steps),
        "selection_policy": "Exclude real triangle-overlap regressions, then minimize displacement within3percent of the lowest overlap count;9positive candidates and explicit off per side/frame;25mm cap",
        "limitations": "Controls fit only this61frame source action at tested integer frames. Per-frame control changes can produce temporal popping. Linear interpolation, other actions and new poses are unverified. Triangle counts cover only fully partitioned sleeve/hand faces and do not prove watertight or all-body collision clearance. Source geometry, original weights and hand poses are preserved; correction is an editable opt-in approximation.",
    }
    (output / "audit.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("status", "before_total", "after_total", "regressed_samples", "disabled_samples", "maximum_vertex_displacement_m")}), flush=True)


if __name__ == "__main__":
    main()
