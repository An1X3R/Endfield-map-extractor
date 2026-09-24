"""Dependency-free projected decal record shared by extraction and Blender."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectedDecal:
    entity_id: int
    entity_base: str
    source_path: str
    matrix: tuple[float, ...]
    inverse_matrix: tuple[float, ...]
    uv_scale: tuple[float, ...]
    uv_offset: tuple[float, ...]
    color: tuple[float, ...]
    intensity: float
    sector_angle: float
    packed_flags: int
    material_id: str
    material_path: str
    material_byte_offset: int
    raw_hex: str
