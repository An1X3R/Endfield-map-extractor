"""Exercise portable scene helper receipts against the installed real runtimes."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from endfield_scene_helpers import load_cached_scene_helper_report, require_prepared_scene_helpers, verified_helpers
from endfield_scene_tool_bundle import export_scene_tool_bundle
from tests import REPO_ROOT, SRC_ROOT


class SceneToolBundleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cache_root = REPO_ROOT / ".tools" / "scene"
        cls.source = load_cached_scene_helper_report(cls.cache_root, SRC_ROOT)
        if cls.source is None:
            raise unittest.SkipTest("A hash-verified real scene helper install is required for bundle integration tests.")

    def test_moved_bundle_initializes_real_tools_and_resolves_without_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "original" / "scene"
            receipt = export_scene_tool_bundle(self.cache_root, destination)
            serialized = receipt.read_text(encoding="utf-8")
            report = json.loads(serialized)
            self.assertNotIn("workspace", report)
            self.assertNotIn(str(REPO_ROOT), serialized)
            self.assertEqual(self.source.result["sourceIdentity"], report["sourceIdentity"])
            for output in report["outputs"].values():
                self.assertFalse(Path(output["path"]).is_absolute())
                self.assertFalse(Path(output["assembly"]["path"]).is_absolute())
                self.assertTrue(all(not Path(row["path"]).is_absolute() for row in output["runtimeFiles"]))
            moved = root / "relocated-scene"
            destination.rename(moved)
            discovered = load_cached_scene_helper_report(moved, SRC_ROOT)
            self.assertIsNotNone(discovered)
            identity = str(report["sourceIdentity"])
            helpers = verified_helpers(discovered.result, identity, root / "unrelated-first-run")
            self.assertIsNotNone(helpers)
            self.assertTrue(helpers.scanner.is_relative_to(moved))
            self.assertTrue(helpers.probe.is_relative_to(moved))
            for executable in (helpers.scanner, helpers.probe):
                process = subprocess.run([str(executable), "--initialize-runtime"], cwd=root,
                                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                                         timeout=30, check=False)
                self.assertEqual(0, process.returncode, process.stdout + process.stderr)
            resolved = require_prepared_scene_helpers(root / "runtime.json", moved)
            self.assertEqual(helpers, resolved)

    def test_bundle_refuses_existing_destination_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "scene"
            destination.mkdir()
            existing = destination / "user-file.txt"
            existing.write_text("preserve this install", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "Refusing to replace"):
                export_scene_tool_bundle(self.cache_root, destination)
            self.assertEqual("preserve this install", existing.read_text(encoding="utf-8"))
            self.assertEqual([existing], list(destination.iterdir()))

    def test_moved_bundle_rejects_modified_assembly_without_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "scene"
            receipt = export_scene_tool_bundle(self.cache_root, destination)
            report = json.loads(receipt.read_text(encoding="utf-8"))
            assembly = receipt.parent / report["outputs"]["sceneProbe"]["assembly"]["path"]
            with assembly.open("ab") as stream:
                stream.write(b"integrity-regression")
            with self.assertRaisesRegex(ValueError, "integrity validation"):
                load_cached_scene_helper_report(destination, SRC_ROOT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
