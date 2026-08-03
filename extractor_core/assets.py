"""Build portable Map01/Map02 asset-resolution tables from a bundle scan."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


class AssetResolutionError(RuntimeError):
    pass


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
    return (0 if own else 2 if other else 1, folded, (subasset or "").casefold())


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


def build_asset_resolution_database(
    map_id: str,
    instances: Path,
    resource_database: Path,
    bundle_scan_results: Path,
    output: Path,
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
            "SELECT path FROM paths WHERE path_folded LIKE '%##%_lod0'"
        ):
            if "##" not in path_value:
                continue
            container, subasset = str(path_value).rsplit("##", 1)
            pair = scan_by_container.get(container.casefold())
            if pair is not None:
                model_by_canonical[canonical_asset_name(subasset)].append(
                    (container, subasset, pair[1])
                )
    finally:
        resources.close()

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
            """
        )
        for base_value, count_value in bases:
            base = str(base_value)
            count = int(count_value)
            resolved: tuple[str, str | None, str | None, str | None, str | None] | None = None
            for name in prefab_names(base, map_id):
                candidates = prefab_by_name.get(name, [])
                if candidates:
                    container, row = min(
                        candidates, key=lambda item: _candidate_rank(item[0], map_id)
                    )
                    serialized = row.get("serialized") or []
                    root_cab = serialized[0].get("name") if serialized else None
                    resolved = ("prefab", container, row.get("logical_name"), root_cab, None)
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
                        break
            if resolved is None:
                resolved = ("unresolved", None, None, None, None)
            connection.execute(
                "INSERT INTO resolutions VALUES (?,?,?,?,?,?,?)", (base, count, *resolved)
            )
            stats[resolved[0]] += 1
            covered[resolved[0]] += count

        meta = {
            "format": "EndfieldAutomaticAssetResolutions/1",
            "map": map_id,
            "unique_bases": str(len(bases)),
            "instances": str(sum(int(row[1]) for row in bases)),
            "resolution_source": "first_run_bundle_container_scan",
        }
        connection.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
        connection.commit()
    finally:
        connection.close()

    return {
        "format": "EndfieldAutomaticAssetResolutionSummary/1",
        "map": map_id,
        "uniqueBases": len(bases),
        "instances": sum(int(row[1]) for row in bases),
        "uniqueByMethod": dict(sorted(stats.items())),
        "instancesByMethod": dict(sorted(covered.items())),
        "containerCount": len(scan_by_container),
        "prefabNameCount": len(prefab_by_name),
        "modelKeyCount": len(model_by_canonical),
    }
