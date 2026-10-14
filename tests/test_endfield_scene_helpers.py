from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import endfield_scene_helpers


def _descriptor(path: Path, relative_to: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(relative_to).as_posix(),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest().upper(),
    }


def _write_helper_receipt(cache_root: Path, identity: str, marker: str) -> Path:
    helper_root = cache_root / "helpers" / identity[:16]
    outputs: dict[str, object] = {}
    for name in ("bundleScanner", "sceneProbe"):
        executable_root = helper_root / "bin" / name
        executable_root.mkdir(parents=True, exist_ok=True)
        files = {
            f"{name}.exe": f"{marker}:{name}:exe".encode(),
            f"{name}.dll": f"{marker}:{name}:assembly".encode(),
            f"{name}.runtimeconfig.json": f"{{\"marker\":\"{marker}\"}}".encode(),
        }
        for file_name, payload in files.items():
            (executable_root / file_name).write_bytes(payload)
        executable = executable_root / f"{name}.exe"
        assembly = executable.with_suffix(".dll")
        outputs[name] = {
            **_descriptor(executable, helper_root),
            "assembly": _descriptor(assembly, helper_root),
            "runtimeFiles": [_descriptor(executable_root / file_name, executable_root) for file_name in files],
        }
    receipt = helper_root / "current.json"
    receipt.write_text(json.dumps({"sourceIdentity": identity, "outputs": outputs}), encoding="utf-8")
    return receipt


class PreparedSceneHelperTests(unittest.TestCase):
    def test_missing_helpers_fail_with_explicit_setup_command_without_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_path = root / "runtime" / "endfield.runtime.local.json"
            runtime_path.parent.mkdir()
            runtime_path.write_text("{}", encoding="utf-8")
            cache_root = root / "cache"
            with (
                mock.patch.object(endfield_scene_helpers, "_helper_identity", return_value="a" * 64),
                mock.patch.object(endfield_scene_helpers.subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(FileNotFoundError, "-PrepareSceneHelpers"):
                    endfield_scene_helpers.require_prepared_scene_helpers(runtime_path, cache_root)
            run.assert_not_called()
            self.assertFalse(cache_root.exists())

    def test_project_local_install_uses_its_own_receipt_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "project" / "src"
            source_root.mkdir(parents=True)
            identity = "b" * 64
            project_cache = source_root.parent / ".tools" / "scene"
            receipt = _write_helper_receipt(project_cache, identity, "project")

            with mock.patch.object(endfield_scene_helpers, "_helper_identity", return_value=identity):
                result = endfield_scene_helpers.load_cached_scene_helper_report(root / "user-cache", source_root)

            self.assertIsNotNone(result)
            self.assertEqual(receipt.resolve(), result.receipt_path)
            self.assertEqual(project_cache / "helpers" / identity[:16], result.helper_root)
            helpers = endfield_scene_helpers.verified_helpers(result.result, identity, result.helper_root)
            self.assertIsNotNone(helpers)
            self.assertEqual(result.helper_root / "bin" / "bundleScanner" / "bundleScanner.exe", helpers.scanner)
            self.assertEqual(result.helper_root / "bin" / "sceneProbe" / "sceneProbe.exe", helpers.probe)

    def test_configured_cache_keeps_priority_over_project_local_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "project" / "src"
            source_root.mkdir(parents=True)
            identity = "c" * 64
            configured_cache = root / "user-cache"
            configured_receipt = _write_helper_receipt(configured_cache, identity, "user")
            project_receipt = _write_helper_receipt(source_root.parent / ".tools" / "scene", identity, "project")

            with mock.patch.object(endfield_scene_helpers, "_helper_identity", return_value=identity):
                result = endfield_scene_helpers.load_cached_scene_helper_report(configured_cache, source_root)

            self.assertIsNotNone(result)
            self.assertEqual(configured_receipt.resolve(), result.receipt_path)
            self.assertNotEqual(project_receipt.resolve(), result.receipt_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
