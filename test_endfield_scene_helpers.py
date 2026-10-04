"""Validate managed helper upgrades independently of unchanged .NET apphosts."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from endfield_scene_helpers import verified_helpers


def descriptor(path: Path) -> dict[str, object]:
    return {"path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper()}


class SceneHelperIntegrityTests(unittest.TestCase):
    def test_old_source_and_apphost_only_receipts_require_upgrade(self) -> None:
        self.assertIsNone(verified_helpers({"sourceIdentity": "old"}, "new", Path.cwd()))
        self.assertIsNone(verified_helpers({"sourceIdentity": "new", "outputs": {
            "bundleScanner": {"path": "old.exe"}}}, "new", Path.cwd()))

    def test_managed_payload_change_is_detected_even_when_apphost_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs: dict[str, object] = {}
            for name in ("bundleScanner", "sceneProbe"):
                directory = root / name
                directory.mkdir()
                executable, assembly = directory / f"{name}.exe", directory / f"{name}.dll"
                executable.write_bytes(b"constant apphost")
                assembly.write_bytes(b"current managed payload")
                dependency = directory / "Dependency.dll"
                dependency.write_bytes(b"current runtime dependency")
                outputs[name] = {**descriptor(executable), "assembly": descriptor(assembly),
                                 "runtimeFiles": [descriptor(path) for path in directory.iterdir()]}
            result = {"sourceIdentity": "new", "outputs": outputs}
            helpers = verified_helpers(result, "new", root)
            self.assertIsNotNone(helpers)
            self.assertEqual(root / "sceneProbe" / "sceneProbe.exe", helpers.probe)
            (root / "sceneProbe" / "Dependency.dll").write_bytes(b"different runtime dependency")
            with self.assertRaisesRegex(ValueError, "integrity validation"):
                verified_helpers(result, "new", root)


if __name__ == "__main__":
    unittest.main()
