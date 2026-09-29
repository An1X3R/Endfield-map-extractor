"""Integration checks for versioned FX prefab evidence and bounded anchors."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endfield_scene_effects_plan import build_effect_plan, observed_effect_prefabs
from extractor_core.effects import EffectRecord, EffectSource, MapEffectInventory
from extractor_core.particle_snapshot import PARTICLE_PARAMETER_MODULE_FIELDS, PARTICLE_PARAMETER_SCHEMA_VERSION


PREFAB = "assets/effects/p_fx.prefab"


def _manifest(version: int) -> dict[str, object]:
    initial: dict[str, object] = {}
    if version == PARTICLE_PARAMETER_SCHEMA_VERSION:
        initial = {"StartRotationX": {}, "StartRotationY": {}, "StartRotation": {},
                   "GravityModifier": {}, "RandomizeRotationDirection": 0.0, "Rotation3D": False}
    return {
        "Format": "EndfieldSceneProbe/1",
        "Containers": [{"Container": PREFAB, "Asset": {"Source": "CAB-root", "PathId": 1,
                                                     "Type": "GameObject", "Name": "P_fx"}}],
        "Nodes": [
            {"Source": "CAB-root", "PathId": 1, "TransformPathId": 10,
             "Parent": {"Source": "CAB-root", "PathId": 0, "Type": "Transform"}},
            {"Source": "CAB-root", "PathId": 2, "TransformPathId": 20,
             "Parent": {"Source": "CAB-root", "PathId": 10, "Type": "Transform"}},
        ],
        "Particles": {
            "DataScope": "component_parameters_no_simulation", "ModuleDataScope": "typed_modules",
            "Systems": [{"Asset": {"Source": "CAB-root", "PathId": 3, "Type": "ParticleSystem"},
                         "Parameters": {**{field: {} for field in PARTICLE_PARAMETER_MODULE_FIELDS},
                                        "SchemaVersion": version, "Initial": initial}}],
            "Renderers": [{"Asset": {"Source": "CAB-root", "PathId": 4, "Type": "ParticleSystemRenderer"},
                           "Materials": [{"IsResolved": True, "ResolutionStatus": "resolved",
                                          "Target": {"Source": "CAB-material", "PathId": 5, "Type": "Material"}}]}],
            "Pairs": [{"GameObject": {"Source": "CAB-root", "PathId": 2, "Type": "GameObject"},
                       "System": {"Source": "CAB-root", "PathId": 3, "Type": "ParticleSystem"},
                       "Renderer": {"Source": "CAB-root", "PathId": 4, "Type": "ParticleSystemRenderer"}}],
            "Diagnostics": [],
        },
    }


class EffectPlanIntegrationTests(unittest.TestCase):
    def test_current_schema_wins_over_older_snapshot_for_same_prefab(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "a_old.json"
            current = root / "z_current.json"
            old.write_text(json.dumps(_manifest(2)), encoding="utf-8")
            current.write_text(json.dumps(_manifest(PARTICLE_PARAMETER_SCHEMA_VERSION)), encoding="utf-8")
            source = "Data/DynamicStreaming/PC/Scene/map01/fb_main_1_0000_0000.bytes"
            inventory = MapEffectInventory("map01", (EffectSource(source, "A" * 32, 0, 1, 1),),
                                           (EffectRecord(source + ":0:0", source, 0, 0, 0,
                                                         "0" * 16, PREFAB, (0.0, 2.0, 0.0),
                                                         (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),), (PREFAB,))
            materials = {("cab-material", 5): {
                "Name": "M_fx", "Source": "CAB-material", "PathId": 5,
                "Shader": {"Source": "CAB-shader", "PathId": 6, "Name": "HGRP/Effect/VFXBase"},
                "Textures": [], "Floats": {}, "Colors": {},
            }}
            bounds = {"xmin": 0.0, "xmax": 1.0, "zmin": 0.0, "zmax": 1.0}
            self.assertNotIn(PREFAB, observed_effect_prefabs((old,)))
            self.assertIn(PREFAB, observed_effect_prefabs((current,)))
            result = build_effect_plan(inventory, bounds, (old, current), materials, {})
            self.assertEqual((result.selected_count, len(result.rows), len(result.pending)), (1, 1, 0))
            self.assertEqual(result.rows[0]["scene_manifest"], str(current.resolve()))
            self.assertEqual(result.rows[0]["matrix"][12:15], [0.0, 2.0, 0.0])
            old_only = build_effect_plan(inventory, bounds, (old,), materials, {})
            self.assertEqual(old_only.pending[0]["reason"], "particle_parameter_schema_unsupported")


if __name__ == "__main__":
    unittest.main()
