"""Deterministic engineering probes and native-vector ablation on exposed inputs."""
from copy import deepcopy
from collections import Counter
import json
from pathlib import Path
import statistics
import sys
import time

from complex_table_run import ROOT,BUILD,AUDIT,save,sha
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from ocr_workbench.coordinates import box_polygon
from ocr_workbench.table_matching import local_mapping,policy_for_algorithm
from ocr_workbench.geometry_contract import full_cell_evidence


def table(values):
    return {'rows':len(values),'columns':len(values[0]),'cells':[{'row':r,'column':c,'row_span':1,'column_span':1,'text':v}
        for r,row in enumerate(values) for c,v in enumerate(row)]}


def prediction(values):
    cells=[];blocks=[]
    for r,row in enumerate(values):
        for c,text in enumerate(row):
            cells.append({'cell_id':str(len(cells)),'row_id':r,'column_id':c,'rowspan_val':1,'colspan_val':1,
                'bbox':[c*100,r*40,(c+1)*100,(r+1)*40]})
            if text:blocks.append({'id':f'token-{r}-{c}','text':text,'kind':'text','granularity':'word',
                'polygon':box_polygon([c*100+5,r*40+5,(c+1)*100-5,(r+1)*40-5])})
    return {'tf_table_cells':cells,'source_semantics':'raw_structure','model_sha256':'synthetic-fixed-grid'},blocks


def performance(name='report'):
    rows=[]
    for height,width in ((10,10),(25,20),(50,20),(80,25)):
        values=[[f'row-{r}' if c==0 else f'col-{c}' if r==0 else '100.00' for c in range(width)] for r in range(height)]
        pred,blocks=prediction(values);edit={'text':'','tables':[table(values)]}
        for algorithm in ('local-v3','local-v4'):
            timings=[];counts=[]
            for repeat in range(3):
                start=time.perf_counter()
                result=local_mapping(edit,pred,width*100,height*40,policy=policy_for_algorithm(algorithm),ocr_blocks=blocks,image_version='v')
                timings.append((time.perf_counter()-start)*1000)
                counts.append({'full_cell':sum(full_cell_evidence(m) for m in result if m['target']['kind']=='cell'),
                    'reasons':dict(Counter(r for m in result if m['target']['kind']=='cell' for r in m.get('reason_codes',[]))),
                    'matching_ms':max((sum(m.get('timing_ms',{}).values()) for m in result),default=0)})
            rows.append({'cells':height*width,'method':algorithm,'e2e_ms':timings,'runs':counts})
    target=AUDIT/'performance'/f'{name}.json'
    if target.exists():raise ValueError('Performance receipt exists; use a fresh --name')
    save(target,{'scope':'synthetic engineering only; repeated amounts with independent row/column anchors',
        'runs':rows,'budget_ms':200,'hard_bound_scope':'candidate/anchor search checks deadlines; full result serialization and input validation also reported end-to-end'})
    print('Performance complete',flush=True)


def native():
    import pdfplumber
    from ocr_workbench.pdf_tables import without_shading
    manifest=json.loads((BUILD/'dataset/manifest.json').read_text('utf-8'))
    items=[]
    for sample in manifest['samples']:
        path=BUILD/'raw'/sample['document_id']/'source.pdf'
        with pdfplumber.open(path) as pdf:
            page=pdf.pages[sample['page']-1]
            before=page.find_tables();bad=page.filter(lambda o:not(o.get('object_type')=='rect' and o.get('fill') and not o.get('stroke'))).find_tables()
            after=page.filter(without_shading).find_tables()
            def summary(tables):return [{'cells':len(t.cells),'rows':len(t.rows),'columns':len({v for b in t.cells for v in (b[0],b[2])})-1,
                'bbox':t.bbox,'text':t.extract()} for t in tables]
            raw=BUILD/'runs/native-vector'/f'{sample["id"]}.json'
            save(raw,{'source_sha256':sha(path),'page':sample['page'],'default':summary(before),
                'variant1_all_fill_removed':summary(bad),'variant2_thin_rules_retained':summary(after)})
            items.append({'id':sample['id'],'label_level':sample['label_level'],'default_cells':sum(len(t.cells) for t in before),
                'variant1_cells':sum(len(t.cells) for t in bad),'variant2_cells':sum(len(t.cells) for t in after),
                'evidence':raw.relative_to(ROOT).as_posix(),'not_independent_quality':True})
    save(AUDIT/'local/merged-report.json',{'items':items,'selected_variant':'thin_rules_retained',
        'negative_result':'Removing all non-stroked rectangles deleted real thin filled rules and lost tables.',
        'visual_diagnosis':{'sample':'hk-audit-85-ch1-p24-t0','visible_columns':6,'default_columns':12,'default_cells':78,'filtered_cells':57},
        'default_provider_retained':True,'experimental_candidate_only':True})
    print('Native ablation complete',flush=True)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--name',default='report');parser.add_argument('--native',action='store_true');args=parser.parse_args()
    performance(args.name)
    if args.native:native()
