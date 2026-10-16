"""Read prepared level maps through the active, portable scene runtime."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re

from endfield_level_maps import LevelMapError, read_level_map_manifest
from endfield_runtime_config import load_json_config
from endfield_scene_material_contract import require_object


class LevelMapsNotPrepared(RuntimeError):
    """The selected runtime does not yet declare exported level map images."""


def prepared_level_map_manifest(runtime_path: Path | None) -> Path:
    """Resolve the declared registry and verify its descriptor before serving it."""
    if runtime_path is None:
        raise LevelMapsNotPrepared("尚未配置场景数据，请先完成场景首次准备。")
    runtime_path = runtime_path.resolve()
    runtime = load_json_config(runtime_path, expected_format="EndfieldRuntimeConfig/1")
    extraction = require_object(runtime.get("extraction"), "runtime.extraction")
    if "levelMaps" not in extraction:
        raise LevelMapsNotPrepared("当前场景配置尚未提取独立区域地图图片。")
    descriptor = require_object(extraction["levelMaps"], "runtime.extraction.levelMaps")
    value = descriptor.get("path")
    digest = descriptor.get("sha256")
    size = descriptor.get("bytes")
    if (not isinstance(value, str) or not value.strip() or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None or type(size) is not int or size < 0):
        raise LevelMapError("Level map descriptor requires path, SHA256 and byte size")
    configured = Path(value)
    manifest = (configured if configured.is_absolute() else runtime_path.parent / configured).resolve()
    payload = manifest.read_bytes()
    if len(payload) != size or sha256(payload).hexdigest() != digest.casefold():
        raise LevelMapError(f"Level map registry differs from its runtime descriptor: {manifest}")
    return manifest


def public_prepared_level_maps(manifest_path: Path) -> dict[str, object]:
    """Expose image selection and coordinate contracts, without local evidence paths."""
    manifest = read_level_map_manifest(manifest_path, manifest_path.parent)
    entries: list[dict[str, object]] = []
    for raw in manifest["entries"]:
        entry = require_object(raw, "level map entry")
        image = require_object(entry["image"], "level map image")
        entries.append({
            **{key: entry[key] for key in ("levelId", "mapId", "artOwner", "worldBounds",
                                          "sourceMapBounds", "selectionGrid", "sourceConfig",
                                          "worldToPixel", "pixelToWorld")},
            "image": {**{key: image[key] for key in ("pixelWidth", "pixelHeight", "bytes", "sha256")},
                      "url": f"/level-maps/{entry['levelId']}/image"},
        })
    return {"format": "EndfieldLevelMaps/1", "status": "prepared",
            "imageValidation": "sha256_on_image_request", "entries": entries}
