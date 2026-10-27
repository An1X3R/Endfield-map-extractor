"""Verify selected catalogs against real SceneProbe files and source versions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from endfield_scene_io import SceneInputChangedError, scene_io_session
from endfield_scene_material_contract import MaterialRecord, load_catalog
from endfield_scene_material_handoff import (
    _payload_sha256, build_material_catalog_handoff, capture_material_catalog_sources,
    load_plan_material_catalog,
)


def _ref(path_id: int, kind: str) -> dict[str, object]:
    return {"Source": "CAB-source", "PathId": path_id, "Type": kind}


def _material(path_id: int) -> MaterialRecord:
    return {"Name": f"material_{path_id}", "Source": "CAB-source", "PathId": path_id,
            "Shader": {"Source": "CAB-shader", "PathId": 100, "Name": "HGRP/Lit"},
            "Textures": [{"Property": "_BaseColorMap", "Texture": {"Source": "CAB-texture", "PathId": 31},
                          "Scale": [2.0, 3.0], "Offset": [0.1, 0.2]}],
            "Floats": {"_AlphaClip": 0.5}, "Colors": {"_Color": [1.0, 0.7, 0.5, 1.0]},
            "RenderState": {"DisabledShaderPasses": ["DepthOnly"], "ValidKeywords": ["_ALPHATEST_ON"],
                            "CustomRenderQueue": 2450}}


def _fixture(root: Path) -> tuple[Path, dict[str, object]]:
    texture = root / "texture.png"
    texture.write_bytes(b"real-file-payload")
    sampler = {"FilterMode": 2, "WrapU": 0, "WrapV": 1, "WrapW": 2, "Aniso": 4, "MipBias": -0.5}
    records = [_material(path_id) for path_id in range(1, 10)]
    records[-1]["Shader"]["TextureDefaults"] = [{"Property": "_LayerBlendMaskMap", "Name": "white", "Dimension": 2}]
    records[0]["Textures"].append({"Property": "_UnusedBinding", "Texture": {"Source": "CAB-texture", "PathId": 32},
                                  "Scale": [1.0, 1.0], "Offset": [0.0, 0.0]})
    manifest = root / "scene.json"
    nodes = [dict(_ref(200, "GameObject"), TransformPathId=300, Parent=_ref(0, "Transform"), GameObjectActive=True),
             dict(_ref(201, "GameObject"), TransformPathId=301, Parent=_ref(300, "Transform"), GameObjectActive=True,
                  RendererType="MeshRenderer", RendererEnabled=True, Materials=[_ref(8, "Material")])]
    manifest.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Materials": records,
        "TextureExports": [{"Asset": {"Source": "CAB-texture", "PathId": 31, "ColorSpace": 1,
                                      "TextureSampler": sampler}, "File": "missing.png"},
                           {"Asset": {"Source": "CAB-texture", "PathId": 31}, "File": "texture.png"}],
        "Nodes": nodes, "Particles": {"Systems": [{"Asset": _ref(400, "ParticleSystem")}],
            "Renderers": [{"Asset": _ref(500, "ParticleSystemRenderer"), "Enabled": True,
                           "RenderMode": 0, "RenderAlignment": 0, "CameraVelocityScale": 0.0,
                           "Materials": [{"Target": _ref(7, "Material")}]}],
            "Pairs": [{"GameObject": _ref(200, "GameObject"), "System": _ref(400, "ParticleSystem"),
                       "Renderer": _ref(500, "ParticleSystemRenderer")}]} }), encoding="utf-8")
    plan: dict[str, object] = {"scene_manifests": [str(manifest)],
        "instances": [{"material_refs": [_ref(1, "Material"), _ref(2, "Material"), None], "selected_submeshes": [0, 2]}],
        "dynamic_geometry_instances": [{"material_refs": [_ref(3, "Material")]}],
        "surface_effect_instances": [{"material_refs": [_ref(4, "Material")]}],
        "water_depth_instances": [{"material_refs": [_ref(5, "Material")]}],
        "direct_effect_renderers": [{"material_asset": _ref(6, "Material")}],
        "effect_instances": [{"scene_manifest": str(manifest), "prefab_root": _ref(200, "GameObject"),
                              "prefab_path": "assets/test.prefab", "component_kind": "mixed"}]}
    return manifest, plan


class SelectedMaterialHandoffTests(unittest.TestCase):
    def test_ordered_submesh_passes_retain_the_complete_selected_material_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory, scene_io_session():
            manifest, plan = _fixture(Path(directory))
            plan["instances"] = [{"material_refs": [_ref(1, "Material"), None],
                "submesh_material_passes": [[_ref(1, "Material"), _ref(2, "Material"), _ref(9, "Material")], []],
                "selected_submeshes": [0]}]
            materials, textures = load_catalog((manifest,))
            payload = build_material_catalog_handoff(plan, materials, textures, capture_material_catalog_sources((manifest,)))
            selected, _ = load_plan_material_catalog({**plan, "material_catalog": payload})
            self.assertTrue({("cab-source", key) for key in (1, 2, 9)} <= selected.keys())
            stale = {**plan, "instances": [{"material_refs": [_ref(1, "Material")],
                "submesh_material_passes": [[_ref(1, "Material"), _ref(10, "Material")]]}], "material_catalog": payload}
            with self.assertRaisesRegex(ValueError, "omits scene material dependencies"):
                load_plan_material_catalog(stale)

    def test_all_scene_branches_preserve_exact_enriched_catalog_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory, scene_io_session():
            manifest, plan = _fixture(Path(directory))
            sources = capture_material_catalog_sources((manifest,))
            full_materials, full_textures = load_catalog((manifest,))
            payload = build_material_catalog_handoff(plan, full_materials, full_textures, sources)
            transported = json.loads(json.dumps({**plan, "material_catalog": payload}))
            selected_materials, selected_textures = load_plan_material_catalog(transported)
            expected = {("cab-source", index) for index in (1, 3, 4, 5, 6, 7, 8)}
            self.assertEqual(expected, selected_materials.keys())
            self.assertEqual({key: full_materials[key] for key in expected}, selected_materials)
            self.assertEqual(full_textures, selected_textures)
            self.assertEqual([{ "Source": "cab-texture", "PathId": 32}], payload["missing_texture_refs"])
            self.assertEqual(full_materials[("cab-source", 9)]["Shader"]["TextureDefaults"],
                             selected_materials[("cab-source", 1)]["Shader"]["TextureDefaults"])
            self.assertEqual(9, len(full_materials))
            self.assertNotIn("material_catalog", plan)

    def test_missing_material_or_texture_dependency_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, plan = _fixture(Path(directory))
            sources = capture_material_catalog_sources((manifest,))
            materials, textures = load_catalog((manifest,))
            payload = build_material_catalog_handoff(plan, materials, textures, sources)
            for field in ("materials", "textures"):
                changed = json.loads(json.dumps(payload))
                changed[field].pop()
                changed["payload_sha256"] = _payload_sha256(changed)
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "dependency closure"):
                    load_plan_material_catalog({**plan, "material_catalog": changed})
            incomplete = {key: value for key, value in materials.items() if key != ("cab-source", 7)}
            with self.assertRaisesRegex(ValueError, "dependency is missing"):
                build_material_catalog_handoff(plan, incomplete, textures, sources)

    def test_scene_dependency_change_cannot_reuse_an_incomplete_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, plan = _fixture(Path(directory))
            sources = capture_material_catalog_sources((manifest,))
            materials, textures = load_catalog((manifest,))
            payload = build_material_catalog_handoff(plan, materials, textures, sources)
            changed = {**plan, "instances": [{"material_refs": [_ref(2, "Material")]}], "material_catalog": payload}
            with self.assertRaisesRegex(ValueError, "omits scene material dependencies"):
                load_plan_material_catalog(changed)

    def test_changed_effect_root_manifest_or_kind_cannot_reuse_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, plan = _fixture(Path(directory))
            sources = capture_material_catalog_sources((manifest,))
            materials, textures = load_catalog((manifest,))
            payload = build_material_catalog_handoff(plan, materials, textures, sources)
            effects = plan["effect_instances"]
            for field, value in (("prefab_root", _ref(201, "GameObject")),
                                 ("scene_manifest", str(manifest.with_name("other.json"))),
                                 ("component_kind", "mesh_renderer")):
                changed = {**plan, "effect_instances": [{**effects[0], field: value}], "material_catalog": payload}
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "effect roots differ"):
                    load_plan_material_catalog(changed)
            repeated = {**plan, "effect_instances": [effects[0], effects[0]], "material_catalog": payload}
            self.assertEqual(load_plan_material_catalog({**plan, "material_catalog": payload}),
                             load_plan_material_catalog(repeated))

    def test_particle_renderer_branches_that_skip_materials_do_not_require_missing_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, plan = _fixture(root)
            original = json.loads(manifest.read_text(encoding="utf-8"))
            original["Materials"] = [row for row in original["Materials"] if row["PathId"] != 7]
            renderer = original["Particles"]["Renderers"][0]
            branches = [{"Enabled": False}, {"RenderMode": 2, "RenderAlignment": None}, {"RenderAlignment": 1},
                        {"RenderMode": 1, "RenderAlignment": 2}, {"CameraVelocityScale": 1.0},
                        {"Materials": [{"Target": _ref(7, "Material")}, {"Target": _ref(9, "Material")}]}]
            for branch in branches:
                source = {**original, "Particles": {**original["Particles"], "Renderers": [{**renderer, **branch}]}}
                manifest.write_text(json.dumps(source), encoding="utf-8")
                sources = capture_material_catalog_sources((manifest,))
                materials, textures = load_catalog((manifest,))
                with self.subTest(branch=branch):
                    payload = build_material_catalog_handoff(plan, materials, textures, sources)
                    selected, _textures = load_plan_material_catalog({**plan, "material_catalog": payload})
                    self.assertNotIn(("cab-source", 7), selected)
                    self.assertIn(("cab-source", 8), selected)

    def test_velocity_aligned_mesh_particle_requires_its_material_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, plan = _fixture(root)
            original = json.loads(manifest.read_text(encoding="utf-8"))
            renderer = {**original["Particles"]["Renderers"][0], "RenderMode": 4, "RenderAlignment": 4}
            source = {**original, "Particles": {**original["Particles"], "Renderers": [renderer]}}
            manifest.write_text(json.dumps(source), encoding="utf-8")

            sources = capture_material_catalog_sources((manifest,))
            materials, textures = load_catalog((manifest,))
            payload = build_material_catalog_handoff(plan, materials, textures, sources)
            selected, _textures = load_plan_material_catalog({**plan, "material_catalog": payload})
            self.assertIn(("cab-source", 7), selected)

            missing_source = {**source, "Materials": [row for row in source["Materials"] if row["PathId"] != 7]}
            manifest.write_text(json.dumps(missing_source), encoding="utf-8")
            sources = capture_material_catalog_sources((manifest,))
            materials, textures = load_catalog((manifest,))
            with self.assertRaisesRegex(ValueError, "Selected material catalog dependency is missing"):
                build_material_catalog_handoff(plan, materials, textures, sources)

    def test_source_changes_before_handoff_or_worker_are_rejected_even_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, plan = _fixture(Path(directory))
            sources = capture_material_catalog_sources((manifest,))
            materials, textures = load_catalog((manifest,))
            payload = build_material_catalog_handoff(plan, materials, textures, sources)
            stamp = manifest.stat()
            changed = manifest.read_bytes().replace(b"material_1", b"material_X", 1)
            manifest.write_bytes(changed)
            os.utime(manifest, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertEqual(stamp.st_size, manifest.stat().st_size)
            with self.assertRaises(SceneInputChangedError):
                build_material_catalog_handoff(plan, materials, textures, sources)
            with self.assertRaises(SceneInputChangedError):
                load_plan_material_catalog({**plan, "material_catalog": payload})

    def test_deleted_selected_texture_and_corrupted_payload_fail_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, plan = _fixture(root)
            sources = capture_material_catalog_sources((manifest,))
            materials, textures = load_catalog((manifest,))
            payload = build_material_catalog_handoff(plan, materials, textures, sources)
            corrupt = {**payload, "payload_sha256": "0" * 64}
            with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
                load_plan_material_catalog({**plan, "material_catalog": corrupt})
            (root / "texture.png").unlink()
            with self.assertRaisesRegex(FileNotFoundError, "texture payload is missing"):
                load_plan_material_catalog({**plan, "material_catalog": payload})

    def test_full_source_conflicts_are_still_rejected_before_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, _plan = _fixture(Path(directory))
            source = json.loads(manifest.read_text(encoding="utf-8"))
            duplicate = {**source["Materials"][-1], "Floats": {"_AlphaClip": 0.25}}
            source["Materials"].append(duplicate)
            manifest.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Conflicting material records"):
                load_catalog((manifest,))

    def test_older_plans_keep_the_original_catalog_reader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, plan = _fixture(Path(directory))
            self.assertEqual(load_catalog((manifest,)), load_plan_material_catalog(plan))


if __name__ == "__main__":
    unittest.main()
