"""Reuse local prefab selection results without rebuilding source bounds per export."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import cast

from endfield_scene_material_contract import require_object
from endfield_scene_prefab_bounds import PrefabBoundsSelection, select_prefab_geometry_inventory
from extractor_core.dynamic_models import DynamicModelInventory


def select_cached_prefab_geometry_inventory(
    inventory: DynamicModelInventory, manifests: tuple[Path, ...],
    bounds: tuple[float, float, float, float], selection: tuple[float, float, float, float], cache_root: Path,
) -> PrefabBoundsSelection:
    """Invalidate local derived results by source timestamps, placements and selector code."""
    from endfield_scene_resources import write_index

    source_root = Path(__file__).parent
    identity: dict[str, object] = {
        "scene": inventory.scene_id, "sources": [asdict(source) for source in inventory.sources],
        "placements": [asdict(record) for record in inventory.records],
        "bounds": bounds, "selection": selection,
        "manifests": [(str(path.resolve()), path.stat().st_size, path.stat().st_mtime_ns) for path in manifests],
        "selector": [hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in
                     ("endfield_scene_prefab_selection_cache.py", "endfield_scene_prefab_bounds.py",
                      "endfield_scene_prefab_geometry_plan.py", "endfield_scene_spatial_plan.py",
                      "endfield_scene_effects_plan.py")],
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path = cache_root / "prefab_selections_v1" / f"{key}.json"
    if path.is_file():
        payload = require_object(json.loads(path.read_text(encoding="utf-8")), str(path))
        keys = payload.get("selected")
        pending = payload.get("pending")
        outside, empty = payload.get("outside_count"), payload.get("empty_count")
        if (payload.get("format") != "EndfieldPrefabSelection/1" or payload.get("identity") != key
                or not isinstance(keys, list) or any(not isinstance(item, str) for item in keys)
                or len(set(keys)) != len(keys) or not isinstance(pending, list)
                or type(outside) is not int or outside < 0 or type(empty) is not int or empty < 0):
            raise ValueError(f"Invalid local prefab selection cache: path={path}")
        records = {record.key: record for record in inventory.records}
        if not set(keys).issubset(records):
            raise ValueError(f"Cached prefab placements are absent from current scene: path={path}")
        evidence = {name: require_object(value, f"prefab selection {name}")
                    for name, value in require_object(payload.get("evidence"), str(path)).items()}
        if set(evidence) != set(keys):
            raise ValueError(f"Cached prefab selection evidence differs from selected placements: path={path}")
        return PrefabBoundsSelection(
            DynamicModelInventory(inventory.scene_id, inventory.sources, tuple(records[name] for name in keys)),
            evidence, tuple(require_object(row, "prefab selection pending") for row in pending),
            cast(int, outside), cast(int, empty),
        )
    result = select_prefab_geometry_inventory(inventory, manifests, bounds, selection)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_index(path, {"format": "EndfieldPrefabSelection/1", "identity": key,
                      "selected": [record.key for record in result.inventory.records], "evidence": dict(result.evidence),
                      "pending": list(result.pending), "outside_count": result.outside_count, "empty_count": result.empty_count})
    return result
