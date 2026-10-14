"""Analytic HGRP water transport nodes with explicit Blender host-input boundaries."""
from __future__ import annotations

import math

import bpy

from blender_scene_effect_layers import _math, _vec, _scale
from blender_scene_materials import layout_shader_node_tree
from endfield_water_transport import WaterAmbientL1, WaterTransport, validate_water_ambient_l1


def connect_water_transport_ambient_l1(transport: bpy.types.Node, normal: bpy.types.NodeSocket,
                                      coefficients: WaterAmbientL1, environment_scale: float) -> None:
    """Bind an explicit default-SH buffer to the existing transport ambient input.

    Source axes are (Blender X, Blender Z, -Blender Y). The supplied world normal
    retains the water's existing normal mapping. Local irradiance grids, their
    occlusion blend and automatic runtime path selection remain outside this API.
    """
    validate_water_ambient_l1(coefficients, environment_scale)
    if (transport.type != "GROUP" or transport.node_tree is None
            or transport.node_tree.get("endfield_contract") != "SourceWaterTransport/1"):
        raise ValueError("Water ambient L1 requires a SourceWaterTransport/1 node")
    tree = transport.id_data
    if normal.node.id_data != tree or transport.inputs["HOST Ambient Radiance"].is_linked:
        raise ValueError("Water ambient L1 requires a local normal and an unbound ambient input")
    frame = tree.nodes.new("NodeFrame")
    frame.label = "Source sky L1 / explicit buffer / local irradiance volumes excluded"
    frame.name = "EF_Water_Source_Ambient_L1"
    existing = set(tree.nodes)
    separate = tree.nodes.new("ShaderNodeSeparateXYZ")
    separate.label = "World normal: Blender Z-up"
    tree.links.new(normal, separate.inputs[0])
    source_normal = tree.nodes.new("ShaderNodeCombineXYZ")
    source_normal.label = "Source normal: Y-up"
    tree.links.new(separate.outputs["X"], source_normal.inputs["X"])
    tree.links.new(separate.outputs["Z"], source_normal.inputs["Y"])
    tree.links.new(_math(tree, "Source normal Z", "MULTIPLY", separate.outputs["Y"], -1.0),
                   source_normal.inputs["Z"])
    rgb = tree.nodes.new("ShaderNodeCombineXYZ")
    rgb.label = "Source nonnegative ambient RGB"
    for index, channel in enumerate(coefficients):
        dot = tree.nodes.new("ShaderNodeVectorMath")
        dot.operation = "DOT_PRODUCT"
        dot.label = f"{'RGB'[index]} source directional SH"
        tree.links.new(source_normal.outputs[0], dot.inputs[0])
        dot.inputs[1].default_value = channel[:3]
        value = _math(tree, f"{'RGB'[index]} source constant SH", "ADD", dot.outputs["Value"], channel[3])
        value = _math(tree, f"{'RGB'[index]} source ambient clamp", "MAXIMUM", value, 0.0)
        tree.links.new(value, rgb.inputs[index])
    scaled = _scale(tree, "Source environment global multiplier", rgb.outputs[0], environment_scale)
    tree.links.new(scaled, transport.inputs["HOST Ambient Radiance"])
    for node in set(tree.nodes) - existing:
        node.parent = frame
        node["endfield_layout_section"] = "03 Source Sky Irradiance"
    transport["endfield_ambient_l1_coefficients"] = [value for channel in coefficients for value in channel]
    transport["endfield_ambient_l1_environment_scale"] = environment_scale
    transport["endfield_ambient_l1_boundary"] = "Explicit selected L1 buffer; native local irradiance volume branch excluded"
    layout_shader_node_tree(tree)


