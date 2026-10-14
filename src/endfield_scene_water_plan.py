"""Build a portable geometry plan from exact global water ECS bindings."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from endfield_scene_io import read_scene_manifest
from typing import cast

from endfield_scene_material_contract import reference_key, resolve_export_path
from endfield_scene_slot_contract import asset_name
from extractor_core.projected_decals import read_chunk
from extractor_core.scene_identity import validate_scene_id
from extractor_core.vfs import VfsFile
from extractor_core.water import WaterInstance, decode_water_instances


@dataclass(frozen=True)
class WaterMeshAsset:
    source: str
    path_id: int
    name: str


@dataclass(frozen=True)
class WaterGeometryRow:
    entity_id: int
    name: str
    init_source_path: str
    streaming_source_path: str
    group_index: int
    entity_index: int
    matrix: tuple[float, ...]
    world_bounds: tuple[float, float, float, float, float, float]
    ownership_anchor: tuple[float, float]
    render_type_mask: int
    runtime_mesh_id: int
    param0: tuple[float, float, float, float]
    param1: tuple[float, float, float, float]
    mesh_resource_path: str
    mesh_hash_hex: str
    config_resource_path: str
    config_hash_hex: str
    mesh_file: str
    mesh_asset: WaterMeshAsset


@dataclass(frozen=True)
class WaterGeometryPending:
    entity_id: int
    name: str
    reason: str
    detail: str
    mesh_resource_path: str
    config_resource_path: str


@dataclass(frozen=True)
class WaterGeometryPlan:
    rows: tuple[WaterGeometryRow, ...]
    pending: tuple[WaterGeometryPending, ...]
    required_resource_paths: tuple[str, ...]
    source_sha256: tuple[tuple[str, str], ...]


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"WATER_MANIFEST_OBJECT_INVALID label={label}")
    return cast(dict[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"WATER_MANIFEST_ARRAY_INVALID label={label}")
    return cast(list[object], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"WATER_MANIFEST_TEXT_INVALID label={label}")
    return value


def _bounds(value: Mapping[str, float], label: str) -> tuple[float, float, float, float]:
    numbers: list[float] = []
    for key in ("xmin", "xmax", "zmin", "zmax"):
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(float(number)):
            raise ValueError(f"WATER_BOUNDS_INVALID label={label} field={key} value={number}")
        numbers.append(float(number))
    xmin, xmax, zmin, zmax = numbers
    if xmin >= xmax or zmin >= zmax:
        raise ValueError(f"WATER_BOUNDS_NONINCREASING label={label} values={numbers}")
    return xmin, xmax, zmin, zmax


def _world_bounds(instance: WaterInstance) -> tuple[float, float, float, float, float, float]:
    peers = {peer.type_id: peer for peer in instance.peer_components}
    missing = set(range(6)) - peers.keys()
    if missing:
        raise ValueError(
            f"WATER_WORLD_BOUNDS_MISSING entity_id={instance.entity_id} types={sorted(missing)}"
        )
    values: list[float] = []
    for type_id in range(6):
        peer = peers[type_id]
        if peer.stride != 4 or len(peer.raw) != 4:
            raise ValueError(
                f"WATER_WORLD_BOUNDS_STRIDE_INVALID entity_id={instance.entity_id} "
                f"type={type_id} stride={peer.stride}"
            )
        value = struct.unpack("<f", peer.raw)[0]
        if not math.isfinite(value) or (type_id >= 3 and value < 0):
            raise ValueError(
                f"WATER_WORLD_BOUNDS_VALUE_INVALID entity_id={instance.entity_id} "
                f"type={type_id} value={value}"
            )
        values.append(value)
    cx, cy, cz, ex, ey, ez = values
    result = (cx - ex, cx + ex, cy - ey, cy + ey, cz - ez, cz + ez)
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"WATER_WORLD_BOUNDS_NONFINITE entity_id={instance.entity_id}")
    return result


def _selection_anchor(
    footprint: tuple[float, float, float, float, float, float],
    selection: tuple[float, float, float, float],
) -> tuple[float, float] | None:
    xmin, xmax, _, _, zmin, zmax = footprint
    sxmin, sxmax, szmin, szmax = selection
    if xmax <= sxmin or xmin >= sxmax or zmax <= szmin or zmin >= szmax:
        return None
    return max(xmin, sxmin), max(zmin, szmin)


def _mesh_asset(value: object, label: str) -> WaterMeshAsset:
    asset = _object(value, label)
    source = _text(asset.get("Source"), f"{label}.Source")
    path_id = asset.get("PathId")
    if type(path_id) is not int:
        raise ValueError(f"WATER_MESH_PATH_ID_INVALID label={label} value={path_id}")
    return WaterMeshAsset(source, path_id, _text(asset.get("Name"), f"{label}.Name"))


def _mesh_payloads(
    scene_manifests: tuple[Path, ...],
    mesh_paths: frozenset[str],
    check_cancelled: Callable[[], None] | None,
) -> dict[str, tuple[tuple[WaterMeshAsset, Path], ...]]:
    wanted = {path.split("##", 1)[0].replace("\\", "/").casefold() for path in mesh_paths}
    names = {asset_name(path) for path in mesh_paths}
    containers: dict[str, set[tuple[str, int]]] = defaultdict(set)
    exports: dict[tuple[str, int], tuple[WaterMeshAsset, Path]] = {}
    for manifest_path in scene_manifests:
        if check_cancelled is not None:
            check_cancelled()
        if not manifest_path.is_file():
            raise FileNotFoundError(f"WATER_SCENE_MANIFEST_MISSING path={manifest_path}")
        data = _object(read_scene_manifest(manifest_path), str(manifest_path))
        if data.get("Format") != "EndfieldSceneProbe/1":
            raise ValueError(f"WATER_SCENE_MANIFEST_FORMAT_INVALID path={manifest_path}")
        for raw in _array(data.get("Containers"), "SceneProbe.Containers"):
            row = _object(raw, "SceneProbe container")
            path = _text(row.get("Container"), "SceneProbe container path")
            normalized = path.replace("\\", "/").casefold()
            if normalized not in wanted:
                continue
            asset = _object(row.get("Asset"), "SceneProbe container Asset")
            if asset.get("Type") == "Mesh":
                containers[normalized].add(reference_key(asset))
        for raw in _array(data.get("MeshExports"), "SceneProbe.MeshExports"):
            row = _object(raw, "SceneProbe mesh export")
            if row.get("File") is None:
                continue
            asset = _mesh_asset(row.get("Asset"), "SceneProbe mesh Asset")
            if asset.name.casefold() not in names:
                continue
            file = resolve_export_path(
                manifest_path.parent / _text(row["File"], "SceneProbe mesh File").replace("\\", "/")
            )
            if not file.is_file():
                continue
            key = asset.source.casefold(), asset.path_id
            previous = exports.get(key)
            if previous is not None and previous[1] != file:
                with previous[1].open("rb") as stream:
                    previous_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                with file.open("rb") as stream:
                    current_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                if previous_hash != current_hash:
                    raise ValueError(
                        f"WATER_MESH_EXPORT_CONFLICT ref={key} "
                        f"first={previous[1]} second={file}"
                    )
            else:
                exports[key] = asset, file
    result: dict[str, tuple[tuple[WaterMeshAsset, Path], ...]] = {}
    for path in mesh_paths:
        stem = path.split("##", 1)[0].replace("\\", "/").casefold()
        expected_name = asset_name(path)
        candidates = tuple(
            exports[key]
            for key in sorted(containers.get(stem, set()))
            if key in exports and exports[key][0].name.casefold() == expected_name
        )
        result[path] = candidates
    return result


def build_water_geometry_plan(
    vfs_root: Path,
    entries: Mapping[str, VfsFile],
    path_index: Mapping[bytes, tuple[str, ...]],
    map_id: str,
    batch_bounds: Mapping[str, float],
    selection_bounds: Mapping[str, float],
    scene_manifests: tuple[Path, ...],
    check_cancelled: Callable[[], None] | None,
) -> WaterGeometryPlan:
    """Assign global type-30 water surfaces once without clipping geometry.

    This plan reads Global chunks only. Sector-scoped water components require a
    separate audited source contract before they can extend this plan.
    """
    validate_scene_id(map_id)
    batch = _bounds(batch_bounds, "batch")
    selection = _bounds(selection_bounds, "selection")
    if not (
        selection[0] <= batch[0] < batch[1] <= selection[1]
        and selection[2] <= batch[2] < batch[3] <= selection[3]
    ):
        raise ValueError(f"WATER_BATCH_OUTSIDE_SELECTION batch={batch} selection={selection}")
    indexed = {name.casefold(): entry for name, entry in entries.items()}
    pattern = re.compile(
        rf"^Data/Streaming/PC/{map_id}/Streaming/InitChunkData_Global_.*\.bytes$",
        re.IGNORECASE,
    )
    global_files = sorted(
        (entry for entry in indexed.values() if pattern.fullmatch(entry.file_name)),
        key=lambda entry: entry.file_name.casefold(),
    )
    if not global_files:
        raise FileNotFoundError(f"WATER_GLOBAL_INIT_MISSING map_id={map_id}")
    source_hashes: dict[str, str] = {}
    selected: list[
        tuple[WaterInstance, tuple[float, float, float, float, float, float], tuple[float, float], str, str]
    ] = []
    seen: set[int] = set()
    for entry in global_files:
        if check_cancelled is not None:
            check_cancelled()
        streaming_path = entry.file_name.replace("InitChunkData", "StreamingChunkData")
        streaming_entry = indexed.get(streaming_path.casefold())
        if streaming_entry is None:
            raise FileNotFoundError(f"WATER_GLOBAL_STREAMING_MISSING path={streaming_path}")
        init_data = read_chunk(vfs_root, entry)
        streaming_data = read_chunk(vfs_root, streaming_entry)
        source_hashes[entry.file_name] = hashlib.sha256(init_data).hexdigest()
        source_hashes[streaming_path] = hashlib.sha256(streaming_data).hexdigest()
        for instance in decode_water_instances(init_data, streaming_data, path_index):
            if instance.entity_id in seen:
                raise ValueError(f"WATER_GLOBAL_ENTITY_DUPLICATE entity_id={instance.entity_id}")
            seen.add(instance.entity_id)
            footprint = _world_bounds(instance)
            anchor = _selection_anchor(footprint, selection)
            if anchor is None or _selection_anchor(footprint, batch) is None:
                continue
            selected.append((instance, footprint, anchor, entry.file_name, streaming_path))
    required = {path for instance, _, _, _, _ in selected for path in (instance.mesh_path, instance.config_path)}
    payloads = _mesh_payloads(
        scene_manifests,
        frozenset(instance.mesh_path for instance, _, _, _, _ in selected),
        check_cancelled,
    ) if selected else {}
    rows: list[WaterGeometryRow] = []
    pending: list[WaterGeometryPending] = []
    for instance, footprint, anchor, init_path, streaming_path in selected:
        if check_cancelled is not None:
            check_cancelled()
        candidates = payloads[instance.mesh_path]
        if len(candidates) != 1:
            pending.append(
                WaterGeometryPending(
                    instance.entity_id,
                    instance.name,
                    "mesh_payload_missing" if not candidates else "mesh_payload_ambiguous",
                    f"Exact SceneProbe Container/OBJ candidates={len(candidates)} "
                    f"for mesh={instance.mesh_path}",
                    instance.mesh_path,
                    instance.config_path,
                )
            )
            continue
        asset, file = candidates[0]
        rows.append(
            WaterGeometryRow(
                instance.entity_id,
                instance.name,
                init_path,
                streaming_path,
                instance.group_index,
                instance.entity_index,
                instance.object_to_world,
                footprint,
                anchor,
                instance.render_type_mask,
                instance.runtime_mesh_id,
                instance.param0,
                instance.param1,
                instance.mesh_path,
                instance.mesh_hash_hex,
                instance.config_path,
                instance.config_hash_hex,
                str(file),
                asset,
            )
        )
    return WaterGeometryPlan(
        tuple(sorted(rows, key=lambda row: row.entity_id)),
        tuple(sorted(pending, key=lambda row: row.entity_id)),
        tuple(sorted(required, key=str.casefold)),
        tuple(sorted(source_hashes.items())),
    )
