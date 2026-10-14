"""Compiled Standard HGRP rain and wet response for Blender previews.

The scalar evaluator accepts already sampled global rain textures so it can be
used as an independent oracle. The node-group builder samples the two exact
global source images and exposes the existing Standard weather-surface inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Callable

import bpy

from character_node_graph import color_mix, node


Vector2 = tuple[float, float]
Vector3 = tuple[float, float, float]
Color4 = tuple[float, float, float, float]


@dataclass(frozen=True)
class DropLayerSample:
    coverage: float
    normal_delta: Vector2
    maximum_radius_coverage: float
    plane_coverages: Vector3


@dataclass(frozen=True)
class StandardRainInputs:
    projection_position: Vector3
    projection_normal: Vector3
    geometric_normal: Vector3
    rain_intensity: float
    water_submersion: float
    global_wetness: float
    native_time_x: float
    orientation_flag: float
    global_tiling: float
    disable_weather: float
    albedo: Vector3
    shadow_color: Vector3
    roughness: float
    metallic: float
    rain_effect_samples: tuple[Color4, Color4, Color4]
    rain_streak_samples: tuple[Color4, Color4, Color4, Color4]


@dataclass(frozen=True)
class StandardRainResult:
    coverage: float
    normal: Vector3
    roughness: float
    albedo: Vector3
    shadow_color: Vector3
    metallic: float
    first_layer: DropLayerSample
    second_layer: DropLayerSample
    second_frequency_enabled: float
    wet_normal_strength: float
    normal_delta_xy: Vector2


def _fract(value: float) -> float:
    return value - math.floor(value)


def source_drop_hashes(cell: Vector2) -> tuple[Vector2, Vector2]:
    """Return the deterministic CPU hash model with float32 rounding per scalar operation."""
    if len(cell) != 2 or not all(math.isfinite(value) for value in cell):
        raise ValueError("Drop hash cell must contain two finite coordinates")

    def f32(value: float) -> float:
        try:
            return struct.unpack("<f", struct.pack("<f", value))[0]
        except (OverflowError, struct.error) as exc:
            raise ValueError("Drop hash coordinates must fit IEEE float32") from exc

    def add(first: float, second: float) -> float:
        return f32(f32(first) + f32(second))

    def multiply(first: float, second: float) -> float:
        return f32(f32(first) * f32(second))

    def fract(value: float) -> float:
        rounded = f32(value)
        return f32(rounded - math.floor(rounded))

    def hashed(offset: float) -> Vector2:
        offset32 = f32(offset)
        x = fract(multiply(add(cell[0], offset32), 123.339996337890625))
        y = fract(multiply(add(cell[1], offset32), 456.209991455078125))
        dot_x = multiply(x, add(x, 34.345001220703125))
        dot_y = multiply(y, add(y, 34.345001220703125))
        dot_value = add(dot_x, dot_y)
        hash_x = add(x, dot_value)
        hash_y = add(y, dot_value)
        return fract(multiply(hash_x, hash_y)), fract(add(hash_x, hash_y))

    return hashed(0.0), hashed(114.51399993896484)


def build_source_drop_hash_nodes(
    tree: bpy.types.NodeTree,
    cell_x: bpy.types.NodeSocket,
    cell_y: bpy.types.NodeSocket,
    label: str,
) -> tuple[tuple[bpy.types.NodeSocket, bpy.types.NodeSocket], tuple[bpy.types.NodeSocket, bpy.types.NodeSocket]]:
    """Build both compiled cell hashes for consumers with their own drop radii."""
    if not label.strip():
        raise ValueError("Drop hash node label must be nonempty")

    def hashed(offset: float, suffix: str) -> Vector2:
        qx = _math(tree, "FRACT", _math(tree, "MULTIPLY", _math(tree, "ADD", cell_x, offset, label + suffix + " cell x"), 123.339996337890625, label + suffix + " x scale"), 0.0, label + suffix + " x fract")
        qy = _math(tree, "FRACT", _math(tree, "MULTIPLY", _math(tree, "ADD", cell_y, offset, label + suffix + " cell y"), 456.209991455078125, label + suffix + " y scale"), 0.0, label + suffix + " y fract")
        dot_x = _math(tree, "MULTIPLY", qx, _math(tree, "ADD", qx, 34.345001220703125, label + suffix + " dot x term"), label + suffix + " dot x")
        dot_y = _math(tree, "MULTIPLY", qy, _math(tree, "ADD", qy, 34.345001220703125, label + suffix + " dot y term"), label + suffix + " dot y")
        dot_value = _math(tree, "ADD", dot_x, dot_y, label + suffix + " dot")
        hx = _math(tree, "ADD", qx, dot_value, label + suffix + " hashed x")
        hy = _math(tree, "ADD", qy, dot_value, label + suffix + " hashed y")
        return (
            _math(tree, "FRACT", _math(tree, "MULTIPLY", hx, hy, label + suffix + " random product"), 0.0, label + suffix + " random x"),
            _math(tree, "FRACT", _math(tree, "ADD", hx, hy, label + suffix + " random sum"), 0.0, label + suffix + " random y"),
        )

    return hashed(0.0, " primary"), hashed(114.51399993896484, " secondary")


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if edge0 == edge1:
        raise ValueError("Smoothstep edges must differ")
    t = min(1.0, max(0.0, (value - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def _weights(normal: Vector3) -> Vector3:
    raw = tuple(max(max(abs(value) - 0.2, 0.0) ** 10, 0.0) for value in normal)
    total = max(sum(raw), 6.103515625e-05)
    return raw[0] / total, raw[1] / total, raw[2] / total


def _texture_weights(normal: Vector3) -> Vector3:
    raw = tuple(max((abs(value) - 0.2) ** 3, 6.103515625e-05) for value in normal)
    total = sum(raw)
    return raw[0] / total, raw[1] / total, raw[2] / total


def _drop_plane(
    coordinate: Vector2,
    tri_weight: float,
    frequency: float,
    phase_base: float,
    time_multiplier: float,
    animated: bool,
    wet_gate: float,
) -> tuple[float, Vector2, float]:
    scaled = coordinate[0] * frequency, coordinate[1] * frequency
    cell = math.floor(scaled[0]), math.floor(scaled[1])
    cell_hash, jitter_hash = source_drop_hashes((float(cell[0]), float(cell[1])))
    radius = 0.25 * (0.6 + 0.4 * cell_hash[0])
    offset_x = (scaled[0] - cell[0]) + (((jitter_hash[0] * 2.0) - 1.0) * 0.25) - 0.5
    offset_y = (scaled[1] - cell[1]) + (((jitter_hash[1] * 2.0) - 1.0) * 0.25) - 0.5
    ellipse_x = offset_x * 1.25
    ellipse_y = offset_y * (1.25 if offset_y < 0.0 else 0.75)
    radius_coverage = _smoothstep(radius, 0.0, math.hypot(ellipse_x, ellipse_y))
    inside = 1.0 if radius_coverage >= 0.001 else 0.0
    animated_phase = _fract((phase_base * time_multiplier) + cell_hash[0])
    static_phase_value = min(1.0, max(0.0, (phase_base * time_multiplier) + cell_hash[0]))
    static_phase = 0.22 + ((0.85 - 0.22) * static_phase_value)
    phase = animated_phase if animated else static_phase
    lifetime = _smoothstep(0.2, 0.22, phase) * _smoothstep(0.85, 0.55, phase)
    wet_cell = 1.0 if (jitter_hash[1] - 0.1) >= (1.0 - wet_gate) else 0.0
    cell_coverage = lifetime * inside * wet_cell
    weighted_coverage = cell_coverage * tri_weight
    gradient_scale = 0.25 + (0.5 - 0.25) * jitter_hash[0]
    gradient_x = min(1.0, max(-1.0, ellipse_x / radius)) * gradient_scale * weighted_coverage
    gradient_y = min(1.0, max(-1.0, ellipse_y / radius)) * gradient_scale * weighted_coverage
    return weighted_coverage, (gradient_x, gradient_y), radius * weighted_coverage


def evaluate_hashed_drop_layer(
    position: Vector3,
    normal: Vector3,
    frequency: float,
    phase_base: float,
    time_multiplier: float,
    animated: bool,
    wet_gate: float,
) -> DropLayerSample:
    """Evaluate the Standard shader's three-plane hash drop layer."""
    values = (*position, *normal, frequency, phase_base, time_multiplier, wet_gate)
    if len(position) != 3 or len(normal) != 3 or not all(math.isfinite(value) for value in values):
        raise ValueError("Drop layer requires finite 3D position/normal and finite controls")
    if frequency <= 0.0 or not 0.0 <= wet_gate <= 1.0:
        raise ValueError("Drop frequency must be positive and wet gate must lie in [0,1]")
    weights = _weights(normal)
    planes = (
        _drop_plane((position[0], position[2]), weights[1], frequency, phase_base, time_multiplier, animated, wet_gate),
        _drop_plane((position[0], position[1]), weights[2], frequency, phase_base, time_multiplier, animated, wet_gate),
        _drop_plane((position[2], position[1]), weights[0], frequency, phase_base, time_multiplier, animated, wet_gate),
    )
    coverage = max(item[0] for item in planes)
    normal_delta = (sum(item[1][0] for item in planes), sum(item[1][1] for item in planes))
    maximum_radius_coverage = max(item[2] for item in planes)
    return DropLayerSample(coverage, normal_delta, maximum_radius_coverage, tuple(item[0] for item in planes))


