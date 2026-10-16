"""Exercise source face retention, measured-only media and open-depth engine wiring."""
from pathlib import Path
import json
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from blender_scene_water import WaterOptics, adapt_water_surface
from blender_scene_water_depth import BottomSource, DepthHit, WaterDepthResult
from blender_water_open_surface import (DEPTH_VALID_ATTRIBUTE, OPEN_DEPTH_POLICY,
    UNMEASURED_OPTICAL_DEPTH, bind_open_water_surface, measured_water_volume, unmeasured_water_faces)


def main() -> None:
    mesh = bpy.data.meshes.new("Authored water")
    points = [(0., 0., 2.), (1., 0., 2.), (0., 1., 2.), (1., 1., 2.)]
    mesh.from_pydata(points, [], [(0, 1, 2), (1, 3, 2)])
    uv = mesh.uv_layers.new(name="UVMap")
    for loop in mesh.loops:
        uv.data[loop.index].uv = points[loop.vertex_index][:2]
    obj = bpy.data.objects.new(mesh.name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    provenance = BottomSource("Measured bottom", None, False, tuple(tuple(row) for row in obj.matrix_world))
    depth = WaterDepthResult(obj.name, tuple(DepthHit(2., provenance, 0) if i != 3 else None for i in range(4)))
    material = bpy.data.materials.new("Water")
    material.use_nodes = True
    normal = material.node_tree.nodes.new("ShaderNodeNewGeometry").outputs["Normal"]
    adapt_water_surface(material, normal, WaterOptics(1.333, .2, .35, .015, (.08, .72, .58, 1.), "endfield_water_depth"))
    mesh.materials.append(material)
    attribute = mesh.attributes.new("endfield_water_depth", "FLOAT", "POINT")
    attribute.data.foreach_set("value", tuple(hit.depth if hit else UNMEASURED_OPTICAL_DEPTH for hit in depth.hits))
    faces = unmeasured_water_faces(obj, depth)
    volume = measured_water_volume(obj, depth, material)
    assert volume is not None
    bpy.context.scene.collection.objects.link(volume)
    assert volume['endfield_water_volume_source'] == obj.name
    assert max(vertex.co.x + vertex.co.y for vertex in volume.data.vertices) <= 1.
    bind_open_water_surface(obj, depth, material, faces)
    assert len(mesh.polygons) == 2 and len(mesh.vertices) == 4
    assert [face.material_index for face in mesh.polygons] == [0, 1]
    assert [x.value for x in mesh.attributes[DEPTH_VALID_ATTRIBUTE].data] == [1., 1., 1., 0.]
    for loop in mesh.loops:
        assert tuple(mesh.uv_layers['UVMap'].data[loop.index].uv) == points[loop.vertex_index][:2]
    open_material = mesh.materials[1]
    tree = open_material.node_tree
    assert tree.nodes['EF_Water_Output_CYCLES'].inputs['Surface'].links[0].from_socket == tree.nodes['EF_Water_Output_EEVEE'].inputs['Surface'].links[0].from_socket
    assert obj['endfield_water_depth_policy'] == OPEN_DEPTH_POLICY
    empty_depth = WaterDepthResult(obj.name, (None,) * 4)
    assert measured_water_volume(obj, empty_depth, material) is None
    root = Path(sys.argv[sys.argv.index('--') + 1])
    root.mkdir(parents=True, exist_ok=True)
    blend = root / 'open_water.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    saved = bpy.data.objects['Authored water']
    assert len(saved.data.polygons) == 2 and saved['endfield_water_unverified_depth_polygons'] == 1
    (root/'verification.json').write_text(json.dumps({'sourceFacesPreserved':2, 'unmeasuredFaces':1,
        'measuredOnlyVolume':True, 'unknownBottomNotInvented':True, 'uvPreserved':True,
        'bothEnginesBoundaryWired':True, 'reopenVerified':True}, indent=2), 'utf-8')


if __name__ == '__main__':
    main()
