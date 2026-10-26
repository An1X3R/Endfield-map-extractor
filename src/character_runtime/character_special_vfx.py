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
from character_shader_uniforms import MARKER as UNIFORM_MARKER, resolve_shader_uniform_socket

if TYPE_CHECKING:
    from rebuild_character_materials import MaterialSource

SECTION = 'Approx Character Special VFX'
KEYWORD = '_CHARACTER_VFX_SPECIAL'
SLOTS = ('_VFXSpecialBlendTex', '_VFXSpecialMainTex')
FLOATS = ('_EnableCharacterVFX', '_UseVFXMainTexAsAlpha', '_VFXSpecialBlendTexRForDisturb',
          '_VFXFresnelBias', '_VFXFresnelPower', '_VFXFresnelFlip', '_VFXFresnelAffectOpacity',
          '_VFXColorAlpha', '_VFXColorIntensity', '_SpecialDissolveScheduleOffset',
          '_VFXFresnelUseNormalMap', '_VFXAlphaAffectTotalMat', '_VFXMainUVSet',
          '_EnableVFXColorAdjustment', '_AlphaPremultiply', '_VFXScreenUVUseDepth', '_VFXBlendUVSet')
COLORS = ('_VFXSpecialParam', '_VFXColor', '_VFXBlendTint', '_VFXFresnelColor')


class MissingSpecialVfxTextureError(ValueError):
    """Identify an unavailable optional texture without hiding malformed inputs."""


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
class SpecialVfxConstant:
    slot: str
    default: Literal['white']


@dataclass(frozen=True, slots=True)
class SpecialVfxSource:
    identity: str
    floats: Mapping[str, float]
    colors: Mapping[str, tuple[float, float, float, float]]
    textures: tuple[SpecialVfxTexture | SpecialVfxConstant, ...]
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
            textures: tuple[SpecialVfxTexture | SpecialVfxConstant, ...], uv_space: Literal['raw_uv0', 'base_map_st'],
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
    if values['_VFXMainUVSet'] not in (0.0, 1.0, 2.0, 3.0):
        raise ValueError(f'Special VFX main UV selector is outside the source enum: {identity}')
    unsupported = {name: values[name] for name in ('_VFXAlphaAffectTotalMat',
                   '_EnableVFXColorAdjustment', '_VFXBlendUVSet') if values[name] != 0.0}
    if values['_VFXFresnelUseNormalMap'] not in (0.0, 1.0):
        raise ValueError(f'Special VFX normal-map Fresnel requires a source toggle: {identity}')
    if (values['_VFXMainUVSet'] not in (0.0, 1.0)
            and not any(isinstance(texture, SpecialVfxConstant) and texture.slot == SLOTS[1] for texture in textures)):
        unsupported['_VFXMainUVSet'] = values['_VFXMainUVSet']
    if unsupported:
        raise ValueError(f'Active source special VFX switches are unsupported: material={identity}; switches={unsupported}')
    for name in ('_UseVFXMainTexAsAlpha', '_VFXFresnelFlip', '_VFXFresnelAffectOpacity', '_AlphaPremultiply'):
        if not 0.0 <= values[name] <= 1.0:
            raise ValueError(f'Special VFX source blend must lie within [0,1]: {identity}/{name}={values[name]}')
    if values['_VFXFresnelPower'] <= 0.0 or values['_VFXColorIntensity'] < 0.0 or values['_VFXColorAlpha'] < 0.0:
        raise ValueError(f'Special VFX source power/intensity/alpha is unsupported: material={identity}; values={values}')
    if tuple(texture.slot for texture in textures) != SLOTS:
        raise ValueError(f'Special VFX requires both exact source UV0 texture bindings: {identity}')
    for texture in textures:
        if isinstance(texture, SpecialVfxConstant):
            if texture.slot != SLOTS[1] or texture.default != 'white':
                raise ValueError(f'Special VFX constant requires the verified null main white default: {identity}/{texture.slot}')
            continue
        if texture.uv_layer != 'UV0':
            raise ValueError(f'Special VFX requires source UV0 texture bindings: {identity}/{texture.slot}')
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
    textures: list[SpecialVfxTexture | SpecialVfxConstant] = []
    for slot in SLOTS:
        matches = tuple(binding for binding in source.textures if binding.name == slot)
        sample = samples.get(slot)
        if len(matches) > 1 or (sample is not None and sample.type != 'TEX_IMAGE'):
            raise ValueError(f'Selected special VFX texture binding is malformed: {source.identity}/{slot}')
        if not matches and sample is None and slot == SLOTS[1] and source.null_texture_defaults.get(slot) == 'white':
            textures.append(SpecialVfxConstant(slot, 'white'))
            continue
        if not matches or sample is None or (sample.type == 'TEX_IMAGE' and sample.image is None):
            raise MissingSpecialVfxTextureError(f'Selected special VFX texture is unavailable: {source.identity}/{slot}')
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
    textures: list[SpecialVfxTexture | SpecialVfxConstant] = []
    defaults = _object(json.loads(group.get('source_null_texture_defaults', '{}')), 'null texture defaults')
    for slot in SLOTS:
        matches = tuple(item for item in group.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == slot)
        default = defaults.get(slot)
        if not matches and slot == SLOTS[1] and isinstance(default, dict) and default.get('default') == 'white':
            textures.append(SpecialVfxConstant(slot, 'white'))
            continue
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