def _validate_inputs(inputs: StandardRainInputs) -> None:
    vectors = (inputs.projection_position, inputs.projection_normal, inputs.geometric_normal, inputs.albedo, inputs.shadow_color)
    colors = (*inputs.rain_effect_samples, *inputs.rain_streak_samples)
    scalars = (inputs.rain_intensity, inputs.water_submersion, inputs.global_wetness, inputs.native_time_x, inputs.orientation_flag, inputs.global_tiling, inputs.disable_weather, inputs.roughness, inputs.metallic)
    if any(len(vector) != 3 or not all(math.isfinite(value) for value in vector) for vector in vectors):
        raise ValueError("Standard rain vectors must be finite RGB/XYZ triples")
    if len(inputs.rain_effect_samples) != 3 or len(inputs.rain_streak_samples) != 4 or any(len(color) != 4 or not all(math.isfinite(value) for value in color) for color in colors):
        raise ValueError("Standard rain requires three effect RGBA samples and four streak RGBA samples")
    if not all(math.isfinite(value) for value in scalars):
        raise ValueError("Standard rain scalar controls must be finite")
    if inputs.orientation_flag not in (0.0, 1.0) or inputs.global_tiling <= 0.0 or inputs.disable_weather not in (0.0, 1.0):
        raise ValueError("Standard rain orientation/disable flags must be 0 or 1 and tiling must be positive")
    if any(not 0.0 <= value <= 1.0 for value in (inputs.rain_intensity, inputs.water_submersion, inputs.global_wetness, inputs.metallic)):
        raise ValueError("Standard rain/wet/metallic inputs must lie in [0,1]")


def evaluate_standard_rain(inputs: StandardRainInputs) -> StandardRainResult:
    """Evaluate drop, streak-normal, final coverage, and material response."""
    _validate_inputs(inputs)
    if inputs.orientation_flag == 1.0:
        position = (inputs.projection_position[0], inputs.projection_position[2], -inputs.projection_position[1])
        projection_normal = (inputs.projection_normal[0], inputs.projection_normal[2], inputs.projection_normal[1])
    else:
        position = inputs.projection_position
        projection_normal = inputs.projection_normal
    position = tuple(value * inputs.global_tiling for value in position)
    weights = _weights(projection_normal)
    wet = max(inputs.global_wetness, inputs.water_submersion)
    animated = not (inputs.rain_intensity <= 0.01 and wet >= 0.01)
    phase_base_first = 3.0 if animated else 1.0 - wet
    phase_base_second = 4.345600128173828 if animated else 1.0 - wet
    time_multiplier = inputs.native_time_x if animated else 1.0
    luminance = sum(channel * weight for channel, weight in zip(inputs.albedo, (0.2126729041337967, 0.7151522040367126, 0.07217500358819962), strict=True))
    nonmetal_luminance = luminance * (1.0 - inputs.metallic)
    dark_mask = _smoothstep(0.3499999940395355, 0.10000000149011612, nonmetal_luminance)
    metallic_mask = _smoothstep(0.5, 0.75, inputs.metallic)
    smooth_dark = _smoothstep(0.800000011920929, 0.6000000238418579, inputs.roughness) * dark_mask
    wet_normal_strength = min(1.0, max(0.0, smooth_dark + metallic_mask)) * max(inputs.rain_intensity, wet)
    first_layer = evaluate_hashed_drop_layer(position, projection_normal, 20.0, phase_base_first, time_multiplier, animated, wet_normal_strength)
    second_layer = evaluate_hashed_drop_layer(position, projection_normal, 34.34560012817383, phase_base_second, time_multiplier, animated, wet_normal_strength)
    second_enabled = 1.0 if first_layer.maximum_radius_coverage <= 0.01 else 0.0
    texture_weights = _texture_weights(projection_normal)
    effect_weights = (texture_weights[1], texture_weights[2], texture_weights[0])
    effect_sample = tuple(sum(inputs.rain_effect_samples[index][channel] * effect_weights[index] for index in range(3)) for channel in range(4))
    projected_normal = (projection_normal[0], 0.0, projection_normal[2])
    normal_raw = tuple(max((max(abs(value) - 0.2, 0.0) ** 3), 6.103515625e-05) for value in projected_normal)
    normal_total = sum(normal_raw)
    streak_weight_x = normal_raw[0] / normal_total
    streak_weight_z = normal_raw[2] / normal_total
    streak_base = tuple(inputs.rain_streak_samples[0][channel] * streak_weight_z + inputs.rain_streak_samples[1][channel] * streak_weight_x for channel in range(4))
    streak_shift_alpha = inputs.rain_streak_samples[2][3] * streak_weight_z + inputs.rain_streak_samples[3][3] * streak_weight_x
    normal_delta = (
        ((effect_sample[0] * 2.0 - 1.0) + first_layer.normal_delta[0] + second_layer.normal_delta[0] * second_enabled)
        + ((streak_base[0] * 2.0 - 1.0) * streak_shift_alpha * wet_normal_strength),
        ((effect_sample[1] * 2.0 - 1.0) + first_layer.normal_delta[1] + second_layer.normal_delta[1] * second_enabled)
        + ((streak_base[1] * 2.0 - 1.0) * streak_shift_alpha * wet_normal_strength),
    )
    first_coverage = first_layer.coverage if first_layer.coverage >= 0.01 else 0.0
    second_coverage = second_layer.coverage * second_enabled
    rain_texture_coverage = 1.0 if effect_sample[2] >= 1.01 - wet_normal_strength else 0.0
    streak_coverage = _smoothstep(1.0 - streak_base[2], 1.100000023841858 - streak_base[2], wet_normal_strength) * wet_normal_strength
    coverage = max(first_coverage, second_coverage, rain_texture_coverage, streak_coverage)
    coverage *= 1.0 if min(1.0, max(0.0, inputs.rain_intensity + wet)) - inputs.disable_weather > 0.009999999776482582 else 0.0
    base_normal = _normalize(inputs.geometric_normal)
    tangent_normal = (normal_delta[0], normal_delta[1], max(1.0000000168623835e-16, math.sqrt(1.0 - min(1.0, max(0.0, normal_delta[0] ** 2 + normal_delta[1] ** 2)))))
    tangent_normal = _normalize(tangent_normal)
    cross = _cross(base_normal, (0.0, 1.0, 0.0))
    tangent = _normalize(cross) if _dot(cross, cross) > 6.103515625e-05 else (1.0, 0.0, 0.0)
    bitangent = _cross(tangent, base_normal)
    mapped = _normalize(_add(_add(_scale(tangent, tangent_normal[0]), _scale(bitangent, tangent_normal[1])), _scale(base_normal, tangent_normal[2])))
    blended_normal = _normalize(_add(_scale(base_normal, 1.0 - coverage), _scale(mapped, coverage)))
    reduced = inputs.roughness + (min(inputs.roughness, 0.05) - inputs.roughness) * coverage
    result_roughness = max(reduced - (0.2 * dark_mask * _rain_alpha_response(inputs, effect_sample)), min(0.2, reduced))
    color_response = _rain_alpha_response(inputs, effect_sample)
    darken = 1.0 + (0.5 - 1.0) * (color_response * (1.0 - dark_mask) * (1.0 - smooth_dark))
    albedo_luma = sum(channel * weight for channel, weight in zip(inputs.albedo, (0.2126729041337967, 0.7151522040367126, 0.07217500358819962), strict=True))
    albedo_luma_gate = _smoothstep(0.7, 0.3, albedo_luma)
    bright_factor = (albedo_luma_gate * 0.5) + 1.0
    lit_albedo = tuple(channel * bright_factor for channel in inputs.albedo)
    albedo_coverage = coverage * metallic_mask
    output_albedo = tuple((inputs.albedo[index] * (1.0 - albedo_coverage) + lit_albedo[index] * albedo_coverage) * darken for index in range(3))
    output_shadow = tuple(channel * darken for channel in inputs.shadow_color)
    branch_enabled = min(1.0, max(0.0, inputs.rain_intensity + wet)) - inputs.disable_weather > 0.009999999776482582
    if not branch_enabled:
        blended_normal = inputs.geometric_normal
        result_roughness = inputs.roughness
        output_albedo = inputs.albedo
        output_shadow = inputs.shadow_color
        coverage = 0.0
    return StandardRainResult(coverage, blended_normal, result_roughness, output_albedo, output_shadow, inputs.metallic, first_layer, second_layer, second_enabled, wet_normal_strength, normal_delta)


