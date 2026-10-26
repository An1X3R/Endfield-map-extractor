"""Prepare a bounded scene plan from current first-run assets and ECS evidence."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from collections.abc import Iterable
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from endfield_scene_io import (SceneSourceCacheSlot, cached_scene_source_value,
                               configure_scene_manifest_cache, read_scene_manifest)
from typing import Callable, Literal, Mapping, NotRequired, TypedDict, cast

from extractor_core.scene_identity import validate_scene_id
from extractor_core.vfs import VfsFile
from endfield_map_layer_contract import ENTITY_CATEGORY_VERSION, classify_entity, is_hlod_name
from endfield_scene_material_contract import (reference_key, resolve_export_path,
                                             resolve_existing_export_files, supported_slots)
from endfield_scene_material_handoff import (MaterialCatalogHandoff, build_material_catalog_handoff,
                                              capture_material_catalog_sources, require_material_catalog_sources)
from endfield_scene_planning_catalog import load_planning_catalog
from endfield_scene_effects_plan import EffectInstance, build_effect_plan
from endfield_scene_direct_effects_plan import (DirectEffectRow, DirectEffectPending, build_direct_effect_plan,
                                                validate_direct_effect_rows)
from endfield_scene_dynamic_geometry_plan import (DynamicGeometryRow, DynamicGeometryPending, build_dynamic_geometry_plan,
                                                   validate_dynamic_mesh_parts)
from endfield_scene_prefab_geometry_plan import (PrefabGeometryRow, PrefabGeometryPending, build_prefab_geometry_plan,
                                               validate_prefab_geometry_plan)
from endfield_scene_mesh_uv_contract import (MeshUvCatalog, RequiredUvStreams, SourceUvStream,
                                             load_mesh_uv_catalog, required_uv_channels, source_uv_streams)
from endfield_scene_lights_plan import (LightPlanRow, build_light_plan,
                                        build_water_transport_directional_dependency,
                                        validate_light_plan_rows)
from endfield_water_transport import (WaterTransportLighting, WaterTransportLightingRow,
                                     read_water_transport_lighting,
                                     serialize_water_transport_lighting)
from endfield_scene_slot_contract import (
    AmbiguousSubmeshSlots, RuntimeDraw, RuntimeLodDraw, asset_name, primary_visible_mesh, highest_detail_visible_meshes,
    runtime_submesh_slots, runtime_submesh_passes, validate_ordered_surface_passes,
)
from endfield_scene_vfxrefract_contract import SceneColorSnapshotPlan, read_scene_color_snapshot
from endfield_effect_presentation import FlowPresentation, read_flow_presentation
from endfield_scene_spatial_plan import (StaticSpatialRow, StaticSpatialSelection, WorldBounds,
                                         owns_static_footprint, select_static_source_rows)
from endfield_scene_spatial_cache import (read_spatial_source_records, require_spatial_source_version,
                                          spatial_parser_identity, spatial_source_identity)
from webui_source_contract import FogDepthPlan, read_fog_depth_options, selected_static_categories


FORMAT = "EndfieldPortableScenePlan/1"


@dataclass(frozen=True)
class _CachedSpatialSource:
    source: str
    selected_rows: tuple[StaticSpatialRow, ...]
    chunk: bytes
    source_sha256: str
    renderer_columns: Mapping[int, Mapping[int, bytes]]


@dataclass(frozen=True)
class _CachedSpatialSelection:
    sources: tuple[_CachedSpatialSource, ...]
    visible_static_ids: frozenset[int]
    bottom_candidate_ids: frozenset[int]
    surface_candidate_ids: frozenset[int]
    selected_static_count: int
    footprint_extras: int


_MANIFEST_INDEX_CACHE = SceneSourceCacheSlot(
    "scene-manifest-index", tuple, 256, 256 * 1024 * 1024
)
_SPATIAL_SELECTION_CACHE = SceneSourceCacheSlot(
    "scene-spatial-selection-v2", _CachedSpatialSelection, 2, 128 * 1024 * 1024
)


class AssetRef(TypedDict):
    Source: str
    PathId: int
    Name: str


class Bounds(TypedDict):
    xmin: float
    xmax: float
    zmin: float
    zmax: float


class RendererPair(TypedDict):
    material_id: str
    material_path: str
    mesh_id: str
    mesh_path: str
    submesh: int
    flags: int


class StaticSourceDraw(RendererPair):
    reference_component: int
    source_draw_index: int
    lod_max_squared: float
    lod_min_squared: float


class PlanInstance(TypedDict):
    entity_id: int
    entity_base: str
    category: str
    source_path: str
    source_sha256: str
    group_index: int
    entity_index: int
    matrix: list[float]
    mesh_path: str
    mesh_file: str
    mesh_asset: AssetRef
    renderer_pairs: list[RendererPair]
    submesh_slot_indices: list[int]
    material_refs: list[AssetRef | None]
    undrawn_submeshes: list[int]
    selected_submeshes: NotRequired[list[int]]
    serialized_draw_parameters: NotRequired[list[float] | None]
    source_uv_streams: NotRequired[list[SourceUvStream]]
    submesh_material_passes: NotRequired[list[list[AssetRef]]]
    submesh_pass_indices: NotRequired[list[list[int]]]
    mesh_part_key: NotRequired[str]
    source_renderer_pairs: NotRequired[list[StaticSourceDraw]]
    source_draw_indices: NotRequired[list[int]]
    source_draw_evidence: NotRequired[list[StaticSourceDraw]]
    selected_mesh_paths: NotRequired[list[str]]
    lod_selection: NotRequired[str]


class WaterMeshAssetPlan(TypedDict):
    source: str
    path_id: int
    name: str


class WaterGeometryPlanRow(TypedDict):
    entity_id: int
    name: str
    init_source_path: str
    streaming_source_path: str
    group_index: int
    entity_index: int
    matrix: list[float]
    world_bounds: list[float]
    ownership_anchor: list[float]
    render_type_mask: int
    runtime_mesh_id: int
    param0: list[float]
    param1: list[float]
    mesh_resource_path: str
    mesh_hash_hex: str
    config_resource_path: str
    config_hash_hex: str
    mesh_file: str
    mesh_asset: WaterMeshAssetPlan


class WaterAssetPlan(TypedDict):
    source: str
    path_id: int
    type_name: str


class WaterTexturePlan(TypedDict):
    asset: WaterAssetPlan
    file: str


class WaterNormalPlan(TypedDict):
    asset: WaterAssetPlan
    slice_index: int
    file: str


class WaterFlowPlan(TypedDict):
    use_flow_map: bool
    material_index: int
    flow_direction: float
    flow_direction_vector: list[float]
    small_wave_normal_type: int
    large_wave_normal_type: int
    flow_speed: float
    large_wave_flow_speed: float
    normal_tilling: float
    large_wave_normal_tilling: float
    normal_scale: float
    large_wave_normal_scale: float
    water_ior: float


class WaterSectorPlanRow(TypedDict):
    resource_path: str
    sector_x: int
    sector_z: int
    asset: WaterAssetPlan
    texture: WaterTexturePlan
    source_json: str


class WaterMaterialPlanRow(TypedDict):
    entity_id: int
    config_resource_path: str
    config_asset: WaterAssetPlan
    global_resource_path: str
    global_asset: WaterAssetPlan
    normal_array_resource_path: str
    flow: WaterFlowPlan
    flow_map: WaterTexturePlan | None
    small_wave_normal: WaterNormalPlan
    large_wave_normal: WaterNormalPlan
    sectors: list[WaterSectorPlanRow]
    sector_complete: bool
    sector_mapping_status: str
    config_source_json: str
    global_source_json: str


class Pending(TypedDict):
    entity_id: int
    entity_base: str
    component: str
    reason: str
    detail: str


class StaticSubmeshPending(Pending):
    source_path: str
    source_sha256: str
    group_index: int
    entity_index: int
    mesh_path: str
    submesh: int
    material_name: str
    material_path: str
    material_id: str
    mesh_id: str
    source_draw: StaticSourceDraw


class StaticRoleExclusion(TypedDict):
    entity_id: int
    entity_base: str
    source_path: str
    source_sha256: str
    group_index: int
    entity_index: int
    role: Literal["shadow_only"]
    source_draws: list[StaticSourceDraw]


class EffectPhysicsPlan(TypedDict):
    source: str
    source_sha256: str
    path_id: int
    gravity: list[float]


class SceneExportPlan(TypedDict):
    format: str
    map_id: str
    bounds: Bounds
    category_filter: list[str]
    scene_manifests: list[str]
    material_catalog: NotRequired[MaterialCatalogHandoff]
    prefab_bounds_audit: NotRequired[str]
    terrain_surfaces: list[str]
    water_depth_surfaces: list[str]
    water_depth_instances: list[PlanInstance]
    water_depth_pending: list[Pending]
    terrain_details: list[str]
    decal_manifest: str
    decal_materials: str
    instances: list[PlanInstance]
    static_role_exclusions: list[StaticRoleExclusion]
    dynamic_geometry_instances: list[DynamicGeometryRow]
    dynamic_geometry_pending: list[DynamicGeometryPending]
    prefab_geometry_instances: list[PrefabGeometryRow]
    prefab_geometry_pending: list[PrefabGeometryPending]
    prefab_placements: list[dict[str, object]]
    prefab_geometry_exclusions: list[dict[str, object]]
    required_prefab_geometry_paths: list[str]
    surface_effect_instances: list[PlanInstance]
    effect_instances: list[EffectInstance]
    effect_physics: EffectPhysicsPlan | None
    direct_effect_renderers: list[DirectEffectRow]
    direct_effect_pending: list[DirectEffectPending]
    required_effect_mesh_paths: list[str]
    light_instances: list[LightPlanRow]
    water_geometry: list[WaterGeometryPlanRow]
    water_materials: list[WaterMaterialPlanRow]
    water_transport_lighting: NotRequired[WaterTransportLightingRow]
    water_transport_lighting_status: NotRequired[Literal["not_applicable"]]
    fog_depth: NotRequired[FogDepthPlan]
    scene_color_snapshot: NotRequired[SceneColorSnapshotPlan]
    effect_flow_presentation: NotRequired[FlowPresentation]
    pending: list[Pending]
    source_sha256: dict[str, str]
    summary: dict[str, int]
    required_resource_paths: list[str]
    required_uv_streams: list[RequiredUvStreams]


def _cached_spatial_selection(
    instance_db: Path,
    cache_root: Path,
    vfs: Path,
    entries: Mapping[str, VfsFile],
    by_source: Mapping[str, tuple[StaticSpatialRow, ...]],
    decal_source_names: frozenset[str],
    bounds: Bounds,
    selection: Bounds,
    groups: set[str],
    layers: Mapping[str, object],
    allowed_categories: frozenset[str],
    water_selected: bool,
    effects_selected: bool,
    include_decals: bool,
    water_geometry_rows: list[WaterGeometryPlanRow],
    read_chunk: Callable[[Path, VfsFile], bytes],
    component_columns: Callable[[bytes], dict[int, dict[int, bytes]]],
    iter_entities: Callable[[bytes], Iterable[Mapping[str, object]]],
    check_cancelled: Callable[[], None] | None,
) -> _CachedSpatialSelection:
    """Select from persisted spatial records and decode only selected/decal sources."""
    candidate_sources = tuple(sorted(set(by_source) | decal_source_names))
    water_sources = tuple(sorted({
        source
        for row in water_geometry_rows
        for source in (row["init_source_path"], row["streaming_source_path"])
    }))
    source_names = tuple(sorted(set(candidate_sources) | set(water_sources)))
    source_paths = [instance_db]
    entry_qualifiers: list[str] = []
    for source in source_names:
        entry = entries.get(source)
        if entry is None:
            raise FileNotFoundError(f"Spatial source is absent from VFS: {source}")
        source_paths.append(vfs / "C3442D43" / f"{entry.file_chunk_md5_name}.chk")
        entry_qualifiers.extend((source, entry.file_name, entry.file_chunk_md5_name,
                                 entry.file_data_md5, str(entry.offset), str(entry.length)))
    unique_sources = tuple({str(path): path for path in source_paths}.values())
    parser_identity = spatial_parser_identity()
    spatial_identities = {
        source: spatial_source_identity(cache_root, vfs, entries[source], parser_identity)
        for source in by_source
    }
    water_bounds = tuple(
        cast(WorldBounds, tuple(row["world_bounds"]))
        for row in water_geometry_rows
    )
    filter_qualifier = json.dumps({
        "bounds": bounds,
        "selection": selection,
        "groups": sorted(groups),
        "layers": {name: layers.get(name) for name in
                   ("terrain", "instances", "roads", "vegetation", "water", "effects", "lights")},
        "allowed_categories": sorted(allowed_categories),
        "water_selected": water_selected,
        "effects_selected": effects_selected,
        "include_decals": include_decals,
        "water_geometry": [
            [row["entity_id"], row["init_source_path"], row["streaming_source_path"], row["world_bounds"]]
            for row in water_geometry_rows
        ],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    qualifiers = ("scene-spatial-selection-v2", ENTITY_CATEGORY_VERSION, str(cache_root.resolve()), parser_identity,
                  filter_qualifier, *entry_qualifiers,
                  *(spatial_identities[source].metadata for source in sorted(spatial_identities)))

    def build() -> _CachedSpatialSelection:
        selected_sources: list[_CachedSpatialSource] = []
        visible_static_ids: set[int] = set()
        bottom_candidate_ids: set[int] = set()
        surface_candidate_ids: set[int] = set()
        selected_static_count = 0
        footprint_extras = 0
        for source in candidate_sources:
            if check_cancelled is not None:
                check_cancelled()
            entry = entries[source]
            chunk: bytes | None = None
            rows = by_source.get(source, ())
            if rows:
                spatial_source = read_spatial_source_records(
                    spatial_identities[source], vfs, entry, read_chunk, iter_entities, check_cancelled)
                chunk = spatial_source.decoded_chunk
                source_selection = select_static_source_rows(
                    source, rows, spatial_source.records, bounds, selection, allowed_categories,
                    water_bounds, effects_selected,
                )
                visible_static_ids.update(source_selection.visible_static_ids)
                bottom_candidate_ids.update(source_selection.bottom_candidate_ids)
                surface_candidate_ids.update(source_selection.surface_candidate_ids)
                selected_static_count += source_selection.selected_static_count
                footprint_extras += source_selection.footprint_extras
                selected_rows = source_selection.selected_rows
                del spatial_source
            else:
                selected_rows = ()
            if selected_rows or source in decal_source_names:
                if chunk is None:
                    chunk = read_chunk(vfs, entry)
                    if rows:
                        require_spatial_source_version(spatial_identities[source])
                all_columns = component_columns(chunk) if selected_rows else {}
                selected_ids = {row[0] for row in selected_rows}
                selected_columns = {entity_id: all_columns[entity_id] for entity_id in selected_ids
                                    if entity_id in all_columns}
                selected_sources.append(_CachedSpatialSource(
                    source, selected_rows, chunk, hashlib.sha256(chunk).hexdigest(), selected_columns))
        return _CachedSpatialSelection(
            tuple(selected_sources), frozenset(visible_static_ids), frozenset(bottom_candidate_ids),
            frozenset(surface_candidate_ids), selected_static_count, footprint_extras,
        )

    return cached_scene_source_value(_SPATIAL_SELECTION_CACHE, unique_sources, qualifiers, build)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    return cast(dict[str, object], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} requires nonempty text")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return cast(list[object], value)


def _file(value: object, label: str) -> Path:
    path = resolve_export_path(value if isinstance(value, Path) else Path(_text(value, label)))
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    return path


def _water_global_resource_path(
    path_index: Mapping[bytes, tuple[str, ...]], map_id: str
) -> str | None:
    """Find this map's unique Global_Water_Config asset path by exact scene scope."""
    validate_scene_id(map_id)
    scene_prefix = f"assets/beyond/scenes/lunascenes/{map_id.capitalize()}/releasedata/".casefold()
    suffix = "/waterdata/global_water_config.asset"
    matches: dict[str, str] = {}
    for paths in path_index.values():
        for path in paths:
            normalized = path.replace("\\", "/").casefold()
            if normalized.startswith(scene_prefix) and normalized.endswith(suffix):
                matches.setdefault(normalized, path)
    if len(matches) > 1:
        raise ValueError(
            f"WATER_GLOBAL_PATH_AMBIGUOUS map_id={map_id} paths={sorted(matches.values())}"
        )
    return next(iter(matches.values()), None)


