"""Evidence-bounded weather masks and fixed-Ruri silk surface equations.

The native preview pack is a bitcast, not a numeric uint-to-float conversion.
Ruri weather uniforms are supplied explicitly; their mapping from live per-draw
state is not established here. Snow is preserved as data, never fabricated as
white coverage. The compiled Skin snow kernel is separately evidence-bound.
The surface-to-Principled specular mapping remains approximate.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import struct
from typing import Callable, Mapping

import bpy

from character_node_graph import node, math_node, color_mix, input_socket
from character_weather_per_draw import WeatherPerDrawSource


@dataclass(frozen=True)
class WeatherUniforms:
    effect: tuple[float, float, float, float]
    water_height: float
    preview_enabled: float
    preview_water_height: float
    provenance: str


@dataclass(frozen=True)
class WeatherOverride:
    weight: float
    effect: tuple[float, float, float, float]
    water_height: float


def pack_native_preview_word(rain: float, snow: float) -> int:
    """Match native float32 multiplication, ties-to-even rounding and byte pack."""
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in (rain, snow)):
        raise ValueError("Native preview rain and snow must be finite within [0,1]")
    def byte_value(value: float) -> int:
        source = struct.unpack("<f", struct.pack("<f", value))[0]
        scaled = struct.unpack("<f", struct.pack("<f", source * 255.0))[0]
        return round(scaled)
    return byte_value(rain) | (255 << 8) | (byte_value(snow) << 24)


def unpack_native_weather_word(word: int) -> tuple[float, float, float, float]:
    if isinstance(word, bool) or not isinstance(word, int) or not 0 <= word <= 0xFFFFFFFF:
        raise ValueError("Packed native weather word must be a uint32")
    return tuple(((word >> shift) & 255) / 255.0 for shift in (0, 8, 16, 24))


def evaluate_ruri_wetness(uniforms: WeatherUniforms, override: WeatherOverride, height: float) -> tuple[float, float]:
    """Return fixed-Ruri height mask/wetness; no material-to-uniform guess."""
    validate_weather(uniforms, override)
    if not math.isfinite(height):
        raise ValueError("World height must be finite")
    water = uniforms.water_height * (1.0 - uniforms.preview_enabled) + uniforms.preview_water_height * uniforms.preview_enabled
    water = water * (1.0 - override.weight) + override.water_height * override.weight
    effects = tuple(source * (1.0 - override.weight) + user * override.weight for source, user in zip(uniforms.effect, override.effect))
    t = min(1.0, max(0.0, (water - height + 0.2) / 0.35))
    mask = t * t * (3.0 - 2.0 * t)
    return mask, max(effects[0], effects[2], mask * effects[1])


def validate_weather(uniforms: WeatherUniforms, override: WeatherOverride) -> None:
    values = (*uniforms.effect, uniforms.water_height, uniforms.preview_enabled, uniforms.preview_water_height, override.weight, *override.effect, override.water_height)
    if len(uniforms.effect) != 4 or len(override.effect) != 4 or not all(math.isfinite(value) for value in values):
        raise ValueError("Weather requires four finite effect channels and finite height/weight values")
    if not uniforms.provenance.strip():
        raise ValueError("Weather source provenance is required")
    if uniforms.preview_enabled not in (0.0, 1.0) or not 0.0 <= override.weight <= 1.0 or any(not 0.0 <= value <= 1.0 for value in (*uniforms.effect, *override.effect)):
        raise ValueError("Weather effect channels/override weight must lie in [0,1]; preview enable must be 0 or 1")


def scalar_mix(tree: bpy.types.NodeTree, weight: bpy.types.NodeSocket | float, first: bpy.types.NodeSocket | float, second: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
    inverse = math_node(tree, "SUBTRACT", 1.0, weight, label + " inverse", "Weather Equations")
    first_part = math_node(tree, "MULTIPLY", first, inverse, label + " first", "Weather Equations")
    second_part = math_node(tree, "MULTIPLY", second, weight, label + " second", "Weather Equations")
    return math_node(tree, "ADD", first_part, second_part, label, "Weather Equations")


def make_weather_controls(uniforms: WeatherUniforms, override: WeatherOverride, layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    validate_weather(uniforms, override)
    tree = bpy.data.node_groups.new("EF Weather Uniforms - Explicit Provenance", "ShaderNodeTree")
    tree.use_fake_user = True
    tree["source_provenance"] = uniforms.provenance
    tree["equation_scope"] = "Fixed Ruri water/max-wetness and separate compiled Skin rain-plus-wetness/snow outputs; live uniform mapping is unverified"
    controls: dict[str, bpy.types.NodeSocket] = {}
    entries = [("Source Effect " + axis, value) for axis, value in zip("XYZW", uniforms.effect)]
    entries += [("Source Water Height", uniforms.water_height), ("Source Preview Enable", uniforms.preview_enabled), ("Source Preview Water Height", uniforms.preview_water_height), ("User Override Weight", override.weight), ("User Water Height", override.water_height)]
    entries += [("User Effect " + axis, value) for axis, value in zip("XYZW", override.effect)]
    for name, value in entries:
        control = node(tree, "ShaderNodeValue", name, "Explicit Weather Controls")
        control.outputs[0].default_value = value
        controls[name] = control.outputs[0]
    tree.interface.new_socket(name="World Height", in_out="INPUT", socket_type="NodeSocketFloat")
    entry = node(tree, "NodeGroupInput", "Audited world height in meters", "Weather Equations")
    output = node(tree, "NodeGroupOutput", "Evidence-bounded weather outputs", "Weather Equations")
    water = scalar_mix(tree, controls["Source Preview Enable"], controls["Source Water Height"], controls["Source Preview Water Height"], "Ruri preview water selection")
    water = scalar_mix(tree, controls["User Override Weight"], water, controls["User Water Height"], "Explicit user water selection")
    delta = math_node(tree, "SUBTRACT", water, entry.outputs["World Height"], "Water height minus world height", "Weather Equations")
    mask = node(tree, "ShaderNodeMapRange", "Source smoothstep -0.2 to 0.15", "Weather Equations")
    mask.interpolation_type = "SMOOTHSTEP"
    mask.clamp = True
    mask.inputs["From Min"].default_value, mask.inputs["From Max"].default_value = -0.2, 0.15
    tree.links.new(delta, mask.inputs["Value"])
    effects = [scalar_mix(tree, controls["User Override Weight"], controls["Source Effect " + axis], controls["User Effect " + axis], "Explicit effect " + axis) for axis in "XYZW"]
    water_coverage = math_node(tree, "MULTIPLY", mask.outputs["Result"], effects[1], "Source water-mask intensity", "Weather Equations")
    wetness = math_node(tree, "MAXIMUM", effects[0], math_node(tree, "MAXIMUM", effects[2], water_coverage, "Source wetness max global water", "Weather Equations"), "Ruri EnvironmentWetness", "Weather Equations")
    global_wetness = math_node(tree, "MAXIMUM", effects[2], water_coverage, "Native wetness max global water", "Weather Equations")
    native_rain_wetness = math_node(tree, "ADD", effects[0], global_wetness, "Native rain plus wetness", "Weather Equations")
    native_rain_wetness = math_node(tree, "MINIMUM", math_node(tree, "MAXIMUM", native_rain_wetness, 0.0, "Native rain wet lower bound", "Weather Equations"), 1.0, "Native rain wet saturation", "Weather Equations")
    for name, socket in (("Wetness", wetness), ("Height Mask", mask.outputs["Result"]), ("Effect W - Unconsumed", effects[3]), ("Native Rain Plus Wetness", native_rain_wetness), ("Native Snow Intensity", effects[3]), ("Native Rain Intensity", effects[0]), ("Native Water Submersion", water_coverage), ("Native Global Wetness", effects[2]), ("Native Water Intensity", effects[1]), ("Native Water Height", water)):
        tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type="NodeSocketFloat")
        tree.links.new(socket, output.inputs[name])
    layout(tree)
    return tree


def bind_weather_per_draw_source(tree: bpy.types.ShaderNodeTree, source: WeatherPerDrawSource) -> None:
    """Bind decoded native weather bytes to the existing editable source controls."""
    values = {"Source Effect " + axis: value for axis, value in zip("XYZW", source.channels.normalized())}
    values.update({
        "Source Water Height": source.wet_effect_world_space_height,
        "Source Preview Enable": 0.0,
        "Source Preview Water Height": 0.0,
    })
    for label, value in values.items():
        matches = tuple(item for item in tree.nodes if item.label == label)
        if len(matches) != 1 or matches[0].bl_idname != "ShaderNodeValue":
            raise ValueError(f"Per-draw weather binding needs one editable Value node: group={tree.name}, label={label}, found={len(matches)}")
        matches[0].outputs[0].default_value = value
    tree["source_provenance"] = source.provenance
    tree["source_input_kind"] = "explicit_native_per_draw_word"
    tree["source_per_draw_packed_word_bits_hex"] = f"0x{source.packed_word_bits:08x}"
    tree["source_per_draw_channels_json"] = json.dumps({
        "rain": source.channels.rain,
        "wet": source.channels.wet,
        "global_wet": source.channels.global_wet,
        "snow": source.channels.snow,
    }, separators=(",", ":"))
    tree["source_per_draw_height_float32_bits_hex"] = f"0x{source.wet_effect_world_space_height_bits:08x}"
    tree["source_per_draw_runtime_capture_verified"] = False


def make_weather_controls_from_per_draw(
    source: WeatherPerDrawSource,
    override: WeatherOverride,
    layout: Callable[[bpy.types.NodeTree], None],
) -> bpy.types.ShaderNodeTree:
    """Create the existing weather graph from a raw per-draw word and explicit artist override."""
    uniforms = WeatherUniforms(
        effect=source.channels.normalized(),
        water_height=source.wet_effect_world_space_height,
        preview_enabled=0.0,
        preview_water_height=0.0,
        provenance=source.provenance,
    )
    tree = make_weather_controls(uniforms, override, layout)
    bind_weather_per_draw_source(tree, source)
    return tree


def make_silk_surface(floats: Mapping[str, float], colors: Mapping[str, tuple[float, float, float, float]], layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    """Port SilkStockingsSurface local equations with explicit texture channels."""
    scalar_names = ("_SilkStockingsAdvance", "_SilkStockingsSpecularInt", "_SilkStockingsSpecularMinAtMinWetness", "_SilkStockingsMinAffect", "_SilkStockingsMaxAffect", "_SilkStockingsAnisoDirection")
    color_names = ("_SilkStockingsColor", "_SilkStockingsDryColor", "_SilkStockingsWetColor")
    if any(name not in floats for name in scalar_names) or any(name not in colors for name in color_names):
        raise ValueError("Silk source parameters are incomplete")
    if not all(math.isfinite(floats[name]) for name in scalar_names) or not all(len(colors[name]) == 4 and all(math.isfinite(value) for value in colors[name]) for name in color_names):
        raise ValueError("Silk source scalar/color parameters must be finite")
    tree = bpy.data.node_groups.new("EF Silk Surface - Source Equations", "ShaderNodeTree")
    tree["source_formula"] = "Ruri_Endfield_Uber.glsl:2600-2621 fixed a6e85083da919d995e7efce6b7f55b8e122ec72c"
    tree["scope"] = "Albedo, roughness, coverage and specular-intensity arithmetic; HGRP lobe to Principled remains approximate; coverage does not change opaque surface alpha"
    for name, kind, value in (("Albedo", "NodeSocketColor", (1.0, 1.0, 1.0, 1.0)), ("Normal", "NodeSocketVector", (0.0, 0.0, 1.0)), ("View", "NodeSocketVector", (0.0, 0.0, 1.0)), ("Wetness", "NodeSocketFloat", 0.0), ("Base Alpha", "NodeSocketFloat", 1.0), ("Roughness", "NodeSocketFloat", 0.5), ("Mask R", "NodeSocketFloat", 0.0), ("Mask G", "NodeSocketFloat", 0.0), ("Mask B", "NodeSocketFloat", 0.0), ("Mask A", "NodeSocketFloat", 0.0)):
        socket = tree.interface.new_socket(name=name, in_out="INPUT", socket_type=kind)
        socket.default_value = value
    for name in scalar_names:
        input_socket(tree, name, "NodeSocketFloat", floats[name], "Original source value; editable override does not mutate source audit")
    for name in color_names:
        input_socket(tree, name, "NodeSocketColor", colors[name], "Original source RGB; editable override does not mutate source audit")
    input_socket(tree, "Source Silk Color Alpha", "NodeSocketFloat", colors["_SilkStockingsColor"][3], "Separate exact source alpha for coverage arithmetic")
    entry = node(tree, "NodeGroupInput", "Source parameters and verified channels", "Silk Source Equations")
    output = node(tree, "NodeGroupOutput", "Source silk outputs", "Silk Source Equations")
    advanced = math_node(tree, "GREATER_THAN", entry.outputs["_SilkStockingsAdvance"], 0.5, "Source advanced branch", "Silk Source Equations")
    base_alpha = scalar_mix(tree, entry.outputs["Wetness"], entry.outputs["Base Alpha"], entry.outputs["Mask A"], "Advanced coverage wet interpolation")
    chosen_alpha = scalar_mix(tree, advanced, entry.outputs["Base Alpha"], base_alpha, "Source coverage branch")
    coverage = math_node(tree, "SUBTRACT", math_node(tree, "ADD", chosen_alpha, 1.0, "Source coverage plus one", "Silk Source Equations"), entry.outputs["Source Silk Color Alpha"], "Source coverage", "Silk Source Equations")
    coverage = math_node(tree, "MINIMUM", math_node(tree, "MAXIMUM", coverage, 0.0, "Coverage lower bound", "Silk Source Equations"), 1.0, "Saturate silk coverage", "Silk Source Equations")
    dot = node(tree, "ShaderNodeVectorMath", "Source N dot V", "Silk Source Equations")
    dot.operation = "DOT_PRODUCT"
    tree.links.new(entry.outputs["Normal"], dot.inputs[0]); tree.links.new(entry.outputs["View"], dot.inputs[1])
    ndotv = math_node(tree, "MINIMUM", math_node(tree, "MAXIMUM", dot.outputs["Value"], 0.0, "NdotV lower bound", "Silk Source Equations"), 1.0, "Saturate NdotV", "Silk Source Equations")
    rim = math_node(tree, "POWER", math_node(tree, "SUBTRACT", 1.0499999523162842, ndotv, "Source silk rim base", "Silk Source Equations"), math_node(tree, "MULTIPLY", coverage, 2.0, "Source coverage exponent", "Silk Source Equations"), "Source silk rim power", "Silk Source Equations")
    rim = math_node(tree, "MINIMUM", rim, 1.0, "Saturate rim power", "Silk Source Equations")
    rim = scalar_mix(tree, rim, entry.outputs["_SilkStockingsMinAffect"], entry.outputs["_SilkStockingsMaxAffect"], "Source silk rim affect")
    tint = color_mix(tree, "MIX", entry.outputs["Wetness"], entry.outputs["_SilkStockingsDryColor"], entry.outputs["_SilkStockingsWetColor"], "Source dry wet tint", "Silk Source Equations")
    tinted = color_mix(tree, "MULTIPLY", 1.0, entry.outputs["Albedo"], tint, "Source albedo times wet tint", "Silk Source Equations")
    albedo = color_mix(tree, "MIX", rim, tinted, entry.outputs["_SilkStockingsColor"], "Source silk rim color", "Silk Source Equations")
    advanced_rough = scalar_mix(tree, entry.outputs["Wetness"], entry.outputs["Roughness"], math_node(tree, "SUBTRACT", 1.0, entry.outputs["Mask B"], "Source mask B roughness", "Silk Source Equations"), "Source advanced wet roughness")
    roughness = scalar_mix(tree, advanced, entry.outputs["Roughness"], advanced_rough, "Source roughness branch")
    specular = math_node(tree, "MULTIPLY", entry.outputs["_SilkStockingsSpecularInt"], scalar_mix(tree, entry.outputs["Wetness"], entry.outputs["_SilkStockingsSpecularMinAtMinWetness"], 1.0, "Source wet specular scale"), "Source wet specular intensity", "Silk Source Equations")
    specular = scalar_mix(tree, advanced, specular, math_node(tree, "MULTIPLY", specular, entry.outputs["Mask R"], "Source advanced mask R specular", "Silk Source Equations"), "Source specular branch")
    for name, kind, socket in (("Albedo", "NodeSocketColor", albedo), ("Roughness", "NodeSocketFloat", roughness), ("Specular Intensity", "NodeSocketFloat", specular), ("Coverage - Not Surface Alpha", "NodeSocketFloat", coverage)):
        tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type=kind)
        tree.links.new(socket, output.inputs[name])
    layout(tree)
    return tree


def smoothstep(tree: bpy.types.NodeTree, low: bpy.types.NodeSocket | float, high: bpy.types.NodeSocket | float, value: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
    """Use the explicit polynomial so reversed native edges remain well-defined."""
    numerator = math_node(tree, "SUBTRACT", value, low, label + " numerator", "Native Snow Equations")
    denominator = math_node(tree, "SUBTRACT", high, low, label + " denominator", "Native Snow Equations")
    t = math_node(tree, "DIVIDE", numerator, denominator, label + " t", "Native Snow Equations")
    t = math_node(tree, "MINIMUM", math_node(tree, "MAXIMUM", t, 0.0, label + " low clamp", "Native Snow Equations"), 1.0, label + " high clamp", "Native Snow Equations")
    square = math_node(tree, "MULTIPLY", t, t, label + " squared", "Native Snow Equations")
    remainder = math_node(tree, "SUBTRACT", 3.0, math_node(tree, "MULTIPLY", t, 2.0, label + " double", "Native Snow Equations"), label + " polynomial", "Native Snow Equations")
    return math_node(tree, "MULTIPLY", square, remainder, label, "Native Snow Equations")


def vector_math(tree: bpy.types.NodeTree, operation: str, first: bpy.types.NodeSocket, second: bpy.types.NodeSocket | tuple[float, float, float], label: str) -> bpy.types.Node:
    result = node(tree, "ShaderNodeVectorMath", label, "Native Snow Equations")
    result.operation = operation
    tree.links.new(first, result.inputs[0])
    if isinstance(second, bpy.types.NodeSocket):
        tree.links.new(second, result.inputs[1])
    else:
        result.inputs[1].default_value = second
    return result


def combine_vector(tree: bpy.types.NodeTree, values: tuple[bpy.types.NodeSocket | float, bpy.types.NodeSocket | float, bpy.types.NodeSocket | float], label: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeCombineXYZ", label, "Native Snow Equations")
    for index, value in enumerate(values):
        if isinstance(value, bpy.types.NodeSocket): tree.links.new(value, result.inputs[index])
        else: result.inputs[index].default_value = value
    return result.outputs[0]


def native_snow_coverage(snow: float, projection_y: float, projection_normal_y: float, sample_b: float, base_alpha: float, front_facing: float, disable: float) -> float:
    """Independent scalar form of the current compiled Skin coverage branch."""
    values = (snow, projection_y, projection_normal_y, sample_b, base_alpha, front_facing, disable)
    if not all(math.isfinite(value) for value in values): raise ValueError("Snow inputs must be finite")
    def step_value(low: float, high: float, value: float) -> float:
        t = min(1.0, max(0.0, (value-low)/(high-low)))
        return t*t*(3.0-2.0*t)
    if snow-disable <= 0.009999999776482582: return 0.0
    boosted = min(1.0, max(0.0, snow+step_value(0.3499999940395355,0.20000000298023224,projection_y)*min(1.0,max(0.0,snow*3.0))))
    nonlinear = boosted*(2.0-boosted)
    response = (step_value(-1.0,0.0,projection_normal_y)+sample_b)*0.6000000238418579
    return step_value(2.0-nonlinear,2.3499999046325684-nonlinear,response)*base_alpha*base_alpha*front_facing


def make_native_skin_snow(image: bpy.types.Image, source_shader_sha256: str, layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    """Current compiled Skin snow equations; callers must bind actual varyings.

    Projection Position is TEXCOORD8, not assumed to be world position. The
    compiled orientation flag conditionally selects x,z,-y for position and
    x,z,y for projection normal. Texture data uses non-color linear channels.
    """
    if image.packed_file is None or image.colorspace_settings.name != "Non-Color" or len(image.size) != 2 or any(value <= 0 for value in image.size):
        raise ValueError("Native snow requires a verified packed non-color global texture with positive dimensions")
    if len(source_shader_sha256) != 64 or any(value not in "0123456789abcdef" for value in source_shader_sha256.lower()):
        raise ValueError("Native snow requires the exact compiled shader SHA-256")
    tree = bpy.data.node_groups.new("EF Native Skin Snow - Compiled Equations", "ShaderNodeTree")
    tree["source_shader_sha256"] = source_shader_sha256
    tree["source_formula"] = "Current HGRP/CharacterNPR_Skin first ForwardLit Vulkan fragment; t39 _CharacterSnowEffectTex; local equations 1602-1632"
    tree["binding_scope"] = "Explicit vertex projection varyings and surface channels; no world-position substitution; shader LOD/keyword variant selection and runtime state remain caller-owned"
    inputs = (("Projection Position", "NodeSocketVector", (0.0,0.0,0.0)), ("Projection Normal", "NodeSocketVector", (0.0,1.0,0.0)), ("Geometric Normal", "NodeSocketVector", (0.0,1.0,0.0)), ("Orientation Flag", "NodeSocketFloat", 0.0), ("Global Tiling", "NodeSocketFloat", 2.25), ("Snow Intensity", "NodeSocketFloat", 0.0), ("Disable Weather", "NodeSocketFloat", 0.0), ("Base Texture Alpha", "NodeSocketFloat", 1.0), ("Front Facing", "NodeSocketFloat", 1.0), ("Roughness", "NodeSocketFloat", 0.5), ("Shadow Color", "NodeSocketColor", (0.5,0.5,0.5,1.0)), ("Albedo", "NodeSocketColor", (0.5,0.5,0.5,1.0)), ("Metallic", "NodeSocketFloat", 0.0))
    for name,kind,value in inputs:
        socket=tree.interface.new_socket(name=name,in_out="INPUT",socket_type=kind)
        socket.default_value=value
    snow_mask = tree.interface.new_socket(name='Snow Coverage Mask', in_out='INPUT', socket_type='NodeSocketFloat')
    snow_mask.default_value = 1.
    snow_mask.description = 'Body: 1. SDF Face: source MaskG multiplied by the pre-weather rim mask.'
    entry=node(tree,"NodeGroupInput","Explicit source varyings and state","Native Snow Inputs")
    output=node(tree,"NodeGroupOutput","Native snow material channels","Native Snow Outputs")
    pos=node(tree,"ShaderNodeSeparateXYZ","Source projection position","Native Snow Inputs"); tree.links.new(entry.outputs["Projection Position"],pos.inputs[0])
    normal=node(tree,"ShaderNodeSeparateXYZ","Source projection normal","Native Snow Inputs"); tree.links.new(entry.outputs["Projection Normal"],normal.inputs[0])
    orientation=entry.outputs["Orientation Flag"]
    py=scalar_mix(tree,orientation,pos.outputs["Y"],pos.outputs["Z"],"Native orientation position Y")
    pz=scalar_mix(tree,orientation,pos.outputs["Z"],math_node(tree,"MULTIPLY",pos.outputs["Y"],-1.0,"Native orientation negative Y","Native Snow Equations"),"Native orientation position Z")
    nx=normal.outputs["X"]
    ny=scalar_mix(tree,orientation,normal.outputs["Y"],normal.outputs["Z"],"Native orientation normal Y")
    nz=scalar_mix(tree,orientation,normal.outputs["Z"],normal.outputs["Y"],"Native orientation normal Z")
    weights=[]
    for axis,value in zip("XYZ",(nx,ny,nz)):
        absolute=math_node(tree,"ABSOLUTE",value,0.0,"Native projection normal abs "+axis,"Native Snow Equations")
        adjusted=math_node(tree,"SUBTRACT",absolute,0.20000000298023224,"Native projection normal minus .2 "+axis,"Native Snow Equations")
        squared=math_node(tree,"MULTIPLY",adjusted,adjusted,"Native projection weight squared "+axis,"Native Snow Equations")
        cube=math_node(tree,"MULTIPLY",squared,adjusted,"Native projection weight cube "+axis,"Native Snow Equations")
        weights.append(math_node(tree,"MAXIMUM",cube,6.103515625e-05,"Native projection weight floor "+axis,"Native Snow Equations"))
    total=math_node(tree,"ADD",math_node(tree,"ADD",weights[0],weights[1],"Native weight XY sum","Native Snow Equations"),weights[2],"Native weight total","Native Snow Equations")
    wx,wy,wz=(math_node(tree,"DIVIDE",value,total,"Native normalized weight "+axis,"Native Snow Equations") for axis,value in zip("XYZ",weights))
    scaled=tuple(math_node(tree,"MULTIPLY",value,entry.outputs["Global Tiling"],"Native projected texture axis "+axis,"Native Snow Equations") for axis,value in zip("XYZ",(pos.outputs["X"],py,pz)))
    samples=[]
    for label,first,second,weight in (("XZ",scaled[0],scaled[2],wy),("XY",scaled[0],scaled[1],wz),("ZY",scaled[2],scaled[1],wx)):
        texture=node(tree,"ShaderNodeTexImage","Native global snow "+label,"Native Snow Sampling")
        texture.image=image; texture.extension="REPEAT"; texture.interpolation="Linear"
        uv=combine_vector(tree,(first,second,0.0),"Native snow UV "+label)
        tree.links.new(uv,texture.inputs["Vector"])
        samples.append(color_mix(tree,"MULTIPLY",1.0,texture.outputs["Color"],combine_vector(tree,(weight,weight,weight),"Native snow weight "+label),"Native weighted snow "+label,"Native Snow Sampling"))
    sample=color_mix(tree,"ADD",1.0,color_mix(tree,"ADD",1.0,samples[0],samples[1],"Native snow first projections","Native Snow Sampling"),samples[2],"Native snow triplanar RGB","Native Snow Sampling")
    channels=node(tree,"ShaderNodeSeparateColor","Native snow XY normal and Z coverage","Native Snow Sampling"); tree.links.new(sample,channels.inputs[0])
    intensity=entry.outputs["Snow Intensity"]
    height=smoothstep(tree,0.3499999940395355,0.20000000298023224,py,"Native low-position snow boost")
    triple=math_node(tree,"MINIMUM",math_node(tree,"MAXIMUM",math_node(tree,"MULTIPLY",intensity,3.0,"Native snow times three","Native Snow Equations"),0.0,"Native intensity floor","Native Snow Equations"),1.0,"Native intensity boost cap","Native Snow Equations")
    boosted=math_node(tree,"ADD",intensity,math_node(tree,"MULTIPLY",height,triple,"Native position intensity boost","Native Snow Equations"),"Native boosted snow intensity","Native Snow Equations")
    boosted=math_node(tree,"MINIMUM",math_node(tree,"MAXIMUM",boosted,0.0,"Native boosted floor","Native Snow Equations"),1.0,"Native boosted cap","Native Snow Equations")
    nonlinear=math_node(tree,"MULTIPLY",boosted,math_node(tree,"SUBTRACT",2.0,boosted,"Native intensity complement","Native Snow Equations"),"Native nonlinear snow intensity","Native Snow Equations")
    input_mask=math_node(tree,"MULTIPLY",math_node(tree,"ADD",smoothstep(tree,-1.0,0.0,ny,"Native normal Y coverage"),channels.outputs["Blue"],"Native orientation plus texture Z","Native Snow Equations"),0.6000000238418579,"Native snow coverage input","Native Snow Equations")
    input_mask=math_node(tree,'MULTIPLY',input_mask,entry.outputs['Snow Coverage Mask'],'Native source-variant snow mask','Native Snow Equations')
    coverage=smoothstep(tree,math_node(tree,"SUBTRACT",2.0,nonlinear,"Native snow threshold low","Native Snow Equations"),math_node(tree,"SUBTRACT",2.3499999046325684,nonlinear,"Native snow threshold high","Native Snow Equations"),input_mask,"Native texture snow coverage")
    alpha_squared=math_node(tree,"MULTIPLY",entry.outputs["Base Texture Alpha"],entry.outputs["Base Texture Alpha"],"Native base-alpha squared coverage","Native Snow Equations")
    coverage=math_node(tree,"MULTIPLY",coverage,math_node(tree,"MULTIPLY",alpha_squared,entry.outputs["Front Facing"],"Native front-facing coverage","Native Snow Equations"),"Native alpha/front snow coverage","Native Snow Equations")
    enabled=math_node(tree,"GREATER_THAN",math_node(tree,"SUBTRACT",intensity,entry.outputs["Disable Weather"],"Native snow minus material disable","Native Snow Equations"),0.009999999776482582,"Native snow branch enabled","Native Snow Equations")
    coverage=math_node(tree,"MULTIPLY",coverage,enabled,"Native branch-gated coverage","Native Snow Equations")
    rough_weight=math_node(tree,"MINIMUM",math_node(tree,"MAXIMUM",math_node(tree,"MULTIPLY",coverage,4.0,"Native roughness coverage times four","Native Snow Equations"),0.0,"Native roughness weight floor","Native Snow Equations"),1.0,"Native roughness weight cap","Native Snow Equations")
    roughness=scalar_mix(tree,rough_weight,entry.outputs["Roughness"],0.8999999761581421,"Native snow roughness")
    shadow=color_mix(tree,"MIX",coverage,entry.outputs["Shadow Color"],(0.30799999833106995,)*3+(1.0,),"Native snow shadow color","Native Snow Equations")
    albedo=color_mix(tree,"MIX",coverage,entry.outputs["Albedo"],(0.8799999952316284,)*3+(1.0,),"Native snow albedo","Native Snow Equations")
    metallic=scalar_mix(tree,coverage,entry.outputs["Metallic"],0.0,"Native snow metallic")
    nxtex=math_node(tree,"SUBTRACT",math_node(tree,"MULTIPLY",channels.outputs["Red"],2.0,"Native snow R times two","Native Snow Equations"),1.0,"Native snow normal X","Native Snow Equations")
    nytex=math_node(tree,"SUBTRACT",math_node(tree,"MULTIPLY",channels.outputs["Green"],2.0,"Native snow G times two","Native Snow Equations"),1.0,"Native snow normal Y","Native Snow Equations")
    length=math_node(tree,"ADD",math_node(tree,"MULTIPLY",nxtex,nxtex,"Native snow X squared","Native Snow Equations"),math_node(tree,"MULTIPLY",nytex,nytex,"Native snow Y squared","Native Snow Equations"),"Native snow XY length squared","Native Snow Equations")
    length=math_node(tree,"MINIMUM",math_node(tree,"MAXIMUM",length,0.0,"Native normal length floor","Native Snow Equations"),1.0,"Native normal length cap","Native Snow Equations")
    ztex=math_node(tree,"MAXIMUM",math_node(tree,"SQRT",math_node(tree,"SUBTRACT",1.0,length,"Native snow reconstructed Z square","Native Snow Equations"),0.0,"Native snow normal Z sqrt","Native Snow Equations"),1e-16,"Native normal Z floor","Native Snow Equations")
    blended=combine_vector(tree,(math_node(tree,"MULTIPLY",math_node(tree,"MULTIPLY",nxtex,2.0,"Native amplified snow X","Native Snow Equations"),coverage,"Native snow coverage X","Native Snow Equations"),math_node(tree,"MULTIPLY",math_node(tree,"MULTIPLY",nytex,2.0,"Native amplified snow Y","Native Snow Equations"),coverage,"Native snow coverage Y","Native Snow Equations"),scalar_mix(tree,coverage,1.0,ztex,"Native snow blended normal Z")),"Native snow tangent perturbation")
    squared=vector_math(tree,"DOT_PRODUCT",blended,blended,"Native snow perturbation length squared").outputs["Value"]
    divisor=math_node(tree,"SQRT",math_node(tree,"MAXIMUM",squared,6.103515625e-05,"Native perturbation length floor","Native Snow Equations"),0.0,"Native perturbation divisor","Native Snow Equations")
    perturb=vector_math(tree,"DIVIDE",blended,combine_vector(tree,(divisor,divisor,divisor),"Native perturbation divisor vector"),"Native normalized perturbation").outputs[0]
    components=node(tree,"ShaderNodeSeparateXYZ","Native perturbation XYZ","Native Snow Equations"); tree.links.new(perturb,components.inputs[0])
    gn=entry.outputs["Geometric Normal"]
    geo=node(tree,"ShaderNodeSeparateXYZ","Source geometric normal XYZ","Native Snow Equations"); tree.links.new(gn,geo.inputs[0])
    remainder=math_node(tree,"SUBTRACT",1.0,math_node(tree,"MULTIPLY",geo.outputs["Y"],geo.outputs["Y"],"Native geometric Y squared","Native Snow Equations"),"Native frame Y remainder","Native Snow Equations")
    selector=math_node(tree,"SUBTRACT",1.0,math_node(tree,"LESS_THAN",remainder,0.009999999776482582,"Native frame pole test","Native Snow Equations"),"Native frame selector","Native Snow Equations")
    dot_axis=scalar_mix(tree,selector,geo.outputs["Z"],geo.outputs["Y"],"Native frame axis dot")
    axis=combine_vector(tree,(0.0,selector,math_node(tree,"SUBTRACT",1.0,selector,"Native frame Z selector","Native Snow Equations")),"Native chosen frame axis")
    scaled_gn=vector_math(tree,"MULTIPLY",gn,combine_vector(tree,(dot_axis,dot_axis,dot_axis),"Native frame dot vector"),"Native geometric frame projection").outputs[0]
    frame=vector_math(tree,"SUBTRACT",axis,scaled_gn,"Native orthogonal frame").outputs[0]
    frame_divisor=math_node(tree,"SQRT",math_node(tree,"MAXIMUM",math_node(tree,"SUBTRACT",1.0,math_node(tree,"MULTIPLY",dot_axis,dot_axis,"Native frame dot squared","Native Snow Equations"),"Native frame length square","Native Snow Equations"),9.999999747378752e-05,"Native frame length floor","Native Snow Equations"),0.0,"Native frame length","Native Snow Equations")
    frame=vector_math(tree,"DIVIDE",frame,combine_vector(tree,(frame_divisor,frame_divisor,frame_divisor),"Native frame divisor vector"),"Native normalized frame").outputs[0]
    cross=vector_math(tree,"CROSS_PRODUCT",frame,gn,"Native frame cross normal").outputs[0]
    terms=[]
    for vector,channel in ((cross,"X"),(frame,"Y"),(gn,"Z")):
        factor=components.outputs[channel]
        terms.append(vector_math(tree,"MULTIPLY",vector,combine_vector(tree,(factor,factor,factor),"Native frame coefficient "+channel),"Native snow frame term "+channel).outputs[0])
    final_normal=vector_math(tree,"ADD",vector_math(tree,"ADD",terms[0],terms[1],"Native snow first frame terms").outputs[0],terms[2],"Native snow surface normal").outputs[0]
    for name,kind,socket in (("Coverage","NodeSocketFloat",coverage),("Normal","NodeSocketVector",final_normal),("Roughness","NodeSocketFloat",roughness),("Shadow Color","NodeSocketColor",shadow),("Albedo","NodeSocketColor",albedo),("Metallic","NodeSocketFloat",metallic),("Triplanar Sample","NodeSocketColor",sample)):
        tree.interface.new_socket(name=name,in_out="OUTPUT",socket_type=kind); tree.links.new(socket,output.inputs[name])
    tree.interface.items_tree["Geometric Normal"].description = "Pre-weather native normal frame; Standard _NORMALMAP variants use decoded normal-map basis"
    layout(tree)
    return tree


def equation_node(tree: bpy.types.NodeTree, label: str) -> bpy.types.Node:
    matches = tuple(item for item in tree.nodes if item.label == label)
    if len(matches) != 1:
        raise ValueError(f"Shared native equation node is ambiguous: {tree.name}/{label}/{len(matches)}")
    return matches[0]


def make_native_standard_snow(image: bpy.types.Image, source_shader_sha256: str, layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    """Current Standard snow coverage/alpha-darkening with shared frame arithmetic.

    Skin and Standard coverage are materially different. Standard uses the raw
    projection normal Y, no base-alpha square, and the source snow alpha channel.
    Incoming albedo/shadow are already computed before weather; CP0.z and light
    reflection factors are not newly applied here.
    """
    tree = make_native_skin_snow(image, source_shader_sha256, layout)
    tree.name = "EF Native Standard Snow - Compiled Equations"
    tree["source_formula"] = "Current HGRP/CharacterNPR first ForwardLit Vulkan fragment; local snow equations 1602-1638"
    entry = equation_node(tree, "Explicit source varyings and state")
    ny = equation_node(tree, "Native orientation normal Y").outputs[0]
    nonlinear = equation_node(tree, "Native nonlinear snow intensity").outputs[0]
    boosted = equation_node(tree, "Native boosted snow intensity").outputs[0]
    channels = equation_node(tree, "Native snow XY normal and Z coverage")
    enabled = equation_node(tree, "Native snow branch enabled").outputs[0]
    source = math_node(tree, "ADD", math_node(tree, "ADD", math_node(tree, "MULTIPLY", ny, 0.6499999761581421, "Standard normal Y times .65", "Native Standard Snow Equations"), 0.3499999940395355, "Standard normal coverage bias", "Native Standard Snow Equations"), channels.outputs["Blue"], "Standard snow normal plus texture B", "Native Standard Snow Equations")
    coverage = smoothstep(tree, math_node(tree, "SUBTRACT", 2.0, nonlinear, "Standard coverage low threshold", "Native Standard Snow Equations"), math_node(tree, "SUBTRACT", 2.3499999046325684, nonlinear, "Standard coverage high threshold", "Native Standard Snow Equations"), source, "Standard native snow coverage")
    coverage = math_node(tree, "MULTIPLY", coverage, math_node(tree, "MULTIPLY", entry.outputs["Front Facing"], enabled, "Standard front and branch coverage", "Native Standard Snow Equations"), "Standard branch-gated coverage", "Native Standard Snow Equations")
    old_coverage = equation_node(tree, "Native branch-gated coverage").outputs[0]
    consumers = tuple(link.to_socket for link in old_coverage.links)
    for target in consumers: tree.links.new(coverage, target)
    alpha_terms = []
    for projection, weight_axis in (("XZ","Y"),("XY","Z"),("ZY","X")):
        texture = equation_node(tree, "Native global snow " + projection)
        weight = equation_node(tree, "Native normalized weight " + weight_axis).outputs[0]
        alpha_terms.append(math_node(tree, "MULTIPLY", texture.outputs["Alpha"], weight, "Standard source snow alpha " + projection, "Native Standard Snow Equations"))
    alpha = math_node(tree, "ADD", math_node(tree, "ADD", alpha_terms[0], alpha_terms[1], "Standard alpha first projections", "Native Standard Snow Equations"), alpha_terms[2], "Standard native snow alpha", "Native Standard Snow Equations")
    nonmetal = math_node(tree, "SUBTRACT", 1.0, entry.outputs["Metallic"], "Standard nonmetal source response", "Native Standard Snow Equations")
    luminance = node(tree, "ShaderNodeVectorMath", "Standard source albedo luminance", "Native Standard Snow Equations")
    luminance.operation = "DOT_PRODUCT"
    tree.links.new(entry.outputs["Albedo"], luminance.inputs[0])
    luminance.inputs[1].default_value = (0.2126729041337967,0.7151522040367126,0.07217500358819962)
    value = math_node(tree, "MULTIPLY", luminance.outputs["Value"], nonmetal, "Standard nonmetal luminance", "Native Standard Snow Equations")
    shadow_weight = math_node(tree, "SUBTRACT", 1.0, smoothstep(tree,0.3499999940395355,0.10000000149011612,value,"Standard snow luminance response"), "Standard bright surface response", "Native Standard Snow Equations")
    factor = math_node(tree, "MULTIPLY", math_node(tree, "MULTIPLY", boosted, shadow_weight, "Standard boosted darkening response", "Native Standard Snow Equations"), alpha, "Standard alpha darkening response", "Native Standard Snow Equations")
    orientation = math_node(tree, "ADD", math_node(tree, "MULTIPLY", ny, 0.25, "Standard darkening normal Y", "Native Standard Snow Equations"), 0.75, "Standard darkening orientation", "Native Standard Snow Equations")
    factor = math_node(tree, "MULTIPLY", factor, orientation, "Standard source darkening weight", "Native Standard Snow Equations")
    factor = math_node(tree, "MULTIPLY", factor, enabled, "Standard branch-gated darkening weight", "Native Standard Snow Equations")
    scale = scalar_mix(tree, factor, 1.0, 0.5, "Standard snow precoverage darkening")
    for channel, label in (("Albedo","Native snow albedo"),("Shadow Color","Native snow shadow color")):
        darkened = color_mix(tree, "MULTIPLY", 1.0, entry.outputs[channel], combine_vector(tree,(scale,scale,scale), "Standard darkening RGB "+channel), "Standard source darkened "+channel, "Native Standard Snow Equations")
        result = equation_node(tree,label)
        tree.links.new(darkened,result.inputs[1])
    output_nodes = tuple(item for item in tree.nodes if item.type == "GROUP_OUTPUT")
    if len(output_nodes) != 1:
        raise ValueError(f"Standard snow output node is ambiguous: {tree.name}/{len(output_nodes)}")
    output = output_nodes[0]
    for channel in ("Roughness", "Metallic"):
        current_links = tuple(output.inputs[channel].links)
        if len(current_links) != 1:
            raise ValueError(f"Standard snow output link is ambiguous: {tree.name}/{channel}")
        current = current_links[0].from_socket
        direct = entry.outputs[channel]
        inactive = scalar_mix(tree, enabled, direct, current, "Standard inactive branch " + channel)
        tree.links.new(inactive, output.inputs[channel])
    for channel in ("Albedo", "Shadow Color"):
        current_links = tuple(output.inputs[channel].links)
        if len(current_links) != 1:
            raise ValueError(f"Standard snow output link is ambiguous: {tree.name}/{channel}")
        current = current_links[0].from_socket
        inactive = color_mix(tree, "MIX", enabled, entry.outputs[channel], current,
                             "Standard inactive branch " + channel, "Native Standard Snow Equations")
        tree.links.new(inactive, output.inputs[channel])
    current_normal_links = tuple(output.inputs["Normal"].links)
    if len(current_normal_links) != 1:
        raise ValueError(f"Standard snow output link is ambiguous: {tree.name}/Normal")
    current_normal = node(tree, "ShaderNodeSeparateXYZ", "Standard computed snow normal XYZ", "Native Standard Snow Equations")
    direct_normal = node(tree, "ShaderNodeSeparateXYZ", "Standard direct incoming normal XYZ", "Native Standard Snow Equations")
    tree.links.new(current_normal_links[0].from_socket, current_normal.inputs[0])
    tree.links.new(entry.outputs["Geometric Normal"], direct_normal.inputs[0])
    selected_components = tuple(
        scalar_mix(tree, enabled, direct_normal.outputs[axis], current_normal.outputs[axis],
                   "Standard inactive branch Normal " + axis)
        for axis in "XYZ"
    )
    selected_normal = node(tree, "ShaderNodeCombineXYZ", "Standard snow branch-selected normal", "Native Standard Snow Equations")
    for axis, component in zip("XYZ", selected_components):
        tree.links.new(component, selected_normal.inputs[axis])
    tree.links.new(selected_normal.outputs[0], output.inputs["Normal"])
    tree["scope"] = "Verified Standard local coverage/normal/material equations; source alpha darkening included; incoming rain/wet channels and vertex/runtime bindings remain caller-owned"
    tree["inactive_branch"] = "Source lines 1605-1647: when snow intensity minus _DisableRainEffectOnMaterial <= 0.009999999776482582, return incoming normal, roughness, shadow color, albedo, and metallic without snow transforms."
    layout(tree)
    return tree


def make_native_standard_wet_film(image: bpy.types.Image, source_shader_sha256: str, layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    """Port Standard static wet-film response with explicitly zero droplet terms.

    Rain and height submersion are weather intensities, never droplet coverage.
    The procedural time-dependent droplets/streak normals are intentionally
    absent. Source rain-texture alpha drives the verified scalar film response.
    """
    tree = make_native_standard_snow(image, source_shader_sha256, layout)
    tree.name = "EF Native Standard Wet Film - Partial Compiled Equations"
    tree["source_formula"] = "Current HGRP/CharacterNPR no-FUR ForwardLit 1385-1403,1580-1588; zero droplet coverage; t44 CharacterRainEffectTex alpha"
    tree["scope"] = "Static wet-film color/roughness only; no procedural droplets/streak or their normal/specular terms; vertex projection remains caller-owned"
    for name in ("Rain Intensity", "Water Submersion", "Global Wetness"):
        socket = tree.interface.new_socket(name=name,in_out="INPUT",socket_type="NodeSocketFloat"); socket.default_value=0.0
    entry = equation_node(tree,"Explicit source varyings and state")
    alpha = equation_node(tree,"Standard native snow alpha").outputs[0]
    nonmetal = equation_node(tree,"Standard nonmetal source response").outputs[0]
    luminance = equation_node(tree,"Standard nonmetal luminance").outputs[0]
    dark = smoothstep(tree,.3499999940395355,.10000000149011612,luminance,"Wet-film material dark mask")
    smooth_dark = math_node(tree,"MULTIPLY",smoothstep(tree,.800000011920929,.6000000238418579,entry.outputs["Roughness"],"Wet-film smooth dark mask"),dark,"Wet-film dark smooth product","Native Wet Film Equations")
    geo=node(tree,"ShaderNodeSeparateXYZ","Wet-film world geometric normal","Native Wet Film Equations");tree.links.new(entry.outputs["Geometric Normal"],geo.inputs[0])
    wet=math_node(tree,"MAXIMUM",entry.outputs["Global Wetness"],entry.outputs["Water Submersion"],"Native global or water wetness","Native Wet Film Equations")
    intensity=math_node(tree,"ADD",entry.outputs["Rain Intensity"],wet,"Native wet-film rain plus wetness","Native Wet Film Equations")
    intensity=math_node(tree,"MINIMUM",math_node(tree,"MAXIMUM",intensity,0.0,"Wet-film intensity lower","Native Wet Film Equations"),1.0,"Wet-film intensity saturate","Native Wet Film Equations")
    enabled=math_node(tree,"GREATER_THAN",math_node(tree,"SUBTRACT",intensity,entry.outputs["Disable Weather"],"Wet-film source disable","Native Wet Film Equations"),.009999999776482582,"Wet-film branch gate","Native Wet Film Equations")
    rain=math_node(tree,"ADD",math_node(tree,"MULTIPLY",entry.outputs["Rain Intensity"],nonmetal,"Wet-film rain nonmetal","Native Wet Film Equations"),math_node(tree,"MULTIPLY",geo.outputs["Y"],.20000000298023224,"Wet-film normal Y bias","Native Wet Film Equations"),"Wet-film rain input","Native Wet Film Equations")
    water=math_node(tree,"MULTIPLY",entry.outputs["Water Submersion"],nonmetal,"Wet-film water nonmetal","Native Wet Film Equations")
    candidates=[]
    upper=math_node(tree,"SUBTRACT",1.100000023841858,alpha,"Wet-film alpha upper threshold","Native Wet Film Equations")
    for label,value,lower in (("Rain",rain,.800000011920929),("Water",water,.44999998807907104)):
        value=math_node(tree,"MINIMUM",math_node(tree,"MAXIMUM",value,0.0,"Wet-film "+label+" input lower","Native Wet Film Equations"),1.0,"Wet-film "+label+" input upper","Native Wet Film Equations")
        candidates.append(smoothstep(tree,math_node(tree,"SUBTRACT",lower,alpha,"Wet-film "+label+" alpha lower","Native Wet Film Equations"),upper,value,"Wet-film "+label+" response"))
    film=math_node(tree,"MULTIPLY",math_node(tree,"MAXIMUM",candidates[0],candidates[1],"Wet-film max response","Native Wet Film Equations"),enabled,"Wet-film gated response","Native Wet Film Equations")
    dark_weight=math_node(tree,"MULTIPLY",film,math_node(tree,"SUBTRACT",1.0,dark,"Wet-film bright material","Native Wet Film Equations"),"Wet-film bright response","Native Wet Film Equations")
    dark_weight=math_node(tree,"MULTIPLY",dark_weight,math_node(tree,"SUBTRACT",1.0,smooth_dark,"Wet-film smooth dark complement","Native Wet Film Equations"),"Wet-film darkening weight","Native Wet Film Equations")
    color_scale=scalar_mix(tree,dark_weight,1.0,.5,"Native wet-film color scale")
    reduced=math_node(tree,"SUBTRACT",entry.outputs["Roughness"],math_node(tree,"MULTIPLY",.20000000298023224,math_node(tree,"MULTIPLY",dark,film,"Wet-film dark roughness weight","Native Wet Film Equations"),"Wet-film roughness reduction","Native Wet Film Equations"),"Wet-film reduced roughness","Native Wet Film Equations")
    roughness=math_node(tree,"MAXIMUM",reduced,math_node(tree,"MINIMUM",.20000000298023224,entry.outputs["Roughness"],"Wet-film roughness floor","Native Wet Film Equations"),"Native wet-film roughness","Native Wet Film Equations")
    output=equation_node(tree,"Native snow material channels")
    for name in ("Albedo","Shadow Color"):
        value=color_mix(tree,"MULTIPLY",1.0,entry.outputs[name],combine_vector(tree,(color_scale,color_scale,color_scale),"Wet-film color scale RGB"+name),"Native wet-film "+name,"Native Wet Film Equations")
        tree.links.new(value,output.inputs[name])
    tree.links.new(roughness,output.inputs["Roughness"])
    tree.links.new(entry.outputs["Metallic"],output.inputs["Metallic"])
    tree.links.new(film,output.inputs["Coverage"])
    tree.links.new(entry.outputs["Geometric Normal"],output.inputs["Normal"])
    layout(tree)
    return tree
