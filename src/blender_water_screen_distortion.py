"""Source-style water distortion through an explicit local-bottom host approximation.

The game offsets a projected background sample, rather than refracting through a
flat glass interface. A virtual transmission normal aims Blender's existing
refraction closure at that offset. Reflection normals and transport are retained.
Native depth-buffer rejection and nonlocal bottom intersections are not emulated.
"""
from __future__ import annotations

import math

import bpy

from blender_scene_effect_layers import _component, _math, _scale, _vec
from blender_scene_materials import layout_shader_node_tree


def _clamp(tree: bpy.types.NodeTree, name: str, value: bpy.types.NodeSocket) -> bpy.types.NodeSocket:
    return _math(tree, name + " upper", "MINIMUM", 1.0,
                 _math(tree, name + " lower", "MAXIMUM", 0.0, value))


def water_screen_distortion_group() -> bpy.types.NodeTree:
    """Expose projection choice and the mathematical domain alongside the normal.

Depth zero has zero displacement. Positive depth assumes an above-water view
and a locally horizontal bottom. Perspective=0 removes the perspective expansion
for orthographic cameras; this explicit host control must match the active view.
"""
    name = "EF_Water_Host_Screen_Distortion_v2"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_contract") != "HostWaterScreenDistortion/2":
            raise ValueError(f"Conflicting water distortion node group: {name}")
        return existing
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    tree["endfield_contract"] = "HostWaterScreenDistortion/2"
    for label in ("Wave Normal", "To Viewer", "Window"):
        tree.interface.new_socket(name=label, in_out="INPUT", socket_type="NodeSocketVector")
    for label in ("Depth", "View Distance", "Source Distortion", "Host IOR", "Host Perspective"):
        tree.interface.new_socket(name=label, in_out="INPUT", socket_type="NodeSocketFloat")
    tree.interface.new_socket(name="Transmission Normal", in_out="OUTPUT", socket_type="NodeSocketVector")
    tree.interface.new_socket(name="Valid Domain", in_out="OUTPUT", socket_type="NodeSocketFloat")
    entry = tree.nodes.new("NodeGroupInput").outputs
    horizontal_scale = tree.nodes.new("ShaderNodeCombineXYZ")
    for axis in ("X", "Y"):
        tree.links.new(entry["Source Distortion"], horizontal_scale.inputs[axis])
    horizontal_scale.inputs["Z"].default_value = 1.0
    scaled = _vec(tree, "Source wave scaling", "MULTIPLY", entry["Wave Normal"], horizontal_scale.outputs[0])
    normalized = _vec(tree, "Source scaled normal", "NORMALIZE", scaled, (0., 0., 0.))
    horizontal = _vec(tree, "Source horizontal offset", "MULTIPLY", normalized, (1., 1., 0.))

    ndc = _vec(tree, "Host window to NDC", "SUBTRACT",
               _scale(tree, "Window twice", entry["Window"], 2.0), (1., 1., 0.))
    edges = [_math(tree, "Source screen margin " + axis, "SUBTRACT", 1.0,
                   _math(tree, "Source NDC magnitude " + axis, "ABSOLUTE",
                         _component(tree, "Source NDC " + axis, ndc, axis), 0.0)) for axis in ("X", "Y")]
    edge = _math(tree, "Source closest screen margin", "MINIMUM", edges[0], edges[1])
    far = _clamp(tree, "Source far edge fade", _math(tree, "Source far fade scale", "MULTIPLY", 5.0,
                 _math(tree, "Source far margin", "SUBTRACT", 0.19999998807907104, edge)))
    near = _clamp(tree, "Source near edge fade", _math(tree, "Source near margin", "SUBTRACT", 1.25, edge))
    # The source orthographic view vector is a unit view-matrix axis, not C-P.
    fade_distance = _math(tree, "Source view-vector length", "ADD", 1.0,
                          _math(tree, "Perspective view length", "MULTIPLY", entry["Host Perspective"],
                                _math(tree, "View length delta", "SUBTRACT", entry["View Distance"], 1.0)))
    near_weight = _math(tree, "Source near camera weight", "SUBTRACT", 1.0,
                       _clamp(tree, "Source camera blend", _math(tree, "Source camera blend scale", "MULTIPLY", 4.0,
                              _math(tree, "Source camera blend start", "SUBTRACT", fade_distance, 0.75))))
    fade = _math(tree, "Source edge and camera fade", "ADD", far,
                 _math(tree, "Source near fade blend", "MULTIPLY", near_weight,
                       _math(tree, "Source fade difference", "SUBTRACT", near, far)))
    horizontal = _scale(tree, "Source faded offset", horizontal,
                        _math(tree, "Source remaining distortion", "SUBTRACT", 1.0, fade))

    vertical = _component(tree, "Above-water view cosine", entry["To Viewer"], "Z")
    capped = _math(tree, "Source half-meter ray cap", "MINIMUM", 1.0,
                   _math(tree, "Host capped displacement over depth", "DIVIDE",
                         _math(tree, "Half view cosine", "MULTIPLY", 0.5, vertical), entry["Depth"]))
    # Blender Divide(0) returns zero. At depth=0, explicitly select no offset.
    positive_depth = _math(tree, "Nonzero bottom depth", "GREATER_THAN", entry["Depth"], 0.0)
    capped = _math(tree, "Zero-depth displacement", "MULTIPLY", capped, positive_depth)
    expansion = _math(tree, "Host projected depth expansion", "ADD", 1.0,
                       _math(tree, "Host perspective contribution", "MULTIPLY", entry["Host Perspective"],
                             _math(tree, "Host relative bottom depth", "DIVIDE", entry["Depth"],
                                   _math(tree, "Host camera height", "MULTIPLY", entry["View Distance"], vertical))))
    incident = _scale(tree, "Host incident ray", entry["To Viewer"], -1.0)
    target = _vec(tree, "Host local-bottom target", "SUBTRACT", incident,
                  _scale(tree, "Host projected offset", horizontal,
                         _math(tree, "Host displacement weight", "MULTIPLY", capped, expansion)))
    target = _vec(tree, "Host target direction", "NORMALIZE", target, (0., 0., 0.))
    inverse_ior = _math(tree, "Host inverse IOR", "DIVIDE", 1.0, entry["Host IOR"])
    effective = _vec(tree, "Inverse Snell normal", "SUBTRACT",
                     _scale(tree, "Incident over host IOR", incident, inverse_ior), target)
    effective = _vec(tree, "Unit transmission normal", "NORMALIZE", effective, (0., 0., 0.))
    dot = tree.nodes.new("ShaderNodeVectorMath")
    dot.operation = "DOT_PRODUCT"
    tree.links.new(incident, dot.inputs[0])
    tree.links.new(target, dot.inputs[1])
    reachable = _math(tree, "Host incident Snell branch", "GREATER_THAN", dot.outputs["Value"], inverse_ior)
    valid = _math(tree, "Host above-water domain", "MULTIPLY", reachable,
                  _math(tree, "Camera above water", "GREATER_THAN", vertical, 0.0))
    valid = _math(tree, "Host positive camera distance", "MULTIPLY", valid,
                  _math(tree, "Camera distance positive", "GREATER_THAN", entry["View Distance"], 0.0))
    valid = _math(tree, "Host nonnegative bottom depth", "MULTIPLY", valid,
                  _math(tree, "Bottom depth nonnegative", "SUBTRACT", 1.0,
                        _math(tree, "Bottom depth negative", "LESS_THAN", entry["Depth"], 0.0)))
    projection_valid = _math(tree, "Host supported projection", "ADD",
                             _math(tree, "Orthographic mode", "COMPARE", entry["Host Perspective"], 0.0),
                             _math(tree, "Perspective mode", "COMPARE", entry["Host Perspective"], 1.0))
    for label in ("Orthographic mode", "Perspective mode"):
        tree.nodes[label].inputs[2].default_value = 0.0
    valid = _math(tree, "Host projection domain", "MULTIPLY", valid, projection_valid)
    output = tree.nodes.new("NodeGroupOutput")
    tree.links.new(effective, output.inputs["Transmission Normal"])
    tree.links.new(valid, output.inputs["Valid Domain"])
    for node in tree.nodes:
        node["endfield_layout_section"] = "10 Source Distortion"
    layout_shader_node_tree(tree)
    return tree


