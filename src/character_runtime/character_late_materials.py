"""Attach source-feature-gated Standard and Silk late material terms."""
from __future__ import annotations

from collections.abc import Callable
import json
import math

import bpy
from mathutils import Vector
from patch_character_hgrp import source_keywords


MAIN_NODE_LABEL = 'Compiled Standard ordinary main-light contribution'
SILK_NODE_LABEL = 'Compiled Silk main-light contribution'
LATE_VERSION = '1'
# Matches the existing Skin and Hair constructor initial values, not runtime state.
CP13_W_CONSTRUCTOR_INITIAL = 1.0
REQUIRED_SWITCHES = (
    '_UseBumpMap', '_UseCharacterFur', '_ClearCoat', '_Pantyhose',
    '_UseAnisotropy', '_SilkStockings', '_SilkStockingsAdvance',
    '_UseMetallicGlossMap', '_UseSpecRampMap',
)


def _source_switches(tree: bpy.types.ShaderNodeTree) -> dict[str, bool]:
    encoded = tree.get('source_floats')
    if not isinstance(encoded, str):
        raise TypeError(f'Standard source floats must be serialized JSON: {tree.name}')
    raw = json.loads(encoded)
    if not isinstance(raw, dict):
        raise TypeError(f'Standard source floats must be an object: {tree.name}')
    values: dict[str, bool] = {}
    for name in REQUIRED_SWITCHES:
        if name not in raw:
            continue
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'Standard source switch must be finite numeric: {tree.name}/{name}={value}')
        values[name] = float(value) > .5
    missing = tuple(name for name in REQUIRED_SWITCHES if name not in values)
    properties = json.loads(tree.get('source_shader_property_types', '{}'))
    if not isinstance(properties, dict):
        raise TypeError(f'Standard source Shader property types must be an object: {tree.name}')
    required_missing = tuple(name for name in missing if name != '_Pantyhose' or not properties or name in properties)
    if '_UseBumpMap' in missing or (values['_UseBumpMap'] and required_missing):
        raise ValueError(f'Standard selected source branch requires missing switches: {tree.name}/{missing}')
    return values


def _supported_standard_reason(switches: dict[str, bool]) -> str | None:
    if not switches['_UseBumpMap']:
        return 'Unmapped normal variant has no attachment proof'
    special = ('_UseCharacterFur', '_Pantyhose')
    enabled = tuple(name for name in special if name in switches and switches[name])
    if enabled:
        return f'Special source branches outside the current Standard path: {enabled}'
    silk = switches['_SilkStockings']
    if silk and (switches['_ClearCoat'] or switches['_UseAnisotropy']):
        return 'Combined Silk and special specular variant has no attachment proof'
    mapped = switches['_UseMetallicGlossMap']
    ramped = switches['_UseSpecRampMap']
    if silk and (switches['_SilkStockingsAdvance'] or not mapped):
        return 'Advanced or unmapped Silk variant has no attachment proof'
    if not ramped and not (silk and mapped):
        return 'No-SpecRamp variant requires mapped nonadvanced Silk'
    return None


def supported_standard_reason(tree: bpy.types.ShaderNodeTree) -> str | None:
    """Return why a source feature combination is outside the connected path."""
    return _supported_standard_reason(_source_switches(tree))


def _shared_cp13_w(visible: frozenset[str]) -> float:
    """Return the shared constructor initial and validate available copies."""
    groups = tuple(tree for tree in bpy.data.node_groups
                   if tree.name in visible and tree.get('source_shader') in (
                       'HGRP/CharacterNPR_Skin', 'HGRP/CharacterNPR_Hair'))
    labels = {
        'HGRP/CharacterNPR_Skin': 'Skin CP13 W - constructor initial',
        'HGRP/CharacterNPR_Hair': 'HGRP Hair CP13.w constructor initial value 1',
    }
    values: list[tuple[str, float]] = []
    for tree in groups:
        nodes = tuple(item for item in tree.nodes if item.label == labels[tree['source_shader']])
        if len(nodes) > 1:
            raise ValueError(f'Ambiguous CP13 W constructor nodes in visible group: {tree.name}')
        values.extend((tree.name, float(item.outputs[0].default_value)) for item in nodes)
    mismatches = tuple((group, value) for group, value in values
                       if not math.isfinite(value) or value != CP13_W_CONSTRUCTOR_INITIAL)
    if mismatches:
        raise ValueError(
            f'Visible Skin/Hair constructor CP13 W differs from the shared initial '
            f'{CP13_W_CONSTRUCTOR_INITIAL}: {mismatches}'
        )
    return CP13_W_CONSTRUCTOR_INITIAL


