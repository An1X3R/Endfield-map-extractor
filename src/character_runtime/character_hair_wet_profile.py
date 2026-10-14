"""Connect compiled Hair wet colors and separate lobe controls to live lighting."""
from __future__ import annotations

from collections.abc import Callable
import json
import math
from pathlib import Path

import bpy

from attach_character_weather_surface import linked_source, outgoing
from character_node_graph import color_mix, math_node, node
from character_weather_nodes import combine_vector, smoothstep, vector_math
from patch_character_hgrp import by_label
from patch_native_character_weather import coordinates


SECTION = 'Hair Wet Surface'
Scalar = bpy.types.NodeSocket | float
Vector = bpy.types.NodeSocket | tuple[float, float, float]


def ensure_hair_weather_clock(profile_path: Path, controls: bpy.types.ShaderNodeTree,
                               layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Create an artist clock only when the shared controls do not have one."""
    from character_skin_weather_profile import ensure_time_output, attach_skin_weather_timeline
    existing = tuple(item for item in controls.nodes if item.type == 'VALUE' and item.label == 'Preview Time X')
    if len(existing) > 1:
        raise ValueError('Hair shared preview clock offset is ambiguous: ' + controls.name)
    if existing:
        offset = float(existing[0].outputs[0].default_value)
        provenance = 'Existing shared artist clock offset retained without reset'
    else:
        document = json.loads(profile_path.read_text(encoding='utf-8'))
        skin = document.get('skin_rain')
        if skin is None:
            offset = 0.0
            provenance = 'Explicit new artist clock offset zero; no Skin clock configuration or native time capture'
        elif not isinstance(skin, dict) or type(skin.get('time_x')) not in (int, float):
            raise ValueError('Hair clock requires a finite configured Skin time_x when Skin configuration is present')
        else:
            offset = float(skin['time_x'])
            provenance = 'Shared profile Skin time_x used as explicit artist clock offset; no Skin consumer required'
    if not math.isfinite(offset):
        raise ValueError('Hair shared artist clock offset must be finite')
    ensure_time_output(controls, offset, layout)
    attach_skin_weather_timeline(controls, layout)
    return {'offset': offset, 'provenance': provenance, 'timeline': controls['endfield_skin_rain_timeline'],
        'native_time_per_second': 0.05, 'native_runtime_time_verified': False}


def _math(tree: bpy.types.NodeTree, operation: str, a: Scalar, b: Scalar, label: str) -> bpy.types.NodeSocket:
    return math_node(tree, operation, a, b, label, SECTION)


def _vector(tree: bpy.types.NodeTree, operation: str, a: Vector, b: Vector, label: str) -> bpy.types.NodeSocket:
    result = vector_math(tree, operation, a, b, label)
    result['endfield_layout_section'] = SECTION
    return result.outputs['Value' if operation in ('DOT_PRODUCT', 'DISTANCE', 'LENGTH') else 'Vector']


def _scale(tree: bpy.types.NodeTree, value: bpy.types.NodeSocket, factor: Scalar, label: str) -> bpy.types.NodeSocket:
    return _vector(tree, 'MULTIPLY', value, combine_vector(tree, (factor, factor, factor), label + ' factor'), label)


def _socket(socket: bpy.types.NodeSocket) -> dict[str, object]:
    sockets = socket.node.outputs if socket.is_output else socket.node.inputs
    return {'node': socket.node.name, 'index': list(sockets).index(socket)}


def _camera_projection(tree: bpy.types.ShaderNodeTree, camera: bpy.types.Object) -> bpy.types.NodeSocket:
    """Use the current host lens/fit; camera type and fit remain explicit bindings."""
    data = camera.data
    if data.type not in ('ORTHO', 'PERSP') or data.sensor_fit not in ('AUTO', 'HORIZONTAL', 'VERTICAL'):
        raise ValueError('Hair wet requires a host orthographic or perspective render camera')
    result = node(tree, 'ShaderNodeValue', 'Hair host camera projection Y', SECTION)
    driver = result.outputs[0].driver_add('default_value').driver
    driver.type = 'SCRIPTED'
    ratio = '(rx*px/(ry*py))'
    fit = 'max(' + ratio + ',1)' if data.sensor_fit == 'AUTO' else ratio if data.sensor_fit == 'HORIZONTAL' else '1'
    driver.expression = '2*' + fit + '/scale' if data.type == 'ORTHO' else '2*lens*' + fit + '/sensor'
    variables = [('rx', bpy.context.scene, 'SCENE', 'render.resolution_x'),
        ('ry', bpy.context.scene, 'SCENE', 'render.resolution_y'),
        ('px', bpy.context.scene, 'SCENE', 'render.pixel_aspect_x'),
        ('py', bpy.context.scene, 'SCENE', 'render.pixel_aspect_y')]
    variables += [('scale', bpy.context.scene, 'SCENE', 'camera.data.ortho_scale')] if data.type == 'ORTHO' else [
        ('lens', bpy.context.scene, 'SCENE', 'camera.data.lens'), ('sensor', bpy.context.scene, 'SCENE',
            'camera.data.sensor_height' if data.sensor_fit == 'VERTICAL' else 'camera.data.sensor_width')]
    for name, identifier, kind, path in variables:
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'SINGLE_PROP'
        variable.targets[0].id_type = kind
        variable.targets[0].id = identifier
        variable.targets[0].data_path = path
    return result.outputs[0]


def _camera_vector(tree: bpy.types.NodeTree, kind: str, value: tuple[float, float, float], label: str) -> bpy.types.NodeSocket:
    result = node(tree, 'ShaderNodeVectorTransform', label, SECTION)
    result.vector_type = kind
    result.convert_from = 'CAMERA'
    result.convert_to = 'WORLD'
    result.inputs[0].default_value = value
    return result.outputs[0]


def _main_lighting(tree: bpy.types.ShaderNodeTree, wet: bpy.types.Node,
                   camera_direction: bpy.types.NodeSocket, alpha_factor: Scalar) -> dict[str, object]:
    """Preserve original strand math and add only the source post-main wet terms."""
    original = by_label(tree, 'HGRP hair dual-strand specular added to NPR light')
    targets = outgoing(original.outputs[0])
    if not targets:
        raise ValueError('Hair wet cannot find the live main lighting consumer: ' + tree.name)
    diffuse = linked_source(original.inputs[1])
    specular = linked_source(original.inputs[2])
    view = coordinates(tree, by_label(tree, 'Normalize Geometry Incoming toward camera').outputs[0],
        (0, 2, 1), (1., 1., -1.), 'Hair wet view in source world')
    light_cast = coordinates(tree, by_label(tree, 'HGRP adjusted light direction times cast shadow').outputs[0],
        (0, 2, 1), (1., 1., -1.), 'Hair wet cast light in source world')
    camera_xyz = node(tree, 'ShaderNodeSeparateXYZ', 'Hair wet camera source axes', SECTION)
    tree.links.new(camera_direction, camera_xyz.inputs[0])
    camera_plane = combine_vector(tree, (camera_xyz.outputs['X'],
        by_label(tree, 'HGRP source light Y cast-shadow blend').outputs[0], camera_xyz.outputs['Z']),
        'Hair wet camera and cast plane')
    camera_plane = _vector(tree, 'NORMALIZE', camera_plane, (0., 0., 0.), 'Hair wet camera plane unit')
    cast = by_label(tree, 'Hair HGRP Cast Shadow Preview - Explicit').outputs[0]
    half = _vector(tree, 'ADD', light_cast, _scale(tree, camera_plane, 2., 'Hair wet camera plane times two'), 'Hair wet half light camera')
    half = _vector(tree, 'ADD', half, _scale(tree, view, _math(tree, 'ADD', 2., cast, 'Hair wet view factor'), 'Hair wet weighted view'), 'Hair wet half sum')
    half = _vector(tree, 'NORMALIZE', half, (0., 0., 0.), 'Hair wet half vector')
    normal = wet.outputs['Wet Normal']
    xyz = node(tree, 'ShaderNodeSeparateXYZ', 'Hair wet lobe normal components', SECTION)
    tree.links.new(normal, xyz.inputs[0])
    axis = combine_vector(tree, (_math(tree, 'MULTIPLY', xyz.outputs['Z'], -1., 'Hair wet axis minus Z'), .001, xyz.outputs['X']), 'Hair wet lobe axis')
    axis = _vector(tree, 'NORMALIZE', axis, (0., 0., 0.), 'Hair wet lobe axis unit')
    first = _math(tree, 'ABSOLUTE', _vector(tree, 'DOT_PRODUCT', half, axis, 'Hair wet first axis dot'), 0., 'Hair wet first axis absolute')
    cross = _vector(tree, 'CROSS_PRODUCT', normal, axis, 'Hair wet second lobe axis')
    second = _math(tree, 'ABSOLUTE', _vector(tree, 'DOT_PRODUCT', half, cross, 'Hair wet second axis dot'), 0., 'Hair wet second axis absolute')
    lobe = _math(tree, 'MULTIPLY', smoothstep(tree, .15, .10, first, 'Hair wet first axis window'),
        smoothstep(tree, .07, .02, second, 'Hair wet second axis window'), 'Hair wet lobe windows')
    lobe = _math(tree, 'MULTIPLY', lobe, _math(tree, 'MAXIMUM', 0., _vector(tree, 'DOT_PRODUCT', normal, half, 'Hair wet normal half dot'), 'Hair wet facing lobe'), 'Hair wet directed lobe')
    lobe = _math(tree, 'MULTIPLY', lobe, _math(tree, 'MULTIPLY', 2., wet.outputs['Wet Specular Coverage'], 'Hair wet coverage times two'), 'Hair wet additional lobe intensity')
    full = by_label(tree, 'Verified local HGRP diffuse equations').outputs['Full Diffuse']
    ambient = by_label(tree, 'HGRP hair source specular ambient intensity').outputs[0]
    light_weight = _scale(tree, full, ambient, 'Hair wet main light weight without CP13')
    gain_specular = _scale(tree, specular, wet.outputs['Wet Specular Gain'], 'Hair wet gain on existing strand sum')
    base = _vector(tree, 'ADD', _scale(tree, diffuse, alpha_factor, 'Hair wet diffuse alpha factor'),
        _scale(tree, gain_specular, wet.outputs['Wet Specular Scale'], 'Hair wet attenuated strand sum'), 'Hair wet base light')
    boost = _scale(tree, _vector(tree, 'ADD', diffuse, gain_specular, 'Hair wet unpremultiplied diffuse spec sum'), wet.outputs['Wet Specular Add'], 'Hair wet source lighting boost')
    response = _vector(tree, 'ADD', base, boost, 'Hair wet base and boost')
    response = _vector(tree, 'ADD', response, _scale(tree, light_weight, lobe, 'Hair wet extra source lobe'), 'Hair wet final main light')
    selected = color_mix(tree, 'MIX', wet.outputs['Enabled'], original.outputs[0], response,
        'Hair exact dry main lighting bypass', SECTION)
    for target in targets:
        tree.links.new(selected, target)
    return {'dry_source': _socket(original.outputs[0]), 'selector': selected.node.name,
        'targets': [_socket(target) for target in targets], 'source_equation': 'D*A+S*G*E+(D+S*G)*B+W*extraLobe'}


def attach_hair_wet_before_snow(tree: bpy.types.ShaderNodeTree, snow: bpy.types.Node,
                                state: bpy.types.Node, shader_sha256: str,
                                layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Reuse exact Hair variant gates, source UV and the shared editable weather state."""
    if snow.node_tree.get('snow_family') != 'Hair' or snow.node_tree.get('source_shader_sha256') != shader_sha256 or tree.get('hair_wet_binding'):
        raise ValueError('Hair wet requires a matching unbound compiled Hair snow variant')
    camera = bpy.context.scene.camera
    if camera is None:
        raise ValueError('Hair wet requires an explicit host render camera')
    old_nodes = frozenset(item.name for item in tree.nodes)
    from character_hair_wet import make_native_hair_wet
    wet = node(tree, 'ShaderNodeGroup', 'Compiled Hair wet before source snow', SECTION)
    wet.node_tree = make_native_hair_wet(shader_sha256, layout)
    floats = json.loads(tree['source_floats'])
    alpha_mode = float(floats['_AlphaPremultiply'])
    if not math.isfinite(alpha_mode):
        raise ValueError('Hair source AlphaPremultiply must be finite: ' + tree.name)
    for name in ('Albedo', 'Shadow Color', 'Projection Position', 'Projection Normal', 'Geometric Normal'):
        tree.links.new(linked_source(snow.inputs[name]), wet.inputs[name])
    for name in ('Orientation Flag', 'Global Tiling', 'Disable Weather'):
        wet.inputs[name].default_value = snow.inputs[name].default_value
    for name, output in (('Rain Intensity', 'Native Rain Intensity'), ('Water Submersion', 'Native Water Submersion'),
                          ('Global Wetness', 'Native Global Wetness'), ('Native Time X', 'Native Time X')):
        tree.links.new(state.outputs[output], wet.inputs[name])
    base = tuple(item for item in tree.nodes if item.type == 'TEX_IMAGE' and item.get('source_slot') == '_BaseMap')
    if len(base) != 1:
        raise ValueError('Hair wet needs the unique actual source BaseMap: ' + tree.name)
    tree.links.new(linked_source(base[0].inputs['Vector']), wet.inputs['UV'])
    geometry = node(tree, 'ShaderNodeNewGeometry', 'Hair wet host geometry', SECTION)
    native_normal = coordinates(tree, geometry.outputs['Normal'], (0, 2, 1), (1., 1., -1.), 'Hair host normal for explicit gravity')
    normal_xyz = node(tree, 'ShaderNodeSeparateXYZ', 'Hair host gravity normal components', SECTION)
    tree.links.new(native_normal, normal_xyz.inputs[0])
    gravity = _vector(tree, 'ADD', _scale(tree, native_normal, normal_xyz.outputs['Y'], 'Hair host normal gravity projection'), (0., -1., 0.), 'Hair explicit host projected gravity')
    tree.links.new(gravity, wet.inputs['Projection Gravity'])
    view = coordinates(tree, by_label(tree, 'Normalize Geometry Incoming toward camera').outputs[0], (0, 2, 1), (1., 1., -1.), 'Hair kernel native view')
    tree.links.new(view, wet.inputs['View'])
    if camera.data.type == 'ORTHO':
        # The compiled ortho view branch takes the length of a unit view axis.
        wet.inputs['View Distance'].default_value = 1.0
    else:
        camera_origin = _camera_vector(tree, 'POINT', (0., 0., 0.), 'Hair current host camera origin')
        distance = _vector(tree, 'DISTANCE', camera_origin, geometry.outputs['Position'], 'Hair current camera world distance')
        tree.links.new(distance, wet.inputs['View Distance'])
    projection = _camera_projection(tree, camera)
    tree.links.new(projection, wet.inputs['Camera Projection Y'])
    camera_direction = coordinates(tree, _camera_vector(tree, 'VECTOR', (0., 0., 1.), 'Hair current camera positive Z'), (0, 2, 1), (1., 1., -1.), 'Hair camera direction in source world')
    colors = json.loads(tree['source_colors'])
    alpha = _math(tree, 'MULTIPLY', base[0].outputs['Alpha'], float(colors['_BaseColor'][3]), 'Hair wet actual source alpha')
    alpha_factor = _math(tree, 'ADD', 1. - alpha_mode, _math(tree, 'MULTIPLY', alpha, alpha_mode, 'Hair wet alpha premultiply term'), 'Hair wet source diffuse alpha factor')
    lighting = _main_lighting(tree, wet, camera_direction, alpha_factor)
    additional: list[str] = []
    for item in tuple(tree.nodes):
        if item.type == 'GROUP' and item.label.startswith('Compiled additional Hair mode0 '):
            socket = item.inputs['Wet Specular Gain']
            if socket.is_linked:
                raise ValueError('Hair additional wet gain was already dynamically bound: ' + tree.name)
            gain = _math(tree, 'MULTIPLY', wet.outputs['Wet Specular Gain'], float(socket.default_value), 'Hair wet gain times explicit additional record gain')
            tree.links.new(gain, socket)
            additional.append(item.name)
    for name in ('Albedo', 'Shadow Color'):
        tree.links.new(wet.outputs[name], snow.inputs[name])
    nodes = tuple(item for item in tree.nodes if item.name not in old_nodes)
    links = [{'source': _socket(link.from_socket), 'target': _socket(link.to_socket)}
        for item in nodes for socket in item.inputs for link in socket.links]
    links.extend({'source': _socket(wet.outputs[name]), 'target': _socket(snow.inputs[name])} for name in ('Albedo', 'Shadow Color'))
    links.extend({'source': _socket(linked_source(tree.nodes[name].inputs['Wet Specular Gain'])),
                  'target': _socket(tree.nodes[name].inputs['Wet Specular Gain'])} for name in additional)
    tree['hair_wet_binding'] = 'HGRPHairWetBeforeSnow/1'
    layout(tree)
    return {'format': 'HGRPHairWetBeforeSnow/1', 'instance': wet.name, 'kernel': wet.node_tree.name,
        'snow': snow.name, 'state': state.name, 'shader_sha256': shader_sha256,
        'links': links, 'lighting': lighting, 'additional_light_consumers': additional,
        'preserved_snow_normal': _socket(linked_source(snow.inputs['Geometric Normal'])),
        'camera': camera.name, 'camera_type': camera.data.type, 'sensor_fit': camera.data.sensor_fit,
        'projection_node': projection.node.name,
        'view_distance_scope': 'Source orthographic unit-axis branch: distance 1' if camera.data.type == 'ORTHO' else 'Host perspective camera-to-surface distance',
        'scope': 'Mapped Hair core wet colors and lobe controls; explicit host camera/projection/gravity, not live native varyings. Camera type/sensor-fit is bound at construction; switching cameras of the same type/fit and lens/scale/render aspect use live scene-camera drivers.',
        'rt_wet_marker_consumer': False}


def verify_hair_wet_before_snow(tree: bpy.types.ShaderNodeTree, receipt: dict[str, object]) -> dict[str, object]:
    """Check saved live consumers and preserve the separate dry/snow normal paths."""
    if receipt['format'] != 'HGRPHairWetBeforeSnow/1' or tree.get('hair_wet_binding') != receipt['format']:
        raise AssertionError('Saved Hair wet binding identity differs')
    wet = tree.nodes[receipt['instance']]
    if wet.node_tree.name != receipt['kernel'] or wet.node_tree.get('source_shader_sha256') != receipt['shader_sha256']:
        raise AssertionError('Saved Hair wet kernel source differs')
    for row in receipt['links']:
        source = tree.nodes[row['source']['node']].outputs[row['source']['index']]
        target = tree.nodes[row['target']['node']].inputs[row['target']['index']]
        if linked_source(target) != source:
            raise AssertionError('Saved Hair wet live link differs: ' + target.node.label + '/' + target.name)
    lighting = receipt['lighting']
    selector = tree.nodes[lighting['selector']]
    dry = lighting['dry_source']
    if linked_source(selector.inputs[1]) != tree.nodes[dry['node']].outputs[dry['index']] or linked_source(selector.inputs[0]) != wet.outputs['Enabled']:
        raise AssertionError('Saved Hair exact dry lighting bypass differs')
    for row in lighting['targets']:
        if linked_source(tree.nodes[row['node']].inputs[row['index']]) != selector.outputs[0]:
            raise AssertionError('Saved Hair wet main lighting no longer reaches its consumer')
    normal = receipt['preserved_snow_normal']
    if linked_source(tree.nodes[receipt['snow']].inputs['Geometric Normal']) != tree.nodes[normal['node']].outputs[normal['index']]:
        raise AssertionError('Hair rain must not replace the original snow geometric normal')
    camera = bpy.context.scene.camera
    if camera is None or camera.data.type != receipt['camera_type'] or camera.data.sensor_fit != receipt['sensor_fit']:
        raise AssertionError('Saved Hair host camera binding type/fit changed')
    if camera.data.type == 'ORTHO' and (wet.inputs['View Distance'].is_linked or wet.inputs['View Distance'].default_value != 1.0):
        raise AssertionError('Saved Hair orthographic source view distance must remain one')
    drivers = tuple(curve for curve in tree.animation_data.drivers if curve.data_path == tree.nodes[receipt['projection_node']].outputs[0].path_from_id('default_value'))
    if len(drivers) != 1 or not drivers[0].driver.is_valid:
        raise AssertionError('Saved Hair camera projection driver is missing or invalid')
    return {'status': 'passed', 'wet_to_snow_channels': 2, 'live_links_checked': len(receipt['links']),
        'main_lighting_consumers': len(lighting['targets']), 'additional_light_consumers': len(receipt['additional_light_consumers']),
        'ordinary_and_snow_normals_preserved': True, 'native_runtime_state_verified': False}
