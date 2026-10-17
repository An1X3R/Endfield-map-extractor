"""Expose an authored character rig and its existing expression controls in a portable sidebar."""

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

import bpy


TEXT_NAME = 'Endfield Character Rig Panel.py'
RIG_FORMAT = 'EndfieldCharacterRigAuthoring/1'
RIG_MARKER = 'endfield_rig_authoring_format'
RIG_REPORT_PROPERTY = 'endfield_rig_authoring_report'
OWNER_PROPERTY = 'endfield_rig_authoring_armature'
ROLE_PROPERTY = 'endfield_rig_authoring_role'
EXPRESSION_COLLECTION_ROLE = 'Expression Controls'
EXPRESSION_COLLECTION_KEY = 'expression_controls'
BASIC_FORMAT = 'EndfieldBasicExpressionControls/1'
INDEPENDENT_FORMAT = 'EndfieldIndependentExpressionControls/1'
PREVIEW_PROPERTY = '完整预览'
IK_OPERATOR_ID = 'endfield.setup_character_ik'
IK_SNAP_OPERATOR_ID = 'endfield.snap_character_ik'
IK_STATE_PROPERTY = 'endfield_edit_ik'
IK_SLOT_PROPERTY = 'endfield_rig_authoring_slot_paths'
IK_LIMBS = (('LeftArm', '左手'), ('RightArm', '右手'),
            ('LeftLeg', '左脚'), ('RightLeg', '右脚'))
IK_SOURCE_SLOTS = {'LeftArm': (14, 16, 18), 'RightArm': (15, 17, 19),
                   'LeftLeg': (1, 3, 5), 'RightLeg': (2, 4, 6)}
BONE_COLLECTION_LABELS = {
    'Body': '躯干',
    'Hands': '手指',
    'Hair': '头发',
    'Face': '面部',
    'Ears': '耳朵',
    'Tail': '尾巴',
    'Clothing': '衣物',
    'Accessories': '附件',
    'Source Details': '源骨骼细节',
    'IK Controls': '四肢IK',
    'IK Mechanism': '求解辅助',
}

ControllerKind = Literal['basic', 'independent', 'material']


class RigPanelReport(TypedDict):
    status: str
    text: str
    armature: str
    controller_count: int
    control_count: int
    soft_range_count: int


class ControllerControls(TypedDict):
    controller: bpy.types.Object
    kind: ControllerKind
    properties: tuple[str, ...]


class SourceBoneMenuOperator(Protocol):
    armature_name: str
    collection_name: str


def _record(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f'Expected object in rig authoring report: {label}')
    return cast(dict[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f'Expected nonempty string in rig authoring report: {label}')
    return value


def _string_rows(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f'Expected array in rig authoring report: {label}')
    rows = tuple(_string(item, f'{label}[{index}]') for index, item in enumerate(value))
    if len(rows) != len(set(rows)):
        raise ValueError(f'Duplicate names in rig authoring report: {label}')
    return rows


def _numeric(value: object, label: str) -> int | float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise TypeError(f'Expression property must be a finite number: {label}={value!r}')
    return cast(int | float, value)


def _authoring_report(rig: bpy.types.Object) -> dict[str, object]:
    if rig.type != 'ARMATURE' or rig.data is None:
        raise TypeError(f'Character rig panel requires an Armature object: {rig.name}')
    if rig.get(RIG_MARKER) != RIG_FORMAT or rig.data.get(RIG_MARKER) != RIG_FORMAT:
        raise ValueError(f'Character rig is missing its authoring marker: {rig.name}')
    raw = rig.get(RIG_REPORT_PROPERTY)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f'Character rig authoring report is missing: {rig.name}/{RIG_REPORT_PROPERTY}')
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f'Character rig authoring report is invalid JSON: {rig.name}: {error.msg}') from error
    report = _record(decoded, f'{rig.name}.{RIG_REPORT_PROPERTY}')
    if report.get('format') != RIG_FORMAT or report.get('armature') != rig.name:
        raise ValueError(f'Character rig authoring report targets another rig: {rig.name}')
    if report.get('verified') is not True:
        raise ValueError(f'Character rig authoring report is not verified: {rig.name}')
    _string_rows(report.get('expression_controls'), 'expression_controls')
    _string_rows(report.get('source_geometry'), 'source_geometry')
    _record(report.get('scene_collections'), 'scene_collections')
    _record(report.get('bone_collections'), 'bone_collections')
    return report


