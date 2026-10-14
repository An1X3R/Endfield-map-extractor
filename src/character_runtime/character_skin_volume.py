"""Current compiled Skin volume rim equations with explicit source/global frames.

CP8/9 describe auto rim; CP14/15 describe face rim and camera-basis selection.
These are additive color responses, not Blender scattering or a complete BRDF.
Native SkinDir uses cross(cameraForward, chosenDirection), in that order. The
explicit source-world frame corrects cross-product handedness when the host
coordinate conversion contains a reflection. It is not a mesh object frame.
All vector inputs share host world space and must use one orthonormal frame.
Constructor defaults are caller-supplied provenance, never assumed runtime data.
"""
from __future__ import annotations

from math import isfinite
from typing import TypeAlias

import bpy

from character_node_graph import node, math_node, color_mix
from character_weather_nodes import scalar_mix, combine_vector, vector_math, smoothstep

Vector4: TypeAlias = tuple[float, float, float, float]


def _validate_cp(value: Vector4, name: str) -> None:
    if len(value) != 4 or not all(isinstance(item, (float, int)) and isfinite(item) for item in value):
        raise ValueError(f"{name} must contain exactly four finite numbers")


def _scalar(tree: bpy.types.NodeTree, op: str, a: bpy.types.NodeSocket | float, b: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
    return math_node(tree, op, a, b, label, "Native Skin Volume Arithmetic")


def _vector(tree: bpy.types.NodeTree, op: str, a: bpy.types.NodeSocket, b: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return vector_math(tree, op, a, b, label).outputs[0]


def _dot(tree: bpy.types.NodeTree, a: bpy.types.NodeSocket, b: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return vector_math(tree, "DOT_PRODUCT", a, b, label).outputs["Value"]


def _scale(tree: bpy.types.NodeTree, value: bpy.types.NodeSocket, amount: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return _vector(tree, "MULTIPLY", value, combine_vector(tree, (amount, amount, amount), label + " coefficient"), label)


def _saturate(tree: bpy.types.NodeTree, value: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return _scalar(tree, "MINIMUM", _scalar(tree, "MAXIMUM", value, 0.0, label + " lower"), 1.0, label + " upper")


def _normalize(tree: bpy.types.NodeTree, value: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    return vector_math(tree, "NORMALIZE", value, (0.0, 0.0, 0.0), label).outputs[0]


def _add_cp(tree: bpy.types.NodeTree, cp8: Vector4, cp9: Vector4, cp15: Vector4) -> None:
    for cp, label in ((cp8, "CP8"), (cp9, "CP9"), (cp15, "CP15")):
        _validate_cp(cp, label)
    for label, kind, value in (
        ("CP8 Rim RGB", "NodeSocketColor", (*cp8[:3], 1.0)),
        ("CP8 Rim Intensity", "NodeSocketFloat", cp8[3]),
        ("CP9 Direction X", "NodeSocketFloat", cp9[0]),
        ("CP9 Direction Y", "NodeSocketFloat", cp9[1]),
        ("CP9 Albedo Mix", "NodeSocketFloat", cp9[2]),
        ("CP9 Rim Width", "NodeSocketFloat", cp9[3]),
        ("CP15 Camera Basis Mix", "NodeSocketFloat", cp15[3]),
    ):
        socket = tree.interface.new_socket(name=label, in_out="INPUT", socket_type=kind)
        socket.default_value = value
        socket.description = "Explicit CP component: caller must distinguish constructor state, runtime capture and preview override."


def _interface(name: str, cp8: Vector4, cp9: Vector4, cp15: Vector4) -> tuple[bpy.types.ShaderNodeTree, bpy.types.Node, bpy.types.Node]:
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    _add_cp(tree, cp8, cp9, cp15)
    vectors = ("Source Surface Normal", "View Direction", "Camera Forward", "Radial Flat Direction", "Source World X", "Source World Y", "Source World Z", "Camera Inverse View X", "Camera Inverse View Y")
    for label, kind in tuple((key, "NodeSocketVector") for key in vectors) + (("Diffuse Color", "NodeSocketColor"), ("Base Alpha", "NodeSocketFloat"), ("Screen Shadow Y", "NodeSocketFloat")):
        socket = tree.interface.new_socket(name=label, in_out="INPUT", socket_type=kind)
        socket.description = "Required explicit source-semantic input; no texture, frame or visibility fallback is supplied."
    for label, kind in (("Skin Direction", "NodeSocketVector"), ("Smooth Falloff", "NodeSocketFloat"), ("Skin Amount", "NodeSocketFloat"), ("Auto Rim Color", "NodeSocketColor"), ("Combined Skin Volume", "NodeSocketColor")):
        tree.interface.new_socket(name=label, in_out="OUTPUT", socket_type=kind)
    tree["source_formula"] = "Current Skin compiled body38844/40391-40396 and face83871/85169-85173"
    tree["shader_sha256"] = "7951feecd36004a69f002e9495d6c32f201a66020e6a67598c0e0ad75ba51e1a"
    tree["scope"] = "Additive CP8 auto rim and optional CP14 SDF face rim only; explicit global frames and CP values, no native runtime capture or fabricated scattering."
    entry = node(tree, "NodeGroupInput", "Explicit Skin volume state", "Native Skin Volume Inputs")
    output = node(tree, "NodeGroupOutput", "Verified additive Skin volume colors", "Native Skin Volume Outputs")
    return tree, entry, output


def _direction_and_falloff(tree: bpy.types.NodeTree, entry: bpy.types.Node) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket]:
    values = entry.outputs
    world = _vector(tree, "ADD", _scale(tree, values["Source World X"], values["CP9 Direction X"], "Global rim X"), _scale(tree, values["Source World Y"], values["CP9 Direction Y"], "Global rim Y"), "Global rim direction")
    camera = _vector(tree, "ADD", _scale(tree, values["Camera Inverse View X"], values["CP9 Direction X"], "Camera rim X"), _scale(tree, values["Camera Inverse View Y"], values["CP9 Direction Y"], "Camera rim Y"), "Camera rim direction")
    chosen = _vector(tree, "ADD", world, _scale(tree, _vector(tree, "SUBTRACT", camera, world, "Camera rim delta"), values["CP15 Camera Basis Mix"], "CP15 camera basis blend"), "Native chosen rim direction")
    handedness = _dot(tree, _vector(tree, "CROSS_PRODUCT", values["Source World X"], values["Source World Y"], "Explicit source frame cross"), values["Source World Z"], "Explicit source frame handedness")
    direction = _normalize(tree, _scale(tree, _vector(tree, "CROSS_PRODUCT", values["Camera Forward"], chosen, "Native cross cameraForward direction"), handedness, "Preserve source cross handedness"), "Native SkinDir")
    fresnel = _scalar(tree, "SUBTRACT", 1.0, _scalar(tree, "ABSOLUTE", _dot(tree, values["View Direction"], values["Source Surface Normal"], "Native V dot skin N"), 0.0, "Native absolute VdotN"), "Native skin fresnel")
    low = scalar_mix(tree, values["CP9 Rim Width"], 0.8, 0.2, "Native rim lower threshold")
    high = scalar_mix(tree, values["CP9 Rim Width"], 0.9, 0.5, "Native rim upper threshold")
    return direction, smoothstep(tree, low, high, fresnel, "Native SkinSmoothFalloff")


def _auto_rim(tree: bpy.types.NodeTree, entry: bpy.types.Node, direction: bpy.types.NodeSocket, amount: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    values = entry.outputs
    shadow = _scalar(tree, "MINIMUM", _scalar(tree, "MINIMUM", _saturate(tree, _scalar(tree, "ADD", _dot(tree, values["Radial Flat Direction"], direction, "Radial direction dot SkinDir"), 1.0, "Skin shadow wrap"), "Skin shadow saturate"), values["Base Alpha"], "Skin alpha minimum"), values["Screen Shadow Y"], "Skin screen-shadow minimum")
    scale = _scalar(tree, "MULTIPLY", _scalar(tree, "MULTIPLY", shadow, amount, "Skin shadow times amount"), _scalar(tree, "MULTIPLY", values["CP8 Rim Intensity"], _saturate(tree, _dot(tree, direction, values["Source Surface Normal"], "SkinDir dot skin N"), "Skin NdotDir saturate"), "Native rim directional intensity"), "Native skin rim scale")
    albedo = color_mix(tree, "MIX", values["CP9 Albedo Mix"], (0.25, 0.25, 0.25, 1.0), values["Diffuse Color"], "Native rim albedo mix", "Native Skin Volume Arithmetic")
    tint = color_mix(tree, "MULTIPLY", 1.0, albedo, values["CP8 Rim RGB"], "Native auto rim tint", "Native Skin Volume Arithmetic")
    return color_mix(tree, "MULTIPLY", 1.0, tint, scale, "Native additive auto rim", "Native Skin Volume Arithmetic")


def build_body_skin_volume(name: str, cp8: Vector4, cp9: Vector4, cp15: Vector4) -> bpy.types.ShaderNodeTree:
    """Build the current nonSDF Skin CP8 response; caller binds every geometry input."""
    tree, entry, output = _interface(name, cp8, cp9, cp15)
    direction, falloff = _direction_and_falloff(tree, entry)
    rim = _auto_rim(tree, entry, direction, falloff)
    for label, value in (("Skin Direction", direction), ("Smooth Falloff", falloff), ("Skin Amount", falloff), ("Auto Rim Color", rim), ("Combined Skin Volume", rim)):
        tree.links.new(value, output.inputs[label])
    return tree


def build_face_skin_volume(name: str, cp8: Vector4, cp9: Vector4, cp14: Vector4, cp15: Vector4) -> bpy.types.ShaderNodeTree:
    """Build SDF-gated CP8 and CP14 terms, without substituting a generic Skin BRDF."""
    _validate_cp(cp14, "CP14")
    tree, entry, output = _interface(name, cp8, cp9, cp15)
    for label, kind in (("Face Right", "NodeSocketVector"), ("Face Forward", "NodeSocketVector"), ("SDF Mask G", "NodeSocketFloat"), ("SDF Mask W", "NodeSocketFloat"), ("SDF Threshold Sample", "NodeSocketFloat")):
        tree.interface.new_socket(name=label, in_out="INPUT", socket_type=kind)
    for label, kind, value in (("CP14 Face Rim RGB", "NodeSocketColor", (*cp14[:3], 1.0)), ("CP14 Face Rim Intensity", "NodeSocketFloat", cp14[3]), ("CP15 Face Threshold Z", "NodeSocketFloat", cp15[2])):
        socket = tree.interface.new_socket(name=label, in_out="INPUT", socket_type=kind)
        socket.default_value = value
    tree.interface.new_socket(name="Face Rim Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    values = entry.outputs
    direction, falloff = _direction_and_falloff(tree, entry)
    camera_x = _dot(tree, values["Camera Forward"], values["Face Right"], "Camera source face X")
    camera_z = _dot(tree, values["Camera Forward"], values["Face Forward"], "Camera source face Z")
    length = _scalar(tree, "SQRT", _scalar(tree, "ADD", _scalar(tree, "MULTIPLY", camera_x, camera_x, "Camera face X squared"), _scalar(tree, "MULTIPLY", camera_z, camera_z, "Camera face Z squared"), "Camera face XZ length squared"), 0.0, "Camera face XZ length")
    angle = _scalar(tree, "ABSOLUTE", _scalar(tree, "DIVIDE", camera_z, length, "Native normalized camera face Z"), 0.0, "Native absolute camera angle")
    gate = smoothstep(tree, 0.9, 1.0, angle, "Native SDF camera gate")
    width_gate = _saturate(tree, _scalar(tree, "SUBTRACT", _scalar(tree, "MULTIPLY", values["CP9 Rim Width"], 10.0, "CP9 rim-width gate scale"), 3.0, "CP9 rim-width gate bias"), "CP9 rim-width gate")
    facing = _scalar(tree, "LESS_THAN", _dot(tree, values["Camera Forward"], direction, "Native camera facing SkinDir"), -0.01, "Native camera-facing threshold")
    amount = scalar_mix(tree, width_gate, _scalar(tree, "MULTIPLY", gate, falloff, "Native SDF-gated falloff"), _scalar(tree, "MULTIPLY", _scalar(tree, "MAXIMUM", gate, facing, "Native facing or camera gate"), values["SDF Mask W"], "Native SDFMaskW rim amount"), "Native face rim amount")
    auto = _auto_rim(tree, entry, direction, amount)
    half = _scalar(tree, "MULTIPLY", values["CP15 Face Threshold Z"], 0.5, "Native CP15 half threshold")
    center = _scalar(tree, "MINIMUM", _scalar(tree, "MAXIMUM", _scalar(tree, "SUBTRACT", 0.5, half, "Native CP15 transition center"), 0.001, "Native CP15 transition low"), 0.999, "Native CP15 transition high")
    double = _scalar(tree, "MULTIPLY", center, 2.0, "Native CP15 transition double")
    low = _scalar(tree, "MAXIMUM", _scalar(tree, "SUBTRACT", double, 1.0, "Native CP15 threshold lower candidate"), 0.0, "Native CP15 threshold lower")
    high = _scalar(tree, "MINIMUM", double, 1.0, "Native CP15 threshold upper")
    threshold = smoothstep(tree, low, high, values["SDF Threshold Sample"], "Native CP15 SDF response")
    response = _scalar(tree, "SUBTRACT", _scalar(tree, "MULTIPLY", _scalar(tree, "ABSOLUTE", _scalar(tree, "ADD", threshold, _scalar(tree, "MULTIPLY", _scalar(tree, "CEIL", half, 0.0, "Native CP15 half ceil"), half, "Native CP15 positive term"), "Native CP15 signed response"), 0.0, "Native CP15 absolute response"), 2.0, "Native CP15 response double"), 1.0, "Native CP15 signed threshold")
    coverage = _scalar(tree, "MULTIPLY", smoothstep(tree, -0.5, 0.5, response, "Native CP14 response smoothstep"), _scalar(tree, "SUBTRACT", 1.0, values["SDF Mask G"], "Native SDFMaskG face complement"), "Native face rim coverage")
    tint = color_mix(tree, "MULTIPLY", 1.0, values["CP14 Face Rim RGB"], values["Diffuse Color"], "Native CP14 albedo tint", "Native Skin Volume Arithmetic")
    face = color_mix(tree, "MULTIPLY", 1.0, tint, _scalar(tree, "MULTIPLY", coverage, values["CP14 Face Rim Intensity"], "Native face rim intensity"), "Native additive face rim", "Native Skin Volume Arithmetic")
    combined = color_mix(tree, "ADD", 1.0, auto, face, "Native additive Skin volume total", "Native Skin Volume Arithmetic")
    for label, value in (("Skin Direction", direction), ("Smooth Falloff", falloff), ("Skin Amount", amount), ("Auto Rim Color", auto), ("Face Rim Color", face), ("Combined Skin Volume", combined)):
        tree.links.new(value, output.inputs[label])
    return tree
