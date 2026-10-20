"""Opt-in editable flow shading and authored ribbons, not native VFX parity."""
from __future__ import annotations

import math
import json
from typing import Literal, TypedDict

import bpy

from blender_scene_effect_animation import animate_effect_material_time
from blender_scene_materials import layout_shader_node_tree
from endfield_effect_presentation import FlowPresentation, read_flow_presentation


class FlowPresentationAudit(TypedDict):
    status: Literal["explicit_authored_approximation"]
    collection: str
    objects: list[str]
    material: str
    provenance: str
    limitations: str


def create_flow_preview_material(uv_map: str, color: tuple[float, float, float],
                                 speed: float, opacity: float, intensity: float,
                                 streak_density: float) -> bpy.types.Material:
    """Build a portable animated streak shader with exposed artist controls."""
    if not uv_map or not all(math.isfinite(value) for value in (*color, speed, opacity, intensity, streak_density)):
        raise ValueError('Flow preview requires a UV name and finite parameters')
    if min(color) < 0 or not 0 <= opacity <= 1 or intensity < 0 or streak_density <= 0:
        raise ValueError('Flow preview color/intensity must be nonnegative, opacity0..1 and density positive')
    material = bpy.data.materials.new('EF Editable Flow Preview')
    material.use_nodes = True
    material.surface_render_method = 'BLENDED'
    material.use_transparent_shadow = False
    material['endfield_fx_status'] = 'authored_flow_preview_not_source_shader'
    material['endfield_fx_limitations'] = (
        'Uses supplied UVs on source or explicitly authored geometry. Streak shading, continuous activation, color and speed '
        'are editable artistic approximations, not recovered game runtime values.')
    tree = material.node_tree
    tree.nodes.clear()
    time = tree.nodes.new('ShaderNodeValue')
    time.name = time.label = 'EF FX Source Time Seconds'
    group = bpy.data.node_groups.new('EF Flow Presentation', 'ShaderNodeTree')
    for name, kind in (('UV', 'NodeSocketVector'), ('Seconds', 'NodeSocketFloat'),
                       ('Flow Speed', 'NodeSocketFloat'), ('Streak Density', 'NodeSocketFloat'),
                       ('Opacity', 'NodeSocketFloat'), ('Glow', 'NodeSocketFloat'),
                       ('Flow Color', 'NodeSocketColor')):
        group.interface.new_socket(name=name, in_out='INPUT', socket_type=kind)
    group.interface.new_socket(name='Surface', in_out='OUTPUT', socket_type='NodeSocketShader')
    inputs = group.nodes.new('NodeGroupInput')
    separate = group.nodes.new('ShaderNodeSeparateXYZ')
    group.links.new(inputs.outputs['UV'], separate.inputs[0])
    density = group.nodes.new('ShaderNodeMath')
    density.operation = 'MULTIPLY'
    group.links.new(separate.outputs['Y'], density.inputs[0])
    group.links.new(inputs.outputs['Streak Density'], density.inputs[1])
    clock = group.nodes.new('ShaderNodeMath')
    clock.operation = 'MULTIPLY'
    group.links.new(inputs.outputs['Seconds'], clock.inputs[0])
    group.links.new(inputs.outputs['Flow Speed'], clock.inputs[1])
    scroll = group.nodes.new('ShaderNodeMath')
    scroll.operation = 'SUBTRACT'
    group.links.new(separate.outputs['X'], scroll.inputs[0])
    group.links.new(clock.outputs[0], scroll.inputs[1])
    angle_u = group.nodes.new('ShaderNodeMath')
    angle_u.operation = 'MULTIPLY'
    angle_u.inputs[1].default_value = math.tau
    group.links.new(scroll.outputs[0], angle_u.inputs[0])
    cosine = group.nodes.new('ShaderNodeMath')
    cosine.operation = 'COSINE'
    sine = group.nodes.new('ShaderNodeMath')
    sine.operation = 'SINE'
    group.links.new(angle_u.outputs[0], cosine.inputs[0])
    group.links.new(angle_u.outputs[0], sine.inputs[0])
    vector = group.nodes.new('ShaderNodeCombineXYZ')
    group.links.new(cosine.outputs[0], vector.inputs['X'])
    group.links.new(sine.outputs[0], vector.inputs['Y'])
    group.links.new(density.outputs[0], vector.inputs['Z'])
    noise = group.nodes.new('ShaderNodeTexNoise')
    noise.noise_dimensions = '3D'
    noise.inputs['Scale'].default_value = 3.0
    noise.inputs['Detail'].default_value = 2.0
    group.links.new(vector.outputs[0], noise.inputs['Vector'])
    threshold = group.nodes.new('ShaderNodeMapRange')
    threshold.interpolation_type = 'SMOOTHSTEP'
    threshold.inputs['From Min'].default_value = 0.48
    threshold.inputs['From Max'].default_value = 0.72
    group.links.new(noise.outputs['Fac'], threshold.inputs['Value'])
    angle = group.nodes.new('ShaderNodeMath')
    angle.operation = 'MULTIPLY'
    angle.inputs[1].default_value = math.pi
    group.links.new(separate.outputs['Y'], angle.inputs[0])
    edge = group.nodes.new('ShaderNodeMath')
    edge.operation = 'SINE'
    edge.use_clamp = True
    group.links.new(angle.outputs[0], edge.inputs[0])
    shape = group.nodes.new('ShaderNodeMath')
    shape.operation = 'MULTIPLY'
    group.links.new(edge.outputs[0], shape.inputs[0])
    group.links.new(threshold.outputs[0], shape.inputs[1])
    coverage = group.nodes.new('ShaderNodeMath')
    coverage.operation = 'MULTIPLY'
    coverage.use_clamp = True
    group.links.new(shape.outputs[0], coverage.inputs[0])
    group.links.new(inputs.outputs['Opacity'], coverage.inputs[1])
    emission = group.nodes.new('ShaderNodeEmission')
    group.links.new(inputs.outputs['Flow Color'], emission.inputs['Color'])
    group.links.new(inputs.outputs['Glow'], emission.inputs['Strength'])
    transparent = group.nodes.new('ShaderNodeBsdfTransparent')
    mix = group.nodes.new('ShaderNodeMixShader')
    group.links.new(coverage.outputs[0], mix.inputs[0])
    group.links.new(transparent.outputs[0], mix.inputs[1])
    group.links.new(emission.outputs[0], mix.inputs[2])
    output = group.nodes.new('NodeGroupOutput')
    group.links.new(mix.outputs[0], output.inputs['Surface'])
    controls = tree.nodes.new('ShaderNodeGroup')
    controls.node_tree = group
    controls.name = controls.label = 'Editable Flow | Artistic Approximation'
    controls.inputs['Flow Speed'].default_value = speed
    controls.inputs['Streak Density'].default_value = streak_density
    controls.inputs['Opacity'].default_value = opacity
    controls.inputs['Glow'].default_value = intensity
    controls.inputs['Flow Color'].default_value = (*color, 1.0)
    uv = tree.nodes.new('ShaderNodeUVMap')
    uv.uv_map = uv_map
    tree.links.new(uv.outputs[0], controls.inputs['UV'])
    tree.links.new(time.outputs[0], controls.inputs['Seconds'])
    surface = tree.nodes.new('ShaderNodeOutputMaterial')
    tree.links.new(controls.outputs['Surface'], surface.inputs['Surface'])
    layout_shader_node_tree(group)
    layout_shader_node_tree(tree)
    animate_effect_material_time(material, bpy.context.scene)
    return material


