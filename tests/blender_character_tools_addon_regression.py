"""Install the real extension and verify file switching, ownership and teardown."""
import argparse
from array import array
import hashlib
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType
import zipfile

import bpy


def scene_signature() -> str:
    """Hash authored data that registering or removing UI must never modify."""
    digest = hashlib.sha256()
    for obj in sorted(bpy.data.objects, key=lambda item: item.name):
        digest.update(repr((obj.name, obj.type, tuple(obj.keys()))).encode())
        if obj.type == 'ARMATURE':
            for bone in obj.data.bones:
                digest.update(repr((bone.name, bone.parent.name if bone.parent else None,
                                    tuple(tuple(row) for row in bone.matrix_local), bone.use_deform)).encode())
        if obj.type == 'MESH':
            values = array('f', [0.0]) * (len(obj.data.vertices) * 3)
            obj.data.vertices.foreach_get('co', values)
            digest.update(values.tobytes())
        for key in sorted(obj.keys()):
            value = obj[key]
            if isinstance(value, (int, float, bool, str)):
                digest.update(repr((key, value)).encode())
    for action in sorted(bpy.data.actions, key=lambda item: item.name):
        digest.update(action.name.encode())
        for curve in action.fcurves:
            digest.update(repr((curve.data_path, curve.array_index,
                                tuple(tuple(point.co) for point in curve.keyframe_points))).encode())
    for tree in sorted(bpy.data.node_groups, key=lambda item: item.name):
        for node in tree.nodes:
            if node.type == 'VALUE':
                digest.update(repr((tree.name, node.name, node.outputs[0].default_value)).encode())
    return digest.hexdigest()


def assert_installed(module: ModuleType) -> None:
    """Check actual RNA registrations and exactly one persistent load callback."""
    for cls in module.SHARED_CLASSES + module.ADDON_CLASSES:
        if not cls.is_registered:
            raise AssertionError(f'Installed extension lost its registered class: {cls.__name__}')
        if not issubclass(cls, bpy.types.AddonPreferences) and getattr(bpy.types, cls.__name__, None) is not cls:
            raise AssertionError(f'Another script replaced an installed UI class: {cls.__name__}')
    if bpy.app.handlers.load_post.count(module.restore_panels_after_load) != 1:
        raise AssertionError('Extension has a missing or duplicated file-load callback')


