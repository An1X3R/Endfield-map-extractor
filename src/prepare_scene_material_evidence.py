"""Complete exact renderer material dependencies from existing SceneProbe caches.

This stage does not infer material names or parse new game formats. Conflicting
references remain explicit, and all selected exports must exist on disk.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import TypedDict, cast

from endfield_scene_material_contract import AssetReference, MaterialRecord, reference_key, require_object, validate_material, resolve_export_path
from endfield_scene_slot_contract import canonical_mesh_name, lod_family_name


class ExportRecord(TypedDict):
    Asset: AssetReference
    File: str


class ContainerRecord(TypedDict):
    Container: str
    Asset: AssetReference


def records(data: dict[str, object], field: str) -> tuple[dict[str, object], ...]:
    values = data.get(field, [])
    if not isinstance(values, list):
        raise ValueError(f"Probe field requires an array: field={field}")
    return tuple(require_object(value, field) for value in values)


def material_fingerprint(record: MaterialRecord) -> str:
    """Compare shader inputs independently of serialization order and extra metadata."""
    signature = {"Name": record["Name"], "Shader": reference_key(record["Shader"]),
                 "Floats": record["Floats"], "Colors": record["Colors"],
                 "Textures": sorted((slot["Property"], reference_key(slot["Texture"]) if slot["Texture"] else None,
                                     slot["Scale"], slot["Offset"]) for slot in record["Textures"])}
    return hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()


def renderer_material_paths(path: Path) -> set[str]:
    payload = require_object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))
    if payload.get("format") != "EndfieldCurrentRendererMaterials/1":
        raise ValueError("Expected EndfieldCurrentRendererMaterials/1 renderer evidence")
    result: set[str] = set()
    for base, values in require_object(payload.get("bases"), "renderer bases").items():
        if not isinstance(values, list):
            raise ValueError(f"Renderer variants require an array: base={base}")
        for value in values:
            pairs = require_object(value, base).get("pairs")
            if not isinstance(pairs, list):
                raise ValueError(f"Renderer pairs require an array: base={base}")
            for pair in pairs:
                if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(item, str) for item in pair):
                    raise ValueError(f"Renderer pair requires two paths: base={base}, pair={pair}")
                result.add(pair[1].replace("\\", "/").casefold())
    return result


def retained_mesh_names(paths: tuple[Path, ...]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        payload = require_object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))
        for row in records(payload, "templates"):
            name = row.get("source_asset")
            if not isinstance(name, str) or not name:
                raise ValueError(f"Template inventory requires source_asset: path={path}, object={row.get('object')}")
            names.add(canonical_mesh_name(name))
    if not names:
        raise ValueError("Template inventories contain no retained meshes")
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("renderer", "cache-list", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("manifest", "template-inventory"):
        parser.add_argument("--" + name, type=Path, action="append", required=True)
    args = parser.parse_args()
    if args.output.exists() or args.audit.exists():
        raise FileExistsError("Evidence output and audit require new paths")
    primary = tuple(path.resolve() for path in args.manifest)
    cached = tuple(Path(line).resolve() for line in args.cache_list.read_text(encoding="utf-8-sig").splitlines() if line.strip())
    paths = tuple(dict.fromkeys((*primary, *cached)))
    requested = renderer_material_paths(args.renderer)
    names = retained_mesh_names(tuple(args.template_inventory))
    families = {lod_family_name(name) for name in names}
    known: dict[tuple[str, int], MaterialRecord] = {}
    known_containers: set[str] = set()
    known_meshes: set[tuple[str, int]] = set()
    known_textures: set[tuple[str, int]] = set()
    containers: dict[str, dict[tuple[str, int], ContainerRecord]] = defaultdict(dict)
    materials: dict[tuple[str, int], dict[str, MaterialRecord]] = defaultdict(dict)
    textures: dict[tuple[str, int], ExportRecord] = {}
    meshes: dict[tuple[str, int], ExportRecord] = {}
    nodes: dict[str, dict[str, object]] = {}
    provenance: dict[tuple[str, int], set[str]] = defaultdict(set)
    required: set[tuple[str, int]] = set()
    for index, path in enumerate(paths):
        data = require_object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))
        if data.get("Format") != "EndfieldSceneProbe/1":
            raise ValueError(f"Expected EndfieldSceneProbe/1: path={path}")
        current = path in primary
        for row in records(data, "Containers"):
            if not row.get("Asset"):
                continue
            asset = require_object(row["Asset"], "container asset")
            container = row.get("Container")
            if not isinstance(container, str):
                raise ValueError(f"Container requires a path: manifest={path}")
            container = container.replace("\\", "/").casefold()
            if asset.get("Type") != "Material" or container not in requested:
                continue
            key = reference_key(asset)
            required.add(key)
            if current:
                known_containers.add(container)
            elif container not in known_containers:
                containers[container][key] = {"Container": container, "Asset": cast(AssetReference, asset)}
        for row in records(data, "Nodes"):
            if not row.get("Mesh") or not row.get("Materials"):
                continue
            mesh = require_object(row["Mesh"], "renderer mesh")
            if not isinstance(mesh.get("Name"), str):
                raise ValueError(f"Renderer mesh requires a name: manifest={path}")
            if lod_family_name(cast(str, mesh["Name"])) not in families:
                continue
            references = row["Materials"]
            if not isinstance(references, list):
                raise ValueError(f"Renderer materials require an array: manifest={path}")
            required.update(reference_key(ref) for ref in references if reference_key(ref)[1] != 0)
            if not current:
                nodes[json.dumps(row, sort_keys=True)] = row
        for row in records(data, "Materials"):
            record = validate_material(row)
            key = reference_key(record)
            if current:
                if key in known and material_fingerprint(known[key]) != material_fingerprint(record):
                    raise ValueError(f"Conflicting primary material records: reference={key}, manifest={path}")
                known[key] = record
            elif key not in known:
                materials[key][material_fingerprint(record)] = record
                provenance[key].add(str(path))
        for field in ("MeshExports", "TextureExports"):
            for row in records(data, field):
                if not row.get("File"):
                    continue
                file_value = row["File"]
                if not isinstance(file_value, str):
                    raise ValueError(f"Export File requires a path: manifest={path}, field={field}")
                asset = require_object(row.get("Asset"), "export asset")
                key = reference_key(asset)
                file = resolve_export_path(path.parent / file_value.replace("\\", "/"))
                if not file.is_file():
                    continue
                export: ExportRecord = {"Asset": cast(AssetReference, asset), "File": str(file)}
                if field == "TextureExports":
                    if current:
                        known_textures.add(key)
                    elif key not in known_textures:
                        textures.setdefault(key, export)
                elif current:
                    known_meshes.add(key)
                elif key not in known_meshes:
                    name = asset.get("Name")
                    if not isinstance(name, str):
                        raise ValueError(f"Mesh export requires a name: manifest={path}")
                    if canonical_mesh_name(name) in names:
                        meshes.setdefault(key, export)
        print(json.dumps({"event": "cached_material_evidence_scanned", "completed": index + 1,
                          "total": len(paths), "manifest": str(path)}), flush=True)
    selected_containers = [next(iter(rows.values())) for name, rows in sorted(containers.items())
                           if name not in known_containers and len(rows) == 1]
    selected = {key: next(iter(materials[key].values())) for key in sorted(required - known.keys()) if len(materials[key]) == 1}
    needed_textures = {reference_key(slot["Texture"]) for key in required
                       for record in ([known[key]] if key in known else [selected[key]] if key in selected else [])
                       for slot in record["Textures"] if slot["Texture"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Nodes": list(nodes.values()),
        "Containers": selected_containers, "Materials": list(selected.values()), "MeshExports": list(meshes.values()),
        "TextureExports": [textures[key] for key in sorted(needed_textures - known_textures) if key in textures]}, indent=2), encoding="utf-8")
    audit = {"format": "EndfieldCachedMaterialEvidence/1", "output": str(args.output),
        "inputs": [str(path) for path in paths], "materials": len(selected), "containers": len(selected_containers),
        "meshes": len(meshes), "nodes": len(nodes),
        "missing_containers": sorted(requested - known_containers - containers.keys()),
        "conflicting_containers": {name: list(rows) for name, rows in containers.items() if name not in known_containers and len(rows) > 1},
        "missing_materials": [key for key in sorted(required - known.keys()) if not materials[key]],
        "conflicting_materials": [key for key in sorted(required - known.keys()) if len(materials[key]) > 1],
        "missing_textures": sorted(needed_textures - known_textures - textures.keys()),
        "material_provenance": {str(key): sorted(provenance[key]) for key in selected}}
    args.audit.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps({"event": "cached_material_evidence_ready", "materials": len(selected),
                      "containers": len(selected_containers), "missing_textures": len(audit["missing_textures"])}), flush=True)


if __name__ == "__main__":
    main()