def attach_supported_late_materials(
    layout: Callable[[bpy.types.NodeTree], None],
    weather_controls: bpy.types.ShaderNodeTree | None,
    geometry_extent: float,
) -> list[dict[str, object]]:
    """Connect the proved ordinary Standard/Silk terms and explicit host lights."""
    from review_character_hgrp import visible_source_group_names
    from character_standard_lighting import attach_standard_main_light
    from character_stocking_lighting import correct_stocking_view, attach_stocking_lighting
    from character_standard_additional_lights import (
        build_standard_default_light, attach_standard_default_lights, StandardAdditionalSettings,
    )
    from character_punctual_kernel import build_skin_punctual
    from character_additional_light_state import AdditionalLightSettings, build_additional_light_state
    from patch_character_weather import patch_silk
    from character_weather_nodes import WeatherUniforms, WeatherOverride, make_weather_controls

    from character_wetfilm_profile import weather_control_source_record
    supplied_weather_source = weather_control_source_record(weather_controls) if weather_controls is not None else None
    native_weather = supplied_weather_source is not None and supplied_weather_source['kind'] == 'explicit_native_per_draw_word'

    visible = visible_source_group_names()
    groups = tuple(tree for tree in bpy.data.node_groups
                   if tree.name in visible and tree.get('source_shader') == 'HGRP/CharacterNPR')
    source_branches = tuple((tree, _source_switches(tree)) for tree in groups)
    cp13_w = (_shared_cp13_w(visible)
              if any(_supported_standard_reason(switches) is None for _, switches in source_branches) else None)
    states = tuple(sorted(
        (tree for tree in bpy.data.node_groups
         if tree.get('bound_light') in ('EF Fill Point', 'EF Fill Spot')),
        key=lambda tree: str(tree.get('bound_light')),
    ))
    if cp13_w is not None and not states:
        light_names = ('EF Fill Point', 'EF Fill Spot')
        missing_lights = tuple(name for name in light_names if bpy.data.objects.get(name) is None)
        if missing_lights:
            raise ValueError(f'Standard additional lights are missing their host objects: {missing_lights}')
        settings = AdditionalLightSettings(.004, geometry_extent * 2.5, 1.)
        states = tuple(build_additional_light_state(bpy.data.objects[name], settings) for name in light_names)
    if cp13_w is not None and (len(states) != 2 or len({str(state.get('bound_light')) for state in states}) != 2):
        raise ValueError(f'Expected two distinct explicit additional light states, found {len(states)}')

    records: list[dict[str, object]] = []
    kernel: bpy.types.ShaderNodeTree | None = None
    dry_weather: bpy.types.ShaderNodeTree | None = None
    for tree, switches in source_branches:
        reason = _supported_standard_reason(switches)
        if reason is None and native_weather and '_CHARACTER_FUR' in source_keywords(tree):
            reason = 'Explicit per-draw weather input is limited to the non-Fur Standard keyword family'
        source_identity = str(tree.get('source_identity', tree.name))
        features = dict(switches)
        if reason is not None:
            records.append({'group': tree.name, 'source_identity': source_identity,
                            'status': 'source_variant_not_implemented', 'source_features': features,
                            'source_switches_absent_and_unconsumed': [name for name in REQUIRED_SWITCHES if name not in switches],
                            'reason': reason})
            continue

        if cp13_w is None:
            raise ValueError('Supported Standard/Silk requires the shared HGRP CP13 W constructor initial')
        camera = tree.nodes.new('ShaderNodeVectorTransform')
        camera.label = 'Explicit host camera-facing axis'
        camera.vector_type = 'VECTOR'
        camera.convert_from = 'CAMERA'
        camera.convert_to = 'WORLD'
        camera.inputs[0].default_value = (0., 0., 1.)
        main = attach_standard_main_light(tree, camera.outputs[0], Vector((0., 0., 1.)), cp13_w, layout)
        silk: dict[str, object] | None = None
        if switches['_SilkStockings']:
            if dry_weather is None:
                dry_weather = weather_controls if weather_controls is not None else make_weather_controls(
                    WeatherUniforms((0., 0., 0., 0.), 0., 0., 0., 'Explicit dry research inputs; not native globals'),
                    WeatherOverride(1., (0., 0., 0., 0.), 0.), layout)
            preparation = patch_silk(tree, dry_weather, layout)
            weather_source = supplied_weather_source
            silk = {'preparation': preparation,
                    'weather_input_kind': 'explicit_native_per_draw_word' if native_weather else 'explicit_dry_artist_values',
                    'weather_controls': dry_weather.name,
                    'weather_source': weather_source,
                    'view': correct_stocking_view(tree, layout),
                    'lighting': attach_stocking_lighting(
                        tree, camera.outputs[0], Vector((0., 0., 1.)), cp13_w, layout)}

        if kernel is None:
            punctual = build_skin_punctual('EF Standard shared punctual')
            kernel = build_standard_default_light('EF Standard mode-zero additional lights', punctual)
        light_names = [str(state['bound_light']) for state in states]
        settings = tuple(
            StandardAdditionalSettings(
                .5, 1., 1., 2.,
                'Explicit host preview settings for the verified mode-zero adapter; not native light records',
            ) for _ in states
        )
        attach_standard_default_lights(tree, kernel, states, settings, layout)
        additional_labels = [f'Compiled Standard mode0 {name}' for name in light_names]
        required_labels = [MAIN_NODE_LABEL, *additional_labels]
        if silk is not None:
            required_labels.append(SILK_NODE_LABEL)
        tree['character_late_materials_version'] = LATE_VERSION
        records.append({
            'group': tree.name,
            'source_identity': source_identity,
            'status': 'connected',
            'source_features': features,
            'source_switches_absent_and_unconsumed': [name for name in REQUIRED_SWITCHES if name not in switches],
            'main': main,
            'lighting_fidelity': main['lighting_fidelity'],
            'unimplemented_specular_features': main['unimplemented_specular_features'],
            'silk': silk,
            'additional_mode': 0,
            'additional_lights': light_names,
            'required_node_labels': required_labels,
            'cp13_w': cp13_w,
            'cp13_provenance': 'Shared Skin/Hair CP13 W constructor initial value 1.0; not a runtime capture',
            'host_inputs': ('Explicit camera, up axis, native per-draw source word and light settings; runtime capture is unverified'
                            if native_weather else
                            'Explicit camera, up axis, dry weather and light settings; not native runtime state'),
        })
    return records


