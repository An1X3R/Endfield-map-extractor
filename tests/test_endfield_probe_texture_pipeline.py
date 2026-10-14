"""Run actual image ownership and completion-order checks in the compiled helper."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest
import uuid


class ProbeTexturePipelineTests(unittest.TestCase):
    def test_native_encoding_completion_and_disposal(self) -> None:
        executable = os.environ.get("ENDFIELD_TEST_TEXTURE_REGRESSION")
        if not executable:
            self.skipTest("Set ENDFIELD_TEST_TEXTURE_REGRESSION to the compiled native regression entry point")
        self.assertTrue(Path(executable).is_file(), executable)
        output_root = os.environ.get("ENDFIELD_TEST_TEXTURE_OUTPUT_ROOT")
        self.assertIsNotNone(output_root, "ENDFIELD_TEST_TEXTURE_OUTPUT_ROOT is required for native artifacts")
        output = Path(str(output_root)) / ("regression_" + uuid.uuid4().hex)
        result = subprocess.run([executable, str(output)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TEXTURE_PIPELINE_REGRESSION_PASSED", result.stdout)


if __name__ == "__main__":
    unittest.main()