def _append_phase_atmosphere_candidate(
    tree: bpy.types.NodeTree, entry: bpy.types.Node, output: bpy.types.Node,
) -> None:
    """Add a separate source-phase RGB path without changing the host-Sun result."""
    input_exists = entry.outputs.get("Phase Atmosphere RGB") is not None
    output_exists = output.inputs.get("Atmosphere Phase Candidate") is not None
    if input_exists and output_exists:
        _set_phase_candidate_layout(tree)
        return
    if input_exists or output_exists:
        raise ValueError(f"Water transport group has a partial phase-RGB extension: group={tree.name}")
    phase_input = tree.interface.new_socket(
        name="Phase Atmosphere RGB", in_out="INPUT", socket_type="NodeSocketColor")
    phase_input.default_value = (0.0, 0.0, 0.0, 1.0)
    tree.interface.new_socket(
        name="Atmosphere Phase Candidate", in_out="OUTPUT", socket_type="NodeSocketColor")
    phase_node = tree.nodes.get("Source phase mixture")
    ambient_node = tree.nodes.get("Source environment scale")
    direct_integrals = [node for node in tree.nodes if node.bl_idname == "ShaderNodeCombineXYZ"
                        and all(len(node.inputs[index].links) == 1
                                and node.inputs[index].links[0].from_node.name
                                == f"{channel}Direct source integral"
                                for index, channel in enumerate(("R", "G", "B")))]
    if phase_node is None or ambient_node is None or len(direct_integrals) != 1:
        raise ValueError(f"Water transport source math is incomplete for phase RGB: group={tree.name}")
    source = entry.outputs
    direct = _vec(tree, "Phase candidate atmosphere RGB integral", "MULTIPLY",
                  direct_integrals[0].outputs[0], source["Phase Atmosphere RGB"])
    direct = _scale(tree, "Phase candidate angular phase", direct, phase_node.outputs[0])
    combined = _vec(tree, "Phase candidate ambient and direct", "ADD", ambient_node.outputs[0], direct)
    scattered = _vec(tree, "Phase candidate scattering coefficient", "MULTIPLY", source["Scattering"], combined)
    tree.links.new(scattered, output.inputs["Atmosphere Phase Candidate"])
    _set_phase_candidate_layout(tree)
    tree["endfield_phase_atmosphere_candidate_revision"] = 1


def _set_phase_candidate_layout(tree: bpy.types.NodeTree) -> None:
    """Keep the additive candidate math together in the source-transport layout."""
    names = (
        "Phase candidate atmosphere RGB integral",
        "Phase candidate angular phase",
        "Phase candidate ambient and direct",
        "Phase candidate scattering coefficient",
    )
    nodes = tuple(tree.nodes.get(name) for name in names)
    if any(node is None for node in nodes):
        raise ValueError(f"Water transport phase candidate nodes are incomplete: group={tree.name}")
    for node in nodes:
        node["endfield_layout_section"] = "02 Source Transport Math"
        node["endfield_phase_atmosphere_candidate_node"] = True


def _refracted_background_weight(
    tree: bpy.types.NodeTree, source: bpy.types.NodeOutputs,
    ambient_integral: bpy.types.NodeSocket, direct_phase: bpy.types.NodeSocket,
    transmission: bpy.types.NodeSocket, label: str,
) -> bpy.types.NodeSocket:
    """Factor source scene RGB out of scattering for the host refraction closure."""
    ambient = _scale(tree, label + " ambient scene integral", ambient_integral,
                     _math(tree, label + " environment scale", "MULTIPLY",
                           source["Environment Intensity"], 0.7170000076293945))
    coefficient = _vec(tree, label + " scene scattering", "MULTIPLY", source["Scattering"],
                       _vec(tree, label + " scene integrals", "ADD", ambient, direct_phase))
    weighted = _scale(tree, label + " scene influence", coefficient, source["Scene Color Env Influence"])
    return _vec(tree, label + " refracted background", "ADD", transmission, weighted)


