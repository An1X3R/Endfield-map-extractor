"""Export a portable, hash-verified scene helper install without downloading."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import shutil

from endfield_scene_helpers import load_cached_scene_helper_report, verified_helpers
from endfield_scene_material_contract import require_object


REPORT_FIELDS = ("format", "status", "sourceIdentity", "completedAt", "commit", "sdk", "patches", "upstreamFiles")


def _relocate_descriptor(descriptor: Mapping[str, object], relative_path: str) -> dict[str, object]:
    return {"path": relative_path, "bytes": descriptor["bytes"], "sha256": descriptor["sha256"]}


def _copy_helper(executable: Path, source_output: Mapping[str, object],
                 destination: Path, receipt_root: Path) -> Mapping[str, object]:
    """Copy the complete verified runtime, preserving its internal layout."""
    if destination.resolve().is_relative_to(executable.parent):
        raise ValueError(f"Scene tool bundle overlaps its source runtime: {destination}")
    copied_executable = destination / executable.name
    assembly = require_object(source_output["assembly"], "helper assembly")
    runtime_files = source_output["runtimeFiles"]
    if not isinstance(runtime_files, list):
        raise ValueError(f"Verified helper runtime inventory changed: {executable}")
    copied_runtime: list[dict[str, object]] = []
    for value in runtime_files:
        row = require_object(value, "helper runtime file")
        source_path = Path(str(row["path"]))
        source_path = (source_path if source_path.is_absolute() else executable.parent / source_path).resolve()
        relative_path = source_path.relative_to(executable.parent).as_posix()
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target)
        copied_runtime.append(_relocate_descriptor(row, relative_path))
    for source_path in (executable, executable.with_suffix(".dll")):
        target = destination / source_path.name
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target)
    return {
        **_relocate_descriptor(source_output, copied_executable.relative_to(receipt_root).as_posix()),
        "assembly": _relocate_descriptor(assembly, copied_executable.with_suffix(".dll").relative_to(receipt_root).as_posix()),
        "runtimeFiles": copied_runtime,
    }


def export_scene_tool_bundle(cache_root: Path, destination: Path) -> Path:
    """Publish a relocatable .tools/scene tree; existing destinations are never replaced."""
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to replace an existing scene tool bundle: {destination}")
    source_root = Path(__file__).resolve().parent
    cached = load_cached_scene_helper_report(cache_root, source_root)
    if cached is None:
        raise FileNotFoundError(f"No hash-verified scene helper install is available in {cache_root} or this project.")
    identity = cached.result.get("sourceIdentity")
    if not isinstance(identity, str) or len(identity) != 64:
        raise ValueError(f"Scene helper receipt has an invalid source identity: {cached.receipt_path}")
    if cached.result.get("format") != "EndfieldDependencyBootstrapReport/1" or cached.result.get("status") != "passed":
        raise ValueError(f"Scene helper receipt has not passed its bootstrap contract: {cached.receipt_path}")
    helpers = verified_helpers(cached.result, identity, cached.helper_root)
    if helpers is None:
        raise ValueError(f"Scene helper receipt changed during bundle preparation: {cached.receipt_path}")
    receipt_root = destination / "helpers" / identity[:16]
    source_outputs = require_object(cached.result["outputs"], "helper outputs")
    outputs = {
        "bundleScanner": _copy_helper(helpers.scanner, require_object(source_outputs["bundleScanner"], "bundleScanner"),
                                      receipt_root / "bin" / "bundleScanner", receipt_root),
        "sceneProbe": _copy_helper(helpers.probe, require_object(source_outputs["sceneProbe"], "sceneProbe"),
                                   receipt_root / "bin" / "sceneProbe", receipt_root),
    }
    report = {field: cached.result[field] for field in REPORT_FIELDS if field in cached.result}
    report["outputs"] = outputs
    receipt = receipt_root / "current.json"
    verified = verified_helpers(require_object(report, "portable scene helper receipt"), identity, receipt_root)
    if verified is None:
        raise ValueError(f"Portable scene helper receipt failed source identity verification: {receipt}")
    receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Bundle an existing scene helper install for offline distribution.")
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    receipt = export_scene_tool_bundle(args.cache_root, args.destination)
    print(json.dumps({"receipt": str(receipt)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