def _reachable_labels(socket: bpy.types.NodeSocket) -> frozenset[str]:
    if len(socket.links) != 1:
        raise AssertionError(f'Expected one live late-material path into {socket.name}')
    pending = [socket.links[0].from_node]
    visited: set[int] = set()
    labels: set[str] = set()
    while pending:
        current = pending.pop()
        pointer = current.as_pointer()
        if pointer in visited:
            continue
        visited.add(pointer)
        labels.add(current.label)
        pending.extend(link.from_node for item in current.inputs for link in item.links)
    return frozenset(labels)


def verify_late_materials(records: object) -> dict[str, object]:
    """Verify saved connected groups remain upstream of their NPR output bus."""
    from patch_character_hgrp import by_label

    if not isinstance(records, list):
        raise TypeError('Late-material receipt must be an array')
    connected: list[dict[str, object]] = []
    preserved: list[dict[str, object]] = []
    for item in records:
        if not isinstance(item, dict):
            raise TypeError('Late-material receipt entries must be objects')
        group_name = item.get('group')
        status = item.get('status')
        if not isinstance(group_name, str) or not group_name or not isinstance(status, str):
            raise TypeError('Late-material receipt requires group and status strings')
        if status != 'connected':
            preserved.append({'group': group_name, 'status': status})
            continue
        tree = bpy.data.node_groups.get(group_name)
        if tree is None or tree.get('source_shader') != 'HGRP/CharacterNPR':
            raise AssertionError(f'Saved late-material source group is missing: {group_name}')
        if tree.get('character_late_materials_version') != LATE_VERSION:
            raise AssertionError(f'Saved late-material version marker differs: {group_name}')
        source_identity = item.get('source_identity')
        if not isinstance(source_identity, str) or tree.get('source_identity') != source_identity:
            raise AssertionError(f'Saved late-material source identity differs: {group_name}')
        raw_labels = item.get('required_node_labels')
        if not isinstance(raw_labels, list) or not raw_labels or any(not isinstance(label, str) for label in raw_labels):
            raise TypeError(f'Late-material node labels are invalid: {group_name}')
        emission = by_label(tree, 'Artist-lit NPR diffuse - shared engines')
        reachable = _reachable_labels(emission.inputs['Color'])
        missing = tuple(label for label in raw_labels if label not in reachable)
        if missing:
            raise AssertionError(f'Saved late-material nodes are disconnected from the NPR bus: {group_name}/{missing}')
        silk = item.get('silk')
        if isinstance(silk, dict) and silk.get('weather_source') is not None:
            if silk.get('weather_input_kind') == 'explicit_native_per_draw_word' and '_CHARACTER_FUR' in source_keywords(tree):
                raise AssertionError(f'Saved native weather input targets a Fur keyword group: {group_name}')
            controls_name = silk.get('weather_controls')
            source_record = silk.get('weather_source')
            if not isinstance(controls_name, str) or not isinstance(source_record, dict):
                raise TypeError(f'Native Silk weather receipt is incomplete: {group_name}')
            controls = bpy.data.node_groups.get(controls_name)
            if controls is None:
                raise AssertionError(f'Saved native Silk weather controls are missing: {controls_name}')
            from character_wetfilm_profile import verify_weather_controls, weather_control_source_record
            verify_weather_controls(controls, source_record)
            if weather_control_source_record(controls)['kind'] != silk.get('weather_input_kind'):
                raise AssertionError(f'Saved Silk weather controls lost their source kind: {controls_name}')
            state = tuple(node for node in tree.nodes if node.type == 'GROUP' and node.label == 'Explicit weather uniforms')
            silk_node = by_label(tree, 'Fixed Ruri SilkStockingsSurface')
            wetness_links = tuple(silk_node.inputs['Wetness'].links)
            if len(state) != 1 or state[0].node_tree != controls or len(wetness_links) != 1 \
                    or wetness_links[0].from_node != state[0] or wetness_links[0].from_socket.name != 'Wetness':
                raise AssertionError(f'Saved native per-draw source is not connected to Silk wetness: {group_name}')
        connected.append({'group': group_name, 'source_identity': source_identity,
                          'status': 'connected_and_reachable', 'node_labels': raw_labels})
    return {'status': 'passed', 'connected_groups': connected, 'preserved_unsupported_groups': preserved}
