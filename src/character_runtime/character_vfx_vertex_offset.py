"""Editable source-bound normal offset with explicit host sampling limitations."""
from __future__ import annotations

import hashlib
import json
import math
from typing import TYPE_CHECKING, Mapping, Sequence

import bpy

from character_npr_vfx import CHARACTER_VFX_SHADER
from character_windows_paths import extended_path

if TYPE_CHECKING:
    from rebuild_character_materials import MaterialSource, SourcePacket

CONTRACT = 'EndfieldCharacterVfxNormalOffset/1'
LIMITATION = ('Source normal-direction offset, UV/ST, texture red, intensity, bidirectional '
              'remap and time speed drive an editable Geometry Nodes preview. Blender point '
              'normals, bilinear repeat sampling and color decoding approximate the native '
              'vertex shader; native sampler, split-normal interpolation and pass parity '
              'are unverified. Shared vertices across different offset surfaces are rejected.')


def validate_offset_controls(floats: Mapping[str, float], colors: Mapping[str, Sequence[float]]) -> None:
    """Accept only the source normal-direction mesh subset implemented below."""
    for name, expected in (('_UseVertexOffset', 1.0), ('_OffsetSwitchDir', 2.0),
                           ('_InParticle', 0.0), ('_UseVertexOffsetMask', 0.0),
                           ('_VertCameraOffset', 0.0)):
        if floats.get(name) != expected:
            raise NotImplementedError(f'Character VFX normal offset control unsupported: property={name}; value={floats.get(name)}; expected={expected}')
    if floats.get('_OffsetUVSet') not in (0.0, 1.0) or floats.get('_Bi_Offset') not in (0.0, 1.0):
        raise NotImplementedError('Character VFX offset needs an explicit UV0/UV1 and binary bidirectional switch')
    intensity = floats.get('_OffsetIntensity')
    if intensity is None or not math.isfinite(intensity):
        raise ValueError('Character VFX offset intensity must be finite')
    for name in ('_OffsetSpeed', '_OffsetDir'):
        value = colors.get(name)
        if value is None or len(value) != 4 or any(not math.isfinite(item) for item in value):
            raise ValueError(f'Character VFX offset vector missing or invalid: property={name}')
    if tuple(colors['_OffsetSpeed'][2:]) != (0.0, 0.0) or colors['_OffsetDir'][3] != 0.0:
        raise NotImplementedError('Character VFX offset custom streams are not implemented')


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _graph(tree: bpy.types.NodeTree) -> dict[str, object]:
    """Fingerprint saved node wiring, constants and sampling roles independently of time."""
    nodes: list[dict[str, object]] = []
    for node in sorted(tree.nodes, key=lambda item: item.name):
        # Authoring layout frames have no sockets or geometry evaluation semantics.
        if node.type == 'FRAME':
            continue
        values: dict[str, object] = {}
        for index, socket in enumerate(node.inputs):
            if hasattr(socket, 'default_value'):
                value = socket.default_value
                if isinstance(value, (float, int, str, bool)):
                    values[str(index)] = value
                elif value is None:
                    values[str(index)] = None
                elif isinstance(value, bpy.types.Image):
                    values[str(index)] = {'image': value.name, 'source': value.get('offset_texture_identity'),
                                          'sha256': value.get('offset_texture_sha256'),
                                          'colorspace': value.colorspace_settings.name}
                else:
                    values[str(index)] = list(value)
        properties = {key: getattr(node, key) for key in
                      ('operation', 'data_type', 'domain', 'interpolation', 'extension')
                      if hasattr(node, key)}
        nodes.append({'name': node.name, 'type': node.bl_idname, 'inputs': values, 'properties': properties})
    links = sorted((link.from_node.name, list(link.from_node.outputs).index(link.from_socket),
                    link.to_node.name, list(link.to_node.inputs).index(link.to_socket)) for link in tree.links)
    interface = [(socket.name, socket.identifier, socket.in_out, socket.socket_type)
                 for socket in tree.interface.items_tree if socket.item_type == 'SOCKET']
    return {'nodes': nodes, 'links': links, 'interface': interface}


