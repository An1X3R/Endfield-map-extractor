"""Prepare an explicit character selection through the existing production API."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

def main(args: argparse.Namespace) -> None:
    sys.path.insert(0,str(args.repository_src))
    from endfield_character_selection import parse_character_selection
    from endfield_character_cache import CharacterDataCache
    from endfield_character_prepare import (CharacterBundlePreparationRequest,CharacterPreparationProgress,
        prepare_character_bundle_set,prepare_character_bundle_set_cached)
    catalog=json.loads(args.catalog.read_text(encoding='utf-8-sig'))
    selection=parse_character_selection({'catalog_identity':catalog['source_identity'],'character_id':args.character_id,'template_id':args.template_id,'part_name':args.part_name,'variant':args.variant or 'characters','resource_path':args.resource_path,'logical_provider':args.provider})
    request=CharacterBundlePreparationRequest(selection,args.game_root,Path(catalog['provider_index_source']['path']),args.database,args.cab_inventory,args.output/'bundles')
    def cancel() -> None:
        return None
    def progress(event: CharacterPreparationProgress) -> None:
        if event.stage not in ('hashing_bundles','extracting_bundles') or event.fraction>=.83:
            print(json.dumps(asdict(event),ensure_ascii=False),flush=True)
    start=time.perf_counter()
    if args.cache_dir is None:
        result=prepare_character_bundle_set(request,cancel,progress)
    else:
        result=prepare_character_bundle_set_cached(request,cancel,progress,CharacterDataCache(args.cache_dir))
    if len(result.provider_serialized_cabs)!=1:
        raise ValueError('Explicit source root CAB requires exactly one selected provider CAB')
    manifest={'format':'EndfieldSingleCharacterInput/1','unity_version':args.unity_version,'bundles':[{'path':r.path,'bytes':r.bytes,'sha256':r.sha256} for r in result.extracted_bundles],'root':{'source':result.provider_serialized_cabs[0].name,'resource_path':selection.resource_path}}
    target=args.output/'input.json';target.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    receipt={'format':'EndfieldCharacterPreparationInvocation/1','elapsed_seconds':time.perf_counter()-start,'selection':asdict(selection),'root_cab':result.provider_serialized_cabs[0].name,'bundle_count':len(result.extracted_bundles),'bundle_bytes':sum(r.bytes for r in result.extracted_bundles),'bundle_receipt':result.receipt_path,'input':str(target),'input_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'game_and_resource_inputs':'read_only'}
    (args.output/'preparation_receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps(receipt),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('repository-src','catalog','game-root','database','cab-inventory','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--cache-dir',type=Path)
    parser.add_argument('--variant',choices=('characters','npc'))
    for name in ('character-id','template-id','part-name','resource-path','provider','unity-version'):parser.add_argument('--'+name,required=True)
    main(parser.parse_args())
