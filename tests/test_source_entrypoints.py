"""Verify public Python entrypoints independently of the working directory."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SourceEntrypointTests(unittest.TestCase):
    def test_entrypoints_load_from_an_unrelated_directory(self) -> None:
        root = Path(__file__).resolve().parents[1]
        entries = (
            root / "launch_webui.py",
            root / "src" / "endfield_first_run.py",
            root / "src" / "endfield_dependency_bootstrap.py",
            root / "src" / "build_endfield_map_layers.py",
        )
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            for entry in entries:
                with self.subTest(entry=entry.name):
                    result = subprocess.run(
                        [sys.executable, "-B", str(entry), "--help"],
                        cwd=directory,
                        env=environment,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("usage:", result.stdout)
