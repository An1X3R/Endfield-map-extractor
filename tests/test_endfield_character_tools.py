"""Check releasable source pins and strict explicit character-tool build inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from endfield_character_tools import CharacterToolsError, ROOT, TOOL_PROJECTS, _relative, _verify_file, _verify_snapshot, character_tool_source_identity
from setup_character_tools import setup_character_tools


class CharacterToolTests(unittest.TestCase):
    def test_all_reader_projects_use_source_references_and_locked_packages(self) -> None:
        lock = json.loads((ROOT / "third_party" / "character-dotnet.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(set(lock["projects"]), set(TOOL_PROJECTS.values()))
        for name in lock["projects"]:
            directory = ROOT / "dotnet" / "CharacterTools" / name
            project = ET.parse(directory / f"{name}.csproj")
            self.assertFalse(project.findall(".//HintPath"), name)
            self.assertTrue(project.findall(".//ProjectReference"), name)
            packages = json.loads((directory / "packages.lock.json").read_text(encoding="utf-8"))
            self.assertEqual(packages["version"], 1)
            self.assertIn("net9.0-windows7.0", packages["dependencies"])

    def test_dependency_patches_are_exactly_pinned_and_third_party_license_is_present(self) -> None:
        lock = json.loads((ROOT / "third_party" / "character-dotnet.lock.json").read_text(encoding="utf-8"))
        for key in ("animeStudio", "ruri"):
            value = lock[key]
            _verify_file(ROOT / value["patch"], value["patchSha256"], key)
        license_text = (ROOT / "dotnet" / "CharacterTools" / "ThirdParty" / "Ruri" / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", license_text)
        self.assertEqual(lock["ruri"]["license"], "AGPL-3.0")
        self.assertEqual(len(character_tool_source_identity()), 64)

    def test_source_change_and_path_escape_fail_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source.cs"
            source.write_bytes(b"original source")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            _verify_file(source, digest, "source")
            source.write_bytes(b"changed source")
            with self.assertRaisesRegex(CharacterToolsError, "SOURCE_HASH_MISMATCH"):
                _verify_file(source, digest, "source")
            with self.assertRaisesRegex(CharacterToolsError, "PATH_OUTSIDE_ROOT"):
                _relative(root, "../escape.cs", "source")
            self.assertEqual(_relative(root, "source.cs", "source"), source)

    def test_setup_rejects_existing_or_overlapping_output_before_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(CharacterToolsError, "SETUP_OUTPUT_INVALID"):
                setup_character_tools(root, root / "source", root / "dotnet.exe")
            with self.assertRaisesRegex(CharacterToolsError, "SETUP_OUTPUT_INVALID"):
                setup_character_tools(root / "source" / "output", root / "source", root / "dotnet.exe")
            self.assertFalse((root / "source").exists())

    def test_source_archive_inventory_detects_changed_or_added_build_inputs(self) -> None:
        dependency = json.loads((ROOT / "third_party" / "character-dotnet.lock.json").read_text(encoding="utf-8"))["ruri"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "reader.cs"
            source.write_bytes(b"source archive integrity fixture")
            manifest = {"format": "EndfieldPinnedSourceSnapshot/1", "repository": dependency["repository"],
                        "commit": dependency["commit"], "license": dependency["license"],
                        "files": {"reader.cs": hashlib.sha256(source.read_bytes()).hexdigest()},
                        "appliedPatches": [{"path": dependency["patch"], "sha256": dependency["patchSha256"]}]}
            (root / "SOURCE-PROVENANCE.json").write_text(json.dumps(manifest), encoding="utf-8")
            _verify_snapshot(root, dependency, "ruri")
            source.write_bytes(b"changed archive input")
            with self.assertRaisesRegex(CharacterToolsError, "SOURCE_HASH_MISMATCH"):
                _verify_snapshot(root, dependency, "ruri")
            (root / "extra.props").write_bytes(b"unexpected build input")
            with self.assertRaisesRegex(CharacterToolsError, "INVENTORY_MISMATCH"):
                _verify_snapshot(root, dependency, "ruri")


if __name__ == "__main__":
    unittest.main()
