from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import endfield_first_run
import launch_webui
from extractor_core.resources import read_bundle_candidate_names, write_bundle_scan_index
from extractor_core.assets import build_asset_resolution_database
from extractor_core.resources import (
    read_bundle_candidate_names_by_map,
    read_bundle_closure_names_by_map,
)
from extractor_core.terrain import decode_dxt5


class FirstRunPlanTests(unittest.TestCase):
    def test_event_file_survives_detached_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / "events.jsonl"
            writer = endfield_first_run.EventWriter(events)
            with mock.patch("builtins.print", side_effect=OSError(22, "Invalid argument")):
                writer.emit("fixture", 0.5, "still recorded")
            payload = json.loads(events.read_text(encoding="utf-8"))
            self.assertEqual("still recorded", payload["message"])

    def test_plan_only_inputs_do_not_create_cache_or_export_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "Endfield Game"
            (game / "Endfield_Data" / "StreamingAssets" / "VFS").mkdir(parents=True)
            (game / "Endfield.exe").write_bytes(b"")
            (game / "Endfield_Data" / "globalgamemanagers").write_bytes(b"")
            cache = root / "cache-not-created"
            export = root / "export-not-created"
            value = endfield_first_run.plan(
                {
                    "gameRoot": game,
                    "cacheRoot": cache,
                    "exportRoot": export,
                    "blenderExe": None,
                },
                "fixture",
            )
            self.assertTrue(value["automatic"])
            self.assertEqual(value["geometryClosure"]["status"], "automatic_pending")
            self.assertIn("write_bundle_scanner_index", value["steps"])
            self.assertIn("extract_map01_full_terrain_surface", value["steps"])
            self.assertIn("extract_map02_quadrant_terrain_surfaces", value["steps"])
            self.assertFalse(cache.exists())
            self.assertFalse(export.exists())

    def test_runtime_activation_uses_per_user_local_config_and_preserves_previous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "LocalAppData"
            first = {"format": "EndfieldRuntimeConfig/1", "paths": {"game_root": "A"}}
            second = {"format": "EndfieldRuntimeConfig/1", "paths": {"game_root": "B"}}
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False):
                first_result = endfield_first_run.activate_runtime_config(first)
                second_result = endfield_first_run.activate_runtime_config(second)
            active = Path(second_result["path"])
            self.assertEqual(json.loads(active.read_text(encoding="utf-8")), second)
            self.assertEqual(first_result["status"], "activated")
            self.assertIn("previousConfigBackup", second_result)
            backup = Path(second_result["previousConfigBackup"]["path"])
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), first)


