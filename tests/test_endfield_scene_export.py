from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from endfield_scene_export import (
    _can_reuse_preflight_plan,
    _refresh_after_resource_writes,
    _scene_export_io_session,
    copy_preflight_plan,
    run_scene_process,
    scene_batches,
    scene_runtime,
)
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_io import (_SOURCE_CACHE_BYTE_BUDGET, SceneSourceCacheSlot,
                               cached_scene_source_value, scene_io_session)


class ProcessCancelled(RuntimeError):
    pass


def create_runtime_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    config_root = root / "config"
    prepared_root = config_root / "prepared"
    prepared_root.mkdir(parents=True)
    game_root = root / "portable-install-name"
    game_root.mkdir()
    (game_root / "Endfield.exe").write_bytes(b"")
    global_managers = game_root / "Endfield_Data" / "globalgamemanagers"
    global_managers.parent.mkdir(parents=True)
    global_managers.write_bytes(b"")
    (game_root / "Endfield_Data" / "StreamingAssets" / "VFS").mkdir(parents=True)
    instances_path = prepared_root / "instances.json"
    instances_path.write_text("{}", encoding="utf-8")
    probe_path = prepared_root / "scene_manifest.json"
    probe_path.write_text("{}", encoding="utf-8")
    runtime_path = config_root / "runtime.json"
    runtime_path.write_text(
        json.dumps(
            {
                "format": "EndfieldRuntimeConfig/1",
                "paths": {"game_root": os.path.relpath(game_root, config_root)},
                "extraction": {
                    "maps": {
                        "map01": {
                            "instances": "prepared/instances.json",
                            "sceneProbe": {"path": "prepared/scene_manifest.json"},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return runtime_path, game_root, instances_path, probe_path


def process_is_running(process_id: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, process_id)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return False
            raise ctypes.WinError(error)
        try:
            status = kernel32.WaitForSingleObject(handle, 0)
            if status not in (0, 0x102):
                raise ctypes.WinError(ctypes.get_last_error())
            return status == 0x102
        finally:
            if not kernel32.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
    try:
        os.kill(process_id, 0)
    except OSError:
        return False
    return True


def stop_process(process_id: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.kill(process_id, signal.SIGTERM)
        except ProcessLookupError:
            return


class SceneBatchTests(unittest.TestCase):
    def test_per_sector_partitions_negative_and_aligned_bounds(self) -> None:
        batches = scene_batches(
            {
                "batch_mode": "per_sector",
                "bounds": {"xmin": -129, "xmax": 129, "zmin": 0, "zmax": 128},
            }
        )
        self.assertEqual(
            (
                {"xmin": -129.0, "xmax": -128.0, "zmin": 0.0, "zmax": 128.0},
                {"xmin": -128.0, "xmax": 0.0, "zmin": 0.0, "zmax": 128.0},
                {"xmin": 0.0, "xmax": 128.0, "zmin": 0.0, "zmax": 128.0},
                {"xmin": 128.0, "xmax": 129.0, "zmin": 0.0, "zmax": 128.0},
            ),
            batches,
        )

    def test_cluster_batches_use_four_sector_width_and_clip_edges(self) -> None:
        batches = scene_batches(
            {
                "batch_mode": "cluster_4x4_sectors",
                "bounds": {"xmin": -512, "xmax": 512, "zmin": 511, "zmax": 513},
            }
        )
        self.assertEqual(
            (
                {"xmin": -512.0, "xmax": 0.0, "zmin": 511.0, "zmax": 512.0},
                {"xmin": 0.0, "xmax": 512.0, "zmin": 511.0, "zmax": 512.0},
                {"xmin": -512.0, "xmax": 0.0, "zmin": 512.0, "zmax": 513.0},
                {"xmin": 0.0, "xmax": 512.0, "zmin": 512.0, "zmax": 513.0},
            ),
            batches,
        )

    def test_merged_selection_preserves_bounds(self) -> None:
        bounds = {"xmin": -3, "xmax": 7, "zmin": -11, "zmax": 13}
        self.assertEqual(
            ({"xmin": -3.0, "xmax": 7.0, "zmin": -11.0, "zmax": 13.0},),
            scene_batches({"batch_mode": "merged_selection", "bounds": bounds}),
        )
        self.assertEqual({"xmin": -3, "xmax": 7, "zmin": -11, "zmax": 13}, bounds)


class PreflightReuseTests(unittest.TestCase):
    def test_single_plan_scope_requires_exact_preflight_selection(self) -> None:
        bounds = {"xmin": 0.0, "xmax": 128.0, "zmin": 0.0, "zmax": 128.0}
        canonical = {"batch_mode": "merged_selection", "bounds": bounds}
        batches = scene_batches(canonical)
        self.assertTrue(_can_reuse_preflight_plan(canonical, batches))
        with _scene_export_io_session(canonical, batches) as session:
            self.assertEqual(_SOURCE_CACHE_BYTE_BUDGET, session.source_cache_byte_budget)
            self.assertEqual(frozenset({"scene-manifest-index", "path-id-index", "dynamic-scene-renderers"}),
                             session.bypassed_source_cache_slots)

        other_selection = {**canonical, "selection_bounds": {**bounds, "xmax": 256.0}}
        self.assertFalse(_can_reuse_preflight_plan(other_selection, batches))
        with _scene_export_io_session(other_selection, batches) as session:
            self.assertEqual(_SOURCE_CACHE_BYTE_BUDGET, session.source_cache_byte_budget)
            self.assertEqual(frozenset(), session.bypassed_source_cache_slots)

        multiple = {"batch_mode": "per_sector", "bounds": {**bounds, "xmax": 256.0}}
        batches = scene_batches(multiple)
        self.assertEqual(2, len(batches))
        self.assertFalse(_can_reuse_preflight_plan(multiple, batches))
        with _scene_export_io_session(multiple, batches) as session:
            self.assertEqual(_SOURCE_CACHE_BYTE_BUDGET, session.source_cache_byte_budget)
            self.assertEqual(frozenset(), session.bypassed_source_cache_slots)

    def test_copies_validated_plan_and_sidecars_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            decals = root / "decals.json"
            materials = root / "materials.json"
            decals.write_text('{"records": []}', encoding="utf-8")
            materials.write_text('{"Materials": []}', encoding="utf-8")
            source = root / "preflight.json"
            source.write_text(json.dumps({
                "format": "EndfieldPortableScenePlan/1", "map_id": "map01",
                "bounds": {"xmin": 0, "xmax": 1, "zmin": 0, "zmax": 1},
                "scene_manifests": [], "terrain_surfaces": [], "category_filter": [],
                "decal_manifest": str(decals), "decal_materials": str(materials),
                "instances": [], "pending": [], "source_sha256": {},
                "summary": {"selected_static": 0, "ready_static": 0, "selected_decals": 0,
                            "ready_decals": 0, "pending": 0},
            }), encoding="utf-8")
            source_bytes = source.read_bytes()
            directory = root / "batch"
            directory.mkdir()
            target = copy_preflight_plan(source, directory / "scene_plan.json")
            original = load_scene_export_plan(source)
            copied = load_scene_export_plan(target)
            for field in ("decal_manifest", "decal_materials"):
                self.assertEqual(Path(copied[field]).parent, directory)
                self.assertEqual(Path(copied[field]).read_bytes(), Path(original[field]).read_bytes())
                copied[field] = original[field]
            self.assertEqual(original, copied)
            self.assertEqual(source_bytes, source.read_bytes())
            with self.assertRaises(FileExistsError):
                copy_preflight_plan(source, target)
            decals.unlink()
            with self.assertRaises(FileNotFoundError):
                copy_preflight_plan(source, directory / "missing_source.json")
            self.assertFalse((directory / "missing_source.json").exists())


class ResourceSourceInvalidationTests(unittest.TestCase):
    def test_dependency_extraction_retains_unchanged_sources_and_rechecks_changed_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "provider.jsonl"
            source.write_text("stable", encoding="utf-8")
            slot = SceneSourceCacheSlot("resource-invalidation-test", str, 1, 64)
            with scene_io_session() as session:
                cached_scene_source_value(
                    slot, (source,), (), lambda: source.read_text(encoding="utf-8"))
                _refresh_after_resource_writes(session, {"missingBefore": 1})
                self.assertEqual(1, session.statistics()["retained_source_cache_entries"])
                _refresh_after_resource_writes(session, {"extraction": {"resources": 1}})
                self.assertEqual(1, session.statistics()["retained_source_cache_entries"])
                self.assertEqual("stable", cached_scene_source_value(
                    slot, (source,), (), lambda: source.read_text(encoding="utf-8")))
                self.assertEqual(1, session.statistics()["source_cache_hits"])
                source.write_text("changed", encoding="utf-8")
                _refresh_after_resource_writes(session, {"extraction": {"resources": 1}})
                self.assertEqual("changed", cached_scene_source_value(
                    slot, (source,), (), lambda: source.read_text(encoding="utf-8")))
                self.assertEqual(2, session.statistics()["source_cache_misses"])


class SceneRuntimeTests(unittest.TestCase):
    def test_resolves_relative_runtime_paths_for_renamed_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_path, game_root, instances_path, probe_path = create_runtime_fixture(Path(temporary))
            runtime = scene_runtime(runtime_path, game_root, "map01")
            self.assertEqual("EndfieldRuntimeConfig/1", runtime["format"])
            self.assertTrue(instances_path.is_file())
            self.assertTrue(probe_path.is_file())

    def test_rejects_game_root_different_from_prepared_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_path, game_root, _, _ = create_runtime_fixture(Path(temporary))
            other_game = Path(temporary) / "another-install"
            other_game.mkdir()
            with self.assertRaisesRegex(ValueError, "Selected game differs"):
                scene_runtime(runtime_path, other_game, "map01")
            self.assertTrue(game_root.is_dir())

    def test_rejects_missing_prepared_scene_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_path, game_root, _, probe_path = create_runtime_fixture(Path(temporary))
            probe_path.unlink()
            with self.assertRaisesRegex(FileNotFoundError, "Prepared sceneProbe is missing"):
                scene_runtime(runtime_path, game_root, "map01")


class SceneProcessTests(unittest.TestCase):
    def test_successful_child_process_captures_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "child.log"
            run_scene_process(
                [sys.executable, "-c", "print('scene child completed')"],
                log_path,
                dict(os.environ),
                lambda: None,
            )
            self.assertIn("scene child completed", log_path.read_text(encoding="utf-8"))

    def test_nonzero_child_exit_reports_status_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "failed-child.log"
            command = [sys.executable, "-c", "import sys; print('failure marker'); sys.exit(23)"]
            with self.assertRaisesRegex(RuntimeError, "code 23"):
                run_scene_process(command, log_path, dict(os.environ), lambda: None)
            self.assertIn("failure marker", log_path.read_text(encoding="utf-8"))

    def test_cancellation_terminates_running_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log_path = root / "cancelled-child.log"
            started_path = root / "started.pid"
            finished_path = root / "finished"
            child_code = (
                "from pathlib import Path; import os, time; "
                f"Path({str(started_path)!r}).write_text(str(os.getpid())); "
                "time.sleep(30); "
                f"Path({str(finished_path)!r}).write_text('finished')"
            )
            deadline = time.monotonic() + 5.0

            def check_cancelled() -> None:
                if started_path.is_file():
                    raise ProcessCancelled("cancel requested")
                if time.monotonic() >= deadline:
                    raise AssertionError("child process did not publish its PID")

            with self.assertRaisesRegex(ProcessCancelled, "cancel requested"):
                run_scene_process(
                    [sys.executable, "-c", child_code],
                    log_path,
                    dict(os.environ),
                    check_cancelled,
                )
            process_id = int(started_path.read_text(encoding="utf-8"))
            try:
                self.assertFalse(process_is_running(process_id))
                self.assertFalse(finished_path.exists())
            finally:
                if process_is_running(process_id):
                    stop_process(process_id)


if __name__ == "__main__":
    unittest.main()
