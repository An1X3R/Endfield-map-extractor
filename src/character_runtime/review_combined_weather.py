"""Render and reopen explicit weather states without modifying source geometry."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import bpy
from mathutils import Vector
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_character_materials import geometry_digest
from rebuild_character_materials import render_view


def control(label: str) -> bpy.types.NodeSocket:
    matches = [item.outputs[0] for tree in bpy.data.node_groups for item in tree.nodes if item.type == 'VALUE' and item.label == label]
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one weather control: label={label}, count={len(matches)}')
    return matches[0]


def apply_state(rain: float, snow: float) -> None:
    control('User Override Weight').default_value = 1.0
    control('User Water Height').default_value = -10.0
    for axis, value in zip('XYZW', (rain, 0.0, 0.0, 0.0)):
        control('User Effect ' + axis).default_value = value
    control('User Snow Intensity 0-1').default_value = snow


def main(source: Path, output: Path) -> None:
    if output.exists(): raise FileExistsError(output)
    bpy.ops.wm.open_mainfile(filepath=str(source))
    objects = tuple(obj for obj in bpy.data.objects if obj.type == 'MESH' and 'source_renderer_identity' in obj)
    names = tuple(obj.name for obj in objects)
    original = geometry_digest(names)
    points = [obj.matrix_world @ Vector(corner) for obj in objects if not obj.hide_render for corner in obj.bound_box]
    minimum = Vector(tuple(min(point[axis] for point in points) for axis in range(3)))
    maximum = Vector(tuple(max(point[axis] for point in points) for axis in range(3)))
    center = (minimum + maximum) / 2
    extent = max(maximum - minimum)
    close_center = center.copy(); close_center.z = minimum.z + (maximum.z - minimum.z) * 0.74
    output.mkdir(parents=True)
    rendered = []
    for state, rain, snow in (('dry', 0.0, 0.0), ('rain', 1.0, 0.0), ('snow', 0.0, 1.0)):
        apply_state(rain, snow)
        for engine, suffix in (('BLENDER_EEVEE_NEXT', 'eevee'), ('CYCLES', 'cycles')):
            rendered.append(render_view(output, state + '_' + suffix + '.png', engine, Vector((0, -1, 0.05)), close_center, extent * 0.53, 512, 512, 32))
    apply_state(0.0, 1.0)
    rendered.append(render_view(output, 'full_body_snow_eevee.png', 'BLENDER_EEVEE_NEXT', Vector((0,-1,0.05)), center, extent, 512, 768, 32))
    bpy.data.texts.new('EDITABLE WEATHER CONTROLS.txt').write('Snow: EF Native Snow State - EXPLICIT USER OVERRIDE / User Snow Intensity 0-1.\nRain: EF Weather Uniforms - Explicit Provenance / User Override Weight = 1, User Effect X = 0..1.\nWaterline: User Effect Y = 1, User Water Height in meters.\nSaved state: snow 1, rain 0. Projection inputs are an explicit static approximation, not recovered runtime streams. Full HGRP and animated droplets remain incomplete.\n')
    target = output / 'character_weather_hgrp.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    bpy.ops.wm.open_mainfile(filepath=str(target))
    if geometry_digest(names) != original: raise AssertionError('Geometry, rig, weights, UV or visibility changed')
    if control('User Snow Intensity 0-1').default_value != 1.0 or control('User Effect X').default_value != 0.0: raise AssertionError('Weather controls changed after reopening')
    report = {'status':'rendered_and_reopened', 'source':str(source), 'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(), 'output':str(target), 'sha256':hashlib.sha256(target.read_bytes()).hexdigest(), 'geometry_preserved':True, 'renderer_count':len(names), 'renders':rendered, 'full_hgrp_reconstructed':False, 'native_projection_binding_verified':False, 'saved_state':{'rain':0, 'snow':1}, 'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (output / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    arguments = sys.argv[sys.argv.index('--')+1:]
    if len(arguments) != 2: raise ValueError('Expected input blend and new output directory')
    main(*(Path(value) for value in arguments))
