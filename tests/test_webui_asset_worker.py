"""Exercise durable asset jobs with a real small-file export connector."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from webui_asset_contract import parse_asset_export_request
from webui_asset_worker import AssetCoordinator
from webui_character_contract import (
    CharacterJobNotFound,
    CharacterRequestConflict,
    CharacterRequestError,
    request_digest,
)
from webui_character_worker import _write


Progress = Callable[[str, float, str], None]


def _payload(request_id: str, item_ids: list[str]) -> dict[str, object]:
    return {
        "format": "AssetExportRequest/1",
        "request_id": request_id,
        "kind": "weapon",
        "catalog_id": "catalog:test",
        "item_ids": item_ids,
        "variant": "base",
        "client_output_path": "F:/client-selected/path",
    }


class TinyAssetService:
    """Write a tiny real artifact through the worker's service boundary."""

    def __init__(self, game_root: Path, output_root: Path) -> None:
        self._game_root = game_root
        self._output_root = output_root
        self.validation_calls = 0
        self.export_calls = 0

    def catalog(self, kind: str) -> dict[str, object]:
        return {"format": "AssetCatalog/1", "kind": kind, "items": []}

    def validate(self, payload: object) -> dict[str, object]:
        parse_asset_export_request(payload)
        self.validation_calls += 1
        return {"status": "valid", "item_count": 1}

    def export(
        self,
        payload: object,
        output: Path,
        check_cancelled: Callable[[], None],
        progress: Progress,
    ) -> dict[str, object]:
        parse_asset_export_request(payload)
        check_cancelled()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise FileExistsError(f"Refusing to overwrite non-empty asset output: {output}")
        output.mkdir(parents=True, exist_ok=True)
        progress("writing", 0.5, "Writing a tiny test artifact")
        artifact = output / "asset.txt"
        artifact.write_text("asset export\n", encoding="utf-8")
        check_cancelled()
        progress("written", 0.9, "Tiny test artifact written")
        self.export_calls += 1
        return {"status": "completed", "files": [artifact.name]}

    def output_root(self) -> Path:
        return self._output_root

    def game_root(self) -> Path:
        return self._game_root


class PartialAssetService(TinyAssetService):
    def export(
        self,
        payload: object,
        output: Path,
        check_cancelled: Callable[[], None],
        progress: Progress,
    ) -> dict[str, object]:
        result = super().export(payload, output, check_cancelled, progress)
        return {**result, "status": "partial"}


class BlockingAssetService(TinyAssetService):
    def __init__(self, game_root: Path, output_root: Path) -> None:
        super().__init__(game_root, output_root)
        self.started = threading.Event()
        self.release = threading.Event()

    def export(
        self,
        payload: object,
        output: Path,
        check_cancelled: Callable[[], None],
        progress: Progress,
    ) -> dict[str, object]:
        self.started.set()
        while not self.release.wait(0.01):
            check_cancelled()
        check_cancelled()
        return super().export(payload, output, check_cancelled, progress)


class UnpreparedAssetService(TinyAssetService):
    def game_root(self) -> Path:
        raise CharacterRequestError("Character source has not been prepared")


class FailingAssetService(TinyAssetService):
    def export(
        self,
        payload: object,
        output: Path,
        check_cancelled: Callable[[], None],
        progress: Progress,
    ) -> dict[str, object]:
        raise RuntimeError(f"Private source path: {self._game_root / 'secret.dat'}")


class AssetCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.game_root = self.root / "game"
        self.output_root = self.root / "output"
        self.jobs_root = self.root / "jobs"
        self.game_root.mkdir()
        self.output_root.mkdir()
        self.coordinators: list[AssetCoordinator] = []

    def tearDown(self) -> None:
        for coordinator in self.coordinators:
            coordinator.shutdown()
        self.temporary.cleanup()

    def _coordinator(self, service: TinyAssetService, jobs_root: Path) -> AssetCoordinator:
        coordinator = AssetCoordinator(service, jobs_root)
        self.coordinators.append(coordinator)
        return coordinator

    def _wait_terminal(self, coordinator: AssetCoordinator, job_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            job = coordinator.get(job_id)
            if job["state"] in {"completed", "partial", "failed", "cancelled"}:
                return job
            time.sleep(0.01)
        self.fail(f"Asset job {job_id} did not reach a terminal state")
        raise AssertionError("unreachable")

    def test_complete_export_is_persisted_at_the_fixed_empty_output_path(self) -> None:
        service = TinyAssetService(self.game_root, self.output_root)
        coordinator = self._coordinator(service, self.jobs_root)
        request_id = str(uuid4())
        payload = _payload(request_id, ["weapon:test"])

        self.assertEqual(coordinator.catalog("weapon")["kind"], "weapon")
        self.assertEqual(coordinator.validate(payload)["status"], "valid")
        submitted = coordinator.submit(payload)
        job = self._wait_terminal(coordinator, request_id)

        expected_request = parse_asset_export_request(payload).as_json()
        artifact_dir = self.output_root / "asset_jobs" / request_id
        record_path = self.jobs_root / "records" / f"{request_id}.json"
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["request"], expected_request)
        self.assertNotIn("client_output_path", job["request"])
        self.assertTrue((artifact_dir / "asset.txt").is_file())
        self.assertEqual(submitted["job_id"], request_id)
        self.assertEqual(coordinator.get(request_id), job)
        self.assertEqual(json.loads(record_path.read_text(encoding="utf-8"))["job"]["state"], "completed")
        self.assertEqual(service.export_calls, 1)
        self.assertEqual(set(job), {
            "format", "job_id", "kind", "state", "phase", "progress", "message", "request", "result", "error",
        })

    def test_request_id_is_idempotent_and_rejects_changed_parameters(self) -> None:
        service = TinyAssetService(self.game_root, self.output_root)
        coordinator = self._coordinator(service, self.jobs_root)
        request_id = str(uuid4())
        payload = _payload(request_id, ["weapon:test"])

        first = coordinator.submit(payload)
        second = coordinator.submit(payload)
        self.assertEqual(first["job_id"], second["job_id"])
        with self.assertRaises(CharacterRequestConflict):
            coordinator.submit(_payload(request_id, ["weapon:other"]))
        self.assertEqual(self._wait_terminal(coordinator, request_id)["state"], "completed")
        self.assertEqual(service.export_calls, 1)

    def test_partial_service_result_is_a_partial_terminal_job(self) -> None:
        service = PartialAssetService(self.game_root, self.output_root)
        coordinator = self._coordinator(service, self.jobs_root)
        request_id = str(uuid4())

        coordinator.submit(_payload(request_id, ["weapon:test"]))
        job = self._wait_terminal(coordinator, request_id)

        self.assertEqual(job["state"], "partial")
        self.assertEqual(job["result"]["status"], "partial")

    def test_running_cancel_reaches_the_exporter_callback(self) -> None:
        service = BlockingAssetService(self.game_root, self.output_root)
        coordinator = self._coordinator(service, self.jobs_root)
        request_id = str(uuid4())
        coordinator.submit(_payload(request_id, ["weapon:test"]))
        self.assertTrue(service.started.wait(2.0))

        acknowledgement = coordinator.cancel(request_id)
        job = self._wait_terminal(coordinator, request_id)

        self.assertEqual(acknowledgement["state"], "accepted")
        self.assertEqual(job["state"], "cancelled")
        service.release.set()

    def test_shutdown_cancels_running_and_queued_jobs(self) -> None:
        service = BlockingAssetService(self.game_root, self.output_root)
        coordinator = self._coordinator(service, self.jobs_root)
        running_id = str(uuid4())
        queued_id = str(uuid4())
        coordinator.submit(_payload(running_id, ["weapon:running"]))
        self.assertTrue(service.started.wait(2.0))
        coordinator.submit(_payload(queued_id, ["weapon:queued"]))

        coordinator.shutdown()
        self.assertEqual(coordinator.get(running_id)["state"], "cancelled")
        self.assertEqual(coordinator.get(queued_id)["state"], "cancelled")
        self.assertEqual(service.export_calls, 0)
        service.release.set()

    def test_public_failure_is_safe_and_local_diagnostic_keeps_traceback(self) -> None:
        service = FailingAssetService(self.game_root, self.output_root)
        coordinator = self._coordinator(service, self.jobs_root)
        request_id = str(uuid4())

        coordinator.submit(_payload(request_id, ["weapon:test"]))
        job = self._wait_terminal(coordinator, request_id)
        record = json.loads((self.jobs_root / "records" / f"{request_id}.json").read_text(encoding="utf-8"))

        self.assertEqual(job["state"], "failed")
        self.assertEqual(set(job["error"]), {"message", "diagnostic_id"})
        self.assertNotIn(str(self.game_root), json.dumps(job["error"]))
        self.assertIn(str(self.game_root / "secret.dat"), record["diagnostic"]["traceback"])

    def test_interrupted_nonterminal_record_recovers_as_failed(self) -> None:
        service = TinyAssetService(self.game_root, self.output_root)
        request_id = str(uuid4())
        request = parse_asset_export_request(_payload(request_id, ["weapon:test"])).as_json()
        job = {
            "format": "AssetJob/1",
            "job_id": request_id,
            "kind": "weapon",
            "state": "running",
            "phase": "exporting",
            "progress": 0.3,
            "message": "Export in progress",
            "cancel_requested": False,
            "result": None,
            "error": None,
        }
        record = {
            "format": "AssetJobRecord/1",
            "session": str(uuid4()),
            "game_root": str(self.game_root),
            "output_root": str(self.output_root),
            "request": request,
            "request_digest": request_digest(request),
            "job": job,
        }
        _write(self.jobs_root / "records" / f"{request_id}.json", record)

        coordinator = self._coordinator(service, self.jobs_root)
        recovered = coordinator.get(request_id)

        self.assertEqual(recovered["state"], "failed")
        self.assertEqual(recovered["error"]["diagnostic_id"], request_id)

    def test_unprepared_service_can_start_and_shutdown_without_source_queries(self) -> None:
        service = UnpreparedAssetService(self.game_root, self.output_root)

        coordinator = self._coordinator(service, self.jobs_root)
        coordinator.shutdown()

    def test_job_storage_and_output_must_be_disjoint_from_sources_and_each_other(self) -> None:
        service = TinyAssetService(self.game_root, self.game_root / "generated")
        service.output_root().mkdir()
        coordinator = self._coordinator(service, self.jobs_root)

        with self.assertRaises(CharacterRequestError):
            coordinator.submit(_payload(str(uuid4()), ["weapon:test"]))

        disjoint_service = TinyAssetService(self.game_root, self.output_root)
        overlapping_jobs = self._coordinator(disjoint_service, self.output_root)
        with self.assertRaises(CharacterRequestError):
            overlapping_jobs.submit(_payload(str(uuid4()), ["weapon:test"]))

    def test_invalid_ids_and_missing_jobs_use_existing_http_error_types(self) -> None:
        coordinator = self._coordinator(TinyAssetService(self.game_root, self.output_root), self.jobs_root)

        with self.assertRaises(CharacterRequestError):
            coordinator.get("../escape")
        with self.assertRaises(CharacterJobNotFound):
            coordinator.get(str(uuid4()))
        with self.assertRaises(CharacterRequestError):
            coordinator.submit({"format": "AssetExportRequest/1"})


if __name__ == "__main__":
    unittest.main()
