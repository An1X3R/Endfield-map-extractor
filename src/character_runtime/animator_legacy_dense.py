"""Bind decoded legacy dense and constant components without reference-pose substitution."""
from __future__ import annotations

from pathlib import Path
from bisect import bisect_right
import math
import struct

from animator_auxiliary_layers import AuxiliaryClip, LocalPose, number, object_row, read_json, rows, unit
from animator_clip_references import binding_width, read_clip_reference
from animator_selected_avatar_bindings import SelectedAvatarBinding, integer

StreamedChannel = tuple[tuple[float, tuple[float, ...]], ...]


def streamed_channels(value: object, count: int) -> tuple[StreamedChannel, ...]:
    """Parse source little-endian cubic keys, retaining sparse per-channel clocks."""
    words = tuple(integer(word, 'streamed uint32') for word in rows(value, 'streamed data'))
    if any(not 0 <= word <= 0xffffffff for word in words):
        raise ValueError('Streamed source words must be uint32')
    data = struct.pack('<' + 'I' * len(words), *words)
    channels: list[list[tuple[float, tuple[float, ...]]]] = [[] for _ in range(count)]
    offset = 0
    previous = -math.inf
    terminated = False
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError('Truncated streamed frame header')
        time, keys = struct.unpack_from('<fi', data, offset)
        offset += 8
        if time == math.inf and keys == 0 and offset == len(data):
            terminated = True
            break
        if not math.isfinite(time) or time <= previous or not 0 <= keys <= count or offset + keys * 20 > len(data):
            raise ValueError('Invalid streamed frame clock, key count or payload length')
        previous = time
        seen: set[int] = set()
        for _ in range(keys):
            index, *coefficients = struct.unpack_from('<i4f', data, offset)
            offset += 20
            if not 0 <= index < count or index in seen or not all(math.isfinite(item) for item in coefficients):
                raise ValueError('Streamed keys require unique in-range indices and finite cubic coefficients')
            seen.add(index)
            channels[index].append((time, tuple(coefficients)))
    if not terminated or any(not channel for channel in channels):
        raise ValueError('Streamed source lacks its terminal sentinel or a declared channel')
    return tuple(tuple(channel) for channel in channels)


def sample_streamed(channels: tuple[StreamedChannel, ...], time: float) -> tuple[float, ...]:
    """Evaluate the serialized cubic coefficients relative to each key's own time."""
    result: list[float] = []
    for channel in channels:
        index = bisect_right(tuple(key[0] for key in channel), time) - 1
        if index < 0:
            raise ValueError(f'Streamed curve does not cover requested source time: time={time}')
        key_time, coefficients = channel[index]
        delta = time - key_time
        a, b, c, d = coefficients
        sampled = ((a * delta + b) * delta + c) * delta + d
        if not math.isfinite(sampled):
            raise ValueError(f'Nonfinite evaluated streamed component: time={time}')
        result.append(sampled)
    return tuple(result)


def legacy_bindings(source: dict[str, object], mapped: dict[int, tuple[int, str | None]]) -> tuple[SelectedAvatarBinding, ...]:
    """Join position and rotation bindings by CRC across dense and constant blocks."""
    if any(integer(source[name], name) != 0 for name in ('OutputTrackCount', 'FloatCurveCount', 'RootTrackCount')):
        raise ValueError('Legacy dense clips must not also declare new ACL tracks')
    if source['masks'] or any(rows(source[name], name) for name in ('defaultIndices', 'constantIndices', 'constantValues')):
        raise ValueError('Legacy dense clips must not also declare new ACL masks or constant tables')
    positions: dict[int, int] = {}
    rotations: set[int] = set()
    scales: set[int] = set()
    for index, raw in enumerate(rows(source['bindings'], 'bindings')):
        binding = object_row(raw, 'binding')
        binding_width(binding)
        if binding['type'] == 'Animator':
            continue
        crc = integer(binding['path'], 'binding.path')
        attribute = integer(binding['attribute'], 'binding.attribute')
        target = rotations if attribute == 2 else scales
        if (attribute == 1 and crc in positions) or (attribute != 1 and crc in target):
            raise ValueError(f'Duplicate legacy Transform channel: hash={crc}, attribute={attribute}')
        if attribute == 1:
            positions[crc] = index
        else:
            target.add(crc)
    if not positions or set(positions) != rotations or not scales <= positions.keys():
        raise ValueError('Legacy dense bindings require paired position/rotation paths and optional matching scales')
    return tuple(SelectedAvatarBinding(index, crc, mapped[crc][0], mapped[crc][1], crc in scales)
                 for index, crc in enumerate(positions))


