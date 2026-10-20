"""Exercise real filesystem reuse of prefab metadata and shared bundle inputs."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from endfield_scene_prefab_bounds_cache import available_bounds_prefabs, link_bounds_bundles, verified_bounds_manifest
from endfield_scene_resources import describe_file


class PrefabBoundsCacheTests(unittest.TestCase):
    def test_shared_batch_hardlinks_survive_another_batch_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'shared.ab'
            source.write_bytes(b'exact source bundle payload')
            available = {'main/shared.ab': source}
            for name in ('first', 'second'):
                link_bounds_bundles(available, ['main/shared.ab'], root / name)
                linked = root / name / 'main/shared.ab'
                self.assertTrue(linked.samefile(source))
                self.assertEqual(linked.read_bytes(), source.read_bytes())
                linked.unlink()
            self.assertEqual(source.read_bytes(), b'exact source bundle payload')
            with self.assertRaisesRegex(ValueError, 'unsafe logical name'):
                link_bounds_bundles(available, ['../outside.ab'], root / 'unsafe')

    def test_legacy_receipt_uses_separate_stat_proof_and_rejects_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'scene_manifest.json'
            manifest.write_text('{"Format":"EndfieldSceneProbe/1"}', encoding='utf-8')
            receipt = root / 'receipt.json'
            original = json.dumps({'format': 'EndfieldPrefabBoundsBatch/1', 'roots': ['main/source.ab'],
                'probeSha256': 'probe', 'gameSourceSha256': 'game', 'manifest': describe_file(manifest)})
            receipt.write_text(original, encoding='utf-8')
            self.assertEqual(verified_bounds_manifest(receipt, root, 'probe', 'game'), manifest)
            self.assertEqual(verified_bounds_manifest(receipt, root, 'probe', 'game'), manifest)
            self.assertTrue(receipt.with_name('receipt_stat_v1.json').is_file())
            self.assertEqual(receipt.read_text(encoding='utf-8'), original)
            manifest.write_text('{"Format":"changed source"}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'cache integrity differs'):
                verified_bounds_manifest(receipt, root, 'probe', 'game')

    def test_coverage_requires_exact_source_root_and_mesh_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scene_manifest.json'
            asset = {'Source': 'CAB-source', 'PathId': 1, 'Type': 'GameObject'}
            mesh = {'Source': 'CAB-mesh', 'PathId': 2, 'Type': 'Mesh'}
            data = {'Format': 'EndfieldSceneProbe/1', 'Containers': [{'Container': 'assets/one.prefab', 'Asset': asset}],
                'Nodes': [{**asset, 'TransformPathId': 10,
                           'Parent': {'Source': 'CAB-source', 'PathId': 0, 'Type': 'Transform'}, 'Mesh': mesh}],
                'LodGroups': [], 'MeshBounds': [{'Asset': mesh, 'Minimum': [-4, 0, -4], 'Maximum': [4, 9, 4]}]}
            requested = frozenset({'assets/one.prefab', 'assets/absent.prefab'})
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(available_bounds_prefabs(path, requested), frozenset({'assets/one.prefab'}))
            data['MeshBounds'] = []
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(available_bounds_prefabs(path, requested), frozenset())
            del data['MeshBounds']
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(available_bounds_prefabs(path, requested), frozenset())


if __name__ == '__main__':
    unittest.main()
