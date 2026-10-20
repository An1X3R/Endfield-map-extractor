"""Reuse exact prefab-bound metadata and stat-identified local receipts."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast
import os

from endfield_scene_io import read_scene_manifest
from endfield_scene_material_contract import reference_key, require_object


def bounds_file_stamp(path: Path) -> dict[str, int]:
    """Record local file identity without rereading an already verified payload."""
    value = path.stat()
    return {'bytes': value.st_size, 'modifiedNs': value.st_mtime_ns, 'createdNs': value.st_ctime_ns,
            'device': value.st_dev, 'inode': value.st_ino}


def verified_bounds_manifest(receipt: Path, root: Path, probe: str, game: str) -> Path:
    """Validate legacy hashes once, keeping stat proofs in a separate new format."""
    from endfield_scene_resources import describe_file, read_json, resolve_path, write_index

    row = read_json(receipt)
    if (row.get('format') not in ('EndfieldPrefabBoundsBatch/1', 'EndfieldPrefabBoundsBatch/2')
            or row.get('probeSha256') != probe or row.get('gameSourceSha256') != game
            or not isinstance(row.get('roots'), list)):
        raise ValueError(f'Prefab bounds cache source identity differs: receipt={receipt}')
    descriptor = require_object(row.get('manifest'), 'prefab bounds manifest descriptor')
    manifest = resolve_path(descriptor['path'], root)
    if not manifest.is_relative_to(root) or not manifest.is_file():
        raise ValueError(f'Prefab bounds manifest escaped or disappeared: receipt={receipt}, manifest={manifest}')
    stamp = bounds_file_stamp(manifest)
    if row['format'] == 'EndfieldPrefabBoundsBatch/2':
        if row.get('manifestStat') != stamp:
            raise ValueError(f'Prefab bounds manifest stat identity changed: manifest={manifest}')
    else:
        proof = receipt.with_name('receipt_stat_v1.json')
        expected = {'format': 'EndfieldPrefabBoundsVerifiedStat/1', 'receiptStat': bounds_file_stamp(receipt),
                    'manifest': descriptor, 'manifestStat': stamp}
        if not proof.is_file() or read_json(proof) != expected:
            if describe_file(manifest) != descriptor:
                raise ValueError(f'Prefab bounds cache integrity differs: receipt={receipt}')
            write_index(proof, expected)
    return manifest


def available_bounds_prefabs(manifest: Path, requested: frozenset[str]) -> frozenset[str]:
    """Require exact source roots and complete mesh-bound references before reuse."""
    from endfield_scene_effects_plan import _PrefabCandidate, _root_members

    data = require_object(read_scene_manifest(manifest), str(manifest))
    if data.get('Format') != 'EndfieldSceneProbe/1':
        raise ValueError(f'Unsupported prefab bounds source manifest: {manifest}')
    if any(not isinstance(data.get(name), list) for name in ('MeshBounds', 'Nodes', 'LodGroups')):
        return frozenset()
    bounds = {reference_key(require_object(require_object(value, 'mesh bound')['Asset'], 'mesh bound asset'))
              for value in cast(list[object], data['MeshBounds'])}
    nodes = {reference_key(require_object(value, 'prefab bounds node')): require_object(value, 'prefab bounds node')
             for value in cast(list[object], data['Nodes'])}
    result: set[str] = set()
    for value in cast(list[object], data['Containers']):
        row = require_object(value, 'prefab bounds container')
        name = str(row['Container']).replace('\\', '/').casefold()
        asset = row.get('Asset')
        if name not in requested or not isinstance(asset, dict) or asset.get('Type') != 'GameObject':
            continue
        if reference_key(asset) not in nodes:
            continue
        candidate = _PrefabCandidate(name, asset, manifest, data)
        members = _root_members(candidate)
        meshes = tuple(nodes[key].get('Mesh') for key in members)
        if all(mesh is None or reference_key(require_object(mesh, 'prefab bounds mesh')) in bounds for mesh in meshes):
            result.add(name)
    return frozenset(result)


def link_bounds_bundles(available: Mapping[str, Path], names: list[str], target: Path) -> None:
    """Expose only one batch closure to the probe without copying shared payloads."""
    target.mkdir(parents=True, exist_ok=False)
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or len(relative.parts) != 2:
            raise ValueError(f'Prefab bounds bundle has an unsafe logical name: name={name}')
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.link(available[name], destination)
