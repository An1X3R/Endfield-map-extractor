"""Standard mode-zero light arithmetic with an explicit Blender light bridge.

The compiled Standard/Silk branch reuses the main-light F0 times SpecRamp color.
Its additional lobe is isotropic GGX; the Silk tangent lobe is main-light only.
Native clustered records, cookie atlases, and shadow maps are not synthesized.
"""
from __future__ import annotations

import bpy
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import math

from character_node_graph import color_mix, math_node, node
from character_standard_lighting import LABEL as MAIN_LABEL
from patch_character_hgrp import by_label


SECTION = 'Standard Additional Light'


@dataclass(frozen=True)
class StandardAdditionalSettings:
    source_wrap: float
    color_normalize: float
    specular_weight: float
    falloff_exponent: float
    provenance: str


def build_standard_default_light(
    name: str, punctual: bpy.types.ShaderNodeTree,
) -> bpy.types.ShaderNodeTree:
    """Adapt verified shared GGX/attenuation arithmetic to Standard mode zero.

Standard fragment 62244-62338 uses quarter-wrap, alpha-premultiplied diffuse,
and normalization lerp(.75,.5,1-CastShadow). The existing shared kernel uses
half-wrap, so this adapter supplies half the source wrap parameter. The source
mode-zero shadow is one; Visibility remains an explicit host attenuation.
"""
    if not name.strip():
        raise ValueError('Standard additional-light group requires a name')
    if not str(punctual.get('scope', '')).startswith('Compiled plain Skin punctual response'):
        raise ValueError('Standard adapter requires the verified punctual arithmetic kernel')
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    replaced = {'Wrap', 'Global Normalize Scale', 'NPR Color', 'Wrap Normal'}
    for item in punctual.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name not in replaced:
            tree.interface.new_socket(name=item.name, in_out='INPUT', socket_type=item.socket_type)
    tree.interface.new_socket(name='NPR Color', in_out='INPUT', socket_type='NodeSocketColor')
    for label in ('Source Wrap', 'Cast Shadow', 'Base Alpha', 'Alpha Premultiply'):
        tree.interface.new_socket(name=label, in_out='INPUT', socket_type='NodeSocketFloat')
    for label, kind in (('Contribution', 'NodeSocketColor'), ('Attenuation', 'NodeSocketFloat'), ('GGX', 'NodeSocketFloat')):
        tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type=kind)
    entry = node(tree, 'NodeGroupInput', 'Explicit Standard inputs', SECTION)
    output = node(tree, 'NodeGroupOutput', 'Source Standard mode zero', SECTION)
    kernel = node(tree, 'ShaderNodeGroup', 'Shared punctual GGX and attenuation', SECTION)
    kernel.node_tree = punctual
    values = entry.outputs
    for socket in values:
        if socket.name in kernel.inputs and socket.name not in replaced:
            tree.links.new(socket, kernel.inputs[socket.name])
    tree.links.new(values['Normal'], kernel.inputs['Wrap Normal'])
    wrap = math_node(tree, 'MULTIPLY', .5, values['Source Wrap'], 'Quarter source wrap adaptation', SECTION)
    tree.links.new(wrap, kernel.inputs['Wrap'])
    cast = math_node(tree, 'MULTIPLY', .25, values['Cast Shadow'], 'Source normalization cast quarter', SECTION)
    normalization = math_node(tree, 'ADD', .5, cast, 'Source .5 plus .25 cast normalization', SECTION)
    tree.links.new(normalization, kernel.inputs['Global Normalize Scale'])
    alpha_delta = math_node(tree, 'SUBTRACT', values['Base Alpha'], 1., 'Source alpha minus one', SECTION)
    alpha_weight = math_node(tree, 'MULTIPLY', alpha_delta, values['Alpha Premultiply'], 'Source premultiplied alpha delta', SECTION)
    alpha = math_node(tree, 'ADD', 1., alpha_weight, 'Source diffuse alpha multiplier', SECTION)
    diffuse = color_mix(tree, 'MULTIPLY', 1., values['NPR Color'], alpha, 'Source premultiplied diffuse', SECTION)
    tree.links.new(diffuse, kernel.inputs['NPR Color'])
    for label in ('Contribution', 'Attenuation', 'GGX'):
        tree.links.new(kernel.outputs[label], output.inputs[label])
    tree['source_mode'] = 0
    tree['scope'] = 'Compiled Standard mode-zero response; shared verified isotropic GGX and explicit host punctual attenuation.'
    tree['source_branch'] = 'Standard Silk fragment 62244-62338; no additional Silk anisotropic lobe.'
    return tree


def _source(item: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
    links = tuple(item.inputs[name].links)
    if len(links) != 1:
        raise ValueError(f'Standard additional binding requires one live source: {item.label}/{name}')
    return links[0].from_socket


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'Standard additional source value must be finite: {label}={value}')
    return float(value)