def _rain_alpha_response(inputs: StandardRainInputs, rain_effect: Color4) -> float:
    nonmetal = tuple(channel * (1.0 - inputs.metallic) for channel in inputs.albedo)
    luminance = sum(channel * weight for channel, weight in zip(nonmetal, (0.2126729041337967, 0.7151522040367126, 0.07217500358819962), strict=True))
    dark = _smoothstep(0.3499999940395355, 0.10000000149011612, luminance)
    alpha = rain_effect[3]
    upper = 1.100000023841858 - alpha
    rain_input = min(1.0, max(0.0, (inputs.rain_intensity * (1.0 - inputs.metallic)) + (inputs.geometric_normal[1] * 0.2)))
    water_input = min(1.0, max(0.0, inputs.water_submersion * (1.0 - inputs.metallic)))
    return max(_smoothstep(0.8 - alpha, upper, rain_input), _smoothstep(0.44999998807907104 - alpha, upper, water_input))


def _dot(first: Vector3, second: Vector3) -> float:
    return sum(left * right for left, right in zip(first, second, strict=True))


def _cross(first: Vector3, second: Vector3) -> Vector3:
    return first[1] * second[2] - first[2] * second[1], first[2] * second[0] - first[0] * second[2], first[0] * second[1] - first[1] * second[0]


def _normalize(value: Vector3) -> Vector3:
    length = math.sqrt(max(_dot(value, value), 1.1754943508222875e-38))
    return value[0] / length, value[1] / length, value[2] / length


def _scale(vector: Vector3, factor: float) -> Vector3:
    return vector[0] * factor, vector[1] * factor, vector[2] * factor


def _add(first: Vector3, second: Vector3) -> Vector3:
    return first[0] + second[0], first[1] + second[1], first[2] + second[2]


def _math(tree: bpy.types.NodeTree, operation: str, left: bpy.types.NodeSocket | float, right: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeMath", label, "Native Rain Drop Equations")
    result.operation = operation
    for index, value in enumerate((left, right)):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, result.inputs[index])
        else:
            result.inputs[index].default_value = value
    return result.outputs[0]


