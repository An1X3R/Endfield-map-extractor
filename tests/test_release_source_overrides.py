from pathlib import Path
import tempfile
import unittest

from src.release_source_overrides import (
    SourceOverrideError,
    apply_animestudio_source_overrides,
    verify_animestudio_source_overrides,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OVERRIDE_ROOT = REPOSITORY_ROOT / "third_party" / "source-overrides" / "animestudio"


def _write_pinned_source(root: Path, embedded_key_call: str) -> None:
    anime_studio = root / "AnimeStudio"
    (anime_studio / "Crypto").mkdir(parents=True)
    (anime_studio / "UnityCNManager.cs").write_text(
        'internal static class OriginalManager { const string Key = "' + "a" * 32 + '"; }\n',
        encoding="utf-8",
    )
    (anime_studio / "Crypto" / "UnityCN.cs").write_text(
        "internal static class OriginalCrypto { }\n",
        encoding="utf-8",
    )
    (anime_studio / "BundleFile.cs").write_text(
        "internal class BundleFile {\n    " + embedded_key_call + "\n}\n",
        encoding="utf-8",
    )
    (anime_studio / "AnimeStudio.csproj").write_text(
        "<Project>\n  <ItemGroup>\n    <Content Include=\"CNKeys.json\">\n"
        "      <CopyToOutputDirectory>PreserveNewest</CopyToOutputDirectory>\n"
        "    </Content>\n  </ItemGroup>\n"
        "  <ItemGroup>\n    <Content Include=\"..\\AnimeStudio.Libraries\\AnimeStudio.Ooz.dll\">\n"
        "      <CopyToOutputDirectory>PreserveNewest</CopyToOutputDirectory>\n"
        "    </Content>\n  </ItemGroup>\n</Project>\n",
        encoding="utf-8",
    )
    (anime_studio / "CNKeys.json").write_text("null\n", encoding="utf-8")


class AnimeStudioSourceOverrideTests(unittest.TestCase):
    def test_overrides_remove_embedded_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_root = Path(temporary_directory) / "AnimeStudio"
            source_root.mkdir()
            _write_pinned_source(source_root, 'UnityCN.SetKey("' + "b" * 32 + '");')

            report = apply_animestudio_source_overrides(source_root, OVERRIDE_ROOT)
            verify_animestudio_source_overrides(source_root, OVERRIDE_ROOT, report)

            self.assertEqual(len(report.files), 4)
            self.assertEqual(report.removed_paths, ("AnimeStudio/CNKeys.json",))
            self.assertFalse((source_root / "AnimeStudio" / "CNKeys.json").exists())
            self.assertIn(
                "GetDefaultKeys() => new();",
                (source_root / "AnimeStudio" / "UnityCNManager.cs").read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                'UnityCN.SetKey("',
                (source_root / "AnimeStudio" / "BundleFile.cs").read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "CNKeys.json",
                (source_root / "AnimeStudio" / "AnimeStudio.csproj").read_text(encoding="utf-8"),
            )
            project = (source_root / "AnimeStudio" / "AnimeStudio.csproj").read_text(encoding="utf-8")
            self.assertIn('Include="$(AnimeStudioOozPath)" Link="AnimeStudio.Ooz.dll"', project)
            self.assertIn("ANIMESTUDIO_OOZ_PATH_MISSING", project)


    def test_overrides_fail_on_unrecognized_key_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_root = Path(temporary_directory) / "AnimeStudio"
            source_root.mkdir()
            _write_pinned_source(source_root, "ReadUnityCN(reader);")

            with self.assertRaisesRegex(
                SourceOverrideError,
                "exactly one recognized embedded UnityCN key call",
            ):
                apply_animestudio_source_overrides(source_root, OVERRIDE_ROOT)


if __name__ == "__main__":
    unittest.main()
