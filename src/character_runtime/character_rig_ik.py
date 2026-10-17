"""Build optional native IK editing chains without rebinding source skin."""
import json
import math
from pathlib import Path
from typing import TypedDict

import bpy
from mathutils import Matrix, Vector


TEXT_NAME = 'Endfield Rig IK.py'
STATE = 'endfield_edit_ik'
SLOTS = 'endfield_rig_authoring_slot_paths'
LIMBS = {'LeftArm': (14, 16, 18), 'RightArm': (15, 17, 19),
         'LeftLeg': (1, 3, 5), 'RightLeg': (2, 4, 6)}
LABELS = {'LeftArm': '左手', 'RightArm': '右手', 'LeftLeg': '左脚', 'RightLeg': '右脚'}


class IKPanelReport(TypedDict):
    text: str
    status: str


def limb_bones(rig: bpy.types.Object, limb: str) -> tuple[str, str, str]:
    """Resolve only the imported Avatar's exact source transform paths."""
    if rig.type != 'ARMATURE' or limb not in LIMBS:
        raise ValueError(f'IK requires a source armature and known limb: {rig.name}/{limb}')
    slots = json.loads(rig.get(SLOTS, '{}'))
    paths: dict[str, str] = {}
    for bone in rig.data.bones:
        path = bone.get('source_transform_path')
        if not isinstance(path, str) or not path:
            continue
        if path in paths:
            raise ValueError(f'Ambiguous source transform path in IK rig: {rig.name}/{path}')
        paths[path] = bone.name
    required = LIMBS[limb]
    if any(str(slot) not in slots or slots[str(slot)] not in paths for slot in required):
        raise ValueError(f'Avatar has no complete exact limb mapping: {rig.name}/{limb}')
    return tuple(paths[slots[str(slot)]] for slot in required)


def _activate(rig: bpy.types.Object, mode: str) -> None:
    if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    rig.hide_set(False)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode=mode)


def _basis(head: Vector, tail: Vector) -> Matrix:
    direction = tail - head
    if direction.length < 1e-5:
        raise ValueError('IK joint centers are coincident; no usable segment length')
    matrix = direction.to_track_quat('Y', 'Z').to_matrix().to_4x4()
    matrix.translation = head
    return matrix


def _signed_angle(first: Vector, second: Vector, axis: Vector) -> float:
    return math.atan2(axis.dot(first.cross(second)), first.dot(second))


def _avatar_landmarks(rig: bpy.types.Object) -> dict[int, bpy.types.PoseBone]:
    """Resolve body landmarks through exact Avatar paths, never name guesses."""
    slots = json.loads(rig.get(SLOTS, '{}'))
    paths = {bone.get('source_transform_path'): bone.name for bone in rig.data.bones
             if bone.get('source_transform_path')}
    required = (0, 1, 2, 5, 6, 11, 20, 21)
    missing = tuple(slot for slot in required if slots.get(str(slot)) not in paths)
    if missing:
        raise ValueError(f'Avatar lacks anatomical IK body landmarks: {rig.name}, slots={missing}')
    return {slot: rig.pose.bones[paths[slots[str(slot)]]] for slot in required}


def _torso_axes(positions: dict[int, Vector]) -> Matrix:
    """Construct an orthogonal torso frame independently of foot rotations."""
    up = positions[11] - positions[0]
    left = positions[1] - positions[2]
    if up.length < 1e-5 or left.length < 1e-5:
        raise ValueError('Avatar body landmarks cannot establish left and up directions')
    up.normalize()
    left -= up * left.dot(up)
    if left.length < 1e-5:
        raise ValueError('Avatar hip width is collinear with its body axis')
    left.normalize()
    forward = left.cross(up).normalized()
    return Matrix((left, up, forward)).transposed()


def _body_axes(positions: dict[int, Vector]) -> Matrix:
    """Determine anatomical handedness from rest foot-toe evidence."""
    axes = _torso_axes(positions)
    forward = axes.col[2].copy()
    toes = positions[20] - positions[5] + positions[21] - positions[6]
    if abs(forward.dot(toes)) < 1e-5:
        raise ValueError('Avatar foot-toe landmarks cannot establish its forward direction')
    if forward.dot(toes) < 0.:
        forward.negate()
    axes.col[2] = forward
    return axes


