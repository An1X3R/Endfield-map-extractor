"""Attach source-local animation through measured rest matrices to an explicit rig."""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import cast

import bpy
from mathutils import Matrix, Quaternion, Vector

from character_animation_affine import (AffineFactors, AffineTarget, affine_target_matrix, assign_affine_target, create_affine_targets,
    finish_affine_targets, key_affine_target, split_affine, trs_residual, validate_affine_targets)


@dataclass(frozen=True)
class SourceNode:
    index: int
    path: str
    parent: int
    rest: Matrix


@dataclass(frozen=True)
class SourceFrame:
    time: float
    locals: tuple[Matrix, ...]


@dataclass(frozen=True)
class BoneBinding:
    source_index: int
    source_path: str
    bone_name: str
    correction: Matrix


@dataclass(frozen=True)
class ClipData:
    nodes: tuple[SourceNode, ...]
    frames: tuple[SourceFrame, ...]
    bindings: tuple[BoneBinding, ...]


def fields(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f'Expected JSON object: {label}')
    return cast(dict[str, object], value)


def rows(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f'Expected JSON array: {label}')
    return cast(list[object], value)


def numbers(value: object, length: int, label: str) -> tuple[float, ...]:
    values = rows(value, label)
    if len(values) != length or any(type(component) not in (float, int) or not math.isfinite(component) for component in values):
        raise ValueError(f'Expected {length} finite numbers: {label}')
    return tuple(float(component) for component in values)


def integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f'Expected integer: {label}')
    return value


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f'Expected nonempty string: {label}')
    return value


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_matrix(value: object, label: str) -> Matrix:
    entry = fields(value, label)
    position = numbers(entry['position'], 3, label + '.position')
    rotation = numbers(entry['rotation'], 4, label + '.rotation')
    scale = numbers(entry['scale'], 3, label + '.scale')
    quaternion = Quaternion((rotation[3], rotation[0], rotation[1], rotation[2]))
    if abs(quaternion.magnitude - 1.) > 1e-4:
        raise ValueError(f'Non-unit source quaternion: {label}')
    reflection = Matrix.Diagonal((-1., 1., 1., 1.))
    local = Matrix.Translation(Vector(position)) @ quaternion.to_matrix().to_4x4() @ Matrix.Diagonal((*scale, 1.))
    return reflection @ local @ reflection


def accumulate(nodes: tuple[SourceNode, ...], locals_: tuple[Matrix, ...]) -> tuple[Matrix, ...]:
    result: dict[int, Matrix] = {}
    active: set[int] = set()
    def solve(index: int) -> Matrix:
        if index in active:
            raise ValueError(f'Cyclic source hierarchy: {nodes[index].path}')
        if index not in result:
            active.add(index)
            parent = nodes[index].parent
            result[index] = locals_[index].copy() if parent < 0 else solve(parent) @ locals_[index]
            active.remove(index)
        return result[index]
    return tuple(solve(index) for index in range(len(nodes)))


def read_source(hierarchy: Path, frames: Path, sample_rate: float) -> tuple[tuple[SourceNode, ...], tuple[SourceFrame, ...]]:
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError('Sample rate must be positive and finite')
    node_values = rows(json.loads(hierarchy.read_text(encoding='utf-8')), 'hierarchy')
    nodes = tuple(SourceNode(integer(fields(value, 'node')['index'], 'index'), text(fields(value, 'node')['path'], 'path'), integer(fields(value, 'node')['parent_index'], 'parent'), source_matrix(fields(value, 'node')['rest_local'], 'rest')) for value in node_values)
    if not nodes or len({node.path for node in nodes}) != len(nodes):
        raise ValueError('Source hierarchy must contain unique paths')
    for index, node in enumerate(nodes):
        if node.index != index or not -1 <= node.parent < len(nodes) or node.parent == index:
            raise ValueError(f'Invalid hierarchy index/parent: {index}/{node.parent}')
    frame_values = rows(json.loads(frames.read_text(encoding='utf-8')), 'frames')
    frame_data = tuple(SourceFrame(float(fields(value, 'frame')['time']), tuple(source_matrix(local, f'frame {index} local {node}') for node, local in enumerate(rows(fields(value, 'frame')['locals'], 'locals')))) for index, value in enumerate(frame_values))
    if not frame_data or any(len(frame.locals) != len(nodes) or not math.isfinite(frame.time) or abs(frame.time - index / sample_rate) > 1e-5 for index, frame in enumerate(frame_data)):
        raise ValueError('Frame dimensions/timestamps disagree with actual source hierarchy/sample rate')
    return nodes, frame_data