def water_transport_group() -> bpy.types.NodeTree:
    """Expose source math separately from scene-depth, light selection and buffer reconstruction."""
    name = "EF_Water_Source_Transport_v2"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_contract") != "SourceWaterTransport/1":
            raise ValueError(f"Water transport group name conflicts: group={name}")
        entries = [node for node in existing.nodes if node.type == "GROUP_INPUT"]
        outputs = [node for node in existing.nodes if node.type == "GROUP_OUTPUT" and node.is_active_output]
        if len(entries) != 1 or len(outputs) != 1:
            raise ValueError(f"Water transport group is missing its single active boundary: group={name}")
        _append_phase_atmosphere_candidate(existing, entries[0], outputs[0])
        layout_shader_node_tree(existing)
        return existing
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    tree["endfield_contract"] = "SourceWaterTransport/1"
    for label in ("Depth", "View Y", "Light Y", "Phase Cosine", "Safe Distance", "Phase Mix", "Environment Intensity",
                  "Scene Color Env Influence"):
        tree.interface.new_socket(name=label, in_out="INPUT", socket_type="NodeSocketFloat")
    for label in ("Absorption Near", "Absorption Far", "Scattering", "HOST Ambient Radiance", "HOST Direct Radiance"):
        tree.interface.new_socket(name=label, in_out="INPUT", socket_type="NodeSocketColor")
    for label in ("Transmission", "In-scattering", "Refracted Background Weight", "Phase Refracted Background Weight"):
        tree.interface.new_socket(name=label, in_out="OUTPUT", socket_type="NodeSocketColor")
    entry = tree.nodes.new("NodeGroupInput")
    entry["endfield_layout_section"] = "01 Source and Host Inputs"
    source = entry.outputs
    depth = _math(tree, "Source depth cap 50 m", "MINIMUM", source["Depth"], 50.0)
    ratio = _math(tree, "Depth interpolation", "MINIMUM",
                  _math(tree, "Depth per safe distance", "DIVIDE", depth, source["Safe Distance"]), 1.0)
    delta = _vec(tree, "Absorption delta", "SUBTRACT", source["Absorption Far"], source["Absorption Near"])
    absorption = _vec(tree, "Depth-dependent absorption", "ADD", source["Absorption Near"],
                      _scale(tree, "Weighted absorption delta", delta, ratio))
    extinction = _vec(tree, "Source extinction per meter", "ADD", absorption, source["Scattering"])
    phase_base = _math(tree, "Source Mie denominator base", "SUBTRACT", 1.728933334350586,
                       _math(tree, "Source phase cosine factor", "MULTIPLY", source["Phase Cosine"], 1.707551956176758))
    denominator = _math(tree, "Source Mie denominator", "MAXIMUM", 0.0010000000474974513,
                        _math(tree, "Source Mie solid angle", "MULTIPLY", 12.566370964050293,
                              _math(tree, "Source Mie exponent", "POWER", phase_base, 1.5)))
    mie = _math(tree, "Source Mie phase", "DIVIDE", 0.2710666060447693, denominator)
    cosine_squared = _math(tree, "Phase cosine squared", "MULTIPLY", source["Phase Cosine"], source["Phase Cosine"])
    rayleigh = _math(tree, "Source Rayleigh phase", "MULTIPLY", 0.05968310311436653,
                     _math(tree, "Rayleigh angular term", "ADD", 1.0, cosine_squared))
    phase = _math(tree, "Source phase mixture", "ADD", mie,
                  _math(tree, "Weighted phase difference", "MULTIPLY", source["Phase Mix"],
                        _math(tree, "Phase difference", "SUBTRACT", rayleigh, mie)))
    ambient_factor = _math(tree, "Ambient path factor", "ADD", 1.0,
                           _math(tree, "Ambient view magnitude", "ABSOLUTE",
                                 _math(tree, "Ambient view scale", "MULTIPLY", source["View Y"], 1.3940000534057617), 0.0))
    direct_factor = _math(tree, "Directional path factor", "ADD", 1.0,
                          _math(tree, "Directional view magnitude", "ABSOLUTE",
                                _math(tree, "View over light elevation", "DIVIDE", source["View Y"],
                                      _math(tree, "Source minimum light elevation", "MAXIMUM", source["Light Y"], 0.05000000074505806)), 0.0))
    extinction_channels = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(extinction, extinction_channels.inputs[0])
    transmission = tree.nodes.new("ShaderNodeCombineXYZ")
    ambient_integral = tree.nodes.new("ShaderNodeCombineXYZ")
    direct_integral = tree.nodes.new("ShaderNodeCombineXYZ")
    for index, channel in enumerate(("R", "G", "B")):
        sigma = extinction_channels.outputs[index]
        exponential = _math(tree, channel + " transmission", "EXPONENT",
                             _math(tree, channel + " optical depth", "MULTIPLY", sigma,
                                   _math(tree, channel + " negative depth", "MULTIPLY", depth, -1.0)), 0.0)
        tree.links.new(exponential, transmission.inputs[index])
        for label, factor, destination in (("Ambient", ambient_factor, ambient_integral),
                                           ("Direct", direct_factor, direct_integral)):
            rate = _math(tree, channel + label + " rate", "MULTIPLY", sigma, factor)
            attenuation = _math(tree, channel + label + " attenuation", "EXPONENT",
                                _math(tree, channel + label + " optical depth", "MULTIPLY", rate,
                                      _math(tree, channel + label + " negative depth", "MULTIPLY", depth, -1.0)), 0.0)
            integral = _math(tree, channel + label + " source integral", "DIVIDE",
                             _math(tree, channel + label + " removed fraction", "SUBTRACT", 1.0, attenuation), rate)
            tree.links.new(integral, destination.inputs[index])
    ambient = _vec(tree, "Ambient integral times host radiance", "MULTIPLY", ambient_integral.outputs[0], source["HOST Ambient Radiance"])
    ambient = _scale(tree, "Source environment scale", ambient,
                     _math(tree, "Source environment intensity", "MULTIPLY", source["Environment Intensity"], 0.7170000076293945))
    direct = _vec(tree, "Direct integral times host radiance", "MULTIPLY", direct_integral.outputs[0], source["HOST Direct Radiance"])
    direct = _scale(tree, "Directional scattering phase", direct, phase)
    scattered = _vec(tree, "Source scattering coefficient", "MULTIPLY", source["Scattering"],
                     _vec(tree, "Ambient and direct scattering", "ADD", ambient, direct))
    output = tree.nodes.new("NodeGroupOutput")
    tree.links.new(transmission.outputs[0], output.inputs["Transmission"])
    tree.links.new(scattered, output.inputs["In-scattering"])
    _append_phase_atmosphere_candidate(tree, entry, output)
    for label, direct_source, destination in (
            ("Host", direct, "Refracted Background Weight"),
            ("Phase", tree.nodes["Phase candidate angular phase"].outputs[0], "Phase Refracted Background Weight")):
        tree.links.new(_refracted_background_weight(
            tree, source, ambient_integral.outputs[0], direct_source, transmission.outputs[0], label),
            output.inputs[destination])
    tree["endfield_scene_color_contract"] = "SourceSceneColorFactoredHostRefraction/1"
    output["endfield_layout_section"] = "03 Transport Outputs"
    for node in tree.nodes:
        if "endfield_layout_section" not in node:
            node["endfield_layout_section"] = "02 Source Transport Math"
    layout_shader_node_tree(tree)
    return tree


