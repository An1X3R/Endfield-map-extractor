"""Bind source BC5 normals to the character VFX Fresnel preview."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

import bpy

from blender_hgrp_surface import packed_normal_group
from blender_scene_effect_layers import _sample, _uv

if TYPE_CHECKING:
    from endfield_scene_material_contract import MaterialRecord
    from rebuild_character_materials import MaterialSource, SourcePacket


def apply_character_vfx_fresnel_normal(
    material: bpy.types.Material,
    record: MaterialRecord,
    source: MaterialSource,
    packet: SourcePacket,
    texture_paths: Mapping[tuple[str, int], tuple[Path, str]],
    mesh_names: tuple[str, ...],
    snapshot_time: float,
) -> None:
    """Reuse source UV sampling and BC5 decoding with Blender's tangent frame."""
    if source.floats['_EnableNormalMap'] != 1.0 or source.floats['_UseFresnel'] != 1.0:
        raise ValueError(f'Character VFX normal binding requires enabled normal and Fresnel branches: material={source.identity}')
    bindings = tuple(binding for binding in source.textures if binding.name == '_NormalMap')
    if len(bindings) != 1:
        raise ValueError(f'Character VFX normal binding must resolve one actual texture: material={source.identity}; count={len(bindings)}')
    binding = bindings[0]
    texture = packet.textures[binding.identity]
    if binding.uv_index != 0 or texture.source_format != 'BC5' or texture.color_space != 0:
        raise ValueError(f'Character VFX normal preview requires UV0 BC5 linear data: material={source.identity}; texture={texture.identity}; uv={binding.uv_index}; format={texture.source_format}; color_space={texture.color_space}')
    for name in mesh_names:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'MESH' or obj.data.uv_layers.get('UV0') is None:
            raise ValueError(f'Character VFX tangent normal requires the actual imported UV0 layer: material={source.identity}; mesh={name}')
    tree = material.node_tree
    if tree is None:
        raise ValueError(f'Character VFX factory material has no node tree: material={material.name}')
    target = tree.nodes.get('Fresnel Source Normal')
    geometry = tree.nodes.get('Fresnel Surface Geometry')
    if target is None or target.type != 'VECT_MATH' or target.operation != 'SCALE' or geometry is None:
        raise ValueError(f'Character VFX factory Fresnel normal consumer is missing: material={source.identity}')
    links = tuple(target.inputs[0].links)
    if len(links) != 1 or links[0].from_node != geometry or links[0].from_socket.name != 'Normal':
        raise ValueError(f'Character VFX factory Fresnel normal has an unexpected upstream binding: material={source.identity}')
    uv = tree.nodes.new('ShaderNodeUVMap')
    uv.name = uv.label = 'Character VFX Normal Source UV0'
    uv.uv_map = 'UV0'
    zero = tree.nodes.new('ShaderNodeVectorMath')
    zero.name = zero.label = 'Character VFX Normal No Disturbance'
    zero.operation = 'ADD'
    zero.inputs[0].default_value = (0.0, 0.0, 0.0)
    zero.inputs[1].default_value = (0.0, 0.0, 0.0)
    coordinates = _uv(tree, record, '_NormalMap', uv.outputs[0], snapshot_time, zero.outputs[0], 0.0)
    sample = _sample(tree, record, '_NormalMap', dict(texture_paths), coordinates)
    packed = tree.nodes.new('ShaderNodeGroup')
    packed.name = packed.label = 'Character VFX BC5 RG Normal Decode'
    packed.node_tree = packed_normal_group()
    packed.inputs['Multiply Red By Alpha'].default_value = 0.0
    packed.inputs['Normal Strength'].default_value = source.floats['_NormalScale']
    tree.links.new(sample.rgba.rgb, packed.inputs['Packed RGB'])
    tree.links.new(sample.rgba.alpha, packed.inputs['Packed Alpha'])
    normal = tree.nodes.new('ShaderNodeNormalMap')
    normal.name = normal.label = 'Character VFX Tangent Normal UV0'
    normal.uv_map = 'UV0'
    tree.links.new(packed.outputs['Encoded Normal'], normal.inputs['Color'])
    tree.links.new(normal.outputs['Normal'], target.inputs[0])
    material['endfield_character_vfx_normal_preview'] = json.dumps({
        'source_texture': texture.identity, 'source_format': texture.source_format,
        'source_strength': source.floats['_NormalScale'], 'source_uv': 'UV0',
        'consumer': 'Fresnel Source Normal',
        'scope': 'Source BC5 RG decode and source UV transforms; Blender tangent frame drives host Fresnel color and opacity. Native CharacterNPR_VFX TBN, lighting and normal interpolation remain approximate.',
    }, sort_keys=True)
    limitations = json.loads(material['endfield_fx_limitations'])
    if not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations):
        raise ValueError(f'Character VFX factory limitation metadata is invalid: material={material.name}')
    limitations.extend(sample.limitations)
    limitations.append('CharacterNPR_VFX source BC5 normal drives host Fresnel through Blender UV0 tangents; native TBN, lighting and interpolated-normal parity are not reproduced.')
    material['endfield_fx_limitations'] = json.dumps(limitations)
