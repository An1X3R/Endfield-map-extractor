"""Evaluate an explicit mathematical cloth-layer candidate from exact source samples.

This module does not reconstruct Unity runtime Animator evaluation. Clip reference
components follow the verified engine reference-vector branch. Normalized pose
clocks and ordered local delta composition remain explicit candidate semantics.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import json
import math
from pathlib import Path
import zlib

Vector = tuple[float, ...]


@dataclass(frozen=True)
class LocalPose:
    position: Vector
    rotation: Vector
    scale: Vector


@dataclass(frozen=True)
class AuxiliaryClip:
    index: int
    times: tuple[float, ...]
    poses: tuple[tuple[LocalPose, ...], ...]
    references: tuple[LocalPose, ...]
    paths: tuple[str, ...]
    has_scale: tuple[bool, ...]


def object_row(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f'Expected JSON object: field={field}')
    return value


def rows(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f'Expected JSON array: field={field}')
    return value


def number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'Expected finite numeric value: field={field}, actual={value}')
    return float(value)


def vector(value: object, width: int, field: str) -> Vector:
    values = rows(value, field)
    if len(values) != width:
        raise ValueError(f'Unexpected vector width: field={field}, expected={width}, actual={len(values)}')
    return tuple(number(item, field) for item in values)


def unit(q: Vector) -> Vector:
    norm = math.sqrt(sum(item * item for item in q))
    if len(q) != 4 or abs(norm - 1.0) > .001:
        raise ValueError(f'Quaternion is not unit length: norm={norm}')
    return tuple(item / norm for item in q)


def multiply(a: Vector, b: Vector) -> Vector:
    x, y, z, w = a
    X, Y, Z, W = b
    result = (w * X + x * W + y * Z - z * Y,
              w * Y - x * Z + y * W + z * X,
              w * Z + x * Y - y * X + z * W,
              w * W - x * X - y * Y - z * Z)
    return unit(result)


def rotation_power(q: Vector, weight: float) -> Vector:
    q = unit(q)
    if q[3] < 0:
        q = tuple(-value for value in q)
    sine = math.sqrt(sum(value * value for value in q[:3]))
    if sine < 1e-12:
        return (0., 0., 0., 1.)
    angle = math.atan2(sine, q[3]) * weight
    return unit(tuple(value * math.sin(angle) / sine for value in q[:3]) + (math.cos(angle),))


def interpolate(a: LocalPose, b: LocalPose, factor: float) -> LocalPose:
    inverse = tuple(-value for value in a.rotation[:3]) + (a.rotation[3],)
    rotation = multiply(a.rotation, rotation_power(multiply(inverse, b.rotation), factor))
    return LocalPose(tuple(x + factor * (y - x) for x, y in zip(a.position, b.position, strict=True)),
                     rotation, tuple(x + factor * (y - x) for x, y in zip(a.scale, b.scale, strict=True)))


def apply_delta(base: LocalPose, sample: LocalPose, reference: LocalPose,
                weight: float, has_scale: bool) -> LocalPose:
    if not math.isfinite(weight) or weight < 0 or weight > 4:
        raise ValueError(f'Candidate weight must be in [0,4]: weight={weight}')
    if weight == 0:
        return base
    inverse = tuple(-value for value in reference.rotation[:3]) + (reference.rotation[3],)
    delta = multiply(inverse, sample.rotation)
    scale = base.scale
    if has_scale:
        if any(abs(value) < 1e-12 for value in reference.scale):
            raise ValueError('Reference scale contains zero; additive ratio is undefined')
        scale = tuple(b * (1 + weight * (s / r - 1)) for b, s, r in zip(base.scale, sample.scale, reference.scale, strict=True))
    return LocalPose(tuple(b + weight * (s - r) for b, s, r in zip(base.position, sample.position, reference.position, strict=True)),
                     multiply(unit(base.rotation), rotation_power(delta, weight)), scale)


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding='utf-8'))


def load_clip(directory: Path, hierarchy: list[object]) -> AuxiliaryClip:
    status = object_row(read_json(directory / 'verification_status.json'), 'verification_status')
    if status.get('status') != 'existing_binding_verifier_passed':
        raise ValueError(f'Full source binding verifier did not pass: clip={directory.name}, status={status}')
    source = object_row(read_json(directory / 'clip_source.json'), 'clip_source')
    bindings = [object_row(row, 'binding') for row in rows(source.get('bindings'), 'bindings')]
    count = int(number(source.get('OutputTrackCount'), 'OutputTrackCount'))
    if len(bindings) < count * 2 or any(bindings[index]['type'] != 'Transform' or bindings[index]['attribute'] != 1
            or bindings[index + count]['type'] != 'Transform' or bindings[index + count]['attribute'] != 2
            or bindings[index]['path'] != bindings[index + count]['path'] for index in range(count)):
        raise ValueError('Reference consumer requires exact position/rotation binding blocks with identical paths')
    from animator_clip_references import read_clip_reference
    references = list(read_clip_reference(source, read_json(directory / 'value_array_delta.json')).components)
    widths = [(3 if binding['attribute'] in (1, 3) else 4) if binding['type'] == 'Transform' else 1 for binding in bindings]
    if len(references) != sum(widths):
        raise ValueError(f'Reference pose component count differs from binding widths: clip={directory.name}')
    flattened: list[Vector] = []
    offset = 0
    for binding, width in zip(bindings, widths, strict=True):
        flattened.append(vector(references[offset:offset + width], width, 'reference_binding'))
        offset += width
    bound = [object_row(row, 'bound_track') for row in rows(read_json(directory / 'bound_tracks.json'), 'bound_tracks')]
    if len(bound) != count or any(row['track'] != index for index, row in enumerate(bound)):
        raise ValueError('Verified track order or dimensions disagree')
    paths = tuple(str(row['source_transform_path']) for row in bound)
    known_paths = {str(object_row(row, 'hierarchy')['path']) for row in hierarchy}
    if any(path not in known_paths for path in paths):
        raise ValueError('Verified clip path is absent from the selected source hierarchy')
    scales = {int(number(binding['path'], 'binding.path')): flattened[index] for index, binding in enumerate(bindings)
              if binding['type'] == 'Transform' and binding['attribute'] == 3}
    reference_poses = tuple(LocalPose(flattened[index], unit(flattened[index + count]), scales.get(int(bindings[index]['path']), (1., 1., 1.))) for index in range(count))
    frames = [object_row(row, 'sample') for row in rows(read_json(directory / 'transform_samples.json'), 'samples')]
    times = tuple(number(frame['time'], 'sample.time') for frame in frames)
    if not times or any(a >= b for a, b in zip(times, times[1:])):
        raise ValueError('Sample times must be strictly increasing and nonempty')
    poses: list[tuple[LocalPose, ...]] = []
    for frame in frames:
        tracks = [object_row(row, 'track') for row in rows(frame['tracks'], 'tracks')]
        if len(tracks) != count or any(row['track'] != index for index, row in enumerate(tracks)):
            raise ValueError('Sample track order or dimensions disagree')
        poses.append(tuple(LocalPose(vector(track['translation'], 3, 'translation'), unit(vector(track['rotation'], 4, 'rotation')), vector(track['scale'], 3, 'scale')) for track in tracks))
    return AuxiliaryClip(int(directory.name), times, tuple(poses), reference_poses, paths, tuple(bool(row['has_scale']) for row in bound))


def sample_pose(clip: AuxiliaryClip, normalized_time: float) -> tuple[LocalPose, ...]:
    if not math.isfinite(normalized_time) or not 0 <= normalized_time <= 1:
        raise ValueError(f'Explicit normalized pose clock must be in [0,1]: value={normalized_time}')
    time = clip.times[0] + normalized_time * (clip.times[-1] - clip.times[0])
    index = bisect_left(clip.times, time)
    if index == 0:
        return clip.poses[0]
    if index == len(clip.times):
        return clip.poses[-1]
    if time == clip.times[index]:
        return clip.poses[index]
    factor = (time - clip.times[index - 1]) / (clip.times[index] - clip.times[index - 1])
    return tuple(interpolate(a, b, factor) for a, b in zip(clip.poses[index - 1], clip.poses[index], strict=True))


def pose_from_row(row: object) -> LocalPose:
    value = object_row(row, 'local')
    return LocalPose(vector(value['position'], 3, 'position'), vector(value['rotation'], 4, 'rotation'), vector(value['scale'], 3, 'scale'))


def pose_to_row(pose: LocalPose) -> dict[str, object]:
    return dict(position=list(pose.position), rotation=list(pose.rotation), scale=list(pose.scale))


def evaluate_frame(base: list[object], hierarchy: list[object], controller: dict[str, object],
                   routes: list[object], clips: dict[int, AuxiliaryClip], parameters: dict[str, object]) -> list[dict[str, object]]:
    if len(base) != len(hierarchy):
        raise ValueError('Source base frame and hierarchy dimensions disagree')
    result = [pose_from_row(row) for row in base]
    indices = {str(object_row(row, 'hierarchy')['path']): index for index, row in enumerate(hierarchy)}
    layer_array = rows(controller['m_LayerArray'], 'm_LayerArray')
    for raw_route in routes:
        route = object_row(raw_route, 'route')
        layer_index = int(number(route['layer'], 'route.layer'))
        layer = object_row(object_row(layer_array[layer_index], 'layer')['data'], 'layer.data')
        if layer['(int&)m_LayerBlendingMode'] != 1 or route['normalize_weights'] is not False or route['use_pose_time'] is not True:
            raise ValueError('Candidate only supports exact source additive Direct pose-time layers')
        if number(layer['m_DefaultWeight'], 'layer.default_weight') != 1 or layer['m_MeshSpace'] is not False:
            raise ValueError('Candidate requires serialized default weight 1 and local-space source layers')
        body_mask = object_row(layer['m_BodyMask'], 'body_mask')
        if any(number(value, 'body_mask.word') != 0 for value in body_mask.values()):
            raise ValueError('Candidate excludes layers with enabled Humanoid body masks')
        mask_data = object_row(object_row(layer['m_SkeletonMask'], 'mask')['data'], 'mask.data')
        mask_rows = [object_row(row, 'mask.row') for row in rows(mask_data['m_Data'], 'mask.rows')]
        masks = {int(number(row['m_PathHash'], 'mask.hash')): number(row['m_Weight'], 'mask.weight') for row in mask_rows}
        if len(masks) != len(mask_rows):
            raise ValueError('Layer mask contains duplicate CRC keys')
        from animator_direct_parameters import read_direct_source, evaluate_direct_parameters
        machine_index = int(number(layer['m_StateMachineIndex'], 'state_machine.index'))
        machine = object_row(object_row(rows(controller['m_StateMachineArray'], 'machines')[machine_index], 'machine')['data'], 'machine.data')
        state = object_row(object_row(rows(machine['m_StateConstantArray'], 'states')[int(number(route['state'], 'route.state'))], 'state')['data'], 'state.data')
        tree = object_row(object_row(rows(state['m_BlendTreeConstantArray'], 'trees')[int(number(route['tree'], 'route.tree'))], 'tree')['data'], 'tree.data')
        node = object_row(object_row(rows(tree['m_NodeArray'], 'nodes')[int(number(route['node'], 'route.node'))], 'node')['data'], 'node.data')
        direct = read_direct_source(node)
        weight_specs, clock_specs = rows(route['weights'], 'weights'), rows(route['pose_times'], 'pose_times')
        if tuple(int(number(object_row(spec, 'weight')['hash'], 'weight.hash')) for spec in weight_specs) != direct.weight_ids or tuple(int(number(object_row(spec, 'clock')['hash'], 'clock.hash')) for spec in clock_specs) != direct.pose_time_ids:
            raise ValueError('Candidate route parameters disagree with the exact source Direct node')
        parameter_values = {int(number(object_row(spec, 'parameter')['hash'], 'parameter.hash')):
            number(parameters[str(object_row(spec, 'parameter')['name'])], 'parameter.value') for spec in weight_specs + clock_specs}
        evaluated = evaluate_direct_parameters(direct, parameter_values)
        if evaluated.pose_times is None:
            raise ValueError('Candidate requires source Direct pose-time parameters')
        for pointer, weight, clock in zip(rows(route['child_clips'], 'child_clips'), evaluated.child_weights, evaluated.pose_times, strict=True):
            clip = clips[int(object_row(pointer, 'clip.pointer')['index'])]
            samples = sample_pose(clip, clock)
            for path, sample, reference, has_scale in zip(clip.paths, samples, clip.references, clip.has_scale, strict=True):
                suffix_hashes = [zlib.crc32('/'.join(path.split('/')[index:]).encode()) for index in range(len(path.split('/')))]
                matching = [masks[value] for value in suffix_hashes if value in masks]
                if len(matching) > 1:
                    raise ValueError(f'Ambiguous layer mask path: path={path}')
                mask_weight = matching[0] if matching else 0.
                index = indices[path]
                result[index] = apply_delta(result[index], sample, reference, weight * mask_weight, has_scale)
    return [pose_to_row(pose) for pose in result]
