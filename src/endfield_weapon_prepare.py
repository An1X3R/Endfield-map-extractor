"""Prepare catalog-selected weapon source geometry and material evidence."""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
from typing import Literal

from endfield_character_bundle_inputs import prepare_source_provider_bundles
from endfield_character_cache import CharacterDataCache, character_bundle_closure, source_stamp
from endfield_character_catalog import CharacterResourceProvider, _normalize_container_path
from endfield_character_prepare import CharacterFileFingerprint, _fingerprint, _logical_bundle_name
from endfield_character_sources import game_source_stamps, validate_character_storage, verify_game_source
from endfield_scene_discovery import tool_identity
from endfield_scene_export import run_scene_process
from endfield_weapon_catalog import WeaponCatalog, WeaponCatalogError, WeaponResourceBinding, read_weapon_catalog


WeaponVariant = Literal['base', 'refined_candidate']
CheckCancelled = Callable[[], None]
Progress = Callable[[str, float, str], None]


@dataclass(frozen=True)
class WeaponPrepareTools:
    static_reader: Path
    shader_reader: Path
    visibility_reader: Path


@dataclass(frozen=True)
class WeaponPreparationRequest:
    game_root: Path
    provider_index: Path
    database: Path
    cab_inventory: Path
    cache_root: Path
    catalog: Path
    weapon_id: str
    variant: WeaponVariant
    resource_path: str
    output: Path
    unity_version: str
    max_bundles: int
    max_compressed_bytes: int
    tools: WeaponPrepareTools


@dataclass(frozen=True)
class WeaponSourceSelection:
    weapon_id: str
    name: str | None
    variant: WeaponVariant
    resource_path: str
    provider: CharacterResourceProvider
    variant_semantics: Literal['source_base', 'unverified_candidate']


@dataclass(frozen=True)
class WeaponPreparedArtifact:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class WeaponPreparedBundle:
    logical_name: str
    artifact: WeaponPreparedArtifact


@dataclass(frozen=True)
class WeaponPreparedOutputs:
    input_manifest: str
    source_dir: str
    geometry: str
    shader_identity: str
    renderer_visibility: str


@dataclass(frozen=True)
class WeaponSourceSummary:
    root_source: str
    root_path_id: int
    root_name: str
    source_renderers: int
    geometry_meshes: int
    source_materials: int
    source_textures: int
    shader_families: tuple[str, ...]
    reader_status: str


@dataclass(frozen=True)
class WeaponPreparationReceipt:
    format: Literal['EndfieldWeaponPreparation/1']
    status: Literal['source_prepared_requires_blender']
    catalog_identity: str
    selection: WeaponSourceSelection
    source_inputs: tuple[CharacterFileFingerprint, ...]
    tool_identities: tuple[tuple[str, str], ...]
    unity_version: str
    extracted_bundles: tuple[WeaponPreparedBundle, ...]
    outputs: WeaponPreparedOutputs
    source_summary: WeaponSourceSummary
    artifacts: tuple[WeaponPreparedArtifact, ...]
    pending: tuple[str, ...]
    scope: str


