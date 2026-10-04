"""Audit a built Endfield map-layer dataset without Blender or game scanning."""

from __future__ import annotations

import argparse
import gzip
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Mapping

from endfield_map_layer_contract import (
    load_json,
    sha256_file,
    sha256_json,
    validate_browser_safe_path_ids,
    validate_dataset_index,
    validate_layer_record,
    validate_manifest,
    write_json,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit standardized Endfield map-layer data")
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--report", type=Path, help="Optional new JSON report path")
    parser.add_argument(
        "--verify-source-hashes",
        action="store_true",
        help="Re-hash every external source evidence file (slower but strongest verification)",
    )
    return parser.parse_args()


def iter_jsonl_gzip(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Non-object JSONL record at {path}:{line_number}")
            yield value


def add_check(checks: list[dict[str, Any]], name: str, passed: bool, **details: Any) -> None:
    checks.append({"name": name, "passed": bool(passed), **details})


def verify_artifact_inventory(root: Path, manifest: Mapping[str, Any], checks: list[dict[str, Any]]) -> None:
    inventory_path = root / str(manifest["artifactInventoryPath"])
    inventory = load_json(inventory_path)
    rows = inventory.get("artifacts") or []
    failures = []
    for row in rows:
        path = root / str(row["path"])
        if not path.is_file():
            failures.append({"path": row["path"], "reason": "missing"})
            continue
        if path.stat().st_size != int(row["bytes"]):
            failures.append({"path": row["path"], "reason": "byte_count"})
            continue
        if sha256_file(path) != str(row["sha256"]):
            failures.append({"path": row["path"], "reason": "sha256"})
    add_check(
        checks,
        "artifact_inventory_hashes",
        not failures,
        artifactCount=len(rows),
        failures=failures,
    )


def verify_source_evidence(
    manifest: Mapping[str, Any], checks: list[dict[str, Any]], verify_hashes: bool
) -> None:
    failures = []
    for row in manifest.get("sourceEvidence") or []:
        path = Path(str(row["path"]))
        if not path.is_file():
            failures.append({"evidenceId": row["evidenceId"], "reason": "missing"})
            continue
        if path.stat().st_size != int(row["bytes"]):
            failures.append({"evidenceId": row["evidenceId"], "reason": "byte_count"})
            continue
        if verify_hashes and sha256_file(path) != str(row["sha256"]):
            failures.append({"evidenceId": row["evidenceId"], "reason": "sha256"})
    add_check(
        checks,
        "source_evidence",
        not failures,
        sourceCount=len(manifest.get("sourceEvidence") or []),
        hashesVerified=verify_hashes,
        failures=failures,
    )


def verify_instances(
    root: Path, map_id: str, index: Mapping[str, Any], checks: list[dict[str, Any]]
) -> dict[str, Any]:
    record_count = 0
    category_counts: Counter[str] = Counter()
    binding_counts: Counter[str] = Counter()
    category_binding_counts: Counter[tuple[str, str]] = Counter()
    shard_failures = []
    for shard in index.get("shards") or []:
        path = root / str(shard["path"])
        if not path.is_file() or sha256_file(path) != str(shard["sha256"]):
            shard_failures.append({"path": shard["path"], "reason": "missing_or_hash"})
            continue
        local_count = 0
        for record in iter_jsonl_gzip(path):
            validate_layer_record(record)
            matrix = record.get("matrixUnityColumnMajor") or []
            if len(matrix) != 16 or not all(math.isfinite(float(value)) for value in matrix):
                raise ValueError(f"Invalid matrix in {path}: {record.get('instanceId')}")
            if (record.get("anchorSector") or {}).get("key") != shard.get("sectorKey"):
                raise ValueError(f"Instance is in the wrong shard: {record.get('instanceId')}")
            category = str(record.get("category") or "unknown")
            binding = str(record.get("bindingStatus") or "unknown")
            category_counts[category] += 1
            binding_counts[binding] += 1
            category_binding_counts[(category, binding)] += 1
            record_count += 1
            local_count += 1
        if local_count != int(shard["recordCount"]):
            shard_failures.append({"path": shard["path"], "reason": "record_count"})

    assets = index["assetTable"]
    asset_path = root / str(assets["path"])
    asset_resolution_counts: Counter[str] = Counter()
    asset_resolution_instance_counts: Counter[str] = Counter()
    asset_semantic_failures = []
    asset_count = 0
    for row in iter_jsonl_gzip(asset_path):
        validate_browser_safe_path_ids(row, context=f"{map_id}.instances.assetTable")
        status = str(row.get("resolutionStatus") or "unknown")
        method = str(row.get("resolutionMethod") or "").casefold()
        lookup_status = str(row.get("lookupStatus") or "")
        instance_count = int(row.get("instanceCount") or 0)
        asset_resolution_counts[status] += 1
        asset_resolution_instance_counts[status] += instance_count
        valid_semantics = (
            (lookup_status == "missing" and status == "missing_resolution" and not method)
            or (lookup_status == "present" and status == "resolved" and method in {"model", "prefab", "ecs_instance_binding"})
            or (lookup_status == "present" and status == "proxy_only" and method == "report_only_known_proxy_or_nonvisible_renderer")
            or (
                lookup_status == "present"
                and status == "unresolved"
                and (method == "unresolved" or (method.startswith("report_only_") and method != "report_only_known_proxy_or_nonvisible_renderer"))
            )
        )
        if not valid_semantics:
            asset_semantic_failures.append(
                {
                    "assetId": row.get("assetId"),
                    "lookupStatus": lookup_status,
                    "resolutionStatus": status,
                    "resolutionMethod": row.get("resolutionMethod"),
                }
            )
        asset_count += 1
    pending_statuses = {"unresolved", "proxy_only", "missing_resolution"}
    expected_asset_pending = sum(int(asset_resolution_counts.get(status, 0)) for status in pending_statuses)
    expected_instance_pending = sum(
        int(asset_resolution_instance_counts.get(status, 0)) for status in pending_statuses
    )
    actual_category_binding = {
        category: {
            status: int(category_binding_counts[(category, status)])
            for status in ("mapped", "candidate", "unmapped")
            if category_binding_counts[(category, status)]
        }
        for category in sorted(category_counts)
    }
    passed = (
        not shard_failures
        and record_count == int(index["recordCount"])
        and record_count == sum(int(row["recordCount"]) for row in index.get("shards") or [])
        and dict(sorted(category_counts.items())) == dict(index.get("categoryCounts") or {})
        and dict(sorted(binding_counts.items())) == dict(index.get("bindingCounts") or {})
        and actual_category_binding == dict(index.get("categoryBindingCounts") or {})
        and asset_count == int(assets["recordCount"])
        and dict(sorted(asset_resolution_counts.items())) == dict(index.get("assetResolutionCounts") or {})
        and dict(sorted(asset_resolution_instance_counts.items()))
        == dict(index.get("assetResolutionInstanceCounts") or {})
        and expected_asset_pending == int(index.get("assetPendingCount") or 0)
        and expected_instance_pending == int(index.get("assetPendingInstanceCount") or 0)
        and not asset_semantic_failures
    )
    add_check(
        checks,
        f"{map_id}_instances",
        passed,
        recordCount=record_count,
        shardCount=len(index.get("shards") or []),
        assetCount=asset_count,
        categoryCounts=dict(sorted(category_counts.items())),
        bindingCounts=dict(sorted(binding_counts.items())),
        assetResolutionCounts=dict(sorted(asset_resolution_counts.items())),
        assetResolutionInstanceCounts=dict(sorted(asset_resolution_instance_counts.items())),
        assetPendingCount=expected_asset_pending,
        assetPendingInstanceCount=expected_instance_pending,
        assetSemanticFailures=asset_semantic_failures,
        shardFailures=shard_failures,
    )
    return {
        "recordCount": record_count,
        "categoryCounts": dict(category_counts),
        "categoryBindingCounts": actual_category_binding,
        "sourceRecordCount": int(index.get("sourceRecordCount") or 0),
        "scanStatus": (index.get("scan") or {}).get("status"),
        "shards": list(index.get("shards") or []),
    }


def read_ref(root: Path, reference: Mapping[str, Any]) -> list[dict[str, Any]]:
    path = root / str(reference["path"])
    if sha256_file(path) != str(reference["sha256"]):
        raise ValueError(f"Dataset reference hash mismatch: {path}")
    return list(iter_jsonl_gzip(path))


def verify_effects(root: Path, map_id: str, index: Mapping[str, Any], checks: list[dict[str, Any]]) -> None:
    effects = read_ref(root, index["records"])
    anchors = read_ref(root, index["anchors"])
    prototypes = read_ref(root, index["prototypes"])
    members = read_ref(root, index["prototypeMembers"])
    for record in effects:
        validate_layer_record(record)
    for record in anchors:
        validate_layer_record(record)
    for record in prototypes:
        validate_layer_record(record)
    for record in members:
        validate_layer_record(record)
    validate_browser_safe_path_ids(
        {"effects": effects, "anchors": anchors, "prototypes": prototypes, "members": members},
        context=f"{map_id}.effects",
    )
    effect_ids = {str(row["effectId"]) for row in effects}
    member_ids = {str(row["prototypeMemberId"]) for row in members}
    prototype_ids = {str(row["prototypeId"]) for row in prototypes}
    references_closed = all(
        set(map(str, anchor.get("memberEffectIds") or [])).issubset(effect_ids)
        and set(map(str, anchor.get("prototypeMemberIds") or [])).issubset(member_ids)
        and int(anchor.get("resolvedMemberCount") or 0)
        == len(anchor.get("memberEffectIds") or []) + len(anchor.get("prototypeMemberIds") or [])
        for anchor in anchors
    )
    prototype_references_closed = all(
        str(member["prototypeId"]) in prototype_ids for member in members
    )
    expected_geometry = ["point"]
    if any(row.get("boundsUnity") is not None for row in effects):
        expected_geometry.insert(0, "bounds")
    passed = (
        len(effects) == int(index["recordCount"])
        and len(anchors) == int(index["anchorCount"])
        and len(prototypes) == int(index["prototypeCount"])
        and len(members) == int(index["prototypeMemberCount"])
        and references_closed
        and prototype_references_closed
        and list(index.get("geometryTypes") or []) == expected_geometry
    )
    add_check(
        checks,
        f"{map_id}_effects",
        passed,
        recordCount=len(effects),
        anchorCount=len(anchors),
        prototypeCount=len(prototypes),
        prototypeMemberCount=len(members),
        referencesClosed=references_closed,
        geometryTypes=index.get("geometryTypes"),
    )


def verify_small_layer(
    root: Path,
    map_id: str,
    layer: str,
    index: Mapping[str, Any],
    checks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records = read_ref(root, index["records"])
    for record in records:
        validate_layer_record(record)
    config_count = None
    if layer == "water":
        configs = read_ref(root, index["configs"])
        for record in configs:
            validate_browser_safe_path_ids(record, context=f"{map_id}.water.configs")
        config_count = len(configs)
    binding_counts = Counter(str(row.get("bindingStatus") or "unknown") for row in records)
    passed = (
        len(records) == int(index["recordCount"])
        and dict(sorted(binding_counts.items())) == dict(index.get("bindingCounts") or {})
    )
    if layer == "water":
        passed = (
            passed
            and all(record.get("bindingStatus") != "mapped" for record in records)
            and config_count == int(index.get("configCount") or 0)
        )
    add_check(
        checks,
        f"{map_id}_{layer}",
        passed,
        recordCount=len(records),
        scanStatus=(index.get("scan") or {}).get("status"),
        bindingCounts=dict(sorted(binding_counts.items())),
        **({"configCount": config_count} if config_count is not None else {}),
    )
    return records


def verify_roads(
    map_id: str,
    index: Mapping[str, Any],
    instance_summary: Mapping[str, Any],
    checks: list[dict[str, Any]],
) -> None:
    expected_count = int((instance_summary.get("categoryCounts") or {}).get("road", 0))
    expected_bindings = dict((instance_summary.get("categoryBindingCounts") or {}).get("road") or {})
    expected_coverage = int(expected_bindings.get("mapped", 0))
    expected_pending = int(expected_bindings.get("candidate", 0) + expected_bindings.get("unmapped", 0))
    source_shards = {str(row["sectorKey"]): row for row in instance_summary.get("shards") or []}
    expected_road_shards = [
        row for row in instance_summary.get("shards") or [] if int((row.get("categoryCounts") or {}).get("road", 0)) > 0
    ]
    road_shards = list(index.get("roadShards") or [])
    road_shard_failures = []
    for row in road_shards:
        source = source_shards.get(str(row.get("sectorKey")))
        if source is None:
            road_shard_failures.append({"sectorKey": row.get("sectorKey"), "reason": "missing_source_shard"})
            continue
        expected_road_count = int((source.get("categoryCounts") or {}).get("road", 0))
        if (
            expected_road_count <= 0
            or str(row.get("path")) != str(source.get("path"))
            or int(row.get("sourceShardRecordCount") or -1) != int(source.get("recordCount") or 0)
            or int(row.get("roadRecordCount") or 0) != expected_road_count
            or str(row.get("sourceShardSha256")) != str(source.get("sha256"))
        ):
            road_shard_failures.append({"sectorKey": row.get("sectorKey"), "reason": "source_mismatch"})
    known_sector_count = {"map01": 124, "map02": 162}.get(map_id)
    known_record_count = {"map01": 12521, "map02": 7884}.get(map_id)
    full_known_source = (
        instance_summary.get("scanStatus") == "complete"
        and int(instance_summary.get("sourceRecordCount") or 0)
        == {"map01": 141262, "map02": 361169}.get(map_id)
    )
    known_baseline_matches = (
        not full_known_source
        or (
            len(road_shards) == known_sector_count
            and int(index.get("recordCount") or 0) == known_record_count
        )
    )
    passed = (
        int(index["recordCount"]) == expected_count
        and dict(index.get("bindingCounts") or {}) == expected_bindings
        and int(index["coverageCount"]) == expected_coverage
        and int(index["pendingCount"]) == expected_pending
        and [str(row.get("sectorKey")) for row in road_shards]
        == [str(row.get("sectorKey")) for row in expected_road_shards]
        and list(index.get("sectorKeys") or []) == [str(row.get("sectorKey")) for row in road_shards]
        and sum(int(row.get("roadRecordCount") or 0) for row in road_shards) == expected_count
        and not road_shard_failures
        and "records" not in index
        and index.get("recordEncoding") == "view-reference"
        and known_baseline_matches
    )
    add_check(
        checks,
        f"{map_id}_roads_view",
        passed,
        recordCount=int(index["recordCount"]),
        bindingCounts=index.get("bindingCounts"),
        coverageCount=int(index["coverageCount"]),
        pendingCount=int(index["pendingCount"]),
        roadSectorCount=len(road_shards),
        roadShardRecordCount=sum(int(row.get("roadRecordCount") or 0) for row in road_shards),
        knownBaselineMatches=known_baseline_matches,
        roadShardFailures=road_shard_failures,
    )


def verify_samples(root: Path, manifest: Mapping[str, Any], checks: list[dict[str, Any]]) -> None:
    samples = load_json(root / str(manifest["validation"]["samplesPath"]))
    bootstrap_mode = samples.get("mode") == "bootstrap"
    bootstrap_optional_labels = {
        "map01 water candidate",
        "map01 effect anchor",
        "map02 water candidate",
        "map02 effect anchor",
        "cross-sector water geometry",
        "full_system_by_anchor",
    }
    failures = []
    for sample in samples.get("samples") or []:
        if not sample.get("available"):
            if not bootstrap_mode or sample.get("label") not in bootstrap_optional_labels:
                failures.append({"label": sample.get("label"), "reason": "unavailable"})
            continue
        record = sample.get("record") or {}
        validate_layer_record(record)
        if sha256_json(record) != sample.get("recordSha256"):
            failures.append({"label": sample.get("label"), "reason": "sha256"})
    selection = samples.get("selectionResolution") or {}
    if selection.get("policy") != "full_system_by_anchor":
        failures.append({"label": "selectionResolution", "reason": "policy"})
    add_check(
        checks,
        "validation_samples",
        not failures,
        sampleCount=len(samples.get("samples") or []),
        mode=samples.get("mode", "legacy_formal"),
        failures=failures,
    )


def audit(root: Path, verify_source_hashes: bool) -> dict[str, Any]:
    resolved = root.expanduser().resolve()
    manifest_path = resolved / "region_map_layer_manifest.json"
    manifest = load_json(manifest_path)
    validate_manifest(manifest)
    checks: list[dict[str, Any]] = []
    add_check(
        checks,
        "manifest_contract",
        True,
        manifestSha256=sha256_file(manifest_path),
        datasetFingerprint=manifest.get("datasetFingerprint"),
    )
    for schema_path in manifest.get("schemas") or []:
        schema = load_json(resolved / str(schema_path))
        add_check(
            checks,
            f"schema_parse:{schema_path}",
            schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema",
            title=schema.get("title"),
        )
    verify_artifact_inventory(resolved, manifest, checks)
    verify_source_evidence(manifest, checks, verify_source_hashes)
    instance_summaries = {}
    for map_id in ("map01", "map02"):
        layers = manifest["maps"][map_id]["layers"]
        instance_index = load_json(resolved / str(layers["instances"]["indexPath"]))
        validate_dataset_index(instance_index)
        instance_summaries[map_id] = verify_instances(resolved, map_id, instance_index, checks)
        effects_index = load_json(resolved / str(layers["effects"]["indexPath"]))
        validate_dataset_index(effects_index)
        verify_effects(resolved, map_id, effects_index, checks)
        water_index = load_json(resolved / str(layers["water"]["indexPath"]))
        validate_dataset_index(water_index)
        verify_small_layer(resolved, map_id, "water", water_index, checks)
        lights_index = load_json(resolved / str(layers["lights"]["indexPath"]))
        validate_dataset_index(lights_index)
        verify_small_layer(resolved, map_id, "lights", lights_index, checks)
        roads_index = load_json(resolved / str(layers["roads"]["indexPath"]))
        validate_dataset_index(roads_index)
        verify_roads(map_id, roads_index, instance_summaries[map_id], checks)
    verify_samples(resolved, manifest, checks)
    validation = load_json(resolved / str(manifest["validation"]["path"]))
    add_check(
        checks,
        "builder_validation",
        validation.get("status") == "passed" and validation.get("datasetFingerprint") == manifest.get("datasetFingerprint"),
        status=validation.get("status"),
    )
    passed = all(check["passed"] for check in checks)
    files = [path for path in resolved.rglob("*") if path.is_file()]
    return {
        "format": "EndfieldMapLayerAudit/1",
        "status": "passed" if passed else "failed",
        "outputRoot": str(resolved),
        "datasetFingerprint": manifest.get("datasetFingerprint"),
        "fileCount": len(files),
        "totalBytes": sum(path.stat().st_size for path in files),
        "checks": checks,
    }


def main() -> int:
    args = arguments()
    report = audit(args.output_root, args.verify_source_hashes)
    if args.report:
        report_path = args.report.expanduser().resolve()
        if report_path.exists():
            raise FileExistsError(report_path)
        write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