def _sample_inputs(tree: bpy.types.NodeTree, texture: SpecialVfxTexture | SpecialVfxConstant,
                   uv: bpy.types.NodeSocket, time: bpy.types.NodeSocket,
                   speed: tuple[float, float], disturb: bpy.types.NodeSocket | float
                   ) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket]:
    if isinstance(texture, SpecialVfxConstant):
        # A verified uniform white sample is independent of UV selection, ST and time.
        color = node(tree, 'ShaderNodeRGB', texture.slot + ' verified shader default white', SECTION)
        color.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)
        alpha = node(tree, 'ShaderNodeValue', texture.slot + ' verified shader default alpha', SECTION)
        alpha.outputs[0].default_value = 1.0
        for item, channel in ((color, 'color'), (alpha, 'alpha')):
            item['special_vfx_source_slot'] = texture.slot
            item['special_vfx_constant_channel'] = channel
        return color.outputs[0], alpha.outputs[0]
    sample = _sample(tree, texture, uv, time, speed, disturb)
    return _sample_color(tree, sample, texture), sample.outputs['Alpha']


def _texture_record(tree: bpy.types.NodeTree, texture: SpecialVfxTexture | SpecialVfxConstant) -> dict[str, object]:
    if isinstance(texture, SpecialVfxConstant):
        channels = {item['special_vfx_constant_channel']: item.name for item in tree.nodes
                    if item.get('special_vfx_source_slot') == texture.slot and item.get('special_vfx_constant_channel')}
        return {'slot': texture.slot, 'default': texture.default, 'constant_nodes': channels,
                'evidence': 'Exact material null PPtr and material-referenced Shader property default'}
    return {'slot': texture.slot, 'identity': texture.identity, 'image': texture.image.name,
            'packed_sha256': hashlib.sha256(texture.image.packed_file.data).hexdigest(),
            'scale': texture.scale, 'offset': texture.offset, 'source_color_space': texture.color_space}


