"""Surface layouts verified against current HGRP program blobs and property enums.

Evidence: shader_dual_engine_20260921 programs Leaf 101, Trunk 33/34,
Lit 310/313 and LitTransparent 24/27. Identities refer to shader assets,
never scene regions or material names. Unknown shaders retain their old contract.
"""
from __future__ import annotations

import math
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


def has_verified_lit_top_layer(record: MaterialRecord) -> bool:
    """Match the decoded two-map top-layer surface branch, independent of asset names."""
    keywords = set((record.get("RenderState") or {}).get("ValidKeywords", []))
    floats = record["Floats"]
    return (surface_family(record) == "lit"
            and {"_LAYERBLEND_TOP", "_TWO_BASEMAP"} <= keywords
            and keywords <= {"_LAYERBLEND_TOP", "_TWO_BASEMAP", "_TERRAIN_BLEND"}
            and floats.get("_BaseTextureMapCount") == 2
            and floats.get("_LayerBlend") == 1
            and floats.get("_LayerBlendType") == 2
            and floats.get("_LayerBlendUVType") in (0, 1, 2)
            and floats.get("_BaseUVSet") in (0, 1)
            and floats.get("_BasePbrMapUVSet") in (0, 1)
            and floats.get("_LayerBlendHeight") in (0, 1))


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


def has_verified_lit_alpha_clip(record: MaterialRecord) -> bool:
    """Match the UV0 two-map alpha contract decoded from Lit DepthOnly 799."""
    floats = record["Floats"]
    return (surface_family(record) == "lit"
            and floats.get("_EnableAlphaTest", 0.0) > 0.5
            and floats.get("_BaseTextureMapCount") == 1
            and floats.get("_BaseUVSet") == 0
            and floats.get("_BasePbrMapUVSet") == 0
            and floats.get("_EnableUVAnimation") == 0)


def has_verified_lit_sweep(record: MaterialRecord) -> bool:
    """Match a reconstructed sweep core, not main-pass runtime parity."""
    state = record.get("RenderState")
    keywords = set(state["ValidKeywords"]) if state is not None else set()
    floats = record["Floats"]
    mapped = (floats.get("_EmissiveMaskChannel") == 0 and floats.get("_EmissiveUVSet") == 0
              and floats.get("_EmissiveNoMapUseFresnel", 0) == 0
              and keywords in ({"_EMISSIVE_ANIM_SWEEP", "_EMISSIVE_MAP"},
                               {"_EMISSIVE_ANIM_SWEEP", "_EMISSIVE_MAP", "_TWO_BASEMAP"},
                               {"_EMISSIVE_ANIM_SWEEP", "_EMISSIVE_MAP", "_LAYERBLEND_MASK"}))
    constant = (floats.get("_EmissiveMaskChannel") == 5
                and floats.get("_EmissiveNoMapUseFresnel", 0) == 0
                and keywords in ({"_EMISSIVE_ANIM_SWEEP", "_EMISSIVE_NOMAP"},
                                 {"_EMISSIVE_ANIM_SWEEP", "_EMISSIVE_NOMAP", "_TWO_BASEMAP"},
                                 {"_EMISSIVE_ANIM_SWEEP", "_EMISSIVE_NOMAP", "_TWO_BASEMAP", "_ALPHATEST_ON"})
                and ("_ALPHATEST_ON" not in keywords or has_verified_lit_alpha_clip(record)))
    tri_constant = (floats.get("_EmissiveMaskChannel") == 5
                    and floats.get("_BaseUVSet") == 0 and floats.get("_BasePbrMapUVSet") == 0
                    and floats.get("_EnableUVAnimation") == 0
                    and floats.get("_EmissiveNoMapUseFresnel", 0) == 0
                    and keywords == {"_EMISSIVE_ANIM_SWEEP", "_EMISSIVE_NOMAP",
                                     "_TRI_CHANNEL_MASK", "_TWO_BASEMAP"})
    masked = (floats.get("_EmissiveMaskChannel") in (1, 2, 3)
              and floats.get("_BaseUVSet") == 0 and floats.get("_BasePbrMapUVSet") == 0
              and floats.get("_EnableUVAnimation") == 0
              and floats.get("_EmissiveNoMapUseFresnel", 0) == 0
              and keywords == {"_EMISSIVE_ANIM_SWEEP", "_EMISSIVE_MASK", "_TWO_BASEMAP"})
    return (surface_family(record) == "lit"
            and floats.get("_BaseUVSet") == 0 and floats.get("_BasePbrMapUVSet") == 0
            and floats.get("_EnableUVAnimation") == 0
            and record["Floats"].get("_EnableEmissiveAnimSweep", 0.0) > 0.5
            and (mapped or constant or masked or tri_constant))


