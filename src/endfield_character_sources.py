"""Prepare character resource indexes without extracting any map geometry."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import sqlite3

from endfield_character_cache import CharacterDataCache, CharacterSourceChangedError, source_stamp
from endfield_scene_discovery import observe_helper
from endfield_scene_resources import validate_scan
from endfield_scene_scanner import run_scanner_shards, scan_index_rows, scanner_capacity
from extractor_core.resources import extract_bootstrap_metadata, build_resource_database, write_bundle_scan_index


Progress = Callable[[str, float, str], None]
CheckCancelled = Callable[[], None]


@dataclass(frozen=True)
class CharacterSourceContext:
    game_root: Path
    blender: Path
    cache_root: Path
    output_root: Path
    provider_index: Path
    resource_database: Path
    cab_inventory: Path


def validate_character_storage(game: Path, target: Path) -> None:
    """Reject resolved directory overlap with the read-only installation."""
    source, destination = game.resolve(), target.resolve()
    if destination.is_relative_to(source) or source.is_relative_to(destination) or destination.is_file():
        raise ValueError("Character storage must be a directory separate from the game installation")


def verify_game_source(game: Path, expected: list[dict[str, object]], check_cancelled: CheckCancelled) -> None:
    """Keep the original source generation across all preparation stages."""
    check_cancelled()
    if game_source_stamps(game) != expected:
        raise CharacterSourceChangedError("Game files changed during character preparation; retry after the update finishes")
    check_cancelled()


def game_source_stamps(game: Path) -> list[dict[str, object]]:
    """Include VFS chunks as well as headers so an in-place game update invalidates readiness."""
    vfs = game / "Endfield_Data/StreamingAssets/VFS"
    paths = sorted((*vfs.glob("*/*.blc"), *vfs.glob("*/*.chk")), key=lambda path: str(path).casefold())
    if not paths:
        raise FileNotFoundError(f"Character VFS source groups are absent: {vfs}")
    return [asdict(source_stamp(path)) for path in (game / "Endfield.exe", *paths)]


def prepare_character_sources(
    source: CharacterSourceContext, workspace: Path, scanner: Path | None,
    reusable: CharacterSourceContext | None, check_cancelled: CheckCancelled, progress: Progress,
) -> CharacterSourceContext:
    """Verify reusable common indexes or build fresh character-only indexes from VFS."""
    check_cancelled()
    for target in (source.cache_root, source.output_root, workspace):
        validate_character_storage(source.game_root, target)
    resolved_workspace = workspace.resolve()
    if resolved_workspace == source.cache_root.resolve() or not resolved_workspace.is_relative_to(source.cache_root.resolve()):
        raise ValueError("Character preparation workspace must remain inside its cache root")
    if reusable is not None and reusable.game_root.resolve() != source.game_root.resolve():
        raise ValueError("Reusable character indexes belong to a different game installation")
    before = game_source_stamps(source.game_root)
    check_cancelled()
    workspace.mkdir(parents=True, exist_ok=False)

    def cancelled() -> bool:
        check_cancelled()
        return False

    def metadata_progress(phase: str, fraction: float, message: str, details: dict[str, object] | None) -> None:
        progress("preparing", .05 + .1 * fraction, message)

    check_cancelled()
    vfs = source.game_root / "Endfield_Data/StreamingAssets/VFS"
    metadata = extract_bootstrap_metadata(vfs, workspace / "metadata", progress=metadata_progress, cancelled=cancelled)
    if reusable is not None:
        # The database records the exact game manifest and path table it was built from.
        with sqlite3.connect(reusable.resource_database.as_uri() + "?mode=ro", uri=True) as connection:
            identity = dict(connection.execute("SELECT key, value FROM meta"))
        for field, path in (("manifest_sha256", metadata["manifest"]), ("string_path_hash_sha256", metadata["main_paths"])):
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if str(identity.get(field, "")).casefold() != actual:
                raise ValueError(f"Configured reusable resource index belongs to different game data: field={field}")
        if identity.get("format") != "EndfieldResourceDB/1":
            raise ValueError("Configured reusable resource index has an unsupported schema")
        for path in (reusable.provider_index, reusable.cab_inventory):
            check_cancelled()
            validate_scan(path)
        result = CharacterSourceContext(source.game_root, source.blender, source.cache_root, source.output_root,
                                        reusable.provider_index, reusable.resource_database, reusable.cab_inventory)
        progress("preparing", .65, "Verified reusable resource indexes against the selected game")
    else:
        if scanner is None or not scanner.is_file():
            raise FileNotFoundError("Character initial preparation requires the configured bundle scanner")
        database = workspace / "resource_index.sqlite"
        build_resource_database(vfs, metadata["manifest"], metadata["main_paths"], database,
                                progress=metadata_progress, cancelled=cancelled)
        index = workspace / "bundle_scan_index.tsv"
        write_bundle_scan_index(database, vfs, index)
        count = scan_index_rows(index, check_cancelled)
        scan_work = workspace / "scan"

        def scan_progress(row: dict[str, object]) -> None:
            total, current = int(row["total"]), int(row["current"])
            progress("preparing", .2 + .5 * (current / total if total else 1.), "Scanning character providers and dependencies")

        run_scanner_shards(scanner, index, scan_work, workspace / "scan.log", 0, count,
                           scanner_capacity(count), ("assets/",), check_cancelled, scan_progress, observe_helper)
        result = CharacterSourceContext(source.game_root, source.blender, source.cache_root, source.output_root,
                                        scan_work / "bundle_candidates.jsonl", database,
                                        scan_work / "bundle_cab_inventory.jsonl")
    cache = CharacterDataCache(source.cache_root)
    for path, schema in ((result.provider_index, "character-provider-jsonl/1"),
                         (result.resource_database, "character-resource-database/1"),
                         (result.cab_inventory, "character-cab-jsonl/1")):
        cache.fingerprint_file(path, schema, check_cancelled)
    verify_game_source(source.game_root, before, check_cancelled)
    return result
