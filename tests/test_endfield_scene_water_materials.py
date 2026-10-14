from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from endfield_scene_water_materials import (
    WaterMaterialPlan,
    build_water_material_plan,
    read_water_material_row,
)
from endfield_scene_water_plan import WaterGeometryRow, WaterMeshAsset


SCENE_PATH = "Assets/Beyond/Scenes/LunaScenes/Map02/ReleaseData"
GLOBAL_PATH = f"{SCENE_PATH}/WaterData/Global_Water_Config.asset"
CONFIG_PATH = f"{SCENE_PATH}/WaterData/Configs/water.asset"
ARRAY_PATH = f"{SCENE_PATH}/WaterData/NormalMapArray.asset"
FLOW_PATH = f"{SCENE_PATH}/WaterData/Flow/T_Global.asset"
SECTOR_PATH = f"{SCENE_PATH}/WaterData/Sector/T_0_0.asset"
SECTOR_TEXTURE_PATH = f"{SCENE_PATH}/WaterData/Sector/T_Texture0.asset"


def _asset(source: str, path_id: int, type_name: str) -> dict[str, object]:
    return {"Source": source, "PathId": path_id, "Type": type_name}


def _record(asset: dict[str, object], values: dict[str, object]) -> dict[str, object]:
    return {"Header": {"Asset": asset}, **values}


