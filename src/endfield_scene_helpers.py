"""Upgrade cached extraction helpers without rerunning map preparation."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from endfield_dependency_bootstrap import helper_source_identity
from endfield_scene_material_contract import require_object


@dataclass(frozen=True)
class SceneHelpers:
    scanner: Path
    probe: Path


def _verified_file(value: object, parent: Path) -> Path:
    row = require_object(value, "helper file descriptor")
    raw_path = row.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("Helper descriptor requires a nonempty path")
    path = Path(raw_path)
    path = (path if path.is_absolute() else parent / path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Cached extraction helper is missing: {path}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest().upper()
    if row.get("bytes") != path.stat().st_size or row.get("sha256") != digest:
        raise ValueError(f"Cached extraction helper failed integrity validation: {path}")
    return path


def verified_helpers(result: Mapping[str, object], source_identity: str,
                     parent: Path) -> SceneHelpers | None:
    """A matching apphost alone cannot prove the managed helper was upgraded."""
    if result.get("sourceIdentity") != source_identity:
        return None
    outputs = require_object(result.get("outputs"), "helper outputs")
    paths: dict[str, Path] = {}
    for name in ("bundleScanner", "sceneProbe"):
        row = require_object(outputs.get(name), f"helper {name}")
        if row.get("assembly") is None or row.get("runtimeFiles") is None:
            return None
        executable = _verified_file(row, parent)
        assembly = _verified_file(row["assembly"], parent)
        if assembly != executable.with_suffix(".dll"):
            raise ValueError(f"Helper managed assembly does not match its executable: {name}")
        runtime_files = row["runtimeFiles"]
        if not isinstance(runtime_files, list) or not runtime_files:
            raise ValueError(f"Helper runtime file inventory is invalid: {name}")
        verified: set[Path] = set()
        for descriptor in runtime_files:
            dependency = _verified_file(descriptor, executable.parent)
            if not dependency.is_relative_to(executable.parent) or dependency in verified:
                raise ValueError(f"Helper runtime inventory has an escaped or repeated path: {dependency}")
            verified.add(dependency)
        actual = {path.resolve() for path in executable.parent.rglob("*") if path.is_file()}
        if verified != actual or not {executable, assembly}.issubset(verified):
            raise ValueError(f"Helper runtime file inventory differs from its receipt: {executable.parent}")
        paths[name] = executable
    return SceneHelpers(paths["bundleScanner"], paths["sceneProbe"])


def ensure_current_scene_helpers(runtime_path: Path, cache_root: Path,
                                 check_cancelled: Callable[[], None],
                                 progress: Callable[[float, str], None]) -> SceneHelpers:
    """Reuse the pinned bootstrap and source identity for on-demand upgrades."""
    from endfield_scene_export import run_scene_process

    source_root = Path(__file__).resolve().parent
    identity = helper_source_identity(source_root.parent)
    state_path = runtime_path.parent / "first_run_state.json"
    state = require_object(json.loads(state_path.read_text(encoding="utf-8-sig")), str(state_path))
    steps = require_object(state.get("steps"), "first-run steps")
    step = require_object(steps.get("helper_dependencies"), "helper dependency step")
    prior = require_object(step.get("outputs"), "prepared helper result")
    prepared = verified_helpers(prior, identity, runtime_path.parent)
    if prepared is not None:
        return prepared
    # Keep native DLL search paths short; receipts still verify the full source hash.
    root = cache_root / "helpers" / identity[:16]
    receipt = root / "current.json"
    if receipt.is_file():
        result = require_object(json.loads(receipt.read_text(encoding="utf-8")), str(receipt))
        cached = verified_helpers(result, identity, root)
        if cached is None:
            raise ValueError(f"Helper cache has an inconsistent source identity: {receipt}")
        return cached
    check_cancelled()
    work = root / uuid4().hex
    work.mkdir(parents=True)
    progress(0.0, "Updating extraction helpers for the current source; existing map caches are retained")
    command = [sys.executable, str(source_root / "endfield_dependency_bootstrap.py"),
               "--workspace", str(cache_root / "helper_dependencies"), "--log-dir", str(work / "log"),
               "--output-root", str(work / "bin")]
    environment = {**os.environ, "TEMP": str(work), "TMP": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
    run_scene_process(command, work / "bootstrap.log", environment, check_cancelled)
    report = work / "log" / "dependency_bootstrap_report.json"
    result = require_object(json.loads(report.read_text(encoding="utf-8")), str(report))
    current = verified_helpers(result, identity, work)
    if current is None:
        raise ValueError(f"Helper source changed during compilation; retry the export: {report}")
    temporary = root / f"current.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(temporary, receipt)
    progress(1.0, "Updated extraction helpers verified")
    return current
