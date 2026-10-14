"""Collect exact, buildable third-party source snapshots for release kits."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Sequence


class SourceCollectionError(ValueError):
    """Raised when a source snapshot violates its declared collection boundary."""


SOURCE_SNAPSHOT_FORMAT = "EndfieldPinnedSourceSnapshot/1"
PROVENANCE_FILENAME = "SOURCE-PROVENANCE.json"
FORBIDDEN_NAMES = frozenset(
    {
        "cnkeys.json",
        "gameassembly.dll",
        "global-metadata.dat",
    }
)
FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".vs",
        "bin",
        "obj",
        "bin-flat",
        "obj-flat",
        "fbxnative",
        "fbxwrapper",
    }
)
FORBIDDEN_SUFFIXES = frozenset(
    {
        ".dll",
        ".exe",
        ".lib",
        ".pdb",
        ".obj",
        ".o",
        ".a",
        ".so",
        ".dylib",
        ".fbx",
        ".glsl",
        ".shader",
        ".hlsl",
        ".spv",
    }
)


def _relative_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or not candidate.parts or any(part in {".", ".."} for part in candidate.parts):
        raise SourceCollectionError(f"Source path must be a normalized relative path: {path}.")
    return candidate


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _collect_inputs(source_root: Path, included_paths: Sequence[str]) -> tuple[Path, ...]:
    selected: set[Path] = set()
    for source_relative in included_paths:
        relative = _relative_path(source_relative)
        source_path = source_root / relative
        if not source_path.exists():
            raise SourceCollectionError(f"Required source input is missing: {source_relative}.")
        if source_path.is_symlink():
            raise SourceCollectionError(f"Source inputs cannot be symbolic links: {source_relative}.")
        if source_path.is_file():
            selected.add(relative)
            continue
        if not source_path.is_dir():
            raise SourceCollectionError(f"Source input is not a regular file or directory: {source_relative}.")

        for file_path in source_path.rglob("*"):
            if file_path.is_symlink():
                raise SourceCollectionError(f"Source inputs cannot contain symbolic links: {file_path}.")
            if file_path.is_file():
                selected.add(file_path.relative_to(source_root))

    if not selected:
        raise SourceCollectionError("The source snapshot contains no selected files.")
    return tuple(sorted(selected, key=lambda item: item.as_posix()))


def _validate_source_path(relative_path: Path) -> None:
    parts = {part.lower() for part in relative_path.parts}
    if parts & FORBIDDEN_PARTS:
        raise SourceCollectionError(f"Build output or excluded SDK source is not allowed: {relative_path}.")
    if relative_path.name.lower() in FORBIDDEN_NAMES:
        raise SourceCollectionError(f"Private game or key material is not allowed: {relative_path.name}.")
    if relative_path.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise SourceCollectionError(f"Compiled, game, or shader payload is not allowed: {relative_path}.")


def _is_generated_path(relative_path: Path) -> bool:
    generated_parts = {"bin", "obj", "bin-flat", "obj-flat"}
    return any(part.lower() in generated_parts for part in relative_path.parts)


def collect_source_snapshot(
    source_root: Path,
    destination_root: Path,
    repository_root: Path,
    repository: str,
    commit: str,
    license_name: str,
    included_paths: Sequence[str],
    applied_patch_paths: Sequence[str],
    removed_paths: Sequence[str],
) -> Path:
    """Copy an allowlisted source closure and record exact files and patch inputs."""

    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    repository_root = repository_root.resolve()
    if not source_root.is_dir():
        raise SourceCollectionError(f"Pinned source root does not exist: {source_root}.")
    if not repository_root.is_dir():
        raise SourceCollectionError(f"Repository root does not exist: {repository_root}.")
    if destination_root.exists():
        raise SourceCollectionError(f"Source snapshot destination already exists: {destination_root}.")
    if source_root == destination_root or destination_root in source_root.parents:
        raise SourceCollectionError("The source snapshot destination cannot be inside its source tree.")
    if not repository or not commit or not license_name:
        raise SourceCollectionError("Repository, commit, and license identity must all be supplied.")

    source_files = _collect_inputs(source_root, included_paths)
    for relative_path in source_files:
        _validate_source_path(relative_path)

    patch_rows: list[dict[str, str]] = []
    for patch_path in sorted(set(applied_patch_paths)):
        relative_path = _relative_path(patch_path)
        absolute_path = repository_root / relative_path
        if not absolute_path.is_file() or absolute_path.is_symlink():
            raise SourceCollectionError(f"Applied patch input is missing or unsafe: {patch_path}.")
        patch_rows.append({"path": relative_path.as_posix(), "sha256": _sha256(absolute_path)})

    destination_root.mkdir(parents=True, exist_ok=False)
    file_rows: dict[str, str] = {}
    try:
        for relative_path in source_files:
            source_path = source_root / relative_path
            destination_path = destination_root / relative_path
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, destination_path)
            file_rows[relative_path.as_posix()] = _sha256(destination_path)

        manifest = {
            "format": SOURCE_SNAPSHOT_FORMAT,
            "repository": repository,
            "commit": commit,
            "license": license_name,
            "files": file_rows,
            "appliedPatches": patch_rows,
            "removedPaths": sorted({_relative_path(path).as_posix() for path in removed_paths}),
        }
        manifest_path = destination_root / PROVENANCE_FILENAME
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise SourceCollectionError(f"Could not write the source snapshot at {destination_root}.") from error

    verify_source_snapshot(destination_root, repository_root, repository, commit, license_name)
    return manifest_path


def _read_manifest(snapshot_root: Path) -> dict[str, object]:
    manifest_path = snapshot_root / PROVENANCE_FILENAME
    if not manifest_path.is_file():
        raise SourceCollectionError(f"Source provenance manifest is missing: {manifest_path}.")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SourceCollectionError("Source provenance manifest is not valid JSON.") from error
    if not isinstance(value, dict):
        raise SourceCollectionError("Source provenance manifest must be a JSON object.")
    return value


def verify_source_snapshot(
    snapshot_root: Path,
    repository_root: Path,
    expected_repository: str,
    expected_commit: str,
    expected_license: str,
) -> None:
    """Require exact file inventory, file hashes, and patch-source hashes."""

    snapshot_root = snapshot_root.resolve()
    repository_root = repository_root.resolve()
    manifest = _read_manifest(snapshot_root)
    expected_identity = {
        "format": SOURCE_SNAPSHOT_FORMAT,
        "repository": expected_repository,
        "commit": expected_commit,
        "license": expected_license,
    }
    if any(manifest.get(key) != value for key, value in expected_identity.items()):
        raise SourceCollectionError("Source provenance identity differs from the requested pinned dependency.")

    file_rows = manifest.get("files")
    patch_rows = manifest.get("appliedPatches")
    removed_rows = manifest.get("removedPaths")
    if not isinstance(file_rows, dict) or not isinstance(patch_rows, list) or not isinstance(removed_rows, list):
        raise SourceCollectionError("Source provenance manifest is missing a valid inventory or patch list.")
    if not all(isinstance(path, str) and isinstance(digest, str) for path, digest in file_rows.items()):
        raise SourceCollectionError("Source file inventory entries must map relative paths to SHA-256 values.")
    if not all(
        isinstance(row, dict)
        and isinstance(row.get("path"), str)
        and isinstance(row.get("sha256"), str)
        for row in patch_rows
    ):
        raise SourceCollectionError("Applied patch entries must contain a relative path and SHA-256 value.")
    if not all(isinstance(path, str) for path in removed_rows):
        raise SourceCollectionError("Removed path entries must be relative path strings.")

    expected_paths = {_relative_path(path).as_posix() for path in file_rows}
    actual_paths = set()
    for path in snapshot_root.rglob("*"):
        relative_path = path.relative_to(snapshot_root)
        if _is_generated_path(relative_path):
            continue
        if path.is_symlink():
            raise SourceCollectionError(f"Source snapshots cannot contain symbolic links: {relative_path}.")
        if path.is_file() and path.name != PROVENANCE_FILENAME:
            actual_paths.add(relative_path.as_posix())
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        raise SourceCollectionError(
            f"Source snapshot inventory differs; missing={missing[:5]}, unexpected={unexpected[:5]}."
        )
    for relative_path, expected_digest in file_rows.items():
        path = snapshot_root / _relative_path(relative_path)
        _validate_source_path(_relative_path(relative_path))
        if _sha256(path) != expected_digest:
            raise SourceCollectionError(f"Source file hash differs from the manifest: {relative_path}.")

    for row in patch_rows:
        relative_path = _relative_path(str(row["path"]))
        path = repository_root / relative_path
        if not path.is_file() or _sha256(path) != row["sha256"]:
            raise SourceCollectionError(f"Applied patch input hash differs from the manifest: {relative_path}.")
