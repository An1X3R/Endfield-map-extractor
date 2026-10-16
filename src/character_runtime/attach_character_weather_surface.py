"""Bind weather surface channels to the currently evaluated HGRP material graph.

Projection coordinates and weather kernels are explicit caller inputs. This
adapter does not infer runtime volume state or replace unsupported families.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Callable

import bpy

from character_node_graph import node, math_node, color_mix
from character_weather_nodes import vector_math, smoothstep, combine_vector
from patch_character_hgrp import by_label, source_keywords
from patch_native_character_weather import coordinates


@dataclass(frozen=True)
class WeatherProjection:
    position_attribute: str
    normal_attribute: str
    orientation: float
    tiling: float
    provenance: str


@dataclass(frozen=True)
class SurfacePort:
    source: bpy.types.NodeSocket
    targets: tuple[bpy.types.NodeSocket, ...]


@dataclass(frozen=True)
class WeatherSurfacePorts:
    albedo: SurfacePort
    shadow: SurfacePort
    roughness: SurfacePort
    metallic: SurfacePort
    normal: SurfacePort


def linked_source(socket: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    if len(socket.links) != 1:
        raise ValueError(f"Weather surface needs one source: {socket.node.name}/{socket.name}; links={len(socket.links)}")
    return socket.links[0].from_socket


def outgoing(source: bpy.types.NodeSocket) -> tuple[bpy.types.NodeSocket, ...]:
    return tuple(link.to_socket for link in source.links)


def read_weather_surface_ports(tree: bpy.types.ShaderNodeTree) -> WeatherSurfacePorts:
    """Capture live consumers before creating any weather nodes, avoiding cycles."""
    family = tree.get('source_shader')
    if family not in ('HGRP/CharacterNPR', 'HGRP/CharacterNPR_Skin') or '_CHARACTER_FUR' in source_keywords(tree):
        raise ValueError(f"No verified weather attachment for family/keywords: {tree.name}/{family}")
    if tree.get('weather_surface_binding'):
        raise ValueError(f"Weather surface already bound: {tree.name}")
    surfaces = tuple(item for item in tree.nodes if item.type == 'BSDF_PRINCIPLED')
    if len(surfaces) != 1:
        raise ValueError(f"Weather surface is ambiguous: {tree.name}; count={len(surfaces)}")
    surface = surfaces[0]
    diffuse = by_label(tree, 'Source diffuse albedo coefficient')
    shadow = linked_source(by_label(tree, 'Scaled source shadow albedo').inputs['Color1'])
    metallic = linked_source(surface.inputs['Metallic'])
    roughness = linked_source(surface.inputs['Roughness'])
    if family == 'HGRP/CharacterNPR_Skin':
        albedo = linked_source(surface.inputs['Base Color'])
        if linked_source(diffuse.inputs['Color1']) != albedo:
            raise ValueError(f'Skin diffuse and physical albedo disagree: {tree.name}')
        color_targets = outgoing(albedo)
        if tree.get('wet_surface_binding') == 'HGRPSkinRainSurface/1':
            # Snow follows the rain surface output, never its dry upstream inputs.
            normal = by_label(tree, 'Native weather normal to HGRP surface frame result').outputs[0]
        elif tree.get('wet_surface_binding') == 'HGRPFaceRainSurface/1':
            normal = linked_source(by_label(tree, 'Compiled SDF Face rain').inputs['Dry Surface Normal'])
        else:
            normal = linked_source(by_label(tree, 'Compiled Skin main-light GGX').inputs['Normal'])
        floats = json.loads(tree['source_floats'])
        native_roughness = 1. - float(floats['_Smoothness'])
        rough_targets = list(outgoing(roughness))
        for item in tree.nodes:
            if item.type != 'GROUP' or 'Perceptual Roughness' not in item.inputs:
                continue
            socket = item.inputs['Perceptual Roughness']
            if socket.is_linked:
                if linked_source(socket) != roughness:
                    raise ValueError(f"Unexpected pre-weather roughness source: {tree.name}/{item.name}")
            elif not math.isclose(socket.default_value, native_roughness, abs_tol=1.e-6):
                raise ValueError(f"Native roughness disagrees with source: {tree.name}/{item.name}")
            if socket not in rough_targets:
                rough_targets.append(socket)
        metal_targets = (*outgoing(metallic), by_label(tree, 'Skin source metallic specular color').inputs[0])
    else:
        albedo = linked_source(surface.inputs['Base Color'])
        if linked_source(diffuse.inputs['Color1']) != albedo:
            raise ValueError(f"Standard HGRP albedo and physical surface disagree: {tree.name}")
        color_targets = tuple(link.to_socket for link in albedo.links if
            link.to_node == surface or link.to_node == diffuse or link.to_node.label.startswith('Approx shadow-to-lit'))
        if tree.get('wet_surface_binding') == 'HGRPStandardWetSurface/1':
            wet_nodes = tuple(item for item in tree.nodes
                              if item.type == 'GROUP' and item.label == 'Compiled wet film before HGRP surface lighting')
            if len(wet_nodes) != 1:
                raise ValueError(f'Standard wet surface node is ambiguous: {tree.name}; count={len(wet_nodes)}')
            wet = wet_nodes[0]
            for socket_name, output_name in (('Base Color', 'Albedo'), ('Roughness', 'Roughness')):
                if linked_source(surface.inputs[socket_name]) != wet.outputs[output_name]:
                    raise ValueError(f'Standard wet surface output is disconnected: {tree.name}/{socket_name}')
            shadow = wet.outputs['Shadow Color']
            roughness = wet.outputs['Roughness']
            metallic = wet.outputs['Metallic']
            if not metallic.links:
                raise ValueError(f'Standard wet surface metallic output is disconnected: {tree.name}')
        normal = linked_source(surface.inputs['Normal']) if surface.inputs['Normal'].is_linked else by_label(tree, 'Normalize evaluated surface normal').outputs[0]
        rough_targets = list(outgoing(roughness))
        metal_targets = outgoing(metallic)
    normal_targets = list(outgoing(normal))
    if family == 'HGRP/CharacterNPR_Skin':
        # Compiled Skin keeps the original normal for GGX and ambient hemisphere.
        # Snow modifies diffuse/rim normals; rain has a separate specular normal.
        normal_targets = [target for target in normal_targets if
            target.node.label not in ('Compiled Skin main-light GGX', 'Normalize final weather surface normal',
                                     'Skin rim normal dot view', 'Compiled SDF Face rain')
            and target.name != 'Specular Normal']
    if surface.inputs['Normal'] not in normal_targets:
        normal_targets.append(surface.inputs['Normal'])
    return WeatherSurfacePorts(SurfacePort(albedo, color_targets), SurfacePort(shadow, outgoing(shadow)),
        SurfacePort(roughness, tuple(rough_targets)), SurfacePort(metallic, metal_targets),
        SurfacePort(normal, tuple(normal_targets)))


def create_weather_instance(tree: bpy.types.ShaderNodeTree, kernel: bpy.types.ShaderNodeTree,
                            weather_state: bpy.types.ShaderNodeTree, projection: WeatherProjection,
                            ports: WeatherSurfacePorts, instance_label: str) -> tuple[bpy.types.Node, bpy.types.Node]:
    """Provide explicit projection, baseline channels and state to a surface kernel."""
    if not projection.position_attribute or not projection.normal_attribute or not projection.provenance.strip():
        raise ValueError('Weather projection requires explicit attributes and provenance')
    if projection.orientation not in (0., 1.) or not math.isfinite(projection.tiling) or projection.tiling <= 0.:
        raise ValueError(f'Invalid weather projection controls: {projection}')
    floats = json.loads(tree['source_floats'])
    disable = float(floats['_DisableRainEffectOnMaterial'])
    if not math.isfinite(disable):
        raise ValueError(f'Nonfinite source weather-disable control: {tree.name}')
    section = 'HGRP Weather Surface'
    instance = node(tree, 'ShaderNodeGroup', instance_label, section)
    instance.node_tree = kernel
    state = node(tree, 'ShaderNodeGroup', 'Shared explicit weather controls', section)
    state.node_tree = weather_state
    geometry = node(tree, 'ShaderNodeNewGeometry', 'Evaluated weather surface geometry', section)
    height = node(tree, 'ShaderNodeSeparateXYZ', 'Weather world height', section)
    tree.links.new(geometry.outputs['Position'], height.inputs[0])
    tree.links.new(height.outputs['Z'], state.inputs['World Height'])
    for label, attribute in (('Projection Position', projection.position_attribute), ('Projection Normal', projection.normal_attribute)):
        value = node(tree, 'ShaderNodeAttribute', label + ' / explicit host binding', section)
        value.attribute_name = attribute
        tree.links.new(value.outputs['Vector'], instance.inputs[label])
    normal = coordinates(tree, ports.normal.source, (0, 2, 1), (1., 1., -1.), 'HGRP surface normal to native weather frame')
    tree.links.new(normal, instance.inputs['Geometric Normal'])
    if 'Front Facing' in instance.inputs:
        tree.links.new(math_node(tree, 'SUBTRACT', 1., geometry.outputs['Backfacing'], 'Weather front-facing surface', section), instance.inputs['Front Facing'])
    if 'Base Texture Alpha' in instance.inputs:
        base = tuple(item for item in tree.nodes if item.get('source_slot') == '_BaseMap')
        if len(base) == 1 and base[0].type == 'TEX_IMAGE' and base[0].image is not None and base[0].image.packed_file is not None:
            tree.links.new(base[0].outputs['Alpha'], instance.inputs['Base Texture Alpha'])
        elif not base:
            defaults = json.loads(tree.get('source_null_texture_defaults', '{}'))
            default = defaults.get('_BaseMap') if isinstance(defaults, dict) else None
            if not isinstance(default, dict) or default.get('default') != 'white':
                raise ValueError(f'Weather BaseMap alpha requires its exact source image or verified white Shader default: {tree.name}')
            alpha = by_label(tree, 'Exact Shader default _BaseMap alpha: one')
            if alpha.type != 'VALUE' or alpha.outputs[0].default_value != 1.0:
                raise ValueError(f'Weather source white Shader alpha node changed: {tree.name}')
            tree.links.new(alpha.outputs[0], instance.inputs['Base Texture Alpha'])
            instance['source_base_alpha'] = 'Exact material-referenced Shader white default alpha; no source image synthesized.'
        else:
            raise ValueError(f'Weather BaseMap alpha is ambiguous or has no packed source image: {tree.name}')
    for label, value in (('Orientation Flag', projection.orientation), ('Global Tiling', projection.tiling), ('Disable Weather', disable)):
        instance.inputs[label].default_value = value
    for label, port in (('Albedo', ports.albedo), ('Shadow Color', ports.shadow), ('Roughness', ports.roughness), ('Metallic', ports.metallic)):
        tree.links.new(port.source, instance.inputs[label])
    return instance, state


def connect_weather_consumers(tree: bpy.types.ShaderNodeTree, instance: bpy.types.Node,
                              ports: WeatherSurfacePorts) -> list[dict[str, object]]:
    """Replace captured live lighting inputs without rewiring kernel source inputs."""
    rows = []
    for label, port in (('Albedo', ports.albedo), ('Shadow Color', ports.shadow), ('Roughness', ports.roughness), ('Metallic', ports.metallic)):
        for target in port.targets:
            tree.links.new(instance.outputs[label], target)
        rows.append({'channel': label, 'consumers': [target.node.label + '/' + target.name for target in port.targets]})
    final_normal = coordinates(tree, instance.outputs['Normal'], (0, 2, 1), (1., -1., 1.), 'Native weather normal to HGRP surface frame')
    for target in ports.normal.targets:
        tree.links.new(final_normal, target)
    rows.append({'channel': 'Normal', 'consumers': [target.node.label + '/' + target.name for target in ports.normal.targets]})
    return rows


def bind_weather_surface(tree: bpy.types.ShaderNodeTree, snow_kernel: bpy.types.ShaderNodeTree,
                         weather_state: bpy.types.ShaderNodeTree, projection: WeatherProjection,
                         layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Insert compiled snow after source rim albedo and before surface lighting."""
    ports = read_weather_surface_ports(tree)
    instance, state = create_weather_instance(tree, snow_kernel, weather_state, projection, ports,
                                              'Compiled snow before HGRP surface lighting')
    tree.links.new(state.outputs['Native Snow Intensity'], instance.inputs['Snow Intensity'])
    if 'Snow Coverage Mask' in instance.inputs:
        if '_SDFLIGHTMAP' in source_keywords(tree):
            mask = math_node(tree, 'MULTIPLY', by_label(tree, 'Actual SDFMask G surface blend').outputs['Green'],
                             by_label(tree, 'Native SDFMask rim gate').outputs[0], 'Native face snow mask', 'HGRP Weather Surface')
            tree.links.new(mask, instance.inputs['Snow Coverage Mask'])
        else:
            instance.inputs['Snow Coverage Mask'].default_value = 1.
    if tree.get('wet_surface_binding') == 'HGRPFaceRainSurface/1':
        tree.links.new(by_label(tree, 'Compiled SDF Face rain').outputs['Base Alpha'], instance.inputs['Base Texture Alpha'])
    rows = connect_weather_consumers(tree, instance, ports)
    tree['weather_surface_binding'] = 'HGRPWeatherSurface/1'
    tree['weather_projection_provenance'] = projection.provenance
    layout(tree)
    return {'material': tree.name, 'family': tree['source_shader'], 'source_disable_weather': instance.inputs['Disable Weather'].default_value,
            'projection_provenance': projection.provenance, 'channels': rows,
            'scope': 'Compiled Skin/Standard snow; shared weather reaches live HGRP consumers',
            'preceding_wet_binding': tree.get('wet_surface_binding', 'none')}


