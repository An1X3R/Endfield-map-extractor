"""Build verified authored ordinary cloth channels with explicit runtime boundaries."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys

from animator_auxiliary_layers import object_row, pose_from_row, read_json, rows, number, unit
from animator_selected_avatar_bindings import load_selected_avatar_clip, sample_input_paths
from animator_ordinary_source import evaluate_ordinary_frame
from animator_ordinary_plan import assess_ordinary_layers, select_ordinary_routes, validate_sampled_clip_pointers
from character_animator_graph import _integer as integer
from dataclasses import asdict


def validate_clip_execution_settings(settings: dict[str, object], samples_root: Path, indices: set[int]) -> None:
    """Reject unsupported clip clock/correction branches before emitting frames."""
    if settings.get('format') != 'EndfieldAuxiliaryClipExecutionSettings/1':
        raise ValueError('Expected exact auxiliary clip execution settings')
    entries = rows(settings['clips'], 'clip_execution_settings.clips')
    by_index = {int(number(object_row(row, 'clip.settings')['index'], 'clip.index')):
                object_row(row, 'clip.settings') for row in entries}
    if len(by_index) != len(entries) or set(by_index) != indices:
        raise ValueError('Clip execution settings must identify each selected Direct child exactly once')
    for index in sorted(indices):
        actual = by_index[index]
        sampled = object_row(read_json(samples_root/str(index)/'clip_source.json'), 'clip.source')
        if actual['cab'].lower() != sampled['cab'].lower() or actual['pathId'] != sampled['pathId'] or actual['rawDataSha256'].lower() != sampled['rawDataSha256'].lower():
            raise ValueError(f'Clip execution settings disagree with exact sampled object: index={index}')
        for field in ('m_LoopTime', 'm_LoopBlend', 'm_LoopBlendOrientation', 'm_LoopBlendPositionY', 'm_LoopBlendPositionXZ'):
            if actual[field] is not False:
                raise ValueError(f'Unsupported clip loop/correction branch: index={index}, field={field}, value={actual[field]}')
        if number(actual['m_CycleOffset'], 'clip.cycle_offset') != 0.:
            raise ValueError(f'Unsupported clip cycle offset: index={index}, value={actual["m_CycleOffset"]}')


def main(request_path: Path) -> None:
    request = object_row(read_json(request_path), 'request')
    if request.get('format') != 'EndfieldOrdinarySourceLayersRequest/1':
        raise ValueError('Unsupported auxiliary candidate request')
    if 'selected_avatar' not in request:
        raise ValueError('Verified ordinary layer evaluation requires selected_avatar: provide the exact exported avatar_parsed.json for this source hierarchy')
    if 'clip_execution_settings' not in request:
        raise ValueError('Verified ordinary layer evaluation requires clip_execution_settings: read the exact sampled clip loop/correction settings first')
    if 'controller_source_references' not in request or 'layer_binding_hashes' not in request:
        raise ValueError('Source-driven ordinary evaluation requires controller_source_references and explicit layer_binding_hashes from animator_ordinary_plan')
    paths = {key: Path(str(request[key])).resolve(strict=True) for key in
             ('controller_source_references','controller_type_tree','clip_samples_root','source_hierarchy','source_local_frames','parameters','selected_avatar','clip_execution_settings')}
    inputs = {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for key,path in paths.items() if key!='clip_samples_root'}
    tree = object_row(read_json(paths['controller_type_tree']), 'tree')
    references = object_row(read_json(paths['controller_source_references']), 'controller.references')
    controller = object_row(tree['m_Controller'], 'controller')
    hierarchy = rows(read_json(paths['source_hierarchy']), 'hierarchy')
    avatar = object_row(read_json(paths['selected_avatar']), 'selected_avatar')
    source_frames = rows(read_json(paths['source_local_frames']), 'source_frames')
    parameter_record = object_row(read_json(paths['parameters']), 'parameter_record')
    parameter_frames = rows(parameter_record['frames'], 'parameter_frames')
    if len(source_frames) != len(parameter_frames) or not source_frames:
        raise ValueError('Exact source and parameter frame counts disagree or are empty')
    assessments = assess_ordinary_layers(tree, references)
    hashes = tuple(integer(value, 'layer_binding_hash') for value in rows(request['layer_binding_hashes'], 'layer_binding_hashes'))
    routes = select_ordinary_routes(assessments, hashes)
    indices = {int(object_row(pointer,'pointer')['index']) for route in routes for pointer in object_row(route,'route')['child_clips']}
    inputs.update({str(path):hashlib.sha256(path.read_bytes()).hexdigest() for index in indices
        for path in sample_input_paths(paths['clip_samples_root']/str(index))})
    validate_sampled_clip_pointers(routes, paths['clip_samples_root'])
    validate_clip_execution_settings(object_row(read_json(paths['clip_execution_settings']), 'clip_execution_settings'), paths['clip_samples_root'], indices)
    clips = {index:load_selected_avatar_clip(paths['clip_samples_root']/str(index),avatar,hierarchy) for index in indices}
    output = Path(str(request['output'])).resolve()
    output.mkdir(parents=True,exist_ok=False)
    frames: list[dict[str,object]] = []
    metrics: list[dict[str,object]] = []
    for raw_frame,raw_parameter in zip(source_frames,parameter_frames,strict=True):
        source = object_row(raw_frame,'source_frame')
        parameter = object_row(raw_parameter,'parameter_frame')
        if abs(number(source['time'],'source.time')-number(parameter['time'],'parameter.time')) > 1e-7:
            raise ValueError('Exact parameter and source timestamps disagree')
        provenance = parameter['provenance']
        if not isinstance(provenance,str) or not provenance:
            raise ValueError('Parameter frame requires explicit source/runtime provenance')
        original = rows(source['locals'],'locals')
        candidate = evaluate_ordinary_frame(original,hierarchy,controller,routes,clips,object_row(parameter['parameters'],'parameters'))
        frames.append(dict(time=source['time'],locals=candidate))
        maximum_angle=0.
        maximum_position=0.
        changed_paths: list[str] = []
        for node,before,after in zip(hierarchy,original,candidate,strict=True):
            a,b=pose_from_row(before),pose_from_row(after)
            dot=min(1.,abs(sum(x*y for x,y in zip(unit(a.rotation),unit(b.rotation),strict=True))))
            angle=math.degrees(2*math.acos(dot))
            displacement=max(abs(x-y) for x,y in zip(a.position,b.position,strict=True))
            maximum_angle=max(maximum_angle,angle)
            maximum_position=max(maximum_position,displacement)
            if angle>1e-4 or displacement>1e-7:
                changed_paths.append(str(object_row(node,'node')['path']))
        metrics.append(dict(time=source['time'],maximum_local_rotation_difference_deg=maximum_angle,
                            maximum_local_position_difference=maximum_position,changed_paths=changed_paths))
    changed_inputs = [path for path,digest in inputs.items() if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest]
    if changed_inputs:
        raise ValueError(f'Ordinary source inputs changed during evaluation: paths={changed_inputs}')
    (output/'source_local_frames.json').write_text(json.dumps(frames,separators=(',',':')),encoding='utf-8')
    (output/'source_hierarchy.json').write_text(json.dumps(hierarchy,separators=(',',':')),encoding='utf-8')
    maximum_frame=max(range(len(metrics)),key=lambda index:metrics[index]['maximum_local_rotation_difference_deg'])
    receipt=dict(format='EndfieldAuxiliaryLayerCandidate/1',status='native_verified_ordinary_authored_channels_source_scenario',frames=len(frames),
        layers=[object_row(route,'route')['layer'] for route in routes],layer_binding_hashes=list(hashes),
        layer_plan=[asdict(row) for row in assessments],
        controller_source=dict(name=tree['m_Name'],cab=references['source'],path_id=references['controller_path_id']),
        clips=sorted(clips),parameters=str(paths['parameters']),parameter_status=parameter_record.get('status'),
        input_sha256=inputs,output_sha256=hashlib.sha256((output/'source_local_frames.json').read_bytes()).hexdigest(),
        maximum_change_frame_index=maximum_frame,per_frame_metrics=metrics,source_clock='first/last stored frame normalized interval',
        rotation_implementation='Native reference subtraction -> source-order Direct hemisphere-aware weighted accumulation -> ordinary additive layer quaternion application',
        layer_weight_implementation='serialized m_DefaultWeight; unique binary skeleton mask channels',runtime_state_reconstructed=False,
        reference_flattening_scope='exact selected Avatar binding; native empty-reference ValueDelta start branch, native ordinary reference/Direct/additive arithmetic verified by bounded offline oracle',
        clip_execution_scope='exact object hashes checked; all selected clips have loop/correction flags false and zero cycle offset; unsupported settings fail explicitly',
        pending=['WriteDefaultValues per-channel default write lists are not applied; only source-authored channels are evaluated', 'Humanoid channels and independent child clocks remain unsupported',
                 'Explicit selected source-default-state layer subset; unselected and unsupported layers remain listed in layer_plan',
                 'Complete layer scheduler, native interpolation and live parameter/gate state are not reconstructed'])
    (output/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps(dict(frames=len(frames),maximum_change_frame_index=maximum_frame,
         maximum_local_rotation_difference_deg=metrics[maximum_frame]['maximum_local_rotation_difference_deg'])))


if __name__=='__main__':
    if len(sys.argv)!=2:
        raise ValueError('Expected one auxiliary candidate request path')
    main(Path(sys.argv[1]))
