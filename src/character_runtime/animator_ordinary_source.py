"""Evaluate verified authored ordinary channels from exact source Direct layers.

Only binary skeleton masks, local-space additive layers and explicit pose clocks
are supported. This does not synthesize missing WriteDefaults channels, evaluate
Humanoid channels or reconstruct a live Animator scheduler.
"""
from __future__ import annotations

import zlib

from animator_auxiliary_layers import (AuxiliaryClip, LocalPose, number, object_row,
    pose_from_row, pose_to_row, rows, sample_pose)
from animator_clip_delta import subtract_clip_reference
from animator_direct_parameters import evaluate_direct_parameters, read_direct_source
from animator_ordinary_direct import mix_ordinary_direct
from animator_ordinary_layers import apply_ordinary_additive_layer
from animator_weighted_channels import MixedPose, WeightedPose


def ordinary_layer_mask(layer: dict[str, object], paths: tuple[str, ...]) -> tuple[bool, ...]:
    """Validate ordinary source layer semantics and resolve exact binary path masks."""
    if layer['(int&)m_LayerBlendingMode'] != 1 or layer['m_MeshSpace'] is not False:
        raise ValueError('Ordinary source evaluator requires a local-space additive layer')
    if any(number(value, 'body_mask.word') != 0 for value in object_row(layer['m_BodyMask'], 'body_mask').values()):
        raise ValueError('Ordinary source evaluator does not support Humanoid body masks')
    for field in ('m_IKPass', 'm_SyncedLayerAffectsTiming', 'm_UseThreePoseBlender', 'm_ConvertSimpleBlend', 'm_PostProcessLayer', 'm_UseIdentifyValues'):
        if layer[field] is not False:
            raise ValueError(f'Unsupported ordinary source layer setting: field={field}, value={layer[field]}')
    mask_rows = rows(object_row(object_row(layer['m_SkeletonMask'], 'mask')['data'], 'mask.data')['m_Data'], 'mask.rows')
    masks = {int(number(object_row(row, 'mask.row')['m_PathHash'], 'mask.hash')):
        number(object_row(row, 'mask.row')['m_Weight'], 'mask.weight') for row in mask_rows}
    if len(masks) != len(mask_rows) or any(value not in (0., 1.) for value in masks.values()):
        raise ValueError('Ordinary source evaluator requires unique binary skeleton mask weights')
    enabled_paths: list[bool] = []
    for path in paths:
        parts = path.split('/')
        matches = tuple(masks[crc] for index in range(len(parts))
            if (crc := zlib.crc32('/'.join(parts[index:]).encode('utf-8'))) in masks)
        if len(matches) > 1:
            raise ValueError(f'Ordinary source skeleton mask path is ambiguous: path={path}')
        enabled_paths.append(bool(matches[0]) if matches else False)
    return tuple(enabled_paths)


