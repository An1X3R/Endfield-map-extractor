"""Authenticate one server-owned explicit ordinary-layer replay selection."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re

from webui_character_contract import CharacterRequestError, json_object, required_text


class CharacterReplayError(CharacterRequestError):
    """A configured replay or its immutable selection no longer matches."""


@dataclass(frozen=True)
class ReplaySelectionBinding:
    source_identity: str
    selection_id: str
    variant_id: str
    action_resource_path: str
    action_logical_provider: str


def replay_file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def replay_source_identity(value: object) -> dict[str, object]:
    source = json_object(value, "animation_source_identity")
    result: dict[str, object] = {name: required_text(source.get(name), "animation_source_identity." + name)
                               for name in ("character_id", "avatar_cab", "clip_source", "controller_cab")}
    for name in ("avatar_cab", "clip_source", "controller_cab"):
        if re.fullmatch(r"CAB-[0-9a-fA-F]{32}", str(result[name])) is None:
            raise CharacterReplayError("Replay source requires exact CAB identities: " + name)
    for name in ("avatar_path_id", "clip_path_id", "controller_path_id"):
        number = source.get(name)
        if type(number) is not int or not -(1 << 63) <= number < (1 << 63) or number == 0:
            raise CharacterReplayError("Replay source requires a nonzero int64 PathID: " + name)
        result[name] = number
    return result


def read_configured_replay(configuration: Path) -> dict[str, object]:
    """Bind ID to exact server configuration and explicitly pinned scenario bytes."""
    configuration_bytes = configuration.read_bytes()
    record = json_object(json.loads(configuration_bytes.decode("utf-8-sig")), "ordinary replay configuration")
    if record.get("format") != "EndfieldCharacterOrdinaryReplay/1":
        raise CharacterReplayError("Expected EndfieldCharacterOrdinaryReplay/1")
    raw_binding = json_object(record.get("selection_binding"), "selection_binding")
    binding = ReplaySelectionBinding(**{name: required_text(raw_binding.get(name), "selection_binding." + name)
        for name in ReplaySelectionBinding.__dataclass_fields__})
    expected = json_object(record.get("scenario_sha256"), "scenario_sha256")
    inputs = {str(configuration): hashlib.sha256(configuration_bytes).hexdigest()}
    scenarios: dict[str, object] = {}
    for name in ("parameter_scenario", "state_scenario"):
        path = (configuration.parent / required_text(record.get(name), name)).resolve(strict=True)
        digest = required_text(expected.get(name), "scenario_sha256." + name)
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None or replay_file_hash(path) != digest:
            raise CharacterReplayError("Configured replay scenario bytes differ from pinned SHA256: " + name)
        scenarios[name] = str(path)
        inputs[str(path)] = digest
    label = required_text(record.get("label"), "replay.label")
    if len(label) > 120:
        raise CharacterReplayError("Replay label must contain at most 120 characters")
    snapshot: dict[str, object] = {"format": "EndfieldCharacterOrdinaryReplaySnapshot/1", "label": label,
        "selection_binding": asdict(binding), "source_identity": replay_source_identity(record.get("animation_source_identity")),
        "provenance": required_text(record.get("provenance"), "replay.provenance"),
        **scenarios, "input_sha256": inputs}
    identity = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return {**snapshot, "replay_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()}


def replay_matches(snapshot: dict[str, object], binding: ReplaySelectionBinding, character_id: str) -> bool:
    return (snapshot["selection_binding"] == asdict(binding)
            and json_object(snapshot["source_identity"], "replay source")["character_id"] == character_id)


def verify_replay_snapshot(snapshot: dict[str, object]) -> None:
    """Reject edits between canonicalization, queue admission and tool execution."""
    if snapshot.get("format") != "EndfieldCharacterOrdinaryReplaySnapshot/1":
        raise CharacterReplayError("Invalid server-owned replay snapshot")
    for name, expected in json_object(snapshot.get("input_sha256"), "replay inputs").items():
        path = Path(name)
        if not path.is_file() or replay_file_hash(path) != expected:
            raise CharacterReplayError("Replay configuration or scenario changed after selection; refresh the action catalog and submit a new request")
