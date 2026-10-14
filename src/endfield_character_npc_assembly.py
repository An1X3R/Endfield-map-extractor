"""Prepare configuration-selected NPC meshes from authenticated source objects."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import struct
import tempfile

from endfield_character_bundle_inputs import prepare_providers_bundles
from endfield_character_cache import CharacterDataCache, CharacterSourceChangedError, source_stamp
from endfield_character_catalog import CharacterResourceProvider, _provider_details
from endfield_character_npc_catalog import IndependentNpcRecord, IndependentNpcConfigReference
from endfield_character_sources import CharacterSourceContext, game_source_stamps, validate_character_storage, verify_game_source
from endfield_scene_export import run_scene_process
from extractor_core.projected_decals import build_path_id_index
from extractor_core.resources import read_virtual_file


class NpcAssemblyPreparationError(ValueError):
    """Selected NPC configuration cannot be assembled without invented evidence."""


@dataclass(frozen=True)
class NpcSourceObject:
    path: Path
    value: dict[str, object]
    metadata: dict[str, object]


@dataclass(frozen=True)
class NpcAssetReference:
    resource_path: str
    provider: CharacterResourceProvider


@dataclass(frozen=True)
class NpcSelectedSlot:
    name: str
    index: int
    use_self_avatar: bool
    self_avatar_guid: tuple[int, int, int, int]
    parent_bone_name: str | None
    parent_bone_transform_name: str | None
    renderers: tuple[dict[str, object], ...]


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise NpcAssemblyPreparationError(f"Expected source object: field={field}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NpcAssemblyPreparationError(f"Expected nonempty source text: field={field}")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise NpcAssemblyPreparationError(f"Expected source integer: field={field}, value={value!r}")
    return value


def _rows(value: object, field: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise NpcAssemblyPreparationError(f"Expected source array: field={field}")
    return tuple(_object(row, field) for row in value)


def _source_text(value: object, field: str) -> str | None:
    if not isinstance(value, str):
        raise NpcAssemblyPreparationError(f"Expected source text: field={field}")
    return value or None


def _guid_words(value: object, field: str) -> tuple[int, int, int, int]:
    row = _object(value, field)
    words = tuple(_integer(row.get("val" + str(index)), field) for index in range(4))
    if any(not 0 <= word < 2**32 for word in words):
        raise NpcAssemblyPreparationError(f"Source GUID words must be uint32: field={field}")
    return words


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _string_path_hash(context: CharacterSourceContext) -> Path:
    with closing(sqlite3.connect(context.resource_database.as_uri() + "?mode=ro", uri=True)) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM meta"))
    expected = str(metadata.get("string_path_hash_sha256", "")).casefold()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise NpcAssemblyPreparationError("NPC resource database has no valid StringPathHash source fingerprint")
    cache_directory = context.cache_root / "string_path_hash"
    cache_directory.mkdir(parents=True, exist_ok=True)
    path = cache_directory / f"{expected}.bin"
    if path.is_file():
        if _sha(path) != expected:
            raise NpcAssemblyPreparationError(f"Cached NPC StringPathHash differs from its content identity: path={path}")
        return path
    payload = read_virtual_file(context.game_root / "Endfield_Data/StreamingAssets/VFS",
                                "D6E622F7", "Data/ExtendData/Main/StringPathHash.bin")
    if hashlib.sha256(payload).hexdigest() != expected:
        raise NpcAssemblyPreparationError("NPC path-hash source differs from the configured resource database")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{expected}.", suffix=".tmp", dir=cache_directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _complete(value: object) -> None:
    if isinstance(value, dict):
        if {"$partial", "$inferred"}.intersection(value):
            raise NpcAssemblyPreparationError("Partial or inferred JSON cannot be used as NPC source evidence")
        for item in value.values():
            _complete(item)
    elif isinstance(value, list):
        for item in value:
            _complete(item)


def read_npc_source_objects(directory: Path) -> tuple[NpcSourceObject, ...]:
    """Read exact serialized objects; diagnostic JSON is never assembly input."""
    objects: list[NpcSourceObject] = []
    for path in sorted(directory.rglob("*.json")):
        value = _object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))
        if "$animestudio" not in value:
            continue
        _complete(value)
        metadata = _object(value["$animestudio"], "$animestudio")
        if _integer(metadata.get("byteSize"), "byteSize") != _integer(metadata.get("rawDataLength"), "rawDataLength"):
            raise NpcAssemblyPreparationError(f"Source JSON has incomplete object bytes: path={path}")
        if len(_text(metadata.get("rawDataSha256"), "rawDataSha256")) != 64:
            raise NpcAssemblyPreparationError(f"Source JSON has invalid object hash: path={path}")
        objects.append(NpcSourceObject(path, value, metadata))
    return tuple(objects)


def _config_provider(reference: IndependentNpcConfigReference | None) -> CharacterResourceProvider:
    if reference is None or reference.resource_path is None or len(reference.providers) != 1:
        raise NpcAssemblyPreparationError("NPC requires one exact AvatarMesh and AvatarTemplet provider")
    provider = reference.providers[0]
    if len(provider.serialized_cabs) != 1:
        raise NpcAssemblyPreparationError("NPC configuration provider must contain one source CAB")
    return provider


def _config_object(objects: tuple[NpcSourceObject, ...], reference: IndependentNpcConfigReference) -> NpcSourceObject:
    cab = _config_provider(reference).serialized_cabs[0].name.casefold()
    matches = tuple(row for row in objects
        if row.metadata.get("type") == "MonoBehaviour"
        and str(row.metadata.get("sourceFile")).casefold() == cab
        and str(row.metadata.get("container")).replace("\\", "/").casefold() == reference.resource_path)
    if len(matches) != 1:
        raise NpcAssemblyPreparationError(f"Configuration source object is not unique: path={reference.resource_path}, matches={len(matches)}")
    return matches[0]


def selected_npc_slots(source: NpcSourceObject, part_names: tuple[str, ...]) -> tuple[NpcSelectedSlot, ...]:
    """Select only the exact PrefabInfo slot set and preserve source attachment metadata."""
    if not part_names or len(set(part_names)) != len(part_names) or any(not name.strip() or name != name.strip() for name in part_names):
        raise NpcAssemblyPreparationError("NPC slot selection must contain unique exact part names")
    source_rows = _rows(source.value.get("avatarSlotMeshDatas"), "avatarSlotMeshDatas")
    selected: list[NpcSelectedSlot] = []
    for part_name in part_names:
        matches = tuple((index, row) for index, row in enumerate(source_rows) if row.get("name") == part_name)
        if len(matches) != 1:
            raise NpcAssemblyPreparationError(f"NPC part must match one exact AvatarMesh slot: part={part_name}, matches={len(matches)}")
        slot_index, slot = matches[0]
        use_self_avatar_value = _integer(slot.get("bUseSelfAvatar"), "bUseSelfAvatar")
        if use_self_avatar_value not in (0, 1):
            raise NpcAssemblyPreparationError(f"NPC self-avatar flag must be exactly 0 or 1: part={part_name}")
        use_self_avatar = use_self_avatar_value == 1
        parent_bone = _source_text(slot.get("parentBoneName"), "parentBoneName")
        parent_transform = _source_text(slot.get("parentBoneTransformName"), "parentBoneTransformName")
        self_avatar_guid = _guid_words(slot.get("selfAvatarGuid"), "selfAvatarGuid")
        renderers = _rows(slot.get("partSubMeshsLOD0"), "partSubMeshsLOD0")
        if not renderers:
            raise NpcAssemblyPreparationError(f"NPC selected slot has no source LOD0 renderers: part={part_name}")
        if use_self_avatar:
            if parent_bone is None or parent_transform is None or not parent_transform.startswith("Root/"):
                raise NpcAssemblyPreparationError(f"Self-avatar slot requires its exact source parent transform: part={part_name}")
            components = parent_transform.split("/")
            if any(component in ("", ".", "..") for component in components) or components[-1] != parent_bone:
                raise NpcAssemblyPreparationError(f"Self-avatar parent path disagrees with its source bone name: part={part_name}")
            if self_avatar_guid == (0, 0, 0, 0):
                raise NpcAssemblyPreparationError(f"Self-avatar slot requires a nonzero source GUID: part={part_name}")
            for renderer in renderers:
                if _guid_words(renderer.get("meshGuid"), "meshGuid") != self_avatar_guid:
                    raise NpcAssemblyPreparationError(f"Self-avatar slot GUID differs from a renderer mesh GUID: part={part_name}")
        elif parent_bone is not None or parent_transform is not None or self_avatar_guid != (0, 0, 0, 0):
            raise NpcAssemblyPreparationError(f"Non-self-avatar slot has attachment metadata requiring a different source path: part={part_name}")
        selected.append(NpcSelectedSlot(part_name, slot_index, use_self_avatar, self_avatar_guid,
                                        parent_bone, parent_transform, renderers))
    return tuple(selected)


def selected_npc_renderers(source: NpcSourceObject, part_name: str) -> tuple[dict[str, object], ...]:
    """Select one plain source slot for compatible explicit single-part requests."""
    slot, = selected_npc_slots(source, (part_name,))
    _require_plain_npc_slot(slot)
    return slot.renderers


def _require_plain_npc_slot(slot: NpcSelectedSlot) -> None:
    """Reject slot forms that require source Avatar attachment semantics."""
    if slot.use_self_avatar:
        raise NpcAssemblyPreparationError("Self-Avatar NPC slots require a grouped source assembly request")
    if slot.parent_bone_name is not None or slot.parent_bone_transform_name is not None:
        raise NpcAssemblyPreparationError("Attached NPC slots require a grouped source assembly request")


def resolve_npc_assets(path_index: dict[bytes, tuple[str, ...]], renderers: tuple[dict[str, object], ...],
                       provider_index: Path, check_cancelled: Callable[[], None]) -> dict[int, NpcAssetReference]:
    """Resolve exact numeric source hashes, then require unique container providers."""
    required: set[int] = set()
    optional: set[int] = set()
    for row in renderers:
        required.add(_integer(row.get("meshPathHash"), "meshPathHash"))
        disabled = _integer(row.get("isRendererDisabled"), "isRendererDisabled")
        if disabled not in (0, 1):
            raise NpcAssemblyPreparationError("NPC renderer disabled flag must be exactly 0 or 1")
        hashes = row.get("materialPathHashes")
        if not isinstance(hashes, list) or not hashes:
            raise NpcAssemblyPreparationError("NPC renderer requires source material hashes")
        target = optional if disabled else required
        target.update(_integer(value, "materialPathHashes") for value in hashes)
    paths: dict[int, str] = {}
    for value in sorted(required | optional):
        if not -(2**63) <= value < 2**64:
            raise NpcAssemblyPreparationError(f"NPC asset hash exceeds 64 bits: hash={value}")
        candidates = path_index.get(struct.pack("<Q", value & (2**64 - 1)), ())
        if len(candidates) != 1:
            if not candidates and value not in required:
                continue
            raise NpcAssemblyPreparationError(f"NPC asset hash must resolve to one source path: hash={value}, matches={candidates}")
        paths[value] = candidates[0].replace("\\", "/")
    containers = frozenset(path.partition("##")[0].casefold() for path in paths.values())
    providers, _ = _provider_details(provider_index, containers, check_cancelled)
    result: dict[int, NpcAssetReference] = {}
    for value, path in paths.items():
        found = providers[path.partition("##")[0].casefold()]
        if not found and value not in required:
            continue
        if len(found) != 1 or len(found[0].serialized_cabs) != 1:
            raise NpcAssemblyPreparationError(f"NPC resource provider is not unique: hash={value}, path={path}, providers={len(found)}")
        result[value] = NpcAssetReference(path, found[0])
    return result


def _config_manifest(source: NpcSourceObject, role: str) -> dict[str, object]:
    meta = source.metadata
    return {"source_json_path": str(source.path), "source_json_sha256": _sha(source.path),
        "configuration_role": role, "object": {"cab": _text(meta.get("sourceFile"), "sourceFile"),
        "path_id": str(_integer(meta.get("pathId"), "pathId")), "name": _text(meta.get("name"), "name"),
        "container": _text(meta.get("container"), "container"),
        "raw_data_sha256": _text(meta.get("rawDataSha256"), "rawDataSha256"),
        "type_tree_source": _text(meta.get("typeTreeSource"), "typeTreeSource")}}


def _renderer_manifest(row: dict[str, object], export_index: int, slot: NpcSelectedSlot, renderer_index: int,
                       assets: dict[int, NpcAssetReference]) -> dict[str, object]:
    mesh_name = _text(row.get("meshName"), "meshName")
    mesh_hash = _integer(row.get("meshPathHash"), "meshPathHash")
    mesh = assets[mesh_hash]
    container, separator, subasset = mesh.resource_path.partition("##")
    if separator and subasset.casefold() != mesh_name.casefold():
        raise NpcAssemblyPreparationError(f"Mesh subasset disagrees with configured name: mesh={mesh_name}, path={mesh.resource_path}")
    guid = _object(row.get("meshGuid"), "meshGuid")
    words = tuple(_integer(guid.get("val" + str(i)), "meshGuid") for i in range(4))
    if any(not 0 <= word < 2**32 for word in words):
        raise NpcAssemblyPreparationError("Mesh GUID words must be uint32")
    hashes = row["materialPathHashes"]
    if not isinstance(hashes, list):
        raise NpcAssemblyPreparationError("materialPathHashes must be an array")
    materials: list[dict[str, object]] = []
    for value in hashes:
        hash_value = _integer(value, "materialPathHashes")
        source = assets.get(hash_value)
        if source is None:
            materials.append({"hash": str(hash_value), "status": "unresolved_source_hash",
                              "diagnostic": "No unique provider for an explicitly disabled source renderer"})
        else:
            path, separator, name = source.resource_path.partition("##")
            materials.append({"hash": str(hash_value), "status": "provider_resolved_name_requires_probe_confirmation",
                "provider_cab": source.provider.serialized_cabs[0].name,
                "provider_logical_name": source.provider.logical_name, "resource_path": source.resource_path,
                "subasset_name": name if separator else None, "name": name if separator else PurePosixPath(path).stem})
    # Object names are deterministic and independent of source-name byte length.
    return {"descriptor_name": mesh_name, "export_name": f"npc_lod0_{export_index:04d}", "lod": 0,
        "slot_name": slot.name, "slot_index": slot.index, "renderer_index": renderer_index,
        "slot_use_self_avatar": slot.use_self_avatar,
        "slot_self_avatar_guid": [str(word) for word in slot.self_avatar_guid],
        "slot_parent_bone_name": slot.parent_bone_name,
        "slot_parent_bone_transform_name": slot.parent_bone_transform_name,
        "mesh": {"provider_cab": mesh.provider.serialized_cabs[0].name,
            "provider_logical_name": mesh.provider.logical_name, "resource_path": mesh.resource_path,
            "subasset_name": subasset if separator else None, "name": mesh_name,
            "mesh_path_hash": str(mesh_hash), "guid_words": [str(word) for word in words],
            "path_id_resolution": "probe_unique_exact_cab_name"},
        "root_bone": {"id": _integer(row.get("rootBoneID"), "rootBoneID"),
                      "name": _text(row.get("rootBoneName"), "rootBoneName")},
        "material_path_hashes": [str(value) for value in hashes], "materials": materials,
        "configured_is_active": _integer(row.get("isActive"), "isActive"),
        "configured_renderer_disabled": _integer(row.get("isRendererDisabled"), "isRendererDisabled"),
        "configured_real_time_shadow_caster": _integer(row.get("realTimeShadowCaster"), "realTimeShadowCaster"),
        "configured_platform_availability": _integer(row.get("platformAvailability"), "platformAvailability"),
        "placement_mode": "bindpose_consensus"}


def prepare_npc_assembly(context: CharacterSourceContext, npc: IndependentNpcRecord, part_name: str,
                         json_reader: Path, unity_version: str, output: Path,
                         check_cancelled: Callable[[], None],
                         progress: Callable[[str, float, str], None]) -> Path:
    """Prepare one plain source slot for compatible explicit single-part requests."""
    return _prepare_npc_assembly(context, npc, part_name, json_reader, unity_version,
                                 output, check_cancelled, progress)


def prepare_npc_assembly_group(context: CharacterSourceContext, npc: IndependentNpcRecord,
                               part_names: tuple[str, ...], json_reader: Path, unity_version: str,
                               output: Path, check_cancelled: Callable[[], None],
                               progress: Callable[[str, float, str], None]) -> Path:
    """Prepare the exact simultaneous slot group recorded by one NPC PrefabInfo."""
    source_names = tuple(part.part_name for part in npc.resource_reference.prefab_parts)
    if part_names != source_names:
        raise NpcAssemblyPreparationError("Grouped NPC assembly must use the exact PrefabInfo partNameIdList order")
    return _prepare_npc_assembly(context, npc, part_names, json_reader, unity_version,
                                 output, check_cancelled, progress)


def _prepare_npc_assembly(context: CharacterSourceContext, npc: IndependentNpcRecord,
                          selection: str | tuple[str, ...],
                          json_reader: Path, unity_version: str, output: Path,
                          check_cancelled: Callable[[], None],
                          progress: Callable[[str, float, str], None]) -> Path:
    """Extract fresh configuration evidence and prepare a strict source assembly input."""
    if isinstance(selection, str):
        part_names = (selection,)
    elif isinstance(selection, tuple):
        part_names = selection
    else:
        raise NpcAssemblyPreparationError("NPC assembly selection must be one part name or an exact PrefabInfo slot tuple")
    validate_character_storage(context.game_root, output)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"NPC assembly output already exists: {output}")
    source_names = tuple(part.part_name for part in npc.resource_reference.prefab_parts)
    if (npc.source_kind != "config_assembled" or not part_names or len(set(part_names)) != len(part_names)
            or not set(part_names).issubset(source_names)):
        raise NpcAssemblyPreparationError("NPC selection is not a source-backed configuration part")
    if isinstance(selection, tuple) and part_names != source_names:
        raise NpcAssemblyPreparationError("Grouped NPC assembly must contain every exact PrefabInfo slot")
    mesh_ref, templet_ref = npc.resource_reference.avatar_mesh, npc.resource_reference.avatar_templet
    providers = (_config_provider(mesh_ref), _config_provider(templet_ref))
    if mesh_ref is None or templet_ref is None:
        raise NpcAssemblyPreparationError("NPC selection has missing configuration references")
    before = game_source_stamps(context.game_root)
    indexes = (context.provider_index, context.resource_database, context.cab_inventory)
    stamps = tuple(source_stamp(path) for path in indexes)
    output.mkdir(parents=True)
    temporary = output / "tmp"
    temporary.mkdir()
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    prepare_providers_bundles(context, providers, output / "configuration_bundles", check_cancelled, progress)
    progress("preparing", .2, "Reading exact NPC configuration and Avatar objects")
    run_scene_process([str(json_reader), str(output / "configuration_bundles"), str(output / "source_objects"),
        "--game", "ArknightsEndfield", "--unity_version", unity_version, "--types", "AssetBundle:Parse", "ResourceManager:Parse", "MonoBehaviour", "Avatar",
        "--export_type", "JSON", "--mono_behaviour_type_tree_priority", "SerializedFirst",
        "--group_assets", "ByType"], output / "json_reader.log", environment, check_cancelled)
    objects = read_npc_source_objects(output / "source_objects")
    mesh_source, templet_source = _config_object(objects, mesh_ref), _config_object(objects, templet_ref)
    slots = selected_npc_slots(mesh_source, part_names)
    if isinstance(selection, str):
        _require_plain_npc_slot(slots[0])
    renderer_rows = tuple((slot, renderer_index, renderer)
                          for slot in slots for renderer_index, renderer in enumerate(slot.renderers))
    renderers = tuple(row for _, _, row in renderer_rows)
    pointer = _object(templet_source.value.get("sizeAvatar"), "sizeAvatar")
    file_id = _integer(pointer.get("m_FileID"), "sizeAvatar.m_FileID")
    path_id = _integer(pointer.get("m_PathID"), "sizeAvatar.m_PathID")
    references = tuple(row for row in _rows(templet_source.metadata.get("pptrReferences"), "pptrReferences")
                       if row.get("path") == "$.sizeAvatar" and row.get("fileId") == file_id and row.get("pathId") == path_id)
    if len(references) != 1 or file_id <= 0 or path_id == 0:
        raise NpcAssemblyPreparationError("AvatarTemplet must expose one exact external sizeAvatar pointer")
    avatar_cab = _text(references[0].get("expectedTargetSourceFile"), "sizeAvatar expected CAB")
    avatars = tuple(row for row in objects if row.metadata.get("type") == "Avatar"
                    and str(row.metadata.get("sourceFile")).casefold() == avatar_cab.casefold()
                    and row.metadata.get("pathId") == path_id)
    if len(avatars) != 1:
        raise NpcAssemblyPreparationError(f"sizeAvatar target must resolve to one actual Avatar object: cab={avatar_cab}, path_id={path_id}, matches={len(avatars)}")
    avatar = avatars[0]
    hash_path = _string_path_hash(context)
    assets = resolve_npc_assets(build_path_id_index(hash_path), renderers, context.provider_index, check_cancelled)
    cache = CharacterDataCache(context.cache_root)
    cab_index, _ = cache.read_character_cab_index(context.cab_inventory, check_cancelled)
    logical = dict(cab_index.providers).get(avatar_cab.casefold(), ())
    if len(logical) != 1:
        raise NpcAssemblyPreparationError("Selected source Avatar CAB has no unique provider")
    # The Avatar is already authenticated in the configuration closure.
    roots = {provider.logical_name: provider for provider in (*providers, *(asset.provider for asset in assets.values()))}
    bundle_files = prepare_providers_bundles(context, tuple(roots.values()), output / "bundles", check_cancelled, progress)
    if not any(file.logical_name == logical[0] for file in bundle_files):
        raise NpcAssemblyPreparationError("Selected Avatar provider is absent from final assembly dependencies")
    manifest = {"format": "EndfieldNpcAssemblyInput/1", "unity_version": unity_version,
        "selection": {"character_id": npc.npc_id, "name_zh": npc.name, "npc_template_id": npc.resource_reference.template_id,
            "part_name": part_names[0], "part_names": list(part_names),
            "avatar_mesh_slot": part_names[0], "static_lod": 0},
        "bundles": [{"path": file.path, "bytes": file.bytes, "sha256": file.sha256} for file in bundle_files],
        "avatar": {"cab": avatar_cab, "path_id": str(path_id), "name": _text(avatar.metadata.get("name"), "Avatar name"),
            "raw_data_sha256": _text(avatar.metadata.get("rawDataSha256"), "Avatar raw hash"),
            "source_fbx": {"logical_name": logical[0], "cab": avatar_cab,
                "resource_path": _text(avatar.metadata.get("container"), "Avatar container")}},
        "avatar_mesh_configuration": _config_manifest(mesh_source, "AvatarMesh"),
        "avatar_templet_configuration": _config_manifest(templet_source, "AvatarTemplet"),
        "avatar_mesh_slots": [{"slot_index": slot.index, "slot_name": slot.name, "lod_index": 0,
            "use_self_avatar": slot.use_self_avatar,
            "self_avatar_guid": [str(word) for word in slot.self_avatar_guid],
            "parent_bone_name": slot.parent_bone_name,
            "parent_bone_transform_name": slot.parent_bone_transform_name} for slot in slots],
        "placement_mode": "bindpose_consensus", "placement_tolerance": {"translation_m": 1e-4, "basis_max_abs": 1e-4},
        "renderers": [_renderer_manifest(row, export_index, slot, renderer_index, assets)
            for export_index, (slot, renderer_index, row) in enumerate(renderer_rows)]}
    verify_game_source(context.game_root, before, check_cancelled)
    if tuple(source_stamp(path) for path in indexes) != stamps:
        raise CharacterSourceChangedError("NPC resource indexes changed during configuration preparation")
    path = output / "assembly_input.json"
    _write(path, manifest)
    return path
