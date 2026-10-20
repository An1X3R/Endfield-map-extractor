"""Exercise an explicit full-material Fur appearance candidate on a real source-shell file."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy
import numpy as np
from mathutils import Vector

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO/'src'), str(REPO/'src/character_runtime')]

from audit_character_materials import source_geometry_digest
from blender_scene_materials import layout_shader_node_tree
from character_fur_appearance import FurAppearance, apply_fur_appearance, verify_fur_appearance
from character_fur_source_shell import verify_source_fur_shell


def positions(obj: bpy.types.Object) -> np.ndarray:
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    values = np.empty(len(evaluated.data.vertices)*3, dtype=np.float32)
    evaluated.data.vertices.foreach_get('co', values)
    matrix = np.asarray(evaluated.matrix_world, dtype=np.float64)
    return values.reshape(-1,3) @ matrix[:3,:3].T + matrix[:3,3]


parser = argparse.ArgumentParser()
parser.add_argument('--source', required=True, type=Path)
parser.add_argument('--output', required=True, type=Path)
args = parser.parse_args(sys.argv[sys.argv.index('--')+1:])
args.output.mkdir(parents=True, exist_ok=False)
bpy.ops.wm.open_mainfile(filepath=str(args.source), load_ui=False, use_scripts=False)
shell, = (item for item in bpy.data.objects if item.get('source_fur_shell') and not item.hide_render)
receipt = json.loads(shell['source_fur_shell'])
geometry_before = source_geometry_digest((shell.name, receipt['source_mesh']))
tree = bpy.data.node_groups[receipt['source_group']]
source_before = {key:tree[key] for key in ('source_fur_dye','source_special_vfx','source_fur_transmission','source_fur_shell_coverage')}
images_before = {item.name:hashlib.sha256(item.packed_file.data).hexdigest()
                 for item in bpy.data.images if item.packed_file is not None}
original_materials = {slot.material.name:slot.material.node_tree.as_pointer()
                      for slot in shell.material_slots if slot.material}
profile = FurAppearance(.15, .3, (1., .3, .15, 1.), 2., 'ALPHA')
appearance = apply_fur_appearance(shell, profile)
expected_controls = {'Fur substrate brightness': profile.substrate_gain,
                     'Ember brightness': profile.ember_gain,
                     'Ember outer-layer fade': profile.outer_fade_power}
for material in bpy.data.materials:
    if not material.use_nodes:
        continue
    for instance in material.node_tree.nodes:
        if instance.type != 'GROUP' or instance.node_tree != tree:
            continue
        for name, expected in expected_controls.items():
            assert abs(instance.inputs[name].default_value-expected)<1e-6, f'Existing instance lost {name}'
        assert np.allclose(instance.inputs['Ember tint'].default_value, profile.ember_tint), 'Existing instance lost Ember tint'
for group_name in (appearance['group'], *(item['group'] for item in appearance['base_materials'])):
    layout_shader_node_tree(bpy.data.node_groups[group_name])
assert source_before == {key:tree[key] for key in source_before}, 'Artist controls changed source metadata'
assert images_before == {item.name:hashlib.sha256(item.packed_file.data).hexdigest()
                        for item in bpy.data.images if item.packed_file is not None}, 'Artist controls changed source images'
assert geometry_before == source_geometry_digest((shell.name, receipt['source_mesh'])), 'Artist controls changed geometry or binding'
for name, pointer in original_materials.items():
    assert bpy.data.materials[name].node_tree.as_pointer()==pointer, 'Original material tree was replaced'
verify_source_fur_shell(receipt)
verify_fur_appearance(shell)
try:
    apply_fur_appearance(shell, profile)
except ValueError as exc:
    assert 'unstyled' in str(exc), str(exc)
else:
    raise AssertionError('Duplicate appearance should fail before modifying the graph')
visible = {s.material for o in bpy.data.objects if o.type=='MESH' and not o.hide_render for s in o.material_slots if s.material}
assert not any(m.get('endfield_simple_material') or m.name.startswith('EF Simple /') for m in visible)
scene=bpy.context.scene
scene.render.resolution_x=768
scene.render.resolution_y=768
scene.render.engine='BLENDER_EEVEE_NEXT'
scene.render.use_compositing=False
scene.frame_set(6)
bpy.context.view_layer.update()
rig=next(mod.object for mod in shell.modifiers if mod.type=='ARMATURE')
original_location=rig.location.copy()
initial=positions(shell)
world_before=rig.matrix_world.translation.copy()
rig.location+=Vector((.13,-.07,.11))
bpy.context.view_layer.update()
expected=np.asarray(rig.matrix_world.translation-world_before)
root_error=float(np.max(np.abs(positions(shell)-initial-expected)))
assert root_error < 1e-5, root_error
rig.location=original_location
bpy.context.view_layer.update()
pixels=[]
for frame in (6,66):
    scene.frame_set(frame)
    path=args.output/f'ember_frame_{frame:03}.png'
    scene.render.filepath=str(path)
    bpy.ops.render.render(write_still=True)
    image=bpy.data.images.load(str(path),check_existing=False)
    values=np.empty(len(image.pixels),dtype=np.float32)
    image.pixels.foreach_get(values)
    pixels.append(values.reshape(-1,4)[:,:3])
    bpy.data.images.remove(image)
delta=np.abs(pixels[1]-pixels[0])
assert float(delta.max()) > 1./255., 'Embers no longer animate'
scene.frame_set(6)
scene['fur_appearance_scope']=appearance['scope']
scene['fur_material_mode']='full'
bpy.context.preferences.filepaths.save_version=0
output=args.output/'艾尔黛拉_完整材质_炭绒暗燃.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(output),compress=False)
shell_name=shell.name
bpy.ops.wm.open_mainfile(filepath=str(output),load_ui=False,use_scripts=False)
verify_source_fur_shell(receipt)
reopened=verify_fur_appearance(bpy.data.objects[shell_name])
scene=bpy.context.scene
scene.render.engine='CYCLES'
scene.cycles.samples=16
scene.cycles.use_denoising=True
scene.cycles.transparent_max_bounces=48
scene.render.filepath=str(args.output/'ember_cycles.png')
bpy.ops.render.render(write_still=True)
scene['fur_viewing_note']='Use Cycles rendered shading or F12 for ember_cycles.png; Material Preview uses EEVEE.'
bpy.ops.wm.save_as_mainfile(filepath=str(output),compress=False)
report={'output':str(output),'material_mode':'full','appearance':appearance,'reopened':reopened,
        'source_metadata_unchanged':True,'source_textures_unchanged':True,'geometry_and_binding_unchanged':True,
        'root_follow_max_error_m':root_error,'time_max_rgb_delta':float(delta.max()),
        'rendered_engines':['EEVEE','Cycles'],'saved_engine':'CYCLES',
        'reference_use':'Structural comparison only; no supplied model, textures or nodes imported.'}
(args.output/'receipt.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print('FUR_ARTIST_APPEARANCE_SMOKE_PASSED',str(output),flush=True)
