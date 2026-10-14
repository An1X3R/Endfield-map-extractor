"""Authenticate one server-owned direct FinalIK scenario and its expected input pose."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

from endfield_character_authoring_replay import pinned_file
from endfield_character_replay import CharacterReplayError, ReplaySelectionBinding
from webui_character_contract import json_object, required_text


def read_configured_finalik_replay(configuration: Path) -> dict[str, object]:
    payload = configuration.read_bytes()
    row = json_object(json.loads(payload.decode("utf-8-sig")), "FinalIK configuration")
    version = row.get("format")
    if version not in ("EndfieldCharacterFinalIKReplay/1", "EndfieldCharacterFinalIKReplay/2"):
        raise CharacterReplayError("Expected EndfieldCharacterFinalIKReplay/1 or /2")
    raw_binding = json_object(row.get("selection_binding"), "selection_binding")
    binding = ReplaySelectionBinding(**{name: required_text(raw_binding.get(name), "selection_binding." + name)
        for name in ReplaySelectionBinding.__dataclass_fields__})
    raw_source = json_object(row.get("animation_source_identity"), "FinalIK source identity")
    identity: dict[str, object] = {name: required_text(raw_source.get(name), "FinalIK source." + name)
        for name in ("character_id", "avatar_cab", "clip_source", "animator_root_path", "resource_path")}
    for name in ("avatar_cab", "clip_source"):
        if re.fullmatch(r"CAB-[0-9a-fA-F]{32}", str(identity[name])) is None:
            raise CharacterReplayError("FinalIK source requires an exact CAB: " + name)
    for name in ("avatar_path_id", "clip_path_id"):
        value = raw_source.get(name)
        if type(value) is not int or value == 0 or not -(1 << 63) <= value < (1 << 63):
            raise CharacterReplayError("FinalIK source requires a nonzero int64: " + name)
        identity[name] = value
    if identity["resource_path"] != binding.action_resource_path:
        raise CharacterReplayError("FinalIK source resource differs from its selected Action")
    expected_pose = required_text(row.get("expected_input_source_local_frames_sha256"), "FinalIK expected input pose SHA256")
    if re.fullmatch(r"[0-9a-f]{64}", expected_pose) is None:
        raise CharacterReplayError("FinalIK expected input pose requires lowercase SHA256")
    hashes = json_object(row.get("source_sha256"), "FinalIK source_sha256")
    inputs = {str(configuration): hashlib.sha256(payload).hexdigest()}
    sources: dict[str, object] = {}
    for name in ("scenario", "source_component_inventory"):
        path, digest = pinned_file(configuration.parent, row.get(name), hashes.get(name), "FinalIK " + name)
        inputs[str(path)] = digest
        sources[name] = str(path)
    scenario = json_object(json.loads(Path(str(sources["scenario"])).read_text(encoding="utf-8-sig")), "FinalIK scenario")
    scenario_format = ("EndfieldFinalIKScenario/1" if version == "EndfieldCharacterFinalIKReplay/1"
                       else "EndfieldGrounderScenario/1")
    if scenario.get("format") != scenario_format:
        raise CharacterReplayError("The selected FinalIK bridge requires " + scenario_format)
    snapshot: dict[str, object] = {"format": "EndfieldCharacterFinalIKSnapshot/1",
        "label": required_text(row.get("label"), "FinalIK label"), "selection_binding": asdict(binding),
        "source_identity": identity, "expected_input_source_local_frames_sha256": expected_pose,
        "provenance": required_text(row.get("provenance"), "FinalIK provenance"), **sources, "input_sha256": inputs}
    if version == "EndfieldCharacterFinalIKReplay/2":
        snapshot["replay_format"] = "EndfieldFinalIKReplay/2"
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return {**snapshot, "replay_id": hashlib.sha256(encoded).hexdigest()}


def verify_finalik_snapshot(snapshot: dict[str, object]) -> None:
    from endfield_character_replay import replay_file_hash
    if snapshot.get("format") != "EndfieldCharacterFinalIKSnapshot/1":
        raise CharacterReplayError("Invalid server-owned FinalIK snapshot")
    for name, digest in json_object(snapshot.get("input_sha256"), "FinalIK inputs").items():
        path = Path(name)
        if not path.is_file() or replay_file_hash(path) != digest:
            raise CharacterReplayError("FinalIK configuration or scenario changed; refresh the Action catalog and submit again")
