"""Attach evidence-bounded Standard snow to source-selected character materials."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Callable, Mapping

import bpy

from attach_character_weather_surface import WeatherProjection, bind_weather_surface
from character_weather_nodes import make_native_standard_snow
from character_wetfilm_profile import (
    DRY_WETFILM_STATUS, NATIVE_WETFILM_STATUS, DRY_WEATHER_SOURCE, NATIVE_WEATHER_SOURCE,
    file_digest, verified_file, verify_weather_controls,
)
from character_material_profile import ShaderIdentity, profile_shader_identity
from patch_character_hgrp import source_keywords


@dataclass(frozen=True)
class StandardSnowProfile:
    shader_identity: ShaderIdentity
    shader_sha256: str
    texture_manifest: Path
    texture_manifest_sha256: str
    source_family: str
    texture_slot: str
    position_attribute: str
    normal_attribute: str
    orientation: float
    tiling: float
    projection_provenance: str


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Expected nonempty string for {label}")
    return value


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"Expected object for {label}")
    return value


def _read_profile(path: Path) -> StandardSnowProfile | None:
    document = _object(json.loads(path.read_text(encoding="utf-8")), "character weather profile")
    if "standard_snow" not in document:
        return None
    snow = _object(document["standard_snow"], "standard_snow")
    if snow.get("format") != "EndfieldStandardSnowProfile/1":
        raise ValueError("Unsupported Standard snow profile format")
    family = _string(snow.get("source_family"), "standard_snow.source_family")
    if family != "HGRP/CharacterNPR":
        raise ValueError(f"Standard snow profile has unsupported source family: {family}")
    weather = _object(document.get("standard_wet_film"), "standard_wet_film required by Standard snow")
    if weather.get("format") != "EndfieldStandardWetFilmProfile/1":
        raise ValueError("Standard snow requires a supported Standard wet-film profile")
    shader_identity = profile_shader_identity(path, family)
    shader_sha256 = shader_identity["object_sha256"]
    manifest = verified_file(path.parent, weather.get("texture_manifest"), weather.get("texture_manifest_sha256"))
    manifest_sha256 = _string(weather.get("texture_manifest_sha256"), "standard_wet_film.texture_manifest_sha256").casefold()
    orientation_value = weather.get("orientation")
    if type(orientation_value) not in (int, float) or orientation_value not in (0, 1):
        raise ValueError("Standard snow orientation must reuse explicit wet-film orientation 0 or 1")
    tiling_value = weather.get("tiling")
    if type(tiling_value) not in (int, float) or not math.isfinite(tiling_value) or not 0 < tiling_value < 1000:
        raise ValueError("Standard snow tiling must reuse finite positive wet-film tiling below 1000")
    slot = _string(snow.get("texture_slot"), "standard_snow.texture_slot")
    if slot != "characterSnowTexture":
        raise ValueError(f"Unsupported Standard snow texture slot: {slot}")
    provenance = _string(snow.get("projection_provenance"), "standard_snow.projection_provenance")
    return StandardSnowProfile(
        shader_identity, shader_sha256, manifest, manifest_sha256, family, slot,
        _string(snow.get("position_attribute"), "standard_snow.position_attribute"),
        _string(snow.get("normal_attribute"), "standard_snow.normal_attribute"),
        float(orientation_value), float(tiling_value), provenance)


def _source_groups(receipt: Mapping[str, object]) -> tuple[bpy.types.ShaderNodeTree, ...]:
    status = _string(receipt.get("status"), "Standard wet-film status")
    if status == "no_matching_standard_materials":
        return ()
    if status not in (DRY_WETFILM_STATUS, NATIVE_WETFILM_STATUS):
        raise ValueError(f"Standard snow requires attached Standard wet film; status={status}")
    names_value = receipt.get("groups")
    if not isinstance(names_value, list) or any(not isinstance(name, str) or not name for name in names_value):
        raise TypeError("Standard wet-film group list is malformed")
    names = tuple(names_value)
    if not names or len(names) != len(set(names)):
        raise ValueError("Standard wet-film group list must be nonempty and unique")
    groups = tuple(bpy.data.node_groups.get(name) for name in names)
    if any(group is None for group in groups):
        raise ValueError("A Standard wet-film source material group is missing")
    typed_groups = tuple(group for group in groups if group is not None)
    for group in typed_groups:
        if group.get("source_shader") != "HGRP/CharacterNPR" or "_CHARACTER_FUR" in source_keywords(group):
            raise ValueError(f"Standard snow source group is outside the verified non-Fur family: {group.name}")
        if group.get("wet_surface_binding") != "HGRPStandardWetSurface/1":
            raise ValueError(f"Standard snow requires the existing wet-film surface: {group.name}")
        if group.get("weather_surface_binding"):
            raise ValueError(f"Standard weather surface is already bound: {group.name}")
    return typed_groups


def _verify_projection_attributes(groups: tuple[bpy.types.ShaderNodeTree, ...], profile: StandardSnowProfile) -> list[dict[str, object]]:
    names = frozenset(group.name for group in groups)
    rows: list[dict[str, object]] = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.hide_render:
            continue
        used = any(slot.material is not None and slot.material.use_nodes
                   and any(node.type == "GROUP" and node.node_tree is not None and node.node_tree.name in names
                           for node in slot.material.node_tree.nodes)
                   for slot in obj.material_slots)
        if not used:
            continue
        for name in (profile.position_attribute, profile.normal_attribute):
            attribute = obj.data.attributes.get(name)
            if attribute is None or attribute.domain != "POINT" or attribute.data_type != "FLOAT_VECTOR" or len(attribute.data) != len(obj.data.vertices):
                raise ValueError(f"Standard snow projection attribute is absent or incompatible: {obj.name}/{name}")
        rows.append({"object": obj.name, "vertices": len(obj.data.vertices),
                     "position_attribute": profile.position_attribute,
                     "normal_attribute": profile.normal_attribute,
                     "provenance": profile.projection_provenance})
    if not rows:
        raise ValueError("Standard snow source material groups have no visible mesh users")
    return rows


def attach_profile_standard_snow(profile_path: Path, layout: Callable[[bpy.types.NodeTree], None],
                                 wetfilm_receipt: Mapping[str, object]) -> dict[str, object]:
    """Bind Standard snow after wet film using the shared explicit weather controls."""
    profile = _read_profile(profile_path)
    if profile is None:
        return {"status": "not_configured", "groups": []}
    groups = _source_groups(wetfilm_receipt)
    if not groups:
        return {"status": "no_matching_standard_materials", "groups": []}
    controls_name = _string(wetfilm_receipt.get("controls"), "Standard wet-film controls")
    controls = bpy.data.node_groups.get(controls_name)
    if controls is None or not controls.get("endfield_standard_wetfilm_controls"):
        raise ValueError("Standard snow cannot find the shared Standard weather controls")
    source_record = _object(wetfilm_receipt.get("weather_source"), "Standard weather source")
    expected_kind = NATIVE_WEATHER_SOURCE if wetfilm_receipt.get("status") == NATIVE_WETFILM_STATUS else DRY_WEATHER_SOURCE
    if source_record.get("kind") != expected_kind:
        raise ValueError("Standard wet-film status and source kind disagree")
    verify_weather_controls(controls, source_record)
    attributes = _verify_projection_attributes(groups, profile)
    rows = json.loads(profile.texture_manifest.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise TypeError("Weather texture manifest must be an array")
    textures = tuple(_object(row, "weather texture row") for row in rows
                     if isinstance(row, dict) and row.get("slot") == profile.texture_slot)
    if len(textures) != 1 or type(textures[0].get("color_space")) is not int or textures[0].get("color_space") != 0:
        raise ValueError("Standard snow requires exactly one linear source characterSnowTexture")
    texture = verified_file(profile.texture_manifest.parent,
                            textures[0].get("artifact"), textures[0].get("sha256"))
    image = bpy.data.images.load(str(texture), check_existing=False)
    image.colorspace_settings.name = "Non-Color"
    image.alpha_mode = "CHANNEL_PACKED"
    image.pack()
    texture_sha256 = file_digest(texture)
    image["endfield_weather_source_sha256"] = texture_sha256
    kernel = make_native_standard_snow(image, profile.shader_sha256, layout)
    projection = WeatherProjection(profile.position_attribute, profile.normal_attribute,
                                   profile.orientation, profile.tiling, profile.projection_provenance)
    bindings = tuple(bind_weather_surface(group, kernel, controls, projection, layout) for group in groups)
    note = bpy.data.texts.new("Standard snow controls")
    note.write("Standard non-Fur HGRP/CharacterNPR snow is connected after the existing wet-film layer.\n"
               + ("The shared controls contain the explicit decoded native per-draw source; set User Override Weight to 1 before using User Effect W. Runtime capture remains unverified.\n"
                  if source_record.get("kind") == NATIVE_WEATHER_SOURCE else
                  "Use the shared EF Weather Uniforms group and set User Effect W to control snow; all effects default to zero.\n")
               + "Projection reuses explicit rest-world attributes; source TEXCOORD and live weather runtime binding remain unverified.\n"
               + "Skin snow and Hair/Fur snow are not enabled by this Standard-only branch.\n")
    status = "standard_snow_connected_native_per_draw" if source_record.get("kind") == NATIVE_WEATHER_SOURCE else "standard_snow_connected_dry_default"
    return {"status": status, "weather_source": source_record, "groups": [group.name for group in groups],
            "controls": controls.name, "bindings": bindings, "projection_attributes": attributes,
            "projection": {"position_attribute": profile.position_attribute,
                           "normal_attribute": profile.normal_attribute,
                           "orientation": profile.orientation, "tiling": profile.tiling,
                           "provenance": profile.projection_provenance},
            "packed_texture": image.name, "texture_sha256": texture_sha256,
            "source_shader_identity": profile.shader_identity,
            "profile_path": str(profile_path.resolve(strict=True)),
            "profile_sha256": file_digest(profile_path),
            "source_files": [{"path": str(path), "sha256": file_digest(path)}
                             for path in (profile_path, profile.texture_manifest, texture)],
            "native_runtime_state_verified": False, "native_projection_verified": False,
            "scope": "Verified HGRP/CharacterNPR Standard non-Fur snow; Skin/Fur not included"}


def verify_profile_standard_snow(receipt: Mapping[str, object]) -> dict[str, object]:
    """Verify packed snow, selected materials and source controls after reopening."""
    status = _string(receipt.get("status"), "Standard snow receipt status")
    if status in ("not_configured", "no_matching_standard_materials"):
        return {"status": status}
    if status not in ("standard_snow_connected_dry_default", "standard_snow_connected_native_per_draw"):
        raise ValueError(f"Unrecognized Standard snow receipt status: {status}")
    controls_name = _string(receipt.get("controls"), "Standard snow controls")
    controls = bpy.data.node_groups.get(controls_name)
    if controls is None or not controls.get("endfield_standard_wetfilm_controls"):
        raise AssertionError(f"Saved Standard snow controls are missing: {controls_name}")
    source_record = _object(receipt.get("weather_source"), "Saved Standard snow weather source")
    expected_kind = NATIVE_WEATHER_SOURCE if status == "standard_snow_connected_native_per_draw" else DRY_WEATHER_SOURCE
    if source_record.get("kind") != expected_kind:
        raise AssertionError("Saved Standard snow status and weather source kind disagree")
    profile_path = Path(_string(receipt.get("profile_path"), "Standard snow profile path")).resolve(strict=True)
    if file_digest(profile_path) != _string(receipt.get("profile_sha256"), "Standard snow profile SHA-256"):
        raise AssertionError(f"Standard snow profile changed after build: {profile_path}")
    source_shader_identity = _object(receipt.get("source_shader_identity"), "Standard snow Shader identity")
    actual_shader_identity = profile_shader_identity(profile_path, "HGRP/CharacterNPR")
    if source_shader_identity != actual_shader_identity:
        raise AssertionError("Saved Standard snow Shader object identity differs from its per-export profile")
    verify_weather_controls(controls, source_record)
    groups_value = receipt.get("groups")
    if not isinstance(groups_value, list) or not groups_value:
        raise TypeError("Saved Standard snow groups must be a nonempty array")
    groups = tuple(_string(name, "Standard snow group") for name in groups_value)
    projection = _object(receipt.get("projection"), "Standard snow projection")
    position_name = _string(projection.get("position_attribute"), "snow position attribute")
    normal_name = _string(projection.get("normal_attribute"), "snow normal attribute")
    for name in groups:
        tree = bpy.data.node_groups.get(name)
        if tree is None or tree.get("source_shader") != "HGRP/CharacterNPR":
            raise AssertionError(f"Saved Standard snow source group is missing: {name}")
        if "_CHARACTER_FUR" in source_keywords(tree):
            raise AssertionError(f"Saved Standard snow unexpectedly targets Fur: {name}")
        if tree.get("wet_surface_binding") != "HGRPStandardWetSurface/1" or tree.get("weather_surface_binding") != "HGRPWeatherSurface/1":
            raise AssertionError(f"Saved Standard snow/wet layers are not both bound: {name}")
        snow_nodes = tuple(node for node in tree.nodes if node.type == "GROUP" and node.label == "Compiled snow before HGRP surface lighting")
        if len(snow_nodes) != 1 or snow_nodes[0].node_tree is None or snow_nodes[0].node_tree.get("source_formula") != "Current HGRP/CharacterNPR first ForwardLit Vulkan fragment; local snow equations 1602-1638" or snow_nodes[0].node_tree.get("source_shader_sha256") != actual_shader_identity["object_sha256"]:
            raise AssertionError(f"Saved Standard snow kernel is missing or ambiguous: {name}")
        instance = snow_nodes[0]
        intensity_links = tuple(instance.inputs["Snow Intensity"].links)
        if len(intensity_links) != 1 or intensity_links[0].from_node.node_tree != controls or intensity_links[0].from_socket.name != "Native Snow Intensity":
            raise AssertionError(f"Saved Standard snow does not use shared weather User Effect W: {name}")
        for socket_name, attribute_name in (("Projection Position", position_name), ("Projection Normal", normal_name)):
            links = tuple(instance.inputs[socket_name].links)
            if len(links) != 1 or links[0].from_node.type != "ATTRIBUTE" or links[0].from_node.attribute_name != attribute_name:
                raise AssertionError(f"Saved Standard snow projection is disconnected: {name}/{socket_name}")
    image_name = _string(receipt.get("packed_texture"), "Standard snow image")
    image = bpy.data.images.get(image_name)
    texture_sha256 = _string(receipt.get("texture_sha256"), "Standard snow texture SHA-256")
    if image is None or image.packed_file is None or hashlib.sha256(image.packed_file.data).hexdigest() != texture_sha256:
        raise AssertionError("Saved Standard snow texture is missing or changed")
    if image.colorspace_settings.name != "Non-Color" or image.alpha_mode != "CHANNEL_PACKED":
        raise AssertionError("Saved Standard snow texture interpretation differs")
    files_value = receipt.get("source_files")
    if not isinstance(files_value, list) or len(files_value) != 3:
        raise TypeError("Standard snow source file fingerprints must contain profile, manifest and texture")
    for index, raw in enumerate(files_value):
        row = _object(raw, "Standard snow source fingerprint")
        path = Path(_string(row.get("path"), "Standard snow source path")).resolve(strict=True)
        expected = _string(row.get("sha256"), "Standard snow source hash").casefold()
        if file_digest(path) != expected:
            raise AssertionError(f"Standard snow source changed after build: {path}")
        if index == 2 and expected != texture_sha256:
            raise AssertionError("Standard snow packed texture and source file fingerprints differ")
    return {"status": "passed", "standard_groups": len(groups),
            "weather_source_kind": expected_kind, "runtime_capture_verified": False,
            "dry_default": expected_kind == DRY_WEATHER_SOURCE,
            "snow_texture_packed": True, "shared_weather_effect": "User Effect W",
            "projection_attributes": [position_name, normal_name]}
