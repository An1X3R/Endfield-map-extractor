"""Verify explicit source depth writers on actual saved character material stacks."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from blender_scene_materials import layout_shader_node_tree
from character_material_authoring import shader_dependencies
from character_npr_vfx import CHARACTER_VFX_SHADER
from rebuild_character_materials import geometry_signature, read_packet, surface_stack


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    blend = args.payload / 'build/character.blend'
    digest = hashlib.sha256(blend.read_bytes()).hexdigest()
    prepared = args.payload / 'prepared'
    packet = read_packet(prepared / 'static_probe', prepared / 'shader_identity/shader_identity.json',
                         args.payload / 'build/selection_report.json', prepared / 'renderer_visibility.json')
    sources = {source.identity: source for source in packet.materials}
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False, load_ui=False)
    objects = tuple(obj for obj in bpy.context.scene.objects if obj.type == 'MESH')
    geometry = geometry_signature(objects)
    originals = {slot.material.as_pointer(): slot.material for obj in objects for slot in obj.material_slots
                 if slot.material is not None and 'source_material_stack' in slot.material}
    replacements: dict[int, bpy.types.Material] = {}
    expected: dict[str, bool] = {}
    rows: list[dict[str, object]] = []
    for pointer, original in originals.items():
        identities = tuple((str(item[0]), int(item[1])) for item in json.loads(original['source_material_stack']))
        stack = tuple(sources[identity] for identity in identities)
        if not any(source.shader == CHARACTER_VFX_SHADER for source in stack):
            continue
        live = shader_dependencies(original)
        pending = [original.node_tree]
        visited: set[int] = set()
        groups: dict[tuple[str, int], bpy.types.ShaderNodeTree] = {}
        while pending:
            tree = pending.pop()
            if tree.as_pointer() in visited:
                continue
            visited.add(tree.as_pointer())
            if 'source_identity' in tree and 'source_shader' in tree:
                cab, path_id = json.loads(tree['source_identity'])
                identity = (str(cab), int(path_id))
                if identity in identities:
                    if identity in groups:
                        raise AssertionError(f'Live source group is ambiguous: {identity}')
                    groups[identity] = tree
            pending.extend(node.node_tree for node in tree.nodes if node.type == 'GROUP'
                           and (tree.as_pointer(), node.as_pointer()) in live.nodes)
        if set(groups) != set(identities):
            raise AssertionError(f'Actual stack has missing live source groups: {original.name}')
        material = surface_stack(stack, groups, layout_shader_node_tree)
        writers = tuple(source.identity for source in stack if source.shader == CHARACTER_VFX_SHADER
                        and source.floats['_ZWrite'] == 1.0)
        nonoverlap = bool(writers) and all(source.floats['_SurfaceType'] == 1.0 for source in stack)
        if material.use_transparency_overlap == nonoverlap:
            raise AssertionError(f'Host overlap disagrees with explicit source depth writers: {material.name}')
        contract = json.loads(material['source_vfx_depth_approximation']) if nonoverlap else None
        if nonoverlap:
            if (contract['source_depth_writers'] != [list(identity) for identity in writers]
                    or contract['native_depth_pass_verified'] is not False):
                raise AssertionError('Depth approximation lost source evidence or overstated native equivalence')
        elif 'source_vfx_depth_approximation' in material:
            raise AssertionError('Source ZWrite=0 material acquired a fabricated depth contract')
        replacements[pointer] = material
        expected[material.name] = nonoverlap
        rows.append({'material': material.name, 'source_depth_writers': writers,
                     'nonoverlap': nonoverlap, 'contract': contract})
    if not expected or not any(expected.values()) or all(expected.values()):
        raise AssertionError('Actual packet must exercise both explicit depth writers and ZWrite=0 stacks')
    for obj in objects:
        for slot in obj.material_slots:
            if slot.material is not None and slot.material.as_pointer() in replacements:
                slot.material = replacements[slot.material.as_pointer()]
    if geometry_signature(objects) != geometry:
        raise AssertionError('Depth policy changed actual geometry or binding counts')
    args.output.mkdir(parents=True, exist_ok=False)
    target = args.output / 'source_depth.blend'
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(target), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    for name, nonoverlap in expected.items():
        material = bpy.data.materials[name]
        if material.use_transparency_overlap == nonoverlap:
            raise AssertionError(f'Saved material lost the source depth policy: {name}')
    if geometry_signature(tuple(obj for obj in bpy.context.scene.objects if obj.type == 'MESH')) != geometry:
        raise AssertionError('Saved source-depth variant changed geometry or binding counts')
    if hashlib.sha256(blend.read_bytes()).hexdigest() != digest:
        raise AssertionError('CPU regression modified the actual input blend')
    (args.output / 'receipt.json').write_text(json.dumps({
        'status': 'passed', 'rendered': False, 'save_reopen': True,
        'input_unchanged': True, 'geometry_unchanged': True,
        'source_pass_presence_assumed': False, 'materials': rows, 'blend': str(target),
    }, indent=2), encoding='utf-8')
    print('CHARACTER_VFX_DEPTH_REGRESSION_PASSED', flush=True)


if __name__ == '__main__':
    main()
