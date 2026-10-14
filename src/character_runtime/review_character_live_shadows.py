"""Build and reopen source-proxy dynamic-shadow character review scenes."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from attach_character_live_shadows import SHADOW_MODIFIER, attach_live_shadows, make_proxy_collection, select_source_casters
from audit_character_materials import geometry_digest
from character_live_shadow_nodes import MAIN_SHADOW_ATTRIBUTE, CHARACTER_SHADOW_ATTRIBUTE
from character_shadow_proxy_alpha import attach_shadow_proxy_alpha, read_shadow_alpha_evidence
from rebuild_character_materials import render_view
from review_character_skin_lights import refresh


def shadow_fields(names: tuple[str, ...]) -> dict[str, dict[str, tuple[float, ...]]]:
    """Require real evaluated fields on every bound receiver."""
    refresh()
    graph = bpy.context.evaluated_depsgraph_get()
    result = {}
    for name in names:
        obj = bpy.data.objects[name].evaluated_get(graph)
        mesh = obj.to_mesh()
        fields = {}
        for attribute in (MAIN_SHADOW_ATTRIBUTE, CHARACTER_SHADOW_ATTRIBUTE):
            field = mesh.attributes.get(attribute)
            if field is None or len(field.data) != len(mesh.vertices):
                raise AssertionError(f'Missing complete shadow field: {name}/{attribute}')
            values = tuple(item.value for item in field.data)
            if not values or any(v < 0. or v > 1. for v in values):
                raise AssertionError(f'Invalid visibility field: {name}/{attribute}')
            fields[attribute] = values
        result[name] = fields
        obj.to_mesh_clear()
    return result


def field_summary(fields: dict[str, dict[str, tuple[float, ...]]]) -> dict[str, dict[str, int]]:
    return {name: {channel: sum(v < .5 for v in values) for channel, values in channels.items()}
            for name, channels in fields.items()}


def changes(first: dict[str, dict[str, tuple[float, ...]]], second: dict[str, dict[str, tuple[float, ...]]]) -> dict[str, int]:
    return {channel: sum(a != b for name in first for a, b in zip(first[name][channel], second[name][channel], strict=True))
            for channel in (MAIN_SHADOW_ATTRIBUTE, CHARACTER_SHADOW_ATTRIBUTE)}


def main(args: argparse.Namespace) -> None:
    if args.output.exists():
        raise FileExistsError(args.output)
    sys.path.insert(0, str(args.repository_src))
    from blender_scene_materials import layout_shader_node_tree
    bpy.ops.wm.open_mainfile(filepath=str(args.blend), use_scripts=False)
    sources = tuple(o for o in bpy.data.objects if o.type == 'MESH' and 'source_renderer_identity' in o)
    names = tuple(o.name for o in sources)
    transforms = {o.name: (tuple(tuple(r) for r in o.matrix_basis), tuple(tuple(r) for r in o.matrix_parent_inverse), o.parent.name if o.parent else '') for o in sources}
    sun = bpy.data.objects[args.sun]
    selection = select_source_casters(args.inventory, sources)
    casters = make_proxy_collection(sources, selection)
    evidence = read_shadow_alpha_evidence(args.shader_identity, args.alpha_audit)
    alpha_receipts = tuple(attach_shadow_proxy_alpha(c, evidence, 2, 42) for c in casters.objects)
    refresh()
    before = geometry_digest(names)
    main_casters = bpy.data.collections.new('EF Main Environment Casters')
    bpy.context.scene.collection.children.link(main_casters)
    bounds = tuple(o.matrix_world @ Vector(c) for o in sources if not o.hide_render for c in o.bound_box)
    low = Vector(tuple(min(p[a] for p in bounds) for a in range(3)))
    high = Vector(tuple(max(p[a] for p in bounds) for a in range(3)))
    extent = max(high - low)
    center = (low + high) * .5
    close = center.copy()
    close.z = low.z + (high.z - low.z) * .78
    args.output.mkdir(parents=True)
    renders = []
    def render(label: str, engine: str) -> None:
        refresh()
        renders.append(render_view(args.output, label + '.png', engine, Vector((0., -1., .02)), close, extent * .46, 512, 512, 24))
    render('before', 'BLENDER_EEVEE_NEXT')
    receivers = attach_live_shadows(sources, sun, main_casters, casters, args.bias, extent * 8., args.subdivision)
    for tree in bpy.data.node_groups:
        if tree.bl_idname == 'ShaderNodeTree':
            layout_shader_node_tree(tree)
    shadow_tree = bpy.data.node_groups['EF Live Sun Shadow Fields']
    caster_node = next(n for n in shadow_tree.nodes if n.label == 'Character casters')
    caster_node.inputs['Collection'].default_value = main_casters
    neutral = shadow_fields(receivers)
    if any(count for row in field_summary(neutral).values() for count in row.values()):
        raise AssertionError('Empty caster collections did not produce fully visible neutral fields')
    render('neutral_fields_eevee', 'BLENDER_EEVEE_NEXT')
    caster_node.inputs['Collection'].default_value = casters
    baseline = shadow_fields(receivers)
    if sum(row[CHARACTER_SHADOW_ATTRIBUTE] for row in field_summary(baseline).values()) == 0:
        raise AssertionError('Native proxy set produces no character shadow points')
    for engine, label in (('BLENDER_EEVEE_NEXT', 'live_shadow_eevee'), ('CYCLES', 'live_shadow_cycles')):
        render(label, engine)
    bpy.context.scene.render.engine = 'BLENDER_EEVEE_NEXT'
    blend = args.output / 'character_live_shadow.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False)
    if shadow_fields(receivers) != baseline:
        raise AssertionError('Shadow fields changed on script-disabled reopening')
    render('reopened_eevee', 'BLENDER_EEVEE_NEXT')
    casters = bpy.data.collections['EF Source Shadow Casters']
    proxy_sources = tuple(bpy.data.objects[c.constraints['Live source proxy transform'].target.name] for c in casters.objects)
    original_locations = tuple(o.location.copy() for o in proxy_sources)
    for obj in proxy_sources:
        obj.location.x += extent * 2.
    moved = shadow_fields(receivers)
    moved_changes = changes(baseline, moved)
    if moved_changes[MAIN_SHADOW_ATTRIBUTE] or not moved_changes[CHARACTER_SHADOW_ATTRIBUTE]:
        raise AssertionError(f'Character caster movement did not isolate G: {moved_changes}')
    render('caster_moved_eevee', 'BLENDER_EEVEE_NEXT')
    for obj, location in zip(proxy_sources, original_locations, strict=True):
        obj.location = location
    if shadow_fields(receivers) != baseline:
        raise AssertionError('Source caster transforms did not restore shadow fields')
    main_casters = bpy.data.collections['EF Main Environment Casters']
    sun = bpy.data.objects[args.sun]
    axis = (sun.matrix_world.to_3x3() @ Vector((0., 0., 1.))).normalized()
    bpy.ops.mesh.primitive_cube_add(size=1., location=close + axis * extent * .65)
    blocker = bpy.context.object
    blocker.name = 'TEST ONLY Main Shadow Blocker'
    for collection in tuple(blocker.users_collection):
        collection.objects.unlink(blocker)
    main_casters.objects.link(blocker)
    blocker.scale = (extent * .5, extent * .5, extent * .5)
    blocker.hide_render = True
    blocked = shadow_fields(receivers)
    main_changes = changes(baseline, blocked)
    if not main_changes[MAIN_SHADOW_ATTRIBUTE] or main_changes[CHARACTER_SHADOW_ATTRIBUTE]:
        raise AssertionError(f'Environment blocker did not isolate R: {main_changes}')
    for engine, label in (('BLENDER_EEVEE_NEXT', 'main_blocker_eevee'), ('CYCLES', 'main_blocker_cycles')):
        render(label, engine)
    bpy.data.objects.remove(blocker, do_unlink=True)
    if shadow_fields(receivers) != baseline:
        raise AssertionError('Environment blocker removal did not restore fields')
    for name in receivers:
        bpy.data.objects[name].modifiers.remove(bpy.data.objects[name].modifiers[SHADOW_MODIFIER])
    refresh()
    if geometry_digest(names) != before:
        raise AssertionError('Source geometry, skin weights, UV, bind matrices or original modifiers changed')
    after_transforms = {n: (tuple(tuple(r) for r in bpy.data.objects[n].matrix_basis), tuple(tuple(r) for r in bpy.data.objects[n].matrix_parent_inverse), bpy.data.objects[n].parent.name if bpy.data.objects[n].parent else '') for n in names}
    if after_transforms != transforms:
        raise AssertionError('Source local transforms or parent bindings changed')
    receipt = {
        'status': 'passed', 'input': str(args.blend), 'output': str(blend),
        'output_sha256': hashlib.sha256(blend.read_bytes()).hexdigest(),
        'selection': asdict(selection), 'alpha_caster_receipts': alpha_receipts, 'source_renderers': len(names), 'receiver_names': receivers,
        'baseline_shadow_point_counts': field_summary(baseline), 'caster_move_changes': moved_changes,
        'main_blocker_changes': main_changes, 'source_geometry_preserved': True,
        'script_disabled_reopen_equal': True, 'bias_world_units': args.bias, 'subdivision_level': args.subdivision,
        'renders': renders,
        'limits': ['Blender live hard-ray producer, not the native screen-space atlas/PCF renderer.',
                   'Alpha clipping and sqrt-alpha dither use subdivided faces, not native per-pixel shadow-map coverage.',
                   'Standard/Hair use existing receiver equations; exact current Skin R/G consumer remains separate.',
                   'Environment caster collection starts empty; external blockers must be added to that collection.'],
        'production_export_changed': False,
    }
    (args.output / 'verification.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('blend', 'output', 'repository-src', 'inventory', 'shader-identity', 'alpha-audit'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--sun', required=True)
    parser.add_argument('--bias', required=True, type=float)
    parser.add_argument('--subdivision', required=True, type=int)
    main(parser.parse_args(sys.argv[sys.argv.index('--') + 1:]))