def _expression_collection(rig: bpy.types.Object, report: dict[str, object]) -> bpy.types.Collection:
    scene_collections = _record(report.get('scene_collections'), 'scene_collections')
    name = _string(scene_collections.get(EXPRESSION_COLLECTION_KEY),
                   f'scene_collections.{EXPRESSION_COLLECTION_KEY}')
    collection = bpy.data.collections.get(name)
    if (collection is None or collection.get(RIG_MARKER) != RIG_FORMAT
            or collection.get(OWNER_PROPERTY) != rig.name
            or collection.get(ROLE_PROPERTY) != EXPRESSION_COLLECTION_ROLE):
        raise ValueError(f'Owned expression-control collection is missing or invalid: rig={rig.name}, collection={name}')
    return collection


def _material_property_names(controller: bpy.types.Object) -> tuple[str, ...]:
    raw = controller.get('expression_material_controls')
    if not isinstance(raw, str) or not raw:
        raise ValueError(f'Material emotion controller has no binding report: {controller.name}')
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f'Material emotion binding report is invalid JSON: {controller.name}: {error.msg}') from error
    report = _record(decoded, f'{controller.name}.expression_material_controls')
    if report.get('status') != 'attached':
        raise ValueError(f'Material emotion binding report is not attached: {controller.name}')
    raw_bindings = report.get('bindings')
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise ValueError(f'Material emotion controller has no property bindings: {controller.name}')
    properties: list[str] = []
    for index, raw_binding in enumerate(raw_bindings):
        binding = _record(raw_binding, f'{controller.name}.bindings[{index}]')
        property_name = _string(binding.get('property'), f'{controller.name}.bindings[{index}].property')
        if property_name not in properties:
            properties.append(property_name)
    for property_name in properties:
        if property_name not in controller:
            raise ValueError(f'Material emotion property is missing: {controller.name}[{property_name}]')
        _numeric(controller[property_name], f'{controller.name}[{property_name}]')
        metadata = controller.id_properties_ui(property_name).as_dict()
        minimum = metadata.get('min')
        maximum = metadata.get('max')
        if (type(minimum) not in (int, float) or type(maximum) not in (int, float)
                or not math.isfinite(float(minimum)) or not math.isfinite(float(maximum))
                or float(minimum) > float(maximum)):
            raise ValueError(f'Material emotion property has no valid source range: {controller.name}[{property_name}]')
    return tuple(properties)


def _controller_controls(controller: bpy.types.Object) -> ControllerControls:
    if controller.type != 'EMPTY':
        raise TypeError(f'Expression controller must be an Empty: {controller.name}')
    expression_format = controller.get('expression_controls_format')
    material_report = controller.get('expression_material_controls')
    if expression_format is not None and material_report is not None:
        raise ValueError(f'Expression controller has conflicting markers: {controller.name}')
    if expression_format == BASIC_FORMAT:
        kind: ControllerKind = 'basic'
        excluded = {'expression_controls_format'}
        properties = tuple(key for key in controller.keys() if key not in excluded
                           and type(controller[key]) in (int, float))
    elif expression_format == INDEPENDENT_FORMAT:
        kind = 'independent'
        excluded = {'expression_controls_format'}
        properties = tuple(key for key in controller.keys() if key not in excluded
                           and type(controller[key]) in (int, float))
    elif expression_format is not None:
        raise ValueError(f'Unsupported expression controller marker: {controller.name}={expression_format!r}')
    elif material_report is not None:
        kind = 'material'
        properties = _material_property_names(controller)
    else:
        raise ValueError(f'Expression controller marker is missing: {controller.name}')
    if not properties:
        raise ValueError(f'Expression controller has no numeric properties: {controller.name}')
    for property_name in properties:
        _string(property_name, f'{controller.name}.property')
        _numeric(controller[property_name], f'{controller.name}[{property_name}]')
    return {'controller': controller, 'kind': kind, 'properties': properties}


def _controller_controls_for_rig(rig: bpy.types.Object) -> tuple[ControllerControls, ...]:
    report = _authoring_report(rig)
    collection = _expression_collection(rig, report)
    names = _string_rows(report.get('expression_controls'), 'expression_controls')
    controls: list[ControllerControls] = []
    for name in names:
        controller = bpy.data.objects.get(name)
        if (controller is None or controller.name not in collection.objects
                or controller.get(OWNER_PROPERTY) not in (None, rig.name)):
            raise ValueError(f'Expression controller is missing or outside the owning rig collection: rig={rig.name}, controller={name}')
        controls.append(_controller_controls(controller))
    return tuple(controls)


def _report_owns_mesh(rig: bpy.types.Object, mesh: bpy.types.Object) -> bool:
    report = _authoring_report(rig)
    names = _string_rows(report.get('source_geometry'), 'source_geometry')
    return mesh.name in names


