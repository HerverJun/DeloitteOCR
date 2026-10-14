"""Blinded visual-choice diagnostic; product gates remain enforced separately."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from prepare_luna_structure_sim import OUT,AUDIT
from complex_table_run import save,sha
from ocr_workbench.structure_arbitration import PROMPT,LIMITS
from ocr_workbench.complex_table_contract import validate_grid
from ocr_workbench.multimodal_runtime import _png
from PIL import Image


def main():
    inbox=OUT/'diagnostic-inbox';inbox.mkdir(exist_ok=False)
    prepared=json.loads((OUT/'prepared.json').read_text('utf-8'))
    rows={r['id']:r for r in prepared['rows']}
    ids=json.loads((ROOT/'audit/complex-tables-20260919-01/api/random-check-policy.json').read_text('utf-8'))['sample_ids']
    from ocr_workbench.store import Store
    store=Store(OUT/'workspace')
    assignments=[]
    for n,ident in enumerate(ids,1):
        row=rows[ident];case=f'case-{n:02d}';folder=inbox/case;folder.mkdir()
        with store.transaction() as db:
            sets=[dict(r) for r in db.execute('SELECT * FROM structure_candidates WHERE result_id=? ORDER BY id',(row['result_id'],))]
        candidates=[];exclusions=[];lineage={};seen=set()
        for record in sets:
            payload=json.loads(record['payload'])
            # Crop diagnostic routing: largest source table, independent of labels.
            tables=sorted(payload['tables'],key=lambda t:len(t['skeleton']['cells']),reverse=True)
            if not tables:continue
            t=tables[0];skeleton=deepcopy(t['skeleton'])
            try:validate_grid(skeleton,tokens=payload['tokens'])
            except ValueError as e:
                exclusions.append({'provider':record['provider'],'error':str(e)});continue
            refs=[tid for c in skeleton['cells'] for tid in c['structure_source']['token_ids']]
            compact_ids={old:f't{i:03d}' for i,old in enumerate(refs)}
            cells=[{**{k:c[k] for k in ('row','column','row_span','column_span','text')},
                'structure_source':{'token_ids':[compact_ids[v] for v in c['structure_source']['token_ids']]}}
                for c in skeleton['cells']]
            table={'rows':skeleton['rows'],'columns':skeleton['columns'],'cells':cells}
            key=json.dumps(table,ensure_ascii=False,sort_keys=True)
            if key in seen:continue
            seen.add(key)
            cid='c'+hashlib.sha256((ident+record['id']).encode()).hexdigest()[:10]
            pool={p['id']:p for p in payload['tokens']}
            tokens=[{'id':compact_ids[v],'raw_text':pool[v]['raw_text'],'polygon':pool[v]['polygon']} for v in refs]
            candidate={'id':cid,'table':table,'tokens':tokens,'token_ids':list(compact_ids.values())}
            validate_grid(table,tokens=tokens)
            candidates.append(candidate)
            lineage[cid]={'candidate_set_id':record['id'],'provider':record['provider'],'table':skeleton,
                'original_token_ids':refs,'unassigned_token_ids':t['unassigned_token_ids'],'reason_codes':t['reason_codes']}
        random.Random('20260919'+ident).shuffle(candidates)
        with Image.open(row['image_path']) as im:png,size=_png(im.convert('RGB'),LIMITS['image_max_pixels'],LIMITS['image_max_side'])
        (folder/'page.png').write_bytes(png)
        current=deepcopy(row['before']['tables'][0]) if row['before']['tables'] else None
        if current:
            current={'rows':current['rows'],'columns':current['columns'],'cells':[
                {k:c[k] for k in ('row','column','row_span','column_span','text')} for c in current['cells']]}
        request={'system_prompt':PROMPT,'image':'page.png','image_size':size,
            'structure':{'version':'structure-arbitration-v1','table_index':0,'current_table':current,'candidates':candidates},
            'diagnostic_only':'Compare table rows/columns/spans/token ownership visually. No edits. Abstain if no candidate is fully supported. These are compressed immutable candidates; eligibility is evaluated independently.'}
        save(folder/'request.json',request)
        # Compact display preserves every cell, position, span, literal and token reference.
        def display(table):
            return None if table is None else {'rows':table['rows'],'columns':table['columns'],
                'cells':[[c['row'],c['column'],c['row_span'],c['column_span'],c['text'],c.get('structure_source',{}).get('token_ids',[])] for c in table['cells']]}
        save(folder/'view.json',{'cell_columns':['row','column','row_span','column_span','literal_text','token_ids'],
            'current':display(current),'candidates':[{'id':c['id'],**display(c['table'])} for c in candidates]})
        assignments.append({'case':case,'sample_id':ident,'table_index':0,'lineage':lineage,'excluded':exclusions,
            'candidate_count':len(candidates),'request_sha256':sha(folder/'request.json'),'image_sha256':sha(folder/'page.png'),
            'request_chars':len(json.dumps(request,ensure_ascii=False))})
    save(OUT/'diagnostic-assignments.json',assignments)
    save(inbox/'batch.json',{'cases':[r['case'] for r in assignments],'system_prompt':PROMPT,
        'instructions':'View page.png and complete view.json for every case. Read request.json as needed. No reference labels or prior scores are available. Output one response.json per case with exact arbitration JSON. You may copy exact token_ids for a chosen candidate using a local script; never change the choice after seeing evaluator feedback.'})
    save(AUDIT/'diagnostic-protocol.json',{'frozen_before_luna':True,'sample_ids':ids,'selection':'unchanged pre-existing 12 random checks; no selection by error or candidate quality',
        'input':'same capped-resolution historical crops and source-derived candidate skeletons; no generated correct answers',
        'transformations':['largest candidate table per provider in a table crop','strip verbose provenance for visual diagnostic','bijective short token IDs','deterministic candidate shuffle'],
        'product_eligibility':'All 120 historical samples blocked by existing product gates; diagnostic selections are never treated as deployable net gains',
        'metrics':'same fixed rows/columns/spans and NFKC+whitespace-normalized text; all targets retained; report full grid and extra/wrong output counts',
        'scope':'multimodal model choice diagnostic, not the shipped API prompt payload/limit/transport acceptance',
        'assignments_sha256':sha(OUT/'diagnostic-assignments.json'),'cases':[{k:r[k] for k in ('case','candidate_count','request_chars')} for r in assignments]})
    print(json.dumps({'cases':len(assignments),'candidates':[r['candidate_count'] for r in assignments],'inbox':str(inbox)},ensure_ascii=False))


if __name__=='__main__':main()