def _node(tree: bpy.types.NodeTree, kind: str, name: str, location: tuple[int, int]) -> bpy.types.Node:
    node = tree.nodes.new(kind)
    node.name = node.label = name
    node.location = location
    return node


def _make_group(source: MaterialSource, image: bpy.types.Image,
                slots: tuple[int, ...], scale: tuple[float, float], offset: tuple[float, float],
                fps: float) -> bpy.types.NodeTree:
    tree = bpy.data.node_groups.new('EF Source Normal Offset - APPROXIMATION ' + source.name, 'GeometryNodeTree')
    tree.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    tree.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    intensity = tree.interface.new_socket(name='Source Offset Intensity', in_out='INPUT', socket_type='NodeSocketFloat')
    intensity.default_value = source.floats['_OffsetIntensity']
    clock = tree.interface.new_socket(name='Preview Time Scale', in_out='INPUT', socket_type='NodeSocketFloat')
    clock.default_value = 1.0
    entry = _node(tree, 'NodeGroupInput', 'Editable Source Controls', (-800, 500))
    output = _node(tree, 'NodeGroupOutput', 'Offset Geometry', (1000, 500))
    setter = _node(tree, 'GeometryNodeSetPosition', 'Source Normal Vertex Offset', (750, 500))
    tree.links.new(entry.outputs['Geometry'], setter.inputs['Geometry'])
    tree.links.new(setter.outputs['Geometry'], output.inputs['Geometry'])
    uv = _node(tree, 'GeometryNodeInputNamedAttribute', 'Source Offset UV', (-1000, 0))
    uv.data_type = 'FLOAT_VECTOR'
    uv.inputs['Name'].default_value = 'UV' + str(int(source.floats['_OffsetUVSet']))
    st = _node(tree, 'ShaderNodeVectorMath', 'Source Offset ST Scale', (-800, 0))
    st.operation = 'MULTIPLY'
    st.inputs[1].default_value = (*scale, 1.0)
    tree.links.new(uv.outputs['Attribute'], st.inputs[0])
    origin = _node(tree, 'ShaderNodeVectorMath', 'Source Offset ST Origin', (-600, 0))
    origin.operation = 'ADD'
    origin.inputs[1].default_value = (*offset, 0.0)
    tree.links.new(st.outputs['Vector'], origin.inputs[0])
    time = _node(tree, 'GeometryNodeInputSceneTime', 'Scene Time', (-1000, -250))
    zero = _node(tree, 'ShaderNodeMath', 'Frame One Is Source Time Zero', (-800, -250))
    zero.operation = 'SUBTRACT'
    zero.inputs[1].default_value = 1.0 / fps
    tree.links.new(time.outputs['Seconds'], zero.inputs[0])
    time_scale = _node(tree, 'ShaderNodeMath', 'Editable Preview Clock', (-600, -250))
    time_scale.operation = 'MULTIPLY'
    tree.links.new(zero.outputs[0], time_scale.inputs[0])
    tree.links.new(entry.outputs['Preview Time Scale'], time_scale.inputs[1])
    speed = _node(tree, 'ShaderNodeVectorMath', 'Source Offset Speed', (-400, -250))
    speed.operation = 'SCALE'
    speed.inputs[0].default_value = (*source.colors['_OffsetSpeed'][:2], 0.0)
    tree.links.new(time_scale.outputs[0], speed.inputs[3])
    animated = _node(tree, 'ShaderNodeVectorMath', 'UV Plus Source Time', (-200, 0))
    animated.operation = 'ADD'
    tree.links.new(origin.outputs[0], animated.inputs[0])
    tree.links.new(speed.outputs[0], animated.inputs[1])
    sample = _node(tree, 'GeometryNodeImageTexture', 'Packed Source Offset Texture', (0, 0))
    sample.inputs['Image'].default_value = image
    sample.interpolation = 'Linear'
    sample.extension = 'REPEAT'
    tree.links.new(animated.outputs[0], sample.inputs['Vector'])
    red = _node(tree, 'FunctionNodeSeparateColor', 'Source Offset Red', (200, 0))
    tree.links.new(sample.outputs['Color'], red.inputs['Color'])
    remap = _node(tree, 'ShaderNodeMath', 'Source Bidirectional Scale', (200, -200))
    remap.operation = 'MULTIPLY_ADD'
    remap.inputs[1].default_value = 1.0 + source.floats['_Bi_Offset']
    remap.inputs[2].default_value = -source.floats['_Bi_Offset']
    tree.links.new(red.outputs['Red'], remap.inputs[0])
    amount = _node(tree, 'ShaderNodeMath', 'Source Offset Intensity', (400, -100))
    amount.operation = 'MULTIPLY'
    tree.links.new(remap.outputs[0], amount.inputs[0])
    tree.links.new(entry.outputs['Source Offset Intensity'], amount.inputs[1])
    normal = _node(tree, 'GeometryNodeInputNormal', 'Source Normal Direction Mode 2', (200, 250))
    vector = _node(tree, 'ShaderNodeVectorMath', 'Normal Times Offset', (550, 250))
    vector.operation = 'SCALE'
    tree.links.new(normal.outputs['Normal'], vector.inputs[0])
    tree.links.new(amount.outputs[0], vector.inputs[3])
    tree.links.new(vector.outputs[0], setter.inputs['Offset'])
    # Explicit face-to-point adaptation is safe only after shared-boundary rejection.
    material = _node(tree, 'GeometryNodeInputMaterialIndex', 'Native Submesh Index', (-800, 800))
    previous: bpy.types.NodeSocket | None = None
    for index, slot in enumerate(slots):
        compare = _node(tree, 'FunctionNodeCompare', 'Source Submesh ' + str(slot), (-500, 800 + index * 160))
        compare.data_type = 'INT'
        compare.operation = 'EQUAL'
        compare.inputs[3].default_value = slot
        tree.links.new(material.outputs[0], compare.inputs[2])
        current = compare.outputs[0]
        if previous is not None:
            union = _node(tree, 'FunctionNodeBooleanMath', 'Source Surface Union ' + str(index), (-200, 800 + index * 160))
            union.operation = 'OR'
            tree.links.new(previous, union.inputs[0])
            tree.links.new(current, union.inputs[1])
            current = union.outputs[0]
        previous = current
    domain = _node(tree, 'GeometryNodeFieldOnDomain', 'Source Surface Vertex Membership', (400, 650))
    domain.data_type = 'BOOLEAN'
    domain.domain = 'FACE'
    tree.links.new(previous, domain.inputs['Value'])
    tree.links.new(domain.outputs[0], setter.inputs['Selection'])
    tree['contract'] = CONTRACT
    tree['source_identity'] = _json(source.identity)
    tree['scope'] = LIMITATION
    return tree


