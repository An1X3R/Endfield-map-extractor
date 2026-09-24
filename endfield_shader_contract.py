"""Surface layouts verified against current HGRP program blobs and property enums.

Evidence: shader_dual_engine_20260921 programs Leaf 101, Trunk 33/34,
Lit 310/313 and LitTransparent 24/27. Identities refer to shader assets,
never scene regions or material names. Unknown shaders retain their old contract.
"""
from __future__ import annotations

from typing import Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from endfield_scene_material_contract import MaterialRecord

SurfaceFamily = Literal["lit", "transparent", "trunk", "leaf", "card", "unverified"]
NormalLayout = Literal["rgb", "rg", "rag"]
EmissionSource = Literal["emissive_map", "base_alpha", "normal_blue", "normal_alpha", "mro_alpha", "constant"]


def surface_family(record: MaterialRecord) -> SurfaceFamily:
    identities: dict[tuple[str, int], SurfaceFamily] = {
        ("cab-93483c2cb844b6b343af0950ad4630ad", 5324015590718682574): "lit",
        ("cab-e695120be0773bc41d044f9018921638", 9181137009890694473): "transparent",
        ("cab-ad80989362fff385c35e870b81de05b4", -5478546954252230161): "trunk",
        ("cab-52fb642f8e63a6d519f2c165e12126fb", 1054010568497046743): "leaf",
        ("cab-e3455e2e817382d0e9b918c2f98a1929", -4823490470451583925): "card",
    }
    shader = record["Shader"]
    return identities.get((shader["Source"].casefold(), shader["PathId"]), "unverified")


def base_texture_mode(record: MaterialRecord) -> int:
    value = record["Floats"].get("_BaseTextureMapCount")
    if value not in (0, 1, 2):
        raise ValueError(f"Verified HGRP material requires BaseTextureMapCount enum 0/1/2: material={record['Name']}, value={value}")
    return int(value)


def normal_layout(record: MaterialRecord) -> NormalLayout:
    family = surface_family(record)
    if family in ("leaf", "card"):
        return "rg"
    if family in ("lit", "transparent", "trunk"):
        return "rg" if base_texture_mode(record) else "rag"
    return "rgb"


def uses_mro(record: MaterialRecord) -> bool:
    family = surface_family(record)
    if family in ("leaf", "card"):
        return False
    return family == "unverified" or base_texture_mode(record) == 0


def uses_normal_map(record: MaterialRecord) -> bool:
    return surface_family(record) != "leaf" or record["Floats"].get("_EnableNormalMap", 0.0) > 0.5


def emission_source(record: MaterialRecord) -> EmissionSource:
    """Decode the serialized source selector for Lit, Trunk, and LitTransparent."""
    family = surface_family(record)
    if family not in ("lit", "trunk", "transparent"):
        raise ValueError(f"Unverified emissive source enum: material={record['Name']}, family={family}")
    channel = record["Floats"].get("_EmissiveMaskChannel")
    sources: tuple[EmissionSource, ...] = ("emissive_map", "base_alpha", "normal_blue",
                                           "normal_alpha", "mro_alpha", "constant")
    if channel not in range(len(sources)):
        raise ValueError(f"Invalid emissive source selector: material={record['Name']}, channel={channel}")
    return sources[int(channel)]


def emissive_map_type(record: MaterialRecord) -> int:
    """Read ChannelR versus ChannelRGBA for the authored emissive map source."""
    value = record["Floats"].get("_EmissiveType")
    if value not in (0, 1):
        raise ValueError(f"Invalid emissive map type: material={record['Name']}, value={value}")
    return int(value)