def _report_owner_matches(mesh: bpy.types.Object) -> tuple[bpy.types.Object, ...]:
    matches: list[bpy.types.Object] = []
    for rig in bpy.context.scene.objects:
        if rig.type == 'ARMATURE' and rig.get(RIG_MARKER) == RIG_FORMAT:
            if _report_owns_mesh(rig, mesh):
                matches.append(rig)
    return tuple(matches)


def resolve_rig_for_active_object(active: bpy.types.Object | None) -> bpy.types.Object:
    """Resolve the active authored rig without choosing a scene-wide fallback."""
    if active is None:
        raise ValueError('Select an authored character rig or one of its skinned meshes')
    if active.type == 'ARMATURE':
        _authoring_report(active)
        return active
    if active.type != 'MESH':
        raise TypeError(f'Active object is not a character rig or skinned mesh: {active.name}, type={active.type}')

    modifier_targets: dict[str, bpy.types.Object] = {}
    for modifier in active.modifiers:
        target = modifier.object if modifier.type == 'ARMATURE' else None
        if target is not None:
            modifier_targets[target.name] = target
    owners = _report_owner_matches(active)
    if len(modifier_targets) == 1:
        rig = next(iter(modifier_targets.values()))
        _authoring_report(rig)
        conflicting = tuple(owner.name for owner in owners if owner != rig)
        if conflicting:
            raise ValueError(f'Skinned mesh and rig report disagree on ownership: mesh={active.name}, modifier={rig.name}, report={conflicting}')
        return rig
    if len(modifier_targets) > 1:
        matches = tuple(rig for rig in modifier_targets.values() if rig in owners)
        if len(matches) == 1:
            _authoring_report(matches[0])
            return matches[0]
        candidate_names = tuple(sorted(modifier_targets))
        raise ValueError(f'Skinned mesh has ambiguous owning rigs: mesh={active.name}, candidates={candidate_names}, report_matches={tuple(rig.name for rig in matches)}')

    ancestors: list[bpy.types.Object] = []
    parent = active.parent
    visited: set[int] = set()
    while parent is not None:
        pointer = parent.as_pointer()
        if pointer in visited:
            raise ValueError(f'Cycle in selected mesh parent hierarchy: {active.name}')
        visited.add(pointer)
        if parent.type == 'ARMATURE':
            ancestors.append(parent)
        parent = parent.parent
    if len(ancestors) == 1:
        _authoring_report(ancestors[0])
        conflicting = tuple(owner.name for owner in owners if owner != ancestors[0])
        if conflicting:
            raise ValueError(f'Mesh parent and rig report disagree on ownership: mesh={active.name}, parent={ancestors[0].name}, report={conflicting}')
        return ancestors[0]
    if len(ancestors) > 1:
        raise ValueError(f'Mesh has ambiguous armature ancestors: mesh={active.name}, candidates={tuple(rig.name for rig in ancestors)}')
    if len(owners) == 1:
        return owners[0]
    if len(owners) > 1:
        raise ValueError(f'Mesh is listed by multiple rig reports: mesh={active.name}, candidates={tuple(rig.name for rig in owners)}')
    raise ValueError(f'No owning authored rig is linked to the active mesh: {active.name}')


def _require_rig(name: str) -> bpy.types.Object:
    rig = bpy.data.objects.get(name)
    if rig is None:
        raise ValueError(f'Character armature no longer exists: {name}')
    _authoring_report(rig)
    return rig


def _mapped_ik_limbs(rig: bpy.types.Object) -> frozenset[str]:
    raw = rig.get(IK_SLOT_PROPERTY, '{}')
    if not isinstance(raw, str) or not raw:
        raise TypeError(f'Rig source slot mapping must be a JSON string: {rig.name}[{IK_SLOT_PROPERTY}]')
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f'Rig source slot mapping is invalid JSON: {rig.name}: {error.msg}') from error
    slots = _record(decoded, f'{rig.name}.{IK_SLOT_PROPERTY}')
    if not all(isinstance(path, str) and path for path in slots.values()):
        raise TypeError(f'Rig source slot mapping must contain nonempty paths: {rig.name}[{IK_SLOT_PROPERTY}]')
    source_paths: set[str] = set()
    for bone in rig.data.bones:
        path = bone.get('source_transform_path')
        if path is not None:
            if not isinstance(path, str) or not path:
                raise TypeError(f'Rig bone source path must be a nonempty string: {rig.name}/{bone.name}')
            source_paths.add(path)
    return frozenset(limb for limb, required_slots in IK_SOURCE_SLOTS.items()
                     if all(str(slot) in slots and slots[str(slot)] in source_paths
                            for slot in required_slots))


