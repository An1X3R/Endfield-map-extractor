"""Export one discovered level map and bind its registry to an explicit runtime."""
from __future__ import annotations

import argparse
from hashlib import file_digest
import json
import os
from pathlib import Path
from uuid import uuid4

from endfield_character_sources import validate_character_storage
from endfield_level_maps import discover_level_maps, export_level_map, level_map_source_context, public_level_map_catalog
from endfield_scene_helpers import require_prepared_scene_helpers
from endfield_scene_resources import resolve_path


def bind_level_map_registry(runtime_path: Path, original_runtime: bytes, manifest: Path) -> None:
    """Bind a verified registry only while the selected runtime remains unchanged."""
    runtime = json.loads(original_runtime.decode("utf-8-sig"))
    if not isinstance(runtime, dict) or runtime.get("format") != "EndfieldRuntimeConfig/1" or not isinstance(runtime.get("extraction"), dict):
        raise ValueError(f"Unsupported runtime configuration: {runtime_path}")
    with manifest.open("rb") as stream:
        digest = file_digest(stream, "sha256").hexdigest()
    runtime["extraction"] = {**runtime["extraction"], "levelMaps": {
        "path": Path(os.path.relpath(manifest, runtime_path.resolve().parent)).as_posix(),
        "sha256": digest, "bytes": manifest.stat().st_size}}
    temporary = runtime_path.with_suffix(f".level-maps-{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(runtime, stream, ensure_ascii=False, indent=2)
    if runtime_path.read_bytes() != original_runtime:
        temporary.unlink()
        raise ValueError("Runtime configuration changed during map export; completed map retained, descriptor not overwritten")
    temporary.replace(runtime_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--level", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--helper-cache", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    original_runtime = args.runtime.read_bytes()
    runtime = json.loads(original_runtime.decode("utf-8-sig"))
    if not isinstance(runtime, dict) or runtime.get("format") != "EndfieldRuntimeConfig/1" or not isinstance(runtime.get("extraction"), dict):
        raise ValueError(f"Unsupported runtime configuration: {args.runtime}")
    context = level_map_source_context(args.runtime.resolve())
    validate_character_storage(context.game_root, args.runtime.resolve().parent)
    validate_character_storage(context.game_root, args.output)
    if resolve_path(runtime["paths"]["game_root"], args.runtime.resolve().parent) != context.game_root:
        raise ValueError("Selected runtime game differs from prepared map source")
    helpers = require_prepared_scene_helpers(args.runtime.resolve(), args.helper_cache.resolve())

    def check_cancelled() -> None:
        return None

    def progress(stage: str, fraction: float, message: str) -> None:
        print(json.dumps({"stage": stage, "fraction": fraction, "message": message}), flush=True)

    catalog = discover_level_maps(context, check_cancelled)
    manifest = export_level_map(context, catalog, args.level, helpers.probe, args.output.resolve(), check_cancelled, progress)
    (args.output / "catalog.json").write_text(json.dumps(public_level_map_catalog(catalog), ensure_ascii=False, indent=2), encoding="utf-8")
    bind_level_map_registry(args.runtime, original_runtime, manifest)
    print(json.dumps({"status": "completed", "manifest": str(manifest), "runtime": str(args.runtime)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
