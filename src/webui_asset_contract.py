"""Canonical request contract for shared WebUI asset exports."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID


AssetKind = Literal["weapon", "illustration", "display_prop"]
AssetExportFormat = Literal["AssetExportRequest/1"]
WeaponVariant = Literal["base", "refined_candidate"]

ASSET_EXPORT_REQUEST_FORMAT: AssetExportFormat = "AssetExportRequest/1"
ASSET_KINDS: tuple[AssetKind, ...] = ("weapon", "illustration", "display_prop")
WEAPON_VARIANTS: tuple[WeaponVariant, ...] = ("base", "refined_candidate")
UUID_TEXT = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True, slots=True)
class AssetExportRequest:
    """Validated asset export request with canonical JSON serialization."""

    format: AssetExportFormat
    request_id: UUID
    kind: AssetKind
    catalog_id: str
    item_ids: tuple[str, ...]
    variant: WeaponVariant | None

    def __post_init__(self) -> None:
        if self.format != ASSET_EXPORT_REQUEST_FORMAT:
            raise ValueError(f"format is unsupported: {self.format!r}")
        if not isinstance(self.request_id, UUID):
            raise ValueError("request_id must be a UUID")
        if not isinstance(self.kind, str) or self.kind not in ASSET_KINDS:
            raise ValueError(f"kind is unsupported: {self.kind!r}")
        _nonempty_string(self.catalog_id, "catalog_id")
        if not isinstance(self.item_ids, tuple) or not self.item_ids:
            raise ValueError("item_ids must be a non-empty tuple of strings")
        if any(not isinstance(item_id, str) or not item_id.strip() for item_id in self.item_ids):
            raise ValueError("item_ids must contain non-empty strings")
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("item_ids must not contain duplicates")
        if self.kind == "weapon":
            if not isinstance(self.variant, str) or self.variant not in WEAPON_VARIANTS:
                raise ValueError("weapon requests require variant 'base' or 'refined_candidate'")
        elif self.variant is not None:
            raise ValueError(f"variant is not supported for kind {self.kind!r}")

    def as_json(self) -> dict[str, str | list[str]]:
        """Return only supported fields using JSON-native array values."""
        result: dict[str, str | list[str]] = {
            "format": self.format,
            "request_id": str(self.request_id),
            "kind": self.kind,
            "catalog_id": self.catalog_id,
            "item_ids": list(self.item_ids),
        }
        if self.variant is not None:
            result["variant"] = self.variant
        return result


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _item_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("item_ids must be a non-empty array of strings")
    if any(not isinstance(item_id, str) or not item_id.strip() for item_id in value):
        raise ValueError("item_ids must contain non-empty strings")
    normalized = tuple(value)
    if len(set(normalized)) != len(normalized):
        raise ValueError("item_ids must not contain duplicates")
    return normalized


def parse_asset_export_request(value: object) -> AssetExportRequest:
    """Parse and canonicalize a shared asset export request object."""
    if not isinstance(value, Mapping):
        raise ValueError("asset export request must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError("asset export request keys must be strings")
    request = cast(Mapping[str, object], value)
    required_fields = ("format", "request_id", "kind", "catalog_id", "item_ids")
    missing_fields = tuple(field for field in required_fields if field not in request)
    if missing_fields:
        raise ValueError("asset export request is missing required fields: " + ", ".join(missing_fields))

    raw_format = _nonempty_string(request["format"], "format")
    if raw_format != ASSET_EXPORT_REQUEST_FORMAT:
        raise ValueError(f"format is unsupported: {raw_format!r}")
    raw_request_id = _nonempty_string(request["request_id"], "request_id")
    if UUID_TEXT.fullmatch(raw_request_id) is None:
        raise ValueError("request_id must be a UUID string")
    try:
        request_id = UUID(raw_request_id)
    except ValueError as error:
        raise ValueError("request_id must be a UUID string") from error

    raw_kind = _nonempty_string(request["kind"], "kind")
    if raw_kind not in ASSET_KINDS:
        raise ValueError(f"kind is unsupported: {raw_kind!r}")
    kind = cast(AssetKind, raw_kind)
    catalog_id = _nonempty_string(request["catalog_id"], "catalog_id")
    item_ids = _item_ids(request["item_ids"])

    if kind == "weapon":
        if "variant" not in request:
            raise ValueError("weapon requests require variant 'base' or 'refined_candidate'")
        raw_variant = _nonempty_string(request["variant"], "variant")
        if raw_variant not in WEAPON_VARIANTS:
            raise ValueError(f"weapon variant is unsupported: {raw_variant!r}")
        variant: WeaponVariant | None = cast(WeaponVariant, raw_variant)
    else:
        if "variant" in request:
            raise ValueError(f"variant is not supported for kind {kind!r}")
        variant = None

    return AssetExportRequest(raw_format, request_id, kind, catalog_id, item_ids, variant)