def rig_rest_hash(armature: bpy.types.Object) -> str:
    digest = hashlib.sha256()
    for bone in sorted(armature.data.bones, key=lambda bone: bone.name):
        digest.update(repr((bone.name, bone.parent.name if bone.parent else None, tuple(tuple(row) for row in bone.matrix_local))).encode())
    return digest.hexdigest()


def bind_rest(armature: bpy.types.Object, nodes: tuple[SourceNode, ...]) -> tuple[BoneBinding, ...]:
    if armature.type != 'ARMATURE' or not armature.data.bones:
        raise ValueError('An explicit nonempty armature object is required')
    if armature.constraints or any(bone.constraints for bone in armature.pose.bones):
        raise ValueError(f'Rig has runtime constraints unsupported by this direct source binder: {armature.name}')
    if any(abs(component - 1.) > 1e-5 for component in armature.scale):
        raise ValueError(f'Rig object scale must be unit: {armature.name}')
    rest = accumulate(nodes, tuple(node.rest for node in nodes))
    leaves: dict[str, list[int]] = {}
    for node in nodes:
        leaves.setdefault(node.path.rsplit('/', 1)[-1], []).append(node.index)
    full_paths = {node.path: node.index for node in nodes}
    bindings = []
    for bone in armature.data.bones:
        source_path = bone.get('source_transform_path')
        if source_path is not None:
            index = full_paths[text(source_path, 'bone source path')]
        else:
            candidates = leaves.get(bone.name, [])
            if len(candidates) != 1:
                raise ValueError(f'Missing/ambiguous source path for {armature.name}/{bone.name}: {len(candidates)} candidates')
            index = candidates[0]
        if abs(rest[index].determinant()) < 1e-12:
            raise ValueError(f'Singular source rest: {nodes[index].path}')
        correction = rest[index].inverted() @ bone.matrix_local
        bindings.append(BoneBinding(index, nodes[index].path, bone.name, correction))
    return tuple(bindings)


def bind_rest_with_bind_worlds(armature: bpy.types.Object, nodes: tuple[SourceNode, ...],
                               bind_worlds_by_path: dict[str, Matrix]) -> tuple[BoneBinding, ...]:
    if armature.type != 'ARMATURE' or not armature.data.bones:
        raise ValueError('An explicit nonempty armature object is required')
    if armature.constraints or any(bone.constraints for bone in armature.pose.bones):
        raise ValueError(f'Rig has runtime constraints unsupported by this direct source binder: {armature.name}')
    if any(abs(component - 1.) > 1e-5 for component in armature.scale):
        raise ValueError(f'Rig object scale must be unit: {armature.name}')
    paths = {node.path for node in nodes}
    if set(bind_worlds_by_path) != paths:
        missing = sorted(paths - set(bind_worlds_by_path))
        extra = sorted(set(bind_worlds_by_path) - paths)
        raise ValueError(f'Bind-world calibration paths differ from source hierarchy: missing={missing[:4]}, extra={extra[:4]}')
    bindings = []
    for bone in armature.data.bones:
        source_path = text(bone.get('source_transform_path'), f'{bone.name} source path')
        if source_path not in bind_worlds_by_path:
            raise ValueError(f'Bind-world calibration has no exact source path: {armature.name}/{bone.name}/{source_path}')
        bind_world = bind_worlds_by_path[source_path]
        if len(bind_world) != 4 or any(len(row) != 4 for row in bind_world):
            raise ValueError(f'Bind-world calibration is not 4x4: {source_path}')
        if any(not math.isfinite(component) for row in bind_world for component in row):
            raise ValueError(f'Bind-world calibration has nonfinite values: {source_path}')
        if abs(bind_world.determinant()) < 1e-12:
            raise ValueError(f'Singular source bind world: {source_path}')
        index = next((node.index for node in nodes if node.path == source_path), None)
        if index is None:
            raise ValueError(f'Bind-world calibration path is absent from source hierarchy: {source_path}')
        correction = bind_world.inverted() @ bone.matrix_local
        bindings.append(BoneBinding(index, source_path, bone.name, correction))
    return tuple(bindings)


