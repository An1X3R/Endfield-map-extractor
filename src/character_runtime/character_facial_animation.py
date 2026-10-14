"""Play source facial curves using an explicit author-approved TRS sum convention.

Source curve tangents remain editable in a separate owner Action. Bone animation
is sampled in an independent copy of the original body Action. This reconstructs
an author playback baseline, not the unknown native facial runtime consumer.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import math
from pathlib import Path
from time import perf_counter
import zlib

import bpy
from mathutils import Matrix, Vector

import basic_expression_shape_baker as baker
import character_animation_binding as binding
from character_facial_curves import FacialAnimation, evaluate_curve, read_facial_animation_probe


@dataclass(frozen=True)
class FacialDelta:
    control: str
    source_index: int
    bone_id: int
    base_euler: Vector
    position: Vector
    rotation: Vector
    scale: Vector
    endpoint: Matrix


@dataclass(frozen=True)
class FacialPlayback:
    clip: binding.ClipData
    animation: FacialAnimation
    deltas: tuple[FacialDelta, ...]
    body_indices: tuple[int, ...]
    sample_rate: float


def _decode(expression_table: Path, source_audit: Path, armature: bpy.types.Object) -> baker.DecodedSource:
    table, payload, _ = baker._read_json(expression_table, 'expression table')
    audit, audit_payload, _ = baker._read_json(source_audit, 'source audit')
    old_action = armature.animation_data.action if armature.animation_data else None
    original = {bone.name: (bone.rotation_mode, bone.matrix_basis.copy()) for bone in armature.pose.bones}
    try:
        if armature.animation_data:
            armature.animation_data.action = None
        for bone in armature.pose.bones:
            bone.matrix_basis = Matrix.Identity(4)
        bpy.context.view_layer.update()
        return baker._decode_source(table, audit, payload, audit_payload, armature,
                                    baker.BASIC_EXPRESSION_DEFINITIONS)
    finally:
        for bone in armature.pose.bones:
            bone.rotation_mode, bone.matrix_basis = original[bone.name]
        if armature.animation_data:
            armature.animation_data.action = old_action
        bpy.context.view_layer.update()


def _read_playback(armature: bpy.types.Object, expected_character_id: str, expression_table: Path,
                   source_audit: Path, source_hierarchy: Path, source_local_frames: Path,
                   facial_probe: Path, facial_asset_path: str, facial_cab: str, facial_path_id: str,
                   body_frame_indices: tuple[int, ...], body_policy: str, sample_rate: float) -> FacialPlayback:
    nodes, frames = binding.read_source(source_hierarchy, source_local_frames, sample_rate)
    bindings = binding.bind_rest(armature, nodes)
    source = _decode(expression_table, source_audit, armature)
    if source.character_id != expected_character_id:
        raise ValueError('Expression table does not identify the explicitly selected character')
    animation = read_facial_animation_probe(str(facial_probe), facial_asset_path, facial_cab, facial_path_id)
    if animation.additive_clip or animation.override_clip:
        raise ValueError('Native additive/override clip flags are unsupported by this explicit author baseline')
    count = round(animation.duration * sample_rate) + 1
    if abs((count - 1) / sample_rate - animation.duration) > 1.0e-6 or len(body_frame_indices) != count:
        raise ValueError('Explicit body frame schedule must cover exactly the facial duration at the author sample rate')
    if any(type(index) is not int or not 1 <= index <= len(frames) for index in body_frame_indices):
        raise ValueError('Explicit body frame index is outside the supplied source clip')
    if body_policy == 'source_body_window':
        if body_frame_indices != tuple(range(body_frame_indices[0], body_frame_indices[0] + count)):
            raise ValueError('Source body window must contain consecutive source frame indices')
    elif body_policy == 'explicit_held_source_body_frame':
        if len(set(body_frame_indices)) != 1:
            raise ValueError('Held body policy requires one explicit repeated source frame index')
    else:
        raise ValueError(f'Unsupported explicit body playback policy: {body_policy}')
    names = tuple(curve.control_name for curve in animation.curves)
    deltas = decode_source_deltas(source, nodes, names)
    if not deltas:
        raise ValueError('Selected facial clip has no source skeletal mappings')
    return FacialPlayback(binding.ClipData(nodes, frames, bindings), animation, deltas, body_frame_indices, sample_rate)


def decode_source_deltas(source: baker.DecodedSource, nodes: tuple[binding.SourceNode, ...],
                         names: tuple[str, ...]) -> tuple[FacialDelta, ...]:
    """Resolve exact source controls through the existing Avatar and rest checks."""
    if len(set(names)) != len(names):
        raise ValueError('Duplicate source control names require a separate explicit combination rule')
    if 'native_runtime_consumer_applied' in names:
        raise ValueError('Source control name collides with owner metadata')
    by_relative = {node.path.split('/', 1)[1]: node for node in nodes if '/' in node.path}
    deltas = []
    for name in sorted(names):
        control = source.control_rows.get(name)
        if control is None:
            raise ValueError(f'Facial animation control is absent from this exact character table: {name}')
        record = source.mapping_records[baker._integer(control['rid'], 'control RID')]
        data = baker._record(record['data'], 'mapping data')
        if record['class'] != 'SkeletalMorphMappingData' or data['id'] != control['index']:
            raise ValueError(f'Control is not an exact skeletal mapping: {name}')
        if (baker._integer(data['nameHash'], 'name hash') & 0xffffffff) != zlib.crc32(name.encode('utf8')):
            raise ValueError(f'Control name CRC does not match its source mapping: {name}')
        seen = set()
        for value in baker._array(data['bones'], 'mapping bones'):
            row = baker._record(value, 'mapping bone')
            bone_id = baker._integer(row['boneID'], 'bone ID')
            if bone_id in seen:
                raise ValueError(f'Duplicate bone ID in source control: {name}/{bone_id}')
            seen.add(bone_id)
            path = source.rig_data.path_by_bone_id[bone_id]
            base = source.rig_data.base_pose_by_bone_id[bone_id]
            if row['boneNameHash'] != base['boneNameHash']:
                raise ValueError(f'Control/base pose bone hash differs: {name}/{bone_id}')
            node = by_relative[path]
            source_row = source.rig_data.source_rows_by_path[path]
            expected_rest = binding.source_matrix(source_row, path)
            if baker._matrix_error(node.rest, expected_rest) > 5.0e-6:
                raise ValueError(f'Body hierarchy rest differs from the exact expression source audit: {path}')
            position = baker._vector3(row['position'], 'position delta')
            deltas.append(FacialDelta(name, node.index, bone_id, baker._vector3(base['rotation'], 'base Euler'),
                                      Vector((-position.x, position.y, position.z)),
                                      baker._vector3(row['rotation'], 'Euler delta'),
                                      baker._vector3(row['scale'], 'scale delta'),
                                      baker._morph_target_matrix(base, row, name)))
    return tuple(deltas)


def _weights(playback: FacialPlayback, frame_index: int) -> dict[str, float]:
    time = frame_index / playback.sample_rate
    return {curve.control_name: evaluate_curve(curve.keys, time) for curve in playback.animation.curves}


def _combined_frame(playback: FacialPlayback, frame_index: int, deltas: tuple[FacialDelta, ...]) -> binding.SourceFrame:
    body = playback.clip.frames[playback.body_indices[frame_index] - 1]
    weights = _weights(playback, frame_index)
    locals_ = list(body.locals)
    grouped: dict[int, list[FacialDelta]] = {}
    for delta in deltas:
        grouped.setdefault(delta.source_index, []).append(delta)
    for index, entries in grouped.items():
        ordered = sorted(entries, key=lambda item: item.control)
        position = Vector(tuple(math.fsum(weights[row.control] * row.position[axis] for row in ordered) for axis in range(3)))
        rotation = Vector(tuple(math.fsum(weights[row.control] * row.rotation[axis] for row in ordered) for axis in range(3)))
        scale = Vector(tuple(math.fsum(weights[row.control] * row.scale[axis] for row in ordered) for axis in range(3)))
        locals_[index] = _weighted_local(body.locals[index], ordered[0].base_euler, position, rotation, scale,
                                         playback.clip.nodes[index].path)
    return binding.SourceFrame(frame_index / playback.sample_rate, tuple(locals_))


def _weighted_local(body: Matrix, base: Vector, position: Vector, rotation: Vector, scale: Vector, path: str) -> Matrix:
    if all(component == 0.0 for vector in (position, rotation, scale) for component in vector):
        return body.copy()
    location, quaternion, body_scale = body.decompose()
    delta_quaternion = baker._morph_euler_quaternion(base).inverted() @ baker._morph_euler_quaternion(base + rotation)
    result_scale = Vector(tuple(body_scale[axis] * (1.0 + scale[axis]) for axis in range(3)))
    if abs(math.prod(result_scale)) < 1.0e-10:
        raise ValueError(f'Weighted facial scale is singular: path={path}')
    return Matrix.LocRotScale(location + position, (quaternion @ delta_quaternion).normalized(), result_scale)


def _combination_audit(playback: FacialPlayback) -> dict[str, object]:
    zero = Vector((0.0, 0.0, 0.0))
    zero_error = 0.0
    endpoint_rows = []
    grouped: dict[int, list[FacialDelta]] = {}
    for delta in playback.deltas:
        node = playback.clip.nodes[delta.source_index]
        grouped.setdefault(node.index, []).append(delta)
        actual = _weighted_local(node.rest, delta.base_euler, delta.position, delta.rotation, delta.scale, node.path)
        endpoint_rows.append({'control': delta.control, 'source_path': node.path,
                              'maximum_local_matrix_difference_from_basic_endpoint': baker._matrix_error(actual, delta.endpoint),
                              'position_difference_m': float((actual.translation - delta.endpoint.translation).length),
                              'rotation_dot_error': 1.0 - min(1.0, abs(actual.to_quaternion().dot(delta.endpoint.to_quaternion())))})
        for source_index in set(playback.body_indices):
            body = playback.clip.frames[source_index - 1].locals[node.index]
            zero_error = max(zero_error, baker._matrix_error(body, _weighted_local(body, delta.base_euler, zero, zero, zero, node.path)))
    weights = tuple(_weights(playback, index) for index in range(len(playback.body_indices)))
    overlaps = [{'source_path': playback.clip.nodes[index].path, 'controls': sorted(delta.control for delta in deltas),
                 'maximum_sum_of_absolute_weights': max(math.fsum(abs(row[delta.control]) for delta in deltas) for row in weights),
                 'maximum_absolute_summed_euler_delta_degrees': max(float(Vector(tuple(math.fsum(row[delta.control] * delta.rotation[axis] for delta in deltas) for axis in range(3))).length) for row in weights)}
                for index, deltas in grouped.items() if len(deltas) > 1]
    return {'zero_weight_maximum_matrix_error': zero_error, 'single_control_endpoint_comparison': endpoint_rows,
            'endpoint_difference_reason': 'Neutral-preserving body/rest relative delta versus the earlier absolute basePose endpoint; measured basePose/Prefab quaternion and position residuals remain visible. Neither proves the native consumer.',
            'overlapping_control_weight_audit': overlaps, 'weight_clamping_or_normalization_applied': False}


def _action_hash(action: bpy.types.Action) -> str:
    digest = hashlib.sha256()
    for curve in action.fcurves:
        digest.update(repr((curve.data_path, curve.array_index, curve.extrapolation, curve.mute)).encode())
        for point in curve.keyframe_points:
            digest.update(repr((tuple(point.co), tuple(point.handle_left), tuple(point.handle_right), point.interpolation,
                                point.handle_left_type, point.handle_right_type)).encode())
    return digest.hexdigest()


def _control_action(animation: FacialAnimation, owner_name: str, action_name: str, sample_rate: float) -> dict[str, object]:
    if bpy.data.objects.get(owner_name) or bpy.data.actions.get(action_name):
        raise ValueError('Source control owner or Action name already exists')
    owner = bpy.data.objects.new(owner_name, None)
    bpy.context.scene.collection.objects.link(owner)
    owner.hide_render = True
    owner['native_runtime_consumer_applied'] = False
    owner.animation_data_create()
    action = bpy.data.actions.new(action_name)
    action.use_fake_user = True
    owner.animation_data.action = action
    for curve in animation.curves:
        if curve.control_name == 'native_runtime_consumer_applied':
            raise ValueError('Source control name collides with owner metadata')
        owner[curve.control_name] = 0.0
        path = '[' + json.dumps(curve.control_name) + ']'
        for key in curve.keys:
            owner[curve.control_name] = key.value
            owner.keyframe_insert(data_path=path, frame=1.0 + key.time * sample_rate)
        fc = action.fcurves.find(path)
        if fc is None or len(fc.keyframe_points) != len(curve.keys):
            raise ValueError(f'Blender did not create the source owner curve in its active Action slot: {path}')
        for index, key in enumerate(curve.keys):
            point = fc.keyframe_points[index]
            frame = 1.0 + key.time * sample_rate
            if len(curve.keys) == 1:
                point.co = (frame, key.value)
                point.interpolation = 'CONSTANT'
                continue
            left_dt = key.time - curve.keys[index - 1].time if index else curve.keys[1].time - key.time
            right_dt = curve.keys[index + 1].time - key.time if index + 1 < len(curve.keys) else key.time - curve.keys[index - 1].time
            point.co = (frame, key.value)
            point.interpolation = 'BEZIER'
            point.handle_left_type = point.handle_right_type = 'FREE'
            point.handle_left = (frame - left_dt * sample_rate / 3.0, key.value - key.in_slope * left_dt / 3.0)
            point.handle_right = (frame + right_dt * sample_rate / 3.0, key.value + key.out_slope * right_dt / 3.0)
        fc.update()
    validation = _validate_control_action(animation, owner_name, action_name, sample_rate)
    return {'owner': owner.name, 'action': action.name, 'curves': len(animation.curves),
            'keys': sum(len(curve.keys) for curve in animation.curves), **validation,
            'tangent_encoding': 'FREE Bezier handles at adjacent segment dt/3; value slopes remain per second',
            'scope': 'Original duration only; no native extrapolation, loop, auto-blink scheduler or consumer claimed'}


def _validate_control_action(animation: FacialAnimation, owner_name: str, action_name: str, sample_rate: float) -> dict[str, object]:
    started = perf_counter()
    owner = bpy.data.objects.get(owner_name)
    if owner is None or not owner.animation_data or not owner.animation_data.action or owner.animation_data.action.name != action_name:
        raise ValueError('Expected source curve owner/Action is missing or not active')
    action = owner.animation_data.action
    if len(action.fcurves) != len(animation.curves):
        raise ValueError('Source control curve count differs from the original clip')
    scene = bpy.context.scene
    original_time = (scene.frame_current, scene.frame_subframe)
    maximum = 0.0
    owner_error = 0.0
    checked_times = sorted({0.0, animation.duration / 2.0, animation.duration,
                            *(max(curve.keys, key=lambda key: key.value).time for curve in animation.curves),
                            *(min(curve.keys, key=lambda key: key.value).time for curve in animation.curves)})
    try:
        for index in range(round(animation.duration * sample_rate * 2) + 1):
            time = index / (sample_rate * 2.0)
            frame = 1.0 + time * sample_rate
            for curve in animation.curves:
                path = '[' + json.dumps(curve.control_name) + ']'
                matches = tuple(fc for fc in action.fcurves if fc.data_path == path and fc.array_index == 0)
                if len(matches) != 1 or len(matches[0].keyframe_points) != len(curve.keys):
                    raise ValueError(f'Source control curve/key identity mismatch: {curve.control_name}')
                expected = evaluate_curve(curve.keys, time)
                maximum = max(maximum, abs(matches[0].evaluate(frame) - expected))
        for time in checked_times:
            frame = 1.0 + time * sample_rate
            scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))
            for curve in animation.curves:
                expected = evaluate_curve(curve.keys, time)
                owner_error = max(owner_error, abs(float(owner[curve.control_name]) - expected))
    finally:
        scene.frame_set(original_time[0], subframe=original_time[1])
    if maximum > 2.0e-5 or owner_error > 2.0e-5:
        raise ValueError(f'Source control Action differs from original Hermite: curve_error={maximum}, owner_error={owner_error}')
    return {'maximum_half_frame_value_error': maximum, 'maximum_live_owner_sample_error': owner_error,
            'live_owner_sample_times_seconds': checked_times,
            'live_owner_curve_sample_count': len(checked_times) * len(animation.curves),
            'half_frame_curve_sample_count': (round(animation.duration * sample_rate * 2) + 1) * len(animation.curves),
            'validation_seconds': perf_counter() - started,
            'live_owner_scope': 'First/middle/end and every control maximum/minimum key value; FCurve/Hermite comparison still covers every half frame'}


def attach_source_facial_animation(armature: bpy.types.Object, expected_character_id: str, expression_table: Path,
                                  source_audit: Path, source_hierarchy: Path, source_local_frames: Path,
                                  facial_probe: Path, facial_asset_path: str, facial_cab: str, facial_path_id: str,
                                  body_frame_indices: tuple[int, ...], body_policy: str, action_name: str,
                                  control_owner_name: str, control_action_name: str, sample_rate: float,
                                  matrix_tolerance: float) -> dict[str, object]:
    started = perf_counter()
    if not math.isfinite(matrix_tolerance) or matrix_tolerance <= 0:
        raise ValueError('Matrix tolerance must be positive and finite')
    if bpy.data.actions.get(action_name) or bpy.data.objects.get(control_owner_name) or bpy.data.actions.get(control_action_name):
        raise ValueError('Explicit destination Action or control owner already exists')
    playback = _read_playback(armature, expected_character_id, expression_table, source_audit, source_hierarchy,
                              source_local_frames, facial_probe, facial_asset_path, facial_cab, facial_path_id,
                              body_frame_indices, body_policy, sample_rate)
    source_read_seconds = perf_counter() - started
    old_action = armature.animation_data.action if armature.animation_data else None
    if old_action is None:
        raise ValueError('An original body Action is required for an independent playback copy')
    original_action_hash = _action_hash(old_action)
    old_action_fake_user_before = old_action.use_fake_user
    old_action.use_fake_user = True
    rest_hash = binding.rig_rest_hash(armature)
    # Resample the approved explicit body window; no implicit hold or loop is used.
    action = old_action.copy()
    action.name = action_name
    action.use_fake_user = True
    for curve in tuple(action.fcurves):
        if curve.data_path.startswith('pose.bones['):
            action.fcurves.remove(curve)
    armature.animation_data.action = action
    checks = []
    previous = {}
    order_error = 0.0
    bone_started = perf_counter()
    for index in range(len(body_frame_indices)):
        bpy.context.scene.frame_set(index + 1)
        frame = _combined_frame(playback, index, playback.deltas)
        reversed_frame = _combined_frame(playback, index, tuple(reversed(playback.deltas)))
        order_error = max(order_error, max(baker._matrix_error(a, b) for a, b in zip(frame.locals, reversed_frame.locals, strict=True)))
        target = binding.targets(playback.clip, frame)
        basis = binding.basis_targets(armature, target)
        for bone in armature.pose.bones:
            location, rotation, scale = basis[bone.name].decompose()
            if bone.name in previous:
                rotation.make_compatible(previous[bone.name])
            previous[bone.name] = rotation.copy()
            bone.rotation_mode = 'QUATERNION'
            bone.location, bone.rotation_quaternion, bone.scale = location, rotation, scale
            for channel in ('location', 'rotation_quaternion', 'scale'):
                bone.keyframe_insert(data_path=channel, frame=index + 1, group=bone.name)
        bpy.context.view_layer.update()
        checks.append(binding.pose_error(armature, target, index + 1, matrix_tolerance))
    for curve in action.fcurves:
        if curve.data_path.startswith('pose.bones['):
            for key in curve.keyframe_points:
                key.interpolation = 'LINEAR'
    bone_bake_seconds = perf_counter() - bone_started
    if order_error != 0.0 or binding.rig_rest_hash(armature) != rest_hash:
        raise ValueError('Control order independence or unchanged rig rest invariant failed')
    if _action_hash(old_action) != original_action_hash:
        raise ValueError('Original body Action curve data changed during facial playback binding')
    control_started = perf_counter()
    control = _control_action(playback.animation, control_owner_name, control_action_name, sample_rate)
    control_seconds = perf_counter() - control_started
    audit_started = perf_counter()
    combination_audit = _combination_audit(playback)
    combination_seconds = perf_counter() - audit_started
    by_bone: dict[int, set[str]] = {}
    for delta in playback.deltas:
        by_bone.setdefault(delta.source_index, set()).add(delta.control)
    scene = bpy.context.scene
    scene.render.fps = round(sample_rate)
    scene.render.fps_base = scene.render.fps / sample_rate
    scene.frame_start, scene.frame_end = 1, len(body_frame_indices)
    scene.frame_set(1)
    inputs = {key: str(path.resolve(strict=True)) for key, path in (
        ('expression_table', expression_table), ('source_audit', source_audit), ('source_hierarchy', source_hierarchy),
        ('source_local_frames', source_local_frames), ('facial_probe', facial_probe))}
    return {'format': 'EndfieldSourceFacialAnimation/1', 'status': 'author_sum_baseline_all_sample_matrices_verified',
            'character_id': expected_character_id, 'armature': armature.name, 'action_name': action.name,
            'original_body_action': old_action.name, 'original_body_action_not_modified': True,
            'original_body_action_curve_sha256': original_action_hash, 'original_body_action_fake_user_before': old_action_fake_user_before,
            'original_body_action_retained_with_fake_user': old_action.use_fake_user,
            'source_control_owner': control['owner'], 'source_control_action': control['action'], 'source_control_audit': control,
            'body_policy': body_policy, 'body_frame_indices': list(body_frame_indices), 'frames': len(body_frame_indices),
            'sample_rate': sample_rate, 'author_sample_rate_not_source_asset_fps': True,
            'facial_asset_path': facial_asset_path, 'facial_cab': facial_cab, 'facial_path_id': facial_path_id,
            'facial_name': playback.animation.name, 'duration': playback.animation.duration,
            'raw_flags': {'additive': playback.animation.additive_clip, 'override': playback.animation.override_clip,
                          'pause_auto_blink': playback.animation.pause_auto_blink},
            'inputs': inputs, 'input_sha256': {key: binding.file_hash(Path(path)) for key, path in inputs.items()},
            'rest_sha256': rest_hash, 'maximum_matrix_error': max(row['maximum_matrix_error'] for row in checks),
            'per_frame_checks': checks, 'control_order_matrix_error': order_error,
            'combination_audit': combination_audit,
            'stage_seconds': {'source_read_and_rest_binding': source_read_seconds, 'bone_bake_and_all_frame_matrix_check': bone_bake_seconds,
                              'source_control_action_and_curve_check': control_seconds, 'combination_audit': combination_seconds,
                              'total': perf_counter() - started},
            'affected_bone_names': sorted(playback.clip.nodes[index].path.rsplit('/', 1)[-1] for index in by_bone),
            'overlapping_bones': [{'source_path': playback.clip.nodes[index].path, 'controls': sorted(names)}
                                  for index, names in by_bone.items() if len(names) > 1],
            'combination': 't_body+sum(w*dT); q_body*inverse(q(baseEuler))*q(baseEuler+sum(w*dEuler)); s_body*(1+sum(w*dS)); q=qz*qy*qx; no clamping or normalization',
            'boundary': 'Author-approved deterministic playback convention; native TRS combination, priority, runtime blend, corrective shapes and shader consumers remain unverified. No handler or source Action modification.'}


def validate_source_facial_animation(armature: bpy.types.Object, receipt: dict[str, object],
                                    frame_indices: tuple[int, ...], matrix_tolerance: float) -> dict[str, object]:
    if receipt['format'] != 'EndfieldSourceFacialAnimation/1' or receipt['armature'] != armature.name:
        raise ValueError('Facial receipt does not identify this exact rig')
    paths = binding.fields(receipt['inputs'], 'source inputs')
    hashes = binding.fields(receipt['input_sha256'], 'source hashes')
    for key, value in paths.items():
        if binding.file_hash(Path(binding.text(value, key))) != hashes[key]:
            raise ValueError(f'Facial playback source input changed: {key}')
    if binding.rig_rest_hash(armature) != receipt['rest_sha256']:
        raise ValueError('Facial playback rig rest changed')
    if not armature.animation_data or not armature.animation_data.action or armature.animation_data.action.name != receipt['action_name']:
        raise ValueError('Expected facial body-copy Action is not active')
    original_action = bpy.data.actions.get(receipt['original_body_action'])
    if original_action is None or _action_hash(original_action) != receipt['original_body_action_curve_sha256']:
        raise ValueError('Original retained body Action is missing or its curve data changed')
    playback = _read_playback(armature, binding.text(receipt['character_id'], 'character'),
                              Path(paths['expression_table']), Path(paths['source_audit']), Path(paths['source_hierarchy']),
                              Path(paths['source_local_frames']), Path(paths['facial_probe']), receipt['facial_asset_path'],
                              receipt['facial_cab'], receipt['facial_path_id'], tuple(receipt['body_frame_indices']),
                              receipt['body_policy'], float(receipt['sample_rate']))
    scene = bpy.context.scene
    original_time = (scene.frame_current, scene.frame_subframe)
    checks = []
    try:
        for frame in frame_indices:
            if type(frame) is not int or not 1 <= frame <= len(playback.body_indices):
                raise ValueError(f'Validation frame outside explicit facial duration: {frame}')
            scene.frame_set(frame)
            target = binding.targets(playback.clip, _combined_frame(playback, frame - 1, playback.deltas))
            checks.append(binding.pose_error(armature, target, frame, matrix_tolerance))
    finally:
        scene.frame_set(original_time[0], subframe=original_time[1])
    if not checks:
        raise ValueError('Explicit facial validation frames must be nonempty')
    control_validation = _validate_control_action(playback.animation, receipt['source_control_owner'], receipt['source_control_action'], playback.sample_rate)
    return {'status': 'active_facial_body_copy_sample_matrices_verified', 'checks': checks, 'source_control_validation': control_validation,
            'maximum_matrix_error': max(row['maximum_matrix_error'] for row in checks)}
