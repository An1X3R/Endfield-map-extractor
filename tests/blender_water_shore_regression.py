"""Exercise shoreline cleanup with real Blender meshes and loop attributes."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_water_shore import _Vertex, _replace_mesh, _water_triangles, clip_water_to_heightfield


def mesh_object(name: str, coordinates: tuple[tuple[float, float, float], ...],
                faces: tuple[tuple[int, int, int], ...]) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(coordinates, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def main() -> None:
    water = mesh_object("RoundoffWater", (
        (0.0, 0.0, 65.0), (1.0, 0.0, 65.0), (0.0, 1.0, 65.0),
        (75.55274963378906, -4.579465866088867, 65.03947448730469),
        (72.49207305908203, -7.472909450531006, 65.02694702148438),
        (69.431396484375, -10.366353034973145, 65.0144271850586),
    ), ((0, 1, 2), (3, 4, 5)))
    materials = tuple(bpy.data.materials.new(name) for name in ("WaterUnused", "WaterSurface"))
    for material in materials:
        water.data.materials.append(material)
    for face in water.data.polygons:
        face.material_index = 1
    expected_uvs = ((0.0, 0.0), (0.5, 0.0), (0.0, 0.5))
    for layer_name in ("UVMap", "Secondary"):
        layer = water.data.uv_layers.new(name=layer_name)
        for index, uv in enumerate((*expected_uvs, *expected_uvs)):
            layer.data[index].uv = uv
    terrain = mesh_object("DistantVerifiedTerrain", (
        (10000.0, 10000.0, -100.0), (10001.0, 10000.0, -100.0), (10000.0, 10001.0, -100.0),
    ), ((0, 1, 2),))
    terrain["endfield_source_heightfield"] = True
    assert _water_triangles(water).removed_degenerate_triangles == 1
    assert clip_water_to_heightfield(water, (terrain,), 0.02) == 0
    assert water["endfield_water_shore_removed_degenerate_triangles"] == 1
    assert len(water.data.polygons) == 1
    assert len(water.data.vertices) == 3
    assert tuple(water.data.materials) == materials
    assert water.data.polygons[0].material_index == 1
    assert tuple(layer.name for layer in water.data.uv_layers) == ("UVMap", "Secondary")
    for layer in water.data.uv_layers:
        assert tuple(tuple(loop.uv) for loop in layer.data) == expected_uvs
    assert _water_triangles(water).removed_degenerate_triangles == 0
    tiny = mesh_object("TinyFloat32Water", (
        (3.814697265625e-06, 94.47230529785156, 242.0),
        (4.76837158203125e-06, 88.36265563964844, 242.0),
        (5.7220458984375e-06, 82.25301361083984, 242.0),
    ), ((0, 1, 2),))
    assert _water_triangles(tiny).removed_degenerate_triangles == 1
    subpixel = mesh_object("SubpixelClipFragment", ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                           ((0, 1, 2),))
    subpixel_points = (
        _Vertex(1.0057473717934124, 54.99425262820659, 67.67266091140367, ()),
        _Vertex(1.0066003769268612, 55.0, 67.67250006871188, ()),
        _Vertex(1.0066006423335425, 55.0, 67.67249644215637, ()),
    )
    subpixel_removed = _replace_mesh(subpixel, ((subpixel_points, 0),))
    assert subpixel_removed == 1
    assert len(subpixel.data.polygons) == 0
    shared_a = _Vertex(85.611572265625, 40.388427734375, 65.69511413574219, ())
    shared_b = _Vertex(86.0, 40.855873107910156, 65.69247436523438, ())
    rounded_b = _Vertex(86.0, 40.85586929321289, 65.69247436523438, ())
    opposite_a = _Vertex(85.76948547363281, 41.0, 65.71758270263672, ())
    opposite_b = _Vertex(85.67184448242188, 40.32815170288086, 65.6871337890625, ())
    slivers = mesh_object("RoundoffClipSlivers", ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                          ((0, 1, 2),))
    removed = _replace_mesh(slivers, (
        ((opposite_a, shared_a, shared_b), 0),
        ((opposite_b, shared_b, shared_a), 0),
        ((shared_b, shared_a, rounded_b), 0),
        ((rounded_b, shared_a, shared_b), 0),
    ))
    assert removed == 2
    assert len(slivers.data.polygons) == 2
    incidence: dict[tuple[int, int], int] = {}
    for face in slivers.data.polygons:
        for a, b in zip(face.vertices, (*face.vertices[1:], face.vertices[0]), strict=True):
            edge = tuple(sorted((a, b)))
            incidence[edge] = incidence.get(edge, 0) + 1
    max_edge_incidence = max(incidence.values())
    assert max_edge_incidence == 2
    vertical = mesh_object("ValidVerticalWater", (
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0),
    ), ((0, 1, 2),))
    try:
        clip_water_to_heightfield(vertical, (terrain,), 0.02)
    except ValueError as error:
        assert "WATER_SHORE_SURFACE_TRIANGLE_DEGENERATE" in str(error)
    else:
        raise AssertionError("A substantive vertical triangle must fail explicitly")
    assert len(vertical.data.polygons) == 1
    print(json.dumps({"water_shore_regression": "passed", "removed_degenerate_triangles": 1,
                      "dry_cut_triangles": 0, "uv_layers_preserved": 2,
                      "material_slots_preserved": 2, "tiny_surface_face_removed": 1,
                      "subpixel_clip_fragment_removed": subpixel_removed,
                      "roundoff_clip_slivers_removed": removed,
                      "max_edge_face_incidence": max_edge_incidence,
                      "vertical_triangle_rejected": True}), flush=True)


if __name__ == "__main__":
    main()
