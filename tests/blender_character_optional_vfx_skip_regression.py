"""Verify isolated unsupported FX skipping with an actual failed export packet."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
from blender_scene_materials import layout_shader_node_tree
from character_npr_nodes import ArtistLighting, make_artist_lighting
from character_special_vfx import KEYWORD, verify_special_vfx
from rebuild_character_materials import (
    SourcePacket, identity, load_images, make_environment, material_group,
    optional_standalone_vfx_renderer_skips, read_packet,
)


def _reject_skip(packet: SourcePacket, material: tuple[str, int], label: str) -> None:
    materials, _ = optional_standalone_vfx_renderer_skips(packet)
    if material in materials:
        raise AssertionError(f'Optional FX isolation guard accepted {label}: {material}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    prepared = args.payload / 'prepared'
    packet = read_packet(prepared / 'static_probe', prepared / 'shader_identity/shader_identity.json',
                         args.payload / 'build/selection_report.json', prepared / 'renderer_visibility.json')
    materials, renderers = optional_standalone_vfx_renderer_skips(packet)
    candidates = tuple(row for row in renderers.values() if 'particle_input_on_mesh_renderer' in row['unsupported_features'])
    if not candidates:
        raise AssertionError('Actual source packet did not exercise isolated unsupported mesh particle inputs')
    renderer_map = {identity(row['object'], 'renderer'): row for row in packet.renderers}
    guards: list[dict[str, object]] = []
    for candidate in candidates:
        material = tuple(candidate['material_identity'])
        key_text = candidate['renderer_identity']
        cab, path_id = key_text.rsplit(':', 1)
        key = (cab, int(path_id))
        renderer = renderer_map[key]
        peer_key, peer = next((peer_key, row) for peer_key, row in renderer_map.items()
                              if peer_key != key and '_fx_' not in row['path'].casefold())
        if not candidate['unsupported_features']:
            raise AssertionError('Optional FX skip omitted concrete unsupported source features')
        for label, replacement in (
            ('non-FX source path', {**renderer, 'path': renderer['path'].replace('_fx_', '_surface_')}),
            ('multiple submeshes', {**renderer, 'submesh_count': 2}),
            ('multiple materials', {**renderer, 'materials': [*renderer['materials'], *renderer['materials']]}),
        ):
            changed = replace(packet, renderers=tuple(replacement if current == key else row
                              for current, row in renderer_map.items()))
            _reject_skip(changed, material, label)
        for label, replacement in (
            ('shared mesh', {**peer, 'mesh': renderer['mesh']}),
            ('body-shared material', {**peer, 'materials': [*peer['materials'], *renderer['materials']]}),
        ):
            changed = replace(packet, renderers=tuple(replacement if current == peer_key else row
                              for current, row in renderer_map.items()))
            _reject_skip(changed, material, label)
        guards.append({'material': material, 'renderer': key_text, 'isolation_negative_cases': 5})
    special = tuple(source for source in packet.materials if KEYWORD in source.keywords
                    and source.identity in packet.visible_materials)
    if not special or any(source.identity in materials for source in special):
        raise AssertionError('Optional FX skipping discarded or omitted the actual body special VFX')
    images = load_images(packet)
    lighting = make_artist_lighting(ArtistLighting((.7, -1., .6), .9, .15, 0., .25, .75), layout_shader_node_tree)
    environment = make_environment(layout_shader_node_tree)
    active: list[dict[str, object]] = []
    for source in special:
        group = material_group(source, packet, environment, images, lighting, layout_shader_node_tree, None)
        record = json.loads(group['source_special_vfx'])
        verify_special_vfx(group, record)
        active.append({'material': source.identity, 'host_inputs': record['host_inputs']})
    result = {'status': 'passed', 'rendered': False, 'skipped_materials': list(materials.values()),
              'skipped_renderers': list(renderers.values()), 'isolation_guards': guards,
              'body_special_vfx_built_and_connected': active,
              'scope': 'Actual packet skip classification and body shader construction; full export binding is separate.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('OPTIONAL_VFX_SKIP_REGRESSION_PASSED', len(candidates), flush=True)


if __name__ == '__main__':
    main()
