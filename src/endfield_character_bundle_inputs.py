"""Prepare exact provider closures for independently selected NPCs and Actions."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from endfield_character_cache import CharacterDataCache, CharacterSourceChangedError, character_bundle_closure, source_stamp
from endfield_character_catalog import CharacterResourceProvider
from endfield_character_prepare import CharacterExtractedBundle, CharacterPreparationError, _bundle_files, _fingerprint, _logical_bundle_name
from endfield_character_sources import CharacterSourceContext, validate_character_storage
from extractor_core.resources import extract_candidate_bundles


def prepare_provider_bundles(context: CharacterSourceContext, provider: CharacterResourceProvider,
                             output: Path, check_cancelled: Callable[[], None],
                             progress: Callable[[str, float, str], None]) -> tuple[CharacterExtractedBundle, ...]:
    """Extract one verified provider and require complete, unchanged CAB dependencies."""
    return prepare_providers_bundles(context, (provider,), output, check_cancelled, progress)


def prepare_providers_bundles(context: CharacterSourceContext, providers: tuple[CharacterResourceProvider, ...],
                              output: Path, check_cancelled: Callable[[], None],
                              progress: Callable[[str, float, str], None]) -> tuple[CharacterExtractedBundle, ...]:
    """Extract the union of exact providers without duplicate dependency payloads."""
    check_cancelled()
    validate_character_storage(context.game_root, output)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Provider bundle output already exists: {output}")
    roots = tuple(sorted({_logical_bundle_name(provider.logical_name, "selected provider") for provider in providers}))
    if not roots:
        raise CharacterPreparationError("Provider extraction requires at least one exact source")
    inputs = (context.provider_index, context.resource_database, context.cab_inventory)
    before = tuple(source_stamp(path) for path in inputs)
    cache = CharacterDataCache(context.cache_root)
    index, _ = cache.read_character_cab_index(context.cab_inventory, check_cancelled)
    for provider in providers:
        serialized = dict(index.bundle_serialized).get(provider.logical_name)
        expected = {cab.name.replace("\\", "/").rsplit("/", 1)[-1].casefold() for cab in provider.serialized_cabs}
        if serialized is None or set(serialized) != expected:
            raise CharacterPreparationError("Selected provider CAB identity differs from the current CAB inventory")
    closures, metadata = character_bundle_closure(index, {"selected": list(roots)})
    if metadata["maps"]["selected"]["unresolvedExternalCount"]:
        raise CharacterPreparationError(f"Selected provider has unresolved CAB dependencies: {metadata['maps']['selected']}")
    names = tuple(sorted(_logical_bundle_name(name, "dependency closure") for name in closures["selected"]))
    if not set(roots).issubset(names):
        raise CharacterPreparationError("Selected provider is missing from its CAB closure")

    def cancelled() -> bool:
        check_cancelled()
        return False

    def extraction_progress(stage: str, fraction: float, message: str, details: dict[str, object] | None) -> None:
        progress("extracting", fraction, message)

    result = extract_candidate_bundles(context.resource_database,
        context.game_root / "Endfield_Data/StreamingAssets/VFS", list(names), output,
        progress=extraction_progress, cancelled=cancelled)
    if tuple(source_stamp(path) for path in inputs) != before:
        raise CharacterSourceChangedError("Character provider inputs changed during extraction")
    if result.get("requested_count") != str(len(names)) or result.get("extracted_count") != str(len(names)):
        raise CharacterPreparationError(f"Provider extraction counts are incomplete: {result}")
    files: list[CharacterExtractedBundle] = []
    for name, path in _bundle_files(output, names):
        fingerprint = _fingerprint(path, check_cancelled)
        files.append(CharacterExtractedBundle(name, fingerprint.path, fingerprint.bytes, fingerprint.sha256))
    if result.get("bytes") != str(sum(file.bytes for file in files)):
        raise CharacterPreparationError("Provider extracted-byte count changed before verification")
    check_cancelled()
    return tuple(files)
