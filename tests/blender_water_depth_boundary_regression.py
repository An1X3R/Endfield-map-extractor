"""Check exact closed source edges without expanding bottoms across holes."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blender_scene_water_depth import BottomSource, boundary_segments, sample_water_depths


def mesh_object(name: str, vertices: list[tuple[float, float, float]],
                faces: list[tuple[int, ...]]) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def main() -> None:
    result_path = Path(sys.argv[sys.argv.index("--") + 1])
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    mesh_object("Lower source", [(256, 0, 1), (256, 1, 2), (255, 0, 1)], [(0, 1, 2)])
    mesh_object("Nearest source", [(256, 0, 11), (256, 1, 12), (255, 0, 11)], [(0, 1, 2)])
    mesh_object("Vertical wall", [(256, 4, 1), (256, 5, 1), (256, 4, 5)], [(0, 1, 2)])
    water = mesh_object("Water test points", [(256, 0.25, 20), (255.75, 0.25, 20),
        (256, 1.5, 20), (256, 4.25, 20), (255, 5, 20)], [])
    bpy.context.view_layer.update()
    graph = bpy.context.evaluated_depsgraph_get()
    audit = sample_water_depths(graph, (water,), 30, 0.01)
    edge, interior, hole, wall, far = audit.waters[0].hits
    if edge is None or abs(edge.depth - 8.75) > 1e-6 or edge.source.object_name != "Nearest source" or edge.polygon != 0:
        raise ValueError("Closed boundary did not retain the nearest interpolated real source face")
    if interior is None or abs(interior.depth - 8.75) > 1e-6:
        raise ValueError("Interior BVH bottom changed")
    if any(hit is not None for hit in (hole, wall, far)):
        raise ValueError("Boundary repair fabricated bottom geometry across a hole or vertical wall")
    source = BottomSource("Wall", None, False, tuple(tuple(row) for row in water.matrix_world))
    if boundary_segments(tuple(Vector(point) for point in ((256, 4, 1), (256, 5, 1), (256, 4, 5))),
                         source, 0, (255, 0, 20), (256, 5, 20)):
        raise ValueError("A vertical triangle was accepted as a bottom boundary segment")
    outside = mesh_object("Outside test points", [(256.001, 0.25, 20), (255.75, 0.25, 20)], [])
    bpy.context.view_layer.update()
    outside_hits = sample_water_depths(bpy.context.evaluated_depsgraph_get(), (outside, water), 30, 0.01).waters[0].hits
    if outside_hits[0] is not None or outside_hits[1] is None:
        raise ValueError("True outer point was filled or interior point was lost")
    result_path.write_text(json.dumps({"format": "ClosedWaterBottomBoundaryRegression/1",
        "nearest_source": edge.source.object_name, "interpolated_depth": edge.depth,
        "interior_bvh_preserved": True, "holes_and_vertical_walls_rejected": True,
        "true_outside_rejected": True, "bvh_epsilon": 0}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
