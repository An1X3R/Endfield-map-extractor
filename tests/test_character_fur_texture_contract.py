"""Validate optional Fur direction texture admission from explicit source fields."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from prepare_character_fur_curves import source_material_requirements


class FurSourceTextureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.material = {'data': {'m_SavedProperties': {
            'm_TexEnvs': [
                {'Key': '_BaseMap', 'Value': {'m_Texture': {'m_FileID': 2, 'm_PathID': 100}, 'm_UVSetIndex': 0}},
                {'Key': '_FurDirMap', 'Value': {'m_Texture': {'m_FileID': 0, 'm_PathID': 0}, 'm_UVSetIndex': 7}},
            ],
            'm_Floats': [{'Key': key, 'Value': value} for key, value in (
                ('_FurDirMapEnable', 0.0), ('_FurLengthIntensity', 1.66),
                ('_FurGravityStrength', 0.0), ('_DisableRainEffectOnMaterial', 0.0))],
        }}}
        self.shader = {'property_defaults': [
            {'m_Name': '_FurDirMap', 'm_Type': 4, 'm_DefTexture': {'m_DefaultName': 'bump', 'm_TexDim': 2}},
            {'m_Name': '_FurDirMapEnable', 'm_Type': 2},
        ]}

    def test_disabled_null_bump_uses_base_uv_without_mutating_source(self) -> None:
        before = deepcopy(self.material)
        uv, values = source_material_requirements(self.material, self.shader, 'source material')
        self.assertEqual(uv, 0)
        self.assertEqual(values['_FurLengthIntensity'], 1.66)
        self.assertEqual(self.material, before)

    def test_enabled_null_direction_fails(self) -> None:
        self.material['data']['m_SavedProperties']['m_Floats'][0]['Value'] = 1.0
        with self.assertRaisesRegex(ValueError, 'disabled directional flow'):
            source_material_requirements(self.material, self.shader, 'source material')

    def test_unproven_or_changed_shader_default_fails(self) -> None:
        for default in ('white', 'black', ''):
            with self.subTest(default=default):
                self.shader['property_defaults'][0]['m_DefTexture']['m_DefaultName'] = default
                with self.assertRaises((ValueError, TypeError)):
                    source_material_requirements(self.material, self.shader, 'source material')

    def test_real_direction_texture_keeps_the_shared_uv_requirement(self) -> None:
        self.material['data']['m_SavedProperties']['m_TexEnvs'][1]['Value']['m_Texture']['m_PathID'] = 101
        with self.assertRaisesRegex(ValueError, 'UV sets differ'):
            source_material_requirements(self.material, self.shader, 'source material')

    def test_changed_switch_property_type_fails(self) -> None:
        self.shader['property_defaults'][1]['m_Type'] = 1
        with self.assertRaisesRegex(ValueError, 'Shader Float switch'):
            source_material_requirements(self.material, self.shader, 'source material')


if __name__ == '__main__':
    unittest.main()
