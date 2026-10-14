"""Validate the source-bound character export request contract."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from export_character import read_request


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

    def test_rejects_previous_request_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request_path = Path(directory) / 'request.json'
            request_path.write_text(json.dumps({'format': 'EndfieldCharacterExportRequest/1'}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Unsupported character export request format'):
                read_request(request_path)


if __name__ == '__main__':
    unittest.main()