def _vector(tree: bpy.types.NodeTree, operation: str, first: bpy.types.NodeSocket, second: bpy.types.NodeSocket | tuple[float, float, float], label: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeVectorMath", label, "Native Rain Drop Equations")
    result.operation = operation
    tree.links.new(first, result.inputs[0])
    if isinstance(second, bpy.types.NodeSocket):
        tree.links.new(second, result.inputs[1])
    else:
        result.inputs[1].default_value = second
    return result.outputs[0]


def _combine(tree: bpy.types.NodeTree, values: tuple[bpy.types.NodeSocket | float, bpy.types.NodeSocket | float, bpy.types.NodeSocket | float], label: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeCombineXYZ", label, "Native Rain Drop Equations")
    for index, value in enumerate(values):
        if isinstance(value, bpy.types.NodeSocket):
            tree.links.new(value, result.inputs[index])
        else:
            result.inputs[index].default_value = value
    return result.outputs[0]


def _separate(tree: bpy.types.NodeTree, vector: bpy.types.NodeSocket, label: str) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    result = node(tree, "ShaderNodeSeparateXYZ", label, "Native Rain Drop Equations")
    tree.links.new(vector, result.inputs[0])
    return result.outputs["X"], result.outputs["Y"], result.outputs["Z"]


def _smoothstep_node(tree: bpy.types.NodeTree, low: bpy.types.NodeSocket | float, high: bpy.types.NodeSocket | float, value: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
    numerator = _math(tree, "SUBTRACT", value, low, label + " numerator")
    denominator = _math(tree, "SUBTRACT", high, low, label + " denominator")
    ratio = _math(tree, "DIVIDE", numerator, denominator, label + " ratio")
    bounded = _math(tree, "MINIMUM", _math(tree, "MAXIMUM", ratio, 0.0, label + " lower clamp"), 1.0, label + " upper clamp")
    square = _math(tree, "MULTIPLY", bounded, bounded, label + " square")
    polynomial = _math(tree, "SUBTRACT", 3.0, _math(tree, "MULTIPLY", bounded, 2.0, label + " double"), label + " polynomial")
    return _math(tree, "MULTIPLY", square, polynomial, label)


def _mix(tree: bpy.types.NodeTree, factor: bpy.types.NodeSocket | float, first: bpy.types.NodeSocket | float, second: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
    inverse = _math(tree, "SUBTRACT", 1.0, factor, label + " inverse")
    return _math(tree, "ADD", _math(tree, "MULTIPLY", first, inverse, label + " first"), _math(tree, "MULTIPLY", second, factor, label + " second"), label)


def _drop_plane_nodes(
    tree: bpy.types.NodeTree,
    coordinate: tuple[bpy.types.NodeSocket, bpy.types.NodeSocket],
    tri_weight: bpy.types.NodeSocket,
    frequency: float,
    phase_base: bpy.types.NodeSocket,
    time_multiplier: bpy.types.NodeSocket,
    animated: bpy.types.NodeSocket,
    wet_gate: bpy.types.NodeSocket,
    label: str,
) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    u = _math(tree, "MULTIPLY", coordinate[0], frequency, label + " frequency u")
    v = _math(tree, "MULTIPLY", coordinate[1], frequency, label + " frequency v")
    cell_x = _math(tree, "FLOOR", u, 0.0, label + " cell x")
    cell_y = _math(tree, "FLOOR", v, 0.0, label + " cell y")
    fraction_x = _math(tree, "FRACT", u, 0.0, label + " fraction x")
    fraction_y = _math(tree, "FRACT", v, 0.0, label + " fraction y")
    primary, secondary = build_source_drop_hash_nodes(tree, cell_x, cell_y, label)
    radius = _math(tree, "MULTIPLY", _math(tree, "ADD", 0.6, _math(tree, "MULTIPLY", 0.4, primary[0], label + " radius variation"), label + " radius scale"), 0.25, label + " radius")
    offset_x = _math(tree, "SUBTRACT", _math(tree, "ADD", fraction_x, _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", secondary[0], 2.0, label + " jitter x double"), 1.0, label + " jitter x center"), 0.25, label + " jitter x scale"), label + " jitter x"), 0.5, label + " local x")
    offset_y = _math(tree, "SUBTRACT", _math(tree, "ADD", fraction_y, _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", secondary[1], 2.0, label + " jitter y double"), 1.0, label + " jitter y center"), 0.25, label + " jitter y scale"), label + " jitter y"), 0.5, label + " local y")
    ellipse_x = _math(tree, "MULTIPLY", offset_x, 1.25, label + " ellipse x")
    y_sign = _math(tree, "LESS_THAN", offset_y, 0.0, label + " ellipse y sign")
    y_scale = _mix(tree, y_sign, 0.75, 1.25, label + " ellipse y scale")
    ellipse_y = _math(tree, "MULTIPLY", offset_y, y_scale, label + " ellipse y")
    length = _math(tree, "SQRT", _math(tree, "ADD", _math(tree, "MULTIPLY", ellipse_x, ellipse_x, label + " ellipse x square"), _math(tree, "MULTIPLY", ellipse_y, ellipse_y, label + " ellipse y square"), label + " ellipse length square"), 0.0, label + " ellipse length")
    radial = _smoothstep_node(tree, radius, 0.0, length, label + " radial edge")
    inside = _math(tree, "SUBTRACT", 1.0, _math(tree, "LESS_THAN", radial, 0.001, label + " outside radius"), label + " inside radius")
    raw_phase = _math(tree, "ADD", _math(tree, "MULTIPLY", phase_base, time_multiplier, label + " time phase"), primary[0], label + " hashed phase")
    phase_animated = _math(tree, "FRACT", raw_phase, 0.0, label + " animated phase")
    phase_static = _mix(tree, _math(tree, "MINIMUM", _math(tree, "MAXIMUM", raw_phase, 0.0, label + " static phase lower"), 1.0, label + " static phase upper"), 0.22, 0.85, label + " static phase")
    phase = _mix(tree, animated, phase_static, phase_animated, label + " phase mode")
    lifetime = _math(tree, "MULTIPLY", _smoothstep_node(tree, 0.2, 0.22, phase, label + " lifetime rise"), _smoothstep_node(tree, 0.85, 0.55, phase, label + " lifetime fall"), label + " lifetime")
    wet_hash = _math(tree, "SUBTRACT", secondary[1], 0.1, label + " wet hash")
    wet_threshold = _math(tree, "SUBTRACT", 1.0, wet_gate, label + " wet threshold")
    wet_cell = _math(tree, "SUBTRACT", 1.0, _math(tree, "LESS_THAN", wet_hash, wet_threshold, label + " wet comparison inverse"), label + " wet gate")
    coverage = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", lifetime, inside, label + " radial coverage"), wet_cell, label + " cell coverage")
    weighted = _math(tree, "MULTIPLY", coverage, tri_weight, label + " triplanar coverage")
    gradient_scale = _math(tree, "ADD", 0.25, _math(tree, "MULTIPLY", 0.25, secondary[0], label + " gradient scale delta"), label + " gradient scale")
    gradient_x = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", _math(tree, "MINIMUM", _math(tree, "MAXIMUM", _math(tree, "DIVIDE", ellipse_x, radius, label + " normalized gradient x"), -1.0, label + " gradient x lower"), 1.0, label + " gradient x upper"), gradient_scale, label + " gradient x scale"), weighted, label + " gradient x")
    gradient_y = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", _math(tree, "MINIMUM", _math(tree, "MAXIMUM", _math(tree, "DIVIDE", ellipse_y, radius, label + " normalized gradient y"), -1.0, label + " gradient y lower"), 1.0, label + " gradient y upper"), gradient_scale, label + " gradient y scale"), weighted, label + " gradient y")
    radius_coverage = _math(tree, "MULTIPLY", radius, weighted, label + " radius coverage")
    return weighted, gradient_x, gradient_y, radius_coverage


def make_native_standard_rain_droplets(
    rain_effect_image: bpy.types.Image,
    rain_streak_image: bpy.types.Image,
    source_shader_sha256: str,
    layout: Callable[[bpy.types.NodeTree], None],
) -> bpy.types.ShaderNodeTree:
    """Build Standard 1385-1588 rain, hash-drop, streak-normal, and material response."""
    images = (rain_effect_image, rain_streak_image)
    if any(image.packed_file is None or image.colorspace_settings.name != "Non-Color" or len(image.size) != 2 or any(value <= 0 for value in image.size) for image in images):
        raise ValueError("Standard rain requires packed non-color effect and streak textures with positive dimensions")
    if len(source_shader_sha256) != 64 or any(value not in "0123456789abcdef" for value in source_shader_sha256.lower()):
        raise ValueError("Standard rain requires the exact compiled source shader SHA-256")
    if rain_effect_image == rain_streak_image:
        raise ValueError("Rain effect and rain streak source textures must be distinct images")
    tree = bpy.data.node_groups.new("EF Native Standard Rain Droplets - Compiled", "ShaderNodeTree")
    tree.use_fake_user = True
    tree["source_shader_sha256"] = source_shader_sha256.lower()
    tree["source_formula"] = "HGRP/CharacterNPR current ForwardLit fragment lines 1385-1588; t44 CharacterRainEffectTex and t41 CharacterRainStreakTex"
    tree["scope"] = "Exact Standard hash drop cells, two-frequency gate, rain/streak triplanar samples, final coverage, tangent normal, roughness and albedo response; runtime varyings/time mapping remain caller-owned"
    input_specs: tuple[tuple[str, str, float | tuple[float, ...]], ...] = (
        ("Projection Position", "NodeSocketVector", (0.0, 0.0, 0.0)),
        ("Projection Normal", "NodeSocketVector", (0.0, 1.0, 0.0)),
        ("Geometric Normal", "NodeSocketVector", (0.0, 1.0, 0.0)),
        ("Orientation Flag", "NodeSocketFloat", 0.0),
        ("Global Tiling", "NodeSocketFloat", 1.0),
        ("Rain Intensity", "NodeSocketFloat", 0.0),
        ("Water Submersion", "NodeSocketFloat", 0.0),
        ("Global Wetness", "NodeSocketFloat", 0.0),
        ("Native Time X", "NodeSocketFloat", 0.0),
        ("Disable Weather", "NodeSocketFloat", 0.0),
        ("Base Texture Alpha", "NodeSocketFloat", 1.0),
        ("Front Facing", "NodeSocketFloat", 1.0),
        ("Roughness", "NodeSocketFloat", 0.5),
        ("Shadow Color", "NodeSocketColor", (0.5, 0.5, 0.5, 1.0)),
        ("Albedo", "NodeSocketColor", (0.5, 0.5, 0.5, 1.0)),
        ("Metallic", "NodeSocketFloat", 0.0),
    )
    input_sockets: dict[str, bpy.types.NodeTreeInterfaceSocket] = {}
    for name, socket_type, default in input_specs:
        socket = tree.interface.new_socket(name=name, in_out="INPUT", socket_type=socket_type)
        socket.default_value = default
        input_sockets[name] = socket
    output_specs = ("Coverage", "NodeSocketFloat", 0.0), ("Normal", "NodeSocketVector", (0.0, 1.0, 0.0)), ("Roughness", "NodeSocketFloat", 0.5), ("Shadow Color", "NodeSocketColor", (0.5, 0.5, 0.5, 1.0)), ("Albedo", "NodeSocketColor", (0.5, 0.5, 0.5, 1.0)), ("Metallic", "NodeSocketFloat", 0.0), ("Drop Normal XY", "NodeSocketVector", (0.0, 0.0, 0.0)), ("Tangent Normal XY", "NodeSocketVector", (0.0, 0.0, 0.0)), ("Wet Normal Strength", "NodeSocketFloat", 0.0), ("Second Frequency Enabled", "NodeSocketFloat", 1.0)
    for name, socket_type, default in output_specs:
        socket = tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type=socket_type)
        socket.default_value = default
    entry = node(tree, "NodeGroupInput", "Explicit weather, projection and surface values", "Native Rain Drop Inputs")
    output = node(tree, "NodeGroupOutput", "Compiled Standard rain surface outputs", "Native Rain Drop Outputs")
    position = _separate(tree, entry.outputs["Projection Position"], "Native projection position components")
    projection_normal = _separate(tree, entry.outputs["Projection Normal"], "Native projection normal components")
    orientation = entry.outputs["Orientation Flag"]
    projected_y = _mix(tree, orientation, position[1], position[2], "Native oriented position y")
    projected_z = _mix(tree, orientation, position[2], _math(tree, "MULTIPLY", position[1], -1.0, "Native oriented negative position y"), "Native oriented position z")
    normal_y = _mix(tree, orientation, projection_normal[1], projection_normal[2], "Native oriented normal y")
    normal_z = _mix(tree, orientation, projection_normal[2], projection_normal[1], "Native oriented normal z")
    raw_position = tuple(_math(tree, "MULTIPLY", value, entry.outputs["Global Tiling"], "Native tiled projection " + axis) for axis, value in zip("XYZ", (position[0], projected_y, projected_z), strict=True))
    abs_normal = tuple(_math(tree, "ABSOLUTE", value, 0.0, "Native normal absolute " + axis) for axis, value in zip("XYZ", (projection_normal[0], normal_y, normal_z), strict=True))
    texture_weights = []
    drop_weights = []
    for axis, value in zip("XYZ", abs_normal, strict=True):
        adjusted = _math(tree, "SUBTRACT", value, 0.20000000298023224, "Native texture normal minus .2 " + axis)
        cube = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", adjusted, adjusted, "Native texture normal square " + axis), adjusted, "Native texture normal cube " + axis)
        texture_weights.append(_math(tree, "MAXIMUM", cube, 6.103515625e-05, "Native texture weight floor " + axis))
        positive = _math(tree, "MAXIMUM", adjusted, 0.0, "Native drop normal positive " + axis)
        square = _math(tree, "MULTIPLY", positive, positive, "Native drop normal square " + axis)
        fourth = _math(tree, "MULTIPLY", square, square, "Native drop normal fourth power " + axis)
        eighth = _math(tree, "MULTIPLY", fourth, fourth, "Native drop normal eighth power " + axis)
        tenth = _math(tree, "MULTIPLY", eighth, square, "Native drop normal tenth power " + axis)
        drop_weights.append(tenth)
    texture_total = _math(tree, "ADD", _math(tree, "ADD", texture_weights[0], texture_weights[1], "Native texture weights xy"), texture_weights[2], "Native texture weights sum")
    tex_weights = tuple(_math(tree, "DIVIDE", value, texture_total, "Native texture normalized weight " + axis) for axis, value in zip("XYZ", texture_weights, strict=True))
    drop_total = _math(tree, "MAXIMUM", _math(tree, "ADD", _math(tree, "ADD", drop_weights[0], drop_weights[1], "Native drop weights xy"), drop_weights[2], "Native drop weight sum"), 6.103515625e-05, "Native drop weight denominator")
    tri_weights = tuple(_math(tree, "DIVIDE", value, drop_total, "Native drop normalized weight " + axis) for axis, value in zip("XYZ", drop_weights, strict=True))
    effect_samples = []
    effect_alpha_samples: list[bpy.types.NodeSocket] = []
    for label, first, second, weight in (("XZ", raw_position[0], raw_position[2], tex_weights[1]), ("XY", raw_position[0], raw_position[1], tex_weights[2]), ("ZY", raw_position[2], raw_position[1], tex_weights[0])):
        image_node = node(tree, "ShaderNodeTexImage", "Source rain effect " + label, "Native Rain Drop Sampling")
        image_node.image = rain_effect_image
        image_node.extension = "REPEAT"
        image_node.interpolation = "Linear"
        uv = _combine(tree, (first, second, 0.0), "Source rain effect uv " + label)
        tree.links.new(uv, image_node.inputs["Vector"])
        weighted = color_mix(tree, "MULTIPLY", 1.0, image_node.outputs["Color"], _combine(tree, (weight, weight, weight), "Source rain effect weight " + label), "Weighted source rain effect " + label, "Native Rain Drop Sampling")
        effect_samples.append(weighted)
        effect_alpha_samples.append(_math(tree, "MULTIPLY", image_node.outputs["Alpha"], weight, "Weighted source rain effect alpha " + label))
    effect_sample = color_mix(tree, "ADD", 1.0, color_mix(tree, "ADD", 1.0, effect_samples[0], effect_samples[1], "Rain effect xz plus xy", "Native Rain Drop Sampling"), effect_samples[2], "Rain effect triplanar", "Native Rain Drop Sampling")
    effect_channels = node(tree, "ShaderNodeSeparateColor", "Source rain effect RGBA channels", "Native Rain Drop Sampling")
    effect_channels.mode = "RGB"
    tree.links.new(effect_sample, effect_channels.inputs[0])
    albedo_channels = node(tree, "ShaderNodeSeparateColor", "Source albedo RGB", "Native Rain Drop Equations")
    albedo_channels.mode = "RGB"
    tree.links.new(entry.outputs["Albedo"], albedo_channels.inputs[0])
    luminance = _math(tree, "ADD", _math(tree, "ADD", _math(tree, "MULTIPLY", albedo_channels.outputs["Red"], 0.2126729041337967, "Native luminance red"), _math(tree, "MULTIPLY", albedo_channels.outputs["Green"], 0.7151522040367126, "Native luminance green"), "Native luminance red green"), _math(tree, "MULTIPLY", albedo_channels.outputs["Blue"], 0.07217500358819962, "Native luminance blue"), "Native albedo luminance")
    nonmetal_luminance = _math(tree, "MULTIPLY", luminance, _math(tree, "SUBTRACT", 1.0, entry.outputs["Metallic"], "Native nonmetal complement"), "Native nonmetal luminance")
    dark_mask = _smoothstep_node(tree, 0.3499999940395355, 0.10000000149011612, nonmetal_luminance, "Native dark material mask")
    metallic_mask = _smoothstep_node(tree, 0.5, 0.75, entry.outputs["Metallic"], "Native metallic rain mask")
    smooth_dark = _math(tree, "MULTIPLY", _smoothstep_node(tree, 0.800000011920929, 0.6000000238418579, entry.outputs["Roughness"], "Native smooth material mask"), dark_mask, "Native smooth dark material")
    wet = _math(tree, "MAXIMUM", entry.outputs["Global Wetness"], entry.outputs["Water Submersion"], "Native combined wetness")
    wet_gate = _math(tree, "MULTIPLY", _math(tree, "MINIMUM", _math(tree, "MAXIMUM", _math(tree, "ADD", smooth_dark, metallic_mask, "Native wet material response"), 0.0, "Native wet material lower"), 1.0, "Native wet material upper"), _math(tree, "MAXIMUM", entry.outputs["Rain Intensity"], wet, "Native rain or wet maximum"), "Native drop wet gate")
    dynamic_first = _math(tree, "GREATER_THAN", entry.outputs["Rain Intensity"], 0.01, "Native animated rain test")
    dynamic_second = _math(tree, "LESS_THAN", wet, 0.01, "Native animated wet test")
    animated = _math(tree, "MAXIMUM", dynamic_first, dynamic_second, "Native dynamic phase flag")
    phase_base_static = _math(tree, "SUBTRACT", 1.0, wet, "Native static phase from wetness")
    phase_base_first = _mix(tree, animated, phase_base_static, 3.0, "Native first frequency phase base")
    phase_base_second = _mix(tree, animated, phase_base_static, 4.345600128173828, "Native second frequency phase base")
    time_multiplier = _mix(tree, animated, 1.0, entry.outputs["Native Time X"], "Native time-scale branch")
    drop_position = (raw_position[0], raw_position[1], raw_position[2])
    drop_components = tuple(_separate(tree, _combine(tree, drop_position, "Native drop position"), "Native drop position components"))
    first_planes = (
        _drop_plane_nodes(tree, (drop_components[0], drop_components[2]), tri_weights[1], 20.0, phase_base_first, time_multiplier, animated, wet_gate, "Native first drop XZ"),
        _drop_plane_nodes(tree, (drop_components[0], drop_components[1]), tri_weights[2], 20.0, phase_base_first, time_multiplier, animated, wet_gate, "Native first drop XY"),
        _drop_plane_nodes(tree, (drop_components[2], drop_components[1]), tri_weights[0], 20.0, phase_base_first, time_multiplier, animated, wet_gate, "Native first drop ZY"),
    )
    second_planes = (
        _drop_plane_nodes(tree, (drop_components[0], drop_components[2]), tri_weights[1], 34.34560012817383, phase_base_second, time_multiplier, animated, wet_gate, "Native second drop XZ"),
        _drop_plane_nodes(tree, (drop_components[0], drop_components[1]), tri_weights[2], 34.34560012817383, phase_base_second, time_multiplier, animated, wet_gate, "Native second drop XY"),
        _drop_plane_nodes(tree, (drop_components[2], drop_components[1]), tri_weights[0], 34.34560012817383, phase_base_second, time_multiplier, animated, wet_gate, "Native second drop ZY"),
    )
    first_coverage = _math(tree, "MAXIMUM", _math(tree, "MAXIMUM", first_planes[0][0], first_planes[1][0], "Native first coverage xy"), first_planes[2][0], "Native first coverage maximum")
    first_radius_max = _math(tree, "MAXIMUM", _math(tree, "MAXIMUM", first_planes[0][3], first_planes[1][3], "Native first radius xy"), first_planes[2][3], "Native first radius maximum")
    second_coverage = _math(tree, "MAXIMUM", _math(tree, "MAXIMUM", second_planes[0][0], second_planes[1][0], "Native second coverage xy"), second_planes[2][0], "Native second coverage maximum")
    second_gate = _math(tree, "SUBTRACT", 1.0, _math(tree, "GREATER_THAN", first_radius_max, 0.01, "Native second frequency suppressor"), "Native second frequency enabled")
    first_delta_x = _math(tree, "ADD", _math(tree, "ADD", first_planes[0][1], first_planes[1][1], "Native first normal xz xy"), first_planes[2][1], "Native first normal x")
    first_delta_y = _math(tree, "ADD", _math(tree, "ADD", first_planes[0][2], first_planes[1][2], "Native first normal y xz xy"), first_planes[2][2], "Native first normal y")
    second_delta_x = _math(tree, "MULTIPLY", _math(tree, "ADD", _math(tree, "ADD", second_planes[0][1], second_planes[1][1], "Native second normal xz xy"), second_planes[2][1], "Native second normal x"), second_gate, "Native gated second normal x")
    second_delta_y = _math(tree, "MULTIPLY", _math(tree, "ADD", _math(tree, "ADD", second_planes[0][2], second_planes[1][2], "Native second normal y xz xy"), second_planes[2][2], "Native second normal y"), second_gate, "Native gated second normal y")
    drop_delta_x = _math(tree, "ADD", first_delta_x, second_delta_x, "Native total drop normal x")
    drop_delta_y = _math(tree, "ADD", first_delta_y, second_delta_y, "Native total drop normal y")
    effect_r = _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", effect_channels.outputs["Red"], 2.0, "Rain texture normal red double"), 1.0, "Rain texture normal x")
    effect_g = _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", effect_channels.outputs["Green"], 2.0, "Rain texture normal green double"), 1.0, "Rain texture normal y")
    normal_xy_x = _math(tree, "ADD", effect_r, drop_delta_x, "Native drop normal x plus texture")
    normal_xy_y = _math(tree, "ADD", effect_g, drop_delta_y, "Native drop normal y plus texture")
    streak_normal = (projection_normal[0], 0.0, normal_z)
    streak_weights_raw = tuple(_math(tree, "MAXIMUM", _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", _math(tree, "ABSOLUTE", value, 0.0, "Native streak normal abs " + axis), 0.2, "Native streak normal minus .2 " + axis), _math(tree, "SUBTRACT", _math(tree, "ABSOLUTE", value, 0.0, "Native streak normal abs2 " + axis), 0.2, "Native streak normal minus .2b " + axis), "Native streak normal square " + axis), _math(tree, "SUBTRACT", _math(tree, "ABSOLUTE", value, 0.0, "Native streak normal abs3 " + axis), 0.2, "Native streak normal minus .2c " + axis), "Native streak normal cube " + axis), 6.103515625e-05, "Native streak weight floor " + axis) for axis, value in zip("XYZ", streak_normal, strict=True))
    streak_total = _math(tree, "ADD", _math(tree, "ADD", streak_weights_raw[0], streak_weights_raw[1], "Native streak weight xy"), streak_weights_raw[2], "Native streak weight sum")
    streak_x = _math(tree, "DIVIDE", streak_weights_raw[0], streak_total, "Native streak weight x")
    streak_z = _math(tree, "DIVIDE", streak_weights_raw[2], streak_total, "Native streak weight z")
    streak_offset = _combine(tree, (0.0, _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", entry.outputs["Native Time X"], entry.outputs["Global Tiling"], "Native streak offset time tiling"), 0.75, "Native streak offset"), 0.0), "Native streak uv offset")
    streak_base_nodes = []
    for label, first, second, weight in (("XY", raw_position[0], raw_position[1], streak_z), ("ZY", raw_position[2], raw_position[1], streak_x)):
        uv = _combine(tree, (first, second, 0.0), "Native streak uv " + label)
        base_texture = node(tree, "ShaderNodeTexImage", "Source rain streak base " + label, "Native Rain Drop Sampling")
        base_texture.image = rain_streak_image
        base_texture.extension = "REPEAT"
        base_texture.interpolation = "Linear"
        tree.links.new(uv, base_texture.inputs["Vector"])
        offset_uv = _vector(tree, "ADD", uv, streak_offset, "Native streak offset uv " + label)
        offset_texture = node(tree, "ShaderNodeTexImage", "Source rain streak offset " + label, "Native Rain Drop Sampling")
        offset_texture.image = rain_streak_image
        offset_texture.extension = "REPEAT"
        offset_texture.interpolation = "Linear"
        tree.links.new(offset_uv, offset_texture.inputs["Vector"])
        streak_base_nodes.append((base_texture, offset_texture, weight))
    streak_color_x = color_mix(tree, "MULTIPLY", 1.0, streak_base_nodes[0][0].outputs["Color"], _combine(tree, (streak_z, streak_z, streak_z), "Native streak weight xy"), "Native weighted streak xy", "Native Rain Drop Sampling")
    streak_color_z = color_mix(tree, "MULTIPLY", 1.0, streak_base_nodes[1][0].outputs["Color"], _combine(tree, (streak_x, streak_x, streak_x), "Native streak weight zy"), "Native weighted streak zy", "Native Rain Drop Sampling")
    streak_color = color_mix(tree, "ADD", 1.0, streak_color_x, streak_color_z, "Native streak triplanar sample", "Native Rain Drop Sampling")
    streak_channels = node(tree, "ShaderNodeSeparateColor", "Native streak RGBA", "Native Rain Drop Sampling")
    streak_channels.mode = "RGB"
    tree.links.new(streak_color, streak_channels.inputs[0])
    shifted_alpha_x = _math(tree, "MULTIPLY", streak_base_nodes[0][1].outputs["Alpha"], streak_z, "Native shifted streak alpha xy")
    shifted_alpha_z = _math(tree, "MULTIPLY", streak_base_nodes[1][1].outputs["Alpha"], streak_x, "Native shifted streak alpha zy")
    shifted_alpha = _math(tree, "ADD", shifted_alpha_x, shifted_alpha_z, "Native shifted streak alpha")
    streak_x_normal = _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", streak_channels.outputs["Red"], 2.0, "Native streak red double"), 1.0, "Native streak normal x"), _math(tree, "MULTIPLY", shifted_alpha, wet_gate, "Native streak normal strength"), "Native streak normal x weighted")
    streak_y_normal = _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", _math(tree, "MULTIPLY", streak_channels.outputs["Green"], 2.0, "Native streak green double"), 1.0, "Native streak normal y"), _math(tree, "MULTIPLY", shifted_alpha, wet_gate, "Native streak normal strength duplicate"), "Native streak normal y weighted")
    normal_xy_x = _math(tree, "ADD", normal_xy_x, streak_x_normal, "Native full tangent normal x")
    normal_xy_y = _math(tree, "ADD", normal_xy_y, streak_y_normal, "Native full tangent normal y")
    first_coverage_gate = _math(tree, "SUBTRACT", 1.0, _math(tree, "LESS_THAN", first_coverage, 0.01, "Native first coverage below threshold"), "Native first coverage activation")
    active_first_coverage = _math(tree, "MULTIPLY", first_coverage, first_coverage_gate, "Native activated first drop coverage")
    active_second_coverage = _math(tree, "MULTIPLY", second_coverage, second_gate, "Native activated second drop coverage")
    rain_sample_coverage = _math(tree, "SUBTRACT", 1.0, _math(tree, "LESS_THAN", effect_channels.outputs["Blue"], _math(tree, "SUBTRACT", 1.0099999904632568, wet_gate, "Native effect blue threshold"), "Native effect blue below threshold"), "Native effect blue coverage")
    streak_sample_z = streak_channels.outputs["Blue"]
    streak_coverage = _math(tree, "MULTIPLY", _smoothstep_node(tree, _math(tree, "SUBTRACT", 1.0, streak_sample_z, "Native streak coverage lower"), _math(tree, "SUBTRACT", 1.100000023841858, streak_sample_z, "Native streak coverage upper"), wet_gate, "Native streak coverage ramp"), wet_gate, "Native streak coverage")
    coverage = _math(tree, "MAXIMUM", _math(tree, "MAXIMUM", active_first_coverage, active_second_coverage, "Native two-frequency coverage"), _math(tree, "MAXIMUM", rain_sample_coverage, streak_coverage, "Native texture coverage"), "Native final rain coverage")
    branch_total = _math(tree, "MINIMUM", _math(tree, "MAXIMUM", _math(tree, "ADD", entry.outputs["Rain Intensity"], wet, "Native branch rain and wet"), 0.0, "Native branch lower"), 1.0, "Native branch upper")
    branch_enabled = _math(tree, "GREATER_THAN", _math(tree, "SUBTRACT", branch_total, entry.outputs["Disable Weather"], "Native rain material disable"), 0.009999999776482582, "Native rain branch gate")
    coverage = _math(tree, "MULTIPLY", coverage, branch_enabled, "Native gated rain coverage")
    base_normal = _vector(tree, "NORMALIZE", entry.outputs["Geometric Normal"], (0.0, 0.0, 0.0), "Native normalized geometric normal")
    normal_length2 = _math(tree, "ADD", _math(tree, "MULTIPLY", normal_xy_x, normal_xy_x, "Native normal x square"), _math(tree, "MULTIPLY", normal_xy_y, normal_xy_y, "Native normal y square"), "Native normal xy square sum")
    normal_z = _math(tree, "MAXIMUM", _math(tree, "SQRT", _math(tree, "SUBTRACT", 1.0, _math(tree, "MINIMUM", _math(tree, "MAXIMUM", normal_length2, 0.0, "Native tangent length lower"), 1.0, "Native tangent length upper"), "Native tangent normal z square"), 0.0, "Native tangent normal z sqrt"), 1.0000000168623835e-16, "Native tangent normal z floor")
    tangent_normal = _vector(tree, "NORMALIZE", _combine(tree, (normal_xy_x, normal_xy_y, normal_z), "Native reconstructed tangent normal"), (0.0, 0.0, 0.0), "Native normalized tangent normal")
    tangent_normal_components = _separate(tree, tangent_normal, "Native tangent normal channels")
    fallback_cross = _vector(tree, "CROSS_PRODUCT", base_normal, (0.0, 1.0, 0.0), "Native tangent frame cross")
    cross_components = _separate(tree, fallback_cross, "Native tangent frame cross components")
    cross_length2 = _math(tree, "ADD", _math(tree, "ADD", _math(tree, "MULTIPLY", cross_components[0], cross_components[0], "Native tangent cross x square"), _math(tree, "MULTIPLY", cross_components[1], cross_components[1], "Native tangent cross y square"), "Native tangent cross xy square"), _math(tree, "MULTIPLY", cross_components[2], cross_components[2], "Native tangent cross z square"), "Native tangent cross length square")
    frame_valid = _math(tree, "GREATER_THAN", cross_length2, 6.103515625e-05, "Native tangent frame validity")
    tangent = _vector(tree, "NORMALIZE", fallback_cross, (0.0, 0.0, 0.0), "Native normalized tangent")
    tangent = _combine(tree, tuple(_mix(tree, frame_valid, (1.0, 0.0, 0.0)[index], _separate(tree, tangent, "Native tangent components")[index], "Native fallback tangent " + "XYZ"[index]) for index in range(3)), "Native tangent frame")
    bitangent = _vector(tree, "CROSS_PRODUCT", tangent, base_normal, "Native bitangent frame")
    mapped = _vector(tree, "ADD", _vector(tree, "ADD", _vector(tree, "MULTIPLY", tangent, _combine(tree, (tangent_normal_components[0], tangent_normal_components[0], tangent_normal_components[0]), "Native tangent x coefficient"), "Native tangent normal x term"), _vector(tree, "MULTIPLY", bitangent, _combine(tree, (tangent_normal_components[1], tangent_normal_components[1], tangent_normal_components[1]), "Native tangent y coefficient"), "Native tangent normal y term"), "Native tangent frame xy"), _vector(tree, "MULTIPLY", base_normal, _combine(tree, (tangent_normal_components[2], tangent_normal_components[2], tangent_normal_components[2]), "Native tangent z coefficient"), "Native tangent normal z term"), "Native tangent frame xyz")
    mapped = _vector(tree, "NORMALIZE", mapped, (0.0, 0.0, 0.0), "Native mapped normal")
    normal_mix = _vector(tree, "ADD", _vector(tree, "MULTIPLY", base_normal, _combine(tree, (1.0, 1.0, 1.0), "Native base normal weight"), "Native base normal mix term"), _vector(tree, "MULTIPLY", mapped, _combine(tree, (coverage, coverage, coverage), "Native coverage vector"), "Native mapped normal mix term"), "Native normal mix sum")
    normal_mix = _vector(tree, "SUBTRACT", normal_mix, _vector(tree, "MULTIPLY", base_normal, _combine(tree, (coverage, coverage, coverage), "Native base normal coverage"), "Native covered base normal"), "Native covered base complement")
    normal = _vector(tree, "NORMALIZE", normal_mix, (0.0, 0.0, 0.0), "Native final surface normal")
    smooth_roughness = _math(tree, "MINIMUM", entry.outputs["Roughness"], 0.05000000074505806, "Native rain minimum roughness")
    roughness_mix = _mix(tree, coverage, entry.outputs["Roughness"], smooth_roughness, "Native rain roughness blend")
    effect_alpha = _math(tree, "ADD", _math(tree, "ADD", effect_alpha_samples[0], effect_alpha_samples[1], "Rain effect alpha xz plus xy"), effect_alpha_samples[2], "Rain effect alpha triplanar")
    wet_upper = _math(tree, "SUBTRACT", 1.100000023841858, effect_alpha, "Native wet alpha upper")
    nonmetal = _math(tree, "SUBTRACT", 1.0, entry.outputs["Metallic"], "Native wet nonmetal factor")
    rain_response_input = _math(tree, "MINIMUM", _math(tree, "MAXIMUM", _math(tree, "ADD", _math(tree, "MULTIPLY", entry.outputs["Rain Intensity"], nonmetal, "Native rain response intensity"), _math(tree, "MULTIPLY", _separate(tree, entry.outputs["Geometric Normal"], "Native normal for rain response")[1], 0.2, "Native rain normal bias"), "Native rain alpha input"), 0.0, "Native rain alpha lower"), 1.0, "Native rain alpha upper")
    water_response_input = _math(tree, "MINIMUM", _math(tree, "MAXIMUM", _math(tree, "MULTIPLY", entry.outputs["Water Submersion"], nonmetal, "Native water alpha input"), 0.0, "Native water alpha lower"), 1.0, "Native water alpha upper")
    rain_response = _smoothstep_node(tree, _math(tree, "SUBTRACT", 0.800000011920929, effect_alpha, "Native rain alpha lower threshold"), wet_upper, rain_response_input, "Native rain alpha response")
    water_response = _smoothstep_node(tree, _math(tree, "SUBTRACT", 0.44999998807907104, effect_alpha, "Native water alpha lower threshold"), wet_upper, water_response_input, "Native water alpha response")
    alpha_response = _math(tree, "MAXIMUM", rain_response, water_response, "Native rain alpha max response")
    reduced_by_dark = _math(tree, "SUBTRACT", roughness_mix, _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", 0.20000000298023224, dark_mask, "Native dark roughness amount"), alpha_response, "Native dark roughness response"), "Native wet reduced roughness")
    roughness = _math(tree, "MAXIMUM", reduced_by_dark, _math(tree, "MINIMUM", 0.20000000298023224, roughness_mix, "Native roughness floor"), "Native final roughness")
    darken_factor = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", alpha_response, _math(tree, "SUBTRACT", 1.0, dark_mask, "Native color dark complement"), "Native color wet bright material"), _math(tree, "SUBTRACT", 1.0, smooth_dark, "Native color rough complement"), "Native material darkening factor")
    darken = _mix(tree, darken_factor, 1.0, 0.5, "Native wet darkening")
    albedo_luma_gate = _smoothstep_node(tree, 0.699999988079071, 0.300000011920929, luminance, "Native albedo bright gate")
    albedo_boost = _math(tree, "ADD", 1.0, _math(tree, "MULTIPLY", albedo_luma_gate, 0.5, "Native albedo boost amount"), "Native albedo boost factor")
    albedo_boosted = color_mix(tree, "MULTIPLY", 1.0, entry.outputs["Albedo"], _combine(tree, (albedo_boost, albedo_boost, albedo_boost), "Native albedo boost vector"), "Native rain boosted albedo", "Native Rain Drop Equations")
    albedo_weight = _math(tree, "MULTIPLY", coverage, metallic_mask, "Native wet albedo coverage")
    albedo_mix = color_mix(tree, "MIX", albedo_weight, entry.outputs["Albedo"], albedo_boosted, "Native rain albedo blend", "Native Rain Drop Equations")
    albedo = color_mix(tree, "MULTIPLY", 1.0, albedo_mix, _combine(tree, (darken, darken, darken), "Native wet albedo darkening"), "Native final albedo", "Native Rain Drop Equations")
    shadow = color_mix(tree, "MULTIPLY", 1.0, entry.outputs["Shadow Color"], _combine(tree, (darken, darken, darken), "Native wet shadow darkening"), "Native final shadow color", "Native Rain Drop Equations")
    branch_complement = _math(tree, "SUBTRACT", 1.0, branch_enabled, "Native disabled rain branch complement")
    disabled_normal = _vector(tree, "MULTIPLY", normal, _combine(tree, (branch_enabled, branch_enabled, branch_enabled), "Native rain normal branch vector"), "Native rain branch normal")
    disabled_normal = _vector(tree, "ADD", disabled_normal, _vector(tree, "MULTIPLY", entry.outputs["Geometric Normal"], _combine(tree, (branch_complement, branch_complement, branch_complement), "Native rain normal complement vector"), "Native normal complement"), "Native normal branch sum")
    normal = disabled_normal
    roughness = _mix(tree, branch_enabled, entry.outputs["Roughness"], roughness, "Native branch-preserved roughness")
    albedo = color_mix(tree, "MIX", branch_enabled, entry.outputs["Albedo"], albedo, "Native branch-preserved albedo", "Native Rain Drop Equations")
    shadow = color_mix(tree, "MIX", branch_enabled, entry.outputs["Shadow Color"], shadow, "Native branch-preserved shadow color", "Native Rain Drop Equations")
    for name, socket in (("Coverage", coverage), ("Normal", normal), ("Roughness", roughness), ("Shadow Color", shadow), ("Albedo", albedo), ("Metallic", entry.outputs["Metallic"]), ("Drop Normal XY", _combine(tree, (drop_delta_x, drop_delta_y, 0.0), "Native drop normal delta output")), ("Tangent Normal XY", _combine(tree, (normal_xy_x, normal_xy_y, 0.0), "Native total tangent normal delta output")), ("Wet Normal Strength", wet_gate), ("Second Frequency Enabled", second_gate)):
        tree.links.new(socket, output.inputs[name])
    input_sockets["Native Time X"].description = "Native Unity _Time.x, not seconds; caller owns the seconds-to-native-time mapping"
    input_sockets["Projection Position"].description = "Native TEXCOORD6 projection position after the caller's verified orientation binding"
    input_sockets["Projection Normal"].description = "Native TEXCOORD5 projection normal after the caller's verified orientation binding"
    input_sockets["Base Texture Alpha"].description = "Retained for existing weather binder compatibility; this source Standard rain branch does not consume the input"
    input_sockets["Front Facing"].description = "Retained for existing weather binder compatibility; front-face normal correction is upstream"
    layout(tree)
    return tree
