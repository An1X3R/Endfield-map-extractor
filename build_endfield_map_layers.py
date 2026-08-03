"""Build reusable, sector-addressable Endfield map-layer datasets.

Only previously extracted evidence is read. The script never scans or writes
the game installation and refuses to reuse an existing output directory.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from endfield_map_layer_contract import (
    DATASET_FORMAT,
    GENERATOR_VERSION,
    MANIFEST_FORMAT,
    SECTOR_SIZE,
    VALIDATION_FORMAT,
    RuntimeResolver,
    bounds_from_minmax,
    classify_effect,
    classify_entity,
    convex_hull,
    ensure_new_output_root,
    infer_source_level,
    load_json,
    parse_matrix_blob,
    sector_filename,
    sector_keys_for_bounds,
    sha256_file,
    sha256_json,
    stable_id,
    union_bounds,
    validate_dataset_index,
    validate_manifest,
    validate_layer_record,
    write_json,
    write_jsonl_gzip,
)


SCRIPT_ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_MANIFEST = SCRIPT_ROOT / "webui" / "src" / "data" / "region_map_runtime_manifest.json"


@dataclass(frozen=True)
class BuildConfig:
    output_root: Path
    export_root: Path
    game_root: Path | None
    runtime_manifest: Path = DEFAULT_RUNTIME_MANIFEST
    map01_instances: Path | None = None
    map01_assets: Path | None = None
    map02_instances: Path | None = None
    map02_assets: Path | None = None
    map01_water: Path | None = None
    map02_water: Path | None = None
    map01_effects: Path | None = None
    map01_effect_semantics: Path | None = None
    map02_effects: Path | None = None
    map02_effect_semantics: Path | None = None
    map01_light_report: Path | None = None
    map02_light_scans: tuple[Path, ...] = ()
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    max_instances_per_map: int | None = None
    bootstrap_mode: bool = False


class EvidenceRegistry:
    def __init__(self, output_root: Path):
        self.output_root = output_root
        self.rows: list[dict[str, Any]] = []
        self.by_path: dict[Path, str] = {}

    def add(self, path: Path, role: str, *, metadata: Mapping[str, Any] | None = None) -> str:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        if resolved in self.by_path:
            return self.by_path[resolved]
        evidence_id = f"source:{len(self.rows) + 1:03d}"
        row = {
            "evidenceId": evidence_id,
            "role": role,
            "path": str(resolved),
            "bytes": resolved.stat().st_size,
            "sha256": sha256_file(resolved),
        }
        if metadata:
            row["metadata"] = dict(metadata)
        self.rows.append(row)
        self.by_path[resolved] = evidence_id
        return evidence_id


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build standardized Endfield map-layer data")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--game-root", type=Path)
    parser.add_argument("--runtime-manifest", type=Path, default=DEFAULT_RUNTIME_MANIFEST)
    parser.add_argument("--map01-instances", type=Path, required=True)
    parser.add_argument("--map01-assets", type=Path, required=True)
    parser.add_argument("--map02-instances", type=Path, required=True)
    parser.add_argument("--map02-assets", type=Path, required=True)
    parser.add_argument("--map01-water", type=Path)
    parser.add_argument("--map02-water", type=Path)
    parser.add_argument("--map01-effects", type=Path)
    parser.add_argument("--map01-effect-semantics", type=Path)
    parser.add_argument("--map02-effects", type=Path)
    parser.add_argument("--map02-effect-semantics", type=Path)
    parser.add_argument("--map01-light-report", type=Path)
    parser.add_argument("--map02-light-scan", type=Path, action="append", default=[])
    parser.add_argument(
        "--bootstrap-mode",
        action="store_true",
        help="Allow absent optional water/effect/light research and preserve it as not_started.",
    )
    parser.add_argument("--generated-at", default=None, help="Fixed ISO timestamp for reproducible builds")
    parser.add_argument("--max-instances-per-map", type=int, help="Test-only cap; omit for full data")
    return parser.parse_args()


def _relative(path: Path, root: Path) -> str:
    return PurePosixPath(path.relative_to(root)).as_posix()


def _sqlite_ro(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _floor_sql(column: str) -> str:
    quotient = f"({column} / {float(SECTOR_SIZE)})"
    truncated = f"CAST({quotient} AS INTEGER)"
    return f"({truncated} - CASE WHEN {column} < 0 AND {quotient} != {truncated} THEN 1 ELSE 0 END)"


def _load_asset_resolutions(path: Path) -> dict[str, dict[str, Any]]:
    connection = _sqlite_ro(path)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "resolutions" not in tables:
            raise ValueError(f"Asset database lacks resolutions: {path}")
        rows = {}
        for row in connection.execute(
            "SELECT entity_base,instance_count,method,container_path,logical_name,root_cab,model_asset_name "
            "FROM resolutions ORDER BY entity_base"
        ):
            rows[str(row["entity_base"])] = dict(row)
        return rows
    finally:
        connection.close()


def _asset_resolution_status(method: Any, lookup_present: bool) -> str:
    if not lookup_present:
        return "missing_resolution"
    normalized = str(method or "").strip().casefold()
    if normalized in {"model", "prefab", "ecs_instance_binding"}:
        return "resolved"
    if normalized == "report_only_known_proxy_or_nonvisible_renderer":
        return "proxy_only"
    return "unresolved"


def _first_not_none(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value and value[key] is not None:
            return value[key]
    return None


def _decimal_string(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer PathID, not bool")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        return value.strip()
    raise ValueError(f"{field} must be an integer or decimal integer string: {value!r}")


def _level_binding(source_level: str | None, spatial_level: str | None) -> tuple[str, str, float]:
    if source_level and spatial_level:
        if source_level == spatial_level:
            return "source_and_ownership", "matched", 1.0
        return "ownership_with_source_conflict", "conflict", 0.7
    if spatial_level:
        return "ownership-grid", "spatial_only", 0.9
    if source_level:
        return "source-only", "source_only", 0.5
    return "unresolved", "unmapped", 0.0


def _asset_records(
    map_id: str,
    instance_database: Path,
    asset_database: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    resolutions = _load_asset_resolutions(asset_database)
    connection = _sqlite_ro(instance_database)
    try:
        counts = {
            str(row["entity_base"]): int(row["count"])
            for row in connection.execute(
                "SELECT entity_base,COUNT(*) AS count FROM instances GROUP BY entity_base ORDER BY entity_base"
            )
        }
    finally:
        connection.close()
    records: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, Any]] = {}
    for entity_base in sorted(counts):
        lookup_present = entity_base in resolutions
        source = resolutions.get(entity_base) or {}
        resolution_status = _asset_resolution_status(source.get("method"), lookup_present)
        category, token = classify_entity(entity_base)
        source_level = infer_source_level(
            map_id,
            entity_base,
            source.get("container_path"),
            source.get("logical_name"),
            source.get("model_asset_name"),
        )
        asset_id = stable_id(map_id, entity_base, prefix=f"{map_id}:asset")
        record = {
            "assetId": asset_id,
            "mapId": map_id,
            "entityBase": entity_base,
            "instanceCount": counts[entity_base],
            "category": category,
            "categoryStatus": "heuristic_name_rule" if token else "unknown",
            "categoryEvidence": token,
            "sourceLevelId": source_level,
            "containerPath": source.get("container_path"),
            "logicalName": source.get("logical_name"),
            "rootCab": source.get("root_cab"),
            "modelAssetName": source.get("model_asset_name"),
            "resolutionMethod": source.get("method"),
            "lookupStatus": "present" if lookup_present else "missing",
            "resolutionStatus": resolution_status,
            "boundsLocal": None,
            "boundsStatus": "not_extracted",
        }
        records.append(record)
        lookup[entity_base] = record
    return records, lookup


def _instance_query(connection: sqlite3.Connection, limit: int | None) -> sqlite3.Cursor:
    sector_x = _floor_sql("x")
    sector_z = _floor_sql("z")
    sql = (
        "SELECT entity_id,name,entity_base,matrix_type,matrix_col_major,x,y,z,source_path,group_index,entity_index,"
        f"{sector_x} AS sector_x,{sector_z} AS sector_z "
        "FROM instances ORDER BY sector_z,sector_x,entity_id"
    )
    if limit is not None:
        if limit <= 0:
            raise ValueError("max_instances_per_map must be positive")
        sql = (
            "SELECT * FROM ("
            "SELECT entity_id,name,entity_base,matrix_type,matrix_col_major,x,y,z,source_path,group_index,entity_index,"
            f"{sector_x} AS sector_x,{sector_z} AS sector_z FROM instances ORDER BY entity_id LIMIT {int(limit)}"
            ") ORDER BY sector_z,sector_x,entity_id"
        )
    return connection.execute(sql)


def build_instance_layer(
    map_id: str,
    instance_database: Path,
    asset_database: Path,
    output_root: Path,
    resolver: RuntimeResolver,
    evidence: EvidenceRegistry,
    generated_at: str,
    max_instances: int | None,
    sample_sink: dict[str, Any],
) -> dict[str, Any]:
    database_evidence = evidence.add(instance_database, f"{map_id} full instance database")
    assets_evidence = evidence.add(asset_database, f"{map_id} asset resolution database")
    map_root = output_root / map_id
    asset_records, asset_lookup = _asset_records(map_id, instance_database, asset_database)
    assets_path = map_root / "instances" / "assets.jsonl.gz"
    asset_count, asset_sha, asset_bytes = write_jsonl_gzip(assets_path, asset_records)

    connection = _sqlite_ro(instance_database)
    category_counts: Counter[str] = Counter()
    binding_counts: Counter[str] = Counter()
    level_binding_counts: Counter[str] = Counter()
    category_binding_counts: Counter[tuple[str, str]] = Counter()
    source_level_counts: Counter[str] = Counter()
    spatial_level_counts: Counter[str] = Counter()
    shard_rows: list[dict[str, Any]] = []
    record_count = 0
    first_mapped: list[dict[str, Any]] = []
    first_unmapped: dict[str, Any] | None = None
    source_record_count = 0
    duplicate_count = 0
    try:
        source_record_count = int(connection.execute("SELECT COUNT(*) FROM instances").fetchone()[0])
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "duplicates" in tables:
            duplicate_count = int(connection.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0])
        cursor = _instance_query(connection, max_instances)
        grouped = itertools.groupby(cursor, key=lambda row: (int(row["sector_x"]), int(row["sector_z"])))
        for (sector_x, sector_z), rows in grouped:
            resolved = resolver.resolve_sector(map_id, sector_x, sector_z)
            local_category_counts: Counter[str] = Counter()
            local_binding_counts: Counter[str] = Counter()
            local_count = 0

            def normalized_rows() -> Iterator[dict[str, Any]]:
                nonlocal record_count, local_count, first_unmapped
                for row in rows:
                    entity_base = str(row["entity_base"])
                    asset = asset_lookup[entity_base]
                    source_level = asset.get("sourceLevelId") or infer_source_level(
                        map_id, entity_base, row["source_path"], row["name"]
                    )
                    method, level_status, level_confidence = _level_binding(source_level, resolved.level_id)
                    binding_status = "mapped" if resolved.level_id else "candidate" if source_level else "unmapped"
                    position = {"x": float(row["x"]), "y": float(row["y"]), "z": float(row["z"])}
                    record = {
                        "recordType": "instance",
                        "instanceId": f"{map_id}:instance:{int(row['entity_id'])}",
                        "assetId": asset["assetId"],
                        "mapId": map_id,
                        "domainId": resolved.domain_id,
                        "sourceLevelId": source_level,
                        "spatialLevelId": resolved.level_id,
                        "levelBindingMethod": method,
                        "levelBindingStatus": level_status,
                        "levelBindingConfidence": level_confidence,
                        "positionUnity": position,
                        "positionBlender": resolver.unity_to_blender_position((position["x"], position["y"], position["z"])),
                        "matrixUnityColumnMajor": parse_matrix_blob(row["matrix_col_major"]),
                        "matrixType": int(row["matrix_type"]),
                        "boundsUnity": None,
                        "boundsStatus": "asset_bounds_not_extracted",
                        "anchorSector": {
                            "sectorX": sector_x,
                            "sectorZ": sector_z,
                            "key": resolved.key,
                        },
                        "sectorKeys": [resolved.key],
                        "category": asset["category"],
                        "categoryStatus": asset["categoryStatus"],
                        "displayClass": "point",
                        "extractionStatus": "normalized",
                        "bindingStatus": binding_status,
                        "worldResolution": "world",
                        "source": {
                            "entityId": int(row["entity_id"]),
                            "name": str(row["name"]),
                            "entityBase": entity_base,
                            "sourcePath": str(row["source_path"]),
                            "groupIndex": int(row["group_index"]),
                            "entityIndex": int(row["entity_index"]),
                            "evidenceId": database_evidence,
                        },
                    }
                    category_counts[asset["category"]] += 1
                    binding_counts[binding_status] += 1
                    level_binding_counts[level_status] += 1
                    category_binding_counts[(asset["category"], binding_status)] += 1
                    local_category_counts[asset["category"]] += 1
                    local_binding_counts[binding_status] += 1
                    if source_level:
                        source_level_counts[source_level] += 1
                    if resolved.level_id:
                        spatial_level_counts[resolved.level_id] += 1
                    if resolved.level_id and len(first_mapped) < 3:
                        first_mapped.append(record)
                    if not resolved.level_id and first_unmapped is None:
                        first_unmapped = record
                    record_count += 1
                    local_count += 1
                    yield record

            shard_path = map_root / "instances" / "sectors" / sector_filename(sector_x, sector_z)
            written, shard_sha, shard_bytes = write_jsonl_gzip(shard_path, normalized_rows())
            if written != local_count:
                raise AssertionError("Instance shard writer count mismatch")
            shard_rows.append(
                {
                    "sectorKey": resolved.key,
                    "sectorX": sector_x,
                    "sectorZ": sector_z,
                    "spatialLevelId": resolved.level_id,
                    "coverage": resolved.coverage,
                    "path": _relative(shard_path, output_root),
                    "recordCount": written,
                    "bytes": shard_bytes,
                    "sha256": shard_sha,
                    "categoryCounts": dict(sorted(local_category_counts.items())),
                    "bindingCounts": dict(sorted(local_binding_counts.items())),
                }
            )
    finally:
        connection.close()

    if max_instances is None and record_count != source_record_count:
        raise AssertionError(
            f"{map_id} instance count mismatch: wrote {record_count}, source has {source_record_count}"
        )
    shard_record_count = sum(int(row["recordCount"]) for row in shard_rows)
    if shard_record_count != record_count:
        raise AssertionError(f"{map_id} instance shard totals do not match recordCount")

    category_binding_summary = {
        category: {
            status: int(category_binding_counts.get((category, status), 0))
            for status in ("mapped", "candidate", "unmapped")
            if category_binding_counts.get((category, status), 0)
        }
        for category in sorted(category_counts)
    }
    asset_resolution_counts = Counter(str(row.get("resolutionStatus") or "unknown") for row in asset_records)
    asset_resolution_instance_counts: Counter[str] = Counter()
    for row in asset_records:
        asset_resolution_instance_counts[str(row.get("resolutionStatus") or "unknown")] += int(
            row.get("instanceCount") or 0
        )
    pending_resolution_statuses = {"unresolved", "proxy_only", "missing_resolution"}
    index = {
        "format": DATASET_FORMAT,
        "datasetId": f"{map_id}.instances.stage119",
        "generatorVersion": GENERATOR_VERSION,
        "generatedAt": generated_at,
        "mapId": map_id,
        "domainId": resolver.map(map_id)["domainId"],
        "layer": "instances",
        "recordType": "instance",
        "recordEncoding": "jsonl+gzip",
        "geometryTypes": ["point"],
        "scan": {
            "status": "complete" if max_instances is None else "partial",
            "coverageScope": "full extracted instance database" if max_instances is None else "test-only capped instance sample",
            "recordsFound": record_count,
            "limitations": ["Asset-local bounds and footprints have not been extracted; records render as points."],
        },
        "recordCount": record_count,
        "sourceRecordCount": source_record_count,
        "coverageCount": int(binding_counts.get("mapped", 0)),
        "duplicateCount": duplicate_count,
        "pendingCount": int(binding_counts.get("candidate", 0) + binding_counts.get("unmapped", 0)),
        "assetTable": {
            "path": _relative(assets_path, output_root),
            "recordCount": asset_count,
            "bytes": asset_bytes,
            "sha256": asset_sha,
            "boundsStatus": "not_extracted",
        },
        "categoryCounts": dict(sorted(category_counts.items())),
        "categoryBindingCounts": category_binding_summary,
        "assetResolutionCounts": dict(sorted(asset_resolution_counts.items())),
        "assetResolutionInstanceCounts": dict(sorted(asset_resolution_instance_counts.items())),
        "assetPendingCount": sum(int(asset_resolution_counts.get(status, 0)) for status in pending_resolution_statuses),
        "assetPendingInstanceCount": sum(
            int(asset_resolution_instance_counts.get(status, 0)) for status in pending_resolution_statuses
        ),
        "bindingCounts": dict(sorted(binding_counts.items())),
        "levelBindingCounts": dict(sorted(level_binding_counts.items())),
        "sourceLevelCounts": dict(sorted(source_level_counts.items())),
        "spatialLevelCounts": dict(sorted(spatial_level_counts.items())),
        "shardCount": len(shard_rows),
        "shards": shard_rows,
        "sourceEvidenceIds": [database_evidence, assets_evidence],
        "validation": {
            "allMatricesFinite": True,
            "stableIdSource": "mapId + entity_id",
            "sectorFormula": "floor(Unity X/128), floor(Unity Z/128)",
            "shardRecordCount": shard_record_count,
            "shardRecordCountMatches": shard_record_count == record_count,
            "sourceRecordCountMatches": max_instances is not None or source_record_count == record_count,
        },
    }
    index_path = map_root / "instances" / "index.json"
    validate_dataset_index(index)
    write_json(index_path, index)
    sample_sink[f"{map_id}.instances.mapped"] = first_mapped
    sample_sink[f"{map_id}.instances.unmapped"] = first_unmapped
    return {
        "indexPath": _relative(index_path, output_root),
        "recordCount": record_count,
        "sourceRecordCount": source_record_count,
        "bindingCounts": index["bindingCounts"],
        "categoryCounts": index["categoryCounts"],
        "categoryBindingCounts": index["categoryBindingCounts"],
        "assetResolutionCounts": index["assetResolutionCounts"],
        "assetResolutionInstanceCounts": index["assetResolutionInstanceCounts"],
        "assetPendingCount": index["assetPendingCount"],
        "assetPendingInstanceCount": index["assetPendingInstanceCount"],
        "scanStatus": index["scan"]["status"],
        "geometryTypes": index["geometryTypes"],
        "coverageCount": index["coverageCount"],
        "duplicateCount": index["duplicateCount"],
        "pendingCount": index["pendingCount"],
        "sectorKeys": [row["sectorKey"] for row in shard_rows],
        "shards": shard_rows,
        "spatialLevelIds": sorted(spatial_level_counts),
        "sourceEvidenceIds": index["sourceEvidenceIds"],
    }


def _record_key(row: Mapping[str, Any]) -> tuple[str, int, int]:
    return (
        str(row.get("file") or ""),
        int(row.get("grid_unique_id") or 0),
        int(row.get("record_index") or 0),
    )


def _prefab_name(row: Mapping[str, Any]) -> str:
    paths = row.get("prefab_paths") or []
    if paths:
        return Path(str(paths[0]).replace("\\", "/")).stem
    return str(row.get("prefab") or row.get("object") or "unknown")


def _asset_reference(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "source": _first_not_none(value, "Source", "source", "file_id"),
        "pathId": _decimal_string(_first_not_none(value, "PathId", "path_id"), field="assetReference.pathId"),
        "type": _first_not_none(value, "Type", "type"),
        "name": _first_not_none(value, "Name", "name"),
    }


def _particle_system_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "gameObject": _asset_reference(value.get("game_object")),
        "lengthSeconds": value.get("length_in_sec"),
        "simulationSpeed": value.get("simulation_speed"),
        "looping": value.get("looping"),
        "prewarm": value.get("prewarm"),
        "playOnAwake": value.get("play_on_awake"),
        "moveWithTransform": value.get("move_with_transform"),
        "scalingMode": value.get("scaling_mode"),
        "maxAliveDistance": value.get("max_alive_distance"),
        "enabledModules": list(value.get("enabled_modules") or []),
    }


def _prototype_summaries(
    semantics: Mapping[str, Any], map_id: str
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, Any]] = {}
    members: list[dict[str, Any]] = []
    for row in sorted(semantics.get("prototypes") or [], key=lambda value: str(value.get("name"))):
        name = str(row.get("name") or "unknown")
        nodes = row.get("nodes") or []
        prototype_id = stable_id(map_id, name, prefix=f"{map_id}:effect-prototype")
        transform_member_ids = {
            int(node.get("transform_path_id") or 0): stable_id(
                map_id,
                name,
                node.get("source"),
                node.get("path_id"),
                node.get("transform_path_id"),
                prefix=f"{map_id}:effect-member",
            )
            for node in nodes
            if node.get("transform_path_id") is not None
        }
        member_ids: list[str] = []
        for member_index, node in enumerate(nodes):
            member_id = stable_id(
                map_id,
                name,
                node.get("source"),
                node.get("path_id"),
                node.get("transform_path_id"),
                prefix=f"{map_id}:effect-member",
            )
            member_ids.append(member_id)
            parent = node.get("parent") if isinstance(node.get("parent"), Mapping) else {}
            parent_path_id = int(parent.get("PathId") or 0) if parent else 0
            particle_systems = [
                _particle_system_summary(system)
                for system in node.get("particle_systems") or []
                if isinstance(system, Mapping)
            ]
            members.append(
                {
                    "recordType": "effect-prototype-member",
                    "prototypeMemberId": member_id,
                    "prototypeId": prototype_id,
                    "mapId": map_id,
                    "memberIndex": member_index,
                    "name": str(node.get("name") or "unknown"),
                    "sourceCab": node.get("source"),
                    "pathId": _decimal_string(node.get("path_id"), field="effectPrototypeMember.pathId"),
                    "transformPathId": _decimal_string(
                        node.get("transform_path_id"), field="effectPrototypeMember.transformPathId"
                    ),
                    "parentTransformPathId": _decimal_string(
                        parent_path_id, field="effectPrototypeMember.parentTransformPathId"
                    )
                    if parent_path_id
                    else None,
                    "parentPrototypeMemberId": transform_member_ids.get(parent_path_id),
                    "localPositionUnity": [float(value) for value in node.get("local_position") or []],
                    "localRotationUnityQuaternion": [float(value) for value in node.get("local_rotation") or []],
                    "localScaleUnity": [float(value) for value in node.get("local_scale") or []],
                    "rendererType": node.get("renderer_type"),
                    "mesh": _asset_reference(node.get("mesh")),
                    "materials": [
                        reference
                        for reference in (_asset_reference(value) for value in node.get("materials") or [])
                        if reference is not None
                    ],
                    "particleSystemCount": len(node.get("particle_systems") or []),
                    "particleRendererCount": len(node.get("particle_renderers") or []),
                    "particleSystems": particle_systems,
                    "semanticStatus": "extracted_local_hierarchy",
                    "worldTransformStatus": "resolved_through_outer_anchor_at_consume_time",
                }
            )
        record = {
            "recordType": "effect-prototype",
            "prototypeId": prototype_id,
            "mapId": map_id,
            "name": name,
            "missing": bool(row.get("missing")),
            "container": row.get("container"),
            "rootAsset": _asset_reference(row.get("root_asset")),
            "nodeCount": len(nodes),
            "particleSystemCount": int(row.get("particle_system_count") or 0),
            "particleRendererCount": int(row.get("particle_renderer_count") or 0),
            "meshRendererCount": int(row.get("mesh_renderer_count") or 0),
            "sourceLevelId": infer_source_level(map_id, name, row.get("container"), row.get("root_asset")),
            "memberIds": member_ids,
            "boundsLocal": None,
            "boundsStatus": "not_extracted",
        }
        records.append(record)
        lookup[name] = record
    return records, lookup, members


def _effect_index(
    *,
    map_id: str,
    output_root: Path,
    resolver: RuntimeResolver,
    generated_at: str,
    records: Sequence[Mapping[str, Any]],
    anchors: Sequence[Mapping[str, Any]],
    prototypes: Sequence[Mapping[str, Any]],
    prototype_members: Sequence[Mapping[str, Any]],
    source_evidence_ids: Sequence[str],
    scan: Mapping[str, Any],
) -> dict[str, Any]:
    layer_root = output_root / map_id / "effects"
    records_path = layer_root / "records.jsonl.gz"
    anchors_path = layer_root / "anchors.jsonl.gz"
    prototypes_path = layer_root / "prototypes.jsonl.gz"
    members_path = layer_root / "prototype_members.jsonl.gz"
    record_count, record_sha, record_bytes = write_jsonl_gzip(records_path, records)
    anchor_count, anchor_sha, anchor_bytes = write_jsonl_gzip(anchors_path, anchors)
    prototype_count, prototype_sha, prototype_bytes = write_jsonl_gzip(prototypes_path, prototypes)
    member_count, member_sha, member_bytes = write_jsonl_gzip(members_path, prototype_members)
    binding_counts = Counter(str(row.get("bindingStatus") or "unknown") for row in records)
    effect_counts = Counter(str(row.get("effectClass") or "unknown") for row in records)
    completeness_counts = Counter(str(row.get("systemCompleteness") or "unknown") for row in anchors)
    geometry_types = ["point"]
    if any(row.get("boundsUnity") is not None for row in records):
        geometry_types.insert(0, "bounds")
    effect_ids = {str(row["effectId"]) for row in records}
    prototype_member_ids = {str(row["prototypeMemberId"]) for row in prototype_members}
    sector_keys = sorted({str(key) for row in records for key in row.get("sectorKeys") or []})
    spatial_level_ids = sorted(
        {str(level) for row in records for level in row.get("spatialLevelIds") or [] if level}
    )
    member_references_resolved = all(
        str(member) in effect_ids
        for anchor in anchors
        for member in anchor.get("memberEffectIds") or []
    )
    prototype_member_references_resolved = all(
        str(member) in prototype_member_ids
        for anchor in anchors
        for member in anchor.get("prototypeMemberIds") or []
    )
    index = {
        "format": DATASET_FORMAT,
        "datasetId": f"{map_id}.effects.stage119",
        "generatorVersion": GENERATOR_VERSION,
        "generatedAt": generated_at,
        "mapId": map_id,
        "domainId": resolver.map(map_id)["domainId"],
        "layer": "effects",
        "recordType": "effect",
        "recordEncoding": "jsonl+gzip",
        "geometryTypes": geometry_types,
        "anchorSelectionPolicy": "full_system_by_anchor",
        "scan": dict(scan),
        "recordCount": record_count,
        "anchorCount": anchor_count,
        "prototypeCount": prototype_count,
        "prototypeMemberCount": member_count,
        "coverageCount": int(binding_counts.get("mapped", 0)),
        "duplicateCount": 0,
        "pendingCount": int(binding_counts.get("candidate", 0) + binding_counts.get("unmapped", 0)),
        "bindingCounts": dict(sorted(binding_counts.items())),
        "effectClassCounts": dict(sorted(effect_counts.items())),
        "systemCompletenessCounts": dict(sorted(completeness_counts.items())),
        "sectorKeys": sector_keys,
        "spatialLevelIds": spatial_level_ids,
        "records": {
            "path": _relative(records_path, output_root),
            "bytes": record_bytes,
            "sha256": record_sha,
        },
        "anchors": {
            "path": _relative(anchors_path, output_root),
            "bytes": anchor_bytes,
            "sha256": anchor_sha,
        },
        "prototypes": {
            "path": _relative(prototypes_path, output_root),
            "bytes": prototype_bytes,
            "sha256": prototype_sha,
        },
        "prototypeMembers": {
            "path": _relative(members_path, output_root),
            "bytes": member_bytes,
            "sha256": member_sha,
        },
        "sourceEvidenceIds": list(source_evidence_ids),
        "validation": {
            "memberReferencesResolved": member_references_resolved,
            "prototypeMemberReferencesResolved": prototype_member_references_resolved,
            "anchorSelectionPolicy": "full_system_by_anchor",
        },
    }
    index_path = layer_root / "index.json"
    validate_dataset_index(index)
    write_json(index_path, index)
    return {
        "indexPath": _relative(index_path, output_root),
        "recordCount": record_count,
        "anchorCount": anchor_count,
        "prototypeCount": prototype_count,
        "prototypeMemberCount": member_count,
        "bindingCounts": index["bindingCounts"],
        "effectClassCounts": index["effectClassCounts"],
        "scanStatus": index["scan"]["status"],
        "geometryTypes": index["geometryTypes"],
        "coverageCount": index["coverageCount"],
        "duplicateCount": index["duplicateCount"],
        "pendingCount": index["pendingCount"],
        "sectorKeys": sector_keys,
        "spatialLevelIds": spatial_level_ids,
        "sourceEvidenceIds": list(source_evidence_ids),
        "memberReferencesResolved": member_references_resolved,
        "prototypeMemberReferencesResolved": prototype_member_references_resolved,
    }


def build_map01_effect_layer(
    binding_path: Path,
    semantics_path: Path,
    output_root: Path,
    resolver: RuntimeResolver,
    evidence: EvidenceRegistry,
    generated_at: str,
    sample_sink: dict[str, Any],
) -> dict[str, Any]:
    map_id = "map01"
    binding_evidence = evidence.add(binding_path, "map01 localized effect binding audit")
    semantics_evidence = evidence.add(semantics_path, "map01 particle semantic audit")
    binding = load_json(binding_path)
    semantics = load_json(semantics_path)
    prototypes, prototype_lookup, prototype_members = _prototype_summaries(semantics, map_id)

    covering_anchors: dict[tuple[str, int, int], list[int]] = defaultdict(list)
    for link in binding.get("anchor_links") or []:
        anchor_index = int(link.get("anchor_index") or 0)
        for candidate in link.get("candidate_effects") or []:
            if candidate.get("default_active") and candidate.get("aabb_contains_anchor"):
                covering_anchors[_record_key(candidate)].append(anchor_index)

    anchor_source = {int(row["index"]): row for row in binding.get("anchors") or []}
    anchor_members: dict[str, list[str]] = defaultdict(list)
    effect_bounds: dict[str, dict[str, float]] = {}
    effects: list[dict[str, Any]] = []
    for row in sorted(binding.get("effects") or [], key=_record_key):
        key = _record_key(row)
        effect_id = stable_id(map_id, *key, prefix="map01:effect")
        prefab = _prefab_name(row)
        prototype = prototype_lookup.get(prefab)
        position = [float(value) for value in row["position"][:3]]
        bounds = bounds_from_minmax(row["bounds_min"], row["bounds_max"])
        keys = sector_keys_for_bounds(bounds)
        spatial = resolver.resolve_point(map_id, position[0], position[2])
        source_level = None
        for condition in row.get("conditions") or []:
            source_level = resolver.level_from_id_num(map_id, condition.get("level_num"))
            if source_level:
                break
        source_level = source_level or infer_source_level(map_id, prefab, *(row.get("prefab_paths") or []))
        method, level_status, level_confidence = _level_binding(source_level, spatial.level_id)
        candidate_indices = sorted(set(covering_anchors.get(key) or []))
        if len(candidate_indices) == 1:
            anchor_id = f"map01:effect-anchor:binding:{candidate_indices[0]}"
            membership_status = "validated_anchor_aabb_cover"
            binding_status = "mapped" if spatial.level_id else "candidate"
        else:
            anchor_id = stable_id(map_id, *key, prefix="map01:effect-anchor:self")
            membership_status = "self_fallback_no_unique_external_anchor"
            binding_status = "candidate" if spatial.level_id else "unmapped"
        record = {
            "recordType": "effect",
            "effectId": effect_id,
            "mapId": map_id,
            "domainId": spatial.domain_id,
            "sourceLevelId": source_level,
            "spatialLevelId": spatial.level_id,
            "spatialLevelIds": resolver.levels_for_sector_keys(map_id, keys),
            "levelBindingMethod": method,
            "levelBindingStatus": level_status,
            "levelBindingConfidence": level_confidence,
            "anchorId": anchor_id,
            "fullSystemGroup": anchor_id,
            "anchorSelectionPolicy": "full_system_by_anchor",
            "membershipStatus": membership_status,
            "candidateAnchorIds": [f"map01:effect-anchor:binding:{value}" for value in candidate_indices],
            "prefabName": prefab,
            "prototypeId": prototype.get("prototypeId") if prototype else None,
            "prototypeMemberIds": list(prototype.get("memberIds") or []) if prototype else [],
            "positionUnity": {"x": position[0], "y": position[1], "z": position[2]},
            "positionBlender": resolver.unity_to_blender_position(position),
            "rotationUnityEuler": [float(value) for value in row.get("rotation_euler") or []],
            "scaleUnity": [float(value) for value in row.get("scale") or []],
            "boundsUnity": bounds,
            "boundsStatus": "extracted_aabb",
            "anchorSector": {
                "sectorX": spatial.sector_x,
                "sectorZ": spatial.sector_z,
                "key": spatial.key,
            },
            "sectorKeys": keys,
            "effectClass": classify_effect(prefab),
            "defaultActive": bool(row.get("default_active")),
            "extractionStatus": "normalized",
            "bindingStatus": binding_status,
            "worldResolution": "world",
            "source": {
                "file": key[0],
                "gridUniqueId": key[1],
                "recordIndex": key[2],
                "prefabPaths": row.get("prefab_paths") or [],
                "conditions": row.get("conditions") or [],
                "evidenceId": binding_evidence,
            },
        }
        effects.append(record)
        anchor_members[anchor_id].append(effect_id)
        effect_bounds[effect_id] = bounds

    anchors: list[dict[str, Any]] = []
    effect_by_id = {row["effectId"]: row for row in effects}
    for anchor_id, member_ids in sorted(anchor_members.items()):
        external_match = re.search(r":binding:(\d+)$", anchor_id)
        if external_match:
            anchor_index = int(external_match.group(1))
            source = anchor_source[anchor_index]
            position = [float(value) for value in source["unity_position"][:3]]
            anchor_kind = "validated_external_anchor"
            completeness = "complete_within_reviewed_binding"
        else:
            source_effect = effect_by_id[member_ids[0]]
            position_object = source_effect["positionUnity"]
            position = [position_object["x"], position_object["y"], position_object["z"]]
            anchor_index = None
            anchor_kind = "self_fallback"
            completeness = "partial"
        bounds = union_bounds(effect_bounds[member] for member in member_ids)
        keys = sector_keys_for_bounds(bounds) if bounds else [resolver.resolve_point(map_id, position[0], position[2]).key]
        spatial = resolver.resolve_point(map_id, position[0], position[2])
        prototype_member_ids = sorted(
            {
                str(member)
                for effect_id in member_ids
                for member in effect_by_id[effect_id].get("prototypeMemberIds") or []
            }
        )
        anchors.append(
            {
                "recordType": "effect-anchor",
                "anchorId": anchor_id,
                "mapId": map_id,
                "domainId": spatial.domain_id,
                "anchorKind": anchor_kind,
                "positionUnity": {"x": position[0], "y": position[1], "z": position[2]},
                "positionBlender": resolver.unity_to_blender_position(position),
                "boundsUnity": bounds,
                "boundsStatus": "union_of_member_aabbs" if bounds else "unavailable",
                "anchorSector": {
                    "sectorX": spatial.sector_x,
                    "sectorZ": spatial.sector_z,
                    "key": spatial.key,
                },
                "sectorKeys": keys,
                "spatialLevelId": spatial.level_id,
                "spatialLevelIds": resolver.levels_for_sector_keys(map_id, keys),
                "memberEffectIds": sorted(member_ids),
                "prototypeMemberIds": prototype_member_ids,
                "expectedMemberCount": len(member_ids) + len(prototype_member_ids),
                "resolvedMemberCount": len(member_ids) + len(prototype_member_ids),
                "systemCompleteness": completeness,
                "missingMembers": [],
                "anchorSelectionPolicy": "full_system_by_anchor",
                "bindingStatus": "mapped" if spatial.level_id and external_match else "candidate",
                "source": {
                    "anchorIndex": anchor_index,
                    "evidenceId": binding_evidence,
                },
            }
        )

    sample_sink["map01.effects"] = effects[:3]
    sample_sink["map01.effectAnchor"] = next((row for row in anchors if len(row["memberEffectIds"]) > 1), anchors[0] if anchors else None)
    return _effect_index(
        map_id=map_id,
        output_root=output_root,
        resolver=resolver,
        generated_at=generated_at,
        records=effects,
        anchors=anchors,
        prototypes=prototypes,
        prototype_members=prototype_members,
        source_evidence_ids=[binding_evidence, semantics_evidence],
        scan={
            "status": "partial",
            "coverageScope": "localized lightgate/main-building binding audit",
            "recordsFound": len(effects),
            "sourceClosures": [str(binding_path.resolve()), str(semantics_path.resolve())],
            "limitations": [
                "Coverage is localized and must not be described as a complete Map01 effects inventory.",
                "Self-fallback anchors remain candidates when no unique validated external anchor covers the effect AABB.",
            ],
        },
    )


def build_map02_effect_layer(
    audit_path: Path,
    semantics_path: Path,
    output_root: Path,
    resolver: RuntimeResolver,
    evidence: EvidenceRegistry,
    generated_at: str,
    sample_sink: dict[str, Any],
) -> dict[str, Any]:
    map_id = "map02"
    audit_evidence = evidence.add(audit_path, "map02 main-city effect instance audit")
    semantics_evidence = evidence.add(semantics_path, "map02 effect semantic audit")
    audit = load_json(audit_path)
    semantics = load_json(semantics_path)
    prototypes, prototype_lookup, prototype_members = _prototype_summaries(semantics, map_id)
    effects: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    for row in sorted(audit.get("instances") or [], key=lambda value: tuple(value.get("key") or [])):
        key = tuple(row.get("key") or [])
        effect_id = stable_id(map_id, *key, row.get("object"), prefix="map02:effect")
        anchor_id = stable_id(map_id, *key, row.get("object"), prefix="map02:effect-anchor")
        prefab = str(row.get("prefab") or "unknown")
        prototype = prototype_lookup.get(prefab)
        position = [float(value) for value in row["unity_position"][:3]]
        spatial = resolver.resolve_point(map_id, position[0], position[2])
        source_level = infer_source_level(map_id, prefab)
        method, level_status, level_confidence = _level_binding(source_level, spatial.level_id)
        completeness = "complete" if prototype and not prototype.get("missing") else "partial"
        effect_class = classify_effect(f"{row.get('group')} {prefab}")
        effect = {
            "recordType": "effect",
            "effectId": effect_id,
            "mapId": map_id,
            "domainId": spatial.domain_id,
            "sourceLevelId": source_level,
            "spatialLevelId": spatial.level_id,
            "spatialLevelIds": [spatial.level_id] if spatial.level_id else [],
            "levelBindingMethod": method,
            "levelBindingStatus": level_status,
            "levelBindingConfidence": level_confidence,
            "anchorId": anchor_id,
            "fullSystemGroup": anchor_id,
            "anchorSelectionPolicy": "full_system_by_anchor",
            "membershipStatus": "outer_dynamic_streaming_instance_is_anchor",
            "prefabName": prefab,
            "prototypeId": prototype.get("prototypeId") if prototype else None,
            "prototypeMemberIds": list(prototype.get("memberIds") or []) if prototype else [],
            "positionUnity": {"x": position[0], "y": position[1], "z": position[2]},
            "positionBlender": resolver.unity_to_blender_position(position),
            "rotationUnityEuler": [float(value) for value in row.get("unity_rotation_euler") or []],
            "scaleUnity": [float(value) for value in row.get("unity_scale") or []],
            "matrixBlenderWorld": row.get("matrix_world"),
            "boundsUnity": None,
            "boundsStatus": "prototype_world_bounds_not_extracted",
            "anchorSector": {
                "sectorX": spatial.sector_x,
                "sectorZ": spatial.sector_z,
                "key": spatial.key,
            },
            "sectorKeys": [spatial.key],
            "effectClass": effect_class,
            "effectGroup": row.get("group"),
            "extractionStatus": "normalized",
            "bindingStatus": "mapped" if spatial.level_id else "unmapped",
            "worldResolution": "world_anchor_only",
            "source": {
                "object": row.get("object"),
                "key": list(key),
                "evidenceId": audit_evidence,
            },
        }
        effects.append(effect)
        node_count = int(prototype.get("nodeCount") if prototype else 0)
        prototype_member_ids = list(prototype.get("memberIds") or []) if prototype else []
        anchors.append(
            {
                "recordType": "effect-anchor",
                "anchorId": anchor_id,
                "mapId": map_id,
                "domainId": spatial.domain_id,
                "anchorKind": "outer_dynamic_streaming_instance",
                "positionUnity": effect["positionUnity"],
                "positionBlender": effect["positionBlender"],
                "boundsUnity": None,
                "boundsStatus": "prototype_world_bounds_not_extracted",
                "anchorSector": effect["anchorSector"],
                "sectorKeys": [spatial.key],
                "spatialLevelId": spatial.level_id,
                "spatialLevelIds": effect["spatialLevelIds"],
                "memberEffectIds": [effect_id],
                "prototypeMemberIds": prototype_member_ids,
                "prototypeId": effect["prototypeId"],
                "expectedMemberCount": node_count + 1,
                "resolvedMemberCount": len(prototype_member_ids) + 1,
                "systemCompleteness": completeness,
                "missingMembers": [],
                "anchorSelectionPolicy": "full_system_by_anchor",
                "bindingStatus": effect["bindingStatus"],
                "source": {"key": list(key), "evidenceId": audit_evidence},
            }
        )

    sample_sink["map02.effects"] = effects[:3]
    sample_sink["map02.effectAnchor"] = anchors[0] if anchors else None
    return _effect_index(
        map_id=map_id,
        output_root=output_root,
        resolver=resolver,
        generated_at=generated_at,
        records=effects,
        anchors=anchors,
        prototypes=prototypes,
        prototype_members=prototype_members,
        source_evidence_ids=[audit_evidence, semantics_evidence],
        scan={
            "status": "partial",
            "coverageScope": "map02 main-city dragon, waterfall, and defense-array closure",
            "recordsFound": len(effects),
            "sourceClosures": [str(audit_path.resolve()), str(semantics_path.resolve())],
            "limitations": [
                "Coverage is not a complete Map02 effects inventory.",
                "World anchor positions are resolved, but prototype world bounds are not extracted.",
            ],
        },
    )


def _resolve_source_file(raw_path: str | None, roots: Sequence[Path]) -> Path | None:
    if not raw_path:
        return None
    normalized = Path(str(raw_path).replace("\\", "/"))
    if normalized.is_absolute() and normalized.is_file():
        return normalized.resolve()
    for root in roots:
        candidate = (root / normalized).resolve()
        if candidate.is_file():
            return candidate
    return None


def _water_index(
    *,
    map_id: str,
    output_root: Path,
    resolver: RuntimeResolver,
    generated_at: str,
    records: Sequence[Mapping[str, Any]],
    configs: Sequence[Mapping[str, Any]],
    source_evidence_ids: Sequence[str],
    scan: Mapping[str, Any],
) -> dict[str, Any]:
    layer_root = output_root / map_id / "water"
    records_path = layer_root / "records.jsonl.gz"
    configs_path = layer_root / "configs.jsonl.gz"
    record_count, record_sha, record_bytes = write_jsonl_gzip(records_path, records)
    config_count, config_sha, config_bytes = write_jsonl_gzip(configs_path, configs)
    binding_counts = Counter(str(row.get("bindingStatus") or "unknown") for row in records)
    geometry_counts = Counter(str((row.get("geometry") or {}).get("type") or "unknown") for row in records)
    sector_keys = sorted({str(key) for row in records for key in row.get("sectorKeys") or []})
    spatial_level_ids = sorted(
        {str(level) for row in records for level in row.get("spatialLevelIds") or [] if level}
    )
    index = {
        "format": DATASET_FORMAT,
        "datasetId": f"{map_id}.water.stage119",
        "generatorVersion": GENERATOR_VERSION,
        "generatedAt": generated_at,
        "mapId": map_id,
        "domainId": resolver.map(map_id)["domainId"],
        "layer": "water",
        "recordType": "water",
        "recordEncoding": "jsonl+gzip",
        "geometryTypes": sorted(geometry_counts),
        "scan": dict(scan),
        "recordCount": record_count,
        "configCount": config_count,
        "coverageCount": int(binding_counts.get("mapped", 0)),
        "duplicateCount": 0,
        "pendingCount": int(binding_counts.get("candidate", 0) + binding_counts.get("unmapped", 0)),
        "bindingCounts": dict(sorted(binding_counts.items())),
        "geometryTypeCounts": dict(sorted(geometry_counts.items())),
        "sectorKeys": sector_keys,
        "spatialLevelIds": spatial_level_ids,
        "records": {
            "path": _relative(records_path, output_root),
            "bytes": record_bytes,
            "sha256": record_sha,
        },
        "configs": {
            "path": _relative(configs_path, output_root),
            "bytes": config_bytes,
            "sha256": config_sha,
        },
        "sourceEvidenceIds": list(source_evidence_ids),
        "validation": {
            "allRecordsHaveGeometry": all(bool(row.get("geometry")) for row in records),
            "allRecordsHaveSectorKeys": all(bool(row.get("sectorKeys")) for row in records),
            "candidateDataNotPromoted": all(
                row.get("bindingStatus") != "mapped" or row.get("geometryStatus") == "verified_surface_geometry"
                for row in records
            ),
        },
    }
    index_path = layer_root / "index.json"
    validate_dataset_index(index)
    write_json(index_path, index)
    return {
        "indexPath": _relative(index_path, output_root),
        "recordCount": record_count,
        "configCount": config_count,
        "bindingCounts": index["bindingCounts"],
        "geometryTypes": index["geometryTypes"],
        "scanStatus": index["scan"]["status"],
        "coverageCount": index["coverageCount"],
        "duplicateCount": index["duplicateCount"],
        "pendingCount": index["pendingCount"],
        "sectorKeys": sector_keys,
        "spatialLevelIds": spatial_level_ids,
        "sourceEvidenceIds": list(source_evidence_ids),
    }


def build_map01_water_layer(
    decoded_path: Path,
    output_root: Path,
    resolver: RuntimeResolver,
    evidence: EvidenceRegistry,
    generated_at: str,
    sample_sink: dict[str, Any],
) -> dict[str, Any]:
    map_id = "map01"
    decoded_evidence = evidence.add(decoded_path, "map01 WaterData decoded Stage101")
    decoded = load_json(decoded_path)
    auxiliary_evidence_ids: list[str] = []
    for field_name, role in (
        ("scene_manifest", "map01 WaterData scene manifest"),
        ("probe", "map01 WaterData raw probe"),
    ):
        source = decoded.get(field_name)
        if source and Path(str(source)).is_file():
            auxiliary_evidence_ids.append(evidence.add(Path(str(source)), role))

    scene_root = Path(str(decoded.get("scene_manifest"))).resolve().parent
    roots = (decoded_path.resolve().parent, scene_root)
    records: list[dict[str, Any]] = []
    flowmap_evidence_ids: list[str] = []
    for row in sorted(decoded.get("sectors") or [], key=lambda value: str(value.get("name"))):
        name = str(row.get("name") or "")
        match = re.fullmatch(r"T_(-?\d+)_(-?\d+)", name)
        if not match:
            raise ValueError(f"Invalid Map01 WaterData sector name: {name}")
        sector_x, sector_z = int(match.group(1)), int(match.group(2))
        spatial = resolver.resolve_sector(map_id, sector_x, sector_z)
        xmin = float(sector_x * SECTOR_SIZE)
        xmax = float((sector_x + 1) * SECTOR_SIZE)
        zmin = float(sector_z * SECTOR_SIZE)
        zmax = float((sector_z + 1) * SECTOR_SIZE)
        center_x = (xmin + xmax) / 2.0
        center_z = (zmin + zmax) / 2.0
        texture_export = row.get("texture_export") if isinstance(row.get("texture_export"), Mapping) else {}
        flowmap_path = _resolve_source_file(texture_export.get("File"), roots)
        flowmap_evidence = evidence.add(flowmap_path, f"map01 WaterData flowmap {name}") if flowmap_path else None
        if flowmap_evidence:
            flowmap_evidence_ids.append(flowmap_evidence)
        flowmap_reference = {
            "name": row.get("texture_name"),
            "sourcePath": str(flowmap_path) if flowmap_path else texture_export.get("File"),
            "sha256": sha256_file(flowmap_path) if flowmap_path else None,
            "evidenceId": flowmap_evidence,
            "status": "resolved" if flowmap_path else "missing",
        }
        record = {
            "recordType": "water",
            "waterId": stable_id(map_id, name, row.get("texture_name"), prefix="map01:water"),
            "mapId": map_id,
            "domainId": spatial.domain_id,
            "sourceLevelId": None,
            "spatialLevelId": spatial.level_id,
            "spatialLevelIds": [spatial.level_id] if spatial.level_id else [],
            "levelBindingMethod": "ownership-grid" if spatial.level_id else "unresolved",
            "levelBindingStatus": "spatial_only" if spatial.level_id else "unmapped",
            "levelBindingConfidence": 0.9 if spatial.level_id else 0.0,
            "positionUnity": {"x": center_x, "y": None, "z": center_z},
            "positionBlender": {"x": center_x, "y": -center_z, "z": None},
            "boundsUnity": {
                "xmin": xmin,
                "xmax": xmax,
                "ymin": None,
                "ymax": None,
                "zmin": zmin,
                "zmax": zmax,
            },
            "geometry": {
                "type": "sector-raster",
                "worldRectUnityXZ": {"xmin": xmin, "xmax": xmax, "zmin": zmin, "zmax": zmax},
                "coordinatesUnityXZ": [
                    [[xmin, zmin], [xmax, zmin], [xmax, zmax], [xmin, zmax], [xmin, zmin]]
                ],
                "imageOrientation": "+Z north/up; source texture orientation retained",
                "footprintMeaning": "WaterData sector coverage, not a verified water-surface footprint",
                "flowmap": flowmap_reference,
            },
            "geometryStatus": "candidate_sector_data_coverage",
            "surfaceY": None,
            "surfaceYMin": None,
            "surfaceYMax": None,
            "waterType": "candidate",
            "semanticHint": "WaterData sector with flowmap",
            "flowDirectionUnityXZ": None,
            "flowDirectionStatus": "flowmap_not_decoded_to_vector_field",
            "anchorSector": {"sectorX": sector_x, "sectorZ": sector_z, "key": spatial.key},
            "sectorKeys": [spatial.key],
            "bindingStatus": "candidate" if spatial.level_id else "unmapped",
            "worldResolution": "sector_coverage_only",
            "confidence": 0.55 if flowmap_path else 0.4,
            "candidateReason": (
                "WaterData and flowmap presence are real, but sector/config/PolygonWater runtime binding and "
                "surface footprint are unresolved."
            ),
            "source": {
                "sectorAssetName": name,
                "texturePathId": _decimal_string(
                    (row.get("texture") or {}).get("path_id"), field="water.source.texturePathId"
                ),
                "rippleLayerCount": int(row.get("ripple_layer_count") or 0),
                "evidenceId": decoded_evidence,
                "flowmapEvidenceId": flowmap_evidence,
            },
        }
        records.append(record)

    configs = []
    for row in sorted(decoded.get("configs") or [], key=lambda value: str(value.get("name"))):
        config = dict(row)
        config["recordType"] = "water-config"
        config["configId"] = stable_id(map_id, row.get("name"), prefix="map01:water-config")
        config["mapId"] = map_id
        config["bindingStatus"] = "unresolved_candidate"
        config["sourceEvidenceId"] = decoded_evidence
        configs.append(config)

    sample_sink["map01.water"] = records[:3]
    return _water_index(
        map_id=map_id,
        output_root=output_root,
        resolver=resolver,
        generated_at=generated_at,
        records=records,
        configs=configs,
        source_evidence_ids=[decoded_evidence, *auxiliary_evidence_ids, *flowmap_evidence_ids],
        scan={
            "status": "partial",
            "coverageScope": f"{len(records)} decoded Map01 WaterData sectors from the supplied decoded probe",
            "recordsFound": len(records),
            "limitations": [
                "Sector rectangles show WaterData coverage only and are not confirmed water-surface masks.",
                "PolygonWater, mesh, surface height, and per-sector config runtime binding remain unresolved.",
                "Flowmap channels are retained as evidence and are not interpreted as height or occupancy.",
            ],
        },
    )


def _obj_vertices(path: Path) -> list[list[float]]:
    vertices: list[list[float]] = []
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        for line in handle:
            if not line.startswith("v "):
                continue
            parts = line.split()
            if len(parts) < 4:
                raise ValueError(f"Malformed OBJ vertex in {path}: {line.rstrip()}")
            vertex = [float(parts[1]), float(parts[2]), float(parts[3])]
            if not all(math.isfinite(value) for value in vertex):
                raise ValueError(f"Non-finite OBJ vertex in {path}")
            vertices.append(vertex)
    if not vertices:
        raise ValueError(f"OBJ has no vertices: {path}")
    return vertices


def _assert_bounds_close(actual: Mapping[str, float], expected: Mapping[str, Sequence[float]], tolerance: float = 1e-3) -> None:
    minimum = expected.get("min") or []
    maximum = expected.get("max") or []
    if len(minimum) < 3 or len(maximum) < 3:
        raise ValueError("Audit mesh bounds are incomplete")
    pairs = (
        (actual["xmin"], float(minimum[0])),
        (actual["ymin"], float(minimum[1])),
        (actual["zmin"], float(minimum[2])),
        (actual["xmax"], float(maximum[0])),
        (actual["ymax"], float(maximum[1])),
        (actual["zmax"], float(maximum[2])),
    )
    if any(abs(left - right) > tolerance for left, right in pairs):
        raise AssertionError(f"OBJ bounds differ from audit: actual={actual}, audit={expected}")


def build_map02_water_layer(
    audit_path: Path,
    output_root: Path,
    resolver: RuntimeResolver,
    evidence: EvidenceRegistry,
    generated_at: str,
    sample_sink: dict[str, Any],
) -> dict[str, Any]:
    map_id = "map02"
    audit_evidence = evidence.add(audit_path, "map02 dam water binding Stage55")
    audit = load_json(audit_path)
    binding = audit.get("binding") if isinstance(audit.get("binding"), Mapping) else {}
    records: list[dict[str, Any]] = []
    mesh_evidence_ids: list[str] = []
    for row in sorted(audit.get("meshes") or [], key=lambda value: str(value.get("path"))):
        mesh_path = Path(str(row.get("path"))).resolve()
        mesh_evidence = evidence.add(mesh_path, "map02 dam PCG water mesh")
        mesh_evidence_ids.append(mesh_evidence)
        mesh_sha = sha256_file(mesh_path)
        if row.get("sha256") and mesh_sha != str(row["sha256"]).upper():
            raise AssertionError(f"Map02 water mesh hash mismatch: {mesh_path}")
        vertices = _obj_vertices(mesh_path)
        minimum = [min(vertex[index] for vertex in vertices) for index in range(3)]
        maximum = [max(vertex[index] for vertex in vertices) for index in range(3)]
        bounds = bounds_from_minmax(minimum, maximum)
        _assert_bounds_close(bounds, row.get("bounds") or {})
        hull = convex_hull((vertex[0], vertex[2]) for vertex in vertices)
        if len(hull) < 4:
            raise AssertionError(f"Map02 water mesh projection is degenerate: {mesh_path}")
        keys = sector_keys_for_bounds(bounds)
        center = [
            (bounds["xmin"] + bounds["xmax"]) / 2.0,
            (bounds["ymin"] + bounds["ymax"]) / 2.0,
            (bounds["zmin"] + bounds["zmax"]) / 2.0,
        ]
        spatial = resolver.resolve_point(map_id, center[0], center[2])
        source_level = infer_source_level(map_id, mesh_path.name)
        method, level_status, level_confidence = _level_binding(source_level, spatial.level_id)
        record = {
            "recordType": "water",
            "waterId": stable_id(map_id, mesh_sha, mesh_path.name, prefix="map02:water"),
            "mapId": map_id,
            "domainId": spatial.domain_id,
            "sourceLevelId": source_level,
            "spatialLevelId": spatial.level_id,
            "spatialLevelIds": resolver.levels_for_sector_keys(map_id, keys),
            "levelBindingMethod": method,
            "levelBindingStatus": level_status,
            "levelBindingConfidence": level_confidence,
            "positionUnity": {"x": center[0], "y": center[1], "z": center[2]},
            "positionBlender": resolver.unity_to_blender_position(center),
            "boundsUnity": bounds,
            "geometry": {
                "type": "mesh-projection",
                "coordinatesUnityXZ": [hull],
                "meshPath": str(mesh_path),
                "meshSha256": mesh_sha,
                "vertexCount": len(vertices),
                "projectionPlane": "Unity XZ",
            },
            "geometryStatus": "extracted_mesh_projection_candidate_water_binding",
            "surfaceY": None,
            "surfaceYMin": bounds["ymin"],
            "surfaceYMax": bounds["ymax"],
            "waterType": "candidate",
            "semanticHint": "dam PCG water mesh",
            "flowDirectionUnityXZ": None,
            "flowDirectionStatus": "not_extracted",
            "materialConfigName": binding.get("configName"),
            "materialConfigIndex": binding.get("candidateConfigIndex"),
            "materialBindingStatus": "strong_evidence_candidate",
            "anchorSector": {
                "sectorX": spatial.sector_x,
                "sectorZ": spatial.sector_z,
                "key": spatial.key,
            },
            "sectorKeys": keys,
            "bindingStatus": "candidate",
            "worldResolution": "world_mesh",
            "confidence": 0.9,
            "candidateReason": (
                "Geometry, UV0, and mesh hashes are verified; Config 29 is a strong binding candidate, "
                "but this local dam closure is not a complete Map02 water inventory."
            ),
            "source": {
                "meshPath": str(mesh_path),
                "meshEvidenceId": mesh_evidence,
                "auditEvidenceId": audit_evidence,
            },
        }
        records.append(record)

    config = {
        "recordType": "water-config",
        "configId": stable_id(map_id, binding.get("configName"), prefix="map02:water-config"),
        "mapId": map_id,
        "name": binding.get("configName"),
        "configIndex": binding.get("candidateConfigIndex"),
        "materialIndex": binding.get("materialIndex"),
        "bindingStatus": "strong_evidence_candidate",
        "parameters": binding.get("relevantFields") or {},
        "sourceEvidenceId": audit_evidence,
    }
    sample_sink["map02.water"] = records[:3]
    sample_sink["water.crossSector"] = next(
        (record for record in records if len(record.get("sectorKeys") or []) > 1), None
    )
    return _water_index(
        map_id=map_id,
        output_root=output_root,
        resolver=resolver,
        generated_at=generated_at,
        records=records,
        configs=[config],
        source_evidence_ids=[audit_evidence, *mesh_evidence_ids],
        scan={
            "status": "partial",
            "coverageScope": "three verified PCG water meshes in the Map02 dam closure",
            "recordsFound": len(records),
            "limitations": [
                "The three meshes cover the dam only and are not a complete Map02 water inventory.",
                "Config 29 remains a strong evidence candidate rather than a final runtime binding.",
                "Mesh projection is exact in Unity XZ, but no canonical flat surfaceY is asserted.",
            ],
        },
    )


def _vector3(value: Any) -> list[float] | None:
    if isinstance(value, Mapping):
        keys = ("x", "y", "z")
        if all(key in value and value[key] is not None for key in keys):
            result = [float(value[key]) for key in keys]
            return result if all(math.isfinite(item) for item in result) else None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 3:
        result = [float(value[0]), float(value[1]), float(value[2])]
        return result if all(math.isfinite(item) for item in result) else None
    return None


def _normalize_light_records(
    map_id: str,
    scans: Sequence[tuple[Mapping[str, Any], str]],
    resolver: RuntimeResolver,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for scan_index, (scan, evidence_id) in enumerate(scans):
        for record_index, source in enumerate(scan.get("records") or []):
            if not isinstance(source, Mapping):
                continue
            position = _vector3(source.get("positionUnity") or source.get("position") or source.get("world_position"))
            direction = _vector3(source.get("directionUnity") or source.get("direction") or source.get("world_direction"))
            type_name = str(source.get("type") or source.get("lightType") or source.get("class") or "unknown").casefold()
            light_type = next(
                (candidate for candidate in ("point", "spot", "area", "directional") if candidate in type_name),
                "unknown",
            )
            spatial = resolver.resolve_point(map_id, position[0], position[2]) if position else None
            light_id = stable_id(
                map_id,
                scan_index,
                record_index,
                source.get("name"),
                source.get("pathId") or source.get("path_id"),
                prefix=f"{map_id}:light",
            )
            sector = (
                {"sectorX": spatial.sector_x, "sectorZ": spatial.sector_z, "key": spatial.key}
                if spatial
                else None
            )
            records.append(
                {
                    "recordType": "light",
                    "lightId": light_id,
                    "mapId": map_id,
                    "domainId": resolver.map(map_id)["domainId"],
                    "sourceLevelId": infer_source_level(map_id, source.get("name"), source.get("source")),
                    "spatialLevelId": spatial.level_id if spatial else None,
                    "spatialLevelIds": [spatial.level_id] if spatial and spatial.level_id else [],
                    "anchorId": source.get("anchorId") or light_id,
                    "type": light_type,
                    "positionUnity": (
                        {"x": position[0], "y": position[1], "z": position[2]} if position else None
                    ),
                    "positionBlender": resolver.unity_to_blender_position(position) if position else None,
                    "directionUnity": (
                        {"x": direction[0], "y": direction[1], "z": direction[2]} if direction else None
                    ),
                    "directionBlender": resolver.unity_to_blender_direction(direction) if direction else None,
                    "directionStatus": "extracted" if direction else "not_applicable_or_unavailable",
                    "rangeMetres": source.get("rangeMetres") or source.get("range"),
                    "colorLinear": source.get("colorLinear") or source.get("color"),
                    "colorSpace": source.get("colorSpace") or "source_unspecified",
                    "intensity": source.get("intensity"),
                    "intensityUnit": source.get("intensityUnit") or "source_unspecified",
                    "areaShape": source.get("areaShape"),
                    "areaSize": source.get("areaSize"),
                    "anchorSector": sector,
                    "sectorKeys": [spatial.key] if spatial else [],
                    "bindingStatus": "mapped" if spatial and spatial.level_id else "candidate",
                    "extractionStatus": "normalized_verified_light_record",
                    "source": {"evidenceId": evidence_id, "recordIndex": record_index},
                }
            )
    return records


def _light_index(
    *,
    map_id: str,
    output_root: Path,
    resolver: RuntimeResolver,
    generated_at: str,
    records: Sequence[Mapping[str, Any]],
    source_evidence_ids: Sequence[str],
    scan: Mapping[str, Any],
) -> dict[str, Any]:
    layer_root = output_root / map_id / "lights"
    records_path = layer_root / "records.jsonl.gz"
    record_count, record_sha, record_bytes = write_jsonl_gzip(records_path, records)
    binding_counts = Counter(str(row.get("bindingStatus") or "unknown") for row in records)
    sector_keys = sorted({str(key) for row in records for key in row.get("sectorKeys") or []})
    spatial_level_ids = sorted(
        {str(level) for row in records for level in row.get("spatialLevelIds") or [] if level}
    )
    index = {
        "format": DATASET_FORMAT,
        "datasetId": f"{map_id}.lights.stage119",
        "generatorVersion": GENERATOR_VERSION,
        "generatedAt": generated_at,
        "mapId": map_id,
        "domainId": resolver.map(map_id)["domainId"],
        "layer": "lights",
        "recordType": "light",
        "recordEncoding": "jsonl+gzip",
        "geometryTypes": ["point", "direction", "range"],
        "scan": dict(scan),
        "recordCount": record_count,
        "coverageCount": int(binding_counts.get("mapped", 0)),
        "duplicateCount": 0,
        "pendingCount": int(binding_counts.get("candidate", 0) + binding_counts.get("unmapped", 0)),
        "bindingCounts": dict(sorted(binding_counts.items())),
        "sectorKeys": sector_keys,
        "spatialLevelIds": spatial_level_ids,
        "records": {
            "path": _relative(records_path, output_root),
            "bytes": record_bytes,
            "sha256": record_sha,
        },
        "sourceEvidenceIds": list(source_evidence_ids),
        "validation": {
            "syntheticLightsExcluded": True,
            "emissiveMaterialsExcluded": True,
            "emptyRecordsPreserveScanStatus": record_count > 0 or scan.get("status") in {"not_started", "partial"},
        },
    }
    index_path = layer_root / "index.json"
    validate_dataset_index(index)
    write_json(index_path, index)
    return {
        "indexPath": _relative(index_path, output_root),
        "recordCount": record_count,
        "bindingCounts": index["bindingCounts"],
        "geometryTypes": index["geometryTypes"],
        "scanStatus": index["scan"]["status"],
        "coverageCount": index["coverageCount"],
        "duplicateCount": index["duplicateCount"],
        "pendingCount": index["pendingCount"],
        "sectorKeys": sector_keys,
        "spatialLevelIds": spatial_level_ids,
        "sourceEvidenceIds": list(source_evidence_ids),
    }


def build_map01_light_layer(
    report_path: Path,
    output_root: Path,
    resolver: RuntimeResolver,
    evidence: EvidenceRegistry,
    generated_at: str,
    sample_sink: dict[str, Any],
) -> dict[str, Any]:
    report_evidence = evidence.add(report_path, "Stage115 light research report")
    sample_sink["map01.light"] = None
    return _light_index(
        map_id="map01",
        output_root=output_root,
        resolver=resolver,
        generated_at=generated_at,
        records=[],
        source_evidence_ids=[report_evidence],
        scan={
            "status": "not_started",
            "coverageScope": "No valid Map01 scene closure light scan has been completed.",
            "recordsFound": 0,
            "limitations": [
                "The prior Map01 UI-map texture closure had zero GameObjects and is not a valid scene light scan.",
                "Stage114/116 synthetic review lights and material emission are excluded from native lighting records.",
            ],
        },
    )


def build_map02_light_layer(
    scan_paths: Sequence[Path],
    output_root: Path,
    resolver: RuntimeResolver,
    evidence: EvidenceRegistry,
    generated_at: str,
    sample_sink: dict[str, Any],
) -> dict[str, Any]:
    scans: list[tuple[Mapping[str, Any], str]] = []
    source_evidence_ids: list[str] = []
    coverage: list[dict[str, Any]] = []
    for path in scan_paths:
        evidence_id = evidence.add(path, "Map02 prepared-closure light scan")
        scan = load_json(path)
        scans.append((scan, evidence_id))
        source_evidence_ids.append(evidence_id)
        coverage.append(
            {
                "input": scan.get("input"),
                "inputFileCount": int(scan.get("inputFileCount") or 0),
                "loadedAssetFileCount": int(scan.get("loadedAssetFileCount") or 0),
                "gameObjectCount": int(scan.get("gameObjectCount") or 0),
                "targetObjectCount": int(scan.get("targetObjectCount") or 0),
                "recordCount": len(scan.get("records") or []),
                "evidenceId": evidence_id,
            }
        )
    records = _normalize_light_records("map02", scans, resolver)
    sample_sink["map02.light"] = records[0] if records else None
    return _light_index(
        map_id="map02",
        output_root=output_root,
        resolver=resolver,
        generated_at=generated_at,
        records=records,
        source_evidence_ids=source_evidence_ids,
        scan={
            "status": "partial",
            "coverageScope": "Map02 dam geometry and main-city dragon/waterfall/energy-flow prepared closures",
            "recordsFound": len(records),
            "closures": coverage,
            "limitations": [
                "Both reviewed closures contained zero native or proven custom light records.",
                "This localized negative result cannot be extrapolated to the full Map02 map.",
                "Synthetic review lights, lamp meshes, names, and emissive materials are excluded.",
            ],
        },
    )


def build_unscanned_optional_layer(
    layer: str,
    map_id: str,
    output_root: Path,
    resolver: RuntimeResolver,
    generated_at: str,
) -> dict[str, Any]:
    scan = {
        "status": "not_started",
        "coverageScope": "No automatic first-run closure has been completed for this layer.",
        "inputCount": 0,
        "limitations": [
            "The layer is intentionally empty and must not be interpreted as absence in the game.",
            "A later reviewed scan may replace this not_started dataset in a new output root.",
        ],
    }
    if layer == "water":
        return _water_index(
            map_id=map_id,
            output_root=output_root,
            resolver=resolver,
            generated_at=generated_at,
            records=[],
            configs=[],
            source_evidence_ids=[],
            scan=scan,
        )
    if layer == "effects":
        return _effect_index(
            map_id=map_id,
            output_root=output_root,
            resolver=resolver,
            generated_at=generated_at,
            records=[],
            anchors=[],
            prototypes=[],
            prototype_members=[],
            source_evidence_ids=[],
            scan=scan,
        )
    if layer == "lights":
        return _light_index(
            map_id=map_id,
            output_root=output_root,
            resolver=resolver,
            generated_at=generated_at,
            records=[],
            source_evidence_ids=[],
            scan=scan,
        )
    raise ValueError(f"Unsupported optional layer: {layer}")


def build_road_view(
    map_id: str,
    instance_layer: Mapping[str, Any],
    output_root: Path,
    resolver: RuntimeResolver,
    generated_at: str,
) -> dict[str, Any]:
    record_count = int((instance_layer.get("categoryCounts") or {}).get("road", 0))
    binding_counts = dict((instance_layer.get("categoryBindingCounts") or {}).get("road") or {})
    coverage_count = int(binding_counts.get("mapped", 0))
    pending_count = int(binding_counts.get("candidate", 0) + binding_counts.get("unmapped", 0))
    road_shards = []
    for shard in instance_layer.get("shards") or []:
        road_record_count = int((shard.get("categoryCounts") or {}).get("road", 0))
        if road_record_count <= 0:
            continue
        road_shards.append(
            {
                "sectorKey": str(shard["sectorKey"]),
                "path": str(shard["path"]),
                "sourceShardRecordCount": int(shard["recordCount"]),
                "roadRecordCount": road_record_count,
                "sourceShardSha256": str(shard["sha256"]),
            }
        )
    if sum(int(row["roadRecordCount"]) for row in road_shards) != record_count:
        raise AssertionError(f"{map_id} road shard totals do not match road recordCount")
    layer_root = output_root / map_id / "roads"
    index = {
        "format": DATASET_FORMAT,
        "datasetId": f"{map_id}.roads.stage119",
        "generatorVersion": GENERATOR_VERSION,
        "generatedAt": generated_at,
        "mapId": map_id,
        "domainId": resolver.map(map_id)["domainId"],
        "layer": "roads",
        "recordType": "instance-view",
        "recordEncoding": "view-reference",
        "geometryTypes": ["point"],
        "scan": {
            "status": instance_layer.get("scanStatus"),
            "coverageScope": "category view over the normalized full instance layer",
            "recordsFound": record_count,
            "limitations": ["Road footprints and polylines are not extracted; this first version renders instance points."],
        },
        "recordCount": record_count,
        "coverageCount": coverage_count,
        "duplicateCount": 0,
        "pendingCount": pending_count,
        "bindingCounts": binding_counts,
        "viewOf": instance_layer["indexPath"],
        "filter": {"category": "road"},
        "sectorKeys": [row["sectorKey"] for row in road_shards],
        "roadShards": road_shards,
        "spatialLevelIds": resolver.levels_for_sector_keys(map_id, [row["sectorKey"] for row in road_shards]),
        "sourceEvidenceIds": list(instance_layer.get("sourceEvidenceIds") or []),
        "validation": {
            "noRecordDuplication": True,
            "sourceCategory": "road",
            "roadShardRecordCount": sum(int(row["roadRecordCount"]) for row in road_shards),
            "roadShardRecordCountMatches": sum(int(row["roadRecordCount"]) for row in road_shards)
            == record_count,
        },
    }
    index_path = layer_root / "index.json"
    validate_dataset_index(index)
    write_json(index_path, index)
    return {
        "indexPath": _relative(index_path, output_root),
        "recordCount": record_count,
        "bindingCounts": binding_counts,
        "geometryTypes": index["geometryTypes"],
        "scanStatus": index["scan"]["status"],
        "coverageCount": coverage_count,
        "duplicateCount": 0,
        "pendingCount": pending_count,
        "sectorKeys": index["sectorKeys"],
        "roadShards": index["roadShards"],
        "spatialLevelIds": index["spatialLevelIds"],
        "sourceEvidenceIds": index["sourceEvidenceIds"],
        "viewOf": index["viewOf"],
        "filter": index["filter"],
    }


def _sample(label: str, value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {"label": label, "available": False, "record": None, "recordSha256": None}
    record = dict(value)
    validate_layer_record(record)
    return {
        "label": label,
        "available": True,
        "record": record,
        "recordSha256": sha256_json(record),
    }


def write_validation_samples(
    output_root: Path,
    sample_sink: Mapping[str, Any],
    *,
    bootstrap_mode: bool = False,
) -> tuple[dict[str, Any], str]:
    samples: list[dict[str, Any]] = []
    for map_id in ("map01", "map02"):
        mapped = list(sample_sink.get(f"{map_id}.instances.mapped") or [])
        for index, record in enumerate(mapped[:3], start=1):
            samples.append(_sample(f"{map_id} mapped instance {index}", record))
        water = list(sample_sink.get(f"{map_id}.water") or [])
        samples.append(_sample(f"{map_id} water candidate", water[0] if water else None))
        samples.append(_sample(f"{map_id} effect anchor", sample_sink.get(f"{map_id}.effectAnchor")))
    unmapped = sample_sink.get("map01.instances.unmapped") or sample_sink.get("map02.instances.unmapped")
    samples.append(_sample("spatial record without UI ownership", unmapped))
    samples.append(_sample("cross-sector water geometry", sample_sink.get("water.crossSector")))
    full_anchor = sample_sink.get("map01.effectAnchor") or sample_sink.get("map02.effectAnchor")
    samples.append(_sample("full_system_by_anchor", full_anchor))
    selection_resolution = None
    if full_anchor:
        member_effect_ids = list(full_anchor.get("memberEffectIds") or [])
        selection_resolution = {
            "hitId": member_effect_ids[0] if member_effect_ids else full_anchor.get("anchorId"),
            "resolvedAnchorId": full_anchor.get("anchorId"),
            "returnedMemberEffectIds": member_effect_ids,
            "returnedPrototypeMemberIds": list(full_anchor.get("prototypeMemberIds") or []),
            "policy": "full_system_by_anchor",
            "resultSha256": sha256_json(full_anchor),
        }
    elif bootstrap_mode:
        selection_resolution = {
            "status": "not_started",
            "policy": "full_system_by_anchor",
            "reason": "Effect anchor extraction is deferred in first-run bootstrap mode.",
        }
    document = {
        "format": "EndfieldMapLayerValidationSamples/1",
        "generatorVersion": GENERATOR_VERSION,
        "mode": "bootstrap" if bootstrap_mode else "formal",
        "samples": samples,
        "selectionResolution": selection_resolution,
    }
    path = output_root / "validation_samples.json"
    write_json(path, document)
    return document, _relative(path, output_root)


def _artifact_inventory(output_root: Path) -> list[dict[str, Any]]:
    rows = []
    excluded = {
        "artifact_inventory.json",
        "region_map_layer_manifest.json",
        "validation.json",
        "build_summary.json",
    }
    for path in sorted(output_root.rglob("*"), key=lambda value: value.as_posix()):
        if not path.is_file() or path.name in excluded or path.name.endswith(".tmp"):
            continue
        rows.append(
            {
                "path": _relative(path, output_root),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return rows


def _manifest_layer(
    map_id: str,
    layer_name: str,
    summary: Mapping[str, Any],
    resolver: RuntimeResolver,
) -> dict[str, Any]:
    map_spec = resolver.map(map_id)
    return {
        "mapId": map_id,
        "domainId": map_spec["domainId"],
        "layer": layer_name,
        "indexPath": summary["indexPath"],
        "worldBounds": dict(map_spec["worldBounds"]),
        "levelIds": [str(level["levelId"]) for level in map_spec.get("levels") or []],
        "spatialLevelIds": list(summary.get("spatialLevelIds") or []),
        "sectorKeys": list(summary.get("sectorKeys") or []),
        "geometryTypes": list(summary.get("geometryTypes") or []),
        "bindingCounts": dict(summary.get("bindingCounts") or {}),
        "bindingStatus": "mixed" if len(summary.get("bindingCounts") or {}) > 1 else next(
            iter(summary.get("bindingCounts") or {"unavailable": 0})
        ),
        "coverageCount": int(summary.get("coverageCount") or 0),
        "duplicateCount": int(summary.get("duplicateCount") or 0),
        "pendingCount": int(summary.get("pendingCount") or 0),
        "recordCount": int(summary.get("recordCount") or 0),
        "scanStatus": summary.get("scanStatus"),
        "sourceEvidenceIds": list(summary.get("sourceEvidenceIds") or []),
        **({"viewOf": summary["viewOf"], "filter": summary["filter"]} if summary.get("viewOf") else {}),
    }


def build_all(config: BuildConfig) -> dict[str, Any]:
    if not all((config.map01_instances, config.map01_assets, config.map02_instances, config.map02_assets)):
        raise ValueError("Both maps require instances and asset-resolution databases")
    output_root = ensure_new_output_root(
        config.output_root,
        export_root=config.export_root,
        game_root=config.game_root,
    )
    evidence = EvidenceRegistry(output_root)
    runtime_evidence = evidence.add(config.runtime_manifest, "WebUI region map runtime manifest")
    resolver = RuntimeResolver.from_path(config.runtime_manifest)
    sample_sink: dict[str, Any] = {}

    map01_instances = build_instance_layer(
        "map01",
        config.map01_instances,
        config.map01_assets,
        output_root,
        resolver,
        evidence,
        config.generated_at,
        config.max_instances_per_map,
        sample_sink,
    )
    map02_instances = build_instance_layer(
        "map02",
        config.map02_instances,
        config.map02_assets,
        output_root,
        resolver,
        evidence,
        config.generated_at,
        config.max_instances_per_map,
        sample_sink,
    )
    optional_pairs = {
        "map01 water": (config.map01_water,),
        "map02 water": (config.map02_water,),
        "map01 effects": (config.map01_effects, config.map01_effect_semantics),
        "map02 effects": (config.map02_effects, config.map02_effect_semantics),
        "map01 lights": (config.map01_light_report,),
    }
    for label, values in optional_pairs.items():
        supplied = [value is not None for value in values]
        if any(supplied) and not all(supplied):
            raise ValueError(f"Incomplete optional source set for {label}")
        if not config.bootstrap_mode and not all(supplied):
            raise ValueError(
                f"Optional source set for {label} is missing. Pass all inputs or use --bootstrap-mode."
            )

    map01_water = (
        build_map01_water_layer(
            config.map01_water, output_root, resolver, evidence, config.generated_at, sample_sink
        )
        if config.map01_water is not None
        else build_unscanned_optional_layer("water", "map01", output_root, resolver, config.generated_at)
    )
    map02_water = (
        build_map02_water_layer(
            config.map02_water, output_root, resolver, evidence, config.generated_at, sample_sink
        )
        if config.map02_water is not None
        else build_unscanned_optional_layer("water", "map02", output_root, resolver, config.generated_at)
    )
    map01_effects = (
        build_map01_effect_layer(
            config.map01_effects,
            config.map01_effect_semantics,
            output_root,
            resolver,
            evidence,
            config.generated_at,
            sample_sink,
        )
        if config.map01_effects is not None and config.map01_effect_semantics is not None
        else build_unscanned_optional_layer("effects", "map01", output_root, resolver, config.generated_at)
    )
    map02_effects = (
        build_map02_effect_layer(
            config.map02_effects,
            config.map02_effect_semantics,
            output_root,
            resolver,
            evidence,
            config.generated_at,
            sample_sink,
        )
        if config.map02_effects is not None and config.map02_effect_semantics is not None
        else build_unscanned_optional_layer("effects", "map02", output_root, resolver, config.generated_at)
    )
    map01_lights = (
        build_map01_light_layer(
            config.map01_light_report, output_root, resolver, evidence, config.generated_at, sample_sink
        )
        if config.map01_light_report is not None
        else build_unscanned_optional_layer("lights", "map01", output_root, resolver, config.generated_at)
    )
    map02_lights = (
        build_map02_light_layer(
            config.map02_light_scans, output_root, resolver, evidence, config.generated_at, sample_sink
        )
        if config.map02_light_scans
        else build_unscanned_optional_layer("lights", "map02", output_root, resolver, config.generated_at)
    )
    map01_roads = build_road_view("map01", map01_instances, output_root, resolver, config.generated_at)
    map02_roads = build_road_view("map02", map02_instances, output_root, resolver, config.generated_at)

    source_evidence_path = output_root / "source_evidence.json"
    write_json(
        source_evidence_path,
        {
            "format": "EndfieldMapLayerSourceEvidence/1",
            "generatorVersion": GENERATOR_VERSION,
            "sources": evidence.rows,
        },
    )
    samples, samples_path = write_validation_samples(
        output_root,
        sample_sink,
        bootstrap_mode=config.bootstrap_mode,
    )

    schema_paths = []
    for schema_name in (
        "region_map_layer_manifest.schema.json",
        "endfield_map_layer_dataset.schema.json",
        "endfield_map_layer_record.schema.json",
    ):
        source_schema = SCRIPT_ROOT / schema_name
        if not source_schema.is_file():
            raise FileNotFoundError(source_schema)
        target_schema = output_root / "schemas" / schema_name
        write_json(target_schema, load_json(source_schema))
        schema_paths.append(_relative(target_schema, output_root))

    layers = {
        "map01": {
            "instances": map01_instances,
            "water": map01_water,
            "effects": map01_effects,
            "lights": map01_lights,
            "roads": map01_roads,
        },
        "map02": {
            "instances": map02_instances,
            "water": map02_water,
            "effects": map02_effects,
            "lights": map02_lights,
            "roads": map02_roads,
        },
    }
    inventory = _artifact_inventory(output_root)
    inventory_path = output_root / "artifact_inventory.json"
    write_json(
        inventory_path,
        {
            "format": "EndfieldMapLayerArtifactInventory/1",
            "generatorVersion": GENERATOR_VERSION,
            "artifacts": inventory,
        },
    )
    dataset_fingerprint = sha256_json(inventory)

    manifest_maps = {}
    for map_id, map_layers in layers.items():
        map_spec = resolver.map(map_id)
        manifest_maps[map_id] = {
            "mapId": map_id,
            "domainId": map_spec["domainId"],
            "worldBounds": dict(map_spec["worldBounds"]),
            "levels": list(map_spec.get("levels") or []),
            "layers": {
                layer_name: _manifest_layer(map_id, layer_name, summary, resolver)
                for layer_name, summary in map_layers.items()
            },
        }

    sample_checks = {
        "map01ThreeMappedInstances": len(sample_sink.get("map01.instances.mapped") or []) >= 3,
        "map02ThreeMappedInstances": len(sample_sink.get("map02.instances.mapped") or []) >= 3,
        "map01WaterCandidate": bool(sample_sink.get("map01.water")) or config.bootstrap_mode,
        "map02WaterCandidate": bool(sample_sink.get("map02.water")) or config.bootstrap_mode,
        "crossSectorObject": bool(sample_sink.get("water.crossSector")) or config.bootstrap_mode,
        "unmappedSpatialObject": bool(
            sample_sink.get("map01.instances.unmapped") or sample_sink.get("map02.instances.unmapped")
        ),
        "fullSystemByAnchor": bool(samples.get("selectionResolution")) or config.bootstrap_mode,
    }
    layer_checks = {
        "map01InstanceSourceCountMatches": config.max_instances_per_map is not None
        or map01_instances["recordCount"] == map01_instances["sourceRecordCount"],
        "map02InstanceSourceCountMatches": config.max_instances_per_map is not None
        or map02_instances["recordCount"] == map02_instances["sourceRecordCount"],
        "map01InstancesNonEmpty": map01_instances["recordCount"] > 0,
        "map02InstancesNonEmpty": map02_instances["recordCount"] > 0,
        "map01RoadViewBuilt": map01_roads["recordCount"] >= 0,
        "map02RoadViewBuilt": map02_roads["recordCount"] >= 0,
        "roadShardTotalsMatch": all(
            sum(int(shard["roadRecordCount"]) for shard in layer["roadShards"]) == int(layer["recordCount"])
            for layer in (map01_roads, map02_roads)
        ),
        "effectMemberReferencesResolved": all(
            layer["memberReferencesResolved"] and layer["prototypeMemberReferencesResolved"]
            for layer in (map01_effects, map02_effects)
        ),
        "waterCandidateBoundariesPreserved": all(
            layer["bindingCounts"].get("mapped", 0) == 0 for layer in (map01_water, map02_water)
        ),
        "map01LightCoveragePreserved": map01_lights["scanStatus"] == "not_started",
        "map02LightCoveragePreserved": map02_lights["scanStatus"] in {"partial", "not_started"},
        "allLayerIndexesExist": all(
            (output_root / summary["indexPath"]).is_file()
            for map_layers in layers.values()
            for summary in map_layers.values()
        ),
    }
    validation_passed = all(sample_checks.values()) and all(layer_checks.values())
    manifest = {
        "format": MANIFEST_FORMAT,
        "resolverVersion": "endfield-sector128-v1",
        "generatorVersion": GENERATOR_VERSION,
        "generatedAt": config.generated_at,
        "datasetFingerprint": dataset_fingerprint,
        "coordinateConvention": dict(resolver.manifest["coordinateConvention"]),
        "layerDefinitions": {
            "instances": "normalized scene instance points and transform records",
            "water": "verified geometry or explicitly marked candidate water coverage",
            "effects": "outer effect instances, anchors, prototypes, and full prototype members",
            "lights": "native or proven custom light records with explicit scan coverage",
            "roads": "zero-copy category view over instances where category=road",
        },
        "maps": manifest_maps,
        "sourceRuntimeManifestEvidenceId": runtime_evidence,
        "sourceEvidence": evidence.rows,
        "sourceEvidencePath": _relative(source_evidence_path, output_root),
        "artifactInventoryPath": _relative(inventory_path, output_root),
        "schemas": schema_paths,
        "validation": {
            "status": "passed" if validation_passed else "failed",
            "path": "validation.json",
            "samplesPath": samples_path,
            "sampleChecks": sample_checks,
            "layerChecks": layer_checks,
        },
    }
    validate_manifest(manifest)
    manifest_path = output_root / "region_map_layer_manifest.json"
    write_json(manifest_path, manifest)

    validation = {
        "format": VALIDATION_FORMAT,
        "generatorVersion": GENERATOR_VERSION,
        "generatedAt": config.generated_at,
        "status": "passed" if validation_passed else "failed",
        "datasetFingerprint": dataset_fingerprint,
        "manifest": {
            "path": _relative(manifest_path, output_root),
            "sha256": sha256_file(manifest_path),
        },
        "sampleChecks": sample_checks,
        "layerChecks": layer_checks,
        "samplesPath": samples_path,
        "artifactInventoryPath": _relative(inventory_path, output_root),
        "gameInstallationWritten": False,
        "blenderRequired": False,
    }
    validation_path = output_root / "validation.json"
    write_json(validation_path, validation)
    if not validation_passed:
        raise AssertionError(f"Stage119 validation failed: {validation}")

    summary = {
        "format": "EndfieldMapLayerBuildSummary/1",
        "generatorVersion": GENERATOR_VERSION,
        "generatedAt": config.generated_at,
        "outputRoot": ".",
        "manifestPath": _relative(manifest_path, output_root),
        "datasetFingerprint": dataset_fingerprint,
        "validationStatus": validation["status"],
        "maps": {
            map_id: {
                layer_name: {
                    "recordCount": int(layer.get("recordCount") or 0),
                    "scanStatus": layer.get("scanStatus"),
                    "indexPath": layer["indexPath"],
                }
                for layer_name, layer in map_layers.items()
            }
            for map_id, map_layers in layers.items()
        },
    }
    write_json(output_root / "build_summary.json", summary)
    return summary


def main() -> int:
    args = arguments()
    generated_at = args.generated_at or datetime.now(timezone.utc).isoformat()
    config = BuildConfig(
        output_root=args.output_root,
        export_root=args.export_root,
        game_root=args.game_root,
        runtime_manifest=args.runtime_manifest,
        map01_instances=args.map01_instances,
        map01_assets=args.map01_assets,
        map02_instances=args.map02_instances,
        map02_assets=args.map02_assets,
        map01_water=args.map01_water,
        map02_water=args.map02_water,
        map01_effects=args.map01_effects,
        map01_effect_semantics=args.map01_effect_semantics,
        map02_effects=args.map02_effects,
        map02_effect_semantics=args.map02_effect_semantics,
        map01_light_report=args.map01_light_report,
        map02_light_scans=tuple(args.map02_light_scan),
        generated_at=generated_at,
        max_instances_per_map=args.max_instances_per_map,
        bootstrap_mode=args.bootstrap_mode,
    )
    summary = build_all(config)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
