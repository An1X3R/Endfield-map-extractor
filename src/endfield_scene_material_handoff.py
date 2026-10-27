"""Carry a validated selection from the planner to the Blender worker."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
from typing import TypedDict, cast

from endfield_scene_io import SceneInputChangedError, _file_stamp, read_scene_manifest
from endfield_scene_material_contract import (
    AssetReference, MaterialRecord, load_catalog, reference_key, require_object,
    resolve_export_path, validate_material,
)


class MaterialSourceVersion(TypedDict):
    device: int
    inode: int
    bytes: int
    modified_ns: int
    changed_time: int
    journal_sequence: int | None
    content_sha256: str | None


class MaterialCatalogSource(TypedDict):
    file: str
    version: MaterialSourceVersion


class MaterialCatalogTexture(TypedDict):
    asset: AssetReference
    file: str


class MaterialCatalogEffectRoot(TypedDict):
    scene_manifest: str
    prefab_root: AssetReference
    component_kind: str


class MaterialCatalogHandoff(TypedDict):
    format: str
    sources: list[MaterialCatalogSource]
    effect_roots: list[MaterialCatalogEffectRoot]
    required_material_refs: list[AssetReference]
    materials: list[MaterialRecord]
    textures: list[MaterialCatalogTexture]
    missing_texture_refs: list[AssetReference]
    payload_sha256: str


def capture_material_catalog_sources(paths: tuple[Path, ...]) -> list[MaterialCatalogSource]:
    """Capture strict source versions before the full catalog is validated."""
    return [{"file": str(resolved), "version": cast(MaterialSourceVersion, asdict(_file_stamp(resolved)))}
            for path in paths for resolved in (resolve_export_path(path),)]


def require_material_catalog_sources(sources: list[MaterialCatalogSource]) -> None:
    """Reject source edits, including same-size writes with restored timestamps."""
    for source in sources:
        path = resolve_export_path(Path(source["file"]))
        if asdict(_file_stamp(path)) != source["version"]:
            raise SceneInputChangedError(f"Selected material catalog source changed; regenerate scene plan: path={path}")


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"Selected material catalog requires an array: field={context}")
    return cast(list[object], value)


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Selected material catalog requires nonempty text: field={context}")
    return value


def _plan_manifest_paths(plan: Mapping[str, object]) -> tuple[Path, ...]:
    return tuple(Path(_text(value, "scene_manifests"))
                 for value in _array(plan.get("scene_manifests"), "scene_manifests"))


def _instance_material_keys(plan: Mapping[str, object]) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    for field in ("instances", "dynamic_geometry_instances", "prefab_geometry_instances", "surface_effect_instances", "water_depth_instances"):
        for value in _array(plan.get(field, []), field):
            row = require_object(value, field)
            refs = _array(row.get("material_refs"), f"{field}.material_refs")
            selected = _array(row.get("selected_submeshes", list(range(len(refs)))), f"{field}.selected_submeshes")
            for index in selected:
                if type(index) is not int or not 0 <= index < len(refs):
                    raise ValueError(f"Selected material catalog submesh index is invalid: field={field}, index={index}")
                ref = refs[index]
                if ref is not None:
                    keys.add(reference_key(ref))
                if "submesh_material_passes" in row:
                    passes = _array(row["submesh_material_passes"], f"{field}.submesh_material_passes")
                    if len(passes) != len(refs):
                        raise ValueError(f"Ordered material pass count differs from source submeshes: field={field}")
                    for value in _array(passes[index], f"{field}.submesh_material_passes[{index}]"):
                        keys.add(reference_key(value))
    for value in _array(plan.get("direct_effect_renderers", []), "direct_effect_renderers"):
        keys.add(reference_key(require_object(value, "direct effect").get("material_asset")))
    return keys


def _effect_roots(plan: Mapping[str, object]) -> list[MaterialCatalogEffectRoot]:
    roots: dict[tuple[str, tuple[str, int], str], MaterialCatalogEffectRoot] = {}
    for value in _array(plan.get("effect_instances", []), "effect_instances"):
        row = require_object(value, "effect instance")
        path = str(resolve_export_path(Path(_text(row.get("scene_manifest"), "effect scene_manifest"))))
        root = require_object(row.get("prefab_root"), "effect prefab_root")
        if root.get("Type") != "GameObject":
            raise ValueError("Selected material catalog effect root must reference a GameObject")
        key = reference_key(root)
        kind = _text(row.get("component_kind", "particle"), "effect component_kind")
        if kind not in ("particle", "mesh_renderer", "mixed"):
            raise ValueError(f"Selected material catalog effect component kind is invalid: kind={kind}")
        roots[(path, key, kind)] = {"scene_manifest": path, "prefab_root": _asset_ref(key), "component_kind": kind}
    return [roots[key] for key in sorted(roots)]


def _particle_material_keys(renderer: Mapping[str, object]) -> set[tuple[str, int]]:
    """Exclude renderer branches that return before the worker's material factory."""
    if renderer.get("Enabled") is False:
        return set()
    if renderer.get("Enabled") is not True:
        raise ValueError("Selected particle renderer requires boolean Enabled")
    mode = renderer.get("RenderMode")
    if type(mode) is not int:
        raise ValueError("Selected particle renderer requires integer RenderMode")
    if mode not in (0, 1, 4):
        return set()
    alignment = renderer.get("RenderAlignment")
    if type(alignment) is not int:
        raise ValueError("Selected particle renderer requires integer RenderAlignment")
    if (alignment not in (0, 2) and not (alignment == 4 and mode == 4)) or (alignment == 2 and mode == 1):
        return set()
    camera_velocity = renderer.get("CameraVelocityScale")
    if type(camera_velocity) not in (int, float) or not math.isfinite(camera_velocity):
        raise ValueError("Selected particle renderer requires finite CameraVelocityScale")
    if camera_velocity != 0:
        return set()
    keys = {reference_key(require_object(value, "particle material pointer").get("Target"))
            for value in _array(renderer.get("Materials"), "particle renderer Materials")}
    return keys if len(keys) == 1 and next(iter(keys))[1] != 0 else set()


