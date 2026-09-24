"""Extract bounded projected decals by joining authenticated Init/Streaming chunks."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from extractor_core.projected_decals import (
    build_path_id_index, decode_decal, read_chunk, rect_decal_records, streaming_records,
)
from extractor_core.resources import load_group


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances-db", type=Path, required=True)
    parser.add_argument("--vfs-root", type=Path, required=True)
    parser.add_argument("--path-hash", type=Path, required=True)
    parser.add_argument("--bounds", type=float, nargs=4, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve().is_relative_to(args.vfs_root.resolve()):
        raise FileExistsError(f"DECAL_OUTPUT_NOT_NEW_OR_EXTERNAL path={args.output}")
    with sqlite3.connect(args.instances_db.resolve().as_uri() + "?mode=ro", uri=True) as database:
        rows = database.execute(
            "SELECT entity_id,entity_base,source_path,matrix_col_major FROM instances "
            "WHERE matrix_type=27 AND x>=? AND x<? AND z>=? AND z<? ORDER BY source_path,entity_id",
            args.bounds).fetchall()
    groups: dict[str, list[tuple[int, str, bytes]]] = defaultdict(list)
    for entity_id, base, source, matrix in rows:
        if not isinstance(base, str) or not base:
            raise ValueError(f"DECAL_BASE_MISSING entity_id={entity_id}")
        groups[source].append((entity_id, base, matrix))
    entries = {entry.file_name: entry for entry in load_group(args.vfs_root, "C3442D43").iter_files()}
    paths = build_path_id_index(args.path_hash)
    decoded = []
    evidence: dict[str, str] = {}
    for source, instances in groups.items():
        partner = source.replace("InitChunkData_", "StreamingChunkData_")
        init = read_chunk(args.vfs_root, entries[source])
        stream = read_chunk(args.vfs_root, entries[partner])
        evidence[source] = hashlib.sha256(init).hexdigest()
        evidence[partner] = hashlib.sha256(stream).hexdigest()
        rects, records = rect_decal_records(init), streaming_records(stream)
        for entity_id, base, expected_matrix in instances:
            raw = rects[entity_id]
            if raw[:64] != expected_matrix:
                raise ValueError(f"DECAL_DATABASE_MATRIX_MISMATCH entity_id={entity_id}")
            decoded.append(decode_decal(entity_id, base, source, raw, stream, records[entity_id], paths))
        print(json.dumps({"event": "chunk_decals_decoded", "source": source, "entities": len(instances)}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump({"format": "EndfieldProjectedDecals/1", "instances_db": str(args.instances_db),
                   "path_hash_sha256": hashlib.sha256(args.path_hash.read_bytes()).hexdigest(),
                   "bounds": args.bounds, "source_sha256": evidence,
                   "records": [asdict(row) for row in decoded]}, handle, indent=2)
    print(json.dumps({"event": "decals_complete", "count": len(decoded), "output": str(args.output)}))


if __name__ == "__main__":
    main()
