from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from endfield_scene_discovery import (
    CHECKPOINT_FORMAT,
    complete_checkpoint,
    describe_file,
    helper_progress,
    observe_helper,
    read_asset_bundle_candidate_names_by_map,
    read_checkpoint,
    stage_work,
    tool_identity,
)


def no_cancel() -> None:
    return None


class SceneDiscoveryCheckpointTests(unittest.TestCase):
    def test_map_roots_ignore_package_containers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            results = Path(temporary) / "providers.jsonl"
            rows = (
                {"kind": "match", "logical_name": "asset_map01", "containers": ["assets/map01/lv001/scene"]},
                {"kind": "match", "logical_name": "package_map01", "containers": ["packages/map01/lv001/scene"]},
                {"kind": "match", "logical_name": "mixed_map01",
                 "containers": ["packages/map01/lv002/scene", "assets/map01/lv002/scene"]},
                {"kind": "match", "logical_name": "asset_map02", "containers": ["Assets\\Map02\\Lv007\\Scene"]},
                {"kind": "match", "logical_name": "package_map02", "containers": ["packages/map02/lv007/scene"]},
                {"kind": "complete", "scanned": 5, "failed": 0, "matches": 5},
            )
            results.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            roots = read_asset_bundle_candidate_names_by_map(results)

            self.assertEqual({"map01": ["asset_map01", "mixed_map01"], "map02": ["asset_map02"]}, roots)

    def test_completed_checkpoint_reuses_verified_file_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint, work = stage_work(root, "scan")
            work.mkdir()
            artifact = work / "providers.jsonl"
            artifact.write_text('{"kind":"complete","failed":0}\n', encoding="utf-8")
            outputs = {"providerCount": 3}

            complete_checkpoint(checkpoint, "source-a", outputs, (artifact,), no_cancel)
            actual, reason = read_checkpoint(checkpoint, "source-a", no_cancel)

            self.assertEqual(outputs, actual)
            self.assertEqual("verified", reason)
            self.assertEqual(CHECKPOINT_FORMAT, json.loads(checkpoint.read_text(encoding="utf-8"))["format"])

    def test_checkpoint_rejects_source_change_and_same_length_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint, work = stage_work(root, "probe")
            work.mkdir()
            artifact = work / "manifest.json"
            artifact.write_text('{"status":"ok"}', encoding="utf-8")
            complete_checkpoint(checkpoint, "source-a", {"map": "map01"}, (artifact,), no_cancel)

            outputs, reason = read_checkpoint(checkpoint, "source-b", no_cancel)
            self.assertIsNone(outputs)
            self.assertEqual("source_or_tool_changed", reason)

            original_size = artifact.stat().st_size
            artifact.write_text('{"status":"NO"}', encoding="utf-8")
            self.assertEqual(original_size, artifact.stat().st_size)
            outputs, reason = read_checkpoint(checkpoint, "source-a", no_cancel)
            self.assertIsNone(outputs)
            self.assertEqual(f"output_changed:{artifact.resolve()}", reason)

    def test_checkpoint_rejects_scanner_descriptor_detached_from_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint, work = stage_work(root, "scan")
            work.mkdir()
            artifact = work / "providers.jsonl"
            artifact.write_text('{"kind":"complete","failed":0}\n', encoding="utf-8")
            decoy = root / "decoy.jsonl"
            decoy.write_bytes(artifact.read_bytes())
            outputs = {"scannerResults": describe_file(artifact)}
            complete_checkpoint(checkpoint, "scan-source", outputs, (artifact,), no_cancel)

            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            payload["outputs"]["scannerResults"] = describe_file(decoy)
            checkpoint.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not a verified artifact: scannerResults"):
                read_checkpoint(checkpoint, "scan-source", no_cancel)

    def test_checkpoint_rejects_extra_file_in_bundle_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint, work = stage_work(root, "map01_bundles")
            work.mkdir()
            bundle_root = work / "bundles"
            bundle_root.mkdir()
            first_file = bundle_root / "first.bundle"
            first_file.write_bytes(b"first")
            outputs = {"root": str(bundle_root)}
            complete_checkpoint(checkpoint, "bundle-source", outputs, (first_file,), no_cancel)

            actual, reason = read_checkpoint(checkpoint, "bundle-source", no_cancel)
            self.assertEqual(outputs, actual)
            self.assertEqual("verified", reason)

            (bundle_root / "unindexed.bundle").write_bytes(b"extra")
            actual, reason = read_checkpoint(checkpoint, "bundle-source", no_cancel)
            self.assertIsNone(actual)
            self.assertEqual(f"output_inventory_changed:{bundle_root.resolve()}", reason)

    def test_checkpoint_rejects_missing_output_and_stages_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan_checkpoint, scan_work = stage_work(root, "scan")
            probe_checkpoint, probe_work = stage_work(root, "map01_probe")
            scan_work.mkdir()
            probe_work.mkdir()
            artifact = scan_work / "scan.jsonl"
            artifact.write_text('{"kind":"complete","failed":0}\n', encoding="utf-8")
            complete_checkpoint(scan_checkpoint, "scan-source", {"done": True}, (artifact,), no_cancel)

            scan_outputs, scan_reason = read_checkpoint(scan_checkpoint, "scan-source", no_cancel)
            probe_outputs, probe_reason = read_checkpoint(probe_checkpoint, "probe-source", no_cancel)
            self.assertEqual({"done": True}, scan_outputs)
            self.assertEqual("verified", scan_reason)
            self.assertIsNone(probe_outputs)
            self.assertEqual("not_prepared", probe_reason)
            self.assertNotEqual(scan_checkpoint.parent, probe_checkpoint.parent)

            artifact.unlink()
            scan_outputs, missing_reason = read_checkpoint(scan_checkpoint, "scan-source", no_cancel)
            self.assertIsNone(scan_outputs)
            self.assertEqual(f"output_changed:{artifact.resolve()}", missing_reason)

    def test_tool_identity_changes_when_managed_dll_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tool_dir = Path(temporary) / "SceneProbe"
            tool_dir.mkdir()
            executable = tool_dir / "SceneProbe.exe"
            library = tool_dir / "SceneProbe.dll"
            executable.write_bytes(b"same apphost")
            library.write_bytes(b"managed build one")
            first_identity = tool_identity(executable)

            library.write_bytes(b"managed build two")
            second_identity = tool_identity(executable)

            self.assertEqual(b"same apphost", executable.read_bytes())
            self.assertNotEqual(first_identity, second_identity)