def _ik_limb_states(rig: bpy.types.Object) -> dict[str, dict[str, str]]:
    raw = rig.get(IK_STATE_PROPERTY, '{}')
    if not isinstance(raw, str) or not raw:
        raise TypeError(f'Rig IK state must be a JSON string: {rig.name}[{IK_STATE_PROPERTY}]')
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f'Rig IK state is invalid JSON: {rig.name}: {error.msg}') from error
    states = _record(decoded, f'{rig.name}.{IK_STATE_PROPERTY}')
    known_limbs = {name for name, _label in IK_LIMBS}
    result: dict[str, dict[str, str]] = {}
    for limb in known_limbs.intersection(states):
        state = _record(states[limb], f'{rig.name}.{IK_STATE_PROPERTY}.{limb}')
        bones = _record(state.get('bones'), f'{rig.name}.{IK_STATE_PROPERTY}.{limb}.bones')
        target = _string(bones.get('Target'), f'{rig.name}.{limb}.bones.Target')
        pole = _string(bones.get('Pole'), f'{rig.name}.{limb}.bones.Pole')
        property_name = _string(state.get('property'), f'{rig.name}.{limb}.property')
        if target not in rig.pose.bones or pole not in rig.pose.bones:
            raise ValueError(f'Rig IK target or pole bone is missing: rig={rig.name}, limb={limb}, target={target}, pole={pole}')
        if property_name not in rig:
            raise ValueError(f'Rig IK blend property is missing: rig={rig.name}, limb={limb}, property={property_name}')
        value = float(_numeric(rig[property_name], f'{rig.name}[{property_name}]'))
        metadata = rig.id_properties_ui(property_name).as_dict()
        minimum = metadata.get('min')
        maximum = metadata.get('max')
        if (value < 0.0 or value > 1.0
                or type(minimum) not in (int, float) or float(minimum) != 0.0
                or type(maximum) not in (int, float) or float(maximum) != 1.0):
            raise ValueError(f'Rig IK blend must have a 0..1 range: rig={rig.name}, limb={limb}, property={property_name}, value={value}, range=({minimum}, {maximum})')
        result[limb] = {'target': target, 'pole': pole, 'property': property_name}
        orientation_name = bones.get('EndOutput')
        if isinstance(orientation_name, str) and orientation_name in rig.pose.bones:
            orientation = rig.pose.bones[orientation_name]
            if orientation.get('endfield_ik_rotation_control') is True:
                result[limb]['orientation'] = orientation_name
    return result


def _property_path(property_name: str) -> str:
    return '[' + json.dumps(property_name) + ']'


def _replace_class(operator_or_panel: type[bpy.types.Operator] | type[bpy.types.Panel]) -> None:
    previous = getattr(bpy.types, operator_or_panel.__name__, None)
    if previous is not None:
        bpy.utils.unregister_class(previous)
    bpy.utils.register_class(operator_or_panel)


def _enter_pose_mode(context: bpy.types.Context, rig: bpy.types.Object) -> None:
    active = context.view_layer.objects.active
    if active is not None and active.mode != 'OBJECT':
        result = bpy.ops.object.mode_set(mode='OBJECT')
        if 'FINISHED' not in result:
            raise RuntimeError(f'Could not leave current object mode before selecting rig: active={active.name}, mode={active.mode}')
    rig.select_set(True)
    context.view_layer.objects.active = rig
    if rig.mode != 'POSE':
        result = bpy.ops.object.mode_set(mode='POSE')
        if 'FINISHED' not in result:
            raise RuntimeError(f'Could not enter Pose Mode for rig: {rig.name}')
    if rig.mode != 'POSE':
        raise RuntimeError(f'Rig did not enter Pose Mode: {rig.name}, mode={rig.mode}')


