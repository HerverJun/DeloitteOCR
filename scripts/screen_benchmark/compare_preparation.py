"""Compare a new CPU policy using immutable actual context; no GPU or oracle crop."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
from context import verify_context_record
from run import digest,fpath,isolate_runtime,write
from review_context import inspect_preparation,summarize


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('inputs','truth','context-run','worker-source','baseline','selection','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    for name in ('inputs','truth','context_run','worker_source','baseline','selection','output'):
        setattr(args,name,fpath(getattr(args,name)))
    if args.output.exists():raise ValueError('Preserve earlier comparison; use a new output filename')
    artifact_root=args.output.parent/(args.output.stem+'-artifacts')
    isolate_runtime(artifact_root)
    sys.path.insert(0,str(args.worker_source))
    from ocr_workbench.screen_imaging import prepare_screen_image,select_screen_first_pass,POLICY_VERSION
    sources={name:digest(args.worker_source/'ocr_workbench'/name) for name in ('screen_imaging.py','screen_evidence.py')}
    inputs=json.loads(args.inputs.read_text('utf-8'))
    binding=json.loads((args.context_run/'binding.json').read_text('utf-8'))
    if inputs.get('truth_available_to_inference') is not False or binding.get('labels_or_ideal_regions_provided') is not False or binding['inputs_sha256']!=digest(args.inputs):
        raise ValueError('Actual context/input identity differs')
    samples={s['id']:s for s in inputs['samples']}
    truth=json.loads(args.truth.read_text('utf-8'))['samples']
    if set(samples)!={s['id'] for s in truth}:raise ValueError('Reference/input identity differs')
    before=json.loads(args.baseline.read_text('utf-8'))
    if before['input_sha256']!=digest(args.inputs) or before['truth_sha256']!=digest(args.truth):raise ValueError('Baseline belongs to other inputs/truth')
    selection=json.loads(args.selection.read_text('utf-8'))
    ids={s['id'] for s in selection['samples']}
    if not ids<=set(samples):raise ValueError('Diagnostic selection escaped current development inputs')
    rows=[];started=time.perf_counter()
    for reference in truth:
        sample=samples[reference['id']]
        if digest(sample['image'])!=sample['image_sha256']:raise ValueError('Actual image changed')
        # Verify the OLD context's stored hashes. New CPU classification is a
        # separate reviewed policy; never pretend old evidence has a new worker.
        _,_,actual_evidence=verify_context_record(args.context_run/sample['id'],sample)
        tick=time.perf_counter()
        prepared=prepare_screen_image(sample['image'],artifact_root/sample['id'],evidence=actual_evidence,perspective=True)
        elapsed=time.perf_counter()-tick
        # References enter only this post-preparation diagnostic computation.
        row=inspect_preparation(reference,prepared,selected=select_screen_first_pass(prepared))
        row['preprocessing_seconds']=elapsed
        row['first_pass_ids']=prepared['first_pass_ids']
        rows.append(row)
        print(json.dumps({'id':sample['id'],'completed':len(rows),'total':len(truth),'primary':row['primary_id'],
                          'selected':row['first_pass_ids'],'seconds':round(elapsed,3)},ensure_ascii=False),flush=True)
    if sources!={name:digest(args.worker_source/'ocr_workbench'/name) for name in sources}:
        raise ValueError('CPU policy source changed during comparison')
    after_summary=summarize(rows)
    report={'scope':'Offline development CPU region diagnostics using unchanged actual context. No truth/ideal regions provided to preprocessing; geometric coverage is not OCR accuracy.',
            'policy_version':POLICY_VERSION,'cpu_policy_sources':sources,'context_worker_binding':binding,
            'input_sha256':digest(args.inputs),'truth_sha256':digest(args.truth),'baseline_sha256':digest(args.baseline),
            'wall_seconds':time.perf_counter()-started,'before':before['summary'],'after':after_summary,
            'diagnostic_before':summarize([r for r in before['samples'] if r['id'] in ids]),
            'diagnostic_after':summarize([r for r in rows if r['id'] in ids]),
            'before_multiple_selected':sum(len(r.get('selected',[]))>1 for r in before['samples']),
            'after_multiple_selected':sum(len(r['selected'])>1 for r in rows),
            'after_original_fallback':sum(r['selected'][0]['kind']=='original' for r in rows),
            'samples':rows}
    write(args.output,report)
    print(json.dumps({k:report[k] for k in ('before','after','diagnostic_before','diagnostic_after','after_original_fallback')},ensure_ascii=False))


if __name__=='__main__':main()
