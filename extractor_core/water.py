"""Decode serialized water ECS entities and their exact streaming resources."""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass

from . import flatbuffer as fb
from .projected_decals import StreamingRecord, streaming_records


class WaterDecodeError(ValueError):
    """A water entity has an invalid or unresolved serialized binding."""


@dataclass(frozen=True)
class WaterPeerComponent:
    type_id: int
    stride: int
    raw: bytes


@dataclass(frozen=True)
class WaterInstance:
    entity_id: int
    name: str
    render_type_mask: int
    runtime_mesh_id: int
    object_to_world: tuple[float, ...]
    param0: tuple[float, float, float, float]
    param1: tuple[float, float, float, float]
    mesh_path: str
    mesh_hash_hex: str
    config_path: str
    config_hash_hex: str
    property_signature: tuple[tuple[int, int, str], ...]
    peer_components: tuple[WaterPeerComponent, ...]
    group_index: int
    entity_index: int


def _resource_kind(path: str) -> str:
    normalized = path.replace("\\", "/").casefold()
    if normalized.endswith(".asset") and (
        "/waterconfigs/" in normalized or "/waterrendererconfig/" in normalized
    ):
        return "config"
    if ".fbx##" in normalized or (
        normalized.endswith(".asset") and "/polygonmeshes/" in normalized
    ):
        return "mesh"
    raise WaterDecodeError(f"WATER_RESOURCE_KIND_UNKNOWN path={path}")


def _resolve_resources(
    data: bytes,
    entity_id: int,
    records: Mapping[int, StreamingRecord],
    path_index: Mapping[bytes, tuple[str, ...]],
) -> tuple[str, str, str, str, tuple[tuple[int, int, str], ...]]:
    record = records.get(entity_id)
    if record is None:
        raise WaterDecodeError(f"WATER_STREAMING_ENTITY_MISSING entity_id={entity_id}")
    if record.type_tag != 2:
        raise WaterDecodeError(
            f"WATER_STREAMING_TAG_INVALID entity_id={entity_id} tag={record.type_tag}"
        )
    child_address = fb.field_address(data, record.start, 3)
    if child_address is None:
        raise WaterDecodeError(f"WATER_PROPERTY_TABLE_MISSING entity_id={entity_id}")
    child = fb.offset_target(data, child_address)
    ids, count = fb.vector_from_field(data, child, 3)
    types, type_count = fb.vector_from_field(data, child, 4)
    values, value_count = fb.vector_from_field(data, child, 5)
    if count != type_count or count != value_count:
        raise WaterDecodeError(
            f"WATER_PROPERTY_VECTOR_MISMATCH entity_id={entity_id} "
            f"ids={count} types={type_count} values={value_count}"
        )
    resources: dict[str, tuple[str, str]] = {}
    signature: list[tuple[int, int, str]] = []
    for index in range(count):
        encoded_id = fb.u32(data, ids + 4 * index)
        data_type = data[types + index]
        if encoded_id >> 24 != 255 or data_type != 15:
            raise WaterDecodeError(
                f"WATER_PROPERTY_SIGNATURE_UNKNOWN entity_id={entity_id} "
                f"property={encoded_id:#x} type={data_type}"
            )
        address = values + 4 * index
        target = fb.offset_target(data, address)
        if not record.start <= target <= record.end - 8:
            raise WaterDecodeError(
                f"WATER_PROPERTY_TARGET_INVALID entity_id={entity_id} "
                f"property={encoded_id:#x} target={target}"
            )
        raw_hash = data[target : target + 8]
        paths = path_index.get(raw_hash, ())
        if len(paths) != 1:
            raise WaterDecodeError(
                f"WATER_RESOURCE_PATH_UNRESOLVED entity_id={entity_id} "
                f"property={encoded_id:#x} hash={raw_hash.hex()} paths={paths}"
            )
        path = paths[0]
        kind = _resource_kind(path)
        if kind in resources:
            raise WaterDecodeError(
                f"WATER_RESOURCE_DUPLICATE entity_id={entity_id} kind={kind} "
                f"first={resources[kind][0]} second={path}"
            )
        resources[kind] = (path, raw_hash.hex())
        signature.append((encoded_id, data_type, kind))
    if set(resources) != {"mesh", "config"}:
        raise WaterDecodeError(
            f"WATER_RESOURCES_INCOMPLETE entity_id={entity_id} "
            f"kinds={sorted(resources)}"
        )
    mesh_path, mesh_hash = resources["mesh"]
    config_path, config_hash = resources["config"]
    return mesh_path, mesh_hash, config_path, config_hash, tuple(signature)


