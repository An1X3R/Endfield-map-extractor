"""Attach source Fur transmission to the existing host NPR ramp, not native lighting."""
from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Mapping, cast

import bpy

from character_node_graph import math_node, node

if TYPE_CHECKING:
    from rebuild_character_materials import MaterialSource


SECTION = 'Approx Fur Transmission'


def _upstream(socket: bpy.types.NodeSocket) -> set[bpy.types.Node]:
    pending = [link.from_node for link in socket.links]
    seen: set[bpy.types.Node] = set()
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(link.from_node for item in current.inputs for link in item.links)
    return seen


def _vector(tree: bpy.types.NodeTree, operation: str, first: bpy.types.NodeSocket,
            second: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    result = node(tree, 'ShaderNodeVectorMath', label, SECTION)
    result.operation = operation
    tree.links.new(first, result.inputs[0])
    tree.links.new(second, result.inputs[1])
    return result.outputs['Value'] if operation == 'DOT_PRODUCT' else result.outputs[0]


def _planar(tree: bpy.types.NodeTree, vector: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    split = node(tree, 'ShaderNodeSeparateXYZ', label + ' components', SECTION)
    tree.links.new(vector, split.inputs[0])
    horizontal = node(tree, 'ShaderNodeCombineXYZ', label + ' in host horizontal XY', SECTION)
    for axis in ('X', 'Y'):
        tree.links.new(split.outputs[axis], horizontal.inputs[axis])
    return _vector(tree, 'NORMALIZE', horizontal.outputs[0], horizontal.outputs[0], label + ' normalized')


def _consumers(tree: bpy.types.ShaderNodeTree, record: Mapping[str, object]) -> tuple[bpy.types.NodeSocket, ...]:
    rows = record.get('consumers')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Fur transmission requires its recorded original NdotL consumers')
    result: list[bpy.types.NodeSocket] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('node'), str) or type(row.get('input')) is not int:
            raise TypeError('Fur transmission consumer must contain a node name and input index')
        result.append(tree.nodes[row['node']].inputs[row['input']])
    return tuple(result)


def restore_fur_ndotl(tree: bpy.types.ShaderNodeTree, record: Mapping[str, object]) -> None:
    """Reconnect the preserved baseline for a controlled comparison."""
    original = tree.nodes[str(record['original_node'])].outputs['Value']
    for socket in _consumers(tree, record):
        tree.links.new(original, socket)


def connect_fur_transmission(tree: bpy.types.ShaderNodeTree, record: Mapping[str, object]) -> None:
    """Reconnect the independently retained Fur transmission result."""
    modified = tree.nodes[str(record['modified_node'])].outputs[0]
    for socket in _consumers(tree, record):
        tree.links.new(modified, socket)


def attach_fur_transmission(tree: bpy.types.ShaderNodeTree, source: MaterialSource) -> dict[str, object]:
    """Reuse active coverage samples and retain the complete ordinary shading graph."""
    if source.shader != 'HGRP/CharacterNPR' or '_CHARACTER_FUR' not in source.keywords:
        return {'status': 'not_selected'}
    values: dict[str, float] = {}
    for key in ('_FurTTIntensity', '_FurNoise'):
        value = source.floats[key]
        if source.shader_property_types.get(key) not in (2, 3) or not math.isfinite(value) or value < 0.0:
            raise ValueError(f'Invalid source Fur transmission parameter: material={source.identity}, {key}={value}')
        values[key] = value
    if not 0.0 <= values['_FurTTIntensity'] <= 1.0:
        raise ValueError(f'Unsupported source Fur transmission intensity: material={source.identity}, values={values}')
    output, = (item for item in tree.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
    coverage = _upstream(output.inputs['Coverage'])
    sample, = (item for item in coverage if item.type == 'TEX_IMAGE' and item.get('source_coverage_slot') == '_FurMap')
    red, = (item for item in coverage if item.type == 'SEPARATE_COLOR'
            and len(item.inputs[0].links) == 1 and item.inputs[0].links[0].from_node == sample)
    layer, = (item for item in coverage if item.type == 'SEPXYZ' and len(item.inputs[0].links) == 1
              and item.inputs[0].links[0].from_node.type == 'UVMAP'
              and item.inputs[0].links[0].from_node.uv_map == 'UV1')
    binding, = (item for item in source.textures if item.name == '_FurMap')
    if tuple(json.loads(sample['source_identity'])) != binding.identity:
        raise ValueError(f'Active Fur coverage sample differs from source transmission binding: {source.identity}')
    if 'source_fur_transmission' in tree:
        record = cast(dict[str, object], json.loads(tree['source_fur_transmission']))
        if record['parameters'] != values or record['source_identity'] != list(source.identity):
            raise ValueError(f'Existing Fur transmission differs from source: {source.identity}')
        for key, replacement, component in (('red_node', red, 'Red'), ('layer_node', layer, 'X')):
            previous = tree.nodes[str(record[key])]
            if previous != replacement:
                for link in tuple(previous.outputs[component].links):
                    if link.to_node.get('endfield_layout_section') == SECTION:
                        tree.links.new(replacement.outputs[component], link.to_socket)
                record[key] = replacement.name
        record['fur_sample_node'] = sample.name
        record['fur_sample_sha256'] = sample['source_sha256']
        tree['source_fur_transmission'] = json.dumps(record, sort_keys=True)
        verify_fur_transmission(tree, record)
        return record
    original, = (item for item in tree.nodes if item.type == 'VECT_MATH'
                 and item.operation == 'DOT_PRODUCT' and item.label == 'Artist N dot L')
    if original not in _upstream(output.inputs['Shader']) or len(original.inputs[1].links) != 1:
        raise ValueError(f'Fur transmission requires the active complete host NPR lighting: {tree.name}')
    direction = original.inputs[1].links[0].from_socket
    destinations = tuple(link.to_socket for link in original.outputs['Value'].links)
    if not destinations:
        raise ValueError(f'Fur transmission cannot find ordinary NdotL consumers: {tree.name}')
    camera = node(tree, 'ShaderNodeVectorTransform', 'Host camera backward axis in world space', SECTION)
    camera.vector_type = 'VECTOR'
    camera.convert_from = 'CAMERA'
    camera.convert_to = 'WORLD'
    camera.inputs[0].default_value = (0.0, 0.0, 1.0)
    dot = _vector(tree, 'DOT_PRODUCT', _planar(tree, camera.outputs[0], 'Camera backward'),
                  _planar(tree, direction, 'Artist light'), 'Host horizontal camera-light dot')
    facing = math_node(tree, 'MULTIPLY', dot, -1.0, 'Toward-light camera facing', SECTION)
    facing = math_node(tree, 'MINIMUM', math_node(tree, 'MAXIMUM', facing, 0.0, 'Camera facing low clamp', SECTION), 1.0, 'Camera facing saturation', SECTION)
    inverse = math_node(tree, 'MULTIPLY', math_node(tree, 'SUBTRACT', 1.0, red.outputs['Red'], 'One minus actual Fur sample', SECTION), 1.4286, 'Source Fur inverse scale', SECTION)
    inverse = math_node(tree, 'MINIMUM', math_node(tree, 'MAXIMUM', inverse, 0.0, 'Fur inverse low clamp', SECTION), 1.0, 'Fur inverse saturation', SECTION)
    smooth = math_node(tree, 'MULTIPLY', math_node(tree, 'MULTIPLY', inverse, inverse, 'Fur inverse squared', SECTION), math_node(tree, 'SUBTRACT', 3.0, math_node(tree, 'MULTIPLY', inverse, 2.0, 'Fur inverse doubled', SECTION), 'Fur inverse smooth factor', SECTION), 'Smooth Fur inverse', SECTION)
    transmission = math_node(tree, 'MULTIPLY', math_node(tree, 'MULTIPLY', smooth, facing, 'Camera-facing Fur transmission', SECTION), values['_FurNoise'] * (1.15 - values['_FurTTIntensity']), 'Source Fur transmission scale', SECTION)
    transmission = math_node(tree, 'ADD', transmission, values['_FurTTIntensity'], 'Source Fur transmission intensity', SECTION)
    modified = math_node(tree, 'ADD', original.outputs['Value'], math_node(tree, 'MULTIPLY', transmission, layer.outputs['X'], 'Source layer-scaled transmission', SECTION), 'Transmitted Fur NdotL', SECTION)
    modified = math_node(tree, 'MINIMUM', math_node(tree, 'MAXIMUM', modified, -1.0, 'Fur NdotL lower clamp', SECTION), 1.0, 'Fur NdotL upper clamp', SECTION)
    record: dict[str, object] = {
        'format': 'EndfieldFurTransmission/1', 'status': 'source_inputs_host_lighting_approximation',
        'source_identity': list(source.identity), 'keyword': '_CHARACTER_FUR', 'parameters': values,
        'original_node': original.name, 'modified_node': modified.node.name,
        'consumers': [{'node': item.node.name, 'input': list(item.node.inputs).index(item)} for item in destinations],
        'fur_sample_node': sample.name, 'fur_sample_sha256': sample['source_sha256'],
        'red_node': red.name, 'layer_node': layer.name, 'camera_node': camera.name,
        'native_shader_semantics_verified': False,
        'scope': 'Generated Fur transmission arithmetic on the active coverage FurMap red sample and UV1.x. Host horizontal XY camera/light facing substitutes unbound game globals. Original NdotL, complete ramp, BRDF, source colors, coverage and emission remain retained.',
    }
    tree['source_fur_transmission'] = json.dumps(record, sort_keys=True)
    connect_fur_transmission(tree, record)
    verify_fur_transmission(tree, record)
    return record


def verify_fur_transmission(tree: bpy.types.ShaderNodeTree, record: Mapping[str, object]) -> None:
    """Check real connections, including the coverage sample and original normal path."""
    if json.loads(tree['source_fur_transmission']) != dict(record):
        raise AssertionError(f'Fur transmission source record changed: {tree.name}')
    original = tree.nodes[str(record['original_node'])]
    modified = tree.nodes[str(record['modified_node'])]
    sample = tree.nodes[str(record['fur_sample_node'])]
    red = tree.nodes[str(record['red_node'])]
    layer = tree.nodes[str(record['layer_node'])]
    output, = (item for item in tree.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
    shader = _upstream(output.inputs['Shader'])
    coverage = _upstream(output.inputs['Coverage'])
    if (original not in shader or modified not in shader or sample not in shader or layer not in shader
            or sample not in coverage or layer not in coverage
            or sample.get('source_coverage_slot') != '_FurMap' or sample['source_sha256'] != record['fur_sample_sha256']
            or len(red.inputs[0].links) != 1 or red.inputs[0].links[0].from_node != sample
            or layer.inputs[0].links[0].from_node.uv_map != 'UV1'
            or any(len(item.links) != 1 or item.links[0].from_node != modified for item in _consumers(tree, record))):
        raise AssertionError(f'Fur transmission is disconnected from its active source coverage or ordinary NPR ramp: {tree.name}')
