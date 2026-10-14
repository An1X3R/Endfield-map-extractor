from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bootstrap_environment


class BootstrapEnvironmentTests(unittest.TestCase):
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