def bind_standard_wet_surface(tree: bpy.types.ShaderNodeTree, wet_kernel: bpy.types.ShaderNodeTree,
                              weather_state: bpy.types.ShaderNodeTree, projection: WeatherProjection,
                              layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Apply compiled Standard rain and wet film before snow."""
    if tree.get('source_shader') != 'HGRP/CharacterNPR' or tree.get('wet_surface_binding'):
        raise ValueError(f'Standard wet binding requires an unpatched Standard material: {tree.name}')
    ports = read_weather_surface_ports(tree)
    instance, state = create_weather_instance(tree, wet_kernel, weather_state, projection, ports,
                                              'Compiled wet film before HGRP surface lighting')
    for label, output in (('Rain Intensity', 'Native Rain Intensity'), ('Water Submersion', 'Native Water Submersion'), ('Global Wetness', 'Native Global Wetness')):
        tree.links.new(state.outputs[output], instance.inputs[label])
    if 'Native Time X' in instance.inputs:
        tree.links.new(state.outputs['Native Time X'], instance.inputs['Native Time X'])
    rows = connect_weather_consumers(tree, instance, ports)
    tree['wet_surface_binding'] = 'HGRPStandardWetSurface/1'
    layout(tree)
    return {'material': tree.name, 'family': tree['source_shader'], 'channels': rows,
            'scope': 'Compiled Standard animated rain and wet film, before compiled snow and actual HGRP lighting'}


def expose_skin_half_vector(instance: bpy.types.Node) -> bpy.types.NodeSocket:
    """Reuse the actual main-light half vector rather than recomputing an approximation."""
    kernel = instance.node_tree
    if kernel is None:
        raise ValueError('Skin GGX instance has no node tree')
    half = by_label(kernel, 'Normalize skin half vector').outputs[0]
    outputs = tuple(item for item in kernel.nodes if item.type == 'GROUP_OUTPUT')
    if len(outputs) != 1:
        raise ValueError('Skin GGX output is ambiguous')
    if 'Half Vector' not in outputs[0].inputs:
        kernel.interface.new_socket(name='Half Vector', in_out='OUTPUT', socket_type='NodeSocketVector')
        kernel.links.new(half, outputs[0].inputs['Half Vector'])
    elif linked_source(outputs[0].inputs['Half Vector']) != half:
        raise ValueError('Existing Skin half-vector output has a different producer')
    return instance.outputs['Half Vector']


def attach_skin_rain_specular(tree: bpy.types.ShaderNodeTree, rain: bpy.types.Node) -> tuple[str, ...]:
    """Apply compiled Skin rain specular composition before post-main chroma."""
    section = 'Skin Rain Specular'
    ggx = by_label(tree, 'Compiled Skin main-light GGX')
    half = coordinates(tree, expose_skin_half_vector(ggx), (0,2,1), (1.,1.,-1.), 'Skin half vector in native rain frame')
    wet_normal = rain.outputs['Wet Normal']
    split = node(tree, 'ShaderNodeSeparateXYZ', 'Native Skin rain normal components', section)
    tree.links.new(wet_normal, split.inputs[0])
    axis = combine_vector(tree, (math_node(tree,'MULTIPLY',split.outputs['Z'],-1.,'Native rain tangent negative Z',section),
                                .0010000000474974513, split.outputs['X']), 'Native Skin rain tangent')
    tangent = vector_math(tree, 'NORMALIZE', axis, (0.,0.,0.), 'Normalize native rain tangent').outputs[0]
    bitangent = vector_math(tree, 'CROSS_PRODUCT', wet_normal, tangent, 'Native rain bitangent').outputs[0]
    ht = vector_math(tree, 'DOT_PRODUCT', half, tangent, 'Skin rain half tangent dot').outputs['Value']
    hb = vector_math(tree, 'DOT_PRODUCT', half, bitangent, 'Skin rain half bitangent dot').outputs['Value']
    hn = vector_math(tree, 'DOT_PRODUCT', half, wet_normal, 'Skin rain half normal dot').outputs['Value']
    first = smoothstep(tree, .15000000596046448, .10000000149011612,
                       math_node(tree, 'ABSOLUTE', ht, 0., 'Skin rain absolute tangent', section), 'Native Skin rain tangent lobe')
    second = smoothstep(tree, .07000000029802322, .019999999552965164,
                        math_node(tree, 'ABSOLUTE', hb, 0., 'Skin rain absolute bitangent', section), 'Native Skin rain bitangent lobe')
    lobe = math_node(tree, 'MULTIPLY', first, second, 'Native Skin rain anisotropy', section)
    lobe = math_node(tree, 'MULTIPLY', lobe, math_node(tree, 'MAXIMUM', hn, 0., 'Native rain positive NdotH', section), 'Native Skin rain directional lobe', section)
    lobe = math_node(tree, 'MULTIPLY', math_node(tree, 'MULTIPLY', lobe, 2., 'Native rain specular double', section),
                     rain.outputs['Wet Specular Coverage'], 'Native rain specular coverage', section)
    combined = by_label(tree, 'Skin diffuse and source GGX')
    diffuse = linked_source(combined.inputs['Color1'])
    main_specular = linked_source(combined.inputs['Color2'])
    targets = outgoing(combined.outputs[0])
    scaled_specular = color_mix(tree, 'MULTIPLY', 1., main_specular, rain.outputs['Wet Specular Scale'], 'Skin rain scales dry GGX', section)
    wet_add = color_mix(tree, 'MULTIPLY', 1., combined.outputs[0], rain.outputs['Wet Specular Add'], 'Skin rain scales diffuse plus GGX', section)
    directional = color_mix(tree, 'MULTIPLY', 1., by_label(tree, 'Skin full diffuse specular weight').outputs[0], lobe, 'Skin rain illuminated directional lobe', section)
    result = color_mix(tree, 'ADD', 1., diffuse, scaled_specular, 'Skin diffuse plus rain-scaled GGX', section)
    result = color_mix(tree, 'ADD', 1., result, wet_add, 'Skin with compiled rain additive response', section)
    result = color_mix(tree, 'ADD', 1., result, directional, 'Skin main including native rain specular', section)
    for target in targets:
        tree.links.new(result, target)
    return tuple(target.node.label + '/' + target.name for target in targets)


def bind_skin_wet_surface(tree: bpy.types.ShaderNodeTree, rain_kernel: bpy.types.ShaderNodeTree,
                          weather_state: bpy.types.ShaderNodeTree, projection: WeatherProjection,
                          layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Attach the separate Skin rain color and specular paths without roughness substitution."""
    if tree.get('source_shader') != 'HGRP/CharacterNPR_Skin' or tree.get('wet_surface_binding'):
        raise ValueError(f'Skin rain binding requires an unpatched Skin material: {tree.name}')
    if '_SDFLIGHTMAP' in source_keywords(tree):
        raise ValueError(f'Face requires its dedicated compiled rain branch: {tree.name}')
    ports = read_weather_surface_ports(tree)
    instance, state = create_weather_instance(tree, rain_kernel, weather_state, projection, ports,
                                              'Compiled Skin rain before HGRP lighting')
    for label, output in (('Rain Intensity', 'Native Rain Intensity'), ('Water Submersion', 'Native Water Submersion'), ('Global Wetness', 'Native Global Wetness')):
        tree.links.new(state.outputs[output], instance.inputs[label])
    tree.links.new(state.outputs['Native Time X'], instance.inputs['Native Time X'])
    view = linked_source(by_label(tree, 'Compiled Skin main-light GGX').inputs['View'])
    native_view = coordinates(tree, view, (0,2,1), (1.,1.,-1.), 'Skin view in native rain frame')
    tree.links.new(native_view, instance.inputs['View'])
    geometry = node(tree, 'ShaderNodeNewGeometry', 'Skin host gravity projection', 'HGRP Weather Surface')
    native_normal = coordinates(tree, geometry.outputs['Normal'], (0,2,1), (1.,1.,-1.), 'Host geometric normal for gravity')
    components = node(tree, 'ShaderNodeSeparateXYZ', 'Host gravity normal components', 'HGRP Weather Surface')
    tree.links.new(native_normal, components.inputs[0])
    scale = combine_vector(tree, (components.outputs['Y'],)*3, 'Host gravity normal coefficient')
    projected = vector_math(tree, 'MULTIPLY', native_normal, scale, 'Host gravity normal component').outputs[0]
    projected = vector_math(tree, 'ADD', projected, (0.,-1.,0.), 'Host tangent-projected gravity').outputs[0]
    tree.links.new(projected, instance.inputs['Projection Gravity'])
    rows = connect_weather_consumers(tree, instance, ports)
    specular_targets = attach_skin_rain_specular(tree, instance)
    tree['wet_surface_binding'] = 'HGRPSkinRainSurface/1'
    layout(tree)
    return {'material':tree.name, 'family':tree['source_shader'], 'channels':rows, 'rain_specular_consumers':specular_targets,
            'scope':'Compiled Skin rain surface and main specular; source roughness preserved; projection remains explicit host binding'}


def attach_weather_clock(state: bpy.types.ShaderNodeTree, scene: bpy.types.Scene,
                          native_time_per_second: float, provenance: str) -> None:
    """Drive the source time channel using an explicit, editable time conversion."""
    if not math.isfinite(native_time_per_second) or native_time_per_second <= 0. or not provenance.strip():
        raise ValueError('Weather clock needs a positive finite conversion and its provenance')
    outputs = tuple(item for item in state.nodes if item.type == 'GROUP_OUTPUT')
    if len(outputs) != 1 or 'Native Time X' in outputs[0].inputs:
        raise ValueError('Weather clock requires an unpatched state with one output')
    section = 'Explicit Weather Clock'
    scale = node(state, 'ShaderNodeValue', 'Native time units per second', section)
    scale.outputs[0].default_value = native_time_per_second
    clock = node(state, 'ShaderNodeValue', 'Blender timeline seconds', section)
    driver = clock.outputs[0].driver_add('default_value').driver
    driver.type = 'SCRIPTED'
    driver.expression = '(frame - 1.0) * fps_base / fps'
    for name, identifier, id_type, path in (
        ('fps', scene, 'SCENE', 'render.fps'), ('fps_base', scene, 'SCENE', 'render.fps_base'),
    ):
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'SINGLE_PROP'
        variable.targets[0].id_type = id_type
        variable.targets[0].id = identifier
        variable.targets[0].data_path = path
    state.interface.new_socket(name='Native Time X', in_out='OUTPUT', socket_type='NodeSocketFloat')
    native_time = math_node(state, 'MULTIPLY', clock.outputs[0], scale.outputs[0], 'Native time from Blender timeline', section)
    state.links.new(native_time, outputs[0].inputs['Native Time X'])
    state['native_time_provenance'] = provenance
