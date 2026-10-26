"""Validate repeated clip references without conflating pointer identity and slot reuse."""
from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from character_runtime.resolve_character_controller import ControllerSourceError, clip_reference_matches
from character_runtime.character_animation_parameters import _controller_names


class ControllerReferenceTests(unittest.TestCase):
    def test_reused_clip_retains_every_controller_slot(self) -> None:
        result = clip_reference_matches({'m_AnimationClips': [
            {'m_FileID': 1, 'm_PathID': 17}, {'m_FileID': 1, 'm_PathID': 18},
            {'m_FileID': 1, 'm_PathID': 17}]},
            {'cab': 'cab-controller', 'external_cabs': ['cab-clip']},
            {'sourceFile': 'CAB-CLIP', 'pathId': 17})
        self.assertEqual([row['controller_slot'] for row in result], [0, 2])
        self.assertTrue(all(row['path_id'] == 17 and row['cab'] == 'cab-clip' for row in result))

    def test_path_id_alone_cannot_match_another_cab(self) -> None:
        result = clip_reference_matches({'m_AnimationClips': [{'m_FileID': 0, 'm_PathID': 17}]},
            {'cab': 'cab-controller', 'external_cabs': []}, {'sourceFile': 'cab-clip', 'pathId': 17})
        self.assertEqual(result, ())

    def test_invalid_external_index_remains_an_error(self) -> None:
        with self.assertRaisesRegex(ControllerSourceError, 'invalid external FileID'):
            clip_reference_matches({'m_AnimationClips': [{'m_FileID': 2, 'm_PathID': 17}]},
                {'cab': 'cab-controller', 'external_cabs': ['cab-clip']},
                {'sourceFile': 'cab-clip', 'pathId': 17})

    def test_parameter_names_accept_reused_verified_pointer(self) -> None:
        with TemporaryDirectory() as directory:
            native = Path(directory)
            (native / 'animator_parameter_verification.json').write_text(json.dumps({
                'status': 'exact_controller_clip_reference_and_tos_verified',
                'binding_type_id': 95, 'binding_custom_type': 0, 'controller': 'Controller',
                'clip_reference_file_id': 1, 'clip_reference_path_id': 17,
                'clip_reference_count': 2, 'parameters': []}), encoding='utf-8')
            (native / 'controller_type_tree.json').write_text(json.dumps({
                'm_Name': 'Controller', 'm_TOSData': [], 'm_AnimationClips': [
                    {'m_FileID': 1, 'm_PathID': 17}, {'m_FileID': 1, 'm_PathID': 17}]}),
                encoding='utf-8')
            names, evidence = _controller_names(native, {'controller_bundle': 'controller.ab'}, {'path_id': 17})
            self.assertEqual(names, {})
            self.assertEqual(evidence, native / 'animator_parameter_verification.json')
            with self.assertRaisesRegex(ValueError, 'exact verified clip pointer'):
                _controller_names(native, {'controller_bundle': 'controller.ab'}, {'path_id': 18})


if __name__ == '__main__':
    unittest.main()