def _effect_material_keys(plan: Mapping[str, object]) -> set[tuple[str, int]]:
    from endfield_scene_effects_plan import _PrefabCandidate, _asset, _root_mesh_nodes, _root_pairs

    keys: set[tuple[str, int]] = set()
    manifests: dict[Path, dict[str, object]] = {}
    seen: set[tuple[Path, tuple[str, int], str]] = set()
    for value in _array(plan.get("effect_instances", []), "effect_instances"):
        row = require_object(value, "effect instance")
        path = resolve_export_path(Path(_text(row.get("scene_manifest"), "effect scene_manifest")))
        root = _asset(row["prefab_root"], "effect prefab root")
        kind = _text(row.get("component_kind", "particle"), "effect component_kind")
        if kind not in ("particle", "mesh_renderer", "mixed"):
            raise ValueError(f"Selected material catalog effect component kind is invalid: kind={kind}")
        identity = (path, reference_key(root), kind)
        if identity in seen:
            continue
        seen.add(identity)
        if path not in manifests:
            manifests[path] = require_object(read_scene_manifest(path), "effect manifest")
        candidate = _PrefabCandidate(_text(row.get("prefab_path"), "effect prefab_path"), root, path, manifests[path])
        if kind != "mesh_renderer":
            for _system, renderer in _root_pairs(candidate):
                keys.update(_particle_material_keys(renderer))
        if kind != "particle":
            for node in _root_mesh_nodes(candidate):
                if node.get("RendererEnabled") is not True or node.get("GameObjectActive") is not True:
                    continue
                for ref in _array(node.get("Materials"), "mesh effect Materials"):
                    keys.add(reference_key(ref))
    return keys


def _asset_ref(key: tuple[str, int]) -> AssetReference:
    return {"Source": key[0], "PathId": key[1]}


