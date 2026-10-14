"""Upgrade cached extraction helpers without rerunning map preparation."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from endfield_dependency_bootstrap import helper_source_identity
from endfield_scene_material_contract import require_object


SETUP_SCRIPT = Path(__file__).resolve().parent.parent / "setup_development_environment.ps1"


@dataclass(frozen=True)
class SceneHelpers:
    scanner: Path
    probe: Path


@dataclass(frozen=True)
class CachedSceneHelperReport:
    result: Mapping[str, object]
    helper_root: Path
    receipt_path: Path


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


def _helper_identity(source_root: Path) -> str:
    return helper_source_identity(source_root.parent)


def _absolute_helper_report(result: Mapping[str, object], helpers: SceneHelpers,
                            parent: Path) -> Mapping[str, object]:
    """Keep portable receipts relative on disk while exposing runnable paths."""
    outputs = require_object(result.get("outputs"), "helper outputs")
    resolved_outputs: dict[str, object] = {}
    for name, executable in (("bundleScanner", helpers.scanner), ("sceneProbe", helpers.probe)):
        row = require_object(outputs[name], f"helper {name}")
        assembly = require_object(row["assembly"], f"helper {name} assembly")
        resolved_outputs[name] = {
            **row,
            "path": str(executable),
            "assembly": {**assembly, "path": str(_verified_file(assembly, parent))},
        }
    return {**result, "outputs": resolved_outputs}


def load_cached_scene_helper_report(cache_root: Path, source_root: Path) -> CachedSceneHelperReport | None:
    """Find a verified receipt in the configured cache or project-local helper install."""
    identity = _helper_identity(source_root)
    candidate_roots = tuple(dict.fromkeys((
        cache_root.expanduser().resolve(),
        (source_root.resolve().parent / ".tools" / "scene").resolve(),
    )))
    for cache in candidate_roots:
        root = cache / "helpers" / identity[:16]
        receipt = root / "current.json"
        if not receipt.is_file():
            continue
        result = require_object(json.loads(receipt.read_text(encoding="utf-8-sig")), str(receipt))
        cached = verified_helpers(result, identity, root)
        if cached is None:
            raise ValueError(f"Prepared helper receipt has a different source identity: {receipt}")
        return CachedSceneHelperReport(_absolute_helper_report(result, cached, root), root, receipt)
    return None


def require_prepared_scene_helpers(runtime_path: Path, cache_root: Path) -> SceneHelpers:
    """Resolve only hash-verified helpers from this run or the shared cache."""
    source_root = Path(__file__).resolve().parent
    identity = _helper_identity(source_root)
    state_path = runtime_path.parent / "first_run_state.json"
    if state_path.is_file():
        state = require_object(json.loads(state_path.read_text(encoding="utf-8-sig")), str(state_path))
        steps = require_object(state.get("steps"), "first-run steps")
        step = steps.get("helper_dependencies")
        if isinstance(step, Mapping) and step.get("status") == "completed":
            prior = require_object(step.get("outputs"), "prepared helper result")
            try:
                prepared = verified_helpers(prior, identity, runtime_path.parent)
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ValueError(f"First-run helper receipt failed integrity validation: {state_path}: {error}") from error
            if prepared is not None:
                return prepared
    root = cache_root / "helpers" / identity[:16]
    receipt = root / "current.json"
    result = load_cached_scene_helper_report(cache_root, source_root)
    if result is not None:
        cached = verified_helpers(result.result, identity, result.helper_root)
        if cached is None:
            raise ValueError(f"Prepared helper cache has an inconsistent source identity: {result.receipt_path}")
        return cached
    setup = (
        f'powershell -ExecutionPolicy Bypass -File "{SETUP_SCRIPT}" '
        f'-PrepareSceneHelpers -HelperCacheRoot "{cache_root.resolve()}"'
    )
    raise FileNotFoundError(
        f"No hash-verified SceneProbe/BundleScanner build exists for source identity {identity}. Run: {setup}"
    )


def bootstrap_scene_helpers(cache_root: Path) -> SceneHelpers:
    """Explicitly fetch and build pinned helpers, then publish a shared verified receipt."""
    source_root = Path(__file__).resolve().parent
    identity = _helper_identity(source_root)
    root = cache_root / "helpers" / identity[:16]
    receipt = root / "current.json"
    existing = load_cached_scene_helper_report(cache_root, source_root)
    if existing is not None:
        prepared = verified_helpers(existing.result, identity, existing.helper_root)
        if prepared is None:
            raise ValueError(f"Prepared helper cache has an inconsistent source identity: {existing.receipt_path}")
        return prepared
    root.mkdir(parents=True, exist_ok=True)
    work = root / uuid4().hex
    work.mkdir(parents=True, exist_ok=False)
    dependency_workspace = cache_root / "helper_dependencies"
    command = [sys.executable, str(source_root / "endfield_dependency_bootstrap.py"),
               "--workspace", str(dependency_workspace), "--log-dir", str(work / "log"),
               "--output-root", str(work / "bin")]
    environment = {**os.environ, "TEMP": str(work), "TMP": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.run(command, cwd=source_root.parent, env=environment, text=True,
                             encoding="utf-8", errors="replace", capture_output=True, check=False)
    bootstrap_log = work / "bootstrap.log"
    bootstrap_log.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_log.write_text(process.stdout + process.stderr, encoding="utf-8", newline="\n")
    report = work / "log" / "dependency_bootstrap_report.json"
    if process.returncode != 0 or not report.is_file():
        raise RuntimeError(
            f"Pinned helper bootstrap failed with exit code {process.returncode}; inspect {bootstrap_log}"
        )
    result = require_object(json.loads(report.read_text(encoding="utf-8")), str(report))
    current = verified_helpers(result, identity, work)
    if current is None:
        raise ValueError(f"Helper source changed during compilation; retry the export: {report}")
    if receipt.exists():
        raise FileExistsError(f"Refusing to replace an existing helper receipt: {receipt}")
    temporary = root / f"current.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, receipt)
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicitly prepare pinned scene helpers in a selected cache root.")
    parser.add_argument("--bootstrap-cache-root", type=Path, required=True)
    args = parser.parse_args()
    helpers = bootstrap_scene_helpers(args.bootstrap_cache_root.expanduser().resolve())
    print(json.dumps({
        "format": "EndfieldPreparedSceneHelpers/1",
        "sourceIdentity": _helper_identity(Path(__file__).resolve().parent),
        "bundleScanner": str(helpers.scanner),
        "sceneProbe": str(helpers.probe),
        "cacheRoot": str(args.bootstrap_cache_root.expanduser().resolve()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
