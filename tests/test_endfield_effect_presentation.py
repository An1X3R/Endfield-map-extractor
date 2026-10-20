"""Validate portable authored recipes and the existing scene-plan boundary."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from endfield_effect_presentation import FlowPresentation, read_flow_presentation
from endfield_scene_export_plan import load_scene_export_plan


def recipe() -> FlowPresentation:
    return {"format": "EndfieldFlowPresentation/1", "coordinate_space": "BLENDER_WORLD",
            "provenance": "Explicit artist review", "color": (0.1, 0.9, 0.6), "speed": 0.08,
            "opacity": 0.35, "intensity": 4.0, "streak_density": 12.0,
            "ribbons": [{"name": "Review Flow", "center": (0.0, 0.0, 1.0), "radii": (5.0, 8.0),
                         "width": 1.0, "height_wave": 0.5, "phase": 0.0, "segments": 32}]}


class FlowPresentationTests(unittest.TestCase):
    def test_json_roundtrip_and_input_ownership(self) -> None:
        original = recipe()
        source = json.loads(json.dumps(original))
        source["ignored"] = "external metadata"
        before = deepcopy(source)
        parsed = read_flow_presentation(source)
        self.assertEqual(parsed, original)
        self.assertEqual(source, before)
        parsed["ribbons"][0]["name"] = "Edited"
        self.assertEqual(source["ribbons"][0]["name"], "Review Flow")

    def test_invalid_inputs_are_rejected(self) -> None:
        for field, value in (("speed", float("nan")), ("speed", True), ("opacity", 2),
                             ("color", [1, 2]), ("streak_density", 0), ("provenance", ""),
                             ("coordinate_space", "UNITY_WORLD"), ("ribbons", [])):
            changed = {**recipe(), field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                read_flow_presentation(changed)
        for field, value in (("segments", True), ("segments", 15), ("segments", 4097),
                             ("center", [1, 2, float("inf")]), ("radii", [1, -1]), ("width", 0)):
            changed = recipe()
            changed["ribbons"] = [{**changed["ribbons"][0], field: value}]
            with self.subTest(field=field), self.assertRaises(ValueError):
                read_flow_presentation(changed)

    def test_duplicate_names_are_rejected(self) -> None:
        changed = recipe()
        changed["ribbons"] *= 2
        with self.assertRaisesRegex(ValueError, "unique"):
            read_flow_presentation(changed)

    def test_scene_plan_requires_explicit_effect_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sidecar = root / "empty.json"
            sidecar.write_text("{}", encoding="utf-8")
            payload = {"format": "EndfieldPortableScenePlan/1", "map_id": "map02",
                       "bounds": {"xmin": 0, "xmax": 10, "zmin": 0, "zmax": 10},
                       "scene_manifests": [], "terrain_surfaces": [], "category_filter": ["effects"],
                       "decal_manifest": str(sidecar), "decal_materials": str(sidecar),
                       "instances": [], "pending": [], "source_sha256": {},
                       "summary": {"selected_static": 0, "ready_static": 0, "selected_decals": 0,
                                   "ready_decals": 0, "pending": 0}, "effect_flow_presentation": recipe()}
            path = root / "plan.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(load_scene_export_plan(path)["effect_flow_presentation"], recipe())
            payload["category_filter"] = ["terrain"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "effects export group"):
                load_scene_export_plan(path)
            del payload["effect_flow_presentation"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotIn("effect_flow_presentation", load_scene_export_plan(path))
