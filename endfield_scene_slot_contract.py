"""Resolve ordered slots from current renderer evidence and exact probe containers."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from endfield_scene_material_contract import reference_key, resolve_export_path


class SlotPlan(NamedTuple):
    base: str
    mesh_name: str
    mesh_file: Path
    material_keys: tuple[tuple[str, int], ...]
    material_paths: tuple[str, ...]
    evidence: str


class StaticSlotMapping(NamedTuple):
    indices: tuple[int, ...]
    evidence: str


class AmbiguousSubmeshSlots(ValueError):
    """Renderer slots cannot be mapped without choosing between authored materials."""


class RuntimeDraw(NamedTuple):
    submesh: int
    flags: int


class RuntimeSlotMapping(NamedTuple):
    indices: tuple[int, ...]
    undrawn_submeshes: tuple[int, ...]
    shadow_only_draws: tuple[int, ...]


def runtime_submesh_slots(submeshes: int, references: tuple[str, ...], draws: tuple[RuntimeDraw, ...]) -> RuntimeSlotMapping:
    """Select authored non-shadow draws; retain undrawn geometry in a separate role slot."""
    if submeshes <= 0 or not references or len(draws) != len(references):
        raise AmbiguousSubmeshSlots("Runtime mapping requires one verified draw per material reference")
    selected: dict[int, list[int]] = defaultdict(list)
    shadow = []
    for index, draw in enumerate(draws):
        if not 0 <= draw.submesh < submeshes or not 0 <= draw.flags <= 0xFFFFFFFF:
            raise AmbiguousSubmeshSlots(f"Invalid native draw: index={index}, draw={draw}, submeshes={submeshes}")
        if draw.flags & 1024:
            shadow.append(index)
        else:
            selected[draw.submesh].append(index)
    if not selected:
        raise AmbiguousSubmeshSlots("No non-shadow native draws; an ordinary surface cannot be inferred")
    indices = []
    undrawn = []
    for submesh in range(submeshes):
        candidates = selected[submesh]
        if len({references[index] for index in candidates}) > 1:
            raise AmbiguousSubmeshSlots(f"Conflicting non-shadow materials: submesh={submesh}, references={references}")
        if not candidates:
            # The extra slot is an explicit non-drawing role, never a guessed material.
            indices.append(len(references))
            undrawn.append(submesh)
        else:
            indices.append(candidates[0])
    return RuntimeSlotMapping(tuple(indices), tuple(undrawn), tuple(shadow))


def load_runtime_draw_plans(path: Path) -> dict[tuple[str, str, tuple[str, ...]], tuple[tuple[RuntimeDraw, ...], ...]]:
    """Read optional native draw evidence without changing older renderer manifests."""
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    result: dict[tuple[str, str, tuple[str, ...]], set[tuple[RuntimeDraw, ...]]] = defaultdict(set)
    for base, variants in payload["bases"].items():
        for variant in variants:
            if "draws" not in variant:
                continue
            if payload.get("runtime_draw_contract") != "RendererInfo24_and_LODInfo8_v29_type92_verified":
                raise ValueError("Native draw evidence requires the verified renderer layout contract")
            if len(variant["pairs"]) != len(variant["draws"]):
                raise ValueError(f"Native draw/reference count mismatch: base={base}")
            grouped: dict[str, list[tuple[str, RuntimeDraw]]] = defaultdict(list)
            for pair, raw in zip(variant["pairs"], variant["draws"], strict=True):
                if type(raw.get("submesh")) is not int or type(raw.get("flags")) is not int:
                    raise ValueError(f"Native draw requires integer submesh and flags: base={base}")
                grouped[pair[0]].append((pair[1], RuntimeDraw(raw["submesh"], raw["flags"])))
            for mesh, rows in grouped.items():
                result[(base, asset_name(mesh), tuple(material for material, _ in rows))].add(tuple(draw for _, draw in rows))
    return {key: tuple(sorted(values)) for key, values in result.items()}


def static_submesh_slots(submeshes: int, references: tuple[str, ...]) -> StaticSlotMapping:
    """Map static surfaces only when surplus slots introduce no material choice.

    Repeated draw passes are not reproduced by this static surface contract.
    Original renderer slots remain intact in the source evidence and audits.
    """
    if submeshes <= 0 or not references:
        raise AmbiguousSubmeshSlots("Static slot mapping requires nonempty submeshes and renderer materials")
    if submeshes == len(references):
        return StaticSlotMapping(tuple(range(submeshes)), "exact_ordered_slots")
    if len(set(references)) == 1:
        return StaticSlotMapping((0,) * submeshes, "uniform_exact_reference")
    if len(references) > submeshes:
        first = references[:submeshes]
        if len(references) % submeshes == 0 and all(
            references[index:index + submeshes] == first for index in range(submeshes, len(references), submeshes)
        ):
            return StaticSlotMapping(tuple(range(submeshes)), "identical_complete_renderer_sequences")
        if all(reference == first[-1] for reference in references[submeshes:]):
            return StaticSlotMapping(tuple(range(submeshes)), "identical_trailing_material_passes")
    raise AmbiguousSubmeshSlots(
        f"Renderer material choices remain ambiguous: submeshes={submeshes}, references={references}"
    )


def asset_name(path: str) -> str:
    return (path.rsplit("##", 1)[1] if "##" in path else Path(path).stem).casefold()


def canonical_mesh_name(name: str) -> str:
    """Preserve the legacy builder's exact punctuation normalization, including LOD."""
    return name.casefold().replace("+1_", "_1_")


