"""Validate real source-bound RGB parallax previews and inactive graph preservation."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import bpy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                    str(Path(__file__).resolve().parents[1] / 'src/character_runtime')]
    from character_parallax_preview import CONTRACT, apply_parallax_preview
    from character_material_authoring import active_shader_nodes
    from character_windows_paths import extended_path
    from rebuild_character_materials import read_packet

    bpy.ops.wm.open_mainfile(filepath=extended_path(args.build_dir / 'weapon.blend'), load_ui=False, use_scripts=False)
    packet = read_packet(args.source_root / 'static_probe', args.source_root / 'shader_identity/shader_identity.json',
                         args.build_dir / 'selection_report.json', args.source_root / 'renderer_visibility.json')
    groups = tuple(group for group in bpy.data.node_groups if 'source_parallax_preview' in group)
    if not groups:
        raise AssertionError('Parallax regression requires a real source-enabled material')
    checks: list[str] = []
    for group in groups:
        metadata = json.loads(group['source_parallax_preview'])
        source = next(source for source in packet.materials if list(source.identity) == metadata['source_identity'])
        binding = next(binding for binding in source.textures if binding.name == '_ParallaxTex')
        if (metadata['format'] != CONTRACT or metadata['native_shader_semantics_verified'] is not False
                or metadata['source_march_count'] != source.floats['_ParallaxMarchNum']
                or metadata['source_scale'] != source.floats['_ParallaxScale']
                or metadata['source_color'] != list(source.colors['_ParallaxColor'])
                or metadata['source_texture_scale'] != list(binding.scale)
                or metadata['source_texture_offset'] != list(binding.offset)):
            raise AssertionError('Parallax source parameter receipt changed')
        count = metadata['source_march_count']
        if metadata['host_layer_depths'] != [(index + 0.5) / count for index in range(count)]:
            raise AssertionError('Host parallax layer depths differ from their declared formula')
        if metadata['host_rgb_weights'] != [1.0 / count] * count:
            raise AssertionError('Host parallax layer weights differ from their declared formula')
        if metadata.get('coverage') != 'BaseMap.a * BaseColor.a':
            raise AssertionError('Parallax must retain the source effect coverage mask')
        assigned = {slot.material for obj in bpy.context.scene.objects if obj.type == 'MESH'
                    for slot in obj.material_slots if slot.material is not None}
        affected = tuple(material for material in assigned if material.use_nodes and any(
            item.type == 'GROUP' and item.node_tree == group for item in material.node_tree.nodes))
        if not affected:
            raise AssertionError('Source parallax group has no actual material instance')
        for material in affected:
            active = active_shader_nodes(material)
            samplers = [item for item in material.node_tree.nodes if item.type == 'TEX_IMAGE'
                        and item.get('source_slot') == '_ParallaxTex'
                        and (material.node_tree.as_pointer(), item.as_pointer()) in active]
            if len(samplers) != count or any(item.image is None or item.image.packed_file is None for item in samplers):
                raise AssertionError('Source parallax layer samplers are missing, inactive or unpacked')
            if {tuple(json.loads(item['source_identity'])) for item in samplers} != {binding.identity}:
                raise AssertionError('Parallax layer texture identity changed')
            masks = [item for tree in (material.node_tree, *bpy.data.node_groups)
                     for item in tree.nodes if item.label == 'Parallax source BaseMap alpha times tint alpha'
                     and (tree.as_pointer(), item.as_pointer()) in active]
            if not masks or any(item.operation != 'MULTIPLY' or not item.inputs[0].is_linked for item in masks):
                raise AssertionError('Active parallax material lost its connected source alpha mask')
            instance = next(item for item in material.node_tree.nodes if item.type == 'GROUP' and item.node_tree == group)
            if abs(instance.inputs['Preview Parallax Strength'].default_value - 0.5) > 1e-8:
                raise AssertionError('Host parallax strength is not the declared editable half-strength')
        checks.append('source_metadata_and_packed_active_layers_verified')

        # Derived malformed controls exercise fail-fast gates using the actual source record.
        socket = next(socket for item in group.nodes for socket in item.outputs)
        entry = next(item for item in group.nodes if item.type == 'GROUP_INPUT')
        before = (len(group.nodes), len(group.links), len(group.interface.items_tree))
        disabled = replace(source, floats={**source.floats, '_UseParallax': 0.0},
                           keywords=tuple(keyword for keyword in source.keywords if keyword != '_PARALLAX_MAP'))
        if apply_parallax_preview(group, disabled, {}, socket, entry) != socket:
            raise AssertionError('Inactive parallax changed its source color socket')
        if before != (len(group.nodes), len(group.links), len(group.interface.items_tree)):
            raise AssertionError('Inactive parallax created nodes or interfaces')
        checks.append('disabled_branch_has_no_graph_mutation')
        for field, value in (('_ParallaxUseNormal', 1.0), ('_ParallaxMarchNum', 0.0), ('_ParallaxScale', float('nan'))):
            malformed = replace(source, floats={**source.floats, field: value})
            try:
                apply_parallax_preview(group, malformed, {}, socket, entry)
            except (ValueError, NotImplementedError):
                checks.append('derived_' + field + '_rejected')
            else:
                raise AssertionError(f'Unsupported source parallax control was ignored: {field}')
            if before != (len(group.nodes), len(group.links), len(group.interface.items_tree)):
                raise AssertionError('Rejected source parallax controls mutated the graph')
    result = {'status': 'passed', 'source_materials': len(groups), 'checks': checks,
              'native_shader_equivalence_claimed': False, 'saved_blend_modified': False}
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'checks': len(checks)}))


if __name__ == '__main__':
    main()
