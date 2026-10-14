"""Current compiled Skin procedural rain, independent of Standard wet shading.

Reference: non-SDF Skin first ForwardLit fragment 1305-1594. SDF face rain is
a different texture-sampling branch and is not implemented by this factory.
Projection Position,
Projection Normal and Projection Gravity are supplied source varyings, not
captured Blender attributes. Gravity is _1113 before the instance orientation
swizzle. Geometric Normal is the source surface normal _486 and may include
normal mapping. All normal/view vectors use native Unity source-world axes.
Ordinary Normal, Roughness and Metallic are preserved. Wet Normal feeds only
the separate rain anisotropic specular consumer at source 1689-1690. Native
Time X is HGRP time / 20, not elapsed seconds. No texture is sampled here.
"""
from __future__ import annotations

from typing import Callable

import bpy

from character_node_graph import color_mix, math_node, node
from character_rain_droplets import build_source_drop_hash_nodes
from character_weather_nodes import scalar_mix, smoothstep


SECTION = "Compiled Skin Rain"
Scalar = bpy.types.NodeSocket | float
Vector = bpy.types.NodeSocket | tuple[float, float, float]


def _math(tree: bpy.types.NodeTree, operation: str, a: Scalar, b: Scalar, label: str) -> bpy.types.NodeSocket:
    return math_node(tree, operation, a, b, label, SECTION)


def _clamp(tree: bpy.types.NodeTree, value: Scalar, low: float, high: float, label: str) -> bpy.types.NodeSocket:
    return _math(tree, "MINIMUM", _math(tree, "MAXIMUM", value, low, label + " lower"), high, label)


def _separate(tree: bpy.types.NodeTree, value: Vector, label: str) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    result = node(tree, "ShaderNodeSeparateXYZ", label, SECTION)
    if isinstance(value, tuple):
        result.inputs[0].default_value = value
    else:
        tree.links.new(value, result.inputs[0])
    return tuple(result.outputs)


