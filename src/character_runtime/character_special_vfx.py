"""Translate source-selected CharacterNPR special VFX into an additive host shader."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Mapping, cast

import bpy

from character_node_graph import color_mix, math_node, node

if TYPE_CHECKING:
    from rebuild_character_materials import MaterialSource

SECTION = 'Approx Character Special VFX'
KEYWORD = '_CHARACTER_VFX_SPECIAL'
SLOTS = ('_VFXSpecialBlendTex', '_VFXSpecialMainTex')
FLOATS = ('_EnableCharacterVFX', '_UseVFXMainTexAsAlpha', '_VFXSpecialBlendTexRForDisturb',
          '_VFXFresnelBias', '_VFXFresnelPower', '_VFXFresnelFlip', '_VFXFresnelAffectOpacity',
          '_VFXColorAlpha', '_VFXColorIntensity', '_SpecialDissolveScheduleOffset',
          '_VFXFresnelUseNormalMap', '_VFXAlphaAffectTotalMat', '_VFXMainUVSet',
          '_EnableVFXColorAdjustment', '_AlphaPremultiply', '_VFXScreenUVUseDepth')
COLORS = ('_VFXSpecialParam', '_VFXColor', '_VFXBlendTint', '_VFXFresnelColor')


@dataclass(frozen=True, slots=True)
class SpecialVfxTexture:
    slot: str
    image: bpy.types.Image
    scale: tuple[float, float]
    offset: tuple[float, float]
    uv_layer: str
    identity: str
    color_space: int


@dataclass(frozen=True, slots=True)
class SpecialVfxSource:
    identity: str
    floats: Mapping[str, float]
    colors: Mapping[str, tuple[float, float, float, float]]
    textures: tuple[SpecialVfxTexture, ...]
    uv_space: Literal['raw_uv0', 'base_map_st']
    surface_scale: tuple[float, float]
    surface_offset: tuple[float, float]


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'Special VFX requires a finite source scalar: {label}={value!r}')
    return float(value)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Special VFX requires a source JSON object: {label}')
    return cast(dict[str, object], value)


def _source(identity: str, floats: Mapping[str, object], colors: Mapping[str, object],
            textures: tuple[SpecialVfxTexture, ...], uv_space: Literal['raw_uv0', 'base_map_st'],
            surface_scale: tuple[float, float], surface_offset: tuple[float, float]) -> SpecialVfxSource:
    values = {name: _number(floats.get(name), identity + '/' + name) for name in FLOATS}
    converted: dict[str, tuple[float, float, float, float]] = {}
    for name in COLORS:
        raw = colors.get(name)
        if not isinstance(raw, (tuple, list)) or len(raw) != 4:
            raise ValueError(f'Special VFX requires a four-component source color: {identity}/{name}')
        converted[name] = cast(tuple[float, float, float, float],
                               tuple(_number(value, identity + '/' + name) for value in raw))
    if values['_EnableCharacterVFX'] != 1.0:
        raise ValueError(f'Special VFX keyword requires its enabled source switch: {identity}')
    unsupported = {name: values[name] for name in ('_VFXFresnelUseNormalMap', '_VFXAlphaAffectTotalMat',
                   '_VFXMainUVSet', '_EnableVFXColorAdjustment') if values[name] != 0.0}
    if unsupported:
        raise ValueError(f'Active source special VFX switches are unsupported: material={identity}; switches={unsupported}')
    for name in ('_UseVFXMainTexAsAlpha', '_VFXFresnelFlip', '_VFXFresnelAffectOpacity', '_AlphaPremultiply'):
        if not 0.0 <= values[name] <= 1.0:
            raise ValueError(f'Special VFX source blend must lie within [0,1]: {identity}/{name}={values[name]}')
    if values['_VFXFresnelPower'] <= 0.0 or values['_VFXColorIntensity'] < 0.0 or values['_VFXColorAlpha'] < 0.0:
        raise ValueError(f'Special VFX source power/intensity/alpha is unsupported: material={identity}; values={values}')
    if tuple(texture.slot for texture in textures) != SLOTS or any(texture.uv_layer != 'UV0' for texture in textures):
        raise ValueError(f'Special VFX requires both exact source UV0 texture bindings: {identity}')
    for texture in textures:
        if type(texture.color_space) is not int or texture.color_space not in (0, 1):
            raise ValueError(f'Special VFX requires native texture color-space metadata: {identity}/{texture.slot}')
        if texture.color_space == 0 and texture.image.colorspace_settings.name != 'Non-Color':
            raise ValueError(f'Special VFX linear source image has an incompatible host color role: {identity}/{texture.slot}')
        if texture.image.packed_file is None or texture.image.colorspace_settings.name not in ('sRGB', 'Non-Color'):
            raise ValueError(f'Special VFX requires its packed source image with an explicit color role: {identity}/{texture.slot}')
        if not all(math.isfinite(value) for value in (*texture.scale, *texture.offset)):
            raise ValueError(f'Special VFX source ST is not finite: {identity}/{texture.slot}')
    if not all(math.isfinite(value) for value in (*surface_scale, *surface_offset)):
        raise ValueError(f'Special VFX surface coordinate transform is not finite: {identity}')
    return SpecialVfxSource(identity, MappingProxyType(values), MappingProxyType(converted), textures,
                            uv_space, surface_scale, surface_offset)


def source_from_material(source: MaterialSource, samples: Mapping[str, bpy.types.Node]) -> SpecialVfxSource | None:
    """Read the selected source material without assigning behavior by object name."""
    if KEYWORD not in source.keywords:
        return None
    if source.shader != 'HGRP/CharacterNPR':
        raise ValueError(f'Special VFX source Shader family is unsupported: {source.identity}/{source.shader}')
    textures: list[SpecialVfxTexture] = []
    for slot in SLOTS:
        matches = tuple(binding for binding in source.textures if binding.name == slot)
        sample = samples.get(slot)
        if len(matches) != 1 or sample is None or sample.type != 'TEX_IMAGE' or sample.image is None:
            raise ValueError(f'Selected special VFX requires an actual source texture: {source.identity}/{slot}')
        binding = matches[0]
        textures.append(SpecialVfxTexture(slot, sample.image, binding.scale, binding.offset,
                                         f'UV{binding.uv_index}', json.dumps(binding.identity), sample.get('source_color_space')))
    if '_CHARACTER_FUR' in source.keywords:
        base, = (binding for binding in source.textures if binding.name == '_BaseMap')
        if base.uv_index != 0:
            raise ValueError(f'Source Fur special VFX requires BaseMap on UV0: {source.identity}')
        return _source(json.dumps(source.identity), source.floats, source.colors, tuple(textures),
                       'base_map_st', base.scale, base.offset)
    return _source(json.dumps(source.identity), source.floats, source.colors, tuple(textures),
                   'raw_uv0', (1.0, 1.0), (0.0, 0.0))


def source_from_group(group: bpy.types.ShaderNodeTree) -> SpecialVfxSource | None:
    """Reuse actual archived source-group images, static ST, scalars and keywords."""
    from patch_character_hgrp import source_keywords
    if KEYWORD not in source_keywords(group):
        return None
    if group.get('source_shader') != 'HGRP/CharacterNPR':
        raise ValueError(f'Special VFX requires the source CharacterNPR group: {group.name}')
    textures: list[SpecialVfxTexture] = []
    for slot in SLOTS:
        matches = tuple(item for item in group.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == slot)
        if len(matches) != 1 or matches[0].image is None or len(matches[0].inputs['Vector'].links) != 1:
            raise ValueError(f'Special VFX requires one bound actual source image: {group.name}/{slot}')
        sample = matches[0]
        mapping = sample.inputs['Vector'].links[0].from_node
        if (sample.projection != 'FLAT' or mapping.type != 'MAPPING' or mapping.vector_type != 'POINT'
                or any(mapping.inputs[name].is_linked for name in ('Location', 'Rotation', 'Scale'))
                or len(mapping.inputs['Vector'].links) != 1
                or tuple(mapping.inputs['Rotation'].default_value) != (0.0, 0.0, 0.0)):
            raise ValueError(f'Special VFX requires static source POINT ST without rotation: {group.name}/{slot}')
        uv = mapping.inputs['Vector'].links[0].from_node
        if uv.type != 'UVMAP':
            raise ValueError(f'Special VFX requires the actual source named UV layer: {group.name}/{slot}')
        image_identity = sample.get('source_identity')
        if not isinstance(image_identity, str) or not image_identity:
            raise ValueError(f'Special VFX source image identity is absent: {group.name}/{slot}')
        textures.append(SpecialVfxTexture(slot, sample.image, tuple(mapping.inputs['Scale'].default_value[:2]),
            tuple(mapping.inputs['Location'].default_value[:2]), uv.uv_map, image_identity, sample.get('source_color_space')))
    scale, offset = (1.0, 1.0), (0.0, 0.0)
    uv_space: Literal['raw_uv0', 'base_map_st'] = 'raw_uv0'
    if '_CHARACTER_FUR' in source_keywords(group):
        base, = (item for item in group.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == '_BaseMap')
        link, = base.inputs['Vector'].links
        mapping = link.from_node
        if (mapping.type != 'MAPPING' or mapping.vector_type != 'POINT'
                or any(mapping.inputs[name].is_linked for name in ('Location', 'Rotation', 'Scale'))
                or tuple(mapping.inputs['Rotation'].default_value) != (0.0, 0.0, 0.0)):
            raise ValueError(f'Source Fur special VFX requires a static BaseMap ST: {group.name}')
        original, = mapping.inputs['Vector'].links
        if original.from_node.type != 'UVMAP' or original.from_node.uv_map != 'UV0':
            raise ValueError(f'Source Fur special VFX requires BaseMap UV0: {group.name}')
        scale = tuple(mapping.inputs['Scale'].default_value[:2])
        offset = tuple(mapping.inputs['Location'].default_value[:2])
        uv_space = 'base_map_st'
    return _source(str(group['source_identity']), _object(json.loads(group['source_floats']), 'source floats'),
                   _object(json.loads(group['source_colors']), 'source colors'), tuple(textures), uv_space, scale, offset)


def _saturate(tree: bpy.types.NodeTree, value: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return math_node(tree, 'MINIMUM', math_node(tree, 'MAXIMUM', value, 0.0, label + ' lower', SECTION), 1.0, label, SECTION)


def _time(tree: bpy.types.NodeTree, scene: bpy.types.Scene) -> bpy.types.NodeSocket:
    value = node(tree, 'ShaderNodeValue', 'Scene seconds / source runtime origin unknown', SECTION)
    driver = value.outputs[0].driver_add('default_value').driver
    for name, path in (('fps', 'render.fps'), ('fps_base', 'render.fps_base')):
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'SINGLE_PROP'
        variable.targets[0].id_type = 'SCENE'
        variable.targets[0].id = scene
        variable.targets[0].data_path = path
    driver.expression = 'frame * fps_base / fps'
    return value.outputs[0]


def _sample(tree: bpy.types.NodeTree, texture: SpecialVfxTexture, uv: bpy.types.NodeSocket,
            time: bpy.types.NodeSocket, speed: tuple[float, float], disturb: bpy.types.NodeSocket | float) -> bpy.types.Node:
    axes = node(tree, 'ShaderNodeSeparateXYZ', texture.slot + ' source UV', SECTION)
    tree.links.new(uv, axes.inputs[0])
    combined = node(tree, 'ShaderNodeCombineXYZ', texture.slot + ' time before ST', SECTION)
    for index, axis in enumerate(('X', 'Y')):
        value = math_node(tree, 'ADD', axes.outputs[axis], disturb, texture.slot + ' shared RG disturbance ' + axis, SECTION)
        value = math_node(tree, 'ADD', value, math_node(tree, 'MULTIPLY', time, speed[index], texture.slot + ' source scroll ' + axis, SECTION), texture.slot + ' scroll before ST ' + axis, SECTION)
        value = math_node(tree, 'ADD', math_node(tree, 'MULTIPLY', value, texture.scale[index], texture.slot + ' source scale ' + axis, SECTION), texture.offset[index], texture.slot + ' source offset ' + axis, SECTION)
        tree.links.new(value, combined.inputs[index])
    sample = node(tree, 'ShaderNodeTexImage', 'Special VFX animated ' + texture.slot, SECTION)
    sample.image = texture.image
    sample.extension = 'REPEAT'
    sample.interpolation = 'Linear'
    sample['special_vfx_source_slot'] = texture.slot
    sample['source_identity'] = texture.identity
    tree.links.new(combined.outputs[0], sample.inputs['Vector'])
    return sample


def _sample_color(tree: bpy.types.NodeTree, sample: bpy.types.Node, texture: SpecialVfxTexture) -> bpy.types.NodeSocket:
    if texture.color_space == 0 or sample.image.colorspace_settings.name == 'sRGB':
        return sample.outputs['Color']
    split = node(tree, 'ShaderNodeSeparateColor', sample.label + ' raw RGB', SECTION)
    combined = node(tree, 'ShaderNodeCombineColor', sample.label + ' reference sRGB decode', SECTION)
    tree.links.new(sample.outputs['Color'], split.inputs[0])
    for channel in ('Red', 'Green', 'Blue'):
        raw = split.outputs[channel]
        low = math_node(tree, 'DIVIDE', raw, 12.92, 'Special VFX linear sRGB segment ' + channel, SECTION)
        raised = math_node(tree, 'DIVIDE', math_node(tree, 'ADD', raw, .055, 'Special VFX sRGB offset ' + channel, SECTION), 1.055, 'Special VFX sRGB scale ' + channel, SECTION)
        high = math_node(tree, 'POWER', raised, 2.4, 'Special VFX sRGB power ' + channel, SECTION)
        choice = math_node(tree, 'GREATER_THAN', raw, .04045, 'Special VFX sRGB segment ' + channel, SECTION)
        result = math_node(tree, 'ADD', low, math_node(tree, 'MULTIPLY', choice, math_node(tree, 'SUBTRACT', high, low, 'Special VFX sRGB delta ' + channel, SECTION), 'Special VFX selected sRGB delta ' + channel, SECTION), 'Special VFX decoded ' + channel, SECTION)
        tree.links.new(result, combined.inputs[channel])
    return combined.outputs[0]


def attach_special_vfx(tree: bpy.types.ShaderNodeTree, source: SpecialVfxSource | None,
                       lit_shader: bpy.types.NodeSocket, uv: bpy.types.NodeSocket,
                       normal: bpy.types.NodeSocket, view: bpy.types.NodeSocket,
                       alpha_premultiply: bpy.types.NodeSocket, scene: bpy.types.Scene) -> bpy.types.NodeSocket:
    """Add the selected source contribution while retaining lit emission and coverage."""
    if source is None:
        return lit_shader
    if tree.get('source_special_vfx') is not None:
        raise ValueError(f'Special VFX is already attached: {tree.name}')
    values, colors = source.floats, source.colors
    surface_uv = node(tree, 'ShaderNodeMapping', 'Special VFX source surface UV before per-texture ST', SECTION)
    surface_uv.vector_type = 'POINT'
    surface_uv.inputs['Scale'].default_value = (*source.surface_scale, 1.0)
    surface_uv.inputs['Location'].default_value = (*source.surface_offset, 0.0)
    tree.links.new(uv, surface_uv.inputs['Vector'])
    uv = surface_uv.outputs[0]
    time = _time(tree, scene)
    velocity = colors['_VFXSpecialParam']
    blend = _sample(tree, source.textures[0], uv, time, velocity[2:], 0.0)
    blend_color = _sample_color(tree, blend, source.textures[0])
    blend_split = node(tree, 'ShaderNodeSeparateColor', 'Special VFX decoded blend R disturbance', SECTION)
    tree.links.new(blend_color, blend_split.inputs[0])
    disturb = math_node(tree, 'MULTIPLY', blend_split.outputs['Red'], values['_VFXSpecialBlendTexRForDisturb'], 'Special VFX blend R shifts both UV axes', SECTION)
    main = _sample(tree, source.textures[1], uv, time, velocity[:2], disturb)
    main_color = _sample_color(tree, main, source.textures[1])
    main_split = node(tree, 'ShaderNodeSeparateColor', 'Special VFX decoded main R alpha', SECTION)
    tree.links.new(main_color, main_split.inputs[0])
    alpha = math_node(tree, 'ADD', main.outputs['Alpha'], math_node(tree, 'MULTIPLY', values['_UseVFXMainTexAsAlpha'], math_node(tree, 'SUBTRACT', main_split.outputs['Red'], main.outputs['Alpha'], 'Special VFX red minus alpha', SECTION), 'Special VFX alpha channel selection delta', SECTION), 'Special VFX alpha or red', SECTION)
    main_rgb = color_mix(tree, 'MIX', values['_UseVFXMainTexAsAlpha'], main_color, (1.0, 1.0, 1.0, 1.0), 'Special VFX main RGB or white', SECTION)
    facing = node(tree, 'ShaderNodeVectorMath', 'Special VFX geometric view dot normal', SECTION)
    facing.operation = 'DOT_PRODUCT'
    for index, socket in enumerate((normal, view)):
        normalized = node(tree, 'ShaderNodeVectorMath', 'Special VFX normalized geometric direction', SECTION)
        normalized.operation = 'NORMALIZE'
        tree.links.new(socket, normalized.inputs[0])
        tree.links.new(normalized.outputs[0], facing.inputs[index])
    fresnel = math_node(tree, 'POWER', _saturate(tree, math_node(tree, 'ADD', facing.outputs['Value'], values['_VFXFresnelBias'], 'Special VFX biased view dot', SECTION), 'Special VFX clamped facing'), values['_VFXFresnelPower'], 'Special VFX source Fresnel power', SECTION)
    inverse = math_node(tree, 'SUBTRACT', 1.0, fresnel, 'Special VFX inverse facing', SECTION)
    flipped = math_node(tree, 'ADD', inverse, math_node(tree, 'MULTIPLY', values['_VFXFresnelFlip'], math_node(tree, 'SUBTRACT', fresnel, inverse, 'Special VFX facing flip delta', SECTION), 'Special VFX facing flip', SECTION), 'Special VFX final Fresnel', SECTION)
    alpha_base = values['_VFXColorAlpha'] * colors['_VFXColor'][3]
    delta = math_node(tree, 'SUBTRACT', blend_split.outputs['Red'], values['_SpecialDissolveScheduleOffset'] * 2.02 - 1.01, 'Special VFX source dissolve delta', SECTION)
    edge = _saturate(tree, math_node(tree, 'MULTIPLY', delta, -1.0, 'Special VFX negative dissolve delta', SECTION), 'Special VFX dissolve edge')
    blend_factor = _saturate(tree, math_node(tree, 'MULTIPLY', math_node(tree, 'ADD', math_node(tree, 'MULTIPLY', alpha, alpha_base, 'Special VFX source alpha product', SECTION), blend.outputs['Alpha'], 'Special VFX blend alpha sum', SECTION), colors['_VFXBlendTint'][3], 'Special VFX blend tint alpha', SECTION), 'Special VFX blend factor')
    blend_term = color_mix(tree, 'MULTIPLY', 1.0, blend_color, colors['_VFXBlendTint'], 'Special VFX blend tint', SECTION)
    blend_term = color_mix(tree, 'MULTIPLY', 1.0, blend_term, blend_factor, 'Special VFX weighted blend term', SECTION)
    color_term = color_mix(tree, 'MULTIPLY', 1.0, main_rgb, tuple(value * values['_VFXColorIntensity'] for value in colors['_VFXColor']), 'Special VFX source main intensity', SECTION)
    main_term = color_mix(tree, 'ADD', 1.0, blend_term, color_term, 'Special VFX blend plus main term', SECTION)
    edge_color = color_mix(tree, 'MULTIPLY', 1.0, edge, tuple(value * values['_VFXColorIntensity'] for value in colors['_VFXFresnelColor']), 'Special VFX dissolve edge tint', SECTION)
    dissolved = color_mix(tree, 'MIX', edge, main_term, edge_color, 'Special VFX dissolved color', SECTION)
    fresnel_alpha = math_node(tree, 'MULTIPLY', flipped, colors['_VFXFresnelColor'][3], 'Special VFX Fresnel tint alpha', SECTION)
    tinted = color_mix(tree, 'MIX', fresnel_alpha, dissolved, colors['_VFXFresnelColor'], 'Special VFX Fresnel color mix', SECTION)
    opacity = _saturate(tree, math_node(tree, 'MULTIPLY', _saturate(tree, delta, 'Special VFX dissolve visibility'), math_node(tree, 'MULTIPLY', alpha, alpha_base, 'Special VFX opacity alpha', SECTION), 'Special VFX pre-Fresnel opacity', SECTION), 'Special VFX clamped opacity')
    opacity_fresnel = math_node(tree, 'ADD', 1.0, math_node(tree, 'MULTIPLY', values['_VFXFresnelAffectOpacity'], math_node(tree, 'SUBTRACT', flipped, 1.0, 'Special VFX opacity Fresnel delta', SECTION), 'Special VFX selected opacity Fresnel', SECTION), 'Special VFX opacity Fresnel', SECTION)
    strength = math_node(tree, 'MULTIPLY', math_node(tree, 'MULTIPLY', opacity, opacity_fresnel, 'Special VFX final opacity', SECTION), alpha_premultiply, 'Special VFX source alpha premultiply', SECTION)
    emission = node(tree, 'ShaderNodeEmission', 'Special VFX additive source contribution / host approximation', SECTION)
    tree.links.new(tinted, emission.inputs['Color'])
    tree.links.new(strength, emission.inputs['Strength'])
    add = node(tree, 'ShaderNodeAddShader', 'Preserve ordinary lighting and emission plus special VFX', SECTION)
    tree.links.new(lit_shader, add.inputs[0])
    tree.links.new(emission.outputs[0], add.inputs[1])
    tree['source_special_vfx'] = json.dumps({'format': 'EndfieldCharacterSpecialVfx/1', 'material_identity': source.identity,
        'keyword': KEYWORD, 'source_values': dict(values), 'source_colors': dict(colors),
        'sum_node': add.name, 'emission_node': emission.name, 'time_node': time.node.name,
        'ordinary_shader_node': lit_shader.node.name, 'ordinary_shader_socket': lit_shader.identifier,
        'surface_uv': {'space': source.uv_space, 'scale': source.surface_scale, 'offset': source.surface_offset,
                       'node': surface_uv.name,
                       'evidence': 'Fur vertex varying and UVSet=0 fragment path use BaseMap ST before VFX ST; ordinary variants retain their previous UV0 policy.'},
        'textures': [{'slot': item.slot, 'identity': item.identity, 'image': item.image.name,
                      'packed_sha256': hashlib.sha256(item.image.packed_file.data).hexdigest(),
                      'scale': item.scale, 'offset': item.offset, 'source_color_space': item.color_space} for item in source.textures],
        'time': 'Scene frame * fps_base / fps; native runtime time origin and per-draw time are not captured.',
        'inactive_switches': {'_VFXScreenUVUseDepth': 'Inactive for the admitted source _VFXMainUVSet=0.'},
        'reference': 'CharacterNPR special VFX source branch; Ruri_Endfield_Uber.glsl semantic translation',
        'native_shader_semantics_verified': False,
        'scope': 'Source keyword, actual textures, ST and material arithmetic drive an additive Blender shader. Host view/normal, sampling, time origin, exposure and native rasterization remain approximations.'}, sort_keys=True)
    return add.outputs[0]


def verify_special_vfx(tree: bpy.types.ShaderNodeTree, record: Mapping[str, object]) -> dict[str, object]:
    """Reject disconnected special emission or an altered saved source/time binding."""
    if json.loads(tree['source_special_vfx']) != dict(record):
        raise AssertionError(f'Saved special VFX provenance changed: {tree.name}')
    added = tree.nodes[str(record['sum_node'])]
    emitted = tree.nodes[str(record['emission_node'])]
    time = tree.nodes[str(record['time_node'])]
    surface = record.get('surface_uv')
    if isinstance(surface, dict):
        mapping = tree.nodes[str(surface['node'])]
        if (mapping.type != 'MAPPING' or mapping.vector_type != 'POINT'
                or len(mapping.inputs['Vector'].links) != 1
                or tuple(mapping.inputs['Rotation'].default_value) != (0.0, 0.0, 0.0)
                or any(mapping.inputs[name].is_linked for name in ('Location', 'Rotation', 'Scale'))
                or any(abs(actual - expected) > 1e-6 for actual, expected in zip(mapping.inputs['Scale'].default_value[:2], surface['scale'], strict=True))
                or any(abs(actual - expected) > 1e-6 for actual, expected in zip(mapping.inputs['Location'].default_value[:2], surface['offset'], strict=True))
                or len(mapping.outputs[0].links) != 2):
            raise AssertionError(f'Saved special VFX surface UV transform changed: {tree.name}')
    if (added.type != 'ADD_SHADER' or emitted.type != 'EMISSION'
            or len(added.inputs[0].links) != 1 or len(added.inputs[1].links) != 1
            or added.inputs[1].links[0].from_node != emitted
            or added.inputs[0].links[0].from_node.name != record['ordinary_shader_node']
            or added.inputs[0].links[0].from_socket.identifier != record['ordinary_shader_socket']
            or not emitted.inputs['Color'].is_linked or not emitted.inputs['Strength'].is_linked):
        raise AssertionError(f'Saved special VFX no longer adds to the retained ordinary shader: {tree.name}')
    drivers = tuple(curve.driver for curve in tree.animation_data.drivers
                    if curve.data_path == time.outputs[0].path_from_id('default_value')) if tree.animation_data is not None else ()
    if (len(drivers) != 1 or drivers[0].expression != 'frame * fps_base / fps'
            or not drivers[0].is_valid
            or {variable.name: variable.targets[0].data_path for variable in drivers[0].variables}
               != {'fps': 'render.fps', 'fps_base': 'render.fps_base'}):
        raise AssertionError(f'Saved special VFX scene time driver is disconnected: {tree.name}')
    pending = [added]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current.as_pointer() in seen:
            continue
        seen.add(current.as_pointer())
        if current.type in ('OUTPUT_MATERIAL', 'GROUP_OUTPUT') and current.is_active_output:
            return {'status': 'passed', 'source_identity': record['material_identity'],
                    'additive_shader_connected': True, 'ordinary_shader_retained': True, 'live_scene_time': True}
        pending.extend(link.to_node for socket in current.outputs for link in socket.links)
    raise AssertionError(f'Saved special VFX is disconnected from the active shader output: {tree.name}')
