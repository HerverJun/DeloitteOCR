"""Repeat-review stress controls from actually adopted results, without label filtering."""
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from native_repair_pilot import read,save,sha
from native_repair_product_replay import header_signature
from ocr_workbench.store import Store
from ocr_workbench.structure_store import refresh_proposals,decide_structure,context
from ocr_workbench.structure_arbitration import build_snapshot,PROMPT,LIMITS
from ocr_workbench.structure_repair import content_baseline,native_header_patches
from ocr_workbench.structure_wire import encode,dumps
from ocr_workbench.geometry_contract import fingerprint

RUN='luna-local-review-20260919-08'
BUILD=ROOT/'build'/RUN;AUDIT=ROOT/'audit'/RUN
SAMPLES=[('06',i) for i in (1,3,5)]+[('07',i) for i in (3,4,6)]


def main():
    if (AUDIT/'protocol.json').exists():raise FileExistsError('Controls already frozen')
    save(AUDIT/'input-config.json',{'split':'repeat-review stress controls; exposed documents; current is actual previously adopted Luna choice'})
    save(AUDIT/'protocol.json',{'samples':SAMPLES,'selection':'previously selected first-row header cases; no reference labels used to filter newly generated candidates',
        'purpose':'test abstention on repeated review of already adopted output; not independent production prevalence estimate',
        'local_baseline':'keep current; additional candidates only monotonically add <=8 header flags, preserve every literal/span',
        'prompt_sha256':fingerprint(PROMPT),'model':'gpt-5.6-luna','reasoning':'medium','source_sha256':sha(ROOT/'src/ocr_workbench/structure_wire.py')})
    records=[]
    for n,(suffix,number) in enumerate(SAMPLES,1):
        parent_run='luna-local-review-20260919-'+suffix;oldcase=f'case-{number:02d}'
        parent=ROOT/'build'/parent_run
        record=next(r for r in read(parent/'prepared.json') if r['case']==oldcase).copy()
        case=f'case-{n:02d}';folder=BUILD/'inbox'/case;folder.mkdir(parents=True,exist_ok=False)
        root=BUILD/'stores'/case;shutil.copytree(parent/'response-replay'/oldcase/'workspace',root)
        store=Store(root);rid=record['result_id'];current=store.result(rid);version=record['version'];ti=record['table']
        baseline=content_baseline(current['edited']['tables'][ti],result_id=rid,revision=current['revision'],image_version=version['id'],image_sha256=version['sha256'])
        patches,rejected=native_header_patches(baseline)
        view=refresh_proposals(store,rid,current['revision'])
        options=[p for p in view['proposals'] if p['can_apply'] and p['table_indices']==[ti]]
        chosen=[];names={}
        for item in patches:
            matches=sorted((p for p in options if p['kind']=='replace_table' and header_signature(p['proposed_tables'][0])==header_signature(item['patch']['after'])),key=lambda p:p['id'])
            if matches:chosen.append(matches[0]);names[matches[0]['id']]=item['variant']
        def reject(p):
            decide_structure(store,rid,p['id'],{'request_id':'control-filter-'+p['id'],'action':'reject','revision':p['revision'],'basis':p['basis'],'version_id':version['id']})
        for p in options:
            if p['id'] not in names:reject(p)
        snapshot=None;attempts=[]
        while chosen:
            try:
                with store.transaction() as db:snapshot=build_snapshot(db,context(db,rid),{'table':ti},version)
                break
            except ValueError as error:
                attempts.append(str(error))
                if len(chosen)==1:break
                reject(chosen.pop())
        record.update(case=case,parent_run=parent_run,parent_case=oldcase,store=str(root),snapshot=snapshot,
            local_candidate_id=None,variant_by_id=names,attempts=attempts)
        if snapshot:
            wire=encode(snapshot);save(folder/'wire.json',wire)
            shutil.copy2(parent/'inbox'/oldcase/'page.png',folder/'page.png')
            (folder/'PROMPT.txt').write_text(PROMPT,'utf-8')
            record.update(wire_characters=len(dumps(wire)),wire_sha256=fingerprint(wire),sent_image_sha256=sha(folder/'page.png'))
        records.append(record);print(case,'eligible',bool(snapshot),'candidates',len(chosen),flush=True)
        save(BUILD/'prepared.json',records)
    save(AUDIT/'eligibility.json',{'tables':len(records),'eligible':sum(bool(r['snapshot']) for r in records)})


if __name__=='__main__':main()
