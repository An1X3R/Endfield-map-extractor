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
from typing import Callable, Mapping, TypedDict, cast

from endfield_map_layer_contract import classify_entity
from endfield_scene_material_contract import load_catalog, reference_key, resolve_export_path, supported_slots
from endfield_scene_slot_contract import (
    AmbiguousSubmeshSlots, RuntimeDraw, asset_name, mesh_lod_index,
    runtime_submesh_slots,
)
from webui_source_contract import selected_static_categories


FORMAT = "EndfieldPortableScenePlan/1"


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


class Pending(TypedDict):
    entity_id: int
    entity_base: str
    component: str
    reason: str
    detail: str


class SceneExportPlan(TypedDict):
    format: str
    map_id: str
    bounds: Bounds
    category_filter: list[str]
    scene_manifests: list[str]
    terrain_surfaces: list[str]
    decal_manifest: str
    decal_materials: str
    instances: list[PlanInstance]
    pending: list[Pending]
    source_sha256: dict[str, str]
    summary: dict[str, int]


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
    path = (value if isinstance(value, Path) else Path(_text(value, label))).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    return path


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
    names = {row["mesh_path"] for row in pairs}
    primary = {name for name in names if mesh_lod_index(asset_name(name)) == 0}
    if len(primary) == 1:
        return next(iter(primary))
    if not primary and len(names) == 1 and mesh_lod_index(asset_name(next(iter(names)))) is None:
        return next(iter(names))
    raise ValueError(f"No unique LOD0 or unique non-LOD mesh: candidates={sorted(names)}")


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
    from dataclasses import asdict

    from extract_scene_renderer_materials import component_columns, decode_draws, decode_pairs
    from extractor_core.projected_decals import (
        build_path_id_index, decode_decal, read_chunk, rect_decal_records, streaming_records,
    )
    from extractor_core.resources import load_group
    from prepare_scene_geometry_repairs import obj_submesh_count

    runtime_path = _file(runtime_config, "runtime config")
    runtime = _object(json.loads(runtime_path.read_text(encoding="utf-8-sig")), "runtime config")
    if runtime.get("format") != "EndfieldRuntimeConfig/1" or canonical.get("format") != "EndfieldCanonicalExportJob/2":
        raise ValueError("Portable plan requires current runtime config and canonical export job")
    map_id = _text(canonical.get("map_id"), "canonical map_id")
    if map_id not in ("map01", "map02"):
        raise ValueError(f"Unsupported map: {map_id}")
    bounds = _bounds(canonical.get("bounds"))
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
    containers: dict[str, set[tuple[str, int]]] = defaultdict(set)
    mesh_exports: dict[tuple[str, int], tuple[AssetRef, Path]] = {}
    for manifest_path in manifest_paths:
        found_containers, found_exports = _manifest_index(manifest_path)
        for name, refs in found_containers.items():
            containers[name].update(refs)
        mesh_exports = _merge_mesh_exports(mesh_exports, found_exports)
    materials, textures = load_catalog(tuple(manifest_paths)) if manifest_paths else ({}, {})
    terrain_surfaces = [(_runtime_file(value, "terrain_surface/terrain_surface.json", runtime_path.parent).parent
                         if str(value).casefold().endswith("terrain_surface.json") else
                         _runtime_path(value, "terrain surface", runtime_path.parent))
                        for value in _list(map_paths.get("terrainSurfaces"), "terrainSurfaces")]
    for surface in terrain_surfaces:
        _file(surface / "terrain_surface.json", "terrain surface manifest")
    groups = {_text(value, "export group") for value in _list(canonical.get("export_groups"), "export_groups")}
    layers = _object(canonical.get("layers"), "canonical layers")
    allowed_categories = selected_static_categories(layers, groups)
    include_decals = bool(groups & {"all", "terrain", "road", "unknown"}) and (
        layers.get("instances") is True or layers.get("terrain") is True or layers.get("roads") is True)
    with closing(sqlite3.connect(instance_db.as_uri() + "?mode=ro", uri=True)) as database:
        rows = database.execute(
            "SELECT entity_id,entity_base,matrix_type,matrix_col_major,source_path,group_index,entity_index "
            "FROM instances WHERE x>=? AND x<? AND z>=? AND z<? ORDER BY source_path,entity_id",
            (bounds["xmin"], bounds["xmax"], bounds["zmin"], bounds["zmax"]),
        ).fetchall()
    selected_static = []
    selected_decals = []
    for entity_id, base, matrix_type, matrix_blob, source_path, group_index, entity_index in rows:
        if not isinstance(base, str) or not base:
            continue
        category, _ = classify_entity(base)
        if matrix_type == 18 and category in allowed_categories:
            selected_static.append((entity_id, base, matrix_blob, source_path, group_index, entity_index, category))
        elif matrix_type == 27 and include_decals:
            selected_decals.append((entity_id, base, matrix_blob, source_path))
    entries = ({entry.file_name: entry for entry in load_group(vfs, "C3442D43").iter_files()}
               if selected_static or selected_decals else {})
    path_index = build_path_id_index(path_hash) if selected_static or selected_decals else {}
    by_source: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in selected_static:
        by_source[cast(str, row[3])].append(row)
    decoded_decals: list[dict[str, object]] = []
    by_decal_source: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    for row in selected_decals:
        by_decal_source[cast(str, row[3])].append(row)
    source_hashes: dict[str, str] = {}
    ready: list[PlanInstance] = []
    pending: list[Pending] = []
    for component, layer_name, group_names in (
        ("water", "water", {"water", "all"}),
        ("effects", "effects", {"effects", "particle", "all"}),
        ("lights", "lights", {"lighting", "all"}),
    ):
        if layers.get(layer_name) is True and groups & group_names:
            pending.append({"entity_id": -1, "entity_base": "selection", "component": component,
                            "reason": "component_plan_not_implemented",
                            "detail": f"Selected {component} requires a separate verified component plan"})
    for source in sorted(set(by_source) | set(by_decal_source)):
        if check_cancelled is not None:
            check_cancelled()
        if source not in entries:
            raise FileNotFoundError(f"Selected current ECS source is absent from VFS: {source}")
        chunk = read_chunk(vfs, entries[source])
        digest = hashlib.sha256(chunk).hexdigest()
        source_hashes[source] = digest
        columns = component_columns(chunk) if source in by_source else {}
        for entity_id, base, matrix_blob, _, group_index, entity_index, category in by_source[source]:
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
                stem = mesh_path.split("##", 1)[0]
                candidates = [mesh_exports[key] for key in containers.get(stem, set()) if key in mesh_exports
                              and mesh_exports[key][0]["Name"].casefold() == asset_name(mesh_path)]
                if len({(reference_key(asset), file) for asset, file in candidates}) != 1:
                    raise ValueError(f"Primary mesh lacks a unique exact SceneProbe Container/OBJ: {mesh_path}")
                mesh_asset, mesh_file = candidates[0]
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
                ready.append({"entity_id": cast(int, entity_id), "entity_base": cast(str, base), "category": cast(str, category),
                              "source_path": source, "source_sha256": digest,
                              "group_index": cast(int, group_index), "entity_index": cast(int, entity_index),
                              "matrix": matrix, "mesh_path": mesh_path, "mesh_file": str(mesh_file),
                              "mesh_asset": mesh_asset, "renderer_pairs": active,
                              "submesh_slot_indices": list(mapping.indices), "material_refs": material_refs,
                              "undrawn_submeshes": list(mapping.undrawn_submeshes)})
            except (ValueError, AmbiguousSubmeshSlots) as error:
                pending.append({"entity_id": cast(int, entity_id), "entity_base": cast(str, base),
                                "component": "static_mesh", "reason": "exact_source_incomplete", "detail": str(error)})
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
        "decal_manifest": str(decal_path), "decal_materials": str(decal_material_path),
        "instances": ready, "pending": pending, "source_sha256": source_hashes,
        "summary": {"selected_static": len(selected_static), "ready_static": len(ready),
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
    _bounds(source.get("bounds"))
    for name in ("scene_manifests", "terrain_surfaces", "instances", "pending", "category_filter"):
        _list(source.get(name), f"scene export plan.{name}")
    for value in _list(source["scene_manifests"], "scene_manifests"):
        _file(value, "scene manifest")
    for value in _list(source["terrain_surfaces"], "terrain_surfaces"):
        _file(Path(_text(value, "terrain surface")) / "terrain_surface.json", "terrain surface manifest")
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
    for value in _list(source["instances"], "scene export instances"):
        row = _object(value, "scene export instance")
        entity_id = row.get("entity_id")
        if type(entity_id) is not int or entity_id in seen:
            raise ValueError(f"Scene instance ID is invalid or repeated: {entity_id}")
        seen.add(entity_id)
        _text(row.get("entity_base"), "scene entity_base")
        _text(row.get("category"), "scene category")
        source_path = _text(row.get("source_path"), "scene source_path")
        if row.get("source_sha256") != hashes.get(source_path):
            raise ValueError(f"Scene source hash mismatch: entity_id={entity_id}, source={source_path}")
        for name in ("group_index", "entity_index"):
            if type(row.get(name)) is not int or cast(int, row[name]) < 0:
                raise ValueError(f"Scene {name} must be nonnegative integer: entity_id={entity_id}")
        _text(row.get("mesh_path"), "scene mesh_path")
        _file(row.get("mesh_file"), "scene mesh_file")
        _asset(row.get("mesh_asset"), "scene mesh_asset")
        matrix = _list(row.get("matrix"), "scene matrix")
        if len(matrix) != 16 or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in matrix):
            raise ValueError(f"Scene instance has invalid 16-float matrix: entity_id={entity_id}")
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
    for value in _list(source["pending"], "scene pending"):
        row = _object(value, "pending row")
        if type(row.get("entity_id")) is not int:
            raise ValueError("Pending row requires integer entity_id")
        for name in ("entity_base", "component", "reason", "detail"):
            _text(row.get(name), f"pending.{name}")
    summary = _object(source.get("summary"), "scene summary")
    for name in ("selected_static", "ready_static", "selected_decals", "ready_decals", "pending"):
        if type(summary.get(name)) is not int or cast(int, summary[name]) < 0:
            raise ValueError(f"Invalid scene summary count: {name}")
    if summary["ready_static"] != len(seen) or summary["pending"] != len(source["pending"]):
        raise ValueError("Scene summary differs from instance/pending rows")
    return cast(SceneExportPlan, source)
