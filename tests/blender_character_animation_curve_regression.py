"""Exercise source animation curves, affine helpers, and saved-file validation in Blender."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import cast

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector


def source_transform(position: tuple[float, float, float], rotation: Quaternion,
                     scale: tuple[float, float, float]) -> dict[str, object]:
    return {
        "position": list(position),
        "rotation": [rotation.x, rotation.y, rotation.z, rotation.w],
        "scale": list(scale),
    }


def matrix_error(left: Matrix, right: Matrix) -> float:
    return max(abs(left[row][column] - right[row][column])
               for row in range(4) for column in range(4))


def write_source_fixture(output_dir: Path, bone_path: str, frame_count: int,
                         sample_rate: float) -> tuple[Path, Path]:
    hierarchy = output_dir / "source_hierarchy.json"
    source_frames = output_dir / "source_local_frames.json"
    rest = source_transform((0.0, 0.0, 0.0), Quaternion(), (1.0, 1.0, 1.0))
    hierarchy.write_text(json.dumps([{
        "index": 0,
        "path": bone_path,
        "parent_index": -1,
        "rest_local": rest,
    }], allow_nan=False, separators=(",", ":")), encoding="utf-8")

    rows: list[dict[str, object]] = []
    axis = Vector((0.0, 0.0, 1.0))
    for index in range(frame_count):
        angle = math.radians(75.0 * index)
        rotation = Quaternion(axis, angle)
        position = (0.08 * index, -0.025 * index, 0.015 * index)
        local = source_transform(position, rotation, (1.8, 0.65, 1.15))
        rows.append({"time": index / sample_rate, "locals": [local]})
    source_frames.write_text(json.dumps(rows, allow_nan=False, separators=(",", ":")),
                             encoding="utf-8")
    return hierarchy, source_frames


def create_scene_rig(bone_name: str, bone_path: str, frame_count: int) -> tuple[bpy.types.Object, bpy.types.Object]:
    scene = bpy.context.scene
    parent = bpy.data.objects.new("Animated transformed parent", None)
    scene.collection.objects.link(parent)
    parent.rotation_mode = "XYZ"
    parent.scale = (1.25, 0.8, 1.4)
    parent.location = (2.0, -1.0, 0.75)
    parent.rotation_euler = Euler((0.21, -0.32, 0.47), "XYZ")
    parent.keyframe_insert(data_path="location", frame=1)
    parent.keyframe_insert(data_path="rotation_euler", frame=1)
    parent.location = (2.7, -0.4, 1.1)
    parent.rotation_euler = Euler((-0.12, 0.18, 0.83), "XYZ")
    parent.keyframe_insert(data_path="location", frame=frame_count)
    parent.keyframe_insert(data_path="rotation_euler", frame=frame_count)

    armature_data = bpy.data.armatures.new("Source curve regression rig data")
    armature = bpy.data.objects.new("Source curve regression rig", armature_data)
    scene.collection.objects.link(armature)
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bone = armature_data.edit_bones.new(bone_name)
    edit_bone.head = (0.0, 0.0, 0.0)
    edit_bone.tail = (0.0, 1.0, 0.0)
    edit_bone.roll = 0.63
    bpy.ops.object.mode_set(mode="OBJECT")
    armature.data.bones[bone_name]["source_transform_path"] = bone_path
    armature.parent = parent
    armature.matrix_parent_inverse = Matrix.Identity(4)
    armature.matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()
    return armature, parent


def validate_quaternion_curves(action: bpy.types.Action, data_path: str,
                               frame_count: int, label: str) -> float:
    curves = {curve.array_index: curve for curve in action.fcurves
              if curve.data_path == data_path}
    if set(curves) != {0, 1, 2, 3}:
        raise AssertionError(f"Quaternion channel indices differ: {label}, got={sorted(curves)}")
    previous: Quaternion | None = None
    minimum_dot = 1.0
    for frame in range(1, frame_count + 1):
        current = Quaternion(tuple(curves[index].evaluate(float(frame)) for index in range(4)))
        if abs(current.magnitude - 1.0) > 2e-4:
            raise AssertionError(f"Quaternion key is not unit length: {label}, frame={frame}, value={tuple(current)}")
        if previous is not None:
            dot = sum(previous[index] * current[index] for index in range(4))
            minimum_dot = min(minimum_dot, dot)
            if dot < -1e-5:
                raise AssertionError(f"Quaternion sign is discontinuous: {label}, frame={frame}, dot={dot}")
        previous = current
    return minimum_dot


def main(source_dir: Path, output_dir: Path) -> None:
    runtime_dir = source_dir / "character_runtime"
    if not runtime_dir.is_dir():
        raise FileNotFoundError(f"Character runtime source directory does not exist: {runtime_dir}")
    sys.path.insert(0, str(runtime_dir))
    from character_animation_affine import AffineTarget
    from character_animation_binding import (ClipData, attach_animation, bind_rest, read_source,
                                            sampled_pose_checks, validate_attached_animation)

    output_dir.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    bone_name = 'Curve "quoted" \\ branch/root'
    bone_path = "Avatar/Root"
    frame_count = 6
    sample_rate = 30.0
    hierarchy_path, frames_path = write_source_fixture(output_dir, bone_path, frame_count, sample_rate)
    armature, parent = create_scene_rig(bone_name, bone_path, frame_count)
    initial_scenes = frozenset(scene_row.name for scene_row in bpy.data.scenes)
    nodes, frames = read_source(hierarchy_path, frames_path, sample_rate)
    clip = ClipData(nodes, frames, bind_rest(armature, nodes))
    if armature.parent != parent or armature.scale != Vector((1.0, 1.0, 1.0)):
        raise AssertionError("Regression rig lost its transformed object parent or unit object scale")

    binding = attach_animation(armature, hierarchy_path, frames_path,
                               "Escaped path affine curve regression", sample_rate, 2e-4)
    if len(binding["per_frame_checks"]) != frame_count:
        raise AssertionError(f"Construction did not check every sampled frame: {len(binding['per_frame_checks'])}")
    if frozenset(scene_row.name for scene_row in bpy.data.scenes) != initial_scenes:
        raise AssertionError("Temporary animation validation Scene was not cleaned up")
    if bpy.context.scene != scene or scene.objects.get(armature.name) != armature:
        raise AssertionError("Source Scene or armature link changed during isolated sampled validation")

    affine_rows = cast(list[dict[str, object]], binding["affine_targets"])
    if len(affine_rows) != 1:
        raise AssertionError(f"Expected exactly one real affine helper pair, got {len(affine_rows)}")
    if float(binding["maximum_basis_trs_residual"]) <= 1e-3:
        raise AssertionError(f"Fixture did not exercise a non-TRS basis: residual={binding['maximum_basis_trs_residual']}")

    helpers: list[AffineTarget] = []
    for helper_row in affine_rows:
        objects = cast(list[dict[str, object]], helper_row["objects"])
        helpers.append(AffineTarget(
            str(helper_row["bone_name"]),
            bpy.data.objects[str(objects[0]["object"])],
            bpy.data.objects[str(objects[1]["object"])],
            str(helper_row["constraint"]),
        ))
    direct_checks = sampled_pose_checks(armature, clip, tuple(helpers), 2e-4)
    if len(direct_checks) != frame_count:
        raise AssertionError(f"Isolated Scene checker did not validate every frame: {len(direct_checks)}")
    scene.frame_set(1)
    bpy.context.view_layer.update()
    if frozenset(scene_row.name for scene_row in bpy.data.scenes) != initial_scenes:
        raise AssertionError("Direct sampled_pose_checks call leaked its temporary Scene")
    parent_world = parent.matrix_world.copy()

    bone = armature.pose.bones[bone_name]
    bone_curve_path = bone.path_from_id("rotation_quaternion")
    if '\\"' not in bone_curve_path or "\\\\" not in bone_curve_path:
        raise AssertionError(f"Escaped bone path was not encoded in the FCurve path: {bone_curve_path}")
    armature.path_resolve(bone_curve_path, False)
    action = armature.animation_data.action if armature.animation_data else None
    if action is None:
        raise AssertionError("Bulk bone curve writer did not attach an Action")
    bone_quaternion_minimum_dot = validate_quaternion_curves(action, bone_curve_path, frame_count,
                                                              "escaped bone")
    if len(action.fcurves) != 10 or any(len(curve.keyframe_points) != frame_count for curve in action.fcurves):
        raise AssertionError("Bulk bone Action has unexpected channel or key counts")

    helper_quaternion_minimum_dots: list[float] = []
    for helper_row in affine_rows:
        objects = cast(list[dict[str, object]], helper_row["objects"])
        if len(objects) != 2:
            raise AssertionError("Affine target receipt does not contain its real helper pair")
        for helper_record in objects:
            helper = bpy.data.objects[str(helper_record["object"])]
            helper_action = helper.animation_data.action if helper.animation_data else None
            if helper_action is None or helper_action.name != helper_record["action"]:
                raise AssertionError(f"Affine helper Action is missing: {helper.name}")
            helper_quaternion_minimum_dots.append(validate_quaternion_curves(
                helper_action, helper.path_from_id("rotation_quaternion"), frame_count, helper.name))

    output_dir.joinpath("binding_receipt.json").write_text(
        json.dumps(binding, allow_nan=False, indent=2), encoding="utf-8")
    blend_path = output_dir / "character_animation_curve_regression.blend"
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    bpy.ops.wm.open_mainfile(filepath=str(blend_path), use_scripts=False)

    restored_armature = bpy.data.objects.get("Source curve regression rig")
    restored_parent = bpy.data.objects.get("Animated transformed parent")
    if restored_armature is None or restored_parent is None or restored_armature.parent != restored_parent:
        raise AssertionError("Saved file did not retain the parented rig")
    parent_matrix_error = matrix_error(restored_parent.matrix_world, parent_world)
    if parent_matrix_error > 1e-5:
        raise AssertionError(f"Saved parent object transform changed: error={parent_matrix_error}")
    if bpy.data.scenes.get("Source animation validation") is not None:
        raise AssertionError("Temporary validation Scene leaked into the saved blend")
    saved_scenes = frozenset(scene_row.name for scene_row in bpy.data.scenes)
    if saved_scenes != initial_scenes:
        raise AssertionError(f"Unexpected saved Scene set: {sorted(saved_scenes)}")

    reopened = validate_attached_animation(restored_armature, binding,
                                           tuple(range(1, frame_count + 1)), 2e-4)
    if reopened["checked_frames"] != list(range(1, frame_count + 1)):
        raise AssertionError("Saved-file matrix validation did not cover every sampled frame")
    report = {
        "status": "passed",
        "blender_version": bpy.app.version_string,
        "frames": frame_count,
        "bone_name": bone_name,
        "escaped_bone_curve_path": bone_curve_path,
        "bone_action_curve_count": len(restored_armature.animation_data.action.fcurves),
        "affine_helper_pairs": len(affine_rows),
        "maximum_basis_trs_residual": binding["maximum_basis_trs_residual"],
        "construction_all_frame_matrix_checks": len(binding["per_frame_checks"]),
        "saved_reopen_all_frame_matrix_checks": len(reopened["checks"]),
        "saved_reopen_maximum_matrix_error": reopened["maximum_matrix_error"],
        "bone_quaternion_minimum_adjacent_dot": bone_quaternion_minimum_dot,
        "helper_quaternion_minimum_adjacent_dots": helper_quaternion_minimum_dots,
        "parent_matrix_error_after_reopen": parent_matrix_error,
        "temporary_scene_cleaned": True,
        "blend": str(blend_path),
        "binding_receipt": str(output_dir / "binding_receipt.json"),
    }
    (output_dir / "validation.json").write_text(json.dumps(report, allow_nan=False, indent=2),
                                                encoding="utf-8")
    print("CHARACTER_ANIMATION_CURVES_VERIFIED", json.dumps(report, allow_nan=False, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    main(arguments.src, arguments.output)