def _pole_direction(rig: bpy.types.Object, limb: str, matrices: tuple[Matrix, ...]) -> Vector:
    """Ignore sub-percent bend noise; use rest anatomy before body directions."""
    a, b, c = (matrix.translation for matrix in matrices)
    axis = (c-a).normalized()
    length = (b-a).length + (c-b).length
    bend = b-a-axis*(b-a).dot(axis)
    if bend.length > length*.01:
        return bend.normalized()
    landmarks = _avatar_landmarks(rig)
    rest = _body_axes({slot: bone.bone.head_local for slot, bone in landmarks.items()})
    posed = _torso_axes({slot: bone.matrix.translation for slot, bone in landmarks.items()})
    posed.col[2] = posed.col[2] * rest.determinant()
    names = limb_bones(rig, limb)
    ra, rb, rc = (rig.data.bones[name].head_local for name in names)
    rest_axis = (rc-ra).normalized()
    rest_bend = rb-ra-rest_axis*(rb-ra).dot(rest_axis)
    if rest_bend.length > ((rb-ra).length+(rc-rb).length)*.01:
        direction = posed @ rest.transposed() @ rest_bend
    elif limb.endswith('Leg'):
        direction = posed.col[2].copy()
    else:
        side = 1. if limb.startswith('Left') else -1.
        direction = posed.col[0]*side-posed.col[2]
    direction -= axis*direction.dot(axis)
    if direction.length < 1e-5:
        raise ValueError(f'Anatomical pole direction is collinear with the limb: {rig.name}/{limb}')
    return direction.normalized()


def _calibrate_pole(rig: bpy.types.Object, generated: dict[str, str],
                    matrices: tuple[Matrix, ...], direction: Vector) -> None:
    """Expose a finite solver bend, calibrate its roll, then restore the endpoint."""
    a, b, c = (matrix.translation for matrix in matrices)
    axis = (c-a).normalized()
    target = rig.pose.bones[generated['Target']]
    original_target = target.matrix.copy()
    solver_bone = rig.pose.bones[generated['Lower']]
    solver = solver_bone.constraints['EF Native Editing IK']
    shortened = original_target.copy()
    first_length, second_length = (b-a).length, (c-b).length
    margin = min(first_length, second_length)*.05
    minimum = abs(first_length-second_length)+margin
    maximum = first_length+second_length-margin
    calibration_reach = min(max((c-a).length*.9, minimum), maximum)
    shortened.translation = a+axis*calibration_reach
    try:
        target.matrix = shortened
        bpy.context.view_layer.update()
        # Measure the native solver's pose-dependent angular response. Its bend
        # does not necessarily rotate one radian for each radian of pole angle.
        for iteration in range(10):
            actual = solver_bone.head-a-axis*(solver_bone.head-a).dot(axis)
            if actual.length < 1e-5:
                raise ValueError(f'Native IK cannot establish a calibration bend: {rig.name}/{generated["Lower"]}')
            correction = _signed_angle(actual.normalized(), direction, axis)
            if abs(correction) < 1e-4:
                break
            angle = solver.pole_angle
            solver.pole_angle = (angle+.01+math.pi) % math.tau-math.pi
            bpy.context.view_layer.update()
            probe = solver_bone.head-a-axis*(solver_bone.head-a).dot(axis)
            if probe.length < 1e-5:
                raise ValueError(f'Native IK lost its calibration bend: {rig.name}/{generated["Lower"]}, pole_angle={angle}')
            response = _signed_angle(actual.normalized(), probe.normalized(), axis)/.01
            if abs(response) < 1e-4:
                raise ValueError(f'Native IK pole angle cannot steer its calibration bend: {rig.name}/{generated["Lower"]}, '
                                 f'pole_angle={angle}, angular_response={response}')
            step = max(-math.pi/2., min(math.pi/2., correction/response))
            solver.pole_angle = (angle+step+math.pi) % math.tau-math.pi
            bpy.context.view_layer.update()
        actual = solver_bone.head-a-axis*(solver_bone.head-a).dot(axis)
        if actual.length < 1e-5 or actual.normalized().dot(direction) < .999:
            raise ValueError(f'Native IK pole calibration disagrees with anatomy: {rig.name}/{generated["Lower"]}, '
                             f'iterations={iteration+1}, pole_angle={solver.pole_angle}, bend={tuple(actual)}, '
                             f'direction={tuple(direction)}')
    finally:
        target.matrix = original_target
        bpy.context.view_layer.update()


