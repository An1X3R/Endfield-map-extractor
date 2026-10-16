"""Build a portable per-export character material profile from verified source identities."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Literal, TypedDict


PROFILE_TEMPLATE_FORMAT = 'EndfieldCharacterMaterialProfileTemplate/1'
PROFILE_FORMAT = 'EndfieldCharacterMaterialProfile/1'
SHADER_IDENTITY_FORMAT = 'EndfieldCharacterShaderIdentity/1'
WEATHER_MANIFEST_FORMAT = 'EndfieldCharacterWeatherTextures/1'
ALPHA_AUDIT_FORMAT = 'EndfieldShadowCasterAlphaAudit/1'
STANDARD_SHADER = 'HGRP/CharacterNPR'
VERIFIED_STANDARD_SHADER_SHA256 = '2ee8e1b24874feaeec421f38f65e28647af60fbaab059f1f6f674dcb18745781'
STANDARD_ALPHA_PROPERTY_TYPES = (
    ('_BaseMap', 4), ('_BaseColor', 0), ('_SurfaceType', 2),
    ('_EnableAlphaTest', 2), ('_AlphaClipThreshold', 3),
)
WEATHER_SLOTS = (
    'characterSnowTexture',
    'characterRainDropTexture',
    'characterRainStreakTexture',
    'characterRainFaceDripTexture',
    'characterRainFaceDropletTexture',
)
SOURCE_EVIDENCE_KEYS = (
    'provider_index_sha256',
    'resource_database_sha256',
    'cab_inventory_sha256',
    'configuration_input_sha256',
    'configuration_typetrees_sha256',
    'configuration_receipt_sha256',
    'texture_input_sha256',
    'target_list_sha256',
    'texture_probe_receipt_sha256',
    'probe_texture_manifest_sha256',
)
SHA256_PATTERN = re.compile(r'[0-9a-f]{64}')


class ShaderIdentity(TypedDict):
    source: str
    path_id: int
    type: str
    name: str
    object_sha256: str


class ShaderCompatibility(TypedDict):
    status: Literal['verified_reference_object', 'host_approximation_source_contract']
    native_shader_semantics_verified: bool
    property_contract_verified: bool
    provenance: str


class WeatherTexture(TypedDict):
    slot: str
    source: str
    path_id: int
    width: int
    height: int
    texture_format: str
    color_space: int
    vertical_flip: bool
    artifact: str
    bytes: int
    sha256: str


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def object_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f'{label} must be a JSON object with string keys')
    return value


def text_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f'{label} must be a nonempty string')
    return value


def integer_value(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f'{label} must be an integer')
    return value


def sha256_value(value: object, label: str) -> str:
    result = text_value(value, label).casefold()
    if SHA256_PATTERN.fullmatch(result) is None:
        raise ValueError(f'{label} must contain exactly 64 hexadecimal characters')
    return result


def read_shader_identities(path: Path) -> dict[str, ShaderIdentity]:
    source = path.resolve(strict=True)
    document = object_value(json.loads(source.read_text(encoding='utf-8-sig')), 'shader identity')
    if document.get('format') != SHADER_IDENTITY_FORMAT:
        raise ValueError(f'Unsupported Shader identity format: {source}')
    audit_path = source.parent.parent / 'static_probe' / 'source_audit.json'
    audit_path = audit_path.resolve(strict=True)
    expected_audit_sha256 = sha256_value(document.get('source_audit_sha256'), 'source_audit_sha256')
    actual_audit_sha256 = file_sha256(audit_path)
    if expected_audit_sha256 != actual_audit_sha256:
        raise ValueError(f'Shader identity belongs to different source audit: expected={expected_audit_sha256}; actual={actual_audit_sha256}')
    rows = document.get('shaders')
    if not isinstance(rows, list) or not rows:
        raise ValueError(f'Shader identity must contain a nonempty shaders array: {source}')
    identities: dict[str, ShaderIdentity] = {}
    for index, raw in enumerate(rows):
        row = object_value(raw, f'shader identity row {index}')
        shader = object_value(row.get('shader'), f'shader identity row {index}.shader')
        name = text_value(shader.get('name'), f'shader identity row {index}.shader.name')
        identity: ShaderIdentity = {
            'source': text_value(shader.get('source'), f'{name}.source'),
            'path_id': integer_value(shader.get('path_id'), f'{name}.path_id'),
            'type': text_value(shader.get('type'), f'{name}.type'),
            'name': name,
            'object_sha256': sha256_value(row.get('object_sha256'), f'{name}.object_sha256'),
        }
        if identity['type'] != 'Shader':
            raise ValueError(f'Shader identity row has an unexpected object type: {name}/{identity["type"]}')
        if name in identities:
            raise ValueError(f'Shader family identity is ambiguous: {name}')
        identities[name] = identity
    return identities


def standard_shader_compatibility(shader_row: dict[str, object]) -> ShaderCompatibility:
    """Separate reference-object evidence from current source property compatibility."""
    shader = object_value(shader_row.get('shader'), 'Standard Shader identity')
    if shader.get('name') != STANDARD_SHADER or shader.get('type') != 'Shader':
        raise ValueError('Standard alpha compatibility requires the CharacterNPR Shader identity')
    digest = sha256_value(shader_row.get('object_sha256'), 'Standard Shader object SHA-256')
    if digest == VERIFIED_STANDARD_SHADER_SHA256:
        return {
            'status': 'verified_reference_object', 'native_shader_semantics_verified': True,
            'property_contract_verified': False,
            'provenance': 'The serialized Standard Shader object matches the audited reference object; Blender face-domain sampling remains an approximation.',
        }
    properties = shader_row.get('property_defaults')
    if not isinstance(properties, list) or not properties:
        raise ValueError(f'Changed Standard Shader requires its source property contract: shader_sha256={digest}')
    required = dict(STANDARD_ALPHA_PROPERTY_TYPES)
    found: dict[str, dict[str, object]] = {}
    for raw in properties:
        row = object_value(raw, 'Standard Shader property')
        name = text_value(row.get('m_Name'), 'Standard Shader property name')
        if name not in required:
            continue
        if name in found:
            raise ValueError(f'Duplicate Standard alpha property: property={name}; shader_sha256={digest}')
        actual_type = integer_value(row.get('m_Type'), name + '.m_Type')
        if actual_type != required[name]:
            raise ValueError(f'Standard alpha property type differs: property={name}; expected={required[name]}; actual={actual_type}; shader_sha256={digest}')
        found[name] = row
    missing = sorted(set(required) - set(found))
    if missing:
        raise ValueError(f'Standard alpha property contract is incomplete: missing={missing}; shader_sha256={digest}')
    texture = object_value(found['_BaseMap'].get('m_DefTexture'), '_BaseMap.m_DefTexture')
    if integer_value(texture.get('m_TexDim'), '_BaseMap.m_TexDim') != 2:
        raise ValueError(f'Standard alpha BaseMap must be a 2D source texture: shader_sha256={digest}')
    attributes = found['_EnableAlphaTest'].get('m_Attributes')
    if not isinstance(attributes, list) or 'Toggle(_ALPHATEST_ON)' not in attributes:
        raise ValueError(f'Standard alpha source toggle must name _ALPHATEST_ON: shader_sha256={digest}')
    return {
        'status': 'host_approximation_source_contract', 'native_shader_semantics_verified': False,
        'property_contract_verified': True,
        'provenance': 'Current source Shader identity and required alpha property types are verified. Existing Blender/reference-derived equations are host approximations; current shader program equivalence and native alpha semantics have not been audited.',
    }


def validate_shadow_alpha_compatibility(audit: dict[str, object], shader_row: dict[str, object]) -> ShaderCompatibility:
    """Reject upgraded audit claims and accept legacy evidence only for the reference object."""
    expected = standard_shader_compatibility(shader_row)
    if 'compatibility' not in audit:
        if expected['status'] != 'verified_reference_object':
            raise ValueError('Changed Standard Shader alpha evidence requires explicit host approximation compatibility')
        return expected
    actual = object_value(audit['compatibility'], 'alpha compatibility')
    if any(actual.get(key) != value or type(actual.get(key)) is not type(value) for key, value in expected.items()):
        raise ValueError('Alpha compatibility evidence differs from the actual Shader reference/property contract')
    return expected


def _safe_artifact_path(parent: Path, value: object, label: str) -> Path:
    relative = Path(text_value(value, label))
    if relative.is_absolute() or relative.drive or any(part in ('..', '') for part in relative.parts):
        raise ValueError(f'{label} must be a relative path without traversal: {relative}')
    candidate = (parent / relative).resolve(strict=True)
    if not candidate.is_relative_to(parent.resolve(strict=True)) or not candidate.is_file():
        raise ValueError(f'{label} resolves outside the weather manifest directory or is not a file: {candidate}')
    if candidate.suffix.casefold() != '.png':
        raise ValueError(f'{label} must refer to a PNG artifact: {candidate}')
    return candidate


def read_weather_textures(path: Path) -> tuple[dict[str, object], tuple[tuple[WeatherTexture, Path], ...]]:
    source = path.resolve(strict=True)
    document = object_value(json.loads(source.read_text(encoding='utf-8-sig')), 'weather texture manifest')
    if document.get('format') != WEATHER_MANIFEST_FORMAT or document.get('status') != 'prepared':
        raise ValueError(f'Unsupported or incomplete weather texture manifest: {source}')
    text_value(document.get('unity_version'), 'weather unity_version')
    text_value(document.get('pipeline_resource_path'), 'weather pipeline_resource_path')
    text_value(document.get('provider_logical_name'), 'weather provider_logical_name')
    root_source = text_value(document.get('root_source'), 'weather root_source')
    source_evidence = object_value(document.get('source_evidence'), 'weather source_evidence')
    for key in SOURCE_EVIDENCE_KEYS:
        sha256_value(source_evidence.get(key), f'weather source_evidence.{key}')
    raw_rows = document.get('textures')
    if not isinstance(raw_rows, list):
        raise TypeError('Weather texture manifest textures must be an array')
    if len(raw_rows) != len(WEATHER_SLOTS):
        raise ValueError(f'Weather texture manifest must contain exactly five texture rows: {source}')
    textures: list[tuple[WeatherTexture, Path]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_rows):
        row = object_value(raw, f'weather texture row {index}')
        slot = text_value(row.get('slot'), f'weather texture row {index}.slot')
        if slot not in WEATHER_SLOTS or slot in seen:
            raise ValueError(f'Weather texture slot is unknown or duplicated: {slot}')
        source_cab = text_value(row.get('source'), f'{slot}.source')
        if source_cab != root_source:
            raise ValueError(f'Weather texture CAB differs from its verified root source: slot={slot}; source={source_cab}; root={root_source}')
        texture: WeatherTexture = {
            'slot': slot,
            'source': source_cab,
            'path_id': integer_value(row.get('path_id'), f'{slot}.path_id'),
            'width': integer_value(row.get('width'), f'{slot}.width'),
            'height': integer_value(row.get('height'), f'{slot}.height'),
            'texture_format': text_value(row.get('texture_format'), f'{slot}.texture_format'),
            'color_space': integer_value(row.get('color_space'), f'{slot}.color_space'),
            'vertical_flip': row.get('vertical_flip'),
            'artifact': text_value(row.get('artifact'), f'{slot}.artifact'),
            'bytes': integer_value(row.get('bytes'), f'{slot}.bytes'),
            'sha256': sha256_value(row.get('sha256'), f'{slot}.sha256'),
        }
        if texture['width'] <= 0 or texture['height'] <= 0 or texture['bytes'] <= 0:
            raise ValueError(f'Weather texture metadata has invalid dimensions: {slot}')
        if texture['color_space'] not in (0, 1) or type(texture['vertical_flip']) is not bool:
            raise ValueError(f'Weather texture interpretation metadata is invalid: {slot}')
        artifact = _safe_artifact_path(source.parent, texture['artifact'], f'{slot}.artifact')
        if artifact.stat().st_size != texture['bytes']:
            raise ValueError(f'Weather texture byte count mismatch: slot={slot}; declared={texture["bytes"]}; actual={artifact.stat().st_size}')
        actual_sha256 = file_sha256(artifact)
        if actual_sha256 != texture['sha256']:
            raise ValueError(f'Weather texture SHA-256 mismatch: slot={slot}; expected={texture["sha256"]}; actual={actual_sha256}')
        seen.add(slot)
        textures.append((texture, artifact))
    if seen != set(WEATHER_SLOTS):
        raise ValueError(f'Weather texture manifest is missing required slots: {sorted(set(WEATHER_SLOTS) - seen)}')
    return {
        'unity_version': document['unity_version'],
        'pipeline_resource_path': document['pipeline_resource_path'],
        'provider_logical_name': document['provider_logical_name'],
        'root_source': root_source,
        'source_evidence': {key: source_evidence[key] for key in SOURCE_EVIDENCE_KEYS},
        'manifest_sha256': file_sha256(source),
    }, tuple(textures)


def create_export_material_profile(
    shader_identity_path: Path,
    weather_manifest_path: Path,
    output_dir: Path,
    template_path: Path,
) -> dict[str, Path]:
    identities = read_shader_identities(shader_identity_path)
    if STANDARD_SHADER not in identities:
        raise ValueError(f'Required audited alpha consumer Shader is absent: {STANDARD_SHADER}')
    standard_identity = identities[STANDARD_SHADER]
    shader_document = object_value(json.loads(shader_identity_path.read_text(encoding='utf-8-sig')), 'shader identity')
    standard_rows = tuple(object_value(row, 'Shader row') for row in shader_document['shaders']
                          if object_value(object_value(row, 'Shader row').get('shader'), 'Shader identity').get('name') == STANDARD_SHADER)
    if len(standard_rows) != 1:
        raise ValueError('Standard alpha compatibility requires exactly one Shader property record')
    compatibility = standard_shader_compatibility(standard_rows[0])
    weather_receipt, weather_textures = read_weather_textures(weather_manifest_path)
    template_file = template_path.resolve(strict=True)
    template = object_value(json.loads(template_file.read_text(encoding='utf-8-sig')), 'material profile template')
    if template.get('format') != PROFILE_TEMPLATE_FORMAT:
        raise ValueError(f'Unsupported character material profile template: {template_file}')
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    texture_directory = output / 'weather_textures'
    texture_directory.mkdir()
    normalized_rows: list[dict[str, object]] = []
    for texture, source in weather_textures:
        target_name = texture['slot'] + '.png'
        target = texture_directory / target_name
        shutil.copyfile(source, target)
        target_sha256 = file_sha256(target)
        if target.stat().st_size != texture['bytes'] or target_sha256 != texture['sha256']:
            raise IOError(f'Copied weather texture does not match its source: slot={texture["slot"]}')
        normalized_rows.append({
            'slot': texture['slot'],
            'source': {'source': texture['source'], 'path_id': texture['path_id']},
            'width': texture['width'], 'height': texture['height'],
            'texture_format': texture['texture_format'], 'color_space': texture['color_space'],
            'vertical_flip': texture['vertical_flip'], 'artifact': 'weather_textures/' + target_name,
            'bytes': texture['bytes'], 'sha256': target_sha256,
        })
    normalized_manifest = output / 'weather_texture_artifacts.json'
    normalized_manifest.write_text(json.dumps(normalized_rows, indent=2) + '\n', encoding='utf-8')
    profile = {key: value for key, value in template.items() if key not in ('format', 'standard_wet_film', 'standard_snow', 'skin_rain', 'hair_fur_snow')}
    profile.update({
        'format': PROFILE_FORMAT,
        'source_artifacts': {
            'shader_identity_sha256': file_sha256(shader_identity_path),
            'weather_manifest_sha256': weather_receipt['manifest_sha256'],
            'template_sha256': file_sha256(template_file),
        },
        'shader_identities': identities,
        'standard_shader_compatibility': compatibility,
    })
    manifest_hash = file_sha256(normalized_manifest)
    if STANDARD_SHADER in identities:
        wet_template = object_value(template.get('standard_wet_film'), 'template.standard_wet_film')
        profile['standard_wet_film'] = {
            'format': 'EndfieldStandardWetFilmProfile/1', 'source_family': STANDARD_SHADER,
            'texture_manifest': normalized_manifest.name, 'texture_manifest_sha256': manifest_hash,
            'orientation': wet_template['orientation'], 'tiling': wet_template['tiling'],
        }
        snow_template = object_value(template.get('standard_snow'), 'template.standard_snow')
        profile['standard_snow'] = snow_template
    else:
        profile.pop('standard_wet_film', None)
        profile.pop('standard_snow', None)
    if 'HGRP/CharacterNPR_Skin' in identities:
        skin_template = object_value(template.get('skin_rain'), 'template.skin_rain')
        profile['skin_rain'] = skin_template
    else:
        profile.pop('skin_rain', None)
    hair_fur_template = object_value(template.get('hair_fur_snow'), 'template.hair_fur_snow')
    hair_fur = {key: value for key, value in hair_fur_template.items()
                if key not in ('hair', 'fur', 'texture_manifest', 'texture_manifest_sha256')}
    if 'HGRP/CharacterNPR_Hair' in identities:
        hair_fur['hair'] = hair_fur_template['hair']
    if STANDARD_SHADER in identities:
        hair_fur['fur'] = hair_fur_template['fur']
    if 'hair' in hair_fur or 'fur' in hair_fur:
        hair_fur['texture_manifest'] = normalized_manifest.name
        hair_fur['texture_manifest_sha256'] = manifest_hash
        profile['hair_fur_snow'] = hair_fur
    else:
        profile.pop('hair_fur_snow', None)
    alpha_audit = {
        'schema': ALPHA_AUDIT_FORMAT,
        'shaderIdentity': {
            'name': STANDARD_SHADER, 'sourceCab': standard_identity['source'],
            'pathId': str(standard_identity['path_id']),
            'serializedObjectSha256': standard_identity['object_sha256'].upper(),
        },
        'compatibility': compatibility,
        'provenance': compatibility['provenance'],
    }
    alpha_path = output / 'shadow_alpha_audit.json'
    alpha_path.write_text(json.dumps(alpha_audit, indent=2) + '\n', encoding='utf-8')
    profile_path = output / 'material_profile.json'
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    receipt = {
        'format': 'EndfieldCharacterMaterialProfileBuild/1',
        'status': 'prepared',
        'profile': profile_path.name,
        'profile_sha256': file_sha256(profile_path),
        'alpha_audit': alpha_path.name,
        'alpha_audit_sha256': file_sha256(alpha_path),
        'weather_texture_manifest': normalized_manifest.name,
        'weather_texture_manifest_sha256': manifest_hash,
        'texture_slots': [row['slot'] for row in normalized_rows],
        'texture_bytes': sum(row['bytes'] for row in normalized_rows),
        'shader_identity_sha256': file_sha256(shader_identity_path),
        'weather_source_evidence': weather_receipt,
        'source_shader_object_hashes': {name: row['object_sha256'] for name, row in identities.items()},
        'standard_shader_compatibility': compatibility,
    }
    receipt_path = output / 'material_profile_receipt.json'
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'profile': profile_path, 'alpha_audit': alpha_path, 'receipt': receipt_path}


def validate_profile_shader_identities(profile_path: Path, shader_identity_path: Path) -> dict[str, ShaderIdentity]:
    profile = object_value(json.loads(profile_path.read_text(encoding='utf-8-sig')), 'generated material profile')
    if profile.get('format') != PROFILE_FORMAT:
        raise ValueError(f'Unsupported generated character material profile: {profile_path}')
    embedded = object_value(profile.get('shader_identities'), 'profile.shader_identities')
    actual = read_shader_identities(shader_identity_path)
    if embedded != actual:
        raise ValueError('Generated material profile Shader identities differ from the current per-export identity file')
    artifact_record = object_value(profile.get('source_artifacts'), 'profile.source_artifacts')
    expected_identity_sha = sha256_value(artifact_record.get('shader_identity_sha256'), 'profile.shader_identity_sha256')
    actual_identity_sha = file_sha256(shader_identity_path)
    if expected_identity_sha != actual_identity_sha:
        raise ValueError('Generated material profile Shader identity artifact changed after profile preparation')
    return actual


def profile_shader_identity(profile_path: Path, family: str) -> ShaderIdentity:
    profile = object_value(json.loads(profile_path.read_text(encoding='utf-8-sig')), 'generated material profile')
    if profile.get('format') != PROFILE_FORMAT:
        raise ValueError(f'Unsupported generated character material profile: {profile_path}')
    identities = object_value(profile.get('shader_identities'), 'profile.shader_identities')
    identity = object_value(identities.get(family), 'profile Shader identity ' + family)
    result: ShaderIdentity = {
        'source': text_value(identity.get('source'), family + '.source'),
        'path_id': integer_value(identity.get('path_id'), family + '.path_id'),
        'type': text_value(identity.get('type'), family + '.type'),
        'name': text_value(identity.get('name'), family + '.name'),
        'object_sha256': sha256_value(identity.get('object_sha256'), family + '.object_sha256'),
    }
    if result['name'] != family or result['type'] != 'Shader':
        raise ValueError(f'Profile Shader identity does not match requested family: {family}')
    return result


def profile_shader_object_sha256(profile_path: Path, family: str) -> str:
    return profile_shader_identity(profile_path, family)['object_sha256']


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shader-identities', required=True, type=Path)
    parser.add_argument('--weather-manifest', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--template', required=True, type=Path)
    args = parser.parse_args()
    result = create_export_material_profile(args.shader_identities, args.weather_manifest, args.output_dir, args.template)
    print(json.dumps({key: str(value) for key, value in result.items()}), flush=True)


if __name__ == '__main__':
    main()
