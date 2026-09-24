"""Generate a runtime level/ownership manifest from extracted evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from extractor_core.runtime_manifest import augment_runtime_manifest, load_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Augment the Endfield runtime map manifest from extracted level evidence")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--scene-manifest", type=Path, required=True)
    parser.add_argument("--region-records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--map-id", default="map02")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"Refusing to overwrite: {args.output}")
    manifest = augment_runtime_manifest(
        load_json(args.base),
        scene_manifest=args.scene_manifest,
        region_records=args.region_records,
        map_id=args.map_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "levels": [x["levelId"] for x in manifest["maps"][args.map_id]["levels"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
