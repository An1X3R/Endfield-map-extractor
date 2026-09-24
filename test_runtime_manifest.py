from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from extractor_core.runtime_manifest import augment_runtime_manifest


class RuntimeManifestDiscoveryTests(unittest.TestCase):
    def test_adds_new_level_and_only_fills_unmapped_region_sectors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scene = root / "scene_manifest.json"
            scene.write_text(
                json.dumps(
                    {
                        "TextureExports": [
                            {"Asset": {"Name": "h_map02_lv009_1_1"}, "File": "textures/a.png"},
                            {"Asset": {"Name": "h_map02_lv009_2_1"}, "File": "textures/b.png"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            regions = root / "regions.log"
            regions.write_text(
                "{'levelId':'map02_lv009','shapeList':[{'position':{'x':-100,'z':100},'size':{'x':200,'z':100}}]}\n",
                encoding="utf-8",
            )
            base = {
                "format": "EndfieldRegionMapRuntimeManifest/1",
                "resolverVersion": "endfield-sector128-v1",
                "coordinateConvention": {},
                "maps": {
                    "map02": {
                        "mapId": "map02",
                        "domainId": "domain_1",
                        "worldBounds": {"xmin": -256, "xmax": 512, "zmin": -256, "zmax": 512},
                        "sideSectors": 6,
                        "sectorSizeMetres": 128,
                        "ownershipGridOrder": "rows south-to-north; columns west-to-east",
                        "ownershipGrid": [[None] * 6 for _ in range(6)],
                        "levels": [],
                        "overview": {},
                    }
                },
            }
            result = augment_runtime_manifest(base, scene_manifest=scene, region_records=regions)
            spec = result["maps"]["map02"]["levels"][0]
            self.assertEqual(spec["levelId"], "map02_lv009")
            self.assertEqual(spec["hTileGrid"], {"x": 2, "z": 1})
            self.assertGreater(spec["ownershipSectorCount"], 0)
            self.assertTrue(any(value == "map02_lv009" for row in result["maps"]["map02"]["ownershipGrid"] for value in row))


if __name__ == "__main__":
    unittest.main()
