"""Validate the source-bound character export request contract."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from export_character import build_arguments, read_request, write_material_options


class CharacterExportRequestTests(unittest.TestCase):
    def test_resolves_explicit_weather_manifest_and_local_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game_root = root / 'game'
            game_root.mkdir()
            fields = (
                'repository_src', 'prepare_script', 'python', 'blender', 'static_reader',
                'shader_reader', 'visibility_reader', 'catalog', 'database', 'cab_inventory',
                'weather_manifest',
            )
            for name in fields:
                path = root / (name + '.bin')
                path.write_bytes(b'fixture')
            request_path = root / 'request.json'
            document = {
                'format': 'EndfieldCharacterExportRequest/2',
                **{name: name + '.bin' for name in fields},
                'game_root': 'game',
                'output': 'export',
                'variant': 'characters',
                'character_id': 'chr-test',
                'template_id': 'template-test',
                'part_name': 'Body',
                'resource_path': 'assets/test/model.fbx',
                'provider': 'test-provider',
                'unity_version': '2021.3.34f5',
                'head_bone': 'Head',
                'face_right': [1, 0, 0],
                'face_up': [0, 0, 1],
                'face_forward': [0, 1, 0],
                'hair_x': [1, 0, 0],
                'hair_y': [0, 1, 0],
                'hair_z': [0, 0, 1],
            }
            request_path.write_text(json.dumps(document), encoding='utf-8')
            request = read_request(request_path)
            self.assertEqual(request.weather_manifest, (root / 'weather_manifest.bin').resolve())
            self.assertEqual(request.output, (root / 'export').resolve())
            self.assertIsNone(request.material_options)
            geometry_dir = root / 'prepared/static_probe'
            geometry_dir.mkdir(parents=True)
            (geometry_dir / 'model.fbx').write_bytes(b'fbx')
            legacy_args = build_arguments(request, root / 'prepared', root / 'build', write_material_options(request))
            self.assertNotIn('--material-options', legacy_args)

            with_materials = {**document, 'output': 'simplified_export', 'material_options': {
                'mode': 'simplified', 'lighting': 'simple', 'weather': {'quality': 'full', 'effects': ['rain', 'wet']}}}
            simplified_path = root / 'simplified_request.json'
            simplified_path.write_text(json.dumps(with_materials), encoding='utf-8')
            simplified_request = read_request(simplified_path)
            simplified_request.output.mkdir()
            options_path = write_material_options(simplified_request)
            self.assertIsNotNone(options_path)
            simplified_args = build_arguments(simplified_request, root / 'prepared', root / 'build', options_path)
            self.assertEqual(simplified_args[-2:], ('--material-options', str(options_path)))
            self.assertEqual(json.loads(options_path.read_text(encoding='utf-8')),
                {'mode': 'simplified', 'lighting': 'simple', 'weather': {'quality': 'full', 'effects': ['wet', 'rain']}})

    def test_rejects_previous_request_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request_path = Path(directory) / 'request.json'
            request_path.write_text(json.dumps({'format': 'EndfieldCharacterExportRequest/1'}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Unsupported character export request format'):
                read_request(request_path)


if __name__ == '__main__':
    unittest.main()
