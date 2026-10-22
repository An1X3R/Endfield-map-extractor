"""Discover and export original character illustration textures without Blender.

CharacterPortrait is the source resource family, not a fixed character roster.
Only exact container Texture2D bindings are exported. Sprite-only atlases require
an explicit future assembly path and must never be mistaken for complete art.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import time

from PIL import Image

from endfield_character_bundle_inputs import prepare_providers_bundles
from endfield_character_cache import CharacterDataCache, CharacterSourceChangedError, CharacterSourceStamp, source_stamp
from endfield_character_catalog import CharacterResourceProvider, _cache_provider, _provider_details, read_character_catalog_cached
from endfield_character_sources import CharacterSourceContext, game_source_stamps, validate_character_storage, verify_game_source
from endfield_scene_export import run_scene_process


CheckCancelled = Callable[[], None]
Progress = Callable[[str, float, str], None]
_RESOURCE_PREFIX = "assets/beyond/dynamicassets/gameplay/ui/sprites/characterportrait/"


class IllustrationExportError(ValueError):
    """An illustration cannot be identified or delivered from exact source data."""


@dataclass(frozen=True)
class IllustrationEntry:
    illustration_id: str
    character_id: str | None
    name: str | None
    resource_path: str
    providers: tuple[CharacterResourceProvider, ...]


@dataclass(frozen=True)
class IllustrationCatalog:
    format: str
    catalog_id: str
    entries: tuple[IllustrationEntry, ...]
    index_stamps: tuple[CharacterSourceStamp, ...]
    game_identity: str


@dataclass(frozen=True)
class IllustrationArtifact:
    illustration_id: str
    character_id: str | None
    name: str | None
    resource_path: str
    source: str
    path_id: str
    relative_path: str
    width: int
    height: int
    mode: str
    alpha_extrema: tuple[int, int]
    bytes: int
    sha256: str


@dataclass(frozen=True)
class IllustrationExportResult:
    """Resolve every files[].relative_path against the absolute image_directory."""

    format: str
    status: str
    catalog_id: str
    files: tuple[IllustrationArtifact, ...]
    elapsed_seconds: float
    image_directory: str


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise IllustrationExportError(f"Expected object: {label}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IllustrationExportError(f"Expected nonempty string: {label}")
    return value


def _rows(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise IllustrationExportError(f"Expected array: {label}")
    return [_object(row, label) for row in value]


def _index_paths(context: CharacterSourceContext) -> tuple[Path, ...]:
    return context.resource_database, context.provider_index, context.cab_inventory


def _resource_paths(database: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM meta"))
        if metadata.get("format") != "EndfieldResourceDB/1":
            raise IllustrationExportError(f"Unsupported resource database: {database}")
        rows = connection.execute("SELECT path FROM paths WHERE path_folded LIKE ?", (_RESOURCE_PREFIX + "%",))
        paths = tuple(sorted({path.replace("\\", "/").casefold() for (path,) in rows
                              if PurePosixPath(path).suffix.casefold() == ".png"}))
    if not paths:
        raise IllustrationExportError(f"No CharacterPortrait PNG resources in database: {database}")
    return paths


def read_illustration_catalog(context: CharacterSourceContext, check_cancelled: CheckCancelled) -> IllustrationCatalog:
    """Join discovered resource paths to exact table IDs and verified providers."""
    validate_character_storage(context.game_root, context.cache_root)
    before = tuple(source_stamp(path) for path in _index_paths(context))
    game_before = game_source_stamps(context.game_root)
    cache = CharacterDataCache(context.cache_root)
    characters = read_character_catalog_cached(context.game_root, context.provider_index, cache, check_cancelled)
    names = {entry.character_id.casefold(): entry for entry in characters.characters}

    def build() -> object:
        paths = _resource_paths(context.resource_database)
        providers, _ = _provider_details(context.provider_index, frozenset(paths), check_cancelled)
        return [{"resource_path": path, "providers": [asdict(provider) for provider in providers[path]]} for path in paths]

    raw, _ = cache.get_or_build_json("illustration_catalog", "EndfieldIllustrationPaths/1",
                                   _index_paths(context), (characters.source_identity,), build, check_cancelled)
    entries: list[IllustrationEntry] = []
    for row in _rows(raw, "illustration catalog"):
        path = _text(row.get("resource_path"), "resource_path")
        identity = names.get(PurePosixPath(path).stem)
        providers = tuple(_cache_provider(value, "illustration provider") for value in _rows(row.get("providers"), "providers"))
        entries.append(IllustrationEntry(sha256(path.encode()).hexdigest(), identity.character_id if identity else None,
                                        identity.name if identity else None, path, providers))
    verify_game_source(context.game_root, game_before, check_cancelled)
    if tuple(source_stamp(path) for path in _index_paths(context)) != before:
        raise CharacterSourceChangedError("Illustration indexes changed while reading the catalog")
    game_identity = sha256(json.dumps(game_before, sort_keys=True).encode()).hexdigest()
    identity = sha256(json.dumps({"character_source": characters.source_identity, "game_identity": game_identity,
                                 "indexes": [asdict(stamp) for stamp in before],
                                 "entries": [asdict(entry) for entry in entries]}, sort_keys=True).encode()).hexdigest()
    return IllustrationCatalog("EndfieldIllustrationCatalog/1", identity, tuple(entries), before, game_identity)


def public_illustration_catalog(catalog: IllustrationCatalog) -> dict[str, object]:
    """Return stable selectors without exposing local source paths or CAB details."""
    return {"format": catalog.format, "catalog_id": catalog.catalog_id, "entries": [
        {"illustration_id": entry.illustration_id, "character_id": entry.character_id, "name": entry.name,
         "resource_path": entry.resource_path, "provider_status": "ready" if len(entry.providers) == 1
         else "missing" if not entry.providers else "ambiguous"} for entry in catalog.entries]}


def select_illustrations(catalog: IllustrationCatalog, illustration_ids: tuple[str, ...]) -> tuple[IllustrationEntry, ...]:
    """Reject stale, empty, duplicate or ambiguous selections before extraction."""
    if not illustration_ids or len(set(illustration_ids)) != len(illustration_ids):
        raise IllustrationExportError("Select one or more distinct illustration IDs")
    by_id = {entry.illustration_id: entry for entry in catalog.entries}
    missing = sorted(set(illustration_ids) - by_id.keys())
    if missing:
        raise IllustrationExportError(f"Illustration IDs are absent from the current catalog: {missing}")
    selected = tuple(by_id[key] for key in illustration_ids)
    for entry in selected:
        if len(entry.providers) != 1:
            raise IllustrationExportError(f"Illustration must resolve to exactly one provider: {entry.resource_path}, count={len(entry.providers)}")
    return selected


def _asset_key(value: object) -> tuple[str, int]:
    row = _object(value, "probe asset")
    source = _text(row.get("Source"), "asset.Source")
    path_id = row.get("PathId")
    if type(path_id) is not int or path_id == 0:
        raise IllustrationExportError("Probe asset requires a nonzero integer PathId")
    return source.casefold(), path_id


def _texture_exports(manifest: dict[str, object]) -> dict[tuple[str, int], dict[str, object]]:
    if manifest.get("Format") != "EndfieldSceneProbe/1":
        raise IllustrationExportError(f"Unsupported probe manifest: {manifest.get('Format')!r}")
    textures: dict[tuple[str, int], dict[str, object]] = {}
    for row in _rows(manifest.get("TextureExports"), "TextureExports"):
        key = _asset_key(row.get("Asset"))
        if key in textures:
            raise IllustrationExportError(f"Duplicate exported texture identity: {key}")
        textures[key] = row
    return textures


def _selected_texture(manifest: dict[str, object], entry: IllustrationEntry) -> tuple[str, int]:
    matches: set[tuple[str, int]] = set()
    for row in _rows(manifest.get("Containers"), "Containers"):
        path = _text(row.get("Container"), "Container").replace("\\", "/").casefold()
        asset = _object(row.get("Asset"), "container asset")
        if path == entry.resource_path and asset.get("Type") == "Texture2D":
            matches.add(_asset_key(asset))
    if len(matches) != 1:
        raise IllustrationExportError(f"Expected one original Texture2D binding; Sprite-only/atlas assembly is unsupported: resource={entry.resource_path}, count={len(matches)}")
    return next(iter(matches))


def _filename_component(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:80] or "illustration"


def _artifact(probe: Path, output: Path, entry: IllustrationEntry, key: tuple[str, int],
              row: dict[str, object]) -> IllustrationArtifact:
    if row.get("Error"):
        raise IllustrationExportError(f"Texture decode failed: resource={entry.resource_path}, error={row['Error']}")
    source = (probe / _text(row.get("File"), "texture.File").replace("\\", "/")).resolve()
    if not source.is_relative_to(probe.resolve()) or not source.is_file():
        raise IllustrationExportError(f"Decoded texture escapes or is absent from probe output: {source}")
    with Image.open(source) as image:
        image.load()
        if image.format != "PNG" or image.mode != "RGBA":
            raise IllustrationExportError(f"Expected original RGBA PNG: resource={entry.resource_path}, format={image.format}, mode={image.mode}")
        width, height = image.size
        alpha = image.getchannel("A").getextrema()
        if alpha[1] == 0:
            raise IllustrationExportError(f"Illustration is entirely transparent: {entry.resource_path}")
    label = _filename_component(entry.name or PurePosixPath(entry.resource_path).stem)
    character = _filename_component(entry.character_id or "unmapped")
    filename = f"{label}__{character}__{entry.illustration_id[:12]}.png"
    destination = output / filename
    if destination.resolve().parent != output.resolve():
        raise IllustrationExportError(f"Illustration filename escapes output directory: {filename}")
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
    with destination.open("rb") as stream:
        digest = file_digest(stream, "sha256").hexdigest()
    return IllustrationArtifact(entry.illustration_id, entry.character_id, entry.name, entry.resource_path,
                                key[0], str(key[1]), filename, width, height, "RGBA", alpha,
                                destination.stat().st_size, digest)


def _remove_completed_work(output: Path, work: Path) -> None:
    """Remove only the intermediate directory created by this export."""
    if work.is_symlink() or work.resolve() != output.resolve() / "_work":
        raise IllustrationExportError(f"Intermediate directory escaped the export root: {work}")
    shutil.rmtree(work)


def export_illustrations(context: CharacterSourceContext, catalog: IllustrationCatalog,
                        illustration_ids: tuple[str, ...], probe_executable: Path, output: Path,
                        check_cancelled: CheckCancelled, progress: Progress) -> IllustrationExportResult:
    """Batch exact providers once; retain original pixels, dimensions and alpha."""
    start = time.perf_counter()
    selected = select_illustrations(catalog, illustration_ids)
    if not probe_executable.is_file():
        raise FileNotFoundError(f"Scene texture reader is missing: {probe_executable}")
    validate_character_storage(context.game_root, output)
    validate_character_storage(context.game_root, context.cache_root)
    if tuple(source_stamp(path) for path in _index_paths(context)) != catalog.index_stamps:
        raise CharacterSourceChangedError("Illustration catalog is stale; refresh before export")
    game_before = game_source_stamps(context.game_root)
    if sha256(json.dumps(game_before, sort_keys=True).encode()).hexdigest() != catalog.game_identity:
        raise CharacterSourceChangedError("Game changed after illustration discovery; prepare current indexes again")
    output.mkdir(parents=True, exist_ok=False)
    work = output / "_work"
    work.mkdir()
    providers = tuple({provider.logical_name: provider for entry in selected for provider in entry.providers}.values())
    def extraction_progress(stage: str, fraction: float, message: str) -> None:
        progress(stage, fraction * .45, message)

    prepare_providers_bundles(context, providers, work / "bundles", check_cancelled, extraction_progress)
    progress("decoding", .5, "Decoding original illustration textures")
    probe = work / "probe"
    run_scene_process([str(probe_executable), str(work / "bundles"), str(probe), "--include-unreferenced"],
                      work / "probe.log", os.environ, check_cancelled)
    manifest = _object(json.loads((probe / "scene_manifest.json").read_text(encoding="utf-8-sig")), "probe manifest")
    textures = _texture_exports(manifest)
    files: list[IllustrationArtifact] = []
    for entry in selected:
        check_cancelled()
        key = _selected_texture(manifest, entry)
        if key not in textures:
            raise IllustrationExportError(f"Selected texture was not exported: resource={entry.resource_path}, asset={key}")
        files.append(_artifact(probe, output, entry, key, textures[key]))
    verify_game_source(context.game_root, game_before, check_cancelled)
    if tuple(source_stamp(path) for path in _index_paths(context)) != catalog.index_stamps:
        raise CharacterSourceChangedError("Illustration indexes changed during export")
    shutil.copyfile(work / "probe.log", output / "decode.log")
    _remove_completed_work(output, work)
    result = IllustrationExportResult("EndfieldIllustrationExport/1", "completed", catalog.catalog_id,
                                      tuple(files), time.perf_counter() - start, str(output.resolve()))
    with (output / "illustrations.json").open("x", encoding="utf-8") as stream:
        json.dump(asdict(result), stream, ensure_ascii=False, indent=2)
    progress("completed", 1., "Illustration PNG export completed")
    return result
