"""Resolve ordinary streaming prefab placements without treating them as effects."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, NotRequired, TypedDict, cast

from endfield_map_layer_contract import classify_entity
from endfield_scene_effects_plan import _PrefabCandidate, _anchor_matrix, _asset, _manifest, _root_members
from endfield_scene_material_contract import MaterialRecord, reference_key, require_object, resolve_export_path, supported_slots
from endfield_scene_mesh_uv_contract import MeshUvCatalog, RequiredUvStreams, SourceUvStream, required_uv_channels, source_uv_streams
from endfield_scene_slot_contract import validate_ordered_surface_passes
from endfield_scene_skin_contract import SourceSceneSkin, source_scene_skin

if TYPE_CHECKING:
    from extractor_core.dynamic_models import DynamicModelInventory
    from endfield_scene_export_plan import AssetRef


SNAPSHOT_STATUS = "serialized_prefab_reference_runtime_visibility_and_animation_unverified"


class PrefabGeometryRow(TypedDict):
    source_key: str
    placement_key: str
    source_path: str
    source_sha256: str
    prefab_path: str
    prefab_root: dict[str, object]
    renderer_owner: dict[str, object]
    renderer_asset: dict[str, object]
    renderer_role: dict[str, object]
    scene_manifest: str
    entity_base: str
    category: str
    matrix: list[float]
    matrix_space: str
    mesh_path: str
    mesh_file: str
    mesh_asset: AssetRef
    mesh_resource_binding: NotRequired[str]
    source_material_refs: list[AssetRef]
    material_refs: list[AssetRef | None]
    submesh_slot_indices: list[int]
    submesh_material_passes: list[list[AssetRef]]
    submesh_pass_indices: list[list[int]]
    selected_submeshes: list[int]
    undrawn_submeshes: list[int]
    source_uv_streams: list[SourceUvStream]
    snapshot_status: str
    source_skin: NotRequired[SourceSceneSkin]


class PrefabGeometryPending(TypedDict):
    source_key: str
    component: str
    reason: str
    detail: str
    placement_key: NotRequired[str]
    scene_manifest: NotRequired[str]
    source_renderer: NotRequired[dict[str, object]]


class UnsupportedPrefabRendererRole(ValueError):
    """A source renderer requires a separate effect, water or shadow adapter."""


class UnresolvedPrefabMaterialBinding(ValueError):
    """The authored material pointer is null, so no asset lookup can resolve it."""


@dataclass(frozen=True)
class PrefabGeometryPlan:
    rows: tuple[PrefabGeometryRow, ...]
    pending: tuple[PrefabGeometryPending, ...]
    placements: tuple[dict[str, object], ...]
    exclusions: tuple[dict[str, object], ...]
    required_resource_paths: tuple[str, ...]
    source_sha256: tuple[tuple[str, str], ...]
    required_uv_streams: tuple[RequiredUvStreams, ...]


def prefab_candidates(manifests: tuple[Path, ...], requested: frozenset[str]) -> dict[str, _PrefabCandidate]:
    """Index source GameObjects independently of ParticleSystem probe coverage."""
    result: dict[str, _PrefabCandidate] = {}
    for path in manifests:
        data = _manifest(resolve_export_path(path))
        for value in cast(list[object], data["Containers"]):
            row = require_object(value, "prefab container")
            name = str(row["Container"]).replace("\\", "/").casefold()
            if name not in requested or row.get("Asset") is None:
                continue
            asset = _asset(row["Asset"], "prefab container Asset")
            if asset["Type"] != "GameObject":
                continue
            previous = result.get(name)
            if previous is not None and reference_key(previous.root) != reference_key(asset):
                raise ValueError(f"Ordinary prefab container has conflicting roots: path={name}")
            result[name] = _PrefabCandidate(name, asset, path, data)
    return result


def prefab_material_pass_indices(submeshes: int, material_count: int) -> list[list[int]]:
    """Retain Unity renderer material order, including surplus last-submesh passes."""
    if submeshes <= 0 or material_count <= 0:
        raise ValueError(f"Prefab renderer material/submesh mapping unsupported: submeshes={submeshes}, materials={material_count}")
    if material_count == 1:
        return [[0] for _ in range(submeshes)]
    if material_count < submeshes:
        return [[index] if index < material_count else [] for index in range(submeshes)]
    return [[index] if index < submeshes - 1 else list(range(index, material_count)) for index in range(submeshes)]


def _owner_chain(candidate: _PrefabCandidate, owner: tuple[str, int]) -> tuple[dict[str, object], ...]:
    nodes = {reference_key(node): node for value in cast(list[object], candidate.data["Nodes"])
             for node in [require_object(value, "prefab node")]}
    transforms = {(key[0], node["TransformPathId"]): key for key, node in nodes.items()}
    root = reference_key(candidate.root)
    chain: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    current = owner
    while True:
        if current in seen or current not in nodes:
            raise ValueError(f"Prefab hierarchy missing or cyclic: prefab={candidate.path}, owner={owner}, node={current}")
        seen.add(current)
        node = nodes[current]
        chain.append(node)
        if current == root:
            return tuple(reversed(chain))
        parent = reference_key(require_object(node["Parent"], "prefab parent"))
        if parent not in transforms:
            raise ValueError(f"Prefab parent transform missing: prefab={candidate.path}, parent={parent}")
        current = transforms[parent]


def observed_prefab_geometry_paths(
    manifests: tuple[Path, ...], requested: frozenset[str],
    materials: Mapping[tuple[str, int], MaterialRecord], textures: Mapping[tuple[str, int], tuple[Path, str]],
) -> frozenset[str]:
    """Require exact hierarchy payloads, independently of host shader support."""
    ready: set[str] = set()
    for name, candidate in prefab_candidates(manifests, requested).items():
        if not isinstance(candidate.data.get("LodGroups"), list):
            continue
        members = _root_members(candidate)
        exports = {reference_key(require_object(require_object(value, "mesh export")["Asset"], "mesh asset")):
                   require_object(value, "mesh export") for value in cast(list[object], candidate.data["MeshExports"])}
        complete = True
        for value in cast(list[object], candidate.data["Nodes"]):
            node = require_object(value, "prefab node")
            if reference_key(node) not in members or node.get("RendererType") not in ("MeshRenderer", "SkinnedMeshRenderer"):
                continue
            role = node.get("RendererRole")
            if (not isinstance(role, dict) or type(role.get("CastShadows")) is not int or role["CastShadows"] not in (0, 1, 2, 3, 4)
                    or type(role.get("PlatformCastShadows")) is not int or role["PlatformCastShadows"] not in (0, 1, 2, 3, 4)):
                complete = False
            mesh = node.get("Mesh")
            export = exports.get(reference_key(require_object(mesh, "prefab mesh"))) if mesh is not None else None
            if export is None or not isinstance(export.get("File"), str) or not (candidate.manifest.parent / export["File"]).is_file():
                complete = False
            if node.get("RendererType") == "SkinnedMeshRenderer":
                if candidate.data.get("MeshSkinStreamVersion") != 1 or not isinstance(node.get("Skin"), dict):
                    complete = False
                elif export is not None and not export.get("SkinGeometryError"):
                    skin_file = export.get("SkinGeometryFile")
                    if not isinstance(skin_file, str) or not (candidate.manifest.parent / skin_file).is_file():
                        complete = False
            for ref in cast(list[object], node.get("Materials") or []):
                material_key = reference_key(require_object(ref, "prefab material"))
                if material_key[1] == 0:
                    continue
                material = materials.get(material_key)
                if material is None or any(slot["Texture"] is not None and reference_key(slot["Texture"]) not in textures
                                           for slot in material["Textures"]):
                    complete = False
        if complete:
            ready.add(name)
    return frozenset(ready)


def _lod_exclusions(candidate: _PrefabCandidate) -> dict[tuple[str, int], dict[str, object]]:
    """Select source LOD level zero instead of overlapping every enabled renderer."""
    groups = candidate.data.get("LodGroups")
    if not isinstance(groups, list):
        raise ValueError(f"Prefab requires SceneProbe LodGroups evidence; refresh its resource closure: prefab={candidate.path}")
    members = _root_members(candidate)
    excluded: dict[tuple[str, int], dict[str, object]] = {}
    selected: set[tuple[str, int]] = set()
    for value in groups:
        group = require_object(value, "prefab LODGroup")
        if reference_key(require_object(group["GameObject"], "LODGroup owner")) not in members:
            continue
        owner_chain = _owner_chain(candidate, reference_key(require_object(group["GameObject"], "LODGroup owner")))
        if any(type(node.get("GameObjectActive")) is not bool for node in owner_chain):
            raise ValueError(f"Prefab LODGroup source activity is missing: group={group.get('Asset')}")
        if not all(node["GameObjectActive"] for node in owner_chain):
            continue
        if type(group.get("Enabled")) is not bool:
            raise ValueError(f"Prefab LODGroup Enabled missing: group={group.get('Asset')}")
        if not group["Enabled"]:
            continue
        levels = group.get("Levels")
        if not isinstance(levels, list):
            raise ValueError(f"Prefab enabled LODGroup source levels are missing: group={group['Asset']}")
        for index, value in enumerate(levels):
            level = require_object(value, "prefab LOD level")
            renderers = level.get("Renderers")
            if not isinstance(renderers, list):
                raise ValueError(f"Prefab LOD renderer pointers missing: group={group['Asset']}, level={index}")
            for ref in renderers:
                key = reference_key(require_object(ref, "LOD renderer"))
                if index == 0:
                    selected.add(key)
                else:
                    excluded[key] = {"lod_group": group, "source_lod_index": index}
    return {key: evidence for key, evidence in excluded.items() if key not in selected}


def _prefab_mesh_binding(
    candidate: _PrefabCandidate, mesh_asset: Mapping[str, object], containers: Mapping[str, set[tuple[str, int]]],
) -> tuple[str, str]:
    """Keep embedded source meshes under their actual prefab resource closure."""
    mesh_key = reference_key(mesh_asset)
    paths = sorted(path for path, keys in containers.items() if mesh_key in keys)
    name = str(mesh_asset["Name"]).casefold()
    if paths:
        return f"{paths[0]}##{name}", "source_mesh_container"
    exports = [require_object(value, "embedded source mesh export")
               for value in cast(list[object], candidate.data["MeshExports"])]
    if mesh_key[0] != reference_key(candidate.root)[0] or not any(
        reference_key(require_object(row["Asset"], "embedded source mesh")) == mesh_key for row in exports
    ):
        raise ValueError(f"Prefab external mesh requires an exact source container: prefab={candidate.path}, mesh={mesh_key}")
    return f"{candidate.path}##{name}", "prefab_embedded_asset"


def build_prefab_geometry_plan(
    inventory: DynamicModelInventory, manifests: tuple[Path, ...], allowed_categories: frozenset[str],
    containers: Mapping[str, set[tuple[str, int]]],
    mesh_exports: Mapping[tuple[str, int], tuple[AssetRef, Path]],
    materials: Mapping[tuple[str, int], MaterialRecord], textures: Mapping[tuple[str, int], tuple[Path, str]],
    uv_catalog: MeshUvCatalog, selection_evidence: Mapping[str, dict[str, object]],
) -> PrefabGeometryPlan:
    """Place serialized ordinary renderers while preserving unresolved runtime behavior."""
    from prepare_scene_geometry_repairs import obj_submesh_count

    candidates = prefab_candidates(manifests, frozenset(record.prefab_path.replace("\\", "/").casefold() for record in inventory.records))
    hashes = {source.virtual_path: source.decoded_sha256 for source in inventory.sources}
    rows: list[PrefabGeometryRow] = []
    pending: list[PrefabGeometryPending] = []
    placements: list[dict[str, object]] = []
    exclusions: list[dict[str, object]] = []
    required: set[str] = set()
    required_uv: dict[str, set[int]] = {}
    for record in inventory.records:
        prefab_path = record.prefab_path.replace("\\", "/").casefold()
        required.add(prefab_path)
        placement: dict[str, object] = {**asdict(record), "source_sha256": hashes[record.source_virtual_path], "snapshot_status": SNAPSHOT_STATUS}
        if record.key in selection_evidence:
            placement["geometry_selection"] = selection_evidence[record.key]
        placements.append(placement)
        candidate = candidates.get(prefab_path)
        if candidate is None:
            pending.append({"source_key": record.key, "component": "prefab_geometry", "reason": "prefab_hierarchy_missing",
                            "detail": f"Exact SceneProbe GameObject root missing: prefab={record.prefab_path}"})
            continue
        members = _root_members(candidate)
        try:
            lod_exclusions = _lod_exclusions(candidate)
        except ValueError as error:
            pending.append({"source_key": record.key, "component": "prefab_geometry", "reason": "prefab_lod_evidence_missing", "detail": str(error)})
            continue
        renderer_count = 0
        for value in cast(list[object], candidate.data["Nodes"]):
            node = require_object(value, "prefab node")
            owner = reference_key(node)
            if owner not in members or node.get("RendererType") is None:
                continue
            renderer_count += 1
            key = f"{record.key}/renderer/{owner[0]}:{owner[1]}"
            try:
                chain = _owner_chain(candidate, owner)
                if any(type(member.get("GameObjectActive")) is not bool for member in chain) or type(node.get("RendererEnabled")) is not bool:
                    raise ValueError(f"Prefab source activity flags missing: owner={owner}")
                if not all(member["GameObjectActive"] for member in chain) or not node["RendererEnabled"]:
                    exclusions.append({"source_key": key, "placement_key": record.key, "renderer_owner": dict(node), "role": "serialized_renderer_disabled_or_gameobject_inactive"})
                    continue
                renderer = require_object(node["RendererAsset"], "prefab RendererAsset")
                renderer_role = require_object(node.get("RendererRole"), "prefab RendererRole; refresh exact resource closure")
                cast_shadows = renderer_role.get("CastShadows")
                if type(cast_shadows) is not int or cast_shadows not in (0, 1, 2, 3, 4):
                    raise ValueError(f"Prefab source ShadowCastingMode is missing or unknown: renderer={renderer}, value={cast_shadows}")
                platform_cast_shadows = renderer_role.get("PlatformCastShadows")
                if type(platform_cast_shadows) is not int or platform_cast_shadows not in (0, 1, 2, 3, 4):
                    raise ValueError(f"Prefab source platform ShadowCastingMode is missing or unknown: renderer={renderer}, value={platform_cast_shadows}")
                if platform_cast_shadows in (3, 4):
                    exclusions.append({"source_key": key, "placement_key": record.key, "renderer_owner": dict(node),
                        "role": "source_renderer_shadows_only", "renderer_role": dict(renderer_role)})
                    continue
                if reference_key(renderer) in lod_exclusions:
                    exclusions.append({"source_key": key, "placement_key": record.key, "renderer_owner": dict(node),
                        "role": "source_lod_group_lower_detail", **lod_exclusions[reference_key(renderer)]})
                    continue
                if node["RendererType"] not in ("MeshRenderer", "SkinnedMeshRenderer"):
                    raise ValueError(f"Ordinary prefab renderer requires separate source support: type={node['RendererType']}, owner={owner}")
                skin = source_scene_skin(candidate, node) if node["RendererType"] == "SkinnedMeshRenderer" else None
                mesh_key = reference_key(require_object(node["Mesh"], "prefab Mesh"))
                if mesh_key not in mesh_exports:
                    raise ValueError(f"Exact prefab source mesh OBJ missing: prefab={record.prefab_path}, mesh={mesh_key}")
                mesh_asset, mesh_file = mesh_exports[mesh_key]
                mesh_path, mesh_binding = _prefab_mesh_binding(candidate, mesh_asset, containers)
                if "/effects/" in mesh_path:
                    raise UnsupportedPrefabRendererRole(f"Prefab effect mesh requires its separate renderer role: mesh={mesh_path}, owner={owner}")
                category, _ = classify_entity(mesh_asset["Name"])
                if category not in allowed_categories:
                    exclusions.append({"source_key": key, "placement_key": record.key, "renderer_owner": dict(node), "role": "category_not_selected"})
                    continue
                material_refs: list[AssetRef] = []
                for value in cast(list[object], node.get("Materials") or []):
                    ref = require_object(value, "prefab material")
                    if reference_key(ref)[1] == 0:
                        raise UnresolvedPrefabMaterialBinding(
                            f"Source renderer material slot is null; runtime binding remains unverified: prefab={record.prefab_path}, owner={owner}, slot={len(material_refs)}")
                    material = materials.get(reference_key(ref))
                    if material is None:
                        raise ValueError(f"Exact prefab material record missing: prefab={record.prefab_path}, material={ref}")
                    if (material["Shader"].get("Name") in ("HGRP/FogSimple", "HGRP/Effect/VFXBaseV2", "HGRP/Effect/VFXDitherAlpha")
                            or material["Name"].casefold().startswith(("m_fx_", "m_fxmap_", "m_water", "m_shadow", "m_fog", "m_volume"))):
                        raise UnsupportedPrefabRendererRole(f"Prefab non-ordinary material requires its separate renderer role: material={material['Name']}, shader={material['Shader'].get('Name')}, owner={owner}")
                    for slot in supported_slots(material):
                        if slot["Texture"] is not None and reference_key(slot["Texture"]) not in textures:
                            raise ValueError(f"Prefab material texture missing: material={material['Name']}, slot={slot['Property']}")
                    material_refs.append({"Source": material["Source"], "PathId": material["PathId"], "Name": material["Name"]})
                count = obj_submesh_count(mesh_file)
                indices = prefab_material_pass_indices(count, len(material_refs))
                passes = [[material_refs[index] for index in group] for group in indices]
                for refs in passes:
                    if len(refs) > 1:
                        validate_ordered_surface_passes(tuple(materials[reference_key(ref)] for ref in refs))
                channels = tuple(sorted({channel for ref in material_refs for channel in required_uv_channels(materials[reference_key(ref)])}))
                if mesh_binding == "source_mesh_container":
                    required.add(mesh_path)
                if channels and mesh_binding == "source_mesh_container":
                    required_uv.setdefault(mesh_path, set()).update(channels)
                streams = source_uv_streams(uv_catalog, mesh_asset, channels)
                unbound = [index for index, refs in enumerate(passes) if not refs]
                if unbound:
                    pending.append({"source_key": key, "component": "prefab_geometry",
                        "reason": "source_submesh_material_binding_unresolved",
                        "detail": f"Source submeshes have no corresponding serialized material slot: submeshes={unbound}, materials={len(material_refs)}; bound submeshes remain ready",
                        "placement_key": record.key, "scene_manifest": str(candidate.manifest), "source_renderer": dict(node)})
                rows.append({"source_key": key, "placement_key": record.key, "source_path": record.source_virtual_path,
                    "source_sha256": hashes[record.source_virtual_path], "prefab_path": prefab_path,
                    "prefab_root": dict(candidate.root), "renderer_owner": {"Source": node["Source"], "PathId": node["PathId"], "Type": "GameObject"},
                    "renderer_asset": dict(renderer), "renderer_role": dict(renderer_role), "scene_manifest": str(candidate.manifest),
                    "entity_base": str(node["Name"]), "category": category,
                    "matrix": _anchor_matrix(record.position, record.rotation_euler_degrees, record.scale),
                    "matrix_space": "unity_anchor_replaces_prefab_root_trs", "mesh_path": mesh_path,
                    "mesh_file": str(mesh_file), "mesh_asset": mesh_asset,
                    **({"mesh_resource_binding": mesh_binding} if mesh_binding == "prefab_embedded_asset" else {}),
                    "source_material_refs": material_refs,
                    "material_refs": [refs[0] if refs else None for refs in passes], "submesh_slot_indices": [group[0] if group else -1 for group in indices],
                    "submesh_material_passes": passes, "submesh_pass_indices": indices,
                    "selected_submeshes": [index for index in range(count) if index not in unbound], "undrawn_submeshes": unbound, "source_uv_streams": streams,
                    "snapshot_status": SNAPSHOT_STATUS, **({"source_skin": skin} if skin is not None else {})})
            except UnresolvedPrefabMaterialBinding as error:
                pending.append({"source_key": key, "component": "prefab_geometry", "reason": "source_null_material_runtime_binding_unresolved",
                    "detail": str(error), "placement_key": record.key, "scene_manifest": str(candidate.manifest), "source_renderer": dict(node)})
            except UnsupportedPrefabRendererRole as error:
                pending.append({"source_key": key, "component": "prefab_geometry", "reason": "effect_renderer_role_requires_separate_support",
                    "detail": str(error), "placement_key": record.key, "scene_manifest": str(candidate.manifest), "source_renderer": dict(node)})
            except (ValueError, NotImplementedError) as error:
                pending.append({"source_key": key, "component": "prefab_geometry", "reason": "exact_prefab_renderer_incomplete",
                    "detail": str(error), "placement_key": record.key, "scene_manifest": str(candidate.manifest), "source_renderer": dict(node)})
        if renderer_count == 0:
            exclusions.append({"source_key": record.key, "placement_key": record.key, "prefab_root": dict(candidate.root),
                "role": "prefab_has_no_mesh_renderer_in_source_hierarchy", "scene_manifest": str(candidate.manifest)})
    return PrefabGeometryPlan(tuple(rows), tuple(pending), tuple(placements), tuple(exclusions), tuple(sorted(required)),
        tuple(sorted(hashes.items())), tuple({"mesh_path": path, "channels": sorted(channels)} for path, channels in sorted(required_uv.items())))


def validate_prefab_geometry_plan(plan: Mapping[str, object]) -> None:
    """Reject stale prefab identity, source slot or hierarchy handoffs before Blender."""
    from endfield_scene_material_contract import resolve_export_path

    fields = ("prefab_geometry_instances", "prefab_geometry_pending", "prefab_placements", "prefab_geometry_exclusions")
    for field in fields:
        if not isinstance(plan.get(field), list):
            raise ValueError(f"Scene {field} must be an array")
    hashes = require_object(plan["source_sha256"], "scene source hashes")
    placements = {str(row["key"]): row for value in cast(list[object], plan["prefab_placements"])
                  for row in [require_object(value, "prefab placement")]}
    if len(placements) != len(cast(list[object], plan["prefab_placements"])):
        raise ValueError("Scene prefab placement keys are repeated")
    for key, record in placements.items():
        if record.get("source_sha256") != hashes.get(str(record.get("source_virtual_path"))) or record.get("snapshot_status") != SNAPSHOT_STATUS:
            raise ValueError(f"Prefab placement source SHA-256 or snapshot status differs: placement={key}")
        if len(bytes.fromhex(str(record.get("raw_record_hex")))) != 96:
            raise ValueError(f"Prefab placement must preserve its 96-byte source record: placement={key}")
        if "geometry_selection" in record:
            from endfield_scene_prefab_bounds import validate_prefab_selection_ownership

            validate_prefab_selection_ownership(record, cast(Mapping[str, float], require_object(plan.get("bounds"), "prefab bounds")))
    manifests = tuple(Path(str(value)) for value in cast(list[object], plan["scene_manifests"]))
    candidates = prefab_candidates(manifests, frozenset(str(row["prefab_path"]).replace("\\", "/").casefold() for row in placements.values()))
    seen: set[str] = set()
    for value in cast(list[object], plan["prefab_geometry_instances"]):
        row = require_object(value, "prefab geometry")
        key = str(row["source_key"])
        placement = placements.get(str(row.get("placement_key")))
        if key in seen or placement is None:
            raise ValueError(f"Prefab geometry has duplicate or missing placement identity: source_key={key}")
        seen.add(key)
        if (row.get("source_path") != placement["source_virtual_path"] or row.get("source_sha256") != placement["source_sha256"]
                or row.get("prefab_path") != str(placement["prefab_path"]).replace("\\", "/").casefold() or row.get("snapshot_status") != SNAPSHOT_STATUS
                or row.get("matrix_space") != "unity_anchor_replaces_prefab_root_trs"):
            raise ValueError(f"Prefab geometry source provenance differs: source_key={key}")
        matrix = _anchor_matrix(cast(tuple[float, float, float], placement["position"]),
            cast(tuple[float, float, float], placement["rotation_euler_degrees"]), cast(tuple[float, float, float], placement["scale"]))
        if row.get("matrix") != matrix or any(not math.isfinite(value) for value in matrix):
            raise ValueError(f"Prefab geometry anchor differs from source TRS: source_key={key}")
        candidate = candidates.get(str(row["prefab_path"]))
        if candidate is None or str(candidate.manifest) != row.get("scene_manifest") or dict(candidate.root) != row.get("prefab_root"):
            raise ValueError(f"Prefab geometry exact root manifest differs: source_key={key}")
        owner = reference_key(require_object(row.get("renderer_owner"), "prefab renderer owner"))
        if key != f"{row['placement_key']}/renderer/{owner[0]}:{owner[1]}":
            raise ValueError(f"Prefab renderer key differs from source owner: source_key={key}")
        chain = _owner_chain(candidate, owner)
        node = chain[-1]
        role = require_object(node.get("RendererRole"), "prefab source renderer role")
        if (type(role.get("CastShadows")) is not int or role["CastShadows"] not in (0, 1, 2, 3, 4)
                or type(role.get("PlatformCastShadows")) is not int or role["PlatformCastShadows"] not in (0, 1, 2)
                or role != row.get("renderer_role")):
            raise ValueError(f"Prefab visible geometry has missing or shadow-only source renderer role: source_key={key}")
        if (node.get("RendererType") not in ("MeshRenderer", "SkinnedMeshRenderer") or node.get("RendererEnabled") is not True
                or any(member.get("GameObjectActive") is not True for member in chain)
                or reference_key(require_object(row["renderer_asset"], "prefab renderer asset")) in _lod_exclusions(candidate)):
            raise ValueError(f"Prefab renderer is inactive or excluded by source LODGroup: source_key={key}")
        expected_skin = source_scene_skin(candidate, node) if node["RendererType"] == "SkinnedMeshRenderer" else None
        if row.get("source_skin") != expected_skin:
            raise ValueError(f"Prefab skin stream or authored bone pose differs from source: source_key={key}")
        if node.get("RendererAsset") != row.get("renderer_asset") or reference_key(require_object(node["Mesh"], "prefab Mesh")) != reference_key(require_object(row["mesh_asset"], "prefab mesh asset")):
            raise ValueError(f"Prefab renderer or mesh differs from hierarchy: source_key={key}")
        if "mesh_resource_binding" in row:
            containers: dict[str, set[tuple[str, int]]] = {}
            for value in cast(list[object], candidate.data["Containers"]):
                container = require_object(value, "prefab source container")
                if container.get("Asset") is not None:
                    containers.setdefault(str(container["Container"]).replace("\\", "/").casefold(), set()).add(
                        reference_key(require_object(container["Asset"], "source container asset")))
            path, binding = _prefab_mesh_binding(candidate, require_object(row["mesh_asset"], "source prefab mesh"), containers)
            if row.get("mesh_resource_binding") != binding or row.get("mesh_path") != path:
                raise ValueError(f"Prefab mesh resource binding differs from its source container or embedded asset: source_key={key}")
        refs = cast(list[dict[str, object]], row["source_material_refs"])
        if [reference_key(ref) for ref in refs] != [reference_key(ref) for ref in cast(list[dict[str, object]], node["Materials"])]:
            raise ValueError(f"Prefab source material order differs: source_key={key}")
        mesh_file = resolve_export_path(Path(str(row["mesh_file"])))
        if not mesh_file.is_file():
            raise FileNotFoundError(f"Prefab exact source OBJ missing: source_key={key}, file={mesh_file}")
        refs_by_submesh = row.get("material_refs")
        if not isinstance(refs_by_submesh, list) or not refs_by_submesh:
            raise ValueError(f"Prefab source submesh references missing: source_key={key}")
        count = len(refs_by_submesh)
        indices = prefab_material_pass_indices(count, len(refs))
        passes = [[refs[index] for index in group] for group in indices]
        if (row.get("submesh_pass_indices") != indices or row.get("submesh_material_passes") != passes
                or row.get("material_refs") != [group[0] if group else None for group in passes]
                or row.get("submesh_slot_indices") != [group[0] if group else -1 for group in indices]
                or row.get("selected_submeshes") != [index for index, group in enumerate(passes) if group]
                or row.get("undrawn_submeshes") != [index for index, group in enumerate(passes) if not group]):
            raise ValueError(f"Prefab material passes differ from source renderer: source_key={key}")
        if any(not group for group in passes) and not any(
                require_object(item, "prefab submesh pending").get("source_key") == key
                and require_object(item, "prefab submesh pending").get("reason") == "source_submesh_material_binding_unresolved"
                for item in cast(list[object], plan["prefab_geometry_pending"])):
            raise ValueError(f"Prefab unbound submesh lacks explicit pending evidence: source_key={key}")
    for field in ("prefab_geometry_pending", "prefab_geometry_exclusions"):
        for value in cast(list[object], plan[field]):
            row = require_object(value, field)
            for name in (("source_key", "component", "reason", "detail") if field.endswith("pending") else ("source_key", "placement_key", "role")):
                if not isinstance(row.get(name), str) or not row[name]:
                    raise ValueError(f"Prefab audit requires nonempty source evidence: field={field}.{name}")
            if row.get("role") == "source_renderer_shadows_only":
                placement = placements.get(str(row["placement_key"]))
                candidate = candidates.get(str(placement["prefab_path"]).replace("\\", "/").casefold()) if placement is not None else None
                owner = require_object(row.get("renderer_owner"), "shadow-only prefab owner")
                if candidate is None or _owner_chain(candidate, reference_key(owner))[-1] != owner:
                    raise ValueError(f"Prefab shadow-only exclusion differs from source hierarchy: source_key={row['source_key']}")
                role = require_object(owner.get("RendererRole"), "shadow-only prefab role")
                if role != row.get("renderer_role") or type(role.get("PlatformCastShadows")) is not int or role["PlatformCastShadows"] not in (3, 4):
                    raise ValueError(f"Prefab shadow-only exclusion lacks source platform casting mode: source_key={row['source_key']}")
    summary = require_object(plan["summary"], "scene summary")
    for name, field in (("selected_prefab_placements", "prefab_placements"), ("ready_prefab_geometry", "prefab_geometry_instances"),
                        ("pending_prefab_geometry", "prefab_geometry_pending"), ("excluded_prefab_geometry", "prefab_geometry_exclusions")):
        if placements or name in summary:
            if type(summary.get(name)) is not int or summary[name] != len(cast(list[object], plan[field])):
                raise ValueError(f"Prefab summary differs from source rows: field={name}")
