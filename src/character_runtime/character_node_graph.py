"""Shared external Blender node-tree operations for character research graphs."""
from __future__ import annotations

import bpy


def node(tree: bpy.types.NodeTree, kind: str, label: str, section: str) -> bpy.types.Node:
    result = tree.nodes.new(kind)
    result.label = label
    sections = {
        "Packed Tangent Normal - Source Channels": "Source Tangent Normal",
        "Overlay Multiply - APPROXIMATION": "Approx Overlay Multiply",
        "Shared Engine Surface - APPROXIMATION": "Approx Engine Surface",
        "Extra Pass Controls - APPROXIMATION": "Approx Extra Pass Controls",
        "Overlay Composition - APPROXIMATION": "Approx Overlay Composition",
        "Approximate Environment Controls": "Approx Environment Controls",
    }
    result["endfield_layout_section"] = sections.get(section, section)
    return result


def math_node(tree: bpy.types.NodeTree, operation: str, left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float, label: str, section: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeMath", label, section)
    result.operation = operation
    for index, value in enumerate((left, right)):
        if isinstance(value, (float, int)):
            result.inputs[index].default_value = value
        else:
            tree.links.new(value, result.inputs[index])
    return result.outputs[0]


def color_mix(tree: bpy.types.NodeTree, mode: str, factor: bpy.types.NodeSocket | float, first: bpy.types.NodeSocket | tuple[float, float, float, float], second: bpy.types.NodeSocket | tuple[float, float, float, float], label: str, section: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeMixRGB", label, section)
    result.blend_type = mode
    for index, value in enumerate((factor, first, second)):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, result.inputs[index])
        else:
            result.inputs[index].default_value = value
    return result.outputs[0]


def input_socket(tree: bpy.types.NodeTree, name: str, kind: str, value: float | tuple[float, float, float, float], description: str) -> None:
    socket = tree.interface.new_socket(name=name, in_out="INPUT", socket_type=kind)
    socket.default_value = value
    socket.description = description


