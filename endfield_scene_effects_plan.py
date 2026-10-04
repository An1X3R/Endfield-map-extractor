"""Resolve bounded DynamicSceneEffect records to source-qualified SceneProbe prefabs."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, NamedTuple, TypedDict, cast

from endfield_scene_material_contract import (
    MaterialRecord,
    load_catalog,
    mesh_material_requires_uv1,
    reference_key,
    require_object,
    validate_texture_sampler,
)
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
    component_kind: str


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


class _MaterialIssue(NamedTuple):
    code: str
    detail: str


_INITIAL_V3_FIELDS = frozenset({
    "StartRotationX", "StartRotationY", "StartRotation", "GravityModifier",
    "RandomizeRotationDirection", "Rotation3D",
})
_MISSING_MATERIAL_DEPENDENCIES = frozenset({
    "material_pointer_unresolved", "material_record_missing", "texture_export_missing",
    "texture_color_space_missing", "texture_sampler_missing",
    "mesh_export_missing", "mesh_vertex_colors_missing",
    "mesh_component_schema_missing", "mesh_uv_metadata_missing", "mesh_uv0_missing", "mesh_uv1_missing",
    "shader_texture_defaults_missing",
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
    materials, textures = load_catalog(manifests)
    observed: set[str] = set()
    for prefab_path, candidates in _candidates(manifests).items():
        paired_seen = False
        fallback_observed = False
        for candidate in candidates:
            try:
                pairs = _root_pairs(candidate)
            except ValueError:
                fallback_observed = True
                continue
            if not pairs:
                mesh_nodes = _root_mesh_nodes(candidate)
                if mesh_nodes:
                    issues = _mesh_issues(candidate, mesh_nodes, materials, textures)
                    if not issues:
                        observed.add(prefab_path)
                else:
                    fallback_observed = True
                continue
            paired_seen = True
            if _schema_issue(pairs) is not None:
                continue
            issues = _material_issues(tuple(renderer for _, renderer in pairs), materials, textures)
            mesh_nodes = _root_mesh_nodes(candidate)
            mesh_issues = _mesh_issues(candidate, mesh_nodes, materials, textures)
            if not any(issue.code in _MISSING_MATERIAL_DEPENDENCIES for issue in (*issues, *mesh_issues)):
                observed.add(prefab_path)
        if not paired_seen and fallback_observed:
            observed.add(prefab_path)
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


def _root_members(candidate: _PrefabCandidate) -> frozenset[tuple[str, int]]:
    nodes = [require_object(value, "FX node") for value in _array(candidate.data["Nodes"], "FX Nodes")]
    by_key = {reference_key(node): node for node in nodes}
    transforms: dict[tuple[str, int], tuple[str, int]] = {}
    for key, node in by_key.items():
        path_id = node.get("TransformPathId")
        if type(path_id) is not int:
            raise ValueError(f"FX node TransformPathId missing: asset={key}, manifest={candidate.manifest}")
        transforms[(key[0], path_id)] = key
    root = reference_key(candidate.root)
    if root not in by_key:
        raise ValueError(f"FX prefab root node missing: path={candidate.path}, asset={root}")

    def belongs(key: tuple[str, int]) -> bool:
        seen: set[tuple[str, int]] = set()
        while key in by_key:
            if key == root:
                return True
            if key in seen:
                raise ValueError(f"FX prefab transform cycle: asset={key}, manifest={candidate.manifest}")
            seen.add(key)
            parent = _asset(by_key[key].get("Parent"), "FX node Parent")
            key = transforms.get(reference_key(parent), ("", 0))
        return False

    return frozenset(key for key in by_key if belongs(key))


def _root_mesh_nodes(candidate: _PrefabCandidate) -> tuple[dict[str, object], ...]:
    members = _root_members(candidate)
    return tuple(node for value in _array(candidate.data["Nodes"], "FX Nodes")
                 if (node := require_object(value, "FX node")).get("RendererType") == "MeshRenderer"
                 and reference_key(node) in members)


def _unpaired_particle_components(candidate: _PrefabCandidate,
                                  pairs: tuple[tuple[dict[str, object], dict[str, object]], ...]) -> bool:
    members = _root_members(candidate)
    particles = require_object(candidate.data["Particles"], "FX Particles")
    paired = {reference_key(require_object(component.get("Asset"), "FX paired component"))
              for pair in pairs for component in pair}
    for field in ("Systems", "Renderers"):
        for value in _array(particles[field], f"FX Particles.{field}"):
            component = require_object(value, f"FX particle {field}")
            pointer = component.get("GameObject")
            if pointer is None:
                continue
            owner = _asset(require_object(pointer, "FX particle owner").get("Target"), "FX particle owner Target")
            if reference_key(owner) in members and reference_key(_asset(component.get("Asset"), "FX particle Asset")) not in paired:
                return True
    return False


def _mesh_issues(candidate: _PrefabCandidate, nodes: tuple[dict[str, object], ...],
                 materials: Mapping[tuple[str, int], MaterialRecord],
                 textures: Mapping[tuple[str, int], tuple[Path, str]]) -> tuple[_MaterialIssue, ...]:
    if not nodes:
        return ()
    exports = {reference_key(_asset(require_object(value, "FX mesh export").get("Asset"), "FX mesh export Asset")):
               require_object(value, "FX mesh export")
               for value in _array(candidate.data.get("MeshExports"), "FX MeshExports")}
    issues: list[_MaterialIssue] = []
    members = _root_members(candidate)
    for value in _array(candidate.data["Nodes"], "FX Nodes"):
        member = require_object(value, "FX node")
        if reference_key(member) in members and type(member.get("GameObjectActive")) is not bool:
            issues.append(_MaterialIssue("mesh_component_schema_missing",
                                         f"GameObject activity flag missing: owner={reference_key(member)}"))
    for node in nodes:
        owner = reference_key(node)
        if type(node.get("GameObjectActive")) is not bool or type(node.get("RendererEnabled")) is not bool:
            issues.append(_MaterialIssue("mesh_component_schema_missing", f"MeshRenderer activity flags missing: owner={owner}"))
            continue
        if not node["GameObjectActive"] or not node["RendererEnabled"]:
            continue
        renderer = _asset(node.get("RendererAsset"), "FX MeshRenderer Asset")
        if renderer["Type"] != "MeshRenderer":
            issues.append(_MaterialIssue("mesh_renderer_pointer_invalid", f"MeshRenderer source pointer invalid: owner={owner}, renderer={renderer}"))
        mesh = _asset(node.get("Mesh"), "FX mesh Asset")
        if mesh["Type"] != "Mesh":
            issues.append(_MaterialIssue("mesh_pointer_invalid", f"Mesh source pointer invalid: owner={owner}, mesh={mesh}"))
            continue
        export = exports.get(reference_key(mesh))
        if export is None or not isinstance(export.get("File"), str) or not export["File"]:
            issues.append(_MaterialIssue("mesh_export_missing", f"Mesh OBJ export missing: owner={owner}, mesh={mesh}"))
        elif not (candidate.manifest.parent / cast(str, export["File"]).replace("\\", "/")).is_file():
            issues.append(_MaterialIssue("mesh_export_missing", f"Mesh OBJ file missing: owner={owner}, mesh={mesh}, file={export['File']}"))
        if export is not None:
            vertex_count = export.get("VertexCount")
            color_components = export.get("VertexColorComponents")
            if type(vertex_count) is not int or vertex_count <= 0 or type(color_components) is not int:
                issues.append(_MaterialIssue("mesh_component_schema_missing", f"Mesh vertex stream metadata missing: owner={owner}, mesh={mesh}"))
            elif color_components not in (0, 4):
                issues.append(_MaterialIssue("mesh_vertex_color_unsupported", f"Mesh vertex color component count unsupported: owner={owner}, mesh={mesh}, components={color_components}"))
            elif color_components == 4:
                color_file = export.get("VertexColorsFile")
                if not isinstance(color_file, str) or not color_file or not (candidate.manifest.parent / color_file.replace("\\", "/")).is_file():
                    issues.append(_MaterialIssue("mesh_vertex_colors_missing", f"Mesh source RGBA export missing: owner={owner}, mesh={mesh}"))
            for channel in (0, 1):
                components = export.get(f"Uv{channel}Components")
                if type(components) is not int or components < 0:
                    issues.append(_MaterialIssue("mesh_uv_metadata_missing", f"Mesh UV{channel} component metadata missing: owner={owner}, mesh={mesh}"))
                    continue
                if components > 0:
                    uv_file = export.get(f"Uv{channel}File")
                    if not isinstance(uv_file, str) or not uv_file or not (candidate.manifest.parent / uv_file.replace("\\", "/")).is_file():
                        issues.append(_MaterialIssue(f"mesh_uv{channel}_missing", f"Mesh source UV{channel} export missing: owner={owner}, mesh={mesh}"))
        refs = _array(node.get("Materials"), f"FX MeshRenderer {owner} Materials")
        if not refs:
            issues.append(_MaterialIssue("no_material_pointer", f"MeshRenderer has no material slots: owner={owner}"))
        for value in refs:
            material_ref = _asset(value, "FX MeshRenderer Material")
            if material_ref["Type"] != "Material":
                issues.append(_MaterialIssue("material_target_invalid", f"MeshRenderer material pointer invalid: owner={owner}, material={material_ref}"))
                continue
            material = materials.get(reference_key(material_ref))
            if material is None:
                issues.append(_MaterialIssue("material_record_missing", f"MeshRenderer material record missing: owner={owner}, material={material_ref}"))
                continue
            if mesh_material_requires_uv1(material) and export is not None:
                uv_file = export.get("Uv1File")
                if (export.get("Uv1Components") != 2 or not isinstance(uv_file, str) or
                        not uv_file or not (candidate.manifest.parent / uv_file.replace("\\", "/")).is_file()):
                    issues.append(_MaterialIssue("mesh_uv1_missing", f"Mesh source UV1 export missing: owner={owner}, mesh={mesh}, material={material['Name']}"))
            shader = require_object(material["Shader"], "FX material Shader")
            issues.extend(_shader_default_issues(material))
            if not isinstance(shader.get("Name"), str) or not shader["Name"]:
                issues.append(_MaterialIssue("shader_name_missing", f"MeshRenderer shader name missing: material={material['Name']}"))
            for slot in material["Textures"]:
                texture = slot["Texture"]
                if texture is not None and reference_key(texture) not in textures:
                    issues.append(_MaterialIssue("texture_export_missing", f"MeshRenderer texture export missing: material={material['Name']}, property={slot['Property']}, asset={reference_key(texture)}"))
            issues.extend(_vertex_offset_sampler_issues(material, textures))
    return tuple(issues)


def _vertex_offset_sampler_issues(
    material: MaterialRecord,
    textures: Mapping[tuple[str, int], tuple[Path, str]],
) -> tuple[_MaterialIssue, ...]:
    """Require source sampling metadata only for an enabled, bound VFX offset texture."""
    if (material["Shader"].get("Name") != "HGRP/Effect/VFXBaseV2" or
            material["Floats"].get("_UseVertexOffset", 0) == 0):
        return ()
    bindings = [slot for slot in material["Textures"] if slot["Property"] == "_OffsetTex"]
    if len(bindings) != 1 or bindings[0]["Texture"] is None:
        return ()
    reference = bindings[0]["Texture"]
    key = reference_key(reference)
    if key not in textures:
        return ()
    if "TextureSampler" not in reference:
        return (_MaterialIssue(
            "texture_sampler_missing",
            f"Enabled VFX vertex offset requires source sampler metadata: material={material['Name']}, property=_OffsetTex, asset={key}",
        ),)
    validate_texture_sampler(
        reference["TextureSampler"], f"material={material['Name']}, property=_OffsetTex")
    return ()


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


def _material_issues(
    renderers: tuple[dict[str, object], ...],
    materials: Mapping[tuple[str, int], MaterialRecord],
    textures: Mapping[tuple[str, int], tuple[Path, str]],
) -> tuple[_MaterialIssue, ...]:
    issues: list[_MaterialIssue] = []
    for renderer in renderers:
        asset = _asset(renderer.get("Asset"), "FX renderer Asset")
        pointers = _array(renderer.get("Materials"), f"FX renderer {asset} Materials")
        if not pointers:
            issues.append(_MaterialIssue("no_material_pointer", f"Renderer has no material pointers: {asset}"))
            continue
        for value in pointers:
            pointer = require_object(value, "FX renderer material pointer")
            if pointer.get("IsResolved") is not True or pointer.get("ResolutionStatus") != "resolved":
                issues.append(_MaterialIssue("material_pointer_unresolved", f"Renderer material pointer is unresolved: renderer={asset}, pointer={pointer}"))
                continue
            target = _asset(pointer.get("Target"), "FX renderer material Target")
            if target["Type"] != "Material":
                issues.append(_MaterialIssue("material_target_invalid", f"Renderer material target is not Material: renderer={asset}, target={target}"))
                continue
            material = materials.get(reference_key(target))
            if material is None:
                issues.append(_MaterialIssue("material_record_missing", f"Renderer material record is missing: renderer={asset}, target={target}"))
                continue
            shader = require_object(material["Shader"], "FX material Shader")
            issues.extend(_shader_default_issues(material))
            if not isinstance(shader.get("Name"), str) or not shader["Name"]:
                issues.append(_MaterialIssue("shader_name_missing", f"Renderer material shader name is missing: material={material['Name']}"))
            for slot in material["Textures"]:
                texture = slot["Texture"]
                if texture is None:
                    continue
                key = reference_key(texture)
                if key not in textures:
                    issues.append(_MaterialIssue("texture_export_missing", f"Renderer material texture export is missing: material={material['Name']}, property={slot['Property']}, asset={key}"))
                    continue
                if texture.get("ColorSpace") not in (0, 1):
                    issues.append(_MaterialIssue("texture_color_space_missing", f"Renderer material texture ColorSpace is missing: material={material['Name']}, property={slot['Property']}, asset={key}"))
            issues.extend(_vertex_offset_sampler_issues(material, textures))
    return tuple(issues)


def _shader_default_issues(material: MaterialRecord) -> tuple[_MaterialIssue, ...]:
    """Refresh legacy VFX metadata only for active, explicitly empty texture bindings."""
    shader = material["Shader"]
    if shader.get("Name") != "HGRP/Effect/VFXBaseV2" or "TextureDefaults" in shader:
        return ()
    active = {"_MainTex"} | {
        f"_SampleTex{index}" for index in range(6)
        if material["Floats"].get(f"_UseSampleTex{index}", 0) != 0
    }
    empty = sorted(slot["Property"] for slot in material["Textures"]
                   if slot["Property"] in active and slot["Texture"] is None)
    if not empty:
        return ()
    return (_MaterialIssue("shader_texture_defaults_missing",
                           f"Legacy FX shader defaults require reprobe: material={material['Name']}, properties={empty}"),)


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
                               tuple[tuple[dict[str, object], dict[str, object]], ...],
                               tuple[dict[str, object], ...], str]] = []
        failures: list[tuple[str, str]] = []
        for candidate in unique.values():
            try:
                paired = _root_pairs(candidate)
                mesh_nodes = _root_mesh_nodes(candidate)
                if _unpaired_particle_components(candidate, paired):
                    failures.append(("particle_component_invalid", f"Prefab contains an unpaired ParticleSystem component under root: path={prefab_path}, manifest={candidate.manifest}"))
                    continue
                if not paired and not mesh_nodes:
                    failures.append(("particle_component_unsupported", f"Prefab has no paired ParticleSystem and Renderer under root: path={prefab_path}, manifest={candidate.manifest}"))
                    continue
                if paired:
                    issue = _schema_issue(paired)
                    if issue is not None:
                        failures.append(("particle_parameter_schema_unsupported", f"path={prefab_path}, manifest={candidate.manifest}; {issue}"))
                        continue
                    issues = _material_issues(tuple(renderer for _, renderer in paired), materials, textures)
                    if issues:
                        failures.append(("particle_material_incomplete", f"path={prefab_path}, manifest={candidate.manifest}; {issues[0].detail}"))
                        continue
                mesh_issues = _mesh_issues(candidate, mesh_nodes, materials, textures)
                if mesh_issues:
                    failures.append(("mesh_component_incomplete", f"path={prefab_path}, manifest={candidate.manifest}; {mesh_issues[0].detail}"))
                    continue
                kind = "mixed" if paired and mesh_nodes else "particle" if paired else "mesh_renderer"
                compatible.append((_schema_score(paired) if paired else (0, 0, 0), candidate,
                                   paired, mesh_nodes, kind))
            except ValueError as error:
                failures.append(("particle_component_invalid", f"path={prefab_path}, manifest={candidate.manifest}; {error}"))
        if not compatible:
            reason, detail = failures[0]
            add_pending(reason, detail)
            continue
        best_score = max(score for score, _, _, _, _ in compatible)
        best = [(candidate, pairs, mesh_nodes, kind)
                for score, candidate, pairs, mesh_nodes, kind in compatible if score == best_score]
        signatures = {json.dumps((pairs, mesh_nodes, kind), sort_keys=True, ensure_ascii=False)
                      for _, pairs, mesh_nodes, kind in best}
        if len(signatures) != 1:
            add_pending("prefab_snapshot_ambiguous", f"Equally complete FX snapshots disagree: path={prefab_path}, manifests={[str(candidate.manifest) for candidate, _, _, _ in best]}")
            continue
        candidate, _, _, kind = min(best, key=lambda item: str(item[0].manifest).casefold())
        try:
            digest = source_md5.get(record.source_virtual_path)
            if digest is None:
                raise ValueError(f"FX source digest is missing: source_key={record.key}")
            rows.append({"entity_id": entity_id, "source_key": record.key,
                         "source_virtual_path": record.source_virtual_path, "source_md5": digest,
                         "path_hash_hex": record.path_hash_hex, "prefab_path": prefab_path,
                         "prefab_root": candidate.root, "scene_manifest": str(candidate.manifest.resolve()),
                         "matrix": _anchor_matrix(record.position, record.rotation_euler_degrees, record.scale),
                         "matrix_space": "dynamic_effect_anchor", "component_kind": kind})
        except ValueError as error:
            add_pending("particle_component_invalid", f"path={prefab_path}, manifest={candidate.manifest}; {error}")
    return EffectPlanResult(tuple(rows), tuple(pending), tuple(sorted(required)), selected_count)
