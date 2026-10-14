"""Bind the compiled Skin highlight-map branch to the existing local diffuse."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math

import bpy

from character_node_graph import node, math_node, color_mix
from character_weather_nodes import combine_vector, vector_math
from patch_character_hgrp import by_label, source_keywords


@dataclass(frozen=True)
class FaceHighlightReceipt:
    group: str
    image: str
    offset: tuple[float, float]
    hemisphere_uses_surface_normal: bool


def attach_face_highlight(tree: bpy.types.ShaderNodeTree) -> FaceHighlightReceipt:
    """Preserve source UV and add highlight RGB with the compiled ambient weight."""
    if tree.get('source_shader') != 'HGRP/CharacterNPR_Skin' or not tree.get('sdf_scope'):
        raise ValueError(f'Expected a source-bound Skin SDF group: {tree.name}')
    floats=json.loads(tree['source_floats'])
    if floats['_FaceHighlightMap'] != 1. or '_HIGHLIGHT_MAP' not in source_keywords(tree):
        raise ValueError(f'Source highlight branch is disabled: {tree.name}')
    if tree.get('face_highlight_connected'):
        raise ValueError(f'Face highlight already attached: {tree.name}')
    colors=json.loads(tree['source_colors'])
    values=colors['_HighlightMapVector']
    offset=(float(values[0]),float(values[1]))
    if not all(math.isfinite(value) for value in offset):
        raise ValueError(f'Non-finite source highlight offset: {tree.name}, {offset}')
    samples=tuple(item for item in tree.nodes if item.type=='TEX_IMAGE' and item.get('source_slot')=='_HighlightMap')
    if len(samples)!=1 or samples[0].image is None or samples[0].image.packed_file is None:
        raise ValueError(f'Missing packed source HighlightMap: {tree.name}')
    original=samples[0]
    if len(original.inputs['Vector'].links)!=1:
        raise ValueError(f'Missing explicit source HighlightMap UV: {tree.name}')
    section='HGRP Face Highlight'
    sdf=by_label(tree,'Native Face SDF / Explicit Adjustable Basis')
    surface=sdf.inputs['Source Surface Normal'].links[0].from_socket
    tree.links.new(surface,by_label(tree,'Source geometry hemisphere components').inputs[0])
    geometry=node(tree,'ShaderNodeNewGeometry','Surface-to-camera highlight direction',section)
    view=vector_math(tree,'NORMALIZE',geometry.outputs['Incoming'],(0.,0.,0.),'Normalize face highlight view').outputs[0]
    components=[]
    for axis,scale in zip(('Right','Up'),offset):
        basis=by_label(tree,'Face '+axis+' live anchor basis').outputs[0]
        basis=vector_math(tree,'NORMALIZE',basis,(0.,0.,0.),'Normalize face highlight '+axis).outputs[0]
        amount=vector_math(tree,'DOT_PRODUCT',view,basis,'Source highlight view '+axis).outputs['Value']
        components.append(math_node(tree,'MULTIPLY',amount,scale,'Source highlight offset '+axis,section))
    shift=combine_vector(tree,(components[0],components[1],0.),'Source view-dependent face UV offset')
    uv=vector_math(tree,'ADD',original.inputs['Vector'].links[0].from_socket,shift,'Source face highlight UV').outputs[0]
    sample=node(tree,'ShaderNodeTexImage','Source face highlight camera sampling',section)
    sample.image=original.image;sample.extension=original.extension;sample.interpolation=original.interpolation
    tree.links.new(uv,sample.inputs['Vector'])
    diffuse=by_label(tree,'Verified local HGRP diffuse equations')
    cast=math_node(tree,'ADD',math_node(tree,'MULTIPLY',diffuse.outputs['Cast Blend'],.5,'Face highlight half cast blend',section),.5,'Face highlight cast weight',section)
    ambient=math_node(tree,'MULTIPLY',cast,diffuse.outputs['Ambient Diffuse Intensity'],'Face source ambient highlight intensity',section)
    weight=color_mix(tree,'MULTIPLY',1.,diffuse.outputs['Full Diffuse'],ambient,'Face source full diffuse highlight weight',section)
    highlight=color_mix(tree,'MULTIPLY',1.,weight,sample.outputs['Color'],'Source weighted face highlight',section)
    control=node(tree,'ShaderNodeValue','Face Highlight A-B Weight',section);control.outputs[0].default_value=1.
    highlight=color_mix(tree,'MULTIPLY',1.,highlight,control.outputs[0],'Editable face highlight comparison',section)
    emission=by_label(tree,'Artist-lit NPR diffuse - shared engines')
    if len(emission.inputs['Color'].links)!=1:
        raise ValueError(f'Face source diffuse output is ambiguous: {tree.name}')
    combined=color_mix(tree,'ADD',1.,emission.inputs['Color'].links[0].from_socket,highlight,'Source diffuse plus face HighlightMap',section)
    tree.links.new(combined,emission.inputs['Color'])
    tree['face_highlight_connected']=True
    tree['face_highlight_scope']='Compiled Skin _HIGHLIGHT_MAP camera UV and ambient weighting; explicit face frame; native skin rim, light loops and screen composition remain incomplete'
    return FaceHighlightReceipt(tree.name,sample.image.name,offset,True)
