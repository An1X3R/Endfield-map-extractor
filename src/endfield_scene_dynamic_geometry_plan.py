"""Resolve ordinary Dynamic ECS geometry through the portable static slot contract."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, NotRequired, TypedDict

from endfield_map_layer_contract import classify_entity, is_hlod_name
from endfield_scene_material_contract import MaterialRecord, reference_key, supported_slots
from endfield_scene_slot_contract import RuntimeDraw, RuntimeLodDraw, asset_name, highest_detail_visible_meshes, runtime_submesh_slots
from endfield_scene_mesh_uv_contract import MeshUvCatalog, RequiredUvStreams, SourceUvStream, required_uv_channels, source_uv_streams

if TYPE_CHECKING:
    from extractor_core.dynamic_renderers import DynamicSceneRendererInventory
    from endfield_scene_export_plan import AssetRef, PlanInstance, RendererPair


class DynamicSourceDraw(TypedDict):
    mesh_path: str
    material_path: str
    mesh_hash_hex: str
    material_hash_hex: str
    reference_offset: int
    submesh: int
    flags: int
    lod_max_squared: float
    lod_min_squared: float
    reference_record_hex: str
    runtime_record_hex: str


class DynamicRoleExclusion(TypedDict):
    source_draw_index: int
    mesh_path: str
    material_path: str
    role: str


class DynamicGeometryRow(TypedDict):
    source_key: str
    mesh_part_key: NotRequired[str]
    selected_mesh_paths: NotRequired[list[str]]
    source_path: str
    source_sha256: str
    chunk_id: int
    init_scene_index: int
    group_index: int
    record_index: int
    reference_keys: list[str]
    common_instance_offset: int
    renderer_reference_offset: int
    runtime_draw_offset: int
    entity_base: str
    category: str
    matrix: list[float]
    mesh_path: str
    mesh_file: str
    mesh_asset: AssetRef
    renderer_pairs: list[RendererPair]
    source_renderer_pairs: list[DynamicSourceDraw]
    excluded_source_draw_roles: list[DynamicRoleExclusion]
    source_draw_indices: list[int]
    submesh_slot_indices: list[int]
    material_refs: list[AssetRef | None]
    undrawn_submeshes: list[int]
    selected_submeshes: list[int]
    serialized_draw_parameters: list[float]
    source_uv_streams: list[SourceUvStream]
    activation_status: str
    lod_selection: str


class DynamicGeometryPending(TypedDict):
    source_key: str
    component: str
    reason: str
    detail: str


@dataclass(frozen=True)
class DynamicGeometryPlan:
    rows: tuple[DynamicGeometryRow, ...]
    pending: tuple[DynamicGeometryPending, ...]
    required_resource_paths: tuple[str, ...]
    source_sha256: tuple[tuple[str, str], ...]
    excluded_static_duplicates: tuple[str, ...]
    selected_count: int
    required_uv_streams: tuple[RequiredUvStreams, ...]


def _draw_identity(row: PlanInstance | DynamicGeometryRow) -> tuple[object, ...]:
    """Compare complete selected draws, never merely overlapping bounds or positions."""
    selected = set(row.get("selected_submeshes", range(len(row["material_refs"]))))
    draws = tuple(sorted((pair["mesh_path"], pair["material_path"], pair["submesh"], pair["flags"])
                         for pair in row["renderer_pairs"]
                         if not pair["flags"] & 1024 and pair["submesh"] in selected))
    refs = tuple((index, reference_key(ref)) for index, ref in enumerate(row["material_refs"])
                 if index in selected and ref is not None)
    parameters = row.get("serialized_draw_parameters")
    return (reference_key(row["mesh_asset"]), tuple(row["matrix"]), draws, refs,
            tuple(parameters) if parameters is not None else None)


def validate_dynamic_mesh_parts(rows: tuple[DynamicGeometryRow, ...], static_instances: tuple[PlanInstance, ...]) -> None:
    """Require every co-authored part, including exact parts already drawn statically."""
    groups: dict[str, list[DynamicGeometryRow]] = {}
    for row in rows:
        groups.setdefault(row["source_key"], []).append(row)
    shared = ("source_path", "source_sha256", "chunk_id", "init_scene_index", "group_index", "record_index",
              "reference_keys", "common_instance_offset", "renderer_reference_offset", "runtime_draw_offset",
              "entity_base", "category", "matrix", "serialized_draw_parameters", "source_renderer_pairs",
              "selected_mesh_paths", "activation_status", "lod_selection")
    for key, parts in groups.items():
        first = parts[0]
        if any(any(row.get(field) != first.get(field) for field in shared) for row in parts):
            raise ValueError(f"Dynamic mesh parts disagree on their original record evidence: source_key={key}")
        if "selected_mesh_paths" not in first:
            continue
        missing = set(first["selected_mesh_paths"]) - {row["mesh_path"] for row in parts}
        for path in missing:
            draws = [{"mesh_path": draw["mesh_path"], "material_path": draw["material_path"],
                      "mesh_id": draw["mesh_hash_hex"], "material_id": draw["material_hash_hex"],
                      "submesh": draw["submesh"], "flags": draw["flags"]}
                     for draw in first["source_renderer_pairs"] if draw["mesh_path"] == path
                     and not draw["flags"] & 1024 and "/effects/" not in draw["material_path"]]
            if not any(row["mesh_path"] == path and _draw_identity(row) == _draw_identity({
                    **row, "matrix": first["matrix"], "renderer_pairs": draws,
                    "serialized_draw_parameters": first["serialized_draw_parameters"]}) for row in static_instances):
                raise ValueError(f"Dynamic co-authored mesh part set is incomplete: source_key={key}, mesh={path}")


def build_dynamic_geometry_plan(
    inventory: DynamicSceneRendererInventory,
    bounds: Mapping[str, float],
    selection: Mapping[str, float],
    allowed_categories: frozenset[str],
    containers: Mapping[str, set[tuple[str, int]]],
    mesh_exports: Mapping[tuple[str, int], tuple[AssetRef, Path]],
    materials: Mapping[tuple[str, int], MaterialRecord],
    textures: Mapping[tuple[str, int], tuple[Path, str]],
    static_instances: tuple[PlanInstance, ...],
    uv_catalog: MeshUvCatalog,
) -> DynamicGeometryPlan:
    """Select the authored base LOD as an explicit preview, without runtime activation."""
    from endfield_scene_export_plan import owns_static_footprint
    from prepare_scene_geometry_repairs import obj_submesh_count

    sources = {source.virtual_path: source.decoded_sha256 for source in inventory.sources}
    static = {_draw_identity(row) for row in static_instances}
    rows: list[DynamicGeometryRow] = []
    pending: list[DynamicGeometryPending] = []
    required: set[str] = set()
    duplicates: list[str] = []
    hashes = {source.virtual_path: source.decoded_sha256 for source in inventory.sources if source.kind == "main"}
    submesh_counts: dict[Path, int] = {}
    required_uv: dict[str, set[int]] = {}
    selected_count = 0
    for record in inventory.records:
        if is_hlod_name(record.name):
            continue
        ordinary = tuple((index, pair) for index, pair in enumerate(record.pairs) if not pair.draw.flags & 1024
                         and "/effects/" not in pair.mesh_path and "/effects/" not in pair.material_path
                         and not is_hlod_name(pair.mesh_path))
        if not ordinary:
            continue
        category, _ = classify_entity(asset_name(ordinary[0][1].mesh_path))
        if category not in allowed_categories:
            continue
        origin = (record.matrix[12], record.matrix[14])
        if record.bounds is None:
            owned = bounds["xmin"] <= origin[0] < bounds["xmax"] and bounds["zmin"] <= origin[1] < bounds["zmax"]
        else:
            owned = owns_static_footprint(bounds, selection, origin, record.bounds)
        if not owned:
            continue
        selected_count += 1
        hashes[record.source_virtual_path] = sources[record.source_virtual_path]
        try:
            mesh_paths = highest_detail_visible_meshes(tuple((pair.mesh_path, RuntimeLodDraw(
                pair.draw.submesh, pair.draw.flags, pair.draw.lod_max_squared, pair.draw.lod_min_squared))
                for _, pair in ordinary))
            required.update(mesh_paths)
            required.update(pair.material_path for _, pair in ordinary if pair.mesh_path in mesh_paths)
            record_rows: list[DynamicGeometryRow] = []
            for mesh_path in mesh_paths:
                active = tuple((index, pair) for index, pair in ordinary if pair.mesh_path == mesh_path)
                active_indices = {index for index, _ in active}
                role_exclusions: list[DynamicRoleExclusion] = [
                    {"source_draw_index": index, "mesh_path": pair.mesh_path,
                     "material_path": pair.material_path, "role": "effect_resource_requires_separate_effect_renderer"}
                    for index, pair in enumerate(record.pairs)
                    if pair.mesh_path == mesh_path and not pair.draw.flags & 1024 and index not in active_indices]
                required.add(mesh_path)
                required.update(pair.material_path for _, pair in active)
                candidates = [mesh_exports[key] for key in containers.get(mesh_path.split("##", 1)[0], set())
                              if key in mesh_exports and mesh_exports[key][0]["Name"].casefold() == asset_name(mesh_path)]
                if len({reference_key(asset) for asset, _ in candidates}) != 1:
                    raise ValueError(f"Dynamic base mesh lacks unique exact SceneProbe Container/OBJ: {mesh_path}")
                mesh_asset, mesh_file = candidates[0]
                if mesh_file not in submesh_counts:
                    submesh_counts[mesh_file] = obj_submesh_count(mesh_file)
                count = submesh_counts[mesh_file]
                mapping = runtime_submesh_slots(count, tuple(pair.material_hash_hex for _, pair in active),
                                                tuple(RuntimeDraw(pair.draw.submesh, pair.draw.flags) for _, pair in active))
                refs: list[AssetRef | None] = []
                for slot in mapping.indices:
                    if slot == len(active):
                        refs.append(None)
                        continue
                    pair = active[slot][1]
                    keys = containers.get(pair.material_path, set())
                    if len(keys) != 1:
                        raise ValueError(f"Dynamic ordinary material container has {len(keys)} AssetRefs: {pair.material_path}")
                    key = next(iter(keys))
                    if key not in materials:
                        raise ValueError(f"Dynamic ordinary material record missing: {pair.material_path}:{key}")
                    material = materials[key]
                    if (material["Shader"].get("Name") in ("HGRP/FogSimple", "HGRP/Effect/VFXBaseV2", "HGRP/Effect/VFXDitherAlpha")
                            or material["Name"].casefold().startswith(("m_fx_", "m_fxmap_", "m_water", "m_shadow", "m_fog", "m_volume"))):
                        raise ValueError(f"Dynamic non-ordinary material requires its separate renderer role: {material['Name']}")
                    for texture_slot in supported_slots(material):
                        texture = texture_slot["Texture"]
                        if texture is not None and reference_key(texture) not in textures:
                            raise ValueError(f"Dynamic required material texture missing: {material['Name']}:{texture_slot['Property']}")
                    refs.append({"Source": material["Source"], "PathId": material["PathId"], "Name": material["Name"]})
                channels = tuple(sorted({channel for ref in refs if ref is not None
                                         for channel in required_uv_channels(materials[reference_key(ref)])}))
                if channels:
                    required_uv.setdefault(mesh_path, set()).update(channels)
                mesh_uv_streams = source_uv_streams(uv_catalog, mesh_asset, channels)
                row: DynamicGeometryRow = {
                    "source_key": record.key, "source_path": record.source_virtual_path,
                    "source_sha256": sources[record.source_virtual_path], "chunk_id": record.chunk_id,
                    "init_scene_index": record.init_scene_index, "group_index": record.group_index,
                    "record_index": record.record_index, "reference_keys": list(record.reference_keys),
                    "common_instance_offset": record.common_instance_offset,
                    "renderer_reference_offset": record.renderer_reference_offset, "runtime_draw_offset": record.runtime_draw_offset,
                    "entity_base": record.name or (asset_name(mesh_paths[0]) if len(mesh_paths) > 1 else mesh_asset["Name"]), "category": category,
                    "matrix": list(record.matrix), "mesh_path": mesh_path, "mesh_file": str(mesh_file), "mesh_asset": mesh_asset,
                    "renderer_pairs": [{"material_id": pair.material_hash_hex, "material_path": pair.material_path,
                                        "mesh_id": pair.mesh_hash_hex, "mesh_path": pair.mesh_path,
                                        "submesh": pair.draw.submesh, "flags": pair.draw.flags} for _, pair in active],
                    "source_renderer_pairs": [{"mesh_path": pair.mesh_path, "material_path": pair.material_path,
                                               "mesh_hash_hex": pair.mesh_hash_hex, "material_hash_hex": pair.material_hash_hex,
                                               "reference_offset": pair.reference_offset, "submesh": pair.draw.submesh,
                                               "flags": pair.draw.flags, "lod_max_squared": pair.draw.lod_max_squared,
                                               "lod_min_squared": pair.draw.lod_min_squared,
                                               "reference_record_hex": pair.reference_record_hex,
                                               "runtime_record_hex": pair.runtime_record_hex} for pair in record.pairs],
                    "source_draw_indices": [index for index, _ in active],
                    "excluded_source_draw_roles": role_exclusions,
                    "submesh_slot_indices": list(mapping.indices), "material_refs": refs,
                    "undrawn_submeshes": list(mapping.undrawn_submeshes),
                    "selected_submeshes": [index for index in range(count) if index not in mapping.undrawn_submeshes],
                    "serialized_draw_parameters": list(record.serialized_draw_parameters),
                    "source_uv_streams": mesh_uv_streams,
                    "activation_status": "serialized_initial_not_runtime",
                    "lod_selection": "authored_base_lod_reference_not_runtime_camera_selection",
                }
                if len(mesh_paths) > 1:
                    mesh_ids = {pair.mesh_hash_hex for _, pair in active}
                    if len(mesh_ids) != 1:
                        raise ValueError(f"Dynamic mesh part has conflicting native mesh IDs: source_key={record.key}, mesh={mesh_path}")
                    row.update({"mesh_part_key": f"{record.key}/mesh:{next(iter(mesh_ids))}",
                                "selected_mesh_paths": list(mesh_paths)})
                record_rows.append(row)
            visible_rows = [row for row in record_rows if _draw_identity(row) not in static]
            if visible_rows:
                rows.extend(visible_rows)
            else:
                duplicates.append(record.key)
        except ValueError as error:
            pending.append({"source_key": record.key, "component": "dynamic_geometry",
                            "reason": "exact_source_incomplete", "detail": str(error)})
    return DynamicGeometryPlan(tuple(rows), tuple(pending), tuple(sorted(required)), tuple(sorted(hashes.items())),
                               tuple(duplicates), selected_count,
                               tuple({"mesh_path": path, "channels": sorted(channels)} for path, channels in sorted(required_uv.items())))
