from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.release_source_collection import (
    SourceCollectionError,
    collect_source_snapshot,
    verify_source_snapshot,
)


class ReleaseSourceCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source_root = self.root / "upstream"
        self.repository_root = self.root / "repository"
        self.snapshot_root = self.root / "snapshot"
        (self.source_root / "src").mkdir(parents=True)
        (self.repository_root / "patches").mkdir(parents=True)
        (self.source_root / "src" / "Reader.cs").write_text("namespace Reader;\n", encoding="utf-8")
        (self.repository_root / "patches" / "reader.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def collect(self) -> Path:
        return collect_source_snapshot(
            source_root=self.source_root,
            destination_root=self.snapshot_root,
            repository_root=self.repository_root,
            repository="https://example.invalid/reader.git",
            commit="0123456789abcdef",
            license_name="MIT",
            included_paths=("src",),
            applied_patch_paths=("patches/reader.patch",),
            removed_paths=("Reader/CNKeys.json",),
        )

    def test_collects_and_verifies_exact_pinned_inventory(self) -> None:
        manifest_path = self.collect()

        self.assertTrue(manifest_path.is_file())
        self.assertEqual((self.snapshot_root / "src" / "Reader.cs").read_text(encoding="utf-8"), "namespace Reader;\n")
        verify_source_snapshot(
            self.snapshot_root,
            self.repository_root,
            "https://example.invalid/reader.git",
            "0123456789abcdef",
            "MIT",
        )
        generated = self.snapshot_root / "obj" / "project.assets.json"
        generated.parent.mkdir(parents=True)
        generated.write_text("{}", encoding="utf-8")
        verify_source_snapshot(
            self.snapshot_root,
            self.repository_root,
            "https://example.invalid/reader.git",
            "0123456789abcdef",
            "MIT",
        )

        (self.snapshot_root / "unexpected.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(SourceCollectionError, "inventory differs"):
            verify_source_snapshot(
                self.snapshot_root,
                self.repository_root,
                "https://example.invalid/reader.git",
                "0123456789abcdef",
                "MIT",
            )

    def test_rejects_private_key_and_compiled_inputs(self) -> None:
        (self.source_root / "src" / "CNKeys.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(SourceCollectionError, "Private game or key material"):
            self.collect()

        (self.source_root / "src" / "CNKeys.json").unlink()
        (self.source_root / "src" / "runtime.dll").write_bytes(b"binary")
        with self.assertRaisesRegex(SourceCollectionError, "Compiled, game, or shader payload"):
            self.collect()


if __name__ == "__main__":
    unittest.main()
