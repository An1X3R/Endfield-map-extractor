"""Verify foam on real saved portable and source-transport water exports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_water_foam import reconnect_source_water_foam


def verify_export(path: Path, expected_targets: set[str]) -> dict[str, object]:
    bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False)
    waters = [obj for obj in bpy.context.scene.objects
              if obj.type == "MESH" and obj.get("endfield_category") == "water"]
    checked: list[dict[str, object]] = []
    for water in waters:
        material = water.data.materials[0]
        if material.get("endfield_water_foam_status") != "source_mask_host_surface":
            continue
        tree = material.node_tree
        outputs = [node for node in tree.nodes if node.type == "OUTPUT_MATERIAL"]
        if {node.target for node in outputs} != expected_targets:
            raise AssertionError(f"Unexpected foam render targets: material={material.name}")
        before = len(tree.nodes)
        reconnect_source_water_foam(material)
        if len(tree.nodes) != before:
            raise AssertionError(f"Repeated foam connection creates nodes: material={material.name}")
        underlying: list[str] = []
        for output in outputs:
            surface = output.inputs["Surface"]
            if len(surface.links) != 1 or surface.links[0].from_node.type != "MIX_SHADER":
                raise AssertionError(f"Missing foam mix: material={material.name}, target={output.target}")
            mix = surface.links[0].from_node
            for index, name in ((0, "EF_Water_Foam_Coverage"), (2, "EF_Water_Foam_Surface")):
                links = mix.inputs[index].links
                if len(links) != 1 or links[0].from_node.name != name:
                    raise AssertionError(f"Foam branch disconnected: target={output.target}, input={index}")
            base = mix.inputs[1].links
            if len(base) != 1 or base[0].from_node == mix:
                raise AssertionError(f"Water base lost or recursive: target={output.target}")
            underlying.append(base[0].from_node.name)
        if len(set(underlying)) != len(outputs):
            raise AssertionError("Engine-specific water bases were collapsed into one surface")
        for node in tree.nodes:
            if node.type == "TEX_IMAGE" and (node.image is None or len(node.image.pixels) == 0):
                raise AssertionError(f"Water texture unavailable after reopen: node={node.name}")
        checked.append({"water": water.name, "targets": sorted(expected_targets), "underlying": underlying})
    if not checked:
        raise AssertionError(f"Fixture contains no restored source foam: path={path}")
    return {"blend": str(path), "checked": checked}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portable-blend", type=Path, required=True)
    parser.add_argument("--transport-blend", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    results = [verify_export(args.portable_blend, {"CYCLES", "EEVEE"}),
               verify_export(args.transport_blend, {"ALL"})]
    args.receipt.write_text(json.dumps({"status": "passed", "exports": results}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