def _shape(rig: bpy.types.Object, label: str, size: float) -> bpy.types.Object:
    name = f'EF IK Widget :: {rig.name} :: {label}'
    mesh = bpy.data.meshes.new(name)
    vertices = [(math.cos(i * math.tau / 16) * size, 0., math.sin(i * math.tau / 16) * size)
                for i in range(16)]
    mesh.from_pydata(vertices, [(i, (i + 1) % 16) for i in range(16)], [])
    obj = bpy.data.objects.new(name, mesh)
    collection = next((item for item in bpy.data.collections
                       if item.get('endfield_rig_authoring_armature') == rig.name
                       and item.get('endfield_rig_authoring_role') == 'Rig Widgets'), None)
    if collection is None:
        raise ValueError(f'IK requires the existing rig authoring widget collection: {rig.name}')
    collection.objects.link(obj)
    obj.hide_render = True
    obj.hide_set(True)
    return obj


def _install_limb_ik(rig: bpy.types.Object, limb: str) -> dict[str, object]:
    """Create a two-segment solver in the current pose; preserve source axes."""
    names = limb_bones(rig, limb)
    state = json.loads(rig.get(STATE, '{}'))
    if limb in state:
        raise ValueError(f'IK controls already exist: {rig.name}/{limb}')
    upper, lower, end = (rig.pose.bones[name] for name in names)
    ancestor = lower.parent
    while ancestor is not None and ancestor != upper:
        ancestor = ancestor.parent
    if ancestor != upper:
        raise ValueError(f'Avatar limb is not a connected source hierarchy: {names}')
    ancestor = end.parent
    while ancestor is not None and ancestor != lower:
        ancestor = ancestor.parent
    if ancestor != lower:
        raise ValueError(f'Avatar end effector does not descend from its lower limb: {names}')
    matrices = tuple(bone.matrix.copy() for bone in (upper, lower, end))
    a, b, c = (matrix.translation for matrix in matrices)
    length = (b - a).length + (c - b).length
    direction = _pole_direction(rig, limb, matrices)
    pole_position = b + direction * length*.3
    prefix = 'EF IK ' + limb + ' :: '
    generated = {role: prefix + role for role in ('Upper', 'Lower', 'UpperOutput', 'LowerOutput', 'EndOutput', 'Target', 'Pole')}
    if any(name in rig.data.bones for name in generated.values()):
        raise ValueError(f'IK bone names collide with existing data: {prefix}')
    parent_name = upper.parent.name if upper.parent is not None else None
    solver_matrices = {'Upper': _basis(a, b), 'Lower': _basis(b, c),
                       'UpperOutput': matrices[0], 'LowerOutput': matrices[1], 'EndOutput': matrices[2],
                       'Target': matrices[2], 'Pole': Matrix.Translation(pole_position)}
    old_mode = rig.mode
    _activate(rig, 'EDIT')
    try:
        for role, name in generated.items():
            bone = rig.data.edit_bones.new(name)
            bone.matrix = solver_matrices[role]
            bone.length = {'Upper': (b-a).length, 'Lower': (c-b).length}.get(role, max(length * .06, .015))
            bone.use_deform = False
        edit = rig.data.edit_bones
        if parent_name is not None:
            edit[generated['Upper']].parent = edit[parent_name]
        edit[generated['Lower']].parent = edit[generated['Upper']]
        edit[generated['Lower']].use_connect = True
        edit[generated['Upper']].head, edit[generated['Upper']].tail = a, b
        edit[generated['Lower']].head, edit[generated['Lower']].tail = b, c
        edit[generated['UpperOutput']].parent = edit[generated['Upper']]
        edit[generated['LowerOutput']].parent = edit[generated['Lower']]
        edit[generated['EndOutput']].parent = edit[generated['Target']]
    finally:
        bpy.ops.object.mode_set(mode='POSE')
    upper, lower, end = (rig.pose.bones[name] for name in names)
    controls = rig.data.collections.get('IK Controls') or rig.data.collections.new('IK Controls')
    mechanism = rig.data.collections.get('IK Mechanism') or rig.data.collections.new('IK Mechanism')
    mechanism.is_visible = False
    for role, name in generated.items():
        bone = rig.data.bones[name]
        for collection in tuple(bone.collections):
            collection.unassign(bone)
        (controls if role in ('Target', 'Pole', 'EndOutput') else mechanism).assign(bone)
        rig.pose.bones[name].matrix = solver_matrices[role]
        bpy.context.view_layer.update()
    # A nearly straight imported pose otherwise leaves the legacy solver at a
    # collinear stationary point. This preferred bend is removed from the source
    # result by the output-axis calibration below.
    lower_solver = rig.pose.bones[generated['Lower']]
    lower_solver.matrix_basis = lower_solver.matrix_basis @ Matrix.Rotation(.05, 4, 'X')
    bpy.context.view_layer.update()
    solver = lower_solver.constraints.new('IK')
    solver.name = 'EF Native Editing IK'
    solver.target = rig
    solver.subtarget = generated['Target']
    solver.chain_count = 2
    solver.use_stretch = False
    solver.iterations = 128
    solver.pole_target = rig
    solver.pole_subtarget = generated['Pole']
    bpy.context.view_layer.update()
    _calibrate_pole(rig, generated, matrices, direction)
    endpoint_error = (rig.pose.bones[generated['Lower']].tail-c).length
    if endpoint_error > max(length * .001, .0001):
        raise ValueError(f'IK cannot reach the initial limb endpoint: {rig.name}/{limb}, error={endpoint_error}')
    # Source transform axes need not point along the anatomical segments.
    # Calibrate the output offsets after the solver chooses its bend-plane roll.
    for role, matrix in zip(('UpperOutput', 'LowerOutput', 'EndOutput'), matrices, strict=True):
        rig.pose.bones[generated[role]].matrix = matrix
        bpy.context.view_layer.update()
    error = max(max(abs(rig.pose.bones[generated[role]].matrix[i][j]-matrix[i][j])
                    for i in range(4) for j in range(4))
                for role, matrix in zip(('UpperOutput', 'LowerOutput', 'EndOutput'), matrices, strict=True))
    if error > 0.005:
        raise ValueError(f'IK initial pose mismatch before source constraints: {rig.name}/{limb}, error={error}')
    prop = 'IK · ' + LABELS[limb]
    rig[prop] = 1.0
    rig.id_properties_ui(prop).update(min=0., max=1., description='0: original FK/action; 1: hand/foot target and elbow/knee pole')
    for bone, role in zip((upper, lower, end), ('UpperOutput', 'LowerOutput', 'EndOutput'), strict=True):
        constraint = bone.constraints.new('COPY_ROTATION' if role == 'EndOutput' else 'COPY_TRANSFORMS')
        constraint.name = prefix + 'Source Output'
        constraint.target = rig
        constraint.subtarget = generated[role]
        constraint.owner_space = 'POSE'
        constraint.target_space = 'POSE'
        driver = constraint.driver_add('influence').driver
        driver.type = 'AVERAGE'
        variable = driver.variables.new()
        variable.type = 'SINGLE_PROP'
        variable.targets[0].id = rig
        variable.targets[0].data_path = '[' + json.dumps(prop, ensure_ascii=False) + ']'
    for role, scale in (('Target', .13), ('Pole', .07), ('EndOutput', .075)):
        pose = rig.pose.bones[generated[role]]
        pose.custom_shape = _shape(rig, limb + role, length * scale)
        pose.use_custom_shape_bone_size = False
        pose.color.palette = {'Target': 'THEME04', 'Pole': 'THEME03', 'EndOutput': 'THEME02'}[role]
        pose.lock_scale = (True, True, True)
        if role == 'EndOutput':
            pose.rotation_mode = 'XYZ'
            pose.lock_location = (True, True, True)
            pose.custom_shape_rotation_euler = (math.pi / 2., 0., 0.)
            pose['endfield_ik_rotation_control'] = True
        elif role == 'Pole':
            pose.lock_rotation = (True, True, True)
            pose.lock_rotation_w = True
    bpy.context.view_layer.update()
    state[limb] = {'bones': generated, 'source': names, 'property': prop, 'initial_error': error}
    rig[STATE] = json.dumps(state, ensure_ascii=False)
    if old_mode == 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    return state[limb]


