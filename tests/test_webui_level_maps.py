"""Exercise authenticated map metadata and image routes on a real local bridge."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from launch_webui import PathPickerHandler, PathPickerServer
from endfield_level_maps import compose_level_map, parse_level_map_config, selection_grid_world_bounds
from tests.test_endfield_level_maps import config_payload, prepare_fixture, write_json


class LevelMapBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        scene, resources = prepare_fixture(self.root)
        entry = parse_level_map_config("map03_lv017", "config.json", config_payload(), resources)
        metadata = compose_level_map(entry, scene, self.root / "map.png")
        self.manifest = self.root / "level_maps.json"
        write_json(self.manifest, {"format": "EndfieldLevelMaps/1", "entries": [metadata]})
        payload = self.manifest.read_bytes()
        self.runtime = self.root / "runtime.json"
        write_json(self.runtime, {"format": "EndfieldRuntimeConfig/1", "extraction": {"levelMaps": {
            "path": self.manifest.name, "sha256": sha256(payload).hexdigest(), "bytes": len(payload)}}})
        self.server = PathPickerServer(("127.0.0.1", 0), PathPickerHandler)
        self.server.token = "level-map-integration"
        self.server.level_map_runtime = self.runtime
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self) -> None:
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()

    def fetch(self, route: str, token: str) -> tuple[int, str, bytes]:
        request = Request(f"http://127.0.0.1:{self.server.server_port}{route}",
                          headers={"X-Endfield-Path-Picker-Token": token})
        with urlopen(request, timeout=10) as response:
            return response.status, response.headers["Content-Type"], response.read()

    def test_metadata_and_image_use_current_portable_runtime(self) -> None:
        status, content_type, payload = self.fetch("/level-maps", self.server.token)
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("application/json"))
        metadata = json.loads(payload)
        self.assertEqual(metadata["status"], "prepared")
        self.assertEqual(metadata["imageValidation"], "sha256_on_image_request")
        self.assertEqual(metadata["entries"][0]["levelId"], "map03_lv017")
        entry = metadata["entries"][0]
        self.assertEqual(selection_grid_world_bounds(entry["selectionGrid"], 0, 1, 0, 1),
                         entry["sourceMapBounds"])
        self.assertEqual(entry["selectionGrid"]["mapId"], "map03")
        self.assertNotIn(str(self.root), payload.decode())
        image_url = metadata["entries"][0]["image"]["url"]
        status, content_type, image = self.fetch(image_url, self.server.token)
        self.assertEqual((status, content_type), (200, "image/png"))
        self.assertEqual(image, (self.root / "map.png").read_bytes())
        for route, token in (("/level-maps", "incorrect"),
                             ("/level-maps/map03_lv018/image", self.server.token),
                             ("/level-maps/../runtime.json/image", self.server.token)):
            with self.assertRaises(HTTPError) as caught:
                self.fetch(route, token)
            self.assertEqual(caught.exception.code, 404)

    def test_registry_change_is_not_served_as_current_data(self) -> None:
        self.manifest.write_bytes(self.manifest.read_bytes() + b" ")
        with self.assertRaises(HTTPError) as caught:
            self.fetch("/level-maps", self.server.token)
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(json.loads(caught.exception.read())["error"], "level_map_source_invalid")

    def test_runtime_activation_and_unprepared_state_are_live(self) -> None:
        self.server.level_map_runtime = None
        with self.assertRaises(HTTPError) as caught:
            self.fetch("/level-maps", self.server.token)
        self.assertEqual(caught.exception.code, 409)
        self.server.level_map_runtime = self.runtime
        self.assertEqual(self.fetch("/level-maps", self.server.token)[0], 200)
        write_json(self.runtime, {"format": "EndfieldRuntimeConfig/1", "extraction": {}})
        with self.assertRaises(HTTPError) as caught:
            self.fetch("/level-maps", self.server.token)
        self.assertEqual(json.loads(caught.exception.read())["error"], "level_maps_not_prepared")


if __name__ == "__main__":
    unittest.main()