def _combine(tree: bpy.types.NodeTree, values: tuple[Scalar, Scalar, Scalar], label: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeCombineXYZ", label, SECTION)
    for socket, value in zip(result.inputs, values):
        if isinstance(value, (int, float)):
            socket.default_value = value
        else:
            tree.links.new(value, socket)
    return result.outputs[0]


def _vector(tree: bpy.types.NodeTree, operation: str, a: Vector, b: Vector, label: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeVectorMath", label, SECTION)
    result.operation = operation
    for socket, value in zip(result.inputs, (a, b)):
        if isinstance(value, tuple):
            socket.default_value = value
        else:
            tree.links.new(value, socket)
    return result.outputs["Value" if operation in ("DOT_PRODUCT", "LENGTH", "DISTANCE") else "Vector"]


def _scale(tree: bpy.types.NodeTree, value: Vector, factor: Scalar, label: str) -> bpy.types.NodeSocket:
    result = node(tree, "ShaderNodeVectorMath", label, SECTION)
    result.operation = "SCALE"
    if isinstance(value, tuple):
        result.inputs[0].default_value = value
    else:
        tree.links.new(value, result.inputs[0])
    if isinstance(factor, (int, float)):
        result.inputs[3].default_value = factor
    else:
        tree.links.new(factor, result.inputs[3])
    return result.outputs[0]


def _oriented(tree: bpy.types.NodeTree, values: tuple[Scalar, Scalar, Scalar], flag: Scalar, z_sign: float, label: str) -> tuple[Scalar, Scalar, Scalar]:
    return values[0], scalar_mix(tree, flag, values[1], values[2], label + " y"), scalar_mix(tree, flag, values[2], _math(tree, "MULTIPLY", values[1], z_sign, label + " alternate z"), label + " z")


def _drop_plane(
    tree: bpy.types.NodeTree,
    pair: tuple[Scalar, Scalar],
    gravity: tuple[Scalar, Scalar],
    weight: Scalar,
    radius_scale: Scalar,
    phase: Scalar,
    animated: Scalar,
    threshold: Scalar,
    label: str,
) -> tuple[bpy.types.NodeSocket, tuple[bpy.types.NodeSocket, bpy.types.NodeSocket], tuple[bpy.types.NodeSocket, bpy.types.NodeSocket]]:
    """Build one compiled plane: coverage, gradient and two radius statistics."""
    def m(operation: str, a: Scalar, b: Scalar, suffix: str) -> bpy.types.NodeSocket:
        return _math(tree, operation, a, b, label + " " + suffix)

    cell = tuple(m("FLOOR", value, 0.0, "cell " + str(index)) for index, value in enumerate(pair))
    primary, secondary = build_source_drop_hash_nodes(tree, cell[0], cell[1], label)
    radius = m("MULTIPLY", m("MULTIPLY", 0.25, scalar_mix(tree, primary[0], 0.6000000238418579, 1.0, label + " cell radius"), "radius base"), radius_scale, "radius")
    offsets = tuple(m("SUBTRACT", m("ADD", m("SUBTRACT", pair[index], cell[index], "cell offset " + str(index)), m("MULTIPLY", m("SUBTRACT", m("MULTIPLY", secondary[index], 2.0, "jitter doubled " + str(index)), 1.0, "jitter signed " + str(index)), 0.25, "jitter " + str(index)), "offset " + str(index)), 0.5, "centered offset " + str(index)) for index in range(2))
    g2 = m("ADD", m("MULTIPLY", gravity[0], gravity[0], "gravity x squared"), m("MULTIPLY", gravity[1], gravity[1], "gravity y squared"), "gravity length squared")
    use_rotation = m("GREATER_THAN", g2, 9.999999747378752e-6, "gravity rotation branch")
    inv_length = m("DIVIDE", 1.0, m("SQRT", m("MAXIMUM", g2, 1e-16, "safe unused gravity denominator"), 0.0, "gravity length"), "gravity inverse length")
    gx, gy = tuple(m("MULTIPLY", value, inv_length, "gravity unit " + str(index)) for index, value in enumerate(gravity))
    rotated_x = m("ADD", m("MULTIPLY", offsets[0], m("MULTIPLY", gy, -1.0, "minus gravity y"), "rotated x first"), m("MULTIPLY", offsets[1], gx, "rotated x second"), "rotated x")
    rotated_y = m("MULTIPLY", m("ADD", m("MULTIPLY", offsets[0], gx, "rotated y first"), m("MULTIPLY", offsets[1], gy, "rotated y second"), "rotated y sum"), -1.0, "rotated y")
    ox = scalar_mix(tree, use_rotation, offsets[0], rotated_x, label + " selected offset x")
    oy = scalar_mix(tree, use_rotation, offsets[1], rotated_y, label + " selected offset y")
    ex = m("MULTIPLY", ox, 1.25, "ellipse x")
    ey = m("MULTIPLY", oy, scalar_mix(tree, m("LESS_THAN", oy, 0.0, "ellipse sign"), 0.75, 1.25, label + " ellipse aspect"), "ellipse y")
    distance = m("SQRT", m("ADD", m("MULTIPLY", ex, ex, "ellipse x squared"), m("MULTIPLY", ey, ey, "ellipse y squared"), "ellipse squared length"), 0.0, "ellipse length")
    clock = m("ADD", phase, primary[0], "cell phase")
    clock = scalar_mix(tree, animated, scalar_mix(tree, _clamp(tree, clock, 0.0, 1.0, label + " static phase clamp"), 0.2199999988079071, 0.8500000238418579, label + " static phase"), m("FRACT", clock, 0.0, "animated phase"), label + " phase branch")
    lifetime = m("MULTIPLY", smoothstep(tree, 0.20000000298023224, 0.2199999988079071, clock, label + " grow"), smoothstep(tree, 0.8500000238418579, 0.550000011920929, clock, label + " fade"), "lifetime")
    inside = m("SUBTRACT", 1.0, m("LESS_THAN", smoothstep(tree, radius, 0.0, distance, label + " radial coverage"), 0.0010000000474974513, "outside radius"), "inside radius")
    chosen = m("SUBTRACT", 1.0, m("LESS_THAN", m("SUBTRACT", primary[1], 0.10000000149011612, "cell threshold value"), threshold, "cell rejected"), "cell chosen")
    shape = m("MULTIPLY", m("MULTIPLY", lifetime, inside, "radial lifetime"), chosen, "cell shape")
    coverage = m("MULTIPLY", shape, weight, "weighted coverage")
    gradient_scale = scalar_mix(tree, secondary[0], 0.25, 0.5, label + " gradient scale")
    gradients = tuple(m("MULTIPLY", m("MULTIPLY", _clamp(tree, m("DIVIDE", value, radius, "gradient ratio " + str(index)), -1.0, 1.0, label + " gradient clamp " + str(index)), gradient_scale, "gradient scale " + str(index)), coverage, "weighted gradient " + str(index)) for index, value in enumerate((ex, ey)))
    ring = m("MULTIPLY", radius, coverage, "weighted radius amplitude"), m("MULTIPLY", m("SUBTRACT", radius, distance, "radius distance"), weight, "weighted radius distance")
    return coverage, gradients, ring


def _layer(tree: bpy.types.NodeTree, position: tuple[Scalar, Scalar, Scalar], gravity: tuple[Scalar, Scalar, Scalar], weights: tuple[Scalar, Scalar, Scalar], frequency: float, radius_scale: Scalar, phase: Scalar, animated: Scalar, threshold: Scalar, label: str) -> tuple[bpy.types.NodeSocket, tuple[bpy.types.NodeSocket, bpy.types.NodeSocket], tuple[bpy.types.NodeSocket, bpy.types.NodeSocket]]:
    scaled = tuple(_math(tree, "MULTIPLY", value, frequency, label + " coordinate " + str(index)) for index, value in enumerate(position))
    planes = tuple(_drop_plane(tree, (scaled[a], scaled[b]), (gravity[a], gravity[b]), weights[w], radius_scale, phase, animated, threshold, label + " " + plane) for a, b, w, plane in ((0, 2, 1, "XZ"), (0, 1, 2, "XY"), (2, 1, 0, "ZY")))
    coverage = _math(tree, "MAXIMUM", planes[0][0], _math(tree, "MAXIMUM", planes[1][0], planes[2][0], label + " coverage latter maximum"), label + " coverage maximum")
    gradient = tuple(_math(tree, "ADD", _math(tree, "ADD", planes[0][1][i], planes[1][1][i], label + " gradient first sum " + str(i)), planes[2][1][i], label + " gradient sum " + str(i)) for i in range(2))
    ring = tuple(_math(tree, "MAXIMUM", planes[0][2][i], _math(tree, "MAXIMUM", planes[1][2][i], planes[2][2][i], label + " ring latter maximum " + str(i)), label + " ring maximum " + str(i)) for i in range(2))
    return coverage, gradient, ring


def build_skin_rain(source_shader_sha256: str, layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    """Build source rain arithmetic; caller owns projection binding and lighting."""
    if len(source_shader_sha256) != 64 or any(value not in "0123456789abcdef" for value in source_shader_sha256.lower()):
        raise ValueError("Skin rain requires the exact compiled shader SHA-256")
    tree = bpy.data.node_groups.new("EF Native Skin Procedural Rain - Compiled", "ShaderNodeTree")
    tree.use_fake_user = True
    tree["source_shader_sha256"] = source_shader_sha256.lower()
    tree["source_formula"] = "Current Skin first ForwardLit fragment 1305-1594; post-main consumer 1689-1690"
    tree["supported_branch"] = "Skin non-SDF procedural rain only; SDF face texture branch excluded"
    tree["scope"] = "Procedural Skin rain arithmetic; projection varyings are supplied and not captured. Ordinary normal/roughness/metallic remain dry. Wet Normal is a separate rain-specular input."
    inputs = (
        ("Projection Position", "NodeSocketVector", (0., 0., 0.)),
        ("Projection Normal", "NodeSocketVector", (0., 1., 0.)),
        ("Projection Gravity", "NodeSocketVector", (0., 0., 0.)),
        ("Geometric Normal", "NodeSocketVector", (0., 1., 0.)),
        ("View", "NodeSocketVector", (0., 1., 0.)),
        ("Orientation Flag", "NodeSocketFloat", 0.),
        ("Global Tiling", "NodeSocketFloat", 1.),
        ("Rain Intensity", "NodeSocketFloat", 0.),
        ("Water Submersion", "NodeSocketFloat", 0.),
        ("Global Wetness", "NodeSocketFloat", 0.),
        ("Native Time X", "NodeSocketFloat", 0.),
        ("Disable Weather", "NodeSocketFloat", 0.),
        ("Roughness", "NodeSocketFloat", .5),
        ("Albedo", "NodeSocketColor", (.5, .5, .5, 1.)),
        ("Shadow Color", "NodeSocketColor", (.5, .5, .5, 1.)),
        ("Metallic", "NodeSocketFloat", 0.),
    )
    for label, kind, value in inputs:
        socket = tree.interface.new_socket(name=label, in_out="INPUT", socket_type=kind)
        socket.default_value = value
        if label == "Projection Gravity":
            socket.description = "Source _1113 projected gravity before instance orientation; caller must supply actual projection/world tangent frames or disclose an experimental binding."
        elif label == "Native Time X":
            socket.description = "Native HGRP clock multiplied by float32 0.05; preview seconds / 20, not seconds."
        elif label in ("Geometric Normal", "View"):
            socket.description = "Native Unity source-world surface normal/view direction; normal may include the source normal map."
    outputs = (("Normal", "NodeSocketVector"), ("Wet Normal", "NodeSocketVector"), ("Albedo", "NodeSocketColor"), ("Shadow Color", "NodeSocketColor"), ("Roughness", "NodeSocketFloat"), ("Metallic", "NodeSocketFloat"), ("Wet Specular Coverage", "NodeSocketFloat"), ("Wet Specular Add", "NodeSocketFloat"), ("Wet Specular Scale", "NodeSocketFloat"), ("Coverage", "NodeSocketFloat"), ("Tone", "NodeSocketFloat"), ("Second Frequency Enabled", "NodeSocketFloat"))
    for label, kind in outputs:
        tree.interface.new_socket(name=label, in_out="OUTPUT", socket_type=kind)
    entry = node(tree, "NodeGroupInput", "Explicit native projection and surface", SECTION)
    output = node(tree, "NodeGroupOutput", "Skin rain material and separate specular inputs", SECTION)
    e = entry.outputs
    flag = e["Orientation Flag"]
    position = _oriented(tree, _separate(tree, e["Projection Position"], "Source projection position"), flag, -1., "Source oriented position")
    position = tuple(_math(tree, "MULTIPLY", v, e["Global Tiling"], "Source tiled coordinate " + str(i)) for i, v in enumerate(position))
    gravity = _oriented(tree, _separate(tree, e["Projection Gravity"], "Source projected gravity"), flag, -1., "Source oriented gravity")
    projection_normal = _oriented(tree, _separate(tree, e["Projection Normal"], "Source projection normal"), flag, 1., "Source oriented projection normal")
    raw_weights = tuple(_math(tree, "POWER", _math(tree, "MAXIMUM", _math(tree, "SUBTRACT", _math(tree, "ABSOLUTE", v, 0., "Absolute projection normal " + str(i)), .20000000298023224, "Projection normal bias " + str(i)), 0., "Projection weight clamp " + str(i)), 10., "Projection weight power " + str(i)) for i, v in enumerate(projection_normal))
    total = _math(tree, "MAXIMUM", _math(tree, "ADD", _math(tree, "ADD", raw_weights[0], raw_weights[1], "Weights first sum"), raw_weights[2], "Weights sum"), 6.103515625e-5, "Weights denominator")
    weights = tuple(_math(tree, "DIVIDE", v, total, "Normalized weight " + str(i)) for i, v in enumerate(raw_weights))
    cell_xz = tuple(_math(tree, "FLOOR", _math(tree, "MULTIPLY", position[i], 20., "Radius cell scaled " + str(i)), 0., "Radius cell " + str(i)) for i in (0, 2))
    q = tuple(_math(tree, "FRACT", _math(tree, "MULTIPLY", v, .1031000018119812, "Radius hash scale " + str(i)), 0., "Radius hash fract " + str(i)) for i, v in enumerate((cell_xz[0], cell_xz[1], cell_xz[0])))
    dot_terms = tuple(_math(tree, "MULTIPLY", q[i], _math(tree, "ADD", q[(i+1)%3], 33.33000183105469, "Radius hash shifted " + str(i)), "Radius hash dot term " + str(i)) for i in range(3))
    dot = _math(tree, "ADD", _math(tree, "ADD", dot_terms[0], dot_terms[1], "Radius hash first sum"), dot_terms[2], "Radius hash dot")
    q = tuple(_math(tree, "ADD", v, dot, "Radius hash mixed " + str(i)) for i, v in enumerate(q))
    inner = _math(tree, "FRACT", _math(tree, "MULTIPLY", _math(tree, "ADD", q[0], q[1], "Radius hash xy sum"), q[2], "Radius hash product"), 0., "Radius inner fract")
    radius_noise = _math(tree, "FRACT", _math(tree, "ADD", _math(tree, "MULTIPLY", position[1], -3., "Radius height phase"), inner, "Radius combined phase"), 0., "Radius outer fract")
    radius_scale = scalar_mix(tree, radius_noise, .30000001192092896, .6499999761581421, "Source radius multiplier")
    wet = _math(tree, "MAXIMUM", e["Global Wetness"], e["Water Submersion"], "Source combined wetness")
    wet_only = _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", 1., _math(tree, "GREATER_THAN", e["Rain Intensity"], .009999999776482582, "Rain exceeds wet-only limit"), "Rain below wet-only limit"), _math(tree, "SUBTRACT", 1., _math(tree, "LESS_THAN", wet, .009999999776482582, "Wet below wet-only limit"), "Wet present"), "Source wet-only branch")
    animated = _math(tree, "SUBTRACT", 1., wet_only, "Source animated branch")
    threshold = _math(tree, "SUBTRACT", 1., _math(tree, "MULTIPLY", .800000011920929, scalar_mix(tree, wet_only, e["Rain Intensity"], wet, "Source rain or wet selection"), "Source cell gate amount"), "Source cell threshold")
    time = scalar_mix(tree, animated, 1., e["Native Time X"], "Source time branch")
    phases = tuple(_math(tree, "MULTIPLY", time, scalar_mix(tree, animated, _math(tree, "SUBTRACT", 1., wet, "Static wet speed " + str(i)), speed, "Layer speed " + str(i)), "Layer phase " + str(i)) for i, speed in enumerate((3., 4.345600128173828)))
    first = _layer(tree, position, gravity, weights, 24., radius_scale, phases[0], animated, threshold, "First layer")
    second = _layer(tree, position, gravity, weights, 36.27000045776367, radius_scale, phases[1], animated, threshold, "Second layer")
    second_enabled = _math(tree, "SUBTRACT", 1., _math(tree, "GREATER_THAN", first[2][0], .009999999776482582, "First radius suppresses second"), "Source second frequency gate")
    ring = tuple(_math(tree, "MAXIMUM", first[2][i], _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", second[2][i], factor, "Second radius normalization " + str(i)), second_enabled, "Second radius gate " + str(i)), "Combined radius statistic " + str(i)) for i, factor in enumerate((.661703884601593, 1.)))
    base_normal = e["Geometric Normal"]
    coverage = _math(tree, "MULTIPLY", _math(tree, "MAXIMUM", _math(tree, "MULTIPLY", first[0], _math(tree, "SUBTRACT", 1., _math(tree, "LESS_THAN", first[0], .009999999776482582, "First coverage below threshold"), "First coverage accepted"), "First gated coverage"), _math(tree, "MULTIPLY", second[0], second_enabled, "Second gated coverage"), "Combined coverage"), _clamp(tree, _vector(tree, "DOT_PRODUCT", e["View"], base_normal, "Source view surface cosine"), 0., 1., "Source view clamp"), "Source view-weighted coverage")
    gradients = tuple(_math(tree, "ADD", first[1][i], _math(tree, "MULTIPLY", second[1][i], second_enabled, "Second gradient gate " + str(i)), "Combined gradient " + str(i)) for i in range(2))
    g2 = _math(tree, "ADD", _math(tree, "MULTIPLY", gradients[0], gradients[0], "Gradient x squared"), _math(tree, "MULTIPLY", gradients[1], gradients[1], "Gradient y squared"), "Gradient squared length")
    nz = _math(tree, "MAXIMUM", 1e-16, _math(tree, "SQRT", _math(tree, "SUBTRACT", 1., _clamp(tree, g2, 0., 1., "Gradient length clamp"), "Gradient z squared"), 0., "Gradient z"), "Gradient positive z")
    n1 = _vector(tree, "NORMALIZE", _combine(tree, (_math(tree, "MULTIPLY", gradients[0], 2.5, "Gradient x strength"), _math(tree, "MULTIPLY", gradients[1], 2.5, "Gradient y strength"), nz), "First tangent normal"), (0., 0., 0.), "Normalize first tangent normal")
    nx, ny, unused_z = _separate(tree, n1, "First tangent normal components")
    nxy2 = _math(tree, "ADD", _math(tree, "MULTIPLY", nx, nx, "Normal x squared"), _math(tree, "MULTIPLY", ny, ny, "Normal y squared"), "Normal xy squared length")
    final_z = _math(tree, "MAXIMUM", 1e-16, _math(tree, "SQRT", _math(tree, "SUBTRACT", 1., _clamp(tree, nxy2, 0., 1., "Normal xy clamp"), "Normal z squared"), 0., "Normal z"), "Normal positive z")
    tangent_normal = _vector(tree, "NORMALIZE", _combine(tree, (nx, ny, final_z), "Reconstructed tangent normal"), (0., 0., 0.), "Normalize reconstructed tangent normal")
    nx, ny, nz = _separate(tree, tangent_normal, "Rain tangent normal components")
    tangent_raw = _vector(tree, "CROSS_PRODUCT", base_normal, (0., 1., 0.), "Source rain tangent cross N and up")
    use_tangent = _math(tree, "GREATER_THAN", _vector(tree, "DOT_PRODUCT", tangent_raw, tangent_raw, "Rain tangent squared length"), 6.103515625e-5, "Source rain tangent branch")
    normalized_tangent = _vector(tree, "NORMALIZE", tangent_raw, (0., 0., 0.), "Normalize rain tangent")
    tangent_values = _separate(tree, normalized_tangent, "Rain tangent components")
    tangent = _combine(tree, tuple(scalar_mix(tree, use_tangent, fallback, value, "Selected rain tangent " + str(i)) for i, (fallback, value) in enumerate(zip((1., 0., 0.), tangent_values))), "Selected source rain tangent")
    wet_normal = _vector(tree, "NORMALIZE", _vector(tree, "ADD", _vector(tree, "ADD", _scale(tree, tangent, nx, "Wet tangent x"), _scale(tree, _vector(tree, "CROSS_PRODUCT", tangent, base_normal, "Source rain bitangent T cross N"), ny, "Wet tangent y"), "Wet tangent sum"), _scale(tree, base_normal, nz, "Wet surface normal z"), "Wet source normal sum"), (0., 0., 0.), "Normalize wet source normal")
    ring_amplitude = _math(tree, "MULTIPLY", ring[0], 4., "Source radius amplitude")
    d = _clamp(tree, _vector(tree, "DOT_PRODUCT", tangent_normal, (0., -.8, .6), "Source rain fixed light dot"), 0., 1., "Source rain fixed light clamp")
    darkness = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", .6000000238418579, coverage, "Coverage darkening factor"), ring_amplitude, "Source darkening amount")
    ring_term = _clamp(tree, _math(tree, "MULTIPLY", ring[1], 17.540000915527344, "Radius edge light"), 0., 1., "Radius edge clamp")
    normal_term = _clamp(tree, _math(tree, "MULTIPLY", ny, 1.600000023841858, "Rain y light"), 0., 1., "Rain y light clamp")
    tone = _math(tree, "ADD", _math(tree, "SUBTRACT", 1., darkness, "Source darkened tone"), _math(tree, "MULTIPLY", _clamp(tree, _math(tree, "ADD", ring_term, normal_term, "Ring and normal light"), 0., 1., "Ring combined light clamp"), darkness, "Source light recovery"), "Source rain tone")
    smooth_tone = smoothstep(tree, .6000000238418579, 1., tone, "Source smooth tone")
    spec_coverage = _math(tree, "MULTIPLY", coverage, ring_amplitude, "Source wet specular coverage")
    d3 = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", d, d, "Fixed light squared"), d, "Fixed light cubed")
    spec_add = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", scalar_mix(tree, d3, .05000000074505806, 1.7999999523162842, "Source additive lobe weight"), ring_amplitude, "Additive radius weight"), smooth_tone, "Additive tone weight"), coverage, "Source wet specular additive factor")
    spec_scale = scalar_mix(tree, coverage, 1., _math(tree, "MULTIPLY", .800000011920929, scalar_mix(tree, smooth_tone, .5, 1., "Source smooth specular factor"), "Source specular target"), "Source wet specular scale")
    gate = _math(tree, "GREATER_THAN", _math(tree, "SUBTRACT", _clamp(tree, _math(tree, "ADD", e["Rain Intensity"], wet, "Rain and wet sum"), 0., 1., "Rain total clamp"), e["Disable Weather"], "Source weather disable subtraction"), .009999999776482582, "Source material weather branch")
    for label, source in (("Normal", base_normal), ("Roughness", e["Roughness"]), ("Metallic", e["Metallic"])):
        tree.links.new(source, output.inputs[label])
    tree.links.new(_vector(tree, "ADD", _scale(tree, base_normal, _math(tree, "SUBTRACT", 1., gate, "Dry normal gate"), "Dry wet-normal branch"), _scale(tree, wet_normal, gate, "Active wet-normal branch"), "Source wet-normal branch"), output.inputs["Wet Normal"])
    for label, source, dry in (("Wet Specular Coverage", spec_coverage, 0.), ("Wet Specular Add", spec_add, 0.), ("Wet Specular Scale", spec_scale, 1.), ("Coverage", coverage, 0.), ("Tone", tone, 1.), ("Second Frequency Enabled", second_enabled, 0.)):
        tree.links.new(scalar_mix(tree, gate, dry, source, "Material branch " + label), output.inputs[label])
    final_tone = scalar_mix(tree, gate, 1., tone, "Material branch color tone")
    for label in ("Albedo", "Shadow Color"):
        factor = _combine(tree, (final_tone, final_tone, final_tone), "Source " + label + " tone")
        tree.links.new(color_mix(tree, "MULTIPLY", 1., e[label], factor, "Source rain " + label, SECTION), output.inputs[label])
    for item in tree.nodes:
        item["endfield_layout_section"] = SECTION
    layout(tree)
    return tree
