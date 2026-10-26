"""Resolve a selected playable's source normal controller without runtime claims."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, fields
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
from typing import cast


class ControllerSourceError(ValueError):
    """Exact selected character, component, controller, or clip evidence is invalid."""


@dataclass(frozen=True)
class ControllerRequest:
    repository_src: Path
    game_root: Path
    provider_index: Path
    resource_database: Path
    cache_root: Path
    character_id: str
    template_id: str
    part_name: str
    resource_path: str
    action_resource_path: str
    action_logical_provider: str
    unity_version: str
    json_reader: Path
    controller_audit: Path
    output: Path


def obj(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ControllerSourceError(f"Expected object: field={field}")
    return cast(dict[str, object], value)


def text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControllerSourceError(f"Expected nonempty text: field={field}")
    return value


def integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise ControllerSourceError(f"Expected integer: field={field}")
    return value


def rows(value: object, field: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ControllerSourceError(f"Expected array: field={field}")
    return tuple(obj(row, field) for row in value)


def load(path: Path) -> dict[str, object]:
    return obj(json.loads(path.read_text(encoding="utf-8-sig")), str(path))


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalize(value: str) -> str:
    return value.replace("\\", "/").casefold()


def source_identity(metadata: dict[str, object]) -> dict[str, object]:
    return {field: metadata[field] for field in ("container", "sourceFile", "pathId", "type", "byteSize", "rawDataLength", "rawDataSha256")}


def check_cancelled() -> None:
    return None


def read_request(path: Path) -> ControllerRequest:
    source = load(path)
    if source.get("format") != "EndfieldCharacterControllerRequest/1":
        raise ControllerSourceError("Expected EndfieldCharacterControllerRequest/1")
    path_fields = {"repository_src", "game_root", "provider_index", "resource_database", "cache_root",
                   "json_reader", "controller_audit", "output"}
    values = {field.name: ((path.parent / Path(text(source.get(field.name), field.name))).resolve()
                          if field.name in path_fields else text(source.get(field.name), field.name))
              for field in fields(ControllerRequest)}
    request = ControllerRequest(**values)
    if re.fullmatch(r"chr_[a-z0-9_]+", request.character_id) is None:
        raise ControllerSourceError("Playable controller requires a source character identity")
    if request.unity_version != "2021.3.34f5":
        raise ControllerSourceError("Configured ControllerAudit supports Unity 2021.3.34f5 only")
    for field in ("provider_index", "resource_database", "json_reader", "controller_audit"):
        if not getattr(request, field).is_file():
            raise FileNotFoundError(getattr(request, field))
    for target in (request.output, request.cache_root):
        if target.is_relative_to(request.game_root) or request.game_root.is_relative_to(target):
            raise ControllerSourceError(f"Storage overlaps read-only game root: storage={target}")
    if request.output.exists() or request.output.is_symlink():
        raise FileExistsError(request.output)
    return request


def controller_string_pool(vfs_root: Path) -> Mapping[int, str] | None:
    """Select the source-declared JSON string storage, without guessing from field bytes."""
    from extractor_core.resources import load_group, read_virtual_file
    from extractor_core.scene_string_pool import read_scene_string_pool

    group = load_group(vfs_root, "775A31D1")
    if not any(item.file_name.casefold() == "data/json/stringpool/stringpool0.byte" for item in group.iter_files()):
        return None
    return read_scene_string_pool(lambda path: read_virtual_file(vfs_root, "775A31D1", path))


def decode_controller_hash(payload: bytes, strings: Mapping[int, str] | None) -> bytes:
    """Decode only the native-verified null-fallback-montage prefix."""
    if len(payload) < 30 or payload[:2] != b"\x0f\xff":
        raise ControllerSourceError("Unsupported AnimationConfig prefix; full schema decoding is required")
    count, = struct.unpack_from("<i", payload, 18)
    if not -1 <= count <= 1024:
        raise ControllerSourceError(f"Invalid source bone-weight-mask count: count={count}")
    offset = 22
    for _ in range(max(0, count)):
        if offset + 5 > len(payload) or payload[offset] != 2:
            raise ControllerSourceError(f"Invalid mask object: offset={offset}")
        size, = struct.unpack_from("<i", payload, offset + 1)
        offset += 5
        if strings is None:
            if size < 1 or offset + size + 8 > len(payload):
                raise ControllerSourceError(f"Invalid mask name: offset={offset}, size={size}")
            payload[offset:offset + size].decode("utf-8")
            offset += size
        elif size not in strings or not strings[size].strip():
            raise ControllerSourceError(f"Source mask name is absent from its string pool: offset={offset - 4}, string_id={size}")
        if offset + 8 > len(payload):
            raise ControllerSourceError(f"Truncated source mask path hash: offset={offset}")
        offset += 8
    key = payload[offset:offset + 8]
    if len(key) != 8 or key == bytes(8):
        raise ControllerSourceError("Source normal-controller hash is missing or truncated")
    return key


def selected_components(source: dict[str, object], character_id: str, part_name: str) -> tuple[str, dict[str, object]]:
    """Follow the actual root RID and its component list, never unrelated references."""
    registry = rows(obj(source.get("references"), "references").get("RefIds"), "RefIds")
    references = {integer(row.get("rid"), "rid"): row for row in registry}
    if len(references) != len(registry):
        raise ControllerSourceError("Character managed-reference IDs are duplicated")
    root_id = integer(obj(source.get("data"), "data").get("rid"), "root RID")
    root = references[root_id]
    root_type = obj(root.get("type"), "root type")
    data = obj(root.get("data"), "CharacterTemplateData")
    if root_type.get("class") != "CharacterTemplateData" or root_type.get("ns") != "Beyond.Gameplay" or data.get("id") != character_id or data.get("exactTypeTreeDecoded") is not True:
        raise ControllerSourceError("Character root identity or exact serialized TypeTree verification failed")
    components = tuple(references[integer(row.get("rid"), "component RID")]
                       for row in rows(data.get("componentList"), "componentList"))
    models = tuple(obj(row.get("data"), "model data") for row in components
                   if obj(row.get("type"), "component type").get("class") == "ModelComponentData")
    if len(models) != 1 or models[0].get("modelId") != part_name:
        raise ControllerSourceError("Selected prefab part disagrees with CharacterData ModelComponentData")
    animations = tuple(obj(row.get("data"), "animation data") for row in components
                       if obj(row.get("type"), "component type").get("class") == "CharacterAnimationComponentData")
    if len(animations) != 1 or animations[0].get("exactTypeTreeDecoded") is not True:
        raise ControllerSourceError("Character requires one exact serialized CharacterAnimationComponentData")
    virtual_path = text(animations[0].get("_animCfgPath"), "_animCfgPath")
    if not normalize(virtual_path).startswith("data/json/animationconfig/") or not virtual_path.endswith(".json"):
        raise ControllerSourceError("Character animation component references an unsupported configuration namespace")
    return virtual_path, animations[0]


def clip_reference_matches(tree: dict[str, object], receipt: dict[str, object], clip: dict[str, object]) -> tuple[dict[str, object], ...]:
    externals = receipt.get("external_cabs")
    if not isinstance(externals, list) or any(not isinstance(item, str) for item in externals):
        raise ControllerSourceError("Controller external CAB inventory is invalid")
    matches: list[dict[str, object]] = []
    for slot, pointer in enumerate(rows(tree.get("m_AnimationClips"), "m_AnimationClips")):
        file_id = integer(pointer.get("m_FileID"), "clip FileID")
        path_id = integer(pointer.get("m_PathID"), "clip PathID")
        if file_id < 0 or file_id > len(externals):
            raise ControllerSourceError("Controller clip pointer has invalid external FileID")
        cab = text(receipt.get("cab"), "controller CAB") if file_id == 0 else externals[file_id - 1]
        if normalize(cab) == normalize(text(clip.get("sourceFile"), "clip CAB")) and path_id == integer(clip.get("pathId"), "selected clip PathID"):
            matches.append({"file_id": file_id, "path_id": path_id, "cab": cab, "controller_slot": slot})
    return tuple(matches)


def resolve_controller(request: ControllerRequest) -> dict[str, object]:
    sys.path.insert(0, str(request.repository_src))
    from endfield_character_cache import CharacterDataCache, source_stamp
    from endfield_character_catalog import _provider_details, read_character_catalog_cached
    from endfield_character_npc_assembly import read_npc_source_objects
    from endfield_character_sources import game_source_stamps, verify_game_source
    from endfield_scene_export import run_scene_process
    from extractor_core.projected_decals import build_path_id_index
    from extractor_core.resources import extract_candidate_bundles, read_virtual_file
    before = game_source_stamps(request.game_root)
    inputs = (request.provider_index, request.resource_database, request.json_reader, request.controller_audit)
    stamps = tuple(source_stamp(path) for path in inputs)
    catalog = read_character_catalog_cached(request.game_root, request.provider_index, CharacterDataCache(request.cache_root), check_cancelled)
    selections = tuple(variant for binding in catalog.model_bindings if binding.character_id == request.character_id
        for template in binding.templates if template.template_id == request.template_id
        for part in template.parts if part.part_name == request.part_name
        for variant in part.variants if normalize(variant.resource_path) == normalize(request.resource_path) and variant.provider_status == "found")
    if len(selections) != 1:
        raise ControllerSourceError("Selected character/template/part/resource is not one exact catalog variant")
    data_path = "assets/beyond/dynamicassets/gamedata/characterdata/data_" + request.character_id + ".asset"
    paths = frozenset((data_path, normalize(request.action_resource_path)))
    providers, _ = _provider_details(request.provider_index, paths, check_cancelled)
    if any(len(providers[path]) != 1 or len(providers[path][0].serialized_cabs) != 1 for path in paths):
        raise ControllerSourceError("CharacterData or selected action provider is absent or ambiguous")
    if providers[normalize(request.action_resource_path)][0].logical_name != request.action_logical_provider:
        raise ControllerSourceError("Selected action logical provider disagrees with the current index")
    request.output.mkdir(parents=True, exist_ok=False)
    environment = {**os.environ, "TEMP": str(request.output), "TMP": str(request.output)}
    extract_candidate_bundles(request.resource_database, request.game_root / "Endfield_Data/StreamingAssets/VFS",
        sorted({providers[path][0].logical_name for path in paths}), request.output / "source_bundles")
    run_scene_process([str(request.json_reader), str(request.output / "source_bundles"), str(request.output / "source_json"),
        "--game", "ArknightsEndfield", "--unity_version", request.unity_version, "--types", "AssetBundle:Parse", "ResourceManager:Parse", "MonoBehaviour", "AnimationClip",
        "--export_type", "JSON", "--mono_behaviour_type_tree_priority", "SerializedFirst", "--group_assets", "ByType"],
        request.output / "source_reader.log", environment, check_cancelled)
    objects = read_npc_source_objects(request.output / "source_json")
    characters = tuple(row for row in objects if normalize(str(row.metadata.get("container", ""))) == data_path and row.metadata.get("type") == "MonoBehaviour")
    clips = tuple(row for row in objects if normalize(str(row.metadata.get("container", ""))) == normalize(request.action_resource_path) and row.metadata.get("type") == "AnimationClip")
    if len(characters) != 1 or len(clips) != 1:
        raise ControllerSourceError("Exact selected CharacterData or AnimationClip JSON object is not unique")
    config_path, animation = selected_components(characters[0].value, request.character_id, request.part_name)
    vfs_root = request.game_root / "Endfield_Data/StreamingAssets/VFS"
    payload = read_virtual_file(vfs_root, "775A31D1", config_path)
    strings = controller_string_pool(vfs_root)
    key = decode_controller_hash(payload, strings)
    hash_path = request.output / "StringPathHash.bin"
    hash_path.write_bytes(read_virtual_file(vfs_root, "D6E622F7", "Data/ExtendData/Main/StringPathHash.bin"))
    with closing(sqlite3.connect(request.resource_database.as_uri() + "?mode=ro", uri=True)) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM meta"))
    if metadata.get("string_path_hash_sha256", "").casefold() != sha(hash_path):
        raise ControllerSourceError("Resource database path hashes disagree with the current game")
    controller_paths = build_path_id_index(hash_path).get(key, ())
    if len(controller_paths) != 1:
        raise ControllerSourceError("Source controller hash does not resolve to one exact path")
    controller_path = controller_paths[0]
    controller_providers, _ = _provider_details(request.provider_index, frozenset((normalize(controller_path),)), check_cancelled)
    choices = controller_providers[normalize(controller_path)]
    if len(choices) != 1 or len(choices[0].serialized_cabs) != 1:
        raise ControllerSourceError("Source normal-controller provider is absent or ambiguous")
    extract_candidate_bundles(request.resource_database, vfs_root, [choices[0].logical_name], request.output / "controller_bundles")
    bundle = request.output / "controller_bundles" / choices[0].logical_name
    run_scene_process([str(request.controller_audit), str(bundle), str(request.output / "controller")],
        request.output / "controller_reader.log", environment, check_cancelled)
    receipt = load(request.output / "controller/controller_receipt.json")
    if receipt.get("sha256") != sha(bundle) or normalize(text(receipt.get("cab"), "controller CAB")) != normalize(choices[0].serialized_cabs[0].name):
        raise ControllerSourceError("Controller audit bundle hash or CAB differs from the exact provider")
    references = clip_reference_matches(load(request.output / "controller/controller_tree.json"), receipt, clips[0].metadata)
    verify_game_source(request.game_root, before, check_cancelled)
    if tuple(source_stamp(path) for path in inputs) != stamps:
        raise ControllerSourceError("Controller resolver source indexes or tools changed during execution")
    result = {"format": "EndfieldCharacterControllerReceipt/1", "status": "source_normal_controller_selected_clip_verified" if references else "source_normal_controller_dynamic_clip_verified",
        "scope": "source_normal_controller_not_runtime", "runtime_state_reconstructed": False, "dependencies_extracted": False,
        "character_id": request.character_id, "template_id": request.template_id, "part_name": request.part_name,
        "resource_path": request.resource_path, "catalog_source_identity": catalog.source_identity,
        "provider_index_sha256": catalog.provider_index_source.sha256,
        "string_path_hash_sha256": sha(hash_path),
        "character_data_source": source_identity(characters[0].metadata), "animation_component": animation,
        "animation_config_virtual_path": config_path, "animation_config_sha256": hashlib.sha256(payload).hexdigest(),
        "animation_config_string_storage": "inline" if strings is None else "pooled",
        "animation_config_string_pool_sha256": None if strings is None else hashlib.sha256(
            json.dumps(dict(strings), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        "controller_resource_path": controller_path, "controller_bundle": str(bundle), "controller_bundle_sha256": sha(bundle),
        "reader_controller_bundle": str(bundle),
        "controller": receipt, "selected_clip_source": source_identity(clips[0].metadata), "clip_reference": references[0] if references else None,
        "clip_references": list(references),
        "boundary": "Normal source controller is proven through selected CharacterData components independently of static clip membership. Dynamic clips may use its exact parameter names and source-authored clothing layers. Optional controller, active state/layers/montage and runtime parameter writes remain unresolved."}
    (request.output / "receipt.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(resolve_controller(read_request(arguments.request.resolve())), allow_nan=False))
