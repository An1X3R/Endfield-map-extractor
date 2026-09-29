"""Resolve bounded DynamicSceneEffect records to source-qualified SceneProbe prefabs."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, TypedDict, cast

from endfield_scene_material_contract import MaterialRecord, reference_key, require_object
from extractor_core.particle_snapshot import PARTICLE_PARAMETER_MODULE_FIELDS, PARTICLE_PARAMETER_SCHEMA_VERSION

if TYPE_CHECKING:
    from extractor_core.effects import MapEffectInventory


class EffectAsset(TypedDict):
    Source: str
    PathId: int
    Type: str


class EffectInstance(TypedDict):
    entity_id: int
    source_key: str
    source_virtual_path: str
    source_md5: str
    path_hash_hex: str
    prefab_path: str
    prefab_root: EffectAsset
    scene_manifest: str
    matrix: list[float]
    matrix_space: str


class EffectPending(TypedDict):
    entity_id: int
    entity_base: str
    component: str
    reason: str
    detail: str


@dataclass(frozen=True)
class EffectPlanResult:
    rows: tuple[EffectInstance, ...]
    pending: tuple[EffectPending, ...]
    required_resource_paths: tuple[str, ...]
    selected_count: int


@dataclass(frozen=True)
class _PrefabCandidate:
    path: str
    root: EffectAsset
    manifest: Path
    data: dict[str, object]


_INITIAL_V3_FIELDS = frozenset({
    "StartRotationX", "StartRotationY", "StartRotation", "GravityModifier",
    "RandomizeRotationDirection", "Rotation3D",
})


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return cast(list[object], value)


def _asset(value: object, label: str) -> EffectAsset:
    row = require_object(value, label)
    source = row.get("Source")
    path_id = row.get("PathId")
    kind = row.get("Type")
    if not isinstance(source, str) or not source or type(path_id) is not int or not isinstance(kind, str) or not kind:
        raise ValueError(f"{label} requires Source, integer PathId, and Type")
    return {"Source": source, "PathId": path_id, "Type": kind}


def _manifest(path: Path) -> dict[str, object]:
    data = require_object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))
    if data.get("Format") != "EndfieldSceneProbe/1":
        raise ValueError(f"Unsupported FX SceneProbe manifest: {path}")
    _array(data.get("Containers"), f"{path}.Containers")
    return data


def _particle_schema(data: dict[str, object], path: Path) -> bool:
    particles = data.get("Particles")
    if particles is None:
        return False
    section = require_object(particles, f"{path}.Particles")
    for field in ("DataScope", "ModuleDataScope"):
        if not isinstance(section.get(field), str) or not section[field]:
            return False
    for field in ("Systems", "Renderers", "Pairs", "Diagnostics"):
        _array(section.get(field), f"{path}.Particles.{field}")
    _array(data.get("Nodes"), f"{path}.Nodes")
    return True


def observed_effect_prefabs(manifests: tuple[Path, ...]) -> frozenset[str]:
    """Treat a probed prefab as present even when its components prove unsupported."""
    observed: set[str] = set()
    for path in manifests:
        data = _manifest(path)
        if not _particle_schema(data, path):
            continue
        for value in _array(data["Containers"], f"{path}.Containers"):
            row = require_object(value, "FX container")
            asset = row.get("Asset")
            if asset is None or _asset(asset, "FX container Asset")["Type"] != "GameObject":
                continue
            name = row.get("Container")
            if not isinstance(name, str) or not name:
                raise ValueError(f"FX GameObject container requires a path: manifest={path}")
            candidate = _PrefabCandidate(name.replace("\\", "/").casefold(),
                                         _asset(asset, "FX container Asset"), path, data)
            try:
                pairs = _root_pairs(candidate)
            except ValueError:
                observed.add(candidate.path)
                continue
            if not pairs or _schema_issue(pairs) is None:
                observed.add(candidate.path)
    return frozenset(observed)


def _candidates(manifests: tuple[Path, ...]) -> dict[str, list[_PrefabCandidate]]:
    result: dict[str, list[_PrefabCandidate]] = {}
    for path in manifests:
        data = _manifest(path)
        if not _particle_schema(data, path):
            continue
        for value in _array(data["Containers"], f"{path}.Containers"):
            row = require_object(value, "FX container")
            asset_value = row.get("Asset")
            if asset_value is None:
                continue
            asset = _asset(asset_value, "FX container Asset")
            if asset["Type"] != "GameObject":
                continue
            name = row.get("Container")
            if not isinstance(name, str) or not name:
                raise ValueError(f"FX GameObject container requires a path: manifest={path}")
            normalized = name.replace("\\", "/").casefold()
            result.setdefault(normalized, []).append(_PrefabCandidate(normalized, asset, path, data))
    return result


def _root_pairs(candidate: _PrefabCandidate) -> tuple[tuple[dict[str, object], dict[str, object]], ...]:
    nodes = _array(candidate.data["Nodes"], f"{candidate.manifest}.Nodes")
    parent_by_transform: dict[tuple[str, int], tuple[str, int] | None] = {}
    transform_by_game_object: dict[tuple[str, int], tuple[str, int]] = {}
    for value in nodes:
        node = require_object(value, "FX node")
        key = reference_key(node)
        transform_id = node.get("TransformPathId")
        if type(transform_id) is not int:
            raise ValueError(f"FX node TransformPathId must be integer: manifest={candidate.manifest}, asset={key}")
        transform = (key[0], transform_id)
        parent = _asset(node.get("Parent"), "FX node Parent")
        parent_by_transform[transform] = reference_key(parent) if parent["PathId"] != 0 else None
        transform_by_game_object[key] = transform
    root_key = reference_key(candidate.root)
    root_transform = transform_by_game_object.get(root_key)
    if root_transform is None:
        raise ValueError(f"FX prefab root node missing: path={candidate.path}, asset={root_key}")
    particles = require_object(candidate.data["Particles"], "FX Particles")
    systems = {reference_key(require_object(row, "FX system")["Asset"]): require_object(row, "FX system")
               for row in _array(particles["Systems"], "FX Systems")}
    renderers = {reference_key(require_object(row, "FX renderer")["Asset"]): require_object(row, "FX renderer")
                 for row in _array(particles["Renderers"], "FX Renderers")}
    selected: list[tuple[dict[str, object], dict[str, object]]] = []
    for value in _array(particles["Pairs"], "FX Pairs"):
        pair = require_object(value, "FX pair")
        game_object = _asset(pair.get("GameObject"), "FX pair GameObject")
        transform = transform_by_game_object.get(reference_key(game_object))
        ancestors: set[tuple[str, int]] = set()
        while transform is not None and transform not in ancestors:
            if transform == root_transform:
                system = _asset(pair.get("System"), "FX pair System")
                renderer = _asset(pair.get("Renderer"), "FX pair Renderer")
                if reference_key(system) not in systems or reference_key(renderer) not in renderers:
                    raise ValueError(f"FX pair has no matching system/renderer: path={candidate.path}, pair={pair}")
                selected.append((systems[reference_key(system)], renderers[reference_key(renderer)]))
                break
            ancestors.add(transform)
            transform = parent_by_transform.get(transform)
    return tuple(selected)


def _schema_issue(pairs: tuple[tuple[dict[str, object], dict[str, object]], ...]) -> str | None:
    for system, _ in pairs:
        asset = _asset(system.get("Asset"), "FX system Asset")
        parameters = require_object(system.get("Parameters"), "FX system Parameters")
        version = parameters.get("SchemaVersion")
        initial = require_object(parameters.get("Initial"), "FX system Initial")
        missing = sorted(_INITIAL_V3_FIELDS - initial.keys())
        missing_modules = [field for field in PARTICLE_PARAMETER_MODULE_FIELDS if not isinstance(parameters.get(field), dict)]
        if type(version) is not int or version != PARTICLE_PARAMETER_SCHEMA_VERSION or missing or missing_modules:
            return (f"Particle parameter schema requires v{PARTICLE_PARAMETER_SCHEMA_VERSION}: "
                    f"system={asset}, version={version}, missing_initial={missing}, missing_modules={missing_modules}")
        for field in ("StartRotationX", "StartRotationY", "StartRotation", "GravityModifier"):
            require_object(initial[field], f"FX Initial {field}")
        direction = initial["RandomizeRotationDirection"]
        if type(direction) not in (int, float) or not math.isfinite(direction):
            return f"Particle RandomizeRotationDirection must be finite: system={asset}, value={direction}"
        if type(initial["Rotation3D"]) is not bool:
            return f"Particle Rotation3D must be boolean: system={asset}, value={initial['Rotation3D']}"
        require_object(parameters.get("Emission"), "FX system Emission")
    return None


def _schema_score(pairs: tuple[tuple[dict[str, object], dict[str, object]], ...]) -> tuple[int, int, int]:
    parameters = [require_object(system["Parameters"], "FX system Parameters") for system, _ in pairs]
    return (min(cast(int, value["SchemaVersion"]) for value in parameters),
            sum(len(require_object(value["Initial"], "FX system Initial")) for value in parameters),
            sum(len(require_object(value["Emission"], "FX system Emission")) for value in parameters))


def _material_issue(
    renderers: tuple[dict[str, object], ...],
    materials: Mapping[tuple[str, int], MaterialRecord],
    textures: Mapping[tuple[str, int], tuple[Path, str]],
) -> str | None:
    for renderer in renderers:
        asset = _asset(renderer.get("Asset"), "FX renderer Asset")
        pointers = _array(renderer.get("Materials"), f"FX renderer {asset} Materials")
        if not pointers:
            return f"Renderer has no material pointers: {asset}"
        for value in pointers:
            pointer = require_object(value, "FX renderer material pointer")
            if pointer.get("IsResolved") is not True or pointer.get("ResolutionStatus") != "resolved":
                return f"Renderer material pointer is unresolved: renderer={asset}, pointer={pointer}"
            target = _asset(pointer.get("Target"), "FX renderer material Target")
            if target["Type"] != "Material":
                return f"Renderer material target is not Material: renderer={asset}, target={target}"
            material = materials.get(reference_key(target))
            if material is None:
                return f"Renderer material record is missing: renderer={asset}, target={target}"
            shader = require_object(material["Shader"], "FX material Shader")
            if not isinstance(shader.get("Name"), str) or not shader["Name"]:
                return f"Renderer material shader name is missing: material={material['Name']}"
            for slot in material["Textures"]:
                texture = slot["Texture"]
                if texture is None:
                    continue
                key = reference_key(texture)
                if key not in textures:
                    return f"Renderer material texture export is missing: material={material['Name']}, property={slot['Property']}, asset={key}"
                if texture.get("ColorSpace") not in (0, 1):
                    return f"Renderer material texture ColorSpace is missing: material={material['Name']}, property={slot['Property']}, asset={key}"
    return None


def _anchor_matrix(position: tuple[float, float, float], euler: tuple[float, float, float],
                   scale: tuple[float, float, float]) -> list[float]:
    x, y, z = (math.radians(value) for value in euler)
    cx, sx = math.cos(x), math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(z), math.sin(z)
    columns = (
        ((cy * cz + sy * sx * sz) * scale[0], (cx * sz) * scale[0], (-sy * cz + cy * sx * sz) * scale[0]),
        ((-cy * sz + sy * sx * cz) * scale[1], (cx * cz) * scale[1], (sy * sz + cy * sx * cz) * scale[1]),
        ((sy * cx) * scale[2], (-sx) * scale[2], (cy * cx) * scale[2]),
    )
    matrix = [*columns[0], 0.0, *columns[1], 0.0, *columns[2], 0.0, *position, 1.0]
    if not all(math.isfinite(value) for value in matrix):
        raise ValueError(f"FX anchor matrix is non-finite: position={position}, euler={euler}, scale={scale}")
    return matrix


def build_effect_plan(
    inventory: MapEffectInventory,
    bounds: Mapping[str, float],
    manifests: tuple[Path, ...],
    materials: Mapping[tuple[str, int], MaterialRecord],
    textures: Mapping[tuple[str, int], tuple[Path, str]],
) -> EffectPlanResult:
    """Select half-open FX anchors and preserve each distinct source record."""
    candidates = _candidates(manifests)
    source_md5 = {source.virtual_path: source.data_md5 for source in inventory.sources}
    rows: list[EffectInstance] = []
    pending: list[EffectPending] = []
    required: set[str] = set()
    seen_ids: set[int] = set()
    selected_count = 0
    for record in inventory.records:
        x, _, z = record.position
        if not (bounds["xmin"] <= x < bounds["xmax"] and bounds["zmin"] <= z < bounds["zmax"]):
            continue
        selected_count += 1
        prefab_path = record.prefab_path.replace("\\", "/").casefold()
        required.add(prefab_path)
        entity_id = -(int.from_bytes(hashlib.sha256(record.key.encode("utf-8")).digest()[:8], "big")
                      & ((1 << 63) - 1))
        if entity_id in seen_ids:
            raise ValueError(f"FX source key hash collision: source_key={record.key}, entity_id={entity_id}")
        seen_ids.add(entity_id)

        def add_pending(reason: str, detail: str) -> None:
            pending.append({"entity_id": entity_id, "entity_base": prefab_path.rsplit("/", 1)[-1],
                            "component": "effects", "reason": reason, "detail": f"source_key={record.key}; {detail}"})

        available = candidates.get(prefab_path, [])
        unique = {(reference_key(candidate.root), candidate.manifest): candidate for candidate in available}
        root_keys = {reference_key(candidate.root) for candidate in unique.values()}
        if not available:
            add_pending("prefab_container_missing", f"No current SceneProbe GameObject container: path={prefab_path}")
            continue
        if len(root_keys) != 1:
            add_pending("prefab_root_ambiguous", f"GameObject container has {len(root_keys)} source-qualified roots: path={prefab_path}")
            continue
        compatible: list[tuple[tuple[int, int, int], _PrefabCandidate,
                               tuple[tuple[dict[str, object], dict[str, object]], ...]]] = []
        failures: list[tuple[str, str]] = []
        for candidate in unique.values():
            try:
                paired = _root_pairs(candidate)
                if not paired:
                    failures.append(("particle_component_unsupported", f"Prefab has no paired ParticleSystem and Renderer under root: path={prefab_path}, manifest={candidate.manifest}"))
                    continue
                issue = _schema_issue(paired)
                if issue is not None:
                    failures.append(("particle_parameter_schema_unsupported", f"path={prefab_path}, manifest={candidate.manifest}; {issue}"))
                    continue
                issue = _material_issue(tuple(renderer for _, renderer in paired), materials, textures)
                if issue is not None:
                    failures.append(("particle_material_incomplete", f"path={prefab_path}, manifest={candidate.manifest}; {issue}"))
                    continue
                compatible.append((_schema_score(paired), candidate, paired))
            except ValueError as error:
                failures.append(("particle_component_invalid", f"path={prefab_path}, manifest={candidate.manifest}; {error}"))
        if not compatible:
            reason, detail = failures[0]
            add_pending(reason, detail)
            continue
        best_score = max(score for score, _, _ in compatible)
        best = [(candidate, pairs) for score, candidate, pairs in compatible if score == best_score]
        signatures = {json.dumps(pairs, sort_keys=True, ensure_ascii=False) for _, pairs in best}
        if len(signatures) != 1:
            add_pending("prefab_snapshot_ambiguous", f"Equally complete particle snapshots disagree: path={prefab_path}, manifests={[str(candidate.manifest) for candidate, _ in best]}")
            continue
        candidate = min((candidate for candidate, _ in best), key=lambda item: str(item.manifest).casefold())
        try:
            digest = source_md5.get(record.source_virtual_path)
            if digest is None:
                raise ValueError(f"FX source digest is missing: source_key={record.key}")
            rows.append({"entity_id": entity_id, "source_key": record.key,
                         "source_virtual_path": record.source_virtual_path, "source_md5": digest,
                         "path_hash_hex": record.path_hash_hex, "prefab_path": prefab_path,
                         "prefab_root": candidate.root, "scene_manifest": str(candidate.manifest.resolve()),
                         "matrix": _anchor_matrix(record.position, record.rotation_euler_degrees, record.scale),
                         "matrix_space": "dynamic_effect_anchor"})
        except ValueError as error:
            add_pending("particle_component_invalid", f"path={prefab_path}, manifest={candidate.manifest}; {error}")
    return EffectPlanResult(tuple(rows), tuple(pending), tuple(sorted(required)), selected_count)
