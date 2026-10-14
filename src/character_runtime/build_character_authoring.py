"""Add editable source authoring data to an accepted material sample in one save."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from character_authoring import apply_authoring, copy_original_table, read_authoring_request, record, text, verify_authoring


def main(request_path: Path) -> None:
    started = time.perf_counter()
    request = record(json.loads(request_path.read_text(encoding='utf-8-sig')), 'build request')
    if request['format'] != 'EndfieldCharacterAuthoringBuild/1':
        raise ValueError('Unsupported authoring build request format')
    def source(key: str) -> Path:
        return (request_path.parent / text(request[key], key)).resolve(strict=True)
    blend = source('source_blend')
    authoring_path = source('authoring')
    raw_cache_root = request.get('cache_root')
    cache_root = None if raw_cache_root is None else (
        request_path.parent / text(raw_cache_root, 'cache_root')).resolve()
    sys.path.insert(0, str(source('repository_src')))
    output = (request_path.parent / text(request['output'], 'output')).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_hash = hashlib.sha256(blend.read_bytes()).hexdigest()
    bpy.ops.wm.open_mainfile(filepath=str(blend), use_scripts=False, load_ui=False)
    bpy.context.preferences.filepaths.save_version = 0
    authoring_document = record(json.loads(authoring_path.read_text(encoding='utf-8-sig')), 'authoring document')
    if 'native_expression_replay_template' in authoring_document:
        from character_native_expression_template import materialize_expression_authoring
        template = (authoring_path.parent / text(authoring_document['native_expression_replay_template'], 'expression template')).resolve(strict=True)
        authoring_path = materialize_expression_authoring(template, authoring_path, blend)
    # Preserve source material trees that Blender would purge on a second save.
    retained_materials = tuple(material.name for material in bpy.data.materials if material.users == 0)
    for name in retained_materials:
        bpy.data.materials[name].use_fake_user = True
    authoring_request = read_authoring_request(authoring_path)
    report = apply_authoring(authoring_request, cache_root)
    report['retained_unused_source_materials'] = retained_materials
    report['original_table'] = copy_original_table(authoring_request, output)
    from rebuild_character_materials import configure_render_view
    visible = tuple(bpy.data.objects[name] for name in report['source_meshes']
                    if not bpy.data.objects[name].hide_render and not bpy.data.objects[name].hide_get())
    scene = bpy.context.scene
    corners = []
    for frame in sorted({scene.frame_start, (scene.frame_start + scene.frame_end)//2, scene.frame_end}):
        scene.frame_set(frame)
        graph = bpy.context.evaluated_depsgraph_get()
        for obj in visible:
            evaluated = obj.evaluated_get(graph)
            corners.extend(evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box)
    scene.frame_set(scene.frame_start)
    low = Vector(tuple(min(point[axis] for point in corners) for axis in range(3)))
    high = Vector(tuple(max(point[axis] for point in corners) for axis in range(3)))
    extent = max((high.x-low.x)*1.15, (high.z-low.z)*(480/720)*1.12)/1.18
    configure_render_view('BLENDER_EEVEE_NEXT', Vector((.08, -1., .02)), (low+high)*.5, extent, 480, 720, 16)
    controller_names = []
    if report['basic_expressions'] is not None:
        controller_names.append(report['basic_expressions']['controller']['object_name'])
    controller_names.extend(row['controller']['object_name'] for row in report['independent_expression_tables'])
    material_controls = report.get('material_expression_controls')
    if material_controls is not None and material_controls['status'] == 'attached':
        controller_names.append(material_controls['controller'])
    if controller_names:
        controller = bpy.data.objects[controller_names[0]]
        bpy.ops.object.select_all(action='DESELECT')
        controller.select_set(True)
        bpy.context.view_layer.objects.active = controller
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.spaces.active.region_3d.view_perspective = 'CAMERA'
            elif area.type == 'PROPERTIES' and controller_names:
                area.spaces.active.context = 'OBJECT'
    help_text = bpy.data.texts.new('Expression and Action controls')
    basic_name = report['basic_expressions']['controller']['object_name'] if report['basic_expressions'] else '(not requested)'
    independent_names = tuple(row['controller']['object_name'] for row in report['independent_expression_tables'])
    help_text.write('Body animation: play the timeline or edit the armature Action.\n'
                    f'Basic expressions: select {basic_name} and edit its custom properties (0 to 1).\n'
                    f'Independent source-table endpoints: {", ".join(independent_names) if independent_names else "(not requested)"}.\n'
                    'The properties drive baked shape keys on affected meshes; defaults are neutral.\n'
                    'Original bones and pre-existing shape keys are retained.\n'
                    'The optional expression_source.json holds the original source table.\n'
                    'Explicit native expression replays are separate full-pose snapshot Actions; their receipts identify matching corrective Actions.\n'
                    'Pose endpoints do not verify native runtime morph mixing.\n')
    if material_controls is not None and material_controls['status'] == 'attached':
        help_text.write(f"Source material expressions: select {material_controls['controller']} and edit _EmotionIndex (0 to 3) / _EmotionBlend (0 to 1).\n"
                        'These alter the source emotion atlas color only; tile meanings are not inferred.\n')
    target = output / 'character_authoring.blend'
    report['blend'] = str(target)
    report['source_blend'] = str(blend)
    report['source_sha256'] = source_hash
    report['authoring_request_sha256'] = hashlib.sha256(authoring_path.read_bytes()).hexdigest()
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    report['blend_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
    report['bytes'] = target.stat().st_size
    report['build_seconds'] = time.perf_counter()-started
    (output/'build_receipt.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    bpy.ops.wm.open_mainfile(filepath=str(target), use_scripts=False, load_ui=False)
    audit = verify_authoring(report)
    if hashlib.sha256(blend.read_bytes()).hexdigest() != source_hash:
        raise AssertionError('Read-only input character was modified')
    audit.update({'scripts_enabled_on_reopen': False, 'save_count': 1,
                  'completed_seconds': time.perf_counter()-started,
                  'blend_sha256': report['blend_sha256']})
    (output/'reopen_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({'status': 'exported_reopened', 'blend': str(target), 'seconds': audit['completed_seconds']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    main(parser.parse_args(sys.argv[sys.argv.index('--')+1:]).request.resolve(strict=True))
