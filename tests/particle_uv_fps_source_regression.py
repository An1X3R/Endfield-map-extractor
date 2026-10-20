"""Validate grid FPS animation using unmodified SceneProbe particle sources."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extractor_core.particle_snapshot import (
    ParticleSnapshotError, _integer, _number, _record, _sequence, _uv_rect,
    generate_fixed_emitter_particle_snapshot,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.manifest.read_bytes()
    manifest = _record(json.loads(source), "Manifest")
    systems = _sequence(_record(manifest["Particles"], "Particles")["Systems"], "Systems")
    results: list[dict[str, object]] = []
    for raw in systems:
        system = _record(raw, "System")
        parameters = _record(system["Parameters"], "Parameters")
        uv = _record(parameters["Uv"], "Uv")
        if not uv["Enabled"] or uv["TimeMode"] != 2:
            continue
        start = _record(uv["StartFrame"], "StartFrame")
        if uv["Mode"] != 0 or uv["AnimationType"] != 0 or start["MinMaxState"] != 0 or start["Scalar"] != 0:
            raise AssertionError("Real FPS fixture is outside the whole-sheet zero-start subset")
        columns = _integer(uv["TilesX"], "Uv.TilesX")
        rows = _integer(uv["TilesY"], "Uv.TilesY")
        fps = _number(uv["Fps"], "Uv.Fps")
        if fps <= 0 or uv["FlipU"] != 0 or uv["FlipV"] != 0:
            raise AssertionError("Real FPS fixture needs a positive rate and unflipped UVs")
        asset = _record(system["Asset"], "Asset")
        identity = f"{asset['Source']}:{asset['PathId']}"
        checks = 0
        frames: set[int] = set()
        for seconds in (0.1, 0.25, 0.5, 0.75, 1.0, 1.125, 1.5, 2.0, 3.0, 5.5):
            snapshot = generate_fixed_emitter_particle_snapshot(
                system, seconds, identity, 1000, 10000, (0.0, -9.81, 0.0), None)
            for particle in snapshot.particles:
                age = seconds * _number(system["SimulationSpeed"], "SimulationSpeed") - particle.birth_time
                frame = math.floor(age * fps) % (columns * rows)
                column, row = frame % columns, frame // columns
                expected = (column / columns, 1 - (row + 1) / rows,
                            (column + 1) / columns, 1 - row / rows)
                if particle.uv_rect != expected:
                    raise AssertionError(f"FPS frame used the wrong particle age: {identity}, age={age}")
                frames.add(frame)
                checks += 1
        for index in range(columns * rows * 2 + 1):
            for epsilon in (-1e-8, 0.0, 1e-8):
                age = index / fps + epsilon
                if age < 0:
                    continue
                actual = _uv_rect(uv, 0.37, age, random.Random(13))
                frame = math.floor(age * fps) % (columns * rows)
                if actual[:2] != (frame % columns / columns, 1 - (frame // columns + 1) / rows):
                    raise AssertionError(f"FPS frame boundary or wrap failed: age={age}, frame={frame}")
                if actual != _uv_rect(uv, 0.91, age, random.Random(13)):
                    raise AssertionError("FPS animation depends on normalized particle lifetime")
        for field, value in (("TimeMode", 1), ("TimeMode", 99), ("Mode", 1), ("Fps", -1),
                             ("Fps", float("nan")), ("Fps", float("inf"))):
            try:
                _uv_rect({**uv, field: value}, 0.5, 0.5, random.Random(13))
            except ParticleSnapshotError:
                pass
            else:
                raise AssertionError(f"Unsupported or invalid UV parameter accepted: {field}={value}")
        missing_fps = {key: value for key, value in uv.items() if key != "Fps"}
        try:
            _uv_rect(missing_fps, 0.5, 0.5, random.Random(13))
        except ParticleSnapshotError:
            pass
        else:
            raise AssertionError("Missing FPS field was silently defaulted")
        if frames != set(range(columns * rows)) or checks == 0:
            raise AssertionError("Real snapshot coverage missed an animation frame")
        results.append({"source": identity, "particle_checks": checks, "frames": sorted(frames),
                        "retained_limits": list(snapshot.unsupported_modules)})
    if not results or source != args.manifest.read_bytes():
        raise AssertionError("FPS source coverage is empty or the source was modified")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump({"status": "passed", "manifest": str(args.manifest),
                   "sha256": hashlib.sha256(source).hexdigest(), "systems": results,
                   "scope": "Documented FPS sampling in a fixed-emitter static preview; no Unity simulation parity claim"}, stream, indent=2)
    print("PARTICLE_UV_FPS_SOURCE_REGRESSION_PASSED", len(results), flush=True)


if __name__ == "__main__":
    main()
