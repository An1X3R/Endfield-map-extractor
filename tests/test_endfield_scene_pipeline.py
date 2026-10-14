from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable

from endfield_scene_export import run_scene_process
from endfield_scene_pipeline import run_scene_pipeline
from tests.test_endfield_scene_export import ProcessCancelled, process_is_running


class ScenePipelineTests(unittest.TestCase):
    def test_real_workers_overlap_and_results_keep_batch_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = threading.get_ident()

            def prepare(index: int, check: Callable[[], None]) -> int:
                self.assertEqual(owner, threading.get_ident())
                check()
                return index

            def execute(index: int, check: Callable[[], None]) -> int:
                path = root / f"{index}.json"
                code = ("import json, os, time; from pathlib import Path; start=time.time_ns(); "
                        f"time.sleep({0.7 if index == 0 else 0.3}); "
                        f"Path({str(path)!r}).write_text(json.dumps([start,time.time_ns(),os.getpid()]))")
                run_scene_process([sys.executable, "-c", code], root / f"{index}.log", dict(os.environ), check)
                return index

            completed: list[int] = []
            result = run_scene_pipeline(3, 2, prepare, execute, lambda item, active: True,
                                        lambda: None, lambda index, value: completed.append(index))
            spans = [json.loads((root / f"{index}.json").read_text()) for index in range(3)]
            self.assertLess(max(spans[0][0], spans[1][0]), min(spans[0][1], spans[1][1]))
            self.assertEqual([0, 1, 2], result)
            self.assertEqual([0, 1, 2], sorted(completed))
            self.assertTrue(all(sum(start <= point < end for start, end, _ in spans) <= 2
                                for point, _, _ in spans))

    def test_memory_gate_serializes_workers_but_preparation_overlaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared_while_running: list[bool] = []

            def prepare(index: int, check: Callable[[], None]) -> int:
                if index:
                    deadline = time.monotonic() + 5
                    while not (root / "started").exists():
                        check()
                        if time.monotonic() >= deadline:
                            self.fail("First worker never started")
                        time.sleep(0.01)
                    prepared_while_running.append(not (root / "finished").exists())
                return index

            def execute(index: int, check: Callable[[], None]) -> int:
                if index == 0:
                    code = ("from pathlib import Path; import time; "
                            f"Path({str(root / 'started')!r}).write_text('1'); time.sleep(0.5); "
                            f"Path({str(root / 'finished')!r}).write_text('1')")
                    run_scene_process([sys.executable, "-c", code], root / "first.log", dict(os.environ), check)
                else:
                    self.assertTrue((root / "finished").exists())
                return index

            result = run_scene_pipeline(2, 2, prepare, execute, lambda item, active: False,
                                        lambda: None, lambda index, value: None)
            self.assertEqual([True], prepared_while_running)
            self.assertEqual([0, 1], result)

    def test_cancellation_joins_and_stops_both_real_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deadline = time.monotonic() + 8

            def check() -> None:
                if all((root / f"{index}.pid").exists() for index in range(2)):
                    raise ProcessCancelled("cancel both scene workers")
                if time.monotonic() >= deadline:
                    self.fail("Scene workers failed to start before cancellation deadline")

            def execute(index: int, check_worker: Callable[[], None]) -> int:
                code = ("from pathlib import Path; import os,time; "
                        f"Path({str(root / f'{index}.pid')!r}).write_text(str(os.getpid())); time.sleep(30)")
                run_scene_process([sys.executable, "-c", code], root / f"{index}.log", dict(os.environ), check_worker)
                return index

            with self.assertRaises(ProcessCancelled):
                run_scene_pipeline(2, 2, lambda index, callback: index, execute,
                                   lambda item, active: True, check, lambda index, value: None)
            for index in range(2):
                self.assertFalse(process_is_running(int((root / f"{index}.pid").read_text())))

    def test_failure_preserves_original_exit_code_and_stops_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def execute(index: int, check: Callable[[], None]) -> int:
                code = ("from pathlib import Path; import os,time,sys; "
                        f"Path({str(root / f'{index}.pid')!r}).write_text(str(os.getpid())); "
                        f"time.sleep({0.75 if index == 0 else 30}); sys.exit({23 if index == 0 else 0})")
                run_scene_process([sys.executable, "-c", code], root / f"{index}.log", dict(os.environ), check)
                return index

            with self.assertRaisesRegex(RuntimeError, "code 23"):
                run_scene_pipeline(2, 2, lambda index, callback: index, execute,
                                   lambda item, active: True, lambda: None, lambda index, value: None)
            for index in range(2):
                self.assertFalse(process_is_running(int((root / f"{index}.pid").read_text())))


if __name__ == "__main__":
    unittest.main()
