"""Freeze a blinded Luna replay through the existing proposal/snapshot gates."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import shutil

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from complex_table_run import sha,save
from compare_tableformer_geometry import checked_artifact,bind_prediction
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image
from ocr_workbench.structure_store import record_candidates,refresh_proposals,context,structure_snapshot
from ocr_workbench.structure_arbitration import build_snapshot,PROMPT,LIMITS
from ocr_workbench.multimodal_runtime import _png
from PIL import Image

RUN='luna-structure-sim-20260919-01'
OUT=ROOT/'build'/RUN
AUDIT=ROOT/'audit'/RUN


def main():
    global OUT,AUDIT
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--run',default=RUN);args=parser.parse_args()
    if not args.run.replace('-','').isalnum():raise ValueError('Invalid run')
    OUT=ROOT/'build'/args.run;AUDIT=ROOT/'audit'/args.run
    OUT.mkdir(parents=True,exist_ok=False);AUDIT.mkdir(parents=True,exist_ok=False)
    history=ROOT/'build/tableformer-next-20260915'
    mp=history/'dataset/inputs/test.json';manifest=json.loads(mp.read_text('utf-8'))
    samples=manifest['samples']
    random_ids=json.loads((ROOT/'audit/complex-tables-20260919-01/api/random-check-policy.json').read_text('utf-8'))['sample_ids']
    protocol={'model_requested':'gpt-5.6-luna','reasoning_effort':'medium','transport':'Codex subagent multimodal simulation, not configured OpenAI/Anthropic endpoint',
        'history':'120 historical exposed public table crops; frozen Paddle adoption and shared PP-OCR blocks; no new OCR',
        'selection':'all product-eligible tables, bounded to 12 sample IDs by sha256(seed+sample ID); reference labels never used to choose requests',
        'seed':20260919,'random_check_ids':random_ids,'random_checks':'pre-existing 12 random IDs included in the denominator; blocked gates cannot be bypassed',
        'metrics':'all 120 tables/all reference cells and triggered cells; exact row,column,spans plus normalized literal text; full-table exact; geometry reported separately if available',
        'normalization':'NFKC, remove whitespace; no numeric/semantic correction',
        'human_adoption':'counterfactual adoption of every validated select, not measured human behavior',
        'abstention_or_failure':'retain current result; invalid output never adopted',
        'prompt':PROMPT,'limits':LIMITS,'manifest_sha256':sha(mp),'blinding':'Luna may only read its inbox; no labels, scores, source or report access',
        'scope_limitations':['agent tools and persistent batch context differ from isolated API requests','no measured API billing or endpoint latency','historical exposure, no fresh acceptance','cropped inputs, not full-page Chinese financial acceptance']}
    save(AUDIT/'protocol.json',protocol)
    store=Store(OUT/'workspace');project=store.project('Luna frozen public replay')
    records=[];eligible=[]
    for n,s in enumerate(samples):
        row={'id':s['id'],'group_id':s['group_id'],'random_check':s['id'] in random_ids,'sample':{k:v for k,v in s.items() if k!='fixed_edit'},'status':'blocked'}
        try:
            image=(mp.parent/s['image']).resolve();assert sha(image)==s['sha256']
            ocr,_,_=checked_artifact(history/'inference/test/ppocr',s,'result.json')
            adopted,_,_=checked_artifact(history/'adopted/test',s,'result.json')
            temporary=OUT/(s['id']+'.import.jpg');shutil.copy2(image,temporary)
            photo=add_image(store,project['id'],s['id']+'.jpg',temporary);version=store.one('versions',photo['active_version'])
            assert sha(image)==s['sha256']
            assert (version['width'],version['height'])==(s['width'],s['height'])
            task=store.enqueue(project['id'],[version['id']],['ppocr'])[0];store.claim()
            store.complete(task,{'engine':'ppocr','text':adopted['text'],'tables':adopted['tables'],
                'project_image_version':version['id'],'image':{'width':s['width'],'height':s['height']},'blocks':[]})
            result_id=store.one('tasks',task)['result_id'];row['result_id']=result_id
            row['before']=store.result(result_id)['edited'];row['providers']=[]
            for provider,name in [('tableformer','prediction.json'),('geometry','geometry.json')]:
                try:
                    pred,_,digest=checked_artifact(history/'inference/test'/provider,s,name)
                    pred=bind_prediction(pred,ocr['blocks'],s,provider)
                    pred['image_version']=version['id'];pred['image_sha256']=version['sha256']
                    # Historical raw TableFormer omitted component; production hosts
                    # supply provider identity. Do not collapse its set into Paddle.
                    pred['candidate_provider_key']=provider
                    with store.transaction() as db:record_candidates(db,result_id,version,pred,ocr['blocks'],source_result='fixed-ppocr:'+s['sha256'])
                    row['providers'].append({'provider':provider,'sha256':digest,'status':'loaded'})
                except (ValueError,OSError,KeyError) as e:row['providers'].append({'provider':provider,'error':str(e)})
            view=refresh_proposals(store,result_id,store.result(result_id)['revision'])
            with store.transaction() as db:
                current=context(db,result_id);live=structure_snapshot(db,current)
                row['proposal_counts']=dict(Counter('applicable' if p['can_apply'] else 'blocked' for p in live['proposals']))
                row['conflict_counts']=dict(Counter(c['kind'] for p in live['proposals'] for c in p.get('conflicts',[])))
                options=[]
                for ti in range(len(current['edited']['tables'])):
                    try:options.append(build_snapshot(db,current,{'table':ti},version))
                    except ValueError as e:row.setdefault('gate_errors',[]).append({'table':ti,'error':str(e)})
            row['snapshots']=options
            if options:
                row['status']='eligible';eligible.append(row)
            row['image_path']=str(store.file(version['path']))
        except (ValueError,OSError,KeyError,AssertionError) as e:row['error']=type(e).__name__+': '+str(e)
        records.append(row)
        if (n+1)%10==0:print(json.dumps({'prepared':n+1,'eligible':len(eligible)}),flush=True)
        save(OUT/'preparation-checkpoint.json',records)
    chosen=sorted(eligible,key=lambda r:hashlib.sha256(('20260919'+r['id']).encode()).hexdigest())[:12]
    inbox=OUT/'inbox';inbox.mkdir();assignments=[]
    for n,row in enumerate(chosen,1):
        for ti,snapshot in enumerate(row['snapshots']):
            case=f'case-{n:02d}-{ti:02d}';folder=inbox/case;folder.mkdir()
            with Image.open(row['image_path']) as image:png,size=_png(image.convert('RGB'),LIMITS['image_max_pixels'],LIMITS['image_max_side'])
            (folder/'page.png').write_bytes(png)
            save(folder/'request.json',{'system_prompt':PROMPT,'image':'page.png','sent_size':size,'structure':snapshot})
            assignments.append({'case':case,'sample_id':row['id'],'table_index':snapshot['table_index'],
                'image_sha256':sha(folder/'page.png'),'request_sha256':sha(folder/'request.json')})
    save(OUT/'prepared.json',{'rows':records,'assignments':assignments})
    save(inbox/'batch.json',{'cases':[a['case'] for a in assignments], 'response_contract':{'decision':'select or abstain','candidate_id':'existing ID or null','reason':'short Chinese visual evidence'},
        'note':'Only inspect provided request/page files. Tokens may be copied from the selected immutable candidate using a local script; this evaluates multimodal choice, not unaided JSON serialization.'})
    summary={'samples':len(records),'eligible_samples':len(eligible),'model_cases':len(assignments),'gates':dict(Counter(g['error'] for r in records for g in r.get('gate_errors',[]))),
        'preparation_errors':[{k:r.get(k) for k in ('id','error')} for r in records if 'error' in r],
        'before_available':sum('before' in r for r in records),'protocol_sha256':sha(AUDIT/'protocol.json'),
        'prepared_sha256':sha(OUT/'prepared.json'),'assignments':assignments}
    save(AUDIT/'preparation.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
