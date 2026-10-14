"""Bake explicitly authenticated native facial writes into retained authoring Actions.

The two replay formats select verified writeback kernels, not an emotion tracker
or Blender NLA blending. Integer frames are explicit full-pose snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Callable

import bpy
from mathutils import Matrix

import character_animation_binding as binding
from character_facial_native import (NativeFacialContext, native_additive_layer_pose_frame,
    native_weighted_absolute_pose_frame, native_weighted_corrective_weights,
    read_native_facial_context)
from character_facial_poses import FacialPose, read_facial_emotion_probe


@dataclass(frozen=True)
class SourceReference:
    path: Path
    sha256: str


@dataclass(frozen=True)
class ReplayFrame:
    source_body_frame: int
    apply_weight: float


@dataclass(frozen=True)
class ExpressionReplay:
    manifest: SourceReference
    character_id: str
    armature: str
    action_name: str
    source_blend: SourceReference
    source_build_receipt: SourceReference
    source_audit: SourceReference
    expression_table: SourceReference
    body_receipt: SourceReference
    mesh_channel_probe: SourceReference
    emotion_probe: SourceReference
    emotion_asset: str
    emotion_cab: str
    emotion_path_id: int
    frames: tuple[ReplayFrame, ...]
    output: Path


@dataclass(frozen=True)
class AbsoluteExpressionReplay:
    source: ExpressionReplay


@dataclass(frozen=True)
class AdditiveExpressionReplay:
    source: ExpressionReplay


NativeExpressionReplay = AbsoluteExpressionReplay | AdditiveExpressionReplay


def _record(value: object, label: str) -> dict[str, object]:
    return binding.fields(value, label)


def _reference(root: Path, value: object, label: str) -> SourceReference:
    row = _record(value, label)
    path = (root / binding.text(row['path'], label + '.path')).resolve(strict=True)
    digest = binding.text(row['sha256'], label + '.sha256')
    if re.fullmatch('[0-9a-f]{64}', digest) is None:
        raise ValueError(f'Expected lowercase SHA256: {label}')
    if binding.file_hash(path) != digest:
        raise ValueError(f'Native expression source changed: {label}, path={path}')
    return SourceReference(path, digest)


def read_native_expression_replay(path: Path) -> NativeExpressionReplay:
    path = path.resolve(strict=True)
    row = _record(json.loads(path.read_text(encoding='utf-8-sig')), 'native expression replay')
    format_ = row['format']
    if format_ not in ('EndfieldNativeExpressionAbsoluteReplay/1', 'EndfieldNativeExpressionAdditiveReplay/1'):
        raise ValueError(f'Unsupported native expression replay format: {format_}')
    refs = _record(row['sources'], 'replay sources')
    emotion = _record(row['emotion'], 'replay emotion identity')
    frames: list[ReplayFrame] = []
    for index, item in enumerate(binding.rows(row['frames'], 'replay frames')):
        frame = _record(item, f'replay frame[{index}]')
        body = binding.integer(frame['source_body_frame'], 'source body frame')
        weight = frame['apply_weight']
        if body < 1 or type(weight) not in (int, float) or not math.isfinite(weight):
            raise ValueError(f'Expected positive body frame and finite explicit weight: frame={index}')
        frames.append(ReplayFrame(body, float(weight)))
    if not frames:
        raise ValueError('Native expression replay requires explicit frames')
    source = ExpressionReplay(SourceReference(path, binding.file_hash(path)),
        binding.text(row['character_id'], 'character ID'), binding.text(row['armature'], 'armature'),
        binding.text(row['action_name'], 'action name'),
        *(_reference(path.parent, refs[key], key) for key in (
            'source_blend', 'source_build_receipt', 'source_audit', 'expression_table',
            'body_receipt', 'mesh_channel_probe', 'emotion_probe')),
        binding.text(emotion['asset_path'], 'emotion asset path'),
        binding.text(emotion['cab'], 'emotion CAB'), binding.integer(emotion['path_id'], 'emotion PathID'),
        tuple(frames), (path.parent / binding.text(row['output'], 'replay output')).resolve())
    if source.output.exists():
        raise FileExistsError(f'Native expression replay output must be new: {source.output}')
    return AbsoluteExpressionReplay(source) if format_ == 'EndfieldNativeExpressionAbsoluteReplay/1' else AdditiveExpressionReplay(source)


def verify_replay_references(source: ExpressionReplay) -> None:
    for ref in (source.manifest, source.source_blend, source.source_build_receipt,
                source.source_audit, source.expression_table, source.body_receipt,
                source.mesh_channel_probe, source.emotion_probe):
        if binding.file_hash(ref.path) != ref.sha256:
            raise ValueError(f'Native expression input changed: path={ref.path}')


def validate_loaded_replay_source(source: ExpressionReplay) -> dict[str, object]:
    verify_replay_references(source)
    if Path(bpy.data.filepath).resolve(strict=True) != source.source_blend.path:
        raise ValueError('Native expression replay source blend differs from the loaded authoring input')
    build = _record(json.loads(source.source_build_receipt.path.read_text(encoding='utf-8')), 'source build receipt')
    if (build['format'] != 'EndfieldCharacterAuthoring/1' or build['character_id'] != source.character_id
            or build['blend_sha256'] != source.source_blend.sha256
            or _record(build['identity'], 'source authoring identity')['source_audit_sha256'] != source.source_audit.sha256):
        raise ValueError('Native expression replay source build/character/audit identity mismatch')
    from character_authoring_identity import validate_authoring_identity
    validate_authoring_identity(source.source_audit.path, source.expression_table.path, None,
                               (source.body_receipt.path,))
    return build


def retained_action_signatures() -> dict[str, str]:
    """Capture existing sampled curves without changing their ownership or data."""
    result: dict[str, str] = {}
    for action in bpy.data.actions:
        curves = [(curve.data_path, curve.array_index, curve.extrapolation,
                   [(tuple(point.co), tuple(point.handle_left), tuple(point.handle_right),
                     point.interpolation, point.handle_left_type, point.handle_right_type)
                    for point in curve.keyframe_points]) for curve in action.fcurves]
        result[action.name] = hashlib.sha256(repr(curves).encode()).hexdigest()
    return result


def verify_retained_actions(expected: dict[str, str]) -> None:
    current = retained_action_signatures()
    if any(current.get(name) != digest for name, digest in expected.items()):
        raise AssertionError('Native expression authoring changed or removed a retained source Action')


def _body_sources(source: ExpressionReplay) -> tuple[Path, Path, float]:
    receipt = _record(json.loads(source.body_receipt.path.read_text(encoding='utf-8')), 'body receipt')
    rate = receipt['sample_rate']
    if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
        raise ValueError('Native expression body sample rate must be positive and finite')
    root = source.body_receipt.path.parent
    hashes = {(root / binding.text(_record(item, 'body output')['path'], 'body output path')).resolve(strict=True):
              _record(item, 'body output')['sha256'] for item in binding.rows(receipt['outputs'], 'body outputs')}
    hierarchy = (root / binding.text(receipt['source_hierarchy'], 'body hierarchy')).resolve(strict=True)
    frames = (root / binding.text(receipt['source_local_frames'], 'body frames')).resolve(strict=True)
    if any(hashes.get(path) != binding.file_hash(path) for path in (hierarchy, frames)):
        raise ValueError('Native expression body hierarchy/frames changed or are absent from the authenticated receipt')
    return hierarchy, frames, float(rate)


def _frame_json(frame: binding.SourceFrame, time: float) -> dict[str, object]:
    """Reflect the verified local matrices back to the existing binder's source TRS format."""
    reflection = Matrix.Diagonal((-1., 1., 1., 1.))
    locals_: list[dict[str, object]] = []
    for index, local in enumerate(frame.locals):
        matrix = reflection @ local @ reflection
        position, quaternion, scale = matrix.decompose()
        rebuilt = Matrix.LocRotScale(position, quaternion, scale)
        if max(abs(matrix[row][column] - rebuilt[row][column]) for row in range(4) for column in range(4)) > 5e-6:
            raise ValueError(f'Native expression snapshot is not representable as source TRS: node={index}')
        locals_.append({'position': list(position), 'rotation': [quaternion.x, quaternion.y, quaternion.z, quaternion.w],
                        'scale': list(scale)})
    return {'time': time, 'locals': locals_}


