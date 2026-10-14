"""Verify both Hair normal branches against real extracted source textures."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'src' / 'character_runtime'))

from blender_scene_materials import layout_shader_node_tree
from character_npr_nodes import ArtistLighting, make_artist_lighting
from rebuild_character_materials import load_images, make_environment, material_group, read_packet


def upstream_nodes(socket: bpy.types.NodeSocket) -> tuple[bpy.types.Node, ...]:
    """Follow the actual connected graph rather than matching unrelated nodes."""
    pending = [link.from_node for link in socket.links]
    visited: dict[str, bpy.types.Node] = {}
    while pending:
        current = pending.pop()
        if current.name in visited:
            continue
        visited[current.name] = current
        pending.extend(link.from_node for entry in current.inputs for link in entry.links)
    return tuple(visited.values())


def main(source_dir: Path, shader_identities: Path, selection: Path,
         renderer_visibility: Path, output: Path) -> None:
    packet = read_packet(source_dir, shader_identities, selection, renderer_visibility)
    hairs = tuple(source for source in packet.materials
                  if source.shader == 'HGRP/CharacterNPR_Hair' and '_NORMALMAP' in source.keywords)
    shared = tuple(source for source in hairs if '_SPECULAR_NORMALMAP' not in source.keywords)
    split = tuple(source for source in hairs if '_SPECULAR_NORMALMAP' in source.keywords)
    if not shared or not split:
        raise ValueError('Real source packet must contain both shared-bump and split-normal Hair materials')
    images = load_images(packet)
    environment = make_environment(layout_shader_node_tree)
    lighting = make_artist_lighting(ArtistLighting((.7, -1., .6), .9, .15, 0., .25, .75), layout_shader_node_tree)
    rows: list[dict[str, object]] = []
    for source, expected_slot in ((shared[0], '_BumpMap'), (split[0], '_SplitNormalMap')):
        tree = material_group(source, packet, environment, images, lighting, layout_shader_node_tree)
        surfaces = tuple(item for item in tree.nodes if item.type == 'BSDF_PRINCIPLED')
        if len(surfaces) != 1 or len(surfaces[0].inputs['Normal'].links) != 1:
            raise AssertionError(f'Expected one connected Hair surface normal: {source.identity}')
        upstream = upstream_nodes(surfaces[0].inputs['Normal'])
        samples = tuple(item for item in upstream if item.type == 'TEX_IMAGE')
        actual_slots = tuple(item['source_slot'] for item in samples)
        if actual_slots != (expected_slot,):
            raise AssertionError(f'Wrong Hair normal texture reaches surface: material={source.identity}, expected={expected_slot}, actual={actual_slots}')
        binding = next(binding for binding in source.textures if binding.name == expected_slot)
        expected_identity = tuple(binding.identity)
        actual_identity = tuple(json.loads(samples[0]['source_identity']))
        if actual_identity != expected_identity or samples[0].image.colorspace_settings.name != 'Non-Color':
            raise AssertionError(f'Hair normal lost source identity or data color space: {source.identity}')
        alpha_multiply = any(item.label == 'Unity R times A' for item in upstream)
        if alpha_multiply != (expected_slot == '_BumpMap'):
            raise AssertionError(f'Wrong Hair normal channel decoding: material={source.identity}, slot={expected_slot}, alpha_multiply={alpha_multiply}')
        rows.append({'material': source.name, 'identity': source.identity, 'normal_texture_slot': expected_slot,
                     'texture_identity': expected_identity, 'red_times_alpha': alpha_multiply})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'status': 'passed', 'real_source_materials': rows}, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'branches': len(rows), 'audit': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-dir', 'shader-identities', 'selection', 'renderer-visibility', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(args.source_dir, args.shader_identities, args.selection, args.renderer_visibility, args.output)
