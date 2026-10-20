"""Source-bound RGB depth layers with explicitly approximate host composition."""
from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Mapping

import bpy

from character_node_graph import color_mix, input_socket, math_node, node

if TYPE_CHECKING:
    from rebuild_character_materials import MaterialSource

CONTRACT = 'EndfieldCharacterParallaxPreview/1'
SCOPE = ('Host layered RGB texture approximation, not native parallax marching. Source '
         'UV0/ST, texture, depth scale, march count and tint are retained. Equally spaced '
         'depth layers, equal RGB weights, UV-derived tangent frame, grazing clamp and '
         'the editable half-strength mix over the original surface are host choices. '
         'Texture RGB is sampled as color, not interpreted as height or alpha coverage. '
         'Source HDR tint is retained; the host color/BSDF response may clamp it. Native '
         'color composition, sampler, ray traversal and lighting equivalence remain unverified.')


def _vector(tree: bpy.types.NodeTree, operation: str, name: str) -> bpy.types.Node:
    result = node(tree, 'ShaderNodeVectorMath', name, 'Approx Source Parallax')
    result.operation = operation
    return result


def apply_parallax_preview(tree: bpy.types.ShaderNodeTree, source: MaterialSource,
                           samples: Mapping[str, bpy.types.Node], base: bpy.types.NodeSocket,
                           entry: bpy.types.Node) -> bpy.types.NodeSocket:
    """Attach only the verified property/keyword subset; leave inactive graphs untouched."""
    if source.shader != 'HGRP/CharacterNPR':
        return base
    enabled = source.floats.get('_UseParallax')
    keyword = '_PARALLAX_MAP' in source.keywords
    if enabled in (None, 0.0) and not keyword:
        return base
    if enabled != 1.0 or not keyword:
        raise ValueError(f'Source parallax property/keyword disagree: material={source.identity}; enabled={enabled}; keyword={keyword}')
    if source.floats.get('_ParallaxUseNormal') != 0.0:
        raise NotImplementedError(f'Source parallax normal-map mode is not implemented: material={source.identity}; value={source.floats.get("_ParallaxUseNormal")}')
    required = {'_UseParallax': 2, '_ParallaxTex': 4, '_ParallaxUseNormal': 2,
                '_ParallaxMarchNum': 3, '_ParallaxScale': 3, '_ParallaxColor': 0}
    for name, expected in required.items():
        if source.shader_property_types.get(name) != expected:
            raise ValueError(f'Source parallax property type is unverified: material={source.identity}; property={name}')
    count = source.floats.get('_ParallaxMarchNum')
    scale = source.floats.get('_ParallaxScale')
    tint = source.colors.get('_ParallaxColor')
    if count is None or not math.isfinite(count) or int(count) != count or not 1 <= count <= 5:
        raise ValueError(f'Source parallax march count must be an integer in [1,5]: material={source.identity}; count={count}')
    if scale is None or not math.isfinite(scale) or not 0.0 <= scale <= 1.0:
        raise ValueError(f'Source parallax scale is outside the native property range: material={source.identity}; scale={scale}')
    if tint is None or len(tint) != 4 or any(not math.isfinite(value) or value < 0.0 for value in tint):
        raise ValueError(f'Source parallax color is malformed: material={source.identity}; color={tint}')
    bindings = tuple(binding for binding in source.textures if binding.name == '_ParallaxTex')
    sample = samples.get('_ParallaxTex')
    if len(bindings) != 1 or sample is None or sample.image is None or sample.image.packed_file is None:
        raise ValueError(f'Enabled source parallax needs its exact packed _ParallaxTex: material={source.identity}')
    binding = bindings[0]
    if binding.uv_index != 0:
        raise NotImplementedError(f'Source parallax preview currently requires UV0: material={source.identity}; uv={binding.uv_index}')
    input_socket(tree, 'Source Parallax Scale', 'NodeSocketFloat', scale, 'Exact source depth-range parameter; host layer offsets approximate the native march.')
    input_socket(tree, 'Source Parallax Color', 'NodeSocketColor', tint, 'Exact source HDR tint; native color-layer composition is unverified.')
    input_socket(tree, 'Preview Parallax Strength', 'NodeSocketFloat', 0.5, 'Host RGB mix: zero restores the original complete base surface; one selects the layered source texture preview.')
    input_socket(tree, 'Preview Parallax Grazing Floor', 'NodeSocketFloat', 0.25, 'Host denominator floor for stable oblique view rays; not a recovered game parameter.')
    uv = node(tree, 'ShaderNodeUVMap', 'Parallax source UV0', 'Approx Source Parallax')
    uv.uv_map = 'UV0'
    geometry = node(tree, 'ShaderNodeNewGeometry', 'Parallax host view frame', 'Approx Source Parallax')
    tangent = node(tree, 'ShaderNodeTangent', 'Parallax host UV0 tangent', 'Approx Source Parallax')
    tangent.direction_type = 'UV_MAP'
    tangent.uv_map = 'UV0'
    bitangent = _vector(tree, 'CROSS_PRODUCT', 'Parallax host normal cross tangent')
    tree.links.new(geometry.outputs['Normal'], bitangent.inputs[0])
    tree.links.new(tangent.outputs['Tangent'], bitangent.inputs[1])
    view_components: list[bpy.types.NodeSocket] = []
    for name, direction in (('Tangent', tangent.outputs['Tangent']),
                            ('Bitangent', bitangent.outputs['Vector']),
                            ('Normal', geometry.outputs['Normal'])):
        dot = _vector(tree, 'DOT_PRODUCT', 'Parallax view dot ' + name)
        tree.links.new(direction, dot.inputs[0])
        tree.links.new(geometry.outputs['Incoming'], dot.inputs[1])
        view_components.append(dot.outputs['Value'])
    denominator = math_node(tree, 'ABSOLUTE', view_components[2], 0.0, 'Parallax absolute view-normal cosine', 'Approx Source Parallax')
    denominator = math_node(tree, 'MAXIMUM', denominator, entry.outputs['Preview Parallax Grazing Floor'], 'Parallax host grazing floor', 'Approx Source Parallax')
    ray = node(tree, 'ShaderNodeCombineXYZ', 'Parallax host tangent-space ray', 'Approx Source Parallax')
    for index in range(2):
        component = math_node(tree, 'DIVIDE', view_components[index], denominator, 'Parallax normalized view ' + str(index), 'Approx Source Parallax')
        tree.links.new(component, ray.inputs[index])
    combined: bpy.types.NodeSocket | None = None
    for index in range(int(count)):
        depth = (index + 0.5) / count
        distance = math_node(tree, 'MULTIPLY', entry.outputs['Source Parallax Scale'], depth, 'Parallax host layer depth ' + str(index), 'Approx Source Parallax')
        delta = _vector(tree, 'SCALE', 'Parallax host view offset ' + str(index))
        tree.links.new(ray.outputs[0], delta.inputs[0])
        tree.links.new(distance, delta.inputs[3])
        coordinates = _vector(tree, 'SUBTRACT', 'Parallax source UV minus host depth ray ' + str(index))
        tree.links.new(uv.outputs['UV'], coordinates.inputs[0])
        tree.links.new(delta.outputs['Vector'], coordinates.inputs[1])
        mapping = node(tree, 'ShaderNodeMapping', 'Parallax exact source ST layer ' + str(index), 'Approx Source Parallax')
        mapping.vector_type = 'POINT'
        mapping.inputs['Scale'].default_value = (*binding.scale, 1.0)
        mapping.inputs['Location'].default_value = (*binding.offset, 0.0)
        tree.links.new(coordinates.outputs['Vector'], mapping.inputs['Vector'])
        layer = sample if index == 0 else tree.nodes.new('ShaderNodeTexImage')
        if index != 0:
            layer.image = sample.image
            layer.interpolation = sample.interpolation
            layer.extension = sample.extension
            for key in ('source_identity', 'source_sha256', 'source_slot'):
                layer[key] = sample[key]
        layer.label = '_ParallaxTex | host depth layer ' + str(index)
        layer['endfield_layout_section'] = 'Approx Source Parallax'
        tree.links.new(mapping.outputs['Vector'], layer.inputs['Vector'])
        weighted = color_mix(tree, 'MULTIPLY', 1.0, layer.outputs['Color'], (1.0 / count,) * 3 + (1.0,), 'Parallax equal RGB layer weight ' + str(index), 'Approx Source Parallax')
        combined = weighted if combined is None else color_mix(tree, 'ADD', 1.0, combined, weighted, 'Parallax host RGB accumulation ' + str(index), 'Approx Source Parallax')
    if combined is None:
        raise AssertionError('Validated parallax layer count did not produce a sample')
    colored = color_mix(tree, 'MULTIPLY', 1.0, combined, entry.outputs['Source Parallax Color'], 'Parallax host texture tint from source color', 'Approx Source Parallax')
    result = color_mix(tree, 'MIX', entry.outputs['Preview Parallax Strength'], base, colored, 'Parallax host layer mix over original base', 'Approx Source Parallax')
    tree['source_parallax_preview'] = json.dumps({
        'format': CONTRACT, 'source_identity': source.identity, 'texture_identity': binding.identity,
        'source_scale': scale, 'source_color': tint, 'source_march_count': int(count),
        'source_texture_scale': binding.scale, 'source_texture_offset': binding.offset,
        'host_layer_depths': [(index + 0.5) / count for index in range(int(count))],
        'host_rgb_weights': [1.0 / count] * int(count), 'host_strength': 0.5,
        'host_grazing_floor': 0.25, 'native_shader_semantics_verified': False, 'scope': SCOPE,
    }, sort_keys=True)
    return result