def _record(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise WeaponCatalogError(f'Expected weapon preparation object: field={field}')
    return value


def _rows(value: object, field: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise WeaponCatalogError(f'Expected weapon preparation array: field={field}')
    return tuple(value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeaponCatalogError(f'Expected nonempty weapon preparation text: field={field}')
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise WeaponCatalogError(f'Expected weapon preparation integer: field={field}, value={value!r}')
    return value


def _read(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding='utf-8-sig'))
    return _record(value, str(path))


def resolve_weapon_source(catalog: WeaponCatalog, weapon_id: str, variant: WeaponVariant, resource_path: str) -> WeaponSourceSelection:
    """Accept only an exact current catalog resource with one verified provider."""
    entries = tuple(row for row in catalog.entries if row.weapon_id == weapon_id)
    if len(entries) != 1:
        raise WeaponCatalogError(f'Weapon selection must resolve exactly once: weapon={weapon_id}, matches={len(entries)}')
    entry = entries[0]
    resources: tuple[WeaponResourceBinding, ...]
    if variant == 'base':
        if entry.model_status != 'resolved':
            raise WeaponCatalogError(f'Weapon base model is unresolved: weapon={weapon_id}, status={entry.model_status}')
        resources = entry.resources
    elif variant == 'refined_candidate':
        resources = tuple(candidate.resource for candidate in entry.refined_candidates)
    else:
        raise WeaponCatalogError(f'Unsupported weapon source variant: variant={variant!r}')
    matches = tuple(row for row in resources
                    if _normalize_container_path(row.resource_path) == _normalize_container_path(resource_path))
    if len(matches) != 1 or matches[0].provider_status != 'resolved' or len(matches[0].providers) != 1:
        raise WeaponCatalogError(f'Weapon resource must be an exact discovered variant with one provider: weapon={weapon_id}, variant={variant}, resource={resource_path}, matches={len(matches)}')
    selected = matches[0]
    return WeaponSourceSelection(weapon_id, entry.name, variant, selected.resource_path, selected.providers[0],
                                 'source_base' if variant == 'base' else 'unverified_candidate')


def prepared_artifact_path(root: Path, value: str) -> Path:
    """Require source artifacts to remain relative and inside their portable directory."""
    normalized = value.replace('\\', '/')
    parts = normalized.split('/')
    if not normalized or ':' in normalized or PurePosixPath(normalized).is_absolute() or any(part in ('', '.', '..') for part in parts):
        raise WeaponCatalogError(f'Unsafe prepared weapon artifact path: path={value!r}')
    target = (root / normalized).resolve(strict=True)
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise WeaponCatalogError(f'Prepared weapon artifact escapes its source directory: path={value!r}')
    return target


def _artifact(path: Path, root: Path, check_cancelled: CheckCancelled) -> WeaponPreparedArtifact:
    fingerprint = _fingerprint(path, check_cancelled)
    return WeaponPreparedArtifact(path.resolve().relative_to(root.resolve()).as_posix(), fingerprint.bytes, fingerprint.sha256)


def _verify_artifact(root: Path, row: dict[str, object], check_cancelled: CheckCancelled) -> None:
    path = prepared_artifact_path(root, _text(row.get('path'), 'artifact.path'))
    actual = _fingerprint(path, check_cancelled)
    if actual.bytes != _integer(row.get('bytes'), 'artifact.bytes') or actual.sha256 != _text(row.get('sha256'), 'artifact.sha256').casefold():
        raise WeaponCatalogError(f'Prepared weapon artifact differs from the native reader receipt: path={path}')


def _closure_budget(request: WeaponPreparationRequest, provider: CharacterResourceProvider, output: Path, check_cancelled: CheckCancelled) -> tuple[str, ...]:
    if request.max_bundles <= 0 or request.max_compressed_bytes <= 0:
        raise WeaponCatalogError('Weapon preparation budgets must be positive')
    index, _fingerprint_value = CharacterDataCache(request.cache_root).read_character_cab_index(request.cab_inventory, check_cancelled)
    closures, metadata = character_bundle_closure(index, {'weapon': [provider.logical_name]})
    details = metadata['maps']['weapon']
    if details['unresolvedExternalCount']:
        raise WeaponCatalogError(f'Weapon source closure contains unresolved CAB dependencies: details={details}')
    names = tuple(sorted(_logical_bundle_name(name, 'weapon dependency closure') for name in closures['weapon']))
    with sqlite3.connect(request.database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        sizes = tuple(connection.execute('SELECT byte_length FROM bundles WHERE logical_name=?', (name,)).fetchone() for name in names)
    if any(row is None or type(row[0]) is not int or row[0] < 0 for row in sizes):
        raise WeaponCatalogError('Weapon dependency closure contains missing or invalid resource database bundle rows')
    compressed = sum(row[0] for row in sizes if row is not None)
    if len(names) > request.max_bundles or compressed > request.max_compressed_bytes:
        raise WeaponCatalogError(f'Weapon source dependency budget exceeded: bundles={len(names)}, compressed_bytes={compressed}, max_bundles={request.max_bundles}, max_compressed_bytes={request.max_compressed_bytes}')
    ancestor = next(path for path in (output.parent, *output.parents) if path.exists())
    free = shutil.disk_usage(ancestor).free
    required = compressed * 8 + 256_000_000
    if free < required:
        raise OSError(f'Weapon preparation has insufficient free space: output={output}, free_bytes={free}, required_bytes={required}')
    return names


def verify_weapon_source_outputs(root: Path, selection: WeaponSourceSelection, check_cancelled: CheckCancelled) -> WeaponSourceSummary:
    """Check native source identity, retained evidence and all portable payload references."""
    source = root / 'static_probe'
    receipt = _read(source / 'receipt.json')
    audit = _read(source / 'source_audit.json')
    if (receipt.get('format') != 'EndfieldCharacterStaticReceipt/1'
            or receipt.get('status') != 'static_export_completed' or receipt.get('complete') is not True
            or audit.get('format') != 'EndfieldCharacterSourceAudit/1'):
        raise WeaponCatalogError(f'Weapon reader has incomplete source coverage: receipt={source / "receipt.json"}, status={receipt.get("status")!r}')
    requested = _record(audit.get('requested_root'), 'source_audit.requested_root')
    actual_root = _record(audit.get('actual_root'), 'source_audit.actual_root')
    root_cabs = tuple(cab.name for cab in selection.provider.serialized_cabs)
    if (requested.get('source') not in root_cabs
            or _normalize_container_path(_text(requested.get('resource_path'), 'requested_root.resource_path')) != _normalize_container_path(selection.resource_path)
            or actual_root != receipt.get('root') or actual_root.get('source') != requested.get('source')
            or actual_root.get('type') != 'GameObject'):
        raise WeaponCatalogError('Weapon native reader resolved a different source root than the catalog selection')
    for raw in _rows(receipt.get('artifacts'), 'static_receipt.artifacts'):
        _verify_artifact(source, _record(raw, 'static artifact'), check_cancelled)
    geometry = _read(source / 'model.geometry.json')
    if geometry.get('format') != 'EndfieldCharacterGeometry/1':
        raise WeaponCatalogError('Weapon native reader did not emit supported portable geometry')
    binary = prepared_artifact_path(source, _text(geometry.get('binary_path'), 'geometry.binary_path'))
    binary_identity = _fingerprint(binary, check_cancelled)
    if binary_identity.bytes != geometry.get('binary_bytes') or binary_identity.sha256 != geometry.get('binary_sha256'):
        raise WeaponCatalogError('Weapon portable geometry binary differs from its manifest')
    renderers = tuple(_record(raw, 'source renderer') for raw in _rows(audit.get('renderers'), 'source_audit.renderers'))
    meshes = _rows(geometry.get('meshes'), 'geometry.meshes')
    if not renderers or not meshes:
        raise WeaponCatalogError('Weapon source contains no exported renderable geometry')
    for renderer in renderers:
        if _integer(renderer.get('positive_unresolved_bone_influences'), 'renderer.positive_unresolved_bone_influences') != 0:
            raise WeaponCatalogError('Weapon source contains unresolved weighted bone influences')
    materials = _rows(audit.get('materials'), 'source_audit.materials')
    textures_raw: object = json.loads((source / 'source_texture_artifacts.json').read_text(encoding='utf-8-sig'))
    textures = _rows(textures_raw, 'source_texture_artifacts')
    if len(materials) != receipt.get('source_materials') or len(textures) != receipt.get('source_textures'):
        raise WeaponCatalogError('Weapon retained source material or texture count differs from its reader receipt')
    for raw in textures:
        _verify_artifact(source, _record(_record(raw, 'source texture').get('png'), 'source texture.png'), check_cancelled)
    for raw in _rows(geometry.get('textures'), 'geometry.textures'):
        prepared_artifact_path(source, _text(_record(raw, 'geometry texture').get('path'), 'geometry.texture.path'))
    shader = _read(root / 'shader_identity/shader_identity.json')
    if shader.get('format') != 'EndfieldCharacterShaderIdentity/1' or _text(shader.get('source_audit_sha256'), 'shader.source_audit_sha256').casefold() != _fingerprint(source / 'source_audit.json', check_cancelled).sha256:
        raise WeaponCatalogError('Weapon Shader identities are not tied to the retained source audit')
    families = tuple(_text(_record(_record(raw, 'Shader row').get('shader'), 'Shader identity').get('name'), 'Shader name')
                     for raw in _rows(shader.get('shaders'), 'shader.shaders'))
    if not families or shader.get('material_count') != len(materials):
        raise WeaponCatalogError('Weapon source Shader identities do not cover its retained materials')
    visibility = _read(root / 'renderer_visibility.json')
    export_map = _read(source / 'renderer_export_map.json')
    if (visibility.get('format') != 'EndfieldCharacterRendererVisibility/1'
            or visibility.get('actual_root') != actual_root
            or len(_rows(visibility.get('renderers'), 'visibility.renderers')) != len(renderers)
            or export_map.get('format') != 'EndfieldCharacterRendererExportMap/1'
            or len(_rows(export_map.get('renderers'), 'export_map.renderers')) != len(renderers)):
        raise WeaponCatalogError('Weapon renderer source mapping or visibility does not cover the selected source')
    for required in ('material_sources.json', 'lod_sources.json'):
        raw_sources: object = json.loads((source / required).read_text(encoding='utf-8-sig'))
        for raw in _rows(raw_sources, required):
            row = _record(raw, required)
            if row.get('available') is not True:
                raise WeaponCatalogError(f'Weapon native source evidence could not be decoded: file={required}, object={row.get("object")!r}, reason={row.get("reason")!r}')
            _record(row.get('data'), required + '.data')
    return WeaponSourceSummary(_text(actual_root.get('source'), 'root.source'),
        _integer(actual_root.get('path_id'), 'root.path_id'), _text(actual_root.get('name'), 'root.name'),
        len(renderers), len(meshes), len(materials), len(textures), tuple(sorted(set(families))),
        _text(receipt.get('status'), 'reader.status'))


def prepare_weapon_source(request: WeaponPreparationRequest, check_cancelled: CheckCancelled, progress: Progress) -> WeaponPreparationReceipt:
    """Prepare one exact current weapon resource without constructing character selections."""
    check_cancelled()
    output = request.output.resolve()
    validate_character_storage(request.game_root, output)
    validate_character_storage(request.game_root, request.cache_root)
    if request.output.is_symlink() or output.exists():
        raise FileExistsError(f'Weapon preparation output already exists or is a symlink: {output}')
    if request.cache_root.resolve().is_relative_to(output) or output.is_relative_to(request.cache_root.resolve()):
        raise WeaponCatalogError('Weapon prepared output and dependency cache must be independent directories')
    before_game = game_source_stamps(request.game_root)
    inputs = (request.provider_index, request.database, request.cab_inventory, request.catalog)
    before_inputs = tuple(source_stamp(path) for path in inputs)
    expected = _read(request.catalog)
    catalog = read_weapon_catalog(request.game_root, request.provider_index, request.database, check_cancelled)
    if expected.get('format') != catalog.format or expected.get('source_identity') != catalog.source_identity:
        raise WeaponCatalogError('Weapon catalog identity differs from the current selected source bytes')
    selection = resolve_weapon_source(catalog, request.weapon_id, request.variant, request.resource_path)
    if len(selection.provider.serialized_cabs) != 1:
        raise WeaponCatalogError('Weapon reader root requires a unique selected provider CAB; multi-CAB root discovery is not implemented')
    if not request.unity_version.strip():
        raise WeaponCatalogError('Weapon source preparation requires an explicit Unity version')
    tools = (('static_reader', request.tools.static_reader), ('shader_reader', request.tools.shader_reader),
             ('visibility_reader', request.tools.visibility_reader))
    tool_before = tuple((name, tool_identity(path)) for name, path in tools)
    names = _closure_budget(request, selection.provider, output, check_cancelled)
    output.mkdir(parents=True)
    progress('source_bundles', .1, 'Extracting the exact selected weapon dependency closure')
    def bundle_progress(stage: str, fraction: float, message: str) -> None:
        progress(stage, .1 + fraction * .25, message)
    bundles = prepare_source_provider_bundles(request.game_root, request.provider_index, request.database,
        request.cab_inventory, request.cache_root, (selection.provider,), output / 'bundles', check_cancelled, bundle_progress)
    if tuple(sorted(bundle.logical_name for bundle in bundles)) != names:
        raise WeaponCatalogError('Weapon dependency closure changed between budget verification and extraction')
    manifest = {'format': 'EndfieldSingleCharacterInput/1', 'unity_version': request.unity_version,
        'bundles': [{'path': row.path, 'bytes': row.bytes, 'sha256': row.sha256} for row in bundles],
        'root': {'source': selection.provider.serialized_cabs[0].name, 'resource_path': _normalize_container_path(selection.resource_path)}}
    input_path = output / 'input.json'
    input_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    logs = output / 'logs'
    temporary = output / 'temporary'
    logs.mkdir()
    temporary.mkdir()
    environment = {**os.environ, 'TEMP': str(temporary), 'TMP': str(temporary)}
    progress('source_geometry', .4, 'Reading source weapon geometry and retained material evidence')
    run_scene_process([str(request.tools.static_reader), '--input', str(input_path), '--output', str(output / 'static_probe')],
                      logs / 'static_reader.log', environment, check_cancelled)
    progress('shader_identity', .7, 'Reading actual weapon Shader identities and property defaults')
    run_scene_process([str(request.tools.shader_reader), '--input', str(input_path), '--source-audit', str(output / 'static_probe/source_audit.json'),
                       '--output', str(output / 'shader_identity')], logs / 'shader_reader.log', environment, check_cancelled)
    run_scene_process([str(request.tools.visibility_reader), '--input', str(input_path), '--output', str(output / 'renderer_visibility.json')],
                      logs / 'visibility_reader.log', environment, check_cancelled)
    summary = verify_weapon_source_outputs(output, selection, check_cancelled)
    receipt = WeaponPreparationReceipt('EndfieldWeaponPreparation/1', 'source_prepared_requires_blender', catalog.source_identity,
        selection, tuple(_fingerprint(path, check_cancelled) for path in inputs), tool_before, request.unity_version,
        tuple(WeaponPreparedBundle(row.logical_name, _artifact(Path(row.path), output, check_cancelled)) for row in bundles),
        WeaponPreparedOutputs('input.json', 'static_probe', 'static_probe/model.geometry.json', 'shader_identity/shader_identity.json', 'renderer_visibility.json'),
        summary, tuple(_artifact(path, output, check_cancelled) for directory in (output / 'static_probe', output / 'shader_identity')
                       for path in sorted(directory.rglob('*')) if path.is_file())
                       + (_artifact(output / 'renderer_visibility.json', output, check_cancelled), _artifact(input_path, output, check_cancelled)),
        ('refined_variant_runtime_semantics_unverified',) if request.variant == 'refined_candidate' else (),
        'Current offline source geometry, materials, texture bytes, Shader identity and renderer evidence; Blender reconstruction, game activation and live simulation are not verified.')
    verify_game_source(request.game_root, before_game, check_cancelled)
    if before_inputs != tuple(source_stamp(path) for path in inputs) or tool_before != tuple((name, tool_identity(path)) for name, path in tools):
        raise WeaponCatalogError('Weapon preparation source indexes or reader tools changed during preparation')
    (output / 'weapon_preparation.json').write_text(json.dumps(asdict(receipt), ensure_ascii=False, indent=2), encoding='utf-8')
    progress('completed', 1., 'Weapon source preparation verified; Blender construction is a separate stage')
    return receipt


def _not_cancelled() -> None:
    return None


def _progress(stage: str, fraction: float, message: str) -> None:
    print(json.dumps({'stage': stage, 'fraction': fraction, 'message': message}), flush=True)


def add_preparation_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the shared explicit source preparation contract on a host parser."""
    for name in ('game-root', 'provider-index', 'database', 'cab-inventory', 'cache-root', 'catalog', 'output',
                 'static-reader', 'shader-reader', 'visibility-reader'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('weapon-id', 'resource-path', 'unity-version'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--variant', choices=('base', 'refined_candidate'), required=True)
    parser.add_argument('--max-bundles', type=int, required=True)
    parser.add_argument('--max-compressed-bytes', type=int, required=True)


def preparation_request_from_arguments(args: argparse.Namespace) -> WeaponPreparationRequest:
    """Construct the typed preparation request from the shared parser fields."""
    return WeaponPreparationRequest(args.game_root, args.provider_index, args.database, args.cab_inventory,
        args.cache_root, args.catalog, args.weapon_id, args.variant, args.resource_path, args.output, args.unity_version,
        args.max_bundles, args.max_compressed_bytes, WeaponPrepareTools(args.static_reader, args.shader_reader, args.visibility_reader))


def main(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_preparation_arguments(parser)
    request = preparation_request_from_arguments(parser.parse_args(list(argv)))
    receipt = prepare_weapon_source(request, _not_cancelled, _progress)
    print(json.dumps({'status': receipt.status, 'output': str(request.output), 'pending': receipt.pending}), flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
