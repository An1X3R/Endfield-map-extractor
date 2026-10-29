"""Explicit one-time public-source checkout, dependency restore and character-reader build."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from endfield_character_tools import CharacterToolBuild, CharacterToolsError, ROOT, _lock, _object, _patch_specs, _relative, _text, _verify_file, build_character_tools
from release_source_overrides import apply_animestudio_source_overrides


def _git(command: list[str], log: Path, environment: dict[str, str]) -> None:
    """Retry explicitly requested network acquisition, preserving the last diagnostic."""
    for attempt in range(1, 4):
        process = subprocess.Popen(["git", "-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=60", *command], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            output, _ = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
            else:
                process.kill()
            output, _ = process.communicate()
            with log.open("ab") as handle:
                handle.write(output)
            raise CharacterToolsError(f"CHARACTER_TOOLS_SOURCE_ACQUISITION_TIMEOUT: command={command}; log={log}; retry setup with a new work directory after restoring network access.")
        with log.open("ab") as handle:
            handle.write(output)
        if process.returncode == 0:
            return
        print(json.dumps({"event": "character_source_acquisition_failed", "attempt": attempt, "exit_code": process.returncode,
                          "command": command, "log": str(log)}), file=sys.stderr)
    raise CharacterToolsError(f"CHARACTER_TOOLS_SOURCE_ACQUISITION_FAILED: exit={process.returncode}; command={command}; log={log}")


def prepare_character_sources(work_root: Path) -> tuple[Path, Path, Path, Path]:
    """Fetch only pinned public repositories; do not initialize private game-hook submodules."""
    if work_root.exists():
        raise CharacterToolsError(f"CHARACTER_TOOLS_SETUP_WORK_EXISTS: choose a new work directory: {work_root}")
    work_root.mkdir(parents=True)
    temporary = work_root / "temp"
    temporary.mkdir()
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "GIT_TERMINAL_PROMPT": "0"}
    lock = _lock()
    roots: dict[str, Path] = {}
    for key, name, autocrlf in (("animeStudio", "AnimeStudio", "true"), ("ruri", "Ruri", "false")):
        dependency = _object(lock[key], key)
        root = work_root / name
        root.mkdir()
        log = work_root / f"{name}.source.log"
        _git(["init", str(root)], log, environment)
        _git(["-C", str(root), "config", "core.autocrlf", autocrlf], log, environment)
        _git(["-C", str(root), "remote", "add", "origin", _text(dependency["repository"], f"{key}.repository")], log, environment)
        commit = _text(dependency["commit"], f"{key}.commit")
        _git(["-C", str(root), "fetch", "--depth", "1", "origin", commit], log, environment)
        _git(["-C", str(root), "checkout", "--detach", commit], log, environment)
        for patch_path, patch_sha256 in _patch_specs(dependency, key):
            patch = _relative(ROOT, patch_path, f"{key}.patch")
            _verify_file(patch, patch_sha256, f"{key}.patchSha256")
            _git(["-C", str(root), "apply", "--check", str(patch)], log, environment)
            _git(["-C", str(root), "apply", str(patch)], log, environment)
        if key == "animeStudio":
            apply_animestudio_source_overrides(root, ROOT / "third_party" / "source-overrides" / "animestudio")
        roots[key] = root
    packages = work_root / "nuget_packages"
    packages.mkdir()
    config = work_root / "nuget.explicit.config"
    config.write_text('<configuration><packageSources><clear /><add key="nuget.org" value="https://api.nuget.org/v3/index.json" /></packageSources></configuration>\n', encoding="utf-8")
    return roots["animeStudio"], roots["ruri"], packages, config


def setup_character_tools(output_root: Path, work_root: Path, dotnet: Path) -> Path:
    """Install tools only when explicitly invoked, preserving the ordinary offline startup path."""
    if output_root.exists() or output_root.is_relative_to(work_root) or work_root.is_relative_to(output_root):
        raise CharacterToolsError("CHARACTER_TOOLS_SETUP_OUTPUT_INVALID: output must be new and separate from the source work directory.")
    anime, ruri, packages, config = prepare_character_sources(work_root)
    return build_character_tools(CharacterToolBuild(output_root, anime, ruri, packages, config, dotnet))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output-root", "work-root", "dotnet"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    receipt = setup_character_tools(args.output_root.resolve(), args.work_root.resolve(), args.dotnet.resolve())
    print(json.dumps({"status": "built", "receipt": str(receipt)}))


if __name__ == "__main__":
    main()
