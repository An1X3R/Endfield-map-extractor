"""Run portable scene builds from a user's prepared runtime and selected Blender."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import TypedDict, cast

from endfield_scene_material_contract import require_object


class SceneArtifact(TypedDict):
    path: str
    audit: str
    plan: str
    bytes: int
    sha256: str
    instances: int
    pending: int


def scene_runtime(runtime_path: Path, game_root: Path, map_id: str) -> dict[str, object]:
    runtime = require_object(json.loads(runtime_path.read_text(encoding="utf-8-sig")), str(runtime_path))
    if runtime.get("format") != "EndfieldRuntimeConfig/1":
        raise ValueError(f"Unsupported runtime configuration: {runtime_path}")
    paths = require_object(runtime.get("paths"), "runtime paths")
    configured_game = Path(str(paths["game_root"])).expanduser()
    if not configured_game.is_absolute():
        configured_game = runtime_path.parent / configured_game
    if configured_game.resolve() != game_root.resolve():
        raise ValueError("Selected game differs from the prepared runtime; prepare the selected installation first")
    extraction = require_object(runtime.get("extraction"), "runtime extraction")
    maps = require_object(extraction.get("maps"), "runtime maps")
    entry = require_object(maps.get(map_id), f"runtime map {map_id}")
    probe = require_object(entry.get("sceneProbe"), f"SceneProbe is not ready for {map_id}; resume first-run preparation")
    for label, value in (("instances", entry.get("instances")), ("sceneProbe", probe.get("path"))):
        if not isinstance(value, str) or not value:
            raise ValueError(f"Missing prepared {label} for {map_id}")
        path = Path(value)
        resolved = path if path.is_absolute() else runtime_path.parent / path
        if not resolved.is_file():
            raise FileNotFoundError(f"Prepared {label} is missing: {resolved}")
    return runtime


def scene_batches(canonical: Mapping[str, object]) -> tuple[dict[str, float], ...]:
    bounds = require_object(canonical.get("bounds"), "canonical bounds")
    values = {name: float(cast(float, bounds[name])) for name in ("xmin", "xmax", "zmin", "zmax")}
    mode = canonical["batch_mode"]
    if mode == "merged_selection":
        return (values,)
    if mode not in ("per_sector", "cluster_4x4_sectors"):
        raise ValueError(f"Unsupported scene batch mode: {mode}")
    size = 128 if mode == "per_sector" else 512
    import math
    batches = []
    for z in range(math.floor(values["zmin"] / size) * size, math.ceil(values["zmax"] / size) * size, size):
        for x in range(math.floor(values["xmin"] / size) * size, math.ceil(values["xmax"] / size) * size, size):
            batches.append({"xmin": max(values["xmin"], x), "xmax": min(values["xmax"], x + size),
                            "zmin": max(values["zmin"], z), "zmax": min(values["zmax"], z + size)})
    return tuple(batches)


def run_scene_process(command: list[str], log: Path, environment: Mapping[str, str],
                      check_cancelled: Callable[[], None]) -> None:
    with log.open("x", encoding="utf-8") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                   env=dict(environment), creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            while process.poll() is None:
                check_cancelled()
                time.sleep(0.2)
            if process.returncode != 0:
                raise RuntimeError(f"Blender exited with code {process.returncode}; inspect {log}")
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                             capture_output=True, text=True, check=False)
                    if stopped.returncode != 0 and process.poll() is None:
                        raise RuntimeError(f"Cannot stop Blender process {process.pid}: {stopped.stderr}")
                else:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        if process.poll() is None:
                            raise
                process.wait(timeout=30)


def build_scene_exports(runtime_path: Path, canonical: Mapping[str, object], output: Path,
                        check_cancelled: Callable[[], None], progress: Callable[[float, str], None]) -> list[SceneArtifact]:
    from endfield_scene_export_plan import prepare_scene_export_plan

    source = require_object(canonical.get("source"), "scene source")
    game = Path(str(source["game_root"])).resolve()
    runtime = scene_runtime(runtime_path, game, str(canonical["map_id"]))
    paths = require_object(runtime["paths"], "runtime paths")
    cache = Path(str(paths["cache_root"])).expanduser()
    if not cache.is_absolute():
        cache = runtime_path.parent / cache
    temporary = cache.resolve() / "scene_export_temp"
    if temporary.is_relative_to(game) or output.resolve().is_relative_to(game):
        raise ValueError("Scene output and temporary files must be outside the game installation")
    temporary.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}
    builder = Path(__file__).resolve().parent / "blender_export_prepared_scene.py"
    batches = scene_batches(canonical)
    results: list[SceneArtifact] = []
    for index, bounds in enumerate(batches):
        check_cancelled()
        directory = output / "scenes" / f"batch_{index + 1:04d}"
        directory.mkdir(parents=True, exist_ok=False)
        progress(index / len(batches), f"Preparing scene batch {index + 1}/{len(batches)}")
        request = {**canonical, "bounds": bounds}
        plan = prepare_scene_export_plan(runtime_path, request, directory / "scene_plan.json", check_cancelled)
        check_cancelled()
        blend = directory / (str(canonical["map_id"]) + ".blend")
        audit = directory / "scene_audit.json"
        command = [str(source["blender_exe"]), "--background", "--factory-startup", "--python-exit-code", "1",
                   "--python", str(builder), "--", "--plan", str(plan), "--output", str(blend), "--audit", str(audit)]
        run_scene_process(command, directory / "blender.log", environment, check_cancelled)
        report = require_object(json.loads(audit.read_text(encoding="utf-8")), "scene audit")
        if report.get("reopen_verified") is not True or not blend.is_file():
            raise ValueError(f"Blender did not produce a verified scene: {audit}")
        with blend.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        results.append({"path": str(blend.relative_to(output)), "audit": str(audit.relative_to(output)),
                        "plan": str(plan.relative_to(output)), "bytes": blend.stat().st_size, "sha256": digest,
                        "instances": int(cast(int, report["instances"])), "pending": int(cast(int, report["pending"]))})
        progress((index + 1) / len(batches), f"Verified scene batch {index + 1}/{len(batches)}")
    return results