def water_camera_perspective(camera: bpy.types.Object | None) -> float:
    """Use perspective for camera-free exports; reject unsupported render projections."""
    if camera is None:
        return 1.0
    if camera.type != "CAMERA" or camera.data.type not in {"PERSP", "ORTHO"}:
        raise ValueError(f"Water screen distortion requires PERSP or ORTHO camera: {camera.name}")
    return float(camera.data.type == "PERSP")


def create_water_screen_distortion(material: bpy.types.Material, normal: bpy.types.NodeSocket,
                                    depth: bpy.types.NodeSocket, distortion: float,
                                    ior: float, perspective: float) -> bpy.types.Node:
    """Build the shared transmission direction without replacing optical closures."""
    tree = material.node_tree
    if tree is None or normal.node.id_data != tree or depth.node.id_data != tree:
        raise ValueError("Water distortion inputs must belong to the material")
    if not math.isfinite(distortion) or distortion < 0:
        raise ValueError(f"Water screen distortion must be finite and nonnegative: {distortion}")
    if not math.isfinite(ior) or ior <= 1:
        raise ValueError(f"Water screen distortion host IOR must exceed one: {ior}")
    if perspective not in (0.0, 1.0):
        raise ValueError(f"Water screen distortion projection must be 0 (ORTHO) or 1 (PERSP): {perspective}")
    if material.get("endfield_screen_distortion_contract") is not None:
        raise ValueError("Water source-style screen distortion is already connected")
    start = set(tree.nodes)
    node = tree.nodes.new("ShaderNodeGroup")
    node.node_tree = water_screen_distortion_group()
    node.name = "EF_Water_Source_Screen_Distortion"
    node.label = "Above-water / Host Perspective: 0 ortho, 1 perspective"
    node.inputs["Host IOR"].default_value = ior
    node.inputs["Source Distortion"].default_value = distortion
    node.inputs["Host Perspective"].default_value = perspective
    tree.links.new(normal, node.inputs["Wave Normal"])
    tree.links.new(depth, node.inputs["Depth"])
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    camera = tree.nodes.new("ShaderNodeCameraData")
    coordinates = tree.nodes.new("ShaderNodeTexCoord")
    tree.links.new(geometry.outputs["Incoming"], node.inputs["To Viewer"])
    tree.links.new(camera.outputs["View Distance"], node.inputs["View Distance"])
    tree.links.new(coordinates.outputs["Window"], node.inputs["Window"])
    for item in set(tree.nodes) - start:
        item["endfield_layout_section"] = "10 Source Distortion"
    material["endfield_screen_distortion_contract"] = "HostWaterScreenDistortion/2"
    material["endfield_screen_distortion_boundary"] = (
        "Source-style offset and view fade, measured vertical depth/local horizontal bottom, "
        "above-water views within the incident Snell branch. Host Perspective is explicit: "
        "1 for perspective, 0 for orthographic; panorama is unsupported. Orthographic fade uses "
        "the unit view-axis length. Update Host Perspective when switching the active view. "
        "Native depth-buffer taps, invalid-hit rejection, "
        "underwater views and non-camera rays are not reconstructed; Valid Domain exposes the ray constraint. "
        "Nonzero source roughness retains a host lobe around the computed central ray.")
    layout_shader_node_tree(tree)
    return node


