"""Bind native mesh-channel ordinals to proven original Blender shape-key names."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import bpy

import basic_expression_shape_baker as baker
import character_animation_binding as binding
from character_authoring_identity import _resolve_renderer_identity
from character_facial_native import NativeFacialContext


@dataclass(frozen=True)
class NativeCorrectiveBinding:
    object_name: str
    key_data_name: str
    key_name: str
    native_index: int


def read_native_corrective_bindings(context: NativeFacialContext, source_audit: Path,
                                     mesh_channel_probe: Path,
                                     meshes: tuple[bpy.types.Object, ...]) -> tuple[NativeCorrectiveBinding, ...]:
    """Resolve renderer and mesh identities before assigning any native shape value."""
    if binding.file_hash(source_audit) != context.source.audit_sha256:
        raise ValueError('Native corrective renderer audit differs from the selected character context')
    audit = json.loads(source_audit.read_text(encoding='utf-8'))
    probe = json.loads(mesh_channel_probe.read_text(encoding='utf-8'))
    if probe['format'] != 'EndfieldUnityRawMeshShapeChannels/1':
        raise ValueError('Unsupported source mesh-channel probe format')
    renderer_map = {(row['object']['source'].casefold(), baker._integer(row['object']['path_id'], 'renderer ID')):row
                    for row in audit['renderers']}
    channels_by_mesh = {(row['source_cab'].casefold(), baker._integer(row['source_path_id'], 'mesh ID')):row
                        for row in probe['meshes']}
    audit_meshes = {(row['object']['source'].casefold(), baker._integer(row['object']['path_id'], 'mesh ID'))
                    for row in audit['meshes']}
    if set(channels_by_mesh) != audit_meshes:
        raise ValueError('Corrective channel probe must cover every exact source audit Mesh identity')
    indices = {index for _, index in context.corrective_map}
    result: list[NativeCorrectiveBinding] = []
    for obj in meshes:
        identity = _resolve_renderer_identity(obj, ())
        if identity is None:
            raise ValueError(f'Corrective mesh has no exact renderer identity: {obj.name}')
        renderer = renderer_map[identity]
        if renderer['object']['type'] != 'SkinnedMeshRenderer':
            continue
        mesh = renderer['mesh']
        source = channels_by_mesh[(mesh['source'].casefold(), baker._integer(mesh['path_id'], 'mesh ID'))]
        count = baker._integer(source['channel_count'], 'native channel count')
        if count == 0:
            continue
        channels = source['channels']
        if len(channels) != count or tuple(row['unity_channel_ordinal'] for row in channels) != tuple(range(count)):
            raise ValueError(f'Invalid native channel sequence: {obj.name}')
        keys = obj.data.shape_keys
        if keys is None:
            raise ValueError(f'Original native shape keys are absent: {obj.name}')
        for row in channels:
            index, name = row['unity_channel_ordinal'], row['name']
            block = keys.key_blocks.get(name)
            if block is None or keys.key_blocks.find(name) != index + 1:
                raise ValueError(f'Original source channel/key order differs: object={obj.name}, index={index}, name={name}')
            if index not in indices:
                continue
            if row['frame_count'] != 1 or len(row['frames']) != 1 or row['frames'][0]['full_weight'] != 100:
                raise ValueError(f'Corrective requires a single fullWeight=100 source frame: {obj.name}/{name}')
            path = block.path_from_id('value')
            if keys.animation_data and any(driver.data_path == path for driver in keys.animation_data.drivers):
                raise ValueError(f'Native corrective target already has a driver: {obj.name}/{name}')
            result.append(NativeCorrectiveBinding(obj.name, keys.name, name, index))
    return tuple(result)
