"""Explicit artist presentation for retained full Fur materials, not native recovery."""
from __future__ import annotations

import json
import math
from typing import Literal, NamedTuple

import bpy

from character_node_graph import color_mix, math_node, node


class FurAppearance(NamedTuple):
    substrate_gain: float
    ember_gain: float
    ember_tint: tuple[float, float, float, float]
    outer_fade_power: float
    coverage_channel: Literal['SOURCE', 'ALPHA']


SECTION = 'Artist Fur Appearance'


class FurSurfaceDetail(NamedTuple):
    contrast: float
    relief_strength: float
    relief_distance: float


def apply_fur_surface_detail(shell: bpy.types.Object, settings: FurSurfaceDetail) -> dict[str, object]:
    """Add restrained source-mask relief to an explicit full-shell artist presentation."""
    if (any(not math.isfinite(value) or value < 0.0 for value in settings)
            or settings.contrast > 1.0 or settings.relief_strength > 1.0):
        raise ValueError(f'Invalid explicit Fur surface detail: {settings}')
    if shell.get('fur_surface_detail'):
        raise ValueError(f'Fur surface detail is already attached: {shell.name}')
    verify_fur_appearance(shell)
    source = json.loads(shell['source_fur_shell'])
    appearance = json.loads(shell['fur_artist_appearance'])
    tree = bpy.data.node_groups[appearance['group']]
    principled, = (item for item in tree.nodes if item.type == 'BSDF_PRINCIPLED')
    normal_link, = principled.inputs['Normal'].links
    original_normal = normal_link.from_socket
    albedo = tree.nodes[appearance['albedo_node']].outputs[0]
    targets = tuple(link.to_socket for link in albedo.links)
    if not targets:
        raise ValueError(f'Full Fur appearance has no albedo consumers: {tree.name}')
    names = ('Fur clump contrast', 'Fur relief strength', 'Fur relief distance')
    if any(tree.interface.items_tree.get(name) is not None for name in names):
        raise ValueError(f'Full Fur detail controls already exist: {tree.name}')
    controls = tuple(_control(tree, name, 'NodeSocketFloat', value)
                     for name, value in zip(names, settings, strict=True))
    section = 'Artist Fur Surface Detail'
    mask = tree.nodes[source['transmission']['fur_sample_node']].outputs['Alpha']
    centered = math_node(tree, 'SUBTRACT', mask, .5, 'Centered source fur mask', section)
    variation = math_node(tree, 'MULTIPLY', centered, controls[0], 'Restrained clump contrast', section)
    variation = math_node(tree, 'ADD', variation, 1.0, 'Preserve charcoal midtone', section)
    color = color_mix(tree, 'MULTIPLY', 1.0, albedo, variation, 'Source clump color relief', section)
    for target in targets:
        tree.links.new(color, target)
    bump = node(tree, 'ShaderNodeBump', 'Source fur mask / shallow surface relief', section)
    tree.links.new(controls[1], bump.inputs['Strength'])
    tree.links.new(controls[2], bump.inputs['Distance'])
    tree.links.new(mask, bump.inputs['Height'])
    tree.links.new(original_normal, bump.inputs['Normal'])
    tree.links.new(bump.outputs['Normal'], principled.inputs['Normal'])
    record: dict[str, object] = {
        'format': 'EndfieldFurSurfaceDetail/1', 'settings': settings._asdict(),
        'group': tree.name, 'color_node': color.node.name, 'bump_node': bump.name,
        'native_equivalence': False,
        'scope': 'Artist source-mask color contrast and shallow BSDF relief. Full shell, coverage, emission, original normal input and geometry retained; no strands or procedural texture.',
    }
    shell['fur_surface_detail'] = json.dumps(record, sort_keys=True)
    return record


