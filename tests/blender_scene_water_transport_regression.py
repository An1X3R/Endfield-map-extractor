"""Exercise the prepared-scene bridge on a real cached water sample and source Sun."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from blender_scene_lights import build_scene_lights
from blender_scene_water_transport import apply_scene_water_transport
from blender_water_transport import (bind_water_transport,
                                    connect_water_transport_phase_atmosphere)
from endfield_water_transport import (WaterTransportLighting, read_water_transport_lighting,
                                      WaterTransport, evaluate_water_scene_color_weight,
                                      evaluate_water_transport,
                                      read_water_transport)


def _require_link(destination: bpy.types.Node, input_index: int,
                  source: bpy.types.Node, source_socket_name: str) -> None:
    links = destination.inputs[input_index].links
    if (len(links) != 1 or links[0].from_node != source
            or links[0].from_socket.name != source_socket_name):
        raise AssertionError(
            f'Water source graph mismatch: destination={destination.name}, input={input_index}, '
            f'expected_source={source.name}, expected_socket={source_socket_name}')


def verify_candidate_graph(material: bpy.types.Material,
                           transport: bpy.types.Node,
                           phase_selected: bool) -> dict[str, object]:
    tree = transport.node_tree
    if tree is None:
        raise AssertionError('Water transport group node has no node tree')
    entry_nodes = tuple(node for node in tree.nodes if node.type == 'GROUP_INPUT')
    output_nodes = tuple(node for node in tree.nodes
                         if node.type == 'GROUP_OUTPUT' and node.is_active_output)
    if len(entry_nodes) != 1 or len(output_nodes) != 1:
        raise AssertionError('Water transport graph must have one input and one active output node')
    entry, output = entry_nodes[0], output_nodes[0]
    legacy_scatter = tree.nodes.get('Source scattering coefficient')
    legacy_sum = tree.nodes.get('Ambient and direct scattering')
    legacy_direct = tree.nodes.get('Direct integral times host radiance')
    legacy_phase = tree.nodes.get('Directional scattering phase')
    phase = tree.nodes.get('Source phase mixture')
    ambient = tree.nodes.get('Source environment scale')
    phase_direct = tree.nodes.get('Phase candidate atmosphere RGB integral')
    phase_scatter = tree.nodes.get('Phase candidate scattering coefficient')
    phase_sum = tree.nodes.get('Phase candidate ambient and direct')
    phase_scale = tree.nodes.get('Phase candidate angular phase')
    nodes = (legacy_scatter, legacy_sum, legacy_direct, legacy_phase, phase,
             ambient, phase_direct, phase_scatter, phase_sum, phase_scale)
    if any(node is None for node in nodes):
        raise AssertionError('Water transport graph is missing a source or candidate node')
    _require_link(legacy_scatter, 0, entry, 'Scattering')
    _require_link(legacy_scatter, 1, legacy_sum, legacy_sum.outputs[0].name)
    _require_link(legacy_sum, 0, ambient, ambient.outputs[0].name)
    _require_link(legacy_sum, 1, legacy_phase, legacy_phase.outputs[0].name)
    _require_link(legacy_direct, 1, entry, 'HOST Direct Radiance')
    _require_link(legacy_phase, 0, legacy_direct, legacy_direct.outputs[0].name)
    _require_link(legacy_phase, 3, phase, phase.outputs[0].name)
    _require_link(phase_scatter, 0, entry, 'Scattering')
    _require_link(phase_scatter, 1, phase_sum, phase_sum.outputs[0].name)
    _require_link(phase_sum, 0, ambient, ambient.outputs[0].name)
    _require_link(phase_sum, 1, phase_scale, phase_scale.outputs[0].name)
    _require_link(phase_scale, 0, phase_direct, phase_direct.outputs[0].name)
    _require_link(phase_scale, 3, phase, phase.outputs[0].name)
    _require_link(phase_direct, 1, entry, 'Phase Atmosphere RGB')
    if (len(legacy_direct.inputs[0].links) != 1
            or len(phase_direct.inputs[0].links) != 1
            or legacy_direct.inputs[0].links[0].from_node
            != phase_direct.inputs[0].links[0].from_node):
        raise AssertionError('Legacy and candidate branches must share the direct integral')
    if (len(output.inputs['In-scattering'].links) != 1
            or output.inputs['In-scattering'].links[0].from_node != legacy_scatter
            or len(output.inputs['Atmosphere Phase Candidate'].links) != 1
            or output.inputs['Atmosphere Phase Candidate'].links[0].from_node != phase_scatter):
        raise AssertionError('Water scatter outputs do not preserve separate source branches')
    for socket_name in ('Refracted Background Weight', 'Phase Refracted Background Weight'):
        links = output.inputs[socket_name].links
        if len(links) != 1 or links[0].from_node.bl_idname != 'ShaderNodeVectorMath':
            raise AssertionError(f'Water group background-weight output is not connected: socket={socket_name}')
    refraction_nodes = tuple(node for node in material.node_tree.nodes
                             if node.type == 'BSDF_REFRACTION'
                             and node.label == 'Single boundary: source transmission applied once')
    expected_background_output = ('Phase Refracted Background Weight' if phase_selected
                                  else 'Refracted Background Weight')
    if (len(refraction_nodes) != 1 or len(refraction_nodes[0].inputs['Color'].links) != 1
            or refraction_nodes[0].inputs['Color'].links[0].from_node != transport
            or refraction_nodes[0].inputs['Color'].links[0].from_socket.name != expected_background_output):
        raise AssertionError(
            f'Water refraction background-weight branch mismatch: expected={expected_background_output}')
    candidate_names = (
        'Phase candidate atmosphere RGB integral',
        'Phase candidate angular phase',
        'Phase candidate ambient and direct',
        'Phase candidate scattering coefficient',
    )
    candidate_nodes = tuple(tree.nodes.get(name) for name in candidate_names)
    if (any(node is None for node in candidate_nodes)
            or any(node.get('endfield_layout_section') != '02 Source Transport Math'
                   for node in candidate_nodes)):
        raise AssertionError('Phase candidate nodes are not in the source-transport layout section')
    frames = tuple(node.parent for node in candidate_nodes)
    if (any(frame is None for frame in frames) or len({frame.as_pointer() for frame in frames}) != 1
            or frames[0].get('endfield_layout_key') != '02 Source Transport Math'):
        raise AssertionError('Phase candidate nodes are not grouped in one readable layout frame')
    return {
        'legacy_direct_input': 'HOST Direct Radiance',
        'candidate_direct_input': 'Phase Atmosphere RGB',
        'shared_direct_integral': True,
        'shared_phase_function': True,
        'shared_ambient_radiance_path': True,
        'shared_scattering_coefficient': True,
        'both_background_weight_outputs_connected': True,
        'selected_refraction_background_weight': expected_background_output,
        'candidate_layout_section': '02 Source Transport Math',
    }


def verify_node_numeric_values(output_dir: Path, phase_rgb: tuple[float, float, float]) -> dict[str, object]:
    settings = WaterTransport(
        absorption_near=(0.10, 0.15, 0.20), absorption_far=(0.12, 0.17, 0.23),
        scattering=(0.20, 0.30, 0.10), safe_distance=5.0,
        phase_mix=0.3, environment_intensity=1.0,
        scene_color_environment_influence=0.5)
    depth, view_y, light_y, phase_cosine = 2.5, 0.4, 0.8, 0.25
    ambient = (0.18, 0.22, 0.28)
    host_direct = (0.9, 0.25, 0.1)
    expected_transmission, expected_host_scatter = evaluate_water_transport(
        settings, depth, view_y, light_y, phase_cosine, ambient, host_direct)
    expected_host_background_weight = tuple(
        transmission + weight for transmission, weight in zip(
            expected_transmission,
            evaluate_water_scene_color_weight(settings, depth, view_y, light_y, phase_cosine, host_direct),
            strict=True))
    phase_transmission, expected_phase_scatter = evaluate_water_transport(
        settings, depth, view_y, light_y, phase_cosine, ambient, phase_rgb)
    expected_phase_background_weight = tuple(
        transmission + weight for transmission, weight in zip(
            phase_transmission,
            evaluate_water_scene_color_weight(settings, depth, view_y, light_y, phase_cosine, phase_rgb),
            strict=True))
    if expected_transmission != phase_transmission:
        raise AssertionError('Pure transport evaluator changed transmission with phase RGB')

    probe_scene = bpy.data.scenes.new('Stage67 Water Node Numeric Probe')
    probe_scene.render.engine = 'BLENDER_EEVEE_NEXT'
    probe_scene.render.resolution_x = probe_scene.render.resolution_y = 1
    probe_scene.render.resolution_percentage = 100
    probe_scene.render.film_transparent = False
    probe_scene.render.image_settings.file_format = 'OPEN_EXR'
    probe_scene.render.image_settings.color_mode = 'RGB'
    probe_scene.render.image_settings.color_depth = '32'
    probe_scene.view_settings.view_transform = 'Standard'
    probe_scene.view_settings.look = 'None'
    probe_collection = bpy.data.collections.new('Stage67 Water Numeric Probe')
    probe_scene.collection.children.link(probe_collection)
    camera_data = bpy.data.cameras.new('Stage67 Water Probe Camera')
    camera_data.type = 'ORTHO'
    camera_data.ortho_scale = 2.0
    camera = bpy.data.objects.new('Stage67 Water Probe Camera', camera_data)
    camera.location = (0.0, 0.0, 5.0)
    probe_collection.objects.link(camera)
    probe_scene.camera = camera
    mesh = bpy.data.meshes.new('Stage67 Water Probe Surface')
    mesh.from_pydata(((-10.0, -10.0, 0.0), (10.0, -10.0, 0.0),
                      (10.0, 10.0, 0.0), (-10.0, 10.0, 0.0)), (), ((0, 1, 2, 3),))
    surface = bpy.data.objects.new('Stage67 Water Probe Surface', mesh)
    probe_collection.objects.link(surface)
    material = bpy.data.materials.new('Stage67 Water Node Numeric Probe')
    material.use_nodes = True
    mesh.materials.append(material)
    tree = material.node_tree
    for node in tuple(tree.nodes):
        tree.nodes.remove(node)
    emission = tree.nodes.new('ShaderNodeEmission')
    material_output = tree.nodes.new('ShaderNodeOutputMaterial')
    tree.links.new(emission.outputs[0], material_output.inputs['Surface'])
    transport = bind_water_transport(tree, settings)
    for label, value in (('Depth', depth), ('View Y', view_y), ('Light Y', light_y),
                         ('Phase Cosine', phase_cosine)):
        transport.inputs[label].default_value = value
    transport.inputs['HOST Ambient Radiance'].default_value = (*ambient, 1.0)
    transport.inputs['HOST Direct Radiance'].default_value = (*host_direct, 1.0)
    transport.inputs['Phase Atmosphere RGB'].default_value = (*phase_rgb, 1.0)

    numeric_renders: list[str] = []

    def rendered_output(socket_name: str) -> tuple[float, float, float]:
        for link in tuple(emission.inputs['Color'].links):
            tree.links.remove(link)
        tree.links.new(transport.outputs[socket_name], emission.inputs['Color'])
        path = output_dir / ('node_numeric_' + socket_name.lower().replace(' ', '_') + '.exr')
        probe_scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True, scene=probe_scene.name)
        result = bpy.data.images.load(str(path), check_existing=False)
        pixels = tuple(result.pixels)
        if len(pixels) < 3:
            raise AssertionError(
                f'Blender Render Result has no RGB pixel: socket={socket_name}, size={tuple(result.size)}')
        numeric_renders.append(str(path))
        bpy.data.images.remove(result)
        return pixels[0], pixels[1], pixels[2]

    actual_transmission = rendered_output('Transmission')
    actual_host_scatter = rendered_output('In-scattering')
    actual_phase_scatter = rendered_output('Atmosphere Phase Candidate')
    actual_host_background_weight = rendered_output('Refracted Background Weight')
    actual_phase_background_weight = rendered_output('Phase Refracted Background Weight')
    errors = {
        'transmission': max(abs(a - b) for a, b in zip(
            actual_transmission, expected_transmission, strict=True)),
        'host_in_scattering': max(abs(a - b) for a, b in zip(
            actual_host_scatter, expected_host_scatter, strict=True)),
        'phase_candidate_in_scattering': max(abs(a - b) for a, b in zip(
            actual_phase_scatter, expected_phase_scatter, strict=True)),
        'host_refracted_background_weight': max(abs(a - b) for a, b in zip(
            actual_host_background_weight, expected_host_background_weight, strict=True)),
        'phase_refracted_background_weight': max(abs(a - b) for a, b in zip(
            actual_phase_background_weight, expected_phase_background_weight, strict=True)),
    }
    if any(error > 0.001 for error in errors.values()):
        raise AssertionError(f'Blender node values diverged from pure transport math: errors={errors}')
    return {
        'engine': 'BLENDER_EEVEE_NEXT',
        'fixture_values': {'ambient_radiance': ambient, 'host_direct_radiance': host_direct,
                           'phase_atmosphere_rgb': phase_rgb,
                           'scene_color_environment_influence': settings.scene_color_environment_influence},
        'maximum_absolute_error': errors,
        'transmission_same_for_both_direct_inputs': expected_transmission == phase_transmission,
        'node_values_match_pure_transport': True,
        'numeric_probe_renders': numeric_renders,
    }


def verify(scene: bpy.types.Scene, material: bpy.types.Material, light: bpy.types.Object,
           settings: WaterTransportLighting, expected: WaterTransport) -> dict[str, object]:
    scene.frame_set(scene.frame_current+1)
    bpy.context.view_layer.update()
    transport = next(node for node in material.node_tree.nodes if node.type == 'GROUP'
                     and node.node_tree.get('endfield_contract') == 'SourceWaterTransport/1')
    phase = transport.inputs['Phase Cosine'].links[0].from_node
    direction = phase.inputs[1].links[0].from_node
    actual = tuple(socket.default_value for socket in direction.inputs)
    expected_direction = tuple(light.matrix_world.to_quaternion() @ Vector((0, 0, -1)))
    delta = max(abs(a-b) for a,b in zip(actual, expected_direction, strict=True))
    radiance = tuple(transport.inputs['HOST Direct Radiance'].default_value[:3])
    expected_radiance = tuple(value*light.data.energy*settings.radiance_scale*(not light.hide_render)
                              for value in light.data.color)
    error = max(abs(a-b) for a,b in zip(radiance, expected_radiance, strict=True))
    if delta > 1e-5 or error > 1e-4:
        raise AssertionError(f'Scene water live light mismatch: direction_error={delta}, radiance_error={error}')
    emission = next(node for node in material.node_tree.nodes
                    if node.type == 'EMISSION' and node.label == 'Source in-scattering radiance')
    expected_output = ('Atmosphere Phase Candidate' if settings.phase_atmosphere_rgb is not None
                       else 'In-scattering')
    if (len(emission.inputs['Color'].links) != 1
            or emission.inputs['Color'].links[0].from_socket != transport.outputs[expected_output]):
        raise AssertionError(f'Wrong water scatter source selected: expected={expected_output}')
    expected_background_output = ('Phase Refracted Background Weight'
                                  if settings.phase_atmosphere_rgb is not None
                                  else 'Refracted Background Weight')
    refraction = next(node for node in material.node_tree.nodes
                      if node.type == 'BSDF_REFRACTION'
                      and node.label == 'Single boundary: source transmission applied once')
    if (len(refraction.inputs['Color'].links) != 1
            or refraction.inputs['Color'].links[0].from_socket
            != transport.outputs[expected_background_output]):
        raise AssertionError(
            f'Wrong water refraction background-weight source selected: expected={expected_background_output}')
    actual_influence = transport.inputs['Scene Color Env Influence'].default_value
    if abs(actual_influence - expected.scene_color_environment_influence) > 1e-7:
        raise AssertionError(
            f'Scene color influence changed: actual={actual_influence}, '
            f'expected={expected.scene_color_environment_influence}')
    if settings.phase_atmosphere_rgb is not None:
        actual_phase_rgb = tuple(transport.inputs['Phase Atmosphere RGB'].default_value[:3])
        phase_error = max(abs(a-b) for a, b in zip(actual_phase_rgb, settings.phase_atmosphere_rgb, strict=True))
        if phase_error > 1e-6:
            raise AssertionError(f'Explicit atmosphere phase RGB changed: error={phase_error}')
    else:
        phase_error = None
    graph_proof = verify_candidate_graph(material, transport,
                                         settings.phase_atmosphere_rgb is not None)
    for obj in scene.objects:
        if obj.get('endfield_water_volume_source') and not obj.hide_render:
            raise AssertionError(f'Prior medium still renders and would apply extinction twice: object={obj.name}')
    if any(node.type=='GROUP' and node.node_tree.get('endfield_contract', '').startswith('PortableWaterOptics/')
           for node in material.node_tree.nodes):
        raise AssertionError('Legacy optics core remains in the active source material')
    return {'maximum_direction_error': delta, 'maximum_host_sun_radiance_error': error,
            'maximum_explicit_phase_rgb_error': phase_error,
            'scene_color_environment_influence': actual_influence,
            'selected_refraction_background_weight': expected_background_output,
            'candidate_graph_proof': graph_proof}


def verify_phase_branch_switch(material: bpy.types.Material, transport: bpy.types.Node,
                               phase_rgb: tuple[float, float, float]) -> dict[str, bool]:
    tree = material.node_tree
    refraction = next(node for node in tree.nodes
                      if node.type == 'BSDF_REFRACTION'
                      and node.label == 'Single boundary: source transmission applied once')
    emission = next(node for node in tree.nodes
                    if node.type == 'EMISSION' and node.label == 'Source in-scattering radiance')
    for destination, output_name in ((refraction.inputs['Color'], 'Refracted Background Weight'),
                                     (emission.inputs['Color'], 'In-scattering')):
        for link in tuple(destination.links):
            tree.links.remove(link)
        tree.links.new(transport.outputs[output_name], destination)
    if (refraction.inputs['Color'].links[0].from_socket.name != 'Refracted Background Weight'
            or emission.inputs['Color'].links[0].from_socket.name != 'In-scattering'):
        raise AssertionError('Water host branch did not select paired legacy outputs')
    connect_water_transport_phase_atmosphere(transport, phase_rgb)
    if (refraction.inputs['Color'].links[0].from_socket.name != 'Phase Refracted Background Weight'
            or emission.inputs['Color'].links[0].from_socket.name != 'Atmosphere Phase Candidate'):
        raise AssertionError('Water phase selection did not switch both refraction and scattering outputs')
    return {'host_background_and_scatter_paired': True,
            'phase_background_and_scatter_paired': True}


def main(source: Path, plan_path: Path, light_map: str, light_entity: int, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    plan_bytes = plan_path.read_bytes()
    plan_hash = hashlib.sha256(plan_bytes).hexdigest()
    plan = json.loads(plan_bytes.decode('utf-8-sig'))
    light_rows = [row for row in plan['light_instances'] if row['map_id']==light_map and row['entity_id']==light_entity]
    if len(light_rows)!=1:
        raise ValueError('Explicit source fixture Sun must match exactly one plan row')
    bpy.ops.wm.open_mainfile(filepath=str(source), use_scripts=False)
    bpy.context.preferences.filepaths.save_version=0
    scene = bpy.context.scene
    # Remove only the fixture's preview lights in memory before importing the exact source row.
    for obj in tuple(scene.objects):
        if obj.type=='LIGHT':
            bpy.data.objects.remove(obj, do_unlink=True)
    collection = bpy.data.collections.new('Source light regression')
    scene.collection.children.link(collection)
    build_scene_lights(light_rows, collection, 1., 1., 4096)
    light = tuple(collection.objects)[0]
    lighting_row = plan.get('water_transport_lighting')
    if not isinstance(lighting_row, dict):
        raise ValueError('Stage plan must include explicit water_transport_lighting')
    if lighting_row.get('map_id') != light_map or lighting_row.get('entity_id') != light_entity:
        raise ValueError('Plan water_transport_lighting must select the same exact source Sun')
    settings = read_water_transport_lighting(lighting_row)
    before = {material.name:len(material.node_tree.nodes) for material in bpy.data.materials if material.node_tree}
    wrong = read_water_transport_lighting({**lighting_row, 'entity_id': -1})
    try:
        apply_scene_water_transport(scene,plan['water_materials'],wrong)
    except ValueError as error:
        if 'exactly one exported directional light' not in str(error):
            raise
    else:
        raise AssertionError('Unknown Sun silently selected another light')
    if before != {material.name:len(material.node_tree.nodes) for material in bpy.data.materials if material.node_tree}:
        raise AssertionError('Rejected Sun selection mutated materials')
    audit = apply_scene_water_transport(scene,plan['water_materials'],settings)
    water = next(obj for obj in scene.objects if obj.get('endfield_category')=='water')
    material = water.data.materials[0]
    expected = read_water_transport(json.loads(next(row for row in plan['water_materials']
        if row['entity_id']==int(water['entity_id']))['config_source_json']))
    node = next(node for node in material.node_tree.nodes if node.type=='GROUP'
                and node.node_tree.get('endfield_contract')=='SourceWaterTransport/1')
    for label, values in (('Absorption Near',expected.absorption_near),('Absorption Far',expected.absorption_far),
                          ('Scattering',expected.scattering)):
        if max(abs(a-b) for a,b in zip(node.inputs[label].default_value[:3],values,strict=True))>1e-6:
            raise AssertionError(f'Serialized water coefficient was not wired: input={label}')
    if abs(node.inputs['Scene Color Env Influence'].default_value
           - expected.scene_color_environment_influence) > 1e-7:
        raise AssertionError('Serialized SceneColorEnvInfluence was not wired to the water node')
    checks = [verify(scene,material,light,settings,expected)]
    original_rotation = light.rotation_euler.copy()
    original_color = tuple(light.data.color)
    light.rotation_euler.z += .3
    light.data.color = (.3,.6,.9)
    checks.append(verify(scene,material,light,settings,expected))
    light.rotation_euler = original_rotation
    light.data.color = original_color
    checks.append(verify(scene,material,light,settings,expected))
    material_name, light_name = material.name, light.name
    artifact = output/'scene_water_transport.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(artifact))
    bpy.ops.wm.open_mainfile(filepath=str(artifact),use_scripts=False)
    scene = bpy.context.scene
    material = bpy.data.materials[material_name]
    light = bpy.data.objects[light_name]
    checks.append(verify(scene,material,light,settings,expected))
    scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 240,160,100
    scene.cycles.samples=8
    renders=[]
    render_deltas=[]
    transport = next(node for node in material.node_tree.nodes if node.type == 'GROUP'
                     and node.node_tree.get('endfield_contract') == 'SourceWaterTransport/1')
    emission = next(node for node in material.node_tree.nodes
                    if node.type == 'EMISSION' and node.label == 'Source in-scattering radiance')
    phase_render_rgb = settings.phase_atmosphere_rgb
    if phase_render_rgb is None:
        phase_render_rgb = (0.04, 0.8, 0.15)
    phase_switch = verify_phase_branch_switch(material, transport, phase_render_rgb)
    checks.append({'phase_branch_switch': phase_switch})
    for engine in ('BLENDER_EEVEE_NEXT','CYCLES'):
        scene.render.engine=engine
        branches = (('host_sun_legacy', 'In-scattering'),
                    ('explicit_phase_candidate', 'Atmosphere Phase Candidate'))
        branch_pixels=[]
        for branch_name, output_name in branches:
            if output_name == 'In-scattering':
                for destination, background_name in (
                        (emission.inputs['Color'], None),
                        (next(node for node in material.node_tree.nodes if node.type == 'BSDF_REFRACTION'
                              and node.label == 'Single boundary: source transmission applied once').inputs['Color'],
                         'Refracted Background Weight')):
                    for link in tuple(destination.links):
                        material.node_tree.links.remove(link)
                    source_name = output_name if background_name is None else background_name
                    material.node_tree.links.new(transport.outputs[source_name], destination)
            else:
                connect_water_transport_phase_atmosphere(transport, phase_render_rgb)
                if (next(node for node in material.node_tree.nodes if node.type == 'BSDF_REFRACTION'
                         and node.label == 'Single boundary: source transmission applied once')
                        .inputs['Color'].links[0].from_socket.name != 'Phase Refracted Background Weight'
                        or emission.inputs['Color'].links[0].from_socket.name != 'Atmosphere Phase Candidate'):
                    raise AssertionError('Rendered phase branch does not pair phase-scattering/background outputs')
            path=output/(engine+'_'+branch_name+'.png')
            scene.render.filepath=str(path)
            bpy.ops.render.render(write_still=True)
            image=bpy.data.images.load(str(path),check_existing=False)
            pixels=tuple(image.pixels)
            branch_pixels.append(pixels)
            bpy.data.images.remove(image)
            renders.append({'engine':engine,'branch':branch_name,'path':str(path)})
        maximum_delta=max(abs(a-b) for a,b in zip(branch_pixels[0],branch_pixels[1],strict=True))
        mean_delta=sum(abs(a-b) for a,b in zip(branch_pixels[0],branch_pixels[1],strict=True))/len(branch_pixels[0])
        if maximum_delta <= 1e-5 or mean_delta <= 1e-7:
            raise AssertionError(f'Phase RGB candidate did not change rendered pixels: engine={engine}, max={maximum_delta}, mean={mean_delta}')
        render_deltas.append({'engine':engine,'maximum_channel_delta':maximum_delta,'mean_channel_delta':mean_delta})
    numeric_phase_rgb = settings.phase_atmosphere_rgb
    if numeric_phase_rgb is None:
        numeric_phase_rgb = (0.04, 0.8, 0.15)
    node_numeric = verify_node_numeric_values(output, numeric_phase_rgb)
    for link in tuple(emission.inputs['Color'].links):
        material.node_tree.links.remove(link)
    material.node_tree.links.new(transport.outputs['Atmosphere Phase Candidate' if settings.phase_atmosphere_rgb is not None
                                                      else 'In-scattering'], emission.inputs['Color'])
    if hashlib.sha256(source.read_bytes()).hexdigest()!=source_hash:
        raise AssertionError('Source sample was modified')
    receipt={'status':'passed','audit':audit,'checks':checks,'source':str(source),'source_sha256':source_hash,
             'plan':str(plan_path),'plan_sha256':plan_hash,'blender_version':bpy.app.version_string,
             'artifact':str(artifact),'artifact_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),
             'renders':renders,'render_deltas':render_deltas,
             'phase_atmosphere_rgb':settings.phase_atmosphere_rgb,
             'phase_render_rgb':phase_render_rgb,
             'phase_rgb_origin':('supplied fixture in Stage67 plan; not a game-captured runtime value'
                                 if settings.phase_atmosphere_rgb is not None else 'not supplied'),
             'candidate_graph_proof':checks[0]['candidate_graph_proof'],
             'node_numeric_oracle':node_numeric,
             'unknown_sun_rejected_before_mutation':True,
             'source_coefficients_verified':True,'host_sun_and_phase_rgb_are_separate_inputs':True,
             'phase_branch_switch_paired_outputs_verified':True,
             'native_active_light_selection':False,'native_phase_rgb_parity':False}
    (output/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps({'status':'passed','checks':len(checks),'artifact':str(artifact)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('source','plan','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--light-map',required=True)
    parser.add_argument('--light-entity',type=int,required=True)
    args=parser.parse_args(sys.argv[sys.argv.index('--')+1:])
    main(args.source,args.plan,args.light_map,args.light_entity,args.output)