def install_limb_ik(rig: bpy.types.Object, limb: str) -> dict[str, object]:
    """Preflight owned resources and roll back partial authoring on failure."""
    limb_bones(rig, limb)
    if rig.mode == 'EDIT':
        raise ValueError('Leave Edit Mode before creating limb IK')
    if not any(item.get('endfield_rig_authoring_armature') == rig.name
               and item.get('endfield_rig_authoring_role') == 'Rig Widgets'
               for item in bpy.data.collections):
        raise ValueError(f'IK requires the existing rig authoring widget collection: {rig.name}')
    if limb in json.loads(rig.get(STATE, '{}')):
        raise ValueError(f'IK controls already exist: {rig.name}/{limb}')
    prop = 'IK · ' + LABELS[limb]
    if prop in rig:
        raise ValueError(f'IK property name is already in use: {rig.name}/{prop}')
    widget_names = tuple(f'EF IK Widget :: {rig.name} :: {limb}{role}' for role in ('Target', 'Pole', 'EndOutput'))
    if any(name in bpy.data.objects or name in bpy.data.meshes for name in widget_names):
        raise ValueError(f'IK widget names already exist: {widget_names}')
    constraint_name = 'EF IK ' + limb + ' :: Source Output'
    if any(constraint_name in bone.constraints for bone in rig.pose.bones):
        raise ValueError(f'IK output constraint name already exists: {rig.name}/{constraint_name}')
    source_names = frozenset(rig.data.bones.keys())
    previous_state = rig.get(STATE)
    previous_collections = frozenset(rig.data.collections.keys())
    old_mode = rig.mode
    try:
        return _install_limb_ik(rig, limb)
    except Exception:
        # Roll back only this operation's resources, then expose the original error.
        prefix = 'EF IK ' + limb + ' :: '
        for bone in rig.pose.bones:
            for constraint in tuple(bone.constraints):
                if constraint.name == prefix + 'Source Output':
                    constraint.driver_remove('influence')
                    bone.constraints.remove(constraint)
        added = tuple(name for name in rig.data.bones.keys() if name not in source_names)
        if added:
            _activate(rig, 'EDIT')
            for name in added:
                rig.data.edit_bones.remove(rig.data.edit_bones[name])
            bpy.ops.object.mode_set(mode='POSE' if old_mode == 'POSE' else 'OBJECT')
        for name in widget_names:
            obj = bpy.data.objects.get(name)
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)
            mesh = bpy.data.meshes.get(name)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        if prop in rig:
            del rig[prop]
        if previous_state is None and STATE in rig:
            del rig[STATE]
        elif previous_state is not None:
            rig[STATE] = previous_state
        for name in ('IK Controls', 'IK Mechanism'):
            collection = rig.data.collections.get(name)
            if name not in previous_collections and collection is not None and not collection.bones:
                rig.data.collections.remove(collection)
        raise


