"""Connect verified local diffuse equations to character groups with explicit state.

This research bridge retains existing ramp coordinates and PBR highlights. It
does not implement the engine shadow atlas, SDF, or runtime light selection.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import hashlib

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from character_hgrp_diffuse import build_hgrp_diffuse
from character_node_graph import node, math_node, color_mix
from character_npr_nodes import shadow_color, sample_image
from rebuild_character_materials import render_view


def by_label(tree: bpy.types.ShaderNodeTree, label: str) -> bpy.types.Node:
    matches = [item for item in tree.nodes if item.label == label]
    if len(matches) != 1:
        raise ValueError(f'Expected exact source graph node: tree={tree.name}, label={label}, matches={len(matches)}')
    return matches[0]


def source_keywords(tree: bpy.types.ShaderNodeTree) -> tuple[str, ...]:
    identity = json.loads(tree['source_identity'])
    records = [json.loads(text.as_string()) for text in bpy.data.texts if text.name.startswith('SOURCE MATERIAL ')]
    matched = [record for record in records if (record['object']['source'].lower(), record['object']['path_id']) == (identity[0].lower(), identity[1])]
    if len(matched) != 1:
        raise ValueError(f'Expected exact material identity evidence: {identity}')
    name = matched[0]['object']['name']
    native = [json.loads(text.as_string()) for text in bpy.data.texts if text.name.startswith('SOURCE TYPETREE ')]
    variants = {tuple(record['m_ValidKeywords']) for record in native if record['m_Name'] == name}
    if len(variants) != 1:
        raise ValueError(f'Material keyword evidence is absent or ambiguous: {identity}')
    return next(iter(variants))


def build_profile_state(profile: dict[str, object]) -> bpy.types.ShaderNodeTree:
    """Construct explicit constructor and preview state from profile inputs."""
    state = bpy.data.node_groups.new('EF HGRP Constructor State + Preview Environment', 'ShaderNodeTree')
    state_output = node(state, 'NodeGroupOutput', 'Explicit state outputs', 'HGRP State')
    for label, spec in profile['inputs'].items():
        kind, value = spec['kind'], spec['value']
        state.interface.new_socket(name=label, in_out='OUTPUT', socket_type=kind)
        if kind == 'NodeSocketColor':
            control = node(state, 'ShaderNodeRGB', label, 'Explicit Source State'); control.outputs[0].default_value = value
        elif kind == 'NodeSocketVector':
            control = node(state, 'ShaderNodeCombineXYZ', label, 'Explicit Source State')
            for axis, component in enumerate(value): control.inputs[axis].default_value = component
        elif kind == 'NodeSocketFloat':
            control = node(state, 'ShaderNodeValue', label, 'Explicit Source State'); control.outputs[0].default_value = value
        else:
            raise ValueError(f'Unsupported state socket kind: {kind}')
        state.links.new(control.outputs[0], state_output.inputs[label])
    return state


def attach_diffuse(tree: bpy.types.ShaderNodeTree, state: bpy.types.ShaderNodeTree, diffuse: bpy.types.ShaderNodeTree) -> None:
    shader = tree['source_shader']
    floats = json.loads(tree['source_floats'])
    bsdf_nodes = [item for item in tree.nodes if item.type == 'BSDF_PRINCIPLED']
    if len(bsdf_nodes) != 1:
        raise ValueError(f'Expected one source Principled baseline: {tree.name}')
    bsdf = bsdf_nodes[0]
    albedo = bsdf.inputs['Base Color'].links[0].from_socket
    metallic = bsdf.inputs['Metallic'].links[0].from_socket
    section = 'HGRP Local Diffuse Bridge'
    shared = node(tree, 'ShaderNodeGroup', 'Explicit HGRP state and preview inputs', section); shared.node_tree = state
    native = node(tree, 'ShaderNodeGroup', 'Verified local HGRP diffuse equations', section); native.node_tree = diffuse
    for socket in shared.outputs:
        if socket.name in native.inputs:
            tree.links.new(socket, native.inputs[socket.name])
    if bsdf.inputs['Normal'].is_linked:
        normalized = node(tree, 'ShaderNodeVectorMath', 'Normalize final weather surface normal', section)
        normalized.operation = 'NORMALIZE'
        tree.links.new(bsdf.inputs['Normal'].links[0].from_socket, normalized.inputs[0])
        normal = normalized.outputs[0]
    else:
        normal = by_label(tree, 'Normalize evaluated surface normal').outputs[0]
    hemisphere = normal
    if shader == 'HGRP/CharacterNPR_Skin':
        components = node(tree, 'ShaderNodeSeparateXYZ', 'Skin world normal components', section)
        tree.links.new(normal, components.inputs[0])
        flat = node(tree, 'ShaderNodeCombineXYZ', 'Skin horizontal normal - SDF blend pending', section)
        tree.links.new(components.outputs['X'], flat.inputs['X']); tree.links.new(components.outputs['Y'], flat.inputs['Y'])
        flat.inputs['Z'].default_value = 6.103515625e-5
        unit = node(tree, 'ShaderNodeVectorMath', 'Skin normalized horizontal hemisphere', section); unit.operation = 'NORMALIZE'
        tree.links.new(flat.outputs[0], unit.inputs[0]); hemisphere = unit.outputs[0]
    tree.links.new(hemisphere, native.inputs['Hemisphere Normal'])
    samples = {item['source_slot']: item for item in tree.nodes if item.type == 'TEX_IMAGE' and 'source_slot' in item}
    if shader == 'HGRP/CharacterNPR_Hair':
        reflectivity = 0.96
    else:
        reflectivity = math_node(tree, 'MULTIPLY', math_node(tree, 'SUBTRACT', 1.0, metallic, 'One minus metallic', section), 0.96, 'Dielectric diffuse coefficient', section)
    diffuse_coefficient = (reflectivity, reflectivity, reflectivity, 1.0) if isinstance(reflectivity, float) else reflectivity
    diffuse_color = color_mix(tree, 'MULTIPLY', 1.0, albedo, diffuse_coefficient, 'Source diffuse albedo coefficient', section)
    old_light = by_label(tree, 'Artist global state - not game constants')
    snow = [item for item in tree.nodes if item.type == 'GROUP' and item.label in ('Native Skin Snow / Experimental Projection', 'Native Standard Snow / Experimental Projection')]
    if len(snow) > 1: raise ValueError('Multiple weather kernels on one source material')
    wet = [item for item in tree.nodes if item.type == 'GROUP' and item.label == 'Native Standard Wet Film / Experimental Projection']
    if len(wet) > 1: raise ValueError('Multiple wet film kernels on one source material')
    if snow:
        shadow = snow[0].outputs['Shadow Color']
    elif wet:
        shadow = wet[0].outputs['Shadow Color']
    else:
        shadow = shadow_color(tree, albedo, floats, samples, old_light)
    shadow_scale = math_node(tree, 'MULTIPLY', reflectivity, shared.outputs['CP0 Z Ambient Floor'], 'Source shadow albedo scale', section)
    shadow = color_mix(tree, 'MULTIPLY', 1.0, shadow, shadow_scale, 'Scaled source shadow albedo', section)
    tree.links.new(diffuse_color, native.inputs['Diffuse Color']); tree.links.new(shadow, native.inputs['Scaled Shadow Albedo'])
    if floats['_UseDiffRampMap'] == 1.0:
        ramp = by_label(tree, 'Source diffuse ramp RGB and alpha')
        ramp_rgb, ramp_alpha = ramp.outputs['Color'], ramp.outputs['Alpha']
    else:
        ramp = by_label(tree, 'Source procedural ramp branch')
        ramp_rgb = ramp_alpha = ramp.outputs['Result']
    tree.links.new(ramp_rgb, native.inputs['Ramp Color']); tree.links.new(ramp_alpha, native.inputs['Ramp Alpha'])
    if shader == 'HGRP/CharacterNPR_Skin':
        occlusion = samples['_BaseMap'].outputs['Alpha']
        shadow_a = math_node(tree, 'MULTIPLY', occlusion, shared.outputs['Preview Atlas Visibility'], 'Skin source alpha times atlas visibility', section)
    else:
        packed = [item for item in tree.nodes if item.label == 'R metallic / G specular / B AO / A smoothness']
        occlusion = packed[0].outputs['Blue'] if packed else 1.0
        camera = node(tree, 'ShaderNodeVectorTransform', 'Preview camera facing direction', section)
        camera.vector_type = 'VECTOR'; camera.convert_from = 'CAMERA'; camera.convert_to = 'WORLD'
        camera.inputs[0].default_value = (0, 0, 1)
        dot = node(tree, 'ShaderNodeVectorMath', 'Preview normal dot camera facing', section); dot.operation = 'DOT_PRODUCT'
        tree.links.new(normal, dot.inputs[0]); tree.links.new(camera.outputs[0], dot.inputs[1])
        if floats['_UseDiffRampMap'] == 1.0:
            u = math_node(tree, 'ADD', math_node(tree, 'MULTIPLY', dot.outputs['Value'], 0.5, 'View ramp half scale', section), 0.5, 'View ramp U', section)
            view_alpha = sample_image(tree, samples['_DiffRampMap'].image, u, 0.5, 'Source view ramp with Blender camera bridge', section).outputs['Alpha']
        else:
            view_ramp = node(tree, 'ShaderNodeMapRange', 'Source procedural view ramp', section)
            view_ramp.interpolation_type = 'SMOOTHSTEP'; view_ramp.clamp = True
            view_ramp.inputs['From Min'].default_value = 0.25; view_ramp.inputs['From Max'].default_value = 1.0
            tree.links.new(dot.outputs['Value'], view_ramp.inputs['Value']); view_alpha = view_ramp.outputs['Result']
        shadow_a = math_node(tree, 'MULTIPLY', view_alpha, math_node(tree, 'MULTIPLY', occlusion, shared.outputs['Preview Atlas Visibility'], 'Source AO times atlas visibility', section), 'Standard view alpha times occlusion', section)
    shadow_b = math_node(tree, 'MINIMUM', math_node(tree, 'MINIMUM', shared.outputs['Preview Atlas Visibility'], occlusion, 'Atlas and source occlusion minimum', section), ramp_alpha, 'Source Shadow B including ramp', section)
    tree.links.new(shadow_a, native.inputs['Shadow A']); tree.links.new(shadow_b, native.inputs['Shadow B'])
    # Remove the earlier AO color multiplier: the local diffuse now consumes AO explicitly.
    old_ao = [item for item in tree.nodes if item.label == 'AO applied to diffuse - Blender approximation']
    for item in old_ao:
        for link in tuple(item.inputs[2].links):
            tree.links.remove(link)
        item.inputs[2].default_value = (1, 1, 1, 1)
    result = native.outputs['Combined Diffuse']
    if bsdf.inputs['Emission Color'].is_linked:
        emission = color_mix(tree, 'MULTIPLY', 1.0, bsdf.inputs['Emission Color'].links[0].from_socket, bsdf.inputs['Emission Strength'].links[0].from_socket, 'Preserve source emission in HGRP preview', section)
        result = color_mix(tree, 'ADD', 1.0, result, emission, 'HGRP diffuse plus source emission', section)
    tree.links.new(result, by_label(tree, 'Artist-lit NPR diffuse - shared engines').inputs['Color'])
    tree['hgrp_scope'] = 'Local diffuse equations and explicit state. Existing approximate ramp coordinates/PBR highlights retained; SDF, atlas production and runtime light loop absent.'


def main(source: Path, profile_path: Path, output: Path, scene_source: Path) -> None:
    if output.exists():
        raise FileExistsError(output)
    profile = json.loads(profile_path.read_text(encoding='utf-8'))
    bpy.ops.wm.open_mainfile(filepath=str(source))
    sys.path.insert(0, str(scene_source))
    from blender_scene_materials import layout_shader_node_tree
    groups = [tree for tree in bpy.data.node_groups if tree.get('source_shader') in ('HGRP/CharacterNPR', 'HGRP/CharacterNPR_Skin')]
    skipped = [tree.name for tree in groups if '_CHARACTER_FUR' in source_keywords(tree)]
    groups = [tree for tree in groups if tree.name not in skipped]
    if not groups:
        raise ValueError('No supported source shader groups found')
    state = build_profile_state(profile)
    diffuse = build_hgrp_diffuse('EF HGRP Local Diffuse - Verified Equations')
    for tree in groups:
        attach_diffuse(tree, state, diffuse); layout_shader_node_tree(tree)
    layout_shader_node_tree(state); layout_shader_node_tree(diffuse)
    response_controls = [item for tree in bpy.data.node_groups for item in tree.nodes if item.type == 'VALUE' and item.label == 'Response Weight']
    if len(response_controls) != 1: raise ValueError('Expected one explicit NPR response weight')
    prior_response_weight = response_controls[0].outputs[0].default_value
    response_controls[0].outputs[0].default_value = profile['preview_response_weight']
    bpy.data.texts.new('HGRP INPUT STATE.json').write(json.dumps(profile, indent=2))
    output.mkdir(parents=True)
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH' and not obj.hide_render and 'source_renderer_identity' in obj]
    points = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    minimum = Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
    maximum = Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    center = (minimum + maximum) / 2; extent = max(maximum - minimum)
    renders = [render_view(output, 'front_eevee.png', 'BLENDER_EEVEE_NEXT', Vector((0, -1, 0.05)), center, extent, 384, 512, 16), render_view(output, 'front_cycles.png', 'CYCLES', Vector((0, -1, 0.05)), center, extent, 192, 256, 16)]
    blend = output/'character_hgrp.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    checked = [tree for tree in bpy.data.node_groups if 'hgrp_scope' in tree]
    if len(checked) != len(groups): raise AssertionError('HGRP material group count changed after reopening')
    report = {'status': 'local_diffuse_preview_verified', 'input_blend': str(source), 'input_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'profile': profile, 'output_blend': str(blend), 'output_sha256': hashlib.sha256(blend.read_bytes()).hexdigest(), 'skipped_fur_groups': skipped, 'connected_groups': len(checked), 'prior_response_weight': prior_response_weight, 'preview_response_weight': profile['preview_response_weight'], 'renders': renders, 'full_hgrp_reconstructed': False, 'limitations': ['No scene shadow atlas production; explicit visibility controls', 'Existing approximate ramp normals and coordinates; no facial SDF', 'PBR highlight and artist mixture retained', 'Constructor values do not equal final scene volume stack state', 'Eye and hair-specific diffuse branches unchanged']}
    (output/'verification.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    arguments = sys.argv[sys.argv.index('--') + 1:]
    if len(arguments) != 4: raise ValueError('Expected input blend, explicit state JSON, new output directory, and scene source directory')
    main(*(Path(argument) for argument in arguments))
