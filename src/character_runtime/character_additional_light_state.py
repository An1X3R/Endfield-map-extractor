"""Live Blender light inputs for explicit HGRP local-light material bridges."""
from __future__ import annotations
from dataclasses import dataclass
import math
import bpy
from character_node_graph import node
from character_hgrp_light_adapter import scalar_property_driver


@dataclass(frozen=True)
class AdditionalLightSettings:
    energy_scale: float
    range_meters: float
    visibility: float


def object_property_driver(socket: bpy.types.NodeSocket,light: bpy.types.Object,path: str) -> None:
    driver=socket.driver_add('default_value').driver
    driver.type='SCRIPTED';driver.expression='value'
    variable=driver.variables.new();variable.name='value';variable.type='SINGLE_PROP'
    variable.targets[0].id=light;variable.targets[0].data_path=path


def build_additional_light_state(light: bpy.types.Object,settings: AdditionalLightSettings) -> bpy.types.ShaderNodeTree:
    if light.type!='LIGHT' or light.data.type not in ('POINT','SPOT','SUN'):
        raise ValueError(f'Additional HGRP bridge supports Point, Spot and Sun: {light.name}')
    if not all(math.isfinite(v) for v in (settings.energy_scale,settings.range_meters,settings.visibility)) or settings.energy_scale<=0 or settings.range_meters<=0 or not 0<=settings.visibility<=1:
        raise ValueError(f'Invalid explicit additional-light bridge settings: {settings}')
    for name,value in (('hgrp_energy_scale',settings.energy_scale),('hgrp_range_meters',settings.range_meters),('hgrp_visibility',settings.visibility)):
        if name in light:raise ValueError(f'Additional light already has configured bridge property: {light.name}/{name}')
        light[name]=value
    light['hgrp_light_scope']='Live Blender light bridge with explicit energy conversion and visibility. Native shadow atlas and light-list culling are not captured.'
    tree=bpy.data.node_groups.new('EF Additional Light - '+light.name,'ShaderNodeTree')
    section='Live Additional Light'
    output=node(tree,'NodeGroupOutput','Live light properties',section)
    for name in ('Position','Toward Axis'):
        tree.interface.new_socket(name=name,in_out='OUTPUT',socket_type='NodeSocketVector')
        xyz=node(tree,'ShaderNodeCombineXYZ',name,section)
        column=3 if name=='Position' else 2
        for row in range(3):object_property_driver(xyz.inputs[row],light,f'matrix_world[{column}][{row}]')
        tree.links.new(xyz.outputs[0],output.inputs[name])
    tree.interface.new_socket(name='Color',in_out='OUTPUT',socket_type='NodeSocketColor')
    rgb=node(tree,'ShaderNodeRGB','Live additional light RGB',section)
    for index in range(3):
        driver=rgb.outputs[0].driver_add('default_value',index).driver;driver.type='SCRIPTED';driver.expression='value'
        var=driver.variables.new();var.name='value';var.type='SINGLE_PROP';var.targets[0].id_type='LIGHT';var.targets[0].id=light.data;var.targets[0].data_path=f'color[{index}]'
    tree.links.new(rgb.outputs[0],output.inputs['Color'])
    for name in ('Energy','Energy Scale','Range','Visibility','Enabled','Is Directional','Is Spot','Spot Size','Spot Blend'):
        tree.interface.new_socket(name=name,in_out='OUTPUT',socket_type='NodeSocketFloat')
        control=node(tree,'ShaderNodeValue',name,section);tree.links.new(control.outputs[0],output.inputs[name])
        if name=='Energy':scalar_property_driver(control.outputs[0],light.data,'energy')
        elif name in ('Energy Scale','Range','Visibility'):
            prop={'Energy Scale':'hgrp_energy_scale','Range':'hgrp_range_meters','Visibility':'hgrp_visibility'}[name]
            object_property_driver(control.outputs[0],light,f'["{prop}"]')
        elif name=='Enabled':
            object_property_driver(control.outputs[0],light,'hide_render');control.outputs[0].driver_add('default_value').driver.expression='1-value'
        elif name=='Is Directional':control.outputs[0].default_value=float(light.data.type=='SUN')
        elif name=='Is Spot':control.outputs[0].default_value=float(light.data.type=='SPOT')
        elif light.data.type=='SPOT':scalar_property_driver(control.outputs[0],light.data,{'Spot Size':'spot_size','Spot Blend':'spot_blend'}[name])
        else:control.outputs[0].default_value=0.
    tree['bound_light']=light.name;tree['light_type']=light.data.type
    return tree
