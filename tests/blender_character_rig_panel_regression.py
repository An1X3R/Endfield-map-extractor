"""Exercise the portable rig sidebar on an existing exported character scene."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import bpy


RUNTIME_ROOT = Path(__file__).resolve().parents[1] / 'src' / 'character_runtime'
sys.path.insert(0, str(RUNTIME_ROOT))
from character_rig_panel import (
    ControllerControls,
    IK_LIMBS,
    IK_STATE_PROPERTY,
    RIG_FORMAT,
    RIG_MARKER,
    PREVIEW_PROPERTY,
    TEXT_NAME,
    _controller_controls_for_rig,
    _ik_limb_states,
    _property_path,
    attach_rig_panel,
    resolve_rig_for_active_object,
    verify_rig_panel,
)
from character_viewport_preview import attach_viewport_preview, verify_viewport_preview
from character_rig_ik import LABELS as IK_LABELS, attach_ik_tools, verify_ik_tools


EXPORT_ROOT = Path('F:/Endfield_Map_Exports').resolve()
ActionState = tuple[int, tuple[tuple[str, int, int], ...]] | None
AnimationState = tuple[ActionState, tuple[tuple[str, int, bool], ...]] | None


def _animation_owners(action: bpy.types.Action | None, excluded: bpy.types.Object) -> tuple[str, ...]:
    if action is None:
        return ()
    return tuple(sorted(obj.name for obj in bpy.data.objects
                        if obj != excluded and obj.animation_data is not None
                        and obj.animation_data.action == action))


def _unrelated_animation_snapshot(excluded: bpy.types.Object) -> dict[str, AnimationState]:
    snapshot: dict[str, AnimationState] = {}
    for obj in bpy.data.objects:
        if obj == excluded:
            continue
        animation = obj.animation_data
        if animation is None:
            snapshot[obj.name] = None
            continue
        action = animation.action
        action_state: ActionState = None if action is None else (
            action.as_pointer(),
            tuple((curve.data_path, curve.array_index, len(curve.keyframe_points))
                  for curve in action.fcurves),
        )
        driver_state = tuple((curve.data_path, curve.array_index, curve.mute)
                             for curve in animation.drivers)
        snapshot[obj.name] = (action_state, driver_state)
    return snapshot


def _existing_key_frames(controller: bpy.types.Object, property_names: tuple[str, ...]) -> set[int]:
    animation = controller.animation_data
    action = animation.action if animation is not None else None
    if action is None:
        return set()
    if _animation_owners(action, controller):
        raise ValueError(f'Test controller shares an Action with another object: {controller.name}, owners={_animation_owners(action, controller)}')
    paths = {_property_path(name) for name in property_names}
    return {int(round(point.co.x)) for curve in action.fcurves if curve.data_path in paths
            for point in curve.keyframe_points}


def _capture_control_values(controls: tuple[ControllerControls, ...]) -> dict[str, dict[str, float | int]]:
    captured: dict[str, dict[str, float | int]] = {}
    for item in controls:
        controller = item['controller']
        properties = item['properties']
        captured[controller.name] = {name: controller[name] for name in properties}
    return captured


def _main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args(arguments).output.resolve()
    try:
        output.relative_to(EXPORT_ROOT)
    except ValueError as error:
        raise ValueError(f'Rig-panel test output must stay under {EXPORT_ROOT}: {output}') from error
    output.mkdir(parents=True, exist_ok=True)

    rigs = tuple(obj for obj in bpy.context.scene.objects
                 if obj.type == 'ARMATURE' and obj.get(RIG_MARKER) == RIG_FORMAT)
    if len(rigs) != 1:
        raise ValueError(f'Open one real authored character sample before running this test: found {len(rigs)} marked rigs')
    rig = rigs[0]
    sample_path = bpy.data.filepath
    controls = _controller_controls_for_rig(rig)
    kinds = {item['kind'] for item in controls}
    expected_kinds = {'basic', 'independent', 'material'}
    if not expected_kinds.issubset(kinds):
        raise AssertionError(f'Real sample does not cover every expression group: present={sorted(kinds)}, required={sorted(expected_kinds)}')

    sample_values = _capture_control_values(controls)
    values_before = {controller: dict(properties) for controller, properties in sample_values.items()}
    basic = next(item for item in controls if item['kind'] == 'basic')
    basic_controller = basic['controller']
    basic_property = basic['properties'][0]
    basic_controller[basic_property] = 1.4
    values_before[basic_controller.name][basic_property] = 1.4

    material_metadata_before: dict[str, dict[str, tuple[object, object]]] = {}
    for item in controls:
        if item['kind'] != 'material':
            continue
        controller = item['controller']
        properties = item['properties']
        material_metadata_before[controller.name] = {}
        for name in properties:
            metadata = controller.id_properties_ui(name).as_dict()
            material_metadata_before[controller.name][name] = (metadata.get('min'), metadata.get('max'))

    skin = next((obj for obj in bpy.context.scene.objects if obj.type == 'MESH'
                 and any(modifier.type == 'ARMATURE' and modifier.object == rig
                         for modifier in obj.modifiers)), None)
    if skin is None:
        raise ValueError(f'Real authored rig has no directly skinned mesh for ownership resolution: {rig.name}')
    skin_name = skin.name
    if resolve_rig_for_active_object(skin) != rig:
        raise AssertionError(f'Skinned mesh did not resolve to its owning rig: mesh={skin.name}, rig={rig.name}')

    automatic_execution_before = bpy.context.preferences.filepaths.use_scripts_auto_execute
    preview_report = attach_viewport_preview(rig)
    verify_viewport_preview(preview_report)
    if PREVIEW_PROPERTY not in rig or type(rig[PREVIEW_PROPERTY]) is not bool:
        raise AssertionError(f'Real rig preview switch is missing or not Boolean: {rig.name}[{PREVIEW_PROPERTY}]')

    report = attach_rig_panel(rig)
    if verify_rig_panel(report) != 'passed':
        raise AssertionError(f'Rig panel verification did not pass: {report}')
    ik_report = attach_ik_tools()
    if verify_ik_tools(ik_report) != 'passed':
        raise AssertionError(f'IK setup operator verification did not pass: {ik_report}')
    if tuple(IK_LIMBS) != tuple(IK_LABELS.items()):
        raise AssertionError(f'Rig panel limb buttons differ from the registered IK mapping: panel={IK_LIMBS}, operator={tuple(IK_LABELS.items())}')
    setup_operator = getattr(bpy.types, 'ENDFIELD_OT_setup_character_ik', None)
    if setup_operator is None or _ik_limb_states(rig):
        raise AssertionError('Registered IK setup operator or uninstalled-limb panel state is missing')
    if IK_STATE_PROPERTY in rig:
        raise AssertionError(f'Real sample unexpectedly has existing IK state before regression setup: {rig.name}')
    bpy.ops.endfield.setup_character_ik('EXEC_DEFAULT', rig_name=rig.name, limb='LeftArm')
    ik_states = _ik_limb_states(rig)
    if set(ik_states) != {'LeftArm'}:
        raise AssertionError(f'Installed IK limb did not expose its slider and target/pole state: {ik_states}')
    if bpy.context.preferences.filepaths.use_scripts_auto_execute != automatic_execution_before:
        raise AssertionError('Rig panel attachment changed Blender global script trust')
    controls = _controller_controls_for_rig(rig)
    if _capture_control_values(controls) != values_before:
        raise AssertionError('Panel attachment changed existing expression values')
    for item in controls:
        controller = item['controller']
        properties = item['properties']
        if item['kind'] in ('basic', 'independent'):
            for name in properties:
                metadata = controller.id_properties_ui(name).as_dict()
                if metadata.get('soft_min') != 0.0 or metadata.get('soft_max') != 1.0:
                    raise AssertionError(f'Expression control soft range is missing: {controller.name}[{name}]')
        elif item['kind'] == 'material':
            for name in properties:
                metadata = controller.id_properties_ui(name).as_dict()
                if material_metadata_before[controller.name][name] != (metadata.get('min'), metadata.get('max')):
                    raise AssertionError(f'Material emotion source range changed: {controller.name}[{name}]')

    collection = next((item for item in rig.data.collections if len(item.bones) > 0), None)
    if collection is None:
        raise ValueError(f'Real authored rig has no nonempty Bone Collection: {rig.name}')
    collection_name = collection.name
    for selected in bpy.context.selected_objects:
        selected.select_set(False)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    visibility_before = collection.is_visible
    result = bpy.ops.endfield.select_rig_bone_collection('EXEC_DEFAULT',
                                                         armature_name=rig.name,
                                                         collection_name=collection.name)
    if result != {'FINISHED'} or rig.mode != 'POSE':
        raise AssertionError(f'Category selection did not enter Pose Mode: result={result}, mode={rig.mode}')
    expected_bones = {bone.name for bone in collection.bones}
    selected_bones = {bone.name for bone in rig.data.bones if bone.select}
    if selected_bones != expected_bones or not collection.is_visible:
        raise AssertionError(f'Category selection differs: selected={len(selected_bones)}, expected={len(expected_bones)}, was_visible={visibility_before}')
    bpy.ops.object.mode_set(mode='OBJECT')

    unrelated_values = {name: values for name, values in values_before.items()
                        if name != basic_controller.name}
    unrelated_animation_before = _unrelated_animation_snapshot(basic_controller)
    action = basic_controller.animation_data.action if basic_controller.animation_data is not None else None
    other_action_owners_before = _animation_owners(action, basic_controller)
    result = bpy.ops.endfield.reset_expression_controller('EXEC_DEFAULT',
                                                          armature_name=rig.name,
                                                          controller_name=basic_controller.name)
    if result != {'FINISHED'} or any(basic_controller[name] != 0.0 for name in basic['properties']):
        raise AssertionError(f'Controller reset did not zero only its existing controls: {basic_controller.name}, result={result}')
    if _animation_owners(action, basic_controller) != other_action_owners_before:
        raise AssertionError(f'Controller reset changed an unrelated Action owner: {basic_controller.name}')
    if _unrelated_animation_snapshot(basic_controller) != unrelated_animation_before:
        raise AssertionError(f'Controller reset changed unrelated object animation: {basic_controller.name}')
    for item in controls:
        controller = item['controller']
        if controller.name == basic_controller.name:
            continue
        if _capture_control_values((item,))[controller.name] != unrelated_values[controller.name]:
            raise AssertionError(f'Controller reset changed another controller: {controller.name}')

    existing_frames = _existing_key_frames(basic_controller, basic['properties'])
    test_frame = bpy.context.scene.frame_current + 100
    while test_frame in existing_frames:
        test_frame += 1
    original_frame = bpy.context.scene.frame_current
    bpy.context.scene.frame_set(test_frame)
    basic_controller[basic_property] = 0.41
    action_before_keyframe = basic_controller.animation_data.action if basic_controller.animation_data is not None else None
    other_action_owners_before = _animation_owners(action_before_keyframe, basic_controller)
    result = bpy.ops.endfield.keyframe_expression_controller('EXEC_DEFAULT',
                                                             armature_name=rig.name,
                                                             controller_name=basic_controller.name)
    if result != {'FINISHED'} or basic_controller.animation_data is None:
        raise AssertionError(f'Controller keyframe operation failed: {basic_controller.name}, result={result}')
    if _animation_owners(action_before_keyframe, basic_controller) != other_action_owners_before:
        raise AssertionError(f'Controller keyframe changed an unrelated Action owner: {basic_controller.name}')
    if _unrelated_animation_snapshot(basic_controller) != unrelated_animation_before:
        raise AssertionError(f'Controller keyframe changed unrelated object animation: {basic_controller.name}')
    action_after_keyframe = basic_controller.animation_data.action
    if action_after_keyframe is None or not any(curve.data_path == _property_path(basic_property)
                                                and any(abs(point.co.x - test_frame) < 1.0e-5
                                                        for point in curve.keyframe_points)
                                                for curve in action_after_keyframe.fcurves):
        raise AssertionError(f'Controller keyframe is missing at the test frame: {basic_controller.name}[{basic_property}]')
    for name in basic['properties']:
        if not basic_controller.keyframe_delete(data_path=_property_path(name), frame=test_frame):
            raise AssertionError(f'Could not remove the temporary test keyframe: {basic_controller.name}[{name}]')
    for item in controls:
        controller = item['controller']
        for name, value in sample_values[controller.name].items():
            controller[name] = value
    bpy.context.scene.frame_set(original_frame)

    bpy.context.preferences.filepaths.save_version = 0
    blend_path = output / 'character_rig_panel_smoke.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    bpy.utils.unregister_class(bpy.types.ENDFIELD_OT_select_rig_source_bone)
    bpy.ops.wm.open_mainfile(filepath=str(blend_path), use_scripts=False, load_ui=False)
    if getattr(bpy.types, 'ENDFIELD_OT_select_rig_source_bone', None) is not None:
        raise AssertionError('Scripts-disabled reopen unexpectedly registered the FK operator')
    if verify_rig_panel(report) != 'passed':
        raise AssertionError('Embedded rig panel did not survive save/reopen with scripts disabled')
    if verify_viewport_preview(preview_report).get('status') != 'verified':
        raise AssertionError('Full preview switch did not survive save/reopen with scripts disabled')
    ik_source = bpy.data.texts[ik_report['text']].as_string()
    exec(compile(ik_source, ik_report['text'], 'exec', dont_inherit=True), {'__name__': '__main__'})
    embedded = bpy.data.texts[TEXT_NAME].as_string()
    exec(compile(embedded, TEXT_NAME, 'exec', dont_inherit=True), {'__name__': '__main__'})
    exec(compile(embedded, TEXT_NAME, 'exec', dont_inherit=True), {'__name__': '__main__'})
    if getattr(bpy.types, 'ENDFIELD_PT_character_rig_panel', None) is None:
        raise AssertionError('Repeated embedded registration did not leave the rig panel registered')
    if getattr(bpy.types, 'ENDFIELD_OT_setup_character_ik', None) is None:
        raise AssertionError('Embedded IK setup script did not register after scripts-disabled reopen')
    if set(_ik_limb_states(bpy.data.objects[report['armature']])) != {'LeftArm'}:
        raise AssertionError('Embedded panel could not resolve installed IK state after scripts-disabled reopen')

    result_report = {
        'status': 'passed',
        'sample': sample_path,
        'armature': report['armature'],
        'skinned_mesh': skin_name,
        'controller_count': report['controller_count'],
        'control_count': report['control_count'],
        'soft_range_count': report['soft_range_count'],
        'preview_switch': 'passed',
        'preview_modifier_count': len(preview_report['modifiers']),
        'ik_setup_module': 'registered_and_embedded',
        'ik_limb_panel_states': sorted(ik_states),
        'ik_state_scripts_disabled_reopen': 'passed',
        'expression_groups': sorted(kinds),
        'bone_collection_selected': collection_name,
        'mesh_owner_resolution': 'passed',
        'controller_reset_scope': 'passed',
        'controller_keyframe_scope': 'passed',
        'embedded_registration_repeated': 'passed',
        'scripts_disabled_reopen': 'passed',
        'blend': str(blend_path),
    }
    (output / 'character_rig_panel_verification.json').write_text(
        json.dumps(result_report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result_report, ensure_ascii=False))


if __name__ == '__main__':
    _main(sys.argv[sys.argv.index('--') + 1:])
