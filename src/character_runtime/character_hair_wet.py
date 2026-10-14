"""Compiled Hair rain specialization of the shared hashed-drop arithmetic.

The Skin factory creates a fresh graph on each call. Only this new graph is
specialized; existing Skin materials and the shared factory remain untouched.
Hair uses UV.x for its radius seed, different frequencies, camera attenuation,
uniform color darkening and strand gain. Wet Normal feeds a separate highlight
and must not replace the ordinary surface or snow normal.
"""
from __future__ import annotations

from typing import Callable

import bpy

from character_skin_rain import build_skin_rain, _math, _clamp, _separate
from character_weather_nodes import scalar_mix, smoothstep


SECTION = "Compiled Hair Rain"


def _find(tree: bpy.types.ShaderNodeTree, label: str) -> bpy.types.Node:
    matches = tuple(item for item in tree.nodes if item.label == label)
    if len(matches) != 1:
        raise ValueError(f"Hair rain shared graph requires one node: {label}; found {len(matches)}")
    return matches[0]


def _constant(tree: bpy.types.ShaderNodeTree, label: str, index: int, expected: float, value: float) -> None:
    socket = _find(tree, label).inputs[index]
    if socket.is_linked or abs(socket.default_value - expected) > 1e-6:
        raise ValueError(f"Hair rain shared graph constant changed: {label}/{index}")
    socket.default_value = value


def _append_input(tree: bpy.types.ShaderNodeTree, label: str, kind: str, value: float | tuple[float, float, float], description: str) -> None:
    socket = tree.interface.new_socket(name=label, in_out="INPUT", socket_type=kind)
    socket.default_value = value
    socket.description = description


def _specialize_radius(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node) -> None:
    """Match Hair _1236.._1258, including the intentional UV.xx swizzle."""
    uv = _separate(tree, entry.outputs["UV"], "Hair source BaseMap UV")
    tiled = _math(tree, "MULTIPLY", uv[0], entry.outputs["Global Tiling"], "Hair radius tiled U")
    first = _math(tree, "MULTIPLY", tiled, 32., "Hair radius U frequency")
    cell_input = _math(tree, "MULTIPLY", first, 1.5, "Hair radius U cell scale")
    for axis in (0, 2):
        tree.links.new(cell_input, _find(tree, "Radius cell " + str(axis)).inputs[0])
    noise = _find(tree, "Radius outer fract").outputs[0]
    z = _find(tree, "Source tiled coordinate 2").outputs[0]
    height = _clamp(tree, _math(tree, "MULTIPLY", z, 10., "Hair radius projection Z scale"), 0., 1., "Hair radius projection Z clamp")
    weighted = _math(tree, "MULTIPLY", noise, 1.2000000476837158, "Hair radius phase weight")
    amount = _clamp(tree, _math(tree, "ADD", weighted, height, "Hair radius combined mask"), 0., 1., "Hair radius mask clamp")
    for label in ("Source radius multiplier inverse", "Source radius multiplier second"):
        tree.links.new(amount, _find(tree, label).inputs[1])
    _constant(tree, "Source radius multiplier first", 0, .30000001192092896, .6000000238418579)
    _constant(tree, "Source radius multiplier second", 0, .6499999761581421, 1.)


def _specialize_camera(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node) -> None:
    """Match Hair _1901 camera coverage without changing the dry branch."""
    projection = _math(tree, "ABSOLUTE", entry.outputs["Camera Projection Y"], 0., "Hair absolute projection Y")
    ratio = _math(tree, "DIVIDE", projection, entry.outputs["View Distance"], "Hair camera projection distance ratio")
    fade = scalar_mix(tree, smoothstep(tree, 0., 4., ratio, "Hair camera distance response"), .4000000059604645, 1., "Hair camera coverage multiplier")
    target = _find(tree, "Source view-weighted coverage").inputs[1]
    if len(target.links) != 1:
        raise ValueError("Hair rain needs the shared view-cosine coverage producer")
    cosine = target.links[0].from_socket
    factor = _math(tree, "MULTIPLY", cosine, fade, "Hair view and camera coverage")
    tree.links.new(factor, target)
    strength = _math(tree, "MULTIPLY", 2.5, cosine, "Hair view-weighted gradient strength")
    for axis in ("x", "y"):
        tree.links.new(strength, _find(tree, "Gradient " + axis + " strength").inputs[1])