def snap_limb_ik(rig: bpy.types.Object, limb: str) -> None:
    """Align existing controls to the current source FK frame and enable IK."""
    names = limb_bones(rig, limb)
    states = json.loads(rig.get(STATE, '{}'))
    if limb not in states:
        raise ValueError(f'Create IK before aligning its controls: {rig.name}/{limb}')
    state = states[limb]
    generated = state['bones']
    prop = state['property']
    previous_weight = rig[prop]
    previous_basis = {name: rig.pose.bones[name].matrix_basis.copy() for name in generated.values()}
    solver = rig.pose.bones[generated['Lower']].constraints['EF Native Editing IK']
    previous_mute, previous_angle = solver.mute, solver.pole_angle
    try:
        rig[prop] = 0.
        rig.update_tag()
        bpy.context.view_layer.update()
        matrices = tuple(rig.pose.bones[name].matrix.copy() for name in names)
        a, b, c = (matrix.translation for matrix in matrices)
        length = sum(rig.data.bones[generated[role]].length for role in ('Upper', 'Lower'))
        direction = _pole_direction(rig, limb, matrices)
        solver.mute = True
        for role, matrix in (('Upper', _basis(a, b)), ('Lower', _basis(b, c)),
                             ('Target', matrices[2]), ('Pole', Matrix.Translation(b + direction*length*.3))):
            rig.pose.bones[generated[role]].matrix = matrix
            bpy.context.view_layer.update()
        lower = rig.pose.bones[generated['Lower']]
        lower.matrix_basis = lower.matrix_basis @ Matrix.Rotation(.05, 4, 'X')
        solver.mute = False
        bpy.context.view_layer.update()
        _calibrate_pole(rig, generated, matrices, direction)
        for role, matrix in zip(('UpperOutput', 'LowerOutput', 'EndOutput'), matrices, strict=True):
            rig.pose.bones[generated[role]].matrix = matrix
            bpy.context.view_layer.update()
        error = (rig.pose.bones[generated['Lower']].tail-c).length
        if error > max(length*.001, .0001):
            raise ValueError(f'Current FK limb changed length beyond IK reach: {rig.name}/{limb}, error={error}')
        rig[prop] = 1.
        rig.update_tag()
        bpy.context.view_layer.update()
    except Exception:
        solver.mute, solver.pole_angle = previous_mute, previous_angle
        for name, basis in previous_basis.items():
            rig.pose.bones[name].matrix_basis = basis
        rig[prop] = previous_weight
        rig.update_tag()
        bpy.context.view_layer.update()
        raise


