"""Complete exact scene resources using current bundle providers and existing probes."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, cast
from uuid import uuid4

from endfield_scene_io import SceneSourceCacheSlot, cached_scene_source_value
from endfield_scene_io import read_scene_manifest, resolve_scene_read_path
from endfield_scene_material_contract import (MaterialRecord, load_catalog, reference_key, require_object, resolve_export_path,
                                             resolve_existing_export_files)
from extractor_core.resources import extract_candidate_bundles, read_bundle_closure_names
from endfield_scene_helpers import SceneHelpers, require_prepared_scene_helpers
from endfield_shader_contract import surface_family


FORMAT = "EndfieldSceneResourceCache/1"
PROVIDER_COVERAGE = "all_asset_containers"
PACKAGE_PROVIDER_COVERAGE = "all_asset_and_package_containers"


@dataclass(frozen=True)
class _FileDescription:
    """Immutable file identity fields reused within one scene IO session."""

    bytes: int
    sha256: str


@dataclass(frozen=True)
class _ProviderIndex:
    """Immutable normalized providers from one complete scanner output."""

    providers: Mapping[str, tuple[str, ...]]


_FILE_DESCRIPTION_CACHE = SceneSourceCacheSlot("scene-resource-file-description", _FileDescription, 256, 256 * 1024)
_PROVIDER_INDEX_CACHE = SceneSourceCacheSlot("scene-resource-provider-index", _ProviderIndex, 1, 384 * 1024 * 1024)


def read_json(path: Path) -> dict[str, object]:
    return require_object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))


def resolve_path(value: object, parent: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected a nonempty resource path relative to {parent}")
    path = Path(value).expanduser()
    return resolve_scene_read_path(path if path.is_absolute() else parent / path)


def describe_file(path: Path) -> dict[str, object]:
    source = resolve_scene_read_path(path)

    def build() -> _FileDescription:
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest().upper()
        return _FileDescription(source.stat().st_size, digest)

    description = cached_scene_source_value(
        _FILE_DESCRIPTION_CACHE, (source,), (str(path),), build)
    return {"path": str(path), "bytes": description.bytes, "sha256": description.sha256}


def write_index(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
    os.replace(temporary, path)


def resource_cache_root(runtime_path: Path, runtime: dict[str, object]) -> Path:
    paths = require_object(runtime["paths"], "runtime paths")
    extraction = require_object(runtime["extraction"], "runtime extraction")
    fingerprint = extraction.get("mapLayerFingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdefABCDEF" for c in fingerprint):
        raise ValueError("Scene resources require a prepared mapLayerFingerprint SHA256")
    bundle_index = resolve_path(extraction.get("bundleScanIndex"), runtime_path.parent)
    resource_database = resolve_path(extraction.get("resourceDatabase"), runtime_path.parent)
    for label, source in (("bundleScanIndex", bundle_index), ("resourceDatabase", resource_database)):
        if not source.is_file():
            raise FileNotFoundError(f"Prepared scene-resource input is missing: {label}={source}")
    bundle_hash = str(describe_file(bundle_index)["sha256"])
    resource_hash = str(describe_file(resource_database)["sha256"])
    source_digest = hashlib.sha256(
        bytes.fromhex(bundle_hash) + bytes.fromhex(resource_hash)
    ).hexdigest()
    cache = resolve_path(paths["cache_root"], runtime_path.parent)
    game = resolve_path(paths["game_root"], runtime_path.parent)
    root = cache / "scene_resources" / fingerprint.lower() / source_digest
    if root.is_relative_to(game):
        raise ValueError(f"Scene resource cache cannot be inside the game: {root}")
    return root


def supplemental_manifests(runtime_path: Path, runtime: dict[str, object]) -> tuple[Path, ...]:
    extraction = require_object(runtime["extraction"], "runtime extraction")
    if "mapLayerFingerprint" not in extraction:
        return ()
    root = resource_cache_root(runtime_path, runtime)
    path = root / "index.json"
    if not path.is_file():
        return ()
    data = read_json(path)
    if data.get("format") != FORMAT or data.get("datasetFingerprint") != extraction["mapLayerFingerprint"]:
        raise ValueError(f"Invalid scene resource cache identity: {path}")
    rows = data.get("manifests")
    if not isinstance(rows, list):
        raise ValueError(f"Scene resource cache needs a manifest list: {path}")
    result = []
    for value in rows:
        row = require_object(value, "supplement manifest")
        source = resolve_path(row["path"], root)
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError(f"Supplement manifest must exist inside its cache: {source}")
        if describe_file(source) != row:
            raise ValueError(f"Supplement manifest changed after verification: {source}")
        result.append(source)
    return tuple(result)


def ready_resource_paths(manifests: tuple[Path, ...]) -> frozenset[str]:
    """Require actual payloads independently of Blender shader support."""
    from endfield_scene_effects_plan import observed_effect_prefabs_from_catalog

    materials, textures = load_catalog(manifests)
    observed = observed_effect_prefabs_from_catalog(manifests, materials, textures)
    return _ready_resource_paths(manifests, materials, textures, lambda path: True, observed)


def ready_requested_resource_paths(manifests: tuple[Path, ...], requested: frozenset[str],
                                   cache_root: Path) -> frozenset[str]:
    """Resolve selected dependencies without decoding unrelated material records."""
    from endfield_scene_effects_plan import observed_requested_effect_prefabs
    from endfield_scene_planning_catalog import load_planning_catalog

    catalog = load_planning_catalog(manifests, cache_root)
    roots = frozenset(path.split("##", 1)[0] for path in requested)
    observed = observed_requested_effect_prefabs(manifests, roots, catalog.materials, catalog.textures)
    return _ready_resource_paths(manifests, catalog.materials, catalog.textures, roots.__contains__, observed)


def _ready_resource_paths(
    manifests: tuple[Path, ...], materials: Mapping[tuple[str, int], MaterialRecord],
    textures: Mapping[tuple[str, int], tuple[Path, str]],
    selects_container: Callable[[str], bool], observed_effects: frozenset[str],
) -> frozenset[str]:
    from endfield_scene_effects_plan import _shader_default_issues

    mesh_keys: set[tuple[str, int]] = set()
    terrain_metadata_paths: set[str] = set()
    water_keys: set[tuple[str, int]] = set()
    array_keys: set[tuple[str, int]] = set()
    water_records: list[tuple[str, dict[str, object]]] = []
    containers: list[dict[str, object]] = []
    for path in manifests:
        data = require_object(read_scene_manifest(path), str(path))
        terrain_metadata: list[tuple[str, tuple[str, int]]] = []
        for value in cast(list[object], data["Containers"]):
            row = require_object(value, "container")
            asset = row.get("Asset")
            container = str(row["Container"]).replace("\\", "/").casefold()
            if (isinstance(asset, dict) and asset.get("Type") == "MonoBehaviour" and selects_container(container)
                    and re.fullmatch(r"assets/beyond/dynamicassets/scenes/pc/[a-z0-9_]+/terrain/hgterrainsurfacesdata\.asset", container)):
                terrain_metadata.append((container, reference_key(asset)))
        terrain_sources = {key[0] for _, key in terrain_metadata}
        serialized_sources = {
            str(row["Name"]).casefold()
            for value in cast(list[object], data.get("SerializedFiles", []))
            for row in [require_object(value, "serialized source")]
            if isinstance(row.get("Name"), str) and str(row["Name"]).casefold() in terrain_sources
            and isinstance(row.get("OriginalPath"), str)
            and row["OriginalPath"]
            and resolve_path(row["OriginalPath"], path.parent).is_file()
        }
        terrain_metadata_paths.update(container for container, key in terrain_metadata if key[0] in serialized_sources)
        array_keys.update(ready_texture_arrays(data, path.parent))
        water = require_object(data.get("Water", {}), "water records")
        for kind in ("Configs", "GlobalConfigs", "Sectors"):
            records = water.get(kind, [])
            if not isinstance(records, list):
                raise ValueError(f"Water {kind} must be an array: {path}")
            for value in records:
                row = require_object(value, "water record")
                water_records.append((kind, row))
        mesh_candidates: list[tuple[dict[str, object], Path]] = []
        for value in cast(list[object], data["MeshExports"]):
            row = require_object(value, "mesh export")
            file = row.get("File")
            if not file:
                continue
            if not isinstance(file, str):
                raise ValueError(f"Expected a nonempty resource path relative to {path.parent}")
            candidate = Path(file).expanduser()
            mesh_candidates.append((row, candidate if candidate.is_absolute() else path.parent / candidate))
        existing_meshes = resolve_existing_export_files(tuple(candidate for _, candidate in mesh_candidates))
        for row, candidate in mesh_candidates:
            if str(candidate) in existing_meshes:
                mesh_keys.add(reference_key(require_object(row["Asset"], "mesh asset")))
        containers.extend(require_object(value, "container") for value in cast(list[object], data["Containers"]))
    for kind, row in water_records:
        header = require_object(row.get("Header"), "water header")
        key = reference_key(require_object(header.get("Asset"), "water asset"))
        dependencies = ("FlowMap", "NormalMapArray") if kind == "GlobalConfigs" else (
            ("WaterSectorTexture0",) if kind == "Sectors" else ())
        complete = True
        for field in dependencies:
            pointer = require_object(row.get(field), f"water {field} pointer")
            if pointer.get("IsNull") is True:
                complete = False
                continue
            target = reference_key(require_object(pointer.get("Target"), f"water {field} target"))
            if target not in (array_keys if field == "NormalMapArray" else textures):
                complete = False
        if complete:
            water_keys.add(key)
    ready = set(observed_effects)
    for row in containers:
        if row.get("Asset") is None:
            continue
        asset = require_object(row["Asset"], "container asset")
        key = reference_key(asset)
        container = str(row["Container"]).replace("\\", "/").casefold()
        if not selects_container(container):
            continue
        if asset.get("Type") == "Mesh" and key in mesh_keys:
            ready.add(container + "##" + str(asset["Name"]).casefold())
            if container.endswith(".asset"):
                ready.add(container)
        elif asset.get("Type") == "Material" and key in materials:
            material = materials[key]
            slots = material["Textures"]
            has_sweep = (surface_family(material) == "lit"
                         and material["Floats"].get("_EnableEmissiveAnimSweep", 0) > 0.5)
            sweep_metadata_ready = not has_sweep or (
                "RenderState" in material and (
                    material["Floats"].get("_EmissiveMaskChannel") != 0 or all(
                        slot["Property"] != "_EmissiveMap" or slot["Texture"] is None
                        or "ColorSpace" in slot["Texture"] for slot in slots
                    )
                )
            )
            if (sweep_metadata_ready and not _shader_default_issues(material)
                    and all(slot["Texture"] is None or reference_key(slot["Texture"]) in textures for slot in slots)):
                ready.add(container)
        elif asset.get("Type") == "MonoBehaviour" and (key in water_keys or container in terrain_metadata_paths):
            ready.add(container)
        elif asset.get("Type") == "Texture2D" and key in textures:
            ready.add(container)
        elif asset.get("Type") == "Texture2DArray" and key in array_keys:
            ready.add(container)
    return frozenset(ready)


def ready_texture_arrays(manifest: dict[str, object], directory: Path) -> frozenset[tuple[str, int]]:
    """Require every distinct array layer; a single layer is not a ready array."""
    rows = manifest.get("TextureArrayExports", [])
    if not isinstance(rows, list):
        raise ValueError("TextureArrayExports must be an array")
    ready: set[tuple[str, int]] = set()
    for value in rows:
        row = require_object(value, "texture array")
        key = reference_key(require_object(row.get("Asset"), "texture array asset"))
        depth, slices = row.get("Depth"), row.get("Slices")
        if type(depth) is not int or depth <= 0 or not isinstance(slices, list) or len(slices) != depth:
            raise ValueError(f"Texture array layer count is invalid: reference={key}, depth={depth}")
        indices: set[int] = set()
        complete = True
        for item in slices:
            layer = require_object(item, "texture array layer")
            index = layer.get("SliceIndex")
            if type(index) is not int or not 0 <= index < depth or index in indices:
                raise ValueError(f"Texture array layer index is invalid: reference={key}, index={index}")
            if reference_key(require_object(layer.get("Asset"), "layer asset")) != key:
                raise ValueError(f"Texture array layer identity differs: reference={key}, index={index}")
            indices.add(index)
            file = layer.get("File")
            if layer.get("Error") or file is None:
                complete = False
            elif not resolve_export_path(resolve_path(file, directory)).is_file():
                complete = False
        if complete:
            ready.add(key)
    return frozenset(ready)


def _provider_index(index: Path) -> _ProviderIndex:
    source = resolve_scene_read_path(index)

    def build() -> _ProviderIndex:
        providers: dict[str, set[str]] = {}
        last: dict[str, object] = {}
        with source.open(encoding="utf-8-sig") as stream:
            for number, line in enumerate(stream, 1):
                row = require_object(json.loads(line), f"provider record {number}")
                last = row
                if row.get("kind") != "match":
                    continue
                name, containers = row.get("logical_name"), row.get("containers")
                if not isinstance(name, str) or not isinstance(containers, list):
                    raise ValueError(f"Invalid provider record: {source}:{number}")
                for container in containers:
                    if not isinstance(container, str):
                        raise ValueError(f"Non-string provider container: {source}:{number}")
                    path = container.replace("\\", "/").casefold()
                    providers.setdefault(path, set()).add(name.casefold())
        if last.get("kind") != "complete" or last.get("failed") != 0:
            raise ValueError(f"Provider scan must finish without failures: {source}; last={last}")
        frozen = MappingProxyType({path: tuple(sorted(names)) for path, names in providers.items()})
        return _ProviderIndex(frozen)

    return cached_scene_source_value(_PROVIDER_INDEX_CACHE, (source,), (), build)


def exact_providers(index: Path, requested: frozenset[str]) -> dict[str, tuple[str, ...]]:
    providers = _provider_index(index).providers
    return {path: providers.get(path, ()) for path in requested}


def validate_scan(path: Path) -> None:
    last: dict[str, object] = {}
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            last = require_object(json.loads(line), "provider scan record")
    if last.get("kind") != "complete" or last.get("failed") != 0:
        raise ValueError(f"Provider scan must finish without failures: {path}; last={last}")


def ensure_scene_resources(runtime_path: Path, plan_path: Path,
                           check_cancelled: Callable[[], None], progress: Callable[[float, str], None]) -> dict[str, object]:
    """Upgrade existing caches on demand without rebuilding terrain or overwriting scenes."""
    from endfield_scene_export import run_scene_process

    runtime, plan = read_json(runtime_path), read_json(plan_path)
    extraction = require_object(runtime["extraction"], "runtime extraction")
    values = plan.get("required_resource_paths")
    if not isinstance(values, list) or any(
        not isinstance(v, str) or not v.replace("\\", "/").casefold().startswith(("assets/", "packages/")) for v in values
    ):
        raise ValueError("Scene preflight requires exact assets/ or packages/ resource paths")
    requested = frozenset(value.replace("\\", "/").casefold() for value in cast(list[str], values))
    if not requested:
        return {"status": "ready", "requested": 0, "missing": []}
    manifests = tuple(resolve_path(p, plan_path.parent) for p in cast(list[str], plan["scene_manifests"]))
    paths = require_object(runtime["paths"], "runtime paths")
    cache_root = resolve_path(paths["cache_root"], runtime_path.parent)
    if cache_root.is_relative_to(resolve_path(paths["game_root"], runtime_path.parent)):
        raise ValueError(f"Scene planning cache cannot be inside the game: {cache_root}")
    missing = requested - ready_requested_resource_paths(manifests, requested, cache_root)
    from endfield_scene_direct_effects_plan import ready_effect_mesh_paths

    streams = plan.get("required_effect_mesh_paths", [])
    if not isinstance(streams, list) or any(not isinstance(path, str) or path not in requested for path in streams):
        raise ValueError("FX source mesh stream requests must be a subset of required_resource_paths")
    required_streams = frozenset(cast(list[str], streams))
    if required_streams:
        missing |= required_streams - ready_effect_mesh_paths(manifests)
    from endfield_scene_mesh_uv_contract import RequiredUvStreams, ready_uv_stream_paths

    uv_values = plan.get("required_uv_streams", [])
    if not isinstance(uv_values, list):
        raise ValueError("Ordinary UV stream requests must be an array")
    uv_requirements: list[RequiredUvStreams] = []
    for value in uv_values:
        row = require_object(value, "ordinary UV requirement")
        path, channels = row.get("mesh_path"), row.get("channels")
        if (not isinstance(path, str) or path not in requested or not isinstance(channels, list)
                or not channels or any(type(channel) is not int or channel not in (1, 2) for channel in channels)
                or len(set(channels)) != len(channels)):
            raise ValueError(f"Ordinary source UV requirements must identify exact requested mesh channels: requirement={row}")
        uv_requirements.append({"mesh_path": path, "channels": channels})
    required_uv_paths = frozenset(row["mesh_path"] for row in uv_requirements)
    if uv_requirements:
        missing |= required_uv_paths - ready_uv_stream_paths(manifests, tuple(uv_requirements))
    from endfield_scene_prefab_geometry_plan import observed_prefab_geometry_paths
    from endfield_scene_planning_catalog import load_planning_catalog

    prefab_values = plan.get("required_prefab_geometry_paths", [])
    if not isinstance(prefab_values, list) or any(not isinstance(path, str) or path not in requested for path in prefab_values):
        raise ValueError("Ordinary prefab requests must be a subset of required_resource_paths")
    required_prefabs = frozenset(cast(list[str], prefab_values))
    if required_prefabs:
        catalog = load_planning_catalog(manifests, cache_root)
        prefab_ready = observed_prefab_geometry_paths(manifests, required_prefabs, catalog.materials, catalog.textures)
        missing = (missing - required_prefabs) | (required_prefabs - prefab_ready)
    if not missing:
        return {"status": "ready", "requested": len(requested), "missing": []}
    root = resource_cache_root(runtime_path, runtime)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "preparing.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    try:
        index_path = root / "index.json"
        existing = read_json(index_path) if index_path.is_file() else {
            "format": FORMAT, "datasetFingerprint": extraction["mapLayerFingerprint"], "manifests": []}
        paths = require_object(runtime["paths"], "runtime paths")
        helper_cache = resolve_path(paths["cache_root"], runtime_path.parent)
        helpers: SceneHelpers | None = None
        work = root / uuid4().hex
        work.mkdir()
        environment = {**os.environ, "TEMP": str(work), "TMP": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
        discovery = require_object(extraction["sceneDiscovery"], "scene discovery")
        provider = existing.get("providerIndex")
        coverage = PACKAGE_PROVIDER_COVERAGE if any(path.startswith("packages/") for path in missing) else PROVIDER_COVERAGE
        accepted_coverage = {coverage, PACKAGE_PROVIDER_COVERAGE}
        if provider is not None and existing.get("providerCoverage", PROVIDER_COVERAGE) in accepted_coverage:
            record = require_object(provider, "provider descriptor")
            provider_path = resolve_path(record["path"], root)
            if not provider_path.is_relative_to(root) or describe_file(provider_path) != record:
                raise ValueError(f"Provider cache failed integrity validation: {provider_path}")
        elif discovery.get("providerCoverage") in accepted_coverage:
            record = require_object(discovery["scannerResults"], "scanner results")
            provider_path = resolve_path(record["path"], runtime_path.parent)
            if describe_file(provider_path) != record:
                raise ValueError(f"Prepared provider index changed: {provider_path}")
        else:
            check_cancelled()
            helpers = require_prepared_scene_helpers(runtime_path, helper_cache)
            provider_path = work / "providers.jsonl"
            scan_index = resolve_path(extraction["bundleScanIndex"], runtime_path.parent)
            progress(0.05, "Upgrading complete resource provider index once for this prepared cache")
            filters = ["--contains", "assets/"]
            if coverage == PACKAGE_PROVIDER_COVERAGE:
                filters.extend(("--contains", "packages/"))
            run_scene_process([str(helpers.scanner), "--input", str(scan_index), "--output", str(provider_path),
                               *filters, "--progress-every", "1000"],
                              work / "providers.log", environment, check_cancelled)
            validate_scan(provider_path)
            existing = {**existing, "providerIndex": describe_file(provider_path), "providerCoverage": coverage}
            write_index(index_path, existing)
        check_cancelled()
        indexed = _provider_index(provider_path).providers
        missing_provider_paths = frozenset(path.split("##", 1)[0] for path in missing)
        providers = {path: indexed[path] for path in missing_provider_paths}
        roots = sorted({names[0] for names in providers.values() if len(names) == 1})
        unresolved = {path: list(names) for path, names in providers.items() if len(names) != 1}
        result: dict[str, object] = {"requested": len(requested), "missingBefore": len(missing),
                                    "providerRoots": len(roots), "unresolvedProviders": unresolved}
        if roots:
            if helpers is None:
                check_cancelled()
                helpers = require_prepared_scene_helpers(runtime_path, helper_cache)
            cab = resolve_path(require_object(discovery["cabInventory"], "CAB inventory")["path"], runtime_path.parent)
            closures, metadata = read_bundle_closure_names({"resources": roots}, cab)
            bundles = work / "bundles"
            paths = require_object(runtime["paths"], "runtime paths")
            vfs = resolve_path(paths["game_root"], runtime_path.parent) / "Endfield_Data" / "StreamingAssets" / "VFS"
            database = resolve_path(extraction["resourceDatabase"], runtime_path.parent)

            def cancelled() -> bool:
                check_cancelled()
                return False

            progress(0.35, f"Preparing exact resource dependency closure: {len(closures['resources'])} bundles")
            result["extraction"] = extract_candidate_bundles(database, vfs, closures["resources"], bundles,
                                                             progress=None, cancelled=cancelled)
            result["closure"] = metadata
            output = work / "probe"
            run_scene_process([str(helpers.probe), str(bundles), str(output), "--include-unreferenced"],
                              work / "probe.log", environment, check_cancelled)
            manifest = output / "scene_manifest.json"
            if read_json(manifest).get("Format") != "EndfieldSceneProbe/1":
                raise ValueError(f"Invalid supplementary SceneProbe output: {manifest}")
            after = ready_requested_resource_paths((*manifests, manifest), requested, cache_root)
            stream_missing = (required_streams - ready_effect_mesh_paths((*manifests, manifest))) if required_streams else frozenset()
            uv_missing = (required_uv_paths - ready_uv_stream_paths((*manifests, manifest), tuple(uv_requirements))) if uv_requirements else frozenset()
            prefab_missing: frozenset[str] = frozenset()
            if required_prefabs:
                catalog = load_planning_catalog((*manifests, manifest), cache_root)
                prefab_missing = required_prefabs - observed_prefab_geometry_paths(
                    (*manifests, manifest), required_prefabs, catalog.materials, catalog.textures)
            result["missingAfter"] = sorted((requested - after - required_prefabs) | stream_missing | uv_missing | prefab_missing)
            prior = existing["manifests"]
            if not isinstance(prior, list):
                raise ValueError("Supplement index requires a manifest list")
            updated = {**existing, "manifests": [*prior, describe_file(manifest)]}
            write_index(index_path, updated)
        else:
            result["missingAfter"] = sorted(missing)
        result["status"] = "partial" if result["missingAfter"] else "ready"
        write_index(work / "audit.json", result)
        progress(1.0, f"Exact resource preparation {result['status']}; unresolved inputs remain explicit")
        return result
    finally:
        lock.unlink()

def _prefab_bounds_game_generation(game: Path) -> tuple[str, list[dict[str, object]]]:
    """Reuse the existing executable and VFS generation guard for metadata caches."""
    from endfield_character_sources import game_source_stamps

    stamps = game_source_stamps(game)
    digest = hashlib.sha256(json.dumps(stamps, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest, stamps


def ensure_prefab_bounds_manifests(
    runtime_path: Path, requested: frozenset[str], check_cancelled: Callable[[], None],
) -> tuple[Path, ...]:
    """Reuse exact bounds and unpack each missing dependency once per preparation."""
    import shutil
    from collections import Counter
    from endfield_scene_export import run_scene_process
    from endfield_scene_prefab_bounds_cache import (
        available_bounds_prefabs, bounds_file_stamp, link_bounds_bundles, verified_bounds_manifest,
    )

    if not requested:
        return ()
    if any(not path.startswith(("assets/", "packages/")) or not path.endswith(".prefab") for path in requested):
        raise ValueError("Prefab bounds requests require normalized exact prefab asset paths")
    runtime = read_json(runtime_path)
    extraction = require_object(runtime["extraction"], "bounds runtime extraction")
    paths = require_object(runtime["paths"], "bounds runtime paths")
    source_root = resource_cache_root(runtime_path, runtime)
    discovery = require_object(extraction["sceneDiscovery"], "bounds scene discovery")
    resource_index = source_root / "index.json"
    existing = read_json(resource_index) if resource_index.is_file() else {}
    descriptor = existing.get("providerIndex")
    coverage = existing.get("providerCoverage")
    if descriptor is None:
        descriptor = discovery.get("scannerResults")
        coverage = discovery.get("providerCoverage")
    if coverage not in (PROVIDER_COVERAGE, PACKAGE_PROVIDER_COVERAGE) or (any(path.startswith("packages/") for path in requested) and coverage != PACKAGE_PROVIDER_COVERAGE):
        raise ValueError(f"Prefab bounds require a complete prepared provider index: coverage={coverage}, runtime={runtime_path}")
    provider = require_object(descriptor, "bounds provider descriptor")
    provider_path = resolve_path(provider["path"], runtime_path.parent)
    helpers = require_prepared_scene_helpers(runtime_path, resolve_path(paths["cache_root"], runtime_path.parent))
    probe_identity = str(describe_file(helpers.probe)["sha256"])
    game = resolve_path(paths["game_root"], runtime_path.parent)
    game_identity, game_stamps = _prefab_bounds_game_generation(game)
    root = source_root / "prefab_bounds" / probe_identity[:16] / game_identity
    root.mkdir(parents=True, exist_ok=True)
    game_sources = root / "game_sources.json"
    generation = {"format": "EndfieldPrefabBoundsGameSources/1", "sha256": game_identity, "stamps": game_stamps}
    if game_sources.is_file():
        if read_json(game_sources) != generation:
            raise ValueError(f"Prefab bounds game source generation differs: path={game_sources}")
    else:
        write_index(game_sources, generation)
    selection_key = hashlib.sha256(json.dumps(
        {"requested": sorted(requested), "provider": provider, "providerStat": bounds_file_stamp(provider_path)},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    selection_path = root / "selections" / (selection_key + ".json")
    if selection_path.is_file():
        selection = read_json(selection_path)
        if (selection.get('format') != 'EndfieldPrefabBoundsSelectionInputs/2'
                or selection.get('requested') != sorted(requested)
                or not isinstance(selection.get('manifests'), list)):
            raise ValueError(f'Invalid prefab bounds selection cache: path={selection_path}')
        selected: list[Path] = []
        for value in cast(list[object], selection['manifests']):
            row = require_object(value, 'selected bounds manifest')
            descriptor = require_object(row['manifest'], 'selected bounds manifest descriptor')
            manifest = resolve_path(descriptor['path'], root)
            if bounds_file_stamp(manifest) != row.get('stat'):
                raise ValueError(f'Prefab bounds selection source changed: manifest={manifest}')
            selected.append(manifest)
        if _prefab_bounds_game_generation(game)[0] != game_identity:
            raise OSError(f'Game files changed during prefab bounds cache lookup: game={game}')
        return tuple(selected)
    if describe_file(provider_path) != provider:
        raise ValueError(f"Prefab bounds provider index changed: path={provider_path}")
    manifests: list[Path] = []
    descriptions: dict[Path, dict[str, object]] = {}
    remaining = requested
    for receipt in sorted(root.glob('*/receipt.json')):
        check_cancelled()
        if not remaining:
            break
        manifest = verified_bounds_manifest(receipt, root, probe_identity, game_identity)
        covered = available_bounds_prefabs(manifest, remaining)
        if covered:
            manifests.append(manifest)
            descriptions[manifest] = require_object(read_json(receipt)['manifest'], 'cached bounds manifest')
            remaining = remaining - covered
    prepared: list[dict[str, object]] = []
    for value in require_object(extraction.get('maps'), 'bounds runtime maps').values():
        descriptor = require_object(value, 'bounds runtime map').get('sceneProbe')
        if descriptor is not None:
            prepared.append(require_object(descriptor, 'prepared bounds manifest'))
    for value in cast(list[object], existing.get('manifests', [])):
        prepared.append(require_object(value, 'supplemental bounds manifest'))
    for descriptor in prepared:
        check_cancelled()
        if not remaining:
            break
        manifest = resolve_path(descriptor['path'], runtime_path.parent)
        if manifest in descriptions:
            continue
        covered = available_bounds_prefabs(manifest, remaining)
        if covered:
            if describe_file(manifest) != descriptor:
                raise ValueError(f'Prepared prefab bounds manifest changed: path={manifest}')
            manifests.append(manifest)
            descriptions[manifest] = descriptor
            remaining = remaining - covered
    providers = exact_providers(provider_path, remaining) if remaining else {}
    unresolved = {path: list(names) for path, names in providers.items() if len(names) != 1}
    roots = sorted({names[0] for names in providers.values() if len(names) == 1})
    missing = {'v2_' + hashlib.sha256("\n".join(roots[index:index + 16]).encode()).hexdigest()[:24]: roots[index:index + 16]
               for index in range(0, len(roots), 16)}
    lock = root / "preparing.lock"
    if missing:
        with lock.open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        pool: Path | None = None
        try:
            cab = resolve_path(require_object(discovery["cabInventory"], "bounds CAB inventory")["path"], runtime_path.parent)
            closures, metadata = read_bundle_closure_names(missing, cab)
            vfs = resolve_path(paths["game_root"], runtime_path.parent) / "Endfield_Data" / "StreamingAssets" / "VFS"
            database = resolve_path(extraction["resourceDatabase"], runtime_path.parent)
            pool = root / ('bundle_pool_' + uuid4().hex)
            pool.mkdir()
            available: dict[str, Path] = {}
            uses = Counter(name for names in closures.values() for name in names)
            for key, names in missing.items():
                check_cancelled()
                work = root / key / uuid4().hex
                work.mkdir(parents=True)
                bundles = work / "bundles"
                environment = {**os.environ, "TEMP": str(work), "TMP": str(work), "PYTHONDONTWRITEBYTECODE": "1"}

                def cancelled() -> bool:
                    check_cancelled()
                    return False

                new_names = [name for name in closures[key] if name not in available]
                extracted = pool / key
                extraction_report = (extract_candidate_bundles(database, vfs, new_names, extracted,
                    progress=None, cancelled=cancelled) if new_names else
                    {'format': 'EndfieldCandidateBundleSet/1', 'requested_count': '0', 'extracted_count': '0', 'bytes': '0'})
                available.update({name: extracted / name for name in new_names})
                link_bounds_bundles(available, closures[key], bundles)
                output = work / "probe"
                run_scene_process([str(helpers.probe), str(bundles), str(output), "--manifest-only"],
                                  work / "probe.log", environment, check_cancelled)
                manifest = output / "scene_manifest.json"
                payload = read_json(manifest)
                if payload.get("Format") != "EndfieldSceneProbe/1" or not isinstance(payload.get("MeshBounds"), list):
                    raise ValueError(f"Prefab bounds probe lacks source mesh bounds: path={manifest}")
                descriptor = describe_file(manifest)
                write_index(root / key / "receipt.json", {"format": "EndfieldPrefabBoundsBatch/2", "roots": names,
                    "probeSha256": probe_identity, "gameSourceSha256": game_identity,
                    "manifest": descriptor, "manifestStat": bounds_file_stamp(manifest), "extraction": extraction_report,
                    "sharedBundleCount": len(closures[key]) - len(new_names),
                    "closure": metadata["maps"][key]})
                manifests.append(manifest)
                descriptions[manifest] = descriptor
                resolved_bundles = bundles.resolve()
                if resolved_bundles.parent != work.resolve() or not resolved_bundles.is_relative_to(root.resolve()):
                    raise ValueError(f"Prefab bounds temporary bundle deletion escaped its cache: path={resolved_bundles}")
                shutil.rmtree(resolved_bundles)
                for name in closures[key]:
                    uses[name] -= 1
                    if uses[name] == 0:
                        available.pop(name).unlink()
        finally:
            try:
                if pool is not None and pool.is_dir():
                    resolved_pool = pool.resolve()
                    if resolved_pool.parent != root.resolve() or not resolved_pool.name.startswith('bundle_pool_'):
                        raise ValueError(f'Prefab bounds shared bundle cleanup escaped its cache: path={resolved_pool}')
                    shutil.rmtree(resolved_pool)
            finally:
                lock.unlink()
    if _prefab_bounds_game_generation(game)[0] != game_identity:
        raise OSError(f"Game files changed during prefab bounds preparation; refresh prepared sources after the update: game={game}")
    write_index(root / "selection_inputs.json", {"format": "EndfieldPrefabBoundsInputs/1", "requested": sorted(requested),
        "gameSources": describe_file(game_sources), "unresolvedProviders": unresolved,
        "manifests": [descriptions[manifest] for manifest in manifests]})
    selection_path.parent.mkdir(exist_ok=True)
    write_index(selection_path, {'format': 'EndfieldPrefabBoundsSelectionInputs/2', 'requested': sorted(requested),
        'unresolvedProviders': unresolved,
        'manifests': [{'manifest': descriptions[manifest], 'stat': bounds_file_stamp(manifest)} for manifest in manifests]})
    return tuple(manifests)
