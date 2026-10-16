"""Export an exact CPU base action for a configuration or prefab NPC."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, fields
import hashlib
import json
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class NpcActionRequest:
    repository_src: Path
    game_root: Path
    provider_index: Path
    resource_database: Path
    cab_inventory: Path
    cache_root: Path
    assembly_input: Path
    avatar_source_directory: Path
    npc_id: str
    template_id: str
    part_name: str
    source_identity: str
    state_name: str
    unity_version: str
    json_reader: Path
    animation_exporter: Path
    animation_reader: Path
    python: Path
    dotnet: Path
    maximum_stored_frames: int
    output: Path


@dataclass(frozen=True)
class NpcPrefabActionRequest:
    repository_src: Path
    game_root: Path
    provider_index: Path
    resource_database: Path
    cab_inventory: Path
    cache_root: Path
    catalog_receipt: Path
    reference_id: str
    static_source_audit: Path
    preparation_receipt: Path
    npc_id: str
    template_id: str
    part_name: str
    source_identity: str
    unity_version: str
    json_reader: Path
    animation_exporter: Path
    animation_reader: Path
    python: Path
    dotnet: Path
    maximum_stored_frames: int
    output: Path


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _request_path(base: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Expected nonempty request path: field={field}")
    return (base / value).resolve()


def read_prefab_request(path: Path, raw: dict[str, object]) -> NpcPrefabActionRequest:
    if raw.get("source_kind") != "prefab":
        raise ValueError("EndfieldNpcAnimationRequest/2 requires source_kind=prefab")
    selection = raw.get("selection")
    reference = raw.get("reference")
    if not isinstance(selection, dict) or not isinstance(reference, dict):
        raise TypeError("Prefab request requires selection and reference objects")
    selection_fields = {"npc_id", "template_id", "part_name", "source_identity"}
    if any(not isinstance(selection.get(field), str) or not selection[field].strip() for field in selection_fields):
        raise TypeError("Prefab request selection requires nonempty npc_id, template_id, part_name, and source_identity")
    if any(not isinstance(reference.get(field), str) or not reference[field].strip()
           for field in ("catalog_receipt", "reference_id")):
        raise TypeError("Prefab request reference requires nonempty catalog_receipt and reference_id")
    maximum_stored_frames = raw.get("maximum_stored_frames")
    if type(maximum_stored_frames) is not int or maximum_stored_frames < 2:
        raise ValueError("maximum_stored_frames must be an explicit integer of at least two")
    path_fields = ("repository_src", "game_root", "provider_index", "resource_database", "cab_inventory",
                   "cache_root", "static_source_audit", "preparation_receipt", "json_reader",
                   "animation_exporter", "animation_reader", "python", "dotnet", "output")
    paths = {field: _request_path(path.parent, raw.get(field), field) for field in path_fields}
    return NpcPrefabActionRequest(
        repository_src=paths["repository_src"], game_root=paths["game_root"],
        provider_index=paths["provider_index"], resource_database=paths["resource_database"],
        cab_inventory=paths["cab_inventory"], cache_root=paths["cache_root"],
        catalog_receipt=_request_path(path.parent, reference["catalog_receipt"], "reference.catalog_receipt"),
        reference_id=reference["reference_id"], static_source_audit=paths["static_source_audit"],
        preparation_receipt=paths["preparation_receipt"], npc_id=selection["npc_id"],
        template_id=selection["template_id"], part_name=selection["part_name"],
        source_identity=selection["source_identity"], unity_version=_required_text(raw, "unity_version"),
        json_reader=paths["json_reader"], animation_exporter=paths["animation_exporter"],
        animation_reader=paths["animation_reader"], python=paths["python"], dotnet=paths["dotnet"],
        maximum_stored_frames=maximum_stored_frames, output=paths["output"])


def _required_text(raw: dict[str, object], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Expected nonempty request text: field={field}")
    return value


def read_request(path: Path) -> NpcActionRequest | NpcPrefabActionRequest:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise TypeError("Expected an NPC animation request object")
    if raw.get("format") == "EndfieldNpcAnimationRequest/2":
        return read_prefab_request(path, raw)
    if raw.get("format") != "EndfieldNpcAnimationRequest/1":
        raise ValueError("Expected EndfieldNpcAnimationRequest/1 or EndfieldNpcAnimationRequest/2")
    path_fields = {"repository_src", "game_root", "provider_index", "resource_database", "cab_inventory",
                   "cache_root", "assembly_input", "avatar_source_directory", "json_reader", "animation_exporter",
                   "animation_reader", "python", "dotnet", "output"}
    values: dict[str, object] = {}
    for field in fields(NpcActionRequest):
        value = raw[field.name]
        if field.name == "maximum_stored_frames":
            if type(value) is not int or value < 2:
                raise ValueError("maximum_stored_frames must be an explicit integer of at least two")
        elif not isinstance(value, str) or not value.strip():
            raise TypeError(f"Expected nonempty request text: field={field.name}")
        values[field.name] = (path.parent / value).resolve() if field.name in path_fields else value
    return NpcActionRequest(**values)


def check_cancelled() -> None:
    return None


def export_configured_npc_animation(request: NpcActionRequest) -> dict[str, object]:
    sys.path.insert(0, str(request.repository_src))
    from endfield_character_cache import CharacterDataCache, source_stamp
    from endfield_character_catalog import _provider_details
    from endfield_character_npc_catalog import read_independent_npc_catalog
    from endfield_character_npc_assembly import NpcSourceObject, read_npc_source_objects, _object, _rows, _text
    from endfield_character_npc_animation import NpcAnimationPointer, _source_pointer, bind_npc_cpu_actions, npc_cpu_animation_resource_path, npc_avatar_source_transforms
    from endfield_character_npc_animation import validate_npc_generic_target, validate_npc_humanoid_target
    from endfield_character_sources import game_source_stamps, validate_character_storage, verify_game_source
    from endfield_scene_export import run_scene_process
    from extractor_core.resources import extract_candidate_bundles
    validate_character_storage(request.game_root, request.output)
    validate_character_storage(request.game_root, request.cache_root)
    if request.output.exists() or request.output.is_symlink():
        raise FileExistsError(request.output)
    if request.unity_version != "2021.3.34f5":
        raise ValueError("NPC source reader supports Unity 2021.3.34f5 only")
    before = game_source_stamps(request.game_root)
    inputs = (request.provider_index, request.resource_database, request.cab_inventory, request.assembly_input,
              request.json_reader, request.animation_exporter, request.animation_reader)
    stamps = tuple(source_stamp(path) for path in inputs)
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
    selections = tuple(row for row in catalog.entries if row.npc_id == request.npc_id
        and row.resource_reference.template_id == request.template_id and row.source_kind == "config_assembled"
        and request.part_name in {part.part_name for part in row.resource_reference.prefab_parts})
    if len(selections) != 1:
        raise ValueError("NPC action requires one exact configuration selection")
    npc = selections[0]
    template = npc.resource_reference.cpu_animation_template
    if template is None:
        raise ValueError("Selected NPC has no source CPU animation template")
    assembly = _object(json.loads(request.assembly_input.read_text(encoding="utf-8-sig")), "assembly input")
    selected = _object(assembly.get("selection"), "selection")
    if assembly.get("format") != "EndfieldNpcAssemblyInput/1" or any(selected.get(field) != value for field, value in
            (("character_id", request.npc_id), ("npc_template_id", request.template_id), ("part_name", request.part_name))):
        raise ValueError("Assembly input belongs to a different NPC selection")
    avatar = _object(assembly.get("avatar"), "avatar")
    avatar_key = NpcAnimationPointer(_text(avatar.get("cab"), "Avatar CAB"), int(_text(avatar.get("path_id"), "Avatar PathID")))
    avatar_logical = _text(_object(avatar.get("source_fbx"), "source_fbx").get("logical_name"), "Avatar provider")
    bundles = _rows(assembly.get("bundles"), "bundles")
    for bundle in bundles:
        path = Path(_text(bundle.get("path"), "bundle path"))
        if path.stat().st_size != bundle.get("bytes") or sha(path) != bundle.get("sha256"):
            raise ValueError(f"NPC assembly bundle changed: path={path}")
    avatar_bundles = tuple(Path(_text(row.get("path"), "bundle path")) for row in bundles
                           if str(row.get("path")).replace("\\", "/").endswith("/" + avatar_logical))
    if len(avatar_bundles) != 1:
        raise ValueError("Exact assembly Avatar provider is absent or duplicated")
    avatar_objects = tuple(row for row in read_npc_source_objects(request.avatar_source_directory)
        if row.metadata.get("type") == "Avatar" and str(row.metadata.get("sourceFile")).casefold() == avatar_key.cab.casefold()
        and row.metadata.get("pathId") == avatar_key.path_id and row.metadata.get("rawDataSha256") == avatar.get("raw_data_sha256"))
    if len(avatar_objects) != 1:
        raise ValueError("Exact assembly Avatar source JSON is absent or duplicated")
    cpu_path = npc_cpu_animation_resource_path(template)
    providers, _ = _provider_details(request.provider_index, frozenset((cpu_path,)), check_cancelled)
    if len(providers[cpu_path]) != 1 or len(providers[cpu_path][0].serialized_cabs) != 1:
        raise ValueError("Exact NPC CPU template source provider is absent or ambiguous")
    request.output.mkdir(parents=True, exist_ok=False)
    temporary = request.output / "temp"
    temporary.mkdir()
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    vfs_root = request.game_root / "Endfield_Data/StreamingAssets/VFS"

    def read_root(logical: str, directory: Path) -> tuple[NpcSourceObject, ...]:
        extract_candidate_bundles(request.resource_database, vfs_root, [logical], directory / "bundles")
        run_scene_process([str(request.json_reader), str(directory / "bundles"), str(directory / "source_json"),
            "--game", "ArknightsEndfield", "--unity_version", request.unity_version, "--types", "AssetBundle:Parse",
            "ResourceManager:Parse", "MonoBehaviour", "Avatar", "AnimationClip", "--export_type", "JSON",
            "--mono_behaviour_type_tree_priority", "SerializedFirst", "--group_assets", "ByType"],
            directory / "reader.log", environment, check_cancelled)
        return read_npc_source_objects(directory / "source_json")

    cpu_logical = providers[cpu_path][0].logical_name
    cpu_objects = tuple(row for row in read_root(cpu_logical, request.output / "cpu")
        if row.metadata.get("type") == "MonoBehaviour" and str(row.metadata.get("container")).casefold() == cpu_path
        and str(row.metadata.get("sourceFile")).casefold() == providers[cpu_path][0].serialized_cabs[0].name.casefold())
    if len(cpu_objects) != 1:
        raise ValueError("Exact CPU source JSON is absent or duplicated")
    cpu_avatar_key = _source_pointer(cpu_objects[0], cpu_objects[0].value.get("avatar"), "$.avatar")
    binding = bind_npc_cpu_actions(template, cpu_objects[0], cpu_avatar_key)
    same_avatar = cpu_avatar_key == avatar_key
    cab_index, _ = cache.read_character_cab_index(request.cab_inventory, check_cancelled)
    cpu_avatar_providers = dict(cab_index.providers).get(cpu_avatar_key.cab.casefold(), ())
    if len(cpu_avatar_providers) != 1:
        raise ValueError("Exact CPU Avatar CAB provider is absent or ambiguous")
    cpu_avatar_logical = cpu_avatar_providers[0]
    cpu_avatars = tuple(row for row in read_root(cpu_avatar_logical, request.output / "cpu_avatar")
        if row.metadata.get("type") == "Avatar" and str(row.metadata.get("sourceFile")).casefold() == cpu_avatar_key.cab.casefold()
        and row.metadata.get("pathId") == cpu_avatar_key.path_id)
    if len(cpu_avatars) != 1:
        raise ValueError("Exact CPU Avatar source object is absent or ambiguous")
    generic_retarget = not same_avatar and binding.template_namespace == "Generic"
    if not same_avatar:
        if binding.template_namespace == "Humanoid":
            validate_npc_humanoid_target(cpu_avatars[0], avatar_objects[0])
        elif not generic_retarget:
            raise ValueError("Cross-Avatar CPU actions require an exact supported Generic or Humanoid namespace")
    actions = tuple(action for action in binding.actions if action.state_name == request.state_name)
    if len(actions) != 1:
        raise ValueError("Selected CPU base state is absent or duplicated")
    cab_index, _ = cache.read_character_cab_index(request.cab_inventory, check_cancelled)
    clip_providers = dict(cab_index.providers).get(actions[0].pointer.cab.casefold(), ())
    if len(clip_providers) != 1:
        raise ValueError("Exact CPU clip CAB provider is absent or ambiguous")
    clip_logical = clip_providers[0]
    clips = tuple(row for row in read_root(clip_logical, request.output / "clip")
        if row.metadata.get("type") == "AnimationClip" and row.metadata.get("pathId") == actions[0].pointer.path_id
        and str(row.metadata.get("sourceFile")).casefold() == actions[0].pointer.cab.casefold())
    if len(clips) != 1:
        raise ValueError("Exact CPU clip PPtr target is absent or duplicated")
    generic_proof = validate_npc_generic_target(cpu_avatars[0], avatar_objects[0], (clips[0],)) if generic_retarget else None
    root = "NPC_Root"

    def evidence(path: Path) -> dict[str, str]:
        return {"path": str(path), "sha256": sha(path)}

    source = {"format": "EndfieldNpcAnimationSource/1", "runtime_animator_reconstructed": False,
        "character_id": request.npc_id, "catalog_source_identity": catalog.source_identity,
        "prefab_info_sha256": npc.resource_reference.prefab_info_sha256, "cpu_animation_template": template,
        "state_name": request.state_name, "cpu_bundle": str(request.output / "cpu/bundles" / cpu_logical),
        "assembly_input": evidence(request.assembly_input), "cpu_source": evidence(cpu_objects[0].path),
        "avatar_source": evidence(avatar_objects[0].path), "clip_source": evidence(clips[0].path),
        "cpu_avatar_source": evidence(cpu_avatars[0].path),
        "cpu_avatar_bundle": str(request.output / "cpu_avatar/bundles" / cpu_avatar_logical),
        "template_namespace": binding.template_namespace,
        "avatar_binding_status": "exact_avatar" if same_avatar else "source_generic_equivalent_avatar" if generic_retarget else "source_humanoid_to_selected_avatar",
        **({"cpu_avatar_source_sha256": generic_proof.source_avatar_sha256,
            "target_avatar_source_sha256": generic_proof.target_avatar_sha256,
            "generic_avatar_equivalence": generic_proof.to_document()} if generic_proof is not None else {}),
        "transforms": npc_avatar_source_transforms(avatar_objects[0], root),
        "scope": "Actual Avatar default local hierarchy and CPU base clip; no GameObject hierarchy or effective Animator state."}
    source_path = request.output / "npc_source.json"
    source_path.write_text(json.dumps(source, indent=2, allow_nan=False), encoding="utf-8")
    animation = {"format": "EndfieldAnimationExportRequest/1", "character_id": request.npc_id,
        "avatar_bundle": str(avatar_bundles[0]), "avatar_cab": avatar_key.cab, "avatar_path_id": avatar_key.path_id,
        "clip_bundle": str(request.output / "clip/bundles" / clip_logical), "resource_path": clips[0].metadata["container"],
        "source_audit": str(source_path), "animator_root_path": root, "controller_bundle": None,
        "unity_version": request.unity_version, "maximum_stored_frames": request.maximum_stored_frames,
        "output": str(request.output / "animation"), "dotnet": str(request.dotnet), "reader": str(request.animation_reader)}
    animation_path = request.output / "animation_request.json"
    animation_path.write_text(json.dumps(animation, indent=2), encoding="utf-8")
    run_scene_process([str(request.python), str(request.animation_exporter), "--request", str(animation_path)],
                      request.output / "animation.log", environment, check_cancelled)
    verify_game_source(request.game_root, before, check_cancelled)
    if tuple(source_stamp(path) for path in inputs) != stamps:
        raise ValueError("NPC action source indexes, assembly input or tools changed during export")
    result = {"format": "EndfieldNpcAnimationReceipt/1", "status": "exact_cpu_base_clip_exported",
        "npc_id": request.npc_id, "template_id": request.template_id, "part_name": request.part_name,
        "catalog_source_identity": catalog.source_identity, "state_name": request.state_name,
        "cpu_animation_template": template, "avatar_identity_matches": same_avatar,
        "template_namespace": binding.template_namespace,
        "avatar_binding_status": "exact_avatar" if same_avatar else "source_generic_equivalent_avatar" if generic_retarget else "source_humanoid_to_selected_avatar",
        **({"cpu_avatar_source_sha256": generic_proof.source_avatar_sha256,
            "target_avatar_source_sha256": generic_proof.target_avatar_sha256,
            "generic_avatar_equivalence": generic_proof.to_document()} if generic_proof is not None else {}),
        "cpu_avatar": {"cab": cpu_avatar_key.cab, "path_id": str(cpu_avatar_key.path_id)},
        "selected_avatar": {"cab": avatar_key.cab, "path_id": str(avatar_key.path_id)},
        "runtime_animator_reconstructed": False,
        "source": evidence(source_path), "animation_receipt": evidence(request.output / "animation/receipt.json"),
        "pending": ["Source Avatar compatibility does not prove final decoded clip channels, saved Action playback, full multi-slot composition, montage/controller state or live runtime Animator reconstruction."]}
    (request.output / "receipt.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def export_prefab_npc_animation(request: NpcPrefabActionRequest) -> dict[str, object]:
    sys.path.insert(0, str(request.repository_src))
    from endfield_character_cache import CharacterDataCache, source_stamp
    from endfield_character_catalog import _provider_details
    from endfield_character_npc_catalog import read_independent_npc_catalog
    from endfield_character_npc_assembly import NpcSourceObject, read_npc_source_objects, _object, _rows, _text
    from endfield_character_npc_animation import NpcAnimationPointer, bind_npc_cpu_actions, npc_cpu_animation_resource_path, npc_avatar_source_transforms
    from endfield_character_sources import game_source_stamps, validate_character_storage, verify_game_source
    from endfield_scene_export import run_scene_process
    from extractor_core.resources import extract_candidate_bundles

    validate_character_storage(request.game_root, request.output)
    validate_character_storage(request.game_root, request.cache_root)
    if request.output.exists() or request.output.is_symlink():
        raise FileExistsError(request.output)
    if request.unity_version != "2021.3.34f5":
        raise ValueError("NPC source reader supports Unity 2021.3.34f5 only")
    receipt = _object(json.loads(request.catalog_receipt.read_text(encoding="utf-8-sig")), "CPU action reference receipt")
    if receipt.get("format") != "EndfieldNpcCpuActionReferences/1" or receipt.get("status") != "exact_avatar_base_clip_references":
        raise ValueError("Prefab request requires EndfieldNpcCpuActionReferences/1 with exact source references")
    if receipt.get("source_kind") != "prefab" or receipt.get("execution_available") is not False or receipt.get("runtime_animator_reconstructed") is not False:
        raise ValueError("CPU action reference receipt must be supplemental prefab source evidence")
    if any(receipt.get(field) != value for field, value in (
            ("npc_id", request.npc_id), ("template_id", request.template_id),
            ("part_name", request.part_name), ("source_identity", request.source_identity))):
        raise ValueError("CPU action reference receipt belongs to a different prefab selection")
    if not isinstance(receipt.get("cpu_animation_template"), str) or not receipt["cpu_animation_template"]:
        raise ValueError("CPU action reference receipt has no animation template")
    template = receipt["cpu_animation_template"]
    if not isinstance(receipt.get("cpu_source_sha256"), str) or not receipt["cpu_source_sha256"]:
        raise ValueError("CPU action reference receipt has no CPU source hash")
    cpu_reference = _object(receipt.get("cpu_source"), "CPU source pointer")
    avatar_reference = _object(receipt.get("selected_avatar"), "selected Avatar pointer")
    cpu_avatar = _object(receipt.get("cpu_avatar"), "CPU Avatar pointer")
    avatar_key = NpcAnimationPointer(_text(avatar_reference.get("cab"), "selected Avatar CAB"),
        int(_text(avatar_reference.get("path_id"), "selected Avatar PathID")))
    if (str(cpu_avatar.get("cab", "")).casefold() != avatar_key.cab.casefold()
            or int(_text(cpu_avatar.get("path_id"), "CPU Avatar PathID")) != avatar_key.path_id
            or receipt.get("avatar_binding_status") != "cpu_avatar_matches_avatar_templet_size_avatar"):
        raise ValueError("CPU Avatar differs from the selected prefab Avatar")
    reference_rows = _rows(receipt.get("actions"), "CPU action references")
    selected_references = tuple(_object(row, "CPU action reference") for row in reference_rows
        if isinstance(row, dict) and row.get("reference_id") == request.reference_id)
    if len(selected_references) != 1:
        raise ValueError("Prefab reference_id is absent or duplicated in the CPU action catalog")
    selected_reference = selected_references[0]
    if selected_reference.get("binding_status") != "exact_cpu_base_clip_reference" or selected_reference.get("execution_available") is not False:
        raise ValueError("Selected CPU action is not an authenticated supplemental base-clip reference")
    state_name = _text(selected_reference.get("state_name"), "CPU base state name")
    reference_clip = NpcAnimationPointer(_text(selected_reference.get("clip_cab"), "CPU clip CAB"),
        int(_text(selected_reference.get("clip_path_id"), "CPU clip PathID")))
    reference_provider = _text(selected_reference.get("logical_provider"), "CPU clip provider")

    preparation = _object(json.loads(request.preparation_receipt.read_text(encoding="utf-8-sig")), "NPC preparation receipt")
    if preparation.get("format") != "EndfieldIndependentNpcPreparation/1" or any(
            preparation.get(field) != value for field, value in (
                ("source_identity", request.source_identity), ("npc_id", request.npc_id), ("part_name", request.part_name))):
        raise ValueError("Preparation receipt belongs to a different prefab source or part")
    static_audit = _object(json.loads(request.static_source_audit.read_text(encoding="utf-8-sig")), "static source audit")
    if static_audit.get("format") != "EndfieldCharacterSourceAudit/1":
        raise ValueError("Prefab animation requires EndfieldCharacterSourceAudit/1")
    animators = _rows(static_audit.get("animators"), "static audit animators")
    selected_animators = tuple(_object(row, "static audit animator") for row in animators
        if isinstance(row, dict)
        and isinstance(row.get("object"), dict)
        and row["object"].get("name") == request.part_name)
    if len(selected_animators) != 1:
        raise ValueError("Static source audit must contain one Animator for the selected prefab part")
    resolved_avatar = _object(_object(selected_animators[0].get("avatar"), "static Animator Avatar").get("resolved"),
        "resolved static Animator Avatar")
    if (resolved_avatar.get("type") != "Avatar"
            or str(resolved_avatar.get("source", "")).casefold() != avatar_key.cab.casefold()
            or int(resolved_avatar.get("path_id", 0)) != avatar_key.path_id):
        raise ValueError("Static prefab Animator Avatar differs from the exact CPU-selected Avatar")

    before = game_source_stamps(request.game_root)
    input_paths = (request.provider_index, request.resource_database, request.cab_inventory, request.catalog_receipt,
        request.static_source_audit, request.preparation_receipt, request.json_reader,
        request.animation_exporter, request.animation_reader)
    stamps = tuple(source_stamp(path) for path in input_paths)
    cache = CharacterDataCache(request.cache_root)
    from endfield_character_enemy_index import prepare_enemy_model_index_from_paths
    enemy_index = prepare_enemy_model_index_from_paths(
        request.game_root, request.provider_index, request.resource_database,
        request.cab_inventory, request.cache_root, request.json_reader,
        request.unity_version, check_cancelled,
    )
    catalog = read_independent_npc_catalog(request.game_root, request.provider_index, cache, enemy_index, check_cancelled)
    if catalog.source_identity != request.source_identity:
        raise ValueError("NPC source catalog changed; select the prefab again")
    selections = tuple(row for row in catalog.entries if row.npc_id == request.npc_id
        and row.resource_reference.template_id == request.template_id and row.source_kind == "prefab"
        and request.part_name in {part.part_name for part in row.resource_reference.prefab_parts})
    if len(selections) != 1:
        raise ValueError("Prefab action requires one exact catalog selection")
    npc = selections[0]
    if npc.resource_reference.cpu_animation_template != template:
        raise ValueError("CPU animation template differs from the current prefab catalog")

    cpu_path = npc_cpu_animation_resource_path(template)
    cpu_providers, _ = _provider_details(request.provider_index, frozenset((cpu_path,)), check_cancelled)
    if len(cpu_providers[cpu_path]) != 1 or len(cpu_providers[cpu_path][0].serialized_cabs) != 1:
        raise ValueError("Exact NPC CPU template source provider is absent or ambiguous")
    cab_index, _ = cache.read_character_cab_index(request.cab_inventory, check_cancelled)
    providers_by_cab = dict(cab_index.providers)
    avatar_providers = providers_by_cab.get(avatar_key.cab.casefold(), ())
    if len(avatar_providers) != 1:
        raise ValueError("Exact prefab Avatar CAB provider is absent or ambiguous")
    avatar_logical = avatar_providers[0]
    clip_providers = providers_by_cab.get(reference_clip.cab.casefold(), ())
    if len(clip_providers) != 1 or clip_providers[0] != reference_provider:
        raise ValueError("CPU reference clip provider differs from the current unique CAB inventory provider")
    clip_logical = clip_providers[0]

    request.output.mkdir(parents=True, exist_ok=False)
    temporary = request.output / "temp"
    temporary.mkdir()
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    vfs_root = request.game_root / "Endfield_Data/StreamingAssets/VFS"

    def read_root(logical: str, directory: Path) -> tuple[NpcSourceObject, ...]:
        extract_candidate_bundles(request.resource_database, vfs_root, [logical], directory / "bundles")
        run_scene_process([str(request.json_reader), str(directory / "bundles"), str(directory / "source_json"),
            "--game", "ArknightsEndfield", "--unity_version", request.unity_version, "--types", "AssetBundle:Parse",
            "ResourceManager:Parse", "MonoBehaviour", "Avatar", "AnimationClip", "--export_type", "JSON",
            "--mono_behaviour_type_tree_priority", "SerializedFirst", "--group_assets", "ByType"],
            directory / "reader.log", environment, check_cancelled)
        return read_npc_source_objects(directory / "source_json")

    cpu_provider = cpu_providers[cpu_path][0]
    cpu_objects = tuple(row for row in read_root(cpu_provider.logical_name, request.output / "cpu")
        if row.metadata.get("type") == "MonoBehaviour"
        and str(row.metadata.get("container")).casefold() == cpu_path.casefold()
        and str(row.metadata.get("sourceFile")).casefold() == cpu_provider.serialized_cabs[0].name.casefold())
    if len(cpu_objects) != 1:
        raise ValueError("Exact prefab CPU source JSON is absent or duplicated")
    cpu = cpu_objects[0]
    cpu_meta = cpu.metadata
    cpu_cab = _text(cpu_meta.get("sourceFile"), "CPU source CAB")
    cpu_path_id = str(cpu_meta.get("pathId"))
    cpu_sha = _text(cpu_meta.get("rawDataSha256"), "CPU source hash")
    if (cpu_sha.casefold() != str(receipt["cpu_source_sha256"]).casefold()
            or cpu_cab.casefold() != str(cpu_reference.get("cab", "")).casefold()
            or cpu_path_id != str(cpu_reference.get("path_id"))):
        raise ValueError("Current CPU source identity differs from the selected reference catalog")
    binding = bind_npc_cpu_actions(template, cpu, avatar_key)
    if (binding.avatar.cab.casefold() != avatar_key.cab.casefold()
            or binding.avatar.path_id != avatar_key.path_id
            or binding.template_namespace != receipt.get("template_namespace")):
        raise ValueError("Current CPU source Avatar or template namespace differs from the reference catalog")
    actions = tuple(action for action in binding.actions if action.state_name == state_name)
    if len(actions) != 1:
        raise ValueError("Selected CPU base state is absent or duplicated in the current CPU source")
    action = actions[0]
    if (action.pointer.cab.casefold() != reference_clip.cab.casefold()
            or action.pointer.path_id != reference_clip.path_id):
        raise ValueError("Current CPU base state points to a different clip than the selected reference")
    identity = [catalog.source_identity, request.npc_id, request.template_id, request.part_name,
        template, cpu_cab.casefold(), cpu_path_id, cpu_sha,
        avatar_key.cab.casefold(), str(avatar_key.path_id), state_name,
        action.pointer.cab.casefold(), str(action.pointer.path_id), clip_logical]
    computed_reference_id = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    if computed_reference_id != request.reference_id:
        raise ValueError("Selected reference_id does not match the current exact CPU source and provider identity")

    avatar_objects = tuple(row for row in read_root(avatar_logical, request.output / "avatar")
        if row.metadata.get("type") == "Avatar"
        and str(row.metadata.get("sourceFile")).casefold() == avatar_key.cab.casefold()
        and row.metadata.get("pathId") == avatar_key.path_id)
    if len(avatar_objects) != 1:
        raise ValueError("Exact selected prefab Avatar source JSON is absent or duplicated")
    avatar_source = avatar_objects[0]
    avatar_meta = avatar_source.metadata
    avatar_raw_sha = _text(avatar_meta.get("rawDataSha256"), "Avatar source hash")

    clips = tuple(row for row in read_root(clip_logical, request.output / "clip")
        if row.metadata.get("type") == "AnimationClip"
        and row.metadata.get("pathId") == reference_clip.path_id
        and str(row.metadata.get("sourceFile")).casefold() == reference_clip.cab.casefold())
    if len(clips) != 1:
        raise ValueError("Exact CPU clip PPtr target is absent or duplicated")
    clip = clips[0]
    clip_meta = clip.metadata
    clip_raw_sha = _text(clip_meta.get("rawDataSha256"), "AnimationClip source hash")
    resource_path = _text(clip_meta.get("container"), "AnimationClip resource path")
    if not resource_path:
        raise ValueError("Exact CPU clip has no resource container")

    def evidence(path: Path) -> dict[str, str]:
        return {"path": str(path), "sha256": sha(path)}

    prefab_input_path = request.output / "prefab_animation_input.json"
    prefab_input = {"format": "EndfieldNpcAssemblyInput/1", "source_kind": "prefab",
        "source_identity": catalog.source_identity,
        "selection": {"character_id": request.npc_id, "npc_template_id": request.template_id, "part_name": request.part_name},
        "avatar": {"cab": avatar_key.cab, "path_id": str(avatar_key.path_id), "raw_data_sha256": avatar_raw_sha},
        "evidence": {"static_source_audit": evidence(request.static_source_audit),
            "preparation_receipt": evidence(request.preparation_receipt), "catalog_receipt": evidence(request.catalog_receipt)}}
    prefab_input_path.write_text(json.dumps(prefab_input, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    root = "NPC_Root"
    source = {"format": "EndfieldNpcAnimationSource/1", "source_kind": "prefab",
        "runtime_animator_reconstructed": False, "character_id": request.npc_id,
        "catalog_source_identity": catalog.source_identity, "prefab_info_sha256": npc.resource_reference.prefab_info_sha256,
        "cpu_animation_template": template, "state_name": state_name,
        "cpu_bundle": str(request.output / "cpu/bundles" / cpu_provider.logical_name),
        "assembly_input": evidence(prefab_input_path), "static_source_audit": evidence(request.static_source_audit),
        "preparation_receipt": evidence(request.preparation_receipt), "catalog_receipt": evidence(request.catalog_receipt),
        "cpu_source": evidence(cpu.path), "avatar_source": evidence(avatar_source.path), "clip_source": evidence(clip.path),
        "transforms": npc_avatar_source_transforms(avatar_source, root),
        "scope": "Exact prefab CPU base clip and selected Avatar source hierarchy; no effective runtime Animator state."}
    source_path = request.output / "npc_source.json"
    source_path.write_text(json.dumps(source, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    animation = {"format": "EndfieldAnimationExportRequest/1", "character_id": request.npc_id,
        "avatar_bundle": str(request.output / "avatar/bundles" / avatar_logical), "avatar_cab": avatar_key.cab,
        "avatar_path_id": avatar_key.path_id, "clip_bundle": str(request.output / "clip/bundles" / clip_logical),
        "resource_path": resource_path, "source_audit": str(source_path), "animator_root_path": root,
        "controller_bundle": None, "unity_version": request.unity_version,
        "maximum_stored_frames": request.maximum_stored_frames, "output": str(request.output / "animation"),
        "dotnet": str(request.dotnet), "reader": str(request.animation_reader)}
    animation_path = request.output / "animation_request.json"
    animation_path.write_text(json.dumps(animation, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    run_scene_process([str(request.python), str(request.animation_exporter), "--request", str(animation_path)],
        request.output / "animation.log", environment, check_cancelled)
    verify_game_source(request.game_root, before, check_cancelled)
    if tuple(source_stamp(path) for path in input_paths) != stamps:
        raise ValueError("NPC prefab source indexes, receipts or tools changed during animation export")
    animation_receipt_path = request.output / "animation/receipt.json"
    if not animation_receipt_path.is_file():
        raise FileNotFoundError(f"Animation exporter did not write its receipt: {animation_receipt_path}")
    animation_receipt = _object(json.loads(animation_receipt_path.read_text(encoding="utf-8-sig")), "animation export receipt")
    expected_animation = {
        "format": "EndfieldAnimationExportReceipt/1", "status": "decoded_bound_and_merged_candidate",
        "character_id": request.npc_id, "resource_path": resource_path, "clip_source": reference_clip.cab,
        "clip_path_id": reference_clip.path_id, "avatar_cab": avatar_key.cab, "avatar_path_id": avatar_key.path_id}
    if any(animation_receipt.get(field) != value for field, value in expected_animation.items()):
        raise ValueError("Decoded animation receipt does not match the selected prefab CPU action")
    animation_outputs = _rows(animation_receipt.get("outputs"), "animation output hashes")
    for field in ("source_hierarchy", "source_local_frames"):
        artifact = _request_path(request.output, animation_receipt.get(field), f"animation receipt {field}")
        matching_outputs = tuple(_object(row, "animation output hash") for row in animation_outputs
            if isinstance(row, dict) and isinstance(row.get("path"), str)
            and Path(row["path"]).resolve() == artifact)
        if len(matching_outputs) != 1 or not artifact.is_file():
            raise ValueError(f"Animation receipt must contain one existing hash for {field}")
        if matching_outputs[0].get("sha256") != sha(artifact):
            raise ValueError(f"Animation receipt output hash disagrees with {field}")
    result = {"format": "EndfieldNpcAnimationReceipt/1", "status": "exact_cpu_base_clip_exported",
        "source_kind": "prefab", "npc_id": request.npc_id, "template_id": request.template_id,
        "part_name": request.part_name, "catalog_source_identity": catalog.source_identity,
        "reference_id": request.reference_id, "state_name": state_name, "cpu_animation_template": template,
        "avatar_identity_matches": True, "runtime_animator_reconstructed": False,
        "cpu_source": {"cab": cpu_cab, "path_id": cpu_path_id, "sha256": cpu_sha},
        "avatar_source": {"cab": avatar_key.cab, "path_id": str(avatar_key.path_id), "sha256": avatar_raw_sha},
        "clip_source": {"cab": reference_clip.cab, "path_id": str(reference_clip.path_id),
            "logical_provider": clip_logical, "sha256": clip_raw_sha, "resource_path": resource_path},
        "static_source_audit": evidence(request.static_source_audit),
        "preparation_receipt": evidence(request.preparation_receipt),
        "catalog_receipt": evidence(request.catalog_receipt), "source": evidence(source_path),
        "animation_receipt": evidence(animation_receipt_path),
        "pending": ["Runtime Animator state, transitions, blend trees and runtime corrections remain unverified."]}
    result_path = request.output / "npc_animation_receipt.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


def export_npc_animation(request: NpcActionRequest | NpcPrefabActionRequest) -> dict[str, object]:
    if isinstance(request, NpcPrefabActionRequest):
        return export_prefab_npc_animation(request)
    return export_configured_npc_animation(request)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    print(json.dumps(export_npc_animation(read_request(parser.parse_args().request.resolve())), allow_nan=False))
