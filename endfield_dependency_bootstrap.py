"""Fetch and build pinned SceneProbe/BundleScanner dependencies for first run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / "third_party" / "animestudio.lock.json"
PROJECTS = {
    "sceneProbe": ROOT / "dotnet" / "EndfieldSceneProbe" / "EndfieldSceneProbe.csproj",
    "bundleScanner": ROOT / "dotnet" / "EndfieldBundleScanner" / "EndfieldBundleScanner.csproj",
}


class DependencyBootstrapError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def load_lock() -> dict[str, Any]:
    payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if payload.get("format") != "EndfieldPinnedDependency/1":
        raise DependencyBootstrapError(f"Unsupported dependency lock: {LOCK_PATH}")
    return payload


def run_logged(
    command: list[str],
    *,
    cwd: Path,
    log_path: Path,
    error_code: str,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: int = 600,
) -> str:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        for index in range(1, 1000):
            candidate = log_path.with_name(f"{log_path.stem}.retry{index:03d}{log_path.suffix}")
            if not candidate.exists():
                log_path = candidate
                break
        else:
            raise DependencyBootstrapError(f"No retry log path is available for {log_path}")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=dict(environment or os.environ),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
        else:
            process.kill()
        try:
            output, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        log_path.write_text(output or "", encoding="utf-8", newline="\n")
        raise DependencyBootstrapError(
            f"{error_code}: command timed out after {timeout_seconds}s. Inspect {log_path}."
        ) from error
    log_path.write_text(output or "", encoding="utf-8", newline="\n")
    if process.returncode != 0:
        raise DependencyBootstrapError(
            f"{error_code}: command failed with exit code {process.returncode}. Inspect {log_path}."
        )
    return output or ""


def require_tool(name: str, error: str) -> str:
    path = shutil.which(name)
    if not path:
        raise DependencyBootstrapError(error)
    return path


def verify_patch(path: Path, expected: str) -> None:
    if not path.is_file() or sha256_file(path) != expected.upper():
        raise DependencyBootstrapError(
            f"ANIMESTUDIO_PATCH_HASH_MISMATCH: expected {expected} for {path}. Restore the published source tree."
        )


def git_output(git: str, checkout: Path, *args: str) -> str:
    try:
        process = subprocess.run(
            [git, "-C", str(checkout), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise DependencyBootstrapError(
            f"ANIMESTUDIO_GIT_TIMEOUT: {' '.join(args)} exceeded 30s"
        ) from error
    if process.returncode != 0:
        raise DependencyBootstrapError(
            f"ANIMESTUDIO_GIT_FAILED: {' '.join(args)}: {(process.stderr or process.stdout).strip()}"
        )
    return process.stdout.strip()


def git_head_or_none(git: str, checkout: Path) -> str | None:
    """Return HEAD, allowing an initialized checkout whose first fetch was interrupted."""
    try:
        return git_output(git, checkout, "rev-parse", "--verify", "HEAD")
    except DependencyBootstrapError as error:
        if "Needed a single revision" in str(error) or "unknown revision" in str(error):
            return None
        raise


def ensure_pinned_checkout(
    git: str,
    *,
    checkout: Path,
    workspace: Path,
    repository: str,
    commit: str,
    log_dir: Path,
) -> str:
    if checkout.exists():
        if not (checkout / ".git").is_dir():
            raise DependencyBootstrapError(
                f"ANIMESTUDIO_WORKSPACE_CONFLICT: existing path is not a Git checkout: {checkout}"
            )
    else:
        checkout.mkdir(parents=True, exist_ok=False)
        run_logged(
            [git, "-C", str(checkout), "init"],
            cwd=workspace,
            log_path=log_dir / "animestudio_init.log",
            error_code="ANIMESTUDIO_INIT_FAILED",
            timeout_seconds=30,
        )
        run_logged(
            [git, "-C", str(checkout), "remote", "add", "origin", repository],
            cwd=workspace,
            log_path=log_dir / "animestudio_remote.log",
            error_code="ANIMESTUDIO_REMOTE_FAILED",
            timeout_seconds=30,
        )

    head = git_head_or_none(git, checkout)
    if head is None:
        run_logged(
            [git, "-C", str(checkout), "fetch", "--depth", "1", "origin", commit],
            cwd=workspace,
            log_path=log_dir / "animestudio_fetch.log",
            error_code="ANIMESTUDIO_FETCH_FAILED",
            timeout_seconds=120,
        )
        run_logged(
            [git, "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"],
            cwd=workspace,
            log_path=log_dir / "animestudio_checkout.log",
            error_code="ANIMESTUDIO_PIN_UNAVAILABLE",
            timeout_seconds=30,
        )
        head = git_output(git, checkout, "rev-parse", "--verify", "HEAD")

    if head.casefold() != commit.casefold():
        raise DependencyBootstrapError(
            f"ANIMESTUDIO_PIN_MISMATCH: expected {commit}, found {head}. Use a new workspace."
        )
    return head


def apply_patch(git: str, checkout: Path, patch: Path) -> str:
    reverse = subprocess.run(
        [git, "-C", str(checkout), "apply", "--reverse", "--check", str(patch)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if reverse.returncode == 0:
        return "already_applied"
    check = subprocess.run(
        [git, "-C", str(checkout), "apply", "--check", str(patch)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check.returncode != 0:
        raise DependencyBootstrapError(
            "ANIMESTUDIO_PATCH_BASE_MISMATCH: the pinned checkout does not accept the published patch. "
            f"Inspect {checkout}; no arbitrary revision will be patched."
        )
    process = subprocess.run(
        [git, "-C", str(checkout), "apply", str(patch)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise DependencyBootstrapError(
            f"ANIMESTUDIO_PATCH_FAILED: {(process.stderr or process.stdout).strip()}"
        )
    return "applied"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build pinned Endfield .NET extraction helpers.")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--include-shader-diagnostics", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    lock = load_lock()
    workspace = args.workspace.resolve()
    log_dir = args.log_dir.resolve()
    checkout = workspace / "AnimeStudio"
    if args.plan_only:
        print(
            json.dumps(
                {
                    "format": "EndfieldDependencyBootstrapPlan/1",
                    "workspace": str(workspace),
                    "logDir": str(log_dir),
                    "repository": lock["repository"],
                    "commit": lock["commit"],
                    "sdk": lock["sdk"],
                    "projects": {name: str(path) for name, path in PROJECTS.items()},
                    "includeShaderDiagnostics": args.include_shader_diagnostics,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    git = require_tool(
        "git.exe",
        "GIT_NOT_FOUND: install Git for Windows, then rerun first-run dependency bootstrap.",
    )
    dotnet = require_tool(
        "dotnet.exe",
        f"DOTNET_NOT_FOUND: .NET SDK {lock['sdk']} x64 is required; a runtime is insufficient.",
    )
    sdk_output = run_logged(
        [dotnet, "--list-sdks"],
        cwd=ROOT,
        log_path=log_dir / "dotnet_sdks.log",
        error_code="DOTNET_SDK_QUERY_FAILED",
    )
    if not any(line.startswith(str(lock["sdk"]) + " ") for line in sdk_output.splitlines()):
        raise DependencyBootstrapError(
            f"DOTNET_SDK_PIN_MISSING: global.json requires {lock['sdk']}. Install that SDK and rerun."
        )

    workspace.mkdir(parents=True, exist_ok=True)
    head = ensure_pinned_checkout(
        git,
        checkout=checkout,
        workspace=workspace,
        repository=str(lock["repository"]),
        commit=str(lock["commit"]),
        log_dir=log_dir,
    )

    applied = []
    for patch_row in lock["patches"]:
        if patch_row["role"] == "optional_shader_diagnostics" and not args.include_shader_diagnostics:
            continue
        patch_path = (ROOT / patch_row["path"]).resolve()
        verify_patch(patch_path, patch_row["sha256"])
        applied.append({"role": patch_row["role"], "status": apply_patch(git, checkout, patch_path)})

    upstream = []
    for row in lock.get("upstreamFiles") or []:
        path = checkout / row["path"]
        actual = sha256_file(path) if path.is_file() else None
        if actual != str(row["sha256"]).upper():
            raise DependencyBootstrapError(
                f"ANIMESTUDIO_UPSTREAM_FILE_MISMATCH: {row['path']} does not match the lock."
            )
        upstream.append({"path": row["path"], "sha256": actual})

    outputs = {}
    for index, (name, project) in enumerate(PROJECTS.items(), start=1):
        lock_file = project.parent / "packages.lock.json"
        if not lock_file.is_file():
            raise DependencyBootstrapError(
                f"NUGET_LOCK_MISSING: published project lock is absent: {lock_file}"
            )
        common = [f"-p:AnimeStudioRoot={checkout}"]
        run_logged(
            [dotnet, "restore", str(project), "--locked-mode", *common],
            cwd=ROOT,
            log_path=log_dir / f"{index:02d}_{name}_restore.log",
            error_code="NUGET_RESTORE_FAILED",
        )
        run_logged(
            [dotnet, "build", str(project), "-c", "Release", "-f", "net9.0-windows", "--no-restore", *common],
            cwd=ROOT,
            log_path=log_dir / f"{index:02d}_{name}_build.log",
            error_code="ANIMESTUDIO_BUILD_FAILED",
        )
        executable = project.parent / "bin" / "Release" / "net9.0-windows" / f"{project.stem}.exe"
        if not executable.is_file():
            raise DependencyBootstrapError(f"ANIMESTUDIO_BUILD_OUTPUT_MISSING: {executable}")
        outputs[name] = {
            "path": str(executable.resolve()),
            "bytes": executable.stat().st_size,
            "sha256": sha256_file(executable),
        }

    report = {
        "format": "EndfieldDependencyBootstrapReport/1",
        "status": "passed",
        "completedAt": utc_now(),
        "workspace": str(workspace),
        "commit": head,
        "sdk": lock["sdk"],
        "patches": applied,
        "upstreamFiles": upstream,
        "outputs": outputs,
    }
    report_path = log_dir / "dependency_bootstrap_report.json"
    if report_path.exists():
        raise DependencyBootstrapError(f"Refusing to overwrite dependency report: {report_path}")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DependencyBootstrapError, OSError, ValueError, KeyError) as error:
        print(f"DEPENDENCY_BOOTSTRAP_ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
