"""Evaluate explicitly selected native facial bone-writeback branches in export coordinates.

Source pose weighting and outer Animator-layer writeback are separate inputs.
These helpers do not infer the game's active tracker, select an emotion or
schedule automatic blinking.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector

import basic_expression_shape_baker as baker
import character_animation_binding as binding
from character_facial_animation import FacialDelta, _decode, decode_source_deltas
from character_facial_poses import FacialPose, source_pose_weights


@dataclass(frozen=True)
class NativeFacialBase:
    source_index: int
    position: Vector
    euler: Vector
    scale: Vector


@dataclass(frozen=True)
class NativeFacialContext:
    source: baker.DecodedSource
    clip: binding.ClipData
    bases: tuple[NativeFacialBase, ...]
    character_id: str
    corrective_map: tuple[tuple[int, int], ...]


def read_native_facial_context(armature: bpy.types.Object, character_id: str,
                               expression_table: Path, source_audit: Path,
                               source_hierarchy: Path, source_local_frames: Path,
                               sample_rate: float) -> NativeFacialContext:
    """Reuse exact Avatar, hierarchy, bind-rest and expression-table validation."""
    nodes, frames = binding.read_source(source_hierarchy, source_local_frames, sample_rate)
    source = _decode(expression_table, source_audit, armature)
    if source.character_id != character_id:
        raise ValueError(f'Native facial context character mismatch: expected={character_id}, actual={source.character_id}')
    by_path = {node.path.split('/', 1)[1]: node for node in nodes if '/' in node.path}
    bases: list[NativeFacialBase] = []
    for bone_id, base in source.rig_data.base_pose_by_bone_id.items():
        path = source.rig_data.path_by_bone_id[bone_id]
        node = by_path[path]
        audited_rest = binding.source_matrix(source.rig_data.source_rows_by_path[path], path)
        if baker._matrix_error(node.rest, audited_rest) > 5.0e-6:
            raise ValueError(f'Native face/body rest mismatch: bone_id={bone_id}, path={path}')
        position = baker._vector3(base['position'], path)
        bases.append(NativeFacialBase(node.index, Vector((-position.x, position.y, position.z)),
                                     baker._vector3(base['rotation'], path), baker._vector3(base['scale'], path)))
    if not bases or len({base.source_index for base in bases}) != len(bases):
        raise ValueError('Native facial base table must bind distinct source bones')
    clip = binding.ClipData(nodes, frames, binding.bind_rest(armature, nodes))
    table, _, _ = baker._read_json(expression_table, 'native corrective map')
    raw_map = table['raw']['asset_prefix']['data']['blendShapeMorphHashMap']
    hashes = baker._array(raw_map['_keyData'], 'corrective hashes')
    indices = baker._array(raw_map['_valueData'], 'corrective indices')
    if len(hashes) != len(indices):
        raise ValueError('Native corrective hash/index arrays have different lengths')
    corrective = tuple((baker._integer(key, 'corrective hash') & 0xffffffff,
                        baker._integer(value, 'native shape index')) for key, value in zip(hashes, indices, strict=True))
    if len({key for key, _ in corrective}) != len(corrective) or any(index < 0 for _, index in corrective):
        raise ValueError('Native corrective map has duplicate hashes or negative indices')
    return NativeFacialContext(source, clip, tuple(bases), character_id, corrective)


def _native_weights(context: NativeFacialContext, pose: FacialPose) -> dict[str, float]:
    weights = source_pose_weights(pose)
    if pose.additive_pose != 0:
        raise ValueError('Native facial source evaluation requires pose.additive_pose=0; outer additive-layer writeback is selected separately')
    result: dict[str, float] = {}
    for name, value in weights.items():
        row = context.source.control_rows.get(name)
        if row is None:
            raise ValueError(f'Facial pose control is absent from the selected Avatar: {name}')
        record = context.source.mapping_records[baker._integer(row['rid'], name)]
        if record['class'] != 'SkeletalMorphMappingData':
            raise ValueError(f'Native bone pose requires a skeletal mapping: control={name}, class={record["class"]}')
        part = baker._integer(record['data']['partType'], name)
        if not 0 <= part <= 255 or not math.isfinite(value):
            raise ValueError(f'Invalid native control input: control={name}, part={part}, value={value}')
        result[name] = 0.0 if part == 0 else (value if part == 32 else min(1.0, max(0.0, value)))
    return result


def native_absolute_pose_frame(context: NativeFacialContext, pose: FacialPose,
                               body_frame_index: int) -> binding.SourceFrame:
    """Apply base plus weighted TRS deltas to configured face bones only."""
    body = _source_body_frame(context, body_frame_index)
    locals_ = list(body.locals)
    for value in _native_pose_components(context, pose):
        # Unity FromMaya uses Z(-z)Y(-y)X(x); export X-reflection yields Z(z)Y(y)X(x).
        locals_[value.source_index] = Matrix.LocRotScale(value.position,
            baker._morph_euler_quaternion(value.euler), value.scale)
    return binding.SourceFrame(body.time, tuple(locals_))


def _source_body_frame(context: NativeFacialContext, body_frame_index: int) -> binding.SourceFrame:
    """Select the explicit one-based source body frame."""
    if type(body_frame_index) is not int or not 1 <= body_frame_index <= len(context.clip.frames):
        raise ValueError(f'Explicit body frame is outside the source clip: {body_frame_index}')
    return context.clip.frames[body_frame_index - 1]


def _native_pose_components(context: NativeFacialContext, pose: FacialPose) -> tuple[NativeFacialBase, ...]:
    """Evaluate source TRS components without losing signed scale through matrices."""
    weights = _native_weights(context, pose)
    deltas = decode_source_deltas(context.source, context.clip.nodes, tuple(weights))
    grouped: dict[int, list[FacialDelta]] = {}
    for delta in deltas:
        grouped.setdefault(delta.source_index, []).append(delta)
    result: list[NativeFacialBase] = []
    for base in context.bases:
        entries = grouped.get(base.source_index, [])
        position_delta, euler_delta, scale_delta = (
            Vector(tuple(math.fsum(weights[row.control] * getattr(row, field)[axis] for row in entries)
                         for axis in range(3))) for field in ('position', 'rotation', 'scale'))
        scale = base.scale + scale_delta
        if not all(math.isfinite(value) for value in scale) or abs(math.prod(scale)) < 1.0e-10:
            raise ValueError(f'Native facial result has invalid scale: node={base.source_index}, scale={tuple(scale)}')
        result.append(NativeFacialBase(base.source_index, base.position + position_delta,
                                      base.euler + euler_delta, scale))
    return tuple(result)


def _checked_apply_weight(apply_weight: float) -> float:
    """Reject invalid external weights without changing native finite boundaries."""
    if type(apply_weight) not in (int, float) or not math.isfinite(apply_weight):
        raise ValueError(f'Native facial apply weight must be finite: {apply_weight!r}')
    return float(apply_weight)


def _native_quaternion_lerp(left: Quaternion, right: Quaternion, weight: float) -> Quaternion:
    """Match Unity Quaternion.Lerp with shortest-hemisphere normalized interpolation."""
    sign = -1.0 if left.dot(right) < 0.0 else 1.0
    return Quaternion(tuple(a + weight * (sign * b - a)
        for a, b in zip(left, right, strict=True))).normalized()


def native_weighted_absolute_pose_frame(context: NativeFacialContext, pose: FacialPose,
                                        body_frame_index: int, apply_weight: float) -> binding.SourceFrame:
    """Apply the explicit managed TransformAccess branch to a selected body frame.

    Native partial translation and scale add the pose-minus-base delta to the
    current transform. Rotation uses shortest-path normalized linear blending.
    This differs from interpolating the entire current TRS toward the target.
    Dirty tracking, Animator buffer writes and tracker scheduling are not inferred.
    """
    weight = _checked_apply_weight(apply_weight)
    body = _source_body_frame(context, body_frame_index)
    values = _native_pose_components(context, pose)
    if weight <= 0.0:
        return body
    if weight >= 1.0:
        return native_absolute_pose_frame(context, pose, body_frame_index)
    locals_ = list(body.locals)
    for base, target in zip(context.bases, values, strict=True):
        position, rotation, scale = body.locals[base.source_index].decompose()
        if body.locals[base.source_index].determinant() <= 0.0:
            raise ValueError(f'Weighted facial writeback requires source signed TRS for reflected bones: node={base.source_index}')
        target_rotation = baker._morph_euler_quaternion(target.euler)
        blended_rotation = _native_quaternion_lerp(rotation, target_rotation, weight)
        result_scale = scale + weight * (target.scale - base.scale)
        if not all(math.isfinite(value) for value in result_scale) or abs(math.prod(result_scale)) < 1.0e-10:
            raise ValueError(f'Native weighted facial scale is singular: node={base.source_index}, weight={weight}')
        locals_[base.source_index] = Matrix.LocRotScale(
            position + weight * (target.position - base.position), blended_rotation, result_scale)
    return binding.SourceFrame(body.time, tuple(locals_))


def native_additive_layer_pose_frame(context: NativeFacialContext, pose: FacialPose,
                                     body_frame_index: int, apply_weight: float) -> binding.SourceFrame:
    """Apply the explicitly selected outer additive-Animator-layer bone branch.

    This models runtime_additiveAnimatorLayer independently of pose.additive_pose.
    The source pose must remain nonadditive so its source control weights keep
    their established meaning. Translation and rotation are written; scale is
    preserved from the current source frame. Dirty tracking and scheduling are
    outside this frame adapter.
    """
    weight = _checked_apply_weight(apply_weight)
    body = _source_body_frame(context, body_frame_index)
    values = _native_pose_components(context, pose)
    if weight <= 0.0:
        return body
    locals_ = list(body.locals)
    identity = Quaternion((1.0, 0.0, 0.0, 0.0))
    for base, target in zip(context.bases, values, strict=True):
        source_local = body.locals[base.source_index]
        position, rotation, scale = source_local.decompose()
        if source_local.determinant() <= 0.0:
            raise ValueError(f'Additive facial writeback requires source signed TRS for reflected bones: node={base.source_index}')
        position_delta = target.position - base.position
        rotation_delta = baker._morph_euler_quaternion(target.euler - base.euler)
        if weight >= 1.0:
            result_position = position + position_delta
            result_rotation = rotation @ rotation_delta
        else:
            result_position = position + weight * position_delta
            weighted_delta = _native_quaternion_lerp(identity, rotation_delta, weight)
            result_rotation = rotation @ weighted_delta
        locals_[base.source_index] = Matrix.LocRotScale(result_position, result_rotation, scale)
    return binding.SourceFrame(body.time, tuple(locals_))


def native_corrective_weights(context: NativeFacialContext, pose: FacialPose) -> dict[int, float]:
    """Retain the established full-weight native eye-control entry point."""
    return native_weighted_corrective_weights(context, pose, 1.0)


def native_weighted_corrective_weights(context: NativeFacialContext, pose: FacialPose,
                                       apply_weight: float) -> dict[int, float]:
    """Return eye-control sums for an explicit fresh corrective accumulation pass."""
    weight = _checked_apply_weight(apply_weight)
    source_weights = source_pose_weights(pose)
    _native_weights(context, pose)
    mapping = dict(context.corrective_map)
    grouped: dict[int, list[float]] = {index: [] for index in mapping.values()}
    for name, value in source_weights.items():
        row = context.source.control_rows[name]
        data = context.source.mapping_records[baker._integer(row['rid'], name)]['data']
        part = baker._integer(data['partType'], name)
        name_hash = baker._integer(data['nameHash'], name) & 0xffffffff
        if part in (1, 2) and value * weight > 0.0 and name_hash in mapping:
            grouped[mapping[name_hash]].append(value * weight)
    # Unity sets Clamp01(sum)*100 on a verified single frame with fullWeight=100.
    return {index: min(1.0, max(0.0, math.fsum(values))) for index, values in grouped.items()}