def _geometry_row() -> WaterGeometryRow:
    return WaterGeometryRow(
        entity_id=7,
        name="water",
        init_source_path="InitChunkData_test",
        streaming_source_path="StreamingChunkData_test",
        group_index=0,
        entity_index=0,
        matrix=(1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        world_bounds=(0.1, 0.2, 0.0, 1.0, 0.1, 0.2),
        ownership_anchor=(0.15, 0.15),
        render_type_mask=0,
        runtime_mesh_id=1,
        param0=(0.0, 0.0, 0.0, 0.0),
        param1=(0.0, 0.0, 0.0, 0.0),
        mesh_resource_path=f"{SCENE_PATH}/Water/water.fbx",
        mesh_hash_hex="0123456789abcdef",
        config_resource_path=CONFIG_PATH,
        config_hash_hex="fedcba9876543210",
        mesh_file="water.obj",
        mesh_asset=WaterMeshAsset("CAB-mesh", 6, "water"),
    )


def _write_manifest(
    root: Path,
    *,
    use_flow_map: int,
    foam_opacity: float,
    export_flow_map: bool,
    export_sector_texture: bool,
) -> Path:
    flow_asset = _asset("CAB-flow", 10, "Texture2D")
    sector_texture_asset = _asset("CAB-sector-texture", 11, "Texture2D")
    array_asset = _asset("CAB-normal-array", 12, "Texture2DArray")
    global_asset = _asset("CAB-global", 13, "MonoBehaviour")
    config_asset = _asset("CAB-config", 14, "MonoBehaviour")
    sector_asset = _asset("CAB-sector", 15, "MonoBehaviour")
    global_record = _record(global_asset, {
        "ScenePath": SCENE_PATH,
        "FlowMap": {"IsNull": False, "Target": flow_asset},
        "NormalMapArray": {"IsNull": False, "Target": array_asset},
        "UseSectorData": 1,
        "MapSize": 2,
        "SectorSize": 1,
        "SceneCenterOffset": {"X": 0.0, "Y": 0.0},
        "SectorDataPaths": [
            f"{SCENE_PATH}/WaterData/Sector/T_-1_-1.asset",
            f"{SCENE_PATH}/WaterData/Sector/T_-1_0.asset",
            f"{SCENE_PATH}/WaterData/Sector/T_0_-1.asset",
            SECTOR_PATH,
        ],
        "SectorDataExists": [False, False, False, True],
        "SectorDataAssetHashes": [-1, -1, -1, 100],
    })
    config_record = _record(config_asset, {
        "Flow": {
            "UseFlowMap": use_flow_map,
            "MaterialIndex": 0,
            "FlowDirection": 0.0,
            "FlowDirectionVector2": {"X": 1.0, "Y": 0.0},
            "SmallWaveNormalType": 0,
            "LargeWaveNormalType": 1,
            "FlowSpeed": 0.1,
            "LargeWaveFlowSpeed": 0.2,
            "NormalTilling": 1.0,
            "LargeWaveNormalTilling": 2.0,
            "NormalScale": 0.5,
            "LargeWaveNormalScale": 0.75,
        },
        "Appearance": {"WaterIor": 1.333, "FoamOpacity": foam_opacity},
    })
    sector_record = _record(sector_asset, {
        "WaterSectorTexture0": {"IsNull": False, "Target": sector_texture_asset},
    })
    containers = [
        {"Container": GLOBAL_PATH, "Asset": global_asset},
        {"Container": CONFIG_PATH, "Asset": config_asset},
        {"Container": ARRAY_PATH, "Asset": array_asset},
        {"Container": FLOW_PATH, "Asset": flow_asset},
        {"Container": SECTOR_PATH, "Asset": sector_asset},
        {"Container": SECTOR_TEXTURE_PATH, "Asset": sector_texture_asset},
    ]
    texture_exports: list[dict[str, object]] = []
    if export_flow_map:
        (root / "flow.png").write_bytes(b"flow")
        texture_exports.append({"Asset": flow_asset, "File": "flow.png"})
    if export_sector_texture:
        (root / "sector.png").write_bytes(b"sector")
        texture_exports.append({"Asset": sector_texture_asset, "File": "sector.png"})
    (root / "normal0.png").write_bytes(b"normal0")
    (root / "normal1.png").write_bytes(b"normal1")
    payload = {
        "Format": "EndfieldSceneProbe/1",
        "Containers": containers,
        "Water": {
            "GlobalConfigs": [global_record],
            "Configs": [config_record],
            "Sectors": [sector_record],
        },
        "TextureExports": texture_exports,
        "TextureArrayExports": [{
            "Asset": array_asset,
            "Depth": 2,
            "Slices": [
                {"Asset": array_asset, "SliceIndex": 0, "File": "normal0.png"},
                {"Asset": array_asset, "SliceIndex": 1, "File": "normal1.png"},
            ],
        }],
    }
    manifest = root / "scene_manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


class WaterMaterialResourceTests(unittest.TestCase):
    def test_duplicate_texture_and_array_pixels_accept_different_png_compression(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            roots = (Path(temporary) / "old", Path(temporary) / "new")
            manifests = []
            for root, compression in zip(roots, (0, 9)):
                root.mkdir()
                manifests.append(_write_manifest(root, use_flow_map=1, foam_opacity=1.0,
                                                 export_flow_map=True, export_sector_texture=True))
                for name in ("flow.png", "normal0.png"):
                    with Image.new("RGBA", (64, 64), (17, 42, 99, 81)) as image:
                        image.save(root / name, compress_level=compression)
            self.assertNotEqual((roots[0] / "flow.png").read_bytes(),
                                (roots[1] / "flow.png").read_bytes())
            plan = build_water_material_plan(tuple(manifests), (_geometry_row(),), "map02")
            self.assertEqual((), plan.pending)
            self.assertEqual(1, len(plan.rows))

    def test_duplicate_texture_rejects_changed_alpha_or_dimensions(self) -> None:
        for size, alpha in (((64, 64), 82), ((128, 32), 81)):
            with self.subTest(size=size, alpha=alpha), tempfile.TemporaryDirectory() as temporary:
                roots = (Path(temporary) / "old", Path(temporary) / "new")
                manifests = []
                for root, dimensions, opacity in ((roots[0], (64, 64), 81),
                                                   (roots[1], size, alpha)):
                    root.mkdir()
                    manifests.append(_write_manifest(root, use_flow_map=1, foam_opacity=1.0,
                                                     export_flow_map=True, export_sector_texture=True))
                    with Image.new("RGBA", dimensions, (17, 42, 99, opacity)) as image:
                        image.save(root / "flow.png")
                with self.assertRaisesRegex(ValueError, "WATER_MATERIAL_EXPORT_PAYLOAD_CONFLICT"):
                    build_water_material_plan(tuple(manifests), (_geometry_row(),), "map02")

    def _plan(
        self,
        root: Path,
        use_flow_map: int,
        foam_opacity: float,
        export_flow_map: bool,
        export_sector_texture: bool,
    ) -> WaterMaterialPlan:
        manifest = _write_manifest(
            root,
            use_flow_map=use_flow_map,
            foam_opacity=foam_opacity,
            export_flow_map=export_flow_map,
            export_sector_texture=export_sector_texture,
        )
        return build_water_material_plan((manifest,), (_geometry_row(),), "map02")

    def test_foam_requires_global_flow_and_sector_resources_without_flow_direction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root, 0, 1.0, True, True)

            self.assertEqual((), plan.pending)
            self.assertEqual(1, len(plan.rows))
            row = plan.rows[0]
            self.assertFalse(row.flow.use_flow_map)
            self.assertIsNotNone(row.flow_map)
            self.assertEqual(1, len(row.sectors))
            self.assertIn(FLOW_PATH, plan.required_resource_paths)
            self.assertIn(SECTOR_PATH, plan.required_resource_paths)
            self.assertIn(SECTOR_TEXTURE_PATH, plan.required_resource_paths)

    def test_missing_global_flow_payload_is_pending_when_foam_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root, 0, 1.0, False, True)

            self.assertEqual((), plan.rows)
            self.assertEqual(["global_flow_map_payload_missing"], [item.reason for item in plan.pending])
            self.assertIn(FLOW_PATH, plan.required_resource_paths)
            self.assertIn(SECTOR_PATH, plan.required_resource_paths)

    def test_no_flow_map_or_sector_dependency_when_flow_and_foam_are_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root, 0, 0.0, False, False)

            self.assertEqual((), plan.pending)
            self.assertEqual(1, len(plan.rows))
            self.assertIsNone(plan.rows[0].flow_map)
            self.assertEqual((), plan.rows[0].sectors)
            self.assertNotIn(FLOW_PATH, plan.required_resource_paths)
            self.assertNotIn(SECTOR_PATH, plan.required_resource_paths)
            self.assertNotIn(SECTOR_TEXTURE_PATH, plan.required_resource_paths)

    def test_cached_rows_keep_supplied_flow_map_and_accept_legacy_missing_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = self._plan(root, 0, 1.0, True, True)
            cached_row = json.loads(json.dumps(asdict(plan.rows[0])))

            parsed_with_map = read_water_material_row(cached_row)
            self.assertIsNotNone(parsed_with_map.flow_map)
            self.assertEqual(str(root / "flow.png"), parsed_with_map.flow_map.file)

            cached_row["flow_map"] = None
            parsed_legacy = read_water_material_row(cached_row)
            self.assertIsNone(parsed_legacy.flow_map)


if __name__ == "__main__":
    unittest.main()