def _source_fk_bone_records(rig: bpy.types.Object, collection_name: str) -> tuple[tuple[str, str], ...]:
    report = _authoring_report(rig)
    collection = rig.data.collections.get(collection_name)
    if collection is None:
        raise ValueError(f'Bone Collection is missing: rig={rig.name}, collection={collection_name}')
    bone_collections = _record(report.get('bone_collections'), 'bone_collections')
    expected_names = set(_string_rows(bone_collections.get(collection_name),
                                      f'bone_collections.{collection_name}'))
    actual_names = {bone.name for bone in collection.bones}
    if actual_names != expected_names:
        raise ValueError(f'Bone Collection differs from the verified rig report: rig={rig.name}, collection={collection_name}')
    raw_controls = report.get('source_fk_controls')
    if not isinstance(raw_controls, list):
        raise TypeError(f'Rig report has no source FK controls: {rig.name}')
    source_paths: dict[str, str] = {}
    control_names: set[str] = set()
    for index, raw_control in enumerate(raw_controls):
        control = _record(raw_control, f'source_fk_controls[{index}]')
        if control.get('role') != collection_name:
            continue
        name = _string(control.get('bone'), f'source_fk_controls[{index}].bone')
        raw_path = control.get('path')
        if not isinstance(raw_path, str):
            raise TypeError(f'Expected source transform path in rig report: source_fk_controls[{index}].path')
        if name in control_names:
            raise ValueError(f'Duplicate source FK control in rig report: rig={rig.name}, bone={name}')
        if name not in expected_names:
            raise ValueError(f'Source FK control is outside its verified category: rig={rig.name}, collection={collection_name}, bone={name}')
        control_names.add(name)
        if raw_path.strip():
            source_paths[name] = raw_path
    if control_names != expected_names:
        raise ValueError(f'Source FK controls differ from their verified category: rig={rig.name}, collection={collection_name}')
    records: list[tuple[str, str]] = []
    for name in sorted(source_paths):
        bone = rig.data.bones.get(name)
        path = source_paths[name]
        if bone is None or bone.get('source_transform_path') != path:
            raise ValueError(f'Source FK bone no longer matches its verified path: rig={rig.name}, bone={name}, path={path}')
        records.append((name, path))
    return tuple(records)


@lru_cache(maxsize=256)
def _source_fk_bone_menu_items(records: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str, str], ...]:
    return tuple((name, name, path) for name, path in records)


def _source_fk_bone_items(operator: SourceBoneMenuOperator,
                           _context: bpy.types.Context) -> tuple[tuple[str, str, str], ...]:
    if not operator.armature_name or not operator.collection_name:
        return ()
    rig = bpy.data.objects.get(operator.armature_name)
    if rig is None or rig.type != 'ARMATURE':
        return ()
    if rig.data.collections.get(operator.collection_name) is None:
        return ()
    return _source_fk_bone_menu_items(_source_fk_bone_records(rig, operator.collection_name))


