"""Prepare source-verified global weather textures for character material profiles."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Literal

from endfield_character_cache import CharacterSourceChangedError, CharacterSourceStamp, source_stamp
from endfield_character_prepare import _bundle_files, _fingerprint
from endfield_character_sources import (
    CharacterSourceContext,
    game_source_stamps,
    validate_character_storage,
    verify_game_source,
)
from endfield_scene_discovery import observe_helper
from extractor_core.resources import extract_candidate_bundles


WEATHER_SLOTS: tuple[tuple[str, str], ...] = (
    ("snowCommonPreSettingParam", "characterSnowTexture"),
    ("rainCommonPreSettingParam", "characterRainDropTexture"),
    ("rainCommonPreSettingParam", "characterRainStreakTexture"),
    ("rainCommonPreSettingParam", "characterRainFaceDripTexture"),
    ("rainCommonPreSettingParam", "characterRainFaceDropletTexture"),
)
PIPELINE_RESOURCE_SUFFIX = "/settings/renderpipeline/hgrenderpipelineasset.asset"

PreparationStage = Literal[
    "validating_inputs", "discovering_pipeline", "extracting_pipeline", "reading_configuration",
    "resolving_textures", "extracting_textures", "validating_outputs", "completed",
]
ProgressScalar = str | int | float | bool
ProgressDetails = tuple[tuple[str, ProgressScalar], ...]
CheckCancelled = Callable[[], None]


@dataclass(frozen=True)
class CharacterWeatherPreparationProgress:
    stage: PreparationStage
    fraction: float
    message: str
    details: ProgressDetails


@dataclass(frozen=True)
class WeatherFileIdentity:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class WeatherTextureTarget:
    slot: str
    source: str
    path_id: int
    owner_path_id: int


@dataclass(frozen=True)
class WeatherTextureArtifact:
    slot: str
    source: str
    path_id: int
    width: int
    height: int
    texture_format: str
    color_space: int
    vertical_flip: bool
    artifact: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class WeatherTextureManifestEntry:
    slot: str
    source: str
    path_id: int
    width: int
    height: int
    texture_format: str
    color_space: int
    vertical_flip: bool
    artifact: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class WeatherTextureSourceEvidence:
    provider_index_sha256: str
    resource_database_sha256: str
    cab_inventory_sha256: str
    configuration_input_sha256: str
    configuration_typetrees_sha256: str
    configuration_receipt_sha256: str
    texture_input_sha256: str
    target_list_sha256: str
    texture_probe_receipt_sha256: str
    probe_texture_manifest_sha256: str


@dataclass(frozen=True)
class CharacterWeatherTextureManifest:
    format: Literal["EndfieldCharacterWeatherTextures/1"]
    status: Literal["prepared"]
    unity_version: str
    pipeline_resource_path: str
    provider_logical_name: str
    root_source: str
    source_evidence: WeatherTextureSourceEvidence
    textures: tuple[WeatherTextureManifestEntry, ...]


@dataclass(frozen=True)
class CharacterWeatherPreparationReceipt:
    format: Literal["EndfieldCharacterWeatherPreparation/1"]
    status: Literal["prepared"]
    unity_version: str
    pipeline_resource_path: str
    provider_logical_name: str
    provider_scan_count: int
    provider_match_count: int
    cab_inventory_record_count: int
    serialized_cabs: tuple[str, ...]
    root_source: str
    provider_index: WeatherFileIdentity
    resource_database: WeatherFileIdentity
    cab_inventory: WeatherFileIdentity
    game_source_stamps: tuple[CharacterSourceStamp, ...]
    reader_files: tuple[WeatherFileIdentity, ...]
    extracted_bundle: WeatherFileIdentity
    bundle_extraction: tuple[tuple[str, str], ...]
    configuration_input: WeatherFileIdentity
    configuration_typetrees: WeatherFileIdentity
    configuration_targets: tuple[WeatherTextureTarget, ...]
    configuration_receipt: WeatherFileIdentity
    texture_input: WeatherFileIdentity
    target_list: WeatherFileIdentity
    texture_receipt: WeatherFileIdentity
    probe_texture_manifest: WeatherFileIdentity
    texture_manifest: WeatherFileIdentity
    textures: tuple[WeatherTextureArtifact, ...]
    receipt_path: str


Progress = Callable[[CharacterWeatherPreparationProgress], None]


class CharacterWeatherPreparationError(RuntimeError):
    """The current game source cannot produce a fully verified weather texture set."""


def _emit(progress: Progress, stage: PreparationStage, fraction: float, message: str,
          details: ProgressDetails) -> None:
    if not 0.0 <= fraction <= 1.0:
        raise CharacterWeatherPreparationError(f"Weather preparation progress is outside [0, 1]: {fraction}")
    progress(CharacterWeatherPreparationProgress(stage, fraction, message, details))


def _file_identity(path: Path, check_cancelled: CheckCancelled) -> WeatherFileIdentity:
    source = path.expanduser().resolve()
    before = source_stamp(source)
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            check_cancelled()
            digest.update(chunk)
            total += len(chunk)
    after = source_stamp(source)
    if before != after or total != after.bytes:
        raise CharacterSourceChangedError(f"Weather source changed while hashing: path={source}")
    return WeatherFileIdentity(str(source), total, digest.hexdigest())


def _write_json(path: Path, value: object) -> WeatherFileIdentity:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Weather preparation refuses to overwrite output: path={path}")
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    with path.open("xb") as stream:
        stream.write(payload)
    return WeatherFileIdentity(str(path.resolve()), len(payload), hashlib.sha256(payload).hexdigest())


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CharacterWeatherPreparationError(f"Weather metadata must be an object: field={context}")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CharacterWeatherPreparationError(f"Weather metadata requires nonempty text: field={context}")
    return value


def _source_stamp_records(values: list[dict[str, object]]) -> tuple[CharacterSourceStamp, ...]:
    stamps: list[CharacterSourceStamp] = []
    for index, value in enumerate(values):
        path = value.get("path")
        integers = tuple(value.get(name) for name in ("device", "inode", "bytes", "modified_ns", "changed_ns"))
        if not isinstance(path, str) or any(type(item) is not int for item in integers):
            raise CharacterWeatherPreparationError(f"Game source stamp is invalid: index={index}")
        stamps.append(CharacterSourceStamp(path, integers[0], integers[1], integers[2], integers[3], integers[4]))
    if not stamps:
        raise CharacterWeatherPreparationError("Game source stamp set is empty")
    return tuple(stamps)


def _read_provider(provider_index: Path, check_cancelled: CheckCancelled) -> tuple[str, str, tuple[str, ...], int, int, WeatherFileIdentity]:
    source = provider_index.expanduser().resolve()
    before = source_stamp(source)
    digest = hashlib.sha256()
    total = 0
    candidates: dict[tuple[str, str], tuple[str, str, tuple[str, ...]]] = {}
    terminal: dict[str, object] = {}
    terminal_line = 0
    line_count = 0
    match_count = 0
    with source.open("rb") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line_count = line_number
            if line_number % 1024 == 0:
                check_cancelled()
            digest.update(raw_line)
            total += len(raw_line)
            try:
                row = _object(json.loads(raw_line.decode("utf-8-sig")), f"provider index line {line_number}")
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CharacterWeatherPreparationError(
                    f"Weather provider index has invalid JSONL: path={source}, line={line_number}, error={error}"
                ) from error
            kind = row.get("kind")
            if kind == "complete":
                if terminal:
                    raise CharacterWeatherPreparationError(f"Weather provider index has multiple terminal records: path={source}")
                terminal = row
                terminal_line = line_number
                continue
            if terminal:
                raise CharacterWeatherPreparationError(
                    f"Weather provider index has records after its terminal row: path={source}, line={line_number}"
                )
            if kind != "match":
                if kind == "error":
                    raise CharacterWeatherPreparationError(
                        f"Weather provider index contains a failed scan record: path={source}, line={line_number}"
                    )
                continue
            match_count += 1
            logical_name = _text(row.get("logical_name"), f"provider index line {line_number}.logical_name")
            containers = row.get("containers")
            if not isinstance(containers, list) or any(not isinstance(path, str) for path in containers):
                raise CharacterWeatherPreparationError(
                    f"Weather provider containers must be strings: path={source}, line={line_number}"
                )
            normalized_matches = tuple(
                (path.replace("\\", "/").strip("/"), path)
                for path in containers
                if path.replace("\\", "/").casefold().endswith(PIPELINE_RESOURCE_SUFFIX)
            )
            if not normalized_matches:
                continue
            serialized = row.get("serialized")
            if not isinstance(serialized, list) or not serialized:
                raise CharacterWeatherPreparationError(
                    f"Pipeline provider has no serialized CAB records: provider={logical_name}"
                )
            cab_names: list[str] = []
            for index, serialized_row in enumerate(serialized):
                cab = _object(serialized_row, f"provider {logical_name}.serialized[{index}]")
                cab_names.append(_text(cab.get("name"), f"provider {logical_name}.serialized[{index}].name"))
            frozen_cabs = tuple(sorted(set(cab_names), key=str.casefold))
            if len(frozen_cabs) != len(cab_names):
                raise CharacterWeatherPreparationError(f"Pipeline provider repeats a serialized CAB: provider={logical_name}")
            for normalized_path, original_path in normalized_matches:
                key = (normalized_path.casefold(), logical_name.casefold())
                prior = candidates.get(key)
                candidate = (original_path, logical_name, frozen_cabs)
                if prior is not None and prior != candidate:
                    raise CharacterWeatherPreparationError(
                        f"Pipeline provider identity or path spelling changes between index rows: provider={logical_name}"
                    )
                candidates[key] = candidate
    after = source_stamp(source)
    if before != after or total != after.bytes:
        raise CharacterSourceChangedError(f"Weather provider index changed while reading: path={source}")
    if (terminal.get("kind") != "complete" or terminal_line != line_count
            or type(terminal.get("failed")) is not int or terminal["failed"] != 0
            or type(terminal.get("scanned")) is not int or terminal["scanned"] <= 0
            or type(terminal.get("matches")) is not int or terminal["matches"] != match_count):
        raise CharacterWeatherPreparationError(f"Weather provider index is incomplete or failed: path={source}")
    if not candidates:
        raise CharacterWeatherPreparationError(
            f"No global HGRP pipeline asset provider was found in the supplied provider index: path={source}; "
            "the index must cover all asset containers, not only selected map paths"
        )
    paths = {path_key for path_key, _provider_key in candidates}
    providers = {provider_key for _path_key, provider_key in candidates}
    if len(paths) != 1 or len(providers) != 1:
        raise CharacterWeatherPreparationError(
            f"Global HGRP pipeline provider is ambiguous: paths={sorted(paths)}, providers={sorted(providers)}"
        )
    selected_path_key = next(iter(paths))
    selected_provider_key = next(iter(providers))
    selected_key = (selected_path_key, selected_provider_key)
    resource_path, logical_name, cab_names = candidates[selected_key]
    return (resource_path, logical_name, cab_names, terminal["scanned"], match_count,
            WeatherFileIdentity(str(source), total, digest.hexdigest()))


def _read_selected_cab_record(cab_inventory: Path, logical_name: str,
                              check_cancelled: CheckCancelled) -> tuple[tuple[str, ...], int, WeatherFileIdentity]:
    source = cab_inventory.expanduser().resolve()
    before = source_stamp(source)
    digest = hashlib.sha256()
    total = 0
    selected: tuple[str, ...] | None = None
    terminal: dict[str, object] = {}
    terminal_line = 0
    line_count = 0
    cab_count = 0
    with source.open("rb") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line_count = line_number
            if line_number % 2048 == 0:
                check_cancelled()
            digest.update(raw_line)
            total += len(raw_line)
            try:
                row = _object(json.loads(raw_line.decode("utf-8-sig")), f"CAB inventory line {line_number}")
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CharacterWeatherPreparationError(
                    f"Weather CAB inventory has invalid JSONL: path={source}, line={line_number}, error={error}"
                ) from error
            if row.get("kind") == "complete":
                if terminal:
                    raise CharacterWeatherPreparationError(f"Weather CAB inventory has multiple terminal records: path={source}")
                terminal = row
                terminal_line = line_number
                continue
            if terminal:
                raise CharacterWeatherPreparationError(
                    f"Weather CAB inventory has records after its terminal row: path={source}, line={line_number}"
                )
            candidate = row.get("logical_name")
            if row.get("kind") != "cab":
                continue
            cab_count += 1
            if not isinstance(candidate, str) or candidate.casefold() != logical_name.casefold():
                continue
            if selected is not None:
                raise CharacterWeatherPreparationError(
                    f"Pipeline provider repeats in CAB inventory: provider={logical_name}, path={source}"
                )
            serialized = row.get("serialized")
            if not isinstance(serialized, list) or not serialized:
                raise CharacterWeatherPreparationError(f"Pipeline CAB record has no serialized assets: provider={logical_name}")
            names: list[str] = []
            for index, serialized_row in enumerate(serialized):
                cab = _object(serialized_row, f"CAB inventory {logical_name}.serialized[{index}]")
                names.append(_text(cab.get("name"), f"CAB inventory {logical_name}.serialized[{index}].name"))
            selected = tuple(sorted(set(names), key=str.casefold))
            if len(selected) != len(names):
                raise CharacterWeatherPreparationError(f"CAB inventory repeats a serialized source: provider={logical_name}")
    after = source_stamp(source)
    if before != after or total != after.bytes:
        raise CharacterSourceChangedError(f"Weather CAB inventory changed while reading: path={source}")
    if (terminal.get("kind") != "complete" or terminal_line != line_count
            or type(terminal.get("failed")) is not int or terminal["failed"] != 0
            or type(terminal.get("scanned")) is not int or terminal["scanned"] != cab_count):
        raise CharacterWeatherPreparationError(f"Weather CAB inventory is incomplete or failed: path={source}")
    if selected is None:
        raise CharacterWeatherPreparationError(
            f"Selected pipeline provider is absent from CAB inventory: provider={logical_name}, path={source}"
        )
    identity = WeatherFileIdentity(str(source), total, digest.hexdigest())
    return selected, cab_count, identity


def _read_configuration_targets(path: Path, root_source: str,
                                expected_count: int, check_cancelled: CheckCancelled) -> tuple[WeatherTextureTarget, ...]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise CharacterWeatherPreparationError(f"Cannot read weather configuration TypeTrees: path={path}: {error}") from error
    if not isinstance(rows, list) or not rows:
        raise CharacterWeatherPreparationError(f"Weather configuration TypeTrees are empty or invalid: path={path}")
    if len(rows) != expected_count:
        raise CharacterWeatherPreparationError(
            f"Weather configuration TypeTree count differs from its probe receipt: expected={expected_count}, actual={len(rows)}"
        )
    found: dict[str, WeatherTextureTarget] = {}
    expected_slots = tuple(slot for _group, slot in WEATHER_SLOTS)
    for row_index, value in enumerate(rows):
        if row_index % 32 == 0:
            check_cancelled()
        row = _object(value, f"configuration TypeTree[{row_index}]")
        data_value = row.get("data")
        if not isinstance(data_value, dict):
            continue
        if row.get("available") is not True:
            raise CharacterWeatherPreparationError(f"Weather configuration TypeTree is unavailable: row={row_index}")
        object_identity = _object(row.get("object"), f"configuration TypeTree[{row_index}].object")
        owner_source = _text(object_identity.get("source"), f"configuration TypeTree[{row_index}].object.source")
        owner_path_id = object_identity.get("path_id")
        if type(owner_path_id) is not int or owner_path_id == 0:
            raise CharacterWeatherPreparationError(f"Configuration object identity has invalid path ID: row={row_index}")
        for group_name, slot in WEATHER_SLOTS:
            group_value = data_value.get(group_name)
            if not isinstance(group_value, dict) or slot not in group_value:
                continue
            if owner_source.casefold() != root_source.casefold():
                raise CharacterWeatherPreparationError(
                    f"Weather setting owner is outside the resolved pipeline CAB: slot={slot}, owner={owner_source}, root={root_source}"
                )
            pointer = _object(group_value[slot], f"configuration.{group_name}.{slot}")
            file_id, path_id = pointer.get("m_FileID"), pointer.get("m_PathID")
            if type(file_id) is not int or file_id != 0 or type(path_id) is not int or path_id == 0:
                raise CharacterWeatherPreparationError(
                    f"Weather texture must be a non-null internal pointer: slot={slot}, file_id={file_id}, path_id={path_id}"
                )
            if slot in found:
                raise CharacterWeatherPreparationError(f"Weather texture slot is ambiguous in source configuration: slot={slot}")
            found[slot] = WeatherTextureTarget(slot, root_source, path_id, owner_path_id)
    missing = sorted(set(expected_slots) - set(found))
    extra = sorted(set(found) - set(expected_slots))
    if missing or extra or len(found) != len(WEATHER_SLOTS):
        raise CharacterWeatherPreparationError(f"Weather texture slots differ from the required profile contract: missing={missing}, extra={extra}")
    return tuple(found[slot] for slot in expected_slots)


def _read_texture_artifacts(path: Path, output: Path, targets: tuple[WeatherTextureTarget, ...],
                            check_cancelled: CheckCancelled) -> tuple[WeatherTextureArtifact, ...]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise CharacterWeatherPreparationError(f"Cannot read extracted weather texture manifest: path={path}: {error}") from error
    if not isinstance(rows, list) or len(rows) != len(targets):
        raise CharacterWeatherPreparationError(
            f"Extracted weather texture manifest has the wrong row count: path={path}, expected={len(targets)}"
        )
    target_by_slot = {target.slot: target for target in targets}
    artifacts: dict[str, WeatherTextureArtifact] = {}
    root = output.resolve()
    for index, value in enumerate(rows):
        check_cancelled()
        row = _object(value, f"weather texture artifact[{index}]")
        slot = _text(row.get("slot"), f"weather texture artifact[{index}].slot")
        target = target_by_slot.get(slot)
        if target is None or slot in artifacts:
            raise CharacterWeatherPreparationError(f"Extracted texture slot is unexpected or repeated: slot={slot}")
        source_value = _object(row.get("source"), f"weather texture artifact[{index}].source")
        source = _text(source_value.get("source"), f"weather texture artifact[{index}].source.source")
        path_id = source_value.get("path_id")
        if source.casefold() != target.source.casefold() or type(path_id) is not int or path_id != target.path_id:
            raise CharacterWeatherPreparationError(
                f"Extracted texture identity differs from the configuration pointer: slot={slot}, source={source}, path_id={path_id}"
            )
        width, height = row.get("width"), row.get("height")
        color_space, byte_count = row.get("color_space"), row.get("bytes")
        texture_format = _text(row.get("texture_format"), f"weather texture artifact[{index}].texture_format")
        vertical_flip = row.get("vertical_flip")
        relative_artifact = _text(row.get("artifact"), f"weather texture artifact[{index}].artifact")
        if (type(width) is not int or width <= 0 or type(height) is not int or height <= 0
                or type(color_space) is not int or type(byte_count) is not int or byte_count <= 0
                or vertical_flip is not True):
            raise CharacterWeatherPreparationError(f"Extracted texture metadata is invalid: slot={slot}")
        artifact_path = (path.parent / relative_artifact).resolve()
        if not artifact_path.is_relative_to(root) or artifact_path.name != relative_artifact or not artifact_path.is_file():
            raise CharacterWeatherPreparationError(f"Extracted texture artifact path is unsafe or missing: slot={slot}, path={artifact_path}")
        fingerprint = _fingerprint(artifact_path, check_cancelled)
        expected_sha = _text(row.get("sha256"), f"weather texture artifact[{index}].sha256")
        if fingerprint.bytes != byte_count or fingerprint.sha256.casefold() != expected_sha.casefold():
            raise CharacterWeatherPreparationError(
                f"Extracted texture bytes or SHA256 differ from probe receipt: slot={slot}, path={artifact_path}"
            )
        artifacts[slot] = WeatherTextureArtifact(
            slot, source, path_id, width, height, texture_format, color_space, vertical_flip,
            str(artifact_path), fingerprint.bytes, fingerprint.sha256,
        )
    if set(artifacts) != set(target_by_slot):
        raise CharacterWeatherPreparationError("Extracted weather texture manifest does not contain every required slot")
    return tuple(artifacts[target.slot] for target in targets)


def _probe_progress(stage: PreparationStage, base: float, span: float, progress: Progress,
                    row: dict[str, object]) -> None:
    phase = row.get("phase")
    current, total = row.get("current"), row.get("total")
    if not isinstance(phase, str) or not phase:
        raise CharacterWeatherPreparationError(f"GlobalWeatherProbe emitted invalid progress: row={row}")
    fraction = base
    if type(current) is int and type(total) is int and total > 0:
        fraction = base + span * min(max(current / total, 0.0), 1.0)
    detail_values: list[tuple[str, ProgressScalar]] = [("probe_phase", phase)]
    for name in ("current", "total", "readBytes", "writeBytes", "peakWorkingSetBytes"):
        value = row.get(name)
        if type(value) in (str, int, float, bool):
            detail_values.append((name, value))
    _emit(progress, stage, fraction, f"GlobalWeatherProbe {phase}", tuple(sorted(detail_values)))


def prepare_character_weather_textures(
    context: CharacterSourceContext,
    unity_version: str,
    probe: Path,
    output: Path,
    check_cancelled: CheckCancelled,
    progress: Progress,
) -> CharacterWeatherPreparationReceipt:
    """Extract the five verified global weather textures from the selected game's pipeline asset."""
    check_cancelled()
    if not unity_version.strip():
        raise ValueError("Weather preparation requires an explicit nonempty Unity version")
    if output.expanduser().is_symlink() or output.expanduser().exists():
        raise FileExistsError(f"Weather preparation requires a fresh output directory: path={output}")
    resolved_output = output.expanduser().resolve()
    validate_character_storage(context.game_root, resolved_output)
    for source_path, label in (
        (context.provider_index, "provider index"),
        (context.resource_database, "resource database"),
        (context.cab_inventory, "CAB inventory"),
    ):
        if not source_path.is_file():
            raise FileNotFoundError(f"Weather preparation input is missing: {label}={source_path}")
        if source_path.resolve().is_relative_to(resolved_output):
            raise CharacterWeatherPreparationError(f"Weather output would contain a source file: label={label}, output={resolved_output}")
    reader = probe.expanduser().resolve()
    if not reader.is_file() or reader.is_relative_to(context.game_root.expanduser().resolve()):
        raise FileNotFoundError(f"Verified weather reader executable is missing or inside the game tree: path={reader}")
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.mkdir()

    _emit(progress, "validating_inputs", 0.02, "Fingerprinting game, indexes, and reader", ())
    expected_game_stamps = game_source_stamps(context.game_root)
    game_stamps = _source_stamp_records(expected_game_stamps)
    database_identity = _file_identity(context.resource_database, check_cancelled)
    reader_files = tuple(
        _file_identity(path, check_cancelled)
        for path in sorted(
            (path for path in reader.parent.rglob("*") if path.is_file() and path.suffix.casefold() in (".exe", ".dll", ".json")),
            key=lambda path: str(path).casefold(),
        )
    )
    if reader not in {Path(row.path) for row in reader_files}:
        reader_files = (*reader_files, _file_identity(reader, check_cancelled))
    check_cancelled()

    _emit(progress, "discovering_pipeline", 0.08, "Resolving the exact pipeline asset from the provider index", ())
    resource_path, provider_name, provider_cabs, provider_scan_count, provider_match_count, provider_identity = (
        _read_provider(context.provider_index, check_cancelled)
    )
    cab_names, cab_record_count, cab_identity = _read_selected_cab_record(context.cab_inventory, provider_name, check_cancelled)
    if provider_scan_count != cab_record_count:
        raise CharacterWeatherPreparationError(
            f"Provider index and CAB inventory cover different asset counts: provider_scanned={provider_scan_count}, "
            f"cab_inventory_records={cab_record_count}"
        )
    if {name.casefold() for name in provider_cabs} != {name.casefold() for name in cab_names}:
        raise CharacterWeatherPreparationError(
            f"Pipeline provider CAB identity differs between provider and CAB indexes: provider={provider_name}"
        )
    check_cancelled()

    bundle_root = resolved_output / "pipeline_bundle"
    def extraction_progress(phase: str, fraction: float, message: str, details: dict[str, object] | None) -> None:
        _emit(progress, "extracting_pipeline", 0.1 + 0.38 * fraction, message,
              (("source_phase", phase),))

    def extraction_cancelled() -> bool:
        check_cancelled()
        return False

    extraction_receipt = extract_candidate_bundles(
        context.resource_database,
        context.game_root / "Endfield_Data" / "StreamingAssets" / "VFS",
        [provider_name],
        bundle_root,
        progress=extraction_progress,
        cancelled=extraction_cancelled,
    )
    if (extraction_receipt.get("requested_count") != "1" or extraction_receipt.get("extracted_count") != "1"):
        raise CharacterWeatherPreparationError(f"Pipeline provider extraction was incomplete: {extraction_receipt}")
    bundle_files = _bundle_files(bundle_root, (provider_name,))
    if len(bundle_files) != 1:
        raise CharacterWeatherPreparationError(f"Pipeline provider extraction produced unexpected files: count={len(bundle_files)}")
    extracted_name, extracted_path = bundle_files[0]
    if extracted_name.casefold() != provider_name.casefold():
        raise CharacterWeatherPreparationError(f"Extracted provider changed logical identity: expected={provider_name}, actual={extracted_name}")
    extracted_fingerprint = _fingerprint(extracted_path, check_cancelled)
    extracted_identity = WeatherFileIdentity(extracted_fingerprint.path, extracted_fingerprint.bytes, extracted_fingerprint.sha256)
    bundle_descriptor = {"path": extracted_path.relative_to(resolved_output).as_posix(),
                         "bytes": extracted_identity.bytes, "sha256": extracted_identity.sha256}

    first_input_path = resolved_output / "configuration_input.json"
    base_input = {
        "format": "EndfieldSingleCharacterInput/1",
        "unity_version": unity_version,
        "bundles": [bundle_descriptor],
        "root": {"source": "", "resource_path": resource_path},
    }
    first_input_identity = _write_json(first_input_path, base_input)
    config_output = resolved_output / "configuration_probe"
    config_log = resolved_output / "logs" / "configuration_probe.log"
    config_log.parent.mkdir(parents=True, exist_ok=True)
    _emit(progress, "reading_configuration", 0.5, "Reading exact pipeline configuration TypeTrees", ())
    observe_helper(
        [str(reader), "--configuration-resource", resource_path,
         "--input", str(first_input_path), "--output", str(config_output)],
        config_log,
        resolved_output / "logs" / "temp_configuration",
        check_cancelled,
        lambda row: _probe_progress("reading_configuration", 0.5, 0.16, progress, row),
    )
    config_receipt_path = config_output / "receipt.json"
    config_tree_path = config_output / "configuration_typetrees.json"
    try:
        config_receipt = _object(json.loads(config_receipt_path.read_text(encoding="utf-8-sig")), str(config_receipt_path))
    except (OSError, json.JSONDecodeError) as error:
        raise CharacterWeatherPreparationError(f"GlobalWeatherProbe did not write a readable configuration receipt: {error}") from error
    if config_receipt.get("format") != "EndfieldGlobalConfigurationProbe/1":
        raise CharacterWeatherPreparationError(f"Unsupported GlobalWeatherProbe configuration receipt: {config_receipt.get('format')}")
    config_input_sha256 = config_receipt.get("input_sha256")
    if not isinstance(config_input_sha256, str) or config_input_sha256.casefold() != first_input_identity.sha256.casefold():
        raise CharacterWeatherPreparationError("GlobalWeatherProbe configuration receipt does not pin its input manifest bytes")
    root_source = _text(config_receipt.get("source"), "configuration receipt.source")
    if root_source.casefold() not in {name.casefold() for name in cab_names}:
        raise CharacterWeatherPreparationError(
            f"Configuration resource resolved outside its provider serialized CAB set: source={root_source}, provider={provider_name}"
        )
    if not config_tree_path.is_file():
        raise CharacterWeatherPreparationError(f"GlobalWeatherProbe omitted configuration TypeTrees: path={config_tree_path}")
    configuration_count = config_receipt.get("configuration_objects")
    if type(configuration_count) is not int or configuration_count <= 0:
        raise CharacterWeatherPreparationError("GlobalWeatherProbe configuration receipt has an invalid TypeTree count")
    targets = _read_configuration_targets(config_tree_path, root_source, configuration_count, check_cancelled)
    config_tree_identity = _file_identity(config_tree_path, check_cancelled)
    config_receipt_identity = _file_identity(config_receipt_path, check_cancelled)
    texture_input_path = resolved_output / "texture_input.json"
    texture_input = {**base_input, "root": {"source": root_source, "resource_path": resource_path}}
    texture_input_identity = _write_json(texture_input_path, texture_input)
    target_path = resolved_output / "weather_texture_targets.json"
    target_identity = _write_json(
        target_path,
        [{"slot": target.slot, "source": target.source, "path_id": target.path_id} for target in targets],
    )

    texture_output = resolved_output / "weather_textures"
    texture_log = resolved_output / "logs" / "texture_probe.log"
    _emit(progress, "extracting_textures", 0.7, "Extracting the five exact internal weather textures", ())
    observe_helper(
        [str(reader), "--input", str(texture_input_path), "--textures", str(target_path), "--output", str(texture_output)],
        texture_log,
        resolved_output / "logs" / "temp_textures",
        check_cancelled,
        lambda row: _probe_progress("extracting_textures", 0.7, 0.22, progress, row),
    )
    texture_manifest_path = texture_output / "weather_texture_artifacts.json"
    texture_receipt_path = texture_output / "receipt.json"
    try:
        texture_receipt = _object(json.loads(texture_receipt_path.read_text(encoding="utf-8-sig")), str(texture_receipt_path))
    except (OSError, json.JSONDecodeError) as error:
        raise CharacterWeatherPreparationError(f"GlobalWeatherProbe did not write a readable texture receipt: {error}") from error
    texture_input_sha256 = texture_receipt.get("input_sha256")
    texture_root_source = texture_receipt.get("source")
    if (texture_receipt.get("format") != "EndfieldGlobalConfigurationProbe/1"
            or not isinstance(texture_input_sha256, str)
            or texture_input_sha256.casefold() != texture_input_identity.sha256.casefold()
            or not isinstance(texture_root_source, str)
            or texture_root_source.casefold() != root_source.casefold()):
        raise CharacterWeatherPreparationError("GlobalWeatherProbe texture receipt does not pin the supplied input and root CAB")
    texture_artifacts = _read_texture_artifacts(texture_manifest_path, resolved_output, targets, check_cancelled)
    texture_receipt_identity = _file_identity(texture_receipt_path, check_cancelled)
    probe_texture_manifest_identity = _file_identity(texture_manifest_path, check_cancelled)
    manifest_entries = tuple(
        WeatherTextureManifestEntry(
            artifact.slot,
            artifact.source,
            artifact.path_id,
            artifact.width,
            artifact.height,
            artifact.texture_format,
            artifact.color_space,
            artifact.vertical_flip,
            Path(artifact.artifact).relative_to(resolved_output).as_posix(),
            artifact.bytes,
            artifact.sha256,
        )
        for artifact in texture_artifacts
    )
    source_evidence = WeatherTextureSourceEvidence(
        provider_identity.sha256,
        database_identity.sha256,
        cab_identity.sha256,
        first_input_identity.sha256,
        config_tree_identity.sha256,
        config_receipt_identity.sha256,
        texture_input_identity.sha256,
        target_identity.sha256,
        texture_receipt_identity.sha256,
        probe_texture_manifest_identity.sha256,
    )
    portable_manifest = CharacterWeatherTextureManifest(
        "EndfieldCharacterWeatherTextures/1",
        "prepared",
        unity_version,
        resource_path,
        provider_name,
        root_source,
        source_evidence,
        manifest_entries,
    )
    texture_manifest_identity = _write_json(
        resolved_output / "character_weather_textures.json", asdict(portable_manifest)
    )
    reader_after = tuple(_file_identity(Path(row.path), check_cancelled) for row in reader_files)
    if reader_after != reader_files:
        raise CharacterSourceChangedError("GlobalWeatherProbe files changed during weather extraction")
    if _file_identity(context.resource_database, check_cancelled) != database_identity:
        raise CharacterSourceChangedError("Weather resource database changed during extraction")
    if _file_identity(context.provider_index, check_cancelled) != provider_identity:
        raise CharacterSourceChangedError("Weather provider index changed during extraction")
    if _file_identity(context.cab_inventory, check_cancelled) != cab_identity:
        raise CharacterSourceChangedError("Weather CAB inventory changed during extraction")
    if _file_identity(extracted_path, check_cancelled) != extracted_identity:
        raise CharacterSourceChangedError("Extracted pipeline bundle changed during weather extraction")
    verify_game_source(context.game_root, expected_game_stamps, check_cancelled)
    _emit(progress, "validating_outputs", 0.96, "Verifying source identities and all emitted texture bytes", ())

    receipt_path = resolved_output / "weather_preparation_receipt.json"
    receipt = CharacterWeatherPreparationReceipt(
        "EndfieldCharacterWeatherPreparation/1",
        "prepared",
        unity_version,
        resource_path,
        provider_name,
        provider_scan_count,
        provider_match_count,
        cab_record_count,
        provider_cabs,
        root_source,
        provider_identity,
        database_identity,
        cab_identity,
        game_stamps,
        reader_files,
        extracted_identity,
        tuple(sorted((str(key), str(value)) for key, value in extraction_receipt.items())),
        first_input_identity,
        config_tree_identity,
        targets,
        config_receipt_identity,
        texture_input_identity,
        target_identity,
        texture_receipt_identity,
        probe_texture_manifest_identity,
        texture_manifest_identity,
        texture_artifacts,
        str(receipt_path.resolve()),
    )
    _write_json(receipt_path, asdict(receipt))
    _emit(progress, "completed", 1.0, "Prepared and verified all five global weather textures", (("texture_count", len(texture_artifacts)),))
    return receipt
