"""Bounded, independently reviewed Luna pilot; no edits to historical runs."""
import argparse
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time
import unicodedata

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from native_repair_pilot import read,save,sha
from native_repair_product_replay import header_signature
from ocr_workbench.store import Store
from ocr_workbench.structure_store import context,refresh_proposals,decide_structure
from ocr_workbench.structure_arbitration import build_snapshot,PROMPT,LIMITS,assert_current
from ocr_workbench.structure_wire import encode,dumps,decode_response
from ocr_workbench.geometry_contract import fingerprint

RUN=os.environ.get('OCR_LUNA_LOCAL_RUN','luna-local-review-20260919-06')
if not RUN.startswith('luna-local-review-') or not RUN.replace('-','').isalnum():raise ValueError('Invalid run')
BUILD=ROOT/'build'/RUN;AUDIT=ROOT/'audit'/RUN
INPUTS=[('structure-repair-native-20260919-01','product-replay-06'),('structure-repair-native-20260919-02','product-replay-02')]
CONFIG=read(AUDIT/'input-config.json') if (AUDIT/'input-config.json').exists() else {}
INPUTS=CONFIG.get('inputs',INPUTS)


def prepare():
    protocol={'run':RUN,'created':datetime.now(timezone.utc).isoformat(),'model':'gpt-5.6-luna','reasoning':'medium',
        'tables':sum(len(r['tables']) for run,replay in INPUTS for r in read(ROOT/'build'/run/replay/'rows.json')),
        'split':CONFIG.get('split','development'),'reference':'independent image-only Astra grids; hidden from Luna',
        'scope':'header metadata only, exact literal and topology preservation, <=8 changed cells; no default integration',
        'selection':'existing fixed fewest-impact/name-ranked variants; no scoring or labels during preparation',
        'candidate_budget':LIMITS,'wire_version':'structure-evidence-v2',
        'model_context':'one table per independent agent, no inherited history; sent image only; tool-assisted JSON IO is simulation difference',
        'baseline':'same retained legal pool: fixed local first choice, keep-current, Luna select/abstain',
        'stop':'no automatic adoption; no harm accepted for a production recommendation; new-document confirmation required',
        'production_cost':'no Astra per document; true endpoint cost/latency unmeasured in agent simulation',
        'prompt':PROMPT,'source_hashes':{p:sha(ROOT/p) for p in ['src/ocr_workbench/structure_wire.py','src/ocr_workbench/structure_arbitration.py']}}
    if (AUDIT/'protocol.json').exists():raise FileExistsError('Run already frozen')
    save(AUDIT/'protocol.json',protocol)
    prepared=[];case_number=0
    for oldrun,replay in INPUTS:
        old=ROOT/'build'/oldrun/replay
        folder=BUILD/oldrun;folder.mkdir(parents=True,exist_ok=False)
        shutil.copytree(old/'workspace',folder/'workspace')
        store=Store(folder/'workspace')
        patches=read(ROOT/'build'/oldrun/'patches.json')
        for row in read(old/'rows.json'):
            rid=row['result_id'];current=store.result(rid)
            while current['can_undo']:current=store.history(rid,-1,current['revision'])
            version=store.one('versions',row['version_id'])
            view=refresh_proposals(store,rid,current['revision'])
            for ti in [t['table'] for t in row['tables']]:
                case_number+=1;ident=f'case-{case_number:02d}';case=BUILD/'inbox'/ident;case.mkdir(parents=True,exist_ok=False)
                patch=next(p for p in patches if p['page_id']==row['page_id'] and p['candidate_index']==ti)
                options=[p for p in view['proposals'] if p['table_indices']==[ti] and p['can_apply']]
                chosen=[];names={}
                for item in patch['candidates']:
                    matching=sorted((p for p in options if p['kind']=='replace_table' and header_signature(p['proposed_tables'][0])==header_signature(item['patch']['after'])),key=lambda p:p['id'])
                    if matching:
                        chosen.append(matching[0]);names[matching[0]['id']]=item['variant']
                def reject(p):
                    decide_structure(store,rid,p['id'],{'request_id':'wire-filter-'+p['id'],'action':'reject','revision':p['revision'],'basis':p['basis'],'version_id':version['id']})
                for p in options:
                    if p['id'] not in names:reject(p)
                attempts=[];snapshot=None
                while chosen:
                    try:
                        with store.transaction() as db:snapshot=build_snapshot(db,context(db,rid),{'table':ti},version)
                        break
                    except ValueError as error:
                        attempts.append(str(error))
                        if '超过上限' not in str(error) or len(chosen)==1:break
                        reject(chosen.pop())
                record={'case':ident,'old_run':oldrun,'page_id':row['page_id'],'table':ti,'reference_case':patch['case_id'],
                    'reference_table':patch['table_id'],'document':patch['document'],'template':patch['template'],
                    'store':str(store.root),'result_id':rid,'version':version,'snapshot':snapshot,
                    'variant_by_id':names,'local_candidate_id':chosen[0]['id'] if chosen else None,'attempts':attempts}
                if snapshot:
                    wire=encode(snapshot);save(case/'wire.json',wire)
                    from PIL import Image
                    from ocr_workbench.multimodal_runtime import _png
                    original=store.file(version['path'])
                    with Image.open(original) as img:png,size=_png(img.convert('RGB'),LIMITS['image_max_pixels'],LIMITS['image_max_side'])
                    (case/'page.png').write_bytes(png)
                    (case/'PROMPT.txt').write_text(PROMPT,'utf-8')
                    record.update(wire_sha256=fingerprint(wire),wire_characters=len(dumps(wire)),sent_size=size,sent_image_sha256=sha(case/'page.png'))
                prepared.append(record)
                save(BUILD/'prepared.json',prepared)
                print(ident,row['page_id'],ti,'eligible',bool(snapshot),'chars',record.get('wire_characters'),'candidates',len(chosen),flush=True)
    save(BUILD/'prepared.json',prepared)
    save(AUDIT/'eligibility.json',{'tables':len(prepared),'eligible':sum(bool(r['snapshot']) for r in prepared),
        'cases':[{k:v for k,v in r.items() if k not in ('snapshot','version')} for r in prepared]})


