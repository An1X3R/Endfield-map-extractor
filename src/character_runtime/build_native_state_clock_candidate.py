"""Bake native-backed ordinary leaf clocks into the existing source pose consumer.

Only external parameter phase, initial normalized clock, elapsed seconds and
global speed are caller inputs. Leaf duration, reset mode and clock arithmetic
come from validated serialized source and original native StateEvaluate.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

from animator_auxiliary_layers import AuxiliaryClip, number, object_row, read_json, rows
from animator_ordinary_plan import validate_sampled_clip_pointers
from animator_selected_avatar_bindings import load_selected_avatar_clip
from animator_state_schedule import source_machine, select_transition, clip_state
from animator_state_clock import (source_clock_policy, NativeStateClock, NativeClockSchedule,
    advance_native_clock_schedule, native_clock_ports)
from animator_state_pose import evaluate_state_pose
from character_animator_graph import _integer, _names


def main(request_path: Path) -> None:
    request = object_row(read_json(request_path),'request')
    if request['format']!='EndfieldNativeLeafClockScenarioRequest/1':
        raise ValueError('Expected EndfieldNativeLeafClockScenarioRequest/1')
    if request['parameter_boundary_convention']!='parameters_before_interval':
        raise ValueError('Caller must explicitly supply parameters_before_interval; live parameter phase is not reconstructed')
    paths = {key:Path(str(request[key])).resolve(strict=True) for key in
        ('controller_type_tree','controller_source_references','selected_avatar','source_hierarchy','source_local_frames','parameters','clip_samples_root')}
    tracked = {path:hashlib.sha256(path.read_bytes()).hexdigest() for key,path in paths.items() if key!='clip_samples_root'}
    tree = object_row(read_json(paths['controller_type_tree']),'tree')
    references = object_row(read_json(paths['controller_source_references']),'references')
    binding = _integer(request['layer_binding_hash'],'layer_binding_hash')
    machine = source_machine(tree,references,binding)
    policy = source_clock_policy(tree,binding)
    layers = rows(object_row(tree['m_Controller'],'controller')['m_LayerArray'],'layers')
    layer = next(object_row(object_row(row,'layer')['data'],'layer.data') for row in layers
        if object_row(object_row(row,'layer')['data'],'layer.data')['m_Binding']==binding)
    hierarchy = rows(read_json(paths['source_hierarchy']),'hierarchy')
    avatar = object_row(read_json(paths['selected_avatar']),'avatar')
    frames = rows(read_json(paths['source_local_frames']),'frames')
    parameters = rows(object_row(read_json(paths['parameters']),'parameter_record')['frames'],'parameter.frames')
    if not frames or len(frames)!=len(parameters):
        raise ValueError('Native clock scenario source/parameter frame counts must agree and be nonempty')
    initial = object_row(request['initial_clock'],'initial_clock')
    state = _integer(initial['state_index'],'initial_clock.state_index')
    normalized = number(initial['normalized'],'initial_clock.normalized')
    if not 0<=state<len(machine.states) or normalized<0.:
        raise ValueError('Initial native state clock is outside source machine or negative')
    schedule = NativeClockSchedule(NativeStateClock(state,normalized),None)
    name_hashes = {value:key for key,value in _names(tree).items()}
    clips: dict[int,AuxiliaryClip] = {}
    durations: dict[int,float] = {}
    output_frames: list[dict[str,object]] = []
    traces: list[dict[str,object]] = []
    previous_time = number(object_row(frames[0],'frame')['time'],'frame.time')
    for raw_frame,raw_parameters in zip(frames,parameters,strict=True):
        frame = object_row(raw_frame,'frame')
        sample = object_row(raw_parameters,'parameters')
        time = number(frame['time'],'frame.time')
        if abs(time-number(sample['time'],'parameter.time'))>1e-7 or time<previous_time:
            raise ValueError('Source/parameter timestamps must agree and be monotonic')
        provenance = sample['provenance']
        if not isinstance(provenance,str) or not provenance:
            raise ValueError('Every explicit parameter/global speed sample requires provenance')
        values = object_row(sample['parameters'],'parameter.values')
        missing = tuple(name for name in values if name not in name_hashes)
        if missing:
            raise ValueError(f'Parameter names must be exact controller TOS names: names={missing}')
        by_hash = {name_hashes[name]:value for name,value in values.items()}
        global_speed = number(sample['global_speed'],'parameter.global_speed')
        current = schedule.current if schedule.transition is None else schedule.transition.source
        entered = [current.state]
        if schedule.transition is not None:
            entered.append(schedule.transition.destination.state)
        else:
            choice = select_transition(machine,current.state,by_hash)
            if choice is not None:
                entered.append(choice.destination)
        for entered_state in entered:
            pointer,_,_ = clip_state(machine,entered_state,by_hash)
            if pointer.index not in clips:
                directory = paths['clip_samples_root']/str(pointer.index)
                for name in ('clip_source.json','value_array_delta.json','transform_samples.json','float_samples.json','root_motion_samples.json'):
                    path = directory/name
                    tracked[path] = hashlib.sha256(path.read_bytes()).hexdigest()
                validate_sampled_clip_pointers([{'child_clips':[asdict(pointer)]}],paths['clip_samples_root'])
                source = object_row(read_json(directory/'clip_source.json'),'clip_source')
                if number(source['start'],'clip.start')!=0. or source['m_LoopTime'] is not True or number(source['m_CycleOffset'],'clip.cycle_offset')!=0.:
                    raise ValueError(f'Native clock pose sampler requires zero-start looping zero-offset source clip: index={pointer.index}')
                durations[pointer.index] = number(source['stop'],'clip.stop')-number(source['start'],'clip.start')
                clips[pointer.index] = load_selected_avatar_clip(directory,avatar,hierarchy)
        schedule = advance_native_clock_schedule(machine,policy,schedule,by_hash,durations,time-previous_time,global_speed)
        ports = native_clock_ports(machine,schedule,by_hash,durations)
        output_frames.append({'time':frame['time'],'locals':evaluate_state_pose(rows(frame['locals'],'frame.locals'),hierarchy,layer,ports,clips)})
        traces.append({'time':frame['time'],'delta_seconds':time-previous_time,'global_speed':global_speed,
            'schedule':asdict(schedule),'ports':[asdict(port) for port in ports],'parameter_provenance':provenance})
        previous_time = time
    for path,digest in tracked.items():
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError(f'Native clock scenario input changed during evaluation: path={path}')
    output = Path(str(request['output'])).resolve()
    output.mkdir(parents=True,exist_ok=False)
    for name,value in (('source_local_frames.json',output_frames),('source_hierarchy.json',hierarchy),('state_schedule.json',traces)):
        (output/name).write_text(json.dumps(value,separators=(',',':'),allow_nan=False),encoding='utf-8')
    receipt = {'format':'EndfieldNativeLeafClockScenarioCandidate/1','status':'native_leaf_clock_source_scenario_partial_runtime',
        'layer_binding_hash':binding,'layer_name':machine.name,'frames':len(frames),'clip_indices':sorted(clips),
        'baseline_source_local_frames':str(paths['source_local_frames']),'initial_clock':initial,
        'serialized_clock_policy':asdict(policy),'runtime_state_reconstructed':False,
        'parameter_boundary_convention':request['parameter_boundary_convention'],
        'input_sha256':{str(path):digest for path,digest in tracked.items()},
        'output_sha256':{name:hashlib.sha256((output/name).read_bytes()).hexdigest() for name in
            ('source_local_frames.json','source_hierarchy.json','state_schedule.json')},
        'implementation':'Native current normalized advance -> source condition selection -> source immediate/deferred destination reset -> native fixed transition weight -> existing verified ordinary pose ports',
        'pending':['External delta/global speed, initial state/normalized clock and parameter phase are explicit reproducible caller inputs; no live scheduler capture',
                   'The full transition schedule requires unit global speed; variable speed is verified only for the isolated native leaf-clock kernel',
                   'Stored pose interpolation, loop correction/wrap, missing-channel WriteDefaults, triggers, exits, active interruptions, Human states and dynamic synchronized/layer gates are not reconstructed',
                   'Unwrapped native clip time outside the verified cached sample interval fails explicitly; there is no authored modulo fallback']}
    (output/'receipt.json').write_text(json.dumps(receipt,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'status':receipt['status'],'frames':len(frames),'output':str(output)}))


if __name__=='__main__':
    if len(sys.argv)!=2:
        raise ValueError('Expected one explicit native leaf clock scenario request path')
    main(Path(sys.argv[1]))
