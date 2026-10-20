"""Export a catalog-selected standalone weapon with verified packed materials."""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Literal

from endfield_scene_export import run_scene_process
from endfield_character_sources import validate_character_storage
from endfield_character_catalog import _normalize_container_path
from endfield_weapon_prepare import (
    WeaponPreparationRequest, WeaponPreparationReceipt, WeaponPreparedArtifact,
    add_preparation_arguments, preparation_request_from_arguments, prepare_weapon_source,
    prepared_artifact_path, WeaponVariant,
)


@dataclass(frozen=True)
class WeaponExportRequest:
    preparation: WeaponPreparationRequest
    blender: Path
    build_output: Path


@dataclass(frozen=True)
class WeaponExportResult:
    format: Literal['EndfieldWeaponExport/1']
    status: Literal['completed', 'partial']
    weapon_id: str
    name: str | None
    variant: WeaponVariant
    variant_semantics: Literal['source_base', 'unverified_candidate']
    catalog_identity: str
    source_resource: str
    blend: WeaponPreparedArtifact
    preparation_receipt: str
    build_receipt: str
    reopen_audit: str
    pending: tuple[str, ...]
    limitations: tuple[str, ...]


def _read(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'Weapon export receipt must be an object: {path}')
    return value


def _hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build_prepared_weapon(request: WeaponExportRequest, prepared: WeaponPreparationReceipt,
                          check_cancelled: Callable[[], None],
                          progress: Callable[[str, float, str], None]) -> WeaponExportResult:
    """Build and reopen one exact prepared source; never silently reuse an old output."""
    validate_character_storage(request.preparation.game_root, request.build_output)
    output = request.build_output.resolve()
    if output.exists():
        raise FileExistsError(f'Weapon build output already exists: {output}')
    if not request.blender.is_file():
        raise FileNotFoundError(f'Weapon Blender executable is missing: {request.blender}')
    root = request.preparation.output.resolve()
    if (request.preparation.weapon_id, request.preparation.variant,
            _normalize_container_path(request.preparation.resource_path)) != (
            prepared.selection.weapon_id, prepared.selection.variant,
            _normalize_container_path(prepared.selection.resource_path)):
        raise ValueError('Prepared weapon selection differs from the requested catalog resource')
    recorded = _read(root / 'weapon_preparation.json')
    if recorded != json.loads(json.dumps(asdict(prepared))):
        raise ValueError('Weapon preparation receipt changed before Blender construction')
    for artifact in prepared.artifacts:
        check_cancelled()
        path = prepared_artifact_path(root, artifact.path)
        if path.stat().st_size != artifact.bytes or _hash(path) != artifact.sha256:
            raise ValueError(f'Prepared weapon source changed before construction: {path}')
    script = Path(__file__).parent / 'character_runtime/build_weapon_blend.py'
    logs = root / 'logs'
    temporary = root / 'temporary'
    environment = {**os.environ, 'TEMP': str(temporary), 'TMP': str(temporary)}
    resources = temporary / 'blender_user'
    environment.update({
        'BLENDER_USER_RESOURCES': str(resources),
        'BLENDER_USER_CONFIG': str(resources / 'config'),
        'BLENDER_USER_SCRIPTS': str(resources / 'scripts'),
        'BLENDER_USER_EXTENSIONS': str(resources / 'extensions'),
        'BLENDER_USER_DATAFILES': str(resources / 'datafiles'),
    })
    command = [str(request.blender), '--background', '--factory-startup', '--python-exit-code', '1',
               '--python', str(script), '--']
    progress('blender_build', .8, 'Building standalone weapon geometry and packed editable materials')
    run_scene_process(command + ['build', '--source-dir', str(root / prepared.outputs.source_dir),
        '--shader-identities', str(root / prepared.outputs.shader_identity),
        '--renderer-visibility', str(root / prepared.outputs.renderer_visibility),
        '--output-dir', str(output)], logs / 'blender_build.log', environment, check_cancelled)
    progress('blender_reopen', .95, 'Checking saved weapon geometry and packed source textures')
    run_scene_process(command + ['audit', '--output-dir', str(output)],
                      logs / 'blender_reopen.log', environment, check_cancelled)
    build = _read(output / 'build_receipt.json')
    audit = _read(output / 'reopen_audit.json')
    blend = output / 'weapon.blend'
    digest = _hash(blend)
    if (build.get('format') != 'EndfieldWeaponBuild/1' or audit.get('format') != 'EndfieldWeaponReopen/1'
            or audit.get('status') != 'passed' or digest != build.get('output_sha256')
            or digest != audit.get('output_sha256') or blend.stat().st_size != build.get('output_bytes')):
        raise ValueError(f'Weapon build, reopen and saved artifact receipts disagree: {output}')
    root_identity = build.get('source_root')
    if (not isinstance(root_identity, dict)
            or str(root_identity.get('source')).casefold() != prepared.source_summary.root_source.casefold()
            or root_identity.get('path_id') != prepared.source_summary.root_path_id):
        raise ValueError('Weapon Blender source identity differs from selected catalog root')
    if audit.get('source_meshes') != prepared.source_summary.geometry_meshes:
        raise ValueError('Weapon Blender source mesh coverage differs from prepared geometry')
    raw_limits = build.get('limitations')
    if not isinstance(raw_limits, list) or any(not isinstance(item, str) for item in raw_limits):
        raise TypeError('Weapon build limitations must be explicit text entries')
    result = WeaponExportResult('EndfieldWeaponExport/1', 'partial' if prepared.pending else 'completed',
        prepared.selection.weapon_id, prepared.selection.name, prepared.selection.variant,
        prepared.selection.variant_semantics, prepared.catalog_identity, prepared.selection.resource_path,
        WeaponPreparedArtifact(str(blend), blend.stat().st_size, digest),
        str(root / 'weapon_preparation.json'), str(output / 'build_receipt.json'),
        str(output / 'reopen_audit.json'), prepared.pending, tuple(raw_limits))
    (output / 'weapon_export.json').write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding='utf-8')
    progress('completed', 1., 'Standalone weapon export and saved-file validation completed')
    return result


def export_weapon(request: WeaponExportRequest, check_cancelled: Callable[[], None],
                  progress: Callable[[str, float, str], None]) -> WeaponExportResult:
    """Resolve current game data and export the selected source weapon variant."""
    validate_character_storage(request.preparation.game_root, request.build_output)
    if request.build_output.resolve().exists():
        raise FileExistsError(f'Weapon build output already exists: {request.build_output}')
    def preparation_progress(stage: str, fraction: float, message: str) -> None:
        progress('prepare.' + stage, fraction * .7, message)
    prepared = prepare_weapon_source(request.preparation, check_cancelled, preparation_progress)
    return build_prepared_weapon(request, prepared, check_cancelled, progress)


def _not_cancelled() -> None:
    return None


def _progress(stage: str, fraction: float, message: str) -> None:
    print(json.dumps({'stage': stage, 'fraction': fraction, 'message': message}), flush=True)


def main(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_preparation_arguments(parser)
    parser.add_argument('--blender', type=Path, required=True)
    parser.add_argument('--build-output', type=Path, required=True)
    args = parser.parse_args(list(argv))
    result = export_weapon(WeaponExportRequest(preparation_request_from_arguments(args), args.blender,
                           args.build_output), _not_cancelled, _progress)
    print(json.dumps(asdict(result), ensure_ascii=True))


if __name__ == '__main__':
    main(sys.argv[1:])
