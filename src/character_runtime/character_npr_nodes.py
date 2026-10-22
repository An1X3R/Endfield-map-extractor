"""Source Shadow LUT/ramp sampling with explicitly approximate shared artist lighting.

Sampling and local lit-branch arithmetic follow the fixed Ruri GLSL. Unknown
HGRP global uniforms, SDF lighting, atlas shadows, and light loops stay unbound.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Mapping

import bpy

from character_node_graph import node, math_node, color_mix


@dataclass(frozen=True)
class ArtistLighting:
    direction: tuple[float, float, float]
    direct_intensity: float
    ambient_intensity: float
    ramp_bias: float
    wrap: float
    response_weight: float


def make_artist_lighting(settings: ArtistLighting, layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    values = (*settings.direction, settings.direct_intensity, settings.ambient_intensity, settings.ramp_bias, settings.wrap, settings.response_weight)
    if not all(math.isfinite(value) for value in values) or sum(value * value for value in settings.direction) == 0.0:
        raise ValueError("Artist lighting requires finite values and a nonzero direction")
    if min(settings.direct_intensity, settings.ambient_intensity) < 0.0 or not 0.0 <= settings.response_weight <= 1.0 or not 0.0 <= settings.wrap <= 1.0:
        raise ValueError("Artist lighting requires nonnegative intensities, unit-range weight and wrap")
    tree = bpy.data.node_groups.new("EF Character Artist Lighting - APPROXIMATION", "ShaderNodeTree")
    tree.interface.new_socket(name="Direction", in_out="OUTPUT", socket_type="NodeSocketVector")
    output = node(tree, "NodeGroupOutput", "Shared artist lighting outputs", "Artist Lighting")
    direction = node(tree, "ShaderNodeCombineXYZ", "World direction toward artist light", "Artist Lighting")
    for index, (label, value) in enumerate(zip(("Light Direction X", "Light Direction Y", "Light Direction Z"), settings.direction)):
        control = node(tree, "ShaderNodeValue", label, "Artist Lighting")
        control.outputs[0].default_value = value
        tree.links.new(control.outputs[0], direction.inputs[index])
    tree.links.new(direction.outputs[0], output.inputs["Direction"])
    for name, value in (("Direct Intensity", settings.direct_intensity), ("Ambient Intensity", settings.ambient_intensity), ("Ramp Bias", settings.ramp_bias), ("Wrap", settings.wrap), ("Response Weight", settings.response_weight), ("Shadow LUT Weight", 1.0)):
        tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type="NodeSocketFloat")
        control = node(tree, "ShaderNodeValue", name, "Artist Lighting")
        control.outputs[0].default_value = value
        tree.links.new(control.outputs[0], output.inputs[name])
    tree["scope"] = "Artist state, not recovered _CharacterParams values. LUT weight is a diagnostic enable control."
    layout(tree)
    return tree


def vector_node(tree: bpy.types.NodeTree, operation: str, first: bpy.types.NodeSocket, second: bpy.types.NodeSocket | None, label: str) -> bpy.types.Node:
    result = node(tree, "ShaderNodeVectorMath", label, "Artist Ramp Coordinates")
    result.operation = operation
    tree.links.new(first, result.inputs[0])
    if second is not None:
        tree.links.new(second, result.inputs[1])
    return result


def linear_to_srgb(tree: bpy.types.NodeTree, value: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    section = "Source LUT Coordinates"
    positive = math_node(tree, "MAXIMUM", value, 0.0, "Nonnegative linear channel", section)
    high = math_node(tree, "SUBTRACT", math_node(tree, "MULTIPLY", math_node(tree, "POWER", positive, 1.0 / 2.4, "Linear channel power", section), 1.055, "sRGB high branch", section), 0.055, "sRGB high offset", section)
    low = math_node(tree, "MULTIPLY", positive, 12.9232102, "Unity reference sRGB low branch", section)
    selector = math_node(tree, "GREATER_THAN", positive, 0.0031308, "sRGB piecewise threshold", section)
    chosen = color_mix(tree, "MIX", selector, low, high, "LinearToSRGB lookup input", section)
    return math_node(tree, "MINIMUM", chosen, 1.0, "Saturate sRGB LUT input", section)


def sample_image(tree: bpy.types.NodeTree, image: bpy.types.Image, x: bpy.types.NodeSocket, y: bpy.types.NodeSocket | float, label: str, section: str) -> bpy.types.Node:
    sample = node(tree, "ShaderNodeTexImage", label, section)
    sample.image = image
    sample.extension = "EXTEND"
    sample.interpolation = "Linear"
    uv = node(tree, "ShaderNodeCombineXYZ", label + " lookup UV", section)
    tree.links.new(x, uv.inputs[0])
    if isinstance(y, bpy.types.NodeSocket):
        tree.links.new(y, uv.inputs[1])
    else:
        uv.inputs[1].default_value = y
    tree.links.new(uv.outputs[0], sample.inputs["Vector"])
    sample["source_formula"] = label
    return sample


def sample_lut_lod0(tree: bpy.types.NodeTree, image: bpy.types.Image, x: bpy.types.NodeSocket, y: bpy.types.NodeSocket, label: str, section: str) -> bpy.types.NodeSocket:
    """Reproduce explicit level-zero bilinear lookup without atlas mip bleeding."""
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError(f"LUT sampling requires a decoded image: {image.name}")
    coordinates: list[tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]] = []
    for value, size, axis in ((x, width, "U"), (y, height, "V")):
        half = 0.5 / size
        clamped = math_node(tree, "MINIMUM", math_node(tree, "MAXIMUM", value, half, label + " " + axis + " edge minimum", section), 1.0 - half, label + " " + axis + " edge maximum", section)
        texel = math_node(tree, "SUBTRACT", math_node(tree, "MULTIPLY", clamped, float(size), label + " " + axis + " pixel scale", section), 0.5, label + " " + axis + " pixel coordinate", section)
        lower = math_node(tree, "FLOOR", texel, 0.0, label + " " + axis + " lower texel", section)
        fraction = math_node(tree, "SUBTRACT", texel, lower, label + " " + axis + " bilinear fraction", section)
        first = math_node(tree, "DIVIDE", math_node(tree, "ADD", lower, 0.5, label + " " + axis + " first center", section), float(size), label + " " + axis + " first UV", section)
        second = math_node(tree, "DIVIDE", math_node(tree, "ADD", lower, 1.5, label + " " + axis + " second center", section), float(size), label + " " + axis + " second UV", section)
        coordinates.append((first, second, fraction))
    u0, u1, fu = coordinates[0]
    v0, v1, fv = coordinates[1]
    rows: list[bpy.types.NodeSocket] = []
    for row, v in enumerate((v0, v1)):
        samples: list[bpy.types.NodeSocket] = []
        for column, u in enumerate((u0, u1)):
            sampled = sample_image(tree, image, u, v, f"{label} LOD0 texel {column},{row}", section)
            # Closest disables Blender's implicit mipmap sampler; interpolate linear colors explicitly.
            sampled.interpolation = "Closest"
            sampled["source_sampling"] = "Explicit LOD0 texel center; no mipmaps"
            samples.append(sampled.outputs["Color"])
        rows.append(color_mix(tree, "MIX", fu, samples[0], samples[1], label + " bilinear row " + str(row), section))
    return color_mix(tree, "MIX", fv, rows[0], rows[1], label + " LOD0 bilinear result", section)


def shadow_lut(tree: bpy.types.NodeTree, albedo: bpy.types.NodeSocket, image: bpy.types.Image) -> bpy.types.NodeSocket:
    if tuple(image.size) != (1024, 32) or image.colorspace_settings.name != "sRGB":
        raise ValueError("Source SampleShadowLut requires a 1024x32 sRGB 32-slice atlas")
    section = "Source Shadow LUT"
    channels = node(tree, "ShaderNodeSeparateColor", "Linear albedo to source LUT axes", section)
    tree.links.new(albedo, channels.inputs[0])
    red, green, blue = (linear_to_srgb(tree, channels.outputs[name]) for name in ("Red", "Green", "Blue"))
    blue31 = math_node(tree, "MULTIPLY", blue, 31.0, "Source blue * 31", section)
    blue_slice = math_node(tree, "FLOOR", blue31, 0.0, "Source blue slice floor", section)
    u = math_node(tree, "ADD", math_node(tree, "MULTIPLY", blue_slice, 0.03125, "Source blue slice width", section), math_node(tree, "ADD", math_node(tree, "MULTIPLY", red, 0.0302734375, "Source red slice coordinate", section), 0.00048828125, "Source half-texel U", section), "Source LUT U", section)
    v = math_node(tree, "ADD", math_node(tree, "MULTIPLY", green, 0.96875, "Source green coordinate", section), 0.015625, "Source half-texel V", section)
    first = sample_lut_lod0(tree, image, u, v, "SampleShadowLut slice 0", section)
    second = sample_lut_lod0(tree, image, math_node(tree, "ADD", u, 0.03125, "Next source blue slice U", section), v, "SampleShadowLut slice 1", section)
    fraction = math_node(tree, "SUBTRACT", blue31, blue_slice, "Source blue slice interpolation", section)
    return color_mix(tree, "MIX", fraction, first, second, "Source LUT two-slice interpolation", section)


def luminance(tree: bpy.types.NodeTree, color: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    split = node(tree, "ShaderNodeSeparateColor", label, "Source NPR Local Arithmetic")
    tree.links.new(color, split.inputs[0])
    products = tuple(math_node(tree, "MULTIPLY", split.outputs[channel], coefficient, "Source luminance component", "Source NPR Local Arithmetic") for channel, coefficient in zip(("Red", "Green", "Blue"), (0.2126729, 0.7151522, 0.0721750)))
    return math_node(tree, "ADD", math_node(tree, "ADD", products[0], products[1], "Source luminance RG", "Source NPR Local Arithmetic"), products[2], "Source luminance RGB", "Source NPR Local Arithmetic")


def shadow_color(tree: bpy.types.NodeTree, albedo: bpy.types.NodeSocket, floats: Mapping[str, float], samples: Mapping[str, bpy.types.Node], lighting: bpy.types.Node) -> bpy.types.NodeSocket:
    if floats["_UseShadowLutTex"] == 1.0:
        if "_ShadowLutTex" not in samples or samples["_ShadowLutTex"].image is None:
            raise ValueError("Source enables ShadowLutTex without its verified image")
        native = shadow_lut(tree, albedo, samples["_ShadowLutTex"].image)
        return color_mix(tree, "MIX", lighting.outputs["Shadow LUT Weight"], albedo, native, "Diagnostic LUT enable; source default on", "Source Shadow LUT")
    if floats["_UseShadowLutTex"] != 0.0:
        raise ValueError("Source _UseShadowLutTex must be zero or one")
    bright = color_mix(tree, "MULTIPLY", 1.0, albedo, (floats["_ShadowColorBrightness"],) * 3 + (1.0,), "Source shadow brightness", "Source Shadow Color")
    lum = luminance(tree, bright, "Source shadow luminance")
    difference = color_mix(tree, "SUBTRACT", 1.0, bright, lum, "Source shadow chroma", "Source Shadow Color")
    scaled = color_mix(tree, "MULTIPLY", 1.0, difference, (floats["_ShadowColorSaturation"],) * 3 + (1.0,), "Source shadow saturation", "Source Shadow Color")
    return color_mix(tree, "ADD", 1.0, scaled, lum, "Source ComputeShadowColor", "Source Shadow Color")


def apply_diffuse_response(tree: bpy.types.NodeTree, shader_name: str, floats: Mapping[str, float], samples: Mapping[str, bpy.types.Node], albedo: bpy.types.NodeSocket, bsdf: bpy.types.Node, lighting_tree: bpy.types.ShaderNodeTree, null_texture_defaults: Mapping[str, str]) -> bpy.types.NodeSocket:
    """Return a shared-engine shader; source sampling feeds an artist lighting approximation."""
    lighting = node(tree, "ShaderNodeGroup", "Artist global state - not game constants", "Artist Lighting")
    lighting.node_tree = lighting_tree
    geometric = node(tree, "ShaderNodeNewGeometry", "World surface normal", "Artist Ramp Coordinates")
    normal = bsdf.inputs["Normal"].links[0].from_socket if bsdf.inputs["Normal"].is_linked else geometric.outputs["Normal"]
    normal = vector_node(tree, "NORMALIZE", normal, None, "Normalize evaluated surface normal").outputs[0]
    direction = vector_node(tree, "NORMALIZE", lighting.outputs["Direction"], None, "Normalize artist light direction").outputs[0]
    ndotl = vector_node(tree, "DOT_PRODUCT", normal, direction, "Artist N dot L").outputs["Value"]
    adjusted = ndotl
    if shader_name != "HGRP/CharacterNPR_Skin":
        wrap = math_node(tree, "MULTIPLY", math_node(tree, "SUBTRACT", 1.0, math_node(tree, "MULTIPLY", ndotl, ndotl, "NdotL squared", "Artist Ramp Coordinates"), "1 minus NdotL squared", "Artist Ramp Coordinates"), 0.5, "Source geometric wrap expression", "Artist Ramp Coordinates")
        adjusted = math_node(tree, "ADD", ndotl, math_node(tree, "MULTIPLY", wrap, lighting.outputs["Wrap"], "Artist substitute for unbound camera/global wrap", "Artist Ramp Coordinates"), "Approx modified NdotL", "Artist Ramp Coordinates")
    adjusted = math_node(tree, "ADD", adjusted, lighting.outputs["Ramp Bias"], "Artist substitute for unbound global ramp bias", "Artist Ramp Coordinates")
    signed = math_node(tree, "MINIMUM", math_node(tree, "MAXIMUM", adjusted, -1.0, "Source ramp lower clamp", "Artist Ramp Coordinates"), 1.0, "Source ramp upper clamp", "Artist Ramp Coordinates")
    ramp_u = math_node(tree, "ADD", math_node(tree, "MULTIPLY", signed, 0.5, "Source ramp half scale", "Artist Ramp Coordinates"), 0.5, "Source signed ramp to U", "Artist Ramp Coordinates")
    if floats["_UseDiffRampMap"] == 1.0:
        if "_DiffRampMap" in samples and samples["_DiffRampMap"].image is not None:
            ramp = sample_image(tree, samples["_DiffRampMap"].image, ramp_u, 0.5, "Source diffuse ramp RGB and alpha", "Source Diffuse Ramp")
            ramp_color, ramp_alpha = ramp.outputs["Color"], ramp.outputs["Alpha"]
        elif "_DiffRampMap" not in samples and null_texture_defaults.get("_DiffRampMap") == "white":
            ramp = node(tree, "ShaderNodeRGB", "Source Shader default white diffuse ramp", "Source Diffuse Ramp")
            ramp.outputs[0].default_value = (1.0, 1.0, 1.0, 1.0)
            ramp_color = ramp.outputs[0]
            ramp_alpha = math_node(tree, "ADD", 1.0, 0.0, "Source white ramp alpha", "Source Diffuse Ramp")
            ramp["source_default_evidence"] = "Null material texture pointer and exact Shader property default white"
        else:
            raise ValueError("Source enables DiffRampMap without its verified image")
    elif floats["_UseDiffRampMap"] == 0.0:
        ramp = node(tree, "ShaderNodeMapRange", "Source procedural ramp branch", "Source Diffuse Ramp")
        ramp.interpolation_type = "SMOOTHSTEP"
        ramp.clamp = True
        ramp.inputs["From Min"].default_value = -0.5 if shader_name == "HGRP/CharacterNPR_Skin" else 0.25
        ramp.inputs["From Max"].default_value = 0.5 if shader_name == "HGRP/CharacterNPR_Skin" else 1.0
        tree.links.new(signed, ramp.inputs["Value"])
        ramp_color = ramp_alpha = ramp.outputs["Result"]
    else:
        raise ValueError("Source _UseDiffRampMap must be zero or one")
    shadow = shadow_color(tree, albedo, floats, samples, lighting)
    blended = color_mix(tree, "MIX", ramp_alpha, shadow, albedo, "Approx shadow-to-lit blend driven by source ramp alpha", "Approx NPR Diffuse")
    split = node(tree, "ShaderNodeSeparateColor", "Source ramp chroma", "Source NPR Local Arithmetic")
    tree.links.new(ramp_color, split.inputs[0])
    maximum = math_node(tree, "MAXIMUM", math_node(tree, "MAXIMUM", split.outputs["Red"], split.outputs["Green"], "Ramp max RG", "Source NPR Local Arithmetic"), split.outputs["Blue"], "Ramp max RGB", "Source NPR Local Arithmetic")
    minimum = math_node(tree, "MINIMUM", math_node(tree, "MINIMUM", split.outputs["Red"], split.outputs["Green"], "Ramp min RG", "Source NPR Local Arithmetic"), split.outputs["Blue"], "Ramp min RGB", "Source NPR Local Arithmetic")
    chroma = math_node(tree, "SUBTRACT", maximum, minimum, "Source ramp max minus min", "Source NPR Local Arithmetic")
    tint = color_mix(tree, "MIX", chroma, (1.0, 1.0, 1.0, 1.0), ramp_color, "Source ramp chroma tint factor", "Source NPR Local Arithmetic")
    tinted = color_mix(tree, "MULTIPLY", 1.0, blended, tint, "Source ramp-tinted diffuse", "Source NPR Local Arithmetic")
    ratio = math_node(tree, "DIVIDE", luminance(tree, blended, "Source untinted luminance"), math_node(tree, "MAXIMUM", luminance(tree, tinted, "Source tinted luminance"), 0.001, "Source normalization floor", "Source NPR Local Arithmetic"), "Source ramp luminance preservation", "Source NPR Local Arithmetic")
    ratio = math_node(tree, "MINIMUM", math_node(tree, "MAXIMUM", ratio, 0.0, "Source luminance lower clamp", "Source NPR Local Arithmetic"), 1.5, "Source luminance upper clamp", "Source NPR Local Arithmetic")
    normalized = color_mix(tree, "MULTIPLY", 1.0, tinted, ratio, "Source local ramp normalization", "Source NPR Local Arithmetic")
    direct = color_mix(tree, "MULTIPLY", 1.0, normalized, lighting.outputs["Direct Intensity"], "Artist direct diffuse intensity", "Approx NPR Diffuse")
    ambient = color_mix(tree, "MULTIPLY", 1.0, shadow, lighting.outputs["Ambient Intensity"], "Artist ambient shadow intensity", "Approx NPR Diffuse")
    result = color_mix(tree, "ADD", 1.0, direct, ambient, "Approx artist light composition", "Approx NPR Diffuse")
    if bsdf.inputs["Emission Color"].is_linked:
        emission_color = bsdf.inputs["Emission Color"].links[0].from_socket
        emission_strength = bsdf.inputs["Emission Strength"].links[0].from_socket
        emitted = color_mix(tree, "MULTIPLY", 1.0, emission_color, emission_strength, "Retain enabled source emission", "Source Emission")
        result = color_mix(tree, "ADD", 1.0, result, emitted, "NPR preview plus source emission", "Approx NPR Diffuse")
    emission = node(tree, "ShaderNodeEmission", "Artist-lit NPR diffuse - shared engines", "Approx NPR Diffuse")
    tree.links.new(result, emission.inputs["Color"])
    blend = node(tree, "ShaderNodeMixShader", "Editable native-sampling response / baseline", "Approx NPR Diffuse")
    tree.links.new(lighting.outputs["Response Weight"], blend.inputs[0])
    tree.links.new(bsdf.outputs[0], blend.inputs[1])
    tree.links.new(emission.outputs[0], blend.inputs[2])
    tree["npr_response_scope"] = "Source LUT/ramp lookup and local chroma normalization; artist light inputs and diffuse composition are approximate. No HGRP light loop/SDF/atlas reconstruction."
    return blend.outputs[0]