def _finite_vector(value: object, label: str, length: int) -> list[float]:
    numbers = _list(value, label)
    if len(numbers) != length or any(
        type(number) not in (int, float) or not math.isfinite(float(number))
        for number in numbers
    ):
        raise ValueError(f"{label} must contain {length} finite numbers")
    return [float(number) for number in numbers]


def _runtime_path(value: object, label: str, config_parent: Path) -> Path:
    raw = value if isinstance(value, Path) else Path(_text(value, label))
    return (raw if raw.is_absolute() else config_parent / raw).expanduser().resolve()


def _runtime_file(value: object, label: str, config_parent: Path) -> Path:
    return _file(_runtime_path(value, label, config_parent), label)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _bounds(value: object) -> Bounds:
    source = _object(value, "canonical bounds")
    result: Bounds = cast(Bounds, {})
    for name in ("xmin", "xmax", "zmin", "zmax"):
        number = source.get(name)
        if type(number) not in (int, float) or not math.isfinite(float(number)):
            raise ValueError(f"canonical bounds.{name} must be finite")
        result[name] = float(number)
    if result["xmin"] >= result["xmax"] or result["zmin"] >= result["zmax"]:
        raise ValueError("canonical bounds must be increasing and half-open")
    return result


def _terrain_surface_roots(values: object, config_parent: Path) -> list[Path]:
    roots: list[Path] = []
    for value in _list(values, "terrainSurfaces"):
        surface = _text(value, "terrain surface")
        root = (_runtime_file(surface, "terrain_surface/terrain_surface.json", config_parent).parent
                if surface.casefold().endswith("terrain_surface.json")
                else _runtime_path(surface, "terrain surface", config_parent))
        _file(root / "terrain_surface.json", "terrain surface manifest")
        roots.append(root)
    return roots


def _asset(value: object, label: str) -> AssetRef:
    source = _object(value, label)
    name = _text(source.get("Name"), f"{label}.Name")
    cab = _text(source.get("Source"), f"{label}.Source")
    path_id = source.get("PathId")
    if type(path_id) is not int:
        raise ValueError(f"{label}.PathId must be integer")
    return {"Source": cab, "PathId": path_id, "Name": name}


def _manifest_index(path: Path) -> tuple[
    Mapping[str, frozenset[tuple[str, int]]],
    Mapping[tuple[str, int], tuple[AssetRef, Path]],
]:
    resolved = resolve_export_path(path)
    containers, candidates = cached_scene_source_value(
        _MANIFEST_INDEX_CACHE, (resolved,), (),
        lambda: _build_manifest_index(resolved),
    )
    exports: dict[tuple[str, int], tuple[AssetRef, Path]] = {}
    resolved_files = resolve_existing_export_files(tuple(candidate
        for values in candidates.values() for _, candidate in values))
    for key, values in candidates.items():
        existing: list[tuple[AssetRef, Path]] = []
        for asset, candidate in values:
            resolved_candidate = resolved_files.get(str(candidate))
            if resolved_candidate is not None:
                existing.append((asset, resolved_candidate))
        if existing:
            paths = {value[1] for value in existing}
            if len(paths) > 1:
                raise ValueError(f"Mesh AssetRef has multiple export files: {key}")
            exports[key] = existing[-1]
    return containers, exports


def _build_manifest_index(path: Path) -> tuple[
    dict[str, set[tuple[str, int]]],
    dict[tuple[str, int], list[tuple[AssetRef, Path]]],
]:
    data = _object(read_scene_manifest(path), str(path))
    if data.get("Format") != "EndfieldSceneProbe/1":
        raise ValueError(f"Unsupported SceneProbe manifest: {path}")
    containers: dict[str, set[tuple[str, int]]] = defaultdict(set)
    exports: dict[tuple[str, int], list[tuple[AssetRef, Path]]] = defaultdict(list)
    for value in _list(data.get("Containers"), "SceneProbe.Containers"):
        row = _object(value, "SceneProbe container")
        if row.get("Asset") is not None and _object(row["Asset"], "container Asset").get("Type") in ("Mesh", "Material"):
            path_value = _text(row.get("Container"), "SceneProbe container path")
            containers[path_value.replace("\\", "/").casefold()].add(reference_key(_object(row["Asset"], "container Asset")))
    for value in _list(data.get("MeshExports"), "SceneProbe.MeshExports"):
        row = _object(value, "SceneProbe mesh export")
        if row.get("File") is None:
            continue
        asset = _asset(row.get("Asset"), "mesh Asset")
        file = path.parent / _text(row["File"], "mesh export File").replace("\\", "/")
        key = reference_key(asset)
        exports[key].append((asset, file))
    return containers, exports


def _merge_mesh_exports(
    existing: dict[tuple[str, int], tuple[AssetRef, Path]],
    incoming: Mapping[tuple[str, int], tuple[AssetRef, Path]],
) -> dict[tuple[str, int], tuple[AssetRef, Path]]:
    """Keep the first exact export and reject conflicting content for shared AssetRefs."""
    merged = dict(existing)
    for key, candidate in incoming.items():
        prior = merged.get(key)
        if prior is None:
            merged[key] = candidate
            continue
        if prior[1] == candidate[1]:
            continue
        with prior[1].open("rb") as stream:
            prior_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        with candidate[1].open("rb") as stream:
            candidate_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        if prior_hash != candidate_hash:
            raise ValueError(f"Shared mesh AssetRef has conflicting OBJ content: reference={key}, "
                             f"first={prior[1]}, second={candidate[1]}")
    return merged


def _primary_mesh(pairs: list[RendererPair]) -> str:
    return primary_visible_mesh(tuple((row["mesh_path"], RuntimeDraw(row["submesh"], row["flags"])) for row in pairs))


