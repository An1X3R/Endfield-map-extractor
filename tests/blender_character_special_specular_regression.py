"""Exercise isotropic special-material lighting on a real exported Si character."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy

sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
                str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'),
                str(Path(__file__).resolve().parent)]

from blender_scene_materials import layout_shader_node_tree
from character_late_materials import supported_standard_reason, _reachable_labels
from character_standard_lighting import attach_standard_main_light, LABEL
from character_standard_additional_lights import (
    attach_standard_default_lights, StandardAdditionalSettings,
)
from patch_character_hgrp import by_label, source_keywords
from blender_character_shading_test_helpers import active_labels, materials_for_source, refresh_materials


def main(blend: Path) -> None:
    """Open the supplied real sample, attach live specular, and leave it unsaved."""
    bpy.ops.wm.open_mainfile(filepath=str(blend.resolve(strict=True)), use_scripts=False)
    names = ('EF Character M_actor_jsspsi_cloth_01', 'EF Character M_actor_jsspsi_cloth_03')
    ordinary = bpy.data.node_groups['EF Character M_actor_jsspsi_cloth_02']
    ordinary_nodes = tuple(node.as_pointer() for node in ordinary.nodes)
    existing = by_label(ordinary, 'Compiled Standard mode0 EF Fill Point')
    kernel = existing.node_tree
    states = tuple(by_label(ordinary, 'Live additional Standard state ' + name).node_tree
                   for name in ('EF Fill Point', 'EF Fill Spot'))
    settings = tuple(StandardAdditionalSettings(.5, 1., 1., 2., 'Real Si host-light regression')
                     for _ in states)
    for name in names:
        tree = bpy.data.node_groups[name]
        assert supported_standard_reason(tree) is None, name
        before = tuple(str(tree[key]) for key in ('source_identity', 'source_floats', 'source_colors'))
        keywords = source_keywords(tree)
        assert {'_CLEARCOAT', '_ANISOTROPY_SPECULAR_ON'} <= set(keywords), name
        surface = by_label(tree, 'CharacterNPR shared Eevee / Cycles surface')
        channels = tuple(tuple(link.from_socket.as_pointer() for link in surface.inputs[key].links)
                         for key in ('Base Color', 'Metallic', 'Roughness', 'Alpha', 'Normal'))
        camera = tree.nodes.new('ShaderNodeVectorTransform')
        camera.vector_type = 'VECTOR'
        camera.convert_from = 'CAMERA'
        camera.convert_to = 'WORLD'
        camera.inputs[0].default_value = (0., 0., 1.)
        record = attach_standard_main_light(tree, camera.outputs[0], (0., 0., 1.), 1., layout_shader_node_tree)
        attach_standard_default_lights(tree, kernel, states, settings, layout_shader_node_tree)
        assert record['lighting_fidelity'] == 'host_isotropic_approximation'
        assert record['unimplemented_specular_features'] == ['_ClearCoat', '_UseAnisotropy']
        main_node = by_label(tree, LABEL)
        assert main_node['lighting_fidelity'] == record['lighting_fidelity']
        assert json.loads(main_node['unimplemented_specular_features']) == record['unimplemented_specular_features']
        assert before == tuple(str(tree[key]) for key in ('source_identity', 'source_floats', 'source_colors'))
        assert source_keywords(tree) == keywords
        assert channels == tuple(tuple(link.from_socket.as_pointer() for link in surface.inputs[key].links)
                                 for key in ('Base Color', 'Metallic', 'Roughness', 'Alpha', 'Normal'))
        reachable = _reachable_labels(by_label(tree, 'Artist-lit NPR diffuse - shared engines').inputs['Color'])
        assert {LABEL, 'Compiled Standard mode0 EF Fill Point', 'Compiled Standard mode0 EF Fill Spot'} <= reachable
        assert tree['standard_additional_lights_connected'] == 2
        print('SPECIAL_SPECULAR_CONNECTED', name, json.dumps(record, sort_keys=True))
    assert ordinary_nodes == tuple(node.as_pointer() for node in ordinary.nodes)
    materials = tuple(material for name in names for material in materials_for_source(name))
    assert all('NPR bus plus ordinary Standard GGX' not in active_labels(material) for material in materials)
    refresh_materials(materials)
    required = {'NPR bus plus ordinary Standard GGX', 'Compiled Standard ordinary specular',
                'Compiled Standard mode0 EF Fill Point', 'Compiled Standard mode0 EF Fill Spot'}
    for material in materials:
        assert required <= active_labels(material), material.name
    print('real Si special-specular Material Output regression passed; source blend was not saved')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blend', type=Path, required=True)
    arguments = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    main(arguments.blend)
