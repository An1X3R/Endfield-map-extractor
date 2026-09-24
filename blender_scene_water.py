"""Native surface and closed-volume water using retained wave and depth evidence.

Optical coefficients are explicit Blender calibration, not recovered HGRP values.
Screen buffers, caustics, underwater camera behavior and history remain unverified.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

import bpy
import bmesh

from blender_hgrp_surface import configure_scene_transmission


@dataclass(frozen=True)
class WaterOptics:
    ior: float
    roughness: float
    attenuation_per_meter: float
    scattering_per_meter: float
    deep_color: tuple[float, float, float, float]
    depth_attribute: str


def _validate_optics(optics: WaterOptics) -> None:
    if not isinstance(optics, WaterOptics):
        raise TypeError(f"Expected WaterOptics, received {type(optics).__name__}")
    if (not isinstance(optics.deep_color, (tuple, list)) or len(optics.deep_color) != 4 or
            not all(isinstance(x, (int, float)) and math.isfinite(x) and 0 <= x <= 1 for x in optics.deep_color)):
        raise ValueError(f"Water deep_color must contain four finite channels in [0, 1]: {optics.deep_color}")
    values = (optics.ior, optics.roughness, optics.attenuation_per_meter, optics.scattering_per_meter)
    if (not all(isinstance(x, (int, float)) and math.isfinite(x) for x in values) or optics.ior <= 1 or
            not 0 <= optics.roughness <= 1 or optics.attenuation_per_meter < 0 or optics.scattering_per_meter < 0 or
            not isinstance(optics.depth_attribute, str) or not optics.depth_attribute.strip()):
        raise ValueError(f"Invalid water optics: {optics}")


def _validate_group_interface(tree: bpy.types.ShaderNodeTree,
                              sockets: tuple[tuple[str, str, str], ...]) -> None:
    available = {(item.name, item.in_out, item.socket_type) for item in tree.interface.items_tree if item.item_type == "SOCKET"}
    missing = set(sockets) - available
    if missing:
        raise ValueError(f"Water node group has incompatible interface: group={tree.name}, missing={sorted(missing)}")


def _validate_group_outputs(tree: bpy.types.ShaderNodeTree, labels: tuple[str, ...]) -> None:
    output = next((node for node in tree.nodes if node.type == "GROUP_OUTPUT" and node.is_active_output), None)
    if output is None or any(output.inputs.get(label) is None or not output.inputs[label].is_linked for label in labels):
        raise ValueError(f"Water node group has unconnected outputs: group={tree.name}, required={labels}")


def _validate_settings_values(tree: bpy.types.ShaderNodeTree) -> None:
    output = next(node for node in tree.nodes if node.type == "GROUP_OUTPUT" and node.is_active_output)

    def scalar(label: str) -> float:
        source = output.inputs[label].links[0].from_socket
        if source.node.type != "VALUE":
            raise ValueError(f"Water settings output must use an editable value node: group={tree.name}, output={label}")
        return float(source.default_value)

    color_source = output.inputs["Deep Color"].links[0].from_socket
    if color_source.node.type != "RGB":
        raise ValueError(f"Water settings output must use an editable color node: group={tree.name}, output=Deep Color")
    _validate_optics(WaterOptics(scalar("IOR"), scalar("Roughness"), scalar("Attenuation Per Meter"),
                                 scalar("Scattering Per Meter"), tuple(color_source.default_value), "settings"))


def water_optics_group() -> bpy.types.ShaderNodeTree:
    name = "EF_Water_Optics_v5"
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if existing.get("endfield_contract") != "PortableWaterOptics/5":
            raise ValueError(f"Conflicting water optics group: {name}")
        _validate_group_interface(existing, (("Normal", "INPUT", "NodeSocketVector"),
                                             ("Depth", "INPUT", "NodeSocketFloat"),
                                             ("Attenuation Per Meter", "INPUT", "NodeSocketFloat"),
                                             ("Deep Color", "INPUT", "NodeSocketColor"),
                                             ("Scattering Per Meter", "INPUT", "NodeSocketFloat"),
                                             ("IOR", "INPUT", "NodeSocketFloat"),
                                             ("Roughness", "INPUT", "NodeSocketFloat"),
                                             ("Surface", "OUTPUT", "NodeSocketShader"),
                                             ("Volume", "OUTPUT", "NodeSocketShader"),
                                             ("Eevee Surface", "OUTPUT", "NodeSocketShader")))
        _validate_group_outputs(existing, ("Surface", "Volume", "Eevee Surface"))
        return existing
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    for label, kind in (("Normal", "NodeSocketVector"), ("Depth", "NodeSocketFloat"),
                        ("Attenuation Per Meter", "NodeSocketFloat"), ("Deep Color", "NodeSocketColor"),
                        ("Scattering Per Meter", "NodeSocketFloat"),
                        ("IOR", "NodeSocketFloat"), ("Roughness", "NodeSocketFloat")):
        tree.interface.new_socket(name=label, in_out="INPUT", socket_type=kind)
    tree.interface.new_socket(name="Surface", in_out="OUTPUT", socket_type="NodeSocketShader")
    tree.interface.new_socket(name="Volume", in_out="OUTPUT", socket_type="NodeSocketShader")
    tree.interface.new_socket(name="Eevee Surface", in_out="OUTPUT", socket_type="NodeSocketShader")
    nodes, links = tree.nodes, tree.links
    entry = nodes.new("NodeGroupInput")
    output = nodes.new("NodeGroupOutput")
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.label = "Shared native water surface"
    bsdf.inputs["Metallic"].default_value = 0
    bsdf.inputs["Coat Weight"].default_value = 0
    bsdf.inputs["Base Color"].default_value = (1, 1, 1, 1)
    bsdf.inputs["Transmission Weight"].default_value = 1
    links.new(entry.outputs["IOR"], bsdf.inputs["IOR"])
    links.new(entry.outputs["Roughness"], bsdf.inputs["Roughness"])
    links.new(entry.outputs["Normal"], bsdf.inputs["Normal"])
    links.new(bsdf.outputs[0], output.inputs["Surface"])
    # Eevee screen rays do not integrate the same refracted medium path as Cycles.
    # Apply spectral attenuation at the boundary using measured bottom depth.
    geometry = nodes.new("ShaderNodeNewGeometry")
    dot = nodes.new("ShaderNodeVectorMath")
    dot.operation = "DOT_PRODUCT"
    links.new(geometry.outputs["Incoming"], dot.inputs[0])
    links.new(geometry.outputs["Normal"], dot.inputs[1])
    cosine2 = nodes.new("ShaderNodeMath")
    cosine2.operation = "MULTIPLY"
    links.new(dot.outputs["Value"], cosine2.inputs[0])
    links.new(dot.outputs["Value"], cosine2.inputs[1])
    sine2 = nodes.new("ShaderNodeMath")
    sine2.operation = "SUBTRACT"
    sine2.inputs[0].default_value = 1
    links.new(cosine2.outputs[0], sine2.inputs[1])
    ior2 = nodes.new("ShaderNodeMath")
    ior2.operation = "MULTIPLY"
    links.new(entry.outputs["IOR"], ior2.inputs[0])
    links.new(entry.outputs["IOR"], ior2.inputs[1])
    refracted_sine2 = nodes.new("ShaderNodeMath")
    refracted_sine2.operation = "DIVIDE"
    links.new(sine2.outputs[0], refracted_sine2.inputs[0])
    links.new(ior2.outputs[0], refracted_sine2.inputs[1])
    refracted_cosine2 = nodes.new("ShaderNodeMath")
    refracted_cosine2.operation = "SUBTRACT"
    refracted_cosine2.inputs[0].default_value = 1
    refracted_cosine2.use_clamp = True
    links.new(refracted_sine2.outputs[0], refracted_cosine2.inputs[1])
    refracted_cosine = nodes.new("ShaderNodeMath")
    refracted_cosine.operation = "SQRT"
    links.new(refracted_cosine2.outputs[0], refracted_cosine.inputs[0])
    distance = nodes.new("ShaderNodeMath")
    distance.operation = "DIVIDE"
    links.new(entry.outputs["Depth"], distance.inputs[0])
    links.new(refracted_cosine.outputs[0], distance.inputs[1])
    separate = nodes.new("ShaderNodeSeparateColor")
    links.new(entry.outputs["Deep Color"], separate.inputs["Color"])
    attenuation = nodes.new("ShaderNodeCombineColor")
    attenuation.label = "Eevee spectral extinction only; no in-scattering"
    for index in range(3):
        inverse_color = nodes.new("ShaderNodeMath")
        inverse_color.operation = "SUBTRACT"
        inverse_color.inputs[0].default_value = 1
        links.new(separate.outputs[index], inverse_color.inputs[1])
        absorption_coefficient = nodes.new("ShaderNodeMath")
        absorption_coefficient.operation = "MULTIPLY"
        links.new(inverse_color.outputs[0], absorption_coefficient.inputs[0])
        links.new(entry.outputs["Attenuation Per Meter"], absorption_coefficient.inputs[1])
        scatter_coefficient = nodes.new("ShaderNodeMath")
        scatter_coefficient.operation = "MULTIPLY"
        links.new(separate.outputs[index], scatter_coefficient.inputs[0])
        links.new(entry.outputs["Scattering Per Meter"], scatter_coefficient.inputs[1])
        extinction = nodes.new("ShaderNodeMath")
        extinction.operation = "ADD"
        links.new(absorption_coefficient.outputs[0], extinction.inputs[0])
        links.new(scatter_coefficient.outputs[0], extinction.inputs[1])
        optical_depth = nodes.new("ShaderNodeMath")
        optical_depth.operation = "MULTIPLY"
        links.new(extinction.outputs[0], optical_depth.inputs[0])
        links.new(distance.outputs[0], optical_depth.inputs[1])
        negative_depth = nodes.new("ShaderNodeMath")
        negative_depth.operation = "MULTIPLY"
        negative_depth.inputs[1].default_value = -1
        links.new(optical_depth.outputs[0], negative_depth.inputs[0])
        channel = nodes.new("ShaderNodeMath")
        channel.operation = "EXPONENT"
        links.new(negative_depth.outputs[0], channel.inputs[0])
        links.new(channel.outputs[0], attenuation.inputs[index])
    eevee = nodes.new("ShaderNodeBsdfPrincipled")
    eevee.label = "Eevee native transmission with scene-depth attenuation"
    eevee.inputs["Transmission Weight"].default_value = 1
    links.new(attenuation.outputs[0], eevee.inputs["Base Color"])
    for label in ("Normal", "IOR", "Roughness"):
        links.new(entry.outputs[label], eevee.inputs[label])
    links.new(eevee.outputs[0], output.inputs["Eevee Surface"])
    absorption = nodes.new("ShaderNodeVolumeAbsorption")
    absorption.label = "Native bounded water absorption"
    links.new(entry.outputs["Deep Color"], absorption.inputs["Color"])
    links.new(entry.outputs["Attenuation Per Meter"], absorption.inputs["Density"])
    scatter = nodes.new("ShaderNodeVolumeScatter")
    scatter.inputs["Anisotropy"].default_value = 0.2
    links.new(entry.outputs["Deep Color"], scatter.inputs["Color"])
    links.new(entry.outputs["Scattering Per Meter"], scatter.inputs["Density"])
    medium = nodes.new("ShaderNodeAddShader")
    links.new(absorption.outputs[0], medium.inputs[0])
    links.new(scatter.outputs[0], medium.inputs[1])
    links.new(medium.outputs[0], output.inputs["Volume"])
    tree["endfield_contract"] = "PortableWaterOptics/5"
    return tree


def water_settings(optics: WaterOptics) -> bpy.types.ShaderNodeTree:
    """Share editable optical controls between the surface and its closed volume."""
    _validate_optics(optics)
    signature = json.dumps((optics.ior, optics.roughness, optics.attenuation_per_meter, optics.scattering_per_meter, optics.deep_color))
    name = "EF_Water_Settings_" + hashlib.sha256(signature.encode()).hexdigest()[:12]
    tree = bpy.data.node_groups.get(name)
    if tree is not None:
        if tree.get("endfield_contract") != "EndfieldWaterSettings/1":
            raise ValueError(f"Conflicting water settings group: {name}")
        _validate_group_interface(tree, (("IOR", "OUTPUT", "NodeSocketFloat"),
                                         ("Roughness", "OUTPUT", "NodeSocketFloat"),
                                         ("Attenuation Per Meter", "OUTPUT", "NodeSocketFloat"),
                                         ("Scattering Per Meter", "OUTPUT", "NodeSocketFloat"),
                                         ("Deep Color", "OUTPUT", "NodeSocketColor")))
        _validate_group_outputs(tree, ("IOR", "Roughness", "Attenuation Per Meter", "Scattering Per Meter", "Deep Color"))
        _validate_settings_values(tree)
        return tree
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    output = tree.nodes.new("NodeGroupOutput")
    for label, value in (("IOR", optics.ior), ("Roughness", optics.roughness),
                         ("Attenuation Per Meter", optics.attenuation_per_meter),
                         ("Scattering Per Meter", optics.scattering_per_meter)):
        tree.interface.new_socket(name=label, in_out="OUTPUT", socket_type="NodeSocketFloat")
        node = tree.nodes.new("ShaderNodeValue")
        node.label = label
        node.outputs[0].default_value = value
        tree.links.new(node.outputs[0], output.inputs[label])
    tree.interface.new_socket(name="Deep Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    color = tree.nodes.new("ShaderNodeRGB")
    color.label = "Water absorption color (Blender calibration)"
    color.outputs[0].default_value = optics.deep_color
    tree.links.new(color.outputs[0], output.inputs["Deep Color"])
    tree["endfield_contract"] = "EndfieldWaterSettings/1"
    return tree


def adapt_water_surface(material: bpy.types.Material, normal: bpy.types.NodeSocket,
                        optics: WaterOptics) -> None:
    if not isinstance(material, bpy.types.Material) or not material.use_nodes or not isinstance(normal, bpy.types.NodeSocket) or normal.node.id_data != material.node_tree:
        raise ValueError("Water normal must belong to the material being adapted")
    _validate_optics(optics)
    existing_core = bpy.data.node_groups.get("EF_Water_Optics_v5")
    core_tree = water_optics_group() if existing_core is not None else None
    settings_tree = water_settings(optics)
    if core_tree is None:
        core_tree = water_optics_group()
    nodes, links = material.node_tree.nodes, material.node_tree.links
    retained: set[str] = set()

    def retain_upstream(node: bpy.types.Node) -> None:
        if node.name in retained:
            return
        retained.add(node.name)
        for socket in node.inputs:
            for link in socket.links:
                retain_upstream(link.from_node)

    retain_upstream(normal.node)
    for node in tuple(nodes):
        if node.name not in retained:
            nodes.remove(node)
    depth = nodes.new("ShaderNodeAttribute")
    depth.attribute_name = optics.depth_attribute
    depth.label = "Scene-derived water depth in meters"
    core = nodes.new("ShaderNodeGroup")
    core.node_tree = core_tree
    core.name = "EF_Water_Shared_Core"
    settings = nodes.new("ShaderNodeGroup")
    settings.node_tree = settings_tree
    settings.name = "EF_Water_Shared_Settings"
    for label in ("IOR", "Roughness", "Attenuation Per Meter", "Scattering Per Meter", "Deep Color"):
        links.new(settings.outputs[label], core.inputs[label])
    links.new(normal, core.inputs["Normal"])
    links.new(depth.outputs["Fac"], core.inputs["Depth"])
    shadow_ray = nodes.new("ShaderNodeLightPath")
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    shadow_adapter = nodes.new("ShaderNodeMixShader")
    shadow_adapter.label = "Cycles: transmit direct light without refractive caustic dependency"
    links.new(shadow_ray.outputs["Is Shadow Ray"], shadow_adapter.inputs[0])
    links.new(core.outputs["Surface"], shadow_adapter.inputs[1])
    links.new(transparent.outputs[0], shadow_adapter.inputs[2])
    for engine in ("CYCLES", "EEVEE"):
        output = nodes.new("ShaderNodeOutputMaterial")
        output.target = engine
        output.name = "EF_Water_Output_" + engine
        links.new(shadow_adapter.outputs[0] if engine == "CYCLES" else core.outputs["Eevee Surface"], output.inputs["Surface"])
        if engine == "EEVEE":
            output.inputs["Thickness"].default_value = 0.01
    material.use_raytrace_refraction = True
    material.surface_render_method = "DITHERED"
    material.thickness_mode = "SLAB"
    material["endfield_surface_contract"] = "EndfieldPortableWater/2"
    material["endfield_depth_attribute"] = optics.depth_attribute
    material["endfield_water_limitations"] = "Requires a validated closed absorption volume; bottom interpolates sampled geometry; underwater cameras, caustics and HGRP screen history remain unverified"
    material["endfield_optics_parameters"] = "Blender review calibration; IOR, attenuation, roughness and deep color are not claimed as decoded game coefficients"
    material["endfield_cycles_light_adapter"] = "Water surface passes shadow rays; avoids a black bottom when refractive caustics are unavailable"
    material["endfield_eevee_medium_adapter"] = "Spectral Beer-Lambert extinction from scene bathymetry and Snell path length; no in-scattering or refracted-volume integration claim"


def create_water_volume(source: bpy.types.Object, depths: tuple[float, ...],
                        surface: bpy.types.Material) -> bpy.types.Object:
    """Construct a manifold volume without changing the source water geometry."""
    if not isinstance(source, bpy.types.Object) or source.type != "MESH":
        raise ValueError("Water volume source must be a mesh object")
    if (not isinstance(depths, (tuple, list)) or not source.data.polygons or len(depths) != len(source.data.vertices) or
            any(not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in depths)):
        raise ValueError(f"Water volume requires faces and a positive finite depth for every source vertex: {source.name}")
    if not isinstance(surface, bpy.types.Material) or not surface.use_nodes:
        raise ValueError(f"Water volume requires an adapted node material: source={source.name}")
    settings_node = surface.node_tree.nodes.get("EF_Water_Shared_Settings")
    if settings_node is None or settings_node.type != "GROUP" or settings_node.node_tree is None:
        raise ValueError(f"Water volume surface has no shared settings group: material={surface.name}")
    core_node = surface.node_tree.nodes.get("EF_Water_Shared_Core")
    if core_node is None or core_node.type != "GROUP" or core_node.node_tree is None:
        raise ValueError(f"Water volume surface has no shared optics core: material={surface.name}")
    if core_node.node_tree.name != "EF_Water_Optics_v5" or core_node.node_tree.get("endfield_contract") != "PortableWaterOptics/5":
        raise ValueError(f"Water volume requires a v5 optics core: material={surface.name}, group={core_node.node_tree.name}")
    settings_tree = settings_node.node_tree
    if settings_tree.get("endfield_contract") != "EndfieldWaterSettings/1":
        raise ValueError(f"Water volume surface has incompatible settings group: material={surface.name}, group={settings_tree.name}")
    _validate_group_interface(settings_tree, (("IOR", "OUTPUT", "NodeSocketFloat"),
                                              ("Roughness", "OUTPUT", "NodeSocketFloat"),
                                              ("Attenuation Per Meter", "OUTPUT", "NodeSocketFloat"),
                                              ("Scattering Per Meter", "OUTPUT", "NodeSocketFloat"),
                                              ("Deep Color", "OUTPUT", "NodeSocketColor")))
    _validate_group_outputs(settings_tree, ("IOR", "Roughness", "Attenuation Per Meter", "Scattering Per Meter", "Deep Color"))
    _validate_settings_values(settings_tree)
    core_tree = water_optics_group()
    original_faces = [tuple(face.vertices) for face in source.data.polygons]
    incident: dict[int, set[int]] = {}
    edge_faces: dict[tuple[int, int], list[int]] = {}
    for index, face in enumerate(original_faces):
        for vertex in face:
            incident.setdefault(vertex, set()).add(index)
        for a, b in zip(face, (*face[1:], face[0]), strict=True):
            edge_faces.setdefault(tuple(sorted((a, b))), []).append(index)
    # Separate face fans that only touch at a vertex. Extruding a bow-tie vertex
    # otherwise creates a vertical edge shared by four volume faces.
    mapping: dict[tuple[int, int], int] = {}
    positions, volume_depths = [], []
    for vertex, remaining_faces in incident.items():
        remaining = set(remaining_faces)
        while remaining:
            pending = [min(remaining)]
            fan: set[int] = set()
            while pending:
                face_index = pending.pop()
                if face_index in fan:
                    continue
                fan.add(face_index)
                face = original_faces[face_index]
                offset = face.index(vertex)
                for neighbor in (face[offset - 1], face[(offset + 1) % len(face)]):
                    pending.extend(i for i in edge_faces[tuple(sorted((vertex, neighbor)))] if i not in fan)
            remaining -= fan
            for face_index in fan:
                mapping[(vertex, face_index)] = len(positions)
            positions.append(source.matrix_world @ source.data.vertices[vertex].co)
            volume_depths.append(depths[vertex])
    faces = [tuple(mapping[(vertex, index)] for vertex in face) for index, face in enumerate(original_faces)]
    vertices = [(p.x, p.y, p.z - min(0.01, depth * 0.25)) for p, depth in zip(positions, volume_depths, strict=True)] + [(p.x, p.y, p.z - depth) for p, depth in zip(positions, volume_depths, strict=True)]
    count = len(positions)
    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for face in faces:
        for a, b in zip(face, (*face[1:], face[0]), strict=True):
            edges.setdefault(tuple(sorted((a, b))), []).append((a, b))
    if any(len(uses) > 2 for uses in edges.values()):
        raise ValueError(f"Water source has nonmanifold overlapping faces: {source.name}")
    bottom = [tuple(index + count for index in reversed(face)) for face in faces]
    sides = [(b, a, a + count, b + count) for uses in edges.values() if len(uses) == 1 for a, b in uses]
    mesh = bpy.data.meshes.new(source.name + "_AbsorptionVolume")
    material = None
    obj = None
    try:
        mesh.from_pydata(vertices, [], faces + bottom + sides)
        mesh.update()
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            if any(not edge.is_manifold for edge in bm.edges):
                invalid = [(tuple(v.index for v in edge.verts), len(edge.link_faces)) for edge in bm.edges if not edge.is_manifold]
                raise ValueError(f"Generated water volume is not closed: source={source.name}, invalid_edges={invalid[:20]}")
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
            if bm.calc_volume(signed=False) <= 0:
                raise ValueError(f"Generated water volume has no enclosed space: {source.name}")
            bm.to_mesh(mesh)
        finally:
            bm.free()
        material = bpy.data.materials.new(source.name + "_WaterAbsorption")
        material.use_nodes = True
        nodes, links = material.node_tree.nodes, material.node_tree.links
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        output.target = "CYCLES"
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        links.new(transparent.outputs[0], output.inputs["Surface"])
        eevee_output = nodes.new("ShaderNodeOutputMaterial")
        eevee_output.target = "EEVEE"
        links.new(transparent.outputs[0], eevee_output.inputs["Surface"])
        core = nodes.new("ShaderNodeGroup")
        core.node_tree = core_tree
        settings = nodes.new("ShaderNodeGroup")
        settings.node_tree = settings_tree
        for label in ("IOR", "Roughness", "Attenuation Per Meter", "Scattering Per Meter", "Deep Color"):
            links.new(settings.outputs[label], core.inputs[label])
        links.new(core.outputs["Volume"], output.inputs["Volume"])
        material["endfield_water_volume_source"] = source.name
        mesh.materials.append(material)
        obj = bpy.data.objects.new(mesh.name, mesh)
        obj["endfield_water_volume_source"] = source.name
        obj["endfield_water_boundary_inset_meters"] = 0.01
        obj["endfield_water_volume_shadow_adapter"] = "Camera medium retained; direct-light shadow attenuation disabled to match pre-lit scene-color approximation"
        obj.visible_shadow = False
        obj.display_type = "WIRE"
        return obj
    except Exception:
        if obj is not None:
            bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)
        if material is not None:
            bpy.data.materials.remove(material)
        raise


def configure_water_scene(scene: bpy.types.Scene) -> None:
    """Select scene-wide ray quality needed for inspectable Eevee water reflections."""
    configure_scene_transmission(scene)
    options = scene.eevee.ray_tracing_options
    options.resolution_scale = "1"
    options.screen_trace_quality = 1
    options.use_denoise = False
    scene.eevee.taa_render_samples = max(128, scene.eevee.taa_render_samples)
    scene["endfield_water_ray_quality"] = "Eevee full-resolution screen tracing, quality 1, denoising off, at least 128 render samples"
    scene["endfield_water_ray_quality_tradeoff"] = "Scene-wide setting increases Eevee render cost and can leave visible ray noise; selected to preserve water reflection detail"
