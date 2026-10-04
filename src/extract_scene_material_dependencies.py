"""Extract exact requested material containers through the existing bundle closure."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from extractor_core.resources import extract_candidate_bundles, read_bundle_closure_names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("paths", "providers", "cab-inventory", "database", "vfs-root", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.audit.exists() or args.output.exists():
        raise FileExistsError("Material dependency extraction requires new output and audit paths")
    requested = json.loads(args.paths.read_text(encoding="utf-8-sig"))
    if not isinstance(requested, list) or not requested or not all(isinstance(path, str) and path.casefold().endswith(".mat") for path in requested):
        raise ValueError("Requested paths must be a nonempty JSON array of material container paths")
    matches: dict[str, set[str]] = {path.replace("\\", "/").casefold(): set() for path in requested}
    with args.providers.open(encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"Material provider record must be an object: path={args.providers}, line={number}")
            if row.get("kind") != "match":
                continue
            logical_name = row.get("logical_name")
            containers = row.get("containers")
            if not isinstance(logical_name, str) or not isinstance(containers, list) or not all(isinstance(value, str) for value in containers):
                raise ValueError(f"Invalid material provider record: path={args.providers}, line={number}")
            for container in containers:
                key = container.replace("\\", "/").casefold()
                if key in matches:
                    matches[key].add(logical_name.casefold())
    invalid = {path: sorted(names) for path, names in matches.items() if len(names) != 1}
    if invalid:
        raise ValueError(f"Material containers require unique current providers: {json.dumps(invalid)}")
    roots = sorted({name for names in matches.values() for name in names})
    closures, metadata = read_bundle_closure_names({"materials": roots}, args.cab_inventory)
    if metadata["maps"]["materials"]["unresolvedExternalCount"]:
        raise ValueError(f"Material dependency closure is incomplete: {json.dumps(metadata)}")
    print(json.dumps({"event": "material_dependency_closure_ready", "roots": len(roots), "bundles": len(closures["materials"])}), flush=True)
    extracted = extract_candidate_bundles(args.database, args.vfs_root, closures["materials"], args.output, progress=None, cancelled=None)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps({"format": "EndfieldMaterialDependencyExtraction/1", "providers": {path: sorted(names) for path, names in matches.items()}, "closure": metadata, "extraction": extracted}, indent=2), encoding="utf-8")
    print(json.dumps({"event": "material_dependencies_extracted", "output": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
