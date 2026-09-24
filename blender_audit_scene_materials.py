"""Read a delivered scene and report material coverage separately from geometry resolution."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from endfield_scene_material_contract import is_neutral_material_name


def material_state(obj: bpy.types.Object) -> str:
    materials = tuple(obj.data.materials)
    if not materials or any(material is None for material in materials):
        return "missing_material_slot"
    if any(is_neutral_material_name(material.name) for material in materials):
        return "neutral"
    if any(material.use_nodes and any(node.type == "TEX_IMAGE" and node.image for node in material.node_tree.nodes)
           for material in materials):
        return "textured"
    return "non_neutral_without_image"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if args.audit.exists():
        raise FileExistsError(args.audit)
    bpy.ops.wm.open_mainfile(filepath=str(args.blend))
    counts: Counter[str] = Counter()
    for obj in bpy.data.objects:
        if "instance_count" not in obj:
            continue
        sources = {node.inputs["Object"].default_value.name
                   for modifier in obj.modifiers if modifier.type == "NODES" and modifier.node_group
                   for node in modifier.node_group.nodes
                   if node.type == "OBJECT_INFO" and node.inputs["Object"].default_value}
        if len(sources) != 1:
            raise ValueError(f"Instance object requires exactly one source template: object={obj.name}, sources={sorted(sources)}")
        counts[next(iter(sources))] += int(obj["instance_count"])
    templates = [{"object": obj.name, "source_asset": str(obj["source_asset"]), "state": material_state(obj), "instances": counts[obj.name],
                  "slots": len(obj.data.materials)}
                 for obj in bpy.data.objects if obj.type == "MESH" and obj.get("source_asset")]
    instance_states: Counter[str] = Counter()
    for row in templates:
        instance_states[row["state"]] += row["instances"]
    direct = [{"object": obj.name, "entity_id": int(obj["entity_id"]), "state": material_state(obj)}
              for obj in bpy.data.objects if obj.type == "MESH" and obj.get("source_asset")
              and "entity_id" in obj and "instance_count" not in obj]
    images = [image for image in bpy.data.images if image.users and image.source == "FILE"]
    unpacked = [image.name for image in images if not image.packed_file]
    if unpacked:
        raise ValueError(f"Scene contains unpacked file textures: images={unpacked}")
    terrain = [obj for obj in bpy.data.objects if obj.type == "MESH" and "unity_tile" in obj]
    terrain_textured = [obj for obj in terrain if any(material and material.get("endfield_terrain_surface_source")
                                                   for material in obj.data.materials)]
    for obj in terrain_textured:
        if obj.data.uv_layers.get("TerrainWorldUV") is None:
            raise ValueError(f"Terrain atlas UV missing: object={obj.name}")
    disconnected_albedo = []
    for material in bpy.data.materials:
        if not material.users or not material.get("endfield_surface_contract") or not material.use_nodes:
            continue
        nodes = material.node_tree.nodes
        if any(node.type == "TEX_IMAGE" and node.label in ("_BaseColorMap", "_BaseMap", "_MainTex") for node in nodes):
            if any(node.type == "BSDF_PRINCIPLED" and not node.inputs["Base Color"].is_linked for node in nodes):
                disconnected_albedo.append(material.name)
    result = {"format": "EndfieldSceneMaterialCoverage/1", "blend": str(args.blend),
              "bytes": args.blend.stat().st_size, "reopen_verified": True,
              "template_states": dict(Counter(row["state"] for row in templates)),
              "instance_states": dict(instance_states), "templates": templates,
              "direct_object_states": dict(Counter(row["state"] for row in direct)), "direct_objects": direct,
              "terrain_tiles": len(terrain), "terrain_textured_tiles": len(terrain_textured),
              "terrain_untextured_tiles": [{"tile": str(obj["unity_tile"]), "vertices": len(obj.data.vertices),
                                             "faces": len(obj.data.polygons)} for obj in terrain if obj not in terrain_textured],
              "projected_decals": sum("projected_decal_entity_id" in obj for obj in bpy.data.objects),
              "packed_file_images": len(images), "unpacked_file_images": unpacked,
              "legacy_shader_albedo_connection_review": disconnected_albedo,
              "geometry_unresolved_instances": bpy.context.scene.get("unresolved_instance_count"),
              "surface_status": bpy.context.scene.get("surface_material_status"),
              "decal_receiver_status": bpy.context.scene.get("projected_receiver_material_status"),
              "coverage_is_shader_fidelity": False}
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in ("templates", "direct_objects")}), flush=True)


if __name__ == "__main__":
    main()