def has_approximate_lit_sweep(record: MaterialRecord) -> bool:
    """Permit a source-driven UV0 preview without certifying the variant formula."""
    if surface_family(record) != "lit" or has_verified_lit_sweep(record):
        return False
    floats = record["Floats"]
    state = record.get("RenderState")
    keywords = set(state["ValidKeywords"]) if state is not None else set()
    allowed = {"_EMISSIVE_ANIM_SWEEP", "_EMISSIVE_MAP", "_EMISSIVE_NOMAP",
               "_EMISSIVE_MASK", "_TWO_BASEMAP", "_ALPHATEST_ON", "_TERRAIN_BLEND",
               "_LAYERBLEND_MASK", "_TRI_CHANNEL_MASK", "_DETAIL_MAP", "_PARALLAX_MAP",
               "_SUBSURFACE", "_SUBSURFACE_THICKNESSMAP", "_VAT_RIGIDBODY"}
    selector = floats.get("_EmissiveMaskChannel")
    branch = ("_EMISSIVE_MAP" if selector == 0 else
              "_EMISSIVE_NOMAP" if selector == 5 else
              "_EMISSIVE_MASK" if selector in (1, 2, 3) else None)
    if (branch is None or "_EMISSIVE_ANIM_SWEEP" not in keywords
            or not keywords <= allowed
            or keywords & {"_EMISSIVE_MAP", "_EMISSIVE_NOMAP", "_EMISSIVE_MASK"} != {branch}
            or floats.get("_EnableEmissiveAnimSweep", 0.0) <= 0.5
            or floats.get("_EnableEmissiveMap", 0.0) <= 0.5
            or floats.get("_BaseUVSet") != 0 or floats.get("_BasePbrMapUVSet") != 0
            or floats.get("_EnableUVAnimation") != 0
            or floats.get("_EmissiveNoMapUseFresnel") != 0):
        return False
    names = ("_EmissiveSweepSpeed", "_EmissiveSweepInterval", "_EmissiveSweepWidth",
             "_EmissiveSweepFalloff", "_EmissiveSweepHeightOffset", "_EmissiveSweepRandom",
             "_EmissiveSweepAlbedoScale", "_AlbedoAffectEmissive")
    if any(name not in floats or not math.isfinite(floats[name]) for name in names):
        return False
    if floats["_EmissiveSweepInterval"] <= 0 or floats["_EmissiveSweepWidth"] <= 0:
        return False
    if selector == 0:
        if floats.get("_EmissiveUVSet") != 0 or floats.get("_EmissiveType") not in (0, 1):
            return False
        bindings = [slot for slot in record["Textures"] if slot["Property"] == "_EmissiveMap"]
        if len(bindings) != 1 or bindings[0]["Texture"] is None:
            return False
        return bindings[0]["Texture"].get("ColorSpace") in (0, 1)
    return True


def has_unresolved_liteffect_sweep(record: MaterialRecord) -> bool:
    """Identify compiled LitEffect sweeps whose draw/instance inputs are unavailable."""
    shader = record["Shader"]
    state = record.get("RenderState")
    keywords = state["ValidKeywords"] if state is not None else []
    return (shader["Source"].casefold() == "cab-2c811ef28608ab220ecdb5c4e0629d2d"
            and shader["PathId"] == 6428594484694422749
            and (record["Floats"].get("_EnableEmissiveAnimSweep", 0.0) > 0.5
                 or "_EMISSIVE_ANIM_SWEEP" in keywords))