def _control(tree: bpy.types.ShaderNodeTree, name: str, kind: str,
             value: float | tuple[float, float, float, float]) -> bpy.types.NodeSocket:
    if tree.interface.items_tree.get(name) is not None:
        raise ValueError(f'Fur appearance input already exists: {tree.name}/{name}')
    socket = tree.interface.new_socket(name=name, in_out='INPUT', socket_type=kind)
    socket.default_value = value
    socket.description = 'Editable Blender appearance approximation; original source parameters remain retained.'
    if kind == 'NodeSocketFloat':
        socket.min_value = 0.0
        socket.max_value = 8.0
    containers = [material.node_tree for material in bpy.data.materials if material.use_nodes]
    containers.extend(bpy.data.node_groups)
    for container in containers:
        for instance in container.nodes:
            if instance.type == 'GROUP' and instance.node_tree == tree:
                instance.inputs[name].default_value = value
    inputs = next((item for item in tree.nodes if item.type == 'GROUP_INPUT'), None)
    if inputs is None:
        raise ValueError(f'Fur appearance requires the existing full material inputs: {tree.name}')
    return inputs.outputs[name]


def _gain_albedo(tree: bpy.types.ShaderNodeTree, source: bpy.types.NodeSocket,
                 gain: float) -> str:
    targets = tuple(link.to_socket for link in source.links)
    if not targets:
        raise ValueError(f'Fur appearance albedo has no existing consumers: {tree.name}')
    control = _control(tree, 'Fur substrate brightness', 'NodeSocketFloat', gain)
    adjusted = color_mix(tree, 'MULTIPLY', 1.0, source, control,
                         'Artist substrate brightness / retained full shading', SECTION)
    for target in targets:
        tree.links.new(adjusted, target)
    return adjusted.node.name


