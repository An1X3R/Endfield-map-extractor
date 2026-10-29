"""Expose real image samplers while preserving native node RNA and control routes."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import json
from typing import TypedDict

import bpy

from character_shader_uniforms import ShaderUniformReport, migrate_shader_uniforms


class MaterialPackagingRow(TypedDict):
    material: str
    controls: str
    textures: list[str]
    calculations: list[str]
    active_textures: list[str]
    inactive_textures: list[str]
    source_invalid_links: list[str]


class MaterialPackagingReport(TypedDict):
    format: str
    materials: list[MaterialPackagingRow]
    scope: str
    uniforms: ShaderUniformReport


@dataclass(frozen=True)
class ShaderDependencies:
    nodes: frozenset[tuple[int, int]]
    input_sockets: frozenset[int]
    output_sockets: frozenset[int]


def contains_images(tree: bpy.types.NodeTree, trail: frozenset[int]) -> bool:
    """Detect texture-bearing descendants and reject recursive node trees."""
    pointer = tree.as_pointer()
    if pointer in trail:
        raise ValueError(f'Recursive shader node tree: {tree.name}')
    return any(item.type == 'TEX_IMAGE' or (
        item.type == 'GROUP' and item.node_tree is not None
        and contains_images(item.node_tree, trail | {pointer})) for item in tree.nodes)


def validate_movable_tree(tree: bpy.types.NodeTree, trail: frozenset[int]) -> None:
    """Avoid Blender 4.4's unsafe ungroup path for animated source trees."""
    pointer = tree.as_pointer()
    if pointer in trail:
        raise ValueError(f'Recursive shader node tree: {tree.name}')
    for item in tree.nodes:
        if item.type == 'GROUP' and item.node_tree is not None and contains_images(item.node_tree, frozenset()):
            if item.node_tree.animation_data is not None:
                raise ValueError(f'Nested animated texture tree cannot be expanded safely: {item.node_tree.name}')
            validate_movable_tree(item.node_tree, trail | {pointer})
            for node in item.node_tree.nodes:
                if node.type == 'GROUP_OUTPUT' and node.is_active_output:
                    for link in tuple(link for socket in node.inputs for link in socket.links):
                        if link.from_socket.type != link.to_socket.type:
                            raise ValueError(f'Nested output conversion needs an explicit source node: {item.node_tree.name}/{link.to_socket.name}')
            for socket in item.outputs:
                if socket.is_linked and not any(
                    node.type == 'GROUP_OUTPUT' and node.is_active_output
                    and next(value for value in node.inputs if value.identifier == socket.identifier).is_linked
                    for node in item.node_tree.nodes
                ):
                    raise ValueError(f'Nested texture group has a linked constant output: {tree.name}/{item.name}/{socket.name}')


@contextmanager
def shader_editor(tree: bpy.types.NodeTree) -> Iterator[bpy.types.SpaceNodeEditor]:
    """Provide the same native node operations in foreground and background runs."""
    screen = bpy.context.screen
    if screen is None or not screen.areas:
        raise RuntimeError('Material packaging requires a Blender screen with an editor area')
    area = next((item for item in screen.areas if item.type != 'NODE_EDITOR'), screen.areas[0])
    original_type = area.type
    area.type = 'NODE_EDITOR'
    space = area.spaces.active
    space.tree_type = 'ShaderNodeTree'
    space.pin = True
    space.path.start(tree)
    region = next(item for item in area.regions if item.type == 'WINDOW')
    try:
        with bpy.context.temp_override(area=area, region=region):
            yield space
    finally:
        area.type = original_type


def select_nodes(tree: bpy.types.NodeTree, selected: Sequence[bpy.types.Node]) -> None:
    """Set native operator selection without changing node names or socket paths."""
    pointers = frozenset(item.as_pointer() for item in selected)
    for item in tree.nodes:
        item.select = item.as_pointer() in pointers
    tree.nodes.active = selected[0] if selected else None


