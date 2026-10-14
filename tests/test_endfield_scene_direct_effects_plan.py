from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from endfield_scene_direct_effects_plan import build_direct_effect_plan
from endfield_scene_vfxrefract_contract import read_scene_color_snapshot
from extractor_core.dynamic_renderers import (DynamicRendererPair, DynamicRendererRecord,
                                              DynamicRendererSource, DynamicSceneRendererInventory)
from extract_scene_renderer_materials import RendererDraw


def source_material() -> dict[str, object]:
    floats = {
        "_SurfaceType": 1, "_BlendMode": 0, "_SrcBlend": 5, "_DstBlend": 10,
        "_AlphaSrcBlend": 1, "_AlphaDstBlend": 10, "_ZWrite": 0, "_ZTest": 4,
        "_DisableZTest": 0, "_UseMask": 1, "_UseRBOffset": 1, "_UseRGBOffset": 0,
        "_UseBlend": 0, "_UseSaturation": 0, "_UseColorTex": 0, "_UseFresnel": 0,
        "_UseDissolve": 0, "_UseSpread": 0, "_UseTrail": 0, "_UseSoftBlend": 0,
        "_UsePolarUV": 0, "_RefractSwitchUV": 0, "_MaskSwitchUV": 0,
        "_DisableParticleInstanceColor": 0, "_DisableOccludedRefract": 0,
        "_RefractIsNormal": 1, "_RefractUseRBOffset": 1, "_UseMaskTexAsAlpha": 0,
        "_UseNearCameraFade": 0, "_MaskTexUseRefract": 0, "_CullMode": 0,
    }
    return {
        "Name": "M_test_refract", "Source": "material-cab", "PathId": 2,
        "Shader": {"Source": "shader-cab", "PathId": 8, "Name": "HGRP/Effect/VFXRefract"},
        "Textures": [], "Floats": floats,
        "Colors": {"_RefractUVSpeed": [0, 0, 0, 0], "_MaskTexUVSpeed": [0, 0, 0, 0]},
        "RenderState": {"ValidKeywords": ["_USE_MASK", "_USE_RBOFFSET"],
                         "DisabledShaderPasses": [], "CustomRenderQueue": 3000},
    }


def source_snapshot() -> dict[str, object]:
    return read_scene_color_snapshot({
        "format": "EndfieldSceneColorSnapshot/1",
        "camera_matrix_world": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        "lens_mm": 50, "resolution": [320, 240], "engine": "CYCLES",
        "samples": 8, "time_seconds": 0.5, "clip_start": 0.1, "clip_end": 100.0,
    })


class DirectEffectSnapshotPlanTests(unittest.TestCase):
    def test_vfxrefract_requires_snapshot_and_actual_uv0_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "mesh.bin").write_bytes(b"mesh")
            (root / "uv0.bin").write_bytes(bytes(3 * 2 * 4))
            mesh_asset = {"Source": "mesh-cab", "PathId": 1, "Type": "Mesh", "Name": "mesh"}
            material_asset = {"Source": "material-cab", "PathId": 2, "Type": "Material", "Name": "material"}
            manifest = root / "scene_manifest.json"
            manifest.write_text(json.dumps({
                "Format": "EndfieldSceneProbe/1",
                "Containers": [
                    {"Container": "assets/effects/mesh.mesh", "Asset": mesh_asset},
                    {"Container": "assets/effects/material.mat", "Asset": material_asset},
                ],
                "MeshExports": [{"Asset": mesh_asset, "File": "mesh.bin", "VertexCount": 3,
                                 "VertexColorComponents": 0, "Uv0Components": 2, "Uv1Components": 0,
                                 "Uv0File": "uv0.bin"}],
            }), encoding="utf-8")
            source_path = "Data/DynamicStreaming/PC/Scene/test/fb_init_0_0.bytes"
            source = DynamicRendererSource(source_path, "a" * 32, 100, 100, "b" * 64, "init")
            pair = DynamicRendererPair(
                "assets/effects/mesh.mesh##mesh", "assets/effects/material.mat", "0" * 16, "1" * 16,
                0, RendererDraw(0, 0, 100.0, 0.0), "2" * 80, "3" * 48,
            )
            record = DynamicRendererRecord(
                f"{source_path}:0:0", source_path, 0, 0, 0, 0, ("ref:0",), "fx", 
                (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
                 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0),
                None, (0.0,) * 20, 0, 0, 0, (pair,),
            )
            inventory = DynamicSceneRendererInventory("map01", (source,), (), (), (record,))
            materials = {("material-cab", 2): source_material()}
            without_snapshot = build_direct_effect_plan(
                inventory, {"xmin": 0.0, "xmax": 2.0, "zmin": 0.0, "zmax": 2.0},
                (manifest,), materials, None,
            )
            self.assertEqual(without_snapshot.rows, ())
            self.assertEqual(without_snapshot.pending[0]["reason"], "shader_family_pending")

            with_snapshot = build_direct_effect_plan(
                inventory, {"xmin": 0.0, "xmax": 2.0, "zmin": 0.0, "zmax": 2.0},
                (manifest,), materials, source_snapshot(),
            )
            self.assertEqual(len(with_snapshot.rows), 1)
            self.assertEqual(with_snapshot.pending, ())

            (root / "uv0.bin").unlink()
            no_uv_manifest = root / "no_uv_manifest.json"
            no_uv_manifest.write_text(json.dumps({
                "Format": "EndfieldSceneProbe/1",
                "Containers": [
                    {"Container": "assets/effects/mesh.mesh", "Asset": mesh_asset},
                    {"Container": "assets/effects/material.mat", "Asset": material_asset},
                ],
                "MeshExports": [{"Asset": mesh_asset, "File": "mesh.bin", "VertexCount": 3,
                                 "VertexColorComponents": 0, "Uv0Components": 0, "Uv1Components": 0}],
            }), encoding="utf-8")
            no_uv = build_direct_effect_plan(
                inventory, {"xmin": 0.0, "xmax": 2.0, "zmin": 0.0, "zmax": 2.0},
                (no_uv_manifest,), materials, source_snapshot(),
            )
            self.assertEqual(no_uv.rows, ())
            self.assertIn("source_uv0_required", no_uv.pending[0]["detail"])


if __name__ == "__main__":
    unittest.main()
