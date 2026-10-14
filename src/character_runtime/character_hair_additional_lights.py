"""Compiled Hair mode-0 response with explicitly separate host light adaptation.

The mapped loops are split/ramp/line blobs126/134/135 and shared/noLine blobs118/119.
The no-map loops are scalar blobs90/93/120/123, with a distinct unramped lobe.
Its mode0 shadow value is one: host Visibility means the per-light bound factor,
never a shadow-map sample. Host energy, range/cone and record controls are preview
inputs; clustered records, cookie projection and native GPU culling are absent.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import math

import bpy

from character_node_graph import node, math_node, color_mix
from character_weather_nodes import vector_math, scalar_mix
from character_hair_lighting import hair_variant
from patch_character_hgrp import source_keywords


@dataclass(frozen=True, slots=True)
class HairAdditionalSettings:
    character_gate: float
    record_gate: float
    wrap: float
    normalize_color: float
    specular_weight: float
    cookie_red: float
    falloff_exponent: float
    wet_specular_gain: float
    provenance: str

    def validate(self) -> None:
        values = (self.character_gate, self.record_gate, self.wrap,
                  self.normalize_color, self.specular_weight, self.cookie_red,
                  self.falloff_exponent, self.wet_specular_gain)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Hair additional-light settings must be finite")
        if not 0. <= self.normalize_color <= 1. or min(self.specular_weight, self.cookie_red, self.wet_specular_gain) < 0.:
            raise ValueError("Hair normalization must be [0,1]; specular/cookie/wet gain must be nonnegative")
        if not self.provenance.strip():
            raise ValueError("Hair per-light record override provenance is required")


def _build_hair_light_base(name: str) -> tuple[bpy.types.ShaderNodeTree, bpy.types.Node, bpy.types.Node, bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    """Build shared compiled mode0 attenuation, diffuse and per-light half vector."""
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    section = 'Hair Additional Mode0'
    vectors = ('Position', 'Normal', 'View', 'Shifted Tangent', 'Light Position', 'Light Axis')
    colors = ('Light Color', 'NPR Color')
    scalars = ('Energy', 'Energy Scale', 'Range', 'Visibility', 'Enabled',
               'Is Directional', 'Is Spot', 'Spot Size', 'Spot Blend',
               'Falloff Exponent', 'Cookie Red', 'Record Gate', 'CP12 Z',
               'Wrap', 'Color Normalize', 'Cast Shadow', 'Premultiply Alpha',
               'Base Alpha', 'Specular Level', 'Anisotropy Intensity',
               'Edge Fade', 'Specular Weight', 'Wet Specular Gain')
    for label in (*vectors, *colors, *scalars):
        kind = 'NodeSocketVector' if label in vectors else 'NodeSocketColor' if label in colors else 'NodeSocketFloat'
        tree.interface.new_socket(name=label, in_out='INPUT', socket_type=kind)
    for label, kind in (('Contribution','NodeSocketColor'), ('Diffuse','NodeSocketColor'),
                        ('Specular','NodeSocketColor'), ('Attenuation','NodeSocketFloat'),
                        ('Ramp U','NodeSocketFloat'), ('Ramp V','NodeSocketFloat')):
        tree.interface.new_socket(name=label, in_out='OUTPUT', socket_type=kind)
    entry = node(tree, 'NodeGroupInput', 'Explicit Hair light inputs', section)
    output = node(tree, 'NodeGroupOutput', 'Hair mode0 contribution', section)
    v = entry.outputs

    def s(op: str, a: bpy.types.NodeSocket | float, b: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
        return math_node(tree, op, a, b, label, section)

    def vec(op: str, a: bpy.types.NodeSocket, b: bpy.types.NodeSocket | tuple[float,float,float], label: str) -> bpy.types.NodeSocket:
        return vector_math(tree, op, a, b, label).outputs[0]

    def dot(a: bpy.types.NodeSocket, b: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        return vector_math(tree, 'DOT_PRODUCT', a, b, label).outputs['Value']

    def unit(a: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        return vec('NORMALIZE', a, (0.,0.,0.), label)

    def sat(a: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        return s('MINIMUM', 1., s('MAXIMUM', 0., a, label+' lower'), label+' upper')

    def mul(a: bpy.types.NodeSocket, b: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        return color_mix(tree, 'MULTIPLY', 1., a, b, label, section)

    delta = vec('SUBTRACT', v['Light Position'], v['Position'], 'Host light delta')
    d2 = dot(delta, delta, 'Host distance squared')
    axis = unit(v['Light Axis'], 'Host light positive Z')
    direction = unit(color_mix(tree, 'MIX', v['Is Directional'], unit(delta,'Host point direction'), axis, 'Host light direction type', section), 'Host light direction')
    q = sat(s('DIVIDE', d2, s('MULTIPLY',v['Range'],v['Range'],'Host range squared'),'Host range ratio'), 'Host q')
    positive = s('POWER', s('SUBTRACT',1.,q,'Host radial fade'), s('MAXIMUM',.0001,v['Falloff Exponent'],'Positive falloff'), 'Positive radial response')
    cutoff = s('MAXIMUM',0.,s('SUBTRACT',1.,s('MULTIPLY',q,q,'Range q squared'),'Radial cutoff'), 'Radial cutoff positive')
    negative = s('DIVIDE',s('MULTIPLY',cutoff,cutoff,'Radial cutoff squared'),s('ADD',1.,d2,'Radial distance denominator'),'Negative radial response')
    radial = scalar_mix(tree,s('LESS_THAN',v['Falloff Exponent'],0.,'Negative falloff branch'),positive,negative,'Radial branch')
    radial = scalar_mix(tree,v['Is Directional'],radial,1.,'Host directional range')
    half = s('MULTIPLY',.5,v['Spot Size'],'Host spot half angle')
    outer = s('COSINE',half,0.,'Host outer cosine')
    inner = s('COSINE',s('MULTIPLY',half,s('SUBTRACT',1.,v['Spot Blend'],'Host spot inner fraction'),'Host inner angle'),0.,'Host inner cosine')
    ratio = sat(s('DIVIDE',s('SUBTRACT',dot(direction,axis,'Host spot cosine'),outer,'Host cone numerator'),s('MAXIMUM',1e-6,s('SUBTRACT',inner,outer,'Host cone span'),'Host safe cone span'),'Host cone ratio'),'Host cone saturate')
    cone = scalar_mix(tree,v['Is Spot'],1.,s('MULTIPLY',ratio,ratio,'Host cone squared'),'Host cone type')
    attenuation = s('MULTIPLY',s('MULTIPLY',radial,cone,'Radial and cone'),s('MULTIPLY',v['Cookie Red'],v['Visibility'],'Cookie times record bound'),'Source A before enable')
    gate = s('SUBTRACT',1.,s('LESS_THAN',s('ADD',v['Record Gate'],v['CP12 Z'],'Record plus character gate'),.5,'Source character skip'),'Source character gate')
    gate = s('MULTIPLY',gate,s('GREATER_THAN',attenuation,.0001,'Source attenuation branch'),'Source light loop active')
    active = s('MULTIPLY',gate,v['Enabled'],'Host enable after source gate')
    attenuation = s('MULTIPLY',attenuation,active,'Effective mode0 attenuation')
    energy = s('MULTIPLY',v['Energy'],v['Energy Scale'],'Explicit host energy conversion')
    color = mul(v['Light Color'],energy,'Host source record color')
    attenuated = mul(color,attenuation,'Source color times attenuation')
    rgb = node(tree,'ShaderNodeSeparateColor','Attenuated RGB',section)
    tree.links.new(attenuated,rgb.inputs[0])
    maximum = s('MAXIMUM',s('MAXIMUM',rgb.outputs['Red'],rgb.outputs['Green'],'Source max RG'),rgb.outputs['Blue'],'Source max RGB')
    normalizer_scale = scalar_mix(tree,s('SUBTRACT',1.,v['Cast Shadow'],'Source inverse cast'),.75,.5,'Source inverse-cast scale')
    reciprocal = s('DIVIDE',1.,s('MAXIMUM',1.,s('MULTIPLY',maximum,normalizer_scale,'Source normalize product'),'Source normalize floor'),'Source reciprocal')
    normalizer = scalar_mix(tree,v['Color Normalize'],1.,reciprocal,'Source record normalize')
    normal = unit(v['Normal'],'Hair source diffuse normal')
    ndl = dot(normal,direction,'Hair source NdotL')
    wrap = scalar_mix(tree,sat(s('ADD',ndl,.5,'Hair half-offset NdotL'),'Hair wrap saturate'),s('MULTIPLY',.25,v['Wrap'],'Hair source quarter-wrap'),1.,'Hair source mode0 wrap')
    radiance = mul(attenuated,s('MULTIPLY',normalizer,wrap,'Normalize times wrap'),'Mode0 shared radiance')
    premultiply = scalar_mix(tree,v['Premultiply Alpha'],1.,v['Base Alpha'],'Source alpha premultiply')
    diffuse = mul(mul(radiance,v['NPR Color'],'Mode0 NPR diffuse'),premultiply,'Mode0 premultiplied diffuse')
    half_sum = vec('ADD',direction,unit(v['View'],'Hair source view'),'Hair per-light half sum')
    half_length2 = dot(half_sum,half_sum,'Hair per-light half length squared')
    half_inverse = s('INVERSE_SQRT',s('MAXIMUM',2.**-14,half_length2,'Source half normalization floor'),0.,'Source half reciprocal length')
    half_scale = node(tree,'ShaderNodeVectorMath','Source per-light half vector',section)
    half_scale.operation='SCALE'
    tree.links.new(half_sum,half_scale.inputs[0]);tree.links.new(half_inverse,half_scale.inputs['Scale'])
    h = half_scale.outputs[0]
    th = dot(v['Shifted Tangent'],h,'Source shifted tangent dot half')
    return tree, entry, output, radiance, diffuse, ndl, th


def _finish_hair_light(tree: bpy.types.ShaderNodeTree, output: bpy.types.Node, radiance: bpy.types.NodeSocket, diffuse: bpy.types.NodeSocket, ndl: bpy.types.NodeSocket, lobe: bpy.types.NodeSocket, specular_color: bpy.types.NodeSocket | tuple[float,float,float,float], u: bpy.types.NodeSocket, ramp_v: bpy.types.NodeSocket) -> None:
    section='Hair Additional Mode0'
    positive=math_node(tree,'MINIMUM',1.,math_node(tree,'MAXIMUM',0.,ndl,'Source positive NdotL lower',section),'Source positive NdotL upper',section)
    weight=math_node(tree,'MULTIPLY',lobe,positive,'Hair NdotL spec weight',section)
    specular=color_mix(tree,'MULTIPLY',1.,color_mix(tree,'MULTIPLY',1.,radiance,specular_color,'Hair additional specular radiance',section),weight,'Hair mode0 specular',section)
    contribution=color_mix(tree,'ADD',1.,diffuse,specular,'Hair mode0 diffuse plus strand specular',section)
    for label,value in (('Contribution',contribution),('Diffuse',diffuse),('Specular',specular),('Attenuation',next(n for n in tree.nodes if n.label=='Effective mode0 attenuation').outputs[0]),('Ramp U',u),('Ramp V',ramp_v)):
        tree.links.new(value,output.inputs[label])
    tree['mode']=0


def build_hair_default_light(name: str, spec_ramp: bpy.types.Image) -> bpy.types.ShaderNodeTree:
    """Build mapped split/shared mode0 with the verified first-lobe ramp arithmetic."""
    if spec_ramp.packed_file is None or len(spec_ramp.pixels)==0:
        raise ValueError('Hair additional spec ramp must have packed source pixels')
    tree,entry,output,radiance,diffuse,ndl,th=_build_hair_light_base(name)
    v=entry.outputs
    section = 'Hair Additional Mode0'
    def s(op: str, a: bpy.types.NodeSocket | float, b: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
        return math_node(tree,op,a,b,label,section)
    def sat(a: bpy.types.NodeSocket, label: str) -> bpy.types.NodeSocket:
        return s('MINIMUM',1.,s('MAXIMUM',0.,a,label+' lower'),label+' upper')
    sine = s('SQRT',s('MAXIMUM',0.,s('SUBTRACT',1.,s('MULTIPLY',th,th,'Hair TH squared'),'Hair sine squared'),'Hair finite sine squared'),0.,'Hair sine root')
    u = sat(s('MULTIPLY',s('POWER',s('MAXIMUM',.0001,sine,'Hair sine source floor'),200.,'Hair fixed exponent'),v['Specular Level'],'Hair source spec level'), 'Hair ramp U')
    ramp_v = s('MULTIPLY',s('GREATER_THAN',th,0.,'Hair positive tangent half'),s('MULTIPLY',v['Edge Fade'],v['Edge Fade'],'Hair edge squared'),'Hair ramp V')
    uv = node(tree,'ShaderNodeCombineXYZ','Hair additional spec ramp coordinates',section)
    tree.links.new(u,uv.inputs['X']);tree.links.new(ramp_v,uv.inputs['Y'])
    ramp = node(tree,'ShaderNodeTexImage','Source additional Hair spec ramp',section)
    ramp.image=spec_ramp;ramp.interpolation='Linear';ramp.extension='EXTEND'
    tree.links.new(uv.outputs[0],ramp.inputs['Vector'])
    lobe = s('MULTIPLY',s('MULTIPLY',u,v['Edge Fade'],'Hair edge lobe'),s('MULTIPLY',s('MULTIPLY',.04,v['Specular Level'],'Hair dielectric F0'),s('MULTIPLY',5.,v['Anisotropy Intensity'],'Hair source intensity x5'),'Hair source spec intensity'),'Hair lobe scale')
    lobe = s('MULTIPLY',lobe,s('MULTIPLY',v['Wet Specular Gain'],v['Specular Weight'],'Hair wet and record spec factors'),'Hair scaled lobe')
    _finish_hair_light(tree,output,radiance,diffuse,ndl,lobe,ramp.outputs['Color'],u,ramp_v)
    tree['source_scope']='Compiled Hair mapped ForwardLit mode0: split/line blobs126/134/135 and shared/noLine blobs118/119. Shadow LUT is consumed by upstream NPR Color. Source mode0 shadow=1; host range/cone/energy/record bindings remain explicit. No second lobe or CP13 multiplier.'
    tree['hair_additional_variant']='mapped_ramp'
    return tree


def build_hair_scalar_default_light(name: str) -> bpy.types.ShaderNodeTree:
    """Build the compiled no-map lobe without a ramp image or mapped G clamp."""
    tree,entry,output,radiance,diffuse,ndl,th=_build_hair_light_base(name)
    v=entry.outputs
    section = 'Hair Additional Mode0'
    def s(op: str, a: bpy.types.NodeSocket | float, b: bpy.types.NodeSocket | float, label: str) -> bpy.types.NodeSocket:
        return math_node(tree,op,a,b,label,section)
    sine=s('SQRT',s('MAXIMUM',0.,s('SUBTRACT',1.,s('MULTIPLY',th,th,'Hair scalar TH squared'),'Hair scalar sine squared'),'Hair scalar finite sine squared'),0.,'Hair scalar sine root')
    power=s('POWER',s('MAXIMUM',.0001,sine,'Hair scalar sine source floor'),200.,'Hair scalar source exponent')
    f0=s('MULTIPLY',.04,v['Specular Level'],'Hair scalar dielectric F0')
    intensity=s('MULTIPLY',5.,v['Anisotropy Intensity'],'Hair scalar source intensity x5')
    lobe=s('MULTIPLY',s('MULTIPLY',power,v['Edge Fade'],'Hair scalar edge lobe'),s('MULTIPLY',f0,intensity,'Hair scalar spec intensity'),'Hair scalar lobe scale')
    lobe=s('MULTIPLY',lobe,s('MULTIPLY',v['Wet Specular Gain'],v['Specular Weight'],'Hair scalar wet and record spec factors'),'Hair scalar scaled lobe')
    zero=node(tree,'ShaderNodeValue','No source spec ramp coordinate',section).outputs[0];zero.default_value=0.
    _finish_hair_light(tree,output,radiance,diffuse,ndl,lobe,(1.,1.,1.,1.),power,zero)
    tree['source_scope']='Compiled Hair scalar ForwardLit mode0 blobs90/93/120/123; pow(sine,200)*edge*(0.04*source Specular)*intensity*5. No map, saturation by mapped G, second lobe or CP13 multiplier; explicit host adaptation.'
    tree['hair_additional_variant']='scalar_no_ramp'
    return tree



def _find(tree: bpy.types.NodeTree, label: str) -> bpy.types.Node:
    matches = tuple(n for n in tree.nodes if n.label == label)
    if len(matches) != 1:
        raise ValueError(f'{tree.name}: expected one source node {label!r}; found {len(matches)}')
    return matches[0]


def additional_hair_support(tree: bpy.types.NodeTree) -> tuple[bool,str]:
    """Match only mode0 families whose actual ForwardLit loops were inspected."""
    if tree.get('source_shader') != 'HGRP/CharacterNPR_Hair':
        return False,'not_source_Hair'
    variant=hair_variant(tree)
    keywords=set(source_keywords(tree))
    floats=json.loads(tree['source_floats'])
    for field,keyword in (('_UseDiffRampMap','_DIFF_RAMP_ON'),('_SpecularLine','_SPECULAR_LINE'),('_UseShadowLutTex','_SHADOW_LUT_TEX')):
        value=floats.get(field)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or value not in (0.,1.):
            raise ValueError(f'Hair source branch switch must be binary: {tree.name}/{field}/{value!r}')
        if bool(value)!=(keyword in keywords):
            raise ValueError(f'Hair source branch switch/keyword mismatch: {tree.name}/{field}/{keyword}')
    ramp_line=('_DIFF_RAMP_ON' in keywords,'_SPECULAR_LINE' in keywords)
    # The preserved manifest places OUTLINE_MASK and DRAW_UNDER_BROW only in
    # outline/pre-GBuffer passes, never ForwardLit. They do not select this loop.
    if 'VFX_CHARACTER_DISSOLVE' in keywords:
        return False,'additional_source_keyword_variant_not_inspected'
    if not tree.get('hair_lighting_patch'):
        return False,'verified_main_Hair_graph_required'
    if variant=='split_normals_mapped_ramp' and ramp_line==(True,True):
        if '_SHADOW_LUT_TEX' in keywords:
            return True,'compiled_split_normal_ramp_line_LUT_mode0_blobs134_135'
        return True,'compiled_split_normal_ramp_line_mode0_blob126'
    if variant=='shared_bump_normal_mapped_ramp' and ramp_line==(True,False):
        return True,'compiled_shared_normal_ramp_noLine_mode0_blobs118_119'
    if variant=='geometric_normal_scalar_single_lobe' and ramp_line in ((False,False),(True,True)) and '_SHADOW_LUT_TEX' not in keywords:
        return True,'compiled_scalar_mode0_blobs90_93_120_123'
    return False,'compiled_mode0_keyword_family_not_inspected'


def _verify_shadow_lut_path(tree: bpy.types.ShaderNodeTree, diffuse: bpy.types.Node) -> None:
    """Require source LUT samples to remain live ancestors of the NPR shadow color."""
    sources=tuple(n for n in tree.nodes if n.type=='TEX_IMAGE' and n.get('source_slot')=='_ShadowLutTex')
    if len(sources)!=1 or sources[0].image is None or sources[0].image.packed_file is None:
        raise ValueError(f'Hair source-enabled ShadowLUT must have one packed identity sample: {tree.name}')
    roots=tuple(link.from_node for link in diffuse.inputs['Scaled Shadow Albedo'].links)
    if len(roots)!=1:
        raise ValueError(f'Hair NPR shadow input must have one upstream color: {tree.name}')
    pending=list(roots);visited:set[str]=set()
    while pending:
        current=pending.pop()
        if current.name in visited:continue
        visited.add(current.name)
        if current.type=='TEX_IMAGE' and current.image==sources[0].image and current.label.startswith('SampleShadowLut slice '):
            return
        pending.extend(link.from_node for socket in current.inputs for link in socket.links)
    raise ValueError(f'Hair source-enabled ShadowLUT is not a live NPR Color ancestor: {tree.name}')


def attach_hair_default_lights(tree: bpy.types.ShaderNodeTree, states: Sequence[bpy.types.ShaderNodeTree], settings: HairAdditionalSettings, layout: Callable[[bpy.types.NodeTree],None]) -> bpy.types.NodeSocket:
    """Append mode0 contributions to the existing main color, leaving it intact."""
    settings.validate()
    supported, reason = additional_hair_support(tree)
    if not supported:
        raise ValueError(f'Hair additional-light source variant is unsupported: {tree.name}/{reason}')
    if tree.get('additional_hair_lights_connected') or not states:
        raise ValueError(f'Hair requires nonempty unbound live states: {tree.name}')
    floats=json.loads(tree['source_floats']);colors=json.loads(tree['source_colors'])
    for key in ('_AlphaPremultiply','_AnisotropyIntensity'):
        if key not in floats or not isinstance(floats[key],(int,float)) or isinstance(floats[key],bool) or not math.isfinite(floats[key]):
            raise ValueError(f'Hair required source scalar invalid: {tree.name}/{key}')
    base_color=colors['_BaseColor']
    if len(base_color)!=4 or not all(isinstance(x,(float,int)) and math.isfinite(x) for x in base_color):
        raise ValueError(f'Hair source _BaseColor must contain four finite components: {tree.name}')
    variant=hair_variant(tree)
    if variant=='geometric_normal_scalar_single_lobe':
        kernel=build_hair_scalar_default_light('EF Hair Additional Mode0 - '+tree.name)
        surface_normal=_find(tree,'Normalize unmapped Hair source normal').outputs[0]
        specular_level=_find(tree,'Hair source scalar Specular').outputs[0]
    else:
        ramp=_find(tree,'HGRP strand 1 source spec ramp lookup').image
        if ramp is None:
            raise ValueError(f'Hair source ramp missing: {tree.name}')
        kernel=build_hair_default_light('EF Hair Additional Mode0 - '+tree.name,ramp)
        bsdf=_find(tree,'CharacterNPR_Hair shared Eevee / Cycles surface')
        if len(bsdf.inputs['Normal'].links)!=1:
            raise ValueError(f'Hair source mapped normal must be linked: {tree.name}')
        surface_normal=bsdf.inputs['Normal'].links[0].from_socket
        specular_level=_find(tree,'R metallic / G specular / B AO / A smoothness').outputs['Green']
    geometry=node(tree,'ShaderNodeNewGeometry','Additional Hair surface geometry','Hair Additional Mode0')
    diffuse=_find(tree,'Verified local HGRP diffuse equations')
    if floats['_UseShadowLutTex']==1.:
        _verify_shadow_lut_path(tree,diffuse)
    tangent=_find(tree,'Normalize hair strand 1 tangent').outputs[0]
    # Split uses BA for the tangent lobe; shared uses the live common BumpMap.
    # Scalar uses its source geometric normal and never requires a texture node.
    edge=_find(tree,'HGRP hair edge fade').outputs[0]
    base_images=tuple(n for n in tree.nodes if n.type=='TEX_IMAGE' and n.label.startswith('_BaseMap '))
    if len(base_images)!=1 or base_images[0].image is None or base_images[0].image.packed_file is None:
        raise ValueError(f'Hair packed source BaseMap must be unique: {tree.name}')
    alpha=math_node(tree,'MULTIPLY',base_images[0].outputs['Alpha'],float(base_color[3]),'Hair actual source base alpha','Hair Additional Mode0')
    if len(diffuse.inputs['Cast Shadow'].links)!=1:
        raise ValueError(f'Hair diffuse Cast Shadow must have one explicit source: {tree.name}')
    cast=diffuse.inputs['Cast Shadow'].links[0].from_socket
    result: bpy.types.NodeSocket | None=None
    for light_state in states:
        if not light_state.get('bound_light'):
            raise ValueError(f'Additional Hair state has no bound light identity: {light_state.name}')
        label=str(light_state['bound_light'])
        live=node(tree,'ShaderNodeGroup','Live additional Hair state '+label,'Hair Additional Mode0');live.node_tree=light_state
        response=node(tree,'ShaderNodeGroup','Compiled additional Hair mode0 '+label,'Hair Additional Mode0');response.node_tree=kernel
        bindings={'Position':geometry.outputs['Position'],'Normal':surface_normal,'View':geometry.outputs['Incoming'],
                  'Shifted Tangent':tangent,'NPR Color':diffuse.outputs['NPR Color'],'Edge Fade':edge,
                  'Specular Level':specular_level,'Base Alpha':alpha,'Cast Shadow':cast,
                  'Light Position':live.outputs['Position'],'Light Axis':live.outputs['Toward Axis'],'Light Color':live.outputs['Color']}
        for key,socket in bindings.items():tree.links.new(socket,response.inputs[key])
        for key in ('Energy','Energy Scale','Range','Visibility','Enabled','Is Directional','Is Spot','Spot Size','Spot Blend'):
            tree.links.new(live.outputs[key],response.inputs[key])
        for key,value in (('CP12 Z',settings.character_gate),('Record Gate',settings.record_gate),('Wrap',settings.wrap),
                          ('Color Normalize',settings.normalize_color),('Specular Weight',settings.specular_weight),
                          ('Cookie Red',settings.cookie_red),('Falloff Exponent',settings.falloff_exponent),
                          ('Wet Specular Gain',settings.wet_specular_gain),('Premultiply Alpha',float(floats['_AlphaPremultiply'])),
                          ('Anisotropy Intensity',float(floats['_AnisotropyIntensity']))):
            response.inputs[key].default_value=value
        response['record_override_provenance']=settings.provenance
        result=response.outputs['Contribution'] if result is None else color_mix(tree,'ADD',1.,result,response.outputs['Contribution'],'Sum Hair light '+label,'Hair Additional Mode0')
    if result is None:raise AssertionError('Hair additional response is empty')
    emission=_find(tree,'Artist-lit NPR diffuse - shared engines')
    links=tuple(emission.inputs['Color'].links)
    if len(links)!=1:raise ValueError(f'Hair main response must have one color source: {tree.name}')
    combined=color_mix(tree,'ADD',1.,links[0].from_socket,result,'Main Hair plus additional mode0','Hair Additional Mode0')
    tree.links.new(combined,emission.inputs['Color'])
    tree['additional_hair_lights_connected']=len(states)
    tree['additional_hair_variant']=variant
    tree['additional_hair_source_family']=reason
    tree['additional_hair_scope']='Mode0 only; source shadow=1; host visibility is bound factor, not shadow. Dry wet gain is explicit, not a runtime sample.'
    layout(kernel);layout(tree)
    return result
