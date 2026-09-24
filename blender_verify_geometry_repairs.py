"""Independently verify evaluated repaired instances and per-material triangle counts."""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
import bpy
import numpy as np
from mathutils import Matrix

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blender_add_projected_decals import shader_matrix
from endfield_scene_slot_contract import static_submesh_slots
from endfield_scene_material_contract import resolve_export_path

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repairs',type=Path,required=True)
parser.add_argument('--audits',type=Path,nargs='+',required=True)
parser.add_argument('--output',type=Path,required=True)
options=parser.parse_args(sys.argv[sys.argv.index('--')+1:])
if options.output.exists():
    raise FileExistsError(options.output)
payload=json.loads(options.repairs.read_text(encoding='utf-8-sig'))
if payload.get('format')!='EndfieldGeometryRepairPlan/1':
    raise ValueError('Repair plan must use EndfieldGeometryRepairPlan/1')
repairs={row['entity_id']:row for row in payload['instances']}
if len(repairs)!=len(payload['instances']):
    raise ValueError('Repair plan contains duplicate entity IDs')
basis=Matrix(((1,0,0,0),(0,0,-1,0),(0,1,0,0),(0,0,0,1)))
results=[]
for file in options.audits:
    audit=json.loads(file.read_text())
    bpy.ops.wm.open_mainfile(filepath=audit['output'])
    selected_templates = {row['object'] for row in audit['templates']}
    actual=defaultdict(list)
    for item in bpy.context.evaluated_depsgraph_get().object_instances:
        obj=item.object
        if not item.is_instance or obj.type!='MESH' or obj.original.name not in selected_templates:
            continue
        matrix=item.matrix_world.copy()
        key=(obj.original['ecs_mesh_path'],tuple(round(v,4) for v in matrix.translation))
        actual[key].append(np.array(matrix))
    largest_error=0.0
    for eid in audit['repaired_entity_ids']:
        row=repairs[eid]
        matrix=basis@shader_matrix(tuple(row['matrix']))@basis.inverted()
        location,rotation,scale=matrix.decompose()
        expected=np.array(Matrix.LocRotScale(location,rotation,scale))
        key=(row['current_primary_mesh_path'],tuple(round(v,4) for v in location))
        candidates=actual[key]
        if not candidates:
            raise ValueError(f'Repaired evaluated instance missing: entity_id={eid}')
        errors=[float(np.max(np.abs(candidate-expected))) for candidate in candidates]
        index=min(range(len(errors)),key=errors.__getitem__)
        if errors[index]>0.0003:
            raise ValueError(f'Repaired instance transform differs: entity_id={eid}, error={errors[index]}')
        largest_error=max(largest_error,errors[index])
        candidates.pop(index)
    if any(actual.values()):
        raise ValueError('Unexpected extra repaired evaluated instances')
    slot_results=[]
    for template in audit['templates']:
        source=bpy.data.objects[template['object']]
        expected_ids=json.loads(source['ecs_material_ids'])
        renderer_ids=json.loads(source.get('ecs_renderer_material_ids', source['ecs_material_ids']))
        mapping = static_submesh_slots(len(source.data.materials), tuple(renderer_ids))
        if expected_ids != [renderer_ids[index] for index in mapping.indices]:
            raise ValueError(f'Static slots differ from original renderer references: source={source.name}')
        actual_ids=[mat['ecs_material_id'] for mat in source.data.materials]
        if actual_ids!=expected_ids:
            raise ValueError(f'Material slot order differs: source={source.name}')
        row=next(repairs[eid] for eid in audit['repaired_entity_ids']
                 if repairs[eid]['current_primary_mesh_path']==template['mesh_path']
                 and [p['material_id'] for p in repairs[eid]['raw_material_pairs']]==renderer_ids)
        expected_counts=Counter()
        duplicate_counts=Counter()
        source_faces=set()
        group=None
        with resolve_export_path(Path(row['import_file'])).open(encoding='utf-8') as handle:
            for line in handle:
                if line.startswith('g '):
                    match=re.search(r'_(\d+)$',line.strip())
                    group=int(match.group(1)) if match else None
                elif line.startswith('f '):
                    if group is None or group>=len(expected_ids):
                        raise ValueError('Source OBJ face lacks a valid material group')
                    # The OBJ importer removes exact duplicate faces. Require the
                    # same group, winding, vertex, UV and normal references.
                    face=(group, tuple(line.split()[1:]))
                    if face in source_faces:
                        duplicate_counts[expected_ids[group]]+=len(face[1])-2
                        continue
                    source_faces.add(face)
                    expected_counts[expected_ids[group]]+=len(line.split())-3
        source.data.calc_loop_triangles()
        actual_counts=Counter(actual_ids[triangle.material_index] for triangle in source.data.loop_triangles)
        if actual_counts!=expected_counts:
            raise ValueError(f'Per-material geometry differs: source={source.name}, expected={dict(expected_counts)}, actual={dict(actual_counts)}, import_file={row["import_file"]}')
        if not source.data.uv_layers:
            raise ValueError(f'Repaired mesh lacks source UV coordinates: source={source.name}')
        slot_results.append({'template':source.name,'material_triangle_counts':dict(actual_counts),
                             'exact_duplicate_source_triangles_removed':dict(duplicate_counts),'source_uv_preserved':True})
    if any(image.source=='FILE' and image.packed_file is None for image in bpy.data.images):
        raise ValueError('Delivery includes an unpacked image')
    results.append({'output':audit['output'],'evaluated_repaired_instances':len(audit['repaired_entity_ids']),
                    'maximum_transform_error':largest_error,'material_slots':slot_results,'images_packed':True})
    print(json.dumps({'event':'evaluated_repairs_verified','output':audit['output'],'count':len(audit['repaired_entity_ids'])}),flush=True)
options.output.write_text(json.dumps(results,indent=2),encoding='utf-8')
