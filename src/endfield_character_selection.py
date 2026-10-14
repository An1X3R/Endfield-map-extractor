"""Resolve exact character selections and verify offline reader root bindings.

These interfaces are independent of scene preparation. A selected provider is a
candidate until the reader verifies the actual container and source root; neither
step certifies complete geometry, materials, animation, or game-build provenance.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

from endfield_character_catalog import CharacterCatalog, CharacterResourceProvider


class CharacterSelectionError(ValueError):
    """A selection is stale, ambiguous, or inconsistent with source evidence."""


@dataclass(frozen=True)
class CharacterSelection:
    catalog_identity: str
    character_id: str
    template_id: str
    part_name: str
    variant: Literal["characters", "npc"]
    resource_path: str
    logical_provider: str


@dataclass(frozen=True)
class CharacterProbeBinding:
    selection: CharacterSelection
    root_source: str
    root_path_id: str
    root_name: str
    status: Literal["source_reference_verified"]


T = TypeVar("T")


def _one(values: Sequence[T], context: str) -> T:
    if len(values) != 1:
        raise CharacterSelectionError(f"Character selection must resolve once: field={context}, matches={len(values)}")
    return values[0]


def _path(value: str) -> str:
    return value.replace("\\", "/").casefold()


def resolve_character_selection(
    catalog: CharacterCatalog, selection: CharacterSelection,
) -> CharacterResourceProvider:
    """Resolve all identity levels explicitly; never choose the first variant."""
    if selection.catalog_identity != catalog.source_identity:
        raise CharacterSelectionError("Character catalog changed; refresh the catalog and repeat the selection")
    _one(tuple(row for row in catalog.characters if row.character_id == selection.character_id), "character_id")
    binding = _one(tuple(row for row in catalog.model_bindings if row.character_id == selection.character_id), "model binding")
    template = _one(tuple(row for row in binding.templates if row.template_id == selection.template_id), "template_id")
    part = _one(tuple(row for row in template.parts if row.part_name == selection.part_name), "part_name")
    variant = _one(tuple(row for row in part.variants if row.variant == selection.variant
                         and _path(row.resource_path) == _path(selection.resource_path)), "variant/resource_path")
    if variant.provider_status != "found":
        raise CharacterSelectionError(f"Character resource has no provider: path={selection.resource_path}")
    return _one(tuple(row for row in variant.providers
                      if row.logical_name.casefold() == selection.logical_provider.casefold()), "logical_provider")


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CharacterSelectionError(f"Expected character reader object: field={field}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CharacterSelectionError(f"Expected character reader string: field={field}, value={value!r}")
    return value


def parse_character_selection(payload: object) -> CharacterSelection:
    """Validate the portable selection payload used by future CLI and UI callers."""
    value = _object(payload, "selection")
    variant = value.get("variant")
    if variant not in ("characters", "npc"):
        raise CharacterSelectionError(f"Unsupported character variant: value={variant!r}")
    return CharacterSelection(
        _text(value.get("catalog_identity"), "catalog_identity"),
        _text(value.get("character_id"), "character_id"),
        _text(value.get("template_id"), "template_id"),
        _text(value.get("part_name"), "part_name"),
        "characters" if variant == "characters" else "npc",
        _text(value.get("resource_path"), "resource_path"),
        _text(value.get("logical_provider"), "logical_provider"),
    )


def verify_character_probe_binding(
    catalog: CharacterCatalog, selection: CharacterSelection,
    prepared_manifest: Mapping[str, object], source_audit: Mapping[str, object],
) -> CharacterProbeBinding:
    """Connect a catalog choice to the actual GameObject selected by the reader.

Callers must separately check prepared artifact hashes and reader exit status.
This verifies identities only and must not be treated as an export-ready flag.
"""
    provider = resolve_character_selection(catalog, selection)
    if prepared_manifest.get("format") != "EndfieldSingleCharacterInput/1":
        raise CharacterSelectionError("Unsupported character reader input format")
    if source_audit.get("format") != "EndfieldCharacterSourceAudit/1":
        raise CharacterSelectionError("Unsupported character reader source audit format")
    prepared = _object(prepared_manifest.get("root"), "input.root")
    requested = _object(source_audit.get("requested_root"), "audit.requested_root")
    source = _text(prepared.get("source"), "input.root.source")
    resource = _text(prepared.get("resource_path"), "input.root.resource_path")
    if _path(resource) != _path(selection.resource_path):
        raise CharacterSelectionError(f"Prepared prefab differs from selection: expected={selection.resource_path}, actual={resource}")
    if source.casefold() not in {cab.name.casefold() for cab in provider.serialized_cabs}:
        raise CharacterSelectionError(f"Prepared root CAB is absent from selected provider: provider={provider.logical_name}, cab={source}")
    if _text(requested.get("source"), "audit.requested_root.source").casefold() != source.casefold() or \
            _path(_text(requested.get("resource_path"), "audit.requested_root.resource_path")) != _path(resource):
        raise CharacterSelectionError("Reader audit requested root differs from prepared input")
    actual = _object(source_audit.get("actual_root"), "audit.actual_root")
    path_id = actual.get("path_id")
    if actual.get("type") != "GameObject" or type(path_id) is not int or path_id == 0:
        raise CharacterSelectionError(f"Reader root is not a valid GameObject: root={actual}")
    actual_source = _text(actual.get("source"), "audit.actual_root.source")
    containers = source_audit.get("source_containers")
    if not isinstance(containers, list):
        raise CharacterSelectionError("Reader audit is missing source container evidence")
    matches: list[Mapping[str, object]] = []
    for value in containers:
        container = _object(value, "audit.source_containers[]")
        if _path(_text(container.get("path"), "container.path")) == _path(resource):
            matches.append(container)
    container = _one(matches, "source container")
    reference = _object(container.get("reference"), "container.reference")
    if type(reference.get("path_id")) is not int or reference.get("path_id") != path_id:
        raise CharacterSelectionError("Reader container raw PathID differs from its resolved GameObject")
    resolved = _object(reference.get("resolved"), "container.reference.resolved")
    if resolved.get("type") != "GameObject" or type(resolved.get("path_id")) is not int or resolved.get("path_id") != path_id or \
            _text(resolved.get("source"), "container root source").casefold() != actual_source.casefold():
        raise CharacterSelectionError("Reader actual root differs from its exact source container reference")
    return CharacterProbeBinding(selection, actual_source, str(path_id),
                                 _text(actual.get("name"), "audit.actual_root.name"), "source_reference_verified")
