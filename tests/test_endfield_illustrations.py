"""Check illustration identity and lossless delivery on real local files."""
from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest

from PIL import Image

from endfield_character_catalog import CharacterResourceProvider
from endfield_illustrations import (
    IllustrationCatalog, IllustrationEntry, IllustrationExportError,
    _artifact, _resource_paths, _selected_texture, select_illustrations,
)


class IllustrationTests(unittest.TestCase):
    def test_resource_discovery_accepts_new_ids_without_a_fixed_roster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "resources.sqlite"
            prefix = "assets/beyond/dynamicassets/gameplay/ui/sprites/characterportrait/"
            rows = (prefix + "chr_9999_future.png", prefix + "chr_9999_future.png",
                    prefix + "chr_9998_future.png", prefix + "metadata.asset",
                    prefix.replace("characterportrait", "chargacha") + "chr_9999_future.png")
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE meta (key TEXT, value TEXT)")
                connection.execute("INSERT INTO meta VALUES ('format', 'EndfieldResourceDB/1')")
                connection.execute("CREATE TABLE paths (path TEXT, path_folded TEXT)")
                connection.executemany("INSERT INTO paths VALUES (?, ?)", ((path, path.casefold()) for path in rows))
                connection.commit()
            self.assertEqual(_resource_paths(database), (prefix + "chr_9998_future.png", prefix + "chr_9999_future.png"))

    def test_selection_never_falls_back_to_all_or_guesses_a_provider(self) -> None:
        provider = CharacterResourceProvider("main/one.ab", ())
        entry = IllustrationEntry("id", "chr_9999_future", "Future", "art.png", (provider,))
        ambiguous = IllustrationEntry("ambiguous", None, None, "other.png", (provider, provider))
        catalog = IllustrationCatalog("EndfieldIllustrationCatalog/1", "catalog", (entry, ambiguous), (), "game")
        self.assertEqual(select_illustrations(catalog, ("id",)), (entry,))
        for identifiers in ((), ("id", "id"), ("missing",), ("ambiguous",)):
            with self.assertRaises(IllustrationExportError):
                select_illustrations(catalog, identifiers)

    def test_sprite_only_and_ambiguous_texture_bindings_are_explicit_errors(self) -> None:
        entry = IllustrationEntry("id", None, None, "art.png", ())
        sprite = {"Container": "art.png", "Asset": {"Source": "CAB-a", "PathId": 1, "Type": "Sprite"}}
        texture = {"Container": "art.png", "Asset": {"Source": "CAB-a", "PathId": 2, "Type": "Texture2D"}}
        self.assertEqual(_selected_texture({"Containers": [sprite, texture]}, entry), ("cab-a", 2))
        with self.assertRaises(IllustrationExportError):
            _selected_texture({"Containers": [sprite]}, entry)
        with self.assertRaises(IllustrationExportError):
            _selected_texture({"Containers": [texture, {**texture, "Asset": {
                "Source": "CAB-b", "PathId": 2, "Type": "Texture2D"}}]}, entry)

    def test_delivery_preserves_png_bytes_and_partial_alpha_without_name_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe, output = root / "probe", root / "output"
            probe.mkdir()
            output.mkdir()
            image = Image.new("RGBA", (3, 2), (37, 73, 109, 0))
            image.putpixel((1, 1), (11, 22, 33, 128))
            image.putpixel((2, 1), (44, 55, 66, 255))
            image.save(probe / "original.png")
            entry = IllustrationEntry("a" * 64, "chr_a", "Same/Name", "original.png", ())
            result = _artifact(probe, output, entry, ("cab-a", 9), {"File": "original.png"})
            original = (probe / "original.png").read_bytes()
            self.assertEqual((output / result.relative_path).read_bytes(), original)
            self.assertEqual(result.sha256, sha256(original).hexdigest())
            self.assertEqual(result.alpha_extrema, (0, 255))
            other = IllustrationEntry("b" * 64, "chr_b", "Same/Name", "original.png", ())
            second = _artifact(probe, output, other, ("cab-a", 9), {"File": "original.png"})
            self.assertNotEqual(result.relative_path, second.relative_path)
            with self.assertRaises(IllustrationExportError):
                _artifact(probe, output, entry, ("cab-a", 9), {"File": "../original.png"})


if __name__ == "__main__":
    unittest.main()