def _payload_sha256(payload: Mapping[str, object]) -> str:
    data = {key: value for key, value in payload.items() if key != "payload_sha256"}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def build_material_catalog_handoff(
    plan: Mapping[str, object],
    materials: Mapping[tuple[str, int], MaterialRecord],
    textures: Mapping[tuple[str, int], tuple[Path, str]],
    sources: list[MaterialCatalogSource],
) -> MaterialCatalogHandoff:
    """Preserve exact enriched records and the planner's chosen texture candidates."""
    if [source["file"] for source in sources] != [str(resolve_export_path(path)) for path in _plan_manifest_paths(plan)]:
        raise ValueError("Selected material catalog source manifests differ from the scene plan")
    require_material_catalog_sources(sources)
    keys = _instance_material_keys(plan) | _effect_material_keys(plan)
    missing = {key for key in keys if key not in materials}
    if missing:
        raise ValueError(f"Selected material catalog dependency is missing: references={sorted(missing)}")
    records = [materials[key] for key in sorted(keys)]
    texture_keys = {reference_key(slot["Texture"]) for record in records for slot in record["Textures"]
                    if slot["Texture"] is not None}
    payload: MaterialCatalogHandoff = {
        "format": "EndfieldSelectedMaterialCatalog/1", "sources": sources,
        "effect_roots": _effect_roots(plan),
        "required_material_refs": [_asset_ref(key) for key in sorted(keys)],
        "materials": records,
        "textures": [{"asset": _asset_ref(key), "file": str(textures[key][0] / textures[key][1])}
                     for key in sorted(texture_keys) if key in textures],
        # Preserve absent unused bindings without making an incomplete required binding usable.
        "missing_texture_refs": [_asset_ref(key) for key in sorted(key for key in texture_keys if key not in textures)],
        "payload_sha256": "",
    }
    payload["payload_sha256"] = _payload_sha256(payload)
    require_material_catalog_sources(sources)
    return payload


def load_plan_material_catalog(plan: Mapping[str, object]) -> tuple[
    dict[tuple[str, int], MaterialRecord], dict[tuple[str, int], tuple[Path, str]],
]:
    """Read the checked selection, retaining the existing reader for older plans."""
    manifests = _plan_manifest_paths(plan)
    if "material_catalog" not in plan:
        return load_catalog(manifests)
    payload = require_object(plan["material_catalog"], "material_catalog")
    if payload.get("format") != "EndfieldSelectedMaterialCatalog/1":
        raise ValueError("Unsupported selected material catalog format")
    if payload.get("payload_sha256") != _payload_sha256(payload):
        raise ValueError("Selected material catalog payload SHA-256 differs; regenerate scene plan")
    sources: list[MaterialCatalogSource] = []
    for value in _array(payload.get("sources"), "sources"):
        row = require_object(value, "material source")
        if not isinstance(row.get("file"), str) or not row["file"]:
            raise ValueError("Selected material catalog source requires a nonempty file")
        version = require_object(row.get("version"), "material source version")
        sources.append({"file": cast(str, row["file"]), "version": cast(MaterialSourceVersion, version)})
    if [source["file"] for source in sources] != [str(resolve_export_path(path)) for path in manifests]:
        raise ValueError("Selected material catalog source manifests differ from the scene plan")
    require_material_catalog_sources(sources)
    if payload.get("effect_roots") != _effect_roots(plan):
        raise ValueError("Selected material catalog effect roots differ from the scene plan; regenerate scene plan")
    materials: dict[tuple[str, int], MaterialRecord] = {}
    for value in _array(payload.get("materials"), "materials"):
        record = validate_material(value)
        key = reference_key(record)
        if key in materials:
            raise ValueError(f"Selected material catalog repeats a material: reference={key}")
        materials[key] = record
    required = [reference_key(value) for value in _array(payload.get("required_material_refs"), "required_material_refs")]
    if len(set(required)) != len(required) or set(required) != materials.keys():
        raise ValueError("Selected material catalog material dependency closure differs from its records")
    missing = _instance_material_keys(plan) - materials.keys()
    if missing:
        raise ValueError(f"Selected material catalog omits scene material dependencies: references={sorted(missing)}")
    texture_keys = {reference_key(slot["Texture"]) for record in materials.values() for slot in record["Textures"]
                    if slot["Texture"] is not None}
    textures: dict[tuple[str, int], tuple[Path, str]] = {}
    for value in _array(payload.get("textures"), "textures"):
        row = require_object(value, "selected texture")
        key = reference_key(row.get("asset"))
        file = row.get("file")
        if key in textures or not isinstance(file, str) or not file:
            raise ValueError(f"Selected material catalog texture is duplicated or has no file: reference={key}")
        path = resolve_export_path(Path(file))
        if not path.is_file():
            raise FileNotFoundError(f"Selected material texture payload is missing; regenerate scene plan: reference={key}, path={path}")
        textures[key] = (path.parent, path.name)
    absent = [reference_key(value) for value in _array(payload.get("missing_texture_refs"), "missing_texture_refs")]
    if (len(set(absent)) != len(absent) or set(absent) & textures.keys()
            or set(absent) | textures.keys() != texture_keys):
        raise ValueError("Selected material catalog texture dependency closure differs from its records")
    require_material_catalog_sources(sources)
    return materials, textures