def mesh_lod_index(name: str) -> int | None:
    """Read an explicit LOD token, including post-export processing suffixes."""
    matches = re.findall(r"(?:^|_)lod(\d+)(?=_|$)", name, flags=re.I)
    if len(matches) > 1:
        raise ValueError(f"Mesh name contains multiple LOD tokens: name={name}")
    return int(matches[0]) if matches else None


def lod_family_name(name: str) -> str:
    """Use the preserved builder's exact LOD family rule, without suffix guessing."""
    return canonical_mesh_name(re.sub(r"_lod\d+$", "", name, flags=re.I))


def load_retained_mesh_slot_plans(manifests: tuple[Path, ...]) -> dict[str, tuple[SlotPlan, ...]]:
    """Recover unanimous original Prefab Renderer slots for retained exported meshes."""
    sequences: dict[tuple[str, int], set[tuple[tuple[str, int], ...]]] = defaultdict(set)
    named_sequences: dict[str, set[tuple[tuple[str, int], ...]]] = defaultdict(set)
    family_sequences: dict[str, set[tuple[tuple[str, int], ...]]] = defaultdict(set)
    source_family_sequences: dict[tuple[str, str], set[tuple[tuple[str, int], ...]]] = defaultdict(set)
    exports: dict[tuple[str, int, str], tuple[str, Path]] = {}
    for path in manifests:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("Format") != "EndfieldSceneProbe/1":
            raise ValueError(f"Unsupported probe: {path}")
        for node in data.get("Nodes", []):
            if not node.get("Mesh") or not node.get("Materials"):
                continue
            sequence = tuple(reference_key(material) for material in node["Materials"])
            sequences[reference_key(node["Mesh"])].add(sequence)
            named_sequences[canonical_mesh_name(node["Mesh"]["Name"])].add(sequence)
            family_sequences[lod_family_name(node["Mesh"]["Name"])].add(sequence)
            source_family_sequences[(reference_key(node["Mesh"])[0], lod_family_name(node["Mesh"]["Name"]))].add(sequence)
        for row in data["MeshExports"]:
            if not row.get("File"):
                continue
            file = resolve_export_path(path.parent / row["File"].replace("\\", "/"))
            if file.is_file():
                key = reference_key(row["Asset"])
                exports[(*key, str(file))] = (row["Asset"]["Name"].casefold(), file)
    result: dict[str, list[SlotPlan]] = defaultdict(list)
    for (source, path_id, _file), (name, file) in exports.items():
        exact = sequences[(source, path_id)]
        agreed = exact
        evidence = "cached_prefab_exact_mesh_renderer"
        if not agreed:
            agreed = source_family_sequences[(source, lod_family_name(name))]
            evidence = "cached_prefab_unanimous_source_lod_family"
        if not agreed:
            agreed = named_sequences[canonical_mesh_name(name)]
            evidence = "cached_prefab_unanimous_exact_mesh_name"
        if not agreed:
            agreed = family_sequences[lod_family_name(name)]
            evidence = "cached_prefab_unanimous_exact_lod_family"
        # Disagreement is evidence, never permission to select a convenient renderer.
        if len(agreed) != 1:
            continue
        result[canonical_mesh_name(name)].append(SlotPlan(
            name, name, file, next(iter(agreed)), (), evidence))
    return {name: tuple(sorted(set(plans), key=lambda plan: str(plan.mesh_file))) for name, plans in result.items()}


