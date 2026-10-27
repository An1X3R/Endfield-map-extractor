"""Match prefab gaps to independently serialized ordinary static draw layouts."""
from __future__ import annotations

from collections.abc import Mapping
import math
import re
from typing import TypedDict, cast

from endfield_scene_material_contract import reference_key, require_object
from endfield_scene_slot_contract import AmbiguousSubmeshSlots, RuntimeDraw, runtime_submesh_passes


BindingKey = tuple[tuple[str, int], tuple[tuple[str, int] | None, ...]]


class NativeDraw(TypedDict):
    material_id: str
    material_path: str
    mesh_id: str
    mesh_path: str
    submesh: int
    flags: int
    reference_component: int
    source_draw_index: int
    lod_max_squared: float
    lod_min_squared: float


class NativeUndrawnEvidence(TypedDict):
    format: str
    entity_id: int
    source_path: str
    source_sha256: str
    group_index: int
    entity_index: int
    mesh_asset: dict[str, object]
    mesh_path: str
    mesh_hash_hex: str
    material_refs: list[dict[str, object] | None]
    selected_submeshes: list[int]
    undrawn_submeshes: list[int]
    reference_components: list[int]
    source_draws: list[NativeDraw]


def _reference(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source, path_id = reference_key(require_object(value, "native draw asset reference"))
    return {"Source": source, "PathId": path_id}


def _binding_key(mesh: Mapping[str, object], refs: list[dict[str, object] | None]) -> BindingKey:
    return reference_key(mesh), tuple(reference_key(ref) if ref is not None else None for ref in refs)


def _source_draw(value: object) -> NativeDraw:
    row = require_object(value, "native source draw")
    for name in ("material_id", "mesh_id"):
        if not isinstance(row.get(name), str) or re.fullmatch(r"[0-9a-f]{16}", cast(str, row[name])) is None:
            raise ValueError(f"Native source draw requires an exact hash: field={name}")
    for name in ("material_path", "mesh_path"):
        if not isinstance(row.get(name), str) or not row[name]:
            raise ValueError(f"Native source draw requires a resource path: field={name}")
    for name in ("submesh", "flags", "reference_component", "source_draw_index"):
        if type(row.get(name)) is not int or cast(int, row[name]) < 0:
            raise ValueError(f"Native source draw requires an unsigned integer: field={name}")
    component = cast(int, row["reference_component"])
    if (not 68 <= component <= 74 or cast(int, row["source_draw_index"]) >= 1 << (component - 68)
            or cast(int, row["flags"]) > 0xFFFFFFFF):
        raise ValueError("Native source draw reference component, index or flags are invalid")
    for name in ("lod_max_squared", "lod_min_squared"):
        if type(row.get(name)) not in (int, float) or not math.isfinite(cast(float, row[name])):
            raise ValueError(f"Native source draw requires a finite LOD interval: field={name}")
    if cast(float, row["lod_max_squared"]) < cast(float, row["lod_min_squared"]):
        raise ValueError("Native source draw LOD interval is reversed")
    return cast(NativeDraw, {name: row[name] for name in NativeDraw.__annotations__})


def _draw_signature(proof: NativeUndrawnEvidence) -> tuple[tuple[object, ...], ...]:
    return tuple((draw["material_id"], draw["mesh_id"], draw["submesh"], draw["flags"], draw["reference_component"])
                 for draw in proof["source_draws"])


def native_undrawn_index(
    rows: tuple[Mapping[str, object], ...],
) -> dict[BindingKey, NativeUndrawnEvidence | None]:
    """Index validated native rows once, blocking conflicting layouts deterministically."""
    result: dict[BindingKey, NativeUndrawnEvidence | None] = {}
    layouts: dict[BindingKey, set[BindingKey]] = {}
    for row in rows:
        if not row.get("undrawn_submeshes"):
            continue
        raw_draws = row.get("source_renderer_pairs", row.get("source_draw_evidence"))
        if raw_draws is None:
            continue
        if not isinstance(raw_draws, list):
            raise ValueError("Native source draw evidence must be an array")
        raw_refs = row.get("material_refs")
        if not isinstance(raw_refs, list) or not raw_refs:
            raise ValueError("Native source draw material references must be a nonempty array")
        refs = [_reference(value) for value in raw_refs]
        mesh = require_object(row.get("mesh_asset"), "native source draw mesh")
        key = _binding_key(mesh, refs)
        binding = (key[0], tuple(ref for ref in key[1] if ref is not None))
        layouts.setdefault(binding, set()).add(key)
        if ("source_renderer_pairs" in row and "source_draw_evidence" in row
                and row["source_renderer_pairs"] != row["source_draw_evidence"]):
            result[key] = None
            continue
        draws = [_source_draw(value) for value in raw_draws]
        target = [draw for draw in draws if draw["mesh_path"] == row.get("mesh_path")]
        pairs = [{name: draw[name] for name in ("material_id", "material_path", "mesh_id", "mesh_path", "submesh", "flags")}
                 for draw in target]
        if not target or pairs != row.get("renderer_pairs") or len({draw["mesh_id"] for draw in target}) != 1:
            result[key] = None
            continue
        identities = [(draw["reference_component"], draw["source_draw_index"]) for draw in draws]
        if len(set(identities)) != len(identities):
            raise ValueError("Native source draw reference identities are repeated")
        try:
            mapping = runtime_submesh_passes(len(refs), tuple(draw["material_id"] for draw in target),
                                            tuple(RuntimeDraw(draw["submesh"], draw["flags"]) for draw in target))
        except AmbiguousSubmeshSlots:
            result[key] = None
            continue
        selected = [index for index, indices in enumerate(mapping.indices) if indices]
        undrawn = list(mapping.undrawn_submeshes)
        if (row.get("selected_submeshes") != selected or row.get("undrawn_submeshes") != undrawn
                or [index for index, ref in enumerate(refs) if ref is None] != undrawn
                or any(len(indices) > 1 for indices in mapping.indices)
                or any(draw["submesh"] in undrawn for draw in target)):
            result[key] = None
            continue
        for name in ("entity_id", "group_index", "entity_index"):
            if type(row.get(name)) is not int or cast(int, row[name]) < 0:
                raise ValueError(f"Native draw witness requires an unsigned source identity: field={name}")
        source, digest = row.get("source_path"), row.get("source_sha256")
        if (not isinstance(source, str) or not source.startswith("Data/Streaming/PC/")
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise ValueError("Native draw witness requires a static source path and SHA-256")
        proof: NativeUndrawnEvidence = {
            "format": "EndfieldNativeUndrawnSubmeshes/1", "entity_id": cast(int, row["entity_id"]),
            "source_path": source, "source_sha256": digest, "group_index": cast(int, row["group_index"]),
            "entity_index": cast(int, row["entity_index"]), "mesh_asset": cast(dict[str, object], _reference(mesh)),
            "mesh_path": cast(str, row["mesh_path"]), "mesh_hash_hex": target[0]["mesh_id"], "material_refs": refs,
            "selected_submeshes": selected, "undrawn_submeshes": undrawn,
            "reference_components": sorted({draw["reference_component"] for draw in target}), "source_draws": target,
        }
        if key not in result:
            result[key] = proof
        else:
            previous = result[key]
            if previous is not None:
                if _draw_signature(previous) != _draw_signature(proof):
                    result[key] = None
                elif (source, proof["entity_id"]) < (previous["source_path"], previous["entity_id"]):
                    result[key] = proof
    for keys in layouts.values():
        if len(keys) > 1:
            for key in keys:
                result[key] = None
    return result


def native_undrawn_evidence(
    index: Mapping[BindingKey, NativeUndrawnEvidence | None], mesh: Mapping[str, object],
    refs: list[Mapping[str, object]], submesh_count: int,
) -> NativeUndrawnEvidence | None:
    """Confirm only trailing absent slots with the exact native per-submesh layout."""
    if not 1 < len(refs) < submesh_count:
        return None
    padded = [_reference(ref) for ref in refs] + [None] * (submesh_count - len(refs))
    proof = index.get(_binding_key(mesh, padded))
    if (proof is None or proof["selected_submeshes"] != list(range(len(refs)))
            or proof["undrawn_submeshes"] != list(range(len(refs), submesh_count))):
        return None
    return proof
