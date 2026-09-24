"""Recover current renderer material sequences using the existing VFS and path-ID readers.

Component 68..74 layouts reuse the Stage66/67 audited fixed-pair contract:
an eight-byte count followed by 40-byte material/mesh pairs. No name aliases.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import struct
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

from extractor_core import flatbuffer as fb
from extractor_core.projected_decals import build_path_id_index, read_chunk
from extractor_core.resources import load_group


class RendererDraw(NamedTuple):
    submesh: int
    flags: int
    lod_max_squared: float
    lod_min_squared: float


def component_columns(data: bytes) -> dict[int, dict[int, bytes]]:
    """Read fixed-stride ECS columns once for reference and draw consumers."""
    root = fb.u32(data, 0)
    ids, id_count = fb.vector_from_field(data, root, 6)
    groups, group_count = fb.vector_from_field(data, root, 7)
    if id_count != group_count:
        raise ValueError("Renderer InitChunkData group count mismatch")
    result: dict[int, dict[int, bytes]] = {}
    for index in range(group_count):
        group = fb.table_vector_element(data, groups, index)
        count_address = fb.field_address(data, group, 1)
        blob_address = fb.field_address(data, group, 4)
        if count_address is None or blob_address is None:
            raise ValueError(f"Renderer group missing count or blob: group={index}")
        count = fb.u32(data, count_address)
        entity_ids, nids = fb.wrapped_vector(data, fb.table_vector_element(data, ids, index))
        descriptors, ndesc = fb.vector_from_field(data, group, 3)
        blob, length = fb.wrapped_vector(data, fb.offset_target(data, blob_address))
        if count != nids:
            raise ValueError(f"Renderer group entity count mismatch: group={index}")
        columns: list[tuple[int, int, int]] = []
        cursor = 0
        for i in range(ndesc):
            kind, stride, reserved = struct.unpack_from("<HHI", data, descriptors + 8 * i)
            if reserved:
                raise ValueError(f"Renderer descriptor reserved bits: group={index}, type={kind}")
            if 68 <= kind <= 74:
                expected = 8 + 40 * (1 << (kind - 68))
                if stride != expected:
                    raise ValueError(f"Renderer stride changed: type={kind}, actual={stride}, expected={expected}")
                columns.append((kind, cursor, stride))
            elif 8 <= kind <= 14:
                expected = 4 + 32 * (1 << (kind - 8))
                if stride != expected:
                    raise ValueError(f"Runtime draw stride changed: type={kind}, actual={stride}, expected={expected}")
                columns.append((kind, cursor, stride))
            cursor += count * stride
        if cursor != length:
            raise ValueError(f"Renderer blob length mismatch: group={index}, actual={cursor}, expected={length}")
        if not columns:
            continue
        for i in range(count):
            entity_id = fb.u32(data, entity_ids + 4 * i)
            if entity_id in result:
                raise ValueError(f"Duplicate renderer entity: entity_id={entity_id}")
            if len({kind for kind, _, _ in columns}) != len(columns):
                raise ValueError(f"Duplicate renderer component: entity_id={entity_id}")
            result[entity_id] = {kind: data[blob + start + i * stride:blob + start + (i + 1) * stride]
                                for kind, start, stride in columns}
    return result


def renderer_columns(data: bytes) -> dict[int, tuple[bytes, ...]]:
    """Preserve the reference-only API used by geometry reconstruction."""
    return {entity: tuple(raw for kind, raw in columns.items() if 68 <= kind <= 74)
            for entity, columns in component_columns(data).items() if any(68 <= kind <= 74 for kind in columns)}


def decode_draws(raw: bytes, references: bytes) -> tuple[RendererDraw, ...]:
    """Decode verified RendererInfo24 plus LODInfo8 arrays, including capacities 32/64."""
    if len(references) < 48 or (len(references) - 8) % 40:
        raise ValueError(f"Invalid renderer reference stride: size={len(references)}")
    capacity = (len(references) - 8) // 40
    if capacity not in (1, 2, 4, 8, 16, 32, 64) or len(raw) != 4 + 32 * capacity:
        raise ValueError(f"Runtime/reference capacity mismatch: runtime_size={len(raw)}, capacity={capacity}")
    count = struct.unpack_from("<I", raw)[0]
    ref_count = struct.unpack_from("<Q", references)[0]
    if count != ref_count or count > capacity:
        raise ValueError(f"Runtime/reference draw count mismatch: runtime={count}, references={ref_count}, capacity={capacity}")
    result = []
    for index in range(count):
        _keywords, submesh, _order, _material, _geometry, _shadow, flags, _mask = struct.unpack_from("<HBbIIIII", raw, 4 + 24 * index)
        lod_max, lod_min = struct.unpack_from("<ff", raw, 4 + 24 * capacity + 8 * index)
        if not math.isfinite(lod_max) or not math.isfinite(lod_min) or lod_max < lod_min:
            raise ValueError(f"Invalid runtime LOD interval: draw={index}, maximum={lod_max}, minimum={lod_min}")
        result.append(RendererDraw(submesh, flags, lod_max, lod_min))
    return tuple(result)


def decode_pairs(raw: bytes, paths: dict[bytes, tuple[str, ...]]) -> tuple[tuple[str, str], ...]:
    if len(raw) < 48 or (len(raw) - 8) % 40:
        raise ValueError(f"Invalid renderer reference stride: size={len(raw)}")
    count = struct.unpack_from("<Q", raw)[0]
    capacity = (len(raw) - 8) // 40
    if count > capacity:
        raise ValueError(f"Renderer record count exceeds capacity: count={count}, capacity={capacity}")
    pairs: list[tuple[str, str]] = []
    for index in range(count):
        material = paths.get(raw[8 + index * 40:16 + index * 40], ())
        mesh = paths.get(raw[16 + index * 40:24 + index * 40], ())
        if len(material) != 1 or len(mesh) != 1 or not material[0].casefold().endswith(".mat"):
            raise ValueError(f"Renderer pair has unresolved or ambiguous IDs: pair={index}, material={material}, mesh={mesh}")
        if Path(mesh[0].split("##")[0]).suffix.casefold() not in (".fbx", ".mesh"):
            raise ValueError(f"Renderer mesh reference has invalid type: {mesh[0]}")
        pairs.append((mesh[0].casefold(), material[0].casefold()))
    return tuple(pairs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("instances-db", "vfs-root", "path-hash", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--bounds", type=float, nargs=4, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve().is_relative_to(args.vfs_root.resolve()):
        raise FileExistsError("Renderer output must be a new external file")
    with closing(sqlite3.connect(args.instances_db.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT entity_id,entity_base,source_path FROM instances WHERE x>=? AND x<? AND z>=? AND z<? AND entity_base IS NOT NULL ORDER BY source_path,entity_id", args.bounds).fetchall()
    sources: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for entity_id, base, source in rows:
        sources[source].append((entity_id, base))
    entries = {entry.file_name: entry for entry in load_group(args.vfs_root, "C3442D43").iter_files()}
    paths = build_path_id_index(args.path_hash)
    signatures: dict[str, dict[tuple[tuple[tuple[str, str], ...], tuple[RendererDraw, ...]], int]] = defaultdict(lambda: defaultdict(int))
    failures = []
    evidence: dict[str, str] = {}
    for source, instances in sources.items():
        data = read_chunk(args.vfs_root, entries[source])
        evidence[source] = hashlib.sha256(data).hexdigest()
        columns = component_columns(data)
        for entity_id, base in instances:
            if entity_id not in columns:
                continue
            try:
                references = [(kind, raw) for kind, raw in columns[entity_id].items() if 68 <= kind <= 74]
                pairs = tuple(pair for _kind, raw in references for pair in decode_pairs(raw, paths))
                missing = [kind - 60 for kind, _raw in references if kind - 60 not in columns[entity_id]]
                if missing:
                    raise ValueError(f"Renderer references lack matching runtime draw components: components={missing}")
                draws = tuple(draw for kind, raw in references for draw in decode_draws(columns[entity_id][kind - 60], raw))
            except ValueError as error:
                failures.append({"entity_id": entity_id, "base": base, "source": source, "error": str(error)})
                continue
            if pairs:
                signatures[base][(pairs, draws)] += 1
        print(json.dumps({"event": "renderer_chunk_decoded", "source": source, "instances": len(instances)}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"format": "EndfieldCurrentRendererMaterials/1", "bounds": args.bounds,
        "source_sha256": evidence, "path_hash_sha256": hashlib.sha256(args.path_hash.read_bytes()).hexdigest(),
        "runtime_draw_contract": "RendererInfo24_and_LODInfo8_v29_type92_verified",
        "bases": {base: [{"count": count, "pairs": pairs, "draws": [draw._asdict() for draw in draws]}
                          for (pairs, draws), count in variants.items()] for base, variants in signatures.items()},
        "failures": failures}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
