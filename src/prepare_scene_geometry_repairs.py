"""Prepare existing geometry-repair plans from current material correspondence failures.

Only a unique primary mesh with complete material inputs is selected. Placement
and ordered material IDs are rechecked against the original current chunk data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import struct
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import TypedDict

from endfield_scene_material_contract import AssetReference, reference_key, supported_slots
from endfield_scene_material_contract import load_catalog
from endfield_scene_slot_contract import SlotPlan, asset_name, load_slot_plans, mesh_lod_index
from endfield_scene_slot_contract import AmbiguousSubmeshSlots, static_submesh_slots
from extract_scene_renderer_materials import decode_pairs, renderer_columns
from extractor_core.projected_decals import build_path_id_index, read_chunk
from extractor_core.resources import load_group


class MaterialPair(TypedDict):
    material_id: str
    material_path: str
    mesh_path: str


class CachedMesh(TypedDict):
    asset: AssetReference
    mesh_file: str


class RepairRow(TypedDict):
    entity_id: int
    entity_base: str
    source_path: str
    group_index: int
    entity_index: int
    matrix: list[float]
    current_primary_mesh_path: str
    cached_mesh: CachedMesh
    raw_material_pairs: list[MaterialPair]
    import_file: str


def export_key(path: Path) -> str:
    return str(path.resolve()).removeprefix("\\\\?\\").casefold()


def variant_key(base: str, pairs: tuple[tuple[str, str], ...]) -> str:
    return base + "::" + hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()


def obj_submesh_count(path: Path) -> int:
    groups: set[str] = set()
    group = ""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("g "):
                group = line.strip()
            elif line.startswith("f "):
                if not group:
                    raise ValueError(f"Source OBJ face has no submesh group: path={path}")
                groups.add(group)
    return len(groups)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("renderer", "instances-db", "vfs-root", "path-hash", "output", "material-selection", "slot-bindings"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("manifest", "slot-audit"):
        parser.add_argument("--" + name, type=Path, action="append", required=True)
    parser.add_argument("--bounds", type=float, nargs=4, required=True)
    args = parser.parse_args()
    for path in (args.output, args.material_selection, args.slot_bindings):
        if path.exists():
            raise FileExistsError(path)
    xmin, xmax, zmin, zmax = args.bounds
    if xmin >= xmax or zmin >= zmax:
        raise ValueError("Repair bounds must be increasing half-open ranges")
    wanted: set[str] = set()
    wanted_origins: set[tuple[str, tuple[float, ...]]] = set()
    for path in args.slot_audit:
        audit = json.loads(path.read_text(encoding="utf-8-sig"))
        if audit.get("format") != "EndfieldSceneSlotRestoration/1" or audit.get("reopen_verified") is not True:
            raise ValueError(f"Repair selection requires a verified slot restoration audit: path={path}")
        for row in audit["pending"]:
            if row["reason"] in ("source_geometry_mismatch", "missing_renderer_evidence", "conflicting_geometry_matched_material_sequences"):
                if not row.get("entity_bases"):
                    raise ValueError(f"Slot audit lacks authoritative entity bases: object={row['object']}, path={path}")
                wanted.update(row["entity_bases"])
                if not row.get("instance_origins"):
                    raise ValueError(f"Slot audit lacks retained instance origins: object={row['object']}, path={path}")
                wanted_origins.update((base, tuple(round(value, 4) for value in position))
                                      for base, position in row["instance_origins"])
    manifests = tuple(args.manifest)
    materials, textures = load_catalog(manifests)
    evidence = json.loads(args.renderer.read_text(encoding="utf-8-sig"))
    variants = {}
    variant_bases: dict[str, str] = {}
    for base in sorted(wanted):
        for row in evidence["bases"].get(base, ()):
            key = variant_key(base, tuple(tuple(pair) for pair in row["pairs"]))
            variants[key] = [row]
            variant_bases[key] = base
    variant_evidence = args.output.with_name(args.output.stem + "_variant_renderers.json")
    if variant_evidence.exists():
        raise FileExistsError(variant_evidence)
    variant_evidence.parent.mkdir(parents=True, exist_ok=True)
    variant_evidence.write_text(json.dumps({"format": "EndfieldCurrentRendererMaterials/1", "bases": variants,
                                          "failures": []}, indent=2), encoding="utf-8")
    plans, _ = load_slot_plans(variant_evidence, manifests)
    path_hash_sha256 = hashlib.sha256(args.path_hash.read_bytes()).hexdigest()
    if path_hash_sha256 != evidence["path_hash_sha256"]:
        raise ValueError("Current path-hash data differs from the renderer snapshot")
    mesh_assets: dict[str, AssetReference] = {}
    for path in manifests:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for row in data["MeshExports"]:
            if row.get("File"):
                key = export_key(path.parent / row["File"].replace("\\", "/"))
                if key in mesh_assets and reference_key(mesh_assets[key]) != reference_key(row["Asset"]):
                    raise ValueError(f"Source export maps to multiple asset references: path={key}")
                mesh_assets[key] = row["Asset"]
    selected: dict[str, tuple[SlotPlan, str]] = {}
    pending: list[dict[str, str]] = [{"base": base, "reason": "no_decoded_current_renderer_evidence"}
                                    for base in sorted(wanted - set(variant_bases.values()))]
    for variant, base in sorted(variant_bases.items()):
        entries = plans.get(variant, ())
        primary = [plan for plan in entries if mesh_lod_index(plan.mesh_name) == 0]
        if not primary and len(entries) == 1 and mesh_lod_index(entries[0].mesh_name) is None:
            primary = list(entries)
        if len(primary) != 1:
            pending.append({"base": base, "reason": "no_unique_current_primary_mesh"})
            continue
        plan = primary[0]
        if any(key not in materials for key in plan.material_keys):
            pending.append({"base": base, "reason": "material_record_missing"})
            continue
        records = [materials[key] for key in plan.material_keys]
        if any(row["Name"].casefold().startswith(("m_fx_", "m_water", "m_shadow", "m_fog", "m_volume")) for row in records):
            pending.append({"base": base, "reason": "deferred_effect_water_or_helper"})
            continue
        try:
            slots = [slot for row in records for slot in supported_slots(row)]
        except ValueError as error:
            pending.append({"base": base, "reason": "unsupported_material_inputs", "detail": str(error)})
            continue
        if any(reference_key(slot["Texture"]) not in textures for slot in slots):
            pending.append({"base": base, "reason": "required_texture_missing"})
            continue
        try:
            static_submesh_slots(obj_submesh_count(plan.mesh_file), tuple(f"{source}:{identifier}" for source, identifier in plan.material_keys))
        except AmbiguousSubmeshSlots as error:
            pending.append({"base": base, "reason": "source_submesh_material_count_mismatch", "detail": str(error)})
            continue
        mesh_paths = {mesh for row in variants[variant] for mesh, material in row["pairs"]
                      if asset_name(mesh) == plan.mesh_name}
        if len(mesh_paths) != 1:
            pending.append({"base": base, "reason": "primary_mesh_path_not_unique"})
            continue
        selected[variant] = (plan, next(iter(mesh_paths)))
    with closing(sqlite3.connect(args.instances_db.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT entity_id,entity_base,source_path,group_index,entity_index,matrix_col_major "
                          "FROM instances WHERE x>=? AND x<? AND z>=? AND z<? ORDER BY source_path,entity_id",
                          args.bounds).fetchall()
    grouped: dict[str, list[tuple[int, str, str, int, int, bytes]]] = defaultdict(list)
    selected_bases = {variant_bases[variant] for variant in selected}
    for row in rows:
        matrix = struct.unpack("<16f", row[5])
        origin = (row[1], tuple(round(value, 4) for value in (matrix[12], -matrix[14], matrix[13])))
        if row[1] in selected_bases and origin in wanted_origins:
            grouped[row[2]].append(row)
    entries = {entry.file_name: entry for entry in load_group(args.vfs_root, "C3442D43").iter_files()}
    paths = build_path_id_index(args.path_hash)
    repairs: list[RepairRow] = []
    verified_sources: dict[str, str] = {}
    used_variants: set[str] = set()
    for source, instances in grouped.items():
        data = read_chunk(args.vfs_root, entries[source])
        digest = hashlib.sha256(data).hexdigest()
        if digest != evidence["source_sha256"].get(source):
            raise ValueError(f"Current chunk changed since renderer extraction: source={source}")
        verified_sources[source] = digest
        columns = renderer_columns(data)
        for entity_id, base, _, group_index, entity_index, matrix in instances:
            if entity_id not in columns:
                pending.append({"base": base, "reason": "current_renderer_missing", "entity_id": str(entity_id)})
                continue
            decoded = tuple(decode_pairs(raw, paths) for raw in columns[entity_id])
            variant = variant_key(base, tuple(pair for group in decoded for pair in group))
            if variant not in selected:
                pending.append({"base": base, "reason": "current_variant_not_ready", "entity_id": str(entity_id)})
                continue
            plan, mesh_path = selected[variant]
            pairs: list[MaterialPair] = []
            for raw, decoded_pairs in zip(columns[entity_id], decoded, strict=True):
                for index, (mesh, material) in enumerate(decoded_pairs):
                    if mesh == mesh_path:
                        pairs.append({"material_id": raw[8 + index * 40:16 + index * 40].hex(),
                                      "material_path": material, "mesh_path": mesh})
            if tuple(pair["material_path"] for pair in pairs) != plan.material_paths:
                raise ValueError(f"Current ordered slots differ from repair plan: entity_id={entity_id}, base={base}")
            used_variants.add(variant)
            repairs.append({"entity_id": entity_id, "entity_base": base, "source_path": source,
                "group_index": group_index, "entity_index": entity_index, "matrix": list(struct.unpack("<16f", matrix)),
                "current_primary_mesh_path": mesh_path,
                "cached_mesh": {"asset": mesh_assets[export_key(plan.mesh_file)], "mesh_file": str(plan.mesh_file)},
                "raw_material_pairs": pairs, "import_file": str(plan.mesh_file)})
        print(json.dumps({"event": "geometry_repair_chunk_verified", "source": source, "instances": len(instances)}), flush=True)
    references = {key for variant in used_variants for key in selected[variant][0].material_keys}
    bindings = {path: key for variant in used_variants
                for path, key in zip(selected[variant][0].material_paths, selected[variant][0].material_keys, strict=True)}
    for path in (args.output, args.material_selection, args.slot_bindings):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"format": "EndfieldGeometryRepairPlan/1",
        "status": "current_renderer_verified_not_applied", "instances": repairs, "pending": pending,
        "source_sha256": verified_sources, "path_hash_sha256": path_hash_sha256,
        "bounds": args.bounds, "slot_audits": [str(path) for path in args.slot_audit]}, indent=2), encoding="utf-8")
    args.material_selection.write_text(json.dumps({"format": "EndfieldMaterialSelection/1", "references": [
        {"Source": key[0], "PathId": key[1]} for key in sorted(references)]}, indent=2), encoding="utf-8")
    args.slot_bindings.write_text(json.dumps({"format": "EndfieldExactMaterialSlots/1", "bindings": [
        {"container": path, "source_reference": f"{key[0]}:{key[1]}"} for path, key in sorted(bindings.items())]}, indent=2), encoding="utf-8")
    print(json.dumps({"event": "geometry_repair_plan_prepared", "instances": len(repairs),
                      "bases": len({row['entity_base'] for row in repairs}), "pending": len(pending)}), flush=True)


if __name__ == "__main__":
    main()
