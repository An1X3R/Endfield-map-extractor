from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from test_webui_export_contract_v2 import payload
from webui_export_contract_v2 import canonicalize_job
from webui_job_store import JobStore


class JobStoreTests(unittest.TestCase):
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
