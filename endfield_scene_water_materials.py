"""Resolve water configuration and texture payloads by serialized asset identity."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from endfield_scene_material_contract import reference_key, resolve_export_path
if TYPE_CHECKING:
    from endfield_scene_water_plan import WaterGeometryRow


ExportCandidate = tuple[dict[str, object], Path, Path | None]


@dataclass(frozen=True)
class WaterAssetRef:
    source: str
    path_id: int
    type_name: str


@dataclass(frozen=True)
class WaterTextureBinding:
    asset: WaterAssetRef
    file: str


@dataclass(frozen=True)
class WaterNormalSlice:
    asset: WaterAssetRef
    slice_index: int
    file: str


@dataclass(frozen=True)
class WaterFlowCore:
    use_flow_map: bool
    material_index: int
    flow_direction: float
    flow_direction_vector: tuple[float, float]
    small_wave_normal_type: int
    large_wave_normal_type: int
    flow_speed: float
    large_wave_flow_speed: float
    normal_tilling: float
    large_wave_normal_tilling: float
    normal_scale: float
    large_wave_normal_scale: float
    water_ior: float


@dataclass(frozen=True)
class WaterSectorBinding:
    resource_path: str
    sector_x: int
    sector_z: int
    asset: WaterAssetRef
    texture: WaterTextureBinding
    source_json: str


@dataclass(frozen=True)
class WaterMaterialRow:
    entity_id: int
    config_resource_path: str
    config_asset: WaterAssetRef
    global_resource_path: str
    global_asset: WaterAssetRef
    normal_array_resource_path: str
    flow: WaterFlowCore
    flow_map: WaterTextureBinding | None
    small_wave_normal: WaterNormalSlice
    large_wave_normal: WaterNormalSlice
    sectors: tuple[WaterSectorBinding, ...]
    sector_complete: bool
    sector_mapping_status: str
    config_source_json: str
    global_source_json: str


@dataclass(frozen=True)
class WaterMaterialPending:
    entity_id: int
    resource_path: str | None
    reason: str
    detail: str


@dataclass(frozen=True)
class WaterMaterialPlan:
    rows: tuple[WaterMaterialRow, ...]
    pending: tuple[WaterMaterialPending, ...]
    required_resource_paths: tuple[str, ...]


def read_water_material_row(value: object) -> WaterMaterialRow:
    """Validate a serialized portable material row before building Blender nodes."""
    row = _object(value, "water material row")

    def asset(value: object) -> WaterAssetRef:
        data = _object(value, "water material asset")
        return WaterAssetRef(_text(data.get("source"), "asset source"),
                             _integer(data.get("path_id"), "asset path_id"),
                             _text(data.get("type_name"), "asset type_name"))

    def texture(value: object) -> WaterTextureBinding:
        data = _object(value, "water texture")
        file = _text(data.get("file"), "water texture file")
        if not Path(file).is_file():
            raise FileNotFoundError(f"WATER_TEXTURE_FILE_MISSING path={file}")
        return WaterTextureBinding(asset(data.get("asset")), file)

    def normal(value: object) -> WaterNormalSlice:
        data = _object(value, "water normal")
        image = texture(data)
        index = _integer(data.get("slice_index"), "normal slice_index")
        if index < 0:
            raise ValueError(f"WATER_NORMAL_SLICE_NEGATIVE index={index}")
        return WaterNormalSlice(image.asset, index, image.file)

    config_json = _text(row.get("config_source_json"), "water config_source_json")
    global_json = _text(row.get("global_source_json"), "water global_source_json")
    config = _object(json.loads(config_json), "water config")
    _object(json.loads(global_json), "water global")
    flow = _flow_core(config)
    if json.loads(_source_json(asdict(flow))) != row.get("flow"):
        raise ValueError("WATER_SERIALIZED_FLOW_DIFFERS_FROM_CONFIG")
    sectors: list[WaterSectorBinding] = []
    for value in _array(row.get("sectors"), "water sectors"):
        sector = _object(value, "water sector")
        sectors.append(WaterSectorBinding(
            _text(sector.get("resource_path"), "sector resource_path"),
            _integer(sector.get("sector_x"), "sector_x"),
            _integer(sector.get("sector_z"), "sector_z"),
            asset(sector.get("asset")), texture(sector.get("texture")),
            _text(sector.get("source_json"), "sector source_json")))
    complete = row.get("sector_complete")
    if type(complete) is not bool:
        raise ValueError("WATER_SECTOR_COMPLETE_MUST_BE_BOOLEAN")
    if "flow_map" not in row:
        raise ValueError("WATER_FLOW_MAP_FIELD_MISSING")
    flow_map_value = row["flow_map"]
    if flow.use_flow_map and flow_map_value is None:
        raise ValueError(f"WATER_FLOW_MAP_REQUIRED entity_id={row.get('entity_id')}")
    if not flow.use_flow_map and flow_map_value is not None:
        unused_flow_map = _object(flow_map_value, "unused water flow map")
        asset(unused_flow_map.get("asset"))
        _text(unused_flow_map.get("file"), "unused water flow map file")
    flow_map = texture(flow_map_value) if flow.use_flow_map else None
    return WaterMaterialRow(
        _integer(row.get("entity_id"), "water entity_id"),
        _text(row.get("config_resource_path"), "water config_resource_path"),
        asset(row.get("config_asset")),
        _text(row.get("global_resource_path"), "water global_resource_path"),
        asset(row.get("global_asset")),
        _text(row.get("normal_array_resource_path"), "water normal_array_resource_path"),
        flow, flow_map, normal(row.get("small_wave_normal")),
        normal(row.get("large_wave_normal")), tuple(sectors), complete,
        _text(row.get("sector_mapping_status"), "water sector_mapping_status"),
        config_json, global_json)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"WATER_MATERIAL_OBJECT_INVALID label={label}")
    return cast(dict[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"WATER_MATERIAL_ARRAY_INVALID label={label}")
    return cast(list[object], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"WATER_MATERIAL_TEXT_INVALID label={label}")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"WATER_MATERIAL_INTEGER_INVALID label={label} value={value}")
    return cast(int, value)


def _number(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"WATER_MATERIAL_NUMBER_INVALID label={label} value={value}")
    return float(value)


def _source_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _record_identity(value: object, target: bool) -> object:
    if isinstance(value, dict):
        return {
            key: _record_identity(item, key == "Target")
            for key, item in value.items()
            if key != "IsResolved" and not (target and key == "Name")
        }
    if isinstance(value, list):
        return [_record_identity(item, False) for item in value]
    return value


def _asset(value: object, label: str) -> WaterAssetRef:
    record = _object(value, label)
    return WaterAssetRef(
        _text(record.get("Source"), f"{label}.Source"),
        _integer(record.get("PathId"), f"{label}.PathId"),
        _text(record.get("Type"), f"{label}.Type"),
    )


def _pointer(value: object, label: str, type_name: str) -> WaterAssetRef:
    """Keep the serialized target identity across unloaded asset bundles."""
    record = _object(value, label)
    if record.get("IsNull") is not False:
        raise ValueError(f"WATER_MATERIAL_POINTER_NULL label={label}")
    target = _asset(record.get("Target"), f"{label}.Target")
    if target.type_name != type_name:
        raise ValueError(
            f"WATER_MATERIAL_POINTER_TYPE_INVALID label={label} "
            f"expected={type_name} actual={target.type_name}"
        )
    return target


def _record_asset(value: object, label: str) -> WaterAssetRef:
    record = _object(value, label)
    header = _object(record.get("Header"), f"{label}.Header")
    asset = _asset(header.get("Asset"), f"{label}.Header.Asset")
    if asset.type_name != "MonoBehaviour":
        raise ValueError(f"WATER_MATERIAL_RECORD_TYPE_INVALID label={label} type={asset.type_name}")
    return asset


def _insert_record(
    target: dict[tuple[str, int], dict[str, object]],
    value: object,
    label: str,
) -> None:
    record = _object(value, label)
    asset = _record_asset(record, label)
    key = asset.source.casefold(), asset.path_id
    prior = target.get(key)
    if prior is not None and _source_json(_record_identity(prior, False)) != _source_json(
        _record_identity(record, False)
    ):
        raise ValueError(f"WATER_MATERIAL_RECORD_CONFLICT kind={label} ref={key}")
    target[key] = record


def _insert_export(
    target: dict[tuple[str, int], list[ExportCandidate]],
    value: object,
    manifest_path: Path,
    label: str,
) -> None:
    record = _object(value, label)
    asset = _asset(record.get("Asset"), f"{label}.Asset")
    key = asset.source.casefold(), asset.path_id
    if label == "TextureExports" and asset.type_name != "Texture2D":
        raise ValueError(f"WATER_TEXTURE_EXPORT_TYPE_INVALID ref={key} type={asset.type_name}")
    if label == "TextureArrayExports" and asset.type_name != "Texture2DArray":
        raise ValueError(f"WATER_ARRAY_EXPORT_TYPE_INVALID ref={key} type={asset.type_name}")
    file_value = record.get("File")
    file = resolve_export_path(
        manifest_path.parent / _text(file_value, f"{label}.File").replace("\\", "/")
    ) if file_value is not None else None
    candidates = target.setdefault(key, [])
    for prior in candidates:
        if _source_json(_export_identity(prior[0])) != _source_json(_export_identity(record)):
            raise ValueError(f"WATER_MATERIAL_EXPORT_CONFLICT kind={label} ref={key}")
    candidates.append((record, manifest_path.parent, file))


def _export_identity(record: dict[str, object]) -> dict[str, object]:
    identity = {
        key: value for key, value in record.items()
        if key not in ("File", "Error", "MipByteLengths")
    }
    asset = _asset(identity.get("Asset"), "export identity Asset")
    identity["Asset"] = (asset.source.casefold(), asset.path_id, asset.type_name)
    if "Slices" in identity:
        identity["Slices"] = [
            {
                "Asset": reference_key(_object(layer, "array slice").get("Asset")),
                "SliceIndex": _integer(
                    _object(layer, "array slice").get("SliceIndex"), "array slice index"
                ),
            }
            for layer in _array(identity["Slices"], "array slices")
        ]
    return identity


def _load_catalog(
    scene_manifests: tuple[Path, ...],
) -> tuple[
    dict[str, set[tuple[str, int]]],
    dict[tuple[str, int], set[str]],
    dict[tuple[str, int], dict[str, object]],
    dict[tuple[str, int], dict[str, object]],
    dict[tuple[str, int], dict[str, object]],
    dict[tuple[str, int], list[ExportCandidate]],
    dict[tuple[str, int], list[ExportCandidate]],
]:
    containers: dict[str, set[tuple[str, int]]] = defaultdict(set)
    paths_by_ref: dict[tuple[str, int], set[str]] = defaultdict(set)
    globals_by_ref: dict[tuple[str, int], dict[str, object]] = {}
    configs_by_ref: dict[tuple[str, int], dict[str, object]] = {}
    sectors_by_ref: dict[tuple[str, int], dict[str, object]] = {}
    textures: dict[tuple[str, int], list[ExportCandidate]] = {}
    arrays: dict[tuple[str, int], list[ExportCandidate]] = {}
    for manifest_path in scene_manifests:
        if not manifest_path.is_file():
            raise FileNotFoundError(f"WATER_MATERIAL_MANIFEST_MISSING path={manifest_path}")
        manifest = _object(json.loads(manifest_path.read_text(encoding="utf-8-sig")), str(manifest_path))
        if manifest.get("Format") != "EndfieldSceneProbe/1":
            raise ValueError(f"WATER_MATERIAL_MANIFEST_FORMAT_INVALID path={manifest_path}")
        water = _object(manifest.get("Water", {}), f"{manifest_path}.Water")
        for field, target in (
            ("GlobalConfigs", globals_by_ref),
            ("Configs", configs_by_ref),
            ("Sectors", sectors_by_ref),
        ):
            for value in _array(water.get(field, []), f"{manifest_path}.Water.{field}"):
                _insert_record(target, value, field)
        for value in _array(manifest.get("Containers"), f"{manifest_path}.Containers"):
            container = _object(value, "water container")
            path = _text(container.get("Container"), "water container path").replace("\\", "/")
            asset = _asset(container.get("Asset"), "water container Asset")
            key = asset.source.casefold(), asset.path_id
            containers[path.casefold()].add(key)
            paths_by_ref[key].add(path)
        for value in _array(manifest.get("TextureExports"), f"{manifest_path}.TextureExports"):
            _insert_export(textures, value, manifest_path, "TextureExports")
        for value in _array(manifest.get("TextureArrayExports", []), "TextureArrayExports"):
            _insert_export(arrays, value, manifest_path, "TextureArrayExports")
    return containers, paths_by_ref, globals_by_ref, configs_by_ref, sectors_by_ref, textures, arrays


def _unique_path(paths: set[str], label: str) -> str:
    unique = {path.casefold(): path for path in paths}
    if len(unique) != 1:
        raise ValueError(f"WATER_MATERIAL_PATH_AMBIGUOUS label={label} paths={sorted(paths)}")
    return next(iter(unique.values()))


def _texture_provider(
    asset: WaterAssetRef,
    paths_by_ref: dict[tuple[str, int], set[str]],
    owner_asset: WaterAssetRef,
    owner_path: str,
) -> tuple[str | None, str | None]:
    paths = {path.casefold(): path for path in paths_by_ref.get(
        (asset.source.casefold(), asset.path_id), set()
    )}
    if len(paths) > 1:
        return None, "texture_container_ambiguous"
    if paths:
        return next(iter(paths.values())), None
    if asset.source.casefold() == owner_asset.source.casefold():
        return owner_path, None
    return None, "texture_container_missing"


def _flow_core(config: dict[str, object]) -> WaterFlowCore:
    flow = _object(config.get("Flow"), "water config Flow")
    appearance = _object(config.get("Appearance"), "water config Appearance")
    direction = _object(flow.get("FlowDirectionVector2"), "water FlowDirectionVector2")
    use_flow = _integer(flow.get("UseFlowMap"), "UseFlowMap")
    if use_flow not in (0, 1):
        raise ValueError(f"WATER_USE_FLOW_MAP_INVALID value={use_flow}")
    return WaterFlowCore(
        bool(use_flow),
        _integer(flow.get("MaterialIndex"), "MaterialIndex"),
        _number(flow.get("FlowDirection"), "FlowDirection"),
        (_number(direction.get("X"), "FlowDirectionVector2.X"),
         _number(direction.get("Y"), "FlowDirectionVector2.Y")),
        _integer(flow.get("SmallWaveNormalType"), "SmallWaveNormalType"),
        _integer(flow.get("LargeWaveNormalType"), "LargeWaveNormalType"),
        _number(flow.get("FlowSpeed"), "FlowSpeed"),
        _number(flow.get("LargeWaveFlowSpeed"), "LargeWaveFlowSpeed"),
        _number(flow.get("NormalTilling"), "NormalTilling"),
        _number(flow.get("LargeWaveNormalTilling"), "LargeWaveNormalTilling"),
        _number(flow.get("NormalScale"), "NormalScale"),
        _number(flow.get("LargeWaveNormalScale"), "LargeWaveNormalScale"),
        _number(appearance.get("WaterIor"), "WaterIor"),
    )


def _texture(
    asset: WaterAssetRef,
    exports: dict[tuple[str, int], list[ExportCandidate]],
) -> WaterTextureBinding | None:
    available: list[Path] = []
    for record, _, file in exports.get((asset.source.casefold(), asset.path_id), []):
        exported = _asset(record.get("Asset"), "TextureExports.Asset")
        if exported.type_name != asset.type_name:
            raise ValueError(f"WATER_TEXTURE_TYPE_CONFLICT asset={asset} export={exported}")
        if not record.get("Error") and file is not None and file.is_file():
            available.append(file)
    if not available:
        return None
    _assert_matching_payloads(available, f"Texture2D ref={asset}")
    return WaterTextureBinding(asset, str(min(available, key=lambda file: str(file).casefold())))


def _assert_matching_payloads(files: list[Path], label: str) -> None:
    if len(files) < 2:
        return
    digests: set[str] = set()
    for file in files:
        with file.open("rb") as stream:
            digests.add(hashlib.file_digest(stream, "sha256").hexdigest())
    if len(digests) != 1:
        raise ValueError(f"WATER_MATERIAL_EXPORT_PAYLOAD_CONFLICT label={label} files={files}")


def _slice_layer(
    record: dict[str, object],
    asset: WaterAssetRef,
    slice_index: int,
) -> dict[str, object]:
    exported = _asset(record.get("Asset"), "TextureArrayExports.Asset")
    if exported.type_name != asset.type_name:
        raise ValueError(f"WATER_NORMAL_ARRAY_TYPE_CONFLICT asset={asset} export={exported}")
    depth = _integer(record.get("Depth"), "TextureArrayExports.Depth")
    if depth <= 0 or not 0 <= slice_index < depth:
        raise ValueError(
            f"WATER_NORMAL_SLICE_OUT_OF_RANGE ref={asset} index={slice_index} depth={depth}"
        )
    slices = _array(record.get("Slices"), "TextureArrayExports.Slices")
    if len(slices) != depth:
        raise ValueError(f"WATER_NORMAL_SLICE_COUNT_INVALID ref={asset} depth={depth}")
    indexed: dict[int, dict[str, object]] = {}
    for value in slices:
        layer = _object(value, "TextureArrayExports.Slices item")
        index = _integer(layer.get("SliceIndex"), "SliceIndex")
        if not 0 <= index < depth or index in indexed:
            raise ValueError(f"WATER_NORMAL_SLICE_INDEX_INVALID ref={asset} index={index}")
        if reference_key(layer.get("Asset")) != (asset.source.casefold(), asset.path_id):
            raise ValueError(f"WATER_NORMAL_SLICE_ASSET_MISMATCH ref={asset} index={index}")
        indexed[index] = layer
    return indexed[slice_index]


def _normal_slice(
    asset: WaterAssetRef,
    slice_index: int,
    exports: dict[tuple[str, int], list[ExportCandidate]],
) -> WaterNormalSlice | None:
    available: list[Path] = []
    for record, directory, _ in exports.get((asset.source.casefold(), asset.path_id), []):
        if record.get("Error"):
            continue
        layer = _slice_layer(record, asset, slice_index)
        file_value = layer.get("File")
        if layer.get("Error") or file_value is None:
            continue
        file = resolve_export_path(
            directory / _text(file_value, "TextureArrayExports.Slices.File").replace("\\", "/")
        )
        if file.is_file():
            available.append(file)
    if not available:
        return None
    _assert_matching_payloads(available, f"Texture2DArray ref={asset} slice={slice_index}")
    return WaterNormalSlice(
        asset, slice_index, str(min(available, key=lambda file: str(file).casefold()))
    )


def _sector_candidates(
    global_record: dict[str, object],
    bounds: tuple[float, float, float, float, float, float],
) -> tuple[tuple[str, int, int], ...]:
    """Apply native floor coordinates and x-major sector index ordering."""
    if len(bounds) != 6 or any(not math.isfinite(value) for value in bounds) or \
            bounds[0] > bounds[1] or bounds[4] > bounds[5]:
        raise ValueError(f"WATER_WORLD_BOUNDS_INVALID bounds={bounds}")
    use_sector = _integer(global_record.get("UseSectorData"), "UseSectorData")
    if use_sector not in (0, 1):
        raise ValueError(f"WATER_USE_SECTOR_DATA_INVALID value={use_sector}")
    if use_sector == 0:
        return ()
    map_size = _integer(global_record.get("MapSize"), "MapSize")
    sector_size = _integer(global_record.get("SectorSize"), "SectorSize")
    if map_size <= 0 or sector_size <= 0 or map_size % sector_size:
        raise ValueError(f"WATER_SECTOR_GRID_INVALID map_size={map_size} sector_size={sector_size}")
    side = map_size // sector_size
    if side % 2:
        raise ValueError(f"WATER_SECTOR_GRID_ODD side={side}")
    center = _object(global_record.get("SceneCenterOffset"), "SceneCenterOffset")
    offset_x = _number(center.get("X"), "SceneCenterOffset.X")
    offset_z = _number(center.get("Y"), "SceneCenterOffset.Y")
    paths = _array(global_record.get("SectorDataPaths"), "SectorDataPaths")
    exists = _array(global_record.get("SectorDataExists"), "SectorDataExists")
    hashes = _array(global_record.get("SectorDataAssetHashes"), "SectorDataAssetHashes")
    if len(paths) != side * side or len(exists) != len(paths) or len(hashes) != len(paths):
        raise ValueError(f"WATER_SECTOR_GRID_LENGTH_INVALID side={side} paths={len(paths)}")
    expected = {(x, z) for x in range(-side // 2, side // 2)
                for z in range(-side // 2, side // 2)}
    found: set[tuple[int, int]] = set()
    selected: list[tuple[str, int, int]] = []
    scene_path = _text(global_record.get("ScenePath"), "ScenePath").rstrip("/")
    for index, value in enumerate(paths):
        path = _text(value, f"SectorDataPaths[{index}]").replace("\\", "/")
        if not path.casefold().startswith((scene_path + "/WaterData/Sector/").casefold()):
            raise ValueError(f"WATER_SECTOR_PATH_OUTSIDE_SCENE path={path} scene={scene_path}")
        match = re.fullmatch(r"T_(-?\d+)_(-?\d+)\.asset", path.rsplit("/", 1)[-1], re.IGNORECASE)
        if match is None:
            raise ValueError(f"WATER_SECTOR_COORDINATES_MISSING path={path}")
        x, z = int(match.group(1)), int(match.group(2))
        expected_x = index // side - side // 2
        expected_z = index % side - side // 2
        if (x, z) != (expected_x, expected_z):
            raise ValueError(
                f"WATER_SECTOR_INDEX_ORDER_INVALID path={path} index={index} "
                f"expected=({expected_x},{expected_z}) actual=({x},{z})"
            )
        if (x, z) in found:
            raise ValueError(f"WATER_SECTOR_COORDINATES_DUPLICATE path={path}")
        found.add((x, z))
        present = exists[index]
        if type(present) is not bool:
            raise ValueError(f"WATER_SECTOR_EXISTS_INVALID path={path} value={present}")
        asset_hash = _integer(hashes[index], f"SectorDataAssetHashes[{index}]")
        if present != (asset_hash != -1):
            raise ValueError(f"WATER_SECTOR_HASH_EXISTS_MISMATCH path={path} hash={asset_hash}")
        if not present:
            continue
        xmin = x * sector_size + offset_x
        zmin = z * sector_size + offset_z
        if xmin < bounds[1] and xmin + sector_size > bounds[0] and \
                zmin < bounds[5] and zmin + sector_size > bounds[4]:
            selected.append((path, x, z))
    if found != expected:
        raise ValueError(f"WATER_SECTOR_GRID_COORDINATES_INVALID missing={len(expected - found)}")
    return tuple(selected)


def build_water_material_plan(
    scene_manifests: tuple[Path, ...],
    rows: tuple[WaterGeometryRow, ...],
    map_id: str,
) -> WaterMaterialPlan:
    """Bind exact water records, exported textures, and intersecting sectors."""
    if map_id not in ("map01", "map02"):
        raise ValueError(f"WATER_MATERIAL_MAP_UNSUPPORTED map_id={map_id}")
    containers, paths_by_ref, globals_by_ref, configs, sectors, textures, arrays = _load_catalog(
        scene_manifests
    )
    scene_path = f"Assets/Beyond/Scenes/LunaScenes/{map_id.capitalize()}/ReleaseData"
    matches = [
        record for record in globals_by_ref.values()
        if _text(record.get("ScenePath"), "GlobalConfig.ScenePath").casefold() == scene_path.casefold()
    ]
    if not matches:
        candidates = [
            path for path in containers
            if path.startswith((scene_path + "/").casefold())
            and path.endswith("/waterdata/global_water_config.asset")
        ]
        if len(candidates) > 1:
            raise ValueError(
                f"WATER_GLOBAL_CONTAINER_CONFLICT map_id={map_id} paths={sorted(candidates)}"
            )
        global_path = candidates[0] if candidates else None
        required = {row.config_resource_path for row in rows}
        if global_path is not None:
            required.add(global_path)
        return WaterMaterialPlan(
            (),
            tuple(WaterMaterialPending(
                row.entity_id, global_path, "global_record_missing",
                f"Water.GlobalConfigs ScenePath={scene_path}",
            ) for row in rows),
            tuple(sorted(required, key=str.casefold)),
        )
    if len(matches) != 1:
        raise ValueError(f"WATER_GLOBAL_CONFIG_AMBIGUOUS map_id={map_id} count={len(matches)}")
    global_record = matches[0]
    global_asset = _record_asset(global_record, "GlobalConfig")
    global_key = global_asset.source.casefold(), global_asset.path_id
    global_path = _unique_path(paths_by_ref.get(global_key, set()), "GlobalConfig")
    flow_asset = _pointer(global_record.get("FlowMap"), "GlobalConfig.FlowMap", "Texture2D")
    array_asset = _pointer(
        global_record.get("NormalMapArray"), "GlobalConfig.NormalMapArray", "Texture2DArray"
    )
    array_key = array_asset.source.casefold(), array_asset.path_id
    array_paths = paths_by_ref.get(array_key, set())
    if not array_paths:
        return WaterMaterialPlan((), tuple(WaterMaterialPending(
            row.entity_id, global_path, "normal_array_container_missing",
            f"GlobalConfig dependency closure must include NormalMapArray.Target={array_asset}",
        ) for row in rows), tuple(sorted(
            {global_path, *(row.config_resource_path for row in rows)}, key=str.casefold)))
    array_path = _unique_path(array_paths, "NormalMapArray")
    global_source_json = _source_json(global_record)
    required: set[str] = {global_path, array_path}
    flow_resolution: tuple[WaterTextureBinding | None, str | None, str | None] | None = None
    ready: list[WaterMaterialRow] = []
    pending: list[WaterMaterialPending] = []
    for row in rows:
        required.add(row.config_resource_path)
        config_refs = containers.get(row.config_resource_path.casefold(), set())
        if len(config_refs) > 1:
            raise ValueError(
                f"WATER_CONFIG_CONTAINER_CONFLICT path={row.config_resource_path} refs={sorted(config_refs)}"
            )
        if not config_refs:
            pending.append(WaterMaterialPending(
                row.entity_id, row.config_resource_path, "config_reference_missing",
                "No exact Water.Configs Container reference",
            ))
            continue
        config_ref = next(iter(config_refs))
        config = configs.get(config_ref)
        if config is None:
            pending.append(WaterMaterialPending(
                row.entity_id, row.config_resource_path, "config_record_missing",
                f"Water.Configs Header.Asset={config_ref}",
            ))
            continue
        config_asset = _record_asset(config, "WaterConfig")
        flow = _flow_core(config)
        small = _normal_slice(array_asset, flow.small_wave_normal_type, arrays)
        large = _normal_slice(array_asset, flow.large_wave_normal_type, arrays)
        core_failures: list[WaterMaterialPending] = []
        flow_map: WaterTextureBinding | None = None
        if flow.use_flow_map:
            if flow_resolution is None:
                provider, issue = _texture_provider(
                    flow_asset, paths_by_ref, global_asset, global_path
                )
                flow_resolution = _texture(flow_asset, textures), provider, issue
            flow_map, flow_provider, flow_provider_issue = flow_resolution
            if flow_provider is not None:
                required.add(flow_provider)
            if flow_provider_issue is not None:
                core_failures.append(WaterMaterialPending(
                    row.entity_id, None, flow_provider_issue,
                    f"GlobalConfig.FlowMap.Target={flow_asset}",
                ))
            if flow_map is None:
                core_failures.append(WaterMaterialPending(
                    row.entity_id, flow_provider, "global_flow_map_payload_missing",
                    f"GlobalConfig.FlowMap.Target={flow_asset}",
                ))
        if small is None:
            core_failures.append(WaterMaterialPending(
                row.entity_id, array_path, "small_wave_normal_slice_missing",
                f"NormalMapArray.Target={array_asset} slice={flow.small_wave_normal_type}",
            ))
        if large is None:
            core_failures.append(WaterMaterialPending(
                row.entity_id, array_path, "large_wave_normal_slice_missing",
                f"NormalMapArray.Target={array_asset} slice={flow.large_wave_normal_type}",
            ))
        sector_bindings: list[WaterSectorBinding] = []
        sector_failures: list[WaterMaterialPending] = []
        sector_candidates = _sector_candidates(global_record, row.world_bounds) if flow.use_flow_map else ()
        for sector_path, sector_x, sector_z in sector_candidates:
            required.add(sector_path)
            sector_refs = containers.get(sector_path.casefold(), set())
            if len(sector_refs) > 1:
                raise ValueError(
                    f"WATER_SECTOR_CONTAINER_CONFLICT path={sector_path} refs={sorted(sector_refs)}"
                )
            if not sector_refs or next(iter(sector_refs)) not in sectors:
                sector_failures.append(WaterMaterialPending(
                    row.entity_id, sector_path, "sector_record_missing",
                    f"Exact Container references={len(sector_refs)}",
                ))
                continue
            sector = sectors[next(iter(sector_refs))]
            sector_asset = _record_asset(sector, "WaterSector")
            texture_asset = _pointer(
                sector.get("WaterSectorTexture0"), "WaterSector.WaterSectorTexture0", "Texture2D"
            )
            texture_provider, texture_provider_issue = _texture_provider(
                texture_asset, paths_by_ref, sector_asset, sector_path
            )
            if texture_provider is not None:
                required.add(texture_provider)
            if texture_provider_issue is not None:
                sector_failures.append(WaterMaterialPending(
                    row.entity_id, None, texture_provider_issue,
                    f"WaterSectorTexture0.Target={texture_asset}",
                ))
            sector_texture = _texture(texture_asset, textures)
            if sector_texture is None:
                sector_failures.append(WaterMaterialPending(
                    row.entity_id, texture_provider, "sector_texture_payload_missing",
                    f"WaterSectorTexture0.Target={texture_asset}",
                ))
                continue
            if texture_provider_issue is not None:
                continue
            sector_bindings.append(WaterSectorBinding(
                sector_path, sector_x, sector_z, sector_asset, sector_texture, _source_json(sector)
            ))
        pending.extend(core_failures)
        pending.extend(sector_failures)
        if core_failures:
            continue
        if (flow.use_flow_map and flow_map is None) or small is None or large is None:
            raise RuntimeError(f"WATER_MATERIAL_CORE_BINDING_INCONSISTENT entity_id={row.entity_id}")
        ready.append(WaterMaterialRow(
            row.entity_id, row.config_resource_path, config_asset, global_path, global_asset,
            array_path, flow, flow_map, small, large, tuple(sector_bindings),
            not sector_failures, "native_grid_verified", _source_json(config), global_source_json,
        ))
    return WaterMaterialPlan(
        tuple(sorted(ready, key=lambda item: item.entity_id)),
        tuple(sorted(pending, key=lambda item: (item.entity_id, item.resource_path or "", item.reason))),
        tuple(sorted(required, key=str.casefold)),
    )
