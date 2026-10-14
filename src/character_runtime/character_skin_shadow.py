"""Current compiled Skin screen-shadow consumption, shared by Eevee and Cycles.

These groups consume supplied raw R/G visibility; they do not render an atlas,
sample screen coordinates, or generate occlusion. R controls main cast shadow;
G controls local receiver visibility before source alpha and ramp conditioning.
The face input Face/View Dot is the camera-frame +Z direction transformed into
the source face frame, projected onto XZ and normalized, then taking local Z.
It is not a surface NdotV or the surface-to-camera Incoming vector.
Body reference: current Skin fragment 0125, lines 40353-40366. Face reference:
current Skin fragment 0269, lines 84830-84834 and 85102-85144. Both references
are SPIR-V-derived current-reader listings, not original author HLSL.
Ambient Shadow A is separate from NPR Shadow A: the SDF ambient branch applies
MaskG once more to local visibility, whereas its NPR dark branch does not.
"""
from __future__ import annotations

import bpy

from character_node_graph import node, math_node
from character_weather_nodes import scalar_mix, smoothstep


SECTION = "Skin Screen Shadow"
COMMON_INPUTS = (
    ("Raw Screen Shadow R", 1.0, "Raw main directional screen-mask R; 1 is an explicit unshadowed input, not an atlas capture."),
    ("Raw Screen Shadow G", 1.0, "Raw local character receiver screen-mask G; kept independent of main R."),
    ("Directional Strength", 1.0, "Source _DirectionalShadowParams.x; caller-supplied strength, not inferred from a Blender light."),
    ("Ignore Main Shadow", 0.0, "Source CP1.z / charIgnoreMainLightShadow; affects R cast visibility only."),
    ("Base Alpha", 1.0, "Source post-weather base alpha/AO at this consumer; frozen dry body uses BaseMap alpha."),
    ("Ramp Alpha", 1.0, "Source sampled diffuse-ramp alpha, not material coverage."),
)
OUTPUTS = ("Cast Shadow", "Raw Local Visibility", "Local Visibility", "Shadow A", "Shadow B", "Ambient Shadow A")


def _interface(name: str, extra_inputs: tuple[tuple[str, float, str], ...]) -> tuple[bpy.types.ShaderNodeTree, bpy.types.Node, bpy.types.Node]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Skin shadow group name must be a nonempty string")
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    for label, value, description in (*COMMON_INPUTS, *extra_inputs):
        socket = tree.interface.new_socket(name=label, in_out="INPUT", socket_type="NodeSocketFloat")
        socket.default_value = value
        socket.description = description
        socket.min_value = -1.0 if label == "Face/View Dot" else 0.0
        socket.max_value = 1.0
    for label in OUTPUTS:
        tree.interface.new_socket(name=label, in_out="OUTPUT", socket_type="NodeSocketFloat")
    entry = node(tree, "NodeGroupInput", "Supplied raw screen shadow and source controls", SECTION)
    output = node(tree, "NodeGroupOutput", "Source-conditioned Skin visibility", SECTION)
    tree["scope"] = "Compiled Skin screen-mask consumer arithmetic only; renderer sampling, atlas production, caster selection and dynamic occlusion are unimplemented."
    return tree, entry, output


def _outputs(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node, output: bpy.types.Node, local: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    directional = scalar_mix(tree, entry.outputs["Directional Strength"], 1.0, entry.outputs["Raw Screen Shadow R"], "Source directional strength applied to R")
    cast = scalar_mix(tree, entry.outputs["Ignore Main Shadow"], directional, 1.0, "Source ignore-main-shadow applied after R")
    shadow_a = math_node(tree, "MULTIPLY", entry.outputs["Base Alpha"], local, "Source base alpha times local visibility", SECTION)
    shadow_b = math_node(tree, "MINIMUM", math_node(tree, "MINIMUM", local, entry.outputs["Base Alpha"], "Source local and alpha minimum", SECTION), entry.outputs["Ramp Alpha"], "Source Shadow B including ramp alpha", SECTION)
    for label, value in (("Cast Shadow", cast), ("Raw Local Visibility", entry.outputs["Raw Screen Shadow G"]), ("Local Visibility", local), ("Shadow A", shadow_a), ("Shadow B", shadow_b)):
        tree.links.new(value, output.inputs[label])
    for item in tree.nodes:
        item["endfield_layout_section"] = SECTION
    return shadow_a


def build_body_skin_shadow(name: str) -> bpy.types.ShaderNodeTree:
    """Build no-SDF body consumption; source G enters local visibility directly."""
    tree, entry, output = _interface(name, ())
    shadow_a = _outputs(tree, entry, output, entry.outputs["Raw Screen Shadow G"])
    tree.links.new(shadow_a, output.inputs["Ambient Shadow A"])
    tree["source_branch"] = "Skin no-SDF body; 0125 fragment lines 40353-40373"
    return tree


def build_face_skin_shadow(name: str) -> bpy.types.ShaderNodeTree:
    """Build SDF face consumption with source MaskG/MaskB and camera-XZ gate."""
    tree, entry, output = _interface(name, (
        ("Mask G", 1.0, "Source SDFMask.g receiver/normal blend channel."),
        ("Mask B", 0.0, "Source SDFMask.b camera-gated receiver channel."),
        ("Face/View Dot", 1.0, "Camera-frame +Z face-local horizontal cosine: dot(camera, FaceForward) / length(dot(camera, FaceRight), dot(camera, FaceForward)); not surface NdotV."),
    ))
    view_gate = smoothstep(tree, 0.75, 0.25, entry.outputs["Face/View Dot"], "Source reversed-edge camera receiver gate")
    mask_b = math_node(tree, "MULTIPLY", entry.outputs["Mask B"], view_gate, "Source Mask B camera gate", SECTION)
    gate = math_node(tree, "MAXIMUM", entry.outputs["Mask G"], mask_b, "Source receiver gate maximum", SECTION)
    local = scalar_mix(tree, gate, 1.0, entry.outputs["Raw Screen Shadow G"], "Source face local visibility from independent G")
    ambient_local = scalar_mix(tree, entry.outputs["Mask G"], 1.0, local, "Source SDF ambient Mask G conditioning")
    ambient_a = math_node(tree, "MULTIPLY", entry.outputs["Base Alpha"], ambient_local, "Source SDF ambient alpha weight", SECTION)
    tree.links.new(ambient_a, output.inputs["Ambient Shadow A"])
    _outputs(tree, entry, output, local)
    tree["source_branch"] = "Skin SDF face; 0269 fragment lines 84830-84834, 85102-85144"
    return tree
