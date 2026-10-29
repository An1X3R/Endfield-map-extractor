"""Resolve bounded direct DynamicSceneEcs FX draws without invented prefab identities."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from endfield_scene_io import read_scene_manifest
from typing import TYPE_CHECKING, Mapping, TypedDict, cast

from endfield_map_layer_contract import is_hlod_name
from endfield_scene_material_contract import MaterialRecord, reference_key, require_object, resolve_export_path
from endfield_scene_slot_contract import asset_name
from endfield_scene_effects_plan import _shader_default_issues
from endfield_scene_vfxrefract_contract import SceneColorSnapshotPlan, vfxrefract_snapshot_issues
from endfield_scene_vfxadvance_contract import (
    vfxadvance_blend_dissolve_disturb_issues,
    vfxadvance_mask_only_issues,
)
if TYPE_CHECKING:
    from extractor_core.dynamic_renderers import DynamicSceneRendererInventory


class DirectEffectAsset(TypedDict):
    Source: str
    PathId: int
    Type: str
    Name: str


class DirectEffectPair(TypedDict):
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


class DirectEffectRow(TypedDict):
    placement_key: str
    source_key: str
    source_virtual_path: str
    source_md5: str
    source_decoded_sha256: str
    chunk_id: int
    init_scene_index: int
    group_index: int
    record_index: int
    reference_keys: list[str]
    source_pair_index: int
    common_instance_offset: int
    renderer_reference_offset: int
    runtime_draw_offset: int
    name: str | None
    matrix: list[float]
    matrix_space: str
    serialized_draw_parameters: list[float]
    renderer_pairs: list[DirectEffectPair]
    mesh_asset: DirectEffectAsset
    material_asset: DirectEffectAsset
    scene_manifest: str
    activation_status: str


class DirectEffectPending(TypedDict):
    source_key: str
    source_pair_index: int
    component: str
    reason: str
    detail: str


@dataclass(frozen=True)
class DirectEffectPlanResult:
    rows: tuple[DirectEffectRow, ...]
    pending: tuple[DirectEffectPending, ...]
    required_resource_paths: tuple[str, ...]
    required_effect_mesh_paths: tuple[str, ...]
    selected_count: int


@dataclass(frozen=True)
class _MeshExport:
    asset: DirectEffectAsset
    manifest: Path
    export: dict[str, object]


def _asset(value: object) -> DirectEffectAsset:
    row = require_object(value, "Direct FX asset")
    if (not isinstance(row.get("Source"), str) or not row["Source"]
            or type(row.get("PathId")) is not int or row.get("Type") not in ("Mesh", "Material")
            or not isinstance(row.get("Name"), str)):
        raise ValueError(f"Direct FX asset is not source-qualified: asset={row}")
    return cast(DirectEffectAsset, {key: row[key] for key in ("Source", "PathId", "Type", "Name")})


def effect_mesh_streams_ready(export: Mapping[str, object], manifest: Path) -> bool:
    """Require actual metadata and sidecars; absent channels remain explicitly absent."""
    if (type(export.get("VertexCount")) is not int or cast(int, export["VertexCount"]) <= 0
            or any(type(export.get(key)) is not int for key in ("VertexColorComponents", "Uv0Components", "Uv1Components"))):
        return False
    count = cast(int, export["VertexCount"])
    for components, field, allowed in ((export["VertexColorComponents"], "VertexColorsFile", (0, 4)),
                                        (export["Uv0Components"], "Uv0File", (0, 2)),
                                        (export["Uv1Components"], "Uv1File", (0, 2))):
        if components not in allowed:
            return False
        if components:
            file = export.get(field)
            if not isinstance(file, str) or not file:
                return False
            path = resolve_export_path(manifest.parent / file.replace("\\", "/"))
            if not path.is_file() or path.stat().st_size != count * cast(int, components) * 4:
                return False
    file = export.get("File")
    return isinstance(file, str) and resolve_export_path(manifest.parent / file.replace("\\", "/")).is_file()


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"Direct FX requires an array: field={context}")
    return cast(list[object], value)


def _indices(manifests: tuple[Path, ...]) -> tuple[dict[str, dict[tuple[str, int], DirectEffectAsset]],
                                                   dict[tuple[str, int], tuple[_MeshExport, ...]]]:
    containers: dict[str, dict[tuple[str, int], DirectEffectAsset]] = {}
    exports: dict[tuple[str, int], list[_MeshExport]] = {}
    for manifest in manifests:
        data = require_object(read_scene_manifest(resolve_export_path(manifest)), "Direct FX manifest")
        if data.get("Format") != "EndfieldSceneProbe/1":
            raise ValueError(f"Direct FX requires SceneProbe manifest: path={manifest}")
        for value in _array(data.get("Containers"), "Containers"):
            container = require_object(value, "Direct FX container")
            raw = container.get("Asset")
            if raw is None or require_object(raw, "Direct FX container asset").get("Type") not in ("Mesh", "Material"):
                continue
            asset = _asset(raw)
            name = container.get("Container")
            if not isinstance(name, str):
                raise ValueError(f"Direct FX container path missing: path={manifest}")
            path = name.replace("\\", "/").casefold()
            key = reference_key(asset)
            prior = containers.setdefault(path, {}).get(key)
            if prior is not None and prior != asset:
                raise ValueError(f"Conflicting direct FX asset metadata: path={path}, asset={key}")
            containers[path][key] = asset
        for value in _array(data.get("MeshExports"), "MeshExports"):
            export = require_object(value, "Direct FX mesh export")
            asset = _asset(export["Asset"])
            exports.setdefault(reference_key(asset), []).append(_MeshExport(asset, manifest, export))
    return containers, {key: tuple(values) for key, values in exports.items()}


def _complete_mesh(candidates: tuple[_MeshExport, ...]) -> _MeshExport | None:
    complete = [row for row in candidates if effect_mesh_streams_ready(row.export, row.manifest)]
    identities: set[tuple[object, ...]] = set()
    for row in complete:
        files = [resolve_export_path(row.manifest.parent / cast(str, row.export[field]).replace("\\", "/"))
                 for field in ("File", "VertexColorsFile", "Uv0File", "Uv1File") if row.export.get(field)]
        identities.add((*(row.export[key] for key in ("VertexCount", "VertexColorComponents", "Uv0Components", "Uv1Components")),
                        *(hashlib.sha256(path.read_bytes()).hexdigest() for path in files)))
    if len(identities) > 1:
        raise ValueError(f"Conflicting complete direct FX source exports: manifests={[str(row.manifest) for row in complete]}")
    return complete[0] if complete else None


def ready_effect_mesh_paths(manifests: tuple[Path, ...]) -> frozenset[str]:
    """Expose stream-qualified FX mesh paths for automatic dependency reprobes."""
    containers, exports = _indices(manifests)
    complete = {key for key, values in exports.items()
                if any(effect_mesh_streams_ready(value.export, value.manifest) for value in values)}
    ready: set[str] = set()
    for path, assets in containers.items():
        for key, asset in assets.items():
            if "/effects/" in path and asset["Type"] == "Mesh" and key in complete:
                ready.add(path + "##" + asset["Name"].casefold())
                if path.endswith((".asset", ".mesh")):
                    ready.add(path)
    return frozenset(ready)


def ready_requested_effect_mesh_paths(manifests: tuple[Path, ...], requested: frozenset[str]) -> frozenset[str]:
    """Check stream sidecars only for the exact requested FX meshes."""
    containers, exports = _indices(manifests)
    requested_by_key: dict[tuple[str, int], set[str]] = {}
    for value in requested:
        normalized = value.replace("\\", "/").casefold()
        path, separator, name = normalized.partition("##")
        if "/effects/" not in path or (not separator and not path.endswith((".asset", ".mesh"))):
            continue
        for key, asset in containers.get(path, {}).items():
            if (asset["Type"] == "Mesh" and
                    (not separator or asset["Name"].casefold() == name)):
                requested_by_key.setdefault(key, set()).add(normalized)
    ready: set[str] = set()
    for key, paths in requested_by_key.items():
        if any(effect_mesh_streams_ready(value.export, value.manifest) for value in exports.get(key, ())):
            ready.update(paths)
    return frozenset(ready)


def validate_direct_effect_rows(values: object, manifests: frozenset[str], required_paths: frozenset[str]) -> None:
    """Validate source draw identities and required inputs before a Blender worker runs."""
    seen: set[str] = set()
    for value in _array(values, "direct_effect_renderers"):
        row = require_object(value, "direct FX row")
        for name in ("placement_key", "source_key", "source_virtual_path", "scene_manifest", "activation_status"):
            if not isinstance(row.get(name), str) or not row[name]:
                raise ValueError(f"Direct FX requires source text: field={name}, row={row.get('source_key')}")
        for name in ("chunk_id", "init_scene_index", "group_index", "record_index", "source_pair_index",
                     "common_instance_offset", "renderer_reference_offset", "runtime_draw_offset"):
            if type(row.get(name)) is not int or cast(int, row[name]) < 0:
                raise ValueError(f"Direct FX requires nonnegative source index: field={name}, row={row['source_key']}")
        key = f"{row['source_virtual_path']}:{row['group_index']}:{row['record_index']}"
        placement = f"{key}:draw:{row['source_pair_index']}"
        if row["source_key"] != key or row["placement_key"] != placement or placement in seen:
            raise ValueError(f"Direct FX placement identity conflicts: placement={row['placement_key']}")
        seen.add(placement)
        for name, count in (("source_md5", 32), ("source_decoded_sha256", 64)):
            text = row.get(name)
            if not isinstance(text, str) or len(text) != count or any(c not in "0123456789abcdefABCDEF" for c in text):
                raise ValueError(f"Direct FX source digest malformed: field={name}, source={key}")
        for name, count in (("matrix", 16), ("serialized_draw_parameters", 20)):
            numbers = _array(row.get(name), name)
            if len(numbers) != count or any(type(n) not in (int, float) or not math.isfinite(cast(float, n)) for n in numbers):
                raise ValueError(f"Direct FX finite source vector missing: field={name}, source={key}")
        if row.get("matrix_space") != "dynamic_renderer_world" or row["activation_status"] != "serialized_initial_not_runtime":
            raise ValueError(f"Direct FX source transform/runtime contract differs: source={key}")
        if str(resolve_export_path(Path(cast(str, row["scene_manifest"])))) not in manifests:
            raise ValueError(f"Direct FX source manifest is outside the plan: source={key}")
        references = _array(row.get("reference_keys"), "reference_keys")
        if not references or any(not isinstance(text, str) or not text for text in references):
            raise ValueError(f"Direct FX main reference identities missing: source={key}")
        mesh, material = _asset(row.get("mesh_asset")), _asset(row.get("material_asset"))
        if mesh["Type"] != "Mesh" or material["Type"] != "Material":
            raise ValueError(f"Direct FX source asset kinds differ: source={key}")
        pairs = _array(row.get("renderer_pairs"), "renderer_pairs")
        index = cast(int, row["source_pair_index"])
        if index >= len(pairs):
            raise ValueError(f"Direct FX selected draw exceeds source pairs: source={key}, index={index}")
        for value in pairs:
            pair = require_object(value, "Direct FX renderer pair")
            for name in ("mesh_path", "material_path"):
                path = pair.get(name)
                if not isinstance(path, str) or path != path.replace("\\", "/").casefold() or not path.startswith("assets/"):
                    raise ValueError(f"Direct FX asset path is not canonical: source={key}, field={name}")
            for name in ("submesh", "flags", "reference_offset"):
                if type(pair.get(name)) is not int or cast(int, pair[name]) < 0:
                    raise ValueError(f"Direct FX source draw index malformed: source={key}, field={name}")
            for name in ("lod_min_squared", "lod_max_squared"):
                n = pair.get(name)
                if type(n) not in (int, float) or not math.isfinite(cast(float, n)):
                    raise ValueError(f"Direct FX source LOD field malformed: source={key}, field={name}")
            for name, size in (("mesh_hash_hex", 16), ("material_hash_hex", 16),
                               ("reference_record_hex", 80), ("runtime_record_hex", 48)):
                text = pair.get(name)
                if not isinstance(text, str) or len(text) != size or any(c not in "0123456789abcdef" for c in text):
                    raise ValueError(f"Direct FX source pair bytes malformed: source={key}, field={name}")
        selected = require_object(pairs[index], "Direct FX selected pair")
        if (selected["mesh_path"] not in required_paths or selected["material_path"] not in required_paths
                or cast(int, selected["flags"]) & 1024):
            raise ValueError(f"Direct FX selected draw lacks dependencies or is shadow-only: source={key}")


def build_direct_effect_plan(inventory: DynamicSceneRendererInventory, bounds: Mapping[str, float],
                             manifests: tuple[Path, ...],
                             materials: Mapping[tuple[str, int], MaterialRecord],
                             scene_color_snapshot: SceneColorSnapshotPlan | None) -> DirectEffectPlanResult:
    """Select real FX assets, retain source placement identity and report missing inputs."""
    containers, exports = _indices(manifests)
    sources = {source.virtual_path: source for source in inventory.sources}
    required: set[str] = set()
    stream_paths: set[str] = set()
    rows: list[DirectEffectRow] = []
    pending: list[DirectEffectPending] = []
    seen: set[tuple[str, int]] = set()
    selected = 0
    resolved_meshes: dict[tuple[str, int], _MeshExport | None] = {}
    for record in inventory.records:
        x, z = record.matrix[12], record.matrix[14]
        if not (bounds["xmin"] <= x < bounds["xmax"] and bounds["zmin"] <= z < bounds["zmax"]):
            continue
        for index, pair in enumerate(record.pairs):
            mesh_path = pair.mesh_path.replace("\\", "/").casefold()
            material_path = pair.material_path.replace("\\", "/").casefold()
            if ("/effects/" not in mesh_path or "/effects/" not in material_path
                    or is_hlod_name(asset_name(mesh_path)) or pair.draw.flags & 1024):
                continue
            material_assets = containers.get(material_path, {})
            known = [materials[key] for key in material_assets if key in materials]
            if known and not any(str(row["Shader"].get("Name", "")).startswith("HGRP/Effect/")
                                 or row["Shader"].get("Name") == "HGRP/FogSimple" for row in known):
                continue
            identity = (record.key, index)
            if identity in seen:
                raise ValueError(f"Repeated direct FX source draw identity: source_key={record.key}, pair={index}")
            seen.add(identity)
            selected += 1
            required.update((mesh_path, material_path))
            stream_paths.add(mesh_path)

            def add_pending(reason: str, detail: str) -> None:
                pending.append({"source_key": record.key, "source_pair_index": index, "component": "direct_effects",
                                "reason": reason, "detail": detail})

            mesh_assets = [asset for asset in containers.get(mesh_path.split("##", 1)[0], {}).values()
                           if asset["Type"] == "Mesh" and asset["Name"].casefold() == asset_name(mesh_path)]
            if len(mesh_assets) != 1 or len(material_assets) != 1:
                add_pending("direct_asset_container_incomplete", f"mesh={mesh_path}, matches={len(mesh_assets)}; material={material_path}, matches={len(material_assets)}")
                continue
            mesh_asset = mesh_assets[0]
            material_asset = next(iter(material_assets.values()))
            material = materials.get(reference_key(material_asset))
            if material is None:
                add_pending("material_record_missing", f"material={material_path}, asset={material_asset}")
                continue
            shader_name = material["Shader"].get("Name")
            vfxadvance_branch: str | None = None
            vfxrefract_branch = False
            if shader_name == "HGRP/Effect/VFXAdvance":
                branch_issues = {
                    "mask_under_ground": vfxadvance_mask_only_issues(material, None),
                    "blend_dissolve_disturb_under_ground": vfxadvance_blend_dissolve_disturb_issues(material, None),
                }
                supported_branches = tuple(name for name, issues in branch_issues.items() if not issues)
                if len(supported_branches) != 1:
                    add_pending("shader_branch_pending",
                                f"material={material_path}, supported_feature_contracts={branch_issues}")
                    continue
                vfxadvance_branch = supported_branches[0]
            elif shader_name == "HGRP/Effect/VFXRefract" and scene_color_snapshot is not None:
                source_issues = vfxrefract_snapshot_issues(material, None)
                if source_issues:
                    add_pending("shader_branch_pending",
                                f"material={material_path}, supported_feature_contract=vfxrefract_snapshot, issues={source_issues}")
                    continue
                vfxrefract_branch = True
            elif shader_name != "HGRP/Effect/VFXBaseV2":
                add_pending("shader_family_pending", f"material={material_path}, shader={shader_name}")
                continue
            if shader_name == "HGRP/Effect/VFXBaseV2":
                if issues := _shader_default_issues(material):
                    add_pending(issues[0].code, issues[0].detail)
                    continue
            key = reference_key(mesh_asset)
            if key not in resolved_meshes:
                resolved_meshes[key] = _complete_mesh(exports.get(key, ()))
            mesh = resolved_meshes[key]
            if mesh is None:
                add_pending("mesh_stream_metadata_missing", f"Current SceneProbe mesh streams required: mesh={mesh_path}, asset={mesh_asset}")
                continue
            if vfxadvance_branch is not None:
                geometry_validator = (vfxadvance_mask_only_issues
                                      if vfxadvance_branch == "mask_under_ground"
                                      else vfxadvance_blend_dissolve_disturb_issues)
                geometry_issues = list(geometry_validator(
                    material, cast(int, mesh.export["VertexColorComponents"])))
                if mesh.export["Uv0Components"] != 2:
                    geometry_issues.append("source_uv0_required")
                if geometry_issues:
                    add_pending("shader_branch_pending",
                                f"material={material_path}, supported_branch={vfxadvance_branch}, issues={tuple(geometry_issues)}")
                    continue
            if vfxrefract_branch:
                geometry_issues = list(vfxrefract_snapshot_issues(
                    material, cast(int, mesh.export["VertexColorComponents"])))
                if mesh.export["Uv0Components"] != 2:
                    geometry_issues.append("source_uv0_required")
                if geometry_issues:
                    add_pending("shader_branch_pending",
                                f"material={material_path}, supported_branch=vfxrefract_snapshot, issues={tuple(geometry_issues)}")
                    continue
            source = sources[record.source_virtual_path]
            pairs: list[DirectEffectPair] = [{"mesh_path": p.mesh_path, "material_path": p.material_path,
                "mesh_hash_hex": p.mesh_hash_hex, "material_hash_hex": p.material_hash_hex,
                "reference_offset": p.reference_offset, "submesh": p.draw.submesh, "flags": p.draw.flags,
                "lod_max_squared": p.draw.lod_max_squared, "lod_min_squared": p.draw.lod_min_squared,
                "reference_record_hex": p.reference_record_hex, "runtime_record_hex": p.runtime_record_hex} for p in record.pairs]
            rows.append({"placement_key": f"{record.key}:draw:{index}", "source_key": record.key,
                "source_virtual_path": record.source_virtual_path, "source_md5": source.data_md5,
                "source_decoded_sha256": source.decoded_sha256, "chunk_id": record.chunk_id,
                "init_scene_index": record.init_scene_index, "group_index": record.group_index, "record_index": record.record_index,
                "reference_keys": list(record.reference_keys), "source_pair_index": index,
                "common_instance_offset": record.common_instance_offset, "renderer_reference_offset": record.renderer_reference_offset,
                "runtime_draw_offset": record.runtime_draw_offset, "name": record.name, "matrix": list(record.matrix),
                "matrix_space": "dynamic_renderer_world", "serialized_draw_parameters": list(record.serialized_draw_parameters),
                "renderer_pairs": pairs, "mesh_asset": mesh_asset, "material_asset": material_asset,
                "scene_manifest": str(mesh.manifest), "activation_status": "serialized_initial_not_runtime"})
    return DirectEffectPlanResult(tuple(rows), tuple(pending), tuple(sorted(required)), tuple(sorted(stream_paths)), selected)
