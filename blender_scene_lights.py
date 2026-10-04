"""Realize bounded native scene lights as explicitly uncalibrated Blender previews."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import struct

import bpy
from mathutils import Matrix, Quaternion, Vector

from blender_build_map02_chunk_20260727 import unity_matrix
from blender_scene_materials import layout_shader_node_tree
from extractor_core.lights import preview_temperature_rgb


def _record(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ValueError(f"Expected native Light record: field={context}")
    return value


def _field(record: Mapping[str, object], name: str, context: str) -> object:
    if name not in record:
        raise ValueError(f"Native Light required field missing: field={context}.{name}")
    return record[name]


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise ValueError(f"Native Light integer required: field={context}, value={value!r}")
    return value


def _number(value: object, context: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"Native Light finite number required: field={context}, value={value!r}")
    return float(value)


def _vector(value: object, length: int, context: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"Native Light vector length differs: field={context}, expected={length}")
    return tuple(_number(item, f"{context}[{index}]") for index, item in enumerate(value))


def _bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"Native Light boolean required: field={context}, value={value!r}")
    return value


def _source_shadow_mode(value: object, context: str) -> int:
    if not isinstance(value, list):
        raise ValueError(f"Native Light properties must be an array: {context}")
    matches: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"Native Light property must be an object: {context}")
        if item.get("encoded_id") == 0x11090000:
            matches.append(item)
    if len(matches) != 1:
        raise ValueError(f"Native Light shadow mode field missing or duplicate: {context}, count={len(matches)}")
    field = matches[0]
    raw = field.get("raw_hex")
    if field.get("data_type") != 2 or type(raw) is not str or len(raw) != 8:
        raise ValueError(f"Native Light shadow mode encoding invalid: {context}, field={field!r}")
    try:
        mode = struct.unpack("<i", bytes.fromhex(raw))[0]
    except ValueError as error:
        raise ValueError(f"Native Light shadow mode hex invalid: {context}, raw={raw!r}") from error
    if mode not in (0, 1, 2):
        raise ValueError(f"Native Light shadow mode unsupported: {context}, mode={mode}")
    return mode


def _zxy_quaternion(euler: tuple[float, ...]) -> Quaternion:
    x, y, z = (math.radians(value) for value in euler)
    return (Quaternion((0.0, 1.0, 0.0), y)
            @ Quaternion((1.0, 0.0, 0.0), x)
            @ Quaternion((0.0, 0.0, 1.0), z))


def _light_matrix(position: tuple[float, ...], euler: tuple[float, ...]) -> Matrix:
    unity = Matrix.LocRotScale(Vector(position), _zxy_quaternion(euler), Vector((1.0, 1.0, 1.0)))
    values = [float(unity[row][column]) for column in range(4) for row in range(4)]
    converted = unity_matrix(struct.pack("<16f", *values))
    # Unity lights emit along local +Z; Blender lights emit along local -Z.
    return converted @ Matrix.Rotation(-math.pi / 2.0, 4, "X")


def _configure_color(data: bpy.types.Light, color: tuple[float, ...],
                     use_temperature: bool, temperature_kelvin: float) -> tuple[str, bool, tuple[float, float, float]]:
    if not use_temperature:
        rgb = (color[0], color[1], color[2])
        data.color = rgb
        return "source_rgb_numeric_preview", False, rgb
    temperature_rgb, clamped = preview_temperature_rgb(temperature_kelvin)
    rgb = tuple(color[index] * temperature_rgb[index] for index in range(3))
    data.color = rgb
    return "materialx_planckian_times_source_rgb_preview", clamped, rgb


def _configure_punctual_range(data: bpy.types.Light, radius: float,
                              preview_color: tuple[float, float, float]) -> None:
    data.color = preview_color
    data.use_custom_distance = True
    data.cutoff_distance = radius
    data.use_nodes = True
    tree = data.node_tree
    if tree is None:
        raise ValueError(f"Blender Light node tree missing for range cutoff: name={data.name}")
    emission = next((node for node in tree.nodes if node.bl_idname == "ShaderNodeEmission"), None)
    if emission is None:
        raise ValueError(f"Blender Light Emission node missing for range cutoff: name={data.name}")
    # Cycles multiplies the node color by Light.color; Eevee uses Light.color directly.
    emission.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    ray = tree.nodes.new("ShaderNodeLightPath")
    ray.name = "Receiver to Light Distance"
    ray.label = "Cycles receiver distance in scene units"
    ray["endfield_layout_section"] = "Source Range Preview"
    cutoff = tree.nodes.new("ShaderNodeMath")
    cutoff.name = "Source Range Hard Cutoff Preview"
    cutoff.label = "Source range; hard cutoff approximation"
    cutoff["endfield_layout_section"] = "Source Range Preview"
    cutoff.operation = "LESS_THAN"
    cutoff.inputs[1].default_value = radius
    tree.links.new(ray.outputs["Ray Length"], cutoff.inputs[0])
    tree.links.new(cutoff.outputs[0], emission.inputs["Strength"])
    emission.label = "Neutral color and range mask; color and power set on Light"
    emission["endfield_layout_section"] = "Light Output"
    layout_shader_node_tree(tree)


def build_scene_lights(light_instances: Sequence[Mapping[str, object]], collection: bpy.types.Collection,
                       preview_punctual_watts_per_source_intensity: float,
                       preview_sun_watts_per_square_meter_per_source_intensity: float,
                       max_light_objects: int) -> list[dict[str, object]]:
    """Create source-positioned lights with a stated, user-visible brightness calibration."""
    punctual_factor = _number(preview_punctual_watts_per_source_intensity, "preview_punctual_watts_per_source_intensity")
    sun_factor = _number(preview_sun_watts_per_square_meter_per_source_intensity,
                         "preview_sun_watts_per_square_meter_per_source_intensity")
    if punctual_factor <= 0.0 or sun_factor <= 0.0 or max_light_objects <= 0:
        raise ValueError(f"Native Light preview limits must be positive: punctualFactor={punctual_factor}, sunFactor={sun_factor}, maxObjects={max_light_objects}")
    audit: list[dict[str, object]] = []
    created = 0
    for index, value in enumerate(light_instances):
        row = _record(value, f"light_instances[{index}]")
        map_id = _field(row, "map_id", "Light")
        entity_id = _integer(_field(row, "entity_id", "Light"), "Light.entity_id")
        name = _field(row, "name", "Light")
        if type(map_id) is not str or type(name) is not str or not name:
            raise ValueError(f"Native Light identity invalid: map={map_id!r}, entity={entity_id}, name={name!r}")
        context = f"map={map_id}, entity={entity_id}"
        light_type = _integer(_field(row, "light_type", context), f"{context}.light_type")
        shape = _integer(_field(row, "shape", context), f"{context}.shape")
        character_only = _bool(_field(row, "character_only", context), f"{context}.character_only")
        cookie = _field(row, "cookie_path", context)
        if cookie is not None and (type(cookie) is not str or not cookie):
            raise ValueError(f"Native Light Cookie path invalid: {context}, cookie={cookie!r}")
        base = {"map_id": map_id, "entity_id": entity_id, "name": name,
                "source_init_paths": _field(row, "source_init_paths", context),
                "baseline_sha256": _field(row, "baseline_sha256", context)}
        if light_type == 3:
            audit.append(base | {"status": "unsupported_baked_area_light"})
            continue
        if light_type not in (0, 1, 2) or shape != 0:
            audit.append(base | {"status": "unsupported_light_type_or_shape", "light_type": light_type, "shape": shape})
            continue
        if character_only:
            audit.append(base | {"status": "unsupported_character_only_render_mask"})
            continue
        if cookie is not None:
            audit.append(base | {"status": "unsupported_cookie_projection", "cookie_path": cookie})
            continue
        if created >= max_light_objects:
            audit.append(base | {"status": "truncated_light_limit", "limit": max_light_objects})
            continue
        position = _vector(_field(row, "position", context), 3, f"{context}.position")
        euler = _vector(_field(row, "euler_zxy", context), 3, f"{context}.euler_zxy")
        scale = _vector(_field(row, "scale", context), 3, f"{context}.scale")
        color = _vector(_field(row, "color", context), 4, f"{context}.color")
        intensity = _number(_field(row, "intensity", context), f"{context}.intensity")
        radius = _number(_field(row, "range", context), f"{context}.range")
        outer = _number(_field(row, "spot_angle", context), f"{context}.spot_angle")
        inner = _number(_field(row, "inner_spot_angle", context), f"{context}.inner_spot_angle")
        linear_length = _number(_field(row, "linear_length", context), f"{context}.linear_length")
        temperature = _number(_field(row, "color_temperature", context), f"{context}.color_temperature")
        use_temperature = _bool(_field(row, "use_color_temperature", context), f"{context}.use_color_temperature")
        enable_obb = _bool(_field(row, "enable_obb", context), f"{context}.enable_obb")
        shadow_mode = _source_shadow_mode(_field(row, "light_properties", context), context)
        if any(component < 0 for component in color[:3]) or intensity < 0 or radius <= 0:
            raise ValueError(f"Native Light source color, intensity or range invalid: {context}, color={color}, intensity={intensity}, range={radius}")
        if light_type == 0 and not (0 <= inner <= outer < 180 and outer > 0):
            raise ValueError(f"Native Light spot cone invalid: {context}, inner={inner}, outer={outer}")
        data = bpy.data.lights.new(f"EF_LIGHT_{map_id}_{entity_id}", {0: "SPOT", 1: "SUN", 2: "POINT"}[light_type])
        color_pipeline, temperature_clamped, preview_color = _configure_color(data, color, use_temperature, temperature)
        factor = sun_factor if light_type == 1 else punctual_factor
        data.energy = intensity * factor
        data.use_shadow = shadow_mode != 0
        if light_type in (0, 2):
            _configure_punctual_range(data, radius, preview_color)
        if light_type == 0:
            data.spot_size = math.radians(outer)
            data.spot_blend = 1.0 - inner / outer
        obj = bpy.data.objects.new(data.name, data)
        collection.objects.link(obj)
        obj.matrix_world = _light_matrix(position, euler)
        obj["endfield_light_map"] = map_id
        obj["endfield_light_entity_id"] = str(entity_id)
        obj["endfield_source_intensity"] = intensity
        obj["endfield_source_intensity_unit"] = "unverified_source_scalar"
        obj["endfield_preview_energy_per_source_intensity"] = factor
        obj["endfield_preview_energy_unit"] = "W/m2" if light_type == 1 else "W"
        obj["endfield_source_color_space"] = "unverified_source_rgba"
        obj["endfield_source_color_temperature_kelvin"] = temperature
        obj["endfield_source_use_color_temperature"] = use_temperature
        obj["endfield_preview_color_pipeline"] = color_pipeline
        obj["endfield_preview_temperature_clamped"] = temperature_clamped
        obj["endfield_preview_range_pipeline"] = ("eevee_custom_distance_cycles_ray_length_hard_cutoff"
                                                    if light_type in (0, 2) else "directional_unbounded")
        obj["endfield_runtime_activation"] = "unverified"
        obj["endfield_source_scale"] = list(scale)
        obj["endfield_source_shadow_mode"] = shadow_mode
        obj["endfield_preview_casts_shadow"] = data.use_shadow
        obj["endfield_source_baseline_sha256"] = str(base["baseline_sha256"])
        limitations = ["runtime_activation_unverified", "source_intensity_unit_unverified",
                       "source_color_space_unverified", "attenuation_not_game_calibrated",
                       "source_shadow_strength_and_bake_state_not_reconstructed"]
        if shadow_mode == 2:
            limitations.append("soft_shadow_filter_not_reconstructed")
        if light_type in (0, 2):
            limitations.append("punctual_hard_range_cutoff_not_game_attenuation")
        if enable_obb:
            limitations.append("obb_spatial_culling_not_reconstructed")
        if linear_length >= 0:
            limitations.append("linear_emitter_length_not_reconstructed")
        if any(abs(component - 1.0) > 1e-6 for component in scale):
            limitations.append("source_transform_scale_not_applied_to_light_profile")
        if light_type == 0:
            limitations.append("spot_soft_edge_approximation")
        if use_temperature:
            limitations.append("materialx_planckian_not_game_color_conversion_calibrated")
        if temperature_clamped:
            limitations.append("color_temperature_outside_materialx_valid_range_clamped")
        created += 1
        audit.append(base | {"status": "source_light_preview_partial", "object": obj.name,
                             "source_type": light_type, "source_position_unity": list(position),
                             "source_forward_unity": list(_zxy_quaternion(euler) @ Vector((0.0, 0.0, 1.0))),
                             "source_color_rgba": list(color), "source_intensity": intensity,
                             "source_color_temperature_kelvin": temperature,
                             "source_use_color_temperature": use_temperature,
                             "preview_color_pipeline": color_pipeline,
                             "preview_color_rgb": list(preview_color),
                             "preview_temperature_clamped": temperature_clamped,
                             "preview_energy_watts": data.energy,
                             "preview_energy_per_source_intensity": factor,
                             "preview_energy_unit": "W/m2" if light_type == 1 else "W",
                             "source_range": radius, "source_spot_angle": outer,
                             "source_shadow_mode": shadow_mode,
                             "preview_casts_shadow": data.use_shadow,
                             "preview_range_pipeline": ("eevee_custom_distance_cycles_ray_length_hard_cutoff"
                                                        if light_type in (0, 2) else "directional_unbounded"),
                             "source_inner_spot_angle": inner, "limitations": limitations})
    return audit
