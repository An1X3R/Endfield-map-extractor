"""Authenticate enemy animation configuration and exact controller clip pointers."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from endfield_character_bundle_inputs import prepare_providers_bundles
from endfield_character_cache import CharacterDataCache, source_stamp
from endfield_character_catalog import CharacterResourceProvider, _normalize_container_path, _provider_details
from endfield_character_enemy_index import EnemyModelBinding, _object, _text, _integer, _rows, _digest
from endfield_character_npc_assembly import _string_path_hash
from endfield_character_selection import CharacterSelection
from endfield_character_sources import CharacterSourceContext, game_source_stamps, verify_game_source, validate_character_storage
from endfield_scene_export import run_scene_process
from endfield_scene_resources import write_index
from extractor_core.projected_decals import build_path_id_index
from extractor_core.resources import read_virtual_file
from character_runtime.resolve_character_controller import controller_string_pool, decode_controller_hash


EMPTY_ENEMY_CLIP_EVIDENCE_FIELDS = (
    "acl_transform_tracks", "acl_root_tracks", "acl_float_tracks", "acl_transform_buffer_bytes",
    "acl_root_buffer_bytes", "acl_float_buffer_bytes", "legacy_stream_curve_count", "legacy_dense_frames",
    "legacy_dense_curve_count", "source_binding_count", "acl_constant_value_count", "legacy_stream_data_count",
    "legacy_dense_sample_count", "legacy_constant_value_count", "source_curve_count",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def empty_enemy_clip_evidence(clip: dict[str, object]) -> dict[str, int] | None:
    """Identify empty source placeholders without relying on clip names or inferred motion."""
    buffer = _object(clip.get("m_AclCompressedBuffer"), "clip ACL buffer")
    counts = {name: _integer(buffer.get(field), field) for name, field in (
        ("acl_transform_tracks", "OutputTrackCount"), ("acl_root_tracks", "RootTrackCount"),
        ("acl_float_tracks", "FloatCurveCount"))}
    for name, field in (("acl_transform_buffer_bytes", "TransformBufferData"),
                        ("acl_root_buffer_bytes", "RootMotionBufferData"), ("acl_float_buffer_bytes", "FloatBufferData")):
        value = buffer.get(field)
        if not isinstance(value, str):
            raise ValueError(f"Enemy clip ACL buffer must be base64 text: field={field}")
        if value:
            return None
        counts[name] = 0
    if any(counts.values()):
        return None
    bindings = _object(clip.get("m_ClipBindingConstant"), "clip bindings")
    muscle = _object(_object(clip.get("m_MuscleClip"), "muscle clip").get("m_Clip"), "legacy clip")
    streamed = _object(muscle.get("m_StreamedClip"), "legacy streamed clip")
    dense = _object(muscle.get("m_DenseClip"), "legacy dense clip")
    constant = _object(muscle.get("m_ConstantClip"), "legacy constant clip")
    old_acl = _object(muscle.get("m_ACLClip"), "legacy ACL clip")
    if old_acl.get("IsSet") is not False or dense.get("m_ACLArray") != "":
        return None
    counts.update({"legacy_stream_curve_count": _integer(streamed.get("curveCount"), "legacy stream curves"),
                   "legacy_dense_frames": _integer(dense.get("m_FrameCount"), "legacy dense frames"),
                   "legacy_dense_curve_count": _integer(dense.get("m_CurveCount"), "legacy dense curves")})
    arrays = (("source_binding_count", bindings.get("genericBindings")),
              ("acl_constant_value_count", buffer.get("m_ConstantValues")),
              ("legacy_stream_data_count", streamed.get("data")), ("legacy_dense_sample_count", dense.get("m_SampleArray")),
              ("legacy_constant_value_count", constant.get("data")))
    for name, values in arrays:
        if not isinstance(values, list):
            raise ValueError(f"Enemy empty-clip evidence requires a source array: field={name}")
        counts[name] = len(values)
    curve_count = 0
    for field in ("m_RotationCurves", "m_CompressedRotationCurves", "m_EulerCurves", "m_PositionCurves",
                  "m_ScaleCurves", "m_FloatCurves", "m_PPtrCurves"):
        values = clip.get(field)
        if not isinstance(values, list):
            raise ValueError(f"Enemy empty-clip evidence requires source curves: field={field}")
        curve_count += len(values)
    counts["source_curve_count"] = curve_count
    return counts if not any(counts.values()) else None


def _documents(directory: Path) -> tuple[tuple[Path, dict[str, object], dict[str, object]], ...]:
    result: list[tuple[Path, dict[str, object], dict[str, object]]] = []
    for path in sorted(directory.rglob("*.json")):
        value = _object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))
        metadata = _object(value.get("$animestudio"), "$animestudio")
        if metadata.get("byteSize") != metadata.get("rawDataLength"):
            raise ValueError(f"Enemy animation source bytes are incomplete: path={path}")
        _digest(metadata.get("rawDataSha256"), "source.rawDataSha256")
        result.append((path, value, metadata))
    return tuple(result)


def _exact_object(objects: tuple[tuple[Path, dict[str, object], dict[str, object]], ...],
                  cab: str, path_id: int, source_type: str) -> tuple[Path, dict[str, object], dict[str, object]]:
    matches = tuple(row for row in objects if row[2].get("sourceFile", "").casefold() == cab.casefold()
                    and row[2].get("pathId") == path_id and row[2].get("type") == source_type)
    if len(matches) != 1:
        raise ValueError(f"Enemy animation source object is not unique: cab={cab}, path_id={path_id}, type={source_type}, count={len(matches)}")
    return matches[0]


def _cab_externals(path: Path, check_cancelled: Callable[[], None]) -> dict[str, tuple[str, ...]]:
    """Preserve the serialized external-file order required by PPtr FileID."""
    result: dict[str, tuple[str, ...]] = {}
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line_number % 1024 == 0:
                check_cancelled()
            row = _object(json.loads(line), f"CAB inventory line {line_number}")
            if row.get("kind") != "cab":
                continue
            for source in _rows(row.get("serialized"), "serialized"):
                cab = _text(source.get("name"), "CAB name").casefold()
                raw = source.get("externals")
                if not isinstance(raw, list):
                    raise ValueError(f"CAB external inventory must preserve an ordered array: cab={cab}")
                externals = tuple(_text(value, "external CAB") for value in raw)
                if cab in result and result[cab] != externals:
                    raise ValueError(f"CAB inventory has conflicting serialized external orders: cab={cab}")
                result[cab] = externals
    return result


def _pointer(value: object, cab: str, externals: dict[str, tuple[str, ...]], field: str) -> tuple[str, int]:
    row = _object(value, field)
    file_id = _integer(row.get("m_FileID"), field + ".FileID")
    path_id = _integer(row.get("m_PathID"), field + ".PathID")
    targets = externals[cab.casefold()]
    if not 0 <= file_id <= len(targets):
        raise ValueError(f"Enemy animation PPtr external FileID out of range: cab={cab}, field={field}, file_id={file_id}")
    return (cab if file_id == 0 else targets[file_id - 1], path_id)


def _provider(path: str, providers: dict[str, tuple[CharacterResourceProvider, ...]]) -> CharacterResourceProvider:
    matches = providers.get(path, ())
    if len(matches) != 1 or len(matches[0].serialized_cabs) != 1:
        raise ValueError(f"Enemy animation asset requires one provider/CAB: path={path}, providers={len(matches)}")
    return matches[0]


def inspect_enemy_animation(
    context: CharacterSourceContext,
    selection: CharacterSelection,
    binding: EnemyModelBinding,
    json_reader: Path,
    unity_version: str,
    output: Path,
    check_cancelled: Callable[[], None],
) -> dict[str, object]:
    """Resolve source configuration, selected prefab Avatar and effective controller clips."""
    validate_character_storage(context.game_root, output)
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if unity_version != "2021.3.34f5" or binding.model_id != selection.part_name or binding.template_id != selection.template_id:
        raise ValueError("Enemy action selection differs from the exact indexed model/template or pinned Unity version")
    before = game_source_stamps(context.game_root)
    inputs = (context.provider_index, context.resource_database, context.cab_inventory, json_reader)
    stamps = tuple(source_stamp(path) for path in inputs)
    provider_rows, _ = _provider_details(context.provider_index, frozenset((binding.resource_path, selection.resource_path)), check_cancelled)
    data_provider = _provider(binding.resource_path, provider_rows)
    model_provider = _provider(selection.resource_path, provider_rows)
    if model_provider.logical_name != selection.logical_provider:
        raise ValueError("Enemy selected prefab provider changed")
    output.mkdir(parents=True)
    temporary = output / "tmp"
    temporary.mkdir()
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    externals = _cab_externals(context.cab_inventory, check_cancelled)

    def read_sources(providers: tuple[CharacterResourceProvider, ...], directory: Path,
                     types: tuple[str, ...]) -> tuple[tuple[Path, dict[str, object], dict[str, object]], ...]:
        prepare_providers_bundles(context, providers, directory / "bundles", check_cancelled,
                                  lambda _stage, _fraction, _message: None)
        run_scene_process([str(json_reader), str(directory / "bundles"), str(directory / "source_json"),
            "--game", "ArknightsEndfield", "--unity_version", unity_version, "--types", "AssetBundle:Parse", "ResourceManager:Parse", *types,
            "--export_type", "JSON", "--mono_behaviour_type_tree_priority", "SerializedFirst", "--group_assets", "ByType"],
            directory / "reader.log", environment, check_cancelled)
        return _documents(directory / "source_json")

    objects = read_sources((data_provider, model_provider), output / "model", ("MonoBehaviour", "GameObject", "Animator", "Avatar"))
    data_json, data, metadata = _exact_object(objects, binding.source_cab, binding.path_id, "MonoBehaviour")
    if metadata.get("rawDataSha256") != binding.object_sha256 or _normalize_container_path(_text(metadata.get("container"), "EnemyData.container")) != binding.resource_path:
        raise ValueError("EnemyData object changed from the authenticated model index")
    components = tuple(row for row in _rows(_object(data.get("references"), "references").get("RefIds"), "RefIds")
        if _object(row.get("type"), "component.type").get("class") == "EnemyAnimationComponentData"
        and _object(row.get("type"), "component.type").get("ns") == "Beyond.Gameplay.View")
    if len(components) != 1:
        raise ValueError(f"EnemyData requires one exact animation component: template={binding.template_id}, count={len(components)}")
    component = components[0]
    animation = _object(component.get("data"), "EnemyAnimationComponentData.data")
    if (animation.get("$decoded") is not True or animation.get("exactTypeTreeDecoded") is not True
            or animation.get("layout") != "Beyond.Gameplay.View.EnemyAnimationComponentData"
            or any(marker in animation for marker in ("$partial", "$inferred", "$undecoded"))):
        raise ValueError("Enemy animation component lacks exact serialized TypeTree evidence")
    config_path = _text(animation.get("_animCfgPath"), "EnemyAnimationComponentData._animCfgPath")
    if not config_path.casefold().startswith("data/json/animationconfig/") or not config_path.endswith(".json") or ".." in config_path.split("/"):
        raise ValueError(f"Unsupported exact enemy AnimationConfig reference: path={config_path}")
    animator_objects = tuple(row for row in objects if row[2].get("type") == "Animator"
        and row[2].get("sourceFile", "").casefold() == model_provider.serialized_cabs[0].name.casefold()
        and _normalize_container_path(str(row[2].get("container", ""))) == selection.resource_path)
    if len(animator_objects) != 1:
        raise ValueError("Enemy selected prefab requires one exact Animator source")
    animator_json, animator, animator_meta = animator_objects[0]
    animator_cab = _text(animator_meta.get("sourceFile"), "Animator CAB")
    root_pointer = _pointer(animator.get("m_GameObject"), animator_cab, externals, "Animator.m_GameObject")
    root_json, root, root_meta = _exact_object(objects, root_pointer[0], root_pointer[1], "GameObject")
    if _normalize_container_path(str(root_meta.get("container", ""))) != selection.resource_path:
        raise ValueError("Enemy Animator root is outside the exact selected prefab")
    root_cab = _text(root_meta.get("sourceFile"), "prefab CAB")
    root_id = _integer(root_meta.get("pathId"), "prefab PathID")
    animator_refs = []
    for row in _rows(root.get("m_Components"), "GameObject.m_Components"):
        pointer = _pointer(row, root_cab, externals, "GameObject.component")
        animator_refs.extend(obj for obj in objects if obj[2].get("type") == "Animator"
            and obj[2].get("sourceFile", "").casefold() == pointer[0].casefold() and obj[2].get("pathId") == pointer[1])
    if len(animator_refs) != 1:
        raise ValueError("Enemy selected prefab root requires one exact Animator component")
    if animator_refs[0][2].get("pathId") != animator_meta.get("pathId"):
        raise ValueError("Enemy Animator and root component pointers disagree")
    avatar_cab, avatar_id = _pointer(animator.get("m_Avatar"), root_cab, externals, "Animator.m_Avatar")
    avatar_json, _avatar, avatar_meta = _exact_object(objects, avatar_cab, avatar_id, "Avatar")
    payload = read_virtual_file(context.game_root / "Endfield_Data/StreamingAssets/VFS", "775A31D1", config_path)
    key = decode_controller_hash(payload, controller_string_pool(context.game_root / "Endfield_Data/StreamingAssets/VFS"))
    paths = build_path_id_index(_string_path_hash(context)).get(key, ())
    if len(paths) != 1:
        raise ValueError(f"Enemy normal-controller hash must resolve to one exact resource: hash={key.hex()}, count={len(paths)}")
    controller_path = _normalize_container_path(paths[0])
    controller_providers, _ = _provider_details(context.provider_index, frozenset((controller_path,)), check_cancelled)
    controller_provider = _provider(controller_path, controller_providers)
    sources = read_sources((controller_provider,), output / "controller", ("AnimatorController", "AnimatorOverrideController", "AnimationClip"))
    controller_matches = tuple(row for row in sources if row[2].get("sourceFile", "").casefold() == controller_provider.serialized_cabs[0].name.casefold()
        and _normalize_container_path(str(row[2].get("container", ""))) == controller_path and row[2].get("type") in ("AnimatorController", "AnimatorOverrideController"))
    if len(controller_matches) != 1:
        raise ValueError("Enemy source controller is absent or ambiguous")
    controller_json, controller, controller_meta = controller_matches[0]
    controller_cab = _text(controller_meta.get("sourceFile"), "controller CAB")
    base_evidence: dict[str, object] | None = None
    override_count = 0
    if controller_meta.get("type") == "AnimatorOverrideController":
        base_cab, base_id = _pointer(controller.get("m_Controller"), controller_cab, externals, "override.m_Controller")
        base_json, base, base_meta = _exact_object(sources, base_cab, base_id, "AnimatorController")
        base_index, _ = CharacterDataCache(context.cache_root).read_character_cab_index(context.cab_inventory, check_cancelled)
        base_providers = dict(base_index.providers).get(base_cab.casefold(), ())
        if len(base_providers) != 1:
            raise ValueError("Enemy base controller must resolve to one exact CAB provider")
        base_evidence = {"type": "AnimatorController", "resource_path": _text(base_meta.get("container"), "base.container"),
                         "logical_provider": base_providers[0], "cab": base_cab, "path_id": str(base_id),
                         "raw_data_sha256": base_meta["rawDataSha256"], "source_json": str(base_json),
                         "source_json_sha256": _sha(base_json), "external_cabs": list(externals[base_cab.casefold()])}
        base_pointers = tuple(_pointer(row, base_cab, externals, "base.clip") for row in _rows(base.get("m_AnimationClips"), "base.m_AnimationClips"))
        originals = set(base_pointers)
        effective = []
        mapped_originals: set[tuple[str, int]] = set()
        override_rows = _rows(controller.get("m_Clips"), "override.m_Clips")
        override_count = len(override_rows)
        for index, row in enumerate(override_rows):
            original = _pointer(row.get("m_OriginalClip"), controller_cab, externals, "override.original")
            if original not in originals:
                raise ValueError(f"Enemy override original clip is absent from its exact base controller: index={index}")
            override = _pointer(row.get("m_OverrideClip"), controller_cab, externals, "override.clip")
            if original in mapped_originals:
                raise ValueError(f"Enemy override has duplicate original clip mappings: index={index}")
            mapped_originals.add(original)
            effective.append((index, original, original if override[1] == 0 else override, "override"))
        effective.extend((index, pointer, pointer, "base") for index, pointer in enumerate(base_pointers)
                         if pointer not in mapped_originals and pointer[1] != 0)
    else:
        base_pointers = tuple(_pointer(row, controller_cab, externals, "controller.clip")
                              for row in _rows(controller.get("m_AnimationClips"), "controller.m_AnimationClips"))
        effective = [(index, pointer, pointer, "base") for index, pointer in enumerate(base_pointers) if pointer[1] != 0]
    cache = CharacterDataCache(context.cache_root)
    cab_index, _ = cache.read_character_cab_index(context.cab_inventory, check_cancelled)
    cab_providers = dict(cab_index.providers)
    actions: list[dict[str, object]] = []
    unsupported: list[dict[str, object]] = []
    for index, original, (cab, path_id), kind in effective:
        clip_json, clip, clip_meta = _exact_object(sources, cab, path_id, "AnimationClip")
        providers = cab_providers.get(cab.casefold(), ())
        if len(providers) != 1:
            raise ValueError(f"Enemy exact controller clip CAB provider is absent or ambiguous: cab={cab}")
        resource = _normalize_container_path(_text(clip_meta.get("container"), "clip.container"))
        state_name = "controller_" + kind + "_clip_" + str(index)
        evidence = empty_enemy_clip_evidence(clip)
        if evidence is not None:
            unsupported.append({"state_name": state_name, "clip_name": _text(clip_meta.get("name"), "clip.name"),
                "resource_path": resource, "logical_provider": providers[0], "clip_cab": cab, "clip_path_id": str(path_id),
                "clip_raw_data_sha256": clip_meta["rawDataSha256"], "source_json": str(clip_json), "source_json_sha256": _sha(clip_json),
                "controller_clip_index": index, "controller_clip_kind": kind, "original_clip": {"cab": original[0], "path_id": str(original[1])},
                "binding_status": "source_enemy_controller_empty_clip_placeholder",
                "reason": "Exact source clip has zero ACL transform/root/scalar tracks, empty buffers and bindings, and no legacy or serialized curve samples.",
                "empty_clip_evidence": evidence})
            continue
        identity = [asdict(selection), binding.object_sha256, hashlib.sha256(payload).hexdigest(), controller_meta["rawDataSha256"],
                    avatar_cab.casefold(), avatar_id, kind, index, cab.casefold(), path_id, clip_meta["rawDataSha256"]]
        action_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        actions.append({"action_id": action_id, "state_name": state_name, "name": _text(clip_meta.get("name"), "clip.name"),
            "resource_path": resource, "logical_provider": providers[0], "clip_cab": cab, "clip_path_id": str(path_id),
            "clip_raw_data_sha256": clip_meta["rawDataSha256"], "source_json": str(clip_json), "source_json_sha256": _sha(clip_json),
            "controller_clip_index": index, "controller_clip_kind": kind, "original_clip": {"cab": original[0], "path_id": str(original[1])},
            "binding_status": "exact_enemy_controller_clip_reference_requires_final_clip_binding"})
    if not actions and not unsupported:
        raise ValueError("Enemy controller has no exact effective clip references")
    result = {"format": "EndfieldEnemyAnimationSource/1", "status": "source_enemy_controller_clip_references",
        "source_identity": selection.catalog_identity, "npc_id": selection.character_id, "template_id": selection.template_id,
        "part_name": selection.part_name, "model_resource_path": selection.resource_path, "model_provider": selection.logical_provider,
        "enemy_data": {"template_id": binding.template_id, "resource_path": binding.resource_path, "cab": binding.source_cab,
            "path_id": str(binding.path_id), "raw_data_sha256": binding.object_sha256, "animation_component_rid": str(component["rid"]),
            "animation_config_path": config_path, "source_json": str(data_json), "source_json_sha256": _sha(data_json)},
        "animation_config": {"virtual_path": config_path, "sha256": hashlib.sha256(payload).hexdigest(), "normal_controller_hash": key.hex()},
        "controller": {"type": controller_meta["type"], "resource_path": controller_path, "logical_provider": controller_provider.logical_name, "cab": controller_cab,
            "path_id": str(controller_meta["pathId"]), "raw_data_sha256": controller_meta["rawDataSha256"],
            "source_json": str(controller_json), "source_json_sha256": _sha(controller_json), "external_cabs": list(externals[controller_cab.casefold()])},
        "base_controller": base_evidence,
        "selected_avatar": {"cab": avatar_cab, "path_id": str(avatar_id), "source_json": str(avatar_json),
            "source_json_sha256": _sha(avatar_json), "raw_data_sha256": avatar_meta["rawDataSha256"]},
        "prefab_source": {"resource_path": selection.resource_path, "logical_provider": selection.logical_provider, "cab": root_cab,
            "root_path_id": str(root_id), "animator_path_id": str(animator_meta["pathId"]), "source_json": str(animator_json),
            "root_source_json": str(root_json)}, "actions": actions, "unsupported_actions": unsupported,
        "runtime_animator_reconstructed": False,
        "coverage_counts": {"scope": "exact_effective_controller_clip_references",
            "base_controller_slots": len(base_pointers),
            "base_controller_unique_nonzero_clips": len({pointer for pointer in base_pointers if pointer[1] != 0}),
            "override_mapping_rows": override_count, "effective_action_references": len(effective),
            "source_reference_count": len(effective), "available_action_count": len(actions),
            "empty_placeholder_count": len(unsupported), "unsupported_action_count": len(unsupported),
            "runtime_state_machine_reconstructed": False},
        "limitations": ["Exact effective controller clips; no live states, transitions, blend weights, montage or IK reconstruction.",
                        "Final clip CRC/Avatar binding and saved Action validation remain mandatory."]}
    verify_game_source(context.game_root, before, check_cancelled)
    if tuple(source_stamp(path) for path in inputs) != stamps:
        raise ValueError("Enemy animation source indexes or configured reader changed during inspection")
    write_index(output / "receipt.json", result)
    return result
