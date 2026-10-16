"""Select authored prefab placements using transformed source vertex bounds."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, cast

from endfield_scene_effects_plan import _PrefabCandidate, _anchor_matrix
from endfield_scene_material_contract import reference_key, require_object, resolve_export_path
from endfield_scene_spatial_plan import owns_static_footprint
from endfield_scene_io import SceneSourceCacheSlot, cached_scene_source_value

if TYPE_CHECKING:
    from extractor_core.dynamic_models import DynamicModelInventory


_PREFAB_BOUNDS_CACHE = SceneSourceCacheSlot("prefab-source-bound-templates-v1", tuple, 1024, 64 * 1024 * 1024)


@dataclass(frozen=True)
class PrefabBoundsSelection:
    inventory: DynamicModelInventory
    evidence: Mapping[str, dict[str, object]]
    pending: tuple[dict[str, object], ...]
    outside_count: int
    empty_count: int


def _multiply(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(sum(left[k * 4 + row] * right[column * 4 + k] for k in range(4))
                 for column in range(4) for row in range(4))


def _node_matrix(node: Mapping[str, object]) -> tuple[float, ...]:
    values: list[tuple[float, ...]] = []
    for field, size in (("LocalPosition", 3), ("LocalRotation", 4), ("LocalScale", 3)):
        value = node.get(field)
        if (not isinstance(value, list) or len(value) != size
                or any(type(item) not in (int, float) or not math.isfinite(item) for item in value)):
            raise ValueError(f"Prefab bounds require finite source TRS: owner={node.get('PathId')}, field={field}")
        values.append(tuple(float(item) for item in value))
    position, rotation, scale = values
    norm = math.sqrt(sum(value * value for value in rotation))
    if norm == 0:
        raise ValueError(f"Prefab bounds source quaternion is zero: owner={node.get('PathId')}")
    x, y, z, w = (value / norm for value in rotation)
    return ((1 - 2 * (y*y + z*z)) * scale[0], 2*(x*y + z*w) * scale[0], 2*(x*z - y*w) * scale[0], 0.,
            2*(x*y - z*w) * scale[1], (1 - 2*(x*x + z*z)) * scale[1], 2*(y*z + x*w) * scale[1], 0.,
            2*(x*z + y*w) * scale[2], 2*(y*z - x*w) * scale[2], (1 - 2*(x*x + y*y)) * scale[2], 0.,
            *position, 1.)


def _prefab_renderer_bounds(candidate: _PrefabCandidate) -> tuple[dict[str, object], ...]:
    """Prepare source-local renderer bounds once per prefab, without placement transforms."""
    from endfield_scene_effects_plan import _root_members
    from endfield_scene_prefab_geometry_plan import _lod_exclusions, _owner_chain

    members = _root_members(candidate)
    lod_exclusions = _lod_exclusions(candidate)
    entries = candidate.data.get("MeshBounds")
    if not isinstance(entries, list):
        raise ValueError(f"Prefab source MeshBounds metadata is missing: prefab={candidate.path}")
    meshes = {reference_key(require_object(row["Asset"], "bounds mesh")): row
              for value in entries for row in [require_object(value, "source mesh bounds")]}
    renderers: list[dict[str, object]] = []
    for value in cast(list[object], candidate.data["Nodes"]):
        node = require_object(value, "bounds source node")
        if reference_key(node) not in members or node.get("RendererType") is None:
            continue
        chain = _owner_chain(candidate, reference_key(node))
        if any(type(member.get("GameObjectActive")) is not bool for member in chain) or type(node.get("RendererEnabled")) is not bool:
            raise ValueError(f"Prefab bounds source activity is missing: prefab={candidate.path}, owner={node['PathId']}")
        if not all(member["GameObjectActive"] for member in chain) or not node["RendererEnabled"]:
            continue
        renderer = require_object(node.get("RendererAsset"), "bounds source renderer")
        role = require_object(node.get("RendererRole"), "bounds source renderer role")
        mode = role.get("PlatformCastShadows")
        if type(mode) is not int or mode not in (0, 1, 2, 3, 4):
            raise ValueError(f"Prefab bounds platform casting mode is missing: renderer={renderer}")
        if mode in (3, 4) or reference_key(renderer) in lod_exclusions:
            continue
        if node["RendererType"] == "SkinnedMeshRenderer" and node.get("Mesh") is None:
            fields = require_object(role.get("SourceFields"), "source skinned renderer fields")
            pointer = require_object(fields.get("m_Mesh"), "source skinned mesh pointer")
            if type(pointer.get("m_FileID")) is int and type(pointer.get("m_PathID")) is int and pointer["m_FileID"] == pointer["m_PathID"] == 0:
                continue
            raise ValueError(f"Source skinned mesh pointer is unresolved: renderer={renderer}, pointer={pointer}")
        method = "source_vertex_aabb"
        mesh = meshes.get(reference_key(require_object(node.get("Mesh"), "bounds source mesh")))
        if node["RendererType"] == "SkinnedMeshRenderer":
            fields = require_object(role.get("SourceFields"), "source skinned renderer fields")
            native = require_object(fields.get("m_AABB"), "source skinned renderer localBounds")
            center = require_object(native.get("m_Center"), "source skinned bounds center")
            extent = require_object(native.get("m_Extent"), "source skinned bounds extent")
            coordinates = [center.get(axis) for axis in ("x", "y", "z")]
            radii = [extent.get(axis) for axis in ("x", "y", "z")]
            if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (*coordinates, *radii))
                    or any(cast(float, value) < 0 for value in radii)):
                raise ValueError(f"Source skinned renderer localBounds are invalid: renderer={renderer}")
            mesh = {"Asset": node["Mesh"], "Minimum": [cast(float, c) - cast(float, e) for c, e in zip(coordinates, radii)],
                    "Maximum": [cast(float, c) + cast(float, e) for c, e in zip(coordinates, radii)]}
            method = "source_skinned_renderer_local_bounds_runtime_animation_unverified"
        elif node["RendererType"] != "MeshRenderer":
            raise ValueError(f"Prefab bounds renderer is unsupported: renderer={renderer}, type={node['RendererType']}")
        if mesh is None or mesh.get("Error") is not None:
            raise ValueError(f"Prefab source vertex bounds are unavailable: renderer={renderer}, mesh={node.get('Mesh')}, evidence={mesh}")
        limits: list[tuple[float, ...]] = []
        for name in ("Minimum", "Maximum"):
            raw = mesh.get(name)
            if (not isinstance(raw, list) or len(raw) != 3
                    or any(type(item) not in (int, float) or not math.isfinite(item) for item in raw)):
                raise ValueError(f"Prefab vertex bounds are invalid: mesh={mesh['Asset']}, field={name}")
            limits.append(tuple(float(item) for item in raw))
        low, high = limits
        if any(low[axis] > high[axis] for axis in range(3)):
            raise ValueError(f"Prefab vertex bounds are reversed: mesh={mesh['Asset']}")
        matrix = tuple(_anchor_matrix((0., 0., 0.), (0., 0., 0.), (1., 1., 1.)))
        for child in chain[1:]:
            matrix = _multiply(matrix, _node_matrix(child))
        renderers.append({"renderer": renderer, "mesh": mesh["Asset"], "source_local_minimum": list(low),
            "source_local_maximum": list(high), "root_relative_matrix": matrix, "bounds_source": method})
    return tuple(renderers)


def _place_prefab_bounds(renderers: tuple[dict[str, object], ...], anchor: tuple[float, ...]) -> dict[str, object]:
    placed: list[dict[str, object]] = []
    for row in renderers:
        low = cast(list[float], row["source_local_minimum"])
        high = cast(list[float], row["source_local_maximum"])
        matrix = _multiply(anchor, cast(tuple[float, ...], row["root_relative_matrix"]))
        if any(not math.isfinite(value) for value in matrix):
            raise ValueError(f"Prefab bounds world transform is non-finite: renderer={row['renderer']}")
        corners = tuple(tuple(sum(matrix[k*4+axis] * point[k] for k in range(3)) + matrix[12+axis]
                              for axis in range(3))
                        for point in ((x, y, z) for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])))
        placed.append({key: value for key, value in row.items() if key != "root_relative_matrix"} | {
            "unity_world_matrix": list(matrix),
            "world_minimum": [min(point[axis] for point in corners) for axis in range(3)],
            "world_maximum": [max(point[axis] for point in corners) for axis in range(3)]})
    return {"method": "source_renderer_bounds_root_relative_native_lod0_platform_shadow_role",
        "minimum": [min(cast(list[float], row["world_minimum"])[axis] for row in placed) for axis in range(3)] if placed else None,
        "maximum": [max(cast(list[float], row["world_maximum"])[axis] for row in placed) for axis in range(3)] if placed else None,
        "renderers": placed}


def prefab_world_bounds(candidate: _PrefabCandidate, anchor: tuple[float, ...]) -> dict[str, object]:
    """Union transformed source mesh or serialized skinned local bounds."""
    return _place_prefab_bounds(_prefab_renderer_bounds(candidate), anchor)


def intersects_prefab_bounds(evidence: Mapping[str, object], bounds: tuple[float, float, float, float]) -> bool:
    """Require positive XZ overlap with the half-open selection region."""
    low, high = evidence["minimum"], evidence["maximum"]
    if low is None or high is None:
        return False
    minimum, maximum = cast(list[float], low), cast(list[float], high)
    return maximum[0] > bounds[0] and minimum[0] < bounds[1] and maximum[2] > bounds[2] and minimum[2] < bounds[3]


def select_prefab_geometry_inventory(
    inventory: DynamicModelInventory, manifests: tuple[Path, ...], bounds: tuple[float, float, float, float],
    selection: tuple[float, float, float, float],
) -> PrefabBoundsSelection:
    """Keep complete intersecting placements with stable source keys for merge deduplication."""
    from extractor_core.dynamic_models import DynamicModelInventory
    from endfield_scene_prefab_geometry_plan import prefab_candidates

    for name, region in (("batch", bounds), ("selection", selection)):
        if (len(region) != 4 or any(type(value) not in (int, float) or not math.isfinite(value) for value in region)
                or region[0] >= region[1] or region[2] >= region[3]):
            raise ValueError(f"Prefab geometry {name} bounds are invalid: bounds={region}")
    if bounds[0] < selection[0] or bounds[1] > selection[1] or bounds[2] < selection[2] or bounds[3] > selection[3]:
        raise ValueError(f"Prefab batch bounds must stay inside the full selection: batch={bounds}, selection={selection}")
    candidates = prefab_candidates(manifests, frozenset(row.prefab_path.replace("\\", "/").casefold() for row in inventory.records))
    selected = []
    evidence: dict[str, dict[str, object]] = {}
    pending: list[dict[str, object]] = []
    outside = empty = 0
    digests: dict[Path, str] = {}
    templates: dict[str, tuple[dict[str, object], ...]] = {}
    failures: dict[str, str] = {}
    for record in inventory.records:
        path = record.prefab_path.replace("\\", "/").casefold()
        candidate = candidates.get(path)
        try:
            if candidate is None:
                raise ValueError(f"Prefab bounds hierarchy is missing: prefab={path}")
            anchor = tuple(_anchor_matrix(record.position, record.rotation_euler_degrees, record.scale))
            if path in failures:
                raise ValueError(failures[path])
            if path not in templates:
                try:
                    templates[path] = cached_scene_source_value(_PREFAB_BOUNDS_CACHE, (candidate.manifest,), (path,),
                        lambda: _prefab_renderer_bounds(candidate))
                except ValueError as error:
                    failures[path] = str(error)
                    raise
            coverage = _place_prefab_bounds(templates[path], anchor)
            if coverage["minimum"] is None:
                empty += 1
                continue
            low, high = cast(list[float], coverage["minimum"]), cast(list[float], coverage["maximum"])
            region = dict(zip(("xmin", "xmax", "zmin", "zmax"), bounds))
            whole = dict(zip(("xmin", "xmax", "zmin", "zmax"), selection))
            footprint = (low[0], high[0], low[1], high[1], low[2], high[2])
            if (not intersects_prefab_bounds(coverage, selection)
                    or not owns_static_footprint(region, whole, (record.position[0], record.position[2]), footprint)):
                outside += 1
                continue
            selected.append(record)
            if candidate.manifest not in digests:
                with resolve_export_path(candidate.manifest).open("rb") as stream:
                    digests[candidate.manifest] = hashlib.file_digest(stream, "sha256").hexdigest()
            evidence[record.key] = {**coverage, "selection_bounds": list(selection), "batch_bounds": list(bounds),
                "source_manifest": str(candidate.manifest), "source_manifest_sha256": digests[candidate.manifest],
                "selection": "complete_prefab_intersects_half_open_xz_region",
                "merge_identity": record.key, "ownership": "native_anchor_inside_selection_else_clipped_footprint_owner_complete_placement"}
        except ValueError as error:
            pending.append({"source_key": record.key, "placement_key": record.key, "component": "prefab_geometry",
                "reason": "prefab_geometry_bounds_unavailable", "detail": str(error), "prefab_path": path})
    return PrefabBoundsSelection(DynamicModelInventory(inventory.scene_id, inventory.sources, tuple(selected)),
                                evidence, tuple(pending), outside, empty)

def validate_prefab_selection_evidence(placement: Mapping[str, object], batch_bounds: Mapping[str, float]) -> None:
    """Recompute saved selection bounds from the pinned source hierarchy in Blender too."""
    from endfield_scene_prefab_geometry_plan import prefab_candidates

    proof = require_object(placement.get("geometry_selection"), "prefab geometry selection evidence")
    manifest = Path(str(proof["source_manifest"]))
    with resolve_export_path(manifest).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != proof.get("source_manifest_sha256"):
        raise ValueError(f"Prefab bounds source manifest changed: path={manifest}")
    path = str(placement["prefab_path"]).replace("\\", "/").casefold()
    candidate = prefab_candidates((manifest,), frozenset({path})).get(path)
    if candidate is None:
        raise ValueError(f"Prefab bounds source root is missing: prefab={path}, manifest={manifest}")
    anchor = tuple(_anchor_matrix(cast(tuple[float, float, float], placement["position"]),
        cast(tuple[float, float, float], placement["rotation_euler_degrees"]), cast(tuple[float, float, float], placement["scale"])))
    expected = prefab_world_bounds(candidate, anchor)
    if any(proof.get(key) != value for key, value in expected.items()):
        raise ValueError(f"Prefab selection bounds differ from source geometry: placement={placement['key']}")
    saved_batch = [batch_bounds[name] for name in ("xmin", "xmax", "zmin", "zmax")]
    whole = proof.get("selection_bounds")
    if (proof.get("batch_bounds") != saved_batch or not isinstance(whole, list) or len(whole) != 4
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in whole)
            or whole[0] >= whole[1] or whole[2] >= whole[3]
            or saved_batch[0] < whole[0] or saved_batch[1] > whole[1]
            or saved_batch[2] < whole[2] or saved_batch[3] > whole[3]
            or proof.get("merge_identity") != placement["key"]):
        raise ValueError(f"Prefab selection ownership evidence differs: placement={placement['key']}")
    low, high = cast(list[float], proof["minimum"]), cast(list[float], proof["maximum"])
    region = dict(zip(("xmin", "xmax", "zmin", "zmax"), whole))
    position = cast(list[float], placement["position"])
    if (not intersects_prefab_bounds(proof, cast(tuple[float, float, float, float], tuple(whole)))
            or not owns_static_footprint(batch_bounds, region, (position[0], position[2]),
                                        (low[0], high[0], low[1], high[1], low[2], high[2]))):
        raise ValueError(f"Prefab source geometry does not own this selection region: placement={placement['key']}")
