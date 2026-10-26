"""Compose explicit authoring variants at the Blender export boundary."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path

import bpy
from mathutils import Vector

from character_material_options import CharacterMaterialOptions, parse_character_material_options
from character_simple_materials import (
    SimpleMaterialOptions, apply_simple_character_materials, bind_simple_face_light,
    simple_material_surfaces, simple_surface_color_inputs,
)
from character_simple_weather import attach_simple_weather_surface
from character_simple_layout import layout_simple_character_material
from character_shader_uniforms import migrate_shader_uniforms, verify_shader_uniforms


def read_material_options(path: Path | None) -> CharacterMaterialOptions | None:
    """Read the canonical export options without changing the legacy default."""
    if path is None:
        return None
    return parse_character_material_options(json.loads(path.read_text(encoding='utf-8')))


def native_weather_required(options: CharacterMaterialOptions | None) -> bool:
    """Select native weather construction only for explicit precision consumers."""
    return options is None or options.mode == 'original' or (
        options.weather.quality == 'full' and bool(options.weather.effects))


def source_materials(scene: bpy.types.Scene) -> tuple[bpy.types.Material, ...]:
    """Resolve actual character slots without touching unrelated scene materials."""
    return tuple({slot.material.name: slot.material for obj in scene.objects if obj.type == 'MESH'
                  for slot in obj.material_slots if slot.material is not None
                  and 'source_material_stack' in slot.material}.values())


def material_trees(material: bpy.types.Material) -> tuple[bpy.types.NodeTree, ...]:
    """Enumerate distinct stored dependencies of one live material."""
    pending = [material.node_tree]
    visited: dict[int, bpy.types.NodeTree] = {}
    while pending:
        tree = pending.pop()
        if tree.as_pointer() in visited:
            continue
        visited[tree.as_pointer()] = tree
        pending.extend(node.node_tree for node in tree.nodes if node.type == 'GROUP' and node.node_tree is not None)
    return tuple(visited.values())


def attach_full_response_simple_weather(scene: bpy.types.Scene, options: CharacterMaterialOptions) -> list[dict[str, object]]:
    """Feed optional surface weather into the existing native lighting consumers."""
    if options.lighting != 'full' or options.weather.quality != 'simple' or not options.weather.effects:
        return []
    trees = {node.node_tree.as_pointer(): node.node_tree for material in source_materials(scene)
             for node in material.node_tree.nodes if node.type == 'GROUP' and node.node_tree is not None
             and node.node_tree.get('source_shader') in ('HGRP/CharacterNPR', 'HGRP/CharacterNPR_Skin', 'HGRP/CharacterNPR_Hair')}
    rows: list[dict[str, object]] = []
    for tree in trees.values():
        surfaces = tuple(node for node in tree.nodes if node.type == 'BSDF_PRINCIPLED')
        if len(surfaces) != 1:
            raise ValueError(f'Full lighting weather needs one source surface: {tree.name}; found={len(surfaces)}')
        surface = surfaces[0]
        consumers: dict[str, tuple[bpy.types.NodeSocket, ...]] = {}
        pending = [link.from_node for socket in surface.inputs for link in socket.links]
        upstream: set[int] = set()
        while pending:
            node = pending.pop()
            if node.as_pointer() in upstream:
                continue
            upstream.add(node.as_pointer())
            pending.extend(link.from_node for socket in node.inputs for link in socket.links)
        for channel in ('Base Color', 'Roughness', 'Normal'):
            socket = surface.inputs[channel]
            consumers[channel] = tuple(link.to_socket for link in socket.links[0].from_socket.links
                                       if link.to_node.as_pointer() not in upstream) if socket.is_linked else ()
        row = attach_simple_weather_surface(tree, surface, options.weather.effects)
        instance = tree.nodes[row.group_node_name]
        for channel, output in (('Base Color', '基础颜色输出'), ('Roughness', '表面粗糙度输出'), ('Normal', '表面法线输出')):
            for target in consumers[channel]:
                tree.links.new(instance.outputs[output], target)
        rows.append(asdict(row))
    return rows


def apply_material_variant(scene: bpy.types.Scene, options: CharacterMaterialOptions,
                           prepared_weather: list[dict[str, object]]) -> dict[str, object]:
    """Apply the selected surface and lighting composition without rebuilding rigs."""
    if options.mode != 'simplified':
        raise ValueError('Material variant composition requires simplified mode')
    rows: list[dict[str, object]] = []
    limits: list[str] = []
    weather_rows = list(prepared_weather)
    if options.lighting == 'simple':
        sun = bpy.data.objects.get('EF HGRP Main Sun')
        if sun is None or sun.type != 'LIGHT' or sun.data.type != 'SUN':
            raise ValueError('Simplified face lighting requires the export main Sun')
        direction = sun.matrix_world.to_quaternion() @ Vector((0., 0., 1.))
        result = apply_simple_character_materials(scene, SimpleMaterialOptions(tuple(direction), .22))
        bind_simple_face_light(scene, sun)
        rows = [asdict(row) for row in result.materials]
        limits.extend(result.limitations)
        surfaces = simple_material_surfaces(scene)
        if options.weather.effects and options.weather.quality == 'simple':
            for surface in surfaces:
                if surface.family in ('eyes', 'overlay'):
                    continue
                color_inputs = simple_surface_color_inputs(surface)
                row = attach_simple_weather_surface(surface.material.node_tree, surface.bsdf, options.weather.effects)
                instance = surface.material.node_tree.nodes[row.group_node_name]
                for target in color_inputs:
                    surface.material.node_tree.links.new(instance.outputs['基础颜色输出'], target)
                weather_rows.append(asdict(row))
        elif options.weather.effects:
            from character_simple_full_weather import FullWeatherOptions, apply_full_weather_to_simple_materials
            full_weather = apply_full_weather_to_simple_materials(scene, FullWeatherOptions(options.weather.effects))
            weather_rows = [asdict(row) for row in full_weather.materials]
            limits.extend(full_weather.limitations)
    else:
        limits.append('Full lighting retains the existing mathematical kernels; only the authoring interface is simplified.')
        if options.weather.quality == 'full' and options.weather.effects:
            from character_simple_full_weather import FullWeatherOptions, filtered_weather_controls
            from character_weather_panel import weather_groups
            sources = {tree.as_pointer(): tree for material in source_materials(scene) for tree in weather_groups(material)}
            replacements = {pointer: filtered_weather_controls(tree, FullWeatherOptions(options.weather.effects))
                            for pointer, tree in sources.items()}
            for tree in (*bpy.data.node_groups, *(material.node_tree for material in source_materials(scene))):
                for node in tree.nodes:
                    if node.type == 'GROUP' and node.node_tree is not None and node.node_tree.as_pointer() in replacements:
                        node.node_tree = replacements[node.node_tree.as_pointer()]
            weather_rows.extend({'group_name': tree.name, 'effects': list(options.weather.effects)} for tree in replacements.values())
            limits.append('Precision weather retains shared native equations; disabled input controls are removed, not every internal arithmetic branch.')
        for material in source_materials(scene):
            material['endfield_full_response_authoring'] = True
            for node in material.node_tree.nodes:
                if node.get('endfield_authoring_parameters'):
                    node.hide = True
                    node.label = '高级原生参数 / ' + node.label.removeprefix('Material parameters / ')
                elif node.get('endfield_authoring_computation'):
                    node.label = '完整光照计算 / ' + node.label
            rows.append({'material': material.name})
    for material in source_materials(scene):
        if material.get('endfield_simple_material'):
            layout_simple_character_material(material)
        material['endfield_material_options'] = options.canonical_json()
    materials = source_materials(scene)
    uniforms = migrate_shader_uniforms(materials, tuple(scene.objects), scene)
    uniform_check = verify_shader_uniforms(materials, tuple(scene.objects), scene)
    report = {'format': 'EndfieldMaterialVariant/1', 'options': options.as_json(),
              'materials': rows, 'weather': weather_rows, 'limits': limits,
              'uniforms': uniforms, 'uniform_check': uniform_check,
              'stored_graph_nodes': {material.name: sum(len(tree.nodes) for tree in material_trees(material))
                                     for material in source_materials(scene)}}
    scene['endfield_material_variant'] = json.dumps(report, ensure_ascii=False)
    return report


def verify_material_variant(report: dict[str, object]) -> dict[str, object]:
    """Verify saved variant ownership, selected controls, images and live outputs."""
    if report.get('format') != 'EndfieldMaterialVariant/1':
        raise ValueError('Unsupported material variant verification receipt')
    options = parse_character_material_options(report['options'])
    rows = report['materials']
    if not isinstance(rows, list) or not rows:
        raise ValueError('Material variant receipt has no exported material rows')
    checked = []
    weather_checked: set[str] = set()
    selected_labels = {'wet': 'User Effect Z', 'snow': 'User Effect W', 'rain': 'User Effect X'}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('material'), str):
            raise TypeError('Material variant row requires a material name')
        material = bpy.data.materials.get(row['material'])
        if material is None or not material.use_nodes or material.users == 0:
            raise AssertionError(f'Exported material is missing or unused: {row["material"]}')
        if material.get('endfield_material_options') != options.canonical_json():
            raise AssertionError(f'Saved material options differ: {material.name}')
        outputs = tuple(node for node in material.node_tree.nodes if node.type == 'OUTPUT_MATERIAL' and node.is_active_output)
        if not outputs or not any(node.inputs['Surface'].is_linked for node in outputs):
            raise AssertionError(f'Exported material has no active surface: {material.name}')
        for tree in material_trees(material):
            if tree.get('endfield_simple_weather') or tree.get('endfield_weather_selected_effects'):
                values = {node.label: float(node.outputs[0].default_value) for node in tree.nodes
                          if node.type == 'VALUE' and node.label in selected_labels.values()}
                expected = {selected_labels[effect] for effect in options.weather.effects}
                if set(values) != expected or any(not math.isfinite(value) for value in values.values()):
                    raise AssertionError(f'Saved weather controls disagree with selection: {tree.name}; expected={expected}; actual={values}')
                weather_checked.add(tree.name)
            for node in tree.nodes:
                if node.type == 'GROUP' and node.node_tree is not None and node.node_tree.get('endfield_simple_weather'):
                    surface = tree.nodes.get(node.node_tree['endfield_simple_weather_surface'])
                    if surface is None:
                        raise AssertionError(f'Weather surface is missing: {material.name}/{node.name}')
                    for output, channel in (('基础颜色输出', 'Base Color'), ('表面粗糙度输出', 'Roughness'),
                                            ('表面法线输出', 'Normal'), ('不透明度输出', 'Alpha')):
                        if not any(link.to_socket == surface.inputs[channel] for link in node.outputs[output].links):
                            raise AssertionError(f'Weather output is disconnected: {material.name}/{node.name}/{output}')
                if node.type == 'TEX_IMAGE' and node.image is not None and not node.image.has_data and len(node.image.pixels) == 0:
                    raise AssertionError(f'Variant texture has no pixels: {material.name}/{node.image.name}')
        checked.append(material.name)
    if options.weather.effects and not weather_checked:
        raise AssertionError('Weather was requested but no live selected weather controls were saved')
    verify_shader_uniforms(tuple(bpy.data.materials[name] for name in checked),
                           tuple(bpy.context.scene.objects), bpy.context.scene)
    return {'status': 'passed', 'materials': checked, 'weather_groups': sorted(weather_checked), 'options': options.as_json()}
