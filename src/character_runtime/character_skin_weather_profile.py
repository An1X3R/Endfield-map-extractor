"""Attach the compiled non-SDF Skin rain branch from an explicit private profile."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Callable, Mapping

import bpy

from attach_character_weather_surface import WeatherProjection, bind_skin_wet_surface, bind_weather_surface, attach_weather_clock
from character_node_graph import math_node, node
from character_skin_rain import build_skin_rain
from character_weather_nodes import (
    WeatherOverride, WeatherUniforms, make_native_skin_snow, make_weather_controls,
    make_weather_controls_from_per_draw,
)
from character_weather_per_draw import WeatherPerDrawSource, decode_weather_per_draw_bits
from character_material_profile import ShaderIdentity, profile_shader_identity
from patch_character_hgrp import source_keywords
from review_character_hgrp import visible_source_group_names
from character_windows_paths import io_path


PROFILE_KEY = "skin_rain"
PROFILE_FORMAT = "EndfieldSkinRainProfile/1"
BINDING_MARKER = "HGRPSkinRainProfile/1"
SOURCE_FAMILY = "HGRP/CharacterNPR_Skin"
WET_BINDING = "HGRPSkinRainSurface/1"
DRY_BYPASS_FORMULA = "saturate(rain + max(global_wetness, water_submersion)) - source_disable > 0.01"
NATIVE_BYPASS_FORMULA = "max(saturate(rain + max(global_wetness, water_submersion)), snow) - source_disable > 0.01"
NATIVE_SKIN_KEYWORDS = frozenset(("_DIFF_RAMP_ON", "_NORMALMAP", "_SHADOW_LUT_TEX"))


@dataclass(frozen=True)
class SkinRainProfile:
    shader_identity: ShaderIdentity
    shader_sha256: str
    position_attribute: str
    normal_attribute: str
    orientation: float
    tiling: float
    time_x: float
    gravity_mode: str
    projection_provenance: str


@dataclass(frozen=True)
class RootUsage:
    material_tree: bpy.types.NodeTree
    source_instance: bpy.types.Node
    material_names: frozenset[str]
    object_names: frozenset[str]


def file_digest(path: Path) -> str:
    with io_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def object_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be a JSON object with string keys")
    return value


def string_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def number_value(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def verified_file(parent: Path, value: object, digest: object, label: str) -> Path:
    relative_path = string_value(value, label + " path")
    expected_digest = string_value(digest, label + " SHA-256").casefold()
    if len(expected_digest) != 64 or any(character not in "0123456789abcdef" for character in expected_digest):
        raise ValueError(f"{label} SHA-256 must contain 64 hexadecimal characters")
    path = io_path(parent / relative_path).resolve(strict=True)
    actual_digest = file_digest(path)
    if actual_digest != expected_digest:
        raise ValueError(f"{label} SHA-256 mismatch: path={path}; expected={expected_digest}; actual={actual_digest}")
    return path


def read_profile(profile_path: Path) -> tuple[SkinRainProfile | None, str]:
    profile = io_path(profile_path).resolve(strict=True)
    document = object_value(json.loads(io_path(profile).read_text(encoding="utf-8")), "character profile")
    raw_config = document.get(PROFILE_KEY)
    if raw_config is None:
        return None, file_digest(profile)
    config = object_value(raw_config, "Skin rain profile")
    if config.get("format") != PROFILE_FORMAT:
        raise ValueError(f"Unsupported Skin rain profile format: {config.get('format')!r}")
    if config.get("source_family") != SOURCE_FAMILY:
        raise ValueError(f"Skin rain profile source_family must equal {SOURCE_FAMILY}")
    shader_identity = profile_shader_identity(profile, SOURCE_FAMILY)
    position_attribute = string_value(config.get("position_attribute"), "Skin projection position attribute")
    normal_attribute = string_value(config.get("normal_attribute"), "Skin projection normal attribute")
    if position_attribute == normal_attribute or not position_attribute.startswith("EF_EXPERIMENTAL_") or not normal_attribute.startswith("EF_EXPERIMENTAL_"):
        raise ValueError("Skin projection attributes must be distinct EF_EXPERIMENTAL_* fields")
    orientation = number_value(config.get("orientation"), "Skin projection orientation")
    if orientation not in (0.0, 1.0):
        raise ValueError("Skin projection orientation must be explicitly 0 or 1")
    tiling = number_value(config.get("tiling"), "Skin projection tiling")
    if not 0.0 < tiling < 1000.0:
        raise ValueError("Skin projection tiling must be greater than zero and below 1000")
    time_x = number_value(config.get("time_x"), "Skin preview time X")
    gravity_mode = string_value(config.get("gravity_mode"), "Skin gravity binding")
    if gravity_mode != "host_geometric_normal_projection":
        raise ValueError("Skin gravity binding only supports the audited host geometric-normal projection")
    provenance = string_value(config.get("projection_provenance"), "Skin projection provenance")
    provenance_lower = provenance.casefold()
    if "not captured" not in provenance_lower and "unverified" not in provenance_lower:
        raise ValueError("Skin projection provenance must state that native source bindings are not captured or verified")
    if config.get("dry_default") is not True:
        raise ValueError("Skin rain profile must require a dry default")
    defaults = object_value(config.get("state_defaults"), "Skin weather defaults")
    expected_defaults = {"rain_intensity": 0.0, "water_submersion": 0.0, "global_wetness": 0.0}
    for name, expected in expected_defaults.items():
        if number_value(defaults.get(name), "Skin weather default " + name) != expected:
            raise ValueError(f"Skin rain profile must keep {name} at zero by default")
    return SkinRainProfile(shader_identity, shader_identity["object_sha256"], position_attribute, normal_attribute,
                           orientation, tiling, time_x, gravity_mode, provenance), file_digest(profile)


def visible_material_trees() -> tuple[tuple[bpy.types.Object, bpy.types.Material, bpy.types.NodeTree], ...]:
    rows = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.hide_render:
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is not None and material.use_nodes and material.node_tree is not None:
                rows.append((obj, material, material.node_tree))
    return tuple(rows)


def matrix_equal(left: bpy.types.Object, right: bpy.types.Object) -> bool:
    return max(abs(left.matrix_world[row][column] - right.matrix_world[row][column])
               for row in range(4) for column in range(4)) <= 1e-6


def ensure_projection_attributes(objects: tuple[bpy.types.Object, ...], profile: SkinRainProfile) -> list[dict[str, object]]:
    unique: dict[int, bpy.types.Object] = {}
    aliases: dict[int, list[str]] = {}
    for obj in objects:
        mesh_key = obj.data.as_pointer()
        prior = unique.get(mesh_key)
        if prior is not None and not matrix_equal(prior, obj):
            raise ValueError(f"Shared Skin weather mesh has incompatible object transforms: {prior.name}; {obj.name}")
        unique[mesh_key] = prior if prior is not None else obj
        aliases.setdefault(mesh_key, []).append(obj.name)
    rows = []
    for mesh_key, obj in unique.items():
        mesh = obj.data
        position = mesh.attributes.get(profile.position_attribute)
        normal = mesh.attributes.get(profile.normal_attribute)
        if (position is None) != (normal is None):
            raise ValueError(f"Skin projection has only one configured attribute: {mesh.name}")
        created = position is None
        if created:
            position = mesh.attributes.new(profile.position_attribute, "FLOAT_VECTOR", "POINT")
            normal = mesh.attributes.new(profile.normal_attribute, "FLOAT_VECTOR", "POINT")
        if position.domain != "POINT" or position.data_type != "FLOAT_VECTOR" or len(position.data) != len(mesh.vertices):
            raise ValueError(f"Skin position attribute schema is incompatible: {mesh.name}/{profile.position_attribute}")
        if normal.domain != "POINT" or normal.data_type != "FLOAT_VECTOR" or len(normal.data) != len(mesh.vertices):
            raise ValueError(f"Skin normal attribute schema is incompatible: {mesh.name}/{profile.normal_attribute}")
        position_matrix = obj.matrix_world.copy()
        normal_matrix = obj.matrix_world.to_3x3().inverted().transposed()
        for index, vertex in enumerate(mesh.vertices):
            position_value = position_matrix @ vertex.co
            normal_value = normal_matrix @ vertex.normal
            normal_value.normalize()
            expected_position = (position_value.x, position_value.z, -position_value.y)
            expected_normal = (normal_value.x, normal_value.z, -normal_value.y)
            if created:
                position.data[index].vector = expected_position
                normal.data[index].vector = expected_normal
            else:
                actual_position = position.data[index].vector
                actual_normal = normal.data[index].vector
                if max(abs(actual_position[axis] - expected_position[axis]) for axis in range(3)) > 1e-6:
                    raise ValueError(f"Existing Skin rest-space position differs: mesh={mesh.name}; vertex={index}")
                if max(abs(actual_normal[axis] - expected_normal[axis]) for axis in range(3)) > 1e-6:
                    raise ValueError(f"Existing Skin rest-space normal differs: mesh={mesh.name}; vertex={index}")
        rows.append({"mesh": mesh.name, "objects": sorted(aliases[mesh_key]), "vertices": len(mesh.vertices),
                     "position_attribute": profile.position_attribute, "normal_attribute": profile.normal_attribute,
                     "action": "created" if created else "reused_after_vertexwise_validation"})
    return rows


def weather_control_value(controls: bpy.types.ShaderNodeTree, label: str) -> float:
    matches = tuple(node for node in controls.nodes if node.type == "VALUE" and node.label == label)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one Skin weather control: group={controls.name}; label={label}; matches={len(matches)}")
    return float(matches[0].outputs[0].default_value)


def ensure_time_output(controls: bpy.types.ShaderNodeTree, time_x: float,
                       layout: Callable[[bpy.types.NodeTree], None]) -> bpy.types.ShaderNodeTree:
    values = tuple(node for node in controls.nodes if node.type == "VALUE" and node.label == "Preview Time X")
    if len(values) > 1:
        raise ValueError(f"Skin weather time control is ambiguous: {controls.name}")
    if values:
        value = values[0]
        if float(value.outputs[0].default_value) != time_x:
            raise ValueError(f"Existing Skin preview time differs from profile: {controls.name}")
    else:
        value = controls.nodes.new("ShaderNodeValue")
        value.label = "Preview Time X"
        value.outputs[0].default_value = time_x
    outputs = tuple(node for node in controls.nodes if node.type == "GROUP_OUTPUT")
    if len(outputs) != 1:
        raise ValueError(f"Skin weather controls need one group output: {controls.name}")
    output = outputs[0]
    sockets = tuple(socket for socket in controls.interface.items_tree
                    if socket.item_type == "SOCKET" and socket.in_out == "OUTPUT" and socket.name == "Native Time X")
    if len(sockets) > 1:
        raise ValueError(f"Skin weather time output is ambiguous: {controls.name}")
    if not sockets:
        controls.interface.new_socket(name="Native Time X", in_out="OUTPUT", socket_type="NodeSocketFloat")
    input_socket = output.inputs.get("Native Time X")
    if input_socket is None:
        raise ValueError(f"Skin weather time output socket was not created: {controls.name}")
    if not input_socket.is_linked:
        controls.links.new(value.outputs[0], input_socket)
    elif len(input_socket.links) != 1 or input_socket.links[0].from_socket != value.outputs[0]:
        timeline = controls.nodes.get("EF Skin Rain Timeline Offset")
        if timeline is None or input_socket.links[0].from_socket != timeline.outputs[0]:
            raise ValueError(f"Existing Skin weather time output has a different producer: {controls.name}")
    controls["endfield_skin_rain_controls"] = "HGRPSkinRainControls/1"
    controls["endfield_skin_rain_time_x_default"] = time_x
    controls["endfield_skin_rain_dry_default"] = True
    layout(controls)
    return controls


def attach_skin_weather_timeline(controls: bpy.types.ShaderNodeTree,
                                 layout: Callable[[bpy.types.NodeTree], None]) -> None:
    """Reuse the weather clock while keeping the profile time as an editable offset."""
    if controls.get("endfield_skin_rain_timeline"):
        return
    clock = bpy.data.node_groups.new("EF Skin Rain Timeline", "ShaderNodeTree")
    clock.nodes.new("NodeGroupOutput")
    attach_weather_clock(clock, bpy.context.scene, 0.05,
                         "Editable Blender timeline; existing Skin kernel Time X uses seconds / 20. Not captured game time.")
    instance = node(controls, "ShaderNodeGroup", "Rain timeline - speed inside", "Explicit Weather Clock")
    instance.node_tree = clock
    offset = next(item for item in controls.nodes if item.type == "VALUE" and item.label == "Preview Time X")
    time = math_node(controls, "ADD", offset.outputs[0], instance.outputs["Native Time X"],
                     "Profile time offset plus timeline", "Explicit Weather Clock")
    time.node.name = "EF Skin Rain Timeline Offset"
    output = next(item for item in controls.nodes if item.type == "GROUP_OUTPUT")
    controls.links.new(time, output.inputs["Native Time X"])
    controls["endfield_skin_rain_timeline"] = clock.name
    layout(clock)
    layout(controls)


def ensure_weather_controls(
    time_x: float,
    layout: Callable[[bpy.types.NodeTree], None],
    weather_source: WeatherPerDrawSource | None,
    weather_source_sha256: str | None,
) -> bpy.types.ShaderNodeTree:
    if (weather_source is None) != (weather_source_sha256 is None):
        raise ValueError("Skin per-draw source and its SHA-256 must be supplied together")
    if weather_source is not None:
        if (not isinstance(weather_source_sha256, str) or len(weather_source_sha256) != 64
                or any(character not in "0123456789abcdef" for character in weather_source_sha256)):
            raise ValueError("Skin per-draw source requires a lowercase SHA-256")
        controls = make_weather_controls_from_per_draw(
            weather_source, WeatherOverride(0.0, (0.0, 0.0, 0.0, 0.0), 0.0), layout)
        controls["source_per_draw_input_sha256"] = weather_source_sha256
        controls["endfield_skin_weather_source_kind"] = "explicit_native_per_draw_word"
        controls["endfield_skin_weather_runtime_capture_verified"] = False
        controls["endfield_skin_weather_projection_verified"] = False
        return ensure_time_output(controls, time_x, layout)
    matches = tuple(group for group in bpy.data.node_groups
                    if group.get("endfield_skin_rain_controls") == "HGRPSkinRainControls/1"
                    or ((group.get("endfield_standard_wetfilm_controls") is True
                         or group.get("endfield_character_weather_controls") is True)
                        and group.get("source_input_kind") != "explicit_native_per_draw_word"))
    if len(matches) > 1:
        raise ValueError(f"Skin rain requires one shared weather-control group; found {[group.name for group in matches]}")
    if matches:
        controls = matches[0]
    else:
        provenance = "Explicit artist weather controls; defaults are dry; runtime per-draw state is not captured."
        controls = make_weather_controls(WeatherUniforms((0.0, 0.0, 0.0, 0.0), 0.0, 0.0, 0.0, provenance),
                                         WeatherOverride(1.0, (0.0, 0.0, 0.0, 0.0), 0.0), layout)
    expected = {"User Override Weight": 1.0, "User Water Height": 0.0,
                "Source Water Height": 0.0, "Source Preview Enable": 0.0,
                "Source Preview Water Height": 0.0}
    expected.update({"Source Effect " + axis: 0.0 for axis in "XYZW"})
    expected.update({"User Effect " + axis: 0.0 for axis in "XYZW"})
    actual = {label: weather_control_value(controls, label) for label in expected}
    if actual != expected:
        raise ValueError(f"Skin rain weather-control defaults are not dry: group={controls.name}; actual={actual}")
    controls = ensure_time_output(controls, time_x, layout)
    attach_skin_weather_timeline(controls, layout)
    return controls


def load_skin_snow_texture(
    profile_path: Path,
    standard_snow_receipt: Mapping[str, object],
) -> tuple[bpy.types.Image, dict[str, object]]:
    """Load the exact configured common snow texture and cross-check any reused Standard image."""
    profile = object_value(json.loads(io_path(profile_path).read_text(encoding="utf-8")), "character profile")
    config = object_value(profile.get("standard_snow"), "Skin snow requires the configured standard_snow source record")
    if config.get("format") != "EndfieldStandardSnowProfile/1" or config.get("texture_slot") != "characterSnowTexture":
        raise ValueError("Skin snow requires the evidenced characterSnowTexture profile slot")
    wetfilm = object_value(profile.get("standard_wet_film"), "Skin snow requires the shared standard_wet_film texture manifest")
    manifest = verified_file(profile_path.parent, wetfilm.get("texture_manifest"),
                             wetfilm.get("texture_manifest_sha256"), "Skin snow texture manifest")
    manifest_rows = json.loads(io_path(manifest).read_text(encoding="utf-8"))
    if not isinstance(manifest_rows, list):
        raise TypeError("Skin snow texture manifest must be an array")
    matches = tuple(object_value(row, "Skin snow texture row") for row in manifest_rows
                    if isinstance(row, dict) and row.get("slot") == "characterSnowTexture")
    if len(matches) != 1:
        raise ValueError(f"Skin snow manifest must identify one characterSnowTexture; matches={len(matches)}")
    row = matches[0]
    if type(row.get("color_space")) is not int or row.get("color_space") != 0:
        raise ValueError("Skin snow texture must be marked as linear source data")
    source_identity = object_value(row.get("source"), "Skin snow source texture identity")
    if (type(source_identity.get("path_id")) is not int
            or not isinstance(source_identity.get("source"), str) or not source_identity.get("source")):
        raise ValueError("Skin snow source texture identity is incomplete")
    texture = verified_file(manifest.parent, row.get("artifact"), row.get("sha256"), "Skin snow texture")
    texture_sha256 = string_value(row.get("sha256"), "Skin snow texture SHA-256").casefold()
    receipt_files_value = standard_snow_receipt.get("source_files", [])
    if not isinstance(receipt_files_value, list):
        raise TypeError("Standard snow receipt source_files must be an array")
    receipt_files = tuple(object_value(item, "Standard snow source artifact") for item in receipt_files_value)
    for expected_path, expected_sha256 in ((manifest, file_digest(manifest)), (texture, texture_sha256)):
        matching_files = tuple(item for item in receipt_files
                               if isinstance(item.get("path"), str)
                               and io_path(str(item["path"])).resolve(strict=True) == expected_path
                               and item.get("sha256") == expected_sha256)
        if receipt_files and len(matching_files) != 1:
            raise ValueError(f"Reused Standard snow receipt does not identify the exact Skin texture source: {expected_path}")
    image_name = standard_snow_receipt.get("packed_texture")
    image = bpy.data.images.get(image_name) if isinstance(image_name, str) else None
    reused_standard_image = image is not None
    if image is None:
        image = bpy.data.images.load(str(texture), check_existing=False)
        image.colorspace_settings.name = "Non-Color"
        image.alpha_mode = "CHANNEL_PACKED"
        image.pack()
    elif (standard_snow_receipt.get("texture_sha256") != texture_sha256
          or image.get("endfield_weather_source_sha256") != texture_sha256):
        raise ValueError("Reused Standard snow image is not the verified characterSnowTexture source")
    if image.packed_file is None:
        raise ValueError(f"Skin snow texture is not packed into the blend: {image.name}")
    image.colorspace_settings.name = "Non-Color"
    image.alpha_mode = "CHANNEL_PACKED"
    image["endfield_weather_source_sha256"] = texture_sha256
    image["endfield_skin_snow_source_identity_json"] = json.dumps(source_identity, separators=(",", ":"))
    if (standard_snow_receipt.get("texture_sha256") is not None
            and standard_snow_receipt.get("texture_sha256") != texture_sha256):
        raise ValueError("Reused Standard snow image SHA-256 differs from the configured Skin snow slot")
    return image, {"slot": "characterSnowTexture", "source_identity": source_identity,
                   "texture_manifest": str(manifest), "texture_manifest_sha256": file_digest(manifest),
                   "texture_path": str(texture), "texture_sha256": texture_sha256,
                   "packed_image": image.name, "standard_image_reused": reused_standard_image}


def connect_exact_dry_bypass(material_tree: bpy.types.NodeTree, dry_instance: bpy.types.Node,
                             wet_group: bpy.types.ShaderNodeTree, controls: bpy.types.ShaderNodeTree,
                             disable_weather: float, source_name: str,
                             include_snow_gate: bool) -> dict[str, object]:
    targets = tuple(link.to_socket for link in dry_instance.outputs[0].links)
    if not targets:
        raise ValueError(f"Skin source output has no root material consumers: {source_name}")
    state = material_tree.nodes.new("ShaderNodeGroup")
    state.label = "Skin explicit weather controls"
    state.node_tree = controls
    geometry = material_tree.nodes.new("ShaderNodeNewGeometry")
    geometry.label = "Skin source weather world position"
    height = material_tree.nodes.new("ShaderNodeSeparateXYZ")
    height.label = "Skin source weather world height"
    material_tree.links.new(geometry.outputs["Position"], height.inputs[0])
    material_tree.links.new(height.outputs["Z"], state.inputs["World Height"])
    rain = material_tree.nodes.new("ShaderNodeGroup")
    rain.label = "Compiled Skin rain candidate"
    rain.node_tree = wet_group
    maximum = material_tree.nodes.new("ShaderNodeMath")
    maximum.operation = "MAXIMUM"
    maximum.label = "Skin rain max wetness/submersion"
    material_tree.links.new(state.outputs["Native Global Wetness"], maximum.inputs[0])
    material_tree.links.new(state.outputs["Native Water Submersion"], maximum.inputs[1])
    total = material_tree.nodes.new("ShaderNodeMath")
    total.operation = "ADD"
    total.label = "Skin rain input sum"
    material_tree.links.new(state.outputs["Native Rain Intensity"], total.inputs[0])
    material_tree.links.new(maximum.outputs[0], total.inputs[1])
    lower = material_tree.nodes.new("ShaderNodeMath")
    lower.operation = "MAXIMUM"
    lower.label = "Skin rain saturate lower"
    lower.inputs[1].default_value = 0.0
    material_tree.links.new(total.outputs[0], lower.inputs[0])
    upper = material_tree.nodes.new("ShaderNodeMath")
    upper.operation = "MINIMUM"
    upper.label = "Skin rain saturate upper"
    upper.inputs[1].default_value = 1.0
    material_tree.links.new(lower.outputs[0], upper.inputs[0])
    subtract_disable = material_tree.nodes.new("ShaderNodeMath")
    subtract_disable.operation = "SUBTRACT"
    subtract_disable.label = "Skin rain source-disable subtraction"
    subtract_disable.inputs[1].default_value = disable_weather
    material_tree.links.new(upper.outputs[0], subtract_disable.inputs[0])
    gate = material_tree.nodes.new("ShaderNodeMath")
    gate.operation = "GREATER_THAN"
    gate.label = "Skin rain source activation gate"
    gate.inputs[1].default_value = 0.01
    material_tree.links.new(subtract_disable.outputs[0], gate.inputs[0])
    final_gate = gate
    snow_gate = None
    snow_subtract = None
    if include_snow_gate:
        snow_subtract = material_tree.nodes.new("ShaderNodeMath")
        snow_subtract.operation = "SUBTRACT"
        snow_subtract.label = "Skin snow source-disable subtraction"
        material_tree.links.new(state.outputs["Native Snow Intensity"], snow_subtract.inputs[0])
        snow_subtract.inputs[1].default_value = disable_weather
        snow_gate = material_tree.nodes.new("ShaderNodeMath")
        snow_gate.operation = "GREATER_THAN"
        snow_gate.label = "Skin snow source activation gate"
        snow_gate.inputs[1].default_value = 0.01
        material_tree.links.new(snow_subtract.outputs[0], snow_gate.inputs[0])
        final_gate = material_tree.nodes.new("ShaderNodeMath")
        final_gate.operation = "MAXIMUM"
        final_gate.label = "Skin rain or snow activation gate"
        material_tree.links.new(gate.outputs[0], final_gate.inputs[0])
        material_tree.links.new(snow_gate.outputs[0], final_gate.inputs[1])
    mix = material_tree.nodes.new("ShaderNodeMixShader")
    mix.label = "Skin exact dry bypass"
    material_tree.links.new(final_gate.outputs[0], mix.inputs[0])
    material_tree.links.new(dry_instance.outputs[0], mix.inputs[1])
    material_tree.links.new(rain.outputs[0], mix.inputs[2])
    for target in targets:
        material_tree.links.new(mix.outputs[0], target)
    roles = ((state, "weather_controls"), (rain, "wet_group"), (maximum, "wetness_maximum"),
             (total, "rain_wetness_sum"), (lower, "saturate_lower"), (upper, "saturate_upper"),
             (subtract_disable, "source_disable"), (gate, "rain_activation_gate"), (mix, "exact_dry_selector"))
    if snow_gate is not None and final_gate is not gate:
        roles += ((snow_subtract, "snow_source_disable"), (snow_gate, "snow_activation_gate"),
                  (final_gate, "combined_activation_gate"))
    for node in (geometry, height):
        node["endfield_skin_rain_binding"] = BINDING_MARKER
        node["endfield_skin_rain_role"] = "world_height_source" if node == height else "world_position_source"
    for node, role in roles:
        node["endfield_skin_rain_binding"] = BINDING_MARKER
        node["endfield_skin_rain_role"] = role
    dry_instance["endfield_skin_rain_binding"] = BINDING_MARKER
    dry_instance["endfield_skin_rain_role"] = "dry_source_group"
    return {"dry_instance": dry_instance.name, "wet_instance": rain.name,
            "state_instance": state.name, "gate_node": final_gate.name, "rain_gate_node": gate.name,
            "snow_gate_node": snow_gate.name if snow_gate is not None else None,
            "snow_disable_node": snow_subtract.name if snow_subtract is not None else None,
            "world_height_node": height.name, "selector_node": mix.name,
            "source_output": dry_instance.outputs[0].name, "consumer_count": len(targets)}


def root_usages(groups: tuple[bpy.types.ShaderNodeTree, ...]) -> tuple[RootUsage, ...]:
    group_pointers = {group.as_pointer(): group for group in groups}
    usage: dict[tuple[int, int], RootUsage] = {}
    for obj, material, tree in visible_material_trees():
        if tree.get("endfield_skin_rain_binding") == BINDING_MARKER:
            raise ValueError(f"Skin rain profile is already attached to a visible root material: {tree.name}")
        for node in tree.nodes:
            if node.type != "GROUP" or node.node_tree is None or node.node_tree.as_pointer() not in group_pointers:
                continue
            key = tree.as_pointer(), node.as_pointer()
            prior = usage.get(key)
            if prior is None:
                usage[key] = RootUsage(tree, node, frozenset((material.name,)), frozenset((obj.name,)))
            else:
                usage[key] = RootUsage(tree, node, prior.material_names | {material.name}, prior.object_names | {obj.name})
    return tuple(usage.values())


def skin_weather_source_record(
    weather_source: WeatherPerDrawSource | None,
    weather_source_sha256: str | None,
) -> dict[str, object]:
    if weather_source is None:
        if weather_source_sha256 is not None:
            raise ValueError("Dry Skin weather cannot have a per-draw input hash")
        return {"kind": "explicit_dry_artist_values", "runtime_capture_verified": False}
    if (not isinstance(weather_source_sha256, str) or len(weather_source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in weather_source_sha256)):
        raise ValueError("Skin per-draw source requires a lowercase SHA-256")
    return {
        "kind": "explicit_native_per_draw_word",
        "input_sha256": weather_source_sha256,
        "packed_word_bits": weather_source.packed_word_bits,
        "packed_word_bits_hex": f"0x{weather_source.packed_word_bits:08x}",
        "channels_u8": {"rain": weather_source.channels.rain, "wet": weather_source.channels.wet,
                        "global_wet": weather_source.channels.global_wet, "snow": weather_source.channels.snow},
        "height_float32": weather_source.wet_effect_world_space_height,
        "height_float32_bits_hex": f"0x{weather_source.wet_effect_world_space_height_bits:08x}",
        "provenance": weather_source.provenance,
        "source_preview_enable": 0.0,
        "user_override_weight_default": 0.0,
        "runtime_capture_verified": False,
    }


def attach_profile_skin_rain(
    profile_path: Path,
    layout: Callable[[bpy.types.NodeTree], None],
    weather_source: WeatherPerDrawSource | None,
    weather_source_sha256: str | None,
    standard_snow_receipt: Mapping[str, object],
) -> dict[str, object]:
    """Attach the exact non-SDF Skin weather route with caller-supplied controls."""
    if (weather_source is None) != (weather_source_sha256 is None):
        raise ValueError("Skin per-draw source and its SHA-256 must be supplied together")
    profile, profile_sha256 = read_profile(profile_path)
    if profile is None:
        return {"format": "EndfieldSkinRainReceipt/1", "status": "not_configured",
                "weather_source_kind": "explicit_dry_artist_values" if weather_source is None else "explicit_native_per_draw_word"}
    active_names = visible_source_group_names()
    groups = tuple(group for group in bpy.data.node_groups
                   if group.name in active_names and group.get("source_shader") == SOURCE_FAMILY)
    exclusions: list[dict[str, object]] = []
    supported = []
    for group in groups:
        keywords = source_keywords(group)
        if "_SDFLIGHTMAP" in keywords:
            exclusions.append({"source_group": group.name, "source_keywords": list(keywords), "reason": "sdf_face_branch_not_supported"})
            continue
        if "_CHARACTER_FUR" in keywords:
            exclusions.append({"source_group": group.name, "source_keywords": list(keywords), "reason": "character_fur_branch_not_supported"})
            continue
        if weather_source is not None and frozenset(keywords) != NATIVE_SKIN_KEYWORDS:
            exclusions.append({"source_group": group.name, "source_keywords": list(keywords),
                               "reason": "skin_native_per_draw_variant_not_exactly_reflected"})
            continue
        if group.get("wet_surface_binding") or group.get("weather_surface_binding") or group.get("endfield_skin_rain_binding"):
            raise ValueError(f"Skin rain cannot be attached twice or after another weather surface: {group.name}")
        source_floats = object_value(json.loads(group["source_floats"]), "Skin source floats " + group.name)
        disable = number_value(source_floats.get("_DisableRainEffectOnMaterial"), group.name + "/_DisableRainEffectOnMaterial")
        if not 0.0 <= disable <= 1.0:
            raise ValueError(f"Skin weather-disable source value must be within [0,1]: {group.name}; value={disable}")
        if disable == 1.0:
            exclusions.append({"source_group": group.name, "source_keywords": list(keywords), "reason": "source_weather_disabled"})
            continue
        supported.append((group, keywords, disable))
    if not supported:
        status = "no_supported_skin_materials" if groups else "no_matching_skin_materials"
        return {"format": "EndfieldSkinRainReceipt/1", "status": status, "excluded_groups": exclusions,
                "weather_source_kind": "explicit_native_per_draw_word" if weather_source is not None else "explicit_dry_artist_values",
                "weather_source": skin_weather_source_record(weather_source, weather_source_sha256),
                "native_runtime_state_verified": False, "native_projection_verified": False}
    candidate_groups = tuple(row[0] for row in supported)
    usages = root_usages(candidate_groups)
    if not usages:
        raise ValueError("Supported non-SDF Skin groups have no visible root material instances")
    usage_by_source = {group.as_pointer(): tuple(item for item in usages if item.source_instance.node_tree == group)
                       for group in candidate_groups}
    if any(not usage_by_source[group.as_pointer()] for group in candidate_groups):
        raise ValueError("At least one visible Skin source group has no root group instance")
    used_objects = {name: bpy.data.objects[name] for usage in usages for name in usage.object_names}
    projection_rows = ensure_projection_attributes(tuple(used_objects.values()), profile)
    controls = ensure_weather_controls(profile.time_x, layout, weather_source, weather_source_sha256)
    if weather_source is not None:
        controls["endfield_skin_rain_dry_default"] = False
    shader_hash = profile.shader_sha256
    kernel = build_skin_rain(shader_hash, layout)
    kernel["endfield_skin_rain_kernel"] = True
    kernel["endfield_skin_rain_profile_format"] = PROFILE_FORMAT
    kernel["endfield_skin_rain_profile_sha256"] = profile_sha256
    kernel["endfield_skin_rain_projection_provenance"] = profile.projection_provenance
    kernel["endfield_skin_rain_native_projection_verified"] = False
    projection = WeatherProjection(profile.position_attribute, profile.normal_attribute, profile.orientation,
                                   profile.tiling, profile.projection_provenance)
    skin_snow = None
    snow_source_record = None
    snow_configured = weather_source is not None or standard_snow_receipt.get("status") in (
        "standard_snow_connected_dry_default", "standard_snow_connected_native_per_draw", "no_matching_standard_materials")
    if snow_configured and any(frozenset(keywords) == NATIVE_SKIN_KEYWORDS for _, keywords, _ in supported):
        snow_image, snow_source_record = load_skin_snow_texture(profile_path, standard_snow_receipt)
        skin_snow = make_native_skin_snow(snow_image, shader_hash, layout)
        skin_snow["endfield_skin_rain_profile_sha256"] = profile_sha256
        skin_snow["endfield_skin_rain_shader_sha256"] = shader_hash
        skin_snow["endfield_skin_snow_texture_sha256"] = snow_source_record["texture_sha256"]
    binding_rows: list[dict[str, object]] = []
    trees: dict[int, tuple[bpy.types.NodeTree, list[dict[str, object]]]] = {}
    for source_group, keywords, disable in supported:
        for usage in usage_by_source[source_group.as_pointer()]:
            dry_group = source_group.copy()
            dry_group.name = source_group.name + " / Skin rain exact dry"
            wet_group = source_group.copy()
            wet_group.name = source_group.name + " / Skin rain compiled wet"
            usage.source_instance.node_tree = dry_group
            dry_group["endfield_skin_rain_binding"] = BINDING_MARKER
            dry_group["endfield_skin_rain_role"] = "exact_source_dry_group"
            dry_group["endfield_skin_rain_profile_sha256"] = profile_sha256
            source_identity = json.loads(source_group["source_identity"])
            source_keywords_json = json.dumps(list(keywords), separators=(",", ":"))
            source_floats = object_value(json.loads(source_group["source_floats"]), "Skin source floats " + source_group.name)
            serialized_weather = {
                key: number_value(source_floats[key], source_group.name + "/" + key) if key in source_floats else None
                for key in ("_DisableRainEffectOnMaterial", "_RainEffectIntensity", "_WetEffectIntensity",
                            "_WetEffectGlobalIntensity", "_WetEffectWorldSpaceHeight")
            }
            selector_boundary = {
                "per_draw_input_selected": weather_source is not None,
                "source_preview_enable": 0.0 if weather_source is not None else None,
                "height_source": "raw_per_draw_Vector4.y" if weather_source is not None else "editable_artist_controls",
                "serialized_material_values": serialized_weather,
                "serialized_height_is_runtime_state": False,
                "global_preview_selector_runtime_capture_verified": False,
            }
            dry_group["endfield_skin_rain_source_keywords_json"] = source_keywords_json
            dry_group["endfield_skin_rain_source_identity_json"] = json.dumps(source_identity, separators=(",", ":"))
            binding = bind_skin_wet_surface(wet_group, kernel, controls, projection, layout)
            if wet_group.get("wet_surface_binding") != WET_BINDING:
                raise AssertionError(f"Skin rain adapter did not bind the compiled surface: {wet_group.name}")
            wet_group["endfield_skin_rain_binding"] = BINDING_MARKER
            wet_group["endfield_skin_rain_role"] = "compiled_non_sdf_skin_rain_group"
            wet_group["endfield_skin_rain_profile_sha256"] = profile_sha256
            wet_group["endfield_skin_rain_shader_sha256"] = shader_hash
            wet_group["endfield_skin_rain_source_keywords_json"] = source_keywords_json
            wet_group["endfield_skin_rain_source_identity_json"] = json.dumps(source_identity, separators=(",", ":"))
            wet_group["endfield_skin_rain_dry_group"] = dry_group.name
            wet_group["endfield_skin_rain_controls"] = controls.name
            wet_group["endfield_skin_rain_dry_default"] = weather_source is None
            wet_group["endfield_skin_rain_native_projection_verified"] = False
            wet_group["endfield_skin_rain_native_runtime_state_verified"] = False
            wet_group["endfield_skin_weather_selector_boundary_json"] = json.dumps(selector_boundary, separators=(",", ":"))
            if weather_source is not None:
                wet_group["endfield_skin_weather_source_sha256"] = weather_source_sha256
            wet_group["endfield_skin_rain_position_attribute"] = profile.position_attribute
            wet_group["endfield_skin_rain_normal_attribute"] = profile.normal_attribute
            wet_group["endfield_skin_rain_orientation"] = profile.orientation
            wet_group["endfield_skin_rain_tiling"] = profile.tiling
            wet_group["endfield_skin_rain_time_x"] = profile.time_x
            wet_group["endfield_skin_rain_gravity_mode"] = profile.gravity_mode
            wet_group["endfield_skin_rain_projection_provenance"] = profile.projection_provenance
            snow_binding = None
            if skin_snow is not None and frozenset(keywords) == NATIVE_SKIN_KEYWORDS:
                snow_binding = bind_weather_surface(wet_group, skin_snow, controls, projection, layout)
                wet_group["endfield_skin_snow_texture_sha256"] = snow_source_record["texture_sha256"]
                wet_group["endfield_skin_snow_source_identity_json"] = json.dumps(
                    snow_source_record["source_identity"], separators=(",", ":"))
            root_binding = connect_exact_dry_bypass(usage.material_tree, usage.source_instance, wet_group,
                                                    controls, disable, source_group.name, snow_binding is not None)
            usage.material_tree["endfield_skin_rain_binding"] = BINDING_MARKER
            usage.material_tree["endfield_skin_rain_profile_sha256"] = profile_sha256
            usage.material_tree["endfield_skin_rain_shader_sha256"] = shader_hash
            usage.material_tree["endfield_skin_rain_source_group"] = source_group.name
            usage.material_tree["endfield_skin_rain_dry_group"] = dry_group.name
            usage.material_tree["endfield_skin_rain_wet_group"] = wet_group.name
            usage.material_tree["endfield_skin_rain_controls"] = controls.name
            usage.material_tree["endfield_skin_rain_branch_formula"] = (
                NATIVE_BYPASS_FORMULA if snow_binding is not None else DRY_BYPASS_FORMULA)
            usage.material_tree["endfield_skin_rain_dry_default"] = weather_source is None
            usage.material_tree["endfield_skin_rain_native_projection_verified"] = False
            usage.material_tree["endfield_skin_rain_native_runtime_state_verified"] = False
            usage.material_tree["endfield_skin_rain_projection_provenance"] = profile.projection_provenance
            usage.material_tree["endfield_skin_weather_selector_boundary_json"] = json.dumps(
                selector_boundary, separators=(",", ":"))
            if weather_source is not None:
                usage.material_tree["endfield_skin_weather_source_sha256"] = weather_source_sha256
            layout(usage.material_tree)
            row = {"material_tree": usage.material_tree.name, "material_names": sorted(usage.material_names),
                   "object_names": sorted(usage.object_names), "source_group": source_group.name,
                   "source_identity": source_identity, "source_keywords": list(keywords), "source_disable_weather": disable,
                   "serialized_material_weather": serialized_weather, "selector_boundary": selector_boundary,
                   "dry_group": dry_group.name, "wet_group": wet_group.name,
                   "dry_instance": root_binding["dry_instance"], "wet_instance": root_binding["wet_instance"],
                   "state_instance": root_binding["state_instance"], "gate_node": root_binding["gate_node"],
                   "rain_gate_node": root_binding["rain_gate_node"], "snow_gate_node": root_binding["snow_gate_node"],
                   "snow_disable_node": root_binding["snow_disable_node"],
                   "world_height_node": root_binding["world_height_node"],
                   "selector_node": root_binding["selector_node"], "channels": binding["channels"],
                   "snow_channels": snow_binding["channels"] if snow_binding is not None else None}
            binding_rows.append(row)
            tree_rows = trees.setdefault(usage.material_tree.as_pointer(), (usage.material_tree, []))[1]
            tree_rows.append(row)
    for tree, rows in trees.values():
        tree["endfield_skin_rain_bindings_json"] = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    notes = bpy.data.texts.get("SKIN RAIN PROFILE SCOPE")
    if notes is not None:
        raise ValueError("Skin rain scope notes already exist; refusing to add a duplicate profile")
    notes = bpy.data.texts.new("SKIN RAIN PROFILE SCOPE")
    notes.write("Compiled HGRP non-SDF Skin procedural rain; exact source dry shader is selected when the compiled weather gate is inactive.\n"
                + ("Skin controls contain the caller-supplied per-draw word and height; set User Override Weight to 1 before editing User Effect X/Y/Z/W. This source is not a runtime capture.\n"
                   if weather_source is not None else
                   "Default weather controls are dry. Edit User Effect X for rain, Y for water submersion, Z for global wetness, or W for supported snow.\n")
                + ("Rain follows Blender FPS from frame 1. Preview Time X is the time offset; Native time units per second inside EF Skin Rain Timeline sets speed (0 freezes playback).\n"
                   if weather_source is None else "")
                + "Projection uses explicit experimental host rest-space attributes, orientation and tiling from the private profile.\n"
                + "The material height and global preview selector are serialized/static boundaries; native TEXCOORD8/TEXCOORD6, tangent gravity, projection and runtime parity are not verified.\n"
                + "SDF Face and FUR keyword branches are excluded.\n")
    return {"format": "EndfieldSkinRainReceipt/1",
            "status": "skin_weather_connected_explicit_per_draw" if weather_source is not None else "skin_rain_connected_dry_default",
            "weather_source_kind": "explicit_native_per_draw_word" if weather_source is not None else "explicit_dry_artist_values",
            "profile_path": str(profile_path.resolve(strict=True)), "profile_sha256": profile_sha256,
            "shader_identity": profile.shader_identity, "shader_sha256": shader_hash, "controls": controls.name,
            "dry_bypass_formula": NATIVE_BYPASS_FORMULA if skin_snow is not None else DRY_BYPASS_FORMULA,
            "dry_default": weather_source is None,
            "weather_source": skin_weather_source_record(weather_source, weather_source_sha256),
            "skin_snow_source": snow_source_record,
            "projection": {"position_attribute": profile.position_attribute, "normal_attribute": profile.normal_attribute,
                           "orientation": profile.orientation, "tiling": profile.tiling, "time_x": profile.time_x,
                           "gravity_mode": profile.gravity_mode, "provenance": profile.projection_provenance},
            "projection_attributes": projection_rows, "bindings": binding_rows, "excluded_groups": exclusions,
            "native_runtime_state_verified": False, "native_projection_verified": False,
            "native_time_verified": False, "preview_selector_runtime_capture_verified": False}


def single_link(socket: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
    if len(socket.links) != 1:
        raise AssertionError(f"Expected one saved Skin weather link: {label}; links={len(socket.links)}")
    return socket.links[0].from_socket


def linked_node(socket: bpy.types.NodeSocket, label: str) -> bpy.types.Node:
    return single_link(socket, label).node


def verify_profile_skin_rain(receipt: dict[str, object]) -> dict[str, object]:
    """Audit reopened Skin weather controls, exact branches and scalar source math."""
    status = string_value(receipt.get("status"), "Skin rain receipt status")
    if status in ("not_configured", "no_matching_skin_materials", "no_supported_skin_materials"):
        return {"status": status}
    if status not in ("skin_rain_connected_dry_default", "skin_weather_connected_explicit_per_draw") or receipt.get("format") != "EndfieldSkinRainReceipt/1":
        raise ValueError("Unrecognized Skin rain build receipt")
    profile_path = Path(string_value(receipt.get("profile_path"), "Skin rain profile path")).resolve(strict=True)
    profile_sha256 = string_value(receipt.get("profile_sha256"), "Skin rain profile SHA-256")
    if file_digest(profile_path) != profile_sha256:
        raise AssertionError(f"Skin rain profile changed after build: {profile_path}")
    shader_sha256 = string_value(receipt.get("shader_sha256"), "Skin shader SHA-256")
    shader_identity = object_value(receipt.get("shader_identity"), "Skin Shader identity")
    actual_shader_identity = profile_shader_identity(profile_path, SOURCE_FAMILY)
    if shader_identity != actual_shader_identity or shader_sha256 != actual_shader_identity["object_sha256"]:
        raise AssertionError("Skin Shader object identity changed after profile preparation")
    controls_name = string_value(receipt.get("controls"), "Skin weather controls group")
    controls = bpy.data.node_groups.get(controls_name)
    if controls is None or controls.get("endfield_skin_rain_controls") != "HGRPSkinRainControls/1":
        raise AssertionError(f"Saved Skin weather controls are missing: {controls_name}")
    weather_source = object_value(receipt.get("weather_source"), "Skin weather source receipt")
    source_kind = string_value(weather_source.get("kind"), "Skin weather source kind")
    native_source = source_kind == "explicit_native_per_draw_word"
    if source_kind not in ("explicit_native_per_draw_word", "explicit_dry_artist_values"):
        raise ValueError(f"Unsupported Skin weather source kind: {source_kind}")
    if native_source != (status == "skin_weather_connected_explicit_per_draw"):
        raise AssertionError("Skin weather status and source kind disagree")
    expected_controls = {"User Override Weight": 0.0 if native_source else 1.0,
                         "User Water Height": 0.0, "Source Preview Enable": 0.0,
                         "Source Preview Water Height": 0.0}
    if native_source:
        input_sha256 = string_value(weather_source.get("input_sha256"), "Skin per-draw input SHA-256")
        if len(input_sha256) != 64 or any(character not in "0123456789abcdef" for character in input_sha256):
            raise ValueError("Skin per-draw input SHA-256 must be lowercase hexadecimal")
        word_hex = string_value(weather_source.get("packed_word_bits_hex"), "Skin packed word raw bits")
        if len(word_hex) != 10 or not word_hex.startswith("0x"):
            raise ValueError("Skin packed word must preserve its uint32 raw bits")
        word_bits = int(word_hex, 16)
        if word_bits != weather_source.get("packed_word_bits"):
            raise AssertionError("Skin packed word raw-bit representations disagree")
        channels = decode_weather_per_draw_bits(word_bits)
        channels_record = object_value(weather_source.get("channels_u8"), "Skin decoded channel bytes")
        expected_bytes = {"rain": channels.rain, "wet": channels.wet,
                          "global_wet": channels.global_wet, "snow": channels.snow}
        if channels_record != expected_bytes:
            raise AssertionError("Skin decoded channel bytes do not match the raw uint32 word")
        expected_controls.update({"Source Effect " + axis: value for axis, value in zip("XYZW", channels.normalized())})
        height_hex = string_value(weather_source.get("height_float32_bits_hex"), "Skin height raw float32 bits")
        if len(height_hex) != 10 or not height_hex.startswith("0x"):
            raise ValueError("Skin source height must preserve its float32 raw bits")
        height_bits = int(height_hex, 16)
        height = struct.unpack("<f", struct.pack("<I", height_bits))[0]
        if not math.isfinite(height) or not math.isclose(height, number_value(weather_source.get("height_float32"), "Skin source height"), rel_tol=0.0, abs_tol=0.0):
            raise AssertionError("Skin source height differs from its float32 raw bits")
        expected_controls["Source Water Height"] = height
        if weather_source.get("source_preview_enable") != 0.0 or weather_source.get("user_override_weight_default") != 0.0:
            raise AssertionError("Skin native source must select per-draw height and keep user controls independent")
        if controls.get("source_per_draw_input_sha256") != input_sha256 or controls.get("source_input_kind") != "explicit_native_per_draw_word":
            raise AssertionError("Skin controls do not carry the caller's exact native source identity")
        if controls.get("endfield_standard_wetfilm_controls") is True:
            raise AssertionError("Skin controls unexpectedly reuse the Standard material-family controls")
    else:
        expected_controls["Source Water Height"] = 0.0
        expected_controls.update({"Source Effect " + axis: 0.0 for axis in "XYZW"})
    expected_controls.update({"User Effect " + axis: 0.0 for axis in "XYZW"})
    actual_controls = {label: weather_control_value(controls, label) for label in expected_controls}
    if any(not math.isclose(actual_controls[label], value, rel_tol=0.0, abs_tol=1e-7)
           for label, value in expected_controls.items()):
        raise AssertionError(f"Saved Skin weather controls differ from the recorded source: actual={actual_controls}; expected={expected_controls}")
    if controls.get("endfield_skin_rain_dry_default") is not (not native_source):
        raise AssertionError("Saved Skin controls report an incorrect dry-default boundary")
    projection = object_value(receipt.get("projection"), "Skin rain projection receipt")
    time_x = number_value(projection.get("time_x"), "Skin rain receipt time X")
    if weather_control_value(controls, "Preview Time X") != time_x:
        raise AssertionError("Saved Skin weather preview clock differs from profile")
    bindings_value = receipt.get("bindings")
    if not isinstance(bindings_value, list) or not bindings_value:
        raise TypeError("Skin rain receipt bindings must be a nonempty array")
    verified_trees: set[str] = set()
    for raw_row in bindings_value:
        row = object_value(raw_row, "Skin rain binding receipt")
        tree_name = string_value(row.get("material_tree"), "Skin material node tree")
        material_names_value = row.get("material_names")
        if not isinstance(material_names_value, list) or not material_names_value or any(not isinstance(name, str) for name in material_names_value):
            raise TypeError(f"Skin receipt material_names must be a nonempty string array: {tree_name}")
        root_trees = {material.node_tree.as_pointer(): material.node_tree
                      for name in material_names_value
                      if (material := bpy.data.materials.get(name)) is not None
                      and material.node_tree is not None and material.node_tree.name == tree_name}
        if len(root_trees) != 1:
            raise AssertionError(f"Saved Skin root material tree is missing or ambiguous: {tree_name}; matches={len(root_trees)}")
        root = next(iter(root_trees.values()))
        if root is None or root.get("endfield_skin_rain_binding") != BINDING_MARKER:
            raise AssertionError(f"Saved Skin root material marker is missing: {tree_name}")
        if root.get("endfield_skin_rain_profile_sha256") != profile_sha256 or root.get("endfield_skin_rain_shader_sha256") != shader_sha256:
            raise AssertionError(f"Saved Skin root material source hashes differ: {tree_name}")
        if native_source and root.get("endfield_skin_weather_source_sha256") != weather_source.get("input_sha256"):
            raise AssertionError(f"Saved Skin material root lost its explicit per-draw source hash: {tree_name}")
        dry_name = string_value(row.get("dry_group"), "Skin dry group")
        wet_name = string_value(row.get("wet_group"), "Skin wet group")
        dry_group = bpy.data.node_groups.get(dry_name)
        wet_group = bpy.data.node_groups.get(wet_name)
        if dry_group is None or wet_group is None:
            raise AssertionError(f"Saved Skin weather group is missing: dry={dry_name}; wet={wet_name}")
        if dry_group.get("endfield_skin_rain_role") != "exact_source_dry_group" or dry_group.get("wet_surface_binding"):
            raise AssertionError(f"Saved source-exact dry Skin group is not pristine: {dry_name}")
        if wet_group.get("endfield_skin_rain_role") != "compiled_non_sdf_skin_rain_group" or wet_group.get("wet_surface_binding") != WET_BINDING:
            raise AssertionError(f"Saved compiled Skin rain group marker is missing: {wet_name}")
        if wet_group.get("endfield_skin_rain_profile_sha256") != profile_sha256 or wet_group.get("endfield_skin_rain_shader_sha256") != shader_sha256:
            raise AssertionError(f"Saved compiled Skin rain group hashes differ: {wet_name}")
        keywords_value = row.get("source_keywords")
        if not isinstance(keywords_value, list) or any(not isinstance(keyword, str) for keyword in keywords_value):
            raise TypeError(f"Skin receipt keywords must be strings: {tree_name}")
        if "_SDFLIGHTMAP" in keywords_value or source_keywords(dry_group) != tuple(keywords_value) or source_keywords(wet_group) != tuple(keywords_value):
            raise AssertionError(f"Saved Skin source keyword branch differs or is unsupported: {tree_name}")
        encoded_keywords = json.dumps(keywords_value, separators=(",", ":"))
        source_identity = row.get("source_identity")
        encoded_identity = json.dumps(source_identity, separators=(",", ":"))
        if json.loads(dry_group["source_identity"]) != source_identity or json.loads(wet_group["source_identity"]) != source_identity:
            raise AssertionError(f"Saved Skin source identity changed: {tree_name}")
        if dry_group.get("endfield_skin_rain_source_keywords_json") != encoded_keywords or wet_group.get("endfield_skin_rain_source_keywords_json") != encoded_keywords:
            raise AssertionError(f"Saved Skin keyword audit properties differ: {tree_name}")
        if dry_group.get("endfield_skin_rain_source_identity_json") != encoded_identity or wet_group.get("endfield_skin_rain_source_identity_json") != encoded_identity:
            raise AssertionError(f"Saved Skin source identity audit properties differ: {tree_name}")
        shader_instances = tuple(node for node in wet_group.nodes if node.type == "GROUP" and node.node_tree is not None
                                 and node.node_tree.get("source_shader_sha256") == shader_sha256
                                 and node.node_tree.get("endfield_skin_rain_kernel") is True)
        if len(shader_instances) != 1:
            raise AssertionError(f"Expected one saved compiled Skin rain kernel: {wet_name}; count={len(shader_instances)}")
        kernel_instance = shader_instances[0]
        if float(kernel_instance.inputs["Orientation Flag"].default_value) != float(projection["orientation"]):
            raise AssertionError(f"Saved Skin rain orientation differs from profile: {wet_name}")
        if float(kernel_instance.inputs["Global Tiling"].default_value) != float(projection["tiling"]):
            raise AssertionError(f"Saved Skin rain tiling differs from profile: {wet_name}")
        if not kernel_instance.inputs["Rain Intensity"].is_linked or not kernel_instance.inputs["Global Wetness"].is_linked or not kernel_instance.inputs["Water Submersion"].is_linked or not kernel_instance.inputs["Native Time X"].is_linked:
            raise AssertionError(f"Saved Skin rain controls are disconnected: {wet_name}")
        for input_name, attribute_key in (("Projection Position", "position_attribute"), ("Projection Normal", "normal_attribute")):
            attribute_node = linked_node(kernel_instance.inputs[input_name], wet_name + "/" + input_name)
            if attribute_node.type != "ATTRIBUTE" or attribute_node.attribute_name != projection[attribute_key]:
                raise AssertionError(f"Saved Skin host projection attribute differs from profile: {wet_name}/{input_name}")
        for input_name, output_name in (("Rain Intensity", "Native Rain Intensity"),
                                        ("Water Submersion", "Native Water Submersion"),
                                        ("Global Wetness", "Native Global Wetness"),
                                        ("Native Time X", "Native Time X")):
            state_socket = single_link(kernel_instance.inputs[input_name], wet_name + "/" + input_name)
            if state_socket.node.node_tree != controls or state_socket.name != output_name:
                raise AssertionError(f"Saved Skin rain input uses unexpected controls: {wet_name}/{input_name}")
        has_snow = row.get("snow_channels") is not None
        if has_snow:
            if frozenset(keywords_value) != NATIVE_SKIN_KEYWORDS:
                raise AssertionError(f"Saved native Skin route is outside the exact reflected keyword variant: {tree_name}")
            snow_source = object_value(receipt.get("skin_snow_source"), "Skin snow source receipt")
            snow_instances = tuple(node for node in wet_group.nodes if node.type == "GROUP" and node.node_tree is not None
                                   and node.node_tree.get("endfield_skin_snow_texture_sha256") == snow_source.get("texture_sha256"))
            if len(snow_instances) != 1:
                raise AssertionError(f"Expected one saved Skin snow kernel: {wet_name}; count={len(snow_instances)}")
            snow_instance = snow_instances[0]
            if snow_instance.node_tree.get("source_shader_sha256") != shader_sha256 or "t39 _CharacterSnowEffectTex" not in str(snow_instance.node_tree.get("source_formula")):
                raise AssertionError(f"Saved Skin snow formula or shader identity differs: {wet_name}")
            snow_controls = single_link(snow_instance.inputs["Snow Intensity"], wet_name + "/Skin snow intensity")
            if snow_controls.node.node_tree != controls or snow_controls.name != "Native Snow Intensity":
                raise AssertionError(f"Saved Skin snow uses a different family of weather controls: {wet_name}")
            image_name = string_value(snow_source.get("packed_image"), "Skin snow packed image")
            image = bpy.data.images.get(image_name)
            if (image is None or image.packed_file is None
                    or image.get("endfield_weather_source_sha256") != snow_source.get("texture_sha256")
                    or image.get("endfield_skin_snow_source_identity_json") != json.dumps(snow_source.get("source_identity"), separators=(",", ":"))):
                raise AssertionError(f"Saved Skin snow texture source identity differs: {image_name}")
            texture_nodes = tuple(node for node in snow_instance.node_tree.nodes if node.type == "TEX_IMAGE")
            if len(texture_nodes) != 3 or any(node.image != image for node in texture_nodes):
                raise AssertionError(f"Saved Skin snow texture sampling does not use the verified source image: {wet_name}")
            if wet_group.get("weather_surface_binding") != "HGRPWeatherSurface/1":
                raise AssertionError(f"Saved Skin snow surface binding is missing: {wet_name}")
        elif wet_group.get("weather_surface_binding"):
            raise AssertionError(f"Skin route without a snow binding unexpectedly added snow: {wet_name}")
        selector_name = string_value(row.get("selector_node"), "Skin exact dry selector node")
        selector = root.nodes.get(selector_name)
        if selector is None or selector.get("endfield_skin_rain_role") != "exact_dry_selector":
            raise AssertionError(f"Saved Skin exact dry selector is missing: {tree_name}/{selector_name}")
        from character_material_authoring import matches_authoring_source_output
        if not matches_authoring_source_output(single_link(selector.inputs[1], tree_name + "/dry selector"), dry_group):
            raise AssertionError(f"Saved Skin dry selector does not use its pristine source group: {tree_name}")
        if not matches_authoring_source_output(single_link(selector.inputs[2], tree_name + "/wet selector"), wet_group):
            raise AssertionError(f"Saved Skin wet selector does not use its compiled group: {tree_name}")
        selector_gate = single_link(selector.inputs[0], tree_name + "/activation gate").node
        if has_snow:
            if selector_gate.get("endfield_skin_rain_role") != "combined_activation_gate" or selector_gate.operation != "MAXIMUM":
                raise AssertionError(f"Saved Skin rain/snow selector gate differs from source: {tree_name}")
            gate = linked_node(selector_gate.inputs[0], tree_name + "/rain activation gate")
            snow_gate = linked_node(selector_gate.inputs[1], tree_name + "/snow activation gate")
            if (gate != root.nodes.get(string_value(row.get("rain_gate_node"), "Skin rain gate node"))
                    or snow_gate != root.nodes.get(string_value(row.get("snow_gate_node"), "Skin snow gate node"))):
                raise AssertionError(f"Saved Skin combined gate does not use its receipted rain and snow gates: {tree_name}")
            if snow_gate.get("endfield_skin_rain_role") != "snow_activation_gate" or snow_gate.operation != "GREATER_THAN" or not math.isclose(float(snow_gate.inputs[1].default_value), 0.01, rel_tol=0.0, abs_tol=1e-6):
                raise AssertionError(f"Saved Skin snow activation threshold differs from source: {tree_name}")
            snow_subtract = linked_node(snow_gate.inputs[0], tree_name + "/snow source disable subtraction")
            if (snow_subtract.get("endfield_skin_rain_role") != "snow_source_disable"
                    or snow_subtract.operation != "SUBTRACT"
                    or not math.isclose(float(snow_subtract.inputs[1].default_value), number_value(row.get("source_disable_weather"), tree_name + "/source disable"), rel_tol=0.0, abs_tol=1e-6)):
                raise AssertionError(f"Saved Skin snow source-disable value differs from source: {tree_name}")
            snow_input = single_link(snow_subtract.inputs[0], tree_name + "/snow intensity source")
            if snow_input.node.node_tree != controls or snow_input.name != "Native Snow Intensity":
                raise AssertionError(f"Saved Skin snow gate uses unexpected controls: {tree_name}")
        else:
            gate = selector_gate
            if gate.get("endfield_skin_rain_role") != "rain_activation_gate":
                raise AssertionError(f"Saved Skin exact activation gate is missing: {tree_name}")
        if gate.operation != "GREATER_THAN" or not math.isclose(float(gate.inputs[1].default_value), 0.01, rel_tol=0.0, abs_tol=1e-6):
            raise AssertionError(f"Saved Skin rain activation threshold differs from source: {tree_name}")
        subtract = linked_node(gate.inputs[0], tree_name + "/source disable subtraction")
        upper = linked_node(subtract.inputs[0], tree_name + "/saturate upper")
        lower = linked_node(upper.inputs[0], tree_name + "/saturate lower")
        total = linked_node(lower.inputs[0], tree_name + "/rain wet sum")
        maximum = linked_node(total.inputs[1], tree_name + "/wetness maximum")
        source_disable = number_value(row.get("source_disable_weather"), tree_name + "/source disable")
        if subtract.get("endfield_skin_rain_role") != "source_disable" or subtract.operation != "SUBTRACT" or not math.isclose(float(subtract.inputs[1].default_value), source_disable, rel_tol=0.0, abs_tol=1e-6):
            raise AssertionError(f"Saved Skin source-disable value differs from source: {tree_name}")
        if upper.operation != "MINIMUM" or float(upper.inputs[1].default_value) != 1.0 or lower.operation != "MAXIMUM" or float(lower.inputs[1].default_value) != 0.0:
            raise AssertionError(f"Saved Skin activation gate does not saturate source weather: {tree_name}")
        if total.operation != "ADD" or maximum.operation != "MAXIMUM":
            raise AssertionError(f"Saved Skin activation gate differs from compiled source formula: {tree_name}")
        for input_socket, output_name in ((maximum.inputs[0], "Native Global Wetness"),
                                          (maximum.inputs[1], "Native Water Submersion"),
                                          (total.inputs[0], "Native Rain Intensity")):
            state_socket = single_link(input_socket, tree_name + "/" + output_name)
            if state_socket.node.get("endfield_skin_rain_role") != "weather_controls" or state_socket.node.node_tree != controls or state_socket.name != output_name:
                raise AssertionError(f"Saved Skin activation gate uses unexpected state: {tree_name}/{output_name}")
        state_instance = root.nodes.get(string_value(row.get("state_instance"), "Skin root weather controls instance"))
        height_node = root.nodes.get(string_value(row.get("world_height_node"), "Skin root world-height node"))
        if state_instance is None or state_instance.node_tree != controls or height_node is None or height_node.type != "SEPXYZ":
            raise AssertionError(f"Saved Skin per-material weather source nodes are missing: {tree_name}")
        geometry = linked_node(height_node.inputs[0], tree_name + "/source world position")
        if geometry.type != "NEW_GEOMETRY" or single_link(state_instance.inputs["World Height"], tree_name + "/source world height") != height_node.outputs["Z"]:
            raise AssertionError(f"Saved Skin height selector does not use world-space geometry Z: {tree_name}")
        selector_boundary = object_value(row.get("selector_boundary"), "Skin material selector boundary")
        if selector_boundary.get("serialized_height_is_runtime_state") is not False or selector_boundary.get("global_preview_selector_runtime_capture_verified") is not False:
            raise AssertionError(f"Skin static material height/preview boundaries are mislabeled: {tree_name}")
        if native_source and (selector_boundary.get("source_preview_enable") != 0.0
                              or selector_boundary.get("height_source") != "raw_per_draw_Vector4.y"):
            raise AssertionError(f"Skin did not select the explicit per-draw height path: {tree_name}")
        serialized_weather = object_value(row.get("serialized_material_weather"), "Skin serialized material weather fields")
        dry_floats = object_value(json.loads(dry_group["source_floats"]), "Saved Skin serialized source floats")
        for property_name, saved_value in serialized_weather.items():
            actual_value = number_value(dry_floats[property_name], tree_name + "/" + property_name) if property_name in dry_floats else None
            if saved_value != actual_value:
                raise AssertionError(f"Saved Skin static material field differs from receipt: {tree_name}/{property_name}")
        expected_formula = NATIVE_BYPASS_FORMULA if has_snow else DRY_BYPASS_FORMULA
        if root.get("endfield_skin_rain_branch_formula") != expected_formula or root.get("endfield_skin_rain_dry_default") is not (not native_source):
            raise AssertionError(f"Saved Skin root material dry bypass metadata differs: {tree_name}")
        verified_trees.add(tree_name)
    source_gate_probes = []
    if native_source:
        channels = decode_weather_per_draw_bits(int(str(weather_source["packed_word_bits_hex"]), 16)).normalized()
        source_height = number_value(weather_source.get("height_float32"), "Skin source water height")
        for row in bindings_value:
            binding = object_value(row, "Skin gate probe binding")
            object_names = binding.get("object_names")
            if not isinstance(object_names, list) or not object_names:
                raise TypeError("Skin gate probe requires actual body mesh users")
            mesh_object = bpy.data.objects.get(string_value(object_names[0], "Skin gate probe object"))
            if mesh_object is None or mesh_object.type != "MESH" or not mesh_object.data.vertices:
                raise AssertionError("Skin gate probe object is not a nonempty mesh")
            world_z = float((mesh_object.matrix_world @ mesh_object.data.vertices[len(mesh_object.data.vertices) // 2].co).z)
            delta = source_height - world_z
            t = min(1.0, max(0.0, (delta + 0.2) / 0.35))
            water_mask = t * t * (3.0 - 2.0 * t)
            water = channels[1] * water_mask
            global_wetness = max(channels[2], water)
            rain_signal = min(1.0, max(0.0, channels[0] + max(global_wetness, water)))
            disable = number_value(binding.get("source_disable_weather"), "Skin gate probe material disable")
            rain_gate_value = float(rain_signal - disable > 0.01)
            snow_gate_value = float(channels[3] - disable > 0.01)
            source_gate_probes.append({"source_group": binding.get("source_group"), "object": mesh_object.name,
                                       "world_height_sample": world_z, "per_draw_height": source_height,
                                       "water_mask": water_mask, "rain_signal": rain_signal,
                                       "rain_gate": rain_gate_value, "snow_gate": snow_gate_value,
                                       "combined_gate": max(rain_gate_value, snow_gate_value),
                                       "zero_source_gate": 0.0, "evaluation": "reopened source values and saved formula; not Blender shader evaluation"})
    return {"status": "passed", "skin_bindings": len(bindings_value), "root_material_trees": len(verified_trees),
            "dry_default": not native_source, "source_exact_dry_group": True,
            "weather_source_kind": source_kind, "source_gate_probes": source_gate_probes,
            "source_shader_sha256": shader_sha256, "native_runtime_state_verified": False,
            "native_projection_verified": False, "native_time_verified": False,
            "preview_selector_runtime_capture_verified": False}
