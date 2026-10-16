"""Verify native face retention, depth indexing, UVs and closed partial volumes."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_water_build import retain_measured_water_faces
from blender_scene_water_depth import BottomSource, DepthHit, WaterDepthResult
from blender_scene_water import WaterOptics, adapt_water_surface, create_water_volume


def main() -> None:
    result_path = Path(sys.argv[sys.argv.index("--") + 1])
    mesh = bpy.data.meshes.new("Partial native water")
    points = [(0., 0., 10.), (1., 0., 10.), (0., 1., 10.), (1., 1., 10.), (2., 0., 10.), (2., 1., 10.)]
    mesh.from_pydata(points, [], [(0, 1, 2), (1, 3, 2), (1, 4, 3), (4, 5, 3)])
    layer = mesh.uv_layers.new(name="SourceUV")
    for loop in mesh.loops:
        layer.data[loop.index].uv = points[loop.vertex_index][:2]
    obj = bpy.data.objects.new(mesh.name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    source = BottomSource("Measured bottom", None, False, tuple(tuple(row) for row in obj.matrix_world))
    hits = tuple(None if index == 3 else DepthHit(1. + index, source, index) for index in range(6))
    result = retain_measured_water_faces(obj, WaterDepthResult(obj.name, hits))
    assert len(obj.data.polygons) == 1
    assert len(obj.data.vertices) == 3
    for vertex, hit in zip(obj.data.vertices, result.hits, strict=True):
        original = points.index(tuple(vertex.co))
        assert hit == hits[original]
    for loop in obj.data.loops:
        assert tuple(obj.data.uv_layers['SourceUV'].data[loop.index].uv) == tuple(obj.data.vertices[loop.vertex_index].co)[:2]
    assert not obj.data.attributes.get("endfield_measured_source_index")
    material = bpy.data.materials.new("Partial water optics")
    material.use_nodes = True
    normal = material.node_tree.nodes.new('ShaderNodeNewGeometry').outputs['Normal']
    adapt_water_surface(material, normal, WaterOptics(1.333, 0.2, 0.35, 0.015, (0.08, 0.72, 0.58, 1.), 'measured_depth'))
    volume = create_water_volume(obj, tuple(hit.depth for hit in result.hits), material)
    bpy.context.scene.collection.objects.link(volume)
    incidence: dict[tuple[int, int], int] = {}
    for face in volume.data.polygons:
        for left, right in zip(face.vertices, (*face.vertices[1:], face.vertices[0])):
            key = tuple(sorted((left, right)))
            incidence[key] = incidence.get(key, 0) + 1
    assert all(count == 2 for count in incidence.values())
    removed = retain_measured_water_faces(obj, WaterDepthResult(obj.name, (None,) * 3))
    assert not obj.data.polygons and not obj.data.vertices and not removed.hits
    result_path.write_text(json.dumps({'retained_source_faces':1, 'uv_and_depth_mapping_preserved':True,
        'closed_volume':True, 'entirely_unmeasured_remains_empty':True}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
