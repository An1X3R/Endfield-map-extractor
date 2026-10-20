"""Validate offline release installs against an explicitly supplied real tool bundle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import REPO_ROOT
from endfield_character_runtime import read_character_tools
from endfield_character_service import CharacterService
from endfield_tool_bundle import export_tool_bundle, read_tool_bundle
from install_bundled_tools import install_bundled_tools


class ToolBundleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configured = os.environ.get("ENDFIELD_TOOL_BUNDLE_TEST_ROOT")
        if not configured:
            raise unittest.SkipTest("Set ENDFIELD_TOOL_BUNDLE_TEST_ROOT to a verified real release .tools directory.")
        cls.source = Path(configured).resolve()
        cls.tools = read_tool_bundle(cls.source)
        executable = shutil.which("dotnet")
        if executable is None:
            raise unittest.SkipTest("The bundled readers require an installed .NET 9 runtime.")
        cls.dotnet = Path(executable).resolve()
        cls.python = Path(sys.executable).resolve()

    def publish(self, destination: Path) -> Path:
        return export_tool_bundle(self.tools.scene, self.tools.character_receipt, destination)

    def prepare_release_source(self, destination: Path) -> None:
        suffixes = {".py", ".cs", ".csproj", ".props", ".targets", ".json", ".patch", ".ps1", ".txt", ".md", ".config"}
        for name in ("src", "dotnet", "patches", "third_party"):
            for source in (REPO_ROOT / name).rglob("*"):
                relative = source.relative_to(REPO_ROOT)
                if source.is_file() and source.suffix in suffixes and not {"bin", "obj", "__pycache__"}.intersection(relative.parts):
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
        shutil.copyfile(REPO_ROOT / "global.json", destination / "global.json")

    def configure_release(self, bundle: Path) -> Path:
        process = subprocess.run([
            str(self.python), "-B", str(bundle.parent / "src" / "install_bundled_tools.py"),
            "--bundle-root", str(bundle), "--dotnet", str(self.dotnet),
        ], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(0, process.returncode, process.stdout + process.stderr)
        return Path(json.loads(process.stdout)["runtime"])

    def test_export_excludes_machine_state_and_moves_with_relative_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "published" / ".tools"
            self.publish(destination)
            self.assertFalse((destination / "character" / "runtime.json").exists())
            self.assertFalse((destination / "character" / "fur_curves_settings.json").exists())
            self.assertFalse((destination / "character" / "state").exists())
            tools = read_tool_bundle(destination)
            files = read_character_tools(tools.character_receipt)
            self.assertTrue(all(path.is_relative_to(destination) for path in files.values()))
            index = json.loads((destination / "tool_bundle.json").read_text(encoding="utf-8"))
            for row in index["receipts"].values():
                self.assertFalse(Path(row["path"]).is_absolute())
                serialized = (destination / row["path"]).read_text(encoding="utf-8")
                self.assertNotIn(str(REPO_ROOT), serialized)
                self.assertNotIn(str(self.source), serialized)
            moved = root / "relocated" / "nested" / ".tools"
            moved.parent.mkdir(parents=True)
            destination.rename(moved)
            relocated = read_tool_bundle(moved)
            self.assertTrue(relocated.character_receipt.is_relative_to(moved))
            self.assertTrue(relocated.scene.is_relative_to(moved))

    def test_configure_without_git_and_repeat_preserves_user_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / ".tools"
            self.publish(destination)
            environment = {**os.environ, "PATH": str(self.dotnet.parent), "PYTHONDONTWRITEBYTECODE": "1"}
            self.assertIsNone(shutil.which("git", path=environment["PATH"]))
            process = subprocess.run([
                str(self.python), "-B", str(REPO_ROOT / "src" / "install_bundled_tools.py"),
                "--bundle-root", str(destination), "--dotnet", str(self.dotnet),
            ], env=environment, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(0, process.returncode, process.stdout + process.stderr)
            result = json.loads(process.stdout)
            self.assertFalse(result["source_downloaded"])
            self.assertFalse(result["tools_compiled"])
            runtime = Path(result["runtime"])
            before = runtime.read_bytes()
            state = runtime.parent / "state" / "user-selection.json"
            state.parent.mkdir()
            state.write_text('{"selection_id":"playable-chr_0004_pelica"}\n', encoding="utf-8")
            state_before = state.read_bytes()
            self.assertEqual(runtime, install_bundled_tools(destination, self.dotnet, self.python))
            self.assertEqual(before, runtime.read_bytes())
            self.assertEqual(state_before, state.read_bytes())
            self.assertFalse((destination / "scene" / "helper_dependencies").exists())

    def test_reconfigure_after_move_binds_existing_python_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "published" / ".tools"
            self.publish(destination)
            self.prepare_release_source(destination.parent)
            runtime = self.configure_release(destination)
            state = runtime.parent / "state" / "user-selection.json"
            state.parent.mkdir()
            state.write_text('{"selection_id":"playable-chr_0004_pelica"}\n', encoding="utf-8")
            state_before = state.read_bytes()
            moved = root / "relocated" / "deeper" / ".tools"
            moved.parent.parent.mkdir(parents=True)
            destination.parent.rename(moved.parent)
            repaired = self.configure_release(moved)
            _, defaults = CharacterService(repaired)._runtime_defaults({}, None)
            self.assertEqual(self.python, Path(str(defaults["python"])))
            self.assertEqual(moved.parent / "src", Path(str(defaults["repository_src"])))
            runtime_config = json.loads(repaired.read_text(encoding="utf-8"))
            for field in ("potential_json_reader", "potential_scene_probe"):
                value = Path(runtime_config["export_defaults"][field])
                resolved = (repaired.parent / value).resolve() if not value.is_absolute() else value.resolve()
                self.assertTrue(resolved.is_file(), f"{field}={resolved}")
            self.assertEqual(state_before, (repaired.parent / "state" / state.name).read_bytes())

    def test_upgrade_rebinds_replaced_readers_and_preserves_artist_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / ".tools"
            self.publish(destination)
            runtime = install_bundled_tools(destination, self.dotnet, self.python)
            document = json.loads(runtime.read_text(encoding="utf-8"))
            document["authoring_tools"]["controller_json_reader"] = "removed-reader/AnimeStudio.CLI.exe"
            document["npc_assembly_tools"]["json_reader"] = "removed-reader/AnimeStudio.CLI.exe"
            document["export_defaults"]["cycles_samples"] = 37
            runtime.write_text(json.dumps(document), encoding="utf-8")
            settings_path = (runtime.parent / document["export_defaults"]["fur_curves_settings"]).resolve()
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["tools"]["snow_channel_probe"] = {"path": "removed-reader/Snow.exe", "sha256": "0" * 64}
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
            install_bundled_tools(destination, self.dotnet, self.python)
            updated = json.loads(runtime.read_text(encoding="utf-8"))
            installed = read_character_tools(read_tool_bundle(destination).character_receipt)
            self.assertEqual(installed["json_reader"], (runtime.parent / updated["authoring_tools"]["controller_json_reader"]).resolve())
            self.assertEqual(37, updated["export_defaults"]["cycles_samples"])
            potential_reader = (runtime.parent / updated["export_defaults"]["potential_json_reader"]).resolve()
            scene_probe = (runtime.parent / updated["export_defaults"]["potential_scene_probe"]).resolve()
            self.assertEqual(installed["json_reader"], potential_reader)
            self.assertTrue(scene_probe.is_file())
            snow = json.loads(settings_path.read_text(encoding="utf-8"))["tools"]["snow_channel_probe"]
            self.assertEqual(installed["snow_channel_reader"], (runtime.parent / snow["path"]).resolve())
            self.assertNotEqual("0" * 64, snow["sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
