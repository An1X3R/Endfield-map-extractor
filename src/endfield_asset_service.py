"""Expose scanned supplemental assets through the installed character runtime."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import threading
from typing import cast

from endfield_character_cache import source_stamp
from endfield_character_service import CharacterService
from endfield_character_sources import CharacterSourceContext, game_source_stamps, validate_character_storage
from endfield_display_props import (DisplayPropCatalog, public_display_prop_catalog,
    read_display_prop_catalog, select_display_props)
from endfield_display_prop_export import (DisplayPropTools, DisplayPropLimits,
    DisplayPropPreparationRequest, DisplayPropExportRequest, export_display_prop)
from endfield_illustrations import (IllustrationCatalog, read_illustration_catalog,
    public_illustration_catalog, select_illustrations, export_illustrations)
from endfield_weapon_catalog import WeaponCatalog, read_weapon_catalog
from endfield_weapon_prepare import (WeaponPrepareTools, WeaponPreparationRequest,
    WeaponVariant, resolve_weapon_source)
from endfield_weapon_export import WeaponExportRequest, export_weapon
from endfield_scene_discovery import tool_identity
from webui_asset_contract import ASSET_KINDS, AssetKind, AssetExportRequest, parse_asset_export_request
from webui_character_contract import json_object, required_text


Catalog = WeaponCatalog | IllustrationCatalog | DisplayPropCatalog
Progress = Callable[[str, float, str], None]
CheckCancelled = Callable[[], None]


def _check() -> None:
    """Synchronous discovery has no job cancellation token."""


def _progress(stage: str, fraction: float, message: str) -> None:
    """Catalog discovery does not publish export progress."""


def _weapon_choices(catalog: WeaponCatalog) -> dict[str, tuple[str, WeaponVariant, str]]:
    choices: dict[str, tuple[str, WeaponVariant, str]] = {}
    for entry in catalog.entries:
        variants: tuple[tuple[WeaponVariant, tuple[str, ...]], ...] = (
            ("base", tuple(row.resource_path for row in entry.resources)),
            ("refined_candidate", tuple(row.resource.resource_path for row in entry.refined_candidates)))
        for variant, resources in variants:
            for resource in resources:
                selector = sha256(json.dumps([entry.weapon_id, variant, resource]).encode()).hexdigest()
                choices[selector] = (entry.weapon_id, variant, resource)
    return choices


def _identity(catalog: Catalog) -> str:
    return catalog.source_identity if isinstance(catalog, WeaponCatalog) else catalog.catalog_id


class AssetService:
    """Connect source catalogs and exporters without accepting client executable paths."""

    def __init__(self, character: CharacterService) -> None:
        self.character = character
        self._lock = threading.RLock()
        self._catalogs: dict[tuple[str, str], Catalog] = {}

    def output_root(self) -> Path:
        return self.character.prepared_source_context().output_root

    def game_root(self) -> Path:
        return self.character.prepared_source_context().game_root

    def _settings(self) -> tuple[dict[str, object], Path]:
        config, parent = self.character._require_config()
        return json_object(config.get("export_defaults"), "export_defaults"), parent

    def _tool(self, name: str) -> Path:
        settings, parent = self._settings()
        path = (parent / required_text(settings.get(name), name)).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Asset reader is missing: field={name}; path={path}")
        return path

    def _version(self) -> str:
        settings, _ = self._settings()
        return required_text(settings.get("unity_version"), "unity_version")

    def _catalog(self, kind: AssetKind, context: CharacterSourceContext,
                 check_cancelled: CheckCancelled, progress: Progress) -> Catalog:
        stamps = [asdict(source_stamp(path)) for path in (
            context.provider_index, context.resource_database, context.cab_inventory)]
        reader = (tool_identity(self._tool("potential_json_reader")), self._version()) if kind == "display_prop" else None
        key = (kind, json.dumps([str(context.game_root), game_source_stamps(context.game_root), stamps, reader], sort_keys=True))
        with self._lock:
            if key in self._catalogs:
                return self._catalogs[key]
            if kind == "weapon":
                catalog: Catalog = read_weapon_catalog(context.game_root, context.provider_index,
                                                       context.resource_database, check_cancelled)
            elif kind == "illustration":
                catalog = read_illustration_catalog(context, check_cancelled)
            else:
                catalog = read_display_prop_catalog(context, self._tool("potential_json_reader"),
                                                    self._version(), check_cancelled, progress)
            self._catalogs = {**{k: v for k, v in self._catalogs.items() if k[0] != kind}, key: catalog}
            return catalog

    def catalog(self, kind: str) -> dict[str, object]:
        if kind not in ASSET_KINDS:
            raise ValueError(f"Unsupported asset catalog: {kind!r}")
        context = self.character.prepared_source_context()
        catalog = self._catalog(cast(AssetKind, kind), context, _check, _progress)
        if isinstance(catalog, IllustrationCatalog):
            return {**public_illustration_catalog(catalog), "kind": kind}
        if isinstance(catalog, DisplayPropCatalog):
            return {**public_display_prop_catalog(catalog), "kind": kind}
        choices = _weapon_choices(catalog)
        return {"format": catalog.format, "kind": kind, "catalog_id": catalog.source_identity,
            "summary": asdict(catalog.summary), "entries": [
                {"weapon_id": entry.weapon_id, "name": entry.name, "name_status": entry.name_status,
                 "rarity": entry.rarity, "weapon_type": entry.weapon_type, "model_status": entry.model_status,
                 "variants": [{"item_id": item_id, "variant": variant, "resource_path": resource,
                    "semantic_status": "source_base" if variant == "base" else "unverified_candidate"}
                    for item_id, (weapon_id, variant, resource) in choices.items() if weapon_id == entry.weapon_id]}
                for entry in catalog.entries]}

    def _selected(self, request: AssetExportRequest, context: CharacterSourceContext,
                  check_cancelled: CheckCancelled, progress: Progress) -> Catalog:
        catalog = self._catalog(request.kind, context, check_cancelled, progress)
        if _identity(catalog) != request.catalog_id:
            raise ValueError("Asset catalog changed; refresh the list before exporting")
        if isinstance(catalog, IllustrationCatalog):
            select_illustrations(catalog, request.item_ids)
        elif isinstance(catalog, DisplayPropCatalog):
            select_display_props(catalog, request.item_ids)
        else:
            choices = _weapon_choices(catalog)
            for selector in request.item_ids:
                if selector not in choices:
                    raise ValueError(f"Unknown weapon item_id: {selector}")
                weapon_id, variant, resource = choices[selector]
                if variant != request.variant:
                    raise ValueError(f"Weapon item_id belongs to a different variant: {selector}")
                resolve_weapon_source(catalog, weapon_id, variant, resource)
        return catalog

    def validate(self, payload: object) -> dict[str, object]:
        request = parse_asset_export_request(payload)
        context = self.character.prepared_source_context()
        self._selected(request, context, _check, _progress)
        if request.kind == "illustration":
            self._tool("potential_scene_probe")
        else:
            for name in ("static_reader", "shader_reader", "visibility_reader"):
                self._tool(name)
            if not context.blender.is_file():
                raise FileNotFoundError(f"Blender executable is missing: {context.blender}")
        return {"format": "AssetRequestValidation/1", "status": "valid",
                "canonical_request": request.as_json(), "issues": []}

    def export(self, payload: object, output: Path, check_cancelled: CheckCancelled,
               progress: Progress) -> dict[str, object]:
        request = parse_asset_export_request(payload)
        context = self.character.prepared_source_context()
        validate_character_storage(context.game_root, output)
        if output.exists() and (not output.is_dir() or next(output.iterdir(), None) is not None):
            raise FileExistsError(f"Asset output must be new or empty: {output}")
        catalog = self._selected(request, context, check_cancelled, progress)
        output.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, object]] = []
        if isinstance(catalog, IllustrationCatalog):
            result = export_illustrations(context, catalog, request.item_ids, self._tool("potential_scene_probe"),
                                          output / "images", check_cancelled, progress)
            results.append(asdict(result))
        else:
            catalog_path = output / "source_catalog.json"
            catalog_path.write_text(json.dumps(asdict(catalog), ensure_ascii=False, indent=2), encoding="utf-8")
            for index, selector in enumerate(request.item_ids):
                check_cancelled()
                item = output / selector
                def item_progress(stage: str, fraction: float, message: str) -> None:
                    progress(stage, (index + fraction) / len(request.item_ids), message)
                if isinstance(catalog, WeaponCatalog):
                    weapon_id, variant, resource = _weapon_choices(catalog)[selector]
                    preparation = WeaponPreparationRequest(context.game_root, context.provider_index,
                        context.resource_database, context.cab_inventory, context.cache_root, catalog_path,
                        weapon_id, variant, resource, item / "source", self._version(), 256, 2 * 1024**3,
                        WeaponPrepareTools(self._tool("static_reader"), self._tool("shader_reader"),
                                           self._tool("visibility_reader")))
                    results.append(asdict(export_weapon(WeaponExportRequest(preparation, context.blender,
                        item / "build"), check_cancelled, item_progress)))
                else:
                    prop = DisplayPropPreparationRequest(context, catalog, selector,
                        DisplayPropTools(self._tool("potential_json_reader"), self._tool("static_reader"),
                            self._tool("shader_reader"), self._tool("visibility_reader")), self._version(),
                        DisplayPropLimits(256, 2 * 1024**3), item / "source")
                    results.append(asdict(export_display_prop(DisplayPropExportRequest(prop, item / "build"),
                                                              check_cancelled, item_progress)))
        result_document = {"format": "AssetExportResult/1", "kind": request.kind,
                "status": "partial" if any(row.get("status") == "partial" for row in results) else "completed",
                "catalog_id": request.catalog_id, "output_directory": str(output.resolve()),
                "request": request.as_json(), "items": results}
        return json_object(json.loads(json.dumps(result_document)), "asset export result")
