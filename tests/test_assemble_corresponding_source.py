from __future__ import annotations

import unittest
from pathlib import PurePosixPath

from src.assemble_corresponding_source import (
    is_source_package_path,
)


class CorrespondingSourcePathTests(unittest.TestCase):
    def test_includes_build_source_and_license_material(self) -> None:
        for value in (
            "src/endfield_dependency_bootstrap.py",
            "dotnet/CharacterTools/CharacterModel/CharacterModel.csproj",
            "patches/ruri-character-humanoid.patch",
            "third_party/licenses/Ruri-AGPL-3.0.txt",
            "webui/src/App.tsx",
            "webui/LICENSE",
            "CHANGELOG.md",
            "docs/INSTALL.md",
            "dotnet/CharacterTools/ThirdParty/Ruri/LICENSE",
        ):
            with self.subTest(path=value):
                self.assertTrue(is_source_package_path(PurePosixPath(value)))

    def test_excludes_generated_game_and_sdk_payloads(self) -> None:
        for value in (
            "webui/dist/assets/app.js",
            "webui/.playwright-cli/traces/page.jpeg",
            "dotnet/CharacterTools/bin/Release/tool.dll",
            "third_party/sources/scene/AnimeStudio/CNKeys.json",
            "third_party/character-nuget/AnimeStudio.FBXWrapper/packages.lock.json",
            ".tools/character/runtime.json",
            "third_party/source-overrides/animestudio/default.shader",
        ):
            with self.subTest(path=value):
                self.assertFalse(is_source_package_path(PurePosixPath(value)))

if __name__ == "__main__":
    unittest.main()