def _strength_control(tree: bpy.types.NodeTree, identity: str) -> bpy.types.ShaderNodeGroup:
    controls = bpy.data.node_groups.new('Special VFX Controls', 'ShaderNodeTree')
    controls['endfield_special_vfx_controls'] = identity
    controls.interface.new_socket(name='Strength', in_out='OUTPUT', socket_type='NodeSocketFloat')
    value = controls.nodes.new('ShaderNodeValue')
    value.label = 'Special VFX Strength'
    value['endfield_special_vfx_control'] = True
    value.outputs[0].default_value = 1.0
    output = controls.nodes.new('NodeGroupOutput')
    controls.links.new(value.outputs[0], output.inputs['Strength'])
    instance = node(tree, 'ShaderNodeGroup', 'Special VFX Strength', SECTION)
    instance.node_tree = controls
    return instance


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
    mapped_normal = values['_VFXFresnelUseNormalMap'] == 1.0
    if mapped_normal and (normal.node.type != 'NORMAL_MAP' or not normal.node.inputs['Color'].is_linked
            or not any(link.to_node.type == 'BSDF_PRINCIPLED' and link.to_socket.name == 'Normal'
                       for link in normal.links)):
        raise ValueError(f'Special VFX mapped Fresnel requires the existing decoded BSDF world normal: {source.identity}')
    surface_uv = node(tree, 'ShaderNodeMapping', 'Special VFX source surface UV before per-texture ST', SECTION)
    surface_uv.vector_type = 'POINT'
    surface_uv.inputs['Scale'].default_value = (*source.surface_scale, 1.0)
    surface_uv.inputs['Location'].default_value = (*source.surface_offset, 0.0)
    tree.links.new(uv, surface_uv.inputs['Vector'])
    uv = surface_uv.outputs[0]
    time = _time(tree, scene)
    velocity = colors['_VFXSpecialParam']
    blend_color, blend_alpha = _sample_inputs(tree, source.textures[0], uv, time, velocity[2:], 0.0)
    blend_split = node(tree, 'ShaderNodeSeparateColor', 'Special VFX decoded blend R disturbance', SECTION)
    tree.links.new(blend_color, blend_split.inputs[0])
    disturb = math_node(tree, 'MULTIPLY', blend_split.outputs['Red'], values['_VFXSpecialBlendTexRForDisturb'], 'Special VFX blend R shifts both UV axes', SECTION)
    main_uv = uv
    uv1 = None
    if values['_VFXMainUVSet'] == 1.0 and isinstance(source.textures[1], SpecialVfxTexture):
        uv1 = node(tree, 'ShaderNodeUVMap', 'Special VFX main UV2 / actual source UV1 host approximation', SECTION)
        uv1.uv_map = 'UV1'
        main_uv = uv1.outputs[0]
    main_color, main_alpha = _sample_inputs(tree, source.textures[1], main_uv, time, velocity[:2], disturb)
    main_split = node(tree, 'ShaderNodeSeparateColor', 'Special VFX decoded main R alpha', SECTION)
    tree.links.new(main_color, main_split.inputs[0])
    alpha = math_node(tree, 'ADD', main_alpha, math_node(tree, 'MULTIPLY', values['_UseVFXMainTexAsAlpha'], math_node(tree, 'SUBTRACT', main_split.outputs['Red'], main_alpha, 'Special VFX red minus alpha', SECTION), 'Special VFX alpha channel selection delta', SECTION), 'Special VFX alpha or red', SECTION)
    main_rgb = color_mix(tree, 'MIX', values['_UseVFXMainTexAsAlpha'], main_color, (1.0, 1.0, 1.0, 1.0), 'Special VFX main RGB or white', SECTION)
    facing = node(tree, 'ShaderNodeVectorMath', 'Special VFX geometric view dot normal', SECTION)
    facing.operation = 'DOT_PRODUCT'
    normal_transform = None
    for index, socket in enumerate((normal, view)):
        normalized = node(tree, 'ShaderNodeVectorMath', 'Special VFX normalized geometric direction', SECTION)
        normalized.operation = 'NORMALIZE'
        tree.links.new(socket, normalized.inputs[0])
        tree.links.new(normalized.outputs[0], facing.inputs[index])
        if index == 0:
            normal_transform = normalized
    fresnel = math_node(tree, 'POWER', _saturate(tree, math_node(tree, 'ADD', facing.outputs['Value'], values['_VFXFresnelBias'], 'Special VFX biased view dot', SECTION), 'Special VFX clamped facing'), values['_VFXFresnelPower'], 'Special VFX source Fresnel power', SECTION)
    inverse = math_node(tree, 'SUBTRACT', 1.0, fresnel, 'Special VFX inverse facing', SECTION)
    flipped = math_node(tree, 'ADD', inverse, math_node(tree, 'MULTIPLY', values['_VFXFresnelFlip'], math_node(tree, 'SUBTRACT', fresnel, inverse, 'Special VFX facing flip delta', SECTION), 'Special VFX facing flip', SECTION), 'Special VFX final Fresnel', SECTION)
    alpha_base = values['_VFXColorAlpha'] * colors['_VFXColor'][3]
    delta = math_node(tree, 'SUBTRACT', blend_split.outputs['Red'], values['_SpecialDissolveScheduleOffset'] * 2.02 - 1.01, 'Special VFX source dissolve delta', SECTION)
    edge = _saturate(tree, math_node(tree, 'MULTIPLY', delta, -1.0, 'Special VFX negative dissolve delta', SECTION), 'Special VFX dissolve edge')
    blend_factor = _saturate(tree, math_node(tree, 'MULTIPLY', math_node(tree, 'ADD', math_node(tree, 'MULTIPLY', alpha, alpha_base, 'Special VFX source alpha product', SECTION), blend_alpha, 'Special VFX blend alpha sum', SECTION), colors['_VFXBlendTint'][3], 'Special VFX blend tint alpha', SECTION), 'Special VFX blend factor')
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
    control = _strength_control(tree, source.identity)
    strength = math_node(tree, 'MULTIPLY', strength, control.outputs['Strength'], 'Special VFX adjustable strength', SECTION)
    emission = node(tree, 'ShaderNodeEmission', 'Special VFX additive source contribution / host approximation', SECTION)
    tree.links.new(tinted, emission.inputs['Color'])
    tree.links.new(strength, emission.inputs['Strength'])
    add = node(tree, 'ShaderNodeAddShader', 'Preserve ordinary lighting and emission plus special VFX', SECTION)
    tree.links.new(lit_shader, add.inputs[0])
    tree.links.new(emission.outputs[0], add.inputs[1])
    tree['source_special_vfx'] = json.dumps({'format': 'EndfieldCharacterSpecialVfx/1', 'material_identity': source.identity,
        'keyword': KEYWORD, 'source_values': dict(values), 'source_colors': dict(colors),
        'sum_node': add.name, 'emission_node': emission.name, 'time_node': time.node.name,
        'control_group': control.node_tree.name, 'control_node': next(item.name for item in control.node_tree.nodes
            if item.get('endfield_special_vfx_control')), 'control_instance': control.name,
        'ordinary_shader_node': lit_shader.node.name, 'ordinary_shader_socket': lit_shader.identifier,
        'surface_uv': {'space': source.uv_space, 'scale': source.surface_scale, 'offset': source.surface_offset,
                       'node': surface_uv.name,
                       'evidence': 'Fur vertex varying and UVSet=0 fragment path use BaseMap ST before VFX ST; ordinary variants retain their previous UV0 policy.'},
        'host_inputs': {'normal': {'route': 'existing_bsdf_decoded_world_normal' if mapped_normal else 'geometry_normal',
            'node': normal.node.name, 'socket': normal.identifier, 'normalize_node': normal_transform.name,
            'approximation': mapped_normal},
            'main_uv': {'route': 'source_uv1' if uv1 is not None else 'existing_surface_uv_or_constant',
                'node': uv1.name if uv1 is not None else surface_uv.name,
                'approximation': uv1 is not None},
            'blend_uv': 'existing_surface_uv0',
            'scope': 'Source Toggle and UV enum select host inputs; compiled variant equivalence is unverified.'},
        'textures': [_texture_record(tree, item) for item in source.textures],
        'time': 'Scene frame * fps_base / fps; native runtime time origin and per-draw time are not captured.',
        'inactive_switches': {'_VFXScreenUVUseDepth': 'UV0/UV1 sampling or a verified constant main sample is independent of screen depth.'},
        'reference': 'CharacterNPR special VFX source branch; Ruri_Endfield_Uber.glsl semantic translation',
        'native_shader_semantics_verified': False,
        'scope': 'Source keyword, actual textures, ST and material arithmetic drive an additive Blender shader. Host view/normal, sampling, time origin, exposure and native rasterization remain approximations.'}, sort_keys=True)
    return add.outputs[0]