def bind_water_transport(tree: bpy.types.NodeTree, settings: WaterTransport) -> bpy.types.Node:
    """Create a source-configured node; every host input remains explicit and unconnected."""
    node = tree.nodes.new("ShaderNodeGroup")
    node.node_tree = water_transport_group()
    node.name = node.label = "Source Water Transport / Host Lighting"
    for label, value in (("Absorption Near", settings.absorption_near), ("Absorption Far", settings.absorption_far),
                         ("Scattering", settings.scattering)):
        node.inputs[label].default_value = (*value, 1.0)
    for label, value in (("Safe Distance", settings.safe_distance), ("Phase Mix", settings.phase_mix),
                         ("Environment Intensity", settings.environment_intensity),
                         ("Scene Color Env Influence", settings.scene_color_environment_influence)):
        node.inputs[label].default_value = value
    node.inputs["Phase Atmosphere RGB"].default_value = (0.0, 0.0, 0.0, 1.0)
    return node


def connect_water_transport_phase_atmosphere(
    transport: bpy.types.Node, phase_atmosphere_rgb: tuple[float, float, float],
) -> None:
    """Opt into the separate atmosphere-phase RGB candidate, never using Sun color as fallback."""
    if (transport.type != "GROUP" or transport.node_tree is None
            or transport.node_tree.get("endfield_contract") != "SourceWaterTransport/1"):
        raise ValueError("Water phase RGB requires a SourceWaterTransport/1 node")
    if (len(phase_atmosphere_rgb) != 3
            or any(not math.isfinite(value) or value < 0 for value in phase_atmosphere_rgb)):
        raise ValueError(f"Phase atmosphere RGB must be finite and nonnegative: {phase_atmosphere_rgb}")
    if (transport.inputs.get("Phase Atmosphere RGB") is None
            or transport.outputs.get("Atmosphere Phase Candidate") is None):
        raise ValueError("Water transport group has no phase atmosphere RGB candidate sockets")
    tree = transport.id_data
    emission_nodes = [node for node in tree.nodes
                      if node.type == "EMISSION" and node.label == "Source in-scattering radiance"]
    if len(emission_nodes) != 1:
        raise ValueError(f"Water phase RGB requires one source in-scattering node: matches={len(emission_nodes)}")
    refractions = [node for node in tree.nodes if node.type == "BSDF_REFRACTION"
                   and node.label == "Single boundary: source transmission applied once"]
    if len(refractions) != 1 or transport.outputs.get("Phase Refracted Background Weight") is None:
        raise ValueError("Water phase RGB requires its single background refraction bridge")
    transport.inputs["Phase Atmosphere RGB"].default_value = (*phase_atmosphere_rgb, 1.0)
    color = emission_nodes[0].inputs["Color"]
    for link in tuple(color.links):
        tree.links.remove(link)
    tree.links.new(transport.outputs["Atmosphere Phase Candidate"], color)
    tree.links.new(transport.outputs["Phase Refracted Background Weight"], refractions[0].inputs["Color"])
    transport["endfield_phase_atmosphere_rgb_origin"] = (
        "Explicit prepared-scene phase_atmosphere_rgb input; per-scene native value not captured")
    transport["endfield_phase_candidate_boundary"] = (
        "Approximate host transport branch based on the native phase RGB pathway; not native render parity")


