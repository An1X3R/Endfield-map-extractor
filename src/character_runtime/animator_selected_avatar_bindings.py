"""Load mapped channels from a shared clip against an exact selected Avatar.

Missing CRCs have the verified native short mapping -1. Ambiguous paths and
damaged source/sample layouts still fail. This does not evaluate an Animator.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
import zlib

from animator_auxiliary_layers import (AuxiliaryClip, LocalPose, number, object_row,
    read_json, rows, unit, vector)
from animator_clip_references import binding_width, read_clip_reference


@dataclass(frozen=True)
class SelectedAvatarBinding:
    track: int
    crc: int
    avatar_index: int
    source_path: str | None
    has_scale: bool


def integer(value: object, field: str) -> int:
    result = number(value, field)
    if result != int(result):
        raise ValueError(f'Expected integral value: field={field}, value={value}')
    return int(result)


def selected_avatar_bindings(source: dict[str, object], avatar: dict[str, object],
                            hierarchy: list[object]) -> tuple[SelectedAvatarBinding, ...]:
    skeleton = object_row(object_row(avatar['m_Avatar'], 'avatar')['m_AvatarSkeleton'], 'skeleton')
    ids = tuple(integer(value, 'avatar.id') for value in rows(skeleton['m_ID'], 'avatar.ids'))
    raw_tos = object_row(avatar['m_TOS'], 'avatar.tos')
    if any(not isinstance(value, str) for value in raw_tos.values()) or any(crc < 0 or crc > 0xffffffff for crc in ids):
        raise ValueError('Selected Avatar requires uint32 IDs and string TOS paths')
    tos = {int(key): value for key, value in raw_tos.items()}
    if len(set(ids)) != len(ids) or set(ids) != set(tos):
        raise ValueError('Exact selected Avatar skeleton IDs and TOS must agree uniquely')
    if any(zlib.crc32(path.encode('utf-8')) != crc for crc, path in tos.items()):
        raise ValueError('Selected Avatar TOS path CRC disagrees with serialized ID')
    avatar_indices = {crc: index for index, crc in enumerate(ids)}
    candidates: dict[int, set[str]] = {}
    for raw in hierarchy:
        path = str(object_row(raw, 'hierarchy')['path'])
        parts = path.split('/')
        for index in range(len(parts)):
            candidates.setdefault(zlib.crc32('/'.join(parts[index:]).encode('utf-8')), set()).add(path)
    bindings = [object_row(raw, 'binding') for raw in rows(source['bindings'], 'bindings')]
    count = integer(source['OutputTrackCount'], 'track_count')
    if count <= 0 or integer(source['FloatCurveCount'], 'float_count') < 0 or not 0 <= integer(source['RootTrackCount'], 'root_count') <= integer(source['FloatCurveCount'], 'float_count'):
        raise ValueError('Source transform/float/root track counts are invalid')
    stride = (count + 7) // 8
    masks = base64.b64decode(str(source['masks']), validate=True)
    payload = stride * 3
    if len(masks) != (payload + 7) // 8 * 8 or any(masks[payload:]):
        raise ValueError('Transform masks require three byte-aligned bitmaps and zero allocation padding')
    if count % 8 and any(masks[component * stride + stride - 1] & ((1 << (8 - count % 8)) - 1) for component in range(3)):
        raise ValueError('Transform masks contain nonzero low padding bits')
    scale_flags = tuple(bool(masks[2 * stride + index // 8] & (1 << (7 - index % 8))) for index in range(count))
    scale_count = sum(scale_flags)
    scalar_start = count * 2 + scale_count
    if len(bindings) != scalar_start + integer(source['FloatCurveCount'], 'float_count'):
        raise ValueError('Transform/scalar binding dimensions disagree with actual source masks')
    scales = bindings[count * 2:scalar_start]
    scale_crcs = {integer(row['path'], 'scale.path') for row in scales}
    if len(scale_crcs) != scale_count or any(row['type'] != 'Transform' or row['attribute'] != 3 for row in scales):
        raise ValueError('Scale binding block must contain unique Transform scale paths')
    if any(row['type'] != 'Animator' for row in bindings[scalar_start:]):
        raise ValueError('Float channels require the verified Animator scalar layout')
    result: list[SelectedAvatarBinding] = []
    for index in range(count):
        position, rotation = bindings[index], bindings[index + count]
        if position['type'] != 'Transform' or position['attribute'] != 1 or rotation['type'] != 'Transform' or rotation['attribute'] != 2 or position['path'] != rotation['path']:
            raise ValueError(f'Transform position/rotation blocks disagree: track={index}')
        crc = integer(position['path'], 'binding.path')
        if not all(masks[component * stride + index // 8] & (1 << (7 - index % 8)) for component in (0, 1)) or scale_flags[index] != (crc in scale_crcs):
            raise ValueError(f'Transform subtrack masks disagree with bindings: track={index}')
        paths = candidates.get(crc, set())
        if len(paths) > 1:
            raise ValueError(f'Ambiguous Transform CRC path: track={index}, hash={crc}')
        avatar_index = avatar_indices.get(crc, -1)
        if (avatar_index == -1) != (len(paths) == 0):
            raise ValueError(f'Selected Avatar and selected hierarchy disagree: track={index}, hash={crc}')
        path = next(iter(paths)) if paths else None
        if path is not None and not (path == tos[crc] or path.endswith('/' + tos[crc])):
            raise ValueError(f'Selected Avatar TOS and hierarchy path disagree: track={index}, hash={crc}')
        result.append(SelectedAvatarBinding(index, crc, avatar_index, path, scale_flags[index]))
    if len({row.crc for row in result}) != count:
        raise ValueError('Multiple ACL tracks resolve to one Transform CRC')
    return tuple(result)


def load_selected_avatar_clip(directory: Path, avatar: dict[str, object],
                              hierarchy: list[object]) -> AuxiliaryClip:
    """Validate every source channel, then omit only proven unmapped transforms."""
    source = object_row(read_json(directory / 'clip_source.json'), 'source')
    bound = selected_avatar_bindings(source, avatar, hierarchy)
    bindings = [object_row(row, 'binding') for row in rows(source['bindings'], 'bindings')]
    count = len(bound)
    reference = read_clip_reference(source, read_json(directory / 'value_array_delta.json'))
    flattened: list[tuple[float, ...]] = []
    offset = 0
    for binding in bindings:
        width = binding_width(binding)
        flattened.append(reference.components[offset:offset + width])
        offset += width
    frames = [object_row(row, 'frame') for row in rows(read_json(directory / 'transform_samples.json'), 'frames')]
    floats = [object_row(row, 'float.frame') for row in rows(read_json(directory / 'float_samples.json'), 'float.frames')]
    roots = [object_row(row, 'root.frame') for row in rows(read_json(directory / 'root_motion_samples.json'), 'root.frames')]
    times = tuple(number(row['time'], 'time') for row in frames)
    for name, collection in (('transform', frames), ('float', floats), ('root', roots)):
        domain = tuple(number(row['time'], name + '.time') for row in collection)
        if not domain or any(a >= b for a, b in zip(domain, domain[1:])):
            raise ValueError(f'Sample timestamps must be nonempty and strictly increasing: buffer={name}')
    common = min(len(frames), len(floats), len(roots))
    for frame in frames:
        tracks = [object_row(row, 'track') for row in rows(frame['tracks'], 'tracks')]
        if len(tracks) != count or any(row['track'] != index for index, row in enumerate(tracks)):
            raise ValueError('Sample transform dimensions or ordering disagree')
        for track in tracks:
            vector(track['translation'], 3, 'translation')
            unit(vector(track['rotation'], 4, 'rotation'))
            vector(track['scale'], 3, 'scale')
    for name, collection, expected_count in (('float', floats, integer(source['FloatCurveCount'], 'float_count')),
                                            ('root', roots, integer(source['RootTrackCount'], 'root_count'))):
        for frame in collection:
            values = tuple(number(value, name + '.value') for value in rows(frame['values'], name + '.values'))
            if len(values) != expected_count:
                raise ValueError(f'Sample dimensions disagree: buffer={name}')
    scale_binding_tracks = {row.crc: row.track for row in bound}
    scalar_start = count * 2 + sum(row.has_scale for row in bound)
    all_components: list[tuple[float, ...]] = []
    all_poses: list[tuple[LocalPose, ...]] = []
    for frame, scalar, root in zip(frames[:common], floats[:common], roots[:common], strict=True):
        if abs(number(scalar['time'], 'float.time') - number(frame['time'], 'time')) > 1e-6 or abs(number(root['time'], 'root.time') - number(frame['time'], 'time')) > 1e-6:
            raise ValueError('Transform, root and scalar timestamps diverge')
        tracks = [object_row(row, 'track') for row in rows(frame['tracks'], 'tracks')]
        if len(tracks) != count or any(row['track'] != index for index, row in enumerate(tracks)):
            raise ValueError('Sample transform dimensions or ordering disagree')
        poses = tuple(LocalPose(vector(row['translation'], 3, 'translation'), unit(vector(row['rotation'], 4, 'rotation')), vector(row['scale'], 3, 'scale')) for row in tracks)
        values = tuple(number(value, 'float.value') for value in rows(scalar['values'], 'float.values'))
        root_values = tuple(number(value, 'root.value') for value in rows(root['values'], 'root.values'))
        if len(values) != integer(source['FloatCurveCount'], 'float_count') or len(root_values) != integer(source['RootTrackCount'], 'root_count') or values[:len(root_values)] != root_values:
            raise ValueError('Root/float dimensions or exact root scalar prefix disagree')
        components: list[float] = []
        for index, binding in enumerate(bindings):
            if index < count:
                components.extend(vector(tracks[index]['translation'], 3, 'translation'))
            elif index < count * 2:
                components.extend(vector(tracks[index - count]['rotation'], 4, 'rotation'))
            elif index < scalar_start:
                components.extend(vector(tracks[scale_binding_tracks[integer(binding['path'], 'scale.path')]]['scale'], 3, 'scale'))
            else:
                components.append(values[index - scalar_start])
        all_components.append(tuple(components))
        all_poses.append(poses)
    constant_indices = tuple(integer(value, 'constant.index') for value in rows(source['constantIndices'], 'constant.indices'))
    constant_values = tuple(number(value, 'constant.value') for value in rows(source['constantValues'], 'constant.values'))
    if len(set(constant_indices)) != len(constant_indices) or any(index < 0 or index >= len(bindings) for index in constant_indices):
        raise ValueError('Constant binding indices are duplicated or out of range')
    offsets: list[int] = []
    offset = 0
    for binding in bindings:
        offsets.append(offset)
        offset += binding_width(binding)
    consumed = 0
    for index in constant_indices:
        width = binding_width(bindings[index])
        expected = constant_values[consumed:consumed + width]
        if len(expected) != width or any(abs(a - b) > 1e-5 for frame in all_components for a, b in zip(frame[offsets[index]:offsets[index] + width], expected, strict=True)):
            raise ValueError(f'Decoded constant differs from actual source constant: binding={index}')
        consumed += width
    if consumed != len(constant_values):
        raise ValueError('Unconsumed constant source values')
    selected = tuple(row.track for row in bound if row.avatar_index != -1)
    scales = {integer(binding['path'], 'scale.path'): flattened[index] for index, binding in enumerate(bindings) if binding['type'] == 'Transform' and binding['attribute'] == 3}
    references = tuple(LocalPose(flattened[index], unit(flattened[index + count]), scales.get(bound[index].crc, (1., 1., 1.))) for index in selected)
    return AuxiliaryClip(int(directory.name), times[:common], tuple(tuple(frame[index] for index in selected) for frame in all_poses), references,
        tuple(str(bound[index].source_path) for index in selected), tuple(bound[index].has_scale for index in selected))
