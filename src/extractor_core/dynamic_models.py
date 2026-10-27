"""Read authored DynamicSceneModel prefab placements from streamed scene grids."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import struct
from typing import Callable

from endfield_scene_io import SceneSourceCacheSlot, cached_scene_source_value
from . import flatbuffer as fb
from .dynamic_renderers import _read_source
from .effects import (
    GROUP_HASH, MAIN_FILE, EffectDecodeError, _dynamic_inventory_qualifiers,
    _scene_inventory_sources, _require_scene_inventory_sources, _require_static_only_scene, _resolve_hashes, _vector,
)
from .resources import load_group, validate_vfs_root
from .scene_identity import validate_scene_id


MODEL_STRIDE = 96


class DynamicModelDecodeError(ValueError):
    """A prefab placement or its exact source path cannot be decoded."""


@dataclass(frozen=True)
class DynamicModelSource:
    virtual_path: str
    data_md5: str
    decoded_sha256: str
    byte_length: int
    model_count: int


@dataclass(frozen=True)
class _ModelPlacement:
    key: str
    source_virtual_path: str
    grid_index: int
    model_index: int
    record_offset: int
    guid_words: tuple[int, int, int, int]
    path_hash_hex: str
    position: tuple[float, float, float]
    rotation_euler_degrees: tuple[float, float, float]
    scale: tuple[float, float, float]
    grid_chain: tuple[int, int, int]
    visibility_center: tuple[float, float, float]
    base_radius: float
    min_distance: float
    importance: int
    raw_record_hex: str


@dataclass(frozen=True)
class DynamicModelRecord(_ModelPlacement):
    prefab_path: str


@dataclass(frozen=True)
class DynamicModelInventory:
    scene_id: str
    sources: tuple[DynamicModelSource, ...]
    records: tuple[DynamicModelRecord, ...]


_MODEL_INVENTORY_CACHE = SceneSourceCacheSlot(
    "dynamic-scene-models", DynamicModelInventory, 4, 32 * 1024 * 1024,
)


def _decode_model_file(data: bytes, virtual_path: str) -> tuple[_ModelPlacement, ...]:
    """Decode the 96-byte FBDynamicSceneModel struct; VisInfo is not mesh bounds."""
    if len(data) < 8:
        raise DynamicModelDecodeError(f"Model source is too short: path={virtual_path}, bytes={len(data)}")
    rows: list[_ModelPlacement] = []
    try:
        grids, grid_count = _vector(data, fb.u32(data, 0), 3, 4)
        if grids == 0:
            raise DynamicModelDecodeError(f"Model source grid field is missing: path={virtual_path}")
        for grid_index in range(grid_count):
            grid = fb.table_vector_element(data, grids, grid_index)
            models, model_count = _vector(data, grid, 7, MODEL_STRIDE)
            for index in range(model_count):
                offset = models + MODEL_STRIDE * index
                guid = struct.unpack_from("<4I", data, offset)
                position = struct.unpack_from("<3f", data, offset + 16)
                rotation = struct.unpack_from("<3f", data, offset + 28)
                scale = struct.unpack_from("<3f", data, offset + 40)
                chain = struct.unpack_from("<3I", data, offset + 52)
                center = struct.unpack_from("<3f", data, offset + 72)
                radius, distance, importance = struct.unpack_from("<ffi", data, offset + 84)
                if not all(math.isfinite(value) for value in (*position, *rotation, *scale, *center, radius, distance)):
                    raise DynamicModelDecodeError(
                        f"Model transform/visibility is non-finite: path={virtual_path}, grid={grid_index}, record={index}"
                    )
                if radius < 0 or distance < 0:
                    raise DynamicModelDecodeError(
                        f"Model visibility radius/distance is negative: path={virtual_path}, grid={grid_index}, record={index}"
                    )
                rows.append(_ModelPlacement(
                    f"{virtual_path}:model:{grid_index}:{index}", virtual_path, grid_index, index, offset,
                    guid, data[offset + 64:offset + 72].hex(), position, rotation, scale, chain, center,
                    radius, distance, importance, data[offset:offset + MODEL_STRIDE].hex(),
                ))
    except (EffectDecodeError, struct.error) as error:
        raise DynamicModelDecodeError(f"Model FlatBuffer decode failed: path={virtual_path}, error={error}") from error
    return tuple(rows)


def _decode_scene_models(
    root: Path, path_hash: Path, scene: str, include: Callable[[_ModelPlacement], bool],
    check_cancelled: Callable[[], None],
) -> DynamicModelInventory:
    group = load_group(root, GROUP_HASH)
    prefix = f"data/dynamicstreaming/pc/scene/{scene}/fb_main_"
    candidates = sorted((item for item in group.iter_files() if item.file_name.casefold().startswith(prefix)),
                        key=lambda item: item.file_name.casefold())
    if not candidates:
        try:
            _require_static_only_scene(root, scene, check_cancelled)
        except EffectDecodeError as error:
            raise DynamicModelDecodeError(f"Model main sources missing: scene={scene}, error={error}") from error
        return DynamicModelInventory(scene, (), ())
    if len({item.file_name.casefold() for item in candidates}) != len(candidates):
        raise DynamicModelDecodeError(f"Duplicate model main paths: scene={scene}, group={GROUP_HASH}")
    sources: list[DynamicModelSource] = []
    selected: list[_ModelPlacement] = []
    for item in candidates:
        check_cancelled()
        if MAIN_FILE.fullmatch(item.file_name) is None:
            raise DynamicModelDecodeError(f"Model main filename differs: path={item.file_name}")
        payload, digest = _read_source(root, group, item)
        rows = _decode_model_file(payload, item.file_name)
        sources.append(DynamicModelSource(item.file_name, digest, hashlib.sha256(payload).hexdigest(), len(payload), len(rows)))
        selected.extend(row for row in rows if include(row))
    wanted = frozenset(bytes.fromhex(row.path_hash_hex) for row in selected)
    try:
        paths = _resolve_hashes(path_hash, wanted, check_cancelled, ("assets/", "packages/")) if wanted else {}
    except EffectDecodeError as error:
        raise DynamicModelDecodeError(f"Model prefab path resolution failed: scene={scene}, error={error}") from error
    result: list[DynamicModelRecord] = []
    for row in selected:
        prefab = paths[bytes.fromhex(row.path_hash_hex)]
        if not prefab.casefold().endswith(".prefab"):
            raise DynamicModelDecodeError(f"Model path is not a prefab: source={row.key}, path={prefab}")
        result.append(DynamicModelRecord(
            row.key, row.source_virtual_path, row.grid_index, row.model_index, row.record_offset, row.guid_words,
            row.path_hash_hex, row.position, row.rotation_euler_degrees, row.scale, row.grid_chain,
            row.visibility_center, row.base_radius, row.min_distance, row.importance, row.raw_record_hex, prefab,
        ))
    return DynamicModelInventory(scene, tuple(sources), tuple(result))


def read_dynamic_scene_models_for_bounds(
    vfs_root: Path, string_path_hash: Path, scene_id: str, bounds: tuple[float, float, float, float],
    check_cancelled: Callable[[], None],
) -> DynamicModelInventory:
    """Select authored anchors with half-open ownership; preserve runtime visibility as evidence."""
    check_cancelled()
    if (len(bounds) != 4 or any(type(value) not in (int, float) or not math.isfinite(value) for value in bounds)
            or bounds[0] >= bounds[1] or bounds[2] >= bounds[3]):
        raise ValueError(f"Model selection bounds are invalid: bounds={bounds}")
    xmin, xmax, zmin, zmax = bounds
    return _read_selected_models(vfs_root, string_path_hash, scene_id, ("anchor-bounds", *(str(value) for value in bounds)),
        lambda row: xmin <= row.position[0] < xmax and zmin <= row.position[2] < zmax, check_cancelled)


def read_dynamic_scene_models(
    vfs_root: Path, string_path_hash: Path, scene_id: str, check_cancelled: Callable[[], None],
) -> DynamicModelInventory:
    """Read all authored candidates so geometry selection cannot lose exterior anchors."""
    return _read_selected_models(vfs_root, string_path_hash, scene_id, ("all-authored-models",), lambda row: True, check_cancelled)


def _read_selected_models(
    vfs_root: Path, string_path_hash: Path, scene_id: str, selection_key: tuple[str, ...],
    include: Callable[[_ModelPlacement], bool], check_cancelled: Callable[[], None],
) -> DynamicModelInventory:
    check_cancelled()
    scene = validate_scene_id(scene_id)
    root = validate_vfs_root(vfs_root)
    path_hash = string_path_hash.expanduser().resolve()
    if not path_hash.is_file():
        raise FileNotFoundError(f"Model StringPathHash source is missing: path={path_hash}")
    sources = _scene_inventory_sources(root, path_hash, (scene,))

    def decode() -> DynamicModelInventory:
        result = _decode_scene_models(root, path_hash, scene, include, check_cancelled)
        _require_scene_inventory_sources(root, path_hash, (scene,), sources)
        return result

    qualifiers = _dynamic_inventory_qualifiers((scene, *selection_key), sources)
    result = cached_scene_source_value(_MODEL_INVENTORY_CACHE, sources, qualifiers, decode)
    check_cancelled()
    return result
