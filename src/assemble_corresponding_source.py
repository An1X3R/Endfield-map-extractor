"""Assemble a source-only archive with verified pinned upstream snapshots."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import zipfile

if __package__:
    from .release_source_collection import SourceCollectionError, verify_source_snapshot
else:
    from release_source_collection import SourceCollectionError, verify_source_snapshot


SOURCE_KIT_FORMAT = "EndfieldCorrespondingSourceKit/1"
SOURCE_ROOTS = frozenset({"docs", "dotnet", "patches", "schemas", "src", "tests", "third_party", "webui"})
ROOT_SOURCE_NAMES = frozenset(
    {
        ".gitignore",
        "LICENSE",
        "README.md",
        "CHANGELOG.md",
        "bootstrap_environment.py",
        "global.json",
        "launch_extractor.ps1",
        "launch_webui.py",
        "pyproject.toml",
        "requirements.txt",
        "setup_development_environment.ps1",
        "setup_release_environment.ps1",
    }
)
SOURCE_SUFFIXES = frozenset(
    {
        ".bat",
        ".c",
        ".cc",
        ".cmd",
        ".cmake",
        ".config",
        ".cpp",
        ".cs",
        ".csproj",
        ".css",
        ".cjs",
        ".def",
        ".editorconfig",
        ".h",
        ".hpp",
        ".html",
        ".inc",
        ".in",
        ".inl",
        ".js",
        ".json",
        ".lock",
        ".md",
        ".mjs",
        ".patch",
        ".props",
        ".ps1",
        ".py",
        ".resx",
        ".sh",
        ".svg",
        ".targets",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".xaml",
        ".yaml",
        ".yml",
    }
)
GENERATED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".playwright-cli",
        ".pytest_cache",
        ".ruff_cache",
        ".tools",
        ".venv",
        ".vs",
        ".vite",
        ".vitest",
        "__pycache__",
        "bin",
        "bin-flat",
        "build",
        "coverage",
        "dist",
        "logs",
        "node_modules",
        "obj",
        "obj-flat",
        "playwright-report",
        "test-results",
        "venv",
    }
)
FORBIDDEN_PARTS = frozenset({"animeStudio.FBXWrapper", "FBXNative", "FBXWrapper"})
FORBIDDEN_NAMES = frozenset({"cnkeys.json", "gameassembly.dll", "global-metadata.dat"})
FORBIDDEN_SUFFIXES = frozenset(
    {
        ".a",
        ".bin",
        ".db",
        ".dds",
        ".dylib",
        ".exe",
        ".fbx",
        ".glb",
        ".glsl",
        ".hlsl",
        ".jpeg",
        ".jpg",
        ".ktx",
        ".lib",
        ".o",
        ".obj",
        ".pdb",
        ".png",
        ".shader",
        ".so",
        ".spv",
        ".sqlite",
    }
)
SNAPSHOT_IDENTITIES = (
    ("scene", "https://github.com/Escartem/AnimeStudio.git", "7cfb26b9c158f6150c887f9157de97a3fa7672e1", "MIT"),
    ("character", "https://github.com/Variante/AnimeStudio.git", "5bb936ca1d3662fe9b956ec669ab7ebea08bfd07", "MIT"),
    ("ruri", "https://github.com/FractalTools/Ruri.RipperHook.git", "ed62f85af4e5da83dabd514b29ee93ef7d34e86b", "AGPL-3.0"),
)


class SourceArchiveError(ValueError):
    """Raised when a source archive would not match its safe, buildable inputs."""


@dataclass(frozen=True)
class SnapshotRecord:
    name: str
    repository: str
    commit: str
    license: str
    manifestSha256: str
    sourceFiles: int
    sourceBytes: int


@dataclass(frozen=True)
class InclusionException:
    path: str
    item: str
    disposition: str


APPROVED_INCLUSION_EXCEPTIONS = (
    InclusionException(
        path="src/extractor_core/vfs.py",
        item="Existing default VFS decryption-key constant",
        disposition=(
            "Retained by explicit release approval for compatibility; the constant value is not duplicated in this manifest."
        ),
    ),
)


@dataclass(frozen=True)
class SourceArchiveReport:
    status: str
    archive: str
    bytes: int
    sha256: str
    sourceManifestSha256: str
    firstPartyFiles: int
    archiveFiles: int
    snapshots: tuple[SnapshotRecord, ...]


def is_source_package_path(path: PurePosixPath) -> bool:
    """Select first-party source, build metadata, and license material paths."""

    parts = tuple(part.casefold() for part in path.parts)
    if not parts or path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        return False
    if set(parts) & {part.casefold() for part in GENERATED_PARTS}:
        return False
    if set(parts) & {part.casefold() for part in FORBIDDEN_PARTS}:
        return False
    if path.name.casefold() in FORBIDDEN_NAMES or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        return False
    if parts in {("webui", "license"), ("dotnet", "charactertools", "thirdparty", "ruri", "license")}:
        return True
    if parts[0] in SOURCE_ROOTS:
        if parts[0] == "third_party" and len(parts) >= 2 and parts[1] == "sources":
            return False
        if parts[0] == "third_party" and len(parts) >= 2 and parts[1] == "licenses":
            return True
        return path.suffix.casefold() in SOURCE_SUFFIXES or path.name.casefold() in {
            ".editorconfig",
            ".gitattributes",
            ".gitignore",
        }
    return len(parts) == 1 and path.name in ROOT_SOURCE_NAMES


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_output(repository_root: Path, arguments: tuple[str, ...]) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository_root), *arguments),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        raise SourceArchiveError(
            f"GIT_SOURCE_INVENTORY_FAILED: exit={result.returncode}; arguments={arguments}; stderr={diagnostic}"
        )
    return result.stdout.decode("utf-8", errors="strict")


def _repository_source_files(repository_root: Path) -> dict[str, Path]:
    names = _git_output(
        repository_root,
        ("ls-files", "-z", "--cached", "--others", "--exclude-standard"),
    ).split("\0")
    selected: dict[str, Path] = {}
    for name in names:
        if not name:
            continue
        relative = PurePosixPath(name.replace("\\", "/"))
        if not is_source_package_path(relative):
            continue
        source = repository_root.joinpath(*relative.parts)
        if source.is_symlink():
            raise SourceArchiveError(f"SOURCE_INPUT_SYMLINK_FORBIDDEN: {relative.as_posix()}")
        if not source.is_file():
            raise SourceArchiveError(f"SOURCE_INPUT_MISSING: {relative.as_posix()}")
        selected[relative.as_posix()] = source
    if not selected:
        raise SourceArchiveError("SOURCE_INPUTS_EMPTY: no first-party source files matched the source-kit policy.")
    required = {
        "src/extractor_core/vfs.py",
        "third_party/NuGet.Config",
        "third_party/SOURCE-BUILD.md",
        "third_party/THIRD-PARTY-NOTICES.md",
    }
    missing = sorted(required - set(selected))
    if missing:
        raise SourceArchiveError(f"SOURCE_REQUIRED_BUILD_OR_LICENSE_INPUT_MISSING: {missing}")
    if not any(name.startswith("third_party/licenses/") for name in selected):
        raise SourceArchiveError("SOURCE_LICENSE_TREE_MISSING: include the complete third_party/licenses tree.")
    return selected


def _snapshot_files(
    repository_root: Path,
    snapshot_root: Path,
    name: str,
    repository: str,
    commit: str,
    license_name: str,
) -> tuple[dict[str, Path], SnapshotRecord]:
    try:
        verify_source_snapshot(snapshot_root, repository_root, repository, commit, license_name)
    except SourceCollectionError as error:
        raise SourceArchiveError(f"SOURCE_SNAPSHOT_INVALID: {name}: {error}") from error
    manifest_path = snapshot_root / "SOURCE-PROVENANCE.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    source_rows = manifest.get("files")
    if not isinstance(source_rows, dict):
        raise SourceArchiveError(f"SOURCE_SNAPSHOT_MANIFEST_INVALID: {name}")
    files: dict[str, Path] = {}
    for path in snapshot_root.rglob("*"):
        relative = path.relative_to(snapshot_root)
        if any(part.casefold() in GENERATED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise SourceArchiveError(f"SOURCE_SNAPSHOT_SYMLINK_FORBIDDEN: {name}/{relative.as_posix()}")
        if path.is_file():
            archive_path = (PurePosixPath("third_party") / "sources" / name / relative.as_posix()).as_posix()
            if archive_path in files:
                raise SourceArchiveError(f"SOURCE_ARCHIVE_PATH_COLLISION: {archive_path}")
            files[archive_path] = path
    expected_files = len(source_rows) + 1
    if len(files) != expected_files:
        raise SourceArchiveError(
            f"SOURCE_SNAPSHOT_PHYSICAL_INVENTORY_MISMATCH: {name}; expected={expected_files}; actual={len(files)}"
        )
    record = SnapshotRecord(
        name=name,
        repository=repository,
        commit=commit,
        license=license_name,
        manifestSha256=hashlib.sha256(manifest_bytes).hexdigest(),
        sourceFiles=len(source_rows),
        sourceBytes=sum(path.stat().st_size for path in files.values()),
    )
    return files, record


def _archive_manifest(
    repository_root: Path,
    source_files: dict[str, Path],
    snapshot_files: dict[str, Path],
    snapshot_records: tuple[SnapshotRecord, ...],
) -> tuple[bytes, dict[str, str]]:
    all_files = {**source_files, **snapshot_files}
    if len(all_files) != len(source_files) + len(snapshot_files):
        raise SourceArchiveError("SOURCE_ARCHIVE_PATH_COLLISION: first-party files overlap pinned snapshots.")
    digests = {name: _sha256_file(path) for name, path in sorted(all_files.items())}
    commit = _git_output(repository_root, ("rev-parse", "HEAD")).strip()
    manifest = {
        "format": SOURCE_KIT_FORMAT,
        "gitCommit": commit,
        "workingTreeIncluded": True,
        "files": digests,
        "snapshots": [asdict(record) for record in snapshot_records],
        "inclusionExceptions": [asdict(record) for record in APPROVED_INCLUSION_EXCEPTIONS],
        "exclusions": [
            "Generated build, cache, and browser-test-output directories.",
            "Compiled binaries and package caches.",
            "Game or shader payloads and CNKeys material.",
            "FBXNative, FBXWrapper, and Autodesk SDK content.",
        ],
    }
    return (json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"), digests


def _write_archive_entry(archive: zipfile.ZipFile, name: str, path: Path, expected_digest: str) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    digest = hashlib.sha256()
    with path.open("rb") as source, archive.open(info, "w") as target:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            target.write(block)
    if digest.hexdigest() != expected_digest:
        raise SourceArchiveError(f"SOURCE_INPUT_CHANGED_DURING_ARCHIVE: {name}")


def assemble_corresponding_source(
    repository_root: Path,
    snapshot_root: Path,
    output_path: Path,
) -> SourceArchiveReport:
    """Create a deterministic source archive and verify every included file."""

    repository_root = repository_root.resolve(strict=True)
    snapshot_root = snapshot_root.resolve(strict=True)
    output_path = output_path.resolve()
    if not repository_root.is_dir() or not snapshot_root.is_dir():
        raise SourceArchiveError("SOURCE_ARCHIVE_ROOT_INVALID: repository and snapshot roots must be directories.")
    if output_path.exists():
        raise SourceArchiveError(f"SOURCE_ARCHIVE_OUTPUT_EXISTS: choose a new output path: {output_path}")
    if output_path.is_relative_to(repository_root) or output_path.is_relative_to(snapshot_root):
        raise SourceArchiveError("SOURCE_ARCHIVE_OUTPUT_INVALID: output must be outside source inputs.")
    expected_snapshot_names = {row[0] for row in SNAPSHOT_IDENTITIES}
    actual_entries = {path.name for path in snapshot_root.iterdir()}
    actual_snapshot_names = {path.name for path in snapshot_root.iterdir() if path.is_dir()}
    if actual_entries != expected_snapshot_names or actual_snapshot_names != expected_snapshot_names:
        raise SourceArchiveError(
            f"SOURCE_SNAPSHOT_SET_MISMATCH: expected={sorted(expected_snapshot_names)}; actual={sorted(actual_snapshot_names)}"
        )

    source_files = _repository_source_files(repository_root)
    snapshot_files: dict[str, Path] = {}
    records: list[SnapshotRecord] = []
    for name, repository, commit, license_name in SNAPSHOT_IDENTITIES:
        files, record = _snapshot_files(
            repository_root,
            snapshot_root / name,
            name,
            repository,
            commit,
            license_name,
        )
        overlap = set(snapshot_files) & set(files)
        if overlap:
            raise SourceArchiveError(f"SOURCE_ARCHIVE_PATH_COLLISION: {sorted(overlap)[:5]}")
        snapshot_files.update(files)
        records.append(record)
    manifest_bytes, file_digests = _archive_manifest(
        repository_root,
        source_files,
        snapshot_files,
        tuple(records),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(output_path.name + ".partial")
    if temporary_path.exists():
        raise SourceArchiveError(f"SOURCE_ARCHIVE_TEMP_EXISTS: remove or rename {temporary_path}")
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    completed = False
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for name, path in sorted({**source_files, **snapshot_files}.items()):
                _write_archive_entry(archive, name, path, file_digests[name])
            manifest_info = zipfile.ZipInfo("SOURCE-MANIFEST.json", date_time=(1980, 1, 1, 0, 0, 0))
            manifest_info.compress_type = zipfile.ZIP_DEFLATED
            manifest_info.create_system = 3
            manifest_info.external_attr = 0o100644 << 16
            archive.writestr(manifest_info, manifest_bytes, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
        with zipfile.ZipFile(temporary_path) as archive:
            corrupt = archive.testzip()
            if corrupt is not None:
                raise SourceArchiveError(f"SOURCE_ARCHIVE_CRC_FAILED: {corrupt}")
            expected_names = set(file_digests) | {"SOURCE-MANIFEST.json"}
            actual_names = set(archive.namelist())
            if actual_names != expected_names:
                raise SourceArchiveError(
                    f"SOURCE_ARCHIVE_INVENTORY_MISMATCH: missing={sorted(expected_names - actual_names)[:5]}; "
                    f"extra={sorted(actual_names - expected_names)[:5]}"
                )
        temporary_path.replace(output_path)
        completed = True
    finally:
        if not completed and temporary_path.exists() and temporary_path.parent == output_path.parent:
            temporary_path.unlink()

    return SourceArchiveReport(
        status="source_archive_created",
        archive=str(output_path),
        bytes=output_path.stat().st_size,
        sha256=_sha256_file(output_path),
        sourceManifestSha256=manifest_digest,
        firstPartyFiles=len(source_files),
        archiveFiles=len(file_digests) + 1,
        snapshots=tuple(records),
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    report = assemble_corresponding_source(args.repository_root, args.snapshot_root, args.output)
    print(json.dumps(asdict(report), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
