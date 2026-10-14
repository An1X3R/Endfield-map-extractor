"""Current compiled non-advanced Silk main-light addition, separate from albedo.

The kernel follows the matching Standard ForwardLit fragment. Renderer tangent
is reconstructed from UV0 in Blender; native packed vertex tangent, camera-axis
blend, clustered extra lights, rain composite and environment BRDF are not
captured. Explicit host camera/up/CP13 inputs keep those bindings reviewable.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import math

import bpy

from character_node_graph import node, math_node, color_mix
from character_weather_nodes import scalar_mix, vector_math
from patch_character_hgrp import by_label, source_keywords

Scalar = bpy.types.NodeSocket | float
Vector = bpy.types.NodeSocket | tuple[float, float, float]
SECTION = 'Compiled Silk Main Light'


def build_stocking_light(name: str, ramp: bpy.types.Image) -> bpy.types.ShaderNodeTree:
    """Build the mapped source additive Silk term; preserve the existing API."""
    if ramp.packed_file is None or len(ramp.pixels)==0:
        raise ValueError('Silk spec ramp requires packed source pixels')
    def color_builder(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node, ggx_d: bpy.types.NodeSocket,
        nv: bpy.types.NodeSocket, rough4: bpy.types.NodeSocket) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]:
        return _mapped_specular_color(tree, entry, ggx_d, nv, rough4, ramp)
    return _build_stocking_light(name, color_builder)


def build_stocking_light_no_spec_ramp(name: str) -> bpy.types.ShaderNodeTree:
    """Current no-SpecRamp variant uses source F0 without a fabricated image."""
    tree = _build_stocking_light(name, _unmapped_specular_color)
    tree['source_scope'] = 'Current nonadvanced mapped Silk/DiffRamp no-SpecRamp ForwardLit blob493; additive lobe uses F0 directly.'
    return tree


def _source_specular_reflectance(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node) -> bpy.types.NodeSocket:
    f0_scalar = math_node(tree, 'MULTIPLY', .04, entry.outputs['Specular Level'], 'Source dielectric reflectance', SECTION)
    return color_mix(tree, 'MIX', entry.outputs['Metallic'], f0_scalar, entry.outputs['Albedo'], 'Source specular reflectance', SECTION)


def _unmapped_specular_color(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node, ggx_d: bpy.types.NodeSocket,
    nv: bpy.types.NodeSocket, rough4: bpy.types.NodeSocket) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    """Zero lookup coordinates are diagnostics for an absent lookup, not UVs."""
    zero = math_node(tree, 'ADD', 0., 0., 'No SpecRamp lookup coordinates', SECTION)
    return _source_specular_reflectance(tree, entry), zero, zero


def _mapped_specular_color(tree: bpy.types.ShaderNodeTree, entry: bpy.types.Node, ggx_d: bpy.types.NodeSocket,
    nv: bpy.types.NodeSocket, rough4: bpy.types.NodeSocket, ramp: bpy.types.Image) -> tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    v = entry.outputs
    ramp_denominator = math_node(tree, 'MINIMUM', 65504., math_node(tree, 'DIVIDE', 1.,
        math_node(tree, 'ADD', rough4, .0001, 'Source alpha4 epsilon', SECTION), 'Ramp reciprocal alpha4', SECTION), 'Ramp reciprocal cap', SECTION)
    ramp_u = scalar_mix(tree, v['Iridescent Mode'], math_node(tree, 'DIVIDE', ggx_d, ramp_denominator, 'GGX ramp coordinate', SECTION),
        math_node(tree, 'MULTIPLY', nv, nv, 'NV ramp coordinate', SECTION), 'Source spec ramp U')
    ramp_v = math_node(tree, 'MULTIPLY', v['Roughness'], math_node(tree, 'SUBTRACT', 1., v['Metallic'], 'Nonmetal ramp fraction', SECTION), 'Source spec ramp V', SECTION)
    uv = node(tree, 'ShaderNodeCombineXYZ', 'Source spec ramp coordinates', SECTION)
    tree.links.new(ramp_u, uv.inputs[0]); tree.links.new(ramp_v, uv.inputs[1])
    sample = node(tree, 'ShaderNodeTexImage', 'Exact packed source SpecRamp', SECTION)
    sample.image = ramp; sample.extension = 'EXTEND'; sample.interpolation = 'Linear'
    tree.links.new(uv.outputs[0], sample.inputs['Vector'])
    color = color_mix(tree, 'MULTIPLY', 1., _source_specular_reflectance(tree, entry), sample.outputs['Color'], 'Source F0 times ramp', SECTION)
    return color, ramp_u, ramp_v


def _build_stocking_light(name: str, color_builder: Callable[[bpy.types.ShaderNodeTree, bpy.types.Node,
    bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket], tuple[bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]]) -> bpy.types.ShaderNodeTree:
    """Share the proved Silk arithmetic with an explicit color construction."""
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    vectors = ('Normal', 'Tangent', 'View', 'Camera Forward', 'World Up', 'Light Direction')
    colors = ('Albedo', 'Full Diffuse')
    scalars = ('Roughness', 'Metallic', 'Specular Level', 'Base Alpha', 'Wetness',
               'Coverage', 'Aniso Direction', 'Specular Intensity', 'Specular Minimum',
               'Specular Falloff', 'Specular Value', 'Iridescent Mode', 'Cast Shadow',
               'Cast Blend', 'Ambient Diffuse Intensity', 'CP13 W')
    for label in (*vectors, *colors, *scalars):
        tree.interface.new_socket(name=label, in_out='INPUT', socket_type=(
            'NodeSocketVector' if label in vectors else 'NodeSocketColor' if label in colors else 'NodeSocketFloat'))
    for label, kind in (('Silk Specular', 'NodeSocketColor'), ('Specular Color', 'NodeSocketColor'), ('NDF', 'NodeSocketFloat'),
                        ('Ramp U', 'NodeSocketFloat'), ('Ramp V', 'NodeSocketFloat')):
        tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type=kind)
    entry = node(tree, 'NodeGroupInput', 'Explicit compiled Silk inputs', SECTION)
    out = node(tree, 'NodeGroupOutput', 'Source additive Silk outputs', SECTION)
    v = entry.outputs

    def s(op: str, a: Scalar, b: Scalar, label: str) -> bpy.types.NodeSocket:
        return math_node(tree, op, a, b, label, SECTION)

    def vec(op: str, a: bpy.types.NodeSocket, b: Vector, label: str) -> bpy.types.NodeSocket:
        return vector_math(tree, op, a, b, label).outputs[0]

    def dot(a: bpy.types.NodeSocket, b: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        return vector_math(tree, 'DOT_PRODUCT', a, b, label).outputs['Value']

    def unit(a: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        return vec('NORMALIZE', a, (0., 0., 0.), label)

    def scale(a: bpy.types.NodeSocket, factor: Scalar, label: str) -> bpy.types.NodeSocket:
        item = node(tree, 'ShaderNodeVectorMath', label, SECTION)
        item.operation = 'SCALE'; tree.links.new(a, item.inputs[0])
        if isinstance(factor, bpy.types.NodeSocket):
            tree.links.new(factor, item.inputs['Scale'])
        else:
            item.inputs['Scale'].default_value = factor
        return item.outputs[0]

    def sat(a: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        return s('MINIMUM', 1., s('MAXIMUM', 0., a, label+' lower'), label+' upper')

    n = unit(v['Normal'], 'Source surface normal')
    view = unit(v['View'], 'View toward camera')
    light = unit(v['Light Direction'], 'Explicit source light direction')
    up = unit(v['World Up'], 'Source world up in host frame')
    camera = unit(v['Camera Forward'], 'Explicit source camera forward')
    camera_y = dot(camera, up, 'Source camera Y')
    light_y = dot(light, up, 'Source light Y')
    mixed_y = scalar_mix(tree, v['Cast Shadow'], .5, light_y, 'Source half basis Y')
    half_basis = unit(vec('ADD', camera, scale(up, s('SUBTRACT',mixed_y,camera_y,'Half basis correction'), 'Half basis up correction'), 'Source camera half basis'), 'Normalize source camera half basis')
    half = unit(vec('ADD', vec('ADD',scale(light,v['Cast Shadow'],'Cast light'), scale(half_basis,2.,'Double camera basis'),'Source light plus camera'), scale(view,s('ADD',2.,v['Cast Shadow'],'View half weight'),'Weighted view'),'Source half vector'), 'Normalize source half vector')
    nv = sat(dot(n,view,'Source normal view'), 'Source NV')
    rough2 = s('MAXIMUM', .0078125, s('MULTIPLY',v['Roughness'],v['Roughness'],'Source roughness squared'), 'Source alpha2')
    rough4 = s('MULTIPLY',rough2,rough2,'Source alpha4')
    nh = dot(n,half,'Source normal half')
    denominator = s('ADD',1.,s('MULTIPLY',s('SUBTRACT',rough4,1.,'GGX alpha4 minus one'),s('MULTIPLY',nh,nh,'GGX NH squared'),'GGX denominator delta'),'GGX denominator')
    denominator2 = s('MULTIPLY',denominator,denominator,'GGX denominator squared')
    # Equal numerator/denominator naturally gives one, matching the source branch.
    ggx_d = s('DIVIDE',rough4,denominator2,'Source raw GGX D')
    spec_ramp, ramp_u, ramp_v = color_builder(tree, entry, ggx_d, nv, rough4)

    tangent = unit(vec('SUBTRACT',v['Tangent'],scale(n,dot(v['Tangent'],n,'Tangent normal projection'),'Tangent projected normal'),'Source tangent orthogonalized'),'Normalize source tangent')
    bitangent = vec('CROSS_PRODUCT',n,tangent,'Source bitangent - sign cancels in squared NDF')
    shifted_half = unit(vec('ADD',half,scale(view,v['Specular Value'],'Source view highlight shift'),'Source shifted half'),'Normalize Silk shifted half')
    alpha_weight = sat(s('MULTIPLY',v['Base Alpha'],.5,'Source half alpha'),'Half alpha')
    direction = s('MULTIPLY',-1.,scalar_mix(tree,alpha_weight,v['Aniso Direction'],.5,'Source nonadvanced direction'),'Source nonadvanced aniso sign')
    aniso = s('MULTIPLY',direction,s('SUBTRACT',1.,sat(s('MULTIPLY',v['Coverage'],v['Specular Falloff'],'Coverage falloff'),'Coverage falloff saturate'),'Coverage complement'),'Source split anisotropy')
    rt = s('MULTIPLY',s('SUBTRACT',1.,aniso,'Source rT direction'),rough2,'Source rT')
    rb = s('MULTIPLY',s('ADD',1.,aniso,'Source rB direction'),rough2,'Source rB')
    rtb = s('MULTIPLY',rt,rb,'Source rTB')
    th = s('MULTIPLY',rb,dot(tangent,shifted_half,'T dot shifted half'),'Source anisotropic T')
    bh = s('MULTIPLY',rt,dot(bitangent,shifted_half,'B dot shifted half'),'Source anisotropic B')
    normal_h = s('MULTIPLY',rtb,dot(n,shifted_half,'N dot shifted half'),'Source anisotropic N')
    d = s('ADD',s('ADD',s('MULTIPLY',th,th,'T squared'),s('MULTIPLY',bh,bh,'B squared'),'T plus B squared'),s('MULTIPLY',normal_h,normal_h,'N squared'),'Source anisotropic denominator')
    d2 = s('MULTIPLY',d,d,'Source anisotropic denominator squared')
    numerator = s('MULTIPLY',s('MULTIPLY',rtb,rtb,'rTB squared'),rtb,'rTB cubed')
    equal = node(tree,'ShaderNodeMath','Source exact NDF equality branch',SECTION)
    equal.operation='COMPARE'; equal.inputs[2].default_value=0.
    tree.links.new(numerator,equal.inputs[0]); tree.links.new(d2,equal.inputs[1])
    raw_ndf = scalar_mix(tree,equal.outputs[0],s('DIVIDE',numerator,d2,'Compiled Silk NDF ratio'),1.,'Source Silk NDF branch')
    ndf = s('MINIMUM',20.,s('MAXIMUM',0.,raw_ndf,'Silk NDF lower'),'Silk NDF cap')
    wet_intensity = s('MULTIPLY',v['Specular Intensity'],scalar_mix(tree,v['Wetness'],v['Specular Minimum'],1.,'Source wet spec intensity'),'Source Silk intensity')
    weight = s('MULTIPLY',s('MULTIPLY',wet_intensity,ndf,'Source Silk weighted NDF'),s('MULTIPLY',v['Ambient Diffuse Intensity'],s('ADD',s('MULTIPLY',v['Cast Blend'],.5,'Spec cast half'),.5,'Spec cast offset'),'Source spec ambient intensity'),'Source Silk spec weight')
    weight = s('MULTIPLY',weight,v['CP13 W'],'Source global specular multiplier')
    result = color_mix(tree,'MULTIPLY',1.,color_mix(tree,'MULTIPLY',1.,spec_ramp,v['Full Diffuse'],'Source Silk lighting color',SECTION),weight,'Compiled additive Silk specular',SECTION)
    for label,value in (('Silk Specular',result),('Specular Color',spec_ramp),('NDF',ndf),('Ramp U',ramp_u),('Ramp V',ramp_v)):
        tree.links.new(value,out.inputs[label])
    tree['source_scope'] = 'Standard exact nonadvanced Silk fragment; dry main-light addition only'
    return tree


def _number(values: dict[str, object], name: str) -> float:
    value = values[name]
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError(f'Silk source field must be finite numeric: {name}')
    return float(value)


def _validate_source(tree: bpy.types.ShaderNodeTree) -> dict[str, object]:
    if tree.get('source_shader') != 'HGRP/CharacterNPR':
        raise ValueError(f'Silk source shader is unsupported: {tree.name}')
    values = json.loads(tree['source_floats'])
    expected = {'_SilkStockings':True,'_UseBumpMap':True,'_UseMetallicGlossMap':True,'_SilkStockingsAdvance':False,'_UseCharacterFur':False,'_ClearCoat':False,'_Pantyhose':False,'_UseAnisotropy':False}
    for name, enabled in expected.items():
        if (_number(values,name)>.5) != enabled:
            raise ValueError(f'Unsupported compiled Silk material feature: {name}={values[name]}')
    keywords = set(source_keywords(tree))
    ramped = _number(values, '_UseSpecRampMap') > .5
    if ramped != ('_SPEC_RAMP_ON' in keywords):
        raise ValueError('Silk source SpecRamp switch and keyword disagree')
    required = {'_SILK_STOCKINGS','_NORMALMAP','_METALLICSPECGLOSSMAP'}
    if not ramped:
        required.add('_DIFF_RAMP_ON')
    if not required.issubset(keywords):
        raise ValueError(f'Compiled Silk keyword proof incomplete: {sorted(keywords)}')
    if keywords & {'_CHARACTER_FUR','_CLEARCOAT','_PANTYHOSE','_ANISOTROPY_SPECULAR_ON'}:
        raise ValueError(f'Silk special branch is outside the proved scope: {sorted(keywords)}')
    return values


def correct_stocking_view(tree: bpy.types.ShaderNodeTree, layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Migrate the proved old Incoming sign error; preserve source color/alpha."""
    _validate_source(tree)
    silk = by_label(tree,'Fixed Ruri SilkStockingsSurface')
    item = by_label(tree,'View toward camera')
    if item.type != 'VECT_MATH' or item.operation != 'SCALE' or len(silk.inputs['View'].links)!=1 or silk.inputs['View'].links[0].from_node != item:
        raise ValueError(f'Silk legacy view graph does not match proven migration: {tree.name}')
    if len(item.inputs[0].links)!=1 or item.inputs[0].links[0].from_socket.name!='Incoming':
        raise ValueError(f'Silk view does not originate from Geometry Incoming: {tree.name}')
    scale = item.inputs['Scale']
    if scale.is_linked or scale.default_value not in (-1.,1.):
        raise ValueError(f'Silk legacy view scale must be explicit +/-1: {tree.name}')
    old = scale.default_value; scale.default_value = 1.
    item['source_view_semantics'] = 'Geometry Incoming points toward camera; source V is toward camera'
    layout(tree)
    return {'source_identity':tree['source_identity'],'status':'unchanged' if old==1. else 'corrected','previous_scale':old,'scale':1.,'albedo_parameters_unchanged':True,'alpha_unchanged':True}


