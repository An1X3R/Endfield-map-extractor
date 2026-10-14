"""Exercise portable character configuration discovery against real local files."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launch_webui import resolve_character_config
from endfield_runtime_config import ConfigurationError


class CharacterRuntimeDiscoveryTests(unittest.TestCase):
    def test_cli_environment_and_local_file_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "endfield.character.local.json"
            explicit = root / "explicit.json"
            environment = root / "environment.json"
            for path in (local, explicit, environment):
                path.write_text('{}', encoding="utf-8")
            settings = {"ENDFIELD_CHARACTER_CONFIG": str(environment)}
            self.assertEqual(resolve_character_config(explicit, settings, root), explicit.resolve())
            self.assertEqual(resolve_character_config(None, settings, root), environment.resolve())
            self.assertEqual(resolve_character_config(None, {}, root), local.resolve())

    def test_unconfigured_scene_only_startup_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(resolve_character_config(None, {}, Path(directory)))

    def test_installed_integrated_runtime_precedes_legacy_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / '.tools' / 'character' / 'runtime.json'
            installed.parent.mkdir(parents=True)
            installed.write_text('{}', encoding='utf-8')
            legacy = root / 'endfield.character.local.json'
            legacy.write_text('{}', encoding='utf-8')
            self.assertEqual(resolve_character_config(None, {}, root), installed.resolve())
            self.assertEqual(resolve_character_config(legacy, {}, root), legacy.resolve())

    def test_missing_explicit_configuration_is_not_replaced_by_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "endfield.character.local.json").write_text('{}', encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                resolve_character_config(root / "missing.json", {}, root)
            with self.assertRaises(ConfigurationError):
                resolve_character_config(None, {"ENDFIELD_CHARACTER_CONFIG": str(root)}, root)


if __name__ == "__main__":
    unittest.main()
