"""Extract one catalog-selected character's exact source bundle closure."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from endfield_character_cache import (
    CharacterDataCache,
    CharacterSourceChangedError,
    CharacterSourceFingerprint,
    character_bundle_closure,
    source_stamp,
)
from endfield_character_catalog import (
    CharacterSerializedCab,
    read_character_catalog_cached,
)
from endfield_character_selection import CharacterSelection, resolve_character_selection
from extractor_core.resources import extract_candidate_bundles

PreparationStage = Literal[
    "reading_catalog", "resolving_selection", "hashing_inputs", "resolving_closure",
    "validating_closure", "extracting_bundles", "hashing_bundles", "writing_receipt", "completed",
]
ProgressScalar = str | int | float | bool
ProgressDetails = tuple[tuple[str, ProgressScalar], ...]


class CharacterPreparationError(RuntimeError):
    """The selected source bundle set cannot be prepared with verified inputs."""


@dataclass(frozen=True)
class CharacterBundlePreparationRequest:
    selection: CharacterSelection
    game_root: Path
    provider_index: Path
    resource_database: Path
    cab_inventory: Path
    output_dir: Path


@dataclass(frozen=True)
class CharacterPreparationProgress:
    stage: PreparationStage
    fraction: float
    message: str
    details: ProgressDetails


@dataclass(frozen=True)
class CharacterFileFingerprint:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class CharacterExtractedBundle:
    logical_name: str
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class CharacterBundleSetReceipt:
    format: Literal["EndfieldCharacterBundleSet/1"]
    status: Literal["bundles_extracted_requires_reader"]
    game_root: str
    selection: CharacterSelection
    catalog_identity: str
    provider_logical_name: str
    provider_serialized_cabs: tuple[CharacterSerializedCab, ...]
    provider_index: CharacterFileFingerprint
    cab_inventory: CharacterFileFingerprint
    resource_database: CharacterFileFingerprint
    root_bundles: tuple[str, ...]
    closure_bundles: tuple[str, ...]
    unresolved_external_count: int
    extracted_bundles: tuple[CharacterExtractedBundle, ...]
    receipt_path: str


Progress = Callable[[CharacterPreparationProgress], None]
CheckCancelled = Callable[[], None]


def _emit(progress: Progress, stage: PreparationStage, fraction: float, message: str, details: ProgressDetails) -> None:
    progress(CharacterPreparationProgress(stage, fraction, message, details))


def _logical_bundle_name(value: str, field: str) -> str:
    parts = value.split("/")
    windows_path = PureWindowsPath(value)
    if (not value or "\\" in value or "\x00" in value or ":" in value or windows_path.drive or windows_path.root
            or PurePosixPath(value).is_absolute() or len(parts) != 2 or any(part in ("", ".", "..") for part in parts)):
        raise CharacterPreparationError(f"Unsafe logical bundle path: field={field}, path={value!r}")
    return value


def _input_paths(request: CharacterBundlePreparationRequest) -> tuple[Path, Path, Path, Path]:
    game, provider, database, cab = (
        path.expanduser().resolve()
        for path in (request.game_root, request.provider_index, request.resource_database, request.cab_inventory)
    )
    for label, path in zip(("provider index", "resource database", "CAB inventory"), (provider, database, cab), strict=True):
        if not path.is_file():
            raise FileNotFoundError(f"Character bundle preparation input is missing: {label}={path}")
    return game, provider, database, cab


def _output_path(requested: Path, game: Path, inputs: tuple[Path, ...]) -> Path:
    if requested.expanduser().is_symlink():
        raise CharacterPreparationError(f"Character output directory cannot be a symlink: path={requested}")
    output = requested.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"Character output directory already exists: path={output}")
    blocked = {game, *(path.parent for path in inputs)}
    if any(output == directory or output.is_relative_to(directory) for directory in blocked):
        raise CharacterPreparationError(f"Character output must be outside game and input directories: path={output}")
    return output


def _fingerprint(path: Path, check_cancelled: CheckCancelled) -> CharacterFileFingerprint:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Character preparation source file is missing: path={source}")
    before = source.stat()
    digest = sha256()
    total = 0
    with source.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            check_cancelled()
            digest.update(chunk)
            total += len(chunk)
    after = source.stat()
    stamp_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    stamp_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if stamp_before != stamp_after or total != after.st_size:
        raise CharacterPreparationError(f"Character preparation input changed while hashing: path={source}")
    return CharacterFileFingerprint(str(source), total, digest.hexdigest())


def _receipt_fingerprint(fingerprint: CharacterSourceFingerprint) -> CharacterFileFingerprint:
    return CharacterFileFingerprint(fingerprint.stamp.path, fingerprint.stamp.bytes, fingerprint.sha256)


def _validate_cache_root(cache: CharacterDataCache, game: Path, inputs: tuple[Path, ...]) -> None:
    root = cache.cache_root
    blocked = {game, *(path.parent for path in inputs)}
    if any(root == directory or root.is_relative_to(directory) for directory in blocked):
        raise CharacterPreparationError(
            f"Character cache must be outside game and input directories: cache={root}, blocked={sorted(map(str, blocked))}"
        )


def _progress_details(phase: str, details: dict[str, object] | None) -> ProgressDetails:
    values = {**(details or {}), "helper_phase": phase}
    if any(not isinstance(key, str) or type(value) not in (str, int, float, bool) for key, value in values.items()):
        raise CharacterPreparationError(f"Unsupported structured extraction progress values: details={values}")
    return tuple(sorted(values.items(), key=lambda item: item[0]))


def _bundle_files(output: Path, names: tuple[str, ...]) -> tuple[tuple[str, Path], ...]:
    expected = {name.casefold(): name for name in names}
    if len(expected) != len(names):
        raise CharacterPreparationError("Character bundle closure contains case-insensitive logical-name collisions")
    actual: dict[str, Path] = {}
    for path in output.rglob("*"):
        if path.is_file():
            key = _logical_bundle_name(path.relative_to(output).as_posix(), "extracted bundle").casefold()
            if key not in expected or key in actual:
                raise CharacterPreparationError(f"Unexpected or duplicate extracted character bundle: path={path}")
            actual[key] = path
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise CharacterPreparationError(f"Extracted character bundle set is incomplete: missing={missing}")
    return tuple((expected[key], actual[key]) for key in sorted(expected))


def prepare_character_bundle_set(
    request: CharacterBundlePreparationRequest,
    check_cancelled: CheckCancelled,
    progress: Progress,
) -> CharacterBundleSetReceipt:
    """Use the output-root cache and preserve the original one-request call shape."""
    cache_root = request.output_dir.expanduser().parent / ".endfield_character_cache"
    return prepare_character_bundle_set_cached(
        request, check_cancelled, progress, CharacterDataCache(cache_root),
    )


def prepare_character_bundle_set_cached(
    request: CharacterBundlePreparationRequest,
    check_cancelled: CheckCancelled,
    progress: Progress,
    cache: CharacterDataCache,
) -> CharacterBundleSetReceipt:
    """Extract through a caller-owned persistent cache shared by separate workers."""
    check_cancelled()
    game, provider_path, database_path, cab_path = _input_paths(request)
    output = _output_path(request.output_dir, game, (provider_path, database_path, cab_path))
    _validate_cache_root(cache, game, (provider_path, database_path, cab_path))
    _emit(progress, "reading_catalog", 0.05, "Reading the current character catalog", ())
    catalog = read_character_catalog_cached(game, provider_path, cache, check_cancelled)
    provider_source = catalog.provider_index_source
    if Path(provider_source.path).resolve() != provider_path:
        raise CharacterPreparationError("Character catalog provider source differs from the requested provider index")
    provider_fingerprint = cache.lookup_fingerprint(provider_path, "character-provider-jsonl/1", check_cancelled)
    if provider_fingerprint is None or provider_fingerprint.sha256 != provider_source.sha256:
        raise CharacterPreparationError(f"Character provider source identity is not cached: path={provider_path}")
    check_cancelled()
    _emit(progress, "resolving_selection", 0.15, "Resolving the exact character, variant, and provider", ())
    selected_provider = resolve_character_selection(catalog, request.selection)
    root_name = _logical_bundle_name(selected_provider.logical_name, "selected provider")
    _emit(progress, "hashing_inputs", 0.22, "Checking CAB and resource database fingerprints", ())
    cab_index, cab_source_fingerprint = cache.read_character_cab_index(cab_path, check_cancelled)
    database_source_fingerprint = cache.fingerprint_file(
        database_path, "character-resource-database/1", check_cancelled,
    )
    cab_fingerprint = _receipt_fingerprint(cab_source_fingerprint)
    database_fingerprint = _receipt_fingerprint(database_source_fingerprint)
    check_cancelled()
    _emit(progress, "resolving_closure", 0.32, "Resolving the selected provider's CAB dependency closure", ())
    closures, closure_metadata = character_bundle_closure(cab_index, {"character": [root_name]})
    closure_group = closure_metadata["maps"]["character"]
    unresolved = closure_group["unresolvedExternalCount"]
    if unresolved:
        raise CharacterPreparationError(
            f"Character CAB closure has unresolved externals: count={unresolved}, "
            f"sample={closure_group['unresolvedExternalSample']}"
        )
    closure_values: list[str] = []
    for name in closures["character"]:
        check_cancelled()
        closure_values.append(_logical_bundle_name(name, "dependency closure"))
    closure_names = tuple(sorted(closure_values))
    if root_name not in closure_names:
        raise CharacterPreparationError(f"Selected provider is absent from its closure: provider={root_name}")
    _emit(progress, "validating_closure", 0.35, "Validated logical bundle paths", (("bundle_count", len(closure_names)),))

    def extraction_progress(phase: str, fraction: float, message: str, details: dict[str, object] | None) -> None:
        if not 0.0 <= fraction <= 1.0:
            raise CharacterPreparationError(f"Invalid bundle extraction progress fraction: value={fraction}")
        _emit(progress, "extracting_bundles", 0.36 + fraction * 0.48, message, _progress_details(phase, details))

    def extraction_cancelled() -> bool:
        check_cancelled()
        return False

    extraction = extract_candidate_bundles(
        database_path, game / "Endfield_Data" / "StreamingAssets" / "VFS", list(closure_names), output,
        progress=extraction_progress, cancelled=extraction_cancelled,
    )
    for fingerprint in (provider_fingerprint, cab_source_fingerprint, database_source_fingerprint):
        if source_stamp(Path(fingerprint.stamp.path)) != fingerprint.stamp:
            raise CharacterSourceChangedError(
                f"Character preparation input changed during bundle extraction: path={fingerprint.stamp.path}"
            )
    if (extraction.get("format") != "EndfieldCandidateBundleSet/1"
            or extraction.get("requested_count") != str(len(closure_names))
            or extraction.get("extracted_count") != str(len(closure_names))):
        raise CharacterPreparationError(f"Bundle extractor returned incomplete counts: result={extraction}")
    extracted: list[CharacterExtractedBundle] = []
    total_bytes = 0
    files = _bundle_files(output, closure_names)
    for index, (logical_name, path) in enumerate(files, start=1):
        fingerprint = _fingerprint(path, check_cancelled)
        total_bytes += fingerprint.bytes
        extracted.append(CharacterExtractedBundle(logical_name, fingerprint.path, fingerprint.bytes, fingerprint.sha256))
        _emit(progress, "hashing_bundles", 0.84 + 0.14 * index / len(files),
              f"Hashed extracted character bundle {index}/{len(files)}",
              (("logical_name", logical_name), ("bytes", fingerprint.bytes)))
    if extraction.get("bytes") != str(total_bytes):
        raise CharacterPreparationError(
            f"Extracted bundle byte count changed before receipt: reported={extraction.get('bytes')}, actual={total_bytes}"
        )
    receipt_path = output / "character_bundle_set_receipt.json"
    receipt = CharacterBundleSetReceipt(
        "EndfieldCharacterBundleSet/1", "bundles_extracted_requires_reader", str(game), request.selection,
        catalog.source_identity, selected_provider.logical_name, selected_provider.serialized_cabs,
        CharacterFileFingerprint(provider_source.path, provider_source.bytes, provider_source.sha256),
        cab_fingerprint, database_fingerprint, (root_name,), closure_names, unresolved, tuple(extracted), str(receipt_path),
    )
    check_cancelled()
    _emit(progress, "writing_receipt", 0.99, "Writing the bundle preparation receipt", ())
    with receipt_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(asdict(receipt), ensure_ascii=False, indent=2) + "\n")
    _emit(progress, "completed", 1.0, "Bundles extracted; character reader validation is still required", ())
    return receipt
