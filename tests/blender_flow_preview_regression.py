"""Review editable flow on source-qualified mesh placements from a local sample."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

import bpy
from mathutils import Vector


def geometry_digest(objects: tuple[bpy.types.Object, ...]) -> str:
    digest = hashlib.sha256()
    for obj in objects:
        digest.update(obj.name.encode())
        for row in obj.matrix_world:
            digest.update(struct.pack('<4f', *row))
        for vertex in obj.data.vertices:
            digest.update(struct.pack('<3f', *vertex.co))
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from blender_effect_flow_preview import create_flow_preview_material, create_flow_ribbon

    bpy.ops.wm.open_mainfile(filepath=str(args.source), load_ui=False, use_scripts=False)
    objects = tuple(sorted((obj for obj in bpy.context.scene.objects
                    if obj.type == 'MESH' and 'endfield_fx_mesh_source' in obj), key=lambda obj: obj.name))
    if not objects or any('UVMap' not in obj.data.uv_layers for obj in objects):
        raise ValueError('Source flow sample requires qualified meshes and their UVMap')
    before = geometry_digest(objects)
    material = create_flow_preview_material('UVMap', (0.12, 0.9, 0.65), 0.08, 0.35, 4.0, 12.0)
    source_names = [obj.name for obj in objects]
    points = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    low = Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
    high = Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    center = (low + high) * 0.5
    extent = (high - low).length
    radii = ((high.x - low.x) * 0.7, (high.y - low.y) * 0.7)
    for obj in objects:
        obj.hide_render = True
        obj.hide_set(True)
    ribbons: list[bpy.types.Object] = []
    for index, scale in enumerate((0.72, 0.85, 1.0, 1.08)):
        ribbon = create_flow_ribbon(f'Editable Surrounding Flow {index+1}',
            (center.x, center.y, center.z + (index-1.5)*2.0),
            (radii[0]*scale, radii[1]*scale), 2.5, 3.0, index*1.3, 192)
        ribbon.data.materials.append(material)
        ribbons.append(ribbon)
    bpy.ops.object.camera_add(location=center + Vector((0.6, -0.8, 0.55)) * extent)
    scene = bpy.context.scene
    scene.camera = bpy.context.object
    scene.camera.rotation_euler = (center - scene.camera.location).to_track_quat('-Z', 'Y').to_euler()
    scene.camera.data.type = 'ORTHO'
    scene.camera.data.ortho_scale = extent * 1.05
    scene.camera.data.clip_end = 5000
    scene.world = bpy.data.worlds.new('Flow Review Background')
    scene.world.use_nodes = True
    scene.world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.005, 0.008, 0.012, 1)
    scene.world.node_tree.nodes['Background'].inputs['Strength'].default_value = 0.2
    scene.render.resolution_x = 640
    scene.render.resolution_y = 480
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.eevee.taa_render_samples = 16
    scene.cycles.samples = 16
    scene.render.fps = 24
    scene.render.fps_base = 1
    scene.frame_start, scene.frame_end = 1, 240
    scene.frame_set(48)
    blend = args.output / 'editable_flow.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    objects = tuple(scene.objects[name] for name in source_names)
    if geometry_digest(objects) != before:
        raise AssertionError('Flow preview changed source geometry or placement')
    image_hashes: dict[str, list[str]] = {}
    for engine in ('BLENDER_EEVEE_NEXT', 'CYCLES'):
        scene.render.engine = engine
        image_hashes[engine] = []
        for frame in (48, 72, 96):
            scene.frame_set(frame)
            image = args.output / f'{engine}_{frame}.png'
            scene.render.filepath = str(image)
            bpy.ops.render.render(write_still=True)
            image_hashes[engine].append(hashlib.sha256(image.read_bytes()).hexdigest())
        if len(set(image_hashes[engine])) != 3:
            raise AssertionError(f'Flow endpoints did not change: engine={engine}')
    (args.output / 'verification.json').write_text(json.dumps({
        'status': 'passed', 'source': str(args.source), 'delivery': str(blend),
        'mesh_count': len(objects), 'source_geometry_preserved': before,
        'reopened': True, 'frames': [48, 72, 96], 'image_sha256': image_hashes,
        'authored_ribbons': len(ribbons),
        'scope': 'Original source meshes preserved hidden; authored closed ribbons enclose source bounds. Continuous activation, geometry and shading are explicit artistic approximations.',
    }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