def install_vertex_offsets(packet: SourcePacket, scene: bpy.types.Scene, skipped_material_identities: frozenset[tuple[str, int]]) -> list[dict[str, object]]:
    """Install exact renderer/submesh-bound modifiers before material adaptation."""
    from rebuild_character_materials import array_value, identity, int_value, object_value, string_value
    sources = {source.identity: source for source in packet.materials
               if source.shader == CHARACTER_VFX_SHADER and source.floats.get('_UseVertexOffset') == 1.0}
    renderers = {identity(row['object'], 'renderer'): row for row in packet.renderers}
    result: list[dict[str, object]] = []
    for decision in packet.selection:
        cab, path_id = string_value(decision['renderer_identity'], 'renderer_identity').rsplit(':', 1)
        renderer = renderers[(cab.casefold(), int(path_id))]
        refs = tuple(identity(object_value(raw, 'material')['resolved'], 'material')
                     for raw in array_value(renderer['materials'], 'materials'))
        count = int_value(renderer['submesh_count'], 'submesh_count')
        obj = scene.objects[string_value(decision['fbx_object_name'], 'mesh')]
        for key in sorted(set(refs).intersection(sources)):
            if key in skipped_material_identities:
                continue
            source = sources[key]
            validate_offset_controls(source.floats, source.colors)
            if '_USE_VERTOFFSET' not in source.keywords:
                raise ValueError(f'Enabled source offset has no matching native keyword: material={key}')
            if source.shader_property_types.get('_OffsetSwitchDir') != 2:
                raise ValueError('Source normal-offset direction property type is not verified')
            slots = tuple(sorted({min(index, count - 1) for index, ref in enumerate(refs) if ref == key}))
            if any(index >= count for index, ref in enumerate(refs) if ref == key):
                raise NotImplementedError(f'Offset on an extra renderer material pass needs separate geometry: mesh={obj.name}; material={key}')
            selected = {vertex for face in obj.data.polygons if face.material_index in slots for vertex in face.vertices}
            unrelated = {vertex for face in obj.data.polygons if face.material_index not in slots for vertex in face.vertices}
            if not selected or selected.intersection(unrelated):
                raise NotImplementedError(f'Offset surface has no vertices or shares vertices with unrelated submeshes: mesh={obj.name}; slots={slots}')
            if any(_is_offset(modifier) for modifier in obj.modifiers):
                raise NotImplementedError(f'Multiple offset source passes on one mesh are unsupported: mesh={obj.name}')
            bindings = tuple(binding for binding in source.textures if binding.name == '_OffsetTex')
            if len(bindings) != 1:
                raise ValueError(f'Enabled normal offset needs exactly one packed source texture: material={key}')
            binding = bindings[0]
            uv = 'UV' + str(int(source.floats['_OffsetUVSet']))
            if binding.uv_index != int(source.floats['_OffsetUVSet']) or obj.data.uv_layers.get(uv) is None:
                raise ValueError(f'Offset source UV binding is absent or inconsistent: mesh={obj.name}; uv={uv}')
            texture = packet.textures[binding.identity]
            image = bpy.data.images.load(extended_path(texture.path), check_existing=False)
            image.name = 'EF OFFSET ' + texture.name
            image.colorspace_settings.name = 'Non-Color' if texture.color_space == 0 else 'sRGB'
            image.alpha_mode = 'STRAIGHT'
            image.pack()
            image['offset_texture_identity'] = _json(texture.identity)
            image['offset_texture_sha256'] = texture.sha256
            tree = _make_group(source, image, slots, binding.scale, binding.offset,
                               scene.render.fps / scene.render.fps_base)
            modifier = obj.modifiers.new('EF Source Normal Offset - APPROXIMATION', 'NODES')
            modifier.node_group = tree
            result.append({'object': obj.name, 'modifier': modifier.name, 'group': tree.name,
                           'material_identity': list(key), 'source_slots': list(slots),
                           'selected_vertices': sorted(selected), 'texture': image.name,
                           'graph': _graph(tree), 'scope': LIMITATION})
    return result