def input_source(tree: bpy.types.NodeTree, socket: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Preserve linked sources and explicit unlinked group-instance constants."""
    if socket.is_linked:
        return socket.links[0].from_socket
    if socket.type in {'VALUE', 'INT', 'BOOLEAN'}:
        constant = tree.nodes.new('ShaderNodeValue')
        constant.outputs[0].default_value = float(socket.default_value)
    elif socket.type == 'RGBA':
        constant = tree.nodes.new('ShaderNodeRGB')
        constant.outputs[0].default_value = tuple(socket.default_value)
    elif socket.type == 'VECTOR':
        constant = tree.nodes.new('ShaderNodeCombineXYZ')
        for target, component in zip(constant.inputs, socket.default_value):
            target.default_value = component
    else:
        raise ValueError(f'Unsupported constant shader interface: {tree.name}/{socket.node.name}/{socket.name}/{socket.type}')
    constant.label = 'Preserved source constant / ' + socket.name
    return constant.outputs[0]


def flatten_image_groups(tree: bpy.types.NodeTree, space: bpy.types.SpaceNodeEditor) -> None:
    """Expand native sampling kernels, preserving instance constants explicitly.

    Expansion also removes group-level false feedback edges. Blender may mark
    those invalid even when the exact output socket dependency graph is acyclic.
    """
    while True:
        candidates = tuple(item for item in tree.nodes if item.type == 'GROUP'
                           and item.node_tree is not None and contains_images(item.node_tree, frozenset()))
        if not candidates:
            return
        for instance in candidates:
            for socket in instance.inputs:
                if socket.is_linked:
                    source = socket.links[0].from_socket
                    if source.type != socket.type:
                        tree.links.new(interface_conversion(tree, source, socket.type), socket)
                    continue
                if not hasattr(socket, 'default_value'):
                    continue
                if not any(node.type == 'GROUP_INPUT' and any(
                    value.identifier == socket.identifier and value.is_linked for value in node.outputs)
                    for node in instance.node_tree.nodes):
                    continue
                tree.links.new(input_source(tree, socket), socket)
        space.path.start(tree)
        select_nodes(tree, candidates)
        if bpy.ops.node.group_ungroup() != {'FINISHED'}:
            raise RuntimeError(f'Native texture-group expansion failed: {tree.name}')


def interface_conversion(tree: bpy.types.NodeTree, source: bpy.types.NodeSocket,
                         target_type: str) -> bpy.types.NodeSocket:
    """Retain implicit interface casts when removing their original boundary."""
    if target_type == 'VALUE':
        node = tree.nodes.new('ShaderNodeMath')
        node.operation = 'MULTIPLY'
        node.inputs[1].default_value = 1.
        target = node.inputs[0]
    elif target_type == 'VECTOR':
        node = tree.nodes.new('ShaderNodeVectorMath')
        node.operation = 'ADD'
        node.inputs[1].default_value = (0., 0., 0.)
        target = node.inputs[0]
    elif target_type == 'RGBA':
        node = tree.nodes.new('ShaderNodeMixRGB')
        node.blend_type = 'MIX'
        node.inputs[0].default_value = 0.
        target = node.inputs[1]
    else:
        raise ValueError(f'Unsupported shader interface conversion: {tree.name}/{source.type} -> {target_type}')
    node.label = 'Preserved interface conversion / ' + source.type + ' to ' + target_type
    tree.links.new(source, target)
    return node.outputs[0]


def computation_stages(tree: bpy.types.NodeTree) -> dict[int, tuple[bpy.types.Node, ...]]:
    """Partition at image sampling barriers so grouping cannot introduce cycles."""
    incoming: dict[str, list[str]] = {item.name: [] for item in tree.nodes}
    for link in tree.links:
        incoming[link.to_node.name].append(link.from_node.name)
    nodes = {item.name: item for item in tree.nodes}
    ranks: dict[str, int] = {}

    def rank(name: str, trail: frozenset[str]) -> int:
        if name in ranks:
            return ranks[name]
        if name in trail:
            raise ValueError(f'Cycle in shader graph: {tree.name}/{name}')
        result = max((rank(parent, trail | {name}) for parent in incoming[name]), default=0)
        ranks[name] = result + int(nodes[name].type == 'TEX_IMAGE')
        return ranks[name]

    stages: dict[int, list[bpy.types.Node]] = {}
    for item in tree.nodes:
        if item.type not in {'FRAME', 'GROUP_INPUT', 'GROUP_OUTPUT', 'TEX_IMAGE'}:
            stages.setdefault(rank(item.name, frozenset()), []).append(item)
    return {index: tuple(items) for index, items in stages.items()}


def package_calculations(tree: bpy.types.NodeTree, space: bpy.types.SpaceNodeEditor,
                         label: str, layout: Callable[[bpy.types.NodeTree], None]) -> tuple[bpy.types.Node, ...]:
    """Move each dependency stage with Blender's native driver/RNA migration."""
    groups: list[bpy.types.Node] = []
    for index, items in sorted(computation_stages(tree).items()):
        space.path.start(tree)
        select_nodes(tree, items)
        if bpy.ops.node.group_make() != {'FINISHED'}:
            raise RuntimeError(f'Native computation grouping failed: {tree.name}/stage {index}')
        group = tree.nodes.active
        group.node_tree.name = f'EF Compute {label} / Stage {index + 1}'
        group.label = 'UV and sampling preparation' if index == 0 else f'Shading / sampling stage {index + 1}'
        group['endfield_authoring_computation'] = True
        group.node_tree['endfield_authoring_computation'] = True
        for interface in group.node_tree.interface.items_tree:
            if interface.item_type != 'SOCKET':
                continue
            endpoints = group.inputs if interface.in_out == 'INPUT' else group.outputs
            endpoint = next(item for item in endpoints if item.identifier == interface.identifier)
            links = tuple(endpoint.links)
            if links:
                source = links[0].from_socket if interface.in_out == 'INPUT' else links[0].to_socket
                interface.name = (source.node.label or source.node.name) + ' / ' + source.name
        layout(group.node_tree)
        groups.append(group)
    return tuple(groups)


def parameter_output(instance: bpy.types.Node, identifier: str) -> bpy.types.NodeSocket:
    """Keep original parameter sockets and their animation paths as live carriers."""
    tree = instance.node_tree
    source = next(item for item in tree.interface.items_tree
                  if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.identifier == identifier)
    name = 'Parameter / ' + source.name
    existing = tuple(item for item in tree.interface.items_tree
                     if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT' and item.name == name)
    if not existing:
        socket = tree.interface.new_socket(name=name, in_out='OUTPUT', socket_type=source.socket_type)
        socket.description = 'Live original material control; retained animation and expression routes.'
        entry = next(item for item in tree.nodes if item.type == 'GROUP_INPUT')
        for output in tree.nodes:
            if output.type == 'GROUP_OUTPUT':
                tree.links.new(entry.outputs[identifier], output.inputs[socket.identifier])
    return instance.outputs[name]


def expose_source_instance(material: bpy.types.Material, instance: bpy.types.Node,
                           layout: Callable[[bpy.types.NodeTree], None]) -> MaterialPackagingRow:
    """Relocate actual samplers/calculation stages; keep source receipts intact."""
    parent = material.node_tree
    source = instance.node_tree
    source_invalid_links = [f'{link.from_node.name}/{link.from_socket.name} -> {link.to_node.name}/{link.to_socket.name}'
                            for link in source.links if not link.is_valid]
    validate_movable_tree(source, frozenset())
    scratch = source.copy()
    scratch.name = 'EF Authoring Temporary / ' + source.name
    temporary = parent.nodes.new('ShaderNodeGroup')
    temporary.node_tree = scratch
    with shader_editor(scratch) as space:
        flatten_image_groups(scratch, space)
        calculation_nodes = package_calculations(scratch, space, source.name, layout)
        movable = tuple(item for item in scratch.nodes if item.type not in {'FRAME', 'GROUP_INPUT', 'GROUP_OUTPUT'})
        boundary_inputs = tuple((link.from_socket.identifier, link.to_socket) for link in scratch.links
                                if link.from_node.type == 'GROUP_INPUT')
        active_output = next(item for item in scratch.nodes if item.type == 'GROUP_OUTPUT' and item.is_active_output)
        boundary_outputs = tuple((link.to_socket.identifier, link.from_socket) for link in scratch.links
                                 if link.to_node == active_output)
        external_outputs = tuple((link.from_socket.identifier, link.to_socket) for link in parent.links
                                 if link.from_node == instance)
        space.path.start(parent)
        space.path.append(scratch, node=temporary)
        select_nodes(scratch, movable)
        if bpy.ops.node.group_separate(type='MOVE') != {'FINISHED'}:
            raise RuntimeError(f'Native sampler relocation failed: {material.name}/{instance.name}')
        for identifier, target in boundary_inputs:
            parent.links.new(parameter_output(instance, identifier), target)
        output_map = {identifier: parameter_output(instance, socket.identifier)
                      if socket.node.type == 'GROUP_INPUT' else socket
                      for identifier, socket in boundary_outputs}
        for identifier, target in external_outputs:
            if identifier in output_map:
                parent.links.new(output_map[identifier], target)
        images = tuple(item for item in movable if item.type == 'TEX_IMAGE')
        for image in images:
            image['endfield_authoring_texture'] = True
            image['endfield_authoring_source_group'] = source.name
            image['endfield_layout_section'] = 'Editable source textures'
        instance.label = 'Material parameters / ' + source.name.removeprefix('EF Character ')
        instance['endfield_authoring_parameters'] = True
        instance['endfield_authoring_output_bindings'] = json.dumps([
            {'source_identifier': identifier, 'node': socket.node.name, 'socket_identifier': socket.identifier}
            for identifier, socket in output_map.items()], sort_keys=True)
        instance['endfield_authoring_retained_evidence'] = 'Original formula/atlas retained for exact source receipts; active sampled shader uses outer textures.'
        for output in instance.outputs:
            output.hide = not output.is_linked
        parent.nodes.remove(temporary)
        bpy.data.node_groups.remove(scratch)
        layout(parent)
    row: MaterialPackagingRow = {'material': material.name, 'controls': instance.name,
                                 'textures': [item.name for item in images],
                                 'calculations': [item.name for item in calculation_nodes],
                                 'active_textures': [], 'inactive_textures': [],
                                 'source_invalid_links': source_invalid_links}
    instance['endfield_authoring_packaging'] = json.dumps(row, sort_keys=True)
    return row


def layout_material_authoring(material: bpy.types.Material) -> None:
    """Arrange editable samplers in a bounded grid beside compact calculation stages."""
    _layout_material_authoring(material, active_shader_nodes(material))


def _layout_material_authoring(material: bpy.types.Material,
                              active: frozenset[tuple[int, int]]) -> None:
    """Arrange nodes using a dependency result computed before layout begins."""
    tree = material.node_tree
    all_images = tuple(sorted((item for item in tree.nodes if item.get('endfield_authoring_texture')),
                          key=lambda item: (str(item.get('source_slot', '')), item.label, item.name)))
    images = tuple(item for item in all_images if (tree.as_pointer(), item.as_pointer()) in active)
    inactive = tuple(item for item in all_images if (tree.as_pointer(), item.as_pointer()) not in active)
    for index, item in enumerate(images):
        item.parent = None
        item.width = 220.
        item.hide = False
        item.location = (-1250. + index % 4 * 260., -(index // 4) * 400.)
        slot = item.get('source_slot')
        if isinstance(slot, str) and slot not in item.label:
            item.label = slot + ' / ' + item.label
    if inactive:
        frame = next((item for item in tree.nodes if item.get('endfield_authoring_inactive_frame')), None)
        if frame is None:
            frame = tree.nodes.new('NodeFrame')
            frame['endfield_authoring_inactive_frame'] = True
        frame.label = 'Inactive source variants (not used by current output)'
        frame.location = (-1250., -((len(images) + 3) // 4 * 400. + 800.))
        for index, item in enumerate(inactive):
            item.parent = frame
            item.width = 220.
            item.hide = True
            item.location = (index % 4 * 260., -(index // 4) * 90.)
    for index, item in enumerate(node for node in tree.nodes if node.get('endfield_authoring_computation')):
        item.parent = None
        item.width = 280.
        item.hide = True
        item.location = (100. + index % 3 * 360., 120. - index // 3 * 140.)
    for index, item in enumerate(node for node in tree.nodes if node.get('endfield_authoring_parameters')):
        item.parent = None
        item.width = 300.
        item.hide = True
        item.location = (-1250. + index * 360., 400.)


def package_material_authoring(materials: Sequence[bpy.types.Material],
                               layout: Callable[[bpy.types.NodeTree], None]) -> MaterialPackagingReport:
    """Expose image replacement at material root after source authoring validation.

    Native copies preserve complete node RNA, packed image roles and nested driver
    groups. Original parameter instances remain live to preserve source receipts.
    Animated nested texture trees fail before mutation; root drivers migrate
    natively, and texture-free lighting/control groups remain shared.
    """
    pending = tuple((material, item) for material in materials if material.use_nodes
                    for item in material.node_tree.nodes if item.type == 'GROUP'
                    and item.node_tree is not None and not item.get('endfield_authoring_parameters')
                    and contains_images(item.node_tree, frozenset()))
    for material, instance in pending:
        if material.library is not None or instance.node_tree.library is not None:
            raise ValueError(f'Linked material trees cannot be repackaged: {material.name}/{instance.node_tree.name}')
        validate_movable_tree(instance.node_tree, frozenset())
    for material, instance in pending:
        expose_source_instance(material, instance, layout)
    rows: list[MaterialPackagingRow] = []
    for material in materials:
        if not material.use_nodes:
            continue
        active = active_shader_nodes(material)
        for instance in material.node_tree.nodes:
            if not instance.get('endfield_authoring_parameters'):
                continue
            row: MaterialPackagingRow = json.loads(instance['endfield_authoring_packaging'])
            row['active_textures'] = [name for name in row['textures']
                                      if (material.node_tree.as_pointer(), material.node_tree.nodes[name].as_pointer()) in active]
            row['inactive_textures'] = [name for name in row['textures'] if name not in row['active_textures']]
            instance['endfield_authoring_packaging'] = json.dumps(row, sort_keys=True)
            rows.append(row)
        _layout_material_authoring(material, active)
    scene = bpy.context.scene
    uniforms = migrate_shader_uniforms(materials, tuple(scene.objects), scene)
    return {'format': 'EndfieldMaterialAuthoring/1', 'materials': rows, 'uniforms': uniforms,
            'scope': 'Actual root image samplers and dependency-stage calculation groups; original parameter/atlas evidence carriers retained.'}


def socket_dependencies(outputs: Sequence[bpy.types.NodeSocket]) -> ShaderDependencies:
    """Follow exact group interfaces upstream from specified shader sockets."""
    visited: set[tuple[int, str, bool, tuple[int, ...]]] = set()
    active: set[tuple[int, int]] = set()
    active_inputs: set[int] = set()
    active_outputs: set[int] = set()
    incoming_by_tree: dict[int, dict[int, tuple[bpy.types.NodeSocket, ...]]] = {}
    active_group_output_by_tree: dict[int, bpy.types.Node] = {}

    def incoming_sources(tree: bpy.types.NodeTree, socket: bpy.types.NodeSocket) -> tuple[bpy.types.NodeSocket, ...]:
        tree_pointer = tree.as_pointer()
        incoming = incoming_by_tree.get(tree_pointer)
        if incoming is None:
            mutable: dict[int, list[bpy.types.NodeSocket]] = {}
            for link in tree.links:
                mutable.setdefault(link.to_socket.as_pointer(), []).append(link.from_socket)
            incoming = {pointer: tuple(sources) for pointer, sources in mutable.items()}
            incoming_by_tree[tree_pointer] = incoming
        return incoming.get(socket.as_pointer(), ())

    def visit(socket: bpy.types.NodeSocket, callers: tuple[bpy.types.Node, ...]) -> None:
        key = (socket.node.as_pointer(), socket.identifier, socket.is_output, tuple(item.as_pointer() for item in callers))
        if key in visited:
            return
        visited.add(key)
        if not socket.is_output:
            active_inputs.add(socket.as_pointer())
            for source in incoming_sources(socket.node.id_data, socket):
                visit(source, callers)
            return
        item = socket.node
        active_outputs.add(socket.as_pointer())
        tree = item.id_data
        active.add((tree.as_pointer(), item.as_pointer()))
        if item.type == 'GROUP':
            group_tree = item.node_tree
            group_tree_pointer = group_tree.as_pointer()
            output = active_group_output_by_tree.get(group_tree_pointer)
            if output is None:
                output = next(node for node in group_tree.nodes if node.type == 'GROUP_OUTPUT' and node.is_active_output)
                active_group_output_by_tree[group_tree_pointer] = output
            visit(next(value for value in output.inputs if value.identifier == socket.identifier), callers + (item,))
        elif item.type == 'GROUP_INPUT':
            if not callers:
                raise ValueError(f'Unowned shader group input: {tree.name}/{socket.name}')
            visit(next(value for value in callers[-1].inputs if value.identifier == socket.identifier), callers[:-1])
        else:
            for value in item.inputs:
                visit(value, callers)

    for socket in outputs:
        visit(socket, ())
    return ShaderDependencies(frozenset(active), frozenset(active_inputs), frozenset(active_outputs))


def shader_dependencies(material: bpy.types.Material) -> ShaderDependencies:
    """Follow exact group interfaces upstream from final material outputs."""
    return socket_dependencies(tuple(socket for node in material.node_tree.nodes
                                     if node.type == 'OUTPUT_MATERIAL' and node.is_active_output
                                     for socket in node.inputs))


def active_shader_nodes(material: bpy.types.Material) -> frozenset[tuple[int, int]]:
    """Return live node ownership without conflating inactive source evidence."""
    return shader_dependencies(material).nodes


def relocated_source_output(instance: bpy.types.Node, source_output: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    """Resolve an exact source output through its recorded native authoring relocation."""
    if instance.type != 'GROUP' or source_output.node != instance or not source_output.is_output:
        raise ValueError('Authoring output resolution requires an output of the exact source group instance')
    if not instance.get('endfield_authoring_parameters'):
        return source_output
    bindings = json.loads(instance['endfield_authoring_output_bindings'])
    matches = tuple(row for row in bindings if row['source_identifier'] == source_output.identifier)
    if len(matches) != 1:
        raise AssertionError(f'Authoring source output has no unique relocation: {instance.name}/{source_output.name}')
    binding = matches[0]
    node = instance.id_data.nodes.get(binding['node'])
    if node is None or node.type != 'GROUP' or not node.get('endfield_authoring_computation'):
        raise AssertionError(f'Relocated source output is missing: {instance.id_data.name}/{binding["node"]}')
    outputs = tuple(item for item in node.outputs if item.identifier == binding['socket_identifier'])
    if len(outputs) != 1:
        raise AssertionError(f'Relocated source output socket is missing or ambiguous: {node.name}/{binding["socket_identifier"]}')
    return outputs[0]


def matches_authoring_source_output(socket: bpy.types.NodeSocket, source: bpy.types.NodeTree) -> bool:
    """Accept only the exact native relocation of the source's primary output."""
    if socket.node.type == 'GROUP' and socket.node.node_tree == source:
        return socket == socket.node.outputs[0]
    parent = socket.node.id_data
    for controls in parent.nodes:
        if controls.type != 'GROUP' or controls.node_tree != source or not controls.get('endfield_authoring_parameters'):
            continue
        return relocated_source_output(controls, controls.outputs[0]) == socket
    return False


def verify_material_authoring(report: MaterialPackagingReport) -> dict[str, int | str]:
    """Require exposed samplers to drive final shading instead of duplicate UI."""
    active_textures = 0
    preserved_textures = 0
    active_parameters = 0
    node_groups = tuple(bpy.data.node_groups)
    invalid_group_owner_by_input: dict[int, int] | None = None
    calculation_image_group: str | None = None
    calculation_images_checked = False
    invalid_material_inputs_by_tree: dict[int, frozenset[int]] = {}
    for row in report['materials']:
        material = bpy.data.materials.get(row['material'])
        if material is None or not material.use_nodes:
            raise AssertionError(f'Packaged material is missing: {row["material"]}')
        tree = material.node_tree
        controls = tree.nodes.get(row['controls'])
        if controls is None or not controls.get('endfield_authoring_parameters'):
            raise AssertionError(f'Original material parameter carrier is missing: {material.name}')
        dependencies = shader_dependencies(material)
        active = dependencies.nodes
        tree_pointer = tree.as_pointer()
        invalid_material_inputs = invalid_material_inputs_by_tree.get(tree_pointer)
        if invalid_material_inputs is None:
            invalid_material_inputs = frozenset(link.to_socket.as_pointer() for link in tree.links if not link.is_valid)
            invalid_material_inputs_by_tree[tree_pointer] = invalid_material_inputs
        if dependencies.input_sockets.intersection(invalid_material_inputs):
            raise AssertionError(f'Active authoring shader contains invalid links: {material.name}/{tree.name}')
        if invalid_group_owner_by_input is None:
            invalid_group_owner_by_input = {}
            for group_index, group in enumerate(node_groups):
                for link in group.links:
                    target = link.to_socket.as_pointer()
                    if not link.is_valid and target not in invalid_group_owner_by_input:
                        invalid_group_owner_by_input[target] = group_index
        invalid_group_indices = tuple(invalid_group_owner_by_input[pointer]
                                      for pointer in dependencies.input_sockets
                                      if pointer in invalid_group_owner_by_input)
        if invalid_group_indices:
            owner = node_groups[min(invalid_group_indices)]
            raise AssertionError(f'Active authoring shader contains invalid links: {material.name}/{owner.name}')
        active_parameters += sum(socket.as_pointer() in dependencies.input_sockets for socket in controls.inputs)
        bindings = json.loads(controls['endfield_authoring_output_bindings'])
        original_outputs = tuple(next(socket for socket in controls.outputs if socket.identifier == binding['source_identifier'])
                                 for binding in bindings if next(socket for socket in tree.nodes[binding['node']].outputs
                                 if socket.identifier == binding['socket_identifier']).as_pointer() in dependencies.output_sockets)
        original_dependencies = socket_dependencies(original_outputs)
        for socket in controls.inputs:
            if socket.as_pointer() in original_dependencies.input_sockets and socket.as_pointer() not in dependencies.input_sockets:
                raise AssertionError(f'Original parameter no longer feeds the active shader: {material.name}/{socket.name}')
        for name in row['textures']:
            image = tree.nodes.get(name)
            if image is None or image.type != 'TEX_IMAGE' or not image.get('endfield_authoring_texture'):
                raise AssertionError(f'Actual outer sampler is missing: {material.name}/{name}')
            if (tree.as_pointer(), image.as_pointer()) in active:
                active_textures += 1
            else:
                preserved_textures += 1
        if not calculation_images_checked:
            calculation_image_group = next((group.name for group in node_groups
                                             if group.get('endfield_authoring_computation')
                                             and contains_images(group, frozenset())), None)
            calculation_images_checked = True
        if calculation_image_group is not None:
            raise AssertionError(f'Packaged calculation still hides textures: {calculation_image_group}')
        carrier_tree = controls.node_tree
        hidden_active_images = tuple(item.name for item in carrier_tree.nodes if item.type == 'TEX_IMAGE'
                                    and (carrier_tree.as_pointer(), item.as_pointer()) in active)
        if hidden_active_images:
            raise AssertionError(f'Original evidence samplers still drive shading: {material.name}/{hidden_active_images}')
    if report['materials'] and active_textures == 0:
        raise AssertionError('Packaged materials have no active outer image samplers')
    return {'status': 'passed', 'active_outer_samplers': active_textures,
            'preserved_inactive_outer_samplers': preserved_textures,
            'active_original_parameter_inputs': active_parameters}