class ENDFIELD_OT_setup_character_ik(bpy.types.Operator):
    """Add native editing IK only after an explicit user action."""
    bl_idname = 'endfield.setup_character_ik'
    bl_label = '从当前姿态创建 IK'
    bl_options = {'REGISTER', 'UNDO'}
    rig_name: bpy.props.StringProperty()
    limb: bpy.props.EnumProperty(items=tuple((name, label, '') for name, label in LABELS.items()))

    def execute(self, context: bpy.types.Context) -> set[str]:
        rig = bpy.data.objects.get(self.rig_name) if self.rig_name else context.object
        if rig is None or rig.type != 'ARMATURE':
            raise ValueError('Select the source armature before creating IK')
        install_limb_ik(rig, self.limb)
        return {'FINISHED'}


class ENDFIELD_OT_snap_character_ik(bpy.types.Operator):
    """Snap current source FK to the generated controls before editing."""
    bl_idname = 'endfield.snap_character_ik'
    bl_label = '对齐当前 FK 并启用 IK'
    bl_options = {'REGISTER', 'UNDO'}
    rig_name: bpy.props.StringProperty()
    limb: bpy.props.EnumProperty(items=tuple((name, label, '') for name, label in LABELS.items()))

    def execute(self, context: bpy.types.Context) -> set[str]:
        rig = bpy.data.objects.get(self.rig_name) if self.rig_name else context.object
        if rig is None or rig.type != 'ARMATURE':
            raise ValueError('Select the source armature before aligning IK')
        snap_limb_ik(rig, self.limb)
        return {'FINISHED'}


class ENDFIELD_OT_select_ik_control(bpy.types.Operator):
    """Select an owned target, pole or end rotation control."""
    bl_idname = 'endfield.select_ik_control'
    bl_label = '选择 IK 控件'
    bl_options = {'REGISTER', 'UNDO'}
    rig_name: bpy.props.StringProperty()
    limb: bpy.props.EnumProperty(items=tuple((name, label, '') for name, label in LABELS.items()))
    control: bpy.props.EnumProperty(items=(('Target', '手脚目标', ''), ('Pole', '肘膝方向', ''),
                                          ('EndOutput', '手腕 / 脚踝旋转', '')))

    def execute(self, context: bpy.types.Context) -> set[str]:
        rig = bpy.data.objects.get(self.rig_name)
        if rig is None or rig.type != 'ARMATURE':
            raise ValueError(f'IK armature is missing: {self.rig_name}')
        state = json.loads(rig.get(STATE, '{}'))
        if self.limb not in state:
            raise ValueError(f'IK limb has not been created: {rig.name}/{self.limb}')
        name = state[self.limb]['bones'][self.control]
        if name not in rig.data.bones:
            raise ValueError(f'IK control bone is missing: {rig.name}/{name}')
        _activate(rig, 'POSE')
        rig.data.collections['IK Controls'].is_visible = True
        for bone in rig.data.bones:
            bone.select = bone.name == name
        rig.data.bones.active = rig.data.bones[name]
        return {'FINISHED'}


def register() -> None:
    owner = getattr(bpy.types, 'ENDFIELD_PT_character_tools_addon', None)
    if owner is not None and owner.__module__ != globals().get('__package__'):
        return
    for cls in (ENDFIELD_OT_setup_character_ik, ENDFIELD_OT_snap_character_ik, ENDFIELD_OT_select_ik_control):
        previous = getattr(bpy.types, cls.__name__, None)
        if previous is not None:
            bpy.utils.unregister_class(previous)
        bpy.utils.register_class(cls)


def attach_ik_tools() -> IKPanelReport:
    source = Path(__file__).read_text(encoding='utf-8') + '\nregister()\n'
    text = bpy.data.texts.get(TEXT_NAME) or bpy.data.texts.new(TEXT_NAME)
    text.clear()
    text.write(source)
    text.use_module = True
    register()
    return {'text': text.name, 'status': 'attached'}


def verify_ik_tools(report: IKPanelReport) -> str:
    text = bpy.data.texts.get(report['text'])
    if text is None or not text.use_module:
        raise AssertionError('Portable editing IK tools are missing')
    compile(text.as_string(), text.name, 'exec')
    return 'passed'
