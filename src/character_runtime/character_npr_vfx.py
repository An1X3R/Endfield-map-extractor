"""Translate CharacterNPR VFX source roles into the existing host VFX preview contract."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING
from character_material_profile import integer_value, object_value, text_value

if TYPE_CHECKING:
    from endfield_scene_material_contract import MaterialRecord, TextureSlot


CHARACTER_VFX_SHADER = 'HGRP/CharacterNPR_VFX'
MESH_EFFECT_SHADERS = frozenset(('HGRP/Effect/VFXBaseV2', 'HGRP/Effect/VFXDitherAlpha', CHARACTER_VFX_SHADER))
ROLE_SOURCES = (
    ('_UseDisturb', 0, '_DisturbTex1', '_DisturbUVSpeed1', '_DisturbUVWeights1', '_DisturbUVRotateMat1'),
    ('_UseMask', 3, '_MaskTex', '_MaskTexUVSpeed', '_MaskTexUVWeights', '_MaskTexUVRotateMat'),
    ('_UseBlend', 4, '_BlendTex', '_BlendTexUVSpeed', '_BlendTexUVWeights', '_BlendTexUVRotateMat'),
)


def validate_character_vfx_shader(shader_row: dict[str, object]) -> None:
    """Require the native property/type table before adapting the shared preview inputs."""
    rows = shader_row.get('property_defaults')
    if not isinstance(rows, list):
        raise ValueError('CharacterNPR_VFX requires the source Shader property table')
    required = {'_TintColor': 0, '_SurfaceType': 2, '_MainTex': 4,
                '_MainTexUVSpeed': 1, '_MainTexUVWeights': 1, '_MainTexUVRotateMat': 1,
                '_EnableNormalMap': 2, '_NormalMap': 4, '_NormalScale': 3,
                '_NormalMapUVSpeed': 1, '_NormalMapUVWeights': 1, '_NormalMapUVRotateMat': 1}
    for flag, _, texture, speed, weights, rotation in ROLE_SOURCES:
        required.update({flag: 2, texture: 4, speed: 1, weights: 1, rotation: 1})
    found: set[str] = set()
    for raw in rows:
        row = object_value(raw, 'Character VFX Shader property')
        name = text_value(row.get('m_Name'), 'Character VFX Shader property name')
        if name == '_OffsetSwitchDir' and row.get('m_Attributes') != ['Enum(Object, 0, World, 1, Normal, 2)']:
            raise ValueError('Character VFX source offset direction enum differs from the implemented native property contract')
        if name not in required:
            continue
        if name in found or integer_value(row.get('m_Type'), name + '.m_Type') != required[name]:
            raise ValueError(f'Character VFX Shader property is duplicated or has an incompatible type: property={name}')
        if required[name] == 4:
            texture = object_value(row.get('m_DefTexture'), name + '.m_DefTexture')
            if integer_value(texture.get('m_TexDim'), name + '.m_TexDim') != 2:
                raise ValueError(f'Character VFX requires source 2D textures: property={name}')
        found.add(name)
    if found != set(required):
        raise ValueError(f'Character VFX Shader property contract is incomplete: missing={sorted(set(required) - found)}')


def character_vfx_preview_record(record: MaterialRecord) -> MaterialRecord:
    """Map named source maps to explicit host role stages without editing source evidence."""
    if record['Shader'].get('Name') != CHARACTER_VFX_SHADER:
        raise ValueError(f'Character VFX adapter received another Shader family: material={record["Name"]}')
    floats, colors = record['Floats'], record['Colors']
    for flag in ('_UseDisturb', '_UseMask', '_UseBlend', '_Bi_Disturb', '_EnableNormalMap'):
        if floats[flag] not in (0.0, 1.0):
            raise ValueError(f'Character VFX source feature switch is not binary: material={record["Name"]}; property={flag}; value={floats[flag]}')
    for flag in ('_UseHeightColorGradient', '_UseEmission', '_UseVertexOffset', '_UseDissolve', '_UseCutOff', '_MainTexUseFlowmap'):
        if floats[flag] != 0.0:
            raise ValueError(f'Character VFX source branch has no host implementation: material={record["Name"]}; property={flag}; value={floats[flag]}')
    if floats['_EnableNormalMap'] == 1.0:
        if floats['_UseFresnel'] != 1.0:
            raise ValueError(f'Character VFX normal preview requires an active Fresnel consumer: material={record["Name"]}')
        if floats['_NormalMapUVSet'] != 0.0 or colors['_NormalMapUVWeights'] != [1.0, 0.0, 0.0, 0.0]:
            raise ValueError(f'Character VFX normal preview only supports source UV0: material={record["Name"]}')
        if colors['_NormalMapUVSpeed'][2:] != [0.0, 0.0]:
            raise ValueError(f'Character VFX normal preview has no source custom UV-speed stream: material={record["Name"]}')
        if not math.isfinite(floats['_NormalScale']) or floats['_NormalScale'] < 0.0:
            raise ValueError(f'Character VFX normal scale must be finite and nonnegative: material={record["Name"]}; value={floats["_NormalScale"]}')
    derived_floats = {f'_UseSampleTex{index}': 0.0 for index in range(6)}
    derived_colors: dict[str, list[float]] = {}
    aliases: list[TextureSlot] = []
    defaults = [dict(row) for row in record['Shader'].get('TextureDefaults', [])]
    active = tuple(row for row in ROLE_SOURCES if floats[row[0]] == 1.0)
    source_slots = list(record['Textures'])
    preview_floats = dict(floats)
    preview_colors = {key: list(value) for key, value in colors.items()}
    if floats['_UseDisturb'] == 1.0 and floats['_Bi_Disturb'] == 1.0:
        bindings = tuple(slot for slot in record['Textures'] if slot['Property'] == '_DisturbTex1')
        if len(bindings) != 1:
            raise ValueError(f'Bidirectional Character VFX requires its source disturbance map: material={record["Name"]}')
        source_slots.append({**bindings[0], 'Property': '_DisturbTex2'})
        active = (active[0], ('_UseDisturb', 1, '_DisturbTex2', '_DisturbUVSpeed2', '_DisturbUVWeights2', '_DisturbUVRotateMat2'), *active[1:])
        speed = colors['_DisturbUVSpeed1']
        preview_colors.update({'_DisturbUVSpeed2': [-speed[0], -speed[1], speed[2], speed[3]],
                               '_DisturbUVWeights2': list(colors['_DisturbUVWeights1']),
                               '_DisturbUVRotateMat2': list(colors['_DisturbUVRotateMat1'])})
        preview_floats.update({'_DisturbUIntensity1': floats['_DisturbUIntensity1'] * 0.5,
                               '_DisturbVIntensity1': floats['_DisturbVIntensity1'] * 0.5,
                               '_DisturbUIntensity2': floats['_DisturbUIntensity1'] * 0.5,
                               '_DisturbVIntensity2': floats['_DisturbVIntensity1'] * 0.5,
                               '_DisturbTex2Normal': floats['_DisturbTex1Normal']})
    for index, (_, role, texture, speed, weights, rotation) in enumerate(active):
        name = f'_SampleTex{index}'
        bindings = tuple(slot for slot in source_slots if slot['Property'] == texture)
        if len(bindings) != 1:
            raise ValueError(f'Character VFX role needs exactly one source binding: material={record["Name"]}; property={texture}; count={len(bindings)}')
        aliases.append({**bindings[0], 'Property': name})
        defaults.extend({**row, 'Property': name} for row in record['Shader'].get('TextureDefaults', []) if row['Property'] == texture)
        derived_floats.update({f'_UseSampleTex{index}': 1.0, name + 'Use': float(role),
                               name + 'UseDisturb': floats[texture + 'UseDisturb'] if role in (3, 4) else 0.0,
                               f'_UseSampleTex{index}AsAlpha': floats['_UseMaskTexAsAlpha'] if role == 3 else 0.0})
        derived_floats.update({name + f'UseWeight{slot}': float(slot == role) for slot in range(6)})
        derived_colors.update({name + 'UVSpeed': list(preview_colors[speed]), name + 'UVWeights': list(preview_colors[weights]),
                               name + 'UVRotateMat': list(preview_colors[rotation])})
    derived_floats['_UsePolarUV'] = float(any(value == [0.0, 0.0, 1.0, 0.0] for key, value in {**colors, **derived_colors}.items() if key.endswith('UVWeights')))
    active_uv_weights = [colors['_MainTexUVWeights'], *(derived_colors[f'_SampleTex{index}UVWeights']
                         for index in range(len(active)))]
    if all(weights[3] == 0.0 for weights in active_uv_weights):
        # A screen-coordinate modifier cannot affect named roles that never sample screen UVs.
        derived_floats['_UsePosYAsScreenV'] = 0.0
    # The host preview represents bidirectional flow as two opposed half-strength samples.
    derived_floats.update({'_UseDisturb2': float(any(row[1] == 1 for row in active)),
                           '_Bi_Disturb': 0.0, '_UseWeightTex': 0.0})
    if floats['_SurfaceType'] == 1.0 and floats['_ZWriteWithAlphaTest'] == 1.0:
        # Blender composites the color pass; its separate native depth draw is archived.
        derived_floats['_ZWrite'] = 0.0
    return {**record, 'Shader': {**record['Shader'], 'Name': 'HGRP/Effect/VFXBaseV2', 'TextureDefaults': defaults},
            'Textures': [*source_slots, *aliases], 'Floats': {**preview_floats, **derived_floats},
            'Colors': {**preview_colors, **derived_colors}}