def load_legacy_dense_clip(directory: Path, source: dict[str, object],
                           bound: tuple[SelectedAvatarBinding, ...]) -> AuxiliaryClip:
    """Validate every stored component, then expose only the source clip time domain."""
    legacy = object_row(read_json(directory / 'legacy_dense_clip.json'), 'legacy dense samples')
    if legacy['format'] != 'EndfieldLegacyDenseClipSamples/1':
        raise ValueError('Expected EndfieldLegacyDenseClipSamples/1')
    for field in ('cab', 'pathId', 'rawDataSha256'):
        if legacy[field] != source[field]:
            raise ValueError(f'Legacy samples disagree with exact source clip identity: field={field}')
    if legacy['oldAclIsSet'] is not False:
        raise ValueError('Legacy dense adapter requires absent independent old ACL channels')
    stream_count = integer(legacy['streamedCurveCount'], 'streamed curves')
    if stream_count < 0:
        raise ValueError('Legacy streamed curve count must be nonnegative')
    streamed = streamed_channels(legacy['streamedData'], stream_count)
    frame_count = integer(legacy['m_FrameCount'], 'dense frame count')
    curve_count = integer(legacy['m_CurveCount'], 'dense curve count')
    rate = number(legacy['m_SampleRate'], 'dense sample rate')
    begin = number(legacy['m_BeginTime'], 'dense begin time')
    if frame_count <= 0 or curve_count < 0 or curve_count + stream_count <= 0 or rate <= 0:
        raise ValueError('Legacy dense sample dimensions and rate must be positive')
    if abs(rate - number(source['m_SampleRate'], 'source sample rate')) > 1e-6:
        raise ValueError('Legacy dense rate disagrees with source clip rate')
    values = tuple(number(value, 'dense component') for value in rows(legacy['m_SampleArray'], 'dense samples'))
    constants = tuple(number(value, 'constant component') for value in rows(legacy['constantValues'], 'legacy constants'))
    bindings = [object_row(raw, 'binding') for raw in rows(source['bindings'], 'bindings')]
    widths = tuple(binding_width(binding) for binding in bindings)
    if len(values) != frame_count * curve_count or sum(widths) != stream_count + curve_count + len(constants):
        raise ValueError('Legacy dense/constant component counts disagree with exact source bindings')
    offsets: list[int] = []
    offset = 0
    for width in widths:
        offsets.append(offset)
        offset += width
    if any(boundary not in (*offsets, offset) for boundary in (stream_count, stream_count + curve_count)):
        raise ValueError('Legacy dense/constant boundary splits a source binding')
    reference = read_clip_reference(source, read_json(directory / 'value_array_delta.json'))
    channels = {(integer(binding['path'], 'binding.path'), integer(binding['attribute'], 'binding.attribute')):
                (offset, width) for binding, offset, width in zip(bindings, offsets, widths, strict=True)
                if binding['type'] == 'Transform'}

    def poses(components: tuple[float, ...]) -> tuple[LocalPose, ...]:
        result: list[LocalPose] = []
        for row in bound:
            position_offset, _ = channels[row.crc, 1]
            rotation_offset, _ = channels[row.crc, 2]
            scale = components[channels[row.crc, 3][0]:channels[row.crc, 3][0] + 3] if row.has_scale else (1., 1., 1.)
            result.append(LocalPose(components[position_offset:position_offset + 3],
                unit(components[rotation_offset:rotation_offset + 4]), scale))
        return tuple(result)

    all_times = tuple(begin + frame / rate for frame in range(frame_count))
    all_poses = tuple(poses(sample_streamed(streamed, time) + values[frame * curve_count:(frame + 1) * curve_count] + constants)
                      for frame, time in enumerate(all_times))
    start = number(source['start'], 'clip start')
    stop = number(source['stop'], 'clip stop')
    included = tuple(index for index, time in enumerate(all_times) if start - 1e-6 <= time <= stop + 1e-6)
    if not included or abs(all_times[included[0]] - start) > 1e-6 or abs(all_times[included[-1]] - stop) > 1e-6:
        raise ValueError('Legacy dense samples do not cover the exact source clip start/stop domain')
    selected = tuple(row.track for row in bound if row.avatar_index != -1)
    references = poses(reference.components)
    return AuxiliaryClip(int(directory.name), tuple(all_times[index] for index in included),
        tuple(tuple(all_poses[frame][index] for index in selected) for frame in included),
        tuple(references[index] for index in selected), tuple(str(bound[index].source_path) for index in selected),
        tuple(bound[index].has_scale for index in selected))
