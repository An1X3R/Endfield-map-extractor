"""Validate portable per-export character material profile generation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from character_material_profile import (
    PROFILE_FORMAT,
    STANDARD_SHADER,
    VERIFIED_STANDARD_SHADER_SHA256,
    WEATHER_SLOTS,
    create_export_material_profile,
    profile_shader_identity,
    read_weather_textures,
    validate_shadow_alpha_compatibility,
    validate_profile_shader_identities,
)

EXPECTED_STANDARD_SHADER_SHA256 = '2ee8e1b24874feaeec421f38f65e28647af60fbaab059f1f6f674dcb18745781'


class CharacterMaterialProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sample = self.root / 'sample'
        self.source_audit = self.sample / 'static_probe' / 'source_audit.json'
        self.shader_identity_path = self.sample / 'shader_identity' / 'shader_identity.json'
        self.weather_root = self.root / 'weather'
        self.weather_manifest_path = self.weather_root / 'character_weather_textures.json'
        self.output = self.root / 'material_profile'
        self.template_path = Path(__file__).resolve().parents[1] / 'src' / 'character_runtime' / 'character_material_profile_template.json'
        self._write_shader_identity()
        self._write_weather_manifest()

    def _write_shader_identity(self) -> None:
        self.source_audit.parent.mkdir(parents=True, exist_ok=True)
        self.source_audit.write_text('{"format":"test"}\n', encoding='utf-8')
        audit_hash = hashlib.sha256(self.source_audit.read_bytes()).hexdigest()
        self.shader_identity_path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        self.assertEqual(VERIFIED_STANDARD_SHADER_SHA256, EXPECTED_STANDARD_SHADER_SHA256)
        for index, name in enumerate((STANDARD_SHADER, 'HGRP/CharacterNPR_Skin', 'HGRP/CharacterNPR_Hair')):
            object_hash = EXPECTED_STANDARD_SHADER_SHA256 if name == STANDARD_SHADER else hashlib.sha256(name.encode()).hexdigest()
            rows.append({
                'shader': {'source': 'CAB-test-character', 'path_id': index + 100, 'type': 'Shader', 'name': name},
                'object_sha256': object_hash,
            })
        document = {
            'format': 'EndfieldCharacterShaderIdentity/1',
            'source_audit_sha256': audit_hash,
            'shaders': rows,
        }
        self.shader_identity_path.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')

    def _write_weather_manifest(self) -> None:
        artifact_root = self.weather_root / 'weather_textures'
        artifact_root.mkdir(parents=True)
        textures = []
        for index, slot in enumerate(WEATHER_SLOTS):
            content = f'png-test-{slot}'.encode('ascii')
            artifact = artifact_root / f'{slot}.png'
            artifact.write_bytes(content)
            textures.append({
                'slot': slot,
                'source': 'CAB-weather-test',
                'path_id': index - 2,
                'width': 4,
                'height': 4,
                'texture_format': 'BC7',
                'color_space': 0,
                'vertical_flip': True,
                'artifact': f'weather_textures/{artifact.name}',
                'bytes': len(content),
                'sha256': hashlib.sha256(content).hexdigest(),
            })
        document = {
            'format': 'EndfieldCharacterWeatherTextures/1',
            'status': 'prepared',
            'unity_version': '2021.3.34f5',
            'pipeline_resource_path': 'assets/test/render-pipeline.asset',
            'provider_logical_name': 'initial/test.ab',
            'root_source': 'CAB-weather-test',
            'source_evidence': {
                key: hashlib.sha256(key.encode()).hexdigest()
                for key in (
                    'provider_index_sha256', 'resource_database_sha256', 'cab_inventory_sha256',
                    'configuration_input_sha256', 'configuration_typetrees_sha256',
                    'configuration_receipt_sha256', 'texture_input_sha256', 'target_list_sha256',
                    'texture_probe_receipt_sha256', 'probe_texture_manifest_sha256',
                )
            },
            'textures': textures,
        }
        self.weather_manifest_path.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')

    def test_generates_portable_profile_and_local_verified_texture_manifest(self) -> None:
        result = create_export_material_profile(
            self.shader_identity_path, self.weather_manifest_path, self.output, self.template_path)
        profile = json.loads(result['profile'].read_text(encoding='utf-8'))
        self.assertEqual(profile['format'], PROFILE_FORMAT)
        self.assertEqual(profile['shader_identities'][STANDARD_SHADER]['object_sha256'], VERIFIED_STANDARD_SHADER_SHA256)
        self.assertEqual(profile['standard_wet_film']['texture_manifest'], 'weather_texture_artifacts.json')
        self.assertEqual(profile['standard_snow']['texture_slot'], 'characterSnowTexture')
        self.assertIn('skin_rain', profile)
        self.assertIn('hair', profile['hair_fur_snow'])
        self.assertIn('fur', profile['hair_fur_snow'])
        identities = validate_profile_shader_identities(result['profile'], self.shader_identity_path)
        self.assertEqual(set(identities), {STANDARD_SHADER, 'HGRP/CharacterNPR_Skin', 'HGRP/CharacterNPR_Hair'})
        self.assertEqual(profile_shader_identity(result['profile'], STANDARD_SHADER)['object_sha256'],
                         VERIFIED_STANDARD_SHADER_SHA256)
        textures = json.loads((self.output / 'weather_texture_artifacts.json').read_text(encoding='utf-8'))
        self.assertEqual({row['slot'] for row in textures}, set(WEATHER_SLOTS))
        for row in textures:
            copied = (self.output / row['artifact']).resolve(strict=True)
            self.assertEqual(copied.stat().st_size, row['bytes'])
            self.assertEqual(hashlib.sha256(copied.read_bytes()).hexdigest(), row['sha256'])
        alpha_audit = json.loads(result['alpha_audit'].read_text(encoding='utf-8'))
        self.assertEqual(alpha_audit['shaderIdentity']['serializedObjectSha256'], VERIFIED_STANDARD_SHADER_SHA256.upper())
        self.assertEqual(alpha_audit['compatibility']['status'], 'verified_reference_object')
        self.assertTrue(alpha_audit['compatibility']['native_shader_semantics_verified'])
        combined = '\n'.join(path.read_text(encoding='utf-8') for path in (result['profile'], result['alpha_audit'], result['receipt']))
        self.assertNotIn(str(self.template_path), combined)
        self.assertNotIn('ruri_glsl', combined)

    def test_reads_weather_manifest_schema_and_all_five_artifacts(self) -> None:
        evidence, rows = read_weather_textures(self.weather_manifest_path)
        self.assertEqual(evidence['root_source'], 'CAB-weather-test')
        self.assertEqual(len(rows), 5)
        self.assertEqual({row['slot'] for row, _path in rows}, set(WEATHER_SLOTS))

    def test_rejects_changed_weather_texture_before_creating_output(self) -> None:
        texture = self.weather_root / 'weather_textures' / f'{WEATHER_SLOTS[0]}.png'
        texture.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'byte count mismatch|SHA-256 mismatch'):
            create_export_material_profile(
                self.shader_identity_path, self.weather_manifest_path, self.output, self.template_path)
        self.assertFalse(self.output.exists())

    def test_rejects_changed_shader_identity_after_profile_generation(self) -> None:
        result = create_export_material_profile(
            self.shader_identity_path, self.weather_manifest_path, self.output, self.template_path)
        document = json.loads(self.shader_identity_path.read_text(encoding='utf-8'))
        document['shaders'][1]['object_sha256'] = '0' * 64
        self.shader_identity_path.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'differ from the current per-export identity file|artifact changed'):
            validate_profile_shader_identities(result['profile'], self.shader_identity_path)

    def test_rejects_changed_standard_shader_without_source_contract_before_creating_output(self) -> None:
        document = json.loads(self.shader_identity_path.read_text(encoding='utf-8'))
        document['shaders'][0]['object_sha256'] = '1' * 64
        self.shader_identity_path.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'requires its source property contract'):
            create_export_material_profile(
                self.shader_identity_path, self.weather_manifest_path, self.output, self.template_path)
        self.assertFalse(self.output.exists())


    def _changed_shader_document(self) -> dict[str, object]:
        document = json.loads(self.shader_identity_path.read_text(encoding='utf-8'))
        document['shaders'][0].update({
            'object_sha256': '1' * 64,
            'property_defaults': [
                {'m_Name': '_BaseMap', 'm_Type': 4, 'm_DefTexture': {'m_TexDim': 2}},
                {'m_Name': '_BaseColor', 'm_Type': 0},
                {'m_Name': '_SurfaceType', 'm_Type': 2},
                {'m_Name': '_EnableAlphaTest', 'm_Type': 2, 'm_Attributes': ['Toggle(_ALPHATEST_ON)']},
                {'m_Name': '_AlphaClipThreshold', 'm_Type': 3},
            ],
        })
        return document

    def test_changed_shader_contract_preserves_source_identity_without_native_semantic_claim(self) -> None:
        document = self._changed_shader_document()
        self.shader_identity_path.write_text(json.dumps(document), encoding='utf-8')
        result = create_export_material_profile(
            self.shader_identity_path, self.weather_manifest_path, self.output, self.template_path)
        profile = json.loads(result['profile'].read_text(encoding='utf-8'))
        alpha = json.loads(result['alpha_audit'].read_text(encoding='utf-8'))
        receipt = json.loads(result['receipt'].read_text(encoding='utf-8'))
        compatibility = validate_shadow_alpha_compatibility(alpha, document['shaders'][0])
        self.assertEqual(compatibility['status'], 'host_approximation_source_contract')
        self.assertFalse(compatibility['native_shader_semantics_verified'])
        self.assertTrue(compatibility['property_contract_verified'])
        self.assertIn('have not been audited', compatibility['provenance'])
        self.assertEqual(profile['standard_shader_compatibility'], compatibility)
        self.assertEqual(receipt['standard_shader_compatibility'], compatibility)
        self.assertEqual(profile_shader_identity(result['profile'], STANDARD_SHADER)['object_sha256'], '1' * 64)
        self.assertEqual(alpha['shaderIdentity']['serializedObjectSha256'], '1' * 64)

    def test_rejects_incompatible_changed_shader_contract_before_creating_output(self) -> None:
        for case in ('missing', 'type', 'duplicate', 'dimension', 'keyword'):
            with self.subTest(case=case):
                document = self._changed_shader_document()
                properties = document['shaders'][0]['property_defaults']
                if case == 'missing':
                    properties.pop()
                elif case == 'type':
                    properties[1]['m_Type'] = 1
                elif case == 'duplicate':
                    properties.append(properties[0].copy())
                elif case == 'dimension':
                    properties[0]['m_DefTexture']['m_TexDim'] = 3
                else:
                    properties[3]['m_Attributes'] = ['Toggle(_OTHER_KEYWORD)']
                self.shader_identity_path.write_text(json.dumps(document), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'property|2D source texture|source toggle'):
                    create_export_material_profile(
                        self.shader_identity_path, self.weather_manifest_path, self.output, self.template_path)
                self.assertFalse(self.output.exists())

    def test_legacy_alpha_evidence_accepts_only_the_reference_object(self) -> None:
        document = json.loads(self.shader_identity_path.read_text(encoding='utf-8'))
        compatibility = validate_shadow_alpha_compatibility({}, document['shaders'][0])
        self.assertEqual(compatibility['status'], 'verified_reference_object')
        changed = self._changed_shader_document()
        with self.assertRaisesRegex(ValueError, 'requires explicit host approximation'):
            validate_shadow_alpha_compatibility({}, changed['shaders'][0])

    def test_changed_shader_cannot_claim_verified_native_semantics(self) -> None:
        document = self._changed_shader_document()
        audit = {'compatibility': {
            'status': 'verified_reference_object', 'native_shader_semantics_verified': True,
            'property_contract_verified': True, 'provenance': 'Claimed verification',
        }}
        with self.assertRaisesRegex(ValueError, 'differs from the actual Shader'):
            validate_shadow_alpha_compatibility(audit, document['shaders'][0])


if __name__ == '__main__':
    unittest.main()
