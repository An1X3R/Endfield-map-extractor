"""Explicit-input HGRP diffuse arithmetic; no runtime state is fabricated here.

The graph follows the fixed Ruri ComputeNPRDiffuse transcription. Consumers must
supply validated pipeline state or label their chosen values as artist overrides.
Screen shadow A, B, and cast visibility are deliberately separate inputs.
"""
from __future__ import annotations

import bpy

from character_node_graph import node, math_node, color_mix
from character_npr_nodes import luminance


def build_hgrp_diffuse(name: str) -> bpy.types.ShaderNodeTree:
    """Build the local diffuse equations without assuming scene or camera state."""
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    colors = ("Ambient Color", "Light Color", "Scaled Shadow Albedo", "Diffuse Color", "Ramp Color")
    vectors = ("Hemisphere Normal", "CP6 Ambient Direction")
    scalars = ("Ambient Intensity", "Light Intensity", "Shadow A", "Shadow B", "Cast Shadow", "Ramp Alpha", "CP0 Y Lit Scale", "CP0 Z Ambient Floor", "CP0 W Dark Scale", "CP1 X Dark Brightness Mix", "CP1 Y Shadow Strength", "CP7 X Bias", "CP7 Y Scale", "CP7 Z Offset", "CP12 Y Light Color Mix")
    for label in colors + vectors + scalars:
        kind = "NodeSocketColor" if label in colors else "NodeSocketVector" if label in vectors else "NodeSocketFloat"
        tree.interface.new_socket(name=label, in_out="INPUT", socket_type=kind)
    for label, kind in (("NPR Color", "NodeSocketColor"), ("Full Diffuse", "NodeSocketColor"), ("Ambient Diffuse Intensity", "NodeSocketFloat"), ("Cast Blend", "NodeSocketFloat"), ("Combined Diffuse", "NodeSocketColor")):
        tree.interface.new_socket(name=label, in_out="OUTPUT", socket_type=kind)
    source = node(tree, "NodeGroupInput", "Explicit pipeline inputs", "HGRP State Boundary")
    output = node(tree, "NodeGroupOutput", "Local diffuse outputs", "HGRP Diffuse Results")
    values = source.outputs

    def scalar(operation: str, a: bpy.types.NodeSocket | float, b: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
        return math_node(tree, operation, a, b, label, "HGRP Diffuse Arithmetic")

    def color(operation: str, factor: bpy.types.NodeSocket | float, a: bpy.types.NodeSocket | tuple[float, float, float, float], b: bpy.types.NodeSocket | tuple[float, float, float, float], label: str) -> bpy.types.NodeSocket:
        if operation == "MIX":
            difference = color_mix(tree, "SUBTRACT", 1.0, b, a, label + " delta", "HGRP Diffuse Arithmetic")
            weight = factor if isinstance(factor, bpy.types.NodeSocket) else (factor, factor, factor, 1.0)
            weighted = color_mix(tree, "MULTIPLY", 1.0, difference, weight, label + " weighted delta", "HGRP Diffuse Arithmetic")
            return color_mix(tree, "ADD", 1.0, a, weighted, label, "HGRP Diffuse Arithmetic")
        return color_mix(tree, operation, factor, a, b, label, "HGRP Diffuse Arithmetic")

    def clamp(value: bpy.types.NodeSocket, low: float, high: float, label: str) -> bpy.types.NodeSocket:
        return scalar("MINIMUM", scalar("MAXIMUM", value, low, label + " lower"), high, label + " upper")

    def interpolate(a: bpy.types.NodeSocket | float, b: bpy.types.NodeSocket | float, t: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        return scalar("ADD", a, scalar("MULTIPLY", t, scalar("SUBTRACT", b, a, label + " delta"), label + " weight"), label)

    one = (1.0, 1.0, 1.0, 1.0)
    dot = node(tree, "ShaderNodeVectorMath", "Hemisphere N dot CP6", "HGRP Hemisphere")
    dot.operation = "DOT_PRODUCT"
    tree.links.new(values["Hemisphere Normal"], dot.inputs[0])
    tree.links.new(values["CP6 Ambient Direction"], dot.inputs[1])
    hemisphere = scalar("ADD", scalar("MULTIPLY", clamp(scalar("ADD", dot.outputs["Value"], values["CP7 X Bias"], "Hemisphere bias"), 0.0, 1.0, "Hemisphere saturate"), values["CP7 Y Scale"], "Hemisphere scale"), values["CP7 Z Offset"], "Hemisphere offset")
    shadow_strength = scalar("MULTIPLY", values["Shadow B"], values["CP1 Y Shadow Strength"], "Shadow B strength")
    shadow_ambient = color("MULTIPLY", 1.0, color("MIX", shadow_strength, values["Ambient Color"], one, "Shadow ambient interpolation"), hemisphere, "Hemisphere shadow ambient")
    lit_color = color("MULTIPLY", 1.0, values["Light Color"], values["Light Intensity"], "Radiant light color")
    light_luma = luminance(tree, lit_color, "Light luminance")
    light_blend = color("MIX", values["CP12 Y Light Color Mix"], one, values["Light Color"], "CP12 light color interpolation")
    brightness = clamp(values["Ambient Intensity"], 0.0, 1.5, "Lit brightness")
    dark_brightness = interpolate(scalar("MINIMUM", interpolate(0.65, 1.0, values["Ambient Intensity"], "Dark base brightness"), 1.5, "Dark base cap"), clamp(values["Ambient Intensity"], 1.25, 1.75, "Dark alternate brightness"), values["CP1 X Dark Brightness Mix"], "Dark brightness branch")
    full_dark = color("MULTIPLY", 1.0, shadow_ambient, scalar("MULTIPLY", dark_brightness, values["CP0 W Dark Scale"], "Dark branch scale"), "Full diffuse dark branch")
    ambient_lit = color("MULTIPLY", 1.0, color("MULTIPLY", 1.0, shadow_ambient, brightness, "Lit ambient brightness"), light_blend, "Lit ambient tint")
    chromatic_light = color("MULTIPLY", 1.0, color("SUBTRACT", 1.0, lit_color, light_luma, "Light chroma"), values["Shadow B"], "Shadow B light chroma")
    full_lit = color("MULTIPLY", 1.0, color("ADD", 1.0, color("ADD", 1.0, ambient_lit, chromatic_light, "Lit ambient plus chroma"), light_luma, "Lit luminance restoration"), values["CP0 Y Lit Scale"], "Full diffuse lit branch")
    full_diffuse = color("MIX", values["Cast Shadow"], full_dark, full_lit, "Cast visibility selects full diffuse branch")
    reduced_shadow = color("MULTIPLY", 1.0, values["Scaled Shadow Albedo"], (0.65, 0.65, 0.65, 1.0), "Shadow albedo reduction")

    def boost_chroma(value: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        lum = luminance(tree, value, label + " luminance")
        return color("ADD", 1.0, color("MULTIPLY", 1.0, color("SUBTRACT", 1.0, value, lum, label + " chroma"), (1.2, 1.2, 1.2, 1.0), label + " chroma boost"), lum, label)

    weight = clamp(scalar("ADD", values["Shadow A"], values["Ramp Alpha"], "Shadow A plus ramp alpha"), 0.0, 1.0, "Combined ambient weight")
    weighted = color("MIX", weight, boost_chroma(reduced_shadow, "Reduced shadow chroma"), values["Scaled Shadow Albedo"], "Weighted ambient albedo")
    shadow_blended = color("MIX", values["Shadow B"], weighted, values["Diffuse Color"], "Shadow B albedo blend")
    split = node(tree, "ShaderNodeSeparateColor", "Ramp RGB", "HGRP Ramp Chroma")
    tree.links.new(values["Ramp Color"], split.inputs[0])
    red, green, blue = (split.outputs[key] for key in ("Red", "Green", "Blue"))
    chroma = scalar("SUBTRACT", scalar("MAXIMUM", scalar("MAXIMUM", red, green, "Ramp max RG"), blue, "Ramp max RGB"), scalar("MINIMUM", scalar("MINIMUM", red, green, "Ramp min RG"), blue, "Ramp min RGB"), "Ramp chroma")
    tinted = color("MULTIPLY", 1.0, shadow_blended, color("MIX", chroma, one, values["Ramp Color"], "Ramp chroma tint"), "Ramp tinted albedo")
    ratio = clamp(scalar("DIVIDE", luminance(tree, shadow_blended, "Untinted luminance"), scalar("MAXIMUM", luminance(tree, tinted, "Tinted luminance"), 0.001, "Tinted luminance floor"), "Ramp luminance ratio"), 0.0, 1.5, "Ramp luminance clamp")
    lit_branch = color("MULTIPLY", 1.0, tinted, ratio, "NPR lit branch")
    dark_branch = color("MIX", values["Shadow A"], values["Scaled Shadow Albedo"], boost_chroma(values["Diffuse Color"], "Diffuse chroma"), "NPR dark branch")
    npr = color("MIX", values["Cast Shadow"], dark_branch, lit_branch, "Cast visibility selects NPR branch")
    cast_blend = interpolate(values["Shadow A"], values["Shadow B"], values["Cast Shadow"], "Cast blend")
    ambient_diffuse = interpolate(values["CP0 Z Ambient Floor"], 1.0, cast_blend, "Ambient diffuse intensity")
    combined = color("MULTIPLY", 1.0, npr, full_diffuse, "NPR color times diffuse illumination")
    for label, value in (("NPR Color", npr), ("Full Diffuse", full_diffuse), ("Ambient Diffuse Intensity", ambient_diffuse), ("Cast Blend", cast_blend), ("Combined Diffuse", combined)):
        tree.links.new(value, output.inputs[label])
    tree["scope"] = "Local ComputeNPRDiffuse arithmetic only. Explicit CP and shadow inputs; no assumed runtime values, atlas, SDF, light loop, or postprocessing."
    return tree
