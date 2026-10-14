"""Compatibility exports for the source-driven Hair lighting implementation."""
from __future__ import annotations

from character_hair_lighting import (
    HairBranchReceipt,
    HairLineMapBinding,
    HairShaderFrame,
    HairSpecularParameters,
    apply_hair_specular_branch,
    bind_source_line_map,
    line_map_binding_from_material,
    set_hair_specular_enabled,
    source_parameters,
)
from character_hgrp_diffuse import build_hgrp_diffuse
from patch_character_hgrp import attach_diffuse


__all__ = (
    'HairBranchReceipt',
    'HairLineMapBinding',
    'HairShaderFrame',
    'HairSpecularParameters',
    'apply_hair_specular_branch',
    'attach_diffuse',
    'bind_source_line_map',
    'build_hgrp_diffuse',
    'line_map_binding_from_material',
    'set_hair_specular_enabled',
    'source_parameters',
)
