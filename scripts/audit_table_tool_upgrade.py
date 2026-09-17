"""Refresh auxiliary candidates on saved public results without rerunning OCR."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

def main():
    p=argparse.ArgumentParser()
    for name in ('bundle','data','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--installed',action='store_true');a=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(a.bundle/'app' if a.installed else root/'src'))
    from ocr_workbench import __version__,table_tool
    from ocr_workbench.store import Store
    from ocr_workbench.documents import Documents
    from ocr_workbench.structure_store import refresh_proposals
    a.output.mkdir(parents=True,exist_ok=False)
    shutil.copytree(a.data/'workspace',a.output/'workspace',ignore=shutil.ignore_patterns('__pycache__','engine-sessions','pdf-ipc'))
    store=Store(a.output/'workspace');manager=Documents(store,a.bundle)
    rows=[]
    before_tasks=store.rows('SELECT * FROM tasks');before_history=store.rows('SELECT * FROM edits')
    before_selections=store.rows('SELECT * FROM selections')
    try:
        for record in json.loads((a.data/'summary.json').read_text('utf-8'))['rows']:
            key=record['result_id'];before=deepcopy(store.result(key))
            job=table_tool.enqueue(manager,key,before['revision'])['stage_id']
            while store.one('document_stages',job)['status']=='queued':
                if not manager.step():raise RuntimeError('Retry not scheduled')
            stage=store.one('document_stages',job)
            assert stage['status']=='succeeded',stage['error']
            view=refresh_proposals(store,key,before['revision'])
            assert store.result(key)==before
            rows.append({'id':record['id'],'result_id':key,'state':view['table_tool']['state'],
                'tool_candidates':sum(c['provider'].startswith('pdfplumber/') for c in view['candidates']),
                'applicable_tool_proposals':sum(p['provider'].startswith('pdfplumber/') and p['can_apply'] for p in view['proposals']),
                'result_unchanged':True})
        assert store.rows('SELECT * FROM tasks')==before_tasks
        assert store.rows('SELECT * FROM edits')==before_history
        assert store.rows('SELECT * FROM selections')==before_selections
    finally:manager.stop()
    receipt={'version':__version__,'module_source':table_tool.__file__,'rows':rows,
        'ocr_tasks_unchanged':True,'history_and_selections_unchanged':True,
        'tool_identity':table_tool.tool_identity(),'scope':'Auxiliary upgrade regression on already exposed material; no fresh OCR or quality claim'}
    (a.output/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'version':__version__,'pages':len(rows),'passed':True,'states':{s:sum(r['state']==s for r in rows) for s in ('ready','empty')}},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
