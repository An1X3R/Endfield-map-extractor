"""Bridge retained native weather surface equations onto compact stock shading.

The source pre-lighting color/scalar/normal channels are reused. Native additive
rain highlights and HGRP lighting closures are deliberately not reconstructed by
Principled. Full weather remains computationally heavy and is reported as such.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

import bpy

from character_node_graph import color_mix, math_node, node
from character_simple_materials import SimpleMaterialSurface, clone_source_sockets, simple_material_surfaces, simple_surface_color_inputs, softened_face_normal


WeatherEffect = Literal['wet', 'snow', 'rain']


@dataclass(frozen=True)
class FullWeatherOptions:
    effects: tuple[WeatherEffect, ...]


@dataclass(frozen=True)
class FullWeatherRow:
    material: str
    source_group: str
    family: str
    status: str
    added_nodes: int
    control_groups: tuple[str, ...]


@dataclass(frozen=True)
class FullWeatherReport:
    format: str
    effects: tuple[WeatherEffect, ...]
    materials: tuple[FullWeatherRow, ...]
    control_groups: tuple[str, ...]
    limitations: tuple[str, ...]


def _weather_groups(tree: bpy.types.ShaderNodeTree) -> tuple[bpy.types.ShaderNodeTree, ...]:
    pending = [tree]
    visited: set[int] = set()
    result: list[bpy.types.ShaderNodeTree] = []
    while pending:
        current = pending.pop()
        if current.as_pointer() in visited:
            continue
        visited.add(current.as_pointer())
        outputs = {item.name for item in current.interface.items_tree if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT'}
        if {'Native Global Wetness', 'Native Snow Intensity', 'Native Rain Intensity'} <= outputs:
            result.append(current)
        pending.extend(item.node_tree for item in current.nodes if item.type == 'GROUP' and item.node_tree is not None)
    return tuple(result)


def filtered_weather_controls(source: bpy.types.ShaderNodeTree, options: FullWeatherOptions) -> bpy.types.ShaderNodeTree:
    """Copy selected native weather controls with an explicit dry export state."""
    tree = source.copy()
    tree.name = 'EF Compact Full Weather / ' + source.name
    tree.use_fake_user = False
    tree['endfield_simple_full_weather_controls'] = True
    tree['selected_effects'] = ','.join(options.effects)
    tree['endfield_weather_selected_effects'] = json.dumps(options.effects)
    zero = node(tree, 'ShaderNodeValue', 'Disabled weather channels', 'Selected weather controls')
    zero.outputs[0].default_value = 0.
    selected_axes = {'X' for effect in options.effects if effect == 'rain'} | {'Z' for effect in options.effects if effect == 'wet'} | {'W' for effect in options.effects if effect == 'snow'}
    for item in tuple(tree.nodes):
        if item.type != 'VALUE':
            continue
        if item.label == 'User Override Weight':
            item.outputs[0].default_value = 1.
        if any(item.label == prefix + axis for prefix in ('Source Effect ', 'User Effect ') for axis in 'XYZW'):
            item.outputs[0].default_value = 0.
        disabled = any(item.label == prefix + axis for prefix in ('Source Effect ', 'User Effect ') for axis in 'XYZW' if axis not in selected_axes)
        if disabled:
            for link in tuple(item.outputs[0].links):
                tree.links.new(zero.outputs[0], link.to_socket)
            tree.nodes.remove(item)
    return tree


def _weather_source(surface: SimpleMaterialSurface) -> bpy.types.Node:
    original = bpy.data.materials[surface.material['source_full_material']]
    wet = tuple(item for item in original.node_tree.nodes if item.type == 'GROUP' and item.node_tree is not None
        and item.node_tree.get('source_identity') == surface.source_group['source_identity'] and 'compiled wet' in item.node_tree.name)
    if len(wet) > 1:
        raise ValueError(f'Compiled wet source instance is ambiguous: {original.name}')
    return wet[0] if wet else surface.source_instance


def _gate(tree: bpy.types.ShaderNodeTree, controls: tuple[bpy.types.Node, ...]) -> bpy.types.NodeSocket:
    if not controls:
        raise ValueError('A full-weather surface clone has no retained native weather control consumer')
    amount: bpy.types.NodeSocket | float = 0.
    for instance in controls:
        for label in ('Native Rain Plus Wetness', 'Native Snow Intensity'):
            amount = math_node(tree, 'MAXIMUM', amount, instance.outputs[label], 'Selected native weather amount', 'Full weather activation')
    return math_node(tree, 'GREATER_THAN', amount, 0., 'Use native weather program only when selected weather is active', 'Full weather activation')


def _apply_surface(surface: SimpleMaterialSurface, options: FullWeatherOptions,
    control_map: dict[int, bpy.types.ShaderNodeTree]) -> FullWeatherRow:
    instance = _weather_source(surface)
    source = instance.node_tree
    originals = _weather_groups(source)
    if not originals:
        return FullWeatherRow(surface.material.name, source.name, surface.family, 'source_has_no_bound_native_weather_surface', 0, ())
    for control in originals:
        if control.as_pointer() not in control_map:
            control_map[control.as_pointer()] = filtered_weather_controls(control, options)
    bsdfs = tuple(item for item in source.nodes if item.type == 'BSDF_PRINCIPLED')
    if len(bsdfs) != 1:
        raise ValueError(f'Full weather needs one retained pre-lighting Principled baseline: {source.name}')
    original_bsdf = bsdfs[0]
    labels = tuple(label for label in ('Base Color', 'Roughness', 'Metallic', 'Specular IOR Level', 'Normal')
        if original_bsdf.inputs[label].is_linked and not (surface.family == 'hair' and label in {'Roughness', 'Metallic'}))
    tree = surface.material.node_tree
    before = {item.as_pointer() for item in tree.nodes}
    cloned = clone_source_sockets(tree, tuple(original_bsdf.inputs[label] for label in labels), (instance,), control_map)
    controls = tuple(item for item in tree.nodes if item.as_pointer() not in before and item.type == 'GROUP'
        and item.node_tree is not None and item.node_tree.get('endfield_simple_full_weather_controls'))
    gate = _gate(tree, controls)
    for label, wet in zip(labels, cloned, strict=True):
        target = surface.bsdf.inputs[label]
        if not target.is_linked:
            raise ValueError(f'Compact surface has no explicit dry source channel: {surface.material.name}/{label}')
        dry = target.links[0].from_socket
        if surface.family == 'hair' and label == 'Specular IOR Level':
            wet = math_node(tree, 'MULTIPLY', wet, .5,
                'Source dielectric F0 0.04 times G at stock IOR 1.5', 'Full weather hair host response')
        if label == 'Base Color' and surface.family == 'face':
            wet = color_mix(tree, 'MULTIPLY', 1., wet, surface.channels.inputs['Face Shade'].links[0].from_socket,
                'Preserve compact face SDF after native weather surface', 'Full weather face bridge')
            wet = color_mix(tree, 'MULTIPLY', 1., wet, surface.channels.inputs['Face Brightness'].links[0].from_socket,
                'Preserve face brightness after native weather surface', 'Full weather face bridge')
        if label == 'Normal':
            if surface.family == 'face':
                wet = softened_face_normal(tree, wet, surface.channels.inputs['Face Forward'].links[0].from_socket,
                    surface.channels.inputs['Face Normal Detail'].links[0].from_socket)
            mixed = node(tree, 'ShaderNodeMix', 'Native weather normal program selection', 'Full weather channels')
            mixed.data_type = 'VECTOR'
            tree.links.new(gate, mixed.inputs[0]); tree.links.new(dry, mixed.inputs[4]); tree.links.new(wet, mixed.inputs[5])
            result = mixed.outputs[1]
        else:
            result = color_mix(tree, 'MIX', gate, dry, wet, 'Native weather channel / ' + label, 'Full weather channels')
        targets = simple_surface_color_inputs(surface) if label == 'Base Color' else (target,)
        for destination in targets:
            tree.links.new(result, destination)
    surface.channels['weather_precision'] = 'full_surface_equations'
    surface.channels['weather_source_group'] = source.name
    return FullWeatherRow(surface.material.name, source.name, surface.family, 'native_surface_equations_connected',
        len(tree.nodes) - len(before), tuple(sorted({item.node_tree.name for item in controls})))


def apply_full_weather_to_simple_materials(scene: bpy.types.Scene, options: FullWeatherOptions) -> FullWeatherReport:
    """Use selected source weather channels without changing the original full path."""
    if not options.effects or len(set(options.effects)) != len(options.effects) or any(effect not in {'wet', 'snow', 'rain'} for effect in options.effects):
        raise ValueError(f'Full weather requires unique selected wet/snow/rain effects: {options.effects}')
    surfaces = simple_material_surfaces(scene)
    if not surfaces:
        raise ValueError('Full weather bridging requires previously built simplified character surfaces')
    if any(surface.channels.get('weather_precision') is not None for surface in surfaces):
        raise ValueError('Weather is already attached to one or more simplified surfaces')
    control_map: dict[int, bpy.types.ShaderNodeTree] = {}
    rows = tuple(_apply_surface(surface, options, control_map) for surface in surfaces)
    if not any(row.status == 'native_surface_equations_connected' for row in rows):
        raise NotImplementedError('The retained scene has no bound native weather surface equations for compact shading')
    return FullWeatherReport('EndfieldCompactNativeWeatherSurfaceBridge/1', options.effects, rows,
        tuple(sorted(tree.name for tree in control_map.values())), (
            'Original native pre-lighting color/scalar/normal equations are retained; their kernels remain computationally heavy.',
            'Native separate Skin/Hair procedural-rain additive highlights, strand gains, native screen shadows and HGRP light loops are omitted by stock closures.',
            'Hair retains the artist stock roughness and zero physical metallic while native weather color/normal equations remain connected. Native packed R blends strand directions and alpha controls a second-lobe gain; these cannot become GGX roughness/metallic. Source specular level is converted to the stock dielectric F0 scale.',
            'Materials whose source has no bound native weather surface remain explicitly listed as unbound; no substitute effect is fabricated.',
            'Disabled weather input channels are removed from copied controls. Internal procedural equations are dependency-sliced, not algebraically constant-folded.',
            'Submersion is disabled because only wet, snow and rain are selected by this material option.',
        ))
