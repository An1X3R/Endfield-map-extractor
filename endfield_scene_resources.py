"""Complete exact scene resources using current bundle providers and existing probes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable, cast
from uuid import uuid4

from endfield_scene_material_contract import load_catalog, reference_key, require_object, resolve_export_path
from extractor_core.resources import extract_candidate_bundles, read_bundle_closure_names


FORMAT = "EndfieldSceneResourceCache/1"
PROVIDER_COVERAGE = "all_asset_containers"
PACKAGE_PROVIDER_COVERAGE = "all_asset_and_package_containers"


def read_json(path: Path) -> dict[str, object]:
    return require_object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))


def resolve_path(value: object, parent: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected a nonempty resource path relative to {parent}")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else parent / path).resolve()


def describe_file(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest().upper()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest}


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
    from endfield_scene_effects_plan import observed_effect_prefabs

    materials, textures = load_catalog(manifests)
    mesh_keys: set[tuple[str, int]] = set()
    water_keys: set[tuple[str, int]] = set()
    array_keys: set[tuple[str, int]] = set()
    water_records: list[tuple[str, dict[str, object]]] = []
    containers: list[dict[str, object]] = []
    for path in manifests:
        data = read_json(path)
        array_keys.update(ready_texture_arrays(data, path.parent))
        water = require_object(data.get("Water", {}), "water records")
        for kind in ("Configs", "GlobalConfigs", "Sectors"):
            records = water.get(kind, [])
            if not isinstance(records, list):
                raise ValueError(f"Water {kind} must be an array: {path}")
            for value in records:
                row = require_object(value, "water record")
                water_records.append((kind, row))
        for value in cast(list[object], data["MeshExports"]):
            row = require_object(value, "mesh export")
            if row.get("File") and resolve_export_path(resolve_path(row["File"], path.parent)).is_file():
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
    ready = set(observed_effect_prefabs(manifests))
    for row in containers:
        if row.get("Asset") is None:
            continue
        asset = require_object(row["Asset"], "container asset")
        key = reference_key(asset)
        container = str(row["Container"]).replace("\\", "/").casefold()
        if asset.get("Type") == "Mesh" and key in mesh_keys:
            ready.add(container + "##" + str(asset["Name"]).casefold())
            if container.endswith(".asset"):
                ready.add(container)
        elif asset.get("Type") == "Material" and key in materials:
            slots = materials[key]["Textures"]
            if all(slot["Texture"] is None or reference_key(slot["Texture"]) in textures for slot in slots):
                ready.add(container)
        elif asset.get("Type") == "MonoBehaviour" and key in water_keys:
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


def exact_providers(index: Path, requested: frozenset[str]) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {path: set() for path in requested}
    with index.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            row = require_object(json.loads(line), f"provider record {number}")
            if row.get("kind") != "match":
                continue
            name, containers = row.get("logical_name"), row.get("containers")
            if not isinstance(name, str) or not isinstance(containers, list):
                raise ValueError(f"Invalid provider record: {index}:{number}")
            for container in containers:
                if not isinstance(container, str):
                    raise ValueError(f"Non-string provider container: {index}:{number}")
                path = container.replace("\\", "/").casefold()
                if path in result:
                    result[path].add(name.casefold())
    return {path: tuple(sorted(names)) for path, names in result.items()}


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
    manifests = tuple(resolve_path(p, plan_path.parent) for p in cast(list[str], plan["scene_manifests"]))
    missing = requested - ready_resource_paths(manifests)
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
        state = read_json(runtime_path.parent / "first_run_state.json")
        steps = require_object(state["steps"], "first-run steps")
        helper_step = require_object(steps["helper_dependencies"], "helper dependency step")
        helper_result = require_object(helper_step["outputs"], "helper result")
        helpers = require_object(helper_result["outputs"], "helpers")
        scanner = resolve_path(require_object(helpers["bundleScanner"], "scanner")["path"], runtime_path.parent)
        probe = resolve_path(require_object(helpers["sceneProbe"], "probe")["path"], runtime_path.parent)
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
            provider_path = work / "providers.jsonl"
            scan_index = resolve_path(extraction["bundleScanIndex"], runtime_path.parent)
            progress(0.05, "Upgrading complete resource provider index once for this prepared cache")
            filters = ["--contains", "assets/"]
            if coverage == PACKAGE_PROVIDER_COVERAGE:
                filters.extend(("--contains", "packages/"))
            run_scene_process([str(scanner), "--input", str(scan_index), "--output", str(provider_path),
                               *filters, "--progress-every", "1000"],
                              work / "providers.log", environment, check_cancelled)
            validate_scan(provider_path)
            existing = {**existing, "providerIndex": describe_file(provider_path), "providerCoverage": coverage}
            write_index(index_path, existing)
        validate_scan(provider_path)
        check_cancelled()
        providers = exact_providers(provider_path, frozenset(path.split("##", 1)[0] for path in missing))
        roots = sorted({names[0] for names in providers.values() if len(names) == 1})
        unresolved = {path: list(names) for path, names in providers.items() if len(names) != 1}
        result: dict[str, object] = {"requested": len(requested), "missingBefore": len(missing),
                                    "providerRoots": len(roots), "unresolvedProviders": unresolved}
        if roots:
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
            run_scene_process([str(probe), str(bundles), str(output), "--include-unreferenced"],
                              work / "probe.log", environment, check_cancelled)
            manifest = output / "scene_manifest.json"
            if read_json(manifest).get("Format") != "EndfieldSceneProbe/1":
                raise ValueError(f"Invalid supplementary SceneProbe output: {manifest}")
            after = ready_resource_paths((*manifests, manifest))
            result["missingAfter"] = sorted(requested - after)
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
