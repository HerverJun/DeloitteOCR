"""Apply one frozen metadata policy to sealed native observations, CPU only.

Source text/caption hashes are reconstructed only from actual saved result
rows in a byte-copied database. Cell values, topology, original text, model
choices and delivery/review flags remain unchanged. This is derived developer
evidence, not a newly exported native result or Agent delivery.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import sqlite3
import sys

from common import digest, fpath, fingerprint, frozen_json, read_json
from controlled_recovery import bound, plain_path, require_environment


def table_values(raw):
    return [{k:v for k,v in table.items() if k!='caption'} for table in raw['tables']]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','observations','baseline-quality','sealed-database','truth','output-root'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args();require_environment()
    source,observations_path,baseline_path,database,truth,output=map(plain_path,
        (args.source,args.observations,args.baseline_quality,args.sealed_database,args.truth,args.output_root))
    if output.exists():raise ValueError('Use a unique metadata replay output')
    baseline=read_json(baseline_path);observations=read_json(observations_path)
    if (baseline['bindings']['observations_sha256']!=digest(observations_path)
            or baseline['bindings']['truth_sha256']!=digest(truth)
            or baseline['bindings']['inputs_sha256']!=observations['inputs_sha256']
            or len(observations['records'])!=baseline['summary']['totals']['images']):
        raise ValueError('Sealed baseline scope/source bindings differ')
    freeze=read_json(truth.parent/'freeze.json')
    if digest(truth)!=freeze['truth_sha256'] or observations['inputs_sha256']!=freeze['inputs_sha256']:
        raise ValueError('Frozen input/truth changed')
    output.mkdir(parents=True)
    actual=output/'sqlite-copy/native.sqlite3';actual.parent.mkdir()
    db_sources={}
    for member in (database,database.with_name(database.name+'-wal'),database.with_name(database.name+'-shm')):
        if member.exists():
            target=actual.with_name(actual.name+member.name[len(database.name):])
            shutil.copyfile(member,target)
            if digest(target)!=digest(member):raise ValueError('Saved database byte copy differs')
            db_sources[str(member)]=bound(member)
    sys.path.insert(0,str(source/'src'))
    from ocr_workbench.screen_evidence import apply_screen_business_metadata
    from quality import evaluate, paired_report
    policy_source=source/'src/ocr_workbench/screen_evidence.py'
    policy_binding=bound(policy_source)
    copied=deepcopy(observations['records']);sources=[];checks=[]
    with closing(sqlite3.connect(actual.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        for record in copied:
            if record['status']!='success':
                checks.append({'id':record['id'],'status':record['status'],'retained_unavailable_output':True});continue
            raw=deepcopy(record['candidate']['original']);screen=raw['screen'];texts=[]
            if screen['input_sha256']!=record['image_sha256']:
                raise ValueError('Candidate input pixel identity differs')
            preparation=screen['prepared'];auxiliary=screen['business_metadata_evidence']['auxiliary_evidence']
            if (preparation['input_sha256']!=record['image_sha256']
                    or digest(plain_path(preparation['input_path']))!=record['image_sha256']):
                raise ValueError('Metadata preparation is not the actual bound original')
            for item in screen['sources']:
                row=db.execute('SELECT id,task_id,revision,original,edited FROM results WHERE id=?',(item['result_id'],)).fetchone()
                if row is None or row['task_id']!=item['task_id'] or row['revision']!=item['revision']:
                    raise ValueError('Selected source result/task/revision differs')
                original=__import__('json').loads(row['original']);edited=__import__('json').loads(row['edited'])
                if original['engine']!=item['engine']:
                    raise ValueError('Selected source engine differs')
                text=edited.get('text','');texts.append(text)
                item['text_sha256']=hashlib.sha256(text.encode('utf-8')).hexdigest()
                item['table_caption']=edited['tables'][0].get('caption','') if len(edited.get('tables',[]))==1 else None
                sources.append({'sample_id':record['id'],'result_id':row['id'],'task_id':row['task_id'],
                    'revision':row['revision'],'text_sha256':item['text_sha256'],
                    'actual_edited_sha256':hashlib.sha256(row['edited'].encode('utf-8')).hexdigest()})
            combined_text_matches_source='\n\n'.join(texts)==raw['text']
            # Some old full-document adapters changed their combined display
            # text while retaining a different selected source text. Preserve
            # both actual strings: the production primary-metadata binding
            # must reject this mismatch, never manufacture a compatible SHA.
            originals=[p for p in preparation['prepared_images'] if p.get('kind')=='original'
                and p.get('prepared_to_source')==[1,0,0,0,1,0,0,0,1]
                and p.get('source_to_prepared')==[1,0,0,0,1,0,0,0,1]]
            if len(originals)==1:
                path=plain_path(originals[0]['path'])
                if digest(path)!=record['image_sha256']:raise ValueError('Original pixel source changed')
                screen['context_pixel_source']={'path':str(path),'sha256':digest(path),
                    'image_version':raw['project_image_version'],'input_sha256':screen['input_sha256']}
            prior=deepcopy(raw)
            after=apply_screen_business_metadata(raw,preparation,auxiliary)
            if (not combined_text_matches_source
                    and after['screen']['business_metadata_evidence']['primary_metadata_comparisons']):
                raise ValueError('Primary metadata accepted a mismatched actual source text')
            if (raw!=prior or table_values(after)!=table_values(prior)
                    or after['text']!=prior['text'] or after['blocks']!=prior['blocks']
                    or apply_screen_business_metadata(after,preparation,auxiliary)!=after):
                raise ValueError('Metadata policy changed cell/text content, input, or idempotence')
            record['candidate']['original']=after
            record['candidate']['edited']['tables']=deepcopy(after['tables'])
            checks.append({'id':record['id'],'values_topology_raw_text_blocks_unchanged':True,'idempotent':True,
                           'binding_reconstructed_from_actual_database':True,
                           'combined_text_matches_actual_selected_source':combined_text_matches_source,
                           'mismatched_primary_metadata_binding_rejected':not combined_text_matches_source,
                           'review_delivery_flags_unchanged':True})
    if digest(policy_source)!=policy_binding['sha256']:
        raise ValueError('Frozen metadata policy changed during replay')
    after_observations={**observations,'records':copied,'evidence_kind':'development_cpu_derived_metadata_replay',
        'derivation':{'policy':policy_binding,'actual_observations':bound(observations_path),
                      'saved_source_database':db_sources,'source_sqlite_opened':False,
                      'native_selection_or_delivery_mutated':False,'model_or_controller_invoked':False}}
    frozen_json(output/'observations.json',after_observations)
    after_quality=evaluate(read_json(truth)['samples'],copied)
    if ([r['errors'] for r in baseline['records']]!=[r['errors'] for r in after_quality['records']]
            or baseline['summary']['totals']['images']!=after_quality['summary']['totals']['images']):
        raise ValueError('Metadata-only policy changed cell errors or complete scope')
    after_quality['bindings']={'truth_sha256':digest(truth),'observations_sha256':digest(output/'observations.json'),
        'inputs_sha256':observations['inputs_sha256'],'evidence_kind':'development_cpu_derived_metadata_replay'}
    after_quality['actual_model_evidence']=False
    frozen_json(output/'quality.json',after_quality)
    before_totals=baseline['summary']['totals'];after_totals=after_quality['summary']['totals']
    report={'format':'m1-common-metadata-policy-native-replay-v1','utc':datetime.now(timezone.utc).isoformat(),
        'status':'passed','scope_images':len(copied),'policy':policy_binding,'before':baseline['summary'],
        'after':after_quality['summary'],'paired':paired_report(baseline,after_quality),
        'metadata_exact_targets_delta':after_totals['metadata_exact_targets']-before_totals['metadata_exact_targets'],
        'metadata_extra_parts_delta':after_totals['metadata_extra_parts']-before_totals['metadata_extra_parts'],
        'ui_marker_leaks_delta':after_totals['ui_marker_leaks']-before_totals['ui_marker_leaks'],
        'source_bindings':sources,'product_checks':checks,'all_original_cell_errors_unchanged':True,
        'truth_used_only_by_independent_scorer':True,'holdout_read':False,'source_sqlite_opened':False,
        'model_or_controller_invoked':False,'actual_native_delivery_or_review_states_changed':False,
        'human_workload_reduction_measured':False,'automatic_delivery_qualification':False,
        'bindings':{k:bound(p) for k,p in {'helper':Path(__file__),'baseline_quality':baseline_path,
            'original_observations':observations_path,'derived_observations':output/'observations.json',
            'derived_quality':output/'quality.json','truth':truth}.items()}}
    frozen_json(output/'result.json',report)
    print({'scope':len(copied),'metadata_exact_delta':report['metadata_exact_targets_delta'],
           'metadata_extra_delta':report['metadata_extra_parts_delta'],'ui_delta':report['ui_marker_leaks_delta'],
           'automatic_delivery_qualification':False})


if __name__=='__main__':main()
