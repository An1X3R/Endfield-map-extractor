"""Inspect configuration NPC CPU actions through an exact-source external reader."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from endfield_character_cache import CharacterDataCache
from endfield_character_selection import CharacterSelection
from endfield_character_sources import CharacterSourceContext, validate_character_storage
from endfield_scene_export import run_scene_process
from endfield_scene_resources import write_index
from webui_character_contract import json_object, required_text


class NpcActionInspectionError(RuntimeError):
    """Expose the source reader cause and a private-log identifier without host paths."""


def public_npc_failure(error: Exception) -> str:
    message = re.sub(r"(?:\\\\\?\\)?[A-Za-z]:[\\/][^\r\n]*", "<local_path>", str(error))
    message = re.sub(r"(?<![A-Za-z0-9])/(?:[^/\s]+/)+[^\r\n]*", "<local_path>", message)
    return type(error).__name__ + ": " + message


def _npc_inspection_source_paths(repository_src: Path) -> tuple[Path, ...]:
    """Invalidate both NPC inspection caches when their source parsers change."""
    modules = tuple(repository_src / name for name in (
        "endfield_character_cache.py", "endfield_character_catalog.py", "endfield_character_npc_assembly.py",
        "endfield_character_npc_animation.py", "endfield_character_npc_catalog.py", "endfield_character_sources.py",
        "endfield_scene_resources.py", "extractor_core/resources.py",
    ))
    return (Path(__file__).resolve(), *modules)


@dataclass(frozen=True)
class NpcActionSource:
    action_id: str
    state_name: str
    name: str
    resource_path: str
    logical_provider: str
    clip_cab: str
    clip_path_id: str
    clip_raw_data_sha256: str


@dataclass(frozen=True)
class NpcActionCatalog:
    status: str
    reason: str | None
    actions: tuple[NpcActionSource, ...]


@dataclass(frozen=True)
class NpcCpuActionReference:
    reference_id: str
    state_name: str
    clip_cab: str
    clip_path_id: str
    logical_provider: str


@dataclass(frozen=True)
class NpcCpuActionReferenceCatalog:
    status: Literal["exact_avatar_base_clip_references"]
    cpu_animation_template: str
    template_namespace: Literal["Humanoid", "Generic"]
    cpu_source_cab: str
    cpu_source_path_id: str
    cpu_source_sha256: str
    avatar_cab: str
    avatar_path_id: str
    avatar_binding_status: str
    actions: tuple[NpcCpuActionReference, ...]


def npc_cpu_reference_document(catalog: NpcCpuActionReferenceCatalog, selection: CharacterSelection,
                               reference_id: str) -> dict[str, object]:
    """Persist one authenticated reference without converting it into a generic Action."""
    references = tuple(row for row in catalog.actions if row.reference_id == reference_id)
    if len(references) != 1:
        raise ValueError("Selected NPC CPU reference is absent or duplicated in the current source catalog")
    avatar = {"cab": catalog.avatar_cab, "path_id": catalog.avatar_path_id}
    result: dict[str, object] = {"format": "EndfieldNpcCpuActionReferences/1", "status": catalog.status,
        "source_kind": "prefab", "source_identity": selection.catalog_identity,
        "npc_id": selection.character_id, "template_id": selection.template_id, "part_name": selection.part_name,
        "cpu_animation_template": catalog.cpu_animation_template, "template_namespace": catalog.template_namespace,
        "cpu_source": {"cab": catalog.cpu_source_cab, "path_id": catalog.cpu_source_path_id},
        "cpu_source_sha256": catalog.cpu_source_sha256, "selected_avatar": avatar, "cpu_avatar": dict(avatar),
        "avatar_binding_status": catalog.avatar_binding_status, "execution_available": False,
        "runtime_animator_reconstructed": False,
        "actions": [{**asdict(references[0]), "binding_status": "exact_cpu_base_clip_reference",
                     "execution_available": False}]}
    parse_npc_cpu_action_references(result, selection)
    return result


def parse_npc_cpu_action_references(value: object, selection: CharacterSelection) -> NpcCpuActionReferenceCatalog:
    """Authenticate prefab CPU references while keeping them outside executable actions."""
    row = json_object(value, "NPC CPU action reference catalog")
    if (row.get("format") != "EndfieldNpcCpuActionReferences/1"
            or row.get("source_identity") != selection.catalog_identity
            or row.get("npc_id") != selection.character_id
            or row.get("template_id") != selection.template_id
            or row.get("part_name") != selection.part_name
            or row.get("source_kind") != "prefab"):
        raise ValueError("NPC CPU reference catalog does not authenticate this exact prefab selection")
    if row.get("execution_available") is not False or row.get("runtime_animator_reconstructed") is not False:
        raise ValueError("NPC CPU source references must not claim animation execution")
    if row.get("status") != "exact_avatar_base_clip_references":
        raise ValueError("NPC CPU reference catalog did not verify exact CPU and configuration Avatars")
    template = required_text(row.get("cpu_animation_template"), "NPC CPU template")
    namespace = required_text(row.get("template_namespace"), "NPC CPU namespace")
    if namespace not in ("Humanoid", "Generic"):
        raise ValueError("NPC CPU template namespace is unsupported")
    template_namespace = cast(Literal["Humanoid", "Generic"], namespace)
    cpu_sha = required_text(row.get("cpu_source_sha256"), "NPC CPU source SHA256")
    if re.fullmatch(r"[0-9a-f]{64}", cpu_sha) is None:
        raise ValueError("NPC CPU source SHA256 is invalid")
    cpu_source = json_object(row.get("cpu_source"), "NPC CPU source object")
    cpu_source_cab = required_text(cpu_source.get("cab"), "NPC CPU source CAB")
    cpu_source_path_id = required_text(cpu_source.get("path_id"), "NPC CPU source path ID")
    if not cpu_source_cab.casefold().startswith("cab-") or int(cpu_source_path_id) == 0:
        raise ValueError("NPC CPU source object identity is invalid")
    selected = json_object(row.get("selected_avatar"), "selected NPC Avatar")
    cpu = json_object(row.get("cpu_avatar"), "CPU Avatar")
    selected_cab = required_text(selected.get("cab"), "selected Avatar CAB")
    cpu_avatar_cab = required_text(cpu.get("cab"), "CPU Avatar CAB")
    selected_path = required_text(selected.get("path_id"), "selected Avatar path ID")
    cpu_path = required_text(cpu.get("path_id"), "CPU Avatar path ID")
    if selected_cab.casefold() != cpu_avatar_cab.casefold() or selected_path != cpu_path:
        raise ValueError("NPC CPU Avatar pointer differs from the selected AvatarTemplet pointer")
    avatar_binding_status = required_text(row.get("avatar_binding_status"), "NPC Avatar binding status")
    if avatar_binding_status != "cpu_avatar_matches_avatar_templet_size_avatar":
        raise ValueError("NPC CPU Avatar binding status is not source-verified")
    values = row.get("actions")
    if not isinstance(values, list) or not values:
        raise ValueError("NPC CPU reference catalog requires base-clip references")
    actions: list[NpcCpuActionReference] = []
    for value in values:
        source = json_object(value, "NPC CPU clip reference")
        reference = NpcCpuActionReference(*(required_text(source.get(field), "NPC CPU reference " + field)
            for field in NpcCpuActionReference.__dataclass_fields__))
        if (re.fullmatch(r"[0-9a-f]{64}", reference.reference_id) is None
                or int(reference.clip_path_id) == 0
                or not reference.clip_cab.casefold().startswith("cab-")
                or not reference.logical_provider.endswith(".ab")
                or ".." in reference.logical_provider.replace("\\", "/").split("/")
                or source.get("binding_status") != "exact_cpu_base_clip_reference"
                or source.get("execution_available") is not False):
            raise ValueError("NPC CPU reference identity, provider, or execution boundary is invalid")
        identity = [selection.catalog_identity, selection.character_id, selection.template_id, selection.part_name,
                    template, cpu_source_cab.casefold(), cpu_source_path_id, cpu_sha,
                    selected_cab.casefold(), selected_path, reference.state_name,
                    reference.clip_cab.casefold(), reference.clip_path_id, reference.logical_provider]
        expected_id = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        if reference.reference_id != expected_id:
            raise ValueError("NPC CPU reference ID does not match its authenticated source pointers")
        actions.append(reference)
    if (len({action.reference_id for action in actions}) != len(actions)
            or len({action.state_name for action in actions}) != len(actions)):
        raise ValueError("NPC CPU reference catalog contains duplicate state names or reference IDs")
    return NpcCpuActionReferenceCatalog("exact_avatar_base_clip_references", template, template_namespace,
        cpu_source_cab, cpu_source_path_id, cpu_sha, selected_cab, selected_path, avatar_binding_status, tuple(actions))


def parse_npc_action_catalog(value: object, selection: CharacterSelection,
                             part_names: tuple[str, ...]) -> NpcActionCatalog:
    """Reject mismatched source identities before exposing server-owned Action IDs."""
    row = json_object(value, "NPC Action catalog")
    if (row.get("format") not in ("EndfieldNpcAnimationCatalog/1", "EndfieldNpcAnimationCatalog/2")
            or row.get("source_identity") != selection.catalog_identity
            or row.get("npc_id") != selection.character_id
            or row.get("template_id") != selection.template_id
            or row.get("part_name") != selection.part_name):
        raise ValueError("NPC Action catalog does not authenticate this exact selection")
    if not part_names or len(set(part_names)) != len(part_names) or part_names[0] != selection.part_name:
        raise ValueError("NPC Action admission requires the complete ordered PrefabInfo tuple and its host")
    if row["format"] == "EndfieldNpcAnimationCatalog/1":
        if part_names != (selection.part_name,):
            raise ValueError("Legacy single-slot NPC Action receipt cannot authenticate a complete slot group")
    else:
        if (row.get("part_names") != list(part_names) or row.get("host_part_name") != selection.part_name
                or row.get("assembly_action_scope") != "host_action_static_mounted_slots"):
            raise ValueError("NPC Action receipt differs from the complete ordered source slot tuple")
        slots = row.get("slots")
        if (not isinstance(slots, list) or len(slots) != len(part_names)
                or any(not isinstance(slot, dict) for slot in slots)
                or [slot.get("part_name") for slot in slots] != list(part_names)
                or any(slot.get("independent_action_exported") is not False for slot in slots)):
            raise ValueError("NPC Action receipt requires every selected slot and static mounted-action scope")
    status = required_text(row.get("status"), "NPC Action catalog status")
    values = row.get("actions")
    if not isinstance(values, list):
        raise ValueError("NPC Action catalog requires source Action records")
    if status == "avatar_mismatch":
        if row.get("avatar_identity_matches") is not False or values:
            raise ValueError("Mismatched NPC Avatar cannot expose exportable Actions")
        return NpcActionCatalog(status, "CPU 模板 Avatar 与所选配置 Avatar 不同；当前不支持跨 Avatar 重定向。", ())
    retarget = status == "source_humanoid_target_avatar_base_clip_references"
    if (status not in ("exact_avatar_base_clip_references", "source_humanoid_target_avatar_base_clip_references")
            or row.get("avatar_identity_matches") is not (False if retarget else True)
            or (retarget and row.get("avatar_binding_status") != "source_humanoid_to_selected_avatar")):
        raise ValueError("NPC Action catalog did not verify exact CPU and configuration Avatars")
    selected = json_object(row.get("selected_avatar"), "selected NPC Avatar")
    cpu = json_object(row.get("cpu_avatar"), "CPU Avatar")
    same_avatar = (required_text(selected.get("cab"), "selected Avatar CAB").casefold()
            == required_text(cpu.get("cab"), "CPU Avatar CAB").casefold()
            and selected.get("path_id") == cpu.get("path_id"))
    if same_avatar == retarget:
        raise ValueError("NPC Action catalog claims equality for different source Avatars")
    if retarget:
        if row.get("template_namespace") != "Humanoid":
            raise ValueError("Cross-Avatar catalog requires an authenticated Humanoid CPU template")
        for field in ("cpu_avatar_source_sha256", "target_avatar_source_sha256"):
            digest = required_text(row.get(field), field)
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("Cross-Avatar catalog requires both actual Avatar source hashes")
    actions: list[NpcActionSource] = []
    for value in values:
        source = json_object(value, "NPC Action")
        action = NpcActionSource(*(required_text(source.get(field), "NPC Action " + field)
                                  for field in NpcActionSource.__dataclass_fields__))
        if (len(action.action_id) != 64 or len(action.clip_raw_data_sha256) != 64
                or any(character not in "0123456789abcdef" for character in action.action_id + action.clip_raw_data_sha256)
                or not action.resource_path.startswith("assets/") or ".." in action.resource_path.split("/")
                or not action.logical_provider.endswith(".ab") or int(action.clip_path_id) == 0
                or source.get("binding_status") != "exact_cpu_avatar_base_clip_reference_requires_final_clip_binding"):
            raise ValueError("NPC Action identity or source binding status is invalid")
        actions.append(action)
    if len({action.action_id for action in actions}) != len(actions) or len({action.state_name for action in actions}) != len(actions):
        raise ValueError("NPC Action catalog contains duplicate source states or Action IDs")
    return NpcActionCatalog(status, None, tuple(actions))


def read_npc_action_catalog(context: CharacterSourceContext, selection: CharacterSelection,
                            tools: dict[str, Path], defaults: dict[str, object],
                            check_cancelled: Callable[[], None]) -> NpcActionCatalog:
    """Cache source-root inspection; never decode the entire NPC animation collection."""
    validate_character_storage(context.game_root, context.cache_root)
    cache = CharacterDataCache(context.cache_root)
    from endfield_character_npc_catalog import read_independent_npc_catalog
    source = read_independent_npc_catalog(context.game_root, context.provider_index, cache, check_cancelled)
    entries = tuple(row for row in source.entries if row.npc_id == selection.character_id
        and row.source_kind == "config_assembled" and row.resource_reference.template_id == selection.template_id)
    if source.source_identity != selection.catalog_identity or len(entries) != 1:
        raise ValueError("NPC Action selection no longer matches one exact source configuration")
    part_names = tuple(part.part_name for part in entries[0].resource_reference.prefab_parts)
    if not part_names or part_names[0] != selection.part_name:
        raise ValueError("NPC Action selection must identify the source slot group's host")
    paths = (context.provider_index, context.resource_database, context.cab_inventory,
             tools["catalog_inspector"], tools["json_reader"],
             *_npc_inspection_source_paths(Path(str(defaults["repository_src"]))))
    qualifier = hashlib.sha256(json.dumps(asdict(selection), sort_keys=True).encode()).hexdigest()

    def inspect() -> object:
        parent = context.cache_root / "npc_action_sources"
        parent.mkdir(parents=True, exist_ok=True)
        identifier = str(uuid4())
        output = parent / identifier
        temporary = parent / (identifier + "_tmp")
        temporary.mkdir()
        request = parent / (identifier + "_request.json")
        write_index(request, {"format": "EndfieldNpcAnimationCatalogRequest/2",
            "repository_src": defaults["repository_src"], "game_root": str(context.game_root),
            "provider_index": str(context.provider_index), "resource_database": str(context.resource_database),
            "cab_inventory": str(context.cab_inventory), "cache_root": str(context.cache_root),
            "npc_id": selection.character_id, "template_id": selection.template_id,
            "part_name": selection.part_name, "part_names": list(part_names), "source_identity": selection.catalog_identity,
            "unity_version": defaults["unity_version"], "json_reader": str(tools["json_reader"]), "output": str(output)})
        log = parent / (identifier + ".log")
        try:
            run_scene_process([str(defaults["python"]), str(tools["catalog_inspector"]), "--request", str(request)],
                log, {**os.environ, "TEMP": str(temporary), "TMP": str(temporary),
                "PYTHONDONTWRITEBYTECODE": "1"}, check_cancelled)
        except RuntimeError as error:
            check_cancelled()
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 16384))
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
            cause = next((line for line in reversed(lines) if line.strip()), str(error))
            write_index(parent / (identifier + "_failure.json"), {"error_type": type(error).__name__,
                "error": str(error), "reader_cause": cause, "log": str(log)})
            safe = public_npc_failure(RuntimeError(cause))
            raise NpcActionInspectionError(f"NPC source reader failed: {safe}; diagnostic_id={identifier}") from error
        value = json.loads((output / "receipt.json").read_text(encoding="utf-8-sig"))
        parse_npc_action_catalog(value, selection, part_names)
        return value

    value, _ = cache.get_or_build_json("npc_cpu_actions", "exact-cpu-avatar-slot-group-base-clips/2", paths,
        (qualifier, str(defaults["unity_version"])), inspect, check_cancelled)
    return parse_npc_action_catalog(value, selection, part_names)


def read_npc_cpu_action_references(context: CharacterSourceContext, selection: CharacterSelection,
                                   tools: dict[str, Path], defaults: dict[str, object],
                                   check_cancelled: Callable[[], None]) -> NpcCpuActionReferenceCatalog:
    """Cache exact prefab CPU pointers without reading clip objects or enabling export."""
    validate_character_storage(context.game_root, context.cache_root)
    cache = CharacterDataCache(context.cache_root)
    paths = (context.provider_index, context.resource_database, context.cab_inventory,
             tools["catalog_inspector"], tools["json_reader"],
             *_npc_inspection_source_paths(Path(str(defaults["repository_src"]))))
    qualifier = hashlib.sha256(json.dumps(asdict(selection), sort_keys=True).encode()).hexdigest()

    def inspect() -> object:
        parent = context.cache_root / "npc_action_references"
        parent.mkdir(parents=True, exist_ok=True)
        identifier = str(uuid4())
        output = parent / identifier
        temporary = parent / (identifier + "_tmp")
        temporary.mkdir()
        request = parent / (identifier + "_request.json")
        write_index(request, {"format": "EndfieldNpcAnimationCatalogRequest/1",
            "repository_src": defaults["repository_src"], "game_root": str(context.game_root),
            "provider_index": str(context.provider_index), "resource_database": str(context.resource_database),
            "cab_inventory": str(context.cab_inventory), "cache_root": str(context.cache_root),
            "npc_id": selection.character_id, "template_id": selection.template_id,
            "part_name": selection.part_name, "source_identity": selection.catalog_identity,
            "unity_version": defaults["unity_version"], "json_reader": str(tools["json_reader"]),
            "output": str(output)})
        log = parent / (identifier + ".log")
        environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            run_scene_process([str(defaults["python"]), str(tools["catalog_inspector"]),
                "--request", str(request), "--references-only"], log, environment, check_cancelled)
        except RuntimeError as error:
            check_cancelled()
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 16384))
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
            cause = next((line for line in reversed(lines) if line.strip()), str(error))
            write_index(parent / (identifier + "_failure.json"), {"error_type": type(error).__name__,
                "error": str(error), "reader_cause": cause, "log": str(log)})
            safe = public_npc_failure(RuntimeError(cause))
            raise NpcActionInspectionError(f"NPC CPU reference reader failed: {safe}; diagnostic_id={identifier}") from error
        value = json.loads((output / "receipt.json").read_text(encoding="utf-8-sig"))
        parse_npc_cpu_action_references(value, selection)
        return value

    value, _ = cache.get_or_build_json("npc_cpu_action_references", "exact-prefab-cpu-references/1", paths,
        (qualifier, str(defaults["unity_version"])), inspect, check_cancelled)
    return parse_npc_cpu_action_references(value, selection)
