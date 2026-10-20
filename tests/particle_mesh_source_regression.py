"""Exercise mesh vertex emission against actual SceneProbe systems and sidecars."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from extractor_core.particle_mesh_geometry import (
    ParticleMeshGeometryError, load_particle_shape_geometry, parse_particle_mesh_geometry,
)
from extractor_core.particle_snapshot import (
    ParticleSnapshotError, UnsupportedParticleSnapshot, _record, _sequence,
    generate_fixed_emitter_particle_snapshot, generate_particle_snapshot,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-systems', type=int, required=True)
    args = parser.parse_args()
    payload = args.manifest.read_bytes()
    manifest = _record(json.loads(payload), 'Manifest')
    before = json.dumps(manifest, sort_keys=True)
    particles = _record(manifest['Particles'], 'Manifest.Particles')
    systems = [_record(item, 'System') for item in _sequence(particles['Systems'], 'Particles.Systems')]
    passed: list[dict[str, object]] = []
    failures_checked = 0
    for system in systems:
        parameters = _record(system['Parameters'], 'Parameters')
        shape = _record(parameters['Shape'], 'Shape')
        if shape['Type'] != 6 or shape['Enabled'] is not True:
            continue
        asset = _record(system['Asset'], 'System.Asset')
        identity = f"{asset['Source']}:{asset['PathId']}"
        geometry = load_particle_shape_geometry(manifest, args.manifest, system)
        if geometry is None:
            raise AssertionError(f'Mesh system did not load real geometry: {identity}')
        try:
            generate_particle_snapshot(system, 1.0, identity, 100000, 100000, (0.0, -9.81, 0.0), geometry)
        except UnsupportedParticleSnapshot:
            failures_checked += 1
        else:
            raise AssertionError(f'Strict dynamic entry accepted world-space emitter: {identity}')
        snapshots = []
        vertex_errors = []
        velocity_source = _record(parameters['VelocityOverLifetime'], 'VelocityOverLifetime')
        velocity_curves = tuple(_record(velocity_source[axis], f'VelocityOverLifetime.{axis}') for axis in ('X', 'Y', 'Z'))
        if any(curve['MinMaxState'] != 0 for curve in velocity_curves):
            raise AssertionError(f'Regression requires the authored constant velocity module: {identity}')
        initial_velocity = tuple(float(curve['Scalar']) for curve in velocity_curves)
        birth_vertices: dict[int, tuple[float, float, float]] = {}
        unique_vertices: set[tuple[float, float, float]] = set()
        for seconds in (0.0, 0.1, 0.25, 0.5, 1.0, 2.0):
            snapshot = generate_fixed_emitter_particle_snapshot(system, seconds, identity, 100000, 100000,
                                                                (0.0, -9.81, 0.0), geometry)
            repeated = generate_fixed_emitter_particle_snapshot(system, seconds, identity, 100000, 100000,
                                                                (0.0, -9.81, 0.0), geometry)
            if snapshot != repeated or 'SimulationSpace.World_fixed_emitter_no_motion_history' not in snapshot.unsupported_modules:
                raise AssertionError(f'Fixed source preview is nondeterministic or hides its world-space boundary: {identity}')
            if not geometry.colors and 'ShapeModule.UseMeshColors_missing_vertex_colors_material_color_unverified' not in snapshot.unsupported_modules:
                raise AssertionError(f'Absent vertex colors were silently replaced: {identity}')
            for particle in snapshot.particles:
                elapsed = seconds * float(system['SimulationSpeed']) - particle.birth_time
                birth = tuple(position - 0.5 * (start + finish) * elapsed
                              for position, start, finish in zip(particle.position, initial_velocity, particle.velocity))
                nearest = min(geometry.vertices, key=lambda vertex: math.dist(vertex, birth))
                error = math.dist(nearest, birth)
                if error > 1e-9:
                    raise AssertionError(f'Particle birth does not belong to source mesh: identity={identity}, id={particle.particle_id}, error={error}')
                if particle.particle_id in birth_vertices and birth_vertices[particle.particle_id] != nearest:
                    raise AssertionError(f'An existing particle switched source vertices over time: identity={identity}, id={particle.particle_id}')
                birth_vertices[particle.particle_id] = nearest
                unique_vertices.add(nearest)
                vertex_errors.append(error)
            snapshots.append({'seconds': seconds, 'alive': len(snapshot.particles), 'generated': snapshot.generated,
                              'scope': snapshot.preview_scope, 'unsupported_modules': snapshot.unsupported_modules})
        for speed in (0.0, 0.5, 2.0):
            actual = generate_fixed_emitter_particle_snapshot({**system, 'SimulationSpeed': speed}, 0.5, identity,
                                                               100000, 100000, (0.0, -9.81, 0.0), geometry)
            expected = generate_fixed_emitter_particle_snapshot({**system, 'SimulationSpeed': 1.0}, 0.5 * speed,
                                                                 identity, 100000, 100000, (0.0, -9.81, 0.0), geometry)
            if actual != expected:
                raise AssertionError(f'Mesh emission changed the simulation-speed contract: {identity}, speed={speed}')
        if len(unique_vertices) < 3 or not vertex_errors:
            raise AssertionError(f'Real source did not exercise distinct emitted vertices: {identity}')
        for changed_shape in ({**shape, 'PlacementMode': 1}, {**shape, 'UseMeshMaterialIndex': True}):
            changed = {**system, 'Parameters': {**parameters, 'Shape': changed_shape}}
            try:
                generate_fixed_emitter_particle_snapshot(changed, 0.5, identity, 100000, 100000,
                                                         (0.0, -9.81, 0.0), geometry)
            except UnsupportedParticleSnapshot:
                failures_checked += 1
            else:
                raise AssertionError(f'Unsupported mesh selection accepted: {identity}')
        try:
            generate_fixed_emitter_particle_snapshot(system, 0.5, identity, 100000, 100000,
                                                     (0.0, -9.81, 0.0), replace(geometry, path_id=geometry.path_id + 1))
        except ParticleSnapshotError:
            failures_checked += 1
        else:
            raise AssertionError(f'Mesh identity mismatch accepted: {identity}')
        export = next(_record(item, 'MeshExport') for item in _sequence(manifest['MeshExports'], 'MeshExports')
                      if _record(_record(item, 'MeshExport')['Asset'], 'Asset')['PathId'] == geometry.path_id)
        sidecar = args.manifest.parent / str(export['ShapeGeometryFile'])
        source_geometry = _record(json.loads(sidecar.read_text(encoding='utf-8-sig')), 'Geometry')
        try:
            parse_particle_mesh_geometry({**source_geometry, 'Normals': []}, (geometry.source, geometry.path_id), len(geometry.vertices))
        except ParticleMeshGeometryError:
            failures_checked += 1
        else:
            raise AssertionError(f'Missing source normals accepted: {identity}')
        absolute_export = {**export, 'ShapeGeometryFile': str(sidecar.resolve())}
        changed_exports = [absolute_export if item is export else item for item in _sequence(manifest['MeshExports'], 'MeshExports')]
        try:
            load_particle_shape_geometry({**manifest, 'MeshExports': changed_exports}, args.manifest, system)
        except ParticleMeshGeometryError:
            failures_checked += 1
        else:
            raise AssertionError(f'Absolute mesh payload path accepted: {identity}')
        passed.append({'system': asset, 'mesh': {'source': geometry.source, 'path_id': str(geometry.path_id)},
                       'vertices': len(geometry.vertices), 'normals': len(geometry.normals), 'colors': len(geometry.colors),
                       'unique_emitted_vertices': len(unique_vertices), 'maximum_birth_vertex_error': max(vertex_errors),
                       'samples': snapshots})
    if len(passed) != args.expected_systems or json.dumps(manifest, sort_keys=True) != before:
        raise AssertionError(f'Unexpected source coverage or mutated manifest: passed={len(passed)}, expected={args.expected_systems}')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump({'status': 'passed', 'source_manifest_sha256': hashlib.sha256(payload).hexdigest(),
                   'passed': passed, 'negative_checks': failures_checked,
                   'scope': 'Real mesh vertex emission and fixed-emitter deterministic previews; no Unity random stream, noise, material-color, or moving world-emitter parity claim'}, stream, indent=2)


if __name__ == '__main__':
    main()
