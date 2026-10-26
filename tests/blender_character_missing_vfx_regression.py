"""Verify optional texture omissions using a real production character export."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import bpy

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from character_special_vfx import KEYWORD, MissingSpecialVfxTextureError, source_from_material
from rebuild_character_materials import read_packet


def main(payload: Path) -> None:
    prepared = payload / 'prepared'
    build = payload / 'build'
    receipt = json.loads((build / 'build_receipt.json').read_text(encoding='utf-8'))
    skipped = receipt['skipped_optional_resources']
    if not skipped:
        raise AssertionError('Regression requires an actual missing optional material texture')
    packet = read_packet(prepared / 'static_probe', prepared / 'shader_identity/shader_identity.json',
                         build / 'selection_report.json', prepared / 'renderer_visibility.json')
    bpy.ops.wm.open_mainfile(filepath=str(build / 'character.blend'), use_scripts=False)
    for row in skipped:
        source, = (item for item in packet.materials if item.identity == tuple(row['identity']))
        group, = (tree for tree in bpy.data.node_groups if tree.get('source_identity')
                  and json.loads(tree['source_identity']) == list(source.identity)
                  and tree.get('skipped_optional_resource')
                  and tree.name.startswith('EF Character '))
        output, = (node for node in group.nodes if node.type == 'GROUP_OUTPUT' and node.is_active_output)
        if not output.inputs['Shader'].is_linked or row['retained'] != 'base_surface':
            raise AssertionError(f'Missing effect discarded the base surface: {source.name}')
        samples = {str(node['source_slot']): node for node in group.nodes
                   if node.type == 'TEX_IMAGE' and node.get('source_slot')}
        missing = {slot: sample for slot, sample in samples.items() if slot != '_VFXSpecialBlendTex'}
        try:
            source_from_material(source, missing)
        except MissingSpecialVfxTextureError:
            pass
        else:
            raise AssertionError(f'Missing texture was not reported specifically: {source.name}')
        first, = (binding for binding in source.textures if binding.name == '_VFXSpecialBlendTex')
        malformed = replace(source, textures=(*source.textures, first))
        try:
            source_from_material(malformed, samples)
        except MissingSpecialVfxTextureError as error:
            raise AssertionError('Malformed duplicate binding was mistaken for a missing resource') from error
        except ValueError:
            pass
        else:
            raise AssertionError('Malformed duplicate binding was accepted')
        disabled = replace(source, keywords=tuple(value for value in source.keywords if value != KEYWORD))
        if source_from_material(disabled, samples) is not None:
            raise AssertionError('Disabled optional effect was activated')
    print(json.dumps({'status': 'passed', 'skipped_optional_resources': len(skipped),
                      'base_surfaces_preserved': True, 'malformed_binding_rejected': True}))


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--payload', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--')+1:])
main(args.payload.resolve(strict=True))
