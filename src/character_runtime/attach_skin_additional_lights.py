"""Bind explicit host light states to the source plain Skin punctual branch."""
from __future__ import annotations
from collections.abc import Sequence
import json
import bpy
from character_node_graph import node,color_mix,math_node
from character_weather_nodes import vector_math
from patch_character_hgrp import by_label


def _linked_input(tree: bpy.types.NodeTree, owner: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
    socket = owner.inputs.get(name)
    if socket is None:
        raise ValueError(f"{tree.name}: compiled Skin input is missing: {owner.label}/{name}")
    links = tuple(socket.links)
    if len(links) != 1:
        raise ValueError(f"{tree.name}: compiled Skin input must have one validated source: {owner.label}/{name}, links={len(links)}")
    return links[0].from_socket


def attach_skin_additional_lights(tree: bpy.types.ShaderNodeTree,kernel: bpy.types.ShaderNodeTree,states: Sequence[bpy.types.ShaderNodeTree]) -> bpy.types.NodeSocket:
    if tree.get('source_shader')!='HGRP/CharacterNPR_Skin':raise ValueError('Additional Skin binding requires a Skin group')
    if tree.get('additional_skin_lights_connected'):raise ValueError('Additional Skin lights are already connected')
    if not states:raise ValueError('At least one explicit additional light state is required')
    section='Skin Additional Lights';floats=json.loads(tree['source_floats'])
    geometry=node(tree,'ShaderNodeNewGeometry','Additional Skin surface geometry',section)
    main=by_label(tree,'Compiled Skin main-light GGX');spec_normal=_linked_input(tree,main,'Normal')
    diffuse=by_label(tree,'Verified local HGRP diffuse equations');shared=by_label(tree,'Explicit HGRP state and preview inputs')
    normal=spec_normal;wrap_normal=normal
    if tree.get('sdf_scope'):
        normal=by_label(tree,'Native Face SDF / Explicit Adjustable Basis').outputs['Blended Surface Normal']
        radial=by_label(tree,'Source radial hemisphere normalized').outputs[0]
        split=node(tree,'ShaderNodeSeparateXYZ','Additional radial components',section);tree.links.new(radial,split.inputs[0])
        flat=node(tree,'ShaderNodeCombineXYZ','Additional radial flat host frame',section)
        tree.links.new(split.outputs['X'],flat.inputs['X']);tree.links.new(split.outputs['Y'],flat.inputs['Y']);flat.inputs['Z'].default_value=2.**-14
        factor=by_label(tree,'Actual SDFMask G surface blend').outputs['Green']
        mixed=color_mix(tree,'MIX',factor,flat.outputs[0],normal,'Source SDF additional wrap blend',section)
        wrap_normal=vector_math(tree,'NORMALIZE',mixed,(0.,0.,0.),'Normalize additional face wrap').outputs[0]
    normalization=math_node(tree,'ADD',.5,math_node(tree,'MULTIPLY',.25,shared.outputs['Cast Shadow'],'Additional cast normalization quarter',section),'Source additional normalization scale',section)
    spec_color=by_label(tree,'Skin source metallic specular color').outputs[0]
    result=None
    for state in states:
        light=node(tree,'ShaderNodeGroup','Live additional state '+state['bound_light'],section);light.node_tree=state
        shading=node(tree,'ShaderNodeGroup','Plain Skin response '+state['bound_light'],section);shading.node_tree=kernel
        bindings={'Position':geometry.outputs['Position'],'Normal':normal,'Wrap Normal':wrap_normal,'Specular Normal':spec_normal,'View':geometry.outputs['Incoming'],'Light Position':light.outputs['Position'],'Light Axis':light.outputs['Toward Axis'],'Light Color':light.outputs['Color'],'NPR Color':diffuse.outputs['NPR Color'],'Specular Color':spec_color,'Global Normalize Scale':normalization}
        for name,socket in bindings.items():tree.links.new(socket,shading.inputs[name])
        for name in ('Energy','Energy Scale','Range','Visibility','Enabled','Is Directional','Is Spot','Spot Size','Spot Blend'):tree.links.new(light.outputs[name],shading.inputs[name])
        for name,value in (('Falloff Exponent',2.),('Wrap',0.),('Color Normalize',1.),('Specular Weight',1.),('Perceptual Roughness',1.-floats['_Smoothness'])):shading.inputs[name].default_value=value
        shading['settings_provenance']='Explicit editable host preview light settings; not captured per-light runtime GPU data.'
        result=shading.outputs['Contribution'] if result is None else color_mix(tree,'ADD',1.,result,shading.outputs['Contribution'],'Sum additional Skin light '+state['bound_light'],section)
    tree['additional_skin_lights_connected']=len(states)
    if result is None:raise AssertionError('No additional light contribution was constructed')
    return result