def connect_water_transport_direction(
    transport: bpy.types.Node, light_forward_world: tuple[float, float, float],
) -> None:
    """Bind an already converted light-forward vector in Blender's Z-up world.

    The source light buffer stores forward, whereas the separate lighting-pass
    direction helper returns its negation. Geometry Incoming supplies the view
    toward the camera; native refracted-background view reconstruction is absent.
    """
    if (transport.type != "GROUP" or transport.node_tree is None
            or transport.node_tree.get("endfield_contract") != "SourceWaterTransport/1"):
        raise ValueError("Water direction requires a SourceWaterTransport/1 node")
    if (len(light_forward_world) != 3
            or any(not math.isfinite(value) for value in light_forward_world)
            or not math.isclose(sum(value * value for value in light_forward_world), 1.0,
                                rel_tol=0.0, abs_tol=1e-5)):
        raise ValueError(f"Water light-forward vector must be finite and unit length: {light_forward_world}")
    tree = transport.id_data
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    geometry.label = "Host view toward camera / native background view approximation"
    separate = tree.nodes.new("ShaderNodeSeparateXYZ")
    separate.label = "Source Y-up elevation expressed as Blender Z"
    tree.links.new(geometry.outputs["Incoming"], separate.inputs[0])
    tree.links.new(separate.outputs["Z"], transport.inputs["View Y"])
    for link in tuple(transport.inputs["Light Y"].links):
        tree.links.remove(link)
    transport.inputs["Light Y"].default_value = light_forward_world[2]
    phase = tree.nodes.new("ShaderNodeVectorMath")
    phase.operation = "DOT_PRODUCT"
    phase.label = "Source phase: outgoing view dot light forward"
    tree.links.new(geometry.outputs["Incoming"], phase.inputs[0])
    phase.inputs[1].default_value = light_forward_world
    tree.links.new(phase.outputs["Value"], transport.inputs["Phase Cosine"])
    transport["endfield_direction_contract"] = "SourceLightForwardBlenderZUp/1"
    transport["endfield_light_forward_world"] = light_forward_world


