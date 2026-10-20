"""Render a real packed CharacterNPR VFX normal at source and zero strength."""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def render_pixels(path: Path) -> array:
    bpy.context.scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    image = bpy.data.images.load(str(path), check_existing=False)
    values = array('f', [0.0]) * len(image.pixels)
    image.pixels.foreach_get(values)
    bpy.data.images.remove(image)
    return values


def main(blend: Path, material_identity: str, output: Path) -> None:
    before_hash = hashlib.sha256(blend.read_bytes()).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
    cab, path_id = material_identity.rsplit(':', 1)
    identity = (cab.casefold(), int(path_id))
    groups = tuple(tree for tree in bpy.data.node_groups
                   if tree.get('source_identity') and tuple(json.loads(tree['source_identity'])) == identity
                   and 'endfield_character_vfx_normal_preview' in tree)
    if len(groups) != 1:
        raise ValueError(f'Real blend must have one exact source material group: identity={identity}, groups={len(groups)}')
    group = groups[0]
    source = json.loads(group['endfield_fx_source_material_json'])
    if source['Floats']['_EnableNormalMap'] != 1 or source['Floats']['_UseFresnel'] != 1:
        raise AssertionError('Real normal and Fresnel source switches must remain enabled')
    normal_record = json.loads(group['endfield_character_vfx_normal_preview'])
    packed = group.nodes['Character VFX BC5 RG Normal Decode']
    normal = group.nodes['Character VFX Tangent Normal UV0']
    sample = group.nodes['Source _NormalMap']
    target = group.nodes['Fresnel Source Normal']
    expected_texture = tuple(normal_record['source_texture'])
    if tuple(json.loads(sample['source_identity'])) != expected_texture:
        raise AssertionError('Actual Fresnel normal texture lost its source identity')
    if sample.image is None or sample.image.packed_file is None or sample.image.colorspace_settings.name != 'Non-Color':
        raise AssertionError('Actual Fresnel normal texture must be packed linear data')
    if packed.node_tree.get('endfield_contract') != 'HGRPPackedNormal/1' or packed.inputs['Multiply Red By Alpha'].default_value != 0:
        raise AssertionError('BC5 RG normals must use the existing shared decoder without alpha multiplication')
    if normal.uv_map != 'UV0' or len(target.inputs[0].links) != 1 or target.inputs[0].links[0].from_node != normal:
        raise AssertionError('Decoded source normal does not reach the real Fresnel consumer')
    source_strength = source['Floats']['_NormalScale']
    if packed.inputs['Normal Strength'].default_value != source_strength or source_strength <= 0:
        raise AssertionError('Source normal strength was not preserved')
    targets = tuple(obj for obj in bpy.context.scene.objects if obj.type == 'MESH' and obj.material_slots
                    and obj.material_slots[0].material is not None and obj.material_slots[0].material.get('source_material_stack')
                    and identity in tuple(tuple(item) for item in json.loads(obj.material_slots[0].material['source_material_stack']))
                    and any(item.type == 'GROUP' and item.node_tree == group for item in obj.material_slots[0].material.node_tree.nodes))
    if len(targets) != 1:
        raise ValueError(f'Normal regression requires one real source mesh: identity={identity}, count={len(targets)}')
    obj = targets[0]
    object_name = obj.name
    for item in bpy.context.scene.objects:
        if 'potential_appearance' in item:
            item['potential_appearance'] = True
        if item.type == 'MESH':
            for property_name in ('hide_render', 'hide_viewport'):
                item.driver_remove(property_name)
            item.hide_render = item != obj
            item.hide_viewport = item != obj
            item.hide_set(item != obj)
    bpy.context.view_layer.update()
    points = tuple(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    low = Vector(tuple(min(point[index] for point in points) for index in range(3)))
    high = Vector(tuple(max(point[index] for point in points) for index in range(3)))
    center = (low + high) * 0.5
    extent = max(high - low) * 1.4
    if extent <= 0:
        raise ValueError('Real normal mesh has an empty bounding box')
    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        raise ValueError('Real blend has no explicit camera')
    camera.location = center + Vector((0.4, -1.0, 0.1)).normalized() * extent * 3
    camera.rotation_euler = (center - camera.location).to_track_quat('-Z', 'Y').to_euler()
    camera.data.type = 'ORTHO'
    camera.data.ortho_scale = extent
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
    scene.eevee.taa_render_samples = 16
    scene.render.resolution_x = 384
    scene.render.resolution_y = 384
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.film_transparent = True
    scene.render.use_compositing = False
    delivery = output / 'normal_source.blend'
    group_name = group.name
    bpy.ops.wm.save_as_mainfile(filepath=str(delivery))
    bpy.ops.wm.open_mainfile(filepath=str(delivery), load_ui=False, use_scripts=False)
    group = bpy.data.node_groups[group_name]
    packed = group.nodes['Character VFX BC5 RG Normal Decode']
    if group.nodes['Source _NormalMap'].image.packed_file is None:
        raise AssertionError('Source normal image did not survive save and reopen')
    original = render_pixels(output / 'normal_source.png')
    packed.inputs['Normal Strength'].default_value = 0.0
    neutral = render_pixels(output / 'normal_zero_strength.png')
    packed.inputs['Normal Strength'].default_value = source_strength
    differences = [abs(left - right) for left, right in zip(original, neutral, strict=True)]
    changed = sum(value > 1 / 255 for value in differences)
    if changed == 0:
        raise AssertionError('Actual source normal produces no measurable Fresnel render change')
    if hashlib.sha256(blend.read_bytes()).hexdigest() != before_hash:
        raise AssertionError('Regression modified its input blend')
    receipt = {'status': 'passed', 'input_blend': str(blend), 'source_identity': identity,
               'texture_identity': expected_texture, 'source_strength': source_strength,
               'reopened_packed_texture': True, 'real_mesh': object_name,
               'changed_components_above_1_over_255': changed, 'max_component_difference': max(differences),
               'mean_component_difference': sum(differences) / len(differences),
               'scope': 'Real source texture, UV0, shared BC5 decode and connected Fresnel influence; native TBN and lighting equivalence unverified.'}
    (output / 'verification.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    parser.add_argument('--material-identity', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.blend, args.material_identity, args.output)
