"""Validate the recovered HGRP/FogSimple material input contract."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

from endfield_scene_material_contract import (
    MaterialRecord,
    has_shader_white_texture_default,
    reference_key,
    validate_material,
    validate_texture_sampler,
)


def _source_float(floats: Mapping[str, float], name: str,
                  material_name: str) -> float:
    value = floats.get(name)
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(
            f"FogSimple source float is missing or invalid: material={material_name}, property={name}, value={value!r}")
    return float(value)


def fog_texture_slot(record: MaterialRecord, name: str) -> Mapping[str, object]:
    """Return the unique serialized texture slot for a FogSimple property."""
    matches = [slot for slot in record["Textures"] if slot["Property"] == name]
    if len(matches) != 1:
        raise ValueError(
            f"FogSimple texture binding must be unique: material={record['Name']}, property={name}, count={len(matches)}")
    return cast(Mapping[str, object], matches[0])


def validate_fog_material(
    record: MaterialRecord,
    vertex_color_components: int | None,
) -> tuple[MaterialRecord, dict[str, float]]:
    """Validate supported FogSimple source state before planning or node creation."""
    source = validate_material(record)
    name = source["Name"]
    shader = source["Shader"]
    if shader.get("Name") != "HGRP/FogSimple":
        raise NotImplementedError(
            f"Fog material shader family is unsupported: material={name}, shader={shader.get('Name')}")
    if "ForwardOnly" not in shader.get("PassNames", []):
        raise NotImplementedError(f"FogSimple shader lacks the verified ForwardOnly pass: material={name}")
    if (vertex_color_components is not None
            and (type(vertex_color_components) is not int or vertex_color_components not in (0, 4))):
        raise NotImplementedError(
            f"FogSimple mesh vertex color layout is unsupported: material={name}, components={vertex_color_components}")
    state = source.get("RenderState")
    if (state is None or state["CustomRenderQueue"] != 3000
            or "DepthOnly" not in state["DisabledShaderPasses"]):
        raise NotImplementedError(
            f"FogSimple render queue or disabled DepthOnly state is unverified: material={name}, state={state}")
    floats = source["Floats"]
    required = (
        "_BaseAlphaIntensity", "_FogNoiseIntensity", "_UseWind", "_WindNoiseIntensity",
        "_WindNoiseContrast", "_WindSpeed", "_WindScale", "_UseSeparateScale", "_WindScaleX",
        "_WindScaleY", "_WindDirX", "_WindDirY", "_UseLighting", "_UseFresnel",
        "_FresnelIntensity", "_FresnelContract", "_FlipFresnel", "_DistanceFade",
        "_DistanceFadeStart", "_DistanceFadeEnd", "_NearCameraFadeDistanceStart",
        "_NearCameraFadeDistanceEnd", "_TopFade", "_TopFadeContract", "_TopFadeOffset",
        "_UseSoftBlend", "_SoftDistance", "_UseFog", "_CullMode", "_SrcBlend", "_DstBlend",
        "_AlphaSrcBlend", "_AlphaDstBlend", "_BlendMode",
    )
    values = {field: _source_float(floats, field, name) for field in required}
    for field in ("_UseWind", "_UseSeparateScale", "_UseLighting", "_UseFresnel", "_DistanceFade",
                  "_TopFade", "_UseSoftBlend", "_UseFog"):
        if values[field] not in (0.0, 1.0):
            raise NotImplementedError(
                f"FogSimple boolean control is unsupported: material={name}, property={field}, value={values[field]}")
    if (values["_UseFog"] != 0.0 or values["_UseLighting"] != 0.0
            or values["_DistanceFade"] != 1.0 or values["_TopFade"] != 1.0):
        raise NotImplementedError(f"FogSimple runtime fog/lighting/fade mode is unsupported: material={name}")
    if values["_CullMode"] != 2.0:
        raise NotImplementedError(f"FogSimple culling state is unsupported: material={name}")
    if values["_FlipFresnel"] not in (0.0, 1.0):
        raise NotImplementedError(
            f"FogSimple Fresnel orientation control is unsupported: material={name}, value={values['_FlipFresnel']}")
    keywords = state["ValidKeywords"]
    if ("_USE_SOFTBLEND" in keywords) != (values["_UseSoftBlend"] == 1.0):
        raise ValueError(
            f"FogSimple soft-blend float/keyword disagree: material={name}, keywords={keywords}, value={values['_UseSoftBlend']}")
    if values["_SrcBlend"] != 1.0 or values["_AlphaSrcBlend"] != 1.0 or values["_DstBlend"] not in (1.0, 10.0):
        raise NotImplementedError(f"FogSimple source blend factors are unsupported: material={name}")
    if values["_AlphaDstBlend"] != values["_DstBlend"]:
        raise NotImplementedError(f"FogSimple RGB/alpha destination blend factors disagree: material={name}")
    expected_mode = 1.0 if values["_DstBlend"] == 1.0 else 0.0
    if values["_BlendMode"] != expected_mode:
        raise NotImplementedError(f"FogSimple blend mode disagrees with compiled factors: material={name}")
    if values["_FresnelContract"] < 0.0 or not 0.0 <= values["_FresnelIntensity"] <= 1.0:
        raise ValueError(f"FogSimple Fresnel controls are out of range: material={name}")
    if not 0.0 <= values["_BaseAlphaIntensity"] <= 1.0:
        raise ValueError(f"FogSimple base alpha is out of range: material={name}")
    if not 0.0 <= values["_FogNoiseIntensity"] <= 1.0 or not 0.0 <= values["_WindNoiseIntensity"] <= 1.0:
        raise ValueError(f"FogSimple noise intensities are out of range: material={name}")
    if not 0.0 <= values["_SoftDistance"] or (
            values["_UseSoftBlend"] == 1.0 and values["_SoftDistance"] == 0.0):
        raise ValueError(f"FogSimple soft distance is invalid: material={name}, value={values['_SoftDistance']}")
    if (values["_DistanceFadeEnd"] < values["_DistanceFadeStart"]
            or values["_NearCameraFadeDistanceEnd"] < values["_NearCameraFadeDistanceStart"]):
        raise ValueError(f"FogSimple fade window end precedes start: material={name}")
    color = source["Colors"].get("_BaseColor")
    if color is None or len(color) != 4:
        raise ValueError(f"FogSimple material lacks a four-channel BaseColor: material={name}")
    for property_name in ("_MainTex", "_NoiseTex"):
        reference = fog_texture_slot(source, property_name)["Texture"]
        if reference is None:
            if not has_shader_white_texture_default(source, property_name):
                raise ValueError(
                    f"FogSimple texture has no source binding or white shader default: material={name}, property={property_name}")
            continue
        asset = cast(Mapping[str, object], reference)
        key = reference_key(asset)
        color_space = asset.get("ColorSpace")
        if type(color_space) is not int or color_space not in (0, 1):
            raise ValueError(
                f"FogSimple texture ColorSpace is missing or invalid: material={name}, property={property_name}, reference={key}, value={color_space!r}")
        if "TextureSampler" in asset:
            validate_texture_sampler(asset["TextureSampler"], f"FogSimple material={name} property={property_name}")
    return source, values
