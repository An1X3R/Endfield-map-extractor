"""Select static ECS rows against exact spatial sources without side effects."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import cast


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


def select_static_source_rows(
    source: str,
    rows: tuple[StaticSpatialRow, ...],
    records: Iterable[Mapping[str, object]],
    bounds: Mapping[str, float],
    selection: Mapping[str, float],
    allowed_categories: frozenset[str],
    water_world_bounds: tuple[WorldBounds, ...],
    effects_selected: bool,
) -> StaticSpatialSelection:
    """Select source rows and visibility candidates from already-decoded ECS records."""
    spatial = {cast(int, record["entity_id"]): record for record in records}
    selected_rows: list[StaticSpatialRow] = []
    visible_static_ids: set[int] = set()
    bottom_candidate_ids: set[int] = set()
    surface_candidate_ids: set[int] = set()
    selected_static_count = 0
    footprint_extras = 0
    for row in rows:
        entity_id, _, matrix_blob, _, _, _, category = row
        record = spatial.get(entity_id)
        if record is None or record["matrix_blob"] != matrix_blob:
            raise ValueError(f"Current spatial source differs from instance index: entity_id={entity_id}, source={source}")
        footprint_value = record["world_bounds"]
        if footprint_value is None:
            raise ValueError(f"Static entity has no source world bounds: entity_id={entity_id}, source={source}")
        footprint = cast(tuple[float, ...], footprint_value)
        origin = (cast(float, record["x"]), cast(float, record["z"]))
        visible = category in allowed_categories and owns_static_footprint(bounds, selection, origin, footprint)
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
    )