def check_controls(module: ModuleType) -> dict[str, int]:
    """Exercise real rig ownership, expression reset, IK selection and weather lookup."""
    rigs = [obj for obj in bpy.data.objects if obj.type == 'ARMATURE']
    if len(rigs) != 1:
        raise AssertionError(f'Fixture must contain exactly one authored armature: {len(rigs)}')
    rig = rigs[0]
    panel = module.character_rig_panel
    if panel.resolve_rig_for_active_object(rig) != rig:
        raise AssertionError('Extension failed to resolve the current file armature')
    controls = panel._controller_controls_for_rig(rig)
    basic = next(item for item in controls if item['kind'] == 'basic')
    controller = basic['controller']
    previous = {name: controller[name] for name in basic['properties']}
    controller[basic['properties'][0]] = 0.75
    result = bpy.ops.endfield.reset_expression_controller(
        armature_name=rig.name, controller_name=controller.name)
    if result != {'FINISHED'} or any(controller[name] != 0.0 for name in previous):
        raise AssertionError('Installed expression operator did not reset the actual controller')
    for name, value in previous.items():
        controller[name] = value
    states = panel._ik_limb_states(rig)
    for limb, state in states.items():
        role = 'EndOutput' if 'orientation' in state else 'Target'
        result = bpy.ops.endfield.select_ik_control(rig_name=rig.name, limb=limb, control=role)
        if result != {'FINISHED'}:
            raise AssertionError(f'Installed IK selection failed: {limb}/{role}')
    weather = sum(bool(module.character_weather_panel.weather_groups(material))
                  for material in bpy.data.materials)
    if weather == 0:
        raise AssertionError('Actual fixture weather material controls were not found')
    return {'controllers': len(controls), 'ik_limbs': len(states), 'weather_materials': weather}


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sample', type=Path, required=True)
    parser.add_argument('--second-sample', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(arguments)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.context.preferences.filepaths.use_scripts_auto_execute = False
    bpy.ops.wm.open_mainfile(filepath=str(args.sample.resolve()), use_scripts=False)
    baseline = scene_signature()
    repository = output / 'extensions'
    repository.mkdir(exist_ok=True)
    repo = bpy.context.preferences.extensions.repos.new(
        name='Endfield offline integration test', module='endfield_test',
        custom_directory=str(repository), remote_url='')
    result = bpy.ops.extensions.package_install_files(
        repo=repo.module, filepath=str(args.archive.resolve()), enable_on_install=True)
    if result != {'FINISHED'}:
        raise AssertionError(f'Blender extension installation failed: {result}')
    module_name = 'bl_ext.endfield_test.endfield_character_tools'
    module = importlib.import_module(module_name)
    assert_installed(module)
    if scene_signature() != baseline:
        raise AssertionError('Installing the extension changed authored scene data')
    module.register()
    assert_installed(module)
    first_controls = check_controls(module)

    # Exercise the pre-plugin embedded scripts from the accepted real fixture.
    for text_name in ('Endfield Rig IK.py', 'Endfield Character Rig Panel.py', 'Endfield Weather Panel.py'):
        exec(compile(bpy.data.texts[text_name].as_string(), text_name, 'exec'), {'__name__': '__main__'})
    bpy.ops.endfield.refresh_character_tools()
    assert_installed(module)
    if scene_signature() != baseline:
        raise AssertionError('Restoring installed UI after legacy scripts changed authored data')

    # New embedded scripts must defer to the installed extension immediately.
    with zipfile.ZipFile(args.archive) as archive:
        for name in ('character_rig_ik.py', 'character_rig_panel.py', 'character_weather_panel.py'):
            source = archive.read(name).decode('utf-8') + '\nregister()\n'
            exec(compile(source, name, 'exec'), {'__name__': '__main__'})
            assert_installed(module)

    bpy.context.preferences.filepaths.use_scripts_auto_execute = True
    bpy.ops.wm.open_mainfile(filepath=str(args.sample.resolve()), use_scripts=True)
    assert_installed(module)
    bpy.context.preferences.filepaths.use_scripts_auto_execute = False
    if scene_signature() != baseline:
        raise AssertionError('Opening legacy auto-run file changed authored data')
    bpy.ops.wm.open_mainfile(filepath=str(args.second_sample.resolve()), use_scripts=False)
    assert_installed(module)
    second_baseline = scene_signature()
    second_controls = check_controls(module)
    for text in tuple(bpy.data.texts):
        if text.name in ('Endfield Rig IK.py', 'Endfield Character Rig Panel.py', 'Endfield Weather Panel.py'):
            bpy.data.texts.remove(text)
    portable = output / 'without_embedded_ui.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(portable))
    bpy.ops.wm.open_mainfile(filepath=str(portable), use_scripts=False)
    assert_installed(module)
    if scene_signature() != second_baseline:
        raise AssertionError('File switching or removing optional UI text changed model data')
    check_controls(module)
    bpy.ops.preferences.addon_disable(module=module_name)
    for cls in module.SHARED_CLASSES + module.ADDON_CLASSES:
        if cls.is_registered or getattr(bpy.types, cls.__name__, None) is not None:
            raise AssertionError(f'Disabling the extension leaked a registered class: {cls.__name__}')
    if module.restore_panels_after_load in bpy.app.handlers.load_post:
        raise AssertionError('Disabling the extension left a persistent load handler')
    if scene_signature() != second_baseline:
        raise AssertionError('Disabling the extension modified authored data')
    bpy.ops.preferences.addon_enable(module=module_name)
    assert_installed(module)
    bpy.ops.wm.read_homefile(use_empty=True)
    assert_installed(module)
    if module.character_rig_panel.ENDFIELD_PT_character_rig_panel.poll(bpy.context):
        raise AssertionError('Empty files must not pretend to contain an authored character')
    if bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise AssertionError('Extension changed the disabled auto-run preference')
    post = module.character_postprocess_panel.post
    if post.stage_nodes(bpy.context.scene):
        raise AssertionError('Registration must not enable postprocessing in empty files')
    bpy.ops.endfield.post_install()
    stage = post.find_group(bpy.context.scene)
    stage.inputs['Exposure EV'].default_value = 0.25
    bpy.ops.endfield.post_bypass()
    if not post.is_bypassed(bpy.context.scene):
        raise AssertionError('Installed bypass operator did not mute the stage')
    bpy.ops.endfield.post_resume()
    preset = output / 'postprocess_preset.json'
    bpy.ops.endfield.post_export(filepath=str(preset))
    bpy.ops.endfield.post_reset()
    bpy.ops.endfield.post_import(filepath=str(preset))
    if abs(stage.inputs['Exposure EV'].default_value - 0.25) > 1e-6:
        raise AssertionError('Preset operators failed the live round trip')
    bpy.ops.preferences.addon_disable(module=module_name)
    if post.find_group(bpy.context.scene) is None:
        raise AssertionError('Disabling the extension removed native compositor data')
    bpy.ops.preferences.addon_enable(module=module_name)
    assert_installed(module)
    bpy.ops.endfield.post_remove()
    if post.stage_nodes(bpy.context.scene):
        raise AssertionError('Remove operator left an owned stage behind')
    report = {'status': 'passed', 'blender': bpy.app.version_string,
              'archive': str(args.archive.resolve()), 'first_file': first_controls,
              'second_file': second_controls, 'real_offline_install': True,
              'embedded_ui_absent_reopen': True, 'legacy_autorun_file': True,
              'legacy_manual_ui_refresh': True, 'new_embedded_script_coexistence': True,
              'registration_idempotent': True, 'disable_reenable': True,
              'authored_data_unchanged': True, 'empty_file_switch': True,
              'native_postprocess_after_disable': True, 'postprocess_operators': True}
    (output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--')+1:])
