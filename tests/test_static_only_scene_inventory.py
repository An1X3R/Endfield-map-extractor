"""Prove absent dynamic payloads using real encrypted scene source fixtures."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from endfield_scene_io import scene_io_session
from extractor_core.dynamic_renderer_cache import _identity, _write, read_dynamic_scene_renderers_cached
from extractor_core.dynamic_renderers import (DynamicRendererDecodeError, DynamicSceneRendererInventory,
                                              read_dynamic_scene_renderers_for_scene)
from extractor_core.dynamic_models import read_dynamic_scene_models
from extractor_core.effects import EffectDecodeError, _scene_inventory_sources, read_dynamic_scene_effects_for_scene
from extractor_core.resources import BootstrapExtractionError
from tests.test_dynamic_renderer_cache import _INIT, _MAIN, _check_cancelled, _init, _main, _write_group, _write_vfs


_SCENE = "indie_fixture"
_STATIC = f"Data/Streaming/PC/{_SCENE}/Streaming/InitChunkData_0_0_0_0.bytes"
_DYNAMIC = f"Data/DynamicStreaming/PC/Scene/{_SCENE}/"


def _fixture(root: Path) -> tuple[Path, Path, Path]:
    game, paths, cache = _write_vfs(root, ((_DYNAMIC + "FBStreamArea.bytes", b"opaque source metadata"),))
    _write_group(game, "C3442D43", ((_STATIC, _init()),))
    return game, paths, cache


class StaticOnlySceneInventoryTests(unittest.TestCase):
    def test_completely_absent_dynamic_namespace_still_requires_exact_static_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, paths, cache = _fixture(Path(temporary))
            _write_group(game, "23D53F5D", ((_MAIN, _main()),))
            inventory = read_dynamic_scene_renderers_cached(game, paths, _SCENE, cache, _check_cancelled)
            self.assertEqual((), inventory.records)
            self.assertEqual((), read_dynamic_scene_effects_for_scene(game, paths, _SCENE, _check_cancelled).records)

    def test_empty_renderer_effect_and_model_inventories_have_verified_static_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, paths, cache = _fixture(Path(temporary))
            inventory = read_dynamic_scene_renderers_cached(game, paths, _SCENE, cache, _check_cancelled)
            self.assertEqual((inventory.sources, inventory.references, inventory.groups, inventory.records), ((), (), (), ()))
            self.assertEqual(inventory, read_dynamic_scene_renderers_for_scene(game, paths, _SCENE, _check_cancelled))
            self.assertEqual(inventory, read_dynamic_scene_renderers_cached(game, paths, _SCENE, cache, _check_cancelled))
            self.assertEqual((), read_dynamic_scene_effects_for_scene(game, paths, _SCENE, _check_cancelled).records)
            self.assertEqual((), read_dynamic_scene_models(game, paths, _SCENE, _check_cancelled).records)
            sources = _scene_inventory_sources(game, paths, (_SCENE,))
            self.assertEqual(2, len([path for path in sources if path.parent.name == "C3442D43"]))
            raw = next(cache.rglob("*.bin")).read_bytes()
            self.assertIn(b"DynamicSceneRendererInventory/3", raw)
            self.assertIn(str(game / "C3442D43").replace("\\", "\\\\").encode(), raw)

    def test_no_static_evidence_and_wrong_scene_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, paths, cache = _fixture(Path(temporary))
            _write_group(game, "C3442D43", ((_STATIC.replace(_SCENE, "another_scene"), _init()),))
            with self.assertRaisesRegex(EffectDecodeError, "requires unique InitChunkData"):
                read_dynamic_scene_renderers_cached(game, paths, _SCENE, cache, _check_cancelled)
            for path in (game / "C3442D43").iterdir():
                path.unlink()
            with self.assertRaises(BootstrapExtractionError):
                read_dynamic_scene_effects_for_scene(game, paths, _SCENE, _check_cancelled)

    def test_orphan_init_unknown_payload_and_corrupt_static_are_never_suppressed(self) -> None:
        for filename in ("fb_init_00_7.bytes", "fb_streaming_00_7.bytes", "fb_main_invalid.bytes", "unexpected.bytes"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                game, paths, _ = _fixture(Path(temporary))
                _write_group(game, "23D53F5D", ((_DYNAMIC + filename, b"unselected is not absent"),))
                with self.assertRaises((DynamicRendererDecodeError, EffectDecodeError)):
                    read_dynamic_scene_renderers_for_scene(game, paths, _SCENE, _check_cancelled)
                with self.assertRaises(EffectDecodeError):
                    read_dynamic_scene_effects_for_scene(game, paths, _SCENE, _check_cancelled)
        with tempfile.TemporaryDirectory() as temporary:
            game, paths, cache = _fixture(Path(temporary))
            _write_group(game, "C3442D43", ((_STATIC, b"bad compressed data"),))
            with self.assertRaisesRegex((DynamicRendererDecodeError, EffectDecodeError), "evidence decode failed"):
                read_dynamic_scene_renderers_cached(game, paths, _SCENE, cache, _check_cancelled)
            self.assertEqual([], list(cache.rglob("*.bin")))

    def test_static_changes_and_new_dynamic_payload_invalidate_warm_empty_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, scene_io_session():
            game, paths, cache = _fixture(Path(temporary))
            read_dynamic_scene_renderers_cached(game, paths, _SCENE, cache, _check_cancelled)
            first = next(cache.rglob("*.bin"))
            original = hashlib.sha256(first.read_bytes()).hexdigest()
            _write_group(game, "C3442D43", ((_STATIC, b"bad compressed data"),))
            with self.assertRaisesRegex((DynamicRendererDecodeError, EffectDecodeError), "evidence decode failed"):
                read_dynamic_scene_renderers_cached(game, paths, _SCENE, cache, _check_cancelled)
            self.assertEqual(original, hashlib.sha256(first.read_bytes()).hexdigest())
            _write_group(game, "C3442D43", ((_STATIC, _init()),))
            _write_group(game, "23D53F5D", ((_DYNAMIC + "fb_init_00_7.bytes", _init()),))
            with self.assertRaisesRegex(DynamicRendererDecodeError, "payloads without main"):
                read_dynamic_scene_renderers_cached(game, paths, _SCENE, cache, _check_cancelled)

    def test_empty_persisted_inventory_cannot_hide_real_dynamic_main_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, paths, cache = _write_vfs(Path(temporary), ((_MAIN, _main()), (_INIT, _init())))
            sources = _scene_inventory_sources(game, paths, ("map01",))
            identity = _identity(cache, "map01", sources)
            _write(identity, DynamicSceneRendererInventory("map01", (), (), (), ()),
                   _check_cancelled, _check_cancelled)
            with self.assertRaisesRegex(EffectDecodeError, "payloads without main"):
                read_dynamic_scene_renderers_cached(game, paths, "map01", cache, _check_cancelled)


if __name__ == "__main__":
    unittest.main()
