from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from endfield_dependency_bootstrap import (
    DependencyBootstrapError,
    ensure_pinned_checkout,
    run_logged,
)


class DependencyBootstrapTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_timeout_terminates_child_holding_stdout_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / "timeout.log"
            script = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "print('child started', flush=True); time.sleep(30)"
            )
            started = time.monotonic()
            with self.assertRaisesRegex(DependencyBootstrapError, "timed out"):
                run_logged(
                    [sys.executable, "-c", script],
                    cwd=root,
                    log_path=log,
                    error_code="TEST_TIMEOUT",
                    timeout_seconds=1,
                )

            self.assertLess(time.monotonic() - started, 10)
            self.assertIn("child started", log.read_text(encoding="utf-8"))

    def test_incomplete_checkout_retries_fetch(self) -> None:
        git = shutil.which("git.exe") or shutil.which("git")
        if not git:
            self.skipTest("Git is unavailable")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            workspace = root / "workspace"
            checkout = workspace / "AnimeStudio"
            logs = root / "logs"
            source.mkdir()
            workspace.mkdir()
            checkout.mkdir()

            def run(*args: str, cwd: Path) -> str:
                result = subprocess.run(
                    [git, *args],
                    cwd=cwd,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    check=True,
                )
                return result.stdout.strip()

            run("init", cwd=source)
            run("config", "user.email", "test@example.invalid", cwd=source)
            run("config", "user.name", "Endfield Bootstrap Test", cwd=source)
            (source / "fixture.txt").write_text("fixture\n", encoding="utf-8")
            run("add", "fixture.txt", cwd=source)
            run("commit", "-m", "fixture", cwd=source)
            commit = run("rev-parse", "HEAD", cwd=source)

            run("init", cwd=checkout)
            run("remote", "add", "origin", str(source), cwd=checkout)
            head = ensure_pinned_checkout(
                git,
                checkout=checkout,
                workspace=workspace,
                repository=str(source),
                commit=commit,
                log_dir=logs,
            )

            self.assertEqual(commit, head)
            self.assertEqual(commit, run("rev-parse", "HEAD", cwd=checkout))
            self.assertTrue((logs / "animestudio_fetch.log").is_file())
            self.assertTrue((logs / "animestudio_checkout.log").is_file())


if __name__ == "__main__":
    unittest.main()
