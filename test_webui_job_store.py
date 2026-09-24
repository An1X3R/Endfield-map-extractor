from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from test_webui_export_contract_v2 import payload
from webui_export_contract_v2 import canonicalize_job
from webui_job_store import JobStore


class JobStoreTests(unittest.TestCase):
    def test_polling_and_updates_preserve_atomic_state_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary))
            job_id = store.create(canonicalize_job(payload()))["job_id"]
            store.transition(job_id, "preparing", progress=0.0, message="prepare")

            def update() -> None:
                for index in range(100):
                    store.update_progress(job_id, phase="preparing", progress=index / 100, message="advance")

            def poll() -> None:
                for _ in range(100):
                    self.assertEqual(store.get(job_id)["state"], "preparing")
                    events = store.events(job_id)
                    self.assertEqual([row["sequence"] for row in events], list(range(1, len(events) + 1)))

            with ThreadPoolExecutor(max_workers=3) as workers:
                futures = [workers.submit(update), workers.submit(poll), workers.submit(store.request_cancel, job_id)]
                for future in futures:
                    future.result(timeout=15)
            self.assertTrue(store.get(job_id)["cancel_requested"])
            self.assertEqual(len(store.events(job_id)), 103)

    def test_create_transition_events_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary))
            job = store.create(canonicalize_job(payload()))
            self.assertEqual(job["state"], "queued")
            store.transition(job["job_id"], "preparing", progress=0.1, message="prepare")
            store.log(job["job_id"], "log line")
            store.request_cancel(job["job_id"])
            current = store.get(job["job_id"])
            self.assertTrue(current["cancel_requested"])
            self.assertTrue(store.cancel_requested(job["job_id"]))
            self.assertGreaterEqual(len(store.events(job["job_id"])), 3)
            with self.assertRaises(ValueError):
                store.transition(job["job_id"], "completed")


if __name__ == "__main__":
    unittest.main()