def bake_absolute_expression(replay: AbsoluteExpressionReplay, meshes: tuple[bpy.types.Object, ...]) -> dict[str, object]:
    return _bake_expression(replay.source, meshes, native_weighted_absolute_pose_frame, 'weighted_absolute')


def bake_additive_expression(replay: AdditiveExpressionReplay, meshes: tuple[bpy.types.Object, ...]) -> dict[str, object]:
    return _bake_expression(replay.source, meshes, native_additive_layer_pose_frame, 'outer_additive')


def _bake_expression(source: ExpressionReplay, meshes: tuple[bpy.types.Object, ...],
                     kernel: Callable[[NativeFacialContext, FacialPose, int, float], binding.SourceFrame],
                     branch: str) -> dict[str, object]:
    from character_facial_correctives import read_native_corrective_bindings
    inherited = validate_loaded_replay_source(source)
    rig = bpy.data.objects.get(source.armature)
    if rig is None or rig.type != 'ARMATURE':
        raise ValueError(f'Native expression source armature is missing: {source.armature}')
    hierarchy, frames, rate = _body_sources(source)
    context = read_native_facial_context(rig, source.character_id, source.expression_table.path,
        source.source_audit.path, hierarchy, frames, rate)
    emotion = read_facial_emotion_probe(source.emotion_probe.path, source.emotion_asset,
                                      source.emotion_cab, str(source.emotion_path_id))
    correctives = read_native_corrective_bindings(context, source.source_audit.path,
                                                  source.mesh_channel_probe.path, meshes)
    # Resolve and evaluate all requested inputs before creating Actions or outputs.
    composed = tuple(kernel(context, emotion.pose, frame.source_body_frame, frame.apply_weight) for frame in source.frames)
    weights = tuple(native_weighted_corrective_weights(context, emotion.pose, frame.apply_weight) for frame in source.frames)
    old_actions = retained_action_signatures()
    action_name = source.action_name + ' :: ' + rig.name
    if bpy.data.actions.get(action_name) is not None:
        raise ValueError(f'Native expression Action name already exists: {action_name}')
    source.output.mkdir(parents=True, exist_ok=False)
    composed_path = source.output / 'source_local_frames.json'
    composed_path.write_text(json.dumps([_frame_json(frame, index / rate) for index, frame in enumerate(composed)],
                                       allow_nan=False, separators=(',', ':')), encoding='utf-8')
    bound = binding.attach_animation(rig, hierarchy, composed_path, source.action_name, rate, 2e-4)
    shape_actions: dict[str, str] = {}
    for entry in correctives:
        keys = bpy.data.shape_keys[entry.key_data_name]
        if keys.name not in shape_actions:
            keys.animation_data_create()
            old = keys.animation_data.action
            action = old.copy() if old else bpy.data.actions.new(source.action_name + ' :: ' + keys.name)
            action.name = source.action_name + ' :: ' + keys.name
            action.use_fake_user = True
            if old:
                old.use_fake_user = True
            keys.animation_data.action = action
            shape_actions[keys.name] = action.name
        block = keys.key_blocks[entry.key_name]
        path = block.path_from_id('value')
        action = keys.animation_data.action
        for curve in tuple(action.fcurves):
            if curve.data_path == path:
                action.fcurves.remove(curve)
        for index, frame_weights in enumerate(weights, start=1):
            block.value = frame_weights.get(entry.native_index, 0.)
            block.keyframe_insert(data_path='value', frame=index, group='Native source corrective')
    # Snapshots have no claim about source tracker clocks or inter-frame morph scheduling.
    for name in (bound['action_name'], *shape_actions.values()):
        for curve in bpy.data.actions[name].fcurves:
            for point in curve.keyframe_points:
                point.interpolation = 'CONSTANT'
    for name in old_actions:
        bpy.data.actions[name].use_fake_user = True
    verify_retained_actions(old_actions)
    report = {'format': 'EndfieldNativeExpressionAuthoring/1', 'branch': branch,
        'character_id': source.character_id, 'manifest': str(source.manifest.path), 'manifest_sha256': source.manifest.sha256,
        'binding': bound, 'corrective_bindings': [entry.__dict__ for entry in correctives],
        'shape_actions': shape_actions, 'corrective_weights': [{str(key): value for key, value in row.items()} for row in weights],
        'samples': [frame.__dict__ for frame in source.frames], 'retained_actions': old_actions,
        'source_build_receipt': str(source.source_build_receipt.path), 'inherited_authoring': inherited,
        'inputs': {str(ref.path): ref.sha256 for ref in (source.manifest, source.source_blend, source.source_build_receipt,
            source.source_audit, source.expression_table, source.body_receipt, source.mesh_channel_probe, source.emotion_probe)},
        'scope': 'Native-verified explicit facial/corrective writeback on authenticated body snapshots; no automatic emotion tracker, gameplay scheduling or NLA additive blend.'}
    bpy.context.scene.frame_end = len(source.frames)
    bpy.context.scene.frame_set(1)
    report['validation'] = validate_native_expression(report)
    return report