def evaluate_ordinary_frame(base: list[object], hierarchy: list[object], controller: dict[str, object],
                             routes: list[object], clips: dict[int, AuxiliaryClip],
                             parameters: dict[str, object]) -> list[dict[str, object]]:
    """Subtract references per child, mix Direct once, then apply each source layer."""
    if not base or len(base) != len(hierarchy):
        raise ValueError('Ordinary source base and hierarchy dimensions disagree')
    paths = tuple(str(object_row(row, 'hierarchy')['path']) for row in hierarchy)
    if len(set(paths)) != len(paths):
        raise ValueError('Ordinary source hierarchy paths must be unique')
    output = tuple(MixedPose(pose_from_row(row), (True, True, True), (0., 0., 0.)) for row in base)
    explicit_base = tuple(row.pose for row in output)
    neutral = LocalPose((0., 0., 0.), (0., 0., 0., 1.), (0., 0., 0.))
    layer_array = rows(controller['m_LayerArray'], 'layers')
    for raw_route in routes:
        route = object_row(raw_route, 'route')
        layer = object_row(object_row(layer_array[int(number(route['layer'], 'layer.index'))], 'layer')['data'], 'layer.data')
        enabled_paths = ordinary_layer_mask(layer, paths)
        machine = object_row(object_row(rows(controller['m_StateMachineArray'], 'machines')[int(number(layer['m_StateMachineIndex'], 'machine.index'))], 'machine')['data'], 'machine.data')
        state = object_row(object_row(rows(machine['m_StateConstantArray'], 'states')[int(number(route['state'], 'state.index'))], 'state')['data'], 'state.data')
        tree = object_row(object_row(rows(state['m_BlendTreeConstantArray'], 'trees')[int(number(route['tree'], 'tree.index'))], 'tree')['data'], 'tree.data')
        node = object_row(object_row(rows(tree['m_NodeArray'], 'nodes')[int(number(route['node'], 'node.index'))], 'node')['data'], 'node.data')
        direct = read_direct_source(node)
        weights, clocks = rows(route['weights'], 'weights'), rows(route['pose_times'], 'clocks')
        if tuple(int(number(object_row(spec, 'weight')['hash'], 'weight.hash')) for spec in weights) != direct.weight_ids or tuple(int(number(object_row(spec, 'clock')['hash'], 'clock.hash')) for spec in clocks) != direct.pose_time_ids:
            raise ValueError('Ordinary source route parameters disagree with the exact Direct node')
        declarations = rows(object_row(object_row(controller['m_Values'], 'values')['data'], 'values.data')['m_ValueArray'], 'value_array')
        float_defaults = rows(object_row(object_row(controller['m_DefaultValues'], 'defaults')['data'], 'defaults.data')['m_FloatValues'], 'float_defaults')
        declared = {int(number(object_row(row, 'declaration')['m_ID'], 'parameter.id')):
            number(float_defaults[int(number(object_row(row, 'declaration')['m_Index'], 'parameter.index'))], 'parameter.default')
            for row in declarations if object_row(row, 'declaration')['m_Type'] == 1}
        parameter_values: dict[int, float] = {}
        for spec in weights + clocks:
            field = object_row(spec, 'parameter')
            crc = int(number(field['hash'], 'parameter.hash'))
            name = str(field['name'])
            if name in parameters:
                parameter_values[crc] = number(parameters[name], 'parameter.value')
            elif crc in declared:
                parameter_values[crc] = declared[crc]
            elif crc != 0xffffffff:
                raise ValueError(f'Ordinary source parameter has no explicit value or serialized float default: name={name}, hash={crc}')
        evaluated = evaluate_direct_parameters(direct, parameter_values)
        if evaluated.pose_times is None:
            raise ValueError('Ordinary source evaluator requires explicit Direct pose-time parameters')
        children: list[tuple[WeightedPose, ...]] = []
        pointers = rows(route['child_clips'], 'child_clips')
        node_rows = rows(tree['m_NodeArray'], 'nodes')
        actual_ids = tuple(int(number(object_row(object_row(node_rows[int(index)], 'child.node')['data'], 'child.data')['m_ClipID'], 'child.clip')) for index in rows(node['m_ChildIndices'], 'child_indices'))
        if tuple(int(number(object_row(pointer, 'pointer')['index'], 'clip.index')) for pointer in pointers) != actual_ids:
            raise ValueError('Ordinary source route clip order disagrees with the exact Direct children')
        for pointer, weight, clock in zip(pointers, evaluated.child_weights, evaluated.pose_times, strict=True):
            clip = clips[int(number(object_row(pointer, 'pointer')['index'], 'clip.index'))]
            sampled = sample_pose(clip, clock)
            if len(set(clip.paths)) != len(clip.paths) or any(path not in paths for path in clip.paths):
                raise ValueError('Ordinary child binding paths are ambiguous or outside the exact selected hierarchy')
            mapped = {path: (subtract_clip_reference(sample, reference, (True, True, has_scale)), has_scale)
                for path, sample, reference, has_scale in zip(clip.paths, sampled, clip.references, clip.has_scale, strict=True)}
            children.append(tuple(WeightedPose(mapped[path][0], weight, (True, True, mapped[path][1]))
                if path in mapped else WeightedPose(neutral, weight, (False, False, False)) for path in paths))
        mixed = mix_ordinary_direct(tuple(children), None, (neutral,) * len(paths))
        masked = tuple(MixedPose(row.pose, tuple(value and allowed for value in row.enabled), row.channel_weights)
            for row, allowed in zip(mixed.channels, enabled_paths, strict=True))
        output = apply_ordinary_additive_layer(output, masked, explicit_base, number(layer['m_DefaultWeight'], 'layer.weight'))
    return [pose_to_row(row.pose) for row in output]
