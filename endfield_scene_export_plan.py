"""Prepare a bounded scene plan from current first-run assets and ECS evidence."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Callable, Mapping, NotRequired, TypedDict, cast

from endfield_map_layer_contract import classify_entity, is_hlod_name
from endfield_scene_material_contract import load_catalog, reference_key, resolve_export_path, supported_slots
from endfield_scene_effects_plan import EffectInstance, build_effect_plan
from endfield_scene_lights_plan import LightPlanRow, build_light_plan, validate_light_plan_rows
from endfield_scene_slot_contract import (
    AmbiguousSubmeshSlots, RuntimeDraw, asset_name, primary_visible_mesh,
    runtime_submesh_slots,
)
from webui_source_contract import selected_static_categories


FORMAT = "EndfieldPortableScenePlan/1"


def owns_static_footprint(bounds: Mapping[str, float], selection: Mapping[str, float],
                          origin: tuple[float, float], footprint: tuple[float, ...]) -> bool:
    """Assign crossing geometry once using the legacy clipped-footprint owner rule."""
    x, z = origin
    if selection["xmin"] <= x < selection["xmax"] and selection["zmin"] <= z < selection["zmax"]:
        return bounds["xmin"] <= x < bounds["xmax"] and bounds["zmin"] <= z < bounds["zmax"]
    xmin, xmax, _, _, zmin, zmax = footprint
    if xmax <= selection["xmin"] or xmin >= selection["xmax"] or zmax <= selection["zmin"] or zmin >= selection["zmax"]:
        return False
    owner_x, owner_z = max(xmin, selection["xmin"]), max(zmin, selection["zmin"])
    return bounds["xmin"] <= owner_x < bounds["xmax"] and bounds["zmin"] <= owner_z < bounds["zmax"]


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
    serialized_draw_parameters: NotRequired[list[float] | None]


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
    terrain_surfaces: list[str]
    water_depth_surfaces: list[str]
    water_depth_instances: list[PlanInstance]
    water_depth_pending: list[Pending]
    terrain_details: list[str]
    decal_manifest: str
    decal_materials: str
    instances: list[PlanInstance]
    effect_instances: list[EffectInstance]
    effect_physics: EffectPhysicsPlan | None
    light_instances: list[LightPlanRow]
    water_geometry: list[WaterGeometryPlanRow]
    water_materials: list[WaterMaterialPlanRow]
    pending: list[Pending]
    source_sha256: dict[str, str]
    summary: dict[str, int]
    required_resource_paths: list[str]


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
    if map_id not in ("map01", "map02"):
        raise ValueError(f"WATER_GLOBAL_PATH_MAP_UNSUPPORTED map_id={map_id}")
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
    dict[str, set[tuple[str, int]]],
    dict[tuple[str, int], tuple[AssetRef, Path]],
]:
    data = _object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))
    if data.get("Format") != "EndfieldSceneProbe/1":
        raise ValueError(f"Unsupported SceneProbe manifest: {path}")
    containers: dict[str, set[tuple[str, int]]] = defaultdict(set)
    exports: dict[tuple[str, int], tuple[AssetRef, Path]] = {}
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
        file = resolve_export_path(path.parent / _text(row["File"], "mesh export File").replace("\\", "/"))
        if file.is_file():
            key = reference_key(asset)
            if key in exports and exports[key][1] != file:
                raise ValueError(f"Mesh AssetRef has multiple export files: {key}")
            exports[key] = (asset, file)
    return containers, exports


def _merge_mesh_exports(
    existing: dict[tuple[str, int], tuple[AssetRef, Path]],
    incoming: dict[tuple[str, int], tuple[AssetRef, Path]],
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
    if map_id not in ("map01", "map02"):
        raise ValueError(f"Unsupported map: {map_id}")
    bounds = _bounds(canonical.get("bounds"))
    selection = _bounds(canonical.get("selection_bounds", bounds))
    if not (selection["xmin"] <= bounds["xmin"] < bounds["xmax"] <= selection["xmax"]
            and selection["zmin"] <= bounds["zmin"] < bounds["zmax"] <= selection["zmax"]):
        raise ValueError("Scene batch bounds must lie inside the canonical selection")
    paths = _object(runtime.get("paths"), "runtime paths")
    game_root = _runtime_path(paths.get("game_root"), "runtime game_root", runtime_path.parent)
    cache_root = _runtime_path(paths.get("cache_root"), "runtime cache_root", runtime_path.parent)
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
    for candidate_map in (map_id, *(name for name in ("map01", "map02") if name != map_id)):
        candidate = maps.get(candidate_map)
        if candidate is None:
            continue
        probe = _object(candidate, f"runtime map {candidate_map}").get("sceneProbe")
        if probe is not None and _object(probe, f"sceneProbe {candidate_map}").get("path") is not None:
            manifest_paths.append(_runtime_file(_object(probe, "sceneProbe")["path"],
                                                "SceneProbe manifest", runtime_path.parent))
    from endfield_scene_resources import supplemental_manifests

    manifest_paths.extend(supplemental_manifests(runtime_path, runtime))
    containers: dict[str, set[tuple[str, int]]] = defaultdict(set)
    mesh_exports: dict[tuple[str, int], tuple[AssetRef, Path]] = {}
    for manifest_path in manifest_paths:
        found_containers, found_exports = _manifest_index(manifest_path)
        for name, refs in found_containers.items():
            containers[name].update(refs)
        mesh_exports = _merge_mesh_exports(mesh_exports, found_exports)
    materials, textures = load_catalog(tuple(manifest_paths)) if manifest_paths else ({}, {})
    groups = {_text(value, "export group") for value in _list(canonical.get("export_groups"), "export_groups")}
    layers = _object(canonical.get("layers"), "canonical layers")
    terrain_selected = layers.get("terrain") is True and bool(groups & {"all", "terrain"})
    water_selected = layers.get("water") is True and bool(groups & {"water", "all"})
    terrain_surface_values = map_paths.get("terrainSurfaces")
    terrain_surfaces = (_terrain_surface_roots(terrain_surface_values, runtime_path.parent)
                        if terrain_selected else [])
    water_depth_surfaces = (
        _terrain_surface_roots(terrain_surface_values, runtime_path.parent)
        if water_selected and terrain_surface_values is not None else []
    )
    water_depth_missing = water_selected and not water_depth_surfaces
    allowed_categories = selected_static_categories(layers, groups)
    include_decals = bool(groups & {"all", "terrain", "road", "unknown"}) and (
        layers.get("instances") is True or layers.get("terrain") is True or layers.get("roads") is True)
    with closing(sqlite3.connect(instance_db.as_uri() + "?mode=ro", uri=True)) as database:
        rows = database.execute(
            "SELECT entity_id,entity_base,matrix_type,matrix_col_major,source_path,group_index,entity_index,x,z "
            "FROM instances WHERE matrix_type=18 OR (x>=? AND x<? AND z>=? AND z<?) ORDER BY source_path,entity_id",
            (bounds["xmin"], bounds["xmax"], bounds["zmin"], bounds["zmax"]),
        ).fetchall()
    selected_static = []
    selected_decals = []
    excluded_hlod = 0
    excluded_hlod_mesh = 0
    for entity_id, base, matrix_type, matrix_blob, source_path, group_index, entity_index, origin_x, origin_z in rows:
        if not isinstance(base, str) or not base:
            continue
        category, _ = classify_entity(base)
        if matrix_type == 18 and (category in allowed_categories or water_selected):
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
    by_source: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in selected_static:
        by_source[cast(str, row[3])].append(row)
    decoded_decals: list[dict[str, object]] = []
    by_decal_source: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in selected_decals:
        by_decal_source[cast(str, row[3])].append(row)
    source_hashes: dict[str, str] = {}
    ready: list[PlanInstance] = []
    water_depth_instances: list[PlanInstance] = []
    water_depth_pending: list[Pending] = []
    visible_static: set[int] = set()
    pending: list[Pending] = []
    required_resources: set[str] = set()
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
    if layers.get("effects") is True and groups & {"effects", "particle", "all"}:
        from extractor_core.effects import read_dynamic_scene_effects

        def effect_cancelled() -> None:
            if check_cancelled is not None:
                check_cancelled()

        inventory = read_dynamic_scene_effects(vfs, path_hash, effect_cancelled)[map_id]
        effect_plan = build_effect_plan(inventory, bounds, tuple(manifest_paths), materials, textures)
        effect_instances.extend(effect_plan.rows)
        pending.extend(cast(list[Pending], list(effect_plan.pending)))
        required_resources.update(effect_plan.required_resource_paths)
        selected_effects = effect_plan.selected_count
        if effect_instances:
            from extractor_core.physics import read_physics_settings

            physics = read_physics_settings(game_root / "Endfield_Data" / "globalgamemanagers")
            effect_physics = {"source": physics.source, "source_sha256": physics.source_sha256,
                              "path_id": physics.path_id, "gravity": list(physics.gravity)}
    light_instances: list[LightPlanRow] = []
    selected_lights = 0
    if layers.get("lights") is True and groups & {"lighting", "all"}:
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
    for source in sorted(set(by_source) | set(by_decal_source)):
        if check_cancelled is not None:
            check_cancelled()
        if source not in entries:
            raise FileNotFoundError(f"Selected current ECS source is absent from VFS: {source}")
        chunk = read_chunk(vfs, entries[source])
        selected_rows = []
        if source in by_source:
            spatial = {record["entity_id"]: record for record in iter_entities(chunk)}
            for row in by_source[source]:
                entity_id = cast(int, row[0])
                record = spatial.get(entity_id)
                if record is None or record["matrix_blob"] != row[2]:
                    raise ValueError(f"Current spatial source differs from instance index: entity_id={entity_id}, source={source}")
                footprint = record["world_bounds"]
                if footprint is None:
                    raise ValueError(f"Static entity has no source world bounds: entity_id={entity_id}, source={source}")
                origin = (record["x"], record["z"])
                visible = row[6] in allowed_categories and owns_static_footprint(bounds, selection, origin, footprint)
                bottom = any(
                    footprint[0] < min(water["world_bounds"][1], bounds["xmax"])
                    and footprint[1] > max(water["world_bounds"][0], bounds["xmin"])
                    and footprint[4] < min(water["world_bounds"][5], bounds["zmax"])
                    and footprint[5] > max(water["world_bounds"][4], bounds["zmin"])
                    and footprint[2] < water["world_bounds"][3]
                    and footprint[3] > water["world_bounds"][2] - 150.0
                    for water in water_geometry_rows
                )
                if visible or bottom:
                    selected_rows.append(row)
                if visible:
                    visible_static.add(entity_id)
                    selected_static_count += 1
                    if not (selection["xmin"] <= origin[0] < selection["xmax"]
                            and selection["zmin"] <= origin[1] < selection["zmax"]):
                        footprint_extras += 1
        if not selected_rows and source not in by_decal_source:
            continue
        digest = hashlib.sha256(chunk).hexdigest()
        source_hashes[source] = digest
        columns = component_columns(chunk) if selected_rows else {}
        for entity_id, base, matrix_blob, _, group_index, entity_index, category in selected_rows:
            if check_cancelled is not None:
                check_cancelled()
            try:
                renderer = columns.get(cast(int, entity_id), {})
                pairs: list[RendererPair] = []
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
                if not pairs:
                    raise ValueError("No current Renderer mesh/material pairs")
                mesh_path = _primary_mesh(pairs)
                if is_hlod_name(mesh_path):
                    excluded_hlod += 1
                    excluded_hlod_mesh += int(entity_id in visible_static)
                    continue
                required_resources.add(mesh_path)
                required_resources.update(pair["material_path"] for pair in pairs
                                          if pair["mesh_path"] == mesh_path and not pair["flags"] & 1024)
                stem = mesh_path.split("##", 1)[0]
                candidates = [mesh_exports[key] for key in containers.get(stem, set()) if key in mesh_exports
                              and mesh_exports[key][0]["Name"].casefold() == asset_name(mesh_path)]
                if len({(reference_key(asset), file) for asset, file in candidates}) != 1:
                    raise ValueError(f"Primary mesh lacks a unique exact SceneProbe Container/OBJ: {mesh_path}")
                mesh_asset, mesh_file = candidates[0]
                if is_hlod_name(mesh_asset["Name"]):
                    excluded_hlod += 1
                    excluded_hlod_mesh += int(entity_id in visible_static)
                    continue
                active = [pair for pair in pairs if pair["mesh_path"] == mesh_path]
                count = obj_submesh_count(mesh_file)
                mapping = runtime_submesh_slots(count, tuple(pair["material_id"] for pair in active),
                                                tuple(RuntimeDraw(pair["submesh"], pair["flags"]) for pair in active))
                material_refs: list[AssetRef | None] = []
                for slot_index in mapping.indices:
                    if slot_index == len(active):
                        material_refs.append(None)
                        continue
                    pair = active[slot_index]
                    refs = containers.get(pair["material_path"], set())
                    if len(refs) != 1:
                        raise ValueError(f"Material container has {len(refs)} AssetRefs: {pair['material_path']}")
                    key = next(iter(refs))
                    if key not in materials:
                        raise ValueError(f"Material record missing: {pair['material_path']}:{key}")
                    record = materials[key]
                    if record["Name"].casefold().startswith(("m_fx_", "m_water", "m_shadow", "m_fog", "m_volume")):
                        raise ValueError(f"Non-ordinary material requires separate renderer: {record['Name']}")
                    for texture_slot in supported_slots(record):
                        texture = texture_slot["Texture"]
                        if texture is not None and reference_key(texture) not in textures:
                            raise ValueError(f"Required material texture missing: {record['Name']}:{texture_slot['Property']}")
                    material_refs.append({"Source": record["Source"], "PathId": record["PathId"], "Name": record["Name"]})
                matrix = list(struct.unpack("<16f", cast(bytes, matrix_blob)))
                if not all(math.isfinite(value) for value in matrix):
                    raise ValueError("Entity matrix has non-finite components")
                destination = ready if entity_id in visible_static else water_depth_instances
                destination.append({"entity_id": cast(int, entity_id), "entity_base": cast(str, base), "category": cast(str, category),
                              "source_path": source, "source_sha256": digest,
                              "group_index": cast(int, group_index), "entity_index": cast(int, entity_index),
                              "matrix": matrix, "mesh_path": mesh_path, "mesh_file": str(mesh_file),
                              "mesh_asset": mesh_asset, "renderer_pairs": active,
                              "submesh_slot_indices": list(mapping.indices), "material_refs": material_refs,
                              "undrawn_submeshes": list(mapping.undrawn_submeshes),
                              "serialized_draw_parameters": serialized_draw_parameters(renderer[18], cast(bytes, matrix_blob))
                              if 18 in renderer else None})
            except (ValueError, AmbiguousSubmeshSlots) as error:
                failures = pending if entity_id in visible_static else water_depth_pending
                failures.append({"entity_id": cast(int, entity_id), "entity_base": cast(str, base),
                                 "component": "static_mesh" if entity_id in visible_static else "water_depth_static",
                                 "reason": "exact_source_incomplete", "detail": str(error)})
        if source in by_decal_source:
            partner = source.replace("InitChunkData_", "StreamingChunkData_")
            if partner not in entries:
                raise FileNotFoundError(f"Projected decal StreamingChunkData is missing: {partner}")
            stream = read_chunk(vfs, entries[partner])
            source_hashes[partner] = hashlib.sha256(stream).hexdigest()
            rects, records = rect_decal_records(chunk), streaming_records(stream)
            for entity_id, base, expected_matrix, _ in by_decal_source[source]:
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
    required_resources.update(str(row["material_path"]).replace("\\", "/").casefold() for row in decoded_decals)
    decal_rows, decal_material_rows, decal_pending = _decal_materials(decoded_decals, containers, materials, textures)
    pending.extend(decal_pending)
    decal_path = target.with_name(target.stem + "_decals.json")
    decal_material_path = target.with_name(target.stem + "_decal_materials.json")
    for path in (decal_path, decal_material_path):
        if path.exists():
            raise FileExistsError(f"Portable scene sidecar already exists: {path}")
    result: SceneExportPlan = {
        "format": FORMAT, "map_id": map_id, "bounds": bounds, "category_filter": sorted(groups),
        "scene_manifests": [str(path) for path in manifest_paths],
        "terrain_surfaces": [str(path) for path in terrain_surfaces],
        "water_depth_surfaces": [str(path) for path in water_depth_surfaces],
        "water_depth_instances": water_depth_instances, "water_depth_pending": water_depth_pending,
        "terrain_details": [],
        "decal_manifest": str(decal_path), "decal_materials": str(decal_material_path),
        "instances": ready, "effect_instances": effect_instances, "effect_physics": effect_physics,
        "light_instances": light_instances,
        "water_geometry": water_geometry_rows,
        "water_materials": water_material_rows, "pending": pending, "source_sha256": source_hashes,
        "required_resource_paths": sorted(required_resources),
        "summary": {"selected_static": selected_static_count - excluded_hlod_mesh,
                    "footprint_overlap_extras": footprint_extras,
                    "excluded_hlod": excluded_hlod,
                    "ready_static": len(ready),
                    "selected_effects": selected_effects, "ready_effects": len(effect_instances),
                    "selected_lights": selected_lights, "ready_lights": len(light_instances),
                    "selected_decals": len(selected_decals), "ready_decals": len(decal_rows), "pending": len(pending)},
    }
    decal_payload = {"format": "EndfieldProjectedDecals/1", "instances_db": str(instance_db),
                     "path_hash_sha256": hashlib.sha256(path_hash.read_bytes()).hexdigest(),
                     "bounds": [bounds[name] for name in ("xmin", "xmax", "zmin", "zmax")],
                     "source_sha256": source_hashes, "records": decal_rows}
    material_payload = {"format": "EndfieldProjectedDecalMaterials/1", "Materials": decal_material_rows}
    created: list[Path] = []
    try:
        for path, payload in ((decal_path, decal_payload), (decal_material_path, material_payload), (target, result)):
            with path.open("x", encoding="utf-8") as handle:
                created.append(path)
                json.dump(payload, handle, indent=2)
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return target


def load_scene_export_plan(path: Path) -> SceneExportPlan:
    """Validate the versioned plan before a Blender worker uses its paths."""
    source = _object(json.loads(_file(path, "scene export plan").read_text(encoding="utf-8-sig")), "scene export plan")
    if source.get("format") != FORMAT or source.get("map_id") not in ("map01", "map02"):
        raise ValueError("Unsupported portable scene plan format or map")
    source.setdefault("water_geometry", [])
    source.setdefault("water_materials", [])
    source.setdefault("water_depth_surfaces", [])
    source.setdefault("water_depth_instances", [])
    source.setdefault("water_depth_pending", [])
    source.setdefault("effect_instances", [])
    source.setdefault("effect_physics", None)
    source.setdefault("light_instances", [])
    _bounds(source.get("bounds"))
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
    seen: set[int] = set()
    validate_light_plan_rows(source["light_instances"], str(source["map_id"]), hashes)
    for value in (*_list(source["instances"], "scene export instances"),
                  *_list(source["water_depth_instances"], "scene export water depth instances")):
        row = _object(value, "scene export instance")
        entity_id = row.get("entity_id")
        if type(entity_id) is not int or entity_id in seen:
            raise ValueError(f"Scene instance ID is invalid or repeated: {entity_id}")
        seen.add(entity_id)
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
        for name in ("group_index", "entity_index"):
            if type(row.get(name)) is not int or cast(int, row[name]) < 0:
                raise ValueError(f"Scene {name} must be nonnegative integer: entity_id={entity_id}")
        _file(row.get("mesh_file"), "scene mesh_file")
        matrix = _list(row.get("matrix"), "scene matrix")
        if len(matrix) != 16 or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in matrix):
            raise ValueError(f"Scene instance has invalid 16-float matrix: entity_id={entity_id}")
        parameters = row.get("serialized_draw_parameters")
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
        for slot in slots:
            if type(slot) is not int or not 0 <= slot <= len(pairs):
                raise ValueError(f"Invalid scene material slot index: entity_id={entity_id}, slot={slot}")
        for ref in refs:
            if ref is not None:
                _asset(ref, "scene material_ref")
        undrawn = _list(row.get("undrawn_submeshes"), "scene undrawn_submeshes")
        if any(type(index) is not int or index < 0 or index >= len(slots) for index in undrawn):
            raise ValueError(f"Invalid undrawn submesh: entity_id={entity_id}")
    geometry_rows = _list(source["water_geometry"], "scene export water_geometry")
    material_rows = _list(source["water_materials"], "scene export water_materials")
    required_path_values = _list(source.get("required_resource_paths", []), "required_resource_paths")
    required_water_paths = {
        _text(value, "required_resource_paths item").replace("\\", "/").casefold()
        for value in required_path_values
    }
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
    for name in ("selected_static", "ready_static", "selected_decals", "ready_decals", "pending"):
        if type(summary.get(name)) is not int or cast(int, summary[name]) < 0:
            raise ValueError(f"Invalid scene summary count: {name}")
    if "excluded_hlod" in summary and (type(summary["excluded_hlod"]) is not int or summary["excluded_hlod"] < 0):
        raise ValueError("Invalid scene summary count: excluded_hlod")
    if summary["ready_static"] != len(source["instances"]) or summary["pending"] != len(source["pending"]):
        raise ValueError("Scene summary differs from instance/pending rows")
    if "selected_effects" in summary or "ready_effects" in summary or effect_rows:
        if type(summary.get("selected_effects")) is not int or type(summary.get("ready_effects")) is not int:
            raise ValueError("Scene summary requires integer selected_effects and ready_effects")
        if summary["selected_effects"] < 0 or summary["ready_effects"] != len(effect_rows):
            raise ValueError("Scene summary differs from selected/ready effect rows")
    return cast(SceneExportPlan, source)
