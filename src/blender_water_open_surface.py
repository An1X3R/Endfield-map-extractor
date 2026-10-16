"""Retain authored open water independently of measured bathymetry coverage."""
from __future__ import annotations

import bpy

from blender_scene_water_depth import WaterDepthResult


UNMEASURED_OPTICAL_DEPTH = 50.0
DEPTH_VALID_ATTRIBUTE = "endfield_water_depth_measured"
OPEN_DEPTH_POLICY = "host_optical_saturation_without_invented_bottom"


def unmeasured_water_faces(obj: bpy.types.Object, depth: WaterDepthResult) -> tuple[int, ...]:
    """Identify source faces requiring the explicit open-surface optics adapter."""
    if obj.name != depth.object_name or len(obj.data.vertices) != len(depth.hits):
        raise ValueError(f"WATER_OPEN_DEPTH_MAPPING_INVALID object={obj.name}")
    return tuple(face.index for face in obj.data.polygons
                 if any(depth.hits[index] is None for index in face.vertices))


def bind_open_water_surface(obj: bpy.types.Object, depth: WaterDepthResult,
                            material: bpy.types.Material, faces: tuple[int, ...]) -> None:
    """Use boundary attenuation for unmeasured faces in both render engines.

    Fifty meters is the retained source transport saturation cap, not a floor.
    Missing vertical host hits do not prove missing native camera-depth hits.
    Keep that distinction in the saved attributes and do not construct a volume.
    """
    if faces != unmeasured_water_faces(obj, depth) or not faces:
        raise ValueError(f"WATER_OPEN_FACE_SET_INVALID object={obj.name}")
    adapter = material.copy()
    adapter.name = material.name + " | Open Depth"
    tree = adapter.node_tree
    if tree is None:
        raise ValueError(f"WATER_OPEN_MATERIAL_NODES_MISSING material={material.name}")
    cycles = tree.nodes.get("EF_Water_Output_CYCLES")
    eevee = tree.nodes.get("EF_Water_Output_EEVEE")
    if cycles is None or eevee is None or not eevee.inputs["Surface"].is_linked:
        raise ValueError(f"WATER_OPEN_ENGINE_OUTPUT_MISSING material={material.name}")
    depth_nodes = [node for node in tree.nodes if node.type == "ATTRIBUTE"
                   and node.attribute_name == material["endfield_depth_attribute"]]
    if len(depth_nodes) != 1:
        raise ValueError(f"WATER_OPEN_DEPTH_NODE_INVALID material={material.name} count={len(depth_nodes)}")
    optical_limit = tree.nodes.new("ShaderNodeValue")
    optical_limit.name = optical_limit.label = "HOST Unmeasured Optical Depth (m)"
    optical_limit.outputs[0].default_value = UNMEASURED_OPTICAL_DEPTH
    optical_limit["endfield_layout_section"] = "08 Open Depth Approximation"
    optical_limit["endfield_layout_description"] = "Optical saturation only; no measured or invented floor"
    for link in tuple(depth_nodes[0].outputs["Fac"].links):
        tree.links.new(optical_limit.outputs[0], link.to_socket)
    tree.links.new(eevee.inputs["Surface"].links[0].from_socket, cycles.inputs["Surface"])
    adapter["endfield_water_depth_policy"] = OPEN_DEPTH_POLICY
    adapter["endfield_water_limitations"] = (
        "Authored surface retained; unmeasured host depths use optical saturation. "
        "No bottom or absorption volume invented; native screen depth and underwater views unverified.")
    adapter["endfield_cycles_light_adapter"] = "Open surface boundary extinction; no unmeasured medium volume"
    validity = obj.data.attributes.new(DEPTH_VALID_ATTRIBUTE, "FLOAT", "POINT")
    validity.data.foreach_set("value", tuple(float(hit is not None) for hit in depth.hits))
    slot = len(obj.data.materials)
    obj.data.materials.append(adapter)
    for index in faces:
        obj.data.polygons[index].material_index = slot
    obj["endfield_water_depth_policy"] = OPEN_DEPTH_POLICY
    obj["endfield_water_unmeasured_optical_depth_meters"] = UNMEASURED_OPTICAL_DEPTH
    obj["endfield_water_unverified_depth_polygons"] = len(faces)
    obj["endfield_water_original_missing_bottom_vertices"] = sum(hit is None for hit in depth.hits)


def measured_water_volume(obj: bpy.types.Object, depth: WaterDepthResult,
                          material: bpy.types.Material) -> bpy.types.Object | None:
    """Build a medium solely for the measured face domain, leaving source faces intact."""
    from blender_scene_water import create_water_volume
    from blender_scene_water_build import retain_measured_water_faces

    domain = obj.copy()
    domain.data = obj.data.copy()
    try:
        retained = retain_measured_water_faces(domain, WaterDepthResult(domain.name, depth.hits))
        if not domain.data.polygons:
            return None
        values = tuple(hit.depth for hit in retained.hits if hit is not None)
        volume = create_water_volume(domain, values, material)
        volume["endfield_water_volume_source"] = obj.name
        for volume_material in volume.data.materials:
            if volume_material is not None:
                volume_material["endfield_water_volume_source"] = obj.name
        volume["endfield_water_volume_measured_faces_only"] = True
        return volume
    finally:
        mesh = domain.data
        bpy.data.objects.remove(domain)
        bpy.data.meshes.remove(mesh)
