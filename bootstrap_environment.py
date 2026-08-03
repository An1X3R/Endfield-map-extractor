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


def venv_python() -> Path:
    return VENV_ROOT / "Scripts" / "python.exe"


def requirement_lines() -> list[str]:
    if not REQUIREMENTS.is_file():
        return []
    rows = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            rows.append(value)
    return rows


def ensure_virtualenv() -> tuple[Path, bool]:
    interpreter = venv_python()
    created = False
    if not interpreter.is_file():
        builder = venv.EnvBuilder(with_pip=True, clear=False, symlinks=False)
        builder.create(str(VENV_ROOT))
        created = True
    if not interpreter.is_file():
        raise RuntimeError("Virtual environment creation did not produce " + str(interpreter))
    return interpreter, created


def install_requirements(interpreter: Path, no_install: bool) -> tuple[bool, str]:
    requirements = requirement_lines()
    if not requirements:
        return True, "no third-party requirements"
    if no_install:
        return False, "dependency installation skipped by --no-install"
    command = [
        str(interpreter),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--requirement",
        str(REQUIREMENTS),
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


def write_status(interpreter: Path, created: bool, dependency_ok: bool, dependency_message: str) -> None:
    payload = {
        "format": "EndfieldExtractorRuntimeStatus/1",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "app_root": str(APP_ROOT),
        "venv": str(VENV_ROOT),
        "interpreter": str(interpreter),
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
    parser = argparse.ArgumentParser(description="Prepare the isolated Endfield extractor environment.")
    parser.add_argument("--check", action="store_true", help="Prepare and print status without launching the UI.")
    parser.add_argument("--no-install", action="store_true", help="Do not invoke pip; useful for offline checks.")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    interpreter, created = ensure_virtualenv()
    dependency_ok, dependency_message = install_requirements(interpreter, args.no_install)
    write_status(interpreter, created, dependency_ok, dependency_message)
    print("ENDFIELD_EXTRACTOR_HOME=" + str(APP_ROOT))
    print("ENDFIELD_EXTRACTOR_PYTHON=" + str(interpreter))
    print("ENDFIELD_EXTRACTOR_DEPENDENCIES=" + ("ok" if dependency_ok else "degraded"))
    print("ENDFIELD_EXTRACTOR_DEPENDENCY_MESSAGE=" + dependency_message.replace("\n", " "))
    return 0 if dependency_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