def _decal_materials(
    decoded: list[dict[str, object]],
    containers: dict[str, set[tuple[str, int]]],
    catalog: Mapping[tuple[str, int], object],
    textures: Mapping[tuple[str, int], tuple[Path, str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[Pending]]:
    ready: list[dict[str, object]] = []
    selected: dict[str, dict[str, object]] = {}
    pending: list[Pending] = []
    required_floats = {"_BaseTexChannel", "_DecalOpacity", "_BlendAlbedo", "_UseSampleTex1",
                       "_AffectRoughness", "_RoughnessMin", "_RoughnessMax", "_AffectNormal",
                       "_NormalScale", "_DrawOrder", "_SampleTex1UVSet"}
    for row in decoded:
        entity_id = cast(int, row["entity_id"])
        base = cast(str, row["entity_base"])
        container = cast(str, row["material_path"]).replace("\\", "/").casefold()
        refs = containers.get(container, set())
        try:
            if len(refs) != 1:
                raise ValueError(f"decal material container has {len(refs)} AssetRefs: {container}")
            ref = next(iter(refs))
            if ref not in catalog:
                raise ValueError(f"decal material record is missing: {container}:{ref}")
            record = cast(dict[str, object], catalog[ref])
            floats = _object(record.get("Floats"), "decal Floats")
            if required_floats - floats.keys():
                raise ValueError(f"decal shader inputs missing: {sorted(required_floats - floats.keys())}")
            needs_sample = (floats["_BaseTexChannel"] == 0 and floats["_UseSampleTex1"] > 0 and
                            (floats["_AffectRoughness"] > 0 or floats["_AffectNormal"] > 0))
            texture_slots = []
            for value in _list(record.get("Textures"), "decal Textures"):
                slot = _object(value, "decal texture slot")
                prop = slot.get("Property")
                if prop not in ("_BaseColorMap", "_SampleTex1"):
                    continue
                texture = slot.get("Texture")
                if texture is None or reference_key(_object(texture, "decal Texture")) not in textures:
                    if prop == "_SampleTex1" and not needs_sample:
                        continue
                    raise ValueError(f"decal texture export missing: material={container}, property={prop}")
                folder, filename = textures[reference_key(_object(texture, "decal Texture"))]
                texture_slots.append({"Property": prop, "File": str(folder / filename),
                                      "Scale": slot.get("Scale"), "Offset": slot.get("Offset")})
            if not any(slot["Property"] == "_BaseColorMap" for slot in texture_slots):
                raise ValueError(f"decal base texture is missing: {container}")
            if needs_sample and not any(slot["Property"] == "_SampleTex1" for slot in texture_slots):
                raise ValueError(f"decal sample texture is missing: {container}")
            selected[container] = {"Container": container, "Record": record, "TextureFiles": texture_slots}
            ready.append(row)
        except ValueError as error:
            pending.append({"entity_id": entity_id, "entity_base": base, "component": "projected_decal",
                            "reason": "decal_material_incomplete", "detail": str(error)})
    return ready, list(selected.values()), pending


def prepare_scene_export_plan(
    runtime_config: Path, canonical: Mapping[str, object], output: Path,
    check_cancelled: Callable[[], None] | None,
) -> Path:
    """Build exact rows from current ECS and available first-run SceneProbe exports."""
    from dataclasses import asdict, replace

    from extract_scene_renderer_materials import component_columns, decode_draws, decode_pairs, serialized_draw_parameters
    from extractor_core.projected_decals import (
        build_path_id_index, decode_decal, read_chunk, rect_decal_records, streaming_records,
    )
    from extractor_core.resources import load_group
    from extractor_core.instances import iter_entities
    from prepare_scene_geometry_repairs import obj_submesh_count

    runtime_path = _file(runtime_config, "runtime config")
    runtime = _object(json.loads(runtime_path.read_text(encoding="utf-8-sig")), "runtime config")
    if runtime.get("format") != "EndfieldRuntimeConfig/1" or canonical.get("format") != "EndfieldCanonicalExportJob/2":
        raise ValueError("Portable plan requires current runtime config and canonical export job")
    map_id = _text(canonical.get("map_id"), "canonical map_id")
    validate_scene_id(map_id)
    water_lighting = (read_water_transport_lighting(canonical["water_transport_lighting"])
                      if "water_transport_lighting" in canonical else None)
    if water_lighting is not None:
        if water_lighting.map_id != map_id:
            raise ValueError(f"Water transport map identity differs: plan={map_id}, lighting={water_lighting.map_id}")
        if canonical.get("export_mode") != "blend":
            raise ValueError("water_transport_lighting requires Blender scene export")
    groups = {_text(value, "export group") for value in _list(canonical.get("export_groups"), "export_groups")}
    layers = _object(canonical.get("layers"), "canonical layers")
    scene_color_snapshot = (read_scene_color_snapshot(canonical["scene_color_snapshot"])
                            if "scene_color_snapshot" in canonical else None)
    if scene_color_snapshot is not None and (
            canonical.get("export_mode") != "blend" or layers.get("effects") is not True
            or not groups.intersection({"effects", "all"})):
        raise ValueError("scene_color_snapshot requires Blender export with the effects layer and effects group")
    water_selected = layers.get("water") is True and bool(groups & {"water", "all"})
    lights_selected = layers.get("lights") is True and bool(groups & {"lighting", "all"})
    if water_lighting is not None and (not water_selected or not lights_selected):
        raise ValueError("water_transport_lighting requires selected water and lighting layers/groups")
    bounds = _bounds(canonical.get("bounds"))
    fog_depth = read_fog_depth_options(canonical["fog_depth"]) if "fog_depth" in canonical else None
    selection = _bounds(canonical.get("selection_bounds", bounds))
    if not (selection["xmin"] <= bounds["xmin"] < bounds["xmax"] <= selection["xmax"]
            and selection["zmin"] <= bounds["zmin"] < bounds["zmax"] <= selection["zmax"]):
        raise ValueError("Scene batch bounds must lie inside the canonical selection")
    paths = _object(runtime.get("paths"), "runtime paths")
    game_root = _runtime_path(paths.get("game_root"), "runtime game_root", runtime_path.parent)
    cache_root = _runtime_path(paths.get("cache_root"), "runtime cache_root", runtime_path.parent)
    if _inside(cache_root, game_root):
        raise ValueError(f"Runtime cache root must be outside the game installation: {cache_root}")
    configure_scene_manifest_cache(cache_root)
    export_root = _runtime_path(paths.get("export_root"), "runtime export_root", runtime_path.parent)
    vfs = game_root / "Endfield_Data" / "StreamingAssets" / "VFS"
    if not vfs.is_dir():
        raise FileNotFoundError(f"Configured game VFS is missing: {vfs}")
    selected_source = canonical.get("source")
    if selected_source is not None:
        selected_root = _object(selected_source, "canonical source").get("game_root")
        if selected_root is not None and _runtime_path(selected_root, "canonical game_root", runtime_path.parent) != game_root:
            raise ValueError("Canonical game root differs from first-run runtime config")
    target = output.expanduser().resolve()
    canonical_output = _object(canonical.get("output"), "canonical output")
    selected_export_root = _runtime_path(canonical_output.get("root"), "canonical output.root", runtime_path.parent)
    if target.exists() or not (_inside(target, cache_root) or _inside(target, export_root)
                               or _inside(target, selected_export_root)) or _inside(target, game_root):
        raise ValueError(f"Plan output must be new, inside selected cache/export root, and outside game: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    extraction = _object(runtime.get("extraction"), "runtime extraction")
    maps = _object(extraction.get("maps"), "runtime extraction.maps")
    map_paths = _object(maps.get(map_id), f"runtime extraction.maps.{map_id}")
    instance_db = _runtime_file(map_paths.get("instances"), "first-run instances", runtime_path.parent)
    _runtime_file(map_paths.get("assets"), "first-run asset index", runtime_path.parent)
    _runtime_file(extraction.get("resourceDatabase"), "first-run resource index", runtime_path.parent)
    _runtime_file(extraction.get("bundleScanIndex"), "first-run bundle scan index", runtime_path.parent)
    state_path = runtime_path.parent / "first_run_state.json"
    state = _object(json.loads(_file(state_path, "first-run state").read_text(encoding="utf-8-sig")), "first-run state")
    steps = _object(state.get("steps"), "first-run steps")
    bootstrap = _object(_object(steps.get("bootstrap_metadata"), "bootstrap step").get("outputs"), "bootstrap outputs")
    path_hash = _runtime_file(_object(bootstrap.get("main_paths"), "bootstrap main_paths").get("path"),
                              "StringPathHash", runtime_path.parent)
    manifest_paths: list[Path] = []
    for candidate_map in (map_id, *(name for name in maps if name != map_id)):
        candidate = maps.get(candidate_map)
        if candidate is None:
            continue
        probe = _object(candidate, f"runtime map {candidate_map}").get("sceneProbe")
        if probe is not None and _object(probe, f"sceneProbe {candidate_map}").get("path") is not None:
            manifest_paths.append(_runtime_file(_object(probe, "sceneProbe")["path"],
                                                "SceneProbe manifest", runtime_path.parent))
    from endfield_scene_resources import supplemental_manifests

    manifest_paths.extend(supplemental_manifests(runtime_path, runtime))
    manifest_paths = list(dict.fromkeys(manifest_paths))
    containers: dict[str, set[tuple[str, int]]] = defaultdict(set)
    mesh_exports: dict[tuple[str, int], tuple[AssetRef, Path]] = {}
    for manifest_path in manifest_paths:
        found_containers, found_exports = _manifest_index(manifest_path)
        for name, refs in found_containers.items():
            containers[name].update(refs)
        mesh_exports = _merge_mesh_exports(mesh_exports, found_exports)
    material_sources = capture_material_catalog_sources(tuple(manifest_paths))
    catalog = load_planning_catalog(tuple(manifest_paths), cache_root)
    materials, textures = catalog.materials, catalog.textures
    uv_catalog = (load_mesh_uv_catalog(tuple(manifest_paths))
                  if catalog.requires_extra_uv else MeshUvCatalog({}, {}))
    required_uv: dict[str, set[int]] = defaultdict(set)
    terrain_selected = layers.get("terrain") is True and bool(groups & {"all", "terrain"})
    terrain_surface_values = map_paths.get("terrainSurfaces")
    terrain_surfaces = (_terrain_surface_roots(terrain_surface_values, runtime_path.parent)
                        if terrain_selected else [])
    water_depth_surfaces = (
        _terrain_surface_roots(terrain_surface_values, runtime_path.parent)
        if water_selected and terrain_surface_values is not None else []
    )
    water_depth_missing = water_selected and not water_depth_surfaces
    allowed_categories = selected_static_categories(layers, groups)
    effects_selected = layers.get("effects") is True and bool(groups & {"effects", "particle", "all"})
    include_decals = bool(groups & {"all", "terrain", "road", "unknown"}) and (
        layers.get("instances") is True or layers.get("terrain") is True or layers.get("roads") is True)
    with closing(sqlite3.connect(instance_db.as_uri() + "?mode=ro", uri=True)) as database:
        rows = database.execute(
            "SELECT entity_id,entity_base,matrix_type,matrix_col_major,source_path,group_index,entity_index,x,z "
            "FROM instances WHERE matrix_type=18 OR (x>=? AND x<? AND z>=? AND z<?)",
            (bounds["xmin"], bounds["xmax"], bounds["zmin"], bounds["zmax"]),
        ).fetchall()
    selected_static: list[StaticSpatialRow] = []
    selected_decals: list[tuple[int, str, bytes, str]] = []
    excluded_hlod = 0
    excluded_hlod_mesh = 0
    for entity_id, base, matrix_type, matrix_blob, source_path, group_index, entity_index, origin_x, origin_z in rows:
        if not isinstance(base, str) or not base:
            continue
        category, _ = classify_entity(base)
        if matrix_type == 18 and (category in allowed_categories or water_selected or effects_selected):
            if is_hlod_name(base):
                if bounds["xmin"] <= origin_x < bounds["xmax"] and bounds["zmin"] <= origin_z < bounds["zmax"]:
                    excluded_hlod += 1
            else:
                selected_static.append((entity_id, base, matrix_blob, source_path, group_index, entity_index, category))
        elif matrix_type == 27 and include_decals:
            selected_decals.append((entity_id, base, matrix_blob, source_path))
    entries = ({entry.file_name: entry for entry in load_group(vfs, "C3442D43").iter_files()}
               if selected_static or selected_decals or water_selected else {})
    path_index = build_path_id_index(path_hash) if selected_static or selected_decals or water_selected else {}
    by_source: dict[str, list[StaticSpatialRow]] = defaultdict(list)
    for row in selected_static:
        by_source[cast(str, row[3])].append(row)
    decoded_decals: list[dict[str, object]] = []
    by_decal_source: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in selected_decals:
        by_decal_source[cast(str, row[3])].append(row)
    source_hashes: dict[str, str] = {}
    ready: list[PlanInstance] = []
    static_role_exclusions: list[StaticRoleExclusion] = []
    submesh_counts: dict[Path, int] = {}
    surface_effect_instances: list[PlanInstance] = []
    visible_surface_candidates: set[int] = set()
    water_depth_instances: list[PlanInstance] = []
    water_depth_pending: list[Pending] = []
    visible_static: set[int] = set()
    bottom_candidates: set[int] = set()
    pending: list[Pending] = []
    required_resources: set[str] = set()
    if terrain_surfaces:
        from prepare_terrain_detail import terrain_detail_source_paths

        required_resources.update(terrain_detail_source_paths(map_id))
    if water_depth_missing:
        water_depth_pending.append({
            "entity_id": -1,
            "entity_base": "selection",
            "component": "water_depth",
            "reason": "terrain_height_source_missing",
            "detail": "No verified terrain height source; water depth must be fully supported by opaque source assets",
        })
    selected_static_count = 0
    footprint_extras = 0
    effect_instances: list[EffectInstance] = []
    effect_physics: EffectPhysicsPlan | None = None
    selected_effects = 0
    direct_effect_renderers: list[DirectEffectRow] = []
    direct_effect_pending: list[DirectEffectPending] = []
    required_effect_mesh_paths: set[str] = set()
    selected_direct_effects = 0
    from extractor_core.dynamic_renderers import read_dynamic_scene_renderers_for_bounds

    def effect_cancelled() -> None:
        if check_cancelled is not None:
            check_cancelled()

    direct_inventory = (read_dynamic_scene_renderers_for_bounds(
                            vfs, path_hash, map_id, tuple(bounds[name] for name in ("xmin", "xmax", "zmin", "zmax")), effect_cancelled)
                        if effects_selected or allowed_categories else None)
    if effects_selected:
        from extractor_core.effects import read_dynamic_scene_effects_for_scene

        inventory = read_dynamic_scene_effects_for_scene(vfs, path_hash, map_id, effect_cancelled)
        effect_plan = build_effect_plan(inventory, bounds, tuple(manifest_paths), materials, textures)
        effect_instances.extend(effect_plan.rows)
        pending.extend(cast(list[Pending], list(effect_plan.pending)))
        required_resources.update(effect_plan.required_resource_paths)
        selected_effects = effect_plan.selected_count
        if direct_inventory is None:
            raise ValueError("Selected effects require the Dynamic ECS source inventory")
        direct_plan = build_direct_effect_plan(
            direct_inventory, bounds, tuple(manifest_paths), materials,
            scene_color_snapshot=scene_color_snapshot,
        )
        direct_effect_renderers.extend(direct_plan.rows)
        direct_effect_pending.extend(direct_plan.pending)
        required_resources.update(direct_plan.required_resource_paths)
        required_effect_mesh_paths.update(direct_plan.required_effect_mesh_paths)
        selected_direct_effects = direct_plan.selected_count
        if effect_instances:
            from extractor_core.physics import read_physics_settings

            physics = read_physics_settings(game_root / "Endfield_Data" / "globalgamemanagers")
            effect_physics = {"source": physics.source, "source_sha256": physics.source_sha256,
                              "path_id": physics.path_id, "gravity": list(physics.gravity)}
    light_instances: list[LightPlanRow] = []
    selected_lights = 0
    water_transport_dependency_lights = 0
    light_inventory = None
    if lights_selected:
        from extractor_core.lights import read_native_lights_cached
        light_inventory = read_native_lights_cached(vfs, path_hash, map_id,
                                                    cache_root / "native_lights", check_cancelled)
        light_plan = build_light_plan(light_inventory, bounds, selection)
        light_instances.extend(light_plan.rows)
        selected_lights = light_plan.selected_count
        required_resources.update(light_plan.required_resource_paths)
        for source, digest in light_plan.source_sha256:
            previous = source_hashes.get(source)
            if previous is not None and previous != digest:
                raise ValueError(f"Light source SHA-256 conflicts with scene source: {source}")
            source_hashes[source] = digest
    water_dependency_plan = None
    if water_lighting is not None:
        if light_inventory is None:
            raise ValueError("Water transport light inventory was not loaded for the selected lighting layer")
        water_dependency_plan = build_water_transport_directional_dependency(
            light_inventory, water_lighting.map_id, water_lighting.entity_id)
    water_geometry_rows: list[WaterGeometryPlanRow] = []
    water_material_rows: list[WaterMaterialPlanRow] = []
    if water_selected:
        from endfield_scene_water_plan import build_water_geometry_plan

        global_water_path = _water_global_resource_path(path_index, map_id)
        if global_water_path is None:
            scene_path = f"Assets/Beyond/Scenes/LunaScenes/{map_id.capitalize()}/ReleaseData"
            pending.append({
                "entity_id": -1,
                "entity_base": "selection",
                "component": "water_materials",
                "reason": "global_resource_path_missing",
                "detail": f"StringPathHash has no {scene_path}/WaterData/Global_Water_Config.asset path",
            })
        else:
            required_resources.add(global_water_path)
        geometry_plan = build_water_geometry_plan(
            vfs,
            entries,
            path_index,
            map_id,
            bounds,
            selection,
            tuple(manifest_paths),
            check_cancelled,
        )
        required_resources.update(geometry_plan.required_resource_paths)
        for source, digest in geometry_plan.source_sha256:
            previous = source_hashes.get(source)
            if previous is not None and previous != digest:
                raise ValueError(f"Water source SHA-256 conflicts with scene source: {source}")
            source_hashes[source] = digest
        for row in geometry_plan.rows:
            serialized = asdict(row)
            for field in ("matrix", "world_bounds", "ownership_anchor", "param0", "param1"):
                serialized[field] = list(serialized[field])
            water_geometry_rows.append(cast(WaterGeometryPlanRow, serialized))
        pending.extend({
            "entity_id": row.entity_id,
            "entity_base": row.name,
            "component": "water_geometry",
            "reason": row.reason,
            "detail": row.detail,
        } for row in geometry_plan.pending)
        if geometry_plan.rows:
            from endfield_scene_water_materials import build_water_material_plan

            material_geometry_rows = tuple(
                replace(
                    row,
                    world_bounds=(
                        max(row.world_bounds[0], bounds["xmin"]),
                        min(row.world_bounds[1], bounds["xmax"]),
                        row.world_bounds[2],
                        row.world_bounds[3],
                        max(row.world_bounds[4], bounds["zmin"]),
                        min(row.world_bounds[5], bounds["zmax"]),
                    ),
                )
                for row in geometry_plan.rows
            )
            material_plan = build_water_material_plan(
                tuple(manifest_paths), material_geometry_rows, map_id
            )
            required_resources.update(material_plan.required_resource_paths)
            names_by_entity = {row.entity_id: row.name for row in geometry_plan.rows}
            for row in material_plan.rows:
                serialized = asdict(row)
                flow = cast(dict[str, object], serialized["flow"])
                flow["flow_direction_vector"] = list(flow["flow_direction_vector"])
                serialized["sectors"] = list(serialized["sectors"])
                water_material_rows.append(cast(WaterMaterialPlanRow, serialized))
            pending.extend({
                "entity_id": row.entity_id,
                "entity_base": names_by_entity.get(row.entity_id, "selection"),
                "component": "water_materials",
                "reason": row.reason,
                "detail": row.detail,
            } for row in material_plan.pending)
    water_transport_field: dict[str, object] = {}
    if water_lighting is not None:
        unresolved_water = any(row.get("component") in {"water_geometry", "water_materials"}
                               for row in pending)
        has_water_scope = bool(water_geometry_rows or water_material_rows or unresolved_water)
        if not has_water_scope:
            water_transport_field["water_transport_lighting_status"] = "not_applicable"
        else:
            if water_dependency_plan is None:
                raise ValueError("Water transport directional light dependency is unavailable")
            matching_lights = [row for row in light_instances
                               if row["map_id"] == water_lighting.map_id
                               and row["entity_id"] == water_lighting.entity_id]
            if len(matching_lights) > 1:
                raise ValueError("Water transport directional light appears more than once in a scene batch")
            if not matching_lights:
                dependency = water_dependency_plan.rows[0]
                light_instances.append(dependency)
                water_transport_dependency_lights = 1
                for source, digest in water_dependency_plan.source_sha256:
                    previous = source_hashes.get(source)
                    if previous is not None and previous != digest:
                        raise ValueError(f"Water transport light source SHA-256 conflicts with scene source: {source}")
                    source_hashes[source] = digest
            water_transport_field["water_transport_lighting"] = serialize_water_transport_lighting(water_lighting)
    spatial_selection = _cached_spatial_selection(
        instance_db, cache_root, vfs, entries,
        {source: tuple(rows) for source, rows in by_source.items()},
        frozenset(by_decal_source), bounds, selection, groups, layers,
        allowed_categories, water_selected, effects_selected, include_decals,
        water_geometry_rows, read_chunk, component_columns,
        cast(Callable[[bytes], Iterable[Mapping[str, object]]], iter_entities), check_cancelled,
    )
    visible_static = set(spatial_selection.visible_static_ids)
    bottom_candidates = set(spatial_selection.bottom_candidate_ids)
    visible_surface_candidates = set(spatial_selection.surface_candidate_ids)
    selected_static_count = spatial_selection.selected_static_count
    footprint_extras = spatial_selection.footprint_extras
    cached_sources = {source.source: source for source in spatial_selection.sources}
    for source in sorted(set(by_source) | set(by_decal_source)):
        if check_cancelled is not None:
            check_cancelled()
        cached_source = cached_sources.get(source)
        if cached_source is None:
            continue
        chunk = cached_source.chunk
        selected_rows = sorted(cached_source.selected_rows, key=lambda row: row[0])
        source_hashes[source] = cached_source.source_sha256
        columns = cached_source.renderer_columns
        for entity_id, base, matrix_blob, _, group_index, entity_index, category in selected_rows:
            if check_cancelled is not None:
                check_cancelled()
            try:
                renderer = columns.get(cast(int, entity_id), {})
                pairs: list[RendererPair] = []
                source_pairs: list[StaticSourceDraw] = []
                shadow_draws: list[StaticSourceDraw] = []
                for kind, references in sorted(renderer.items()):
                    if not 68 <= kind <= 74:
                        continue
                    if kind - 60 not in renderer:
                        raise ValueError(f"Renderer component {kind} has no matching draw component")
                    decoded = decode_pairs(references, path_index)
                    draws = decode_draws(renderer[kind - 60], references)
                    for index, ((mesh_path, material_path), draw) in enumerate(zip(decoded, draws, strict=True)):
                        pairs.append({"material_id": references[8 + 40 * index:16 + 40 * index].hex(),
                                      "material_path": material_path, "mesh_id": references[16 + 40 * index:24 + 40 * index].hex(),
                                      "mesh_path": mesh_path, "submesh": draw.submesh, "flags": draw.flags})
                        source_pairs.append({**pairs[-1], "reference_component": kind, "source_draw_index": index,
                                             "lod_max_squared": draw.lod_max_squared, "lod_min_squared": draw.lod_min_squared})
                        if draw.flags & 1024:
                            shadow_draws.append(source_pairs[-1])
                if not pairs:
                    raise ValueError("No current Renderer mesh/material pairs")
                if len(shadow_draws) == len(pairs):
                    static_role_exclusions.append({
                        "entity_id": entity_id, "entity_base": base, "source_path": source,
                        "source_sha256": cached_source.source_sha256,
                        "group_index": group_index, "entity_index": entity_index,
                        "role": "shadow_only", "source_draws": shadow_draws,
                    })
                    continue
                mesh_paths = highest_detail_visible_meshes(tuple((pair["mesh_path"], RuntimeLodDraw(
                    pair["submesh"], pair["flags"], pair["lod_max_squared"], pair["lod_min_squared"])) for pair in source_pairs))
                if any(is_hlod_name(path) for path in mesh_paths):
                    excluded_hlod += 1
                    excluded_hlod_mesh += int(entity_id in visible_static)
                    continue
                selected_paths_by_mesh: dict[str, set[str]] = {}
                for mesh_path in mesh_paths:
                    selected_pair_paths: set[str] = set()
                    for pair in pairs:
                        if pair["mesh_path"] != mesh_path or pair["flags"] & 1024:
                            continue
                        refs = containers.get(pair["material_path"], set())
                        record = materials.get(next(iter(refs))) if len(refs) == 1 else None
                        if (record is None or (record["Shader"].get("Name") == "HGRP/FogSimple"
                                and entity_id in visible_surface_candidates)
                                or (record["Shader"].get("Name") != "HGRP/FogSimple"
                                    and (entity_id in visible_static or entity_id in bottom_candidates))):
                            selected_pair_paths.add(pair["material_path"])
                    selected_paths_by_mesh[mesh_path] = selected_pair_paths
                    if selected_pair_paths:
                        required_resources.add(mesh_path)
                        required_resources.update(selected_pair_paths)
                for mesh_path, paths in selected_paths_by_mesh.items():
                    for path in paths:
                        refs = containers.get(path, set())
                        record = materials.get(next(iter(refs))) if len(refs) == 1 else None
                        if record is not None and record["Shader"].get("Name") != "HGRP/FogSimple":
                            channels = required_uv_channels(record)
                            if channels:
                                required_uv[mesh_path].update(channels)
                entity_ready: list[PlanInstance] = []
                entity_bottom: list[PlanInstance] = []
                entity_surfaces: list[PlanInstance] = []
                entity_role_pending: list[StaticSubmeshPending] = []
                entity_hlod = False
                for mesh_path in mesh_paths:
                    if not selected_paths_by_mesh[mesh_path]:
                        continue
                    stem = mesh_path.split("##", 1)[0]
                    candidates = [mesh_exports[key] for key in containers.get(stem, set()) if key in mesh_exports
                                  and mesh_exports[key][0]["Name"].casefold() == asset_name(mesh_path)]
                    if len({(reference_key(asset), file) for asset, file in candidates}) != 1:
                        raise ValueError(f"Primary mesh lacks a unique exact SceneProbe Container/OBJ: {mesh_path}")
                    mesh_asset, mesh_file = candidates[0]
                    if is_hlod_name(mesh_asset["Name"]):
                        excluded_hlod += 1
                        excluded_hlod_mesh += int(entity_id in visible_static)
                        entity_hlod = True
                        break
                    active = [pair for pair in pairs if pair["mesh_path"] == mesh_path]
                    active_sources = [pair for pair in source_pairs if pair["mesh_path"] == mesh_path]
                    if mesh_file not in submesh_counts:
                        submesh_counts[mesh_file] = obj_submesh_count(mesh_file)
                    count = submesh_counts[mesh_file]
                    passes = runtime_submesh_passes(count, tuple(pair["material_id"] for pair in active),
                                                    tuple(RuntimeDraw(pair["submesh"], pair["flags"]) for pair in active))
                    slot_indices = [indices[0] if indices else len(active) for indices in passes.indices]
                    material_refs: list[AssetRef | None] = []
                    material_passes: list[list[AssetRef]] = []
                    regular_submeshes: list[int] = []
                    fog_submeshes: list[int] = []
                    for submesh, slot_index in enumerate(slot_indices):
                        if slot_index == len(active):
                            material_refs.append(None)
                            material_passes.append([])
                            continue
                        pair = active[slot_index]
                        refs = containers.get(pair["material_path"], set())
                        if len(refs) != 1:
                            raise ValueError(f"Material container has {len(refs)} AssetRefs: {pair['material_path']}")
                        key = next(iter(refs))
                        if key not in materials:
                            raise ValueError(f"Material record missing: {pair['material_path']}:{key}")
                        record = materials[key]
                        material_refs.append({"Source": record["Source"], "PathId": record["PathId"], "Name": record["Name"]})
                        source_records = [record]
                        for pass_index in passes.indices[submesh][1:]:
                            path = active[pass_index]["material_path"]
                            pass_refs = containers.get(path, set())
                            if len(pass_refs) != 1 or next(iter(pass_refs)) not in materials:
                                raise ValueError(f"Ordered pass material lacks a unique exact record: {path}")
                            source_records.append(materials[next(iter(pass_refs))])
                        if len(source_records) > 1:
                            validate_ordered_surface_passes(tuple(source_records))
                        material_passes.append([{"Source": item["Source"], "PathId": item["PathId"], "Name": item["Name"]}
                                                for item in source_records])
                        fog = record["Shader"].get("Name") == "HGRP/FogSimple"
                        if fog:
                            if entity_id not in visible_surface_candidates:
                                continue
                            from endfield_scene_fog_contract import validate_fog_material

                            validate_fog_material(record, None)
                            fog_submeshes.append(submesh)
                            texture_slots = tuple(slot for slot in record["Textures"]
                                                  if slot["Property"] in ("_MainTex", "_NoiseTex"))
                        else:
                            if entity_id not in visible_static and entity_id not in bottom_candidates:
                                continue
                            unsupported_passes = [
                                (pass_index, item)
                                for pass_index, item in zip(passes.indices[submesh], source_records, strict=True)
                                if item["Name"].casefold().startswith(("m_fx_", "m_water", "m_shadow", "m_fog", "m_volume"))
                            ]
                            if unsupported_passes:
                                for pass_index, item in unsupported_passes:
                                    source_draw = active_sources[pass_index]
                                    entity_role_pending.append({
                                        "entity_id": cast(int, entity_id), "entity_base": cast(str, base),
                                        "component": "static_mesh_submesh", "reason": "source_surface_role_unverified",
                                        "detail": f"Material role is not validated for ordinary surface export: {item['Name']}",
                                        "source_path": source, "source_sha256": cached_source.source_sha256,
                                        "group_index": cast(int, group_index), "entity_index": cast(int, entity_index),
                                        "mesh_path": mesh_path, "submesh": submesh, "material_name": item["Name"],
                                        "material_path": source_draw["material_path"],
                                        "material_id": source_draw["material_id"], "mesh_id": source_draw["mesh_id"],
                                        "source_draw": source_draw,
                                    })
                                continue
                            regular_submeshes.append(submesh)
                            texture_slots = tuple(slot for item in source_records for slot in supported_slots(item))
                        for texture_slot in texture_slots:
                            texture = texture_slot["Texture"]
                            if texture is not None and reference_key(texture) not in textures:
                                raise ValueError(f"Required material texture missing: {record['Name']}:{texture_slot['Property']}")
                    channels = tuple(sorted({channel for submesh in regular_submeshes
                                             for reference in material_passes[submesh]
                                             for channel in required_uv_channels(materials[reference_key(reference)])}))
                    if channels:
                        required_uv[mesh_path].update(channels)
                    mesh_uv_streams = source_uv_streams(uv_catalog, mesh_asset, channels)
                    matrix = list(struct.unpack("<16f", cast(bytes, matrix_blob)))
                    if not all(math.isfinite(value) for value in matrix):
                        raise ValueError("Entity matrix has non-finite components")
                    source_row: PlanInstance = {"entity_id": cast(int, entity_id), "entity_base": cast(str, base), "category": cast(str, category),
                                  "source_path": source, "source_sha256": cached_source.source_sha256,
                                  "group_index": cast(int, group_index), "entity_index": cast(int, entity_index),
                                  "matrix": matrix, "mesh_path": mesh_path, "mesh_file": str(mesh_file),
                                  "mesh_asset": mesh_asset, "renderer_pairs": active,
                                  "submesh_slot_indices": slot_indices, "material_refs": material_refs,
                                  "undrawn_submeshes": list(passes.undrawn_submeshes),
                                  "source_uv_streams": mesh_uv_streams,
                                  "serialized_draw_parameters": serialized_draw_parameters(renderer[18], cast(bytes, matrix_blob))
                                  if 18 in renderer else None}
                    if len(mesh_paths) > 1:
                        mesh_ids = {pair["mesh_id"] for pair in active}
                        if len(mesh_ids) != 1:
                            raise ValueError(f"Static mesh part has conflicting native mesh IDs: entity_id={entity_id}, mesh={mesh_path}")
                        source_row.update({"mesh_part_key": f"{source}:entity:{entity_id}/mesh:{next(iter(mesh_ids))}",
                            "source_renderer_pairs": source_pairs,
                            "source_draw_indices": [index for index, pair in enumerate(pairs) if pair["mesh_path"] == mesh_path],
                            "selected_mesh_paths": list(mesh_paths), "lod_selection": "coauthored_highest_detail_source_interval"})
                    if any(len({active[index]["material_id"] for index in indices}) > 1 for indices in passes.indices):
                        source_row["submesh_material_passes"] = material_passes
                        source_row["submesh_pass_indices"] = [list(indices) for indices in passes.indices]
                    if (len(mesh_paths) == 1 and any(row["mesh_path"] == mesh_path for row in entity_role_pending)):
                        source_row["source_draw_evidence"] = source_pairs
                    if regular_submeshes:
                        destination = entity_ready if entity_id in visible_static else entity_bottom
                        destination.append({**source_row, "selected_submeshes": regular_submeshes})
                    if fog_submeshes:
                        entity_surfaces.append({**source_row, "selected_submeshes": fog_submeshes})
                if not entity_hlod:
                    if len(mesh_paths) > 1 and {row["mesh_path"] for row in (*entity_ready, *entity_bottom, *entity_surfaces)} != set(mesh_paths):
                        raise ValueError(f"Static co-authored mesh set is incomplete: entity_id={entity_id}, expected={mesh_paths}")
                    ready.extend(entity_ready)
                    water_depth_instances.extend(entity_bottom)
                    surface_effect_instances.extend(entity_surfaces)
                    pending_target = pending if entity_id in visible_static or entity_id in visible_surface_candidates else water_depth_pending
                    pending_target.extend(entity_role_pending)
            except (ValueError, NotImplementedError, AmbiguousSubmeshSlots) as error:
                failures = pending if entity_id in visible_static or entity_id in visible_surface_candidates else water_depth_pending
                failures.append({"entity_id": cast(int, entity_id), "entity_base": cast(str, base),
                                 "component": ("static_mesh" if entity_id in visible_static else
                                                "surface_effect" if entity_id in visible_surface_candidates else "water_depth_static"),
                                 "reason": "exact_source_incomplete", "detail": str(error)})
        if source in by_decal_source:
            partner = source.replace("InitChunkData_", "StreamingChunkData_")
            if partner not in entries:
                raise FileNotFoundError(f"Projected decal StreamingChunkData is missing: {partner}")
            stream = read_chunk(vfs, entries[partner])
            source_hashes[partner] = hashlib.sha256(stream).hexdigest()
            rects, records = rect_decal_records(chunk), streaming_records(stream)
            for entity_id, base, expected_matrix, _ in sorted(by_decal_source[source], key=lambda row: row[0]):
                if check_cancelled is not None:
                    check_cancelled()
                try:
                    raw = rects[cast(int, entity_id)]
                    if raw[:64] != expected_matrix:
                        raise ValueError("Projected decal ECS matrix differs from instance index")
                    decoded_decals.append(cast(dict[str, object], asdict(decode_decal(
                        cast(int, entity_id), cast(str, base), source, raw, stream,
                        records[cast(int, entity_id)], path_index))))
                except (KeyError, ValueError) as error:
                    pending.append({"entity_id": cast(int, entity_id), "entity_base": cast(str, base),
                                    "component": "projected_decal", "reason": "decal_decode_failed", "detail": str(error)})
    dynamic_rows: list[DynamicGeometryRow] = []
    dynamic_pending: list[DynamicGeometryPending] = []
    dynamic_selected = 0
    dynamic_duplicates = 0
    if direct_inventory is not None and allowed_categories:
        dynamic_plan = build_dynamic_geometry_plan(
            direct_inventory, bounds, selection, allowed_categories, containers, mesh_exports,
            materials, textures, tuple(ready), uv_catalog)
        dynamic_rows.extend(dynamic_plan.rows)
        dynamic_pending.extend(dynamic_plan.pending)
        required_resources.update(dynamic_plan.required_resource_paths)
        for source, digest in dynamic_plan.source_sha256:
            if source in source_hashes and source_hashes[source] != digest:
                raise ValueError(f"Dynamic source SHA-256 conflicts with scene source: {source}")
            source_hashes[source] = digest
        dynamic_selected = dynamic_plan.selected_count
        dynamic_duplicates = len(dynamic_plan.excluded_static_duplicates)
        for requirement in dynamic_plan.required_uv_streams:
            required_uv[requirement["mesh_path"]].update(requirement["channels"])
    required_resources.update(str(row["material_path"]).replace("\\", "/").casefold() for row in decoded_decals)
    prefab_rows: list[PrefabGeometryRow] = []
    prefab_pending: list[PrefabGeometryPending] = []
    prefab_placements: list[dict[str, object]] = []
    prefab_exclusions: list[dict[str, object]] = []
    prefab_paths: list[str] = []
    prefab_bounds_audit: Path | None = None
    prefab_bounds_payload: dict[str, object] | None = None
    if allowed_categories:
        from extractor_core.dynamic_models import read_dynamic_scene_models
        from endfield_scene_prefab_selection_cache import select_cached_prefab_geometry_inventory
        from endfield_scene_resources import describe_file, ensure_prefab_bounds_manifests

        all_models = read_dynamic_scene_models(vfs, path_hash, map_id, effect_cancelled)
        bounds_manifests = ensure_prefab_bounds_manifests(runtime_path,
            frozenset(record.prefab_path.replace("\\", "/").casefold() for record in all_models.records), effect_cancelled)
        model_selection = select_cached_prefab_geometry_inventory(all_models, bounds_manifests,
            tuple(bounds[name] for name in ("xmin", "xmax", "zmin", "zmax")),
            tuple(selection[name] for name in ("xmin", "xmax", "zmin", "zmax")), cache_root)
        model_inventory = model_selection.inventory
        prefab_plan = build_prefab_geometry_plan(model_inventory, tuple(manifest_paths), allowed_categories,
            containers, mesh_exports, materials, textures, uv_catalog, model_selection.evidence)
        prefab_rows.extend(prefab_plan.rows)
        prefab_pending.extend(prefab_plan.pending)
        prefab_pending.extend(cast(list[PrefabGeometryPending], list(model_selection.pending)))
        prefab_placements.extend(prefab_plan.placements)
        prefab_exclusions.extend(prefab_plan.exclusions)
        prefab_paths = sorted({record.prefab_path.replace("\\", "/").casefold() for record in model_inventory.records})
        required_resources.update(prefab_plan.required_resource_paths)
        for source, digest in prefab_plan.source_sha256:
            if source in source_hashes and source_hashes[source] != digest:
                raise ValueError(f"Prefab source SHA-256 conflicts with scene source: {source}")
            source_hashes[source] = digest
        for requirement in prefab_plan.required_uv_streams:
            required_uv[requirement["mesh_path"]].update(requirement["channels"])
        prefab_bounds_audit = target.with_name(target.stem + "_prefab_bounds.json")
        if prefab_bounds_audit.exists():
            raise FileExistsError(f"Prefab bounds audit already exists: {prefab_bounds_audit}")
        prefab_bounds_payload = {"format": "EndfieldPrefabBoundsSelection/1", "scene_id": map_id,
            "batch_bounds": bounds, "selection_bounds": selection, "source_candidates": len(all_models.records),
            "selected_placements": len(model_inventory.records), "outside_or_other_batch": model_selection.outside_count,
            "no_active_source_mesh": model_selection.empty_count, "bounds_unavailable": list(model_selection.pending),
            "source_files": [asdict(source) for source in all_models.sources],
            "metadata_manifests": [describe_file(path) for path in bounds_manifests],
            "selected": model_selection.evidence,
            "coverage_scope": "Transformed source mesh vertex AABBs or serialized skinned renderer localBounds; native LOD0 and platform shadow roles; runtime animation and deformation are not evaluated.",
            "ownership": "Existing clipped-footprint ownership within one selection; stable placement source_key deduplicates independent overlapping exports."}
    decal_rows, decal_material_rows, decal_pending = _decal_materials(decoded_decals, containers, materials, textures)
    pending.extend(decal_pending)
    decal_path = target.with_name(target.stem + "_decals.json")
    decal_material_path = target.with_name(target.stem + "_decal_materials.json")
    for path in (decal_path, decal_material_path):
        if path.exists():
            raise FileExistsError(f"Portable scene sidecar already exists: {path}")
    ready_static_entities = {row["entity_id"] for row in ready}
    static_pending_entities = {row["entity_id"] for row in pending
                               if row["component"] in ("static_mesh", "static_mesh_submesh")}
    partial_static_entities = ready_static_entities & static_pending_entities
    complete_static_entities = ready_static_entities - partial_static_entities
    result: SceneExportPlan = {
        "format": FORMAT, "map_id": map_id, "bounds": bounds, "category_filter": sorted(groups),
        "scene_manifests": [str(path) for path in manifest_paths],
        "terrain_surfaces": [str(path) for path in terrain_surfaces],
        "water_depth_surfaces": [str(path) for path in water_depth_surfaces],
        "water_depth_instances": water_depth_instances, "water_depth_pending": water_depth_pending,
        "terrain_details": [],
        "decal_manifest": str(decal_path), "decal_materials": str(decal_material_path),
        "instances": ready, "static_role_exclusions": static_role_exclusions,
        "surface_effect_instances": surface_effect_instances, "effect_instances": effect_instances, "effect_physics": effect_physics,
        "dynamic_geometry_instances": dynamic_rows, "dynamic_geometry_pending": dynamic_pending,
        "prefab_geometry_instances": prefab_rows, "prefab_geometry_pending": prefab_pending,
        "prefab_placements": prefab_placements, "prefab_geometry_exclusions": prefab_exclusions,
        "required_prefab_geometry_paths": prefab_paths,
        "direct_effect_renderers": direct_effect_renderers, "direct_effect_pending": direct_effect_pending,
        "required_effect_mesh_paths": sorted(required_effect_mesh_paths),
        "light_instances": light_instances,
        "water_geometry": water_geometry_rows,
        "water_materials": water_material_rows, "pending": pending, "source_sha256": source_hashes,
        **water_transport_field,
        **({"fog_depth": fog_depth} if fog_depth is not None else {}),
        **({"scene_color_snapshot": scene_color_snapshot} if scene_color_snapshot is not None else {}),
        "required_resource_paths": sorted(required_resources),
        "required_uv_streams": [{"mesh_path": path, "channels": sorted(channels)} for path, channels in sorted(required_uv.items())],
        "summary": {"selected_static": selected_static_count - excluded_hlod_mesh - sum(
                        row["entity_id"] in visible_static for row in static_role_exclusions),
                    "excluded_static_shadow_only": len(static_role_exclusions),
                    "footprint_overlap_extras": footprint_extras,
                    "excluded_hlod": excluded_hlod,
                    "ready_static": len(ready), "ready_static_entities": len(ready_static_entities),
                    "partial_static_entities": len(partial_static_entities),
                    "complete_static_entities": len(complete_static_entities),
                    "ready_surface_effects": len(surface_effect_instances),
                    "selected_dynamic_geometry": dynamic_selected, "ready_dynamic_geometry": len(dynamic_rows),
                    "ready_dynamic_geometry_records": len({row["source_key"] for row in dynamic_rows}),
                    "pending_dynamic_geometry": len(dynamic_pending), "excluded_dynamic_static_duplicates": dynamic_duplicates,
                    "selected_prefab_placements": len(prefab_placements), "ready_prefab_geometry": len(prefab_rows),
                    "pending_prefab_geometry": len(prefab_pending), "excluded_prefab_geometry": len(prefab_exclusions),
                    "selected_effects": selected_effects, "ready_effects": len(effect_instances),
                    "selected_direct_effects": selected_direct_effects, "ready_direct_effects": len(direct_effect_renderers),
                    "pending_direct_effects": len(direct_effect_pending),
                    "selected_lights": selected_lights, "ready_lights": len(light_instances),
                    "water_transport_dependency_lights": water_transport_dependency_lights,
                    "selected_decals": len(selected_decals), "ready_decals": len(decal_rows), "pending": len(pending)},
    }
    if prefab_bounds_audit is not None:
        result["prefab_bounds_audit"] = str(prefab_bounds_audit)
    result["material_catalog"] = build_material_catalog_handoff(result, materials, textures, material_sources)
    decal_payload = {"format": "EndfieldProjectedDecals/1", "instances_db": str(instance_db),
                     "path_hash_sha256": hashlib.sha256(path_hash.read_bytes()).hexdigest(),
                     "bounds": [bounds[name] for name in ("xmin", "xmax", "zmin", "zmax")],
                     "source_sha256": source_hashes, "records": decal_rows}
    material_payload = {"format": "EndfieldProjectedDecalMaterials/1", "Materials": decal_material_rows}
    created: list[Path] = []
    payloads: list[tuple[Path, object]] = [(decal_path, decal_payload), (decal_material_path, material_payload), (target, result)]
    if prefab_bounds_audit is not None:
        payloads.append((prefab_bounds_audit, prefab_bounds_payload))
    try:
        for path, payload in payloads:
            with path.open("x", encoding="utf-8") as handle:
                created.append(path)
                json.dump(payload, handle, indent=2)
        require_material_catalog_sources(material_sources)
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return target


def _validate_static_mesh_parts(rows: tuple[object, ...]) -> None:
    """Require every co-authored source part without fabricating entity identities."""
    groups: dict[int, list[dict[str, object]]] = defaultdict(list)
    legacy: set[int] = set()
    fields = ("material_id", "material_path", "mesh_id", "mesh_path", "submesh", "flags")
    for value in rows:
        row = _object(value, "static mesh part")
        entity = cast(int, row["entity_id"])
        proof_fields = ("mesh_part_key", "source_renderer_pairs", "source_draw_indices", "selected_mesh_paths", "lod_selection")
        if not any(field in row for field in proof_fields):
            legacy.add(entity)
            continue
        if not all(field in row for field in proof_fields):
            raise ValueError(f"Static mesh part requires complete source selection evidence: entity_id={entity}")
        if row["lod_selection"] != "coauthored_highest_detail_source_interval":
            raise ValueError(f"Static mesh part has unsupported LOD selection: entity_id={entity}")
        original = [_object(draw, "static original draw") for draw in _list(row["source_renderer_pairs"], "static source_renderer_pairs")]
        source_indices: set[tuple[int, int]] = set()
        for draw in original:
            for field in fields[:4]:
                _text(draw.get(field), f"static original draw.{field}")
            for field in ("submesh", "flags", "reference_component", "source_draw_index"):
                if type(draw.get(field)) is not int or draw[field] < 0:
                    raise ValueError(f"Invalid static original draw: entity_id={entity}, field={field}")
            component, index = cast(int, draw["reference_component"]), cast(int, draw["source_draw_index"])
            if not 68 <= component <= 74 or not 0 <= index < 1 << (component - 68) or (component, index) in source_indices:
                raise ValueError(f"Invalid or repeated static source draw index: entity_id={entity}, component={component}, index={index}")
            source_indices.add((component, index))
            for field in ("lod_max_squared", "lod_min_squared"):
                if type(draw.get(field)) not in (int, float) or not math.isfinite(draw[field]):
                    raise ValueError(f"Invalid static source LOD value: entity_id={entity}, field={field}")
        paths = highest_detail_visible_meshes(tuple((str(draw["mesh_path"]), RuntimeLodDraw(
            cast(int, draw["submesh"]), cast(int, draw["flags"]), cast(float, draw["lod_max_squared"]),
            cast(float, draw["lod_min_squared"]))) for draw in original))
        if len(paths) < 2 or row["selected_mesh_paths"] != list(paths) or row["mesh_path"] not in paths:
            raise ValueError(f"Static mesh part differs from the co-authored source mesh set: entity_id={entity}")
        indices = [index for index, draw in enumerate(original) if draw["mesh_path"] == row["mesh_path"]]
        if row["source_draw_indices"] != indices or row["renderer_pairs"] != [
            {field: original[index][field] for field in fields} for index in indices
        ]:
            raise ValueError(f"Static mesh part draws differ from source references: entity_id={entity}")
        mesh_ids = {str(original[index]["mesh_id"]) for index in indices}
        if len(mesh_ids) != 1 or row["mesh_part_key"] != f"{row['source_path']}:entity:{entity}/mesh:{next(iter(mesh_ids))}":
            raise ValueError(f"Static mesh part key differs from its native mesh identity: entity_id={entity}")
        groups[entity].append(row)
    shared = ("entity_base", "category", "source_path", "source_sha256", "group_index", "entity_index", "matrix",
              "serialized_draw_parameters", "source_renderer_pairs", "selected_mesh_paths", "lod_selection")
    for entity, parts in groups.items():
        first = parts[0]
        if entity in legacy or any(any(row.get(field) != first.get(field) for field in shared) for row in parts):
            raise ValueError(f"Static mesh parts disagree on their original entity evidence: entity_id={entity}")
        if {row["mesh_path"] for row in parts} != set(cast(list[str], first["selected_mesh_paths"])):
            raise ValueError(f"Static co-authored mesh part set is incomplete: entity_id={entity}")


def _validate_static_submesh_pending(
    pending_rows: tuple[object, ...], rows: tuple[object, ...], source_hashes: Mapping[str, object]
) -> None:
    """Bind unresolved ordinary-role slots to exact native draws and keep them unselected."""
    seen: set[tuple[int, str, int, int]] = set()
    prefixes = ("m_fx_", "m_water", "m_shadow", "m_fog", "m_volume")
    for value in pending_rows:
        pending = _object(value, "static submesh pending")
        if pending.get("component") != "static_mesh_submesh":
            continue
        entity_id = pending.get("entity_id")
        source_path = _text(pending.get("source_path"), "static submesh pending.source_path")
        source_sha256 = _text(pending.get("source_sha256"), "static submesh pending.source_sha256")
        mesh_path = _text(pending.get("mesh_path"), "static submesh pending.mesh_path")
        material_name = _text(pending.get("material_name"), "static submesh pending.material_name")
        _text(pending.get("material_path"), "static submesh pending.material_path")
        _text(pending.get("material_id"), "static submesh pending.material_id")
        _text(pending.get("mesh_id"), "static submesh pending.mesh_id")
        detail = _text(pending.get("detail"), "static submesh pending.detail")
        submesh = pending.get("submesh")
        group_index, entity_index = pending.get("group_index"), pending.get("entity_index")
        if (type(entity_id) is not int or type(group_index) is not int or group_index < 0
                or type(entity_index) is not int or entity_index < 0
                or type(submesh) is not int or not 0 <= submesh <= 255):
            raise ValueError(f"Invalid exact static submesh pending identity: entity_id={entity_id}")
        if source_sha256 != source_hashes.get(source_path):
            raise ValueError(f"Static submesh pending differs from source SHA-256: entity_id={entity_id}, source={source_path}")
        if pending.get("reason") != "source_surface_role_unverified":
            raise ValueError(f"Static submesh pending has an unsupported reason: entity_id={entity_id}")
        if not material_name.casefold().startswith(prefixes) or material_name not in detail:
            raise ValueError(f"Static submesh pending material has no unresolved-role marker: entity_id={entity_id}, material={material_name}")
        draw = _object(pending.get("source_draw"), "static submesh pending.source_draw")
        draw_fields = ("mesh_path", "material_path", "material_id", "mesh_id", "submesh", "flags")
        for name in ("mesh_path", "material_path", "material_id", "mesh_id"):
            _text(draw.get(name), f"static submesh source draw.{name}")
        flags = draw.get("flags")
        component, draw_index = draw.get("reference_component"), draw.get("source_draw_index")
        maximum, minimum = draw.get("lod_max_squared"), draw.get("lod_min_squared")
        if (type(draw.get("submesh")) is not int or draw["submesh"] != submesh
                or type(flags) is not int or not 0 <= flags <= 0xFFFFFFFF or flags & 1024
                or type(component) is not int or not 68 <= component <= 74
                or type(draw_index) is not int or not 0 <= draw_index < 1 << (component - 68)
                or type(maximum) not in (int, float) or type(minimum) not in (int, float)
                or not math.isfinite(maximum) or not math.isfinite(minimum) or maximum < minimum
                or draw.get("mesh_path") != mesh_path):
            raise ValueError(f"Static submesh pending does not identify a valid active source draw: entity_id={entity_id}, mesh={mesh_path}, submesh={submesh}")
        for name in ("material_id", "material_path", "mesh_id"):
            if draw.get(name) != pending.get(name):
                raise ValueError(f"Static submesh pending duplicated {name} differs: entity_id={entity_id}, mesh={mesh_path}")
        identity = (entity_id, source_path, component, draw_index)
        if identity in seen:
            raise ValueError(f"Static submesh source draw is repeated in pending: entity_id={entity_id}, component={component}, index={draw_index}")
        seen.add(identity)
        row_objects = [_object(row_value, "static submesh ready row") for row_value in rows]
        matches = [row for row in row_objects if row.get("entity_id") == entity_id
                   and row.get("entity_base") == pending.get("entity_base")
                   and row.get("source_path") == source_path and row.get("source_sha256") == source_sha256
                   and row.get("group_index") == group_index and row.get("entity_index") == entity_index
                   and row.get("mesh_path") == mesh_path]
        for row in matches:
            evidence = row.get("source_renderer_pairs", row.get("source_draw_evidence"))
            if not isinstance(evidence, list) or sum(_object(item, "static source draw evidence") == draw for item in evidence) != 1:
                raise ValueError(f"Static submesh pending differs from ready source draw evidence: entity_id={entity_id}, mesh={mesh_path}, submesh={submesh}")
            selected = _list(row.get("selected_submeshes"), "static ready selected_submeshes")
            if submesh in selected:
                raise ValueError(f"Static ready row selects a pending source submesh: entity_id={entity_id}, mesh={mesh_path}, submesh={submesh}")
            pairs = [_object(item, "static ready renderer pair") for item in _list(row.get("renderer_pairs"), "static ready renderer_pairs")]
            if sum(all(pair.get(name) == draw.get(name) for name in draw_fields) for pair in pairs) != 1:
                raise ValueError(f"Static submesh pending differs from ready renderer pair: entity_id={entity_id}, mesh={mesh_path}, submesh={submesh}")


def load_scene_export_plan(path: Path) -> SceneExportPlan:
    """Validate the versioned plan before a Blender worker uses its paths."""
    source = _object(json.loads(_file(path, "scene export plan").read_text(encoding="utf-8-sig")), "scene export plan")
    if source.get("format") != FORMAT:
        raise ValueError("Unsupported portable scene plan format")
    validate_scene_id(_text(source.get("map_id"), "scene map_id"))
    lighting: WaterTransportLighting | None = None
    if "water_transport_lighting" in source:
        lighting = read_water_transport_lighting(source["water_transport_lighting"])
    lighting_status = source.get("water_transport_lighting_status")
    if lighting_status is not None and lighting_status != "not_applicable":
        raise ValueError(f"Unsupported water transport lighting status: {lighting_status!r}")
    if lighting is not None and lighting_status is not None:
        raise ValueError("Water transport lighting cannot be both active and not_applicable")
    if "fog_depth" in source:
        source["fog_depth"] = read_fog_depth_options(source["fog_depth"])
    if "effect_flow_presentation" in source:
        source["effect_flow_presentation"] = read_flow_presentation(source["effect_flow_presentation"])
        selected_categories = _list(source.get("category_filter"), "scene export category_filter")
        if not any(category in ("all", "effects") for category in selected_categories):
            raise ValueError("effect_flow_presentation plan requires the effects export group")
    if "scene_color_snapshot" in source:
        source["scene_color_snapshot"] = read_scene_color_snapshot(source["scene_color_snapshot"])
        selected_categories = _list(source.get("category_filter"), "scene export category_filter")
        if not any(category in ("all", "effects") for category in selected_categories):
            raise ValueError("scene_color_snapshot plan requires the effects export group")
    source.setdefault("water_geometry", [])
    source.setdefault("water_materials", [])
    source.setdefault("water_depth_surfaces", [])
    source.setdefault("water_depth_instances", [])
    source.setdefault("water_depth_pending", [])
    source.setdefault("surface_effect_instances", [])
    source.setdefault("static_role_exclusions", [])
    source.setdefault("dynamic_geometry_instances", [])
    source.setdefault("dynamic_geometry_pending", [])
    for field in ("prefab_geometry_instances", "prefab_geometry_pending", "prefab_placements", "prefab_geometry_exclusions", "required_prefab_geometry_paths"):
        source.setdefault(field, [])
    source.setdefault("required_uv_streams", [])
    source.setdefault("effect_instances", [])
    source.setdefault("effect_physics", None)
    source.setdefault("direct_effect_renderers", [])
    source.setdefault("direct_effect_pending", [])
    source.setdefault("required_effect_mesh_paths", [])
    source.setdefault("light_instances", [])
    _bounds(source.get("bounds"))
    water_geometry_rows = _list(source["water_geometry"], "scene export water geometry")
    water_material_rows = _list(source["water_materials"], "scene export water materials")
    pending_rows = _list(source.get("pending", []), "scene export pending")
    water_pending = any(isinstance(value, dict)
                        and value.get("component") in {"water_geometry", "water_materials"}
                        for value in pending_rows)
    has_water_scope = bool(water_geometry_rows or water_material_rows or water_pending)
    if lighting is not None and lighting.map_id != source["map_id"]:
        raise ValueError(f"Water transport map identity differs: plan={source['map_id']}, lighting={lighting.map_id}")
    if lighting is not None and not has_water_scope:
        raise ValueError("Active water transport lighting requires water geometry or explicit water pending rows")
    if lighting_status == "not_applicable":
        if has_water_scope:
            raise ValueError("not_applicable water transport status conflicts with water geometry or pending rows")
    for name in ("scene_manifests", "terrain_surfaces", "water_depth_surfaces", "instances", "pending", "category_filter"):
        _list(source.get(name), f"scene export plan.{name}")
    details = _list(source.get("terrain_details", []), "scene export plan.terrain_details")
    for value in _list(source["scene_manifests"], "scene_manifests"):
        _file(value, "scene manifest")
    for value in _list(source["terrain_surfaces"], "terrain_surfaces"):
        _file(Path(_text(value, "terrain surface")) / "terrain_surface.json", "terrain surface manifest")
    for value in _list(source["water_depth_surfaces"], "water_depth_surfaces"):
        _file(Path(_text(value, "water depth surface")) / "terrain_surface.json", "water depth surface manifest")
    if details and not source["terrain_surfaces"]:
        raise ValueError("Terrain details require selected terrain surfaces")
    for value in details:
        from endfield_terrain_detail_contract import load_terrain_detail
        load_terrain_detail(_file(value, "terrain detail"))
    for value in _list(source["category_filter"], "category_filter"):
        _text(value, "category")
    _file(source.get("decal_manifest"), "decal_manifest")
    _file(source.get("decal_materials"), "decal_materials")
    hashes = _object(source.get("source_sha256"), "scene source_sha256")
    for name, digest in hashes.items():
        _text(name, "source path")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"Invalid source SHA-256: source={name}")
    excluded_ids: set[int] = set()
    static_exclusions = _list(source["static_role_exclusions"], "static role exclusions")
    for value in static_exclusions:
        row = _object(value, "static role exclusion")
        entity_id = row.get("entity_id")
        if type(entity_id) is not int or entity_id < 0 or entity_id in excluded_ids or row.get("role") != "shadow_only":
            raise ValueError(f"Invalid static role exclusion: entity_id={entity_id}")
        excluded_ids.add(entity_id)
        _text(row.get("entity_base"), "static excluded entity base")
        source_path = _text(row.get("source_path"), "static excluded source path")
        if row.get("source_sha256") != hashes.get(source_path) or source_path not in hashes:
            raise ValueError(f"Static role exclusion differs from source SHA-256: entity_id={entity_id}")
        for name in ("group_index", "entity_index"):
            if type(row.get(name)) is not int or row[name] < 0:
                raise ValueError(f"Invalid static role exclusion source index: entity_id={entity_id}, field={name}")
        draws = _list(row.get("source_draws"), "static excluded source draws")
        if not draws:
            raise ValueError(f"Static role exclusion requires source draws: entity_id={entity_id}")
        for value in draws:
            draw = _object(value, "static excluded source draw")
            flags, submesh = draw.get("flags"), draw.get("submesh")
            if (type(flags) is not int or not 0 <= flags <= 0xFFFFFFFF or not flags & 1024
                    or type(submesh) is not int or not 0 <= submesh <= 255):
                raise ValueError(f"Static role exclusion requires validated shadow-only draws: entity_id={entity_id}")
            for name in ("mesh_path", "material_path", "mesh_id", "material_id"):
                _text(draw.get(name), "static excluded " + name)
            component, index = draw.get("reference_component"), draw.get("source_draw_index")
            if type(component) is not int or not 68 <= component <= 74 or type(index) is not int or not 0 <= index < 1 << (component - 68):
                raise ValueError(f"Invalid static excluded draw index: entity_id={entity_id}")
            maximum, minimum = draw.get("lod_max_squared"), draw.get("lod_min_squared")
            if (type(maximum) not in (int, float) or type(minimum) not in (int, float)
                    or not math.isfinite(maximum) or not math.isfinite(minimum) or maximum < minimum):
                raise ValueError(f"Invalid static excluded source LOD interval: entity_id={entity_id}")
    seen: set[int | str] = set()
    validate_light_plan_rows(source["light_instances"], str(source["map_id"]), hashes)
    if lighting is not None:
        matches = [row for row in source["light_instances"]
                   if row["map_id"] == lighting.map_id and row["entity_id"] == lighting.entity_id]
        if len(matches) != 1 or matches[0]["light_type"] != 1:
            raise ValueError("Water transport Sun must identify exactly one directional light in this scene plan")
    instance_rows = (*_list(source["instances"], "scene export instances"),
                     *_list(source["water_depth_instances"], "scene export water depth instances"))
    surface_rows = _list(source["surface_effect_instances"], "scene export surface effects")
    surface_ids: set[int | str] = set()
    dynamic_rows = _list(source["dynamic_geometry_instances"], "scene dynamic geometry")
    dynamic_keys: set[int | str] = set()
    for ids, value in (*((seen, row) for row in instance_rows), *((surface_ids, row) for row in surface_rows),
                       *((dynamic_keys, row) for row in dynamic_rows)):
        row = _object(value, "scene export instance")
        dynamic = ids is dynamic_keys
        entity_id = row.get("source_key") if dynamic else row.get("entity_id")
        if dynamic:
            _text(entity_id, "dynamic source_key")
            if "entity_id" in row or "entity_index" in row:
                raise ValueError("Dynamic geometry must preserve source record identity without fabricated entity IDs")
            for name in ("chunk_id", "init_scene_index", "record_index", "common_instance_offset",
                         "renderer_reference_offset", "runtime_draw_offset"):
                if type(row.get(name)) is not int or cast(int, row[name]) < 0:
                    raise ValueError(f"Invalid dynamic source provenance: field={name}, source_key={entity_id}")
            references = _list(row.get("reference_keys"), "dynamic main reference_keys")
            if not references or any(not isinstance(key, str) or not key for key in references):
                raise ValueError(f"Dynamic geometry requires actual main reference keys: source_key={entity_id}")
            if entity_id != f"{row.get('source_path')}:{row['group_index']}:{row['record_index']}":
                raise ValueError(f"Dynamic source key differs from source group/record: source_key={entity_id}")
            if (row.get("activation_status") != "serialized_initial_not_runtime"
                    or row.get("lod_selection") != "authored_base_lod_reference_not_runtime_camera_selection"):
                raise ValueError(f"Dynamic geometry must disclose activation and reference LOD limits: source_key={entity_id}")
        elif type(entity_id) is not int:
            raise ValueError(f"Scene instance ID is invalid: {entity_id}")
        elif entity_id in excluded_ids:
            raise ValueError(f"Static role exclusion conflicts with a visible instance: entity_id={entity_id}")
        identity = row.get("mesh_part_key", entity_id)
        if "mesh_part_key" in row:
            _text(identity, "dynamic mesh_part_key" if dynamic else "static mesh_part_key")
        if identity in ids:
            raise ValueError(f"Scene instance identity is repeated: {entity_id}")
        ids.add(identity)
        entity_base = _text(row.get("entity_base"), "scene entity_base")
        _text(row.get("category"), "scene category")
        mesh_path = _text(row.get("mesh_path"), "scene mesh_path")
        mesh_asset = _asset(row.get("mesh_asset"), "scene mesh_asset")
        if any(is_hlod_name(name) for name in (entity_base, mesh_path, mesh_asset["Name"])):
            raise ValueError(f"Scene plan contains excluded HLOD instance: entity_id={entity_id}, "
                             f"entity_base={entity_base}, mesh_path={mesh_path}")
        source_path = _text(row.get("source_path"), "scene source_path")
        if row.get("source_sha256") != hashes.get(source_path):
            raise ValueError(f"Scene source hash mismatch: entity_id={entity_id}, source={source_path}")
        for name in (("group_index",) if dynamic else ("group_index", "entity_index")):
            if type(row.get(name)) is not int or cast(int, row[name]) < 0:
                raise ValueError(f"Scene {name} must be nonnegative integer: entity_id={entity_id}")
        _file(row.get("mesh_file"), "scene mesh_file")
        matrix = _list(row.get("matrix"), "scene matrix")
        if len(matrix) != 16 or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in matrix):
            raise ValueError(f"Scene instance has invalid 16-float matrix: entity_id={entity_id}")
        parameters = row.get("serialized_draw_parameters")
        for value in _list(row.get("source_uv_streams", []), "source UV streams"):
            uv = _object(value, "source UV stream")
            if uv.get("channel") not in (1, 2) or type(uv.get("channel")) is not int:
                raise ValueError(f"Invalid ordinary source UV channel: placement={entity_id}")
            if type(uv.get("vertex_count")) is not int or uv["vertex_count"] <= 0:
                raise ValueError(f"Invalid ordinary source UV vertex count: placement={entity_id}")
            _file(uv.get("source_obj_file"), "source UV exact OBJ")
            _file(uv.get("source_manifest"), "source UV manifest")
            if uv.get("components") == 0 and type(uv.get("components")) is int:
                if uv.get("status") != "absent_source_zero_reference" or uv.get("file") is not None:
                    raise ValueError(f"Missing source UV channel must disclose zero-reference status: placement={entity_id}")
            elif uv.get("components") == 2 and type(uv.get("components")) is int:
                if uv.get("status") != "source_stream" or _file(uv.get("file"), "source UV stream file").stat().st_size != uv["vertex_count"] * 8:
                    raise ValueError(f"Source UV file metadata differs: placement={entity_id}")
            else:
                raise ValueError(f"Unsupported source UV components: placement={entity_id}")
        if parameters is not None:
            parameters = _list(parameters, "serialized draw parameters")
            if len(parameters) != 20 or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in parameters):
                raise ValueError(f"Scene instance has invalid 20-float draw parameters: entity_id={entity_id}")
        refs = _list(row.get("material_refs"), "scene material_refs")
        slots = _list(row.get("submesh_slot_indices"), "scene submesh_slot_indices")
        if len(refs) != len(slots) or not refs:
            raise ValueError(f"Scene material/geometry slot count differs: entity_id={entity_id}")
        pairs = _list(row.get("renderer_pairs"), "scene renderer_pairs")
        for pair_value in pairs:
            pair = _object(pair_value, "scene renderer pair")
            for name in ("material_id", "material_path", "mesh_id", "mesh_path"):
                _text(pair.get(name), f"scene renderer pair.{name}")
            for name in ("submesh", "flags"):
                if type(pair.get(name)) is not int or cast(int, pair[name]) < 0:
                    raise ValueError(f"Invalid renderer {name}: entity_id={entity_id}")
            if pair["mesh_path"] != row["mesh_path"]:
                raise ValueError(f"Renderer mesh differs from selected primary mesh: entity_id={entity_id}")
        if not pairs:
            raise ValueError(f"Scene renderer pairs are empty: entity_id={entity_id}")
        if dynamic:
            original = _list(row.get("source_renderer_pairs"), "dynamic source_renderer_pairs")
            indices = _list(row.get("source_draw_indices"), "dynamic source_draw_indices")
            if len(indices) != len(pairs) or not original:
                raise ValueError(f"Dynamic selected draw indices differ from source evidence: source_key={entity_id}")
            original_pairs = [_object(value, "dynamic original draw") for value in original]
            for draw in original_pairs:
                for name in ("submesh", "flags"):
                    if type(draw.get(name)) is not int or draw[name] < 0:
                        raise ValueError(f"Invalid dynamic original draw: source_key={entity_id}, field={name}")
                for name in ("lod_max_squared", "lod_min_squared"):
                    if type(draw.get(name)) not in (int, float) or not math.isfinite(draw[name]):
                        raise ValueError(f"Invalid dynamic source LOD value: source_key={entity_id}, field={name}")
            base_paths = highest_detail_visible_meshes(tuple(
                (_text(value.get("mesh_path"), "dynamic original mesh"),
                 RuntimeLodDraw(value["submesh"], value["flags"], value["lod_max_squared"], value["lod_min_squared"])) for value in original_pairs
                if type(value.get("flags")) is int and type(value.get("submesh")) is int
                and "/effects/" not in _text(value.get("mesh_path"), "dynamic original mesh")
                and "/effects/" not in _text(value.get("material_path"), "dynamic original material")
                and not is_hlod_name(_text(value.get("mesh_path"), "dynamic original mesh"))))
            if mesh_path not in base_paths:
                raise ValueError(f"Dynamic selected mesh differs from authored base LOD: source_key={entity_id}")
            if len(base_paths) > 1:
                expected_indices = [index for index, draw in enumerate(original_pairs) if draw["mesh_path"] == mesh_path
                                    and not draw["flags"] & 1024 and "/effects/" not in draw["material_path"]]
                mesh_ids = {original_pairs[index].get("mesh_hash_hex") for index in expected_indices}
                if (row.get("selected_mesh_paths") != list(base_paths) or indices != expected_indices
                        or len(mesh_ids) != 1 or not all(isinstance(key, str) and key for key in mesh_ids)
                        or row.get("mesh_part_key") != f"{entity_id}/mesh:{next(iter(mesh_ids))}"):
                    raise ValueError(f"Dynamic mesh part differs from its native source identity or draws: source_key={entity_id}")
            elif "mesh_part_key" in row or "selected_mesh_paths" in row:
                raise ValueError(f"Single-mesh dynamic record has unexpected mesh part metadata: source_key={entity_id}")
            exclusions = _list(row.get("excluded_source_draw_roles", []), "dynamic excluded source draw roles")
            for value in exclusions:
                exclusion = _object(value, "dynamic role exclusion")
                index = exclusion.get("source_draw_index")
                if type(index) is not int or not 0 <= index < len(original) or index in indices:
                    raise ValueError(f"Invalid dynamic role exclusion index: source_key={entity_id}, index={index}")
                evidence = original_pairs[index]
                if any(exclusion.get(name) != evidence.get(name) for name in ("mesh_path", "material_path")):
                    raise ValueError(f"Dynamic role exclusion differs from source draw: source_key={entity_id}, index={index}")
                _text(exclusion.get("role"), "dynamic excluded source role")
            for index, pair in zip(indices, pairs, strict=True):
                if type(index) is not int or not 0 <= index < len(original):
                    raise ValueError(f"Invalid dynamic source draw index: source_key={entity_id}, index={index}")
                evidence = _object(original[index], "dynamic source draw")
                current = _object(pair, "dynamic selected draw")
                if any(evidence.get(name) != current[name] for name in ("mesh_path", "material_path", "submesh", "flags")):
                    raise ValueError(f"Selected dynamic draw differs from original source: source_key={entity_id}, index={index}")
                if current["flags"] & 1024:
                    raise ValueError(f"Dynamic geometry includes a shadow-only selected draw: source_key={entity_id}")
                for name in ("lod_min_squared", "lod_max_squared"):
                    number = evidence.get(name)
                    if type(number) not in (int, float) or not math.isfinite(float(number)):
                        raise ValueError(f"Invalid dynamic source LOD value: source_key={entity_id}, field={name}")
        for slot in slots:
            if type(slot) is not int or not 0 <= slot <= len(pairs):
                raise ValueError(f"Invalid scene material slot index: entity_id={entity_id}, slot={slot}")
        for ref in refs:
            if ref is not None:
                _asset(ref, "scene material_ref")
        if "submesh_material_passes" in row or "submesh_pass_indices" in row:
            source_passes = runtime_submesh_passes(len(refs), tuple(str(_object(value, "renderer pair")["material_id"]) for value in pairs),
                tuple(RuntimeDraw(_object(value, "renderer pair")["submesh"], _object(value, "renderer pair")["flags"]) for value in pairs))
            pass_refs = _list(row.get("submesh_material_passes"), "scene submesh material passes")
            pass_indices = _list(row.get("submesh_pass_indices"), "scene submesh pass indices")
            if pass_indices != [list(indices) for indices in source_passes.indices] or len(pass_refs) != len(refs):
                raise ValueError(f"Ordered material passes differ from native source draws: entity_id={entity_id}")
            for submesh, (values, indices) in enumerate(zip(pass_refs, source_passes.indices, strict=True)):
                values = _list(values, "submesh material passes")
                if len(values) != len(indices) or (values and values[0] != refs[submesh]):
                    raise ValueError(f"Ordered material references differ from source slots: entity_id={entity_id}, submesh={submesh}")
                for value in values:
                    _asset(value, "ordered material reference")
        row.setdefault("selected_submeshes", list(range(len(slots))))
        selected = _list(row["selected_submeshes"], "scene selected_submeshes")
        if not selected or any(type(index) is not int or index < 0 or index >= len(slots) for index in selected):
            raise ValueError(f"Invalid selected source submesh: entity_id={entity_id}")
        if len(set(cast(list[int], selected))) != len(selected):
            raise ValueError(f"Repeated selected source submesh: entity_id={entity_id}")
        undrawn = _list(row.get("undrawn_submeshes"), "scene undrawn_submeshes")
        if any(type(index) is not int or index < 0 or index >= len(slots) for index in undrawn):
            raise ValueError(f"Invalid undrawn submesh: entity_id={entity_id}")
    _validate_static_submesh_pending(
        (*pending_rows, *_list(source["water_depth_pending"], "water depth pending")),
        (*instance_rows, *surface_rows), hashes,
    )
    _validate_static_mesh_parts((*instance_rows, *surface_rows))
    ordinary_by_id = {_object(value, "instance").get("mesh_part_key", _object(value, "instance")["entity_id"]):
                      _object(value, "instance") for value in instance_rows}
    for value in surface_rows:
        surface = _object(value, "surface effect")
        ordinary = ordinary_by_id.get(surface.get("mesh_part_key", surface["entity_id"]))
        if ordinary is not None:
            identity_fields = ("source_path", "source_sha256", "group_index", "entity_index", "matrix",
                               "mesh_asset", "mesh_file", "renderer_pairs", "material_refs", "submesh_slot_indices",
                               "undrawn_submeshes", "serialized_draw_parameters")
            if any(surface.get(name) != ordinary.get(name) for name in identity_fields):
                raise ValueError(f"Split surface source differs: entity_id={surface['entity_id']}")
            if set(cast(list[int], surface["selected_submeshes"])) & set(cast(list[int], ordinary["selected_submeshes"])):
                raise ValueError(f"Split surface source submeshes overlap: entity_id={surface['entity_id']}")
    if "ready_surface_effects" in _object(source.get("summary"), "scene summary"):
        count = _object(source["summary"], "scene summary")["ready_surface_effects"]
        if type(count) is not int or count != len(surface_rows):
            raise ValueError("Scene summary differs from ready surface effect rows")
    geometry_rows = _list(source["water_geometry"], "scene export water_geometry")
    material_rows = _list(source["water_materials"], "scene export water_materials")
    required_path_values = _list(source.get("required_resource_paths", []), "required_resource_paths")
    required_water_paths = {
        _text(value, "required_resource_paths item").replace("\\", "/").casefold()
        for value in required_path_values
    }
    for value in _list(source["required_uv_streams"], "required UV streams"):
        requirement = _object(value, "required UV stream")
        path = _text(requirement.get("mesh_path"), "required UV mesh_path")
        channels = _list(requirement.get("channels"), "required UV channels")
        if path not in required_water_paths or not channels or len(set(channels)) != len(channels) or any(type(channel) is not int or channel not in (1, 2) for channel in channels):
            raise ValueError(f"Required ordinary UV streams must identify exact requested mesh channels: requirement={requirement}")
    effect_rows = _list(source["effect_instances"], "scene export effect_instances")
    if effect_rows:
        physics = _object(source["effect_physics"], "effect_physics; regenerate legacy FX plans to capture game gravity")
        _text(physics.get("source"), "effect_physics.source")
        _finite_vector(physics.get("gravity"), "effect_physics.gravity", 3)
        if type(physics.get("path_id")) is not int:
            raise ValueError("effect_physics.path_id must identify a PhysicsManager object")
        digest = physics.get("source_sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("effect_physics.source_sha256 must contain the source file SHA256")
    manifest_names = {str(_file(value, "scene manifest")) for value in source["scene_manifests"]}
    validate_direct_effect_rows(source["direct_effect_renderers"], frozenset(manifest_names), frozenset(required_water_paths))
    stream_paths = _list(source["required_effect_mesh_paths"], "required_effect_mesh_paths")
    if any(not isinstance(path, str) or path not in required_water_paths for path in stream_paths):
        raise ValueError("FX source mesh stream paths must occur in required_resource_paths")
    for value in _list(source["direct_effect_pending"], "direct_effect_pending"):
        row = _object(value, "Direct FX pending")
        for field in ("source_key", "component", "reason", "detail"):
            _text(row.get(field), f"Direct FX pending.{field}")
        if type(row.get("source_pair_index")) is not int or row["source_pair_index"] < 0:
            raise ValueError("Direct FX pending requires a nonnegative source pair index")
    seen_effects: set[int] = set()
    for value in effect_rows:
        row = _object(value, "effect instance")
        entity_id = row.get("entity_id")
        if type(entity_id) is not int or entity_id in seen_effects or entity_id in seen:
            raise ValueError(f"FX entity_id must be a unique integer: entity_id={entity_id}")
        seen_effects.add(entity_id)
        for field in ("source_key", "source_virtual_path", "prefab_path"):
            _text(row.get(field), f"effect instance.{field}")
        for field, length in (("source_md5", 32), ("path_hash_hex", 16)):
            digest = row.get(field)
            if not isinstance(digest, str) or len(digest) != length or any(c not in "0123456789abcdefABCDEF" for c in digest):
                raise ValueError(f"FX {field} must be {length} hexadecimal characters: entity_id={entity_id}")
        path_value = cast(str, row["prefab_path"])
        if path_value != path_value.replace("\\", "/").casefold() or not path_value.startswith("assets/"):
            raise ValueError(f"FX prefab_path must be a canonical assets/ path: entity_id={entity_id}, path={path_value}")
        if path_value not in required_water_paths:
            raise ValueError(f"FX prefab is absent from required_resource_paths: entity_id={entity_id}, path={path_value}")
        root = _object(row.get("prefab_root"), "effect instance.prefab_root")
        _text(root.get("Source"), "effect instance.prefab_root.Source")
        if type(root.get("PathId")) is not int or root.get("Type") != "GameObject":
            raise ValueError(f"FX prefab_root must be a source-qualified GameObject: entity_id={entity_id}")
        manifest = _file(row.get("scene_manifest"), "effect instance.scene_manifest")
        if str(manifest) not in manifest_names:
            raise ValueError(f"FX scene_manifest is absent from scene_manifests: entity_id={entity_id}, path={manifest}")
        _finite_vector(row.get("matrix"), "effect instance.matrix", 16)
        if row.get("matrix_space") != "dynamic_effect_anchor":
            raise ValueError(f"FX matrix_space must be dynamic_effect_anchor: entity_id={entity_id}")
    seen_water: set[int] = set()
    for value in geometry_rows:
        row = _object(value, "water geometry row")
        entity_id = row.get("entity_id")
        if type(entity_id) is not int or entity_id in seen_water:
            raise ValueError(f"Water geometry ID is invalid or repeated: {entity_id}")
        seen_water.add(entity_id)
        _text(row.get("name"), "water geometry name")
        init_source = _text(row.get("init_source_path"), "water init_source_path")
        streaming_source = _text(row.get("streaming_source_path"), "water streaming_source_path")
        for source_path in (init_source, streaming_source):
            if not isinstance(hashes.get(source_path), str):
                raise ValueError(f"Water source hash is missing: entity_id={entity_id}, source={source_path}")
        for name in ("group_index", "entity_index"):
            if type(row.get(name)) is not int or cast(int, row[name]) < 0:
                raise ValueError(f"Water {name} must be nonnegative integer: entity_id={entity_id}")
        for name, length in (("matrix", 16), ("world_bounds", 6), ("ownership_anchor", 2),
                             ("param0", 4), ("param1", 4)):
            _finite_vector(row.get(name), f"water geometry.{name}", length)
        for name in ("render_type_mask", "runtime_mesh_id"):
            if type(row.get(name)) is not int:
                raise ValueError(f"Water {name} must be integer: entity_id={entity_id}")
        mesh_resource = _text(row.get("mesh_resource_path"), "water mesh_resource_path")
        config_resource = _text(row.get("config_resource_path"), "water config_resource_path")
        for name in ("mesh_hash_hex", "config_hash_hex"):
            digest = row.get(name)
            if not isinstance(digest, str) or len(digest) != 16 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"Water {name} must be 16 lowercase hexadecimal characters: entity_id={entity_id}")
        for resource in (mesh_resource, config_resource):
            if resource.replace("\\", "/").casefold() not in required_water_paths:
                raise ValueError(f"Water resource is absent from required_resource_paths: entity_id={entity_id}, path={resource}")
        mesh_asset = _object(row.get("mesh_asset"), "water mesh_asset")
        _text(mesh_asset.get("source"), "water mesh_asset.source")
        if type(mesh_asset.get("path_id")) is not int:
            raise ValueError(f"Water mesh asset path_id must be integer: entity_id={entity_id}")
        _text(mesh_asset.get("name"), "water mesh_asset.name")
        _file(row.get("mesh_file"), "water mesh_file")
    if material_rows:
        from endfield_scene_water_materials import read_water_material_row

        seen_materials: set[int] = set()
        for value in material_rows:
            material = read_water_material_row(value)
            if material.entity_id not in seen_water or material.entity_id in seen_materials:
                raise ValueError(f"Water material has no unique geometry row: entity_id={material.entity_id}")
            seen_materials.add(material.entity_id)
            geometry = next(
                cast(dict[str, object], _object(row, "water geometry row"))
                for row in geometry_rows
                if _object(row, "water geometry row").get("entity_id") == material.entity_id
            )
            if material.config_resource_path != geometry.get("config_resource_path"):
                raise ValueError(f"Water material config differs from geometry: entity_id={material.entity_id}")
            for resource_path in (
                material.config_resource_path,
                material.global_resource_path,
                material.normal_array_resource_path,
                *(sector.resource_path for sector in material.sectors),
            ):
                if resource_path.replace("\\", "/").casefold() not in required_water_paths:
                    raise ValueError(f"Water material resource is absent from required_resource_paths: entity_id={material.entity_id}, path={resource_path}")
    for value in (*_list(source["pending"], "scene pending"),
                  *_list(source["water_depth_pending"], "water depth pending")):
        row = _object(value, "pending row")
        if type(row.get("entity_id")) is not int:
            raise ValueError("Pending row requires integer entity_id")
        for name in ("entity_base", "component", "reason", "detail"):
            _text(row.get(name), f"pending.{name}")
    summary = _object(source.get("summary"), "scene summary")
    validate_prefab_geometry_plan(source)
    if static_exclusions or "excluded_static_shadow_only" in summary:
        if type(summary.get("excluded_static_shadow_only")) is not int or summary["excluded_static_shadow_only"] != len(static_exclusions):
            raise ValueError("Scene summary differs from static role exclusions")
    dynamic_pending = _list(source["dynamic_geometry_pending"], "dynamic geometry pending")
    validate_dynamic_mesh_parts(tuple(cast(DynamicGeometryRow, row) for row in dynamic_rows),
                                tuple(cast(PlanInstance, row) for row in source["instances"]))
    for value in dynamic_pending:
        row = _object(value, "dynamic geometry pending row")
        for name in ("source_key", "component", "reason", "detail"):
            _text(row.get(name), f"dynamic geometry pending.{name}")
    if dynamic_rows or dynamic_pending or "selected_dynamic_geometry" in summary:
        for name in ("selected_dynamic_geometry", "ready_dynamic_geometry", "pending_dynamic_geometry",
                     "excluded_dynamic_static_duplicates"):
            if type(summary.get(name)) is not int or summary[name] < 0:
                raise ValueError(f"Invalid dynamic geometry summary: field={name}")
        ready_records = len({_object(row, "dynamic geometry row")["source_key"] for row in dynamic_rows})
        pending_records = [_object(row, "dynamic pending row")["source_key"] for row in dynamic_pending]
        if (len(set(pending_records)) != len(pending_records)
                or set(pending_records) & {_object(row, "dynamic geometry row")["source_key"] for row in dynamic_rows}):
            raise ValueError("Dynamic source record is repeated across ready or pending states")
        if ((ready_records != len(dynamic_rows) or "ready_dynamic_geometry_records" in summary)
                and (type(summary.get("ready_dynamic_geometry_records")) is not int
                     or summary["ready_dynamic_geometry_records"] != ready_records)):
            raise ValueError("Scene summary differs from unique dynamic source records")
        if (summary["ready_dynamic_geometry"] != len(dynamic_rows)
                or summary["pending_dynamic_geometry"] != len(dynamic_pending)
                or summary["selected_dynamic_geometry"] != ready_records + len(dynamic_pending)
                + summary["excluded_dynamic_static_duplicates"]):
            raise ValueError("Scene summary differs from ordinary dynamic geometry rows")
    for name in ("selected_static", "ready_static", "selected_decals", "ready_decals", "pending"):
        if type(summary.get(name)) is not int or cast(int, summary[name]) < 0:
            raise ValueError(f"Invalid scene summary count: {name}")
    if "excluded_hlod" in summary and (type(summary["excluded_hlod"]) is not int or summary["excluded_hlod"] < 0):
        raise ValueError("Invalid scene summary count: excluded_hlod")
    if summary["ready_static"] != len(source["instances"]) or summary["pending"] != len(source["pending"]):
        raise ValueError("Scene summary differs from instance/pending rows")
    ready_entity_ids = {_object(row, "static instance")["entity_id"] for row in source["instances"]}
    if "ready_static_entities" in summary and (type(summary["ready_static_entities"]) is not int
            or summary["ready_static_entities"] != len(ready_entity_ids)):
        raise ValueError("Scene summary differs from unique static source entities")
    has_partial_entities = "partial_static_entities" in summary
    has_complete_entities = "complete_static_entities" in summary
    if has_partial_entities != has_complete_entities:
        raise ValueError("Scene summary requires both partial_static_entities and complete_static_entities")
    if has_partial_entities:
        if "ready_static_entities" not in summary:
            raise ValueError("Scene summary requires ready_static_entities when reporting partial/complete static entities")
        for name in ("partial_static_entities", "complete_static_entities"):
            if type(summary.get(name)) is not int or summary[name] < 0:
                raise ValueError(f"Invalid scene summary count: {name}")
        static_pending_entities = {
            cast(int, _object(row, "static pending row")["entity_id"])
            for row in source["pending"]
            if _object(row, "static pending row").get("component") in ("static_mesh", "static_mesh_submesh")
        }
        expected_partial = ready_entity_ids & static_pending_entities
        expected_complete = ready_entity_ids - expected_partial
        if (summary["partial_static_entities"] != len(expected_partial)
                or summary["complete_static_entities"] != len(expected_complete)):
            raise ValueError("Scene summary differs from partial/complete static source entities")
    if "selected_effects" in summary or "ready_effects" in summary or effect_rows:
        if type(summary.get("selected_effects")) is not int or type(summary.get("ready_effects")) is not int:
            raise ValueError("Scene summary requires integer selected_effects and ready_effects")
        if summary["selected_effects"] < 0 or summary["ready_effects"] != len(effect_rows):
            raise ValueError("Scene summary differs from selected/ready effect rows")
    if source["direct_effect_renderers"] or source["direct_effect_pending"] or "selected_direct_effects" in summary:
        for name in ("selected_direct_effects", "ready_direct_effects", "pending_direct_effects"):
            if type(summary.get(name)) is not int or summary[name] < 0:
                raise ValueError(f"Invalid direct FX scene summary count: field={name}")
        if (summary["ready_direct_effects"] != len(source["direct_effect_renderers"])
                or summary["pending_direct_effects"] != len(source["direct_effect_pending"])
                or summary["selected_direct_effects"] != summary["ready_direct_effects"] + summary["pending_direct_effects"]):
            raise ValueError("Scene summary differs from direct source FX draw rows")
    return cast(SceneExportPlan, source)
