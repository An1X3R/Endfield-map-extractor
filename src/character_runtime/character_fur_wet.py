"""Build the source-backed Fur wet response as an isolated Blender node kernel."""
from __future__ import annotations

from collections.abc import Callable
import re

import bpy

from character_node_graph import color_mix, input_socket, math_node, node
from character_weather_nodes import smoothstep


_SOURCE_FUR_SHADER = "HGRP/CharacterNPR _CHARACTER_FUR ForwardLit blob514"
_TREE_NAME = "EF Native Fur Wet - Compiled Equations"
_GATE_SECTION = "Fur Wet Gate"
_EQUATION_SECTION = "Fur Wet Equations"
_OUTPUT_SECTION = "Fur Wet Outputs"


def make_native_fur_wet(
    source_shader_sha256: str,
    layout: Callable[[bpy.types.NodeTree], None],
) -> bpy.types.ShaderNodeTree:
    """Create the explicit-input Fur wet kernel before the Fur snow stage."""
    if not isinstance(source_shader_sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", source_shader_sha256) is None:
        raise ValueError("Fur wet kernel requires the exact 64-character source shader SHA-256")
    if not callable(layout):
        raise TypeError("Fur wet kernel requires an explicit node layout callback")
    tree = bpy.data.node_groups.new(_TREE_NAME, "ShaderNodeTree")
    tree.use_fake_user = True
    tree["source_shader"] = _SOURCE_FUR_SHADER
    tree["source_shader_sha256"] = source_shader_sha256.casefold()
    tree["source_formula"] = "Stage87 Fur blob514 lines 292961-292982: rain/submersion/global wet gate, dark mask, shaped wet response, roughness and FurTT"
    tree["scope"] = "Source-backed local wet equations only; explicit host inputs; not native runtime state or Fur lighting reconstruction"
    tree["insertion_order"] = "Fur wet outputs feed the existing Fur snow kernel inputs; snow outputs continue to the retained host surface ports."

    input_sockets = (
        ("Rain Intensity", "NodeSocketFloat", 0.0, "Explicit decoded byte0/rain input in [0,1]."),
        ("Water Submersion", "NodeSocketFloat", 0.0, "Explicit height-gated byte1/wet input; do not multiply by the height mask again."),
        ("Global Wetness", "NodeSocketFloat", 0.0, "Explicit decoded byte2/global-wet input in [0,1]."),
        ("Disable Rain", "NodeSocketFloat", 0.0, "Explicit source _DisableRainEffectOnMaterial value."),
        ("Albedo", "NodeSocketColor", (0.5, 0.5, 0.5, 1.0), "Explicit pre-wet Fur base RGB and alpha."),
        ("Shadow Color", "NodeSocketColor", (0.25, 0.25, 0.25, 1.0), "Explicit pre-wet Fur shadow RGB and alpha."),
        ("Roughness", "NodeSocketFloat", 0.5, "Explicit pre-wet Fur roughness."),
        ("Metallic", "NodeSocketFloat", 0.0, "Explicit MetallicGlossMap.R equivalent."),
        ("Fur TT", "NodeSocketFloat", 0.0, "Explicit source _FurTTIntensity; diagnostic output only, no host consumer is claimed."),
    )
    for name, socket_type, default_value, description in input_sockets:
        input_socket(tree, name, socket_type, default_value, description)
    output_sockets = (
        ("Albedo", "NodeSocketColor", "Pre-snow wet-adjusted Fur base color."),
        ("Shadow Color", "NodeSocketColor", "Pre-snow wet-adjusted Fur shadow color."),
        ("Roughness", "NodeSocketFloat", "Pre-snow wet-adjusted Fur roughness."),
        ("Fur TT", "NodeSocketFloat", "Pre-snow wet-adjusted source FurTT; diagnostic output, not attached to a host lobe."),
        ("Enabled", "NodeSocketFloat", "Binary source wet-branch gate after saturate and material disable."),
        ("Wet Weight", "NodeSocketFloat", "Source wetWeight before the material branch gate."),
    )
    for name, socket_type, description in output_sockets:
        socket = tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type=socket_type)
        socket.description = description

    group_input = tree.nodes.new("NodeGroupInput")
    group_input.label = "Explicit weather and pre-snow channels"
    group_input["endfield_layout_section"] = "Fur Wet Inputs"
    group_output = tree.nodes.new("NodeGroupOutput")
    group_output.label = "Fur wet outputs"
    group_output["endfield_layout_section"] = _OUTPUT_SECTION

    def math_output(operation: str, left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float, label: str, section: str) -> bpy.types.NodeSocket:
        result = math_node(tree, operation, left, right, label, section)
        result.node["endfield_layout_section"] = section
        return result

    def source_smoothstep(low: float, high: float, value: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        existing_nodes = frozenset(item.as_pointer() for item in tree.nodes)
        result = smoothstep(tree, low, high, value, label)
        for item in tree.nodes:
            if item.as_pointer() not in existing_nodes:
                item["endfield_layout_section"] = _EQUATION_SECTION
        return result

    rain = group_input.outputs["Rain Intensity"]
    submersion = group_input.outputs["Water Submersion"]
    global_wetness = group_input.outputs["Global Wetness"]
    disable_rain = group_input.outputs["Disable Rain"]
    albedo = group_input.outputs["Albedo"]
    shadow_color = group_input.outputs["Shadow Color"]
    roughness = group_input.outputs["Roughness"]
    metallic = group_input.outputs["Metallic"]
    fur_tt = group_input.outputs["Fur TT"]

    adjusted_wet = math_output("MAXIMUM", global_wetness, submersion, "max(global wetness, height-gated water submersion)", _GATE_SECTION)
    gate_sum = math_output("ADD", rain, adjusted_wet, "rain + adjusted wet", _GATE_SECTION)
    nonnegative_sum = math_output("MAXIMUM", gate_sum, 0.0, "saturate lower bound", _GATE_SECTION)
    saturated_sum = math_output("MINIMUM", nonnegative_sum, 1.0, "saturate upper bound", _GATE_SECTION)
    gate_margin = math_output("SUBTRACT", saturated_sum, disable_rain, "saturated source amount - DisableRain", _GATE_SECTION)
    enabled = math_output("GREATER_THAN", gate_margin, 0.009999999776482582, "source branch enabled when margin > 0.01", _GATE_SECTION)

    nonmetal = math_output("SUBTRACT", 1.0, metallic, "1 - MetallicGlossMap.R", _EQUATION_SECTION)
    albedo_luminance = node(tree, "ShaderNodeVectorMath", "Luminance of pre-wet Albedo", _EQUATION_SECTION)
    albedo_luminance.operation = "DOT_PRODUCT"
    tree.links.new(albedo, albedo_luminance.inputs[0])
    albedo_luminance.inputs[1].default_value = (0.2126729041337967, 0.7151522040367126, 0.07217500358819962)
    albedo_luminance["endfield_layout_section"] = _EQUATION_SECTION
    nonmetal_luminance = math_output("MULTIPLY", albedo_luminance.outputs["Value"], nonmetal, "luma(Albedo * (1 - metallic))", _EQUATION_SECTION)
    dark_mask = source_smoothstep(0.35, 0.1, nonmetal_luminance, "Fur wet dark mask · reversed smoothstep 0.35→0.1")
    max_rain_submersion = math_output("MAXIMUM", rain, submersion, "max(rain, heightWeight)", _EQUATION_SECTION)
    wet_weight = math_output("MULTIPLY", max_rain_submersion, nonmetal, "wetWeight = max(rain,heightWeight) * (1-metallic)", _EQUATION_SECTION)
    roughness_shape = source_smoothstep(0.8, 0.6, roughness, "roughness ramp · reversed smoothstep 0.8→0.6")
    not_dark = math_output("SUBTRACT", 1.0, dark_mask, "1 - darkMask", _EQUATION_SECTION)
    dark_roughness = math_output("MULTIPLY", roughness_shape, dark_mask, "roughnessRamp * darkMask", _EQUATION_SECTION)
    roughness_factor = math_output("SUBTRACT", 1.0, dark_roughness, "1 - roughnessRamp * darkMask", _EQUATION_SECTION)
    wet_times_not_dark = math_output("MULTIPLY", wet_weight, not_dark, "wetWeight * (1-darkMask)", _EQUATION_SECTION)
    shaped = math_output("MULTIPLY", wet_times_not_dark, roughness_factor, "shaped weather response", _EQUATION_SECTION)
    half_factor = math_output("MULTIPLY", shaped, 0.5, "shaped response * 0.5", _EQUATION_SECTION)
    color_factor = math_output("SUBTRACT", 1.0, half_factor, "lerp(1,0.5,shaped response)", _EQUATION_SECTION)
    color_scale = node(tree, "ShaderNodeCombineColor", "RGB wet color scale", _EQUATION_SECTION)
    color_scale.mode = "RGB"
    for channel in ("Red", "Green", "Blue"):
        tree.links.new(color_factor, color_scale.inputs[channel])
    color_scale["endfield_layout_section"] = _EQUATION_SECTION
    wet_albedo = color_mix(tree, "MULTIPLY", 1.0, albedo, color_scale.outputs["Color"], "source Albedo * wet color factor", _EQUATION_SECTION)
    wet_albedo.node["endfield_layout_section"] = _EQUATION_SECTION
    wet_shadow = color_mix(tree, "MULTIPLY", 1.0, shadow_color, color_scale.outputs["Color"], "source Shadow Color * wet color factor", _EQUATION_SECTION)
    wet_shadow.node["endfield_layout_section"] = _EQUATION_SECTION

    roughness_delta_base = math_output("MULTIPLY", 0.2, dark_mask, "0.2 * darkMask", _EQUATION_SECTION)
    roughness_delta = math_output("MULTIPLY", roughness_delta_base, wet_weight, "roughness reduction amount", _EQUATION_SECTION)
    roughness_candidate = math_output("SUBTRACT", roughness, roughness_delta, "roughness - 0.2 * darkMask * wetWeight", _EQUATION_SECTION)
    roughness_floor = math_output("MINIMUM", 0.2, roughness, "min(0.2, roughness)", _EQUATION_SECTION)
    wet_roughness = math_output("MAXIMUM", roughness_candidate, roughness_floor, "max(roughness candidate, min(0.2, roughness))", _EQUATION_SECTION)

    fur_tt_amount = math_output("MAXIMUM", rain, adjusted_wet, "max(rain, adjustedByte2)", _EQUATION_SECTION)
    fur_tt_scaled = math_output("MULTIPLY", fur_tt_amount, 1.2, "1.2 * max(rain, adjustedByte2)", _EQUATION_SECTION)
    fur_tt_nonnegative = math_output("MAXIMUM", fur_tt_scaled, 0.0, "FurTT fade lower clamp", _EQUATION_SECTION)
    fur_tt_fade = math_output("MINIMUM", fur_tt_nonnegative, 1.0, "FurTT fade saturation", _EQUATION_SECTION)
    fur_tt_complement = math_output("SUBTRACT", 1.0, fur_tt_fade, "1 - FurTT fade", _EQUATION_SECTION)
    wet_fur_tt = math_output("MULTIPLY", fur_tt, fur_tt_complement, "lerp(FurTT, 0, saturated fade)", _EQUATION_SECTION)

    albedo_output = color_mix(tree, "MIX", enabled, albedo, wet_albedo, "preserve Albedo exactly when disabled", _OUTPUT_SECTION)
    shadow_output = color_mix(tree, "MIX", enabled, shadow_color, wet_shadow, "preserve Shadow Color exactly when disabled", _OUTPUT_SECTION)
    roughness_delta_output = math_output("SUBTRACT", wet_roughness, roughness, "roughness wet delta", _OUTPUT_SECTION)
    roughness_gated_delta = math_output("MULTIPLY", enabled, roughness_delta_output, "gate roughness wet delta", _OUTPUT_SECTION)
    roughness_output = math_output("ADD", roughness, roughness_gated_delta, "preserve roughness exactly when disabled", _OUTPUT_SECTION)
    fur_tt_delta = math_output("SUBTRACT", wet_fur_tt, fur_tt, "Fur TT wet delta", _OUTPUT_SECTION)
    fur_tt_gated_delta = math_output("MULTIPLY", enabled, fur_tt_delta, "gate Fur TT wet delta", _OUTPUT_SECTION)
    fur_tt_output = math_output("ADD", fur_tt, fur_tt_gated_delta, "preserve Fur TT exactly when disabled", _OUTPUT_SECTION)

    outputs: tuple[tuple[str, bpy.types.NodeSocket], ...] = (
        ("Albedo", albedo_output),
        ("Shadow Color", shadow_output),
        ("Roughness", roughness_output),
        ("Fur TT", fur_tt_output),
        ("Enabled", enabled),
        ("Wet Weight", wet_weight),
    )
    for name, socket in outputs:
        tree.links.new(socket, group_output.inputs[name])
    tree["endfield_fur_wet_format"] = "EndfieldCharacterFurWetKernel/1"
    layout(tree)
    return tree
