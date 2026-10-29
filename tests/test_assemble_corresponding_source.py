from __future__ import annotations

import unittest
from pathlib import PurePosixPath

from src.assemble_corresponding_source import (
    is_source_package_path,
)


class CorrespondingSourcePathTests(unittest.TestCase):
    def test_includes_build_source_and_license_material(self) -> None:
        for value in (
            ".gitattributes",
            "src/endfield_dependency_bootstrap.py",
            "src/extractor_core/lz4_numba.py",
            "dotnet/CharacterTools/CharacterModel/CharacterModel.csproj",
            "patches/ruri-character-humanoid.patch",
            "third_party/licenses/Ruri-AGPL-3.0.txt",
            "webui/src/App.tsx",
            "webui/LICENSE",
            "CHANGELOG.md",
            "docs/INSTALL.md",
            "NuGet.config",
            "endfield.config.example.json",
            "endfield.runtime.example.json",
            "launch_first_run.ps1",
            "dotnet/CharacterTools/ThirdParty/Ruri/LICENSE",
        ):
            with self.subTest(path=value):
                self.assertTrue(is_source_package_path(PurePosixPath(value)))

    def test_excludes_generated_game_and_sdk_payloads(self) -> None:
        for value in (
            "webui/dist/assets/app.js",
            "src/extractor_core/__pycache__/lz4_numba.pyc",
            "webui/.playwright-cli/traces/page.jpeg",
            "dotnet/CharacterTools/bin/Release/tool.dll",
            "third_party/sources/scene/AnimeStudio/CNKeys.json",
            "third_party/character-nuget/AnimeStudio.FBXWrapper/packages.lock.json",
            ".tools/character/runtime.json",
            "endfield.runtime.local.json",
            "endfield.character.local.json",
            "reconstruction.local.json",
            "third_party/source-overrides/animestudio/default.shader",
        ):
            with self.subTest(path=value):
                self.assertFalse(is_source_package_path(PurePosixPath(value)))

if __name__ == "__main__":
    unittest.main()
