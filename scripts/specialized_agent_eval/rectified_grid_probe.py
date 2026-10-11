"""Freeze source-only grid replay and independently score source polygons."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import importlib
import os
from pathlib import Path
import sys
import time

from common import digest, fpath, fingerprint, frozen_bytes, frozen_json, read_json
from controlled_recovery import bound, require_environment

MODULES=('__init__','coordinates','image_utils','screen_diagnostics','screen_rectified_grid','screen_imaging')


def freeze(args):
    source,output=map(fpath,(args.source,args.output))
    if output.exists():raise ValueError('New frozen source identity required')
    output.mkdir(parents=True)
    files={}
    for name in MODULES:
        relative='src/ocr_workbench/'+name+'.py'
        frozen_bytes(output/relative,(source/relative).read_bytes())
        files[relative]=digest(output/relative)
    frozen_bytes(output/'probe.py',Path(__file__).read_bytes())
    frozen_json(output/'policy.json',{'format':'source-rectified-grid-replay-v1','files':files,
        'probe':bound(output/'probe.py'),'independent_coordinate_iou_minimum':.9,
        'independent_coordinate_maximum_corner_error_pixels':5.,
        'reference_semantics':'Synthetic box is full cell. canonical_pdf_bbox marks public text bounds: check unique containment, not full-cell equality.',
        'independent_text_containment_minimum':.9,
        'scope':'development/previously-exposed validation only; holdout/challenge forbidden',
        'values_or_review_changed':False,'semantic_header_release':False,'adoption_qualified':False})
    print({'modules':len(files),'policy_sha256':digest(output/'policy.json')})


def run(args):
    capsule,inputs_path,observations_path,output=map(fpath,
        (args.capsule,args.inputs,args.observations,args.output))
    if output.exists() or inputs_path.parent.name not in {'development','validation'}:
        raise ValueError('New development/validation replay identity required')
    policy=read_json(capsule/'policy.json')
    if bound(capsule/'probe.py')!=policy['probe'] or any(digest(capsule/name)!=sha for name,sha in policy['files'].items()):
        raise ValueError('Frozen module/helper changed')
    original,inputs=read_json(observations_path),read_json(inputs_path)
    samples={row['id']:row for row in inputs['samples']}
    records={row['id']:row for row in original['records']}
    if (original['inputs_sha256']!=digest(inputs_path) or inputs['truth_available_to_inference'] is not False
            or len(records)!=len(original['records']) or len(samples)!=len(inputs['samples']) or records.keys()!=samples.keys()):
        raise ValueError('Full source-only denominator differs')
    for sid,row in records.items():
        if row['image_sha256']!=samples[sid]['image_sha256'] or digest(samples[sid]['image'])!=row['image_sha256']:
            raise ValueError('Original source pixels changed')
    output.mkdir(parents=True)
    def audit(event,values):
        if event in {'subprocess.Popen','socket.connect','socket.getaddrinfo','os.system'}:
            raise ValueError('CPU source-only replay cannot invoke model/network')
        if event!='open' or not isinstance(values[0],(str,bytes,os.PathLike)):return
        path=Path(os.fsdecode(values[0])).absolute()
        mode,flags=values[1:3]
        writing=(isinstance(mode,str) and any(c in mode for c in 'wax+') or isinstance(flags,int)
            and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
        if writing and not path.resolve().is_relative_to(output):raise ValueError('Replay write escaped output')
        if 'truth' in path.name.lower() or any(part in {'holdout','challenge'} for part in path.parts):
            raise ValueError('Reference/final input forbidden in geometry replay')
    sys.addaudithook(audit)
    sys.path.insert(0,str(capsule/'src'))
    native=importlib.import_module('ocr_workbench.screen_diagnostics')
    decisions=[];start=time.monotonic()
    for record in original['records']:
        decision={'id':record['id'],'before_cells':0,'after_cells':0,'verified':False}
        if record['status']=='success':
            raw=deepcopy(record['candidate']['original'])
            raw['tables']=deepcopy(record['candidate'].get('edited',{}).get('tables',raw['tables']))
            prep=raw['screen']['prepared']
            evidence=raw['screen']['business_metadata_evidence']['auxiliary_evidence']
            before=fingerprint((raw,prep,evidence))
            direct=native._observe_grid_cells(raw,prep,evidence)
            proof=native.observe_screen_grid_cells(raw,prep,evidence)
            if fingerprint((raw,prep,evidence))!=before:raise ValueError('Grid observation mutated source values')
            decision.update(before_cells=direct['cell_count'],after_cells=proof['cell_count'],verified=bool(proof['cell_count']),
                source_sha256=record['image_sha256'],input_version_id=record['input_version_id'],
                result_id=record['result_id'],result_revision=record['result_revision'],
                candidate_table_sha256=fingerprint(raw['tables']),direct=direct,proof=proof)
        decisions.append(decision)
    if any(digest(capsule/name)!=sha for name,sha in policy['files'].items()):raise ValueError('Source changed during replay')
    frozen_json(output/'decisions.json',{'decisions':decisions})
    result={'format':'source-rectified-grid-replay-v1','policy':bound(capsule/'policy.json'),
        'inputs':bound(inputs_path),'observations':bound(observations_path),'decisions':bound(output/'decisions.json'),
        'scope_inputs':len(records),'before_images':sum(bool(row['before_cells']) for row in decisions),
        'after_images':sum(row['verified'] for row in decisions),
        'before_cells':sum(row['before_cells'] for row in decisions),'after_cells':sum(row['after_cells'] for row in decisions),
        'newly_mapped_images':sum(row['verified'] and not row['before_cells'] for row in decisions),
        'lost_mapped_images':sum(bool(row['before_cells']) and not row['verified'] for row in decisions),
        'elapsed_seconds':time.monotonic()-start,'truth_read':False,'holdout_read':False,'native_model_executed':False,
        'values_changed':False,'review_changed':False,'semantic_header_release':False,'adoption_qualified':False}
    frozen_json(output/'result.json',result)
    print({k:result[k] for k in ('scope_inputs','before_images','after_images','before_cells','after_cells','elapsed_seconds')})


def score(args):
    import cv2
    import numpy as np
    from quality import match_tables, tables, span, key
    replay,truth_path,output=map(fpath,(args.replay,args.truth,args.output))
    if output.exists() or truth_path.parent.name not in {'development','validation'}:
        raise ValueError('New development/validation coordinate score required')
    receipt=read_json(replay/'result.json')
    if bound(replay/'decisions.json')!=receipt['decisions'] or bound(fpath(receipt['observations']['path']))!=receipt['observations']:
        raise ValueError('Source replay changed')
    policy=read_json(fpath(receipt['policy']['path']))
    if bound(fpath(receipt['policy']['path']))!=receipt['policy']:raise ValueError('Policy changed')
    truth={row['id']:row for row in read_json(truth_path)['samples']}
    original={row['id']:row for row in read_json(receipt['observations']['path'])['records']}
    decisions=read_json(replay/'decisions.json')['decisions']
    if {r['id'] for r in decisions}!=truth.keys() or original.keys()!=truth.keys():raise ValueError('Coordinate denominator differs')
    rows=[];failures=[];counts=Counter()
    for decision in decisions:
        sid=decision['id']
        if not decision['verified']:continue
        if truth[sid]['image_sha256']!=decision['source_sha256']:raise ValueError('Reference/source identity differs')
        actual=tables(original[sid]['candidate'])
        pairs,_=match_tables(truth[sid]['tables'],actual)
        mapped={ai:(ti,expected) for ti,expected,ai,_ in pairs}
        for proof in decision['proof']['tables']:
            if proof['status']!='verified':continue
            counts['mapped_tables']+=1
            expected_table=mapped.get(proof['table'])
            expected_cells={key(cell):cell for cell in expected_table[1]['cells']} if expected_table else {}
            for cell in proof['cells']:
                expected=expected_cells.get(key(cell))
                newly_mapped=not decision['before_cells']
                row={'id':sid,'table':proof['table'],'row':cell['row'],'column':cell['column'],
                    'source_polygon':cell['polygon'],'newly_mapped':newly_mapped}
                if expected and expected.get('polygon') and span(expected)==span(cell):
                    observed=np.asarray(cell['polygon'],dtype=np.float32)
                    reference=np.asarray(expected['polygon'],dtype=np.float32)
                    intersection,_=cv2.intersectConvexConvex(observed,reference)
                    union=abs(cv2.contourArea(observed))+abs(cv2.contourArea(reference))-intersection
                    iou=float(intersection/max(union,1e-12))
                    error=float(np.max(np.linalg.norm(observed-reference,axis=1))) if observed.shape==reference.shape else None
                    if 'canonical_pdf_bbox' in expected:
                        containment=float(intersection/max(abs(cv2.contourArea(reference)),1e-12))
                        owners=[]
                        for other in proof['cells']:
                            overlap,_=cv2.intersectConvexConvex(np.asarray(other['polygon'],dtype=np.float32),reference)
                            if overlap/max(abs(cv2.contourArea(reference)),1e-12)>=policy['independent_text_containment_minimum']:
                                owners.append(key(other))
                        correct=containment>=policy['independent_text_containment_minimum'] and owners==[key(cell)]
                        row.update(reference_semantics='text_bounds_only',containment=containment,unique_owner_count=len(owners))
                        counts['text_bounds_reference_cells']+=1
                        counts['correct_text_bounds_reference_cells']+=correct
                    else:
                        correct=(iou>=policy['independent_coordinate_iou_minimum'] and error is not None
                            and error<=policy['independent_coordinate_maximum_corner_error_pixels'])
                        row['reference_semantics']='full_cell'
                        counts['full_cell_reference_cells']+=1
                        counts['correct_full_cell_reference_cells']+=correct
                    row.update(reference_polygon=expected['polygon'],iou=iou,maximum_corner_error_pixels=error,correct=correct)
                else:row.update(correct=False,reason='unmatched_reference_partition_or_geometry')
                rows.append(row);counts['mapped_cells']+=1;counts['correct_cells']+=row['correct']
                counts['newly_mapped_cells']+=newly_mapped
                counts['correct_newly_mapped_cells']+=newly_mapped and row['correct']
                if not row['correct']:failures.append(row)
        counts['mapped_images']+=1
    output.mkdir(parents=True)
    report={'format':'independent-source-polygon-coordinate-score-v1','replay':bound(replay/'result.json'),
        'reference':bound(truth_path),'policy':receipt['policy'],'score_helper':bound(Path(__file__)),
        'scope_inputs':len(truth),'counts':dict(counts),
        'failures':failures,'cells':rows,'all_mapped_coordinates_verified':not failures and bool(rows),
        'newly_mapped_coordinate_failures':sum(row['newly_mapped'] for row in failures),
        'existing_mapping_coordinate_failures':sum(not row['newly_mapped'] for row in failures),
        'model_executed':False,'review_scope_changed':False,'holdout_read':False,'adoption_qualified':False}
    frozen_json(output/'review.json',report)
    print({'scope_inputs':len(truth),**dict(counts),'coordinate_failures':len(failures)})


if __name__=='__main__':
    require_environment()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('freeze','run','score'))
    for name in ('source','capsule','inputs','observations','output','replay','truth'):parser.add_argument('--'+name)
    args=parser.parse_args()
    {'freeze':freeze,'run':run,'score':score}[args.phase](args)