def targets(clip: ClipData, frame: SourceFrame) -> dict[str, Matrix]:
    globals_ = accumulate(clip.nodes, frame.locals)
    return {binding.bone_name: globals_[binding.source_index] @ binding.correction for binding in clip.bindings}


def basis_targets(armature: bpy.types.Object, target: dict[str, Matrix]) -> dict[str, Matrix]:
    basis = {}
    for pose_bone in armature.pose.bones:
        bone = pose_bone.bone
        if pose_bone.parent is None:
            basis[pose_bone.name] = bone.convert_local_to_pose(target[pose_bone.name], bone.matrix_local, invert=True)
        else:
            basis[pose_bone.name] = bone.convert_local_to_pose(target[pose_bone.name], bone.matrix_local, parent_matrix=target[pose_bone.parent.name], parent_matrix_local=pose_bone.parent.bone.matrix_local, invert=True)
    return basis


def assign_basis(armature: bpy.types.Object, basis: dict[str, Matrix]) -> None:
    for bone in armature.pose.bones:
        location, rotation, scale = basis[bone.name].decompose()
        bone.rotation_mode = 'QUATERNION'
        bone.location = location
        bone.rotation_quaternion = rotation
        bone.scale = scale
    bpy.context.view_layer.update()


def pose_error(armature: bpy.types.Object, target: dict[str, Matrix], frame: int, tolerance: float) -> dict[str, object]:
    values = {}
    for bone in armature.pose.bones:
        if any(not math.isfinite(component) for row in bone.matrix for component in row):
            raise ValueError(f'Nonfinite actual pose: {armature.name}/{frame}/{bone.name}')
        values[bone.name] = max(abs(bone.matrix[row][column] - target[bone.name][row][column]) for row in range(4) for column in range(4))
    worst = max(values, key=values.get)
    report = {'frame': frame, 'maximum_matrix_error': values[worst], 'worst_bone': worst, 'finite_bones': len(values)}
    if values[worst] > tolerance:
        raise ValueError(f'Pose does not match actual source target: {armature.name}/{json.dumps(report)}')
    return report


def attach_animation(armature: bpy.types.Object, source_hierarchy: Path, source_local_frames: Path,
                     action_name: str, sample_rate: float, matrix_tolerance: float) -> dict[str, object]:
    nodes, frames = read_source(source_hierarchy, source_local_frames, sample_rate)
    return _attach_animation_with_bindings(armature, source_hierarchy, source_local_frames, action_name,
        sample_rate, matrix_tolerance, nodes, frames, bind_rest(armature, nodes),
        {'format': 'EndfieldBindRestSource/1', 'calibrated': False,
         'source': 'Avatar default-pose hierarchy rest; no mesh bind-world calibration supplied.'})


