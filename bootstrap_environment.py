from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import venv
from datetime import datetime, timezone
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
VENV_ROOT = APP_ROOT / ".venv"
REQUIREMENTS = APP_ROOT / "requirements.txt"
STATUS_FILE = APP_ROOT / ".runtime_status.json"
SETUP_SCRIPT = APP_ROOT / "setup_development_environment.ps1"
MINIMUM_PYTHON = (3, 11)
MAXIMUM_PYTHON_EXCLUSIVE = (3, 15)


def venv_python() -> Path:
    return VENV_ROOT / "Scripts" / "python.exe"


def requirement_lines(requirements: Path) -> list[str]:
    if not requirements.is_file():
        return []
    rows: list[str] = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            rows.append(value)
    return rows


def setup_command() -> str:
    return f'powershell -ExecutionPolicy Bypass -File "{SETUP_SCRIPT}"'


def require_supported_python() -> None:
    version = sys.version_info[:2]
    if sys.implementation.name != "cpython" or not MINIMUM_PYTHON <= version < MAXIMUM_PYTHON_EXCLUSIVE:
        raise RuntimeError(
            "CPython 3.11 through 3.14 is required for the pinned runtime dependencies: "
            f"detected={sys.implementation.name} {version[0]}.{version[1]}"
        )


def require_virtualenv() -> Path:
    require_supported_python()
    interpreter = venv_python()
    if not interpreter.is_file():
        raise RuntimeError(
            f"Prepared Python environment is missing: {interpreter}. Run: {setup_command()}"
        )
    valid, message = verify_requirements(interpreter, REQUIREMENTS)
    if not valid:
        raise RuntimeError(
            f"Prepared Python dependencies are unavailable: {message}. Run: {setup_command()}"
        )
    return interpreter


def ensure_virtualenv() -> tuple[Path, bool]:
    """Create the local venv only for the explicit setup command."""
    interpreter = venv_python()
    created = False
    if not interpreter.is_file():
        builder = venv.EnvBuilder(with_pip=True, clear=False, symlinks=False)
        builder.create(str(VENV_ROOT))
        created = True
    if not interpreter.is_file():
        raise RuntimeError("Virtual environment creation did not produce " + str(interpreter))
    return interpreter, created


def verify_requirements(interpreter: Path, requirements: Path) -> tuple[bool, str]:
    version_process = subprocess.run(
        [
            str(interpreter),
            "-c",
            "import sys; print(f'{sys.implementation.name} {sys.version_info.major}.{sys.version_info.minor}'); "
            "raise SystemExit(0 if sys.implementation.name == 'cpython' "
            "and (3, 11) <= sys.version_info[:2] < (3, 15) else 1)",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if version_process.returncode != 0:
        detected = (version_process.stdout or version_process.stderr or "unknown interpreter").strip()
        return False, f"prepared interpreter must be CPython 3.11 through 3.14; detected={detected}"
    if not requirement_lines(requirements):
        return True, "no third-party requirements"
    command = [
        str(interpreter),
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--no-index",
        "--no-deps",
        "--disable-pip-version-check",
        "--requirement",
        str(requirements),
    ]
    process = subprocess.run(command, text=True, capture_output=True, check=False)
    if process.returncode == 0:
        return True, "all pinned requirements are already available offline"
    details = (process.stderr or process.stdout or "offline requirement check failed").strip()
    return False, details[-1200:]


def install_requirements(interpreter: Path, requirements: Path) -> tuple[bool, str]:
    if not requirement_lines(requirements):
        return True, "no third-party requirements"
    command = [
        str(interpreter),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--requirement",
        str(requirements),
    ]
    process = subprocess.run(command, text=True, capture_output=True, check=False)
    if process.returncode == 0:
        return True, "requirements installed or already satisfied"
    details = (process.stderr or process.stdout or "pip returned a non-zero exit code").strip()
    return False, details[-1200:]


def blender_path() -> str | None:
    configured = os.environ.get("ENDFIELD_BLENDER")
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    return None


def write_status(interpreter: Path | None, created: bool, dependency_ok: bool, dependency_message: str) -> None:
    payload = {
        "format": "EndfieldExtractorRuntimeStatus/1",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "app_root": str(APP_ROOT),
        "venv": str(VENV_ROOT),
        "interpreter": str(interpreter) if interpreter is not None else None,
        "venv_created_this_run": created,
        "dependency_ok": dependency_ok,
        "dependency_message": dependency_message,
        "blender": blender_path(),
        "game_root": os.environ.get("ENDFIELD_GAME_ROOT"),
        "cache_root": os.environ.get("ENDFIELD_CACHE_ROOT"),
        "export_root": os.environ.get("ENDFIELD_EXPORT_ROOT"),
    }
    STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check or explicitly prepare the isolated extractor environment.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Check the existing environment without creating or installing anything (default).")
    mode.add_argument("--prepare", action="store_true", help="Create the venv if needed and install Python requirements; setup-only.")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    try:
        require_supported_python()
    except RuntimeError as error:
        print(f"ENDFIELD_EXTRACTOR_PYTHON_UNSUPPORTED: {error}", file=sys.stderr)
        return 2
    if args.prepare:
        interpreter, created = ensure_virtualenv()
        dependency_ok, dependency_message = install_requirements(interpreter, REQUIREMENTS)
    else:
        interpreter = venv_python()
        created = False
        if interpreter.is_file():
            dependency_ok, dependency_message = verify_requirements(interpreter, REQUIREMENTS)
        else:
            dependency_ok, dependency_message = False, f"prepared interpreter is missing: {interpreter}"
    write_status(interpreter if interpreter.is_file() else None, created, dependency_ok, dependency_message)
    print("ENDFIELD_EXTRACTOR_HOME=" + str(APP_ROOT))
    print("ENDFIELD_EXTRACTOR_PYTHON=" + (str(interpreter) if interpreter.is_file() else "missing"))
    print("ENDFIELD_EXTRACTOR_DEPENDENCIES=" + ("ok" if dependency_ok else "missing"))
    print("ENDFIELD_EXTRACTOR_DEPENDENCY_MESSAGE=" + dependency_message.replace("\n", " "))
    if not dependency_ok:
        print("ENDFIELD_EXTRACTOR_SETUP_COMMAND=" + setup_command())
    return 0 if dependency_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
