"""Source-qualified extra UV streams for explicitly verified ordinary shader branches."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from endfield_scene_io import (SceneSourceCacheSlot, cached_scene_source_value,
                               read_scene_manifest)
from typing import Mapping, TypedDict, cast

from endfield_scene_material_contract import MaterialRecord, reference_key, require_object, resolve_export_path
from endfield_shader_contract import has_verified_lit_top_layer


class RequiredUvStreams(TypedDict):
    mesh_path: str
    channels: list[int]


class SourceUvStream(TypedDict):
    channel: int
    components: int
    vertex_count: int
    file: str | None
    source_obj_file: str
    source_manifest: str
    status: str


class SourceUvStreamsUnavailable(ValueError):
    """A current exact probe is needed before the source channel can be classified."""


@dataclass(frozen=True)
class MeshUvExport:
    record: Mapping[str, object]
    manifest: Path


@dataclass(frozen=True)
class MeshUvCatalog:
    exports: Mapping[tuple[str, int], tuple[MeshUvExport, ...]]
    containers: Mapping[str, frozenset[tuple[str, int]]]


_MESH_UV_CATALOG_CACHE = SceneSourceCacheSlot(
    "mesh-uv-catalog", MeshUvCatalog, 2, 128 * 1024 * 1024
)


def required_uv_channels(record: MaterialRecord) -> tuple[int, ...]:
    """Require only UV selectors inside the decoded TOP plus TWO_BASEMAP branch."""
    if not has_verified_lit_top_layer(record):
        return ()
    floats = record["Floats"]
    channels: set[int] = set()
    if floats["_BaseUVSet"] == 1 or floats["_BasePbrMapUVSet"] == 1:
        channels.add(1)
    if floats["_LayerBlendUVType"] == 2:
        channels.add(2)
    return tuple(sorted(channels))


def load_mesh_uv_catalog(manifests: tuple[Path, ...]) -> MeshUvCatalog:
    if not manifests:
        return MeshUvCatalog(MappingProxyType({}), MappingProxyType({}))
    resolved = tuple(resolve_export_path(manifest) for manifest in manifests)
    return cached_scene_source_value(
        _MESH_UV_CATALOG_CACHE, resolved, (),
        lambda: _build_mesh_uv_catalog(resolved),
    )


def _build_mesh_uv_catalog(manifests: tuple[Path, ...]) -> MeshUvCatalog:
    exports: dict[tuple[str, int], list[MeshUvExport]] = {}
    containers: dict[str, set[tuple[str, int]]] = {}
    for manifest in manifests:
        data = require_object(read_scene_manifest(resolve_export_path(manifest)), "UV SceneProbe")
        if data.get("MeshUvStreamVersion") != 2:
            continue
        if not isinstance(data.get("Containers"), list) or not isinstance(data.get("MeshExports"), list):
            raise ValueError(f"Current UV SceneProbe requires Containers and MeshExports arrays: manifest={manifest}")
        for value in cast(list[object], data["Containers"]):
            row = require_object(value, "UV container")
            asset = row.get("Asset")
            if asset is None or require_object(asset, "UV container asset").get("Type") != "Mesh":
                continue
            name = row.get("Container")
            if not isinstance(name, str):
                raise ValueError(f"UV mesh container path missing: manifest={manifest}")
            containers.setdefault(name.replace("\\", "/").casefold(), set()).add(reference_key(cast(Mapping[str, object], asset)))
        for value in cast(list[object], data["MeshExports"]):
            row = require_object(value, "UV mesh export")
            key = reference_key(require_object(row.get("Asset"), "UV mesh asset"))
            exports.setdefault(key, []).append(MeshUvExport(row, manifest))
    return MeshUvCatalog(MappingProxyType({key: tuple(rows) for key, rows in exports.items()}),
                         MappingProxyType({path: frozenset(keys) for path, keys in containers.items()}))


def source_uv_streams(catalog: MeshUvCatalog, asset: Mapping[str, object], channels: tuple[int, ...]) -> list[SourceUvStream]:
    """Missing metadata is unresolved; an explicitly absent source channel is a zero reference."""
    if not channels:
        return []
    if any(channel not in (1, 2) for channel in channels) or len(set(channels)) != len(channels):
        raise ValueError(f"Extra source UV channels require unique UV1/UV2 indices: channels={channels}")
    key = reference_key(asset)
    candidates: list[list[SourceUvStream]] = []
    signatures: set[tuple[object, ...]] = set()
    for export in catalog.exports.get(key, ()):
        count = export.record.get("VertexCount")
        obj_file = export.record.get("File")
        if type(count) is not int or count <= 0 or not isinstance(obj_file, str):
            continue
        obj_path = resolve_export_path(export.manifest.parent / obj_file.replace("\\", "/"))
        if not obj_path.is_file():
            continue
        rows: list[SourceUvStream] = []
        signature: list[object] = [count, hashlib.sha256(obj_path.read_bytes()).hexdigest()]
        for channel in channels:
            components = export.record.get(f"Uv{channel}Components")
            if components not in (0, 2) or type(components) is not int:
                break
            file = None
            digest = None
            if components:
                value = export.record.get(f"Uv{channel}File")
                if not isinstance(value, str) or not value:
                    break
                path = resolve_export_path(export.manifest.parent / value.replace("\\", "/"))
                if not path.is_file() or path.stat().st_size != count * components * 4:
                    break
                file = str(path)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append({"channel": channel, "components": components, "vertex_count": count, "file": file,
                         "source_obj_file": str(obj_path), "source_manifest": str(export.manifest),
                         "status": "source_stream" if components else "absent_source_zero_reference"})
            signature.extend((channel, components, digest))
        if len(rows) == len(channels):
            candidates.append(rows)
            signatures.add(tuple(signature))
    if len(signatures) > 1:
        raise ValueError(f"Source extra UV streams conflict across current probe exports: asset={key}, channels={channels}")
    if not candidates:
        raise SourceUvStreamsUnavailable(f"Source extra UV metadata/sidecars missing; refresh exact mesh with MeshUvStreamVersion=2: asset={key}, channels={channels}")
    return candidates[0]


def ready_uv_stream_paths(manifests: tuple[Path, ...], requirements: tuple[RequiredUvStreams, ...]) -> frozenset[str]:
    catalog = load_mesh_uv_catalog(manifests)
    ready: set[str] = set()
    for requirement in requirements:
        path = requirement["mesh_path"]
        container, name = path.replace("\\", "/").casefold().rsplit("##", 1)
        keys = catalog.containers.get(container, frozenset())
        matching: list[tuple[str, int]] = []
        for key in keys:
            names = [require_object(row.record["Asset"], "UV mesh asset").get("Name")
                     for row in catalog.exports.get(key, ())]
            if any(not isinstance(value, str) for value in names):
                raise ValueError(f"Current UV SceneProbe mesh name missing: asset={key}, path={path}")
            if any(cast(str, value).casefold() == name for value in names):
                matching.append(key)
        if len(matching) != 1:
            continue
        key = matching[0]
        try:
            source_uv_streams(catalog, {"Source": key[0], "PathId": key[1]}, tuple(requirement["channels"]))
        except SourceUvStreamsUnavailable:
            continue
        ready.add(path)
    return frozenset(ready)