def score():
    records=[]
    norm=lambda text:''.join(unicodedata.normalize('NFKC',text).split())
    for row in read(BUILD/'prepared.json'):
        reference=read(ROOT/'build'/row['old_run']/'reference-inbox'/row['reference_case']/'response.json')
        ref=next(t for t in reference['tables'] if t['id']==row['reference_table'])
        uncertain={(u['row'],u['column']) for u in ref['unresolved']}
        refcells={(c['row'],c['column']):c for c in ref['cells']}
        s=row['snapshot']
        if not s:records.append({'case':row['case'],'eligible':False,'attempts':row['attempts']});continue
        response_path=BUILD/'inbox'/row['case']/'response.json'
        raw=read(response_path);validated=None;error=None
        try:validated=decode_response(raw,s)
        except ValueError as exc:error=str(exc)
        before=s['current_table'];base={(c['row'],c['column']):c for c in before['cells']}
        def measure(table):
            corrected=harmed=unqualified=changed=0
            for c in table['cells']:
                key=c['row'],c['column'];old=base[key];expected=refcells.get(key)
                assert c['text']==old['text'] and all(c[k]==old[k] for k in ('row_span','column_span'))
                if c.get('is_header',False)==old.get('is_header',False):continue
                changed+=1
                good=expected and key not in uncertain and norm(c['text'])==norm(expected['text']) and all(c[k]==expected[k] for k in ('row_span','column_span'))
                if not good:unqualified+=1;continue
                corrected+=c.get('is_header',False)==expected['is_header'];harmed+=old.get('is_header',False)==expected['is_header']
            return {'changed':changed,'corrected':corrected,'harmed':harmed,'unqualified':unqualified,'net':corrected-harmed,'literal_changes':0}
        local=next((c for c in s['candidates'] if c['id']==row['local_candidate_id']),None)
        model=next((c for c in s['candidates'] if validated and c['id']==validated['candidate_id']),None)
        records.append({'case':row['case'],'page':row['page_id'],'document':row['document'],'template':row['template'],'eligible':True,
            'response_sha256':sha(response_path),'response':validated,'validation_error':error,
            'local_variant':row['variant_by_id'][local['id']] if local else 'keep','luna_variant':row['variant_by_id'][model['id']] if model else 'keep',
            'local':measure(local['table'] if local else before),'luna':measure(model['table'] if model else before),'wire_characters':row['wire_characters'],
            'all_candidates':[{ 'variant':row['variant_by_id'][c['id']], **measure(c['table'])} for c in s['candidates']]})
    summary={'tables':len(records),'eligible':sum(r['eligible'] for r in records),'model':'gpt-5.6-luna','production_calls':0,
        'protocol_failures':sum(bool(r.get('validation_error')) for r in records),
        'selected':sum(bool(r.get('response') and r['response']['decision']=='select') for r in records),
        'local':{k:sum(r['local'][k] for r in records if r['eligible']) for k in ('changed','corrected','harmed','unqualified','net','literal_changes')},
        'luna':{k:sum(r['luna'][k] for r in records if r['eligible']) for k in ('changed','corrected','harmed','unqualified','net','literal_changes')},
        'scope':CONFIG.get('split','development')+' model simulation, independent per table, Astra model references, header-only','records':records}
    save(AUDIT/'scores.json',summary);print(dumps({k:v for k,v in summary.items() if k!='records'}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['prepare','score']);args=parser.parse_args()
    {'prepare':prepare,'score':score}[args.phase]()
