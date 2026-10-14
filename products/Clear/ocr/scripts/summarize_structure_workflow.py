"""Retain all public engineering attempts and distinguish them from quality scores."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from geometry_eval_common import sha,write_json


def read(path):return json.loads(path.read_text('utf-8'))
def times(values):
    values=sorted(values)
    return {'count':len(values),'median':statistics.median(values) if values else None,'p95':values[math.ceil(.95*len(values))-1] if values else None,'sum':sum(values)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--run',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    manifest=read(a.run/'public-pdfs-02/inputs/development.json');samples=manifest['samples']
    stages=[];ledger=[]
    for phase in ('context','adopted','paddle','tableformer-raw','rapidtable'):
        rows=[dict(id=s['id'],**read(a.run/'public-inference-01'/phase/s['id']/'evaluation.json')) for s in samples]
        ledger += [dict(phase=phase,**r) for r in rows]
        stages.append({'phase':phase,'status':dict(Counter(r['status'] for r in rows)),
            'wall_seconds_all_attempts':times([r['seconds'] for r in rows]),
            'worker_load_seconds':read(a.run/'public-inference-01'/phase/'timing.json')['worker_load_seconds']})
    replay=read(a.run/'public-inference-01/replay-01/summary.json')['rows']
    comparisons=[]
    for provider,algorithm in sorted({(r['provider'],r['algorithm']) for r in replay}):
        rows=[r for r in replay if (r['provider'],r['algorithm'])==(provider,algorithm)]
        comparisons.append({'provider':provider,'algorithm':algorithm,'pages':len(rows),'status':dict(Counter(r['status'] for r in rows)),
            'adopted_cells':sum(r.get('adopted_cells',0) for r in rows),'offered_cells_not_accuracy':sum(r.get('offered_cells',0) for r in rows),
            'page_mapping_ms':times([r['mapping_ms'] for r in rows if 'mapping_ms' in r]),
            'system_rejection_stages':dict(sum((Counter(r.get('failure_stages',{})) for r in rows),Counter()))})
    b=read(a.run/'public-workflow-01/B/summary.json')['rows'];b_summary=[]
    for pool in ('native','ocr'):
        for provider in ('paddle','tableformer-raw','rapidtable','paddlevl'):
            rows=[r for r in b if r['pool']==pool and r['provider']==provider]
            b_summary.append({'pool':pool,'provider':provider,'status':dict(Counter(r['status'] for r in rows)),
                'tables':sum(r.get('tables',0) for r in rows),'unassigned':sum(r.get('unassigned',0) for r in rows),
                'reason_codes':dict(sum((Counter(r.get('reasons',{})) for r in rows),Counter())),
                'maximum_table_ms':max((r.get('maximum_table_ms',0) for r in rows),default=0)})
    c=[]
    for name in ('public-workflow-01','public-workflow-02'):
        rows=read(a.run/name/'C/summary.json')['rows']
        c.append({'run':name,'status':dict(Counter(r['status'] for r in rows)),
            'pdf_ready':sum(r.get('pdf_preflight',{}).get('ready',False) for r in rows),
            'xlsx_readbacks':sum(r.get('xlsx_readback')=='passed' for r in rows),
            'wall_seconds_all_attempts':times([r['wall_seconds'] for r in rows]),'rows':rows})
    baseline=read(ROOT/'audit/structure-workflow-20260916/baseline-source.json')
    unchanged={name:next(x['sha256'] for x in baseline['files'] if x['path']==name)==sha(ROOT/name)
        for name in ('src/ocr_workbench/table_matching.py','src/ocr_workbench/table_anchor_refinement.py','config/geometry-matching-policy.json','config/geometry-matching-policy-v3.json')}
    result={'scope':'public-material experimental delivery; not independent quality acceptance',
        'named_documents':len({s['original_document_id'] for s in samples}),'unique_original_documents':len({s['source_pdf_sha256'] for s in samples}),
        'pages':len(samples),'unique_page_images':len({s['sha256'] for s in samples}),'template_groups':len({s['group_id'] for s in samples}),
        'physical_scans_verified':0,'independent_annotations':0,'human_participants':0,
        'default_provider':'paddle','default_matcher':'local-v2','automatic_adoption':False,'matcher_source_unchanged':unchanged,
        'inference_stages':stages,'inference_ledger':ledger,'A':comparisons,'B':b_summary,'C':c,
        'export_followup':read(a.run/'public-export-02/receipt.json'),
        'acceptance':{k:'unmeasured' for k in ('usable_table_gain','correction_burden_reduction','human_time_reduction','merged_structure_gain','suggestion_precision','geometry_CANW','independent_residual_errors')},
        'timing_limit':'Development wall times include loading and shared-machine contention; no comparative human-efficiency or isolated-throughput claim'}
    write_json(a.output,result)


if __name__=='__main__':main()