def _specialize_uniforms(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node, output: bpy.types.Node) -> None:
    """Match Hair _660, _1259, _1261 and the independent _1973 strand gain."""
    wet = _find(tree, "Source combined wetness").outputs[0]
    maximum = _math(tree, "MAXIMUM", entry.outputs["Rain Intensity"], wet, "Hair rain and adjusted wet maximum")
    inverse = _math(tree, "SUBTRACT", 1., maximum, "Hair uniform dry weight")
    darkening = _math(tree, "ADD", inverse, _math(tree, "MULTIPLY", .800000011920929, maximum, "Hair uniform wet color weight"), "Hair uniform color factor")
    tone = _find(tree, "Source rain tone").outputs[0]
    final = _math(tree, "MULTIPLY", tone, darkening, "Hair rain tone with uniform darkening")
    tree.links.new(final, _find(tree, "Material branch color tone second").inputs[0])
    gain = _math(tree, "ADD", inverse, _math(tree, "MULTIPLY", 2., maximum, "Hair uniform double wet weight"), "Hair uniform strand gain")
    gate = _find(tree, "Source material weather branch").outputs[0]
    tree.links.new(scalar_mix(tree, gate, 1., gain, "Hair strand gain branch"), output.inputs["Wet Specular Gain"])
    tree.links.new(gate, output.inputs["Enabled"])


def make_native_hair_wet(source_shader_sha256: str, layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    """Create the audited Hair specialization, preserving the shared Skin graph."""
    tree = build_skin_rain(source_shader_sha256, lambda graph: None)
    tree.name = "EF Native Hair Procedural Rain - Compiled"
    tree["source_formula"] = "Hair opaque blob126 lines 77123-77417; alpha blob131 lines 87633-87927"
    tree["supported_branch"] = "Hair opaque and alpha compiled rain; original surface and snow normals preserved"
    tree["scope"] = "Shared hashed-drop arithmetic specialized to Hair source; projection and camera are explicit supplied inputs, not live game captures. RT Wet Marker is not opacity or roughness."
    tree["shared_arithmetic"] = "character_skin_rain fresh factory; no existing Skin graph modified"
    _append_input(tree, "UV", "NodeSocketVector", (0., 0., 0.), "Source BaseMap transformed UV; Hair radius hash intentionally uses U for both cell axes.")
    _append_input(tree, "View Distance", "NodeSocketFloat", 1., "Positive source view-vector distance; host camera binding must be disclosed.")
    _append_input(tree, "Camera Projection Y", "NodeSocketFloat", 1., "Source projection matrix [1].y; supply the actual host camera projection when reconstructed.")
    for label in ("Wet Specular Gain", "Enabled"):
        tree.interface.new_socket(name=label, in_out="OUTPUT", socket_type="NodeSocketFloat")
    entry = _find(tree, "Explicit native projection and surface")
    output = _find(tree, "Skin rain material and separate specular inputs")
    output.label = "Hair rain material and separate specular inputs"
    for prefix, old, new in (("First layer", 24., 32.), ("Second layer", 36.27000045776367, 48.34560012817383)):
        for axis in range(3):
            _constant(tree, prefix + " coordinate " + str(axis), 1, old, new)
    _constant(tree, "Source cell gate amount", 0, .800000011920929, .6499999761581421)
    _constant(tree, "Layer speed 1 second", 0, 4.345600128173828, 4.340000152587891)
    _constant(tree, "Second radius normalization 0", 1, .661703884601593, .6619009971618652)
    _specialize_radius(tree, entry)
    _specialize_camera(tree, entry)
    _specialize_uniforms(tree, entry, output)
    for item in tree.nodes:
        if item.label.startswith("First layer"):
            section = "Hair Rain - Primary Drops"
        elif item.label.startswith("Second layer"):
            section = "Hair Rain - Secondary Drops"
        elif "Radius" in item.label or "radius" in item.label:
            section = "Hair Rain - Radius and UV Seed"
        elif any(word in item.label for word in ("normal", "Normal", "tangent", "Tangent")):
            section = "Hair Rain - Separate Wet Normal"
        else:
            section = SECTION
        item["endfield_layout_section"] = section
    layout(tree)
    return tree
