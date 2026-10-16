"""Check releasable source pins and strict explicit character-tool build inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from endfield_character_tools import CharacterToolsError, ROOT, TOOL_PROJECTS, _relative, _verify_file, _verify_snapshot, character_tool_source_identity
from setup_character_tools import setup_character_tools
from assemble_corresponding_source import is_source_package_path


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

    def test_public_source_inventory_keeps_character_identity_after_relocation(self) -> None:
        expected = character_tool_source_identity()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            candidates = {ROOT / "global.json"}
            for directory in (ROOT / "src", ROOT / "dotnet", ROOT / "patches", ROOT / "third_party"):
                candidates.update(path for path in directory.rglob("*") if path.is_file()
                                  and is_source_package_path(PurePosixPath(path.relative_to(ROOT).as_posix())))
            for source in candidates:
                destination = root / source.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            command = [sys.executable, "-B", "-c",
                       "import sys; sys.path.insert(0, 'src'); "
                       "from endfield_character_tools import character_tool_source_identity; "
                       "print(character_tool_source_identity())"]
            clean = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True, timeout=30)
            self.assertEqual(clean.stdout.strip(), expected)
            unused = "third_party/character-nuget/AnimeStudio.FBXWrapper/packages.lock.json"
            destination = root / unused
            destination.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "third_party/character-nuget/AnimeStudio/packages.lock.json", destination)
            with_unused_lock = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True, timeout=30)
            self.assertEqual(with_unused_lock.stdout.strip(), expected)
            project = root / "dotnet/CharacterTools/CharacterModel/CharacterModel.csproj"
            project.write_bytes(project.read_bytes() + b"\n")
            changed = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True, timeout=30)
            self.assertNotEqual(changed.stdout.strip(), expected)

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
