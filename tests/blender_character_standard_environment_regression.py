"""Attach shared optional environment lighting to real saved Si material graphs."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import bpy

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'),
                str(Path(__file__).resolve().parent)]

from blender_scene_materials import layout_shader_node_tree
from character_late_materials import _reachable_labels
from character_standard_environment import (
    LABEL, attach_standard_environment_specular, create_standard_environment_controls,
    validate_standard_environment_controls,
)
from patch_character_hgrp import by_label
from blender_character_shading_test_helpers import active_labels, materials_for_source, refresh_materials


def main(blend: Path) -> None:
    """Check real graph integration and parameter preservation without rendering."""
    bpy.ops.wm.open_mainfile(filepath=str(blend.resolve(strict=True)), use_scripts=False)
    count = len(bpy.data.node_groups)
    for radiance in ((float('nan'), 0., 0.), (-1., 0., 0.), (float('inf'), 0., 0.)):
        try:
            create_standard_environment_controls('Invalid environment control', radiance, 1.)
        except ValueError:
            pass
        else:
            raise AssertionError(f'Invalid environment radiance was accepted: {radiance}')
        assert len(bpy.data.node_groups) == count
    controls = create_standard_environment_controls('Shared regression host environment', (.21586, .21586, .21586), 1.)
    names = tuple('EF Character M_actor_jsspsi_cloth_0' + str(index) for index in (1, 2, 3, 4))
    for name in names:
        tree = bpy.data.node_groups[name]
        original = tuple(str(tree[key]) for key in ('source_identity', 'source_floats', 'source_colors'))
        blend_node = by_label(tree, 'Editable native-sampling response / baseline')
        blend_links = tuple((link.from_socket.as_pointer(), link.to_socket.as_pointer())
                            for socket in blend_node.inputs for link in socket.links)
        surface = by_label(tree, 'CharacterNPR shared Eevee / Cycles surface')
        surface_links = tuple((link.from_socket.as_pointer(), link.to_socket.as_pointer())
                              for socket in surface.inputs for link in socket.links)
        emission = by_label(tree, 'Artist-lit NPR diffuse - shared engines')
        prior_labels = _reachable_labels(emission.inputs['Color'])
        report = attach_standard_environment_specular(tree, controls, layout_shader_node_tree)
        assert report['weighted_by_existing_npr_blend']
        assert report['lighting_fidelity'] == 'uniform_host_environment_approximation'
        assert prior_labels | {LABEL} <= _reachable_labels(emission.inputs['Color'])
        env = by_label(tree, LABEL)
        radiance_link = env.inputs['Environment Radiance'].links[0]
        assert radiance_link.from_node.node_tree == controls
        guard_labels = {node.label for node in env.node_tree.nodes if node.type == 'MATH' and node.operation == 'MAXIMUM'}
        assert {'DFG X positive denominator guard', 'DFG Y positive denominator guard',
                'Environment reflectance denominator guard'} <= guard_labels
        assert original == tuple(str(tree[key]) for key in ('source_identity', 'source_floats', 'source_colors'))
        assert blend_links == tuple((link.from_socket.as_pointer(), link.to_socket.as_pointer())
                                    for socket in blend_node.inputs for link in socket.links)
        assert surface_links == tuple((link.from_socket.as_pointer(), link.to_socket.as_pointer())
                                      for socket in surface.inputs for link in socket.links)
        attached_nodes = len(tree.nodes)
        try:
            attach_standard_environment_specular(tree, controls, layout_shader_node_tree)
        except ValueError:
            pass
        else:
            raise AssertionError(f'Duplicate environment attachment accepted: {name}')
        assert len(tree.nodes) == attached_nodes
    materials = tuple(material for name in names for material in materials_for_source(name))
    assert all(LABEL not in active_labels(material) for material in materials)
    refresh_materials(materials)
    for material in materials:
        assert {LABEL, 'NPR bus plus ordinary Standard GGX', 'Compiled Standard ordinary specular'} <= active_labels(material)
    radiance = by_label(controls, 'Uniform environment radiance - linear host setting')
    for level in (0., .21586, .5):
        radiance.outputs[0].default_value = (level, level, level, 1.)
        validate_standard_environment_controls(controls)
        assert all(by_label(bpy.data.node_groups[name], 'Shared uniform host environment controls').node_tree == controls
                   for name in names)
    exposure = by_label(controls, 'Host exposure - explicit setting')
    exposure.outputs[0].default_value = -1.
    try:
        validate_standard_environment_controls(controls)
    except ValueError:
        pass
    else:
        raise AssertionError('Edited negative environment exposure was accepted')
    print('real Si shared environment Material Output regression passed; no render or blend save performed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    arguments = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(arguments.blend)