def require_installed_offset(source: MaterialSource, mesh_names: tuple[str, ...]) -> None:
    """Reject material-only activation when matching geometry has not been installed."""
    validate_offset_controls(source.floats, source.colors)
    for name in mesh_names:
        matches = tuple(modifier for modifier in bpy.data.objects[name].modifiers
                        if _is_offset(modifier)
                        and modifier.node_group.get('source_identity') == _json(source.identity))
        if len(matches) != 1 or matches[0].node_group is None:
            raise ValueError(f'Character VFX offset geometry is absent or ambiguous: mesh={name}; material={source.identity}')


def verify_vertex_offsets(receipt: Sequence[dict[str, object]], scene: bpy.types.Scene) -> dict[str, object]:
    """Check installed graphs, packed textures and evaluated finite multi-frame geometry."""
    expected = {(str(row['object']), str(row['modifier'])) for row in receipt}
    actual = {(obj.name, modifier.name) for obj in scene.objects for modifier in obj.modifiers
              if _is_offset(modifier)}
    if actual != expected:
        raise ValueError(f'Vertex-offset modifier coverage changed: expected={expected}; actual={actual}')
    saved = scene.frame_current
    frames: list[dict[str, object]] = []
    try:
        for row in receipt:
            obj = scene.objects[str(row['object'])]
            modifier = obj.modifiers[str(row['modifier'])]
            tree = modifier.node_group
            if (tree is None or tree.name != row['group'] or not modifier.show_viewport or not modifier.show_render
                    or tree.get('source_identity') != _json(row['material_identity'])
                    or _json(_graph(tree)) != _json(row['graph'])):
                raise ValueError(f'Vertex-offset graph, source identity or enable state changed: object={obj.name}')
            if tree.animation_data is not None:
                raise ValueError(f'Unexpected vertex-offset node animation data: object={obj.name}')
            for socket in tree.interface.items_tree:
                if socket.item_type == 'SOCKET' and socket.in_out == 'INPUT' and socket.socket_type != 'NodeSocketGeometry':
                    actual_value = modifier.get(socket.identifier, socket.default_value)
                    if actual_value != socket.default_value:
                        raise ValueError(f'Vertex-offset saved editable input changed: object={obj.name}; input={socket.name}')
            image = bpy.data.images.get(str(row['texture']))
            if image is None or image.packed_file is None or min(image.size) < 1:
                raise ValueError(f'Vertex-offset source image is not packed: object={obj.name}')
            selected = set(row['selected_vertices'])
            actual_selected = {vertex for face in obj.data.polygons if face.material_index in row['source_slots']
                               for vertex in face.vertices}
            if selected != actual_selected:
                raise ValueError(f'Vertex-offset source surface membership changed: object={obj.name}')
            per_frame: list[dict[str, object]] = []
            for frame in (1, 13, 25):
                scene.frame_set(frame)
                evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
                mesh = evaluated.to_mesh()
                try:
                    if len(mesh.vertices) != len(obj.data.vertices):
                        raise ValueError(f'Vertex-offset changed source vertex count: object={obj.name}')
                    coordinates = [tuple(float(value) for value in vertex.co) for vertex in mesh.vertices]
                    if any(not math.isfinite(value) for vertex in coordinates for value in vertex):
                        raise ValueError(f'Vertex-offset generated nonfinite geometry: object={obj.name}; frame={frame}')
                    deltas = [(vertex.co - obj.data.vertices[index].co).length for index, vertex in enumerate(mesh.vertices)]
                    if any(delta > 1e-7 for index, delta in enumerate(deltas) if index not in selected):
                        raise ValueError(f'Vertex-offset changed unrelated source surface: object={obj.name}; frame={frame}')
                    per_frame.append({'frame': frame, 'sha256': hashlib.sha256(_json(coordinates).encode()).hexdigest(),
                                      'max_local_offset': max(deltas)})
                finally:
                    evaluated.to_mesh_clear()
            frames.append({'object': obj.name, 'samples': per_frame,
                           'observed_time_variation': len({sample['sha256'] for sample in per_frame}) > 1})
    finally:
        scene.frame_set(saved)
    return {'format': CONTRACT, 'status': 'verified_host_approximation', 'frames': frames, 'scope': LIMITATION}


def source_modifiers_without_offsets(obj: bpy.types.Object) -> Sequence[bpy.types.Modifier]:
    """Exclude only caller-verified offset modifiers from the original source digest."""
    return tuple(modifier for modifier in obj.modifiers if not _is_offset(modifier))


def verify_offset_surface_bindings(receipt: Sequence[dict[str, object]], scene: bpy.types.Scene) -> None:
    """Require the installed offset to still target its exact source material surfaces."""
    for row in receipt:
        obj = scene.objects[str(row['object'])]
        for index in row['source_slots']:
            material = obj.material_slots[index].material
            if material is None or 'source_material_stack' not in material:
                raise ValueError(f'Offset source surface lost its material stack: object={obj.name}; slot={index}')
            stack = json.loads(material['source_material_stack'])
            if stack != [row['material_identity']]:
                raise ValueError(f'Offset source surface binding changed: object={obj.name}; slot={index}; actual={stack}')


def _is_offset(modifier: bpy.types.Modifier) -> bool:
    return (modifier.type == 'NODES' and modifier.node_group is not None
            and modifier.node_group.get('contract') == CONTRACT)