class BundleDiscoveryBootstrapTests(unittest.TestCase):
    def test_completed_orphan_scene_payload_is_recovered_without_reextracting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "run"
            log_root = root / "log"
            discovery = run_root / "scene_discovery" / "map_scene_candidates"
            discovery.mkdir(parents=True)
            scan_index = run_root / "scene_discovery" / "bundle_scan_index.tsv"
            scan_index.write_text("fixture\n", encoding="utf-8")
            (discovery / "bundle_candidates.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            "kind": "match",
                            "logical_name": f"main/{map_id}.ab",
                            "containers": [f"Assets/{map_id}/fixture.prefab"],
                        }
                    )
                    + "\n"
                    for map_id in ("map01", "map02")
                ),
                encoding="utf-8",
            )
            (discovery / "bundle_cab_inventory.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            "kind": "cab",
                            "logical_name": f"main/{map_id}.ab",
                            "serialized": [{"name": f"CAB-{map_id}", "externals": []}],
                        }
                    )
                    + "\n"
                    for map_id in ("map01", "map02")
                ),
                encoding="utf-8",
            )
            for map_id in ("map01", "map02"):
                bundle = discovery / map_id / "bundles" / "main" / f"{map_id}.ab"
                bundle.parent.mkdir(parents=True)
                bundle.write_bytes(b"bundle")
                manifest = discovery / map_id / "scene_probe" / "scene_manifest.json"
                manifest.parent.mkdir(parents=True)
                manifest.write_text("{}\n", encoding="utf-8")

            recovered = endfield_first_run.recover_completed_scene_discovery(
                run_root, log_root, scan_index
            )
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual("completed", recovered["status"])
            self.assertTrue(recovered["recoveredFromCompletedOutputs"])
            self.assertEqual(1, recovered["maps"]["map01"]["bundleCount"])

    def test_deferred_asset_step_upgrades_when_scanner_results_arrive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scan = Path(temporary) / "bundle_candidates.jsonl"
            scan.write_text("{}\n", encoding="utf-8")
            self.assertTrue(
                endfield_first_run.automatic_asset_upgrade_needed(
                    {"format": "EndfieldBootstrapAssetResolutionSummary/1"}, scan
                )
            )
            self.assertFalse(
                endfield_first_run.automatic_asset_upgrade_needed(
                    {"format": "EndfieldAutomaticAssetResolutionSummary/1"}, scan
                )
            )

    def test_scan_index_and_candidate_names_are_generated_from_local_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vfs_root = root / "game-vfs"
            group = vfs_root / "7064D8E2"
            group.mkdir(parents=True)
            (group / "ABC.chk").write_bytes(b"bundle")
            database = root / "resources.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE bundles (logical_name TEXT, group_hash TEXT, virtual_path TEXT, "
                    "chunk_name TEXT, chunk_offset INTEGER, byte_length INTEGER, data_md5 TEXT, "
                    "present_in_manifest INTEGER)"
                )
                connection.execute(
                    "INSERT INTO bundles VALUES (?,?,?,?,?,?,?,?)",
                    ("main/test.ab", "7064D8E2", "Data/test.ab", "ABC", 2, 4, "00", 1),
                )
                connection.commit()
            finally:
                connection.close()
            index = root / "bundle_scan_index.tsv"
            meta = write_bundle_scan_index(database, vfs_root, index)
            self.assertEqual(meta["bundle_count"], "1")
            self.assertIn("logical_name\tchunk_path\toffset\tlength", index.read_text(encoding="utf-8"))
            results = root / "results.jsonl"
            results.write_text(
                json.dumps({"kind": "match", "logical_name": "MAIN/Test.ab"}) + "\n"
                + json.dumps({"kind": "complete"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(read_bundle_candidate_names(results), ["main/test.ab"])

    def test_map_roots_and_external_bundle_closure_are_derived_from_scans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matches = root / "matches.jsonl"
            matches.write_text(
                json.dumps(
                    {
                        "kind": "match",
                        "logical_name": "main/map01.ab",
                        "containers": ["Assets/map01/prefabs/p_prop_map01_crate.prefab"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cabs = root / "cabs.jsonl"
            cabs.write_text(
                json.dumps(
                    {
                        "kind": "cab",
                        "logical_name": "main/map01.ab",
                        "serialized": [{"name": "CAB-map01", "externals": ["CAB-common"]}],
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "kind": "cab",
                        "logical_name": "main/common.ab",
                        "serialized": [{"name": "CAB-common", "externals": []}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            roots = read_bundle_candidate_names_by_map(matches)
            self.assertEqual(roots["map01"], ["main/map01.ab"])
            self.assertEqual(roots["map02"], [])
            closures, meta = read_bundle_closure_names_by_map(matches, cabs)
            self.assertEqual(closures["map01"], ["main/common.ab", "main/map01.ab"])
            self.assertEqual(meta["maps"]["map01"]["unresolvedExternalCount"], 0)

    def test_asset_resolution_database_is_built_from_first_run_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instances = root / "instances.sqlite"
            connection = sqlite3.connect(instances)
            connection.execute("CREATE TABLE instances (entity_base TEXT)")
            connection.executemany(
                "INSERT INTO instances VALUES (?)", [("p_prop_crate",), ("p_prop_crate",)]
            )
            connection.commit()
            connection.close()
            resources = root / "resources.sqlite"
            connection = sqlite3.connect(resources)
            connection.execute("CREATE TABLE paths (path TEXT, path_folded TEXT)")
            connection.commit()
            connection.close()
            scan = root / "scan.jsonl"
            scan.write_text(
                json.dumps(
                    {
                        "kind": "match",
                        "logical_name": "main/map01.ab",
                        "containers": ["Assets/map01/p_prop_map01_crate.prefab"],
                        "serialized": [{"name": "CAB-map01"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "assets.sqlite"
            summary = build_asset_resolution_database(
                "map01", instances, resources, scan, output
            )
            self.assertEqual(summary["uniqueByMethod"], {"prefab": 1})
            connection = sqlite3.connect(output)
            try:
                row = connection.execute(
                    "SELECT method,logical_name,root_cab FROM resolutions"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("prefab", "main/map01.ab", "CAB-map01"))

    def test_embedded_dxt5_decoder_has_no_external_fixture_dependency(self) -> None:
        block = bytes([255, 0]) + bytes(6) + (0xF800).to_bytes(2, "little")
        block += (0).to_bytes(2, "little") + bytes(4)
        decoded = decode_dxt5(block, 4, 4)
        self.assertEqual(len(decoded), 64)
        self.assertEqual(decoded[:4], bytes((255, 0, 0, 255)))


class LauncherBootstrapResolutionTests(unittest.TestCase):
    def test_stage_and_cache_defaults_are_derived_from_export_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            export = root / "exports"
            game.mkdir()
            export.mkdir()
            config = root / "runtime.json"
            config.write_text(
                json.dumps(
                    {
                        "format": "EndfieldRuntimeConfig/1",
                        "paths": {"game_root": str(game), "export_root": str(export)},
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                runtime_config=config,
                game_root=None,
                export_root=None,
                cache_root=None,
                stage119_root=None,
                webui_output_root=None,
                blender_exe=None,
                first_run_name="fixture",
            )
            paths, _ = launch_webui.resolve_runtime_paths(args)
            self.assertEqual(paths["cache_root"], (export / "cache" / "first_run_work").resolve())
            self.assertEqual(paths["stage119_root"], (export / "cache" / "map_layers_fixture").resolve())
            self.assertIsNone(paths["blender_exe"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
