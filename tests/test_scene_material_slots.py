"""Verify ordered renderer-slot recovery against actual serialized probe fixtures."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endfield_scene_slot_contract import load_slot_plans, load_retained_mesh_slot_plans
from endfield_scene_slot_contract import AmbiguousSubmeshSlots, static_submesh_slots
from endfield_scene_slot_contract import mesh_lod_index
from endfield_scene_slot_contract import (
    RuntimeDraw, RuntimeLodDraw, highest_detail_visible_meshes, primary_visible_mesh,
    runtime_submesh_slots, runtime_submesh_passes, validate_ordered_surface_passes,
)


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
    def test_coauthored_base_parts_require_matching_native_lod_intervals(self) -> None:
        a, b = "assets/a_lod0.fbx##a_lod0", "assets/b_lod0.fbx##b_lod0"
        high = RuntimeLodDraw(0, 134217728, 3.4028234663852886e38, .7225000262260437)
        lower = RuntimeLodDraw(0, 134217728, high.lod_min_squared, .25)
        shadow = RuntimeLodDraw(0, 200803328, high.lod_max_squared, high.lod_min_squared)
        pairs = ((a, high), (b, high), ("assets/a_lod1.fbx##a_lod1", lower),
                 ("assets/b_lod1.fbx##b_lod1", lower), ("assets/proxy.fbx##proxy", shadow))
        self.assertEqual((a, b), highest_detail_visible_meshes(pairs))
        self.assertEqual((a, b), highest_detail_visible_meshes(tuple(reversed(pairs))))
        for changed in (
            ((a, high), (b, high._replace(lod_min_squared=.8)), *pairs[2:]),
            (*pairs[:2], (pairs[2][0], lower._replace(lod_max_squared=.9)), *pairs[3:]),
            (*pairs, (a, lower)),
            ((a, high), (b, high._replace(lod_max_squared=float("inf")))),
        ):
            with self.subTest(changed=changed), self.assertRaises(AmbiguousSubmeshSlots):
                highest_detail_visible_meshes(changed)
        self.assertEqual((a,), highest_detail_visible_meshes(((a, high), pairs[2], pairs[-1])))
        sole = "assets/sole_lod1.fbx##sole_lod1"
        self.assertEqual((sole,), highest_detail_visible_meshes(((sole, high),)))

    def test_ordered_draws_preserve_every_visible_material_on_the_same_submesh(self) -> None:
        mapping = runtime_submesh_passes(3, ("tower", "wall", "metal_b", "shadow", "metal_a"),
            (RuntimeDraw(0, 200802304), RuntimeDraw(1, 200802304), RuntimeDraw(1, 200802304),
             RuntimeDraw(1, 200803328), RuntimeDraw(1, 200802304)))
        self.assertEqual(((0,), (1, 2, 4), ()), mapping.indices)
        self.assertEqual((2,), mapping.undrawn_submeshes)
        self.assertEqual((3,), mapping.shadow_only_draws)

    def test_ordered_surface_contract_rejects_unverified_depth_alpha_and_stencil(self) -> None:
        from copy import deepcopy

        record = {"Source": "CAB", "PathId": 1, "Name": "ordinary", "Colors": {}, "Textures": [],
                  "Shader": {"Source": "CAB", "PathId": 3, "Name": "HGRP/Lit", "PassNames": ["HGBuffer"]},
                  "RenderState": {"CustomRenderQueue": 2000, "DisabledShaderPasses": [], "ValidKeywords": []},
                  "Floats": {"_EnableAlphaTest": 0, "_DisableZWrite": 0, "_ZTestGBuffer": 4,
                             "_ZWriteGBuffer": 1, "_CullMode": 2, "_DitherTransparentAlpha": 1,
                             "_StencilRef": 0, "_StencilOpGBuffer": 2, "_StencilOpPreZ": 0}}
        overlay = {**record, "PathId": 2}
        validate_ordered_surface_passes((record, overlay))
        for field, value in (("_EnableAlphaTest", 1), ("_ZTestGBuffer", 8), ("_ZWriteGBuffer", 0),
                             ("_CullMode", 0), ("_EnableAlphaBlend", 1), ("_StencilRef", 4)):
            changed = deepcopy(overlay)
            changed["Floats"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(AmbiguousSubmeshSlots, "explicit compatible opaque"):
                validate_ordered_surface_passes((record, changed))
        missing = deepcopy(overlay)
        del missing["Floats"]["_ZWriteGBuffer"]
        with self.assertRaises(AmbiguousSubmeshSlots):
            validate_ordered_surface_passes((record, missing))

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

    def test_visible_primary_mesh_accepts_implicit_base_beside_lods(self) -> None:
        base = "assets/tree.fbx##tree"
        self.assertEqual(base, primary_visible_mesh(((base, RuntimeDraw(0, 256)),
            ("assets/tree_lod1.fbx##tree_lod1", RuntimeDraw(0, 256)),
            ("assets/tree_lod2.fbx##tree_lod2", RuntimeDraw(0, 256)),
            ("assets/tree_shadowproxy.fbx##tree_shadowproxy", RuntimeDraw(0, 1024)))))
        lod0 = "assets/tree_lod0.fbx##tree_lod0"
        self.assertEqual(lod0, primary_visible_mesh(((base, RuntimeDraw(0, 256)),
            (lod0, RuntimeDraw(0, 256)))))
        for pairs in (((base, RuntimeDraw(0, 256)),
                       ("assets/tree_card.fbx##tree_card", RuntimeDraw(0, 256))),
                      (("assets/tree_lod1.fbx##tree_lod1", RuntimeDraw(0, 256)),
                       ("assets/tree_lod2.fbx##tree_lod2", RuntimeDraw(0, 256))),
                      ((base, RuntimeDraw(0, 1024)),)):
            with self.subTest(pairs=pairs), self.assertRaisesRegex(AmbiguousSubmeshSlots, "No unique visible base mesh"):
                primary_visible_mesh(pairs)

    def test_visible_primary_mesh_uses_container_lod_for_generic_subassets(self) -> None:
        paths = tuple(f"assets/machine_lod{lod}.fbx##mesh" for lod in (0, 1, 2))
        self.assertEqual(paths[0], primary_visible_mesh(tuple((path, RuntimeDraw(0, 256)) for path in paths)))
        self.assertEqual(paths[1], primary_visible_mesh(((paths[1], RuntimeDraw(0, 256)),)))
        with self.assertRaisesRegex(AmbiguousSubmeshSlots, "LOD disagree"):
            primary_visible_mesh((("assets/machine_lod0.fbx##machine_lod1", RuntimeDraw(0, 256)),))

    def test_single_authored_lod_mesh_keeps_draws_without_adding_other_lods(self) -> None:
        mesh = "assets/rock_lod1.fbx##rock_lod1"
        shadow = "assets/rock_shadow.fbx##rock_shadow"
        self.assertEqual(mesh, primary_visible_mesh(((mesh, RuntimeDraw(0, 200802304)),
                                                    (mesh, RuntimeDraw(1, 200802304)),
                                                    (shadow, RuntimeDraw(0, 1024)))))
        with self.assertRaisesRegex(AmbiguousSubmeshSlots, "No unique visible base mesh"):
            primary_visible_mesh(((mesh, RuntimeDraw(0, 1024)),))

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
