"""Verify ordered renderer-slot recovery against actual serialized probe fixtures."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endfield_scene_slot_contract import load_slot_plans, load_retained_mesh_slot_plans
from endfield_scene_slot_contract import AmbiguousSubmeshSlots, static_submesh_slots
from endfield_scene_slot_contract import mesh_lod_index
from endfield_scene_slot_contract import RuntimeDraw, runtime_submesh_slots


def write_inputs(root: Path, sequences: list[list[list[str]]], failures: list[dict[str, str]]) -> tuple[Path, Path]:
    mesh = {"Source": "CAB-mesh", "PathId": 1, "Name": "model_lod0", "Type": "Mesh"}
    material = {"Source": "CAB-material", "PathId": 2, "Type": "Material"}
    (root / "model.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\ng submesh_0\nf 1 2 3\n", encoding="utf-8")
    probe = root / "probe.json"
    probe.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Containers": [
        {"Container": "assets/model.fbx", "Asset": mesh}, {"Container": "assets/surface.mat", "Asset": material}],
        "MeshExports": [{"Asset": mesh, "File": "model.obj"}]}), encoding="utf-8")
    renderer = root / "renderers.json"
    renderer.write_text(json.dumps({"format": "EndfieldCurrentRendererMaterials/1", "failures": failures,
        "bases": {"P_model": [{"pairs": sequence, "count": 1} for sequence in sequences]}}), encoding="utf-8")
    return renderer, probe


class SceneMaterialSlotTests(unittest.TestCase):
    def test_native_draw_roles_preserve_undrawn_groups_and_separate_shadow_materials(self) -> None:
        lamp = runtime_submesh_slots(3, ("lamp02", "lamp01"),
                                     (RuntimeDraw(0, 200802304), RuntimeDraw(1, 200802304)))
        self.assertEqual((0, 1, 2), lamp.indices)
        self.assertEqual((2,), lamp.undrawn_submeshes)
        pine = runtime_submesh_slots(2, ("bark02", "leaf02", "bark01", "leaf02"),
                                     (RuntimeDraw(0, 134217728), RuntimeDraw(1, 134217728),
                                      RuntimeDraw(0, 200803328), RuntimeDraw(1, 200803328)))
        self.assertEqual((0, 1), pine.indices)
        self.assertEqual((2, 3), pine.shadow_only_draws)
        with self.assertRaisesRegex(AmbiguousSubmeshSlots, "Conflicting non-shadow"):
            runtime_submesh_slots(1, ("bark02", "bark01"), (RuntimeDraw(0, 256), RuntimeDraw(0, 256)))
        with self.assertRaisesRegex(AmbiguousSubmeshSlots, "No non-shadow"):
            runtime_submesh_slots(1, ("shadow",), (RuntimeDraw(0, 1024),))

    def test_explicit_lod_tokens_survive_processing_suffixes(self) -> None:
        for name, expected in (("tree_lod0", 0), ("tree_lod0_transferred", 0),
                               ("tree_LOD12_processed", 12), ("tree_shadowproxy0_transferred", None),
                               ("tree_cardmesh", None), ("tree_lodging", None)):
            with self.subTest(name=name):
                self.assertEqual(expected, mesh_lod_index(name))
        with self.assertRaisesRegex(ValueError, "multiple LOD tokens"):
            mesh_lod_index("tree_lod0_lod1")

    def test_static_repetitions_preserve_material_choices_and_reject_conflicts(self) -> None:
        for count, references, expected in (
            (3, ('a', 'a', 'b'), (0, 1, 2)),
            (3, ('a',), (0, 0, 0)),
            (2, ('a', 'b', 'a', 'b'), (0, 1)),
            (2, ('a', 'b', 'b'), (0, 1)),
        ):
            with self.subTest(references=references):
                self.assertEqual(expected, static_submesh_slots(count, references).indices)
        for count, references in ((2, ('a', 'b', 'c')), (2, ('a', 'b', 'a', 'c')), (3, ('a', 'b')), (0, ('a',)), (1, ())):
            with self.subTest(references=references), self.assertRaises(AmbiguousSubmeshSlots):
                static_submesh_slots(count, references)

    def test_source_lod_evidence_precedes_cross_bundle_name_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, probe = write_inputs(root, [], [])
            data = json.loads(probe.read_text())
            material = {"Source": "CAB-material", "PathId": 2}
            data["Nodes"] = [
                {"Mesh": {"Source": "CAB-mesh", "PathId": 3, "Name": "model_lod1"}, "Materials": [material]},
                {"Mesh": {"Source": "CAB-other", "PathId": 4, "Name": "model_lod0"}, "Materials": [material, material]},
            ]
            probe.write_text(json.dumps(data), encoding="utf-8")
            plan = load_retained_mesh_slot_plans((probe,))["model_lod0"][0]
            self.assertEqual((("cab-material", 2),), plan.material_keys)
            self.assertEqual("cached_prefab_unanimous_source_lod_family", plan.evidence)

    def test_cached_lod_renderer_requires_unanimous_ordered_slots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, probe = write_inputs(root, [], [])
            data = json.loads(probe.read_text())
            material = {"Source": "CAB-material", "PathId": 2}
            data["Nodes"] = [{"Mesh": {"Source": "CAB-lod", "PathId": 3, "Name": "model_lod1"},
                              "Materials": [material, material]}]
            probe.write_text(json.dumps(data), encoding="utf-8")
            plans = load_retained_mesh_slot_plans((probe,))
            self.assertEqual((("cab-material", 2), ("cab-material", 2)), plans["model_lod0"][0].material_keys)
            self.assertEqual("cached_prefab_unanimous_exact_lod_family", plans["model_lod0"][0].evidence)
            data["Nodes"].append({"Mesh": {"Source": "CAB-lod", "PathId": 4, "Name": "model_lod2"},
                                  "Materials": [material]})
            probe.write_text(json.dumps(data), encoding="utf-8")
            self.assertNotIn("model_lod0", load_retained_mesh_slot_plans((probe,)))

    def test_conflicting_exact_mesh_renderers_are_not_replaced_by_other_lods(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, probe = write_inputs(root, [], [])
            data = json.loads(probe.read_text())
            mesh = data["MeshExports"][0]["Asset"]
            material = {"Source": "CAB-material", "PathId": 2}
            data["Nodes"] = [{"Mesh": mesh, "Materials": [material]}, {"Mesh": mesh, "Materials": [material, material]}]
            probe.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual({}, load_retained_mesh_slot_plans((probe,)))

    def test_repeated_material_slots_keep_authored_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pair = ["assets/model.fbx##model_lod0", "assets/surface.mat"]
            renderer, probe = write_inputs(root, [[pair, pair]], [])
            plans, pending = load_slot_plans(renderer, (probe,))
            self.assertEqual({}, pending)
            self.assertEqual((("cab-material", 2), ("cab-material", 2)), plans["P_model"][0].material_keys)
            self.assertEqual(root / "model.obj", plans["P_model"][0].mesh_file)

    def test_conflicting_instance_variants_are_not_flattened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pair = ["assets/model.fbx##model_lod0", "assets/surface.mat"]
            renderer, probe = write_inputs(Path(directory), [[pair], [pair, pair]], [])
            plans, pending = load_slot_plans(renderer, (probe,))
            self.assertEqual({}, plans)
            self.assertIn("P_model", pending)

    def test_failed_instance_prevents_partial_base_consensus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            renderer, probe = write_inputs(Path(directory), [[["assets/model.fbx##model_lod0", "assets/surface.mat"]]], [{"base": "P_model"}])
            plans, pending = load_slot_plans(renderer, (probe,))
            self.assertEqual({}, plans)
            self.assertEqual("current_renderer_decode_failure", pending["P_model"])


if __name__ == "__main__":
    unittest.main()
