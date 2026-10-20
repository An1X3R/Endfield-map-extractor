"""Prepare and export explicitly selected standalone display prop models."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import file_digest
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
from typing import Literal

from character_runtime.character_windows_paths import io_path
from endfield_character_bundle_inputs import prepare_providers_bundles
from endfield_character_cache import CharacterDataCache, CharacterSourceChangedError, character_bundle_closure, source_stamp
from endfield_character_prepare import CharacterFileFingerprint, _fingerprint
from endfield_character_sources import CharacterSourceContext, game_source_stamps, validate_character_storage, verify_game_source
from endfield_display_props import (
    CheckCancelled, DisplayPropCatalog, DisplayPropEntry, DisplayPropError, Progress,
    _integer, _object, _read, _rows, _text, read_display_prop_catalog, select_display_props,
)
from endfield_scene_discovery import tool_identity
from endfield_scene_export import run_scene_process


@dataclass(frozen=True)
class DisplayPropTools:
    json_reader: Path
    static_reader: Path
    shader_reader: Path
    visibility_reader: Path


@dataclass(frozen=True)
class DisplayPropLimits:
    max_bundles: int
    max_compressed_bytes: int


@dataclass(frozen=True)
class DisplayPropPreparationRequest:
    context: CharacterSourceContext
    catalog: DisplayPropCatalog
    prop_id: str
    tools: DisplayPropTools
    unity_version: str
    limits: DisplayPropLimits
    output: Path


@dataclass(frozen=True)
class DisplayPropArtifact:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class DisplayPropSourceSummary:
    source: str
    path_id: int
    name: str
    renderers: int
    meshes: int
    materials: int
    textures: int


@dataclass(frozen=True)
class DisplayPropPreparation:
    format: Literal['EndfieldDisplayPropPreparation/1']
    status: Literal['source_prepared_requires_blender']
    catalog_id: str
    selection: DisplayPropEntry
    source_inputs: tuple[CharacterFileFingerprint, ...]
    tool_identities: tuple[tuple[str, str], ...]
    source_summary: DisplayPropSourceSummary
    artifacts: tuple[DisplayPropArtifact, ...]
    pending: tuple[str, ...]


@dataclass(frozen=True)
class DisplayPropExportRequest:
    preparation: DisplayPropPreparationRequest
    build_output: Path


@dataclass(frozen=True)
class DisplayPropExportResult:
    format: Literal['EndfieldDisplayPropExport/1']
    status: Literal['completed']
    catalog_id: str
    prop_id: str
    resource_path: str
    blend: DisplayPropArtifact
    preparation_receipt: str
    build_receipt: str
    reopen_audit: str
    pending: tuple[str, ...]
    limitations: tuple[str, ...]


def _artifact(path: Path, root: Path, check_cancelled: CheckCancelled) -> DisplayPropArtifact:
    check_cancelled()
    with io_path(path).open('rb') as stream:
        digest = file_digest(stream, 'sha256').hexdigest()
    return DisplayPropArtifact(path.relative_to(root).as_posix(), io_path(path).stat().st_size, digest)


def _artifact_path(root: Path, value: str) -> Path:
    path = PurePosixPath(value.replace('\\', '/'))
    if (not value or ':' in value or path.is_absolute()
            or any(part in ('', '.', '..') for part in value.replace('\\', '/').split('/'))):
        raise DisplayPropError(f'Unsafe prepared display prop artifact path: {value!r}')
    target = (root/str(path)).resolve()
    if not target.is_relative_to(root.resolve()) or not io_path(target).is_file():
        raise DisplayPropError(f'Prepared display prop artifact is missing or outside its root: {target}')
    return target


def _verify_artifact(root: Path, row: dict[str, object], check_cancelled: CheckCancelled) -> None:
    path = _artifact_path(root, _text(row.get('path'), 'artifact.path'))
    actual = _artifact(path, root, check_cancelled)
    if actual.bytes != _integer(row.get('bytes'), 'artifact.bytes') or actual.sha256 != _text(row.get('sha256'), 'artifact.sha256').casefold():
        raise DisplayPropError(f'Prepared display prop artifact differs from its receipt: {path}')


def _asset_key(value: object) -> tuple[str, int]:
    row = _object(value, 'asset identity')
    return _text(row.get('source'), 'asset.source').casefold(), _integer(row.get('path_id'), 'asset.path_id')


def verify_display_prop_outputs(root: Path, entry: DisplayPropEntry,
                                check_cancelled: CheckCancelled) -> DisplayPropSourceSummary:
    """Verify source identity and retained geometry, renderer, shader and texture evidence."""
    source = root/'static_probe'
    receipt = _read(source/'receipt.json')
    audit = _read(source/'source_audit.json')
    if (receipt.get('format') != 'EndfieldCharacterStaticReceipt/1' or receipt.get('complete') is not True
            or receipt.get('status') != 'static_export_completed' or audit.get('format') != 'EndfieldCharacterSourceAudit/1'):
        raise DisplayPropError('Display prop native reader did not complete source extraction')
    if _text(receipt.get('input_sha256'), 'static.input_sha256').casefold() != _artifact(root/'input.json', root, check_cancelled).sha256:
        raise DisplayPropError('Display prop native receipt is not bound to the selected input manifest')
    requested = _object(audit.get('requested_root'), 'requested_root')
    actual = _object(audit.get('actual_root'), 'actual_root')
    if (len(entry.providers) != 1 or len(entry.providers[0].serialized_cabs) != 1
            or requested.get('source') != entry.providers[0].serialized_cabs[0].name
            or requested.get('resource_path') != entry.resource_path or actual != receipt.get('root')
            or actual.get('source') != requested.get('source') or actual.get('type') != 'GameObject'):
        raise DisplayPropError('Display prop native source root differs from the exact catalog selection')
    for artifact in _rows(receipt.get('artifacts'), 'static artifacts'):
        _verify_artifact(source, artifact, check_cancelled)
    geometry = _read(source/'model.geometry.json')
    if geometry.get('format') != 'EndfieldCharacterGeometry/1':
        raise DisplayPropError('Display prop reader returned unsupported portable geometry')
    binary = _artifact_path(source, _text(geometry.get('binary_path'), 'geometry.binary_path'))
    _verify_artifact(source, {'path':binary.relative_to(source).as_posix(),
        'bytes':geometry.get('binary_bytes'), 'sha256':geometry.get('binary_sha256')}, check_cancelled)
    renderers = _rows(audit.get('renderers'), 'source renderers')
    meshes = _rows(geometry.get('meshes'), 'geometry meshes')
    if not renderers or not meshes:
        raise DisplayPropError('Display prop source contains no portable renderable geometry')
    source_keys = tuple(_asset_key(row.get('object')) for row in renderers)
    if len(source_keys) != len(set(source_keys)):
        raise DisplayPropError('Display prop source repeats a renderer identity')
    for row in renderers:
        if _integer(row.get('positive_unresolved_bone_influences'), 'positive_unresolved_bone_influences') != 0:
            raise DisplayPropError('Display prop source has unresolved weighted bone influences')
    visibility = _read(root/'renderer_visibility.json')
    visibility_rows = _rows(visibility.get('renderers'), 'renderer visibility')
    if (visibility.get('format') != 'EndfieldCharacterRendererVisibility/1' or visibility.get('actual_root') != actual
            or len(visibility_rows) != len(renderers)
            or {_asset_key(row.get('renderer')) for row in visibility_rows} != set(source_keys)):
        raise DisplayPropError('Display prop visibility evidence differs from the selected source renderers')
    visibility_by_id = {_asset_key(row.get('renderer')):row for row in visibility_rows}
    for row in renderers:
        enabled = row.get('enabled')
        observed = visibility_by_id[_asset_key(row.get('object'))]
        if type(enabled) is not bool or observed.get('enabled') is not enabled:
            raise DisplayPropError('Display prop source renderer enabled state differs between readers')
    shader = _read(root/'shader_identity/shader_identity.json')
    source_hash = _artifact(source/'source_audit.json', source, check_cancelled).sha256
    materials = _rows(audit.get('materials'), 'source materials')
    if (shader.get('format') != 'EndfieldCharacterShaderIdentity/1'
            or _text(shader.get('source_audit_sha256'), 'shader.source_audit_sha256').casefold() != source_hash
            or shader.get('material_count') != len(materials)):
        raise DisplayPropError('Display prop shader identities are not bound to the retained source audit')
    texture_raw: object = json.loads((source/'source_texture_artifacts.json').read_text(encoding='utf-8-sig'))
    textures = _rows(texture_raw, 'source textures')
    if receipt.get('source_materials') != len(materials) or receipt.get('source_textures') != len(textures):
        raise DisplayPropError('Display prop retained material/texture counts differ from the reader receipt')
    for row in textures:
        _verify_artifact(source, _object(row.get('png'), 'texture.png'), check_cancelled)
    for filename in ('material_sources.json', 'lod_sources.json'):
        raw: object = json.loads((source/filename).read_text(encoding='utf-8-sig'))
        if any(row.get('available') is not True for row in _rows(raw, filename)):
            raise DisplayPropError(f'Display prop source evidence could not be decoded: file={filename}')
    return DisplayPropSourceSummary(_text(actual.get('source'), 'root.source'),
        _integer(actual.get('path_id'), 'root.path_id'), _text(actual.get('name'), 'root.name'),
        len(renderers), len(meshes), len(materials), len(textures))


def _closure_names(request: DisplayPropPreparationRequest, entry: DisplayPropEntry,
                    check_cancelled: CheckCancelled) -> tuple[str, ...]:
    limits = request.limits
    if limits.max_bundles <= 0 or limits.max_compressed_bytes <= 0:
        raise DisplayPropError('Display prop extraction limits must be positive')
    context = request.context
    index, _ = CharacterDataCache(context.cache_root).read_character_cab_index(context.cab_inventory, check_cancelled)
    closures, metadata = character_bundle_closure(index, {'prop':[entry.providers[0].logical_name]})
    if metadata['maps']['prop']['unresolvedExternalCount']:
        raise DisplayPropError(f"Display prop closure has unresolved dependencies: {metadata['maps']['prop']}")
    names = tuple(sorted(closures['prop']))
    with closing(sqlite3.connect(context.resource_database.resolve().as_uri()+'?mode=ro', uri=True)) as connection:
        rows = tuple(connection.execute('SELECT byte_length FROM bundles WHERE logical_name=?', (name,)).fetchone() for name in names)
    if any(row is None or type(row[0]) is not int or row[0] < 0 for row in rows):
        raise DisplayPropError('Display prop closure contains absent or invalid resource database rows')
    size = sum(row[0] for row in rows if row is not None)
    if len(names) > limits.max_bundles or size > limits.max_compressed_bytes:
        raise DisplayPropError(f'Display prop dependency budget exceeded: bundles={len(names)}, bytes={size}, limits={limits}')
    ancestor = next(path for path in request.output.resolve().parents if path.exists())
    required = size*8 + 256_000_000
    if shutil.disk_usage(ancestor).free < required:
        raise OSError(f'Display prop extraction has insufficient free space: output={request.output}, required_bytes={required}')
    return names


def prepare_display_prop_source(request: DisplayPropPreparationRequest, check_cancelled: CheckCancelled,
                                 progress: Progress) -> DisplayPropPreparation:
    """Prepare one exact prop without treating it as a character or weapon selection."""
    context = request.context
    output = request.output.resolve()
    validate_character_storage(context.game_root, output)
    validate_character_storage(context.game_root, context.cache_root)
    if output.exists() or request.output.is_symlink():
        raise FileExistsError(f'Display prop source output already exists: {output}')
    if output.is_relative_to(context.cache_root.resolve()) or context.cache_root.resolve().is_relative_to(output):
        raise DisplayPropError('Display prop output and dependency cache must be independent')
    current = read_display_prop_catalog(context, request.tools.json_reader, request.unity_version, check_cancelled, progress)
    if current != request.catalog:
        raise DisplayPropError('Display prop catalog changed; refresh the catalog before extraction')
    entry = select_display_props(current, (request.prop_id,))[0]
    if len(entry.providers[0].serialized_cabs) != 1:
        raise DisplayPropError('Display prop root requires one exact provider CAB; multi-CAB root selection is unsupported')
    before_game = game_source_stamps(context.game_root)
    inputs = (context.provider_index, context.resource_database, context.cab_inventory)
    before = tuple(source_stamp(path) for path in inputs)
    tools = (('json_reader',request.tools.json_reader), ('static_reader',request.tools.static_reader),
             ('shader_reader',request.tools.shader_reader), ('visibility_reader',request.tools.visibility_reader))
    tool_before = tuple((name,tool_identity(path)) for name,path in tools)
    names = _closure_names(request, entry, check_cancelled)
    output.mkdir(parents=True)
    bundles = prepare_providers_bundles(context, entry.providers, output/'bundles', check_cancelled, progress)
    if tuple(sorted(row.logical_name for row in bundles)) != names:
        raise DisplayPropError('Display prop dependency closure changed between budget check and extraction')
    manifest = {'format':'EndfieldSingleCharacterInput/1', 'unity_version':request.unity_version,
        'bundles':[{'path':row.path,'bytes':row.bytes,'sha256':row.sha256} for row in bundles],
        'root':{'source':entry.providers[0].serialized_cabs[0].name,'resource_path':entry.resource_path}}
    (output/'input.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    logs, temporary = output/'logs', output/'temporary'
    logs.mkdir()
    temporary.mkdir()
    environment = {**os.environ,'TEMP':str(temporary),'TMP':str(temporary)}
    commands = (
        ('static',request.tools.static_reader, ['--input',str(output/'input.json'),'--output',str(output/'static_probe')]),
        ('shader',request.tools.shader_reader, ['--input',str(output/'input.json'),'--source-audit',str(output/'static_probe/source_audit.json'),'--output',str(output/'shader_identity')]),
        ('visibility',request.tools.visibility_reader, ['--input',str(output/'input.json'),'--output',str(output/'renderer_visibility.json')]),
    )
    for index,(name,tool,arguments) in enumerate(commands):
        progress(name,.3+index*.2,'Reading exact display prop source evidence')
        run_scene_process([str(tool),*arguments],logs/(name+'.log'),environment,check_cancelled)
    summary = verify_display_prop_outputs(output,entry,check_cancelled)
    artifacts = tuple(_artifact(path,output,check_cancelled) for directory in (output/'static_probe',output/'shader_identity')
                      for path in sorted(directory.rglob('*')) if path.is_file()) + (
        _artifact(output/'input.json',output,check_cancelled),_artifact(output/'renderer_visibility.json',output,check_cancelled))
    verify_game_source(context.game_root,before_game,check_cancelled)
    if before != tuple(source_stamp(path) for path in inputs) or tool_before != tuple((name,tool_identity(path)) for name,path in tools):
        raise CharacterSourceChangedError('Display prop source indexes or reader tools changed during extraction')
    result = DisplayPropPreparation('EndfieldDisplayPropPreparation/1','source_prepared_requires_blender',current.catalog_id,
        entry,tuple(_fingerprint(path,check_cancelled) for path in inputs),tool_before,summary,artifacts,())
    (output/'display_prop_preparation.json').write_text(json.dumps(asdict(result),ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def export_display_prop(request: DisplayPropExportRequest, check_cancelled: CheckCancelled,
                        progress: Progress) -> DisplayPropExportResult:
    """Build and independently reopen the selected static prop with packed editable materials."""
    preparation = request.preparation
    output = request.build_output.resolve()
    validate_character_storage(preparation.context.game_root,output)
    if output.exists() or request.build_output.is_symlink():
        raise FileExistsError(f'Display prop build output already exists: {output}')
    if output.is_relative_to(preparation.output.resolve()) or preparation.output.resolve().is_relative_to(output):
        raise DisplayPropError('Display prop source and Blender outputs must be independent directories')
    if not preparation.context.blender.is_file():
        raise FileNotFoundError(preparation.context.blender)
    prepared = prepare_display_prop_source(preparation,check_cancelled,progress)
    root = preparation.output.resolve()
    for artifact in prepared.artifacts:
        _verify_artifact(root,asdict(artifact),check_cancelled)
    script = Path(__file__).parent/'character_runtime/build_weapon_blend.py'
    temporary = root/'temporary'
    resources = temporary/'blender_user'
    environment = {**os.environ,'TEMP':str(temporary),'TMP':str(temporary),'BLENDER_USER_RESOURCES':str(resources),
        **{'BLENDER_USER_'+name:str(resources/path) for name,path in
           (('CONFIG','config'),('SCRIPTS','scripts'),('EXTENSIONS','extensions'),('DATAFILES','datafiles'))}}
    command = [str(preparation.context.blender),'--background','--factory-startup','--disable-autoexec',
               '--python-exit-code','1','--python',str(script),'--']
    run_scene_process(command+['build','--source-dir',str(root/'static_probe'),'--shader-identities',
        str(root/'shader_identity/shader_identity.json'),'--renderer-visibility',str(root/'renderer_visibility.json'),
        '--output-dir',str(output)],root/'logs/blender_build.log',environment,check_cancelled)
    run_scene_process(command+['audit','--output-dir',str(output)],root/'logs/blender_reopen.log',environment,check_cancelled)
    build, audit = _read(output/'build_receipt.json'), _read(output/'reopen_audit.json')
    blend = _artifact(output/'weapon.blend',output,check_cancelled)
    if (build.get('format') != 'EndfieldWeaponBuild/1' or audit.get('format') != 'EndfieldWeaponReopen/1'
            or audit.get('status') != 'passed' or build.get('output_sha256') != blend.sha256
            or audit.get('output_sha256') != blend.sha256 or build.get('output_bytes') != blend.bytes
            or _asset_key(build.get('source_root')) != (prepared.source_summary.source.casefold(),prepared.source_summary.path_id)
            or audit.get('source_meshes') != prepared.source_summary.meshes):
        raise DisplayPropError('Display prop build, saved artifact and reopened source evidence disagree')
    raw_limits = build.get('limitations')
    if not isinstance(raw_limits,list) or any(not isinstance(value,str) for value in raw_limits):
        raise DisplayPropError('Display prop builder omitted explicit material approximation limits')
    limitations = tuple(raw_limits)+('Standalone static prop only; source configuration mounts are evidence, not automatic character attachment or game activation rules.',)
    result = DisplayPropExportResult('EndfieldDisplayPropExport/1','completed',prepared.catalog_id,prepared.selection.prop_id,
        prepared.selection.resource_path,DisplayPropArtifact(str(output/blend.path),blend.bytes,blend.sha256),
        str(root/'display_prop_preparation.json'),str(output/'build_receipt.json'),str(output/'reopen_audit.json'),prepared.pending,limitations)
    (output/'display_prop_export.json').write_text(json.dumps(asdict(result),ensure_ascii=False,indent=2),encoding='utf-8')
    progress('completed',1.,'Display prop export and saved-file verification completed')
    return result
