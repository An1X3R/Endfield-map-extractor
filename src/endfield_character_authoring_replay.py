"""Pin server-owned facial replay templates to one exact character Action."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

from endfield_character_replay import CharacterReplayError, ReplaySelectionBinding, replay_file_hash
from webui_character_contract import json_object, required_text


EXPRESSION_ROLES = ("source_blend", "source_build_receipt", "source_audit", "expression_table",
                    "body_receipt", "mesh_channel_probe", "emotion_probe")
BODY_IDENTITY_FIELDS = ("character_id", "avatar_cab", "avatar_path_id", "clip_source", "clip_path_id",
                        "animator_root_path", "resource_path")


def pinned_file(parent: Path, value: object, expected: object, field: str) -> tuple[Path, str]:
    path = (parent / required_text(value, field)).resolve(strict=True)
    digest = required_text(expected, field + ".sha256")
    if not path.is_file() or re.fullmatch(r"[0-9a-f]{64}", digest) is None or replay_file_hash(path) != digest:
        raise CharacterReplayError("Configured replay bytes differ from pinned SHA256: " + field)
    return path, digest


def read_configured_expression_replay(configuration: Path) -> dict[str, object]:
    """Retain the old manifests as evidence, never as fresh output destinations."""
    payload = configuration.read_bytes()
    row = json_object(json.loads(payload.decode("utf-8-sig")), "native expression configuration")
    if row.get("format") != "EndfieldCharacterNativeExpressionReplay/1":
        raise CharacterReplayError("Expected EndfieldCharacterNativeExpressionReplay/1")
    raw_binding = json_object(row.get("selection_binding"), "selection_binding")
    binding = ReplaySelectionBinding(**{name: required_text(raw_binding.get(name), "selection_binding." + name)
        for name in ReplaySelectionBinding.__dataclass_fields__})
    templates = row.get("templates")
    if not isinstance(templates, list) or not templates:
        raise CharacterReplayError("Native expression replay requires a nonempty templates array")
    inputs = {str(configuration): hashlib.sha256(payload).hexdigest()}
    paths: list[str] = []
    identities: list[dict[str, object]] = []
    actions: set[str] = set()
    for item in templates:
        ref = json_object(item, "native expression template")
        path, digest = pinned_file(configuration.parent, ref.get("path"), ref.get("sha256"), "template")
        manifest = json_object(json.loads(path.read_text(encoding="utf-8-sig")), "native expression manifest")
        if manifest.get("format") not in ("EndfieldNativeExpressionAbsoluteReplay/1", "EndfieldNativeExpressionAdditiveReplay/1"):
            raise CharacterReplayError("Unsupported native expression template format")
        action = required_text(manifest.get("action_name"), "expression action_name")
        if action in actions:
            raise CharacterReplayError("Native expression templates require distinct Action names")
        actions.add(action)
        sources = json_object(manifest.get("sources"), "expression sources")
        references: dict[str, Path] = {}
        for role in EXPRESSION_ROLES:
            source = json_object(sources.get(role), "expression source." + role)
            target, source_digest = pinned_file(path.parent, source.get("path"), source.get("sha256"), role)
            references[role] = target
            inputs[str(target)] = source_digest
        body = json_object(json.loads(references["body_receipt"].read_text(encoding="utf-8-sig")), "body receipt")
        identity: dict[str, object] = {key: required_text(body.get(key), "template body." + key)
            for key in ("character_id", "avatar_cab", "clip_source", "animator_root_path", "resource_path")}
        for key in ("avatar_cab", "clip_source"):
            if re.fullmatch(r"CAB-[0-9a-fA-F]{32}", str(identity[key])) is None:
                raise CharacterReplayError("Template body requires an exact CAB: " + key)
        for key in ("avatar_path_id", "clip_path_id"):
            number = body.get(key)
            if type(number) is not int or number == 0 or not -(1 << 63) <= number < (1 << 63):
                raise CharacterReplayError("Template body requires a nonzero int64: " + key)
            identity[key] = number
        if (body.get("format") != "EndfieldAnimationExportReceipt/1"
                or body.get("status") != "decoded_bound_and_merged_candidate"
                or identity["character_id"] != manifest.get("character_id")
                or identity["resource_path"] != binding.action_resource_path):
            raise CharacterReplayError("Expression template body source differs from its selected Action")
        identities.append(identity)
        inputs[str(path)] = digest
        paths.append(str(path))
    if any(identity != identities[0] for identity in identities):
        raise CharacterReplayError("Expression templates must share one exact body Action identity")
    snapshot: dict[str, object] = {"format": "EndfieldCharacterNativeExpressionSnapshot/1",
        "label": required_text(row.get("label"), "expression label"), "selection_binding": asdict(binding),
        "source_identity": identities[0], "templates": paths, "input_sha256": inputs,
        "provenance": required_text(row.get("provenance"), "expression provenance")}
    identity_bytes = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return {**snapshot, "replay_id": hashlib.sha256(identity_bytes).hexdigest()}


def verify_authoring_replay_snapshot(snapshot: dict[str, object]) -> None:
    if snapshot.get("format") != "EndfieldCharacterNativeExpressionSnapshot/1":
        raise CharacterReplayError("Invalid native expression replay snapshot")
    for name, digest in json_object(snapshot.get("input_sha256"), "expression replay inputs").items():
        path = Path(name)
        if not path.is_file() or replay_file_hash(path) != digest:
            raise CharacterReplayError("Expression configuration or source evidence changed; refresh the Action catalog and submit again")