def attach_animation_with_bind_worlds(armature: bpy.types.Object, source_hierarchy: Path,
                                      source_local_frames: Path, action_name: str, sample_rate: float,
                                      matrix_tolerance: float, bind_worlds_by_path: dict[str, Matrix],
                                      calibration_receipt: dict[str, object]) -> dict[str, object]:
    if calibration_receipt.get('format') != 'EndfieldNpcBindWorldCalibration/1':
        raise ValueError('NPC animation requires the verified source bind-world calibration contract')
    if calibration_receipt.get('calibrated') is not True:
        raise ValueError('NPC animation bind-world calibration is not marked source verified')
    nodes, frames = read_source(source_hierarchy, source_local_frames, sample_rate)
    bindings = bind_rest_with_bind_worlds(armature, nodes, bind_worlds_by_path)
    return _attach_animation_with_bindings(armature, source_hierarchy, source_local_frames, action_name,
        sample_rate, matrix_tolerance, nodes, frames, bindings, calibration_receipt)


def bone_curve_samples(armature: bpy.types.Object, clip: ClipData) -> dict[str, tuple[array, ...]]:
    """Calculate continuous local TRS samples without changing the evaluated pose."""
    samples = {bone.name: tuple(array('f') for _ in range(10)) for bone in armature.pose.bones}
    previous: dict[str, Quaternion] = {}
    for index, frame in enumerate(clip.frames):
        basis = basis_targets(armature, targets(clip, frame))
        for name, matrix in basis.items():
            location, rotation, scale = matrix.decompose()
            if name in previous:
                rotation.make_compatible(previous[name])
            previous[name] = rotation.copy()
            for coordinates, value in zip(samples[name], (*location, *rotation, *scale), strict=True):
                coordinates.extend((index + 1, value))
    return samples


def write_bone_curves(armature: bpy.types.Object, action: bpy.types.Action,
                      samples: dict[str, tuple[array, ...]], frame_count: int) -> None:
    """Populate editable curves directly, avoiding per-frame pose RNA assignments."""
    for bone in armature.pose.bones:
        bone.rotation_mode = 'QUATERNION'
        offset = 0
        for channel, components in (('location', 3), ('rotation_quaternion', 4), ('scale', 3)):
            for component in range(components):
                curve = action.fcurves.new(bone.path_from_id(channel), index=component, action_group=bone.name)
                curve.keyframe_points.add(frame_count)
                curve.keyframe_points.foreach_set('co', samples[bone.name][offset + component])
                for point in curve.keyframe_points:
                    point.interpolation = 'LINEAR'
                    # Match the editable easing defaults of keyframe_insert.
                    point.back, point.amplitude, point.period = 1.70158, .8, 4.1
                curve.update()
            offset += components


