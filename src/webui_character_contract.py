"""Validate the public character request envelope without executing tools."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, TypedDict, cast
from uuid import UUID


CharacterJobKind = Literal["preparation", "export"]


class CharacterRequestError(ValueError):
    """A public request violates the character API contract."""


class CharacterRequestConflict(CharacterRequestError):
    """A request UUID already belongs to different parameters."""


class CharacterJobNotFound(FileNotFoundError):
    """A durable character task does not exist."""


class CharacterPublicError(TypedDict):
    code: str
    message: str
    stage: str
    retryable: bool
    diagnostic_id: str


def json_object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CharacterRequestError(f"{field}: expected an object")
    return cast(dict[str, object], value)


def required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CharacterRequestError(f"{field}: expected non-empty text")
    return value.strip()


def request_uuid(value: object) -> str:
    text = required_text(value, "request_id")
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise CharacterRequestError("request_id: expected a canonical lowercase UUID") from error
    if str(parsed) != text:
        raise CharacterRequestError("request_id: expected a canonical lowercase UUID")
    return text


def normalize_character_request(payload: object) -> tuple[CharacterJobKind, dict[str, object]]:
    """Require public fields, ignore extras, and leave source checks to the service."""
    value = json_object(payload, "request")
    request_format = value.get("format")
    request: dict[str, object] = {"format": request_format, "request_id": request_uuid(value.get("request_id"))}
    if request_format == "CharacterPreparationRequest/1":
        source = json_object(value.get("source"), "source")
        request["source"] = {field: required_text(source.get(field), f"source.{field}")
                             for field in ("game_root", "blender_exe", "cache_root", "output_root")}
        return "preparation", request
    if request_format != "CharacterExportRequest/1":
        raise CharacterRequestError("Unsupported character request format")
    for field in ("catalog_id", "source_identity", "selection_id", "variant_id", "export_profile_id", "output_label"):
        request[field] = required_text(value.get(field), field)
    if request["export_profile_id"] != "hgrp-preview":
        raise CharacterRequestError("export_profile_id: expected hgrp-preview")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", str(request["output_label"])) is None:
        raise CharacterRequestError("output_label: expected 1-80 filename-safe characters")
    if "options" in value:
        options = json_object(value["options"], "options")
        normalized: dict[str, object] = {}
        for field in ("raw_expressions", "basic_mmd"):
            flag = options.get(field, False)
            if type(flag) is not bool:
                raise CharacterRequestError(f"options.{field}: expected boolean")
            normalized[field] = flag
        animation = options.get("animation")
        if "native_expression_replay_id" in options:
            identifier = required_text(options["native_expression_replay_id"], "options.native_expression_replay_id")
            if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
                raise CharacterRequestError("options.native_expression_replay_id: expected lowercase SHA256 identity")
            if animation is None:
                raise CharacterRequestError("Native expression replay requires an explicit body Action")
            normalized["native_expression_replay_id"] = identifier
        if animation is not None:
            row = json_object(animation, "options.animation")
            frames = row.get("maximum_stored_frames")
            if type(frames) is not int or not 2 <= frames <= 18000:
                raise CharacterRequestError("options.animation.maximum_stored_frames: expected integer from 2 to 18000")
            selectors = tuple(field for field in ("action_id", "reference_id") if field in row)
            if len(selectors) != 1:
                raise CharacterRequestError("options.animation: require exactly one of action_id or reference_id")
            selector = selectors[0]
            identifier = required_text(row[selector], "options.animation." + selector)
            if selector == "reference_id" and re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
                raise CharacterRequestError("options.animation.reference_id: expected lowercase SHA256 identity")
            normalized["animation"] = {selector: identifier, "maximum_stored_frames": frames}
            if "native_expression_replay_id" in normalized and selector != "action_id":
                raise CharacterRequestError("Native expression replay requires action_id; NPC reference replay is unsupported")
            if "replay_id" in row:
                replay_id = required_text(row["replay_id"], "options.animation.replay_id")
                if selector != "action_id":
                    raise CharacterRequestError("options.animation.replay_id requires action_id; NPC reference replay is unsupported")
                if re.fullmatch(r"[0-9a-f]{64}", replay_id) is None:
                    raise CharacterRequestError("options.animation.replay_id: expected lowercase SHA256 identity")
                normalized["animation"]["replay_id"] = replay_id
            if "finalik_replay_id" in row:
                identifier = required_text(row["finalik_replay_id"], "options.animation.finalik_replay_id")
                if selector != "action_id":
                    raise CharacterRequestError("options.animation.finalik_replay_id requires action_id; NPC reference replay is unsupported")
                if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
                    raise CharacterRequestError("options.animation.finalik_replay_id: expected lowercase SHA256 identity")
                normalized["animation"]["finalik_replay_id"] = identifier
        request["options"] = normalized
    return "export", request


def request_digest(request: dict[str, object]) -> str:
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def public_request(request: dict[str, object]) -> dict[str, object]:
    return {field: request[field] for field in
            ("format", "request_id", "catalog_id", "source_identity", "selection_id", "variant_id", "export_profile_id", "output_label", "options")
            if field in request}