def connect_water_screen_distortion(material: bpy.types.Material, normal: bpy.types.NodeSocket,
                                     depth: bpy.types.NodeSocket, distortion: float,
                                     perspective: float) -> bpy.types.Node:
    """Keep Fresnel/reflection intact and replace only the two background ray normals."""
    tree = material.node_tree
    if tree is None:
        raise ValueError("Water distortion requires a material node tree")
    labels = {"Single boundary: source transmission applied once",
              "Host refracted background before source water coverage"}
    refractions = [node for node in tree.nodes if node.type == "BSDF_REFRACTION" and node.label in labels]
    if len(refractions) != 2 or {node.label for node in refractions} != labels:
        raise ValueError("Source-style distortion requires both transport and edge background refractions")
    iors = {float(node.inputs["IOR"].default_value) for node in refractions}
    if len(iors) != 1 or not all(math.isfinite(value) and value > 1 for value in iors):
        raise ValueError(f"Source-style distortion requires equal host IOR values above one: {iors}")
    ior = next(iter(iors))
    node = tree.nodes.get("EF_Water_Source_Screen_Distortion")
    if node is None:
        node = create_water_screen_distortion(material, normal, depth, distortion, ior, perspective)
    else:
        if (node.type != "GROUP" or node.node_tree is None
                or node.node_tree.get("endfield_contract") != "HostWaterScreenDistortion/2"
                or material.get("endfield_screen_distortion_contract") != "HostWaterScreenDistortion/2"):
            raise ValueError("Existing portable water distortion has an incompatible contract")
        for label, source in (("Wave Normal", normal), ("Depth", depth)):
            links = node.inputs[label].links
            if len(links) != 1 or links[0].from_socket != source:
                raise ValueError(f"Portable water distortion input differs from source transport: {label}")
        for label, value in (("Source Distortion", distortion), ("Host Perspective", perspective)):
            socket = node.inputs[label]
            if socket.is_linked or not math.isclose(socket.default_value, value, rel_tol=1e-6, abs_tol=1e-7):
                raise ValueError(f"Portable water distortion setting differs from source transport: {label}")
        ior_input = node.inputs["Host IOR"]
        if ior_input.is_linked:
            link = ior_input.links[0]
            if link.from_node != tree.nodes.get("EF_Water_Shared_Settings") or link.from_socket.name != "IOR":
                raise ValueError("Portable water distortion IOR must use shared water settings")
            tree.links.remove(link)
        elif not math.isclose(ior_input.default_value, ior, rel_tol=1e-6, abs_tol=1e-7):
            raise ValueError("Portable water distortion IOR differs from source transport")
        # The transport upgrade removes the portable settings node after this call.
        ior_input.default_value = ior
    for refraction in refractions:
        tree.links.new(node.outputs["Transmission Normal"], refraction.inputs["Normal"])
    layout_shader_node_tree(tree)
    return node