def connect_water_transport_surface(
    material: bpy.types.Material, transport: bpy.types.Node,
    normal: bpy.types.NodeSocket, ior: float, roughness: float,
) -> None:
    """Compose source radiance at one open boundary with explicit Eevee zero thickness."""
    tree = material.node_tree
    if tree is None or transport.id_data != tree or normal.node.id_data != tree:
        raise ValueError("Water transport and normal must belong to the target material")
    if (transport.type != "GROUP" or transport.node_tree is None
            or transport.node_tree.get("endfield_contract") != "SourceWaterTransport/1"):
        raise ValueError("Water surface requires a SourceWaterTransport/1 node")
    if transport.outputs.get("Refracted Background Weight") is None:
        raise ValueError("Water surface requires the scene-color transport revision; rebuild with bind_water_transport")
    if not math.isfinite(ior) or ior < 1 or not math.isfinite(roughness) or not 0 <= roughness <= 1:
        raise ValueError(f"Invalid water host optics: ior={ior}, roughness={roughness}")
    refraction = tree.nodes.new("ShaderNodeBsdfRefraction")
    refraction.label = "Single boundary: source transmission applied once"
    refraction.inputs["IOR"].default_value = ior
    refraction.inputs["Roughness"].default_value = roughness
    tree.links.new(normal, refraction.inputs["Normal"])
    tree.links.new(transport.outputs["Refracted Background Weight"], refraction.inputs["Color"])
    scattering = tree.nodes.new("ShaderNodeEmission")
    scattering.label = "Source in-scattering radiance"
    tree.links.new(transport.outputs["In-scattering"], scattering.inputs["Color"])
    transmitted = tree.nodes.new("ShaderNodeAddShader")
    tree.links.new(refraction.outputs[0], transmitted.inputs[0])
    tree.links.new(scattering.outputs[0], transmitted.inputs[1])
    reflection = tree.nodes.new("ShaderNodeBsdfGlossy")
    reflection.inputs["Color"].default_value = (1, 1, 1, 1)
    reflection.inputs["Roughness"].default_value = roughness
    tree.links.new(normal, reflection.inputs["Normal"])
    fresnel = tree.nodes.new("ShaderNodeFresnel")
    fresnel.inputs["IOR"].default_value = ior
    tree.links.new(normal, fresnel.inputs["Normal"])
    surface = tree.nodes.new("ShaderNodeMixShader")
    tree.links.new(fresnel.outputs[0], surface.inputs[0])
    tree.links.new(transmitted.outputs[0], surface.inputs[1])
    tree.links.new(reflection.outputs[0], surface.inputs[2])
    shadow = tree.nodes.new("ShaderNodeLightPath")
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    shadow_adapter = tree.nodes.new("ShaderNodeMixShader")
    shadow_adapter.label = "Host direct illumination passes through the open boundary"
    tree.links.new(shadow.outputs["Is Shadow Ray"], shadow_adapter.inputs[0])
    tree.links.new(surface.outputs[0], shadow_adapter.inputs[1])
    tree.links.new(transparent.outputs[0], shadow_adapter.inputs[2])
    for node in tuple(tree.nodes):
        if node.type == "OUTPUT_MATERIAL":
            tree.nodes.remove(node)
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(shadow_adapter.outputs[0], output.inputs["Surface"])
    thickness = tree.nodes.new("ShaderNodeValue")
    thickness.label = "Open water boundary: explicit zero host thickness"
    thickness.outputs[0].default_value = 0.0
    # An unconnected default is ignored: Eevee estimates bounds and models two interfaces.
    tree.links.new(thickness.outputs[0], output.inputs["Thickness"])
    material.use_raytrace_refraction = True
    material.surface_render_method = "DITHERED"
    material.thickness_mode = "SLAB"
    material["endfield_surface_contract"] = "SourceWaterOpenBoundary/1"
    material["endfield_scene_color_contract"] = "SourceSceneColorFactoredHostRefraction/1"
    material["endfield_scene_color_boundary"] = (
        "Serialized SceneColorEnvInfluence contributes through actual host refracted background radiance; "
        "independent ambient/direct emission is unchanged. Native screen color, caustics and visibility "
        "are approximated by Blender refraction and explicitly supplied host lighting.")
    material["endfield_water_limitations"] = (
        "Explicit host radiance and optical depth; single above-water interface; "
        "Eevee screen rays, source refraction distortion and underwater cameras remain host approximations")
    layout_shader_node_tree(tree)


