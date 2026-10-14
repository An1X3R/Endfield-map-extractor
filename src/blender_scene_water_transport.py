"""Connect prepared source water to explicitly selected editable host lighting."""
from __future__ import annotations

from collections.abc import Sequence
import json
import math
from typing import TypedDict

import bpy

from blender_scene_water_foam import reconnect_source_water_foam
from blender_water_edge_blend import connect_water_edge_blend
from blender_water_screen_distortion import connect_water_screen_distortion, water_camera_perspective
from blender_water_transport import (bind_water_transport, connect_water_transport_phase_atmosphere,
    connect_water_transport_ambient_l1, connect_water_transport_sun, connect_water_transport_surface)
from endfield_scene_water_materials import read_water_material_row
from endfield_water_transport import (WaterEdgeBlend, WaterTransport, WaterTransportLighting,
                                      read_water_edge_blend, read_water_transport)


class SceneWaterTransportAudit(TypedDict):
    status: str
    entity_ids: list[int]
    source_light_map: str
    source_light_entity_id: int
    host_sun: str
    disabled_volume_objects: list[str]
    phase_atmosphere_rgb_enabled: bool
    ambient_l1_enabled: bool
    edge_blend_enabled: bool
    screen_distortion_enabled: bool
    boundary: str


def apply_scene_water_transport(scene: bpy.types.Scene, material_rows: Sequence[object],
                                lighting: WaterTransportLighting) -> SceneWaterTransportAudit:
    """Use measured depth and serialized optics; never pick a light by name or brightness."""
    perspective = water_camera_perspective(scene.camera)
    lights = [obj for obj in scene.objects if obj.type == "LIGHT"
              and obj.get("endfield_light_map") == lighting.map_id
              and obj.get("endfield_light_entity_id") == str(lighting.entity_id)]
    if len(lights) != 1 or lights[0].data.type != "SUN":
        raise ValueError(f"Explicit water Sun must match exactly one exported directional light: "
                         f"map_id={lighting.map_id}, entity_id={lighting.entity_id}, matches={len(lights)}")
    waters = [obj for obj in scene.objects if obj.type == "MESH" and obj.get("endfield_category") == "water"]
    if not waters:
        raise ValueError("Source water transport requested but no restored water surfaces exist in the batch")
    specs = [read_water_material_row(row) for row in material_rows]
    by_entity = {spec.entity_id: spec for spec in specs}
    if len(by_entity) != len(specs):
        raise ValueError("Source water transport material entity ids must be unique")
    prepared: list[tuple[int, bpy.types.Material, bpy.types.Node, bpy.types.Node,
                         WaterTransport, bpy.types.Object, float, float, WaterEdgeBlend]] = []
    for obj in waters:
        entity = int(obj["entity_id"])
        if entity not in by_entity or len(obj.data.materials) != 1:
            raise ValueError(f"Restored water needs its exact source specification and one material: entity_id={entity}")
        material = obj.data.materials[0]
        if material is None or material.users != 1 or material.node_tree is None:
            raise ValueError(f"Restored water requires a private node material: entity_id={entity}")
        if material.get("endfield_surface_contract") != "EndfieldPortableWater/2":
            raise ValueError(f"Source transport requires the existing measured-depth water stage: entity_id={entity}")
        core = material.node_tree.nodes.get("EF_Water_Shared_Core")
        settings = material.node_tree.nodes.get("EF_Water_Shared_Settings")
        if core is None or settings is None or not core.inputs["Normal"].is_linked or not core.inputs["Depth"].is_linked:
            raise ValueError(f"Restored water normal/depth/settings connections are missing: entity_id={entity}")
        config = json.loads(by_entity[entity].config_source_json)
        coefficients = read_water_transport(config)
        volumes = [volume for volume in scene.objects if volume.get("endfield_water_volume_source") == obj.name]
        if len(volumes) != 1:
            raise ValueError(f"Restored water must have exactly one prior bounded medium: entity_id={entity}, count={len(volumes)}")
        outputs = [node for node in settings.node_tree.nodes if node.type == "GROUP_OUTPUT" and node.is_active_output]
        if len(outputs) != 1 or not outputs[0].inputs["IOR"].is_linked:
            raise ValueError(f"Restored water settings have no active IOR output: entity_id={entity}")
        ior = float(outputs[0].inputs["IOR"].links[0].from_socket.default_value)
        roughness = float(config["Appearance"]["Roughness"])
        if not math.isfinite(ior) or ior < 1 or not math.isfinite(roughness) or not 0 <= roughness <= 1:
            raise ValueError(f"Restored water host IOR or source roughness is invalid: entity_id={entity}")
        edge = read_water_edge_blend(config)
        prepared.append((entity, material, core, settings, coefficients, volumes[0], ior, roughness, edge))
    disabled: list[str] = []
    for entity, material, core, settings, coefficients, volume, ior, roughness, edge in prepared:
        tree = material.node_tree
        normal = core.inputs["Normal"].links[0].from_socket
        depth = core.inputs["Depth"].links[0].from_socket
        transport = bind_water_transport(tree, coefficients)
        tree.links.new(depth, transport.inputs["Depth"])
        transport.inputs["HOST Ambient Radiance"].default_value = (*lighting.ambient_radiance, 1.)
        if lighting.ambient_l1 is not None:
            connect_water_transport_ambient_l1(transport, normal, lighting.ambient_l1.coefficients,
                                               lighting.ambient_l1.environment_global_scale)
        connect_water_transport_sun(transport, lights[0], lighting.radiance_scale)
        connect_water_transport_surface(material, transport, normal, ior, roughness)
        reconnect_source_water_foam(material)
        if lighting.phase_atmosphere_rgb is not None:
            connect_water_transport_phase_atmosphere(transport, lighting.phase_atmosphere_rgb)
            material["endfield_phase_atmosphere_rgb_source"] = (
                "Explicit water_transport_lighting.phase_atmosphere_rgb input; native per-scene value not captured")
        connect_water_edge_blend(material, normal, depth, edge)
        connect_water_screen_distortion(material, normal, depth, by_entity[entity].flow.water_ior, perspective)
        tree.nodes.remove(core)
        tree.nodes.remove(settings)
        volume.hide_render = True
        volume.hide_set(True)
        volume["endfield_water_disabled_medium_reason"] = "Source transport applies extinction once at the open surface boundary"
        material["endfield_optics_parameters"] = "Serialized absorption/scattering/phase coefficients and roughness; measured bottom depth; explicit host lighting and retained physical IOR"
        for key in ("endfield_cycles_light_adapter", "endfield_eevee_light_adapter", "endfield_eevee_medium_adapter"):
            if key in material:
                del material[key]
        disabled.append(volume.name)
    return {"status": "source_transport_with_explicit_host_lighting", "entity_ids": [row[0] for row in prepared],
            "source_light_map": lighting.map_id, "source_light_entity_id": lighting.entity_id,
            "host_sun": lights[0].name, "disabled_volume_objects": disabled,
            "phase_atmosphere_rgb_enabled": lighting.phase_atmosphere_rgb is not None,
            "ambient_l1_enabled": lighting.ambient_l1 is not None,
            "edge_blend_enabled": True,
            "screen_distortion_enabled": True,
            "boundary": "Source transport coefficients, measured vertical depth and editable host Sun; Sun energy scale and ambient inputs are explicit. Optional ambient_l1 supplies a source-selected default-SH buffer and environment global multiplier with local irradiance volumes excluded. Optional phase_atmosphere_rgb selects a separate candidate branch and never uses host Sun color as fallback. Automatic native environment selection, main-light activation, refraction buffers and underwater views remain unverified."}