def attach_stocking_lighting(tree: bpy.types.ShaderNodeTree, camera_forward: Vector, source_world_up: Vector, cp13_w: Scalar, layout: Callable[[bpy.types.NodeTree], None]) -> dict[str, object]:
    """Attach only the additive source lobe to the existing NPR emission bus."""
    values = _validate_source(tree)
    if any(item.label=='Compiled Silk main-light contribution' for item in tree.nodes):
        raise ValueError(f'Silk lighting already attached: {tree.name}')
    silk = by_label(tree,'Fixed Ruri SilkStockingsSurface')
    bsdf = by_label(tree,'CharacterNPR shared Eevee / Cycles surface')
    diffuse = by_label(tree,'Verified local HGRP diffuse equations')
    emission = by_label(tree,'Artist-lit NPR diffuse - shared engines')
    ramp_nodes = [item for item in tree.nodes if item.type=='TEX_IMAGE' and item.label.startswith('_SpecRampMap |')]
    ramped = _number(values, '_UseSpecRampMap') > .5
    if ramped and (len(ramp_nodes)!=1 or ramp_nodes[0].image is None):
        raise ValueError(f'Exact source SpecRamp image is absent or ambiguous: {tree.name}')
    kernel = build_stocking_light('EF Compiled Silk Main Light',ramp_nodes[0].image) if ramped else build_stocking_light_no_spec_ramp('EF Compiled Silk Main Light - No SpecRamp')
    layout(kernel)
    group = node(tree,'ShaderNodeGroup','Compiled Silk main-light contribution',SECTION); group.node_tree=kernel
    geometry = node(tree,'ShaderNodeNewGeometry','Silk source world geometry',SECTION)
    tangent = node(tree,'ShaderNodeTangent','UV0 reconstructed source tangent',SECTION); tangent.direction_type='UV_MAP'; tangent.uv_map='UV0'
    def from_input(item: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
        links = item.inputs[name].links
        if len(links)!=1:
            raise ValueError(f'Silk required live input must have exactly one source: {tree.name}/{item.label}/{name}')
        return links[0].from_socket
    bindings = {'Normal':from_input(silk,'Normal'),'View':geometry.outputs['Incoming'],'Tangent':tangent.outputs['Tangent'],
                'Albedo':from_input(bsdf,'Base Color'),'Roughness':from_input(bsdf,'Roughness'),'Metallic':from_input(bsdf,'Metallic'),
                'Specular Level':by_label(tree,'R metallic / G specular / B AO / A smoothness').outputs['Green'],
                'Base Alpha':from_input(silk,'Base Alpha'),'Wetness':from_input(silk,'Wetness'),
                'Coverage':silk.outputs['Coverage - Not Surface Alpha'],
                'Full Diffuse':diffuse.outputs['Full Diffuse'],'Cast Blend':diffuse.outputs['Cast Blend'],
                'Ambient Diffuse Intensity':diffuse.outputs['Ambient Diffuse Intensity'],
                'Cast Shadow':from_input(diffuse,'Cast Shadow'),
                'Light Direction':by_label(tree,'Normalize artist light direction').outputs[0]}
    base_color = json.loads(tree['source_colors'])['_BaseColor']
    if not isinstance(base_color,list) or len(base_color)!=4 or not all(isinstance(x,(float,int)) and not isinstance(x,bool) and math.isfinite(x) for x in base_color):
        raise ValueError('Silk source _BaseColor must contain four finite channels')
    bindings['Base Alpha'] = math_node(tree,'MULTIPLY',bindings['Base Alpha'],float(base_color[3]),'Silk source base alpha times color alpha',SECTION)
    for name,value in bindings.items(): tree.links.new(value,group.inputs[name])
    colors = json.loads(tree['source_colors'])
    silk_color = colors['_SilkStockingsColor']
    if not isinstance(silk_color,list) or len(silk_color)!=4 or not all(isinstance(x,(float,int)) and not isinstance(x,bool) and math.isfinite(x) for x in silk_color):
        raise ValueError('Silk source edge color must contain four finite channels')
    coverage = math_node(tree,'SUBTRACT',math_node(tree,'ADD',bindings['Base Alpha'],1.,'Silk source coverage plus one',SECTION),float(silk_color[3]),'Silk source coverage',SECTION)
    coverage = math_node(tree,'MINIMUM',1.,math_node(tree,'MAXIMUM',0.,coverage,'Silk source coverage lower',SECTION),'Silk source coverage saturate',SECTION)
    tree.links.new(coverage,group.inputs['Coverage'])
    for name,value in (('Camera Forward',camera_forward),('World Up',source_world_up),('CP13 W',cp13_w)):
        if isinstance(value,bpy.types.NodeSocket): tree.links.new(value,group.inputs[name])
        else: group.inputs[name].default_value=value
    for name,source in (('Aniso Direction','_SilkStockingsAnisoDirection'),('Specular Intensity','_SilkStockingsSpecularInt'),('Specular Minimum','_SilkStockingsSpecularMinAtMinWetness'),('Specular Falloff','_SilkStockingsSpecularFalloff'),('Specular Value','_SilkStockingsSpecularValue'),('Iridescent Mode','_SpecRampIridescentMode')):
        group.inputs[name].default_value=_number(values,source)
    original = from_input(emission,'Color')
    added = color_mix(tree,'ADD',1.,original,group.outputs['Silk Specular'],'NPR diffuse plus compiled Silk specular',SECTION)
    tree.links.new(added,emission.inputs['Color'])
    layout(tree)
    return {'source_identity':tree['source_identity'],'kernel':kernel.name,'scope':kernel['source_scope'],'spec_ramp_enabled':ramped,'albedo_unchanged':True,'opacity_unchanged':True,'source_specular_intensity':_number(values,'_SilkStockingsSpecularInt'),'tangent_binding':'Blender UV0 reconstructed tangent; sign cancels in squared NDF','limitations':['native vertex tangent not captured','explicit host camera/up/CP13 binding','nonadvanced dry main-light additive term; no rain/environment/extra-light Silk reconstruction']}
