from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import endfield_blender_selection_worker_v1 as blender_worker
from endfield_runtime_config import (
    ConfigurationError,
    configured_optional_path,
    configured_path,
    load_runtime_config,
)
import launch_webui
from webui_export_worker import ExportCoordinator, resolve_runtime_paths as resolve_webui_worker_paths


class RuntimeConfigTests(unittest.TestCase):
    def write_runtime_config(self, root: Path, paths: dict[str, str]) -> Path:
        config = root / "endfield.runtime.local.json"
        config.write_text(
            json.dumps({"format": "EndfieldRuntimeConfig/1", "paths": paths}),
            encoding="utf-8",
        )
        return config

    def test_cli_environment_config_and_default_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("default", "configured", "environment", "cli"):
                (root / name).mkdir()
            config_path = self.write_runtime_config(root, {"stage119_root": "configured"})
            runtime = load_runtime_config(config_path)
            with mock.patch.dict(os.environ, {"ENDFIELD_STAGE119_ROOT": str(root / "environment")}, clear=False):
                self.assertEqual(
                    configured_path(
                        root / "cli",
                        env_name="ENDFIELD_STAGE119_ROOT",
                        label="Stage119 cache root",
                        config_value=runtime.value("stage119_root"),
                        config_dir=runtime.directory,
                        default=root / "default",
                        kind="dir",
                        cli_option="--stage119-root",
                        config_key="stage119_root",
                    ),
                    (root / "cli").resolve(),
                )
                self.assertEqual(
                    configured_path(
                        None,
                        env_name="ENDFIELD_STAGE119_ROOT",
                        label="Stage119 cache root",
                        config_value=runtime.value("stage119_root"),
                        config_dir=runtime.directory,
                        default=root / "default",
                        kind="dir",
                        cli_option="--stage119-root",
                        config_key="stage119_root",
                    ),
                    (root / "environment").resolve(),
                )
            self.assertEqual(
                configured_path(
                    None,
                    env_name="ENDFIELD_STAGE119_ROOT",
                    label="Stage119 cache root",
                    config_value=runtime.value("stage119_root"),
                    config_dir=runtime.directory,
                    default=root / "default",
                    kind="dir",
                    cli_option="--stage119-root",
                    config_key="stage119_root",
                ),
                (root / "configured").resolve(),
            )

    def test_portable_local_config_is_optional_and_relative_to_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "cache").mkdir()
            self.write_runtime_config(root, {"stage119_root": "cache"})
            runtime = load_runtime_config(root=root)
            self.assertEqual(runtime.path, root / "endfield.runtime.local.json")
            self.assertEqual(
                configured_path(
                    None,
                    env_name="ENDFIELD_STAGE119_ROOT",
                    label="Stage119 cache root",
                    config_value=runtime.value("stage119_root"),
                    config_dir=runtime.directory,
                    kind="dir",
                    cli_option="--stage119-root",
                    config_key="stage119_root",
                ),
                (root / "cache").resolve(),
            )

    def test_missing_path_error_names_every_supported_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            with self.assertRaisesRegex(
                ConfigurationError,
                r"--stage119-root.*ENDFIELD_STAGE119_ROOT.*paths\.stage119_root",
            ):
                configured_path(
                    missing,
                    env_name="ENDFIELD_STAGE119_ROOT",
                    label="Stage119 cache root",
                    kind="dir",
                    cli_option="--stage119-root",
                    config_key="stage119_root",
                )

    def test_worker_blender_default_remains_profile_controlled(self) -> None:
        self.assertIsNone(
            configured_optional_path(
                None,
                env_name="ENDFIELD_BLENDER",
                label="Blender executable",
                cli_option="--blender-exe",
                config_key="blender_exe",
            )
        )

    def test_launcher_and_workers_share_local_config_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "Endfield Game"
            export = root / "exports"
            stage = root / "stage119"
            output = export / "webui"
            game.mkdir()
            export.mkdir()
            stage.mkdir()
            output.mkdir()
            blender = root / "blender.exe"
            profiles = root / "profiles.json"
            blender.write_bytes(b"not-a-real-executable")
            profiles.write_text("{}", encoding="ascii")
            config_path = self.write_runtime_config(
                root,
                {
                    "game_root": "Endfield Game",
                    "export_root": "exports",
                    "stage119_root": "stage119",
                    "webui_output_root": "exports/webui",
                    "blender_exe": "blender.exe",
                    "blender_profile_registry": "profiles.json",
                },
            )
            launcher_args = argparse.Namespace(
                runtime_config=config_path,
                game_root=None,
                export_root=None,
                stage119_root=None,
                webui_output_root=None,
                blender_exe=None,
            )
            launcher_paths, _ = launch_webui.resolve_runtime_paths(launcher_args)
            self.assertEqual(launcher_paths["stage119_root"], stage.resolve())
            self.assertEqual(launcher_paths["webui_output_root"], output.resolve())

            coordinator_paths, _ = resolve_webui_worker_paths(runtime_config=config_path)
            self.assertEqual(coordinator_paths["game_root"], game.resolve())
            self.assertEqual(coordinator_paths["profile_registry"], profiles.resolve())

            worker_args = argparse.Namespace(
                runtime_config=config_path,
                game_root=None,
                export_root=None,
                stage119_root=None,
                blender_exe=None,
                profile_registry=None,
            )
            worker_paths, _ = blender_worker.resolve_runtime_paths(worker_args)
            self.assertEqual(worker_paths["export_root"], export.resolve())
            self.assertEqual(worker_paths["blender_exe"], blender.resolve())

    def test_missing_dependencies_have_actionable_boundary_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing_stage = root / "missing-stage119"
            coordinator = ExportCoordinator(stage119_root=missing_stage)
            with self.assertRaisesRegex(RuntimeError, r"Stage119 cache directory is missing.*--stage119-root"):
                coordinator._load_dataset()
            with self.assertRaisesRegex(blender_worker.WorkerError, r"Endfield game directory is missing.*--game-root"):
                blender_worker.ensure_game_read_only_boundary(root / "missing-game")
            with self.assertRaisesRegex(blender_worker.WorkerError, r"Stage119 cache directory is missing.*--stage119-root"):
                blender_worker.validate_stage119({"root": str(missing_stage)})
            output = root / "output"
            output.mkdir()
            validation = coordinator.validate(
                {
                    "format": "EndfieldWebUIExportJob/2",
                    "map_id": "map01",
                    "selection": {"type": "world_bounds", "xmin": 0, "xmax": 128, "zmin": 0, "zmax": 128},
                    "layers": {"terrain": True, "instances": False, "water": False, "effects": False},
                    "water_mode": "stable_eevee",
                    "effects": {"selection_mode": "full_system_by_anchor"},
                    "output": {"root": str(output), "label": "package-only"},
                }
            )
            self.assertEqual(validation["exportMode"], "data_package")
            self.assertEqual(validation["blenderBuild"]["status"], "not_ready")


if __name__ == "__main__":
    unittest.main()
