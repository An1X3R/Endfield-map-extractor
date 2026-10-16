"""Inspect exact NPC CPU base-clip references without decoding or guessing actor families."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, fields
import hashlib
import json
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class NpcAnimationCatalogRequest:
    repository_src: Path
    game_root: Path
    provider_index: Path
    resource_database: Path
    cab_inventory: Path
    cache_root: Path
    npc_id: str
    template_id: str
    part_name: str
    part_names: tuple[str, ...] | None
    source_identity: str
    unity_version: str
    json_reader: Path
    output: Path


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_request(path: Path) -> NpcAnimationCatalogRequest:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.get("format") not in ("EndfieldNpcAnimationCatalogRequest/1", "EndfieldNpcAnimationCatalogRequest/2"):
        raise ValueError("Expected EndfieldNpcAnimationCatalogRequest/1 or /2")
    paths = {"repository_src", "game_root", "provider_index", "resource_database", "cab_inventory",
             "cache_root", "json_reader", "output"}
    values: dict[str, object] = {}
    for field in fields(NpcAnimationCatalogRequest):
        if field.name == "part_names":
            names = raw.get("part_names")
            if names is None and raw["format"] == "EndfieldNpcAnimationCatalogRequest/1":
                values[field.name] = None
            elif (isinstance(names, list) and names and all(isinstance(name, str) and name.strip()
                    and name == name.strip() for name in names) and len(set(names)) == len(names)):
                values[field.name] = tuple(names)
            else:
                raise ValueError("Catalog /2 requires an explicit unique exact part_names tuple")
            continue
        value = raw[field.name]
        if not isinstance(value, str) or not value:
            raise TypeError(f"Expected request text: field={field.name}")
        values[field.name] = (path.parent / value).resolve() if field.name in paths else value
    return NpcAnimationCatalogRequest(**values)


def check_cancelled() -> None:
    return None


def inspect_npc_cpu_action_references(request: NpcAnimationCatalogRequest) -> dict[str, object]:
    """Read exact prefab CPU pointers and providers without decoding clip bundles."""
    sys.path.insert(0, str(request.repository_src))
    from endfield_character_cache import CharacterDataCache, source_stamp
    from endfield_character_catalog import _provider_details
    from endfield_character_npc_catalog import read_independent_npc_catalog
    from endfield_character_npc_assembly import NpcSourceObject, _config_object, _config_provider
    from endfield_character_npc_assembly import read_npc_source_objects, selected_npc_renderers
    from endfield_character_npc_animation import _source_pointer, bind_npc_cpu_actions, npc_cpu_animation_resource_path
    from endfield_character_npc_animation import validate_npc_humanoid_target
    from endfield_character_sources import game_source_stamps, validate_character_storage, verify_game_source
    from endfield_scene_export import run_scene_process
    from extractor_core.resources import extract_candidate_bundles

    validate_character_storage(request.game_root, request.output)
    validate_character_storage(request.game_root, request.cache_root)
    if request.output.exists() or request.output.is_symlink():
        raise FileExistsError(request.output)
    if request.unity_version != "2021.3.34f5":
        raise ValueError("NPC source catalog supports Unity 2021.3.34f5 only")
    before = game_source_stamps(request.game_root)
    paths = (request.provider_index, request.resource_database, request.cab_inventory, request.json_reader)
    stamps = tuple(source_stamp(path) for path in paths)
    cache = CharacterDataCache(request.cache_root)
    from endfield_character_enemy_index import prepare_enemy_model_index_from_paths
    enemy_index = prepare_enemy_model_index_from_paths(
        request.game_root, request.provider_index, request.resource_database,
        request.cab_inventory, request.cache_root, request.json_reader,
        request.unity_version, check_cancelled,
    )
    catalog = read_independent_npc_catalog(request.game_root, request.provider_index, cache, enemy_index, check_cancelled)
    if catalog.source_identity != request.source_identity:
        raise ValueError("NPC source catalog changed; select the NPC again")
    from endfield_character_npc_catalog import npc_entity_category
    enemy_entries = tuple(row for row in catalog.entries if row.npc_id == request.npc_id
        and row.resource_reference.template_id == request.template_id and row.source_kind == "prefab"
        and npc_entity_category(row).source_field == "enemyModelPrefab")
    if enemy_entries:
        raise ValueError("Enemy controller clips use the Action catalog endpoint; they are not NPC CPU references")
    matches = tuple(row for row in catalog.entries if row.npc_id == request.npc_id
        and row.resource_reference.template_id == request.template_id and row.source_kind == "prefab"
        and request.part_name in {part.part_name for part in row.resource_reference.prefab_parts})
    if len(matches) != 1:
        raise ValueError("NPC CPU reference catalog requires one exact prefab part selection")
    npc = matches[0]
    reference = npc.resource_reference
    template = reference.cpu_animation_template
    if template is None or reference.avatar_mesh is None or reference.avatar_templet is None:
        raise ValueError("NPC CPU reference requires CPU, AvatarMesh and AvatarTemplet sources")
    cpu_path = npc_cpu_animation_resource_path(template)
    provider_rows, _ = _provider_details(request.provider_index, frozenset((cpu_path,)), check_cancelled)
    if len(provider_rows[cpu_path]) != 1 or len(provider_rows[cpu_path][0].serialized_cabs) != 1:
        raise ValueError("Exact NPC CPU source provider is absent or ambiguous")
    cpu_provider = provider_rows[cpu_path][0]
    roots = tuple(sorted({cpu_provider.logical_name, _config_provider(reference.avatar_mesh).logical_name,
                          _config_provider(reference.avatar_templet).logical_name}))
    request.output.mkdir(parents=True, exist_ok=False)
    temporary = request.output / "temp"
    temporary.mkdir()
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    vfs = request.game_root / "Endfield_Data/StreamingAssets/VFS"

    def read_sources(logical: tuple[str, ...], directory: Path) -> tuple[NpcSourceObject, ...]:
        extract_candidate_bundles(request.resource_database, vfs, list(logical), directory / "bundles")
        run_scene_process([str(request.json_reader), str(directory / "bundles"), str(directory / "source_json"),
            "--game", "ArknightsEndfield", "--unity_version", request.unity_version, "--types", "AssetBundle:Parse",
            "ResourceManager:Parse", "MonoBehaviour", "Avatar", "--export_type", "JSON",
            "--mono_behaviour_type_tree_priority", "SerializedFirst", "--group_assets", "ByType"],
            directory / "reader.log", environment, check_cancelled)
        return read_npc_source_objects(directory / "source_json")

    sources = read_sources(roots, request.output / "configuration")
    selected_npc_renderers(_config_object(sources, reference.avatar_mesh), request.part_name)
    templet = _config_object(sources, reference.avatar_templet)
    selected_avatar = _source_pointer(templet, templet.value.get("sizeAvatar"), "$.sizeAvatar")
    cpu_objects = tuple(row for row in sources if row.metadata.get("type") == "MonoBehaviour"
        and str(row.metadata.get("container")).casefold() == cpu_path
        and str(row.metadata.get("sourceFile")).casefold() == cpu_provider.serialized_cabs[0].name.casefold())
    if len(cpu_objects) != 1:
        raise ValueError("CPU source object is absent or ambiguous")
    cpu = cpu_objects[0]
    binding = bind_npc_cpu_actions(template, cpu, selected_avatar)
    cab_index, _ = cache.read_character_cab_index(request.cab_inventory, check_cancelled)
    providers_by_cab = dict(cab_index.providers)
    avatar_providers = providers_by_cab.get(selected_avatar.cab.casefold(), ())
    if len(avatar_providers) != 1:
        raise ValueError("Exact NPC Avatar CAB provider is absent or ambiguous")
    action_providers: dict[tuple[str, int], str] = {}
    for action in binding.actions:
        providers = providers_by_cab.get(action.pointer.cab.casefold(), ())
        if len(providers) != 1:
            raise ValueError(f"CPU clip CAB provider is absent or ambiguous: state={action.state_name}")
        action_providers[(action.pointer.cab.casefold(), action.pointer.path_id)] = providers[0]
    actions: list[dict[str, object]] = []
    cpu_cab = str(cpu.metadata.get("sourceFile"))
    cpu_path_id = str(cpu.metadata.get("pathId"))
    cpu_sha = str(cpu.metadata.get("rawDataSha256"))
    for action in binding.actions:
        provider = action_providers[(action.pointer.cab.casefold(), action.pointer.path_id)]
        identity = [catalog.source_identity, request.npc_id, request.template_id, request.part_name,
                    template, cpu_cab.casefold(), cpu_path_id, cpu_sha,
                    selected_avatar.cab.casefold(), str(selected_avatar.path_id),
                    action.state_name, action.pointer.cab.casefold(), str(action.pointer.path_id), provider]
        reference_id = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        actions.append({"reference_id": reference_id, "state_name": action.state_name,
            "clip_cab": action.pointer.cab, "clip_path_id": str(action.pointer.path_id),
            "logical_provider": provider, "binding_status": "exact_cpu_base_clip_reference",
            "execution_available": False})
    verify_game_source(request.game_root, before, check_cancelled)
    if tuple(source_stamp(path) for path in paths) != stamps:
        raise ValueError("NPC source indexes or JSON reader changed during CPU reference inspection")
    result = {"format": "EndfieldNpcCpuActionReferences/1", "status": "exact_avatar_base_clip_references",
        "source_kind": "prefab", "npc_id": request.npc_id, "template_id": request.template_id,
        "part_name": request.part_name, "source_identity": catalog.source_identity,
        "cpu_animation_template": binding.template, "template_namespace": binding.template_namespace,
        "cpu_source": {"cab": cpu_cab, "path_id": cpu_path_id}, "cpu_source_sha256": cpu_sha,
        "selected_avatar": {"cab": selected_avatar.cab, "path_id": str(selected_avatar.path_id)},
        "cpu_avatar": {"cab": binding.avatar.cab, "path_id": str(binding.avatar.path_id)},
        "avatar_binding_status": "cpu_avatar_matches_avatar_templet_size_avatar",
        "actions": actions, "execution_available": False, "runtime_animator_reconstructed": False,
        "limitations": ["CPU clip PPtrs and unique CAB providers are verified without decoding AnimationClip objects.",
            "These source references are supplemental metadata, not selectable or exportable actions; runtime Animator, clip track binding and saved Action validation remain unsupported here."]}
    (request.output / "receipt.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    return result


def configured_slot_scope(mesh_source: object, part_names: tuple[str, ...]) -> dict[str, object]:
    """Validate the complete source slot set without enabling independent mounted motion."""
    from endfield_character_npc_assembly import NpcSourceObject, selected_npc_slots, _require_plain_npc_slot
    if not isinstance(mesh_source, NpcSourceObject):
        raise TypeError("Expected exact AvatarMesh source object")
    slots = selected_npc_slots(mesh_source, part_names)
    _require_plain_npc_slot(slots[0])
    return {"assembly_action_scope": "host_action_static_mounted_slots",
        "host_part_name": slots[0].name,
        "avatar_mesh_source_sha256": mesh_source.metadata["rawDataSha256"],
        "slots": [{"part_name": slot.name, "avatar_mesh_slot_index": slot.index,
            "use_self_avatar": slot.use_self_avatar, "self_avatar_guid": list(slot.self_avatar_guid),
            "parent_bone_name": slot.parent_bone_name, "parent_bone_transform_name": slot.parent_bone_transform_name,
            "lod0_renderer_count": len(slot.renderers),
            "independent_action_exported": False} for slot in slots]}


def inspect_npc_animation(request: NpcAnimationCatalogRequest) -> dict[str, object]:
    sys.path.insert(0, str(request.repository_src))
    from endfield_character_cache import CharacterDataCache, source_stamp
    from endfield_character_catalog import _provider_details
    from endfield_character_npc_catalog import read_independent_npc_catalog
    from endfield_character_npc_assembly import NpcSourceObject, _config_object, _config_provider
    from endfield_character_npc_assembly import read_npc_source_objects
    from endfield_character_npc_animation import _source_pointer, bind_npc_cpu_actions, npc_cpu_animation_resource_path
    from endfield_character_npc_animation import NpcAnimationBindingError, validate_npc_generic_target, validate_npc_humanoid_target
    from endfield_character_sources import game_source_stamps, validate_character_storage, verify_game_source
    from endfield_scene_export import run_scene_process
    from extractor_core.resources import extract_candidate_bundles
    validate_character_storage(request.game_root, request.output)
    validate_character_storage(request.game_root, request.cache_root)
    if request.output.exists() or request.output.is_symlink():
        raise FileExistsError(request.output)
    if request.unity_version != "2021.3.34f5":
        raise ValueError("NPC source catalog supports Unity 2021.3.34f5 only")
    before = game_source_stamps(request.game_root)
    paths = (request.provider_index, request.resource_database, request.cab_inventory, request.json_reader)
    stamps = tuple(source_stamp(path) for path in paths)
    cache = CharacterDataCache(request.cache_root)
    from endfield_character_enemy_index import prepare_enemy_model_index_from_paths
    enemy_index = prepare_enemy_model_index_from_paths(
        request.game_root, request.provider_index, request.resource_database,
        request.cab_inventory, request.cache_root, request.json_reader,
        request.unity_version, check_cancelled,
    )
    catalog = read_independent_npc_catalog(request.game_root, request.provider_index, cache, enemy_index, check_cancelled)
    if catalog.source_identity != request.source_identity:
        raise ValueError("NPC source catalog changed; select the NPC again")
    from endfield_character_npc_catalog import npc_entity_category
    enemy_entries = tuple(row for row in catalog.entries if row.npc_id == request.npc_id
        and row.resource_reference.template_id == request.template_id and row.source_kind == "prefab"
        and npc_entity_category(row).source_field == "enemyModelPrefab")
    if enemy_entries:
        from endfield_character_enemy_animation import inspect_enemy_animation
        from endfield_character_enemy_index import read_enemy_model_index
        from endfield_character_selection import CharacterSelection
        from endfield_character_sources import CharacterSourceContext
        parts = tuple(part for row in enemy_entries for part in row.resource_reference.prefab_parts if part.part_name == request.part_name)
        bindings = tuple(row for row in read_enemy_model_index(enemy_index).bindings
                         if row.template_id == request.template_id and row.model_id == request.part_name)
        if len(parts) != 1 or len(parts[0].providers) != 1 or len(bindings) != 1:
            raise ValueError("Enemy action request must identify one exact prefab/model/provider")
        if request.part_names is not None and request.part_names != (request.part_name,):
            raise ValueError("Enemy action request must preserve the exact selected prefab part")
        context = CharacterSourceContext(request.game_root, Path(), request.cache_root, request.output.parent,
                                         request.provider_index, request.resource_database, request.cab_inventory)
        selection = CharacterSelection(catalog.source_identity, request.npc_id, request.template_id, request.part_name,
                                        "npc", parts[0].resource_path, parts[0].providers[0].logical_name)
        return inspect_enemy_animation(context, selection, bindings[0], request.json_reader,
                                        request.unity_version, request.output, check_cancelled)
    matches = tuple(row for row in catalog.entries if row.npc_id == request.npc_id
        and row.resource_reference.template_id == request.template_id and row.source_kind == "config_assembled"
        and request.part_name in {part.part_name for part in row.resource_reference.prefab_parts})
    if len(matches) != 1:
        raise ValueError("NPC action catalog requires one exact source configuration selection")
    npc = matches[0]
    reference = npc.resource_reference
    part_names = tuple(part.part_name for part in reference.prefab_parts)
    if request.part_name != part_names[0] or (request.part_names is not None and request.part_names != part_names):
        raise ValueError("NPC action selection must match the complete ordered PrefabInfo slot tuple and its host")
    template = reference.cpu_animation_template
    if template is None or reference.avatar_mesh is None or reference.avatar_templet is None:
        raise ValueError("NPC action source requires CPU, AvatarMesh and AvatarTemplet references")
    cpu_path = npc_cpu_animation_resource_path(template)
    provider_rows, _ = _provider_details(request.provider_index, frozenset((cpu_path,)), check_cancelled)
    if len(provider_rows[cpu_path]) != 1 or len(provider_rows[cpu_path][0].serialized_cabs) != 1:
        raise ValueError("Exact NPC CPU source provider is absent or ambiguous")
    cpu_provider = provider_rows[cpu_path][0]
    roots = tuple(sorted({cpu_provider.logical_name, _config_provider(reference.avatar_mesh).logical_name,
                          _config_provider(reference.avatar_templet).logical_name}))
    request.output.mkdir(parents=True, exist_ok=False)
    temporary = request.output / "temp"
    temporary.mkdir()
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    vfs = request.game_root / "Endfield_Data/StreamingAssets/VFS"

    def read_sources(logical: tuple[str, ...], directory: Path) -> tuple[NpcSourceObject, ...]:
        extract_candidate_bundles(request.resource_database, vfs, list(logical), directory / "bundles")
        run_scene_process([str(request.json_reader), str(directory / "bundles"), str(directory / "source_json"),
            "--game", "ArknightsEndfield", "--unity_version", request.unity_version, "--types", "AssetBundle:Parse",
            "ResourceManager:Parse", "MonoBehaviour", "Avatar", "AnimationClip", "--export_type", "JSON",
            "--mono_behaviour_type_tree_priority", "SerializedFirst", "--group_assets", "ByType"],
            directory / "reader.log", environment, check_cancelled)
        return read_npc_source_objects(directory / "source_json")

    sources = read_sources(roots, request.output / "configuration")
    slot_scope = configured_slot_scope(_config_object(sources, reference.avatar_mesh), part_names)
    templet = _config_object(sources, reference.avatar_templet)
    selected_avatar = _source_pointer(templet, templet.value.get("sizeAvatar"), "$.sizeAvatar")
    cpus = tuple(row for row in sources if row.metadata.get("type") == "MonoBehaviour"
        and str(row.metadata.get("container")).casefold() == cpu_path
        and str(row.metadata.get("sourceFile")).casefold() == cpu_provider.serialized_cabs[0].name.casefold())
    if len(cpus) != 1:
        raise ValueError("CPU source object is absent or ambiguous")
    cpu = cpus[0]
    cpu_avatar = _source_pointer(cpu, cpu.value.get("avatar"), "$.avatar")
    # The bind helper verifies the exact GameplayTag even when the selected Avatar differs.
    cpu_binding = bind_npc_cpu_actions(template, cpu, cpu_avatar)
    identities_match = selected_avatar.cab.casefold() == cpu_avatar.cab.casefold() and selected_avatar.path_id == cpu_avatar.path_id
    actions: list[dict[str, object]] = []
    unsupported_actions: list[dict[str, object]] = []
    retarget = not identities_match and cpu_binding.template_namespace == "Humanoid"
    generic_retarget = not identities_match and cpu_binding.template_namespace == "Generic"
    avatar_evidence: dict[str, str] = {}
    generic_avatar_equivalence: dict[str, str | int | float] | None = None
    if identities_match or retarget or generic_retarget:
        cab_index, _ = cache.read_character_cab_index(request.cab_inventory, check_cancelled)
        cab_providers = dict(cab_index.providers)
        avatar_providers = cab_providers.get(selected_avatar.cab.casefold(), ())
        if len(avatar_providers) != 1:
            raise ValueError("Exact NPC Avatar CAB provider is absent or ambiguous")
        cpu_avatar_providers = cab_providers.get(cpu_avatar.cab.casefold(), ())
        if len(cpu_avatar_providers) != 1:
            raise ValueError("Exact CPU Avatar CAB provider is absent or ambiguous")
        clip_roots: dict[str, str] = {}
        for action in cpu_binding.actions:
            choices = cab_providers.get(action.pointer.cab.casefold(), ())
            if len(choices) != 1:
                raise ValueError(f"CPU clip CAB provider is absent or ambiguous: state={action.state_name}")
            clip_roots[action.pointer.cab.casefold()] = choices[0]
        if len(clip_roots) > 128:
            raise ValueError("NPC CPU catalog exceeds the explicit 128-provider inspection budget")
        assets = read_sources(tuple(sorted({*clip_roots.values(), avatar_providers[0], cpu_avatar_providers[0]})), request.output / "assets")
        avatars = tuple(row for row in assets if row.metadata.get("type") == "Avatar"
            and str(row.metadata.get("sourceFile")).casefold() == selected_avatar.cab.casefold()
            and row.metadata.get("pathId") == selected_avatar.path_id)
        if len(avatars) != 1:
            raise ValueError("Exact configured and CPU Avatar does not resolve to one actual source object")
        if retarget:
            cpu_avatars = tuple(row for row in assets if row.metadata.get("type") == "Avatar"
                and str(row.metadata.get("sourceFile")).casefold() == cpu_avatar.cab.casefold()
                and row.metadata.get("pathId") == cpu_avatar.path_id)
            if len(cpu_avatars) != 1:
                raise ValueError("Exact CPU Avatar source object is absent or ambiguous")
            validate_npc_humanoid_target(cpu_avatars[0], avatars[0])
            avatar_evidence = {"cpu_avatar_source_sha256": str(cpu_avatars[0].metadata["rawDataSha256"]),
                "target_avatar_source_sha256": str(avatars[0].metadata["rawDataSha256"])}
        elif generic_retarget:
            cpu_avatars = tuple(row for row in assets if row.metadata.get("type") == "Avatar"
                and str(row.metadata.get("sourceFile")).casefold() == cpu_avatar.cab.casefold()
                and row.metadata.get("pathId") == cpu_avatar.path_id)
            if len(cpu_avatars) != 1:
                raise ValueError("Exact CPU Avatar source object is absent or ambiguous")
        resolved_clips: dict[str, NpcSourceObject] = {}
        for action in cpu_binding.actions:
            clips = tuple(row for row in assets if row.metadata.get("type") == "AnimationClip"
                and str(row.metadata.get("sourceFile")).casefold() == action.pointer.cab.casefold()
                and row.metadata.get("pathId") == action.pointer.path_id)
            if len(clips) != 1:
                raise ValueError(f"CPU clip PPtr does not resolve to one actual source object: state={action.state_name}")
            clip = clips[0]
            resource = clip.metadata.get("container")
            if not isinstance(resource, str) or not resource:
                raise ValueError(f"Exact CPU clip has no resource container: state={action.state_name}")
            resolved_clips[action.state_name] = clip
        generic_supported_states: frozenset[str] | None = None
        if generic_retarget:
            generic_validated_clips: list[NpcSourceObject] = []
            generic_validated_states: list[str] = []
            for action in cpu_binding.actions:
                clip = resolved_clips[action.state_name]
                resource = clip.metadata.get("container")
                if not isinstance(resource, str) or not resource:
                    raise ValueError(f"Exact CPU clip has no resource container: state={action.state_name}")
                try:
                    validate_npc_generic_target(cpu_avatars[0], avatars[0], (clip,))
                except NpcAnimationBindingError as error:
                    unsupported_actions.append({
                        "state_name": action.state_name, "clip_name": clip.metadata.get("name"),
                        "resource_path": resource, "logical_provider": clip_roots[action.pointer.cab.casefold()],
                        "clip_cab": action.pointer.cab, "clip_path_id": str(action.pointer.path_id),
                        "clip_raw_data_sha256": clip.metadata["rawDataSha256"],
                        "binding_status": "source_generic_equivalent_avatar_rejected", "reason": str(error),
                    })
                else:
                    generic_validated_clips.append(clip)
                    generic_validated_states.append(action.state_name)
            if not generic_validated_clips:
                reasons = tuple(sorted({str(row["reason"]) for row in unsupported_actions}))
                raise NpcAnimationBindingError(f"No Generic CPU action passed source Avatar equivalence: reasons={reasons[:4]}")
            generic_proof = validate_npc_generic_target(cpu_avatars[0], avatars[0], tuple(generic_validated_clips))
            generic_avatar_equivalence = generic_proof.to_document()
            generic_supported_states = frozenset(generic_validated_states)
            avatar_evidence = {"cpu_avatar_source_sha256": generic_proof.source_avatar_sha256,
                "target_avatar_source_sha256": generic_proof.target_avatar_sha256}
        for action in cpu_binding.actions:
            if generic_retarget and generic_supported_states is not None and action.state_name not in generic_supported_states:
                continue
            clip = resolved_clips[action.state_name]
            resource = clip.metadata.get("container")
            if not isinstance(resource, str) or not resource:
                raise ValueError(f"Exact CPU clip has no resource container: state={action.state_name}")
            identity = [catalog.source_identity, request.npc_id, request.template_id, request.part_name,
                        cpu.metadata["rawDataSha256"], selected_avatar.cab.casefold(), selected_avatar.path_id,
                        action.state_name, action.pointer.cab.casefold(), action.pointer.path_id, clip.metadata["rawDataSha256"]]
            action_id = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
            actions.append({"action_id": action_id, "state_name": action.state_name, "name": clip.metadata.get("name"),
                "resource_path": resource, "logical_provider": clip_roots[action.pointer.cab.casefold()],
                "clip_cab": action.pointer.cab, "clip_path_id": str(action.pointer.path_id),
                "clip_raw_data_sha256": clip.metadata["rawDataSha256"],
                "binding_status": "source_generic_equivalent_avatar_base_clip_reference_requires_final_clip_binding"
                    if generic_retarget else "exact_cpu_avatar_base_clip_reference_requires_final_clip_binding"})
    verify_game_source(request.game_root, before, check_cancelled)
    if tuple(source_stamp(path) for path in paths) != stamps:
        raise ValueError("NPC source indexes or JSON reader changed during action inspection")
    result = {"format": "EndfieldNpcAnimationCatalog/2", "status": "exact_avatar_base_clip_references" if identities_match else "source_humanoid_target_avatar_base_clip_references" if retarget else "source_generic_equivalent_avatar_base_clip_references" if generic_retarget else "avatar_mismatch",
        "npc_id": request.npc_id, "template_id": request.template_id, "part_name": request.part_name,
        "part_names": list(part_names), **slot_scope,
        "source_identity": catalog.source_identity, "cpu_animation_template": template,
        "template_namespace": cpu_binding.template_namespace, **avatar_evidence,
        **({"generic_avatar_equivalence": generic_avatar_equivalence} if generic_avatar_equivalence is not None else {}),
        "cpu_source_sha256": cpu.metadata["rawDataSha256"], "avatar_templet_source_sha256": templet.metadata["rawDataSha256"],
        "selected_avatar": {"cab": selected_avatar.cab, "path_id": str(selected_avatar.path_id)},
        "cpu_avatar": {"cab": cpu_avatar.cab, "path_id": str(cpu_avatar.path_id)},
        "avatar_identity_matches": identities_match, "avatar_binding_status": "source_humanoid_to_selected_avatar" if retarget else "source_generic_equivalent_avatar" if generic_retarget else "exact_avatar", "actions": actions,
        "unsupported_actions": unsupported_actions, "runtime_animator_reconstructed": False,
        "reason": None if identities_match or retarget or generic_retarget else "CPU Avatar differs from AvatarTemplet.sizeAvatar; cross-Avatar retargeting is not verified.",
        "limitations": ["Exact base-clip references do not prove compatible transform CRCs, supported materials or live Animator state; final reader and saved Action validation remain mandatory.",
                        "The CPU clip drives the selected host Avatar; mounted self-Avatar slots retain calibrated static local poses and follow their source parent. Independent mounted-slot actions and live visibility are not reconstructed. Generic clip layouts still require the existing exact reader admission."]}
    (request.output / "receipt.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--references-only", action="store_true")
    arguments = parser.parse_args()
    request = read_request(arguments.request.resolve())
    result = inspect_npc_cpu_action_references(request) if arguments.references_only else inspect_npc_animation(request)
    if arguments.references_only:
        summary = {"status": result["status"], "actions": len(result["actions"]),
                   "execution_available": result["execution_available"]}
    else:
        summary = {"status": result["status"], "actions": len(result["actions"]),
                   "avatar_identity_matches": result.get("avatar_identity_matches")}
    print(json.dumps(summary), flush=True)
