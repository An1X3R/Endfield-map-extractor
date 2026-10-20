"""Observe existing helpers and resume independently verified preparation stages."""
from __future__ import annotations

import hashlib
import codecs
from dataclasses import asdict
import inspect
import json
import math
import os
from pathlib import Path
import time
from collections.abc import Callable
from typing import cast
from uuid import uuid4

from endfield_scene_export import run_scene_process
from endfield_scene_resources import PACKAGE_PROVIDER_COVERAGE, describe_file, read_json, validate_scan, write_index
from endfield_scene_scanner import run_scanner_shards, scan_index_rows, scanner_capacity
from extractor_core.effects import GROUP_HASH as EFFECT_GROUP_HASH, read_dynamic_scene_effects
from extractor_core.resources import (
    extract_candidate_bundles,
    read_bundle_closure_names,
    read_bundle_provider_names_for_paths,
)


Progress = Callable[[float, str, dict[str, object]], None]
CHECKPOINT_FORMAT = "EndfieldPreparationCheckpoint/1"
SCENE_SCAN_CONTAINERS = ("assets/", "packages/")


def identity(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tool_identity(executable: Path) -> str:
    """Include managed code and dependencies; an apphost EXE alone is not a version."""
    if not executable.is_file():
        raise FileNotFoundError(executable)
    files = {executable, *(path for path in executable.parent.rglob("*")
                          if path.is_file() and path.suffix.casefold() in (".dll", ".json"))}
    return identity([(str(path.relative_to(executable.parent)), describe_file(path)["sha256"])
                     for path in sorted(files)])


def read_asset_bundle_candidate_names_by_map(results: Path) -> dict[str, list[str]]:
    """Group scene roots by Map01/Map02 evidence from assets containers only."""
    source = results.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Bundle scanner results are missing: {source}")
    names: dict[str, set[str]] = {"map01": set(), "map02": set()}
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Bundle scanner result is invalid JSONL at line {line_number}: {source}") from error
            if not isinstance(value, dict):
                raise ValueError(f"Bundle scanner result must be an object at line {line_number}: {source}")
            row = cast(dict[str, object], value)
            logical_name = row.get("logical_name")
            if row.get("kind") != "match" or not isinstance(logical_name, str):
                continue
            containers = row.get("containers")
            if not isinstance(containers, list) or any(not isinstance(item, str) for item in containers):
                raise ValueError(f"Bundle scanner match requires string containers at line {line_number}: {source}")
            asset_containers: list[str] = []
            for item in containers:
                normalized = item.casefold().replace("\\", "/")
                if normalized.startswith("assets/"):
                    asset_containers.append(normalized)
            for map_id in names:
                if any(f"/{map_id}/" in item or f"{map_id}_" in item for item in asset_containers):
                    names[map_id].add(logical_name.casefold())
    return {map_id: sorted(values) for map_id, values in names.items()}


def helper_progress(line: str) -> dict[str, object] | None:
    if not line.startswith("{") or '"EndfieldHelperProgress/1"' not in line:
        return None
    value = json.loads(line)
    if not isinstance(value, dict) or value.get("format") != "EndfieldHelperProgress/1":
        return None
    if not isinstance(value.get("phase"), str) or not value["phase"]:
        raise ValueError("Helper telemetry requires a nonempty phase")
    for name in ("current", "total", "peakWorkingSetBytes", "readBytes", "writeBytes"):
        if name in value and (type(value[name]) is not int or value[name] < 0):
            raise ValueError(f"Helper telemetry requires a nonnegative integer: {name}={value[name]}")
    if "current" in value and "total" in value and value["current"] > value["total"]:
        raise ValueError(f"Helper telemetry current exceeds total: {value}")
    elapsed = value.get("elapsedSeconds")
    if elapsed is not None and (type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0):
        raise ValueError(f"Invalid helper elapsed time: {elapsed}")
    return cast(dict[str, object], value)


def observe_helper(command: list[str], log: Path, temporary: Path,
                   check_cancelled: Callable[[], None], emit: Callable[[dict[str, object]], None]) -> None:
    """Relay structured stdout without changing the existing process/cancellation runner."""
    position = 0
    fragment = ""
    decoder = codecs.getincrementaldecoder("utf-8-sig")()

    def drain() -> None:
        nonlocal position, fragment
        if not log.is_file():
            return
        with log.open("rb") as stream:
            stream.seek(position)
            block = decoder.decode(stream.read())
            position = stream.tell()
        lines = (fragment + block).split("\n")
        fragment = lines.pop()
        for line in lines:
            row = helper_progress(line.strip())
            if row is not None:
                emit(row)

    def check() -> None:
        drain()
        check_cancelled()

    temporary.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        run_scene_process(command, log, environment, check)
    finally:
        drain()


def read_checkpoint(path: Path, source_identity: str,
                    check_cancelled: Callable[[], None]) -> tuple[dict[str, object] | None, str]:
    if not path.is_file():
        return None, "not_prepared"
    data = read_json(path)
    if data.get("format") != CHECKPOINT_FORMAT or data.get("sourceIdentity") != source_identity:
        return None, "source_or_tool_changed"
    rows = data.get("artifacts")
    outputs = data.get("outputs")
    if not isinstance(rows, list) or not rows or not isinstance(outputs, dict):
        raise ValueError(f"Invalid preparation checkpoint: {path}")
    for row in rows:
        check_cancelled()
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ValueError(f"Invalid checkpoint artifact: {path}")
        artifact = Path(row["path"]).resolve()
        if not artifact.is_relative_to(path.parent.resolve()):
            raise ValueError(f"Checkpoint artifact escapes its stage: {artifact}")
        if not artifact.is_file() or describe_file(artifact) != row:
            return None, f"output_changed:{artifact}"
    for name in ("scannerResults", "cabInventory", "manifest", "performance"):
        if name in outputs and outputs[name] not in rows:
            raise ValueError(f"Checkpoint output is not a verified artifact: {name} in {path}")
    if "root" in outputs:
        if not isinstance(outputs["root"], str):
            raise ValueError(f"Invalid checkpoint bundle root: {path}")
        root = Path(outputs["root"]).resolve()
        if not root.is_relative_to(path.parent.resolve()) or not root.is_dir():
            raise ValueError(f"Checkpoint bundle root escapes or is missing: {root}")
        actual = {str(file.resolve()) for file in root.rglob("*") if file.is_file()}
        expected = {str(Path(row["path"]).resolve()) for row in rows}
        if actual != expected:
            return None, f"output_inventory_changed:{root}"
    return cast(dict[str, object], outputs), "verified"


def complete_checkpoint(path: Path, source_identity: str, outputs: dict[str, object],
                        files: tuple[Path, ...], check_cancelled: Callable[[], None]) -> None:
    artifacts = []
    for file in files:
        check_cancelled()
        if not file.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError(f"Checkpoint output escapes its stage: {file}")
        artifacts.append(describe_file(file.resolve()))
    write_index(path, {"format": CHECKPOINT_FORMAT, "sourceIdentity": source_identity,
                       "outputs": outputs, "artifacts": artifacts})


def stage_work(root: Path, name: str) -> tuple[Path, Path]:
    stage = root / name
    stage.mkdir(parents=True, exist_ok=True)
    return stage / "checkpoint.json", stage / uuid4().hex


def effect_candidates(root: Path, vfs: Path, string_path_hash: Path, providers: Path,
                      check_cancelled: Callable[[], None]) -> tuple[dict[str, list[str]], dict[str, object]]:
    """Bind source-qualified scene effects to exact providers without inventing missing assets."""
    if root.resolve().is_relative_to(vfs.resolve()):
        raise ValueError("Effect discovery checkpoints cannot be inside the game VFS")
    source_identity = identity({
        "catalog": describe_file(vfs / EFFECT_GROUP_HASH / f"{EFFECT_GROUP_HASH}.blc"),
        "paths": describe_file(string_path_hash),
        "decoder": describe_file(Path(inspect.getfile(read_dynamic_scene_effects))),
        "contract": 1,
    })
    checkpoint, work = stage_work(root, "dynamic_effects")
    cached, reuse_reason = read_checkpoint(checkpoint, source_identity, check_cancelled)
    if cached is None:
        inventories = read_dynamic_scene_effects(vfs, string_path_hash, check_cancelled)
        work.mkdir()
        inventory_path = work / "inventory.json"
        write_index(inventory_path, {
            "format": "EndfieldDynamicEffectInventory/1",
            "scope": "DynamicStreaming PC Scene fb_main effects; excludes other runtime effect sources",
            "maps": {map_id: asdict(inventory) for map_id, inventory in inventories.items()},
        })
        cached = {"manifest": describe_file(inventory_path)}
        complete_checkpoint(checkpoint, source_identity, cached, (inventory_path,), check_cancelled)
    manifest = cast(dict[str, object], cached["manifest"])
    inventory = read_json(Path(str(manifest["path"])))
    if inventory.get("format") != "EndfieldDynamicEffectInventory/1":
        raise ValueError("Dynamic effect inventory has an unsupported format")
    maps = inventory.get("maps")
    if not isinstance(maps, dict) or set(maps) != {"map01", "map02"}:
        raise ValueError("Dynamic effect inventory requires Map01 and Map02 records")
    requested: dict[str, list[str]] = {}
    summaries: dict[str, dict[str, object]] = {}
    for map_id, entry in maps.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Dynamic effect map must be an object: map={map_id}")
        paths, records, sources = (entry.get(key) for key in ("prefab_paths", "records", "sources"))
        if not isinstance(paths, list) or any(not isinstance(path, str) or not path for path in paths):
            raise ValueError(f"Dynamic effect prefab paths must be nonempty strings: map={map_id}")
        if not isinstance(records, list) or not isinstance(sources, list):
            raise ValueError(f"Dynamic effect source records must be arrays: map={map_id}")
        requested[map_id] = paths
        summaries[map_id] = {"sourceRecordCount": len(records), "sourceFileCount": len(sources),
                             "prefabPathCount": len(paths)}
    roots, issues = read_bundle_provider_names_for_paths(providers, requested)
    for map_id in requested:
        summaries[map_id].update({
            "providerRootBundleCount": len(roots[map_id]),
            "resolvedPrefabPathCount": len(requested[map_id]) - len(issues[map_id]),
            "unresolvedProviders": [asdict(issue) for issue in issues[map_id]],
        })
    return roots, {
        "format": "EndfieldDynamicEffectDiscovery/1", "inventory": manifest,
        "status": "partial" if any(issues.values()) else "providers_resolved",
        "scope": inventory["scope"], "maps": summaries, "inventoryReuse": reuse_reason,
        "reconstructionStatus": "not_implemented",
    }


def discovery(run_root: Path, log_root: Path, scan_index: Path, database: Path, vfs: Path,
              string_path_hash: Path,
              scanner: Path, probe: Path, check_cancelled: Callable[[], None], progress: Progress) -> dict[str, object]:
    """Reuse only complete source/tool-matched scans, bundle sets and per-map probes."""
    root = run_root / "scene_discovery" / "checkpoints_v21"
    root.mkdir(parents=True, exist_ok=True)
    if root.resolve().is_relative_to(vfs.resolve()):
        raise ValueError("Scene preparation checkpoints cannot be inside the game VFS")
    logs = log_root / "scene_discovery"
    logs.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    database_hash = describe_file(database)["sha256"]
    scan_source = identity({"index": describe_file(scan_index)["sha256"], "tool": tool_identity(scanner),
                            "database": database_hash, "containers": list(SCENE_SCAN_CONTAINERS), "contract": 2})
    scan_state, scan_work = stage_work(root, "scan")
    scan, reason = read_checkpoint(scan_state, scan_source, check_cancelled)
    reused: list[str] = []
    if scan is None:
        progress(0.05, "Preparing combined provider and CAB scan", {"checkpoint": "scan", "reuse": reason})
        scan_work.mkdir()
        providers, cab = scan_work / "bundle_candidates.jsonl", scan_work / "bundle_cab_inventory.jsonl"
        log = logs / f"combined_scan_{uuid4().hex}.log"
        performance = scan_work / "performance.json"

        def scan_progress(row: dict[str, object]) -> None:
            total = cast(int, row.get("total", 0))
            done = cast(int, row.get("current", 0))
            progress(0.05 + 0.35 * (done / total if total else 0),
                     f"Scanning providers and dependencies: {done}/{total or '?'}", {"helperProgress": row})

        rows = scan_index_rows(scan_index, check_cancelled)
        run_scanner_shards(scanner, scan_index, scan_work, log, 0, rows, scanner_capacity(rows), SCENE_SCAN_CONTAINERS,
                           check_cancelled, scan_progress, observe_helper)
        validate_scan(providers)
        validate_scan(cab)
        scan = {"scannerResults": describe_file(providers), "cabInventory": describe_file(cab),
                "scannerConsole": describe_file(log), "performance": describe_file(performance)}
        complete_checkpoint(scan_state, scan_source, scan, (providers, cab, performance), check_cancelled)
    else:
        reused.append("scan")
        progress(0.4, "Reused verified provider and CAB scan", {"checkpoint": "scan", "reuse": reason})
    providers = Path(str(cast(dict[str, object], scan["scannerResults"])["path"]))
    cab = Path(str(cast(dict[str, object], scan["cabInventory"])["path"]))
    progress(0.4, "Resolving dynamic effect prefab providers", {"checkpoint": "dynamic_effects"})
    effect_roots, effects = effect_candidates(root, vfs, string_path_hash, providers, check_cancelled)
    roots = read_asset_bundle_candidate_names_by_map(providers)
    roots = {map_id: sorted(set(values) | set(effect_roots[map_id])) for map_id, values in roots.items()}
    names, closure = read_bundle_closure_names(roots, cab)
    probe_tool = tool_identity(probe)
    extractor_version = identity(inspect.getsource(extract_candidate_bundles))
    maps: dict[str, object] = {}
    for number, map_id in enumerate(("map01", "map02")):
        check_cancelled()
        base = 0.4 + number * 0.28
        bundle_source = identity({"database": database_hash, "names": names[map_id], "extractor": extractor_version})
        bundle_state, bundle_work = stage_work(root, map_id + "_bundles")
        bundle, reason = read_checkpoint(bundle_state, bundle_source, check_cancelled)
        if bundle is None:
            progress(base, f"Preparing exact bundles for {map_id}", {"checkpoint": map_id + "_bundles", "reuse": reason})

            def extraction_progress(phase: str, value: float, message: str, details: object) -> None:
                progress(base + value * 0.07, message, {"sourcePhase": phase, "counts": details})

            def cancelled() -> bool:
                check_cancelled()
                return False

            meta = extract_candidate_bundles(database, vfs, names[map_id], bundle_work,
                                             progress=extraction_progress, cancelled=cancelled)
            bundle = {"root": str(bundle_work), "meta": meta}
            files = tuple(sorted(path for path in bundle_work.rglob("*") if path.is_file()))
            if files:
                complete_checkpoint(bundle_state, bundle_source, bundle, files, check_cancelled)
        else:
            reused.append(map_id + "_bundles")
        manifest: dict[str, object] | None = None
        if names[map_id]:
            probe_source = identity({"bundles": bundle_source, "tool": probe_tool, "includeUnreferenced": True})
            probe_state, probe_work = stage_work(root, map_id + "_probe")
            parsed, reason = read_checkpoint(probe_state, probe_source, check_cancelled)
            if parsed is None:
                progress(base + 0.07, f"Parsing scene assets for {map_id}", {"checkpoint": map_id + "_probe", "reuse": reason})
                probe_work.mkdir()
                output = probe_work / "scene_probe"
                log = logs / f"{map_id}_probe_{uuid4().hex}.log"
                performance = probe_work / "performance.json"

                def probe_progress(row: dict[str, object]) -> None:
                    phase = str(row["phase"])
                    progress(base + 0.08, f"{map_id} {phase}: {row.get('current', '?')}/{row.get('total', '?')}",
                             {"mapId": map_id, "helperProgress": row})

                observe_helper([str(probe), str(bundle["root"]), str(output), "--include-unreferenced",
                                "--performance-report", str(performance)], log, probe_work / "temp", check_cancelled, probe_progress)
                scene = output / "scene_manifest.json"
                if read_json(scene).get("Format") != "EndfieldSceneProbe/1":
                    raise ValueError(f"Invalid SceneProbe output: {scene}")
                parsed = {"manifest": describe_file(scene), "console": describe_file(log),
                          "performance": describe_file(performance)}
                files = tuple(sorted(path for path in output.rglob("*") if path.is_file())) + (performance,)
                complete_checkpoint(probe_state, probe_source, parsed, files, check_cancelled)
            else:
                reused.append(map_id + "_probe")
            manifest = cast(dict[str, object], parsed["manifest"])
        maps[map_id] = {"bundleCount": len(names[map_id]), "candidateBundles": bundle, "sceneManifest": manifest}
        progress(base + 0.28, f"Verified scene preparation for {map_id}", {"reusedStages": list(reused)})
    return {"status": "completed", "checkpointVersion": 1,
            "coverage": "map01_map02_container_and_dynamic_effect_candidates_plus_cab_external_closure",
            "providerCoverage": PACKAGE_PROVIDER_COVERAGE, "scanIndex": describe_file(scan_index),
            "scannerResults": scan["scannerResults"], "scannerConsole": scan["scannerConsole"],
            "cabInventory": scan["cabInventory"], "cabConsole": scan["scannerConsole"],
            "closureStatus": "expanded_from_cab_inventory", "closure": closure, "maps": maps,
            "dynamicEffects": {**effects, "preparationStatus": "matched_provider_bundles_prepared"},
            "performance": {"elapsedSeconds": time.perf_counter() - started, "reusedStages": reused,
                            "scanReport": scan["performance"]},
            "limitations": ["Helper I/O counters measure process transfers, not physical disk traffic.",
                            "Scene parsing retains the existing dependency-safe memory model."]}