def connect_water_transport_sun(
    transport: bpy.types.Node, light: bpy.types.Object, radiance_scale: float,
) -> None:
    """Drive the explicit host bridge from one editable Sun using built-in drivers.

    Host energy conversion is explicit; this does not recover native atmosphere
    radiance, runtime light selection or scene-buffer reconstruction.
    """
    if light.type != "LIGHT" or light.data.type != "SUN":
        raise ValueError("Water host lighting requires an explicitly selected Sun")
    if not math.isfinite(radiance_scale) or radiance_scale <= 0:
        raise ValueError(f"Water host radiance scale must be finite and positive: {radiance_scale}")
    if transport.get("endfield_direction_contract") == "LiveHostSunForwardBlenderZUp/1":
        raise ValueError("Water transport already has a live Sun binding")
    forward = tuple(-value for value in light.matrix_world.to_quaternion().to_matrix().col[2])
    connect_water_transport_direction(transport, forward)
    phase = transport.inputs["Phase Cosine"].links[0].from_node
    expressions = ("-(cos(z)*sin(y)*cos(x)+sin(z)*sin(x))",
                   "-(sin(z)*sin(y)*cos(x)-cos(z)*sin(x))", "-cos(y)*cos(x)")
    elevation = transport.id_data.nodes.new("ShaderNodeSeparateXYZ")
    elevation.label = "Live selected Sun forward / source elevation"
    direction = transport.id_data.nodes.new("ShaderNodeCombineXYZ")
    direction.label = "Live selected Sun forward"
    for index in range(3):
        driver = direction.inputs[index].driver_add("default_value").driver
        driver.type, driver.expression = "SCRIPTED", expressions[index]
        for axis in "XYZ":
            variable = driver.variables.new()
            variable.name, variable.type = axis.lower(), "TRANSFORMS"
            target = variable.targets[0]
            target.id, target.transform_type = light, "ROT_" + axis
            target.transform_space, target.rotation_mode = "WORLD_SPACE", "XYZ"
    transport.id_data.links.new(direction.outputs[0], phase.inputs[1])
    transport.id_data.links.new(direction.outputs[0], elevation.inputs[0])
    transport.id_data.links.new(elevation.outputs["Z"], transport.inputs["Light Y"])
    for link in tuple(transport.inputs["HOST Direct Radiance"].links):
        transport.id_data.links.remove(link)
    for index in range(3):
        driver = transport.inputs["HOST Direct Radiance"].driver_add("default_value", index).driver
        driver.type = "SCRIPTED"
        driver.expression = f"color*energy*{radiance_scale!r}*(1-hidden)"
        for name, owner, id_type, path in (("color", light.data, "LIGHT", f"color[{index}]"),
                                           ("energy", light.data, "LIGHT", "energy"),
                                           ("hidden", light, "OBJECT", "hide_render")):
            variable = driver.variables.new()
            variable.name, variable.type = name, "SINGLE_PROP"
            target = variable.targets[0]
            target.id_type, target.id, target.data_path = id_type, owner, path
    transport["endfield_direction_contract"] = "LiveHostSunForwardBlenderZUp/1"
    del transport["endfield_light_forward_world"]
    transport["endfield_host_sun"] = light.name
    transport["endfield_host_radiance_scale"] = radiance_scale
    transport["endfield_host_lighting_boundary"] = (
        "Selected Blender Sun rotation/color/energy and object render visibility; "
        "explicit energy conversion, no native atmosphere buffer or automatic light selection")
    layout_shader_node_tree(transport.id_data)
