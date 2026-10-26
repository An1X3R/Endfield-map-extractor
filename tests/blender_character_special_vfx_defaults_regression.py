"""Build real source VFX packets and verify null defaults without rendering."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
from types import MappingProxyType
import sys

import bpy

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from blender_scene_materials import layout_shader_node_tree
from character_npr_nodes import ArtistLighting, make_artist_lighting
from character_fur_appearance import apply_fur_appearance, verify_fur_appearance
from character_fur_backend import CHARCOAL_EMBER_APPEARANCE
from character_special_vfx import (
    KEYWORD, MissingSpecialVfxTextureError, SpecialVfxConstant, SpecialVfxTexture,
    source_from_group, source_from_material, verify_special_vfx,
)
from rebuild_character_materials import (
    SourcePacket, array_value, file_hash, identity, load_images, make_environment, material_group,
    object_value, read_json, read_material_sources, read_packet,
)


def _verify_charcoal_appearance(group: bpy.types.ShaderNodeTree, blend: Path) -> str:
    with bpy.data.libraries.load(str(blend), link=False) as (source, target):
        name, = [name for name in source.objects if 'Expanded Source Fur Shell' in name]
        target.objects = [name]
    shell, = target.objects
    bpy.context.scene.collection.objects.link(shell)
    receipt = json.loads(shell['source_fur_shell'])
    special = json.loads(group['source_special_vfx'])
    shell['source_fur_shell'] = json.dumps({**receipt, 'source_group': group.name,
        'dye': json.loads(group['source_fur_dye']),
        'transmission': json.loads(group['source_fur_transmission']), 'special_vfx': special})
    apply_fur_appearance(shell, CHARCOAL_EMBER_APPEARANCE)
    verify_fur_appearance(shell)
    verify_special_vfx(group, special)
    instance = group.nodes[special['control_instance']]
    multiplier = instance.outputs['Strength'].links[0].to_node
    destinations = tuple(link.to_socket for link in multiplier.outputs[0].links)
    for link in tuple(multiplier.outputs[0].links):
        group.links.remove(link)
    try:
        verify_special_vfx(group, special)
    except AssertionError as error:
        if 'strength control changed' not in str(error):
            raise
    else:
        raise AssertionError('Disconnected control multiplier passed after the artist wrapper')
    finally:
        for destination in destinations:
            group.links.new(multiplier.outputs[0], destination)
    verify_special_vfx(group, special)
    return shell.name


def _material_packet(source_dir: Path, shader_path: Path, material_name: str) -> SourcePacket:
    audit = object_value(read_json(source_dir / 'source_audit.json'), 'source audit')
    shader = object_value(read_json(shader_path), 'shader identities')
    if (audit['format'] != 'EndfieldCharacterSourceAudit/1'
            or shader['format'] != 'EndfieldCharacterShaderIdentity/1'
            or shader['source_audit_sha256'].casefold() != file_hash(source_dir / 'source_audit.json')):
        raise ValueError('Material-only smoke requires identity-matched actual source evidence')
    records = tuple(object_value(row, 'material') for row in array_value(audit['materials'], 'materials')
                    if object_value(object_value(row, 'material')['object'], 'material object')['name'] == material_name)
    if len(records) != 1:
        raise ValueError(f'Material-only smoke requires one actual source material: {material_name}, count={len(records)}')
    visible = frozenset(identity(row['object'], 'material object') for row in records)
    materials, textures = read_material_sources(source_dir, records, shader, visible)
    return SourcePacket(materials, textures, (), (), MappingProxyType({}), MappingProxyType({}), visible)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-dir', 'shader-identities', 'output-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--selection-report', type=Path)
    selection.add_argument('--material-source-name')
    parser.add_argument('--visibility', type=Path)
    parser.add_argument('--sample-kind', choices=('constant', 'image'), required=True)
    parser.add_argument('--appearance-blend', type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    if args.material_source_name is not None:
        packet = _material_packet(args.source_dir, args.shader_identities, args.material_source_name)
    else:
        if args.visibility is None:
            parser.error('--selection-report requires --visibility')
        packet = read_packet(args.source_dir, args.shader_identities, args.selection_report, args.visibility)
    sources = tuple(source for source in packet.materials if KEYWORD in source.keywords)
    if not sources:
        raise AssertionError('Real source packet has no selected special VFX material')
    images = load_images(packet)
    lighting = make_artist_lighting(ArtistLighting((.7, -1., .6), .9, .15, 0., .25, .75), layout_shader_node_tree)
    environment = make_environment(layout_shader_node_tree)
    rows: list[dict[str, object]] = []
    for source in sources:
        bpy.data.texts.new('SOURCE MATERIAL ' + source.name + '.json').write(source.source_json)
        bpy.data.texts.new('SOURCE TYPETREE ' + source.name + '.json').write(source.tree_json)
        group = material_group(source, packet, environment, images, lighting, layout_shader_node_tree, None)
        samples = {str(item['source_slot']): item for item in group.nodes
                   if item.type == 'TEX_IMAGE' and item.get('source_slot')}
        selected = source_from_material(source, samples)
        archived = source_from_group(group)
        if selected is None or archived is None:
            raise AssertionError(f'Archived VFX source is absent: {source.identity}')
        if (archived.identity != selected.identity or archived.floats != selected.floats
                or archived.colors != selected.colors or archived.uv_space != selected.uv_space
                or len(archived.textures) != len(selected.textures)):
            raise AssertionError(f'Archived VFX metadata differs from real packet: {source.identity}')
        for actual, expected in zip((*archived.surface_scale, *archived.surface_offset),
                                    (*selected.surface_scale, *selected.surface_offset), strict=True):
            if abs(actual - expected) > 1e-6:
                raise AssertionError(f'Archived surface UV transform differs: {source.identity}')
        for actual, expected in zip(archived.textures, selected.textures, strict=True):
            if isinstance(actual, SpecialVfxTexture) and isinstance(expected, SpecialVfxTexture):
                if (actual.slot, actual.image, actual.identity, actual.uv_layer, actual.color_space) != (
                        expected.slot, expected.image, expected.identity, expected.uv_layer, expected.color_space):
                    raise AssertionError(f'Archived source texture differs: {source.identity}/{expected.slot}')
                if any(abs(left - right) > 1e-6 for left, right in zip(
                        (*actual.scale, *actual.offset), (*expected.scale, *expected.offset), strict=True)):
                    raise AssertionError(f'Archived source ST differs: {source.identity}/{expected.slot}')
            elif actual != expected:
                raise AssertionError(f'Archived source constant differs: {source.identity}')
        main = selected.textures[1]
        if args.sample_kind == 'constant':
            if not isinstance(main, SpecialVfxConstant) or source.null_texture_defaults.get(main.slot) != 'white':
                raise AssertionError(f'Expected verified null white source main: {source.identity}')
            if any(item.get('special_vfx_source_slot') == main.slot and item.type == 'TEX_IMAGE' for item in group.nodes):
                raise AssertionError('A null default generated a fabricated image sampler')
            for uv_set in (0., 1., 2., 3.):
                source_from_material(replace(source, floats={**source.floats, '_VFXMainUVSet': uv_set}), samples)
        elif not isinstance(main, SpecialVfxTexture):
            raise AssertionError(f'Expected original real source main texture: {source.identity}')
        for uv_set in (-1., .5, 4.):
            try:
                source_from_material(replace(source, floats={**source.floats, '_VFXMainUVSet': uv_set}), samples)
            except ValueError as error:
                if 'outside the source enum' not in str(error):
                    raise
            else:
                raise AssertionError(f'Invalid main UV selector was accepted: {uv_set}')
        for binding in source.textures:
            if binding.name not in ('_VFXSpecialBlendTex', '_VFXSpecialMainTex'):
                continue
            missing = {slot: sample for slot, sample in samples.items() if slot != binding.name}
            try:
                source_from_material(source, missing)
            except MissingSpecialVfxTextureError:
                pass
            else:
                raise AssertionError(f'A missing non-null PPtr was treated as a default: {binding.name}')
        try:
            source_from_material(replace(source, floats={**source.floats, '_VFXBlendUVSet': 1.}), samples)
        except ValueError as error:
            if 'Active source special VFX switches are unsupported' not in str(error):
                raise
        else:
            raise AssertionError('Unsupported nonzero blend UV set was accepted')
        record = json.loads(group['source_special_vfx'])
        host = record['host_inputs']
        if source.floats['_VFXFresnelUseNormalMap'] == 1.:
            if host['normal']['route'] != 'existing_bsdf_decoded_world_normal':
                raise AssertionError('Mapped Fresnel did not reuse the actual BSDF decoded normal')
            missing_normal = replace(source, floats={**source.floats, '_UseBumpMap': 0.},
                keywords=tuple(keyword for keyword in source.keywords if keyword != '_NORMALMAP'))
            try:
                material_group(missing_normal, packet, environment, images, lighting, layout_shader_node_tree, None)
            except ValueError as error:
                if 'requires a connected BSDF normal' not in str(error):
                    raise
            else:
                raise AssertionError('Mapped Fresnel fabricated an absent BSDF normal')
        if source.floats['_VFXMainUVSet'] == 1. and isinstance(main, SpecialVfxTexture):
            if host['main_uv']['route'] != 'source_uv1' or not host['main_uv']['approximation']:
                raise AssertionError('Real UV2 main did not declare its UV1 host approximation')
            uv = group.nodes[host['main_uv']['node']]
            uv.uv_map = 'UV0'
            try:
                verify_special_vfx(group, record)
            except AssertionError as error:
                if 'main source UV1 changed' not in str(error):
                    raise
            else:
                raise AssertionError('UV2 main silently accepted UV0 substitution')
            finally:
                uv.uv_map = 'UV1'
        controls = bpy.data.node_groups[record['control_group']]
        value = controls.nodes[record['control_node']]
        if value.outputs[0].default_value != 1.:
            raise AssertionError('Source strength control does not start at one')
        for strength in (0., 1., 2.):
            value.outputs[0].default_value = strength
            verify_special_vfx(group, record)
        value.outputs[0].default_value = 1.
        appearance_shell = None
        if args.appearance_blend is not None and '_CHARACTER_FUR' in source.keywords:
            appearance_shell = _verify_charcoal_appearance(group, args.appearance_blend)
        bpy.context.scene.frame_set(17)
        bpy.context.view_layer.update()
        verify_special_vfx(group, record)
        group.use_fake_user = True
        rows.append({'group': group.name, 'source': source.identity, 'record': record,
                     'archived_source_matches': True, 'missing_nonnull_rejected': True,
                     'nonzero_blend_uv_rejected': True, 'invalid_main_uv_rejected': True,
                     'charcoal_appearance_shell': appearance_shell})
    args.output_dir.mkdir(parents=True, exist_ok=False)
    bpy.context.preferences.filepaths.save_version = 0
    target = args.output_dir / 'special_vfx_defaults.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(target), check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    for row in rows:
        group = bpy.data.node_groups[row['group']]
        verify_special_vfx(group, row['record'])
        if row['charcoal_appearance_shell'] is not None:
            verify_fur_appearance(bpy.data.objects[row['charcoal_appearance_shell']])
        if source_from_group(group) is None:
            raise AssertionError('Saved archived source no longer selects special VFX')
    result = {'status': 'passed', 'rendered': False, 'sample_kind': args.sample_kind,
              'materials': rows, 'save_reopen': True,
              'scope': 'Actual source material construction; mesh UV availability is checked during production binding.'}
    (args.output_dir / 'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('SPECIAL_VFX_DEFAULTS_REGRESSION_PASSED', len(rows), flush=True)


if __name__ == '__main__':
    main()