def validate_native_expression(report: dict[str, object]) -> dict[str, object]:
    for path, digest in _record(report['inputs'], 'replay input hashes').items():
        if binding.file_hash(Path(path)) != digest:
            raise ValueError(f'Native expression input changed after baking: {path}')
    verify_retained_actions(_record(report['retained_actions'], 'retained Actions'))
    bound = _record(report['binding'], 'replay binding')
    rig = bpy.data.objects[binding.text(bound['armature'], 'replay rig')]
    previous = rig.animation_data.action
    previous_shapes = {name: bpy.data.shape_keys[name].animation_data.action for name in report['shape_actions']}
    rig.animation_data.action = bpy.data.actions[bound['action_name']]
    for name, action in report['shape_actions'].items():
        bpy.data.shape_keys[name].animation_data.action = bpy.data.actions[action]
    try:
        result = binding.validate_attached_animation(rig, bound, tuple(range(1, int(bound['frames']) + 1)), 2e-4)
        maximum = 0.
        for index, weights in enumerate(report['corrective_weights'], start=1):
            bpy.context.scene.frame_set(index)
            bpy.context.view_layer.update()
            for entry in report['corrective_bindings']:
                value = bpy.data.shape_keys[entry['key_data_name']].key_blocks[entry['key_name']].value
                maximum = max(maximum, abs(value - weights.get(str(entry['native_index']), 0.)))
        if maximum > 2e-6:
            raise AssertionError(f'Native corrective replay changed: maximum_error={maximum}')
        return {'status': 'passed', 'pose': result, 'maximum_corrective_error': maximum}
    finally:
        rig.animation_data.action = previous
        for name, action in previous_shapes.items():
            bpy.data.shape_keys[name].animation_data.action = action