class SceneDiscoveryTelemetryTests(unittest.TestCase):
    def test_helper_progress_accepts_missing_metrics_and_rejects_invalid_numbers(self) -> None:
        minimal = {"format": "EndfieldHelperProgress/1", "phase": "read"}
        self.assertEqual(minimal, helper_progress(json.dumps(minimal)))

        invalid_metrics = (
            {"current": -1},
            {"current": 4, "total": 3},
            {"readBytes": 1.5},
            {"peakWorkingSetBytes": True},
            {"elapsedSeconds": -0.25},
            {"elapsedSeconds": "fast"},
        )
        for metrics in invalid_metrics:
            with self.subTest(metrics=metrics):
                payload = {**minimal, **metrics}
                with self.assertRaises(ValueError):
                    helper_progress(json.dumps(payload))

    def test_observe_helper_relays_separate_progress_rows_and_surfaces_exit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = {"format": "EndfieldHelperProgress/1", "phase": "object_parse", "current": 1, "total": 2}
            second = {"format": "EndfieldHelperProgress/1", "phase": "mesh_export", "current": 2, "total": 2}
            code = (
                "import json,time;"
                f"print({json.dumps(first)!r}, flush=True);"
                "time.sleep(0.45);"
                f"print({json.dumps(second)!r}, flush=True)"
            )
            observations: list[tuple[float, dict[str, object]]] = []

            def emit(row: dict[str, object]) -> None:
                observations.append((time.perf_counter(), row))

            observe_helper(
                [sys.executable, "-c", code],
                root / "helper.log",
                root / "helper-temp",
                no_cancel,
                emit,
            )

            self.assertEqual([first, second], [row for _, row in observations])
            self.assertGreaterEqual(observations[1][0] - observations[0][0], 0.15)

            with self.assertRaisesRegex(RuntimeError, "exited with code 7"):
                observe_helper(
                    [sys.executable, "-c", "raise SystemExit(7)"],
                    root / "failing-helper.log",
                    root / "failing-helper-temp",
                    no_cancel,
                    lambda _row: None,
                )


if __name__ == "__main__":
    unittest.main()
