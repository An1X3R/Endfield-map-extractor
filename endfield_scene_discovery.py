"""Observe existing helpers and resume independently verified preparation stages."""
from __future__ import annotations

import hashlib
import codecs
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
from endfield_scene_resources import describe_file, read_json, validate_scan, write_index
from extractor_core.resources import extract_candidate_bundles, read_bundle_closure_names_by_map


Progress = Callable[[float, str, dict[str, object]], None]
CHECKPOINT_FORMAT = "EndfieldPreparationCheckpoint/1"


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


def discovery(run_root: Path, log_root: Path, scan_index: Path, database: Path, vfs: Path,
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
                            "database": database_hash, "containers": ["assets/"], "contract": 1})
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

        observe_helper([str(scanner), "--input", str(scan_index), "--output", str(providers),
                        "--cab-output", str(cab), "--contains", "assets/", "--progress-every", "1000",
                        "--performance-report", str(performance)], log, scan_work / "temp", check_cancelled, scan_progress)
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
    names, closure = read_bundle_closure_names_by_map(providers, cab)
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
            "coverage": "map01_map02_container_candidates_plus_cab_external_closure",
            "providerCoverage": "all_asset_containers", "scanIndex": describe_file(scan_index),
            "scannerResults": scan["scannerResults"], "scannerConsole": scan["scannerConsole"],
            "cabInventory": scan["cabInventory"], "cabConsole": scan["scannerConsole"],
            "closureStatus": "expanded_from_cab_inventory", "closure": closure, "maps": maps,
            "performance": {"elapsedSeconds": time.perf_counter() - started, "reusedStages": reused,
                            "scanReport": scan["performance"]},
            "limitations": ["Helper I/O counters measure process transfers, not physical disk traffic.",
                            "Scene parsing retains the existing dependency-safe memory model."]}
