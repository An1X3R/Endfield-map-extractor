"""Identify runtime-owned material slots from serialized component evidence."""
from __future__ import annotations

import math
from typing import Mapping, TypedDict, cast

from endfield_scene_material_contract import AssetReference, MaterialRecord, reference_key, require_object


def separate_surface_role(material: MaterialRecord) -> str | None:
    """Keep volume and effect shaders out of the ordinary surface preview."""
    shader = material["Shader"].get("Name", "")
    if shader.startswith("Hidden/VLB_"):
        return "volumetric_light_beam"
    if (shader in ("HGRP/FogSimple", "HGRP/Effect/VFXBaseV2", "HGRP/Effect/VFXDitherAlpha")
            or material["Name"].casefold().startswith(("m_fx_", "m_fxmap_", "m_water", "m_shadow", "m_fog", "m_volume"))):
        return "effect_or_volume_surface"
    return None


class RuntimeFogMaterialProvider(TypedDict):
    ComponentAsset: AssetReference
    ScriptAsset: AssetReference
    Namespace: str
    Class: str
    Enabled: bool
    UseFog: bool
    FogIntensity: float


def source_runtime_fog_provider(node: Mapping[str, object]) -> RuntimeFogMaterialProvider | None:
    """Classify known active fog providers without inventing a surface material."""
    values = node.get("RuntimeMaterialProviders", [])
    if not isinstance(values, list):
        raise ValueError("Source RuntimeMaterialProviders must be an array; refresh the scene probe")
    for value in values:
        provider = require_object(value, "runtime material provider")
        if (provider.get("Namespace") != "HG.Rendering.Runtime"
                or provider.get("Class") != "VFXFakeVolumeFog"):
            continue
        if type(provider.get("Enabled")) is not bool or type(provider.get("UseFog")) is not bool:
            raise ValueError(f"Source runtime fog provider requires boolean activity fields: provider={provider}")
        intensity = provider.get("FogIntensity")
        if type(intensity) not in (int, float) or not math.isfinite(cast(float, intensity)):
            raise ValueError(f"Source runtime fog intensity must be finite: provider={provider}")
        component = require_object(provider.get("ComponentAsset"), "runtime fog component")
        script = require_object(provider.get("ScriptAsset"), "runtime fog script")
        for reference, kind in ((component, "MonoBehaviour"), (script, "MonoScript")):
            if reference.get("Type") != kind or reference_key(reference)[1] == 0:
                raise ValueError(f"Source runtime fog provider has an invalid {kind} reference: reference={reference}")
        if provider["Enabled"] and provider["UseFog"]:
            return {
                "ComponentAsset": cast(AssetReference, dict(component)),
                "ScriptAsset": cast(AssetReference, dict(script)),
                "Namespace": "HG.Rendering.Runtime", "Class": "VFXFakeVolumeFog",
                "Enabled": True, "UseFog": True, "FogIntensity": float(cast(float, intensity)),
            }
    return None
