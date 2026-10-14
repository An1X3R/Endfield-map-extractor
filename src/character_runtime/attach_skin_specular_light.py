"""Select the compiled SpecularOnly response for an explicitly bound host light."""
from __future__ import annotations

from dataclasses import dataclass
import math

import bpy

from character_node_graph import node


@dataclass(frozen=True, slots=True)
class SpecularLightParameters:
    roughness_threshold: float
    alpha_narrowing: float
    metallic_gate: float


def attach_skin_specular_light(
    tree: bpy.types.ShaderNodeTree, light: bpy.types.Object,
    kernel: bpy.types.ShaderNodeTree, parameters: SpecularLightParameters,
) -> dict[str, str | float]:
    """Replace one plain response while preserving its existing live light inputs."""
    if tree.get('source_shader') != 'HGRP/CharacterNPR_Skin' or kernel.get('source_mode') != 2:
        raise ValueError(f'SpecularOnly attachment requires Skin and the mode-2 kernel: {tree.name}/{kernel.name}')
    values = (parameters.roughness_threshold, parameters.alpha_narrowing, parameters.metallic_gate)
    if not all(math.isfinite(v) for v in values) or not 0. <= parameters.alpha_narrowing <= 1.:
        raise ValueError(f'SpecularOnly parameters exceed the verified domain: {parameters}')
    matches = tuple(n for n in tree.nodes if n.type == 'GROUP' and n.label == 'Plain Skin response ' + light.name)
    surfaces = tuple(n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED')
    if len(matches) != 1 or len(surfaces) != 1:
        raise ValueError(f'SpecularOnly requires one bound plain response and source surface: {tree.name}/{light.name}')
    original = matches[0]
    targets = tuple(link.to_socket for link in original.outputs['Contribution'].links)
    if not targets:
        raise ValueError(f'Bound light has no output consumers: {tree.name}/{light.name}')
    instance = node(tree, 'ShaderNodeGroup', 'SpecularOnly Skin response ' + light.name, 'Skin Additional Lights')
    instance.node_tree = kernel
    for socket in instance.inputs:
        if socket.name not in original.inputs:
            continue
        source = original.inputs[socket.name]
        if len(source.links) > 1:
            raise ValueError(f'Ambiguous source light input: {tree.name}/{socket.name}')
        if source.is_linked:
            tree.links.new(source.links[0].from_socket, socket)
        else:
            socket.default_value = source.default_value
    metallic = surfaces[0].inputs['Metallic']
    if len(metallic.links) != 1:
        raise ValueError(f'SpecularOnly needs the evaluated source metallic input: {tree.name}')
    tree.links.new(metallic.links[0].from_socket, instance.inputs['Metallic'])
    for label, value in (('Roughness Threshold', parameters.roughness_threshold), ('Alpha Narrowing', parameters.alpha_narrowing), ('Metallic Gate', parameters.metallic_gate)):
        instance.inputs[label].default_value = value
    for target in targets:
        tree.links.new(instance.outputs['Contribution'], target)
    tree.nodes.remove(original)
    instance['parameter_provenance'] = 'Explicit editable preview mode-2 parameters; no native light record or runtime values captured.'
    return {'group': tree.name, 'light': light.name, 'mode': 2., 'roughness_threshold': parameters.roughness_threshold, 'alpha_narrowing': parameters.alpha_narrowing, 'metallic_gate': parameters.metallic_gate}
