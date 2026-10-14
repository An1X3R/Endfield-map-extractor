"""Run the native Blender shoreline regression when an executable is supplied."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest

from tests import REPO_ROOT


class WaterShoreIntegrationTests(unittest.TestCase):
    def test_native_mesh_cleanup_preserves_attributes(self) -> None:
        executable = os.environ.get("ENDFIELD_TEST_BLENDER")
        if not executable:
            self.skipTest("Set ENDFIELD_TEST_BLENDER to run the native Blender mesh regression")
        self.assertTrue(Path(executable).is_file(), executable)
        result = subprocess.run(
            [executable, "--background", "--factory-startup", "--python-exit-code", "1", "--python",
             str(REPO_ROOT / "tests" / "blender_water_shore_regression.py")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"water_shore_regression": "passed"', result.stdout)


if __name__ == "__main__":
    unittest.main()