def attach_standard_default_lights(
    tree: bpy.types.ShaderNodeTree, kernel: bpy.types.ShaderNodeTree,
    states: Sequence[bpy.types.ShaderNodeTree], settings: Sequence[StandardAdditionalSettings],
    layout: Callable[[bpy.types.NodeTree], None],
) -> bpy.types.NodeSocket:
    """Bind proved Standard surface inputs and append explicit host lights.

Main-light SpecRamp coordinates are deliberately reused for every local light,
matching the compiled branch. Each state supplies a Blender light selected by
the caller, not an invented native record. Existing source alpha stays intact.
"""
    if tree.get('source_shader') != 'HGRP/CharacterNPR':
        raise ValueError('Standard additional binding requires CharacterNPR')
    if tree.get('standard_additional_lights_connected'):
        raise ValueError(f'Standard additional lights already connected: {tree.name}')
    if not states or kernel.get('source_mode') != 0 or not str(kernel.get('scope', '')).startswith('Compiled Standard mode-zero'):
        raise ValueError('Standard binding requires explicit light states and the verified mode-zero kernel')
    if len(settings) != len(states):
        raise ValueError('Standard light states require one explicit settings record per light')
    for record in settings:
        numbers = (record.source_wrap, record.color_normalize, record.specular_weight, record.falloff_exponent)
        if not all(math.isfinite(value) for value in numbers) or not record.provenance.strip():
            raise ValueError('Standard light settings require finite values and explicit provenance')
        if not 0. <= record.color_normalize <= 1. or record.specular_weight < 0.:
            raise ValueError('Standard color normalization must be in [0,1] and specular weight nonnegative')
    main = by_label(tree, MAIN_LABEL)
    diffuse = by_label(tree, 'Verified local HGRP diffuse equations')
    surface = by_label(tree, 'CharacterNPR shared Eevee / Cycles surface')
    emission = by_label(tree, 'Artist-lit NPR diffuse - shared engines')
    original = _source(emission, 'Color')
    values = json.loads(tree['source_floats'])
    colors = json.loads(tree['source_colors'])
    premul = _finite(values['_AlphaPremultiply'], '_AlphaPremultiply')
    base_color = colors['_BaseColor']
    if not isinstance(base_color, list) or len(base_color) != 4:
        raise ValueError('Standard source BaseColor requires four channels')
    base_alpha = _finite(base_color[3], '_BaseColor.a')
    textures = tuple(n for n in tree.nodes if n.type == 'TEX_IMAGE' and n.label.startswith('_BaseMap |'))
    if len(textures) != 1 or textures[0].image is None:
        raise ValueError(f'Standard source BaseMap is missing or ambiguous: {tree.name}')
    state_names = tuple(str(state.get('bound_light', '')) for state in states)
    if any(not name for name in state_names) or len(set(state_names)) != len(state_names):
        raise ValueError('Standard lights require distinct named host light bindings')
    geometry = node(tree, 'ShaderNodeNewGeometry', 'Standard additional surface geometry', SECTION)
    alpha = math_node(tree, 'MULTIPLY', textures[0].outputs['Alpha'], base_alpha, 'Source additional base alpha', SECTION)
    bindings = {
        'Position': geometry.outputs['Position'], 'View': geometry.outputs['Incoming'],
        'Normal': _source(surface, 'Normal'), 'Specular Normal': _source(main, 'Normal'),
        'Specular Color': main.outputs['Specular Color'], 'NPR Color': diffuse.outputs['NPR Color'],
        'Cast Shadow': _source(diffuse, 'Cast Shadow'), 'Base Alpha': alpha,
    }
    total: bpy.types.NodeSocket | None = None
    for state, record in zip(states, settings, strict=True):
        light = node(tree, 'ShaderNodeGroup', 'Live additional Standard state ' + state['bound_light'], SECTION)
        light.node_tree = state
        response = node(tree, 'ShaderNodeGroup', 'Compiled Standard mode0 ' + state['bound_light'], SECTION)
        response.node_tree = kernel
        for label, source in bindings.items():
            tree.links.new(source, response.inputs[label])
        for target, source in (('Light Position', 'Position'), ('Light Axis', 'Toward Axis'), ('Light Color', 'Color')):
            tree.links.new(light.outputs[source], response.inputs[target])
        for label in ('Energy', 'Energy Scale', 'Range', 'Visibility', 'Enabled', 'Is Directional', 'Is Spot', 'Spot Size', 'Spot Blend'):
            tree.links.new(light.outputs[label], response.inputs[label])
        roughness = main.inputs['Perceptual Roughness']
        if roughness.is_linked:
            tree.links.new(_source(main, 'Perceptual Roughness'), response.inputs['Perceptual Roughness'])
        else:
            response.inputs['Perceptual Roughness'].default_value = roughness.default_value
        for label, value in (('Source Wrap', record.source_wrap), ('Color Normalize', record.color_normalize),
                             ('Specular Weight', record.specular_weight), ('Falloff Exponent', record.falloff_exponent),
                             ('Alpha Premultiply', premul)):
            response.inputs[label].default_value = value
        response['settings_provenance'] = record.provenance
        total = response.outputs['Contribution'] if total is None else color_mix(
            tree, 'ADD', 1., total, response.outputs['Contribution'], 'Sum Standard light ' + state['bound_light'], SECTION)
    if total is None:
        raise AssertionError('Standard light binding produced no contribution')
    added = color_mix(tree, 'ADD', 1., original, total, 'NPR bus plus additional Standard lights', SECTION)
    tree.links.new(added, emission.inputs['Color'])
    tree['standard_additional_lights_connected'] = len(states)
    layout(kernel)
    layout(tree)
    return total