def load_slot_plans(renderer_file: Path, manifests: tuple[Path, ...]) -> tuple[dict[str, tuple[SlotPlan, ...]], dict[str, str]]:
    evidence = json.loads(renderer_file.read_text(encoding="utf-8-sig"))
    if evidence.get("format") != "EndfieldCurrentRendererMaterials/1":
        raise ValueError("Renderer evidence requires EndfieldCurrentRendererMaterials/1")
    containers: dict[str, set[tuple[str, int]]] = defaultdict(set)
    mesh_exports: dict[tuple[str, int], tuple[str, Path]] = {}
    for path in manifests:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("Format") != "EndfieldSceneProbe/1":
            raise ValueError(f"Unsupported probe: {path}")
        for row in data["Containers"]:
            if row.get("Asset") and row["Asset"].get("Type") in ("Material", "Mesh"):
                containers[row["Container"].replace("\\", "/").casefold()].add(reference_key(row["Asset"]))
        for row in data["MeshExports"]:
            if not row.get("File"):
                continue
            file = resolve_export_path(path.parent / row["File"].replace("\\", "/"))
            if file.is_file():
                mesh_exports.setdefault(reference_key(row["Asset"]), (row["Asset"]["Name"].casefold(), file))
    failures = {row["base"] for row in evidence["failures"]}
    plans: dict[str, tuple[SlotPlan, ...]] = {}
    pending: dict[str, str] = {}
    for base, variants in evidence["bases"].items():
        if base in failures:
            pending[base] = "current_renderer_decode_failure"
            continue
        signatures: dict[str, set[tuple[str, ...]]] = defaultdict(set)
        presence: dict[str, int] = defaultdict(int)
        for variant in variants:
            groups: dict[str, list[str]] = defaultdict(list)
            for mesh, material in variant["pairs"]:
                groups[mesh].append(material)
            for mesh, materials in groups.items():
                signatures[mesh].add(tuple(materials))
                presence[mesh] += 1
        found = []
        for mesh, sequences in signatures.items():
            if len(sequences) != 1 or presence[mesh] != len(variants):
                continue
            materials = next(iter(sequences))
            references = [containers[p] for p in materials]
            if any(len(keys) != 1 for keys in references):
                continue
            stem = mesh.split("##", 1)[0]
            candidates = [mesh_exports[k] for k in containers[stem] if k in mesh_exports
                          and mesh_exports[k][0] == asset_name(mesh)]
            unique_files = sorted(set(file for _, file in candidates))
            if len(unique_files) != 1:
                continue
            found.append(SlotPlan(base, asset_name(mesh), unique_files[0],
                                  tuple(next(iter(keys)) for keys in references), materials, "current_runtime_renderer"))
        if found:
            plans[base] = tuple(found)
        else:
            pending[base] = "ambiguous_variant_or_missing_exact_probe_reference"
    return plans, pending