class ENDFIELD_OT_select_rig_bone_collection(bpy.types.Operator):
    """Activate the rig and select only bones from one bone collection."""

    bl_idname = 'endfield.select_rig_bone_collection'
    bl_label = 'Select Rig Category'
    bl_options = {'REGISTER', 'UNDO'}

    armature_name: bpy.props.StringProperty(name='Armature')
    collection_name: bpy.props.StringProperty(name='Bone Collection')

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            rig = _require_rig(self.armature_name)
            collection = rig.data.collections.get(self.collection_name)
            if collection is None:
                raise ValueError(f'Bone Collection is missing: rig={rig.name}, collection={self.collection_name}')
            _enter_pose_mode(context, rig)
            collection.is_visible = True
            selected_names = {bone.name for bone in collection.bones}
            for pose_bone in rig.pose.bones:
                selected = pose_bone.name in selected_names
                pose_bone.bone.select = selected
                pose_bone.bone.select_head = selected
                pose_bone.bone.select_tail = selected
            context.view_layer.update()
            return {'FINISHED'}
        except (RuntimeError, TypeError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class ENDFIELD_OT_select_rig_source_bone(bpy.types.Operator):
    """Search for and activate one verified source FK bone."""

    bl_idname = 'endfield.select_rig_source_bone'
    bl_label = 'Search Source FK Bone'
    bl_description = 'Search by source bone name and select only that FK control'
    bl_options = {'REGISTER', 'UNDO'}
    bl_property = 'bone_name'

    armature_name: bpy.props.StringProperty(name='Armature')
    collection_name: bpy.props.StringProperty(name='Bone Collection')
    bone_name: bpy.props.EnumProperty(name='Source FK Bone', items=_source_fk_bone_items)

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            rig = _require_rig(self.armature_name)
            records = dict(_source_fk_bone_records(rig, self.collection_name))
            if self.bone_name not in records:
                raise ValueError(f'Source FK bone is outside its verified category: rig={rig.name}, collection={self.collection_name}, bone={self.bone_name}')
            _enter_pose_mode(context, rig)
            collection = rig.data.collections[self.collection_name]
            collection.is_visible = True
            for pose_bone in rig.pose.bones:
                selected = pose_bone.name == self.bone_name
                pose_bone.bone.select = selected
                pose_bone.bone.select_head = selected
                pose_bone.bone.select_tail = selected
            rig.data.bones.active = rig.data.bones[self.bone_name]
            context.view_layer.update()
            return {'FINISHED'}
        except (RuntimeError, TypeError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class ENDFIELD_OT_reset_expression_controller(bpy.types.Operator):
    """Reset only the existing numeric properties on one owned controller."""

    bl_idname = 'endfield.reset_expression_controller'
    bl_label = 'Reset Controller'
    bl_options = {'REGISTER', 'UNDO'}

    armature_name: bpy.props.StringProperty(name='Armature')
    controller_name: bpy.props.StringProperty(name='Controller')

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            rig = _require_rig(self.armature_name)
            matches = tuple(item for item in _controller_controls_for_rig(rig)
                            if item['controller'].name == self.controller_name)
            if len(matches) != 1:
                raise ValueError(f'Controller is not uniquely owned by the selected rig: rig={rig.name}, controller={self.controller_name}, matches={len(matches)}')
            controls = matches[0]
            controller = controls['controller']
            for property_name in controls['properties']:
                controller[property_name] = 0 if type(controller[property_name]) is int else 0.0
            controller.update_tag()
            context.view_layer.update()
            return {'FINISHED'}
        except (RuntimeError, TypeError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class ENDFIELD_OT_keyframe_expression_controller(bpy.types.Operator):
    """Key only the existing numeric properties on one owned controller."""

    bl_idname = 'endfield.keyframe_expression_controller'
    bl_label = 'Keyframe Controller'
    bl_options = {'REGISTER', 'UNDO'}

    armature_name: bpy.props.StringProperty(name='Armature')
    controller_name: bpy.props.StringProperty(name='Controller')

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            rig = _require_rig(self.armature_name)
            matches = tuple(item for item in _controller_controls_for_rig(rig)
                            if item['controller'].name == self.controller_name)
            if len(matches) != 1:
                raise ValueError(f'Controller is not uniquely owned by the selected rig: rig={rig.name}, controller={self.controller_name}, matches={len(matches)}')
            controls = matches[0]
            controller = controls['controller']
            for property_name in controls['properties']:
                inserted = controller.keyframe_insert(data_path=_property_path(property_name),
                                                      frame=context.scene.frame_current,
                                                      group='Endfield Expression Controls')
                if not inserted:
                    raise RuntimeError(f'Could not keyframe controller property: {controller.name}[{property_name}]')
            return {'FINISHED'}
        except (RuntimeError, TypeError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class ENDFIELD_PT_character_rig_panel(bpy.types.Panel):
    """Present the selected character rig and its original authoring controls."""

    bl_label = '终末地 · 角色工作台'
    bl_idname = 'ENDFIELD_PT_character_rig_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '终末地'
    bl_order = -2

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        active = context.active_object
        if active is None:
            return False
        if active.type == 'ARMATURE':
            return active.get(RIG_MARKER) == RIG_FORMAT
        if active.type == 'MESH':
            return any(modifier.type == 'ARMATURE' and modifier.object is not None
                       for modifier in active.modifiers)
        return False

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        try:
            rig = resolve_rig_for_active_object(context.active_object)
            controls = _controller_controls_for_rig(rig)
            self._draw_preview(layout, rig)
            self._draw_bone_collections(layout, rig)
            self._draw_expression_group(layout, rig, controls, 'basic',
                                        '面部基础表情', 'endfield_rig_panel_basic_expressions')
            self._draw_expression_group(layout, rig, controls, 'independent',
                                        '独立耳部控制', 'endfield_rig_panel_independent_expressions')
            self._draw_expression_group(layout, rig, controls, 'material',
                                        '材质情绪控制', 'endfield_rig_panel_material_expressions')
            self._draw_ik_tools(layout, rig)
        except (TypeError, ValueError) as error:
            row = layout.row()
            row.alert = True
            row.label(text=str(error), icon='ERROR')

    @staticmethod
    def _draw_preview(layout: bpy.types.UILayout, rig: bpy.types.Object) -> None:
        row = layout.row()
        row.label(text=rig.name, icon='ARMATURE_DATA')
        if PREVIEW_PROPERTY in rig:
            if type(rig[PREVIEW_PROPERTY]) is not bool:
                raise TypeError(f'Rig preview property must be Boolean: {rig.name}[{PREVIEW_PROPERTY}]')
            row.prop(rig, _property_path(PREVIEW_PROPERTY), text=PREVIEW_PROPERTY)
        else:
            row.label(text='完整预览开关未附加', icon='INFO')

    @staticmethod
    def _draw_bone_collections(layout: bpy.types.UILayout, rig: bpy.types.Object) -> None:
        box = layout.box()
        box.label(text='骨骼分类', icon='BONE_DATA')
        if not rig.data.collections:
            box.label(text='此 Rig 没有 Bone Collection', icon='INFO')
            return
        for collection in rig.data.collections:
            row = box.row(align=True)
            row.prop(collection, 'is_visible', text='', emboss=False,
                     icon='HIDE_OFF' if collection.is_visible else 'HIDE_ON')
            selected = any(bone.select and bone.name in collection.bones for bone in rig.data.bones)
            operator = row.operator('endfield.select_rig_bone_collection',
                                    text=BONE_COLLECTION_LABELS.get(collection.name, collection.name),
                                    icon='BONE_DATA', depress=selected)
            operator.armature_name = rig.name
            operator.collection_name = collection.name
            has_source_bone = any(isinstance(bone.get('source_transform_path'), str)
                                  and bool(bone.get('source_transform_path').strip())
                                  for bone in collection.bones)
            if collection.name not in ('IK Controls', 'IK Mechanism') and has_source_bone:
                picker = row.operator('endfield.select_rig_source_bone',
                                      text='搜单骨FK', icon='VIEWZOOM')
                picker.armature_name = rig.name
                picker.collection_name = collection.name

    @staticmethod
    def _draw_expression_group(layout: bpy.types.UILayout, rig: bpy.types.Object,
                               controls: tuple[ControllerControls, ...], kind: ControllerKind,
                               title: str, panel_id: str) -> None:
        selected = tuple(item for item in controls if item['kind'] == kind)
        if not selected:
            return
        header, body = layout.panel(panel_id, default_closed=kind != 'basic')
        header.label(text=title)
        if body is None:
            return
        for item in selected:
            controller = item['controller']
            box = body.box()
            box.label(text=controller.name, icon='EMPTY_AXIS')
            for property_name in item['properties']:
                box.prop(controller, _property_path(property_name), text=property_name)
            row = box.row(align=True)
            reset = row.operator('endfield.reset_expression_controller', text='重置', icon='LOOP_BACK')
            reset.armature_name = rig.name
            reset.controller_name = controller.name
            keyframe = row.operator('endfield.keyframe_expression_controller', text='插入关键帧', icon='KEY_HLT')
            keyframe.armature_name = rig.name
            keyframe.controller_name = controller.name

    @staticmethod
    def _draw_ik_tools(layout: bpy.types.UILayout, rig: bpy.types.Object) -> None:
        header, body = layout.panel('endfield_rig_panel_ik_tools', default_closed=True)
        header.label(text='IK 工具')
        if body is None:
            return
        states = _ik_limb_states(rig)
        setup_limbs = frozenset(limb for limb, _label in IK_LIMBS if limb not in states)
        mapped_limbs = _mapped_ik_limbs(rig) if setup_limbs else frozenset()
        setup_available = getattr(bpy.types, 'ENDFIELD_OT_setup_character_ik', None) is not None
        snap_available = getattr(bpy.types, 'ENDFIELD_OT_snap_character_ik', None) is not None
        for limb, label in IK_LIMBS:
            state = states.get(limb)
            row = body.row(align=True)
            if state is None:
                if limb not in mapped_limbs:
                    row.enabled = False
                    row.label(text=f'{label} · 缺少源骨骼映射', icon='INFO')
                elif setup_available:
                    operator = row.operator(IK_OPERATOR_ID, text=label, icon='CONSTRAINT_BONE')
                    operator.rig_name = rig.name
                    operator.limb = limb
                else:
                    row.enabled = False
                    row.label(text=f'{label} · IK 设置器未加载', icon='INFO')
                continue
            row.label(text=label, icon='CONSTRAINT_BONE')
            row.prop(rig, _property_path(state['property']), text='FK 0 / IK 1', slider=True)
            if snap_available:
                snap = body.row(align=True)
                operator = snap.operator(IK_SNAP_OPERATOR_ID, text='吸附当前姿态到 IK 目标', icon='SNAP_ON')
                operator.rig_name = rig.name
                operator.limb = limb
            if getattr(bpy.types, 'ENDFIELD_OT_select_ik_control', None) is not None:
                buttons = body.row(align=True)
                for role, text in (('Target', '选择手脚目标'), ('Pole', '选择肘膝方向')):
                    select = buttons.operator('endfield.select_ik_control', text=text)
                    select.rig_name, select.limb, select.control = rig.name, limb, role
                if 'orientation' in state:
                    select = body.operator('endfield.select_ik_control', text='旋转手腕 / 脚踝 · R', icon='ORIENTATION_GIMBAL')
                    select.rig_name, select.limb, select.control = rig.name, limb, 'EndOutput'


def register() -> None:
    """Replace only classes owned by this embedded sidebar script."""
    owner = getattr(bpy.types, 'ENDFIELD_PT_character_tools_addon', None)
    if owner is not None and owner.__module__ != globals().get('__package__'):
        return
    _replace_class(ENDFIELD_OT_select_rig_bone_collection)
    _replace_class(ENDFIELD_OT_select_rig_source_bone)
    _replace_class(ENDFIELD_OT_reset_expression_controller)
    _replace_class(ENDFIELD_OT_keyframe_expression_controller)
    _replace_class(ENDFIELD_PT_character_rig_panel)


def attach_rig_panel(rig: bpy.types.Object) -> RigPanelReport:
    """Embed and register a portable sidebar without changing global script trust."""
    authoring = _authoring_report(rig)
    controls = _controller_controls_for_rig(rig)
    original_keys = {item['controller'].name: tuple(item['controller'].keys()) for item in controls}
    original_values = {item['controller'].name: tuple((name, item['controller'][name])
                                                       for name in item['properties']) for item in controls}
    source = Path(__file__).read_text(encoding='utf-8') + '\nregister()\n'
    compile(source, TEXT_NAME, 'exec', dont_inherit=True)
    soft_range_count = 0
    control_count = 0
    for item in controls:
        controller = item['controller']
        control_count += len(item['properties'])
        if item['kind'] == 'material':
            continue
        for property_name in item['properties']:
            controller.id_properties_ui(property_name).update(soft_min=0.0, soft_max=1.0)
            soft_range_count += 1
        if tuple(controller.keys()) != original_keys[controller.name]:
            raise AssertionError(f'Panel attachment added or removed custom properties: {controller.name}')
        if tuple((name, controller[name]) for name in item['properties']) != original_values[controller.name]:
            raise AssertionError(f'Panel attachment changed expression values: {controller.name}')

    text = bpy.data.texts.get(TEXT_NAME)
    if text is None:
        text = bpy.data.texts.new(TEXT_NAME)
    text.clear()
    text.write(source)
    text.use_module = True
    register()
    if getattr(bpy.types, 'ENDFIELD_OT_select_rig_source_bone', None) is None:
        raise AssertionError('Single source FK selection operator is not registered')
    report: RigPanelReport = {
        'status': 'attached',
        'text': text.name,
        'armature': rig.name,
        'controller_count': len(controls),
        'control_count': control_count,
        'soft_range_count': soft_range_count,
    }
    if authoring.get('armature') != report['armature']:
        raise AssertionError(f'Rig authoring report changed during panel attachment: {rig.name}')
    return report


def verify_rig_panel(report: RigPanelReport) -> str:
    """Verify the embedded source and only the controls owned by its armature."""
    rig_name = _string(report.get('armature'), 'panel report.armature')
    text_name = _string(report.get('text'), 'panel report.text')
    if report.get('status') != 'attached':
        raise ValueError(f'Unsupported rig panel report status: {report.get("status")!r}')
    for field in ('controller_count', 'control_count', 'soft_range_count'):
        if type(report.get(field)) is not int or cast(int, report.get(field)) < 0:
            raise TypeError(f'Rig panel report field must be a nonnegative integer: {field}')
    rig = _require_rig(rig_name)
    controls = _controller_controls_for_rig(rig)
    expected_control_count = sum(len(item['properties']) for item in controls)
    expected_soft_range_count = sum(len(item['properties']) for item in controls if item['kind'] != 'material')
    if (report['controller_count'] != len(controls)
            or report['control_count'] != expected_control_count
            or report['soft_range_count'] != expected_soft_range_count):
        raise AssertionError(f'Rig panel report counts differ from the owned controllers: rig={rig.name}')
    for item in controls:
        if item['kind'] == 'material':
            continue
        controller = item['controller']
        for property_name in item['properties']:
            metadata = controller.id_properties_ui(property_name).as_dict()
            if (metadata.get('soft_min') != 0.0 or metadata.get('soft_max') != 1.0):
                raise AssertionError(f'Expression UI range is missing: {controller.name}[{property_name}]')
    text = bpy.data.texts.get(text_name)
    if text is None or not text.use_module:
        raise AssertionError(f'Embedded rig panel is missing or not reloadable: {text_name}')
    source = text.as_string()
    if not source.endswith('\nregister()\n'):
        raise AssertionError(f'Embedded rig panel does not end with register(): {text_name}')
    compile(source, text_name, 'exec', dont_inherit=True)
    return 'passed'
