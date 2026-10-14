"""Source plain Skin punctual response with explicit host light/visibility inputs."""
from __future__ import annotations
import bpy
from character_node_graph import node,math_node,color_mix
from character_weather_nodes import vector_math,combine_vector,scalar_mix


def build_skin_punctual(name: str) -> bpy.types.ShaderNodeTree:
    tree=bpy.data.node_groups.new(name,'ShaderNodeTree');section='Skin Additional Light'
    vectors=('Position','Normal','Wrap Normal','Specular Normal','View','Light Position','Light Axis')
    colors=('Light Color','NPR Color','Specular Color')
    scalars=('Energy','Energy Scale','Range','Visibility','Enabled','Is Directional','Is Spot','Spot Size','Spot Blend','Falloff Exponent','Wrap','Color Normalize','Global Normalize Scale','Specular Weight','Perceptual Roughness')
    for label in vectors+colors+scalars:
        kind='NodeSocketVector' if label in vectors else 'NodeSocketColor' if label in colors else 'NodeSocketFloat'
        tree.interface.new_socket(name=label,in_out='INPUT',socket_type=kind)
    for label,kind in (('Contribution','NodeSocketColor'),('Attenuation','NodeSocketFloat'),('GGX','NodeSocketFloat')):
        tree.interface.new_socket(name=label,in_out='OUTPUT',socket_type=kind)
    entry=node(tree,'NodeGroupInput','Explicit additional-light inputs',section);output=node(tree,'NodeGroupOutput','Additional Skin contribution',section);v=entry.outputs
    def s(op: str,a: bpy.types.NodeSocket|float,b: bpy.types.NodeSocket|float,label: str) -> bpy.types.NodeSocket:return math_node(tree,op,a,b,label,section)
    def vec(op: str,a: bpy.types.NodeSocket,b: bpy.types.NodeSocket|tuple[float,float,float],label: str) -> bpy.types.NodeSocket:return vector_math(tree,op,a,b,label).outputs[0]
    def dot(a: bpy.types.NodeSocket,b: bpy.types.NodeSocket,label: str) -> bpy.types.NodeSocket:return vector_math(tree,'DOT_PRODUCT',a,b,label).outputs['Value']
    def unit(a: bpy.types.NodeSocket,label: str) -> bpy.types.NodeSocket:return vec('NORMALIZE',a,(0.,0.,0.),label)
    def sat(a: bpy.types.NodeSocket,label: str) -> bpy.types.NodeSocket:return s('MINIMUM',1.,s('MAXIMUM',0.,a,label+' lower'),label+' upper')
    def col(op: str,a: bpy.types.NodeSocket,b: bpy.types.NodeSocket,label: str) -> bpy.types.NodeSocket:return color_mix(tree,op,1.,a,b,label,section)
    delta=vec('SUBTRACT',v['Light Position'],v['Position'],'Source light position minus surface')
    distance2=dot(delta,delta,'Squared light distance')
    direction=unit(delta,'Surface toward point light');axis=unit(v['Light Axis'],'Normalize light local positive Z')
    direction=color_mix(tree,'MIX',v['Is Directional'],direction,axis,'Select host directional light vector',section)
    direction=unit(direction,'Normalize selected light direction')
    range2=s('MULTIPLY',v['Range'],v['Range'],'Source light range squared')
    q=sat(s('DIVIDE',distance2,range2,'Squared normalized light distance'),'Range q')
    fade=s('SUBTRACT',1.,q,'Positive source range fade')
    positive=s('POWER',fade,s('MAXIMUM',v['Falloff Exponent'],.0001,'Positive falloff exponent'),'Positive source radial response')
    cut=s('MAXIMUM',0.,s('SUBTRACT',1.,s('MULTIPLY',q,q,'Negative range q squared'),'Negative source radial cutoff'),'Nonnegative radial cutoff')
    negative=s('DIVIDE',s('MULTIPLY',cut,cut,'Negative source cutoff squared'),s('ADD',1.,distance2,'Negative source distance denominator'),'Negative source radial response')
    radial=scalar_mix(tree,s('LESS_THAN',v['Falloff Exponent'],0.,'Negative falloff branch'),positive,negative,'Select source radial branch')
    radial=scalar_mix(tree,v['Is Directional'],radial,1.,'Host Sun has no distance fade')
    half=s('MULTIPLY',v['Spot Size'],.5,'Host spot outer half angle')
    outer=s('COSINE',half,0.,'Host spot outer cosine')
    inner=s('COSINE',s('MULTIPLY',half,s('SUBTRACT',1.,v['Spot Blend'],'Host spot inner fraction'),'Host spot inner half angle'),0.,'Host spot inner cosine')
    cone=sat(s('DIVIDE',s('SUBTRACT',dot(direction,axis,'Host spot angular cosine'),outer,'Host spot cone numerator'),s('MAXIMUM',.000001,s('SUBTRACT',inner,outer,'Host spot cone span'),'Host spot cone denominator'),'Host spot cone ratio'),'Host cone')
    cone=scalar_mix(tree,v['Is Spot'],1.,s('MULTIPLY',cone,cone,'Source squared angular response'),'Host spot gate')
    attenuation=s('MULTIPLY',s('MULTIPLY',radial,cone,'Radial times angular response'),s('MULTIPLY',v['Visibility'],v['Enabled'],'Explicit light visibility'),'Source-style light attenuation')
    energy=s('MULTIPLY',s('MULTIPLY',v['Energy'],v['Energy Scale'],'Explicit host energy conversion'),attenuation,'Attenuated light strength')
    radiant=col('MULTIPLY',v['Light Color'],energy,'Additional light radiance')
    rgb=node(tree,'ShaderNodeSeparateColor','Additional radiance channels',section);tree.links.new(radiant,rgb.inputs[0])
    maximum=s('MAXIMUM',s('MAXIMUM',rgb.outputs['Red'],rgb.outputs['Green'],'Radiance max RG'),rgb.outputs['Blue'],'Radiance max RGB')
    normalize=s('DIVIDE',1.,s('MAXIMUM',1.,s('MULTIPLY',maximum,v['Global Normalize Scale'],'Source radiance normalization scale'),'Source normalization floor'),'Source radiance normalization reciprocal')
    normalize=scalar_mix(tree,v['Color Normalize'],1.,normalize,'Source per-light color normalization')
    normal=unit(v['Normal'],'Normalize Skin light normal');view=unit(v['View'],'Normalize local-light view')
    ndl=dot(normal,direction,'Source punctual NdotL')
    wrap_normal=unit(v['Wrap Normal'],'Normalize Skin wrap normal')
    wrap_ndl=dot(wrap_normal,direction,'Source punctual wrap NdotL')
    wrap=scalar_mix(tree,sat(s('ADD',.5,wrap_ndl,'Source half-offset wrap NdotL'),'Punctual half-offset'),s('MULTIPLY',.5,v['Wrap'],'Source wrap minimum'),1.,'Source plain diffuse wrap')
    radiant=col('MULTIPLY',radiant,s('MULTIPLY',normalize,wrap,'Source normalized plain wrap'),'Wrapped additional radiance')
    h=unit(vec('ADD',view,direction,'Local light view half vector'),'Normalize punctual half vector')
    sn=unit(v['Specular Normal'],'Normalize punctual specular normal');nh=dot(sn,h,'Punctual NdotH');nv=sat(dot(sn,view,'Punctual NdotV'),'Punctual NV')
    rough=s('MAXIMUM',.0078125,s('MULTIPLY',v['Perceptual Roughness'],v['Perceptual Roughness'],'Punctual roughness square'),'Punctual alpha minimum');a2=s('MULTIPLY',rough,rough,'Punctual alpha square')
    den=s('ADD',1.,s('MULTIPLY',s('SUBTRACT',a2,1.,'Punctual alpha difference'),s('MULTIPLY',nh,nh,'Punctual NH square'),'Punctual distribution product'),'Punctual D denominator');d2=s('MULTIPLY',den,den,'Punctual D denominator square')
    eq=node(tree,'ShaderNodeMath','Exact punctual distribution equality',section);eq.operation='COMPARE';eq.inputs[2].default_value=0.;tree.links.new(a2,eq.inputs[0]);tree.links.new(d2,eq.inputs[1])
    distribution=scalar_mix(tree,eq.outputs[0],s('DIVIDE',a2,d2,'Punctual distribution ratio'),1.,'Punctual distribution selection')
    vis=s('DIVIDE',.5,s('ADD',s('ADD',s('MULTIPLY',2.,nv,'Punctual double NV'),rough,'Punctual visibility alpha'),.0001,'Punctual visibility epsilon'),'Punctual Kelemen')
    ggx=s('MINIMUM',20.,s('MAXIMUM',0.,s('SUBTRACT',s('MULTIPLY',distribution,vis,'Punctual DV'),2.**-14,'Punctual half-min subtraction'),'Punctual GGX lower'),'Punctual GGX upper')
    spec=col('MULTIPLY',v['Specular Color'],s('MULTIPLY',ggx,s('MULTIPLY',v['Specular Weight'],sat(ndl,'Punctual specular NdotL'),'Source per-light specular weight'),'Punctual weighted GGX'),'Punctual source specular')
    contribution=col('MULTIPLY',radiant,col('ADD',v['NPR Color'],spec,'Source plain diffuse plus specular'),'Source plain additional Skin color')
    for label,value in (('Contribution',contribution),('Attenuation',attenuation),('GGX',ggx)):tree.links.new(value,output.inputs[label])
    tree['scope']='Compiled plain Skin punctual response and two radial branches; explicit Blender point/spot/Sun bridge. Geometric emitter integration, clustered culling, cookies and native shadow-map sampling are outside this host bridge.'
    return tree