def sampled_pose_checks(armature: bpy.types.Object, clip: ClipData,
                        helpers: tuple[AffineTarget, ...], tolerance: float) -> list[dict[str, object]]:
    """Evaluate every sampled pose without updating unrelated meshes and materials."""
    source_scene = bpy.context.scene
    scene = bpy.data.scenes.new('Source animation validation')
    objects = {armature, *(obj for helper in helpers for obj in (helper.left, helper.right))}
    for obj in tuple(objects):
        parent = obj.parent
        while parent is not None:
            objects.add(parent)
            parent = parent.parent
    try:
        for obj in objects:
            scene.collection.objects.link(obj)
        checks = []
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
            for index, frame in enumerate(clip.frames):
                scene.frame_set(index + 1)
                graph = bpy.context.evaluated_depsgraph_get()
                checks.append(pose_error(armature.evaluated_get(graph), targets(clip, frame), index + 1, tolerance))
    finally:
        bpy.data.scenes.remove(scene)
    for index in sorted({0, (len(clip.frames) - 1) // 2, len(clip.frames) - 1}):
        source_scene.frame_set(index + 1)
        bpy.context.view_layer.update()
        pose_error(armature, targets(clip, clip.frames[index]), index + 1, tolerance)
    return checks


def _attach_animation_with_bindings(armature: bpy.types.Object, source_hierarchy: Path,
                                    source_local_frames: Path, action_name: str, sample_rate: float,
                                    matrix_tolerance: float, nodes: tuple[SourceNode, ...],
                                    frames: tuple[SourceFrame, ...], bindings: tuple[BoneBinding, ...],
                                    calibration_receipt: dict[str, object]) -> dict[str, object]:
    if not math.isfinite(matrix_tolerance) or matrix_tolerance <= 0:
        raise ValueError('Matrix tolerance must be positive and finite')
    clip = ClipData(nodes, frames, bindings)
    rest_hash = rig_rest_hash(armature)
    affine_bones: set[str] = set()
    maximum_basis_trs_residual = 0.
    for frame in frames:
        for name, matrix in basis_targets(armature, targets(clip, frame)).items():
            residual = trs_residual(matrix)
            maximum_basis_trs_residual = max(maximum_basis_trs_residual, residual)
            if residual > matrix_tolerance * .5:
                affine_bones.add(name)
    helpers = create_affine_targets(armature, tuple(sorted(affine_bones)))
    preflight = []
    for index in range(min(3, len(frames))):
        target = targets(clip, frames[index])
        assign_basis(armature, basis_targets(armature, target))
        for helper in helpers:
            assign_affine_target(helper, split_affine(affine_target_matrix(helper, target), None))
        bpy.context.view_layer.update()
        preflight.append(pose_error(armature, target, index + 1, matrix_tolerance))
    armature.animation_data_create()
    old_action = armature.animation_data.action
    if old_action:
        old_action.use_fake_user = True
    action = bpy.data.actions.new(action_name + ' :: ' + armature.name)
    action.use_fake_user = True
    samples = bone_curve_samples(armature, clip)
    write_bone_curves(armature, action, samples, len(frames))
    # Bind after curve creation so Blender also selects the new Action slot.
    armature.animation_data.action = action
    previous_factors: dict[str, AffineFactors] = {}
    helper_rotations: dict[str, Quaternion] = {}
    if helpers:
        for index, frame in enumerate(frames):
            target = targets(clip, frame)
            for helper in helpers:
                factors = split_affine(affine_target_matrix(helper, target), previous_factors.get(helper.bone_name))
                helper_rotations = key_affine_target(helper, factors, index + 1, helper_rotations)
                previous_factors[helper.bone_name] = factors
    checks = sampled_pose_checks(armature, clip, helpers, matrix_tolerance)
    for curve in action.fcurves:
        for point in curve.keyframe_points:
            point.interpolation = 'LINEAR'
    if len(action.fcurves) != len(clip.bindings) * 10 or any(len(curve.keyframe_points) != len(frames) for curve in action.fcurves):
        raise ValueError('Action channel/keyframe counts disagree with the actual rig/clip')
    if rig_rest_hash(armature) != rest_hash:
        raise ValueError('Binding changed the rig rest matrices')
    affine_receipt = finish_affine_targets(helpers, len(frames))
    scene = bpy.context.scene
    scene.render.fps = round(sample_rate)
    scene.render.fps_base = scene.render.fps / sample_rate
    scene.frame_start, scene.frame_end = 1, len(frames)
    scene.frame_set(1)
    if calibration_receipt.get('format') == 'EndfieldNpcBindWorldCalibration/1':
        source_space = 'Unity parent-local meters, xyzw; H=diag(-1,1,1,1) reflects every local before accumulation; K=inverse(explicit source mesh bind-world global)*BlenderBoneRest; target=sourceFrameGlobal*K.'
    else:
        source_space = 'Unity parent-local meters, xyzw; H=diag(-1,1,1,1) reflects every local before accumulation; K=inverse(Avatar default-pose hierarchy rest global)*BlenderBoneRest; target=sourceFrameGlobal*K. This legacy path has no mesh bind-world calibration.'
    return {'format': 'EndfieldAnimationBindingReceipt/1', 'status': 'all_frames_written_and_matrix_verified_candidate', 'armature': armature.name, 'action_name': action.name, 'replaced_action_retained': old_action.name if old_action else None, 'sample_rate': sample_rate, 'frames': len(frames), 'bones': len(clip.bindings), 'hierarchy_nodes': len(nodes), 'source_hierarchy': str(source_hierarchy), 'source_local_frames': str(source_local_frames), 'source_hierarchy_sha256': file_hash(source_hierarchy), 'source_local_frames_sha256': file_hash(source_local_frames), 'rest_sha256': rest_hash, 'bind_world_calibration': calibration_receipt, 'preflight': preflight, 'affine_targets': affine_receipt, 'maximum_basis_trs_residual': maximum_basis_trs_residual, 'affine_representation': 'Only non-TRS targets use paired SVD TRS objects with continuous axis permutations/signs and world CopyTransforms; original bone rest and weights preserved; sampled integer-frame validation does not prove between-frame native interpolation.', 'maximum_matrix_error': max(cast(float, row['maximum_matrix_error']) for row in checks), 'per_frame_checks': checks, 'bone_mapping': [{'source_index': binding.source_index, 'source_path': binding.source_path, 'bone_name': binding.bone_name, 'K_row_major': [list(row) for row in binding.correction]} for binding in clip.bindings], 'source_space': source_space, 'validation_boundary': 'Actual Blender pose matrices match merged source targets; runtime corrections and visual/game parity are separate evidence. No save, mesh edit, shape-key edit or additional RootMotion application.'}


def validate_attached_animation(armature: bpy.types.Object, binding_receipt: dict[str, object], frame_indices: tuple[int, ...], matrix_tolerance: float) -> dict[str, object]:
    if binding_receipt['format'] != 'EndfieldAnimationBindingReceipt/1' or binding_receipt['armature'] != armature.name:
        raise ValueError('Binding receipt does not identify this exact rig')
    hierarchy = Path(text(binding_receipt['source_hierarchy'], 'hierarchy'))
    frames_path = Path(text(binding_receipt['source_local_frames'], 'frames'))
    if file_hash(hierarchy) != binding_receipt['source_hierarchy_sha256'] or file_hash(frames_path) != binding_receipt['source_local_frames_sha256'] or rig_rest_hash(armature) != binding_receipt['rest_sha256']:
        raise ValueError('Source frames or rig rest changed since Action attachment')
    if not armature.animation_data or not armature.animation_data.action or armature.animation_data.action.name != binding_receipt['action_name']:
        raise ValueError('Expected saved source Action is not active on the exact rig')
    nodes, frames = read_source(hierarchy, frames_path, float(binding_receipt['sample_rate']))
    bindings = []
    for raw_binding in rows(binding_receipt['bone_mapping'], 'bone mapping'):
        row = fields(raw_binding, 'bone binding')
        correction = Matrix(tuple(numbers(part, 4, 'correction row') for part in rows(row['K_row_major'], 'K')))
        if len(correction) != 4:
            raise ValueError('Expected 4x4 correction')
        index = integer(row['source_index'], 'source index')
        path = text(row['source_path'], 'source path')
        if nodes[index].path != path:
            raise ValueError('Stored binding source path/index mismatch')
        bindings.append(BoneBinding(index, path, text(row['bone_name'], 'bone name'), correction))
    clip = ClipData(nodes, frames, tuple(bindings))
    validate_affine_targets(armature, rows(binding_receipt.get('affine_targets', []), 'affine targets'), len(frames))
    checks = []
    for index in frame_indices:
        if type(index) is not int or not 1 <= index <= len(frames):
            raise ValueError(f'Frame outside actual clip: {index}')
        bpy.context.scene.frame_set(index)
        bpy.context.view_layer.update()
        checks.append(pose_error(armature, targets(clip, frames[index - 1]), index, matrix_tolerance))
    if not checks:
        raise ValueError('Explicit validation frame list is empty')
    return {'format': 'EndfieldAttachedAnimationValidation/1', 'status': 'saved_active_action_sample_matrix_verified', 'armature': armature.name, 'action_name': binding_receipt['action_name'], 'checked_frames': list(frame_indices), 'maximum_matrix_error': max(cast(float, row['maximum_matrix_error']) for row in checks), 'checks': checks}