def decode_water_instances(
    init_data: bytes,
    streaming_data: bytes,
    path_index: Mapping[bytes, tuple[str, ...]],
) -> tuple[WaterInstance, ...]:
    """Join type-30 Init columns to same-entity typed streaming references."""
    root = fb.u32(init_data, 0)
    id_groups, id_group_count = fb.vector_from_field(init_data, root, 6)
    groups, group_count = fb.vector_from_field(init_data, root, 7)
    if id_group_count != group_count:
        raise WaterDecodeError(
            f"WATER_INIT_GROUP_MISMATCH ids={id_group_count} groups={group_count}"
        )
    records = streaming_records(streaming_data)
    result: list[WaterInstance] = []
    seen: set[int] = set()
    for group_index in range(group_count):
        group = fb.table_vector_element(init_data, groups, group_index)
        count_address = fb.field_address(init_data, group, 1)
        blob_address = fb.field_address(init_data, group, 4)
        if count_address is None or blob_address is None:
            raise WaterDecodeError(f"WATER_INIT_GROUP_FIELD_MISSING group={group_index}")
        count = fb.u32(init_data, count_address)
        entity_ids, id_count = fb.wrapped_vector(
            init_data, fb.table_vector_element(init_data, id_groups, group_index)
        )
        if count != id_count:
            raise WaterDecodeError(
                f"WATER_INIT_ENTITY_COUNT_MISMATCH group={group_index} count={count} ids={id_count}"
            )
        descriptors, descriptor_count = fb.vector_from_field(init_data, group, 3)
        columns: dict[int, tuple[int, int]] = {}
        cursor = 0
        for index in range(descriptor_count):
            type_id, stride, reserved = struct.unpack_from(
                "<HHI", init_data, descriptors + 8 * index
            )
            if reserved or type_id in columns:
                raise WaterDecodeError(
                    f"WATER_INIT_DESCRIPTOR_INVALID group={group_index} "
                    f"type={type_id} reserved={reserved}"
                )
            columns[type_id] = (cursor, stride)
            cursor += count * stride
        blob, blob_length = fb.wrapped_vector(
            init_data, fb.offset_target(init_data, blob_address)
        )
        if cursor != blob_length:
            raise WaterDecodeError(
                f"WATER_INIT_BLOB_LENGTH_MISMATCH group={group_index} "
                f"expected={cursor} actual={blob_length}"
            )
        if 30 not in columns:
            continue
        if columns[30][1] != 104 or 21 not in columns:
            raise WaterDecodeError(
                f"WATER_INIT_COLUMN_INVALID group={group_index} "
                f"water={columns[30]} name={columns.get(21)}"
            )
        for entity_index in range(count):
            entity_id = fb.u32(init_data, entity_ids + 4 * entity_index)
            if entity_id in seen:
                raise WaterDecodeError(f"WATER_INIT_ENTITY_DUPLICATE entity_id={entity_id}")
            seen.add(entity_id)
            name_offset, name_stride = columns[21]
            name_start = blob + name_offset + entity_index * name_stride
            name = fb.parse_name(init_data[name_start : name_start + name_stride])
            if name is None:
                raise WaterDecodeError(f"WATER_INIT_NAME_MISSING entity_id={entity_id}")
            water_offset, water_stride = columns[30]
            water_start = blob + water_offset + entity_index * water_stride
            raw = init_data[water_start : water_start + water_stride]
            render_type_mask, runtime_mesh_id = struct.unpack_from("<Ii", raw)
            matrix = struct.unpack_from("<16f", raw, 8)
            param0 = struct.unpack_from("<4f", raw, 72)
            param1 = struct.unpack_from("<4f", raw, 88)
            if render_type_mask == 0 or not all(
                math.isfinite(value) for value in (*matrix, *param0, *param1)
            ):
                raise WaterDecodeError(
                    f"WATER_COMPONENT_INVALID entity_id={entity_id} "
                    f"render_type_mask={render_type_mask}"
                )
            peers: list[WaterPeerComponent] = []
            for type_id, (offset, stride) in columns.items():
                if type_id == 30:
                    continue
                start = blob + offset + entity_index * stride
                peers.append(
                    WaterPeerComponent(type_id, stride, init_data[start : start + stride])
                )
            mesh_path, mesh_hash, config_path, config_hash, signature = _resolve_resources(
                streaming_data, entity_id, records, path_index
            )
            result.append(
                WaterInstance(
                    entity_id,
                    name,
                    render_type_mask,
                    runtime_mesh_id,
                    matrix,
                    param0,
                    param1,
                    mesh_path,
                    mesh_hash,
                    config_path,
                    config_hash,
                    signature,
                    tuple(peers),
                    group_index,
                    entity_index,
                )
            )
    return tuple(result)
