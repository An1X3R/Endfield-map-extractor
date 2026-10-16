"""Describe scene workloads and bound admission with verified process measurements."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import math
from pathlib import Path
import struct
import sys
from typing import cast

from endfield_scene_io import read_scene_manifest
from endfield_scene_material_contract import load_catalog, reference_key, require_object, resolve_export_path


_LOGGER = logging.getLogger(__name__)
_CACHE_FORMAT = "EndfieldSceneMemoryCalibration/1"
_MAX_CALIBRATIONS = 32
_MODEL_VERSION = "scene-feature-envelope/1"
_CATALOG_FORMAT = "EndfieldSceneMemoryCatalog/1"
_CATALOG_MAX_BYTES = 32 * 1024 ** 2


@dataclass(frozen=True)
class SceneMemoryWorkload:
    input_signature: str
    features: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class SceneMemoryCalibration:
    tool_signature: str
    workload: SceneMemoryWorkload
    peak_working_set_bytes: int
    peak_commit_bytes: int
    reopen_verified: bool


@dataclass(frozen=True)
class SceneMemoryEstimate:
    budget_bytes: int
    calibration_count: int
    calibrated: bool
    extrapolation_factor: float
    basis_input_signature: str


@dataclass(frozen=True)
class _MaterialTextureBinding:
    material_key: tuple[str, int]
    path: Path
    property_name: str


@dataclass(frozen=True)
class _ManifestFootprint:
    stamp: tuple[str, int, int]
    heap_bytes: int
    references: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class SceneMemoryCatalog:
    source_stamps: tuple[tuple[str, int, int], ...]
    heap_bytes: int
    parse_peak_bytes: int
    bindings: tuple[_MaterialTextureBinding, ...]
    manifests: tuple[_ManifestFootprint, ...]


def _children(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield child
    elif isinstance(value, (list, tuple)):
        yield from value


def _heap_bytes(value: object) -> int:
    """Count Python object storage once per identity without copying input trees."""
    seen: set[int] = set()
    pending = [value]
    total = 0
    while pending:
        item = pending.pop()
        if id(item) not in seen:
            seen.add(id(item))
            total += sys.getsizeof(item)
            pending.extend(_children(item))
    return total


def _objects(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        row = require_object(value, "scene memory record")
        yield row
        for child in row.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _references(value: object) -> set[tuple[str, int]]:
    return {reference_key(row) for row in _objects(value)
            if isinstance(row.get("Source"), str) and type(row.get("PathId")) is int}


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _array(data: dict[str, object], field: str) -> list[object]:
    value = data.get(field)
    if not isinstance(value, list):
        raise ValueError(f"Scene memory workload requires an array: field={field}")
    return cast(list[object], value)


def _file_stamp(path: Path) -> tuple[str, int, int]:
    resolved = resolve_export_path(path)
    stamp = resolved.stat()
    return str(resolved), stamp.st_size, stamp.st_mtime_ns


def _manifest_footprint(stamp: tuple[str, int, int]) -> _ManifestFootprint:
    path = Path(stamp[0])
    manifest = read_scene_manifest(path)
    footprint = _heap_bytes(manifest)
    references = tuple(sorted(_references(manifest)))
    if _file_stamp(path) != stamp:
        raise OSError(f"Scene memory manifest changed during measurement: path={path}")
    return _ManifestFootprint(stamp, footprint, references)


def scene_memory_catalog(stamps: tuple[tuple[str, int, int], ...]) -> SceneMemoryCatalog:
    """Retain only compact bindings and counts, never a second parsed catalog tree."""
    paths = tuple(Path(stamp[0]) for stamp in stamps)
    materials, textures = load_catalog(paths)
    heap_bytes = _heap_bytes((materials, textures))
    # load_catalog already returns canonical payload paths, including Windows long paths.
    resolved_files = {(root, relative): root / relative for root, relative in set(textures.values())}
    bindings = tuple(_MaterialTextureBinding(key, resolved_files[(root, relative)], slot["Property"])
                     for key, material in materials.items() for slot in material["Textures"]
                     if slot["Texture"] is not None and reference_key(slot["Texture"]) in textures
                     for root, relative in (textures[reference_key(slot["Texture"])],))
    manifests = tuple(_manifest_footprint(stamp) for stamp in stamps)
    parse_peak_bytes = max((row.heap_bytes + row.stamp[1] for row in manifests), default=0)
    if tuple(_file_stamp(path) for path in paths) != stamps:
        raise OSError("Scene memory catalogs changed during measurement")
    return SceneMemoryCatalog(stamps, heap_bytes, parse_peak_bytes, bindings, manifests)


def scene_memory_catalog_key(plan: Path) -> tuple[tuple[str, int, int], ...]:
    """Return exact manifest identities for an export-owned descriptor cache."""
    data = require_object(json.loads(plan.read_text(encoding="utf-8-sig")), "scene memory catalog plan")
    stamps: list[tuple[str, int, int]] = []
    for value in _array(data, "scene_manifests"):
        if not isinstance(value, str) or not value:
            raise ValueError("Scene memory manifests require nonempty paths")
        stamps.append(_file_stamp(Path(value)))
    return tuple(stamps)


def _stamp_record(value: object) -> tuple[str, int, int]:
    if not isinstance(value, list) or len(value) != 3 or not isinstance(value[0], str) or not value[0]:
        raise ValueError("Memory catalog source identity requires [path, size, modified_ns]")
    return value[0], _positive_integer(value[1], "source size"), _positive_integer(value[2], "source modified_ns")


def _reference_record(value: object) -> tuple[str, int]:
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str) or not value[0] or type(value[1]) is not int:
        raise ValueError("Memory catalog reference requires [source, integer path_id]")
    return value[0], value[1]


def _read_catalog_record(data: dict[str, object], stamps: tuple[tuple[str, int, int], ...]) -> SceneMemoryCatalog:
    files: list[Path] = []
    for value in _array(data, "files"):
        if not isinstance(value, str) or not value:
            raise ValueError("Memory catalog file requires a nonempty path")
        files.append(Path(value))
    bindings: list[_MaterialTextureBinding] = []
    for value in _array(data, "bindings"):
        if not isinstance(value, list) or len(value) != 4 or not all(isinstance(value[index], str) and value[index] for index in (0, 3)) or type(value[1]) is not int or type(value[2]) is not int or not 0 <= value[2] < len(files):
            raise ValueError("Memory catalog binding requires [source, path_id, valid file index, property]")
        bindings.append(_MaterialTextureBinding((value[0], value[1]), files[value[2]], value[3]))
    manifests: list[_ManifestFootprint] = []
    for value in _array(data, "manifests"):
        row = require_object(value, "memory catalog manifest")
        manifests.append(_ManifestFootprint(_stamp_record(row.get("stamp")),
                         _positive_integer(row.get("heap_bytes"), "manifest heap_bytes"),
                         tuple(_reference_record(reference) for reference in _array(row, "references"))))
    if tuple(row.stamp for row in manifests) != stamps:
        raise ValueError("Memory catalog manifest identities disagree with source identities")
    return SceneMemoryCatalog(stamps, _positive_integer(data.get("heap_bytes"), "catalog heap_bytes"),
                              _positive_integer(data.get("parse_peak_bytes"), "catalog parse_peak_bytes"),
                              tuple(bindings), tuple(manifests))


def cached_scene_memory_catalog(cache_root: Path, stamps: tuple[tuple[str, int, int], ...],
                                tool_signature: str) -> SceneMemoryCatalog:
    """Reuse compact descriptors across exports in four explicitly owned cache slots."""
    key = hashlib.sha256(json.dumps([tool_signature, stamps]).encode()).hexdigest()
    directory = cache_root / "scene_memory_catalogs"
    path = directory / f"catalog_slot_{int(key, 16) % 4}.json"
    if path.exists():
        if path.stat().st_size > _CATALOG_MAX_BYTES:
            raise ValueError(f"Memory catalog cache exceeds 32 MiB: path={path}")
        data = require_object(json.loads(path.read_text(encoding="utf-8")), "memory catalog cache")
        if data.get("format") != _CATALOG_FORMAT:
            raise ValueError(f"Unsupported memory catalog cache format: path={path}, format={data.get('format')!r}")
        if data.get("key") == key and data.get("tool_signature") == tool_signature:
            recorded = tuple(_stamp_record(value) for value in _array(data, "source_stamps"))
            if recorded != stamps:
                raise ValueError(f"Memory catalog cache key conflicts with source identities: path={path}")
            return _read_catalog_record(data, stamps)
    catalog = scene_memory_catalog(stamps)
    files = tuple(sorted({row.path for row in catalog.bindings}))
    file_indices = {path: index for index, path in enumerate(files)}
    payload = json.dumps({"format": _CATALOG_FORMAT, "key": key, "tool_signature": tool_signature,
                          "source_stamps": catalog.source_stamps, "heap_bytes": catalog.heap_bytes,
                          "parse_peak_bytes": catalog.parse_peak_bytes,
                          "files": [str(path) for path in files],
                          "bindings": [(row.material_key[0], row.material_key[1], file_indices[row.path], row.property_name)
                                       for row in catalog.bindings],
                          "manifests": [{"stamp": row.stamp, "heap_bytes": row.heap_bytes, "references": row.references}
                                        for row in catalog.manifests]}, separators=(",", ":"))
    payload_bytes = len(payload.encode("utf-8"))
    if payload_bytes > _CATALOG_MAX_BYTES:
        raise ValueError(f"Measured memory catalog descriptor exceeds 32 MiB cache limit: bytes={payload_bytes}, sources={len(stamps)}, bindings={len(catalog.bindings)}")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    temporary.replace(path)
    return catalog


def _png_float_rgba_bytes(path: Path) -> int:
    """Read dimensions only; RGBA float32 is a workload unit, not measured Blender use."""
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"Scene memory texture must have a PNG IHDR header: path={path}")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise ValueError(f"Scene memory texture dimensions are invalid: path={path}, width={width}, height={height}")
    return width * height * 4 * 4


def scene_memory_workload(plan: Path, catalog: SceneMemoryCatalog) -> SceneMemoryWorkload:
    """Inspect selected resources, source variants and actual Python catalog storage."""
    data = {"prefab_geometry_instances": [], **require_object(json.loads(plan.read_text(encoding="utf-8-sig")), "scene memory plan")}
    if data.get("format") != "EndfieldPortableScenePlan/1":
        raise ValueError(f"Unsupported scene memory plan: path={plan}, format={data.get('format')!r}")
    dependencies: set[Path] = {resolve_export_path(plan)}
    paths: list[Path] = []
    for value in _array(data, "scene_manifests"):
        if not isinstance(value, str) or not value:
            raise ValueError("Scene memory manifests require nonempty paths")
        paths.append(resolve_export_path(Path(value)))
    dependencies.update(paths)
    if tuple(_file_stamp(path) for path in paths) != catalog.source_stamps:
        raise ValueError(f"Scene memory catalog descriptor does not match current manifests: plan={plan}")
    features = {"plan_heap_bytes": _heap_bytes(data), "catalog_heap_bytes": catalog.heap_bytes,
                "catalog_parse_peak_bytes": catalog.parse_peak_bytes, "mesh_variant_source_bytes": 0, "mesh_variant_count": 0,
                "instance_count": 0, "texture_float_rgba_bytes": 0, "texture_packed_bytes": 0,
                "texture_file_count": 0, "texture_role_count": 0, "terrain_input_bytes": 0,
                "terrain_tile_count": 0, "effect_manifest_heap_bytes": 0, "effect_count": 0,
                "water_count": len(_array(data, "water_geometry")),
                "light_count": len(_array(data, "light_instances")), "decal_count": 0,
                "terrain_surface_count": len(_array(data, "terrain_surfaces")),
                "depth_terrain_surface_count": len(_array(data, "water_depth_surfaces")),
                "dynamic_geometry_count": len(_array(data, "dynamic_geometry_instances")),
                "prefab_geometry_count": len(_array(data, "prefab_geometry_instances")),
                "surface_effect_count": len(_array(data, "surface_effect_instances")),
                "direct_effect_count": len(_array(data, "direct_effect_renderers")),
                "uv_source_bytes": 0}
    selected_refs = _references(data)
    for field in ("instances", "dynamic_geometry_instances", "prefab_geometry_instances", "water_depth_instances", "surface_effect_instances"):
        variants: dict[str, Path] = {}
        rows = _array(data, field)
        features["instance_count"] += len(rows)
        for value in rows:
            row = require_object(value, f"scene memory {field}")
            mesh_file = row.get("mesh_file")
            if not isinstance(mesh_file, str) or not mesh_file:
                raise ValueError(f"Scene memory instance requires mesh_file: field={field}")
            refs = _array(row, "material_refs")
            identity = json.dumps([mesh_file, refs, row.get("selected_submeshes", list(range(len(refs)))),
                                   row.get("source_uv_streams", [])], sort_keys=True)
            variants[identity] = resolve_export_path(Path(mesh_file))
        features["mesh_variant_count"] += len(variants)
        features["mesh_variant_source_bytes"] += sum(path.stat().st_size for path in variants.values())
        dependencies.update(variants.values())
        uv_paths = {resolve_export_path(Path(cast(str, record["file"])))
                    for value in rows for record in _objects(require_object(value, field).get("source_uv_streams", []))
                    if isinstance(record.get("file"), str)}
        dependencies.update(uv_paths)
        features["uv_source_bytes"] += sum(path.stat().st_size for path in uv_paths)
    effect_paths: set[Path] = set()
    for field in ("effect_instances", "direct_effect_renderers"):
        rows = _array(data, field)
        features["effect_count"] += len(rows)
        for value in rows:
            row = require_object(value, f"scene memory {field}")
            if isinstance(row.get("scene_manifest"), str):
                effect_paths.add(resolve_export_path(Path(cast(str, row["scene_manifest"]))))
    png_paths: set[Path] = set()
    manifest_rows = {row.stamp[0]: row for row in catalog.manifests}
    for path in sorted(effect_paths):
        footprint = manifest_rows.get(str(path))
        if footprint is None:
            footprint = _manifest_footprint(_file_stamp(path))
        features["effect_manifest_heap_bytes"] += footprint.heap_bytes
        # FX manifests contain selected prefab renderer material references.
        selected_refs.update(footprint.references)
    dependencies.update(effect_paths)
    role_keys: set[tuple[Path, str]] = set()
    for binding in catalog.bindings:
        if binding.material_key in selected_refs:
            png_paths.add(binding.path)
            role_keys.add((binding.path, binding.property_name))
    features["texture_role_count"] = len(role_keys)
    metadata_paths: set[Path] = set()
    for field in ("decal_manifest", "decal_materials"):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Scene memory plan requires a path: field={field}")
        metadata_paths.add(resolve_export_path(Path(value)))
    for field in ("terrain_surfaces", "water_depth_surfaces"):
        for value in _array(data, field):
            if not isinstance(value, str):
                raise ValueError(f"Terrain memory input requires a path: field={field}")
            metadata_paths.add(resolve_export_path(Path(value) / "terrain_surface.json"))
    for value in _array(data, "terrain_details"):
        if not isinstance(value, str):
            raise ValueError("Terrain detail memory input requires a path")
        metadata_paths.add(resolve_export_path(Path(value)))
    for path in sorted(metadata_paths):
        metadata = require_object(json.loads(path.read_text(encoding="utf-8-sig")), f"memory metadata {path}")
        dependencies.add(path)
        if metadata.get("format") == "EndfieldTerrainDetail/1":
            tiles = metadata.get("tiles")
            if not isinstance(tiles, list):
                raise ValueError(f"Terrain detail memory metadata requires tiles: path={path}")
            features["terrain_tile_count"] += len(tiles)
        if path == resolve_export_path(Path(cast(str, data["decal_manifest"]))):
            for key in ("decals", "Decals", "instances", "records"):
                if isinstance(metadata.get(key), list):
                    features["decal_count"] += len(cast(list[object], metadata[key]))
        for value in _strings(metadata):
            suffix = Path(value).suffix.casefold()
            if suffix in (".png", ".sqlite"):
                source = resolve_export_path(path.parent / value.replace("\\", "/"))
                dependencies.add(source)
                if suffix == ".png":
                    png_paths.add(source)
                else:
                    features["terrain_input_bytes"] += source.stat().st_size
    for value in _strings(data.get("water_materials")):
        if Path(value).suffix.casefold() == ".png":
            png_paths.add(resolve_export_path(Path(value)))
    dependencies.update(png_paths)
    features["texture_float_rgba_bytes"] = sum(_png_float_rgba_bytes(path) for path in png_paths)
    features["texture_packed_bytes"] = sum(path.stat().st_size for path in png_paths)
    features["texture_file_count"] = len(png_paths)
    stamps = [_file_stamp(path) for path in sorted(dependencies)]
    plan_digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    signature = hashlib.sha256(json.dumps([_MODEL_VERSION, plan_digest, stamps], sort_keys=True).encode()).hexdigest()
    return SceneMemoryWorkload(signature, tuple(sorted(features.items())))


def scene_memory_tool_signature(blender_exe: Path) -> str:
    """Invalidate calibration on executable, Python ABI or Blender adapter changes."""
    digest = hashlib.sha256()
    digest.update(json.dumps([_MODEL_VERSION, sys.version, _file_stamp(blender_exe)]).encode())
    root = Path(__file__).parent
    paths = sorted({*root.glob("blender_*.py"), *root.glob("endfield_scene_*.py"), root / "endfield_shader_contract.py"})
    for path in paths:
        if path.name == "endfield_scene_export.py":
            continue
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def scene_memory_estimate(workload: SceneMemoryWorkload, calibrations: tuple[SceneMemoryCalibration, ...],
                          tool_signature: str) -> SceneMemoryEstimate:
    """Use observed feature envelopes; unobserved positive features require a solo build."""
    candidates: list[tuple[int, float, str]] = []
    features = dict(workload.features)
    matching = tuple(row for row in merge_scene_memory_calibrations(calibrations)
                     if row.tool_signature == tool_signature and row.reopen_verified)
    for row in matching:
        observed = dict(row.workload.features)
        if set(observed) != set(features) or any(value > 0 and observed[name] == 0 for name, value in features.items()):
            continue
        factor = max((1.0, *(value / observed[name] for name, value in features.items() if observed[name] > 0)))
        peak = max(row.peak_working_set_bytes, row.peak_commit_bytes)
        if peak <= 0:
            raise ValueError(f"Verified calibration requires positive process peaks: input={row.workload.input_signature}")
        scaled = math.ceil(peak * factor)
        # This declared operational margin is a policy, not a fitted accuracy claim.
        margin = max(256 * 1024 ** 2, math.ceil(scaled / 4))
        candidates.append((scaled + margin, factor, row.workload.input_signature))
    if not candidates:
        return SceneMemoryEstimate(0, len(matching), False, 0.0, "")
    budget, factor, basis = min(candidates)
    exact = tuple(row for row in matching if row.workload.input_signature == workload.input_signature)
    if exact:
        peak = max(max(row.peak_working_set_bytes, row.peak_commit_bytes) for row in exact)
        floor = peak + max(256 * 1024 ** 2, math.ceil(peak / 4))
        if floor > budget:
            budget, factor, basis = floor, 1.0, workload.input_signature
    return SceneMemoryEstimate(budget, len(matching), True, factor, basis)


def _positive_integer(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"Memory calibration requires a nonnegative integer: field={context}, value={value!r}")
    return cast(int, value)


def _calibration_record(value: object) -> SceneMemoryCalibration:
    row = require_object(value, "memory calibration")
    workload = require_object(row.get("workload"), "memory calibration workload")
    for field, record in (("tool_signature", row), ("input_signature", workload)):
        if not isinstance(record.get(field), str) or len(cast(str, record[field])) != 64:
            raise ValueError(f"Memory calibration requires a SHA256 identity: field={field}")
    if row.get("reopen_verified") is not True:
        raise ValueError("Memory calibration must contain a verified save/reopen measurement")
    features = workload.get("features")
    if not isinstance(features, list):
        raise ValueError("Memory calibration features require an array")
    pairs: list[tuple[str, int]] = []
    for value in features:
        if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str):
            raise ValueError("Memory calibration feature requires [name, nonnegative integer]")
        pairs.append((value[0], _positive_integer(value[1], value[0])))
    if len({name for name, _ in pairs}) != len(pairs):
        raise ValueError("Memory calibration contains duplicate features")
    working = _positive_integer(row.get("peak_working_set_bytes"), "peak_working_set_bytes")
    commit = _positive_integer(row.get("peak_commit_bytes"), "peak_commit_bytes")
    if max(working, commit) <= 0:
        raise ValueError("Memory calibration process peak must be positive")
    return SceneMemoryCalibration(cast(str, row["tool_signature"]),
                                  SceneMemoryWorkload(cast(str, workload["input_signature"]), tuple(pairs)),
                                  working, commit, True)


def read_scene_memory_calibrations(path: Path, tool_signature: str) -> tuple[SceneMemoryCalibration, ...]:
    """Load bounded verified observations and explicitly report incompatible tools."""
    if not path.exists():
        return ()
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(f"Memory calibration cache exceeds 1 MiB: path={path}")
    data = require_object(json.loads(path.read_text(encoding="utf-8")), "memory calibration cache")
    if data.get("format") != _CACHE_FORMAT:
        raise ValueError(f"Unsupported memory calibration cache: path={path}, format={data.get('format')!r}")
    values = _array(data, "calibrations")
    if len(values) > _MAX_CALIBRATIONS:
        raise ValueError(f"Memory calibration cache exceeds record limit: path={path}, count={len(values)}")
    records = tuple(_calibration_record(value) for value in values)
    matched = tuple(row for row in records if row.tool_signature == tool_signature)
    if len(matched) != len(records):
        _LOGGER.warning("scene_memory_calibration_tool_mismatch", extra={"cache_path": str(path),
                        "rejected_records": len(records) - len(matched), "tool_signature": tool_signature})
    return matched


def write_scene_memory_calibrations(path: Path, calibrations: tuple[SceneMemoryCalibration, ...]) -> None:
    """Atomically replace a bounded cache; callers serialize writes for their export."""
    kept = merge_scene_memory_calibrations(calibrations)
    payload = json.dumps({"format": _CACHE_FORMAT, "calibrations": [asdict(row) for row in kept]}, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    temporary.replace(path)


def merge_scene_memory_calibrations(calibrations: tuple[SceneMemoryCalibration, ...]) -> tuple[SceneMemoryCalibration, ...]:
    """Retain componentwise maximum observations per exact input and bound retention."""
    unique: dict[tuple[str, str], SceneMemoryCalibration] = {}
    for row in calibrations:
        _calibration_record(json.loads(json.dumps(asdict(row))))
        key = row.tool_signature, row.workload.input_signature
        previous = unique.pop(key, None)
        if previous is not None:
            if previous.workload != row.workload:
                raise ValueError(f"Memory calibration feature conflict for the same input: input={row.workload.input_signature}")
            row = SceneMemoryCalibration(row.tool_signature, row.workload,
                                         max(previous.peak_working_set_bytes, row.peak_working_set_bytes),
                                         max(previous.peak_commit_bytes, row.peak_commit_bytes), True)
        unique[key] = row
    return tuple(unique.values())[-_MAX_CALIBRATIONS:]