def _socket_reaches(source: bpy.types.NodeSocket, target: bpy.types.NodeSocket) -> bool:
    pending = [source]
    visited: set[int] = set()
    while pending:
        output = pending.pop()
        if output.as_pointer() in visited:
            continue
        visited.add(output.as_pointer())
        for link in output.links:
            if link.to_socket == target:
                return True
            pending.extend(link.to_node.outputs)
    return False


def verify_special_vfx(tree: bpy.types.ShaderNodeTree, record: Mapping[str, object]) -> dict[str, object]:
    """Reject disconnected special emission or an altered saved source/time binding."""
    if json.loads(tree['source_special_vfx']) != dict(record):
        raise AssertionError(f'Saved special VFX provenance changed: {tree.name}')
    added = tree.nodes[str(record['sum_node'])]
    emitted = tree.nodes[str(record['emission_node'])]
    time = tree.nodes[str(record['time_node'])]
    if 'control_group' in record:
        controls = bpy.data.node_groups[str(record['control_group'])]
        value = controls.nodes[str(record['control_node'])]
        instance = tree.nodes[str(record['control_instance'])]
        links = tuple(instance.outputs['Strength'].links)
        if (controls.get('endfield_special_vfx_controls') != record['material_identity']
                or value.type != 'VALUE' or not value.get('endfield_special_vfx_control')
                or value.label != 'Special VFX Strength' or not math.isfinite(value.outputs[0].default_value)
                or instance.node_tree != controls or len(links) != 1
                or links[0].to_node.type != 'MATH' or links[0].to_node.operation != 'MULTIPLY'
                or not _socket_reaches(links[0].to_node.outputs[0], emitted.inputs['Strength'])
                or not any(link.from_node == value for output in controls.nodes if output.type == 'GROUP_OUTPUT'
                           for link in output.inputs['Strength'].links)):
            raise AssertionError(f'Saved special VFX strength control changed: {tree.name}')
    surface = record.get('surface_uv')
    if isinstance(surface, dict):
        mapping = tree.nodes[str(surface['node'])]
        if (mapping.type != 'MAPPING' or mapping.vector_type != 'POINT'
                or len(mapping.inputs['Vector'].links) != 1
                or tuple(mapping.inputs['Rotation'].default_value) != (0.0, 0.0, 0.0)
                or any(mapping.inputs[name].is_linked for name in ('Location', 'Rotation', 'Scale'))
                or any(abs(actual - expected) > 1e-6 for actual, expected in zip(mapping.inputs['Scale'].default_value[:2], surface['scale'], strict=True))
                or any(abs(actual - expected) > 1e-6 for actual, expected in zip(mapping.inputs['Location'].default_value[:2], surface['offset'], strict=True))
                or len(mapping.outputs[0].links) != (sum('default' not in item for item in record['textures'])
                    - int(record.get('host_inputs', {}).get('main_uv', {}).get('route') == 'source_uv1'))):
            raise AssertionError(f'Saved special VFX surface UV transform changed: {tree.name}')
    host = record.get('host_inputs')
    if isinstance(host, dict):
        normal_record = host['normal']
        normal = tree.nodes[normal_record['node']]
        output = next((socket for socket in normal.outputs if socket.identifier == normal_record['socket']), None)
        normalized = tree.nodes[normal_record['normalize_node']]
        if (output is None or normalized.type != 'VECT_MATH' or normalized.operation != 'NORMALIZE'
                or len(normalized.inputs[0].links) != 1 or normalized.inputs[0].links[0].from_socket != output
                or not _socket_reaches(normalized.outputs[0], emitted.inputs['Strength'])):
            raise AssertionError(f'Saved special VFX Fresnel normal disconnected: {tree.name}')
        if normal_record['route'] == 'existing_bsdf_decoded_world_normal' and (
                normal.type != 'NORMAL_MAP' or not normal.inputs['Color'].is_linked
                or not any(link.to_node.type == 'BSDF_PRINCIPLED' and link.to_socket.name == 'Normal'
                           for link in output.links)):
            raise AssertionError(f'Saved special VFX decoded BSDF normal changed: {tree.name}')
        if host['main_uv']['route'] == 'source_uv1':
            uv1 = tree.nodes[host['main_uv']['node']]
            axes, = (item for item in tree.nodes if item.label == SLOTS[1] + ' source UV')
            if (uv1.type != 'UVMAP' or uv1.uv_map != 'UV1' or len(axes.inputs[0].links) != 1
                    or axes.inputs[0].links[0].from_node != uv1):
                raise AssertionError(f'Saved special VFX main source UV1 changed: {tree.name}')
    for texture in record['textures']:
        if 'default' not in texture:
            continue
        channels = texture['constant_nodes']
        color = tree.nodes[channels['color']]
        alpha = tree.nodes[channels['alpha']]
        if (texture['slot'] != SLOTS[1] or texture['default'] != 'white'
                or color.type != 'RGB' or tuple(color.outputs[0].default_value) != (1.0, 1.0, 1.0, 1.0)
                or alpha.type != 'VALUE' or alpha.outputs[0].default_value != 1.0
                or not color.outputs[0].is_linked or not alpha.outputs[0].is_linked):
            raise AssertionError(f'Saved special VFX shader default changed: {tree.name}/{texture["slot"]}')
    if (added.type != 'ADD_SHADER' or emitted.type != 'EMISSION'
            or len(added.inputs[0].links) != 1 or len(added.inputs[1].links) != 1
            or added.inputs[1].links[0].from_node != emitted
            or added.inputs[0].links[0].from_node.name != record['ordinary_shader_node']
            or added.inputs[0].links[0].from_socket.identifier != record['ordinary_shader_socket']
            or not emitted.inputs['Color'].is_linked or not emitted.inputs['Strength'].is_linked):
        raise AssertionError(f'Saved special VFX no longer adds to the retained ordinary shader: {tree.name}')
    if UNIFORM_MARKER in time:
        resolve_shader_uniform_socket(tree, time.outputs[0])
        if json.loads(time[UNIFORM_MARKER])['expression'] != 'frame * fps_base / fps':
            raise AssertionError(f'Saved special VFX scene time expression changed: {tree.name}')
    else:
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