def create_flow_ribbon(name: str, center: tuple[float, float, float],
                        radii: tuple[float, float], width: float,
                        height_wave: float, phase: float, segments: int) -> bpy.types.Object:
    """Create an explicitly authored closed ribbon; no source runtime is inferred."""
    if (not name or segments < 16 or min(radii) <= 0 or width <= 0
            or not all(math.isfinite(value) for value in (*center, *radii, width, height_wave, phase))):
        raise ValueError('Flow ribbon requires finite dimensions, positive radii/width and at least16segments')
    vertices: list[tuple[float, float, float]] = []
    for index in range(segments):
        angle = math.tau * index / segments
        for side in (-0.5, 0.5):
            vertices.append((center[0] + radii[0] * math.cos(angle),
                             center[1] + radii[1] * math.sin(angle),
                             center[2] + height_wave * math.sin(angle * 2 + phase) + side * width))
    faces = [(2 * index, 2 * ((index + 1) % segments), 2 * ((index + 1) % segments) + 1, 2 * index + 1)
             for index in range(segments)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    uv = mesh.uv_layers.new(name='UVMap')
    for index, face in enumerate(mesh.polygons):
        coords = ((index / segments, 0), ((index + 1) / segments, 0),
                  ((index + 1) / segments, 1), (index / segments, 1))
        for loop, coords_uv in zip(face.loop_indices, coords):
            uv.data[loop].uv = coords_uv
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj['endfield_flow_preview_scope'] = 'authored_closed_ribbon_not_recovered_source_geometry'
    obj['endfield_flow_center'] = center
    obj['endfield_flow_radii'] = radii
    return obj


def apply_flow_presentation(scene: bpy.types.Scene, recipe: FlowPresentation) -> FlowPresentationAudit:
    """Add an opt-in recipe to its prepared selection without replacing source effects."""
    validated = read_flow_presentation(recipe)
    collection_name = "Endfield_Authored_Flow_Presentation"
    if scene != bpy.context.scene:
        raise ValueError("Flow presentation requires the active export scene")
    collisions = [row["name"] for row in validated["ribbons"] if row["name"] in bpy.data.objects]
    if collection_name in bpy.data.collections or collisions:
        raise ValueError(f"Flow presentation already exists or object names collide: {collisions}")
    collection = bpy.data.collections.new(collection_name)
    scene.collection.children.link(collection)
    collection["endfield_flow_recipe_json"] = json.dumps(validated, sort_keys=True)
    collection["endfield_fx_status"] = "explicit_authored_approximation"
    material = create_flow_preview_material("UVMap", validated["color"], validated["speed"],
        validated["opacity"], validated["intensity"], validated["streak_density"])
    material["endfield_flow_provenance"] = validated["provenance"]
    names: list[str] = []
    for row in validated["ribbons"]:
        obj = create_flow_ribbon(row["name"], row["center"], row["radii"], row["width"],
                                row["height_wave"], row["phase"], row["segments"])
        collection.objects.link(obj)
        scene.collection.objects.unlink(obj)
        obj.data.materials.append(material)
        names.append(obj.name)
    return {"status": "explicit_authored_approximation", "collection": collection.name,
            "objects": names, "material": material.name, "provenance": validated["provenance"],
            "limitations": "Authored continuous flow; source activation and geometry are not reconstructed. "
                           "Recipe belongs to this prepared selection and is not automatically copied to chunks."}
