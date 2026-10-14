"""Apply and verify the key-free AnimeStudio source overlays used for releases."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


class SourceOverrideError(ValueError):
    """Raised when a pinned source tree does not match the expected overlay shape."""


@dataclass(frozen=True)
class FileDigest:
    """A repository-relative path and the SHA-256 digest of its exact bytes."""

    path: str
    sha256: str


@dataclass(frozen=True)
class SourceOverrideReport:
    """The exact source files and patch inputs changed by the overlay operation."""

    files: tuple[FileDigest, ...]
    applied_patches: tuple[FileDigest, ...]
    removed_paths: tuple[str, ...]


EMBEDDED_SET_KEY = re.compile(
    r'(?m)^(?P<indent>[ \t]*)UnityCN\.SetKey\(\s*"[0-9a-fA-F]{32}"\s*\);[ \t]*\r?$'
)
ANY_EMBEDDED_SET_KEY = re.compile(
    r'(?i)(?:UnityCN\.)?SetKey\(\s*["\'][0-9a-f]{32}["\']'
)
MANAGER_KEY_LITERAL = re.compile(r'(?i)["\'][0-9a-f]{32}["\']')
PROJECT_CNKEYS_ITEM = re.compile(
    r'(?is)[ \t]*<ItemGroup>\s*<Content\s+Include="CNKeys\.json">'
    r'\s*<CopyToOutputDirectory>[^<]*</CopyToOutputDirectory>\s*'
    r'</Content>\s*</ItemGroup>[ \t]*(?:\r?\n)?'
)
OOZ_CONTENT_ITEM = re.compile(
    r'(?m)^(?P<indent>[ \t]*)<Content\s+Include="\.\.[\\/]AnimeStudio\.Libraries[\\/]'
    r'AnimeStudio\.Ooz\.dll">[ \t]*\r?$'
)
OOZ_PROPERTY_CONTENT_ITEM = re.compile(
    r'(?m)^[ \t]*<Content\s+Include="\$\(AnimeStudioOozPath\)"\s+'
    r'Link="AnimeStudio\.Ooz\.dll">[ \t]*$'
)
OOZ_PATH_VALIDATION_TARGET = re.compile(
    r"(?is)<Target\s+Name=\"ValidateAnimeStudioOozPath\"\s+BeforeTargets=\"Restore;Build\">"
    r".*?<Error\s+Condition=\"'\$\(AnimeStudioOozPath\)'\s*==\s*''\"\s+"
    r"Text=\"ANIMESTUDIO_OOZ_PATH_MISSING:.*?/\s*>"
    r".*?<Error\s+Condition=\"!Exists\('\$\(AnimeStudioOozPath\)'\)\"\s+"
    r"Text=\"ANIMESTUDIO_OOZ_PATH_INVALID:.*?/\s*>.*?</Target>"
)
PROJECT_CLOSE = re.compile(r"(?m)^[ \t]*</Project>[ \t]*\r?$")
KEY_LOG_TEMPLATE = re.compile(r'(?i)Initializing decryptor with key\s*\{')
KEY_TOSTRING_TEMPLATE = re.compile(r'\{Name\}\s*\(\{Key\}\)')
EXPECTED_REPLACED_PATHS = (
    "AnimeStudio/AnimeStudio.csproj",
    "AnimeStudio/BundleFile.cs",
    "AnimeStudio/Crypto/UnityCN.cs",
    "AnimeStudio/UnityCNManager.cs",
)
EXPECTED_REMOVED_PATHS = ("AnimeStudio/CNKeys.json",)


def sha256_bytes(content: bytes) -> str:
    """Return a lowercase SHA-256 digest for exact file bytes."""

    return hashlib.sha256(content).hexdigest()


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig")
    except FileNotFoundError as error:
        raise SourceOverrideError(f"Required source file is missing: {path.name}.") from error
    except UnicodeDecodeError as error:
        raise SourceOverrideError(f"Source file is not UTF-8 text: {path.name}.") from error


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.release-source.tmp")
    try:
        temporary.write_bytes(content.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _digest(path: Path, relative_path: str) -> FileDigest:
    return FileDigest(path=relative_path, sha256=sha256_bytes(path.read_bytes()))


def _remove_one_embedded_key(source: str) -> str:
    matches = tuple(EMBEDDED_SET_KEY.finditer(source))
    if len(matches) != 1:
        raise SourceOverrideError(
            "AnimeStudio/BundleFile.cs must contain exactly one recognized embedded UnityCN key call; "
            f"found {len(matches)}."
        )
    return EMBEDDED_SET_KEY.sub(
        lambda match: f'{match.group("indent")}// The caller must provide any required local key profile.',
        source,
        count=1,
    )


def _remove_cnkeys_content_item(source: str) -> str:
    matches = tuple(PROJECT_CNKEYS_ITEM.finditer(source))
    if len(matches) != 1:
        raise SourceOverrideError(
            "AnimeStudio/AnimeStudio.csproj must contain exactly one CNKeys content item; "
            f"found {len(matches)}."
        )
    return PROJECT_CNKEYS_ITEM.sub("", source, count=1)


def _replace_ooz_content_item(source: str) -> str:
    matches = tuple(OOZ_CONTENT_ITEM.finditer(source))
    if len(matches) != 1:
        raise SourceOverrideError(
            "AnimeStudio/AnimeStudio.csproj must contain exactly one pinned AnimeStudio.Ooz.dll content item; "
            f"found {len(matches)}."
        )
    replaced = OOZ_CONTENT_ITEM.sub(
        lambda match: (
            f'{match.group("indent")}<Content Include="$(AnimeStudioOozPath)" '
            'Link="AnimeStudio.Ooz.dll">'
        ),
        source,
        count=1,
    )
    project_close = tuple(PROJECT_CLOSE.finditer(replaced))
    if len(project_close) != 1:
        raise SourceOverrideError("AnimeStudio/AnimeStudio.csproj must have one closing Project element.")

    target = (
        "\t<Target Name=\"ValidateAnimeStudioOozPath\" BeforeTargets=\"Restore;Build\">\n"
        "\t\t<Error Condition=\"'$(AnimeStudioOozPath)' == ''\" Text=\"ANIMESTUDIO_OOZ_PATH_MISSING: "
        "pass -p:AnimeStudioOozPath=&lt;rebuilt library&gt;.\" />\n"
        "\t\t<Error Condition=\"!Exists('$(AnimeStudioOozPath)')\" Text=\"ANIMESTUDIO_OOZ_PATH_INVALID: "
        "the source-built AnimeStudio.Ooz.dll was not found at $(AnimeStudioOozPath).\" />\n"
        "\t</Target>\n"
    )
    insertion = project_close[0].start()
    return f"{replaced[:insertion]}{target}{replaced[insertion:]}"


def _override_file(source_root: Path, override_root: Path, override_path: str, source_path: str) -> None:
    override_file = override_root.joinpath(*override_path.split("/"))
    destination_file = source_root.joinpath(*source_path.split("/"))
    if not override_file.is_file():
        raise SourceOverrideError(f"Required source override is missing: {override_path}.")
    if not destination_file.parent.is_dir():
        raise SourceOverrideError(f"Pinned source directory is missing for: {source_path}.")
    temporary = destination_file.with_name(f".{destination_file.name}.release-source.tmp")
    try:
        temporary.write_bytes(override_file.read_bytes())
        os.replace(temporary, destination_file)
    finally:
        if temporary.exists():
            temporary.unlink()


def _patch_inputs(override_root: Path) -> tuple[FileDigest, ...]:
    repository_root = override_root.resolve().parents[2]
    module_path = Path(__file__).resolve()
    inputs = (
        (override_root / "UnityCNManager.cs", "third_party/source-overrides/animestudio/UnityCNManager.cs"),
        (override_root / "Crypto" / "UnityCN.cs", "third_party/source-overrides/animestudio/Crypto/UnityCN.cs"),
        (module_path, module_path.relative_to(repository_root).as_posix()),
    )
    return tuple(_digest(path, relative_path) for path, relative_path in inputs)


def apply_animestudio_source_overrides(
    source_root: Path,
    override_root: Path,
) -> SourceOverrideReport:
    """Sanitize one fresh pinned AnimeStudio checkout without preserving its embedded keys."""

    source_root = source_root.resolve()
    override_root = override_root.resolve()
    if not source_root.is_dir():
        raise SourceOverrideError(f"AnimeStudio source root does not exist: {source_root}.")
    if not override_root.is_dir():
        raise SourceOverrideError(f"AnimeStudio override directory does not exist: {override_root}.")
    if source_root == override_root or source_root in override_root.parents:
        raise SourceOverrideError("The source root and override directory must be separate trees.")

    _override_file(
        source_root,
        override_root,
        "UnityCNManager.cs",
        "AnimeStudio/UnityCNManager.cs",
    )
    _override_file(
        source_root,
        override_root,
        "Crypto/UnityCN.cs",
        "AnimeStudio/Crypto/UnityCN.cs",
    )

    bundle_path = source_root / "AnimeStudio" / "BundleFile.cs"
    _atomic_write_text(bundle_path, _remove_one_embedded_key(_read_text(bundle_path)))

    project_path = source_root / "AnimeStudio" / "AnimeStudio.csproj"
    project = _remove_cnkeys_content_item(_read_text(project_path))
    _atomic_write_text(project_path, _replace_ooz_content_item(project))

    cnkeys_path = source_root / "AnimeStudio" / "CNKeys.json"
    if cnkeys_path.exists():
        if not cnkeys_path.is_file():
            raise SourceOverrideError("AnimeStudio/CNKeys.json must be a file before it can be removed.")
        cnkeys_path.unlink()

    report = SourceOverrideReport(
        files=tuple(
            _digest(source_root.joinpath(*relative_path.split("/")), relative_path)
            for relative_path in EXPECTED_REPLACED_PATHS
        ),
        applied_patches=_patch_inputs(override_root),
        removed_paths=EXPECTED_REMOVED_PATHS,
    )
    verify_animestudio_source_overrides(source_root, override_root, report)
    return report


def verify_animestudio_source_overrides(
    source_root: Path,
    override_root: Path,
    expected_report: SourceOverrideReport,
) -> None:
    """Verify exact overlay hashes and reject embedded-key or CNKeys content regressions."""

    source_root = source_root.resolve()
    override_root = override_root.resolve()
    actual_files = tuple(
        _digest(source_root.joinpath(*relative_path.split("/")), relative_path)
        for relative_path in EXPECTED_REPLACED_PATHS
    )
    if actual_files != expected_report.files:
        raise SourceOverrideError("Sanitized AnimeStudio source hashes differ from the recorded overlay manifest.")
    if _patch_inputs(override_root) != expected_report.applied_patches:
        raise SourceOverrideError("AnimeStudio override inputs changed after source preparation.")
    if expected_report.removed_paths != EXPECTED_REMOVED_PATHS:
        raise SourceOverrideError("The recorded AnimeStudio exclusion set is incomplete.")

    manager = _read_text(source_root / "AnimeStudio" / "UnityCNManager.cs")
    crypto = _read_text(source_root / "AnimeStudio" / "Crypto" / "UnityCN.cs")
    bundle = _read_text(source_root / "AnimeStudio" / "BundleFile.cs")
    project = _read_text(source_root / "AnimeStudio" / "AnimeStudio.csproj")
    if MANAGER_KEY_LITERAL.search(manager) is not None:
        raise SourceOverrideError("UnityCNManager.cs still contains a literal 128-bit key value.")
    if ANY_EMBEDDED_SET_KEY.search(bundle) is not None:
        raise SourceOverrideError("BundleFile.cs still contains an embedded UnityCN key call.")
    if KEY_LOG_TEMPLATE.search(crypto) is not None or KEY_TOSTRING_TEMPLATE.search(crypto) is not None:
        raise SourceOverrideError("UnityCN.cs still formats a supplied key into a log or string representation.")
    if PROJECT_CNKEYS_ITEM.search(project) is not None:
        raise SourceOverrideError("AnimeStudio.csproj still copies a CNKeys.json file into build output.")
    if OOZ_CONTENT_ITEM.search(project) is not None or OOZ_PROPERTY_CONTENT_ITEM.search(project) is None:
        raise SourceOverrideError("AnimeStudio.csproj does not consume only an explicit source-built Ooz library.")
    if OOZ_PATH_VALIDATION_TARGET.search(project) is None:
        raise SourceOverrideError("AnimeStudio.csproj does not reject a missing source-built Ooz library path.")
    if (source_root / "AnimeStudio" / "CNKeys.json").exists():
        raise SourceOverrideError("AnimeStudio/CNKeys.json must not be present in a sanitized source tree.")
