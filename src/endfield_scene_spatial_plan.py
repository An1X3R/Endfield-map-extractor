"""Select static ECS rows against exact spatial sources without side effects."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
import re
import struct
from typing import cast

from endfield_map_layer_contract import is_hlod_name


StaticSpatialRow = tuple[int, str, bytes, str, int, int, str]
WorldBounds = tuple[float, float, float, float, float, float]


@dataclass(frozen=True)
class StaticSpatialSelection:
    selected_rows: tuple[StaticSpatialRow, ...]
    visible_static_ids: frozenset[int]
    bottom_candidate_ids: frozenset[int]
    surface_candidate_ids: frozenset[int]
    selected_static_count: int
    footprint_extras: int
    excluded_level_ids: frozenset[int]


def owns_static_footprint(
    bounds: Mapping[str, float],
    selection: Mapping[str, float],
    origin: tuple[float, float],
    footprint: tuple[float, ...],
) -> bool:
    """Assign crossing geometry once using the legacy clipped-footprint owner rule."""
    x, z = origin
    if selection["xmin"] <= x < selection["xmax"] and selection["zmin"] <= z < selection["zmax"]:
        return bounds["xmin"] <= x < bounds["xmax"] and bounds["zmin"] <= z < bounds["zmax"]
    xmin, xmax, _, _, zmin, zmax = footprint
    if xmax <= selection["xmin"] or xmin >= selection["xmax"] or zmax <= selection["zmin"] or zmin >= selection["zmax"]:
        return False
    owner_x, owner_z = max(xmin, selection["xmin"]), max(zmin, selection["zmin"])
    return bounds["xmin"] <= owner_x < bounds["xmax"] and bounds["zmin"] <= owner_z < bounds["zmax"]


def _shared_pivot_owners(
    source: str,
    rows: tuple[StaticSpatialRow, ...],
    spatial: Mapping[int, Mapping[str, object]],
    selection: Mapping[str, float],
    allowed_categories: frozenset[str],
) -> dict[int, tuple[float, float] | None]:
    """Keep authored Global assemblies whole without owning empty union-AABB gaps."""
    match = re.fullmatch(r"Data/Streaming/PC/[^/]+/Streaming/InitChunkData_Global_0_(-?[0-9]+)\.bytes", source, re.I)
    if match is None or int(match[1]) == 0:
        return {}
    groups: dict[tuple[float, float, float], list[int]] = {}
    for entity_id, base, matrix_blob, _, _, _, category in rows:
        if category not in allowed_categories or category not in {"building", "prop"} or is_hlod_name(base):
            continue
        if len(matrix_blob) != 64:
            raise ValueError(f"Static assembly matrix has invalid length: entity_id={entity_id}, source={source}")
        pivot = cast(tuple[float, float, float], struct.unpack_from("<3f", matrix_blob, 48))
        if not all(math.isfinite(value) for value in pivot):
            raise ValueError(f"Static assembly pivot is non-finite: entity_id={entity_id}, source={source}")
        if pivot != (0.0, 0.0, 0.0):
            groups.setdefault(pivot, []).append(entity_id)
    owners: dict[int, tuple[float, float] | None] = {}
    for (x, _, z), members in groups.items():
        if len(members) < 2:
            continue
        intersections: list[tuple[float, float]] = []
        for entity_id in members:
            footprint = cast(WorldBounds, spatial[entity_id]["world_bounds"])
            if (footprint[0] < selection["xmax"] and footprint[1] > selection["xmin"]
                    and footprint[4] < selection["zmax"] and footprint[5] > selection["zmin"]):
                intersections.append((max(footprint[0], selection["xmin"]), max(footprint[4], selection["zmin"])))
        owner: tuple[float, float] | None = None
        if intersections:
            owner = ((x, z) if selection["xmin"] <= x < selection["xmax"]
                     and selection["zmin"] <= z < selection["zmax"] else min(intersections))
        owners.update((entity_id, owner) for entity_id in members)
    return owners


def select_static_source_rows(
    source: str,
    rows: tuple[StaticSpatialRow, ...],
    records: Iterable[Mapping[str, object]],
    bounds: Mapping[str, float],
    selection: Mapping[str, float],
    allowed_categories: frozenset[str],
    water_world_bounds: tuple[WorldBounds, ...],
    effects_selected: bool,
    static_namespace: int | None,
) -> StaticSpatialSelection:
    """Select source rows and visibility candidates from already-decoded ECS records."""
    spatial = {cast(int, record["entity_id"]): record for record in records}
    for entity_id, _, matrix_blob, _, _, _, _ in rows:
        record = spatial.get(entity_id)
        if record is None or record["matrix_blob"] != matrix_blob:
            raise ValueError(f"Current spatial source differs from instance index: entity_id={entity_id}, source={source}")
        if record["world_bounds"] is None:
            raise ValueError(f"Static entity has no source world bounds: entity_id={entity_id}, source={source}")
    visible_rows = tuple(row for row in rows if static_namespace is None or row[0] >> 27 == static_namespace)
    assembly_owners = _shared_pivot_owners(source, visible_rows, spatial, selection, allowed_categories)
    selected_rows: list[StaticSpatialRow] = []
    visible_static_ids: set[int] = set()
    bottom_candidate_ids: set[int] = set()
    surface_candidate_ids: set[int] = set()
    selected_static_count = 0
    footprint_extras = 0
    excluded_level_ids: set[int] = set()
    for row in rows:
        entity_id, _, _, _, _, _, category = row
        record = spatial[entity_id]
        footprint = cast(tuple[float, ...], record["world_bounds"])
        origin = (cast(float, record["x"]), cast(float, record["z"]))
        if entity_id in assembly_owners:
            owner = assembly_owners[entity_id]
            owned = (owner is not None and bounds["xmin"] <= owner[0] < bounds["xmax"]
                     and bounds["zmin"] <= owner[1] < bounds["zmax"])
        else:
            owned = owns_static_footprint(bounds, selection, origin, footprint)
        visible = category in allowed_categories and owned
        if visible and static_namespace is not None and entity_id >> 27 != static_namespace:
            excluded_level_ids.add(entity_id)
            visible = False
        bottom = any(
            footprint[0] < min(water[1], bounds["xmax"])
            and footprint[1] > max(water[0], bounds["xmin"])
            and footprint[4] < min(water[5], bounds["zmax"])
            and footprint[5] > max(water[4], bounds["zmin"])
            and footprint[2] < water[3]
            and footprint[3] > water[2] - 150.0
            for water in water_world_bounds
        )
        surface_candidate = effects_selected and owns_static_footprint(bounds, selection, origin, footprint)
        if bottom:
            bottom_candidate_ids.add(entity_id)
        if surface_candidate:
            surface_candidate_ids.add(entity_id)
        if visible or bottom or surface_candidate:
            selected_rows.append(row)
        if visible:
            visible_static_ids.add(entity_id)
            selected_static_count += 1
            if not (selection["xmin"] <= origin[0] < selection["xmax"]
                    and selection["zmin"] <= origin[1] < selection["zmax"]):
                footprint_extras += 1
    return StaticSpatialSelection(
        tuple(selected_rows),
        frozenset(visible_static_ids),
        frozenset(bottom_candidate_ids),
        frozenset(surface_candidate_ids),
        selected_static_count,
        footprint_extras,
        frozenset(excluded_level_ids),
    )
