"""Check nested material-group traversal preserves instance context and evidence samplers."""
from __future__ import annotations

from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'))
from character_material_authoring import shader_dependencies, socket_dependencies


def make_leaf_group() -> bpy.types.NodeTree:
    tree = bpy.data.node_groups.new('Dependency Fixture Leaf', 'ShaderNodeTree')
    tree.interface.new_socket(name='Factor', in_out='INPUT', socket_type='NodeSocketFloat')
    tree.interface.new_socket(name='Factor', in_out='OUTPUT', socket_type='NodeSocketFloat')
    entry = tree.nodes.new('NodeGroupInput')
    output = tree.nodes.new('NodeGroupOutput')
    tree.links.new(entry.outputs['Factor'], output.inputs['Factor'])
    return tree


def make_outer_group(leaf: bpy.types.NodeTree) -> tuple[bpy.types.NodeTree, bpy.types.Node]:
    tree = bpy.data.node_groups.new('Dependency Fixture Outer', 'ShaderNodeTree')
    tree.interface.new_socket(name='Factor', in_out='INPUT', socket_type='NodeSocketFloat')
    tree.interface.new_socket(name='Shader', in_out='OUTPUT', socket_type='NodeSocketShader')
    entry = tree.nodes.new('NodeGroupInput')
    nested = tree.nodes.new('ShaderNodeGroup')
    nested.node_tree = leaf
    emission = tree.nodes.new('ShaderNodeEmission')
    output = tree.nodes.new('NodeGroupOutput')
    tree.links.new(entry.outputs['Factor'], nested.inputs['Factor'])
    tree.links.new(nested.outputs['Factor'], emission.inputs['Strength'])
    tree.links.new(emission.outputs['Emission'], output.inputs['Shader'])
    return tree, nested


def make_material(outer: bpy.types.NodeTree) -> tuple[bpy.types.Material, bpy.types.Node, bpy.types.Node,
                                                       bpy.types.Node]:
    material = bpy.data.materials.new('Dependency Fixture Material')
    material.use_nodes = True
    tree = material.node_tree
    for node in tuple(tree.nodes):
        tree.nodes.remove(node)
    branch_a = tree.nodes.new('ShaderNodeGroup')
    branch_a.name = 'Outer Instance A'
    branch_a.node_tree = outer
    branch_a.inputs['Factor'].default_value = 0.25
    branch_b = tree.nodes.new('ShaderNodeGroup')
    branch_b.name = 'Outer Instance B'
    branch_b.node_tree = outer
    branch_b.inputs['Factor'].default_value = 0.75
    add = tree.nodes.new('ShaderNodeAddShader')
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    tree.links.new(branch_a.outputs['Shader'], add.inputs[0])
    tree.links.new(branch_b.outputs['Shader'], add.inputs[1])
    tree.links.new(add.outputs['Shader'], output.inputs['Surface'])
    image = bpy.data.images.new('Dependency Fixture Retained Image', width=1, height=1)
    sampler = tree.nodes.new('ShaderNodeTexImage')
    sampler.image = image
    sampler['endfield_authoring_texture'] = True
    return material, branch_a, branch_b, sampler


def main() -> None:
    leaf = make_leaf_group()
    outer, nested = make_outer_group(leaf)
    material, branch_a, branch_b, sampler = make_material(outer)
    tree = material.node_tree
    input_a = branch_a.inputs['Factor'].as_pointer()
    input_b = branch_b.inputs['Factor'].as_pointer()
    dependencies_a = socket_dependencies((branch_a.outputs['Shader'],))
    dependencies_b = socket_dependencies((branch_b.outputs['Shader'],))
    if input_a not in dependencies_a.input_sockets or input_b in dependencies_a.input_sockets:
        raise AssertionError('Nested group traversal conflated the first outer instance input')
    if input_b not in dependencies_b.input_sockets or input_a in dependencies_b.input_sockets:
        raise AssertionError('Nested group traversal conflated the second outer instance input')
    dependencies = shader_dependencies(material)
    if not {input_a, input_b}.issubset(dependencies.input_sockets):
        raise AssertionError('The final shader omitted one nested group instance input')
    if ((tree.as_pointer(), branch_a.as_pointer()) not in dependencies.nodes
            or (tree.as_pointer(), branch_b.as_pointer()) not in dependencies.nodes
            or (outer.as_pointer(), nested.as_pointer()) not in dependencies.nodes):
        raise AssertionError('The final shader omitted a nested group instance')
    if ((tree.as_pointer(), sampler.as_pointer()) in dependencies.nodes
            or any(socket.as_pointer() in dependencies.output_sockets for socket in sampler.outputs)
            or sampler.inputs[0].as_pointer() in dependencies.input_sockets):
        raise AssertionError('A retained disconnected source sampler became active')
    print('CHARACTER_MATERIAL_DEPENDENCIES_REGRESSION_PASSED', flush=True)


if __name__ == '__main__':
    main()
