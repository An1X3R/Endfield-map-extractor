"""Read referenced DynamicSceneEcs groups without inventing static entity IDs."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Callable, Literal

from . import flatbuffer as fb
from . import vfs
from .effects import (GROUP_HASH, MAIN_FILE, EffectDecodeError, _dynamic_inventory_qualifiers, _dynamic_inventory_sources,
                      _require_dynamic_inventory_sources, _resolve_hashes, _vector)
from .lz4 import decompress_lz4_inv
from .resources import load_group, validate_vfs_root
from .scene_identity import validate_scene_id
from extract_scene_renderer_materials import RendererDraw, decode_draws, decode_renderer_pair_paths, serialized_draw_parameters
from endfield_scene_io import SceneSourceCacheSlot, cached_scene_source_value


INIT_FILE = re.compile(r"^Data/DynamicStreaming/PC/Scene/([^/]+)/fb_init_(\d+)_(\d+)\.bytes$", re.IGNORECASE)


class DynamicRendererDecodeError(ValueError):
    """A referenced dynamic renderer source violates the audited layout."""


@dataclass(frozen=True)
class DynamicRendererSource:
    virtual_path: str
    data_md5: str
    byte_length: int
    decoded_byte_length: int
    decoded_sha256: str
    kind: Literal["main", "init"]


@dataclass(frozen=True)
class DynamicEcsReference:
    key: str
    source_virtual_path: str
    grid_index: int
    reference_index: int
    record_offset: int
    chunk_id: int
    group_index: int
    center: tuple[float, float, float]
    radius: float
    min_distance: float
    importance: int
    scene_index: int


@dataclass(frozen=True)
class DynamicRendererGroup:
    source_virtual_path: str
    chunk_id: int
    init_scene_index: int
    group_index: int
    record_count: int
    descriptors: tuple[tuple[int, int, int], ...]
    blob_offset: int
    blob_length: int
    reference_keys: tuple[str, ...]
    status: Literal["renderer", "no_renderer_references"]


@dataclass(frozen=True)
class DynamicRendererPair:
    mesh_path: str
    material_path: str
    mesh_hash_hex: str
    material_hash_hex: str
    reference_offset: int
    draw: RendererDraw
    reference_record_hex: str
    runtime_record_hex: str


@dataclass(frozen=True)
class DynamicRendererRecord:
    key: str
    source_virtual_path: str
    chunk_id: int
    init_scene_index: int
    group_index: int
    record_index: int
    reference_keys: tuple[str, ...]
    name: str | None
    matrix: tuple[float, ...]
    bounds: tuple[float, ...] | None
    serialized_draw_parameters: tuple[float, ...]
    common_instance_offset: int
    renderer_reference_offset: int
    runtime_draw_offset: int
    pairs: tuple[DynamicRendererPair, ...]


@dataclass(frozen=True)
class DynamicSceneRendererInventory:
    scene_id: str
    sources: tuple[DynamicRendererSource, ...]
    references: tuple[DynamicEcsReference, ...]
    groups: tuple[DynamicRendererGroup, ...]
    records: tuple[DynamicRendererRecord, ...]


_RENDERER_INVENTORY_CACHE = SceneSourceCacheSlot(
    "dynamic-scene-renderers", DynamicSceneRendererInventory, 4, 144 * 1024 * 1024
)


@dataclass(frozen=True)
class _RawRenderer:
    group: DynamicRendererGroup
    record_index: int
    name: str | None
    common: bytes
    references: bytes
    runtime: bytes
    common_offset: int
    references_offset: int
    runtime_offset: int
    bounds: tuple[float, ...] | None


@dataclass(frozen=True)
class _RawDynamicSceneRendererInventory:
    scene_id: str
    sources: tuple[DynamicRendererSource, ...]
    references: tuple[DynamicEcsReference, ...]
    groups: tuple[DynamicRendererGroup, ...]
    records: tuple[_RawRenderer, ...]


def _read_source(root: Path, group: vfs.VfsGroup, item: vfs.VfsFile) -> tuple[bytes, str]:
    output = BytesIO()
    chunk = root / GROUP_HASH / f"{item.file_chunk_md5_name}.chk"
    with chunk.open("rb") as source:
        digest = vfs.copy_virtual_file(source, output, item, group.version, bytes.fromhex(vfs.DEFAULT_VFS_KEY_HEX))
    if digest != item.file_data_md5.upper():
        raise DynamicRendererDecodeError(
            f"Dynamic ECS VFS checksum mismatch: path={item.file_name}, expected={item.file_data_md5}, actual={digest}"
        )
    return output.getvalue(), digest


def _main_references(data: bytes, path: str) -> tuple[DynamicEcsReference, ...]:
    if len(data) < 8:
        raise DynamicRendererDecodeError(f"Dynamic ECS main is too short: path={path}, bytes={len(data)}")
    grids, count = _vector(data, fb.u32(data, 0), 3, 4)
    if not grids:
        raise DynamicRendererDecodeError(f"Dynamic ECS main has no grid field: path={path}")
    rows: list[DynamicEcsReference] = []
    for grid_index in range(count):
        vector, nrows = _vector(data, fb.table_vector_element(data, grids, grid_index), 9, 36)
        for index in range(nrows):
            offset = vector + index * 36
            chunk, group_index, x, y, z, radius, distance, importance, scene_index = struct.unpack_from("<ii5fii", data, offset)
            if min(chunk, group_index, scene_index) < 0 or not all(math.isfinite(v) for v in (x, y, z, radius, distance)):
                raise DynamicRendererDecodeError(f"Invalid Dynamic ECS reference: path={path}, grid={grid_index}, record={index}")
            if radius < 0 or distance < 0:
                raise DynamicRendererDecodeError(f"Negative Dynamic ECS reference radius/distance: path={path}, offset={offset}")
            rows.append(DynamicEcsReference(
                f"{path}:{grid_index}:{index}", path, grid_index, index, offset,
                chunk, group_index, (x, y, z), radius, distance, importance, scene_index,
            ))
    return tuple(rows)


def _selected_group(
    data: bytes, path: str, chunk_id: int, init_scene_index: int,
    group_index: int, references: tuple[DynamicEcsReference, ...],
) -> tuple[DynamicRendererGroup, tuple[_RawRenderer, ...]]:
    groups, count = _vector(data, fb.u32(data, 0), 7, 4)
    if group_index >= count:
        raise DynamicRendererDecodeError(f"Dynamic ECS group index exceeds init groups: path={path}, group={group_index}, count={count}")
    table = fb.table_vector_element(data, groups, group_index)
    descriptors, descriptor_count = _vector(data, table, 3, 8)
    count_at = fb.field_address(data, table, 1)
    blob_at = fb.field_address(data, table, 4)
    if count_at is None or blob_at is None or count_at + 4 > len(data):
        raise DynamicRendererDecodeError(f"Dynamic ECS group lacks count/blob: path={path}, group={group_index}")
    nrows = fb.u32(data, count_at)
    blob, length = _vector(data, fb.offset_target(data, blob_at), 0, 1)
    decoded = tuple(struct.unpack_from("<HHI", data, descriptors + 8 * i) for i in range(descriptor_count))
    columns: dict[int, tuple[int, int]] = {}
    cursor = blob
    for kind, stride, reserved in decoded:
        if reserved or stride == 0 or kind in columns:
            raise DynamicRendererDecodeError(f"Invalid Dynamic ECS descriptor: path={path}, group={group_index}, descriptor={(kind, stride, reserved)}")
        expected = None
        if 68 <= kind <= 74:
            expected = 8 + 40 * (1 << (kind - 68))
        elif 8 <= kind <= 14:
            expected = 4 + 32 * (1 << (kind - 8))
        elif kind == 18:
            expected = 256
        if expected is not None and stride != expected:
            raise DynamicRendererDecodeError(f"Dynamic ECS component stride differs: path={path}, group={group_index}, kind={kind}, stride={stride}, expected={expected}")
        columns[kind] = cursor, stride
        cursor += nrows * stride
    if cursor != blob + length:
        raise DynamicRendererDecodeError(f"Dynamic ECS blob length differs: path={path}, group={group_index}, columnsLength={cursor - blob}, blobLength={length}")
    ref_kinds = tuple(k for k in columns if 68 <= k <= 74)
    group = DynamicRendererGroup(path, chunk_id, init_scene_index, group_index, nrows, decoded, blob, length,
                                 tuple(r.key for r in references), "renderer" if ref_kinds else "no_renderer_references")
    if not ref_kinds:
        return group, ()
    if len(ref_kinds) != 1 or 18 not in columns or ref_kinds[0] - 60 not in columns:
        raise DynamicRendererDecodeError(f"Dynamic ECS renderer lacks one matching draw/CommonInstanceData column: path={path}, group={group_index}, kinds={tuple(columns)}")
    for kind, expected in ((21, 64), *((i, 4) for i in range(6))):
        if kind in columns and columns[kind][1] != expected:
            raise DynamicRendererDecodeError(f"Dynamic ECS name/bounds stride differs: path={path}, group={group_index}, kind={kind}")
    if any(i in columns for i in range(6)) and not all(i in columns for i in range(6)):
        raise DynamicRendererDecodeError(f"Dynamic ECS bounds columns are incomplete: path={path}, group={group_index}")
    rows: list[_RawRenderer] = []
    for index in range(nrows):
        common_at = columns[18][0] + index * 256
        ref_at, ref_stride = columns[ref_kinds[0]]
        runtime_at, runtime_stride = columns[ref_kinds[0] - 60]
        ref_at += index * ref_stride
        runtime_at += index * runtime_stride
        name = None
        if 21 in columns:
            at = columns[21][0] + index * 64
            name_bytes = data[at:at + 64].split(b"\0", 1)[0]
            name = name_bytes.decode("utf-8") if name_bytes else None
        bounds = None
        if all(i in columns for i in range(6)):
            x, y, z, ex, ey, ez = tuple(struct.unpack_from("<f", data, columns[i][0] + index * 4)[0] for i in range(6))
            if not all(math.isfinite(v) for v in (x, y, z, ex, ey, ez)) or min(ex, ey, ez) < 0:
                raise DynamicRendererDecodeError(f"Invalid Dynamic ECS bounds: path={path}, group={group_index}, record={index}")
            bounds = (x - ex, x + ex, y - ey, y + ey, z - ez, z + ez)
        rows.append(_RawRenderer(group, index, name, data[common_at:common_at + 256], data[ref_at:ref_at + ref_stride],
                                 data[runtime_at:runtime_at + runtime_stride], common_at, ref_at, runtime_at, bounds))
    return group, tuple(rows)


def _renderer_record(row: _RawRenderer, paths: dict[bytes, tuple[str, ...]]) -> DynamicRendererRecord:
    identity = row.group
    try:
        matrix = struct.unpack_from("<16f", row.common)
        if not all(math.isfinite(v) for v in matrix):
            raise ValueError("CommonInstanceData contains a non-finite matrix")
        pairs = decode_renderer_pair_paths(row.references, paths, (".fbx", ".mesh", ".asset"))
        draws = decode_draws(row.runtime, row.references)
        parameters = tuple(serialized_draw_parameters(row.common, row.common[:64]))
        resolved = tuple(DynamicRendererPair(
            mesh, material, row.references[16 + i * 40:24 + i * 40].hex(),
            row.references[8 + i * 40:16 + i * 40].hex(), row.references_offset + 8 + i * 40, draw,
            row.references[8 + i * 40:48 + i * 40].hex(), row.runtime[4 + i * 24:28 + i * 24].hex(),
        ) for i, ((mesh, material), draw) in enumerate(zip(pairs, draws, strict=True)))
    except (ValueError, struct.error) as error:
        raise DynamicRendererDecodeError(
            f"Dynamic ECS renderer decode failed: path={identity.source_virtual_path}, "
            f"group={identity.group_index}, record={row.record_index}, error={error}"
        ) from error
    return DynamicRendererRecord(
        f"{identity.source_virtual_path}:{identity.group_index}:{row.record_index}", identity.source_virtual_path,
        identity.chunk_id, identity.init_scene_index, identity.group_index, row.record_index, identity.reference_keys,
        row.name, matrix, row.bounds, parameters, row.common_offset, row.references_offset, row.runtime_offset, resolved,
    )


def _read_raw_dynamic_scene_renderers(
    vfs_root: Path, string_path_hash: Path, scene_id: str, check_cancelled: Callable[[], None],
) -> _RawDynamicSceneRendererInventory:
    """Inventory only init groups selected by actual main field-9 references."""
    scene = validate_scene_id(scene_id)
    root = validate_vfs_root(vfs_root)
    hash_path = string_path_hash.expanduser().resolve()
    if not hash_path.is_file():
        raise FileNotFoundError(f"Dynamic ECS StringPathHash source is missing: {hash_path}")
    group = load_group(root, GROUP_HASH)
    prefix = f"data/dynamicstreaming/pc/scene/{scene}/"
    mains: list[vfs.VfsFile] = []
    inits: dict[int, list[tuple[int, vfs.VfsFile]]] = {}
    for item in group.iter_files():
        if not item.file_name.casefold().startswith(prefix):
            continue
        if item.file_name.casefold().startswith(prefix + "fb_main_"):
            if MAIN_FILE.fullmatch(item.file_name) is None:
                raise DynamicRendererDecodeError(f"Dynamic ECS main filename differs: path={item.file_name}")
            mains.append(item)
        match = INIT_FILE.fullmatch(item.file_name)
        if match:
            segment, chunk = int(match[2]), int(match[3])
            inits.setdefault(chunk, []).append((segment, item))
    if not mains:
        raise DynamicRendererDecodeError(f"Dynamic ECS main sources missing: scene={scene}, group={GROUP_HASH}")
    if len({i.file_name.casefold() for i in mains}) != len(mains):
        raise DynamicRendererDecodeError(f"Duplicate Dynamic ECS main source paths: scene={scene}")
    sources: list[DynamicRendererSource] = []
    refs: list[DynamicEcsReference] = []
    for item in sorted(mains, key=lambda i: i.file_name.casefold()):
        check_cancelled()
        data, digest = _read_source(root, group, item)
        try:
            refs.extend(_main_references(data, item.file_name))
        except (EffectDecodeError, struct.error) as error:
            raise DynamicRendererDecodeError(f"Dynamic ECS main decode failed: path={item.file_name}, error={error}") from error
        sources.append(DynamicRendererSource(item.file_name, digest, len(data), len(data), hashlib.sha256(data).hexdigest(), "main"))
    selected: dict[int, dict[int, list[DynamicEcsReference]]] = {}
    for ref in refs:
        selected.setdefault(ref.chunk_id, {}).setdefault(ref.group_index, []).append(ref)
    groups: list[DynamicRendererGroup] = []
    raw_rows: list[_RawRenderer] = []
    for chunk_id, selection in sorted(selected.items()):
        check_cancelled()
        if chunk_id not in inits:
            raise DynamicRendererDecodeError(f"Referenced Dynamic ECS init source missing: scene={scene}, chunk={chunk_id}")
        candidates = inits[chunk_id]
        if len(candidates) != 1:
            raise DynamicRendererDecodeError(f"Ambiguous referenced Dynamic ECS chunk path: scene={scene}, chunk={chunk_id}, paths={tuple(i.file_name for _, i in candidates)}")
        segment, item = candidates[0]
        packed, digest = _read_source(root, group, item)
        if len(packed) < 4:
            raise DynamicRendererDecodeError(f"Dynamic ECS init compression header missing: path={item.file_name}")
        data = decompress_lz4_inv(packed[4:], struct.unpack_from("<I", packed)[0])
        sources.append(DynamicRendererSource(item.file_name, digest, len(packed), len(data), hashlib.sha256(data).hexdigest(), "init"))
        for index, group_refs in sorted(selection.items()):
            try:
                decoded_group, rows = _selected_group(data, item.file_name, chunk_id, segment, index, tuple(group_refs))
            except (EffectDecodeError, struct.error, UnicodeDecodeError) as error:
                raise DynamicRendererDecodeError(f"Dynamic ECS group decode failed: path={item.file_name}, group={index}, error={error}") from error
            groups.append(decoded_group)
            raw_rows.extend(rows)
    for row in raw_rows:
        try:
            decode_draws(row.runtime, row.references)
        except ValueError as error:
            raise DynamicRendererDecodeError(f"Dynamic ECS draw decode failed: path={row.group.source_virtual_path}, group={row.group.group_index}, record={row.record_index}, error={error}") from error
    return _RawDynamicSceneRendererInventory(scene, tuple(sources), tuple(refs), tuple(groups), tuple(raw_rows))


def _resolve_dynamic_scene_renderers(
    inventory: _RawDynamicSceneRendererInventory, hash_path: Path, check_cancelled: Callable[[], None],
) -> DynamicSceneRendererInventory:
    wanted: set[bytes] = set()
    for row in inventory.records:
        for index in range(struct.unpack_from("<Q", row.references)[0]):
            offset = 8 + 40 * index
            wanted.update((row.references[offset:offset + 8], row.references[offset + 8:offset + 16]))
    paths = {key: (value,) for key, value in _resolve_hashes(hash_path, frozenset(wanted), check_cancelled, ("assets/", "data/", "packages/")).items()} if wanted else {}
    records: list[DynamicRendererRecord] = []
    for row in inventory.records:
        check_cancelled()
        records.append(_renderer_record(row, paths))
    return DynamicSceneRendererInventory(inventory.scene_id, inventory.sources, inventory.references, inventory.groups, tuple(records))


def _decode_dynamic_scene_renderers_for_scene(
    vfs_root: Path, string_path_hash: Path, scene_id: str, check_cancelled: Callable[[], None],
) -> DynamicSceneRendererInventory:
    inventory = _read_raw_dynamic_scene_renderers(vfs_root, string_path_hash, scene_id, check_cancelled)
    return _resolve_dynamic_scene_renderers(inventory, string_path_hash.expanduser().resolve(), check_cancelled)


def _renderer_intersects_bounds(row: _RawRenderer, bounds: tuple[float, float, float, float]) -> bool:
    """Include source footprints crossing a selection edge, even with outside anchors."""
    matrix = struct.unpack_from("<16f", row.common)
    if not all(math.isfinite(value) for value in matrix):
        raise DynamicRendererDecodeError(f"Dynamic ECS matrix is non-finite: path={row.group.source_virtual_path}, record={row.record_index}")
    xmin, xmax, zmin, zmax = bounds
    if xmin <= matrix[12] < xmax and zmin <= matrix[14] < zmax:
        return True
    if row.bounds is None:
        return False
    return row.bounds[0] < xmax and row.bounds[1] > xmin and row.bounds[4] < zmax and row.bounds[5] > zmin


def read_dynamic_scene_renderers_for_bounds(
    vfs_root: Path, string_path_hash: Path, scene_id: str, bounds: tuple[float, float, float, float],
    check_cancelled: Callable[[], None],
) -> DynamicSceneRendererInventory:
    """Validate every source draw, then resolve only records intersecting the requested scope.

    Group provenance remains complete. Records are a spatial selection and must
    never be written into the complete-inventory persistent cache.
    """
    if (len(bounds) != 4 or any(type(value) not in (int, float) or not math.isfinite(value) for value in bounds)
            or bounds[0] >= bounds[1] or bounds[2] >= bounds[3]):
        raise ValueError(f"Dynamic ECS selection bounds are invalid: bounds={bounds}")
    check_cancelled()
    scene = validate_scene_id(scene_id)
    root = validate_vfs_root(vfs_root)
    hash_path = string_path_hash.expanduser().resolve()
    sources = _dynamic_inventory_sources(root, hash_path)

    def decode() -> DynamicSceneRendererInventory:
        raw = _read_raw_dynamic_scene_renderers(root, hash_path, scene, check_cancelled)
        selected = replace(raw, records=tuple(row for row in raw.records if _renderer_intersects_bounds(row, bounds)))
        result = _resolve_dynamic_scene_renderers(selected, hash_path, check_cancelled)
        _require_dynamic_inventory_sources(root, hash_path, sources)
        return result

    qualifiers = _dynamic_inventory_qualifiers((scene, "bounded", *(str(value) for value in bounds)), sources)
    result = cached_scene_source_value(_RENDERER_INVENTORY_CACHE, sources, qualifiers, decode)
    check_cancelled()
    return result


def read_dynamic_scene_renderers_for_scene(
    vfs_root: Path, string_path_hash: Path, scene_id: str, check_cancelled: Callable[[], None],
) -> DynamicSceneRendererInventory:
    """Reuse a frozen scene inventory with live metadata, payload and path-hash stamps."""
    check_cancelled()
    scene = validate_scene_id(scene_id)
    root = validate_vfs_root(vfs_root)
    hash_path = string_path_hash.expanduser().resolve()
    if not hash_path.is_file():
        raise FileNotFoundError(f"Dynamic ECS StringPathHash source is missing: {hash_path}")
    sources = _dynamic_inventory_sources(root, hash_path)

    def decode() -> DynamicSceneRendererInventory:
        result = _decode_dynamic_scene_renderers_for_scene(root, hash_path, scene, check_cancelled)
        _require_dynamic_inventory_sources(root, hash_path, sources)
        return result

    qualifiers = _dynamic_inventory_qualifiers((scene,), sources)
    result = cached_scene_source_value(_RENDERER_INVENTORY_CACHE, sources, qualifiers, decode)
    check_cancelled()
    return result
