"""Discover physical instanced scenes and prepare source indexes on demand.

Names and categories are game-table evidence, not geometry acceptance. Preparing
an index does not certify resource closure; the portable export preflight does.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import struct
from typing import Literal, TypedDict, cast
from uuid import uuid4

from endfield_scene_io import SceneInputChangedError, _FileStamp, _file_stamp, scene_io_session
from endfield_scene_material_contract import require_object
from endfield_scene_resources import describe_file, resolve_path
from extractor_core import vfs
from extractor_core.instances import build_instance_database, build_unresolved_asset_database
from extractor_core.resources import load_group
from extractor_core.scene_identity import validate_scene_id
from extractor_core.scene_tables import TableValue, read_scene_table
from extractor_core.scene_string_pool import read_scene_string_pool


class InstancedSceneError(ValueError):
    """An instanced scene lacks validated source evidence or preparation inputs."""


class SceneAvailability(TypedDict):
    ready: bool
    reason: str


class SceneExportCapability(SceneAvailability):
    export_modes: list[Literal["blend"]]


class SceneEvidence(TypedDict):
    table: str
    key: str
    field: str
    value: str
    i18n_id: int | None


class InstancedSceneGroup(TypedDict):
    group_id: str
    name: str
    source_evidence: list[SceneEvidence]


class InstancedSceneSummary(TypedDict):
    scene_id: str
    name: str
    group_id: str
    form: Literal["blueprint"]
    scope: Literal["whole_physical_scene"]
    export_capability: SceneExportCapability
    component_counts: dict[str, int | None]
    logical_level_ids: list[str]
    category_ids: list[str]
    source_evidence: list[SceneEvidence]
    limitations: list[str]


class InstancedSceneCatalog(TypedDict):
    format: Literal["InstancedSceneCatalog/1"]
    catalog_id: str
    source_identity: str
    availability: SceneAvailability
    groups: list[InstancedSceneGroup]
    scenes: list[InstancedSceneSummary]
    limitations: list[str]


class InstancedSceneBounds(TypedDict):
    xmin: float
    xmax: float
    zmin: float
    zmax: float


@dataclass(frozen=True)
class PreparedInstancedScene:
    runtime_path: Path
    bounds: InstancedSceneBounds
    instances_path: Path
    assets_path: Path
    source_identity: str
    component_counts: Mapping[str, int | None]
    limitations: tuple[str, ...]
    resource_status: Literal["deferred_exact_preflight"]


_TABLE_GROUP = "42A8FCA6"
_JSON_GROUP = "775A31D1"
_STATIC_GROUP = "C3442D43"
_DYNAMIC_GROUP = "23D53F5D"
_COMPONENTS = ("building", "prop", "lighting", "particle", "vegetation", "terrain", "road", "water", "effects", "unknown")
_BINDING_LIMIT = "Dungeon sceneId takes precedence over levelId; this table relationship is not a live gameplay activation check."
_SCOPE_LIMIT = "Names identify whole physical resource scenes; no interior subregion or rendered completeness has been certified."


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise InstancedSceneError(f"Expected scene source string: field={context}, value={value!r}")
    return value


def _source_path(value: object, parent: Path, context: str) -> Path:
    path = resolve_path(value, parent)
    if not path.is_file():
        raise FileNotFoundError(f"Required instanced scene input is missing: field={context}, path={path}")
    return path


def _runtime(runtime_path: Path) -> tuple[dict[str, object], Path]:
    runtime = require_object(json.loads(runtime_path.read_text(encoding="utf-8-sig")), str(runtime_path))
    if runtime.get("format") != "EndfieldRuntimeConfig/1":
        raise InstancedSceneError(f"Unsupported instanced scene runtime: path={runtime_path}")
    paths = require_object(runtime.get("paths"), "runtime paths")
    game = resolve_path(paths.get("game_root"), runtime_path.parent)
    for item in (game / "Endfield.exe", game / "Endfield_Data" / "globalgamemanagers"):
        if not item.is_file():
            raise FileNotFoundError(f"Instanced scene game installation is incomplete: path={item}")
    return runtime, game / "Endfield_Data" / "StreamingAssets" / "VFS"


def _first_run_inputs(path: Path) -> tuple[Path, dict[str, object], Path]:
    state_path = path.parent / "first_run_state.json"
    state = require_object(json.loads(state_path.read_text(encoding="utf-8-sig")), str(state_path))
    if state.get("format") != "EndfieldFirstRunState/1" or state.get("status") != "completed":
        raise InstancedSceneError(f"Instanced scenes require completed first-run state: path={state_path}")
    bootstrap = require_object(require_object(require_object(state.get("steps"), "first-run steps").get("bootstrap_metadata"),
                                             "bootstrap metadata").get("outputs"), "bootstrap outputs")
    path_hash = _source_path(require_object(bootstrap.get("main_paths"), "main_paths").get("path"), path.parent, "StringPathHash")
    return state_path, state, path_hash


def _shared_probes(path: Path, runtime: Mapping[str, object]) -> tuple[Path, ...]:
    extraction = require_object(runtime.get("extraction"), "runtime extraction")
    maps = require_object(extraction.get("maps"), "runtime maps")
    probes: list[Path] = []
    for name, value in maps.items():
        row = require_object(value, f"runtime map {name}")
        if row.get("sceneProbe") is not None:
            descriptor = require_object(row["sceneProbe"], f"runtime sceneProbe {name}")
            probes.append(_source_path(descriptor.get("path"), path.parent, f"sceneProbe {name}"))
    if not probes:
        raise InstancedSceneError("Instanced scenes need a first-run shared SceneProbe resource catalog")
    return tuple(dict.fromkeys(probes))


def _group_sources(root: Path, groups: tuple[str, ...]) -> tuple[Path, ...]:
    return tuple(path for name in groups for path in
                 (root / name / f"{name}.blc", *sorted((root / name).glob("*.chk"))))


def _virtual_files(group: vfs.VfsGroup) -> dict[str, vfs.VfsFile]:
    result: dict[str, vfs.VfsFile] = {}
    for item in group.iter_files():
        key = item.file_name.casefold()
        if key in result:
            raise InstancedSceneError(f"Ambiguous VFS virtual path: path={item.file_name}")
        result[key] = item
    return result


def _payload(root: Path, group_hash: str, group: vfs.VfsGroup,
             files: Mapping[str, vfs.VfsFile], name: str) -> bytes:
    item = files.get(name.casefold())
    if item is None:
        raise InstancedSceneError(f"Required scene directory source is absent: group={group_hash}, path={name}")
    output = BytesIO()
    with (root / group_hash / f"{item.file_chunk_md5_name}.chk").open("rb") as stream:
        digest = vfs.copy_virtual_file(stream, output, item, group.version, bytes.fromhex(vfs.DEFAULT_VFS_KEY_HEX))
    if digest != item.file_data_md5.upper():
        raise InstancedSceneError(f"Scene directory source checksum mismatch: path={name}, expected={item.file_data_md5}, actual={digest}")
    return output.getvalue()


def _chunk_source_evidence(root: Path, group_hash: str, items: tuple[vfs.VfsFile, ...],
                           stamps: Mapping[Path, _FileStamp]) -> list[dict[str, object]]:
    """Bind prepared-scene identity to VFS entries and already-collected CHK stamps."""
    result: list[dict[str, object]] = []
    for item in sorted(items, key=lambda row: row.file_name.casefold()):
        source = root / group_hash / f"{item.file_chunk_md5_name}.chk"
        stamp = stamps.get(source)
        if stamp is None:
            raise InstancedSceneError(f"Missing source stamp for instanced chunk: path={source}")
        result.append({"virtual_path": item.file_name, "chunk": item.file_chunk_md5_name,
                       "data_md5": item.file_data_md5.upper(), "offset": item.offset,
                       "length": item.length, "chunk_stamp": asdict(stamp)})
    return result


def _art_owner(data: bytes, path: str, strings: Mapping[int, str] | None) -> str:
    """Read an explicitly selected inline or pooled LevelConfig art reference."""
    if len(data) < 6:
        raise InstancedSceneError(f"Truncated LevelConfig art reference: path={path}, bytes={len(data)}")
    size = struct.unpack_from("<i", data, 2)[0]
    if strings is None:
        if size < 1 or size > len(data) - 6:
            raise InstancedSceneError(f"Invalid LevelConfig art reference length: path={path}, length={size}")
        reference = data[6:6 + size].decode("utf-8")
    else:
        if size not in strings:
            raise InstancedSceneError(f"LevelConfig art string ID is absent: path={path}, string_id={size}")
        reference = strings[size]
    reference = reference.replace("\\", "/")
    parts = PurePosixPath(reference).parts
    if len(parts) != 2 or parts[1].casefold() not in (f"{parts[0]}_art_streaming.asset".casefold(), f"{parts[0]}_streaming.asset".casefold()):
        raise InstancedSceneError(f"Unsupported LevelConfig art reference: path={path}, reference={reference!r}")
    return parts[0] if parts[0].casefold().startswith("devonly") else validate_scene_id(parts[0])


def _localized(value: object, context: str, texts: Mapping[str, TableValue]) -> tuple[str | None, int]:
    reference = require_object(value, context)
    identifier = reference.get("id")
    if type(identifier) is not int:
        raise InstancedSceneError(f"Invalid localized scene name ID: field={context}, id={identifier!r}")
    _text(reference.get("text"), f"{context}.text")
    if identifier == 0:
        return None, identifier
    name = texts.get(str(identifier))
    if not isinstance(name, str):
        raise InstancedSceneError(f"Localized scene name is missing: field={context}, i18n_id={identifier}")
    return name or None, identifier


def _evidence(table: str, key: str, field: str, value: str, identifier: int | None) -> SceneEvidence:
    return {"table": table, "key": key, "field": field, "value": value, "i18n_id": identifier}


def _assemble_catalog(owners: Mapping[str, str], roots: Mapping[str, int],
                      descriptions: Mapping[str, TableValue], dungeons: Mapping[str, TableValue],
                      types: Mapping[str, TableValue], texts: Mapping[str, TableValue],
                      evidence: Mapping[str, list[SceneEvidence]], identity: str) -> InstancedSceneCatalog:
    """Deduplicate physical roots while preserving logical names and categories."""
    independent = {owner for owner in (*owners.values(), *roots)
                   if re.fullmatch(r"map[0-9]+", owner) is None and not owner.casefold().startswith("devonly")}
    levels = {scene: sorted(key for key, owner in owners.items() if owner == scene) for scene in independent}
    source_rows: dict[str, list[SceneEvidence]] = {scene: list(evidence.get(scene, ())) for scene in independent}
    level_names: dict[str, set[str]] = {scene: set() for scene in independent}
    dungeon_names: dict[str, set[str]] = {scene: set() for scene in independent}
    categories: dict[str, set[str]] = {scene: set() for scene in independent}
    groups: dict[str, InstancedSceneGroup] = {}
    for level, owner in owners.items():
        if owner not in independent or level not in descriptions:
            continue
        row = require_object(descriptions[level], f"LevelDescTable[{level}]")
        name, identifier = _localized(row.get("showName"), f"LevelDescTable[{level}].showName", texts)
        if name:
            level_names[owner].add(name)
            source_rows[owner].append(_evidence("LevelDescTable", level, "showName", name, identifier))
    for key, value in dungeons.items():
        row = require_object(value, f"DungeonTable[{key}]")
        raw_scene = _text(row.get("sceneId"), f"DungeonTable[{key}].sceneId")
        raw_level = _text(row.get("levelId"), f"DungeonTable[{key}].levelId")
        selected = raw_scene or raw_level
        owner = selected if selected in independent else owners.get(selected)
        if owner not in independent:
            continue
        category = _text(row.get("dungeonCategory"), f"DungeonTable[{key}].dungeonCategory")
        if category not in types:
            raise InstancedSceneError(f"Dungeon category source is missing: dungeon={key}, category={category}")
        name, identifier = _localized(row.get("dungeonName"), f"DungeonTable[{key}].dungeonName", texts)
        categories[owner].add(category)
        source_rows[owner].append(_evidence("DungeonTable", key, "sceneId" if raw_scene else "levelId", selected, None))
        if name:
            dungeon_names[owner].add(name)
            source_rows[owner].append(_evidence("DungeonTable", key, "dungeonName", name, identifier))
        if category not in groups:
            type_row = require_object(types[category], f"DungeonTypeTable[{category}]")
            title, title_id = _localized(type_row.get("enemyInfoTitle"), f"DungeonTypeTable[{category}].enemyInfoTitle", texts)
            groups[category] = {"group_id": category, "name": title or category,
                                "source_evidence": [_evidence("DungeonTypeTable", category, "enemyInfoTitle", title or category, title_id)]}
    scenes: list[InstancedSceneSummary] = []
    for scene in sorted(independent):
        names = level_names[scene] or dungeon_names[scene]
        names_sorted = sorted(names)
        name = names_sorted[0] if len(names_sorted) == 1 else scene
        category_ids = sorted(categories[scene])
        group_id = category_ids[0] if len(category_ids) == 1 else "multiple_categories" if category_ids else "unclassified"
        if group_id not in groups:
            groups[group_id] = {"group_id": group_id, "name": "多类别物理场景" if category_ids else "未分类物理场景", "source_evidence": []}
        available = bool(levels[scene]) and roots.get(scene, 0) > 0
        reason = "" if available else "Physical scene has no logical registration or no supported InitChunkData sources."
        limits = [_SCOPE_LIMIT]
        if len(names_sorted) > 1:
            limits.append("Conflicting source names are retained as evidence; the physical ID is displayed.")
        scenes.append({"scene_id": scene, "name": name, "group_id": group_id, "form": "blueprint",
                       "scope": "whole_physical_scene", "export_capability": {"ready": available, "reason": reason,
                       "export_modes": ["blend"] if available else []},
                       "component_counts": {key: None for key in _COMPONENTS}, "logical_level_ids": levels[scene],
                       "category_ids": category_ids, "source_evidence": source_rows[scene], "limitations": limits})
    labels = [row["name"] for row in groups.values()]
    unique_groups = [{**row, "name": f"{row['name']} ({row['group_id']})" if labels.count(row["name"]) > 1 else row["name"]}
                     for _, row in sorted(groups.items())]
    return {"format": "InstancedSceneCatalog/1", "catalog_id": identity, "source_identity": identity,
            "availability": {"ready": True, "reason": ""}, "groups": unique_groups,
            "scenes": scenes, "limitations": [_BINDING_LIMIT, _SCOPE_LIMIT]}


def _read_instanced_catalog(runtime_path: Path, check_cancelled: Callable[[], None]) -> InstancedSceneCatalog:
    """Read current game tables; never write game files or research directories."""
    check_cancelled()
    path = runtime_path.expanduser().resolve()
    runtime, root = _runtime(path)
    extraction = require_object(runtime.get("extraction"), "runtime extraction")
    fingerprint = _text(extraction.get("mapLayerFingerprint"), "runtime mapLayerFingerprint")
    if re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) is None:
        raise InstancedSceneError("Instanced scene catalog requires a prepared mapLayerFingerprint SHA256")
    group_names = (_JSON_GROUP, _TABLE_GROUP, _STATIC_GROUP, _DYNAMIC_GROUP)
    sources = _group_sources(root, group_names)
    before = tuple(_file_stamp(source) for source in sources)
    source_stamps = dict(zip(sources, before, strict=True))
    loaded = {name: load_group(root, name) for name in group_names}
    files = {name: _virtual_files(group) for name, group in loaded.items()}
    payload_hashes: dict[str, str] = {}

    def read(name: str, group_name: str) -> bytes:
        check_cancelled()
        data = _payload(root, group_name, loaded[group_name], files[group_name], name)
        payload_hashes[name] = hashlib.sha256(data).hexdigest()
        return data

    strings = (read_scene_string_pool(lambda name: read(name, _JSON_GROUP))
               if "data/json/stringpool/stringpool0.byte" in files[_JSON_GROUP] else None)
    basic_path = "Data/Json/GameplayConfig/LevelBasicInfoTable.json"
    basic = require_object(json.loads(read(basic_path, _JSON_GROUP)), basic_path)
    owners: dict[str, str] = {}
    evidence: dict[str, list[SceneEvidence]] = {}
    for level, value in basic.items():
        if not level.casefold().startswith("devonly"):
            validate_scene_id(level)
        row = require_object(value, f"LevelBasicInfoTable[{level}]")
        config_path = _text(row.get("configPath"), f"LevelBasicInfoTable[{level}].configPath")
        if not config_path.casefold().startswith("data/json/levelconfig/") or ".." in PurePosixPath(config_path).parts:
            raise InstancedSceneError(f"Invalid scene LevelConfig source path: level={level}, path={config_path}")
        owner = _art_owner(read(config_path, _JSON_GROUP), config_path, strings)
        owners[level] = owner
        evidence.setdefault(owner, []).append(_evidence("LevelBasicInfoTable", level, "configPath", config_path, None))
    tables = {name: read_scene_table(read(f"Data/TableCfg/{name}.bytes", _TABLE_GROUP), name)
              for name in ("LevelDescTable", "DungeonTable", "DungeonTypeTable", "I18nTextTable_CN")}
    roots: dict[str, int] = {}
    pattern = re.compile(r"^Data/Streaming/PC/([^/]+)/Streaming/InitChunkData_(?:Global|-?\d+_-?\d+)_0_0\.bytes$", re.I)
    instance_items = tuple(item for item in files[_STATIC_GROUP].values() if pattern.fullmatch(item.file_name))
    dynamic_pattern = re.compile(r"^Data/DynamicStreaming/PC/Scene/[^/]+/fb_(?:main|init)_.*\.bytes$", re.I)
    dynamic_items = tuple(item for item in files[_DYNAMIC_GROUP].values() if dynamic_pattern.fullmatch(item.file_name))
    for item in files[_STATIC_GROUP].values():
        match = pattern.fullmatch(item.file_name)
        if match:
            if match[1].casefold().startswith("devonly"):
                continue
            scene = validate_scene_id(match[1])
            roots[scene] = roots.get(scene, 0) + 1
    inputs = {name: describe_file(_source_path(extraction.get(name), path.parent, name))
              for name in ("resourceDatabase", "bundleScanIndex")}
    inputs["StringPathHash"] = describe_file(_first_run_inputs(path)[2])
    identity_inputs = {"format": "InstancedSceneCatalogSource/1", "mapLayerFingerprint": fingerprint.lower(),
                       "game": {"globalgamemanagers": describe_file(root.parents[1] / "globalgamemanagers")["sha256"],
                                "Endfield.exe": describe_file(root.parents[2] / "Endfield.exe")["sha256"]},
                       "inputs": {name: {"bytes": value["bytes"], "sha256": value["sha256"]} for name, value in inputs.items()},
                       "sceneProbes": sorted(str(describe_file(probe)["sha256"]) for probe in _shared_probes(path, runtime)),
                       "payloads": payload_hashes,
                       "sceneChunkDependencies": {
                           "instances": _chunk_source_evidence(root, _STATIC_GROUP, instance_items, source_stamps),
                           "dynamic": _chunk_source_evidence(root, _DYNAMIC_GROUP, dynamic_items, source_stamps),
                       },
                       "groups": {name: describe_file(root / name / f"{name}.blc")["sha256"] for name in group_names},
                       "reader": hashlib.sha256(Path(__file__).read_bytes() + Path(read_scene_table.__code__.co_filename).read_bytes() + Path(read_scene_string_pool.__code__.co_filename).read_bytes()).hexdigest()}
    identity = hashlib.sha256(json.dumps(identity_inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if sources != _group_sources(root, group_names) or before != tuple(_file_stamp(source) for source in sources):
        raise SceneInputChangedError(f"Instanced scene catalog sources changed during read: vfs={root}")
    check_cancelled()
    return _assemble_catalog(owners, roots, tables["LevelDescTable"], tables["DungeonTable"], tables["DungeonTypeTable"],
                             tables["I18nTextTable_CN"], evidence, identity)


def _catalog_sources(path: Path, runtime: Mapping[str, object], root: Path) -> tuple[Path, ...]:
    extraction = require_object(runtime.get("extraction"), "runtime extraction")
    state_path, _, path_hash = _first_run_inputs(path)
    return (path, state_path, path_hash, root.parents[1] / "globalgamemanagers", root.parents[2] / "Endfield.exe",
            *_group_sources(root, (_JSON_GROUP, _TABLE_GROUP, _STATIC_GROUP, _DYNAMIC_GROUP)),
            *_shared_probes(path, runtime),
            *(_source_path(extraction.get(name), path.parent, name) for name in ("resourceDatabase", "bundleScanIndex")))


def _validate_catalog(value: object) -> InstancedSceneCatalog:
    """Validate every field consumed by the service after reading owned cache JSON."""
    catalog = require_object(value, "cached instanced catalog")
    if catalog.get("format") != "InstancedSceneCatalog/1":
        raise InstancedSceneError("Unsupported cached instanced scene catalog format")
    for name in ("catalog_id", "source_identity"):
        if re.fullmatch(r"[0-9a-f]{64}", _text(catalog.get(name), name)) is None:
            raise InstancedSceneError(f"Invalid cached scene identity: field={name}")
    availability = require_object(catalog.get("availability"), "catalog availability")
    if type(availability.get("ready")) is not bool:
        raise InstancedSceneError("Invalid cached catalog availability.ready")
    _text(availability.get("reason"), "catalog availability.reason")
    for name in ("groups", "scenes", "limitations"):
        if not isinstance(catalog.get(name), list):
            raise InstancedSceneError(f"Missing cached catalog array: field={name}")
    group_ids: set[str] = set()
    for value in cast(list[object], catalog["groups"]):
        group = require_object(value, "cached scene group")
        group_id = _text(group.get("group_id"), "group_id")
        if group_id in group_ids:
            raise InstancedSceneError(f"Duplicate cached scene group: group={group_id}")
        group_ids.add(group_id)
        _text(group.get("name"), "group name")
    scene_ids: set[str] = set()
    for value in cast(list[object], catalog["scenes"]):
        scene = require_object(value, "cached physical scene")
        identifier = validate_scene_id(_text(scene.get("scene_id"), "scene_id"))
        if identifier in scene_ids:
            raise InstancedSceneError(f"Duplicate cached physical scene: scene={identifier}")
        scene_ids.add(identifier)
        _text(scene.get("name"), "scene name")
        if scene.get("form") != "blueprint" or scene.get("scope") != "whole_physical_scene" or scene.get("group_id") not in group_ids:
            raise InstancedSceneError(f"Invalid cached physical scene scope/group: scene={identifier}")
        capability = require_object(scene.get("export_capability"), "scene capability")
        if type(capability.get("ready")) is not bool or capability.get("export_modes") not in ([], ["blend"]):
            raise InstancedSceneError(f"Invalid cached export capability: scene={identifier}")
        _text(capability.get("reason"), "scene capability.reason")
        counts = require_object(scene.get("component_counts"), "scene counts")
        if any(key not in counts or (counts[key] is not None and (type(counts[key]) is not int or cast(int, counts[key]) < 0)) for key in _COMPONENTS):
            raise InstancedSceneError(f"Invalid cached scene component counts: scene={identifier}")
        for name in ("logical_level_ids", "category_ids", "limitations", "source_evidence"):
            if not isinstance(scene.get(name), list):
                raise InstancedSceneError(f"Invalid cached scene array: scene={identifier}, field={name}")
    return cast(InstancedSceneCatalog, catalog)


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def read_instanced_catalog(runtime_path: Path, check_cancelled: Callable[[], None]) -> InstancedSceneCatalog:
    """Reuse one compact catalog only while all current source identities match."""
    check_cancelled()
    path = runtime_path.expanduser().resolve()
    runtime, root = _runtime(path)
    paths = require_object(runtime.get("paths"), "runtime paths")
    cache = resolve_path(paths.get("cache_root"), path.parent) / "instanced_catalog"
    if cache.is_relative_to(root.parents[2]):
        raise InstancedSceneError(f"Instanced catalog cache cannot be inside the game: path={cache}")
    sources = _catalog_sources(path, runtime, root)
    stamps = tuple(_file_stamp(source) for source in sources)
    key_inputs = {"sources": [(str(source), asdict(stamp)) for source, stamp in zip(sources, stamps, strict=True)],
                  "reader": hashlib.sha256(Path(__file__).read_bytes() + Path(read_scene_table.__code__.co_filename).read_bytes() + Path(read_scene_string_pool.__code__.co_filename).read_bytes()).hexdigest()}
    key = hashlib.sha256(json.dumps(key_inputs, sort_keys=True).encode()).hexdigest()
    cache_path = cache / "catalog.json"
    catalog: InstancedSceneCatalog
    hit = False
    if cache_path.is_file():
        if cache_path.stat().st_size > 8 * 1024 * 1024:
            raise InstancedSceneError(f"Instanced catalog cache exceeds 8 MiB: path={cache_path}")
        cached = require_object(json.loads(cache_path.read_text(encoding="utf-8")), "instanced catalog cache")
        if cached.get("format") != "InstancedSceneCatalogCache/1":
            raise InstancedSceneError(f"Unsupported instanced catalog cache: path={cache_path}")
        if cached.get("source_key") == key:
            actual_hash = hashlib.sha256(json.dumps(cached.get("catalog"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if cached.get("catalog_sha256") != actual_hash:
                raise SceneInputChangedError(f"Instanced catalog cache payload changed: path={cache_path}")
            catalog = _validate_catalog(cached.get("catalog"))
            hit = True
        else:
            catalog = _read_instanced_catalog(path, check_cancelled)
    else:
        catalog = _read_instanced_catalog(path, check_cancelled)
    if sources != _catalog_sources(path, runtime, root) or stamps != tuple(_file_stamp(source) for source in sources):
        raise SceneInputChangedError(f"Instanced catalog sources changed during read: runtime={path}")
    check_cancelled()
    if not hit:
        cache.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        _write_json(cache_path, {"format": "InstancedSceneCatalogCache/1", "source_key": key, "catalog_sha256": digest, "catalog": catalog})
    return catalog


def _prepared_from_marker(marker: Path, scene: str, identity: str, input_hash: str,
                          check_cancelled: Callable[[], None]) -> PreparedInstancedScene | None:
    if not marker.is_file():
        return None
    if marker.stat().st_size > 128 * 1024:
        raise InstancedSceneError(f"Instanced preparation marker exceeds 128 KiB: path={marker}")
    value = require_object(json.loads(marker.read_text(encoding="utf-8")), "instanced preparation marker")
    if value.get("format") != "PreparedInstancedScene/1" or value.get("scene_id") != scene or value.get("source_identity") != identity:
        raise InstancedSceneError(f"Invalid instanced preparation marker identity: path={marker}, scene={scene}")
    if value.get("input_hash") != input_hash:
        return None
    records = require_object(value.get("files"), "prepared scene files")
    resolved: dict[str, Path] = {}
    for key in ("runtime", "state", "instances", "assets"):
        check_cancelled()
        record = require_object(records.get(key), f"prepared {key}")
        source = _source_path(record.get("path"), marker.parent, key)
        if not source.is_relative_to(marker.parent):
            raise InstancedSceneError(f"Prepared scene artifact escapes its cache: path={source}")
        actual = describe_file(source)
        if record.get("sha256") != actual["sha256"] or record.get("bytes") != actual["bytes"]:
            raise SceneInputChangedError(f"Prepared scene artifact changed: path={source}")
        resolved[key] = source
    bounds = require_object(value.get("bounds"), "prepared scene bounds")
    numeric_bounds: dict[str, float] = {}
    for key in ("xmin", "xmax", "zmin", "zmax"):
        coordinate = bounds.get(key)
        if type(coordinate) not in (int, float):
            raise InstancedSceneError(f"Invalid prepared scene bounds: path={marker}")
        try:
            numeric = float(coordinate)
        except OverflowError as error:
            raise InstancedSceneError(f"Invalid prepared scene bounds: path={marker}") from error
        if not math.isfinite(numeric):
            raise InstancedSceneError(f"Invalid prepared scene bounds: path={marker}")
        numeric_bounds[key] = numeric
    if numeric_bounds["xmin"] >= numeric_bounds["xmax"] or numeric_bounds["zmin"] >= numeric_bounds["zmax"]:
        raise InstancedSceneError(f"Empty prepared scene bounds: path={marker}")
    if any(not math.isclose(numeric_bounds[key] / 128.0,
                            round(numeric_bounds[key] / 128.0), rel_tol=0.0, abs_tol=1e-9)
           for key in ("xmin", "xmax", "zmin", "zmax")):
        raise InstancedSceneError(f"Prepared scene bounds are not aligned to the 128 m grid: path={marker}")
    from types import MappingProxyType

    limits = value.get("limitations")
    if not isinstance(limits, list) or any(not isinstance(item, str) for item in limits) or value.get("resource_status") != "deferred_exact_preflight":
        raise InstancedSceneError(f"Invalid prepared scene limitations/resource status: path={marker}")
    counts = MappingProxyType({key: None for key in _COMPONENTS})
    check_cancelled()
    return PreparedInstancedScene(resolved["runtime"], cast(InstancedSceneBounds, bounds), resolved["instances"], resolved["assets"],
                                 identity, counts, tuple(limits), "deferred_exact_preflight")


def _absolute_inputs(value: object, parent: Path, key: str) -> object:
    """Copy runtime descriptors while keeping relocated inputs unambiguous."""
    path_keys = {"path", "root", "instances", "assets", "bundleScanIndex", "resourceDatabase", "gameRoot", "cacheRoot", "exportRoot"}
    if isinstance(value, dict):
        return {name: _absolute_inputs(item, parent, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_absolute_inputs(item, parent, "path" if key == "terrainSurfaces" else key) for item in value]
    if isinstance(value, str) and value and key in path_keys:
        return str(resolve_path(value, parent))
    return value


def _whole_scene_bounds(instances: Path, vfs_root: Path, path_hash: Path,
                        scene: str, cache: Path, check_cancelled: Callable[[], None]) -> InstancedSceneBounds:
    from extractor_core.dynamic_renderer_cache import read_dynamic_scene_renderers_cached
    from extractor_core.effects import read_dynamic_scene_effects_for_scene

    with closing(sqlite3.connect(instances.as_uri() + "?mode=ro", uri=True)) as connection:
        conflicts = connection.execute("SELECT entity_id,source_path FROM duplicates WHERE same_payload=0 LIMIT 1").fetchone()
        if conflicts is not None:
            raise InstancedSceneError(f"Conflicting physical scene entity payloads: scene={scene}, entity={conflicts[0]}, source={conflicts[1]}")
        rows = connection.execute("SELECT MIN(x),MAX(x),MIN(z),MAX(z),COUNT(*) FROM instances").fetchone()
        if rows is None or rows[4] == 0:
            raise InstancedSceneError(f"Scene has no indexed placement records: scene={scene}")
        xs, zs = [float(rows[0]), float(rows[1])], [float(rows[2]), float(rows[3])]
        for (source,) in connection.execute("SELECT virtual_path FROM chunks"):
            match = re.search(r"InitChunkData_(-?\d+)_(-?\d+)_0_0\.bytes$", source)
            if match:
                xs.extend((int(match[1]) * 128, (int(match[1]) + 1) * 128))
                zs.extend((int(match[2]) * 128, (int(match[2]) + 1) * 128))
    renderers = read_dynamic_scene_renderers_cached(
        vfs_root, path_hash, scene, cache / "dynamic_renderers", check_cancelled)
    for record in renderers.records:
        xs.append(record.matrix[12])
        zs.append(record.matrix[14])
        if record.bounds is not None:
            xs.extend((record.bounds[0], record.bounds[1]))
            zs.extend((record.bounds[4], record.bounds[5]))
    effects = read_dynamic_scene_effects_for_scene(vfs_root, path_hash, scene, check_cancelled)
    for effect in effects.records:
        xs.append(effect.position[0])
        zs.append(effect.position[2])
    if any(not math.isfinite(value) for value in (*xs, *zs)):
        raise InstancedSceneError(f"Scene placement extent contains nonfinite values: scene={scene}")
    # Cover the entire source grid, including records exactly on its final edge.
    return {"xmin": math.floor(min(xs) / 128) * 128.0, "xmax": (math.floor(max(xs) / 128) + 1) * 128.0,
            "zmin": math.floor(min(zs) / 128) * 128.0, "zmax": (math.floor(max(zs) / 128) + 1) * 128.0}


def prepare_instanced_scene(runtime_path: Path, scene_id: str, check_cancelled: Callable[[], None],
                           progress: Callable[[float, str], None]) -> PreparedInstancedScene:
    """Prepare one source index; exact selected resource closure stays with export."""
    check_cancelled()
    scene = validate_scene_id(scene_id)
    path = runtime_path.expanduser().resolve()
    runtime, vfs_root = _runtime(path)
    catalog = read_instanced_catalog(path, check_cancelled)
    selected = next((row for row in catalog["scenes"] if row["scene_id"] == scene), None)
    if selected is None or not selected["export_capability"]["ready"]:
        raise InstancedSceneError(f"Scene is not exportable from this catalog: scene={scene}, reason={None if selected is None else selected['export_capability']['reason']}")
    paths = require_object(runtime.get("paths"), "runtime paths")
    cache = resolve_path(paths.get("cache_root"), path.parent)
    game = resolve_path(paths.get("game_root"), path.parent)
    if cache.is_relative_to(game):
        raise InstancedSceneError(f"Instanced scene cache cannot be inside the game installation: cache={cache}")
    extraction = require_object(runtime.get("extraction"), "runtime extraction")
    maps = require_object(extraction.get("maps"), "runtime maps")
    if not any(isinstance(value, dict) and isinstance(value.get("sceneProbe"), dict) for value in maps.values()):
        raise InstancedSceneError("Instanced scene preparation needs a first-run shared SceneProbe resource catalog")
    state_path, state, path_hash = _first_run_inputs(path)
    source_root = cache / "instanced_scenes" / catalog["source_identity"] / scene
    marker = source_root / "complete.json"
    input_hash = hashlib.sha256((str(describe_file(path)["sha256"]) + str(describe_file(state_path)["sha256"])).encode()).hexdigest()
    reused = _prepared_from_marker(marker, scene, catalog["source_identity"], input_hash, check_cancelled)
    if reused is not None:
        progress(1.0, f"Reused validated physical scene {scene} source index")
        return reused
    target = source_root / uuid4().hex
    target.mkdir(parents=True)
    instances, assets = target / "instances.sqlite", target / "assets.sqlite"

    def cancelled() -> bool:
        check_cancelled()
        return False

    def report(phase: str, value: float, message: str, details: object) -> None:
        progress(0.1 + 0.5 * value, message)

    progress(0.1, f"Indexing physical scene {scene}")
    group_sources = _group_sources(vfs_root, (_STATIC_GROUP, _DYNAMIC_GROUP))
    source_paths = (path, state_path, path_hash, *_shared_probes(path, runtime), *group_sources)
    stamps = tuple(_file_stamp(source) for source in source_paths)
    build_instance_database(scene, vfs_root, instances, progress=report, cancelled=cancelled)
    build_unresolved_asset_database(instances, assets)
    with scene_io_session():
        bounds = _whole_scene_bounds(instances, vfs_root, path_hash, scene, cache, check_cancelled)
    if group_sources != _group_sources(vfs_root, (_STATIC_GROUP, _DYNAMIC_GROUP)) or stamps != tuple(_file_stamp(source) for source in source_paths):
        raise SceneInputChangedError(f"Instanced scene sources changed during preparation: scene={scene}")
    absolute = require_object(_absolute_inputs(runtime, path.parent, ""), "absolute runtime")
    absolute_paths = {name: str(resolve_path(value, path.parent)) if isinstance(value, str) else value for name, value in paths.items()}
    absolute["paths"] = absolute_paths
    absolute_extraction = require_object(absolute["extraction"], "absolute extraction")
    absolute_maps = require_object(absolute_extraction["maps"], "absolute maps")
    shared_probe = next(require_object(value, "prepared map")["sceneProbe"] for value in absolute_maps.values()
                        if isinstance(value, dict) and isinstance(value.get("sceneProbe"), dict))
    absolute_extraction["maps"] = {**absolute_maps, scene: {"instances": str(instances), "assets": str(assets),
                                                          "sceneProbe": shared_probe, "terrainSurfaces": [],
                                                          "assetClosureStatus": "deferred_exact_preflight"}}
    prepared_path = target / "endfield.runtime.local.json"
    for destination, value in ((prepared_path, absolute), (target / "first_run_state.json", _absolute_inputs(state, path.parent, ""))):
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
    files = {key: describe_file(source) for key, source in (("runtime", prepared_path), ("state", target / "first_run_state.json"),
                                                          ("instances", instances), ("assets", assets))}
    _write_json(marker, {"format": "PreparedInstancedScene/1", "scene_id": scene, "source_identity": catalog["source_identity"],
                         "input_hash": input_hash, "bounds": bounds, "files": files,
                         "resource_status": "deferred_exact_preflight", "limitations": selected["limitations"]})
    check_cancelled()
    progress(1.0, f"Physical scene {scene} indexed; selected resources will be validated by export preflight")
    from types import MappingProxyType

    return PreparedInstancedScene(prepared_path, bounds, instances, assets, catalog["source_identity"],
                                 MappingProxyType(dict(selected["component_counts"])), tuple(selected["limitations"]), "deferred_exact_preflight")
