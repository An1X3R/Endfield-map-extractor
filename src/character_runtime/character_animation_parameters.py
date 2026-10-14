"""Preserve exact source Animator float parameters on an isolated editable owner.

The caller establishes the clip sample rate in the scene. Parameter Actions do
not change bone poses, materials, basic expressions, scene rate, or frame range.
Playing these properties preserves source values without recreating their native
runtime consumers. Unresolved names retain their exact attribute-hash identity.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import zlib

@dataclass(frozen=True)
class ParameterChannel:
    track: int
    attribute: int
    name: str | None
    property_name: str
    values: tuple[float, ...]


@dataclass(frozen=True)
class ParameterSource:
    character_id: str
    animator_path: str
    clip_name: str
    sample_rate: float
    frame_count: int
    channels: tuple[ParameterChannel, ...]
    input_hashes: tuple[tuple[Path, str], ...]
    controller_evidence: Path | None


def _record(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Expected JSON object: {context}')
    return value


def _rows(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f'Expected JSON array: {context}')
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f'Expected nonempty string: {context}')
    return value


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise TypeError(f'Expected integer: {context}')
    return value


def _number(value: object, context: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'Expected finite number: {context}')
    return float(value)


def _hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _source_path(parent: Path, value: object, context: str) -> Path:
    path = (parent / _text(value, context)).resolve(strict=True)
    if not path.is_file():
        raise FileNotFoundError(f'Required source file is absent: {context}={path}')
    return path


def _controller_names(native: Path, request: dict[str, object], clip: dict[str, object]) -> tuple[dict[int, str | None], Path | None]:
    if request['controller_bundle'] is None:
        return {}, None
    evidence_path = native / 'animator_parameter_verification.json'
    evidence = _record(_json(evidence_path), 'controller evidence')
    if evidence['status'] != 'exact_controller_clip_reference_and_tos_verified' or evidence['clip_reference_path_id'] != clip['path_id']:
        raise ValueError('Controller name evidence does not verify this exact source clip')
    if evidence['binding_type_id'] != 95 or evidence['binding_custom_type'] != 0:
        raise ValueError('Controller name evidence is not for Animator parameters')
    tree = _record(_json(native / 'controller_type_tree.json'), 'controller type tree')
    if tree['m_Name'] != evidence['controller']:
        raise ValueError('Parsed controller name differs from its verification receipt')
    pointers = tuple(_record(row, 'controller clip pointer') for row in _rows(tree['m_AnimationClips'], 'controller clips'))
    if sum(row['m_FileID'] == evidence['clip_reference_file_id'] and row['m_PathID'] == clip['path_id'] for row in pointers) != 1:
        raise ValueError('Parsed controller lacks the exact verified clip pointer')
    names_by_hash: dict[int, set[str]] = {}
    for value in _rows(tree['m_TOSData'], 'controller TOS strings'):
        if not isinstance(value, str):
            raise TypeError('Controller TOS entry must be a string; the serialized empty sentinel is permitted')
        name = value
        names_by_hash.setdefault(zlib.crc32(name.encode()) & 0xffffffff, set()).add(name)
    if any(len(names) != 1 for names in names_by_hash.values()):
        raise ValueError('Controller TOS has ambiguous CRC32 names; parameter naming is refused')
    resolved: dict[int, str | None] = {}
    for value in _rows(evidence['parameters'], 'controller parameter rows'):
        row = _record(value, 'controller parameter')
        attribute = _integer(row['attribute_hash'], 'parameter hash')
        if attribute in resolved:
            raise ValueError(f'Duplicate controller parameter hash: {attribute}')
        matches = names_by_hash.get(attribute, set())
        expected = next(iter(matches)) if matches else None
        if row['name'] != expected:
            raise ValueError(f'Controller name receipt differs from exact TOS hash lookup: {attribute}')
        resolved[attribute] = expected
    return resolved, evidence_path


def read_source_animator_parameters(source_receipt: Path) -> ParameterSource:
    """Validate existing decoded samples and retain all source hashes in a new receipt."""
    source_receipt = source_receipt.resolve(strict=True)
    receipt = _record(_json(source_receipt), 'animation export receipt')
    if receipt['format'] != 'EndfieldAnimationExportReceipt/1' or receipt['status'] != 'decoded_bound_and_merged_candidate':
        raise ValueError('Parameter preservation requires a decoded source animation receipt')
    hashes: dict[Path, str] = {source_receipt: _hash(source_receipt)}
    for value in _rows(receipt['inputs'], 'export input hashes'):
        row = _record(value, 'export input')
        path = _source_path(source_receipt.parent, row['path'], 'export input path')
        expected = _text(row['sha256'], 'export input hash').casefold()
        if _hash(path) != expected:
            raise ValueError(f'Export input changed since its source receipt: {path}')
        hashes[path] = expected
    request_path = source_receipt.parent / 'request.json'
    if request_path not in hashes:
        raise ValueError('Animation source receipt does not hash its reader request')
    request = _record(_json(request_path), 'reader request')
    native = source_receipt.parent / 'native'
    clip = _record(_json(native / 'clip_audit.json'), 'clip audit')
    if clip['source'] != receipt['clip_source'] or clip['path_id'] != receipt['clip_path_id'] or clip['m_name'] != receipt['clip_name']:
        raise ValueError('Decoded clip identity differs from the export receipt')
    bindings = tuple(_record(value, 'scalar binding') for value in _rows(_json(native / 'scalar_bindings.json'), 'scalar bindings'))
    raw_bindings = _rows(clip['bindings'], 'raw GenericBindings')
    transform_end = _integer(clip['output_track_count'], 'transform track count') * 2
    offset = len(raw_bindings) - len(bindings)
    if offset < transform_end or len(bindings) != clip['float_curve_count']:
        raise ValueError('Scalar binding counts differ from the actual clip layout')
    scale_paths: set[int] = set()
    for value in raw_bindings[transform_end:offset]:
        scale = _record(value, 'source scale GenericBinding')
        path_hash = _integer(scale['path'], 'source scale path hash')
        if scale['type'] != 'Transform' or scale['attribute'] != 3 or path_hash in scale_paths:
            raise ValueError('Intervening scale bindings must contain unique Transform scale paths')
        scale_paths.add(path_hash)
    frame_count = _integer(receipt['frames'], 'stored frame count')
    rate = _number(receipt['sample_rate'], 'sample rate')
    if frame_count <= 0 or rate <= 0:
        raise ValueError('Stored frame count and sample rate must be positive')
    frames = tuple(_record(value, 'scalar frame') for value in _rows(_json(native / 'float_samples.json'), 'scalar frames'))
    if len(frames) != frame_count:
        raise ValueError('Stored scalar frames differ from the decoded clip receipt')
    samples: list[tuple[float, ...]] = []
    for index, frame in enumerate(frames):
        if abs(_number(frame['time'], 'scalar time') - index / rate) > 1e-5:
            raise ValueError(f'Scalar frame timestamp differs from its sample rate: frame={index}')
        values = tuple(_number(value, 'scalar value') for value in _rows(frame['values'], 'scalar values'))
        if len(values) != len(bindings):
            raise ValueError(f'Scalar frame dimension differs from its bindings: frame={index}')
        samples.append(values)
    names, evidence = _controller_names(native, request, clip)
    channels: list[ParameterChannel] = []
    seen: set[int] = set()
    for track, binding in enumerate(bindings):
        raw = _record(raw_bindings[offset + track], 'source GenericBinding')
        if binding['track'] != track or binding['binding'] != offset + track or binding['attribute'] != raw['attribute'] or binding['custom_type'] != raw['custom_type']:
            raise ValueError(f'Scalar binding differs from its source GenericBinding: track={track}')
        if raw['custom_type'] != 0:
            continue
        if raw['type'] != 'Animator' or raw['path'] != 0 or raw['script_file_id'] != 0 or raw['script_path_id'] != 0 or raw['is_p_ptr_curve'] != 0 or raw['is_int_curve'] != 0:
            raise ValueError(f'Parameter is not the supported source-root Animator float binding: track={track}')
        attribute = _integer(raw['attribute'], 'Animator attribute hash')
        if not 0 <= attribute <= 0xffffffff or attribute in seen:
            raise ValueError(f'Animator parameter attribute is invalid or ambiguous: {attribute}')
        seen.add(attribute)
        if evidence is not None and attribute not in names:
            raise ValueError(f'Controller verification is missing a sampled parameter: {attribute}')
        name = names.get(attribute)
        if name == '':
            raise ValueError(f'Empty TOS sentinel cannot identify an authorable parameter: {attribute}')
        key = name if name is not None else f'hash_{attribute:08X}'
        if key in ('source_character_id', 'source_animator_path', 'source_clip_name', 'native_runtime_effects_applied'):
            raise ValueError(f'Source parameter name conflicts with owner provenance metadata: {key}')
        if len(key.encode()) > 63:
            raise ValueError(f'Source parameter name exceeds Blender custom-property limits: {key}')
        channels.append(ParameterChannel(track, attribute, name, key, tuple(frame[track] for frame in samples)))
    if not channels or len({channel.property_name for channel in channels}) != len(channels):
        raise ValueError('Source Animator float parameters are absent or have ambiguous property names')
    for name in ('clip_audit.json', 'scalar_bindings.json', 'float_samples.json', 'float.acl', 'float_header.json'):
        path = native / name
        hashes[path] = _hash(path)
    if evidence is not None:
        for path in (evidence, native / 'controller_type_tree.json'):
            hashes[path] = _hash(path)
        controller = _source_path(request_path.parent, request['controller_bundle'], 'controller bundle')
        if controller not in hashes:
            raise ValueError('Controller binary is not hash-linked by the original animation export')
    return ParameterSource(_text(receipt['character_id'], 'character ID'), _text(receipt['animator_root_path'], 'Animator owner path'),
                           _text(receipt['clip_name'], 'clip name'), rate, frame_count, tuple(channels),
                           tuple(hashes.items()), evidence)


def attach_source_animator_parameters(source_receipt: Path, owner_name: str, action_name: str) -> dict[str, object]:
    """Attach a dedicated source-parameter Action without touching existing Actions."""
    import bpy
    source = read_source_animator_parameters(source_receipt)
    _text(owner_name, 'parameter owner name')
    _text(action_name, 'parameter Action name')
    if bpy.data.objects.get(owner_name) is not None or bpy.data.actions.get(action_name) is not None:
        raise ValueError(f'Parameter owner or Action already exists: owner={owner_name}, action={action_name}')
    scene = bpy.context.scene
    if abs(scene.render.fps / scene.render.fps_base - source.sample_rate) > 1e-6:
        raise ValueError(f'Caller must establish the clip sample rate: scene_fps={scene.render.fps / scene.render.fps_base}, clip_fps={source.sample_rate}')
    owner = bpy.data.objects.new(owner_name, None)
    scene.collection.objects.link(owner)
    owner.hide_render = True
    owner['source_character_id'] = source.character_id
    owner['source_animator_path'] = source.animator_path
    owner['source_clip_name'] = source.clip_name
    owner['native_runtime_effects_applied'] = False
    action = bpy.data.actions.new(action_name)
    action.use_fake_user = True
    owner.animation_data_create()
    owner.animation_data.action = action
    maximum_error = 0.0
    for channel in source.channels:
        path = '[' + json.dumps(channel.property_name, ensure_ascii=False) + ']'
        for index, value in enumerate(channel.values):
            owner[channel.property_name] = value
            if not owner.keyframe_insert(data_path=path, frame=index + 1, group='Source Animator parameters'):
                raise RuntimeError(f'Cannot key source parameter: property={channel.property_name}, frame={index + 1}')
        curve = action.fcurves.find(path)
        if curve is None or len(curve.keyframe_points) != source.frame_count:
            raise AssertionError(f'Parameter curve is missing or incomplete: {channel.property_name}')
        for index, point in enumerate(curve.keyframe_points):
            point.interpolation = 'LINEAR'
            error = abs(float(point.co.y) - channel.values[index])
            maximum_error = max(maximum_error, error)
            if point.co.x != index + 1 or error > 1e-6:
                raise AssertionError(f'Parameter key differs from decoded source: property={channel.property_name}, frame={index + 1}, error={error}')
    if len(action.fcurves) != len(source.channels):
        raise AssertionError('Parameter Action has unexpected curves')
    scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)
    return {'format': 'EndfieldSourceAnimatorParameterBinding/1', 'status': 'source_float_parameters_keyed_without_runtime_consumers',
            'character_id': source.character_id, 'source_receipt': str(source_receipt.resolve()),
            'owner': owner.name, 'action': action.name, 'animator_owner_path': source.animator_path,
            'sample_rate': source.sample_rate, 'frames': source.frame_count,
            'parameter_count': len(source.channels), 'changing_parameters': sum(len(set(channel.values)) > 1 for channel in source.channels),
            'resolved_names': sum(channel.name is not None for channel in source.channels),
            'maximum_stored_key_error': maximum_error,
            'controller_name_evidence': str(source.controller_evidence) if source.controller_evidence else None,
            'inputs': [{'path': str(path), 'sha256': digest} for path, digest in source.input_hashes],
            'channels': [{'track': channel.track, 'attribute_hash': channel.attribute, 'source_name': channel.name,
                          'property': channel.property_name, 'name_evidence': 'exact_controller_TOS_CRC32' if channel.name is not None else 'source_GenericBinding_attribute_hash_only',
                          'minimum': min(channel.values), 'maximum': max(channel.values)} for channel in source.channels],
            'scope': 'Separate source Animator parameter owner and Action. No bone, cloth, IK, face, material, root-motion or gameplay consumer is applied. Linear interpolation is a host choice; stored frames retain decoded source values.'}


def validate_source_animator_parameters(binding: dict[str, object], frame_indices: tuple[int, ...], tolerance: float) -> dict[str, object]:
    """Check active source parameter properties against decoded source and restore time."""
    import bpy
    if binding['format'] != 'EndfieldSourceAnimatorParameterBinding/1' or not math.isfinite(tolerance) or tolerance <= 0 or not frame_indices:
        raise ValueError('Invalid parameter validation receipt, frame set, or tolerance')
    for value in _rows(binding['inputs'], 'parameter source hashes'):
        row = _record(value, 'parameter source hash')
        path = Path(_text(row['path'], 'parameter input path'))
        if _hash(path) != row['sha256']:
            raise ValueError(f'Parameter source changed after attachment: {path}')
    source = read_source_animator_parameters(Path(_text(binding['source_receipt'], 'source receipt')))
    owner = bpy.data.objects.get(_text(binding['owner'], 'parameter owner'))
    if owner is None or owner.animation_data is None or owner.animation_data.action is None or owner.animation_data.action.name != binding['action']:
        raise ValueError('Saved source parameter owner or active Action is missing')
    action = owner.animation_data.action
    if len(action.fcurves) != len(source.channels):
        raise AssertionError('Active parameter Action curve count differs from its source')
    scene = bpy.context.scene
    original_frame, original_subframe = scene.frame_current, scene.frame_subframe
    checks = []
    try:
        for frame in frame_indices:
            if type(frame) is not int or not 1 <= frame <= source.frame_count:
                raise ValueError(f'Parameter validation frame is outside the source clip: {frame}')
            scene.frame_set(frame, subframe=0.0)
            errors = []
            for channel in source.channels:
                error = abs(float(owner[channel.property_name]) - channel.values[frame - 1])
                errors.append(error)
                if error > tolerance:
                    raise AssertionError(f'Active parameter value differs from source: property={channel.property_name}, frame={frame}, error={error}')
            checks.append({'frame': frame, 'parameters': len(errors), 'maximum_value_error': max(errors)})
    finally:
        scene.frame_set(original_frame, subframe=original_subframe)
    return {'status': 'active_parameter_action_matches_source_samples', 'checked_frames': list(frame_indices),
            'maximum_value_error': max(row['maximum_value_error'] for row in checks), 'checks': checks,
            'original_scene_time_restored': True}
