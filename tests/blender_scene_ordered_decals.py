"""Exercise real projected decals on shared and ordered source materials."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import bpy
from mathutils import Matrix

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blender_add_projected_decals import project_decal, projected_material, read_decals, read_materials, receiver_index
from blender_scene_material_passes import create_ordered_material, effective_opaque_surface, verify_ordered_material
from blender_scene_materials import create_material
from endfield_scene_export_plan import load_scene_export_plan
from endfield_scene_material_contract import reference_key
from endfield_scene_material_handoff import load_plan_material_catalog


def tree_signature(material: bpy.types.Material) -> tuple[object, ...]:
    tree = material.node_tree
    return (tuple((node.name, node.bl_idname) for node in tree.nodes),
            tuple((link.from_node.name, link.from_socket.identifier, link.to_node.name,
                   link.to_socket.identifier) for link in tree.links))


def main() -> None:
    plan_path, decal_path, decal_material_path, output, result_path = (Path(value) for value in sys.argv[sys.argv.index("--") + 1:])
    if output.exists() or result_path.exists():
        raise FileExistsError("Ordered decal regression outputs must be new")
    plan = load_scene_export_plan(plan_path)
    records, textures = load_plan_material_catalog(plan)
    passes = next(refs for refs in plan["instances"][0]["submesh_material_passes"] if len(refs) > 1)
    source_records = tuple(records[reference_key(ref)] for ref in passes)
    surfaces = tuple(create_material(record, textures, True) for record in source_records)
    snapshots = tuple(tree_signature(surface) for surface in surfaces)
    original_materials = set(bpy.data.materials)
    ordered = create_ordered_material(source_records, surfaces)
    if set(bpy.data.materials) != original_materials | {ordered}:
        raise ValueError("Grouping left temporary materials in the database")
    if snapshots != tuple(tree_signature(surface) for surface in surfaces):
        raise ValueError("Grouping changed a shared source material")
    if effective_opaque_surface(ordered) != surfaces[-1]:
        raise ValueError("Ordered receiver did not resolve its final opaque source")
    collection = bpy.data.collections.new("Ordered decal regression receivers")
    bpy.context.scene.collection.children.link(collection)
    originals = []
    for index, material in enumerate((surfaces[1], ordered)):
        mesh = bpy.data.meshes.new(f"Receiver {index}")
        mesh.from_pydata([(-0.18, -0.18, 0), (0.18, -0.18, 0), (0.18, 0.18, 0), (-0.18, 0.18, 0)], [], [(0, 1, 2), (0, 2, 3)])
        for name in ("UVMap", "EF_SOURCE_UV1", "EF_SOURCE_UV2"):
            mesh.uv_layers.new(name=name)
        mesh.materials.append(material)
        obj = bpy.data.objects.new(f"Receiver {index}", mesh)
        obj.location.x = -0.25 if index == 0 else 0.25
        obj["entity_id"] = str(index)
        obj["source_asset"] = "regression_projection_fixture"
        collection.objects.link(obj)
        originals.append((obj.name, len(mesh.vertices), len(mesh.polygons), material.name))
    bpy.context.view_layer.update()
    source_decals = read_decals(decal_path)
    specs = read_materials(decal_material_path)
    source_decal = next(decal for decal in source_decals
        if specs[decal.material_path.casefold()].floats["_BaseTexChannel"] == 0
        and specs[decal.material_path.casefold()].floats["_SampleTex1UVSet"] == 0)
    identity = tuple(Matrix.Identity(4).transposed()[row][column] for row in range(4) for column in range(4))
    decal = replace(source_decal, matrix=identity, inverse_matrix=identity)
    spec = specs[decal.material_path.casefold()]
    receivers, grid = receiver_index()
    projected_collection = bpy.data.collections.new("Regression projected decals")
    bpy.context.scene.collection.children.link(projected_collection)
    material_cache = {}
    polygons, triangles = project_decal(decal, spec, receivers, grid, {}, material_cache, projected_collection, 0.001)
    if polygons != 4 or triangles != 4 or len(material_cache) != 2:
        raise ValueError(f"Projection did not retain both receiver surfaces: polygons={polygons}, triangles={triangles}")
    ordered_projected = material_cache[(ordered.name, spec.container)]
    if ordered_projected["receiver_effective_source_material"] != surfaces[-1].name:
        raise ValueError("Decal copied a non-effective ordered source layer")
    if snapshots != tuple(tree_signature(surface) for surface in surfaces):
        raise ValueError("Projection changed a shared source material")
    enable = ordered.node_tree.nodes["Source Opaque Pass 1 Enable"]
    enable.outputs[0].default_value = 0
    try:
        projected_material(ordered, spec)
    except ValueError:
        pass
    else:
        raise ValueError("Decal accepted a modified opaque overwrite chain")
    finally:
        enable.outputs[0].default_value = 1
    names = [material.name for material in material_cache.values()]
    ordered_name = ordered.name
    bpy.ops.wm.save_as_mainfile(filepath=str(output), compress=True)
    bpy.ops.wm.open_mainfile(filepath=str(output))
    verify_ordered_material(bpy.data.materials[ordered_name])
    for name, vertices, faces, material_name in originals:
        obj = bpy.data.objects[name]
        if (len(obj.data.vertices), len(obj.data.polygons), obj.data.materials[0].name) != (vertices, faces, material_name):
            raise ValueError("Projected decal changed original receiver geometry or material")
    for name in names:
        material = bpy.data.materials[name]
        principled = [node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"]
        if len(principled) != 1 or not principled[0].inputs["Base Color"].is_linked or not material["projected_receiver_alpha_preserved"]:
            raise ValueError("Saved decal lost its effective receiver shader or alpha")
    result_path.write_text(json.dumps({"format": "OrderedDecalRegression/1", "source_plan": str(plan_path),
        "decal_source_entity": source_decal.entity_id, "projection_transform": "identity_for_bounded_test_receivers",
        "source_materials_unchanged": True, "temporary_materials_removed": True,
        "ordinary_and_ordered_receivers": 2, "projected_polygons": polygons,
        "negative_checks": ["modified_overwrite_chain_rejected"], "reopened": True}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
