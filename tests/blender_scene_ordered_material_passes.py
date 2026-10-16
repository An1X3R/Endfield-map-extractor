"""Check real source geometry and ordered material consumers in a saved scene."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blender_scene_material_passes import verify_ordered_material
from blender_scene_water_depth import bottom_surface_exclusion
from endfield_scene_material_contract import reference_key


def main() -> None:
    plan_path, result_path = (Path(value) for value in sys.argv[sys.argv.index("--") + 1:])
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if len(meshes) != len(plan["instances"]):
        raise ValueError("Saved object count differs from selected source entities")
    rows: list[dict[str, object]] = []
    for source in plan["instances"]:
        objects = [obj for obj in meshes if obj.get("entity_id") == str(source["entity_id"])]
        if len(objects) != 1:
            raise ValueError(f"Source entity does not have one original geometry object: entity={source['entity_id']}")
        obj = objects[0]
        source_faces = sum(line.startswith("f ") for line in Path(source["mesh_file"]).read_text().splitlines())
        if len(obj.data.polygons) != source_faces:
            raise ValueError(f"Ordered draws duplicated or omitted source faces: entity={source['entity_id']}")
        if json.loads(obj["endfield_submesh_material_passes"]) != source["submesh_material_passes"]:
            raise ValueError("Saved ordered source material references changed")
        ordered = [slot.material for slot in obj.material_slots if "endfield_ordered_material_refs" in slot.material]
        if len(ordered) != 1:
            raise ValueError("Expected one ordered material on the repeated source submesh")
        material = ordered[0]
        expected = [list(reference_key(ref)) for refs in source["submesh_material_passes"] if len(refs) > 1 for ref in refs]
        if verify_ordered_material(material)["source_material_refs"] != expected:
            raise ValueError("Saved material order differs from exact source draws")
        if bottom_surface_exclusion(material) is not None:
            raise ValueError("Validated opaque ordered material was excluded from water bottoms")
        rows.append({"entity_id": source["entity_id"], "source_faces": source_faces,
                     "saved_faces": len(obj.data.polygons), "source_material_refs": expected})
    material = ordered[0]
    tree = material.node_tree
    enable = tree.nodes["Source Opaque Pass 1 Enable"]
    enable.outputs[0].default_value = 0
    try:
        bottom_surface_exclusion(material)
    except ValueError:
        pass
    else:
        raise ValueError("Water bottom classification accepted disabled opaque source coverage")
    try:
        verify_ordered_material(material)
    except ValueError:
        pass
    else:
        raise ValueError("Ordered material audit accepted disabled opaque source coverage")
    finally:
        enable.outputs[0].default_value = 1
    mix = tree.nodes["Ordered Source Overwrite 1"]
    source_socket = mix.inputs[2].links[0].from_socket
    tree.links.remove(mix.inputs[2].links[0])
    try:
        verify_ordered_material(material)
    except ValueError:
        pass
    else:
        raise ValueError("Ordered material audit accepted a disconnected source pass")
    finally:
        tree.links.new(source_socket, mix.inputs[2])
    verify_ordered_material(material)
    images = [image for image in bpy.data.images if image.source == "FILE" and image.users]
    if any(not image.packed_file for image in images):
        raise ValueError("An original ordered-pass texture is not packed")
    result_path.write_text(json.dumps({"format": "OrderedSourceMaterialRegression/1", "objects": rows,
        "used_packed_images": len(images), "opaque_water_bottom_materials": len(rows),
        "negative_checks": ["disabled_coverage", "disabled_water_bottom_coverage", "disconnected_source_pass"],
        "saved_blend_unchanged": True}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
