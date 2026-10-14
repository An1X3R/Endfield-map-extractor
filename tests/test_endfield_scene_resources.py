"""Integration tests for exact, cache-bound scene-resource discovery."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from endfield_scene_resources import (
    FORMAT,
    PROVIDER_COVERAGE,
    describe_file,
    ensure_scene_resources,
    exact_providers,
    ready_resource_paths,
    ready_requested_resource_paths,
    resource_cache_root,
    supplemental_manifests,
    validate_scan,
)
from endfield_scene_io import scene_io_session
from extractor_core.resources import BundleProviderIssue, read_bundle_provider_names_for_paths


FINGERPRINT = "a1" * 32


class SceneResourceIntegrationTests(unittest.TestCase):
    def test_selected_readiness_matches_full_catalog_as_payloads_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            texture = {"Source": "CAB", "PathId": 8}
            material = {"Source": "CAB", "PathId": 1, "Name": "selected",
                        "Shader": {"Source": "CAB", "PathId": 2}, "Floats": {}, "Colors": {},
                        "Textures": [{"Property": "_BaseMap", "Texture": texture,
                                      "Scale": [1, 1], "Offset": [0, 0]}]}
            mesh = {"Source": "CAB", "PathId": 3, "Type": "Mesh", "Name": "mesh"}
            material_path, mesh_path = "assets/mat.mat", "assets/mesh.fbx"
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"Format": "EndfieldSceneProbe/1", "Materials": [material],
                "Containers": [{"Container": material_path, "Asset": {**material, "Type": "Material"}},
                               {"Container": mesh_path, "Asset": mesh}],
                "MeshExports": [{"Asset": mesh, "File": "mesh.obj"}],
                "TextureExports": [{"Asset": texture, "File": "texture.png"}]}), encoding="utf-8")
            requested = frozenset({material_path, mesh_path + "##mesh", "assets/absent.mat"})
            for mesh_ready, texture_ready in ((True, True), (False, True), (True, False), (True, True)):
                for name, present in (("mesh.obj", mesh_ready), ("texture.png", texture_ready)):
                    path = root / name
                    if present:
                        path.write_bytes(b"payload")
                    else:
                        path.unlink(missing_ok=True)
                with scene_io_session():
                    full = ready_resource_paths((manifest,))
                    selected = ready_requested_resource_paths((manifest,), requested, root / "cache")
                self.assertEqual(full & requested, selected & requested)
                self.assertEqual(material_path in selected, texture_ready)
                self.assertEqual(mesh_path + "##mesh" in selected, mesh_ready)

    def test_mapped_sweep_cache_requires_source_texture_color_space(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "scene_manifest.json"
            texture = {"Source": "CAB-texture", "PathId": 3}
            material = {
                "Source": "CAB-material", "PathId": 2, "Name": "sweep",
                "Shader": {"Source": "cab-93483c2cb844b6b343af0950ad4630ad",
                           "PathId": 5324015590718682574},
                "Floats": {"_EmissiveMaskChannel": 0, "_EmissiveUVSet": 0,
                           "_EnableEmissiveAnimSweep": 1}, "Colors": {},
                "RenderState": {"ValidKeywords": ["_EMISSIVE_MAP", "_EMISSIVE_ANIM_SWEEP"],
                                "DisabledShaderPasses": [], "CustomRenderQueue": -1},
                "Textures": [{"Property": "_EmissiveMap", "Texture": texture,
                              "Scale": [1, 1], "Offset": [0, 0]}],
            }
            container = "assets/materials/sweep.mat"
            payload = {"Format": "EndfieldSceneProbe/1", "Materials": [material],
                       "Containers": [{"Container": container, "Asset": {
                           "Source": "CAB-material", "PathId": 2, "Type": "Material"}}],
                       "MeshExports": [], "TextureExports": [{"Asset": texture, "File": "map.png"}]}
            (root / "map.png").write_bytes(b"texture payload")
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotIn(container, ready_resource_paths((manifest,)))
            supplement = root / "supplement.json"
            for color_space in (0, 1):
                evidence = {"Format": "EndfieldSceneProbe/1", "Materials": [], "Containers": [],
                            "MeshExports": [], "TextureExports": [{
                                "Asset": {**texture, "ColorSpace": color_space}, "File": "map.png"}]}
                supplement.write_text(json.dumps(evidence), encoding="utf-8")
                self.assertIn(container, ready_resource_paths((manifest, supplement)))
            legacy_material = {key: value for key, value in material.items() if key != "RenderState"}
            manifest.write_text(json.dumps({**payload, "Materials": [legacy_material]}), encoding="utf-8")
            self.assertNotIn(container, ready_resource_paths((manifest, supplement)))
            supplement.write_text(json.dumps({**evidence, "Materials": [material]}), encoding="utf-8")
            self.assertIn(container, ready_resource_paths((manifest, supplement)))

    def test_probed_prefab_is_ready_even_without_particle_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "scene_manifest.json"
            payload = {"Format": "EndfieldSceneProbe/1", "Containers": [
                {"Container": "Assets/Effects/P_fx.prefab",
                 "Asset": {"Source": "CAB-root", "PathId": 7, "Type": "GameObject", "Name": "P_fx"}}],
                "Nodes": [], "Particles": {"DataScope": "component_parameters_no_simulation",
                                           "ModuleDataScope": "typed_modules", "Systems": [],
                                           "Renderers": [], "Pairs": [], "Diagnostics": []},
                "Materials": [], "TextureExports": [], "MeshExports": []}
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIn("assets/effects/p_fx.prefab", ready_resource_paths((manifest,)))
            self.assertIn("assets/effects/p_fx.prefab", ready_requested_resource_paths(
                (manifest,), frozenset({"assets/effects/p_fx.prefab"}), Path(temporary) / "cache"))
            self.assertNotIn("assets/effects/p_fx.prefab", ready_requested_resource_paths(
                (manifest,), frozenset({"assets/effects/other.prefab"}), Path(temporary) / "cache"))
            del payload["Particles"]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self.assertNotIn("assets/effects/p_fx.prefab", ready_resource_paths((manifest,)))

    def test_exact_fx_providers_leave_missing_and_ambiguous_paths_unselected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scan = Path(temporary) / "providers.jsonl"
            rows = (
                {"kind": "match", "logical_name": "main/unique.ab",
                 "containers": ["assets/effects/shared.prefab"]},
                {"kind": "match", "logical_name": "main/first.ab",
                 "containers": ["assets/effects/ambiguous.prefab"]},
                {"kind": "match", "logical_name": "main/second.ab",
                 "containers": ["assets/effects/ambiguous.prefab"]},
                {"kind": "match", "logical_name": "main/similar.ab",
                 "containers": ["assets/effects/missing_variant.prefab"]},
                {"kind": "complete", "failed": 0},
            )
            scan.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            names, issues = read_bundle_provider_names_for_paths(scan, {
                "map01": (
                    "Assets/Effects/Shared.prefab",
                    "Assets/Effects/Ambiguous.prefab",
                    "Assets/Effects/Missing.prefab",
                ),
                "map02": ("assets/effects/shared.prefab",),
            })

            self.assertEqual({"map01": ["main/unique.ab"], "map02": ["main/unique.ab"]}, names)
            self.assertEqual([
                BundleProviderIssue("Assets/Effects/Ambiguous.prefab", "ambiguous",
                                    ("main/first.ab", "main/second.ab")),
                BundleProviderIssue("Assets/Effects/Missing.prefab", "missing", ()),
            ], issues["map01"])
            self.assertEqual([], issues["map02"])

    def test_exact_provider_lookup_keeps_com_indie_matches_and_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan = root / "providers.jsonl"
            rows = (
                {"kind": "match", "logical_name": "main/indie_mesh.ab",
                 "containers": ["Assets/COM/Indie/mesh/cedar.fbx"]},
                {"kind": "match", "logical_name": "main/similar.ab",
                 "containers": ["assets/com/indie/mesh/cedar_lod0.fbx"]},
                {"kind": "match", "logical_name": "main/provider_a.ab",
                 "containers": ["assets/com/indie/mesh/ambiguous.prefab"]},
                {"kind": "match", "logical_name": "main/provider_b.ab",
                 "containers": ["assets/com/indie/mesh/ambiguous.prefab"]},
                {"kind": "complete", "failed": 0},
            )
            scan.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            validate_scan(scan)

            requested = frozenset({
                "assets/com/indie/mesh/cedar.fbx",
                "assets/com/indie/mesh/ambiguous.prefab",
                "assets/com/indie/mesh/cedar.prefab",
            })
            providers = exact_providers(scan, requested)

            self.assertEqual(("main/indie_mesh.ab",), providers["assets/com/indie/mesh/cedar.fbx"])
            self.assertEqual(
                ("main/provider_a.ab", "main/provider_b.ab"),
                providers["assets/com/indie/mesh/ambiguous.prefab"],
            )
            self.assertEqual((), providers["assets/com/indie/mesh/cedar.prefab"])

    def test_provider_index_is_shared_and_same_size_source_changes_invalidate_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scan = Path(temporary) / "providers.jsonl"
            requested = frozenset({"assets/com/indie/mesh/cedar.fbx"})
            extended = frozenset({"assets/com/indie/mesh/cedar.fbx", "assets/com/indie/mesh/missing.fbx"})

            def write_provider(name: str) -> None:
                scan.write_text("".join(json.dumps(row) + "\n" for row in (
                    {"kind": "match", "logical_name": name,
                     "containers": ["Assets/COM/Indie/Mesh/Cedar.fbx"]},
                    {"kind": "complete", "failed": 0},
                )), encoding="utf-8")

            write_provider("main/provider_a.ab")
            with scene_io_session() as session:
                validate_scan(scan)
                first = exact_providers(scan, requested)
                self.assertEqual({"assets/com/indie/mesh/cedar.fbx": ("main/provider_a.ab",)}, first)
                self.assertEqual(first, exact_providers(scan, requested))
                self.assertEqual({
                    "assets/com/indie/mesh/cedar.fbx": ("main/provider_a.ab",),
                    "assets/com/indie/mesh/missing.fbx": (),
                }, exact_providers(scan, extended))
                self.assertEqual(2, session.statistics()["source_cache_hits"])
                self.assertEqual(1, session.statistics()["source_cache_misses"])

                stamp = scan.stat()
                write_provider("main/provider_b.ab")
                os.utime(scan, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                changed = exact_providers(scan, requested)
                self.assertEqual({"assets/com/indie/mesh/cedar.fbx": ("main/provider_b.ab",)}, changed)
                self.assertEqual(2, session.statistics()["source_cache_misses"])

    def test_ambiguous_provider_is_reported_without_automatic_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / "config"
            config_dir.mkdir()
            runtime_path = config_dir / "endfield.runtime.local.json"
            inputs = config_dir / "inputs"
            inputs.mkdir()
            bundle_index = inputs / "bundle_scan_index.tsv"
            resource_database = inputs / "resources.sqlite"
            bundle_index.write_text("bundle index fixture\n", encoding="utf-8")
            self._create_resource_database(resource_database, "initial")
            scan = root / "prepared-providers.jsonl"
            scan.write_text(
                "".join(json.dumps(row) + "\n" for row in (
                    {"kind": "match", "logical_name": "main/one.ab",
                     "containers": ["assets/com/indie/model/cedar.fbx"]},
                    {"kind": "match", "logical_name": "main/two.ab",
                     "containers": ["assets/com/indie/model/cedar.fbx"]},
                    {"kind": "complete", "failed": 0},
                )),
                encoding="utf-8",
            )
            runtime = {
                "format": "EndfieldRuntimeConfig/1",
                "paths": {"cache_root": "../configured-cache", "game_root": "../game"},
                "extraction": {
                    "mapLayerFingerprint": FINGERPRINT,
                    "bundleScanIndex": "inputs/bundle_scan_index.tsv",
                    "resourceDatabase": "inputs/resources.sqlite",
                    "sceneDiscovery": {
                        "providerCoverage": PROVIDER_COVERAGE,
                        "scannerResults": describe_file(scan),
                    },
                },
            }
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            state = {
                "steps": {
                    "helper_dependencies": {
                        "outputs": {
                            "outputs": {
                                "bundleScanner": {"path": "bundle-scanner.exe"},
                                "sceneProbe": {"path": "scene-probe.exe"},
                            },
                        },
                    },
                },
            }
            (config_dir / "first_run_state.json").write_text(json.dumps(state), encoding="utf-8")
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({
                "required_resource_paths": ["assets/com/indie/model/cedar.fbx##cedar"],
                "scene_manifests": [],
            }), encoding="utf-8")

            result = ensure_scene_resources(
                runtime_path,
                plan_path,
                lambda: None,
                lambda _progress, _message: None,
            )

            self.assertEqual("partial", result["status"])
            self.assertEqual(0, result["providerRoots"])
            self.assertEqual(
                {"assets/com/indie/model/cedar.fbx": ["main/one.ab", "main/two.ab"]},
                result["unresolvedProviders"],
            )
            self.assertNotIn("extraction", result)

    def test_scene_resource_cache_root_changes_when_either_prepared_input_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path, runtime = self._runtime(root)
            extraction = runtime["extraction"]
            assert isinstance(extraction, dict)
            inputs = runtime_path.parent / "inputs"
            bundle_index = inputs / "bundle_scan_index.tsv"
            resource_database = inputs / "resources.sqlite"

            initial = resource_cache_root(runtime_path, runtime)
            bundle_index.write_text("changed bundle index\n", encoding="utf-8")
            after_index_change = resource_cache_root(runtime_path, runtime)
            self.assertNotEqual(initial, after_index_change)

            with closing(sqlite3.connect(resource_database)) as database, database:
                database.execute("INSERT INTO resource_fixture VALUES (?)", ("changed",))
            after_database_change = resource_cache_root(runtime_path, runtime)
            self.assertNotEqual(after_index_change, after_database_change)

    def test_scene_resource_cache_root_fails_clearly_for_missing_prepared_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path, runtime = self._runtime(root)
            extraction = runtime["extraction"]
            assert isinstance(extraction, dict)
            extraction["resourceDatabase"] = "inputs/missing.sqlite"

            with self.assertRaisesRegex(FileNotFoundError, "resourceDatabase"):
                resource_cache_root(runtime_path, runtime)

    def test_scan_requires_successful_completion_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scan = Path(temporary) / "providers.jsonl"
            cases = (
                ('{"kind":"match","logical_name":"main/a.ab","containers":[]}\n', "finish"),
                ('{"kind":"complete","failed":1}\n', "finish"),
            )
            for contents, message in cases:
                with self.subTest(contents=contents):
                    scan.write_text(contents, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        validate_scan(scan)
            scan.write_text('{"kind":"complete","failed":0}\n', encoding="utf-8")
            validate_scan(scan)

    def test_supplemental_manifest_rejects_path_outside_configured_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path, runtime = self._runtime(root)
            external = root / "outside.json"
            external.write_text("{}\n", encoding="utf-8")
            cache_root = resource_cache_root(runtime_path, runtime)
            cache_root.mkdir(parents=True)
            self._write_index(cache_root, runtime, [describe_file(external)])

            with self.assertRaisesRegex(ValueError, "inside its cache"):
                supplemental_manifests(runtime_path, runtime)

    def test_supplemental_manifest_rejects_content_changed_after_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path, runtime = self._runtime(root)
            cache_root = resource_cache_root(runtime_path, runtime)
            manifest = cache_root / "provider" / "scene_manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"Format":"EndfieldSceneProbe/1"}\n', encoding="utf-8")
            self._write_index(cache_root, runtime, [describe_file(manifest)])
            manifest.write_text('{"Format":"EndfieldSceneProbe/1","changed":true}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "changed after verification"):
                supplemental_manifests(runtime_path, runtime)

    def test_runtime_cache_supplement_requires_real_mesh_obj_and_texture_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path, runtime = self._runtime(root)
            cache_root = resource_cache_root(runtime_path, runtime)
            manifest = cache_root / "provider" / "scene_manifest.json"
            manifest.parent.mkdir(parents=True)
            mesh_file = manifest.parent / "cedar.obj"
            texture_file = manifest.parent / "cedar.png"
            fog_texture_file = manifest.parent / "fog_noise.png"
            mesh_file.write_text("o cedar\nv 0 0 0\n", encoding="utf-8")
            texture_file.write_bytes(b"PNG fixture")
            fog_texture_file.write_bytes(b"Fog texture fixture")
            mesh_asset = {"Source": "CAB-mesh", "PathId": 1, "Name": "cedar", "Type": "Mesh"}
            material_asset = {"Source": "CAB-material", "PathId": 2, "Name": "cedar_mat", "Type": "Material"}
            texture_asset = {"Source": "CAB-texture", "PathId": 3, "Name": "cedar_albedo"}
            fog_asset = {"Source": "CAB-material", "PathId": 5, "Name": "volume_fog", "Type": "Material"}
            fog_texture_asset = {"Source": "CAB-texture", "PathId": 6, "Name": "fog_noise"}
            sample_asset = {"Source": "CAB-material", "PathId": 7, "Name": "decal_sample", "Type": "Material"}
            sample_texture_asset = {"Source": "CAB-texture", "PathId": 8, "Name": "decal_sample_1"}
            payload = {
                "Format": "EndfieldSceneProbe/1",
                "Containers": [
                    {"Container": "assets/com/indie/mesh/cedar.fbx", "Asset": mesh_asset},
                    {"Container": "assets/com/indie/material/cedar.mat", "Asset": material_asset},
                    {"Container": "assets/com/indie/material/fog.mat", "Asset": fog_asset},
                    {"Container": "assets/com/indie/material/decal.mat", "Asset": sample_asset},
                ],
                "MeshExports": [{"Asset": mesh_asset, "File": "cedar.obj"}],
                "Materials": [{
                    "Name": "cedar_mat",
                    "Source": "CAB-material",
                    "PathId": 2,
                    "Shader": {"Source": "CAB-shader", "PathId": 4},
                    "Textures": [{
                        "Property": "_BaseMap",
                        "Texture": texture_asset,
                        "Scale": [1.0, 1.0],
                        "Offset": [0.0, 0.0],
                    }],
                    "Floats": {"_BaseTextureMapCount": 1.0},
                    "Colors": {},
                }, {
                    "Name": "volume_fog",
                    "Source": "CAB-material",
                    "PathId": 5,
                    "Shader": {"Source": "CAB-shader", "PathId": 9},
                    "Textures": [{
                        "Property": "_NoiseTex3D",
                        "Texture": fog_texture_asset,
                        "Scale": [1.0, 1.0],
                        "Offset": [0.0, 0.0],
                    }],
                    "Floats": {"_FogDensity": 0.5, "_CubEdgeFade": 0.25},
                    "Colors": {"_FogColor": [0.1, 0.2, 0.3, 1.0]},
                }, {
                    "Name": "decal_sample",
                    "Source": "CAB-material",
                    "PathId": 7,
                    "Shader": {"Source": "CAB-shader", "PathId": 10},
                    "Textures": [{
                        "Property": "_SampleTex1",
                        "Texture": sample_texture_asset,
                        "Scale": [1.0, 1.0],
                        "Offset": [0.0, 0.0],
                    }],
                    "Floats": {},
                    "Colors": {},
                }],
                "TextureExports": [
                    {"Asset": texture_asset, "File": "cedar.png"},
                    {"Asset": fog_texture_asset, "File": "fog_noise.png"},
                    {"Asset": sample_texture_asset, "File": "sample_tex_1.png"},
                ],
            }
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            self._write_index(cache_root, runtime, [describe_file(manifest)])

            supplemental = supplemental_manifests(runtime_path, runtime)
            self.assertEqual((manifest.resolve(),), supplemental)
            self.assertTrue(supplemental[0].is_relative_to(cache_root.resolve()))
            ready = ready_resource_paths(supplemental)
            self.assertIn("assets/com/indie/mesh/cedar.fbx##cedar", ready)
            self.assertIn("assets/com/indie/material/cedar.mat", ready)
            self.assertIn("assets/com/indie/material/fog.mat", ready)
            self.assertNotIn("assets/com/indie/material/decal.mat", ready)

            mesh_file.unlink()
            without_mesh = ready_resource_paths(supplemental)
            self.assertNotIn("assets/com/indie/mesh/cedar.fbx##cedar", without_mesh)
            self.assertIn("assets/com/indie/material/cedar.mat", without_mesh)

            mesh_file.write_text("o cedar\nv 0 0 0\n", encoding="utf-8")
            texture_file.unlink()
            without_texture = ready_resource_paths(supplemental)
            self.assertIn("assets/com/indie/mesh/cedar.fbx##cedar", without_texture)
            self.assertNotIn("assets/com/indie/material/cedar.mat", without_texture)

    def _runtime(self, root: Path) -> tuple[Path, dict[str, object]]:
        config_dir = root / "runtime"
        inputs = config_dir / "inputs"
        inputs.mkdir(parents=True)
        (inputs / "bundle_scan_index.tsv").write_text("bundle index fixture\n", encoding="utf-8")
        self._create_resource_database(inputs / "resources.sqlite", "initial")
        runtime_path = config_dir / "endfield.runtime.local.json"
        runtime: dict[str, object] = {
            "format": "EndfieldRuntimeConfig/1",
            "paths": {"cache_root": "../selected-cache", "game_root": "../game"},
            "extraction": {
                "mapLayerFingerprint": FINGERPRINT,
                "bundleScanIndex": "inputs/bundle_scan_index.tsv",
                "resourceDatabase": "inputs/resources.sqlite",
            },
        }
        runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
        return runtime_path, runtime

    def _create_resource_database(self, path: Path, payload: str) -> None:
        with closing(sqlite3.connect(path)) as database, database:
            database.execute("CREATE TABLE resource_fixture (payload TEXT NOT NULL)")
            database.execute("INSERT INTO resource_fixture VALUES (?)", (payload,))

    def _write_index(self, cache_root: Path, runtime: dict[str, object], rows: list[dict[str, object]]) -> None:
        extraction = runtime["extraction"]
        index = {
            "format": FORMAT,
            "datasetFingerprint": extraction["mapLayerFingerprint"],
            "manifests": rows,
        }
        (cache_root / "index.json").write_text(json.dumps(index), encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
