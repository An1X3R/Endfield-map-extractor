"""Check compact layout against a real exported character without rendering."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from character_simple_layout import _height, _ordered, _section, layout_simple_character_material


def _value(value: object) -> object:
    if isinstance(value, bpy.types.ID):
        return (value.bl_rna.identifier, value.name_full)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return tuple(value)


def _properties(owner: bpy.types.bpy_struct, excluded: frozenset[str]) -> dict[str, object]:
    return {prop.identifier: _value(getattr(owner, prop.identifier)) for prop in owner.bl_rna.properties
            if prop.identifier not in excluded and prop.type != 'COLLECTION' and not prop.is_readonly}


def _trees(materials: tuple[bpy.types.Material, ...]) -> tuple[bpy.types.NodeTree, ...]:
    pending = [material.node_tree for material in materials]
    visited: dict[int, bpy.types.NodeTree] = {}
    while pending:
        tree = pending.pop()
        if tree.as_pointer() in visited:
            continue
        visited[tree.as_pointer()] = tree
        pending.extend(item.node_tree for item in tree.nodes if item.type == 'GROUP' and item.node_tree is not None)
    return tuple(visited.values())


def _semantics(trees: tuple[bpy.types.NodeTree, ...]) -> str:
    excluded = frozenset({'name', 'label', 'location', 'location_absolute', 'width', 'width_hidden', 'height',
                         'dimensions', 'parent', 'select', 'hide', 'show_preview', 'show_options', 'show_texture',
                         'color', 'use_custom_color', 'rna_type', 'id_data'})
    rows: list[object] = []
    for tree in trees:
        nodes = [(item.name, item.bl_idname, dict(item.items()), _properties(item, excluded),
                  tuple(_value(socket.default_value) if hasattr(socket, 'default_value') else None for socket in item.inputs),
                  tuple(_value(socket.default_value) if hasattr(socket, 'default_value') else None for socket in item.outputs))
                 for item in tree.nodes if item.type != 'FRAME']
        links = sorted((link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier)
                       for link in tree.links)
        drivers = []
        if tree.animation_data is not None:
            for curve in tree.animation_data.drivers:
                variables = [(variable.name, variable.type, tuple(_properties(target, frozenset())
                              for target in variable.targets)) for variable in curve.driver.variables]
                drivers.append((curve.data_path, curve.array_index, curve.driver.type, curve.driver.expression,
                                curve.driver.use_self, variables))
        interface = [_properties(item, frozenset({'parent'})) for item in tree.interface.items_tree]
        rows.append((tree.name, nodes, links, drivers, interface))
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def _layout(trees: tuple[bpy.types.NodeTree, ...]) -> tuple[object, ...]:
    return tuple((tree.name, tuple((item.name, item.parent.name if item.parent else None,
                                   tuple(item.location), item.width, item.height) for item in tree.nodes))
                 for tree in trees)


def _intersects(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> bool:
    return first[0] < second[2] and second[0] < first[2] and first[1] > second[3] and second[1] > first[3]


def _verify_stable_order(trees: tuple[bpy.types.NodeTree, ...]) -> int:
    """Compare indexed traversal with the prior socket-based layer ordering."""
    checked = 0
    for tree in trees:
        sections: dict[tuple[int, str], list[bpy.types.Node]] = {}
        for item in tree.nodes:
            if item.type != 'FRAME':
                sections.setdefault(_section(item), []).append(item)
        for members in sections.values():
            pending = {item.name: item for item in members}
            expected: list[bpy.types.Node] = []
            while pending:
                ready = tuple(sorted((item for item in pending.values() if not any(
                    link.from_node.name in pending for socket in item.inputs for link in socket.links)),
                    key=lambda item: (str(item.get('source_slot', '')), item.label, item.name)))
                if not ready:
                    raise AssertionError(f'Real compact fixture has a cyclic frame: {tree.name}/{sorted(pending)}')
                expected.extend(ready)
                for item in ready:
                    del pending[item.name]
            actual = _ordered(tree, tuple(members))
            if actual != tuple(expected):
                raise AssertionError(f'Indexed layout changed stable node order: {tree.name}')
            checked += 1
    return checked


def _verify_geometry(tree: bpy.types.NodeTree) -> int:
    frames = tuple(item for item in tree.nodes if item.get('endfield_simple_layout_frame'))
    if not frames:
        return 0
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for frame in frames:
        x, y = frame.location
        boxes.append((frame.name, (x, y, x + frame.width, y - frame.height)))
        children = tuple(item for item in tree.nodes if item.parent == frame)
        node_boxes = []
        for item in children:
            left, top = item.location
            height = max(_height(item), float(item.dimensions.y))
            width = max(item.width, float(item.dimensions.x))
            box = (left, top, left + width, top - height)
            if left < 0. or top > -48. or box[2] > frame.width or box[3] < -frame.height:
                raise AssertionError(f'Node escapes its frame: {tree.name}/{frame.name}/{item.name}; box={box}')
            node_boxes.append((item.name, box))
        for index, (name, box) in enumerate(node_boxes):
            for other_name, other_box in node_boxes[index + 1:]:
                if _intersects(box, other_box):
                    raise AssertionError(f'Node overlap: {tree.name}/{name}/{other_name}')
    for index, (name, box) in enumerate(boxes):
        for other_name, other_box in boxes[index + 1:]:
            if _intersects(box, other_box):
                raise AssertionError(f'Frame overlap: {tree.name}/{name}/{other_name}')
    return len(frames)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    materials = tuple(material for material in bpy.data.materials if material.get('endfield_simple_material'))
    if not materials:
        raise AssertionError('The real fixture contains no simplified character materials')
    trees = _trees(materials)
    ordered_sections = _verify_stable_order(trees)
    before = _semantics(trees)
    for material in materials:
        layout_simple_character_material(material)
    if before != _semantics(trees):
        raise AssertionError('Layout changed shader nodes, sockets, images, links, interfaces or drivers')
    first_layout = _layout(trees)
    frame_count = sum(_verify_geometry(tree) for tree in trees)
    for material in materials:
        layout_simple_character_material(material)
    if first_layout != _layout(trees) or before != _semantics(trees):
        raise AssertionError('Repeating compact layout changed positions or shader semantics')
    document = {'status': 'passed', 'blend': bpy.data.filepath, 'materials': len(materials),
                'reachable_trees': len(trees), 'frames': frame_count, 'semantic_sha256': before,
                'semantic_invariance': True, 'idempotent': True, 'overlapping_nodes': 0,
                'stable_order_sections': ordered_sections,
                'overlapping_frames': 0, 'bounds': 'conservative panel/socket estimates and stored dimensions'}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(document, ensure_ascii=False))


if __name__ == '__main__':
    main()
