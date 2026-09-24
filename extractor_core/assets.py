"""Build portable Map01/Map02 asset-resolution tables from a bundle scan."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


class AssetResolutionError(RuntimeError):
    pass


ASSET_RESOLUTION_POLICY_VERSION = "2"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _is_hlod_name(value: str | None) -> bool:
    folded = str(value or "").casefold().replace("\\", "/")
    return bool(re.search(r"(?:^|[/_])hlod\d+(?:[/_]|$)", folded))


def _lod_kind(value: str | None) -> str:
    if _is_hlod_name(value):
        return "hlod"
    folded = str(value or "").casefold()
    match = re.search(r"_lod(\d+)(?:_|$)", folded)
    return f"lod{int(match.group(1))}" if match else "non_lod"


def canonical_asset_name(value: str) -> str:
    normalized = value.casefold().replace("\\", "/").rsplit("/", 1)[-1]
    normalized = normalized.split("##")[-1]
    normalized = re.sub(r"\.(?:prefab|fbx|asset)$", "", normalized)
    normalized = re.sub(r"^[ps]_", "", normalized)
    normalized = re.sub(r"_(?:lod[0-9]+|ecsmerged)$", "", normalized)
    normalized = normalized.replace("+", "_")
    replacements = (
        ("mod_map02_", "module_"),
        ("module_map02_", "module_"),
        ("model_map02_", "module_"),
        ("prop_map02_", "prop_"),
        ("rock_map02_", "rock_"),
        ("build_map02_", "building_"),
        ("building_map02_", "building_"),
        ("road_map02_", "road_"),
        ("cloth_map02_", "cloth_"),
        ("mod_map01_", "module_"),
        ("module_map01_", "module_"),
        ("model_map01_", "module_"),
        ("prop_map01_", "prop_"),
        ("rock_map01_", "rock_"),
        ("build_map01_", "building_"),
        ("building_map01_", "building_"),
        ("road_map01_", "road_"),
        ("cloth_map01_", "cloth_"),
        ("meshdecal_", "mdecal_"),
        ("mondule_", "module_"),
    )
    for old, new in replacements:
        normalized = normalized.replace(old, new)
    normalized = re.sub(r"(^|_)common(?=_)", r"\1com", normalized)
    return re.sub(r"_+", "_", normalized).strip("_")


def canonical_variants(value: str) -> list[str]:
    primary = canonical_asset_name(value)
    shared = re.sub(r"(_\d{2})[a-z]$", r"\1", primary)
    return list(dict.fromkeys((primary, shared)))


def prefab_names(base: str, map_id: str) -> list[str]:
    lower = base.casefold()
    result = [lower + ".prefab"]
    common = (
        ("p_module_", f"p_mod_{map_id}_"),
        ("p_prop_", f"p_prop_{map_id}_"),
        ("p_rock_", f"p_rock_{map_id}_"),
        ("p_building_", f"p_build_{map_id}_"),
    )
    for old, new in common:
        if lower.startswith(old) and not lower[len(old) :].startswith(("map01_", "map02_")):
            result.append(new + lower[len(old) :] + ".prefab")
    return list(dict.fromkeys(result))


def _candidate_rank(container: str, map_id: str, subasset: str | None = None) -> tuple[Any, ...]:
    folded = container.casefold().replace("\\", "/")
    own = f"/{map_id}/" in folded or f"{map_id}_" in folded
    other_id = "map02" if map_id == "map01" else "map01"
    other = f"/{other_id}/" in folded or f"{other_id}_" in folded
    name = (subasset or "").casefold()
    lod_match = re.search(r"_lod(\d+)(?:_|$)", name)
    lod = int(lod_match.group(1)) if lod_match else 99
    rejected = any(token in name for token in ("shadowproxy", "cardmesh", "imposterlod"))
    return (
        0 if own else 2 if other else 1,
        1 if _is_hlod_name(name) else 0,
        1 if rejected else 0,
        lod,
        folded,
        name,
    )


def _read_matches(path: Path) -> list[dict[str, Any]]:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Bundle scan result is missing: {source}")
    matches: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise AssetResolutionError(
                    f"Invalid bundle scan JSONL at line {line_number}: {source}"
                ) from error
            if row.get("kind") == "match":
                matches.append(row)
    return matches


def _probe_key(asset: dict[str, Any]) -> tuple[str, int] | None:
    source = asset.get("Source")
    path_id = asset.get("PathId")
    if not isinstance(source, str) or path_id is None:
        return None
    try:
        return source.casefold(), int(path_id)
    except (TypeError, ValueError):
        return None


def _probe_row(root_cab: str, manifest_path: Path | None = None, **metadata: Any) -> dict[str, Any]:
    return {
        "logical_name": None,
        "serialized": [{"name": root_cab}],
        "source_kind": "scene_probe",
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
        **metadata,
    }


def _read_scene_probe(
    path: Path | None,
) -> tuple[
    dict[str, list[tuple[str | None, dict[str, Any]]]],
    dict[str, list[tuple[str | None, dict[str, Any]]]],
    dict[str, int],
    list[dict[str, Any]],
]:
    if path is None:
        return {}, {}, {"meshCount": 0, "prefabCount": 0, "rendererFallbackCount": 0, "hlodCount": 0}, []
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SceneProbe manifest is missing: {source}")
    manifest = json.loads(source.read_text(encoding="utf-8-sig"))
    if manifest.get("Format") != "EndfieldSceneProbe/1":
        raise AssetResolutionError(f"Unsupported SceneProbe manifest: {source}")

    containers_by_asset: dict[tuple[str, int], list[str]] = defaultdict(list)
    prefab_candidates: dict[str, list[tuple[str | None, dict[str, Any]]]] = defaultdict(list)
    for entry in manifest.get("Containers") or []:
        container = entry.get("Container")
        asset = entry.get("Asset") or {}
        key = _probe_key(asset)
        if not isinstance(container, str) or key is None:
            continue
        containers_by_asset[key].append(container)
        if container.casefold().endswith(".prefab"):
            root_cab = str(asset.get("Source") or entry.get("DeclaredBy") or "")
            if root_cab:
                row = _probe_row(root_cab, source, evidence_level="probe_prefab_container")
                prefab_candidates[Path(container).name.casefold()].append((container, row))
                prefab_candidates[canonical_asset_name(Path(container).name)].append((container, row))

    model_candidates: dict[str, list[tuple[str | None, str, dict[str, Any]]]] = defaultdict(list)
    nodes_by_transform: dict[tuple[str, int], dict[str, Any]] = {}
    for node in manifest.get("Nodes") or []:
        source_name = node.get("Source")
        transform_id = node.get("TransformPathId")
        if isinstance(source_name, str) and transform_id is not None:
            try:
                nodes_by_transform[(source_name.casefold(), int(transform_id))] = node
            except (TypeError, ValueError):
                pass
    renderer_fallback_count = 0
    for root_node in manifest.get("Nodes") or []:
        node_name = root_node.get("Name")
        source_name = root_node.get("Source")
        if not isinstance(node_name, str) or not isinstance(source_name, str):
            continue
        source_key = source_name.casefold()
        queue = list(root_node.get("Children") or [])
        seen_transforms: set[int] = set()
        renderer_nodes: list[dict[str, Any]] = []
        while queue:
            child_ref = queue.pop()
            transform_id = child_ref.get("PathId") if isinstance(child_ref, dict) else None
            if transform_id is None:
                continue
            try:
                transform_id = int(transform_id)
            except (TypeError, ValueError):
                continue
            if transform_id in seen_transforms:
                continue
            seen_transforms.add(transform_id)
            child_node = nodes_by_transform.get((source_key, transform_id))
            if child_node is None:
                continue
            if child_node.get("RendererType") == "MeshRenderer" and child_node.get("Mesh"):
                renderer_nodes.append(child_node)
            queue.extend(child_node.get("Children") or [])
        if root_node.get("Mesh"):
            renderer_nodes.insert(0, root_node)
        for renderer_node in renderer_nodes:
            mesh = renderer_node.get("Mesh") or {}
            mesh_name = mesh.get("Name")
            mesh_source = mesh.get("Source")
            if not isinstance(mesh_name, str) or not isinstance(mesh_source, str):
                continue
            if _is_hlod_name(mesh_name):
                continue
            mesh_paths = containers_by_asset.get(_probe_key(mesh), [])
            container = min(mesh_paths, key=lambda value: value.casefold()) if mesh_paths else None
            model_candidates[canonical_asset_name(node_name)].append(
                (
                    container,
                    mesh_name,
                    _probe_row(
                        mesh_source,
                        source,
                        evidence_level="probe_renderer_mesh_exact",
                        mesh_ref={
                            "Source": mesh_source,
                            "PathId": mesh.get("PathId"),
                            "Name": mesh_name,
                        },
                        renderer_ref={
                            "Source": renderer_node.get("Source"),
                            "PathId": renderer_node.get("PathId"),
                            "Name": renderer_node.get("Name"),
                        },
                    ),
                )
            )
            renderer_fallback_count += 1
    mesh_count = 0
    hlod_rows: list[dict[str, Any]] = []
    for entry in manifest.get("MeshExports") or []:
        asset = entry.get("Asset") or {}
        if str(asset.get("Type", "")).casefold() != "mesh":
            continue
        name = asset.get("Name")
        source_cab = asset.get("Source")
        if not isinstance(name, str) or not isinstance(source_cab, str):
            continue
        if _is_hlod_name(name):
            key = _probe_key(asset)
            paths = containers_by_asset.get(key, []) if key is not None else []
            hlod_rows.append(
                {
                    "source_file": source_cab,
                    "source_path_id": str(asset.get("PathId")) if asset.get("PathId") is not None else None,
                    "source_name": name,
                    "container_path": min(paths, key=lambda value: value.casefold()) if paths else None,
                    "mesh_file": entry.get("File"),
                    "manifest_path": str(source),
                }
            )
            continue
        key = _probe_key(asset)
        paths = containers_by_asset.get(key, []) if key is not None else []
        container = min(paths, key=lambda value: value.casefold()) if paths else None
        row = _probe_row(
            source_cab,
            source,
            evidence_level="probe_mesh_name_candidate",
            mesh_ref={
                "Source": source_cab,
                "PathId": asset.get("PathId"),
                "Name": name,
            },
        )
        model_candidates[canonical_asset_name(name)].append((container, name, row))
        mesh_count += 1

    counts = {
        "meshCount": mesh_count,
        "prefabCount": len({path for values in prefab_candidates.values() for path, _ in values}),
        "rendererFallbackCount": renderer_fallback_count,
        "hlodCount": len(hlod_rows),
    }
    return model_candidates, prefab_candidates, counts, hlod_rows


def _probe_candidate_rank(
    container: str | None, subasset: str | None = None
) -> tuple[Any, ...]:
    folded = str(container or "").casefold().replace("\\", "/")
    name = (subasset or "").casefold()
    lod_match = re.search(r"_lod(\d+)(?:_|$)", name)
    lod = int(lod_match.group(1)) if lod_match else 99
    rejected = any(token in name for token in ("shadowproxy", "cardmesh", "imposterlod"))
    return (1 if _is_hlod_name(name) else 0, 1 if rejected else 0, lod, folded, name)


def build_asset_resolution_database(
    map_id: str,
    instances: Path,
    resource_database: Path,
    bundle_scan_results: Path,
    output: Path,
    scene_probe_manifest: Path | None = None,
) -> dict[str, Any]:
    """Resolve entity bases from first-run outputs without historical databases."""

    if map_id not in {"map01", "map02"}:
        raise ValueError(f"Unsupported map id: {map_id}")
    instance_path = instances.expanduser().resolve()
    resource_path = resource_database.expanduser().resolve()
    target = output.expanduser().resolve()
    if not instance_path.is_file():
        raise FileNotFoundError(f"Instance database is missing: {instance_path}")
    if not resource_path.is_file():
        raise FileNotFoundError(f"Resource database is missing: {resource_path}")
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite asset database: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    scan_by_container: dict[str, tuple[str, dict[str, Any]]] = {}
    prefab_by_name: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    prefab_by_canonical: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for row in _read_matches(bundle_scan_results):
        for container_value in row.get("containers") or []:
            if not isinstance(container_value, str):
                continue
            folded = container_value.casefold()
            existing = scan_by_container.get(folded)
            if existing is None or _candidate_rank(container_value, map_id) < _candidate_rank(
                existing[0], map_id
            ):
                scan_by_container[folded] = (container_value, row)
            if folded.endswith(".prefab"):
                prefab_by_name[Path(container_value).name.casefold()].append((container_value, row))
                prefab_by_canonical[canonical_asset_name(Path(container_value).name)].append(
                    (container_value, row)
                )

    model_by_canonical: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
    resources = sqlite3.connect(f"file:{resource_path}?mode=ro", uri=True)
    try:
        for (path_value,) in resources.execute(
            "SELECT path FROM paths WHERE path_folded LIKE '%##%_lod%'"
        ):
            if "##" not in path_value:
                continue
            container, subasset = str(path_value).rsplit("##", 1)
            if _is_hlod_name(subasset):
                continue
            pair = scan_by_container.get(container.casefold())
            if pair is not None:
                model_by_canonical[canonical_asset_name(subasset)].append(
                    (container, subasset, pair[1])
                )
    finally:
        resources.close()

    probe_models, probe_prefabs, probe_counts, probe_hlod_rows = _read_scene_probe(scene_probe_manifest)

    source = sqlite3.connect(f"file:{instance_path}?mode=ro", uri=True)
    try:
        bases = list(
            source.execute(
                "SELECT entity_base,COUNT(*) FROM instances WHERE entity_base IS NOT NULL "
                "AND entity_base != '' GROUP BY entity_base ORDER BY entity_base"
            )
        )
    finally:
        source.close()

    connection = sqlite3.connect(target)
    stats: Counter[str] = Counter()
    covered: Counter[str] = Counter()
    binding_rows: list[tuple[Any, ...]] = []
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE resolutions (
                entity_base TEXT PRIMARY KEY, instance_count INTEGER NOT NULL,
                method TEXT NOT NULL, container_path TEXT, logical_name TEXT,
                root_cab TEXT, model_asset_name TEXT
            );
            CREATE INDEX resolutions_method_idx ON resolutions(method);
            CREATE TABLE asset_bindings (
                entity_base TEXT PRIMARY KEY, instance_count INTEGER NOT NULL,
                binding_status TEXT NOT NULL, source_kind TEXT NOT NULL,
                evidence_level TEXT NOT NULL, lod_kind TEXT NOT NULL,
                source_file TEXT, source_path_id TEXT, source_name TEXT,
                container_path TEXT, root_cab TEXT, model_asset_name TEXT,
                renderer_source TEXT, renderer_path_id TEXT, renderer_name TEXT,
                manifest_path TEXT, logical_name TEXT
            );
            CREATE INDEX asset_bindings_source_idx ON asset_bindings(source_kind);
            CREATE INDEX asset_bindings_evidence_idx ON asset_bindings(evidence_level);
            CREATE TABLE hlod_candidates (
                source_file TEXT, source_path_id TEXT, source_name TEXT,
                container_path TEXT, mesh_file TEXT, manifest_path TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO hlod_candidates VALUES (?,?,?,?,?,?)",
            [
                (
                    row.get("source_file"),
                    row.get("source_path_id"),
                    row.get("source_name"),
                    row.get("container_path"),
                    row.get("mesh_file"),
                    row.get("manifest_path"),
                )
                for row in probe_hlod_rows
            ],
        )
        for base_value, count_value in bases:
            base = str(base_value)
            count = int(count_value)
            resolved: tuple[str, str | None, str | None, str | None, str | None] | None = None
            binding_source = "unresolved"
            binding_evidence = "unresolved"
            binding_row: dict[str, Any] = {}
            for name in prefab_names(base, map_id):
                candidates = prefab_by_name.get(name, [])
                if candidates:
                    container, row = min(
                        candidates, key=lambda item: _candidate_rank(item[0], map_id)
                    )
                    serialized = row.get("serialized") or []
                    root_cab = serialized[0].get("name") if serialized else None
                    resolved = ("prefab", container, row.get("logical_name"), root_cab, None)
                    binding_source = "bundle_scanner"
                    binding_evidence = "bundle_prefab_exact"
                    binding_row = row
                    break
            if resolved is None:
                candidates = prefab_by_canonical.get(canonical_asset_name(base), [])
                if candidates:
                    container, row = min(
                        candidates, key=lambda item: _candidate_rank(item[0], map_id)
                    )
                    serialized = row.get("serialized") or []
                    root_cab = serialized[0].get("name") if serialized else None
                    resolved = ("prefab", container, row.get("logical_name"), root_cab, None)
                    binding_source = "bundle_scanner"
                    binding_evidence = "bundle_prefab_canonical"
                    binding_row = row
            if resolved is None:
                for key in canonical_variants(base):
                    candidates = model_by_canonical.get(key, [])
                    if candidates:
                        container, subasset, row = min(
                            candidates,
                            key=lambda item: _candidate_rank(item[0], map_id, item[1]),
                        )
                        serialized = row.get("serialized") or []
                        root_cab = serialized[0].get("name") if serialized else None
                        resolved = (
                            "model",
                            container,
                            row.get("logical_name"),
                            root_cab,
                            subasset,
                        )
                        binding_source = "bundle_scanner"
                        binding_evidence = (
                            "bundle_model_exact" if key == canonical_asset_name(base)
                            else "bundle_model_family_candidate"
                        )
                        binding_row = row
                        break
            if resolved is None:
                for name in prefab_names(base, map_id):
                    candidates = probe_prefabs.get(name, [])
                    if candidates:
                        container, row = min(candidates, key=lambda item: _candidate_rank(item[0], map_id))
                        serialized = row.get("serialized") or []
                        root_cab = serialized[0].get("name") if serialized else None
                        resolved = ("prefab", container, row.get("logical_name"), root_cab, None)
                        binding_source = "scene_probe"
                        binding_evidence = row.get("evidence_level") or "probe_prefab_container"
                        binding_row = row
                        break
            if resolved is None:
                candidates = probe_prefabs.get(canonical_asset_name(base), [])
                if candidates:
                    container, row = min(candidates, key=lambda item: _candidate_rank(item[0], map_id))
                    serialized = row.get("serialized") or []
                    root_cab = serialized[0].get("name") if serialized else None
                    resolved = ("prefab", container, row.get("logical_name"), root_cab, None)
                    binding_source = "scene_probe"
                    binding_evidence = row.get("evidence_level") or "probe_prefab_canonical"
                    binding_row = row
            if resolved is None:
                for key in canonical_variants(base):
                    candidates = probe_models.get(key, [])
                    if candidates:
                        container, subasset, row = min(
                            candidates, key=lambda item: _probe_candidate_rank(item[0], item[1])
                        )
                        serialized = row.get("serialized") or []
                        root_cab = serialized[0].get("name") if serialized else None
                        resolved = ("model", container, row.get("logical_name"), root_cab, subasset)
                        binding_source = "scene_probe"
                        binding_evidence = (
                            row.get("evidence_level") or "probe_mesh_name_candidate"
                            if key == canonical_asset_name(base)
                            else "probe_mesh_family_candidate"
                        )
                        binding_row = row
                        break
            if resolved is None:
                resolved = ("unresolved", None, None, None, None)
            connection.execute(
                "INSERT INTO resolutions VALUES (?,?,?,?,?,?,?)", (base, count, *resolved)
            )
            mesh_ref = binding_row.get("mesh_ref") or {}
            renderer_ref = binding_row.get("renderer_ref") or {}
            binding_rows.append(
                (
                    base,
                    count,
                    "resolved" if resolved[0] != "unresolved" else "unresolved",
                    binding_source,
                    binding_evidence,
                    _lod_kind(resolved[4]) if resolved[4] else "prefab" if resolved[0] == "prefab" else "non_lod",
                    mesh_ref.get("Source"),
                    str(mesh_ref.get("PathId")) if mesh_ref.get("PathId") is not None else None,
                    mesh_ref.get("Name") or resolved[4],
                    resolved[1],
                    resolved[3],
                    resolved[4],
                    renderer_ref.get("Source"),
                    str(renderer_ref.get("PathId")) if renderer_ref.get("PathId") is not None else None,
                    renderer_ref.get("Name"),
                    binding_row.get("manifest_path"),
                    resolved[2],
                )
            )
            stats[resolved[0]] += 1
            covered[resolved[0]] += count

        connection.executemany(
            "INSERT INTO asset_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            binding_rows,
        )
        meta = {
            "format": "EndfieldAutomaticAssetResolutions/1",
            "asset_resolution_policy_version": ASSET_RESOLUTION_POLICY_VERSION,
            "map": map_id,
            "unique_bases": str(len(bases)),
            "instances": str(sum(int(row[1]) for row in bases)),
            "resolution_source": "first_run_bundle_container_scan",
            "scene_probe_manifest": str(scene_probe_manifest.expanduser().resolve())
            if scene_probe_manifest is not None else "",
            "instances_sha256": sha256_file(instance_path),
            "resource_database_sha256": sha256_file(resource_path),
            "bundle_scan_sha256": sha256_file(bundle_scan_results.expanduser().resolve()),
            "scene_probe_manifest_sha256": (
                sha256_file(scene_probe_manifest.expanduser().resolve())
                if scene_probe_manifest is not None else ""
            ),
            "scene_probe_mesh_count": str(probe_counts["meshCount"]),
            "scene_probe_prefab_count": str(probe_counts["prefabCount"]),
            "scene_probe_hlod_count": str(probe_counts["hlodCount"]),
            "asset_binding_count": str(len(binding_rows)),
        }
        connection.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
        connection.commit()
    finally:
        connection.close()

    return {
        "format": "EndfieldAutomaticAssetResolutionSummary/1",
        "asset_resolution_policy_version": ASSET_RESOLUTION_POLICY_VERSION,
        "map": map_id,
        "uniqueBases": len(bases),
        "instances": sum(int(row[1]) for row in bases),
        "uniqueByMethod": dict(sorted(stats.items())),
        "instancesByMethod": dict(sorted(covered.items())),
        "containerCount": len(scan_by_container),
        "prefabNameCount": len(prefab_by_name),
        "modelKeyCount": len(model_by_canonical),
        "probeMeshCount": probe_counts["meshCount"],
        "probePrefabCount": probe_counts["prefabCount"],
        "probeHlodCount": probe_counts["hlodCount"],
        "assetBindingCount": len(binding_rows),
        "probeFallbackEnabled": scene_probe_manifest is not None,
    }
