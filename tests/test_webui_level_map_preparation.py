"""Verify map publication and runtime isolation using genuine local PNG files."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from launch_webui import PathPickerHandler, PathPickerServer
from endfield_level_maps import LevelMapCatalog, compose_level_map, parse_level_map_config, resolve_level_map_image
from export_level_map import bind_level_map_registry
from tests.test_endfield_level_maps import config_payload, prepare_fixture, write_json
from webui_level_map_preparation import (LevelMapPreparationConflict, LevelMapPreparationCoordinator,
    LevelMapPreparationUnavailable, catalog_game_identity, current_map_entries, publish_level_map, runtime_source_key)


class LevelMapPreparationIntegrationTests(unittest.TestCase):
    def test_authenticated_routes_preserve_specific_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = PathPickerServer(("127.0.0.1", 0), PathPickerHandler)
            server.token = "map-preparation-contract"
            server.level_map_preparation = LevelMapPreparationCoordinator(lambda: None, root / "jobs")
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                for route, method, payload, token, expected in (
                    ("/level-map-preparation", "GET", None, "wrong", 404),
                    ("/level-map-preparation", "GET", None, server.token, 409),
                    ("/level-map-preparation", "POST", {"format": "bad"}, server.token, 400),
                    ("/level-map-preparation/unknown", "GET", None, server.token, 404),
                    ("/level-map-preparation/unknown/cancel", "POST", {}, server.token, 404),
                    ("/level-map-preparation/unknown/extra", "GET", None, server.token, 404),
                ):
                    request = Request(f"http://127.0.0.1:{server.server_port}{route}",
                        data=json.dumps(payload).encode() if payload is not None else None, method=method,
                        headers={"X-Endfield-Path-Picker-Token": token, "Content-Type": "application/json"})
                    with self.subTest(route=route, method=method, token=token), self.assertRaises(HTTPError) as caught:
                        urlopen(request, timeout=5)
                    self.assertEqual(caught.exception.code, expected)
                    self.assertIn("error", json.loads(caught.exception.read()))
            finally:
                server.shutdown()
                thread.join(5)
                server.server_close()

    def test_published_registry_preserves_original_and_runtime_compare(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "jobs" / ("a" * 32)
            staged.mkdir(parents=True)
            scene, resources = prepare_fixture(staged)
            entry = parse_level_map_config("map03_lv017", "config.json", config_payload(), resources)
            metadata = compose_level_map(entry, scene, staged / "map.png")
            catalog = LevelMapCatalog("b" * 64, (entry,), (), ())
            metadata["sourceGameIdentity"] = catalog_game_identity(catalog)
            write_json(staged / "level_maps.json", {"format": "EndfieldLevelMaps/1", "entries": [metadata]})
            published = publish_level_map(staged / "level_maps.json", root, "a" * 32, [])
            self.assertEqual(resolve_level_map_image(published, root, entry.level_id), staged / "map.png")
            runtime = root / "runtime.json"
            write_json(runtime, {"format": "EndfieldRuntimeConfig/1", "extraction": {}, "paths": {}})
            original = runtime.read_bytes()
            bind_level_map_registry(runtime, original, published)
            self.assertEqual(runtime_source_key(original), runtime_source_key(runtime.read_bytes()))
            self.assertEqual(current_map_entries(runtime, catalog)[1][0]["levelId"], entry.level_id)
            stale = replace(entry, config_sha256="c" * 64)
            with self.assertRaisesRegex(LevelMapPreparationConflict, "current source"):
                current_map_entries(runtime, replace(catalog, entries=(stale,)))
            bound = runtime.read_bytes()
            with self.assertRaisesRegex(ValueError, "changed during map export"):
                bind_level_map_registry(runtime, original, published)
            self.assertEqual(runtime.read_bytes(), bound)
            self.assertTrue((staged / "level_maps.json").is_file())
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_unprepared_source_and_unsafe_job_ids_fail_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = LevelMapPreparationCoordinator(lambda: None, root / "jobs")
            try:
                with self.assertRaises(LevelMapPreparationUnavailable):
                    service.catalog()
                for job_id in ("../runtime", "a" * 31, "G" * 32):
                    with self.subTest(job_id=job_id), self.assertRaises(FileNotFoundError):
                        service.status(job_id)
                with self.assertRaises(ValueError):
                    service.start({"format": "bad"})
                self.assertFalse((root / "jobs").exists())
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
