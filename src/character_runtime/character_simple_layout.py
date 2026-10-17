"""Arrange compact character graphs without changing their shader contracts."""
from __future__ import annotations

import bpy


_FRAME_MARKER = 'endfield_simple_layout_frame'


def _section(item: bpy.types.Node) -> tuple[int, str]:
    if item.type == 'GROUP_INPUT':
        return 0, '01 / Channel inputs'
    if item.type in {'UVMAP', 'TEX_COORD', 'MAPPING', 'ATTRIBUTE', 'VERTEX_COLOR'}:
        return 0, '01 / Source coordinates'
    if item.type == 'TEX_IMAGE':
        return 1, '02 / Editable source textures'
    if item.type in {'VALUE', 'RGB'} or item.get('endfield_simple_face_light'):
        return 0, '01 / Artist and source controls'
    if item.type == 'GROUP' and item.node_tree is not None:
        if item.node_tree.get('endfield_simple_full_weather_controls'):
            return 0, '01 / Selected weather controls'
        if item.get('endfield_simple_channels'):
            return 3, '04 / Surface channels'
        if item.node_tree.get('endfield_simple_weather'):
            return 4, '05 / Editable weather'
    if item.type in {'OUTPUT_MATERIAL', 'GROUP_OUTPUT'}:
        return 6, '07 / Output'
    if item.type.startswith('BSDF_') or item.type in {'EMISSION', 'MIX_SHADER', 'ADD_SHADER'}:
        return 5, '06 / Surface and pass composition'
    section = str(item.get('endfield_layout_section', 'Channel operations'))
    return 2, '03 / ' + section.removeprefix('Simple ')


def _height(item: bpy.types.Node) -> float:
    """Reserve panel and socket space before Blender draws node dimensions."""
    if item.hide:
        return 80.
    inputs = sum(3 if socket.type == 'VECTOR' and not socket.is_linked and not socket.hide_value else 1
                 for socket in item.inputs if not socket.hide and socket.enabled)
    outputs = sum(not socket.hide and socket.enabled for socket in item.outputs)
    panel = {'TEX_IMAGE': 400., 'RGB': 380., 'MAPPING': 440., 'VALTORGB': 360.,
             'CURVE_RGB': 420., 'CURVE_VEC': 420., 'BSDF_PRINCIPLED': 1200.}.get(item.type, 180.)
    return max(panel, 100. + 32. * max(inputs, outputs)) + (180. if item.show_preview else 0.)


def _ordered(tree: bpy.types.NodeTree, members: tuple[bpy.types.Node, ...]) -> tuple[bpy.types.Node, ...]:
    """Keep each bounded frame in upstream order with stable disconnected nodes."""
    pending = {item.name: item for item in members}
    result: list[bpy.types.Node] = []
    while pending:
        ready = tuple(sorted((item for item in pending.values() if not any(
            link.from_node.name in pending for socket in item.inputs for link in socket.links)),
            key=lambda item: (str(item.get('source_slot', '')), item.label, item.name)))
        if not ready:
            raise ValueError(f'Cannot lay out a cyclic shader frame: tree={tree.name}, nodes={sorted(pending)}')
        result.extend(ready)
        for item in ready:
            del pending[item.name]
    return tuple(result)


def _layout_tree(tree: bpy.types.NodeTree) -> None:
    members = tuple(item for item in tree.nodes if item.type != 'FRAME')
    frames = tuple(item for item in tree.nodes if item.type == 'FRAME')
    if any(not item.get(_FRAME_MARKER) and not item.get('endfield_layout_frame') for item in frames):
        raise ValueError(f'Compact layout cannot replace a manually authored frame: {tree.name}')
    for item in members:
        item.parent = None
    for frame in frames:
        tree.nodes.remove(frame)
    sections: dict[tuple[int, str], list[bpy.types.Node]] = {}
    for item in members:
        sections.setdefault(_section(item), []).append(item)
        item.width = 260. if item.type in {'TEX_IMAGE', 'GROUP', 'BSDF_PRINCIPLED'} else 220.
    stage_x = 0.
    for stage in sorted({key[0] for key in sections}):
        stage_width = 0.
        frame_y = 0.
        for key in sorted(key for key in sections if key[0] == stage):
            ordered = _ordered(tree, tuple(sections[key]))
            columns = min(3 if stage == 2 else 2 if stage in {0, 1} else 1, len(ordered))
            column_width = max(item.width for item in ordered) + 72.
            frame = tree.nodes.new('NodeFrame')
            frame[_FRAME_MARKER] = True
            frame.label = key[1]
            frame.name = 'EF Compact Layout / ' + key[1]
            frame.label_size = 24
            frame.shrink = False
            frame.use_custom_color = True
            frame.color = ((.16, .25, .28) if stage in {0, 1, 4} else (.20, .21, .24))
            frame.width = max(columns * column_width + 24., 32. + 13. * len(frame.label))
            row_y = -72.
            for start in range(0, len(ordered), columns):
                row = ordered[start:start + columns]
                for column, item in enumerate(row):
                    item.parent = frame
                    item.location = (32. + column * column_width, row_y)
                row_y -= max(_height(item) for item in row) + 48.
            frame.height = -row_y + 16.
            frame.location = (stage_x, frame_y)
            stage_width = max(stage_width, frame.width)
            frame_y -= frame.height + 120.
        stage_x += stage_width + 180.


def layout_simple_character_material(material: bpy.types.Material) -> None:
    """Expose root samplers and controls beside bounded calculation frames."""
    if not material.use_nodes or not material.get('endfield_simple_material'):
        raise ValueError(f'Compact layout requires a simplified character material: {material.name}')
    _layout_tree(material.node_tree)
    pending = [item.node_tree for item in material.node_tree.nodes if item.type == 'GROUP'
               and item.node_tree is not None and (item.node_tree.get('endfield_simple_calculation')
               or item.node_tree.get('endfield_simple_weather'))]
    visited: set[int] = set()
    while pending:
        tree = pending.pop()
        if tree.as_pointer() in visited:
            continue
        visited.add(tree.as_pointer())
        _layout_tree(tree)
