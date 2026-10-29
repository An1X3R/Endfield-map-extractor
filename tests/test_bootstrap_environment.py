from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import bootstrap_environment


class BootstrapEnvironmentTests(unittest.TestCase):
    def test_python_range_matches_pinned_numba_and_numpy_wheels(self) -> None:
        for version in ((3, 11, 9), (3, 14, 0)):
            with self.subTest(version=version):
                with mock.patch.object(bootstrap_environment.sys, "version_info", version):
                    bootstrap_environment.require_supported_python()

        for version in ((3, 10, 14), (3, 15, 0), (3, 16, 0)):
            with self.subTest(version=version):
                with mock.patch.object(bootstrap_environment.sys, "version_info", version):
                    with self.assertRaisesRegex(RuntimeError, "CPython 3.11 through 3.14"):
                        bootstrap_environment.require_supported_python()

        with mock.patch.object(bootstrap_environment.sys, "implementation", SimpleNamespace(name="pypy")):
            with self.assertRaisesRegex(RuntimeError, "CPython 3.11 through 3.14"):
                bootstrap_environment.require_supported_python()

    def test_offline_requirement_check_disables_index_and_dependency_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requirements = root / "requirements.txt"
            requirements.write_text("sample-package==1.0\n", encoding="utf-8")
            interpreter = root / "python.exe"
            with mock.patch.object(
                bootstrap_environment.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ) as run:
                ready, message = bootstrap_environment.verify_requirements(interpreter, requirements)

            self.assertTrue(ready)
            self.assertIn("offline", message)
            command = run.call_args.args[0]
            self.assertIn("--dry-run", command)
            self.assertIn("--no-index", command)
            self.assertIn("--no-deps", command)

    def test_rejects_prepared_python_outside_the_supported_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requirements = root / "requirements.txt"
            requirements.write_text("sample-package==1.0\n", encoding="utf-8")
            interpreter = root / "python.exe"
            with mock.patch.object(
                bootstrap_environment.subprocess,
                "run",
                return_value=mock.Mock(returncode=1, stdout="3.15\n", stderr=""),
            ) as run:
                ready, message = bootstrap_environment.verify_requirements(interpreter, requirements)

            self.assertFalse(ready)
            self.assertIn("3.15", message)
            self.assertEqual(run.call_count, 1)

    def test_normal_requirement_gate_does_not_create_missing_virtualenv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(bootstrap_environment, "VENV_ROOT", root / ".venv"),
                mock.patch.object(bootstrap_environment, "REQUIREMENTS", root / "requirements.txt"),
            ):
                with self.assertRaisesRegex(RuntimeError, "setup_development_environment.ps1"):
                    bootstrap_environment.require_virtualenv()
            self.assertFalse((root / ".venv").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