def apply_fur_appearance(shell: bpy.types.Object, settings: FurAppearance) -> dict[str, object]:
    """Apply an explicitly selected visual profile without rewriting source scalars or maps."""
    for value in (settings.substrate_gain, settings.ember_gain, settings.outer_fade_power, *settings.ember_tint):
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f'Fur appearance values must be finite and nonnegative: {settings}')
    if settings.coverage_channel not in ('SOURCE', 'ALPHA') or settings.outer_fade_power <= 0.0:
        raise ValueError(f'Unsupported Fur appearance coverage or fade power: {settings}')
    if shell.type != 'MESH' or 'source_fur_shell' not in shell or shell.get('fur_artist_appearance'):
        raise ValueError(f'Fur appearance requires an unstyled, explicit source-shell mesh: {shell.name}')
    source = json.loads(shell['source_fur_shell'])
    tree = bpy.data.node_groups[source['source_group']]
    dye = tree.nodes[source['dye']['node']]
    special = source['special_vfx']
    emitted = tree.nodes[special['emission_node']]
    color, = emitted.inputs['Color'].links
    strength, = emitted.inputs['Strength'].links
    original_color, original_strength = color.from_socket, strength.from_socket
    layer = tree.nodes[source['transmission']['layer_node']].outputs['X']
    tint = _control(tree, 'Ember tint', 'NodeSocketColor', settings.ember_tint)
    gain = _control(tree, 'Ember brightness', 'NodeSocketFloat', settings.ember_gain)
    power = _control(tree, 'Ember outer-layer fade', 'NodeSocketFloat', settings.outer_fade_power)
    albedo_node = _gain_albedo(tree, dye.outputs[0], settings.substrate_gain)
    weight = math_node(tree, 'MAXIMUM', math_node(tree, 'SUBTRACT', 1.0, layer,
                       'Inner-fibre ember weight', SECTION), 0.0, 'Bounded inner-fibre weight', SECTION)
    weight = math_node(tree, 'POWER', weight, power, 'Artist outer-layer ember attenuation', SECTION)
    adjusted_strength = math_node(tree, 'MULTIPLY', math_node(tree, 'MULTIPLY', original_strength,
                                 weight, 'Source emission with fibre occlusion', SECTION), gain,
                                 'Artist ember exposure', SECTION)
    adjusted_color = color_mix(tree, 'MULTIPLY', 1.0, original_color, tint,
                              'Artist ember color / source pattern retained', SECTION)
    tree.links.new(adjusted_color, emitted.inputs['Color'])
    tree.links.new(adjusted_strength, emitted.inputs['Strength'])
    coverage_targets: list[dict[str, object]] = []
    if settings.coverage_channel == 'ALPHA':
        red = tree.nodes[source['transmission']['red_node']]
        fur = tree.nodes[source['transmission']['fur_sample_node']]
        for link in tuple(red.outputs['Red'].links):
            if link.to_node.get('endfield_layout_section') != 'Approx Fur Coverage':
                continue
            coverage_targets.append({'node': link.to_node.name, 'input': list(link.to_node.inputs).index(link.to_socket)})
            tree.links.new(fur.outputs['Alpha'], link.to_socket)
        if not coverage_targets:
            raise ValueError(f'Fur alpha presentation found no coverage consumers: {tree.name}')
    base_materials: list[dict[str, str]] = []
    for index, slot in enumerate(shell.material_slots):
        if index in source['material_slots'] or slot.material is None:
            continue
        original = slot.material
        material = original.copy()
        material.name = original.name + ' Artist Fur Substrate'
        instance, = (item for item in material.node_tree.nodes if item.type == 'GROUP'
                     and 'source_identity' in item.node_tree and 'Base Tint' in item.inputs)
        copied = instance.node_tree.copy()
        instance.node_tree = copied
        base, = (item for item in copied.nodes if item.label == 'Source base sample times BaseColor')
        base_node = _gain_albedo(copied, base.outputs[0], settings.substrate_gain)
        slot.link = 'OBJECT'
        slot.material = material
        base_materials.append({'original': original.name, 'material': material.name,
                               'group': copied.name, 'albedo_node': base_node})
    record: dict[str, object] = {
        'format': 'EndfieldFurArtistAppearance/1', 'settings': settings._asdict(),
        'material_mode': 'full', 'group': tree.name, 'albedo_node': albedo_node,
        'emission_color_node': adjusted_color.node.name, 'emission_strength_node': adjusted_strength.node.name,
        'base_materials': base_materials, 'coverage_targets': coverage_targets,
        'native_equivalence': False,
        'scope': 'Explicit artist approximation: charcoal substrate, orange embers beneath outer fibres. Source maps, full shading, time, geometry and skin remain retained. Alpha coverage is an artist-selected hypothesis, not a recovered GPU swizzle. No character-name dispatch or automatic global preset.',
    }
    shell['fur_artist_appearance'] = json.dumps(record, sort_keys=True)
    return record


def verify_fur_appearance(shell: bpy.types.Object) -> dict[str, object]:
    """Check actual appearance connections without treating the chosen look as source truth."""
    record = json.loads(shell['fur_artist_appearance'])
    source = json.loads(shell['source_fur_shell'])
    tree = bpy.data.node_groups[record['group']]
    emission = tree.nodes[source['special_vfx']['emission_node']]
    for input_name, key in (('Color', 'emission_color_node'), ('Strength', 'emission_strength_node')):
        links = emission.inputs[input_name].links
        if len(links) != 1 or links[0].from_node.name != record[key]:
            raise AssertionError(f'Fur artist emission input disconnected: {tree.name}/{input_name}')
    for row in record['coverage_targets']:
        link, = tree.nodes[row['node']].inputs[row['input']].links
        if link.from_node.name != source['transmission']['fur_sample_node'] or link.from_socket.name != 'Alpha':
            raise AssertionError(f'Fur artist alpha coverage disconnected: {tree.name}')
    for row in record['base_materials']:
        if not any(slot.material and slot.material.name == row['material'] for slot in shell.material_slots):
            raise AssertionError(f'Fur artist substrate material is not assigned: {shell.name}')
    return {'status': 'passed', 'material_mode': 'full', 'native_equivalence': False}
