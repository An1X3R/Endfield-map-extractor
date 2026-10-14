from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import struct
import tempfile
import unittest

from endfield_scene_memory import (SceneMemoryCalibration, SceneMemoryWorkload,
    cached_scene_memory_catalog, merge_scene_memory_calibrations, read_scene_memory_calibrations, scene_memory_catalog,
    scene_memory_catalog_key, scene_memory_estimate, scene_memory_workload, write_scene_memory_calibrations)


class SceneMemoryTests(unittest.TestCase):
    def test_measured_envelope_scales_the_dominant_workload_and_unknown_phases_stay_solo(self) -> None:
        measured = SceneMemoryWorkload("a" * 64, (("mesh", 10), ("texture", 100), ("terrain", 0)))
        sample = SceneMemoryCalibration("b" * 64, measured, 1024 ** 3, 900 * 1024 ** 2, True)
        target = replace(measured, input_signature="c" * 64, features=(("mesh", 20), ("texture", 100), ("terrain", 0)))
        estimate = scene_memory_estimate(target, (sample,), "b" * 64)
        self.assertTrue(estimate.calibrated)
        self.assertEqual(estimate.extrapolation_factor, 2.0)
        self.assertEqual(estimate.budget_bytes, 2560 * 1024 ** 2)
        unseen = replace(target, features=(("mesh", 20), ("texture", 100), ("terrain", 1)))
        self.assertFalse(scene_memory_estimate(unseen, (sample,), "b" * 64).calibrated)
        self.assertFalse(scene_memory_estimate(target, (sample,), "d" * 64).calibrated)

    def test_cache_keeps_maximum_peaks_and_rejects_incompatible_tools(self) -> None:
        workload = SceneMemoryWorkload("a" * 64, (("mesh", 10),))
        higher = SceneMemoryCalibration("b" * 64, workload, 1024 ** 3, 1200 * 1024 ** 2, True)
        lower = replace(higher, peak_working_set_bytes=700 * 1024 ** 2, peak_commit_bytes=800 * 1024 ** 2)
        self.assertEqual(merge_scene_memory_calibrations((higher, lower)), (higher,))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calibration.json"
            write_scene_memory_calibrations(path, (higher, lower))
            self.assertEqual(read_scene_memory_calibrations(path, "b" * 64), (higher,))
            with self.assertLogs("endfield_scene_memory", level="WARNING"):
                self.assertEqual(read_scene_memory_calibrations(path, "c" * 64), ())
            write_scene_memory_calibrations(path, tuple(replace(higher, workload=replace(workload, input_signature=f"{index:064x}"))
                                                         for index in range(40)))
            self.assertEqual(len(read_scene_memory_calibrations(path, "b" * 64)), 32)

    def test_an_exact_observed_peak_cannot_be_undercut_by_another_sample_envelope(self) -> None:
        exact = SceneMemoryWorkload("a" * 64, (("mesh", 10),))
        slow = SceneMemoryCalibration("b" * 64, exact, 4 * 1024 ** 3, 3 * 1024 ** 3, True)
        other = SceneMemoryCalibration("b" * 64, replace(exact, input_signature="c" * 64), 1024 ** 3, 1024 ** 3, True)
        estimate = scene_memory_estimate(exact, (slow, other), "b" * 64)
        self.assertEqual(estimate.budget_bytes, 5 * 1024 ** 3)
        self.assertEqual(estimate.basis_input_signature, exact.input_signature)

    def test_workload_counts_selected_pngs_and_mesh_variants_and_validates_descriptor_identity(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, width in (("selected.png", 8), ("unselected.png", 64)):
                (root / name).write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, 4))
            material = {"Name": "Material", "Source": "CAB-M", "PathId": 1,
                        "Shader": {"Source": "CAB-S", "PathId": 2}, "Floats": {}, "Colors": {},
                        "Textures": [{"Property": "_BaseMap", "Texture": {"Source": "CAB-T", "PathId": 3},
                                      "Scale": [1, 1], "Offset": [0, 0]}]}
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Materials": [material],
                "TextureExports": [{"Asset": {"Source": "CAB-T", "PathId": 3}, "File": "selected.png"},
                                   {"Asset": {"Source": "CAB-T", "PathId": 4}, "File": "unselected.png"}]}), encoding="utf-8")
            mesh = root / "mesh.obj"
            mesh.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
            empty = root / "empty.json"
            empty.write_text("{}", encoding="utf-8")
            arrays = ("instances", "dynamic_geometry_instances", "water_depth_instances", "surface_effect_instances",
                      "effect_instances", "direct_effect_renderers", "water_geometry", "water_materials", "light_instances",
                      "terrain_surfaces", "water_depth_surfaces", "terrain_details")
            data: dict[str, object] = {field: [] for field in arrays}
            row = {"mesh_file": str(mesh), "material_refs": [{"Source": "CAB-M", "PathId": 1}]}
            data.update({"format": "EndfieldPortableScenePlan/1", "scene_manifests": [str(manifest)],
                         "decal_manifest": str(empty), "decal_materials": str(empty), "instances": [row, row],
                         "water_depth_instances": [row]})
            plan = root / "plan.json"
            plan.write_text(json.dumps(data), encoding="utf-8")
            catalog = scene_memory_catalog(scene_memory_catalog_key(plan))
            persisted = cached_scene_memory_catalog(root / "cache", scene_memory_catalog_key(plan), "a" * 64)
            self.assertEqual(persisted, catalog)
            cached_path = next((root / "cache" / "scene_memory_catalogs").glob("*.json"))
            cached_bytes = cached_path.read_bytes()
            self.assertEqual(cached_scene_memory_catalog(root / "cache", scene_memory_catalog_key(plan), "a" * 64), catalog)
            self.assertEqual(cached_path.read_bytes(), cached_bytes)
            workload = scene_memory_workload(plan, catalog)
            features = dict(workload.features)
            self.assertEqual(features["mesh_variant_count"], 2)
            self.assertEqual(features["mesh_variant_source_bytes"], mesh.stat().st_size * 2)
            self.assertEqual(features["instance_count"], 3)
            self.assertEqual(features["texture_file_count"], 1)
            self.assertEqual(features["texture_float_rgba_bytes"], 8 * 4 * 16)
            (root / "selected.png").write_bytes((root / "selected.png").read_bytes() + b"changed")
            self.assertNotEqual(workload.input_signature, scene_memory_workload(plan, catalog).input_signature)
            manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "descriptor does not match"):
                scene_memory_workload(plan, catalog)


if __name__ == "__main__":
    unittest.main()
