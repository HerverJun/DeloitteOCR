"""Isolated, resumable structure-repair research. Never consumes original inputs."""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'scripts'), str(ROOT/'tests')]
RUN = os.environ.get('OCR_STRUCTURE_REPAIR_RUN','structure-repair-20260919-01')
if not RUN.startswith('structure-repair-') or not RUN.replace('-','').isalnum():
    raise ValueError('Invalid isolated research run name')
AUDIT = ROOT/'audit'/RUN
BUILD = ROOT/'build'/RUN
HISTORY = ROOT/'build/tableformer-next-20260915'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', 'utf-8')
    temp.replace(path)


def read(path):
    return json.loads(Path(path).read_text('utf-8'))


def update(ids, status, evidence, **outcomes):
    state = read(AUDIT/'progress.json')
    for task in state['tasks']:
        if task['id'] in ids:
            task.update(status=status, evidence=evidence, **outcomes)
    state['updated_utc'] = datetime.now(timezone.utc).isoformat()
    save(AUDIT/'progress.json', state)


def initialize():
    AUDIT.mkdir(parents=True, exist_ok=False)
    BUILD.mkdir(parents=True, exist_ok=False)
    plan = ROOT/'docs/structure-repair-exploration-tasks-20260919.json'
    tasks = read(plan)['tasks']
    identity = {'run': RUN, 'started_utc': datetime.now(timezone.utc).isoformat(),
                'plan_sha256': sha(plan), 'python': sys.executable,
                'git_head': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                'initial_status': subprocess.check_output(['git','status','--short'],cwd=ROOT,text=True),
                'source_first': str(ROOT/'src'), 'input_import': 'temporary copies only; add_image consumes upload'}
    save(AUDIT/'run-identity.json', identity)
    for task in tasks:
        task.update(attempts=[], evidence=[], errors=[], next_command=None,
                    engineering='not_measured', quality='not_measured',
                    model_simulation='not_measured', real_service='not_measured')
    save(AUDIT/'progress.json', {'identity':identity, 'tasks':tasks})
    files = [* (ROOT/'src/ocr_workbench').glob('*.py'), * (ROOT/'tests').glob('test_*.py')]
    for p in files:
        target = BUILD/'baseline-source'/p.relative_to(ROOT)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(p,target)
    save(AUDIT/'baseline-source-lock.json', {str(p.relative_to(ROOT)):sha(p) for p in files})
    save(AUDIT/'gaps.json', [])
    (AUDIT/'NEXT.md').write_text('Current: T01 baseline replay.\nNext: scripts/structure_repair_run.py replay --name baseline\nNo GPU jobs started. Never overwrite baseline-source or historical inputs.\n','utf-8')
    (AUDIT/'decisions.md').write_text('# Decisions\n\n- Single agent; preserve dirty working tree and old deliveries.\n- Source snapshot captured before changes; baseline before variants.\n- No new offline bundle requested.\n','utf-8')


def protocol():
    manifest = read(HISTORY/'dataset/inputs/test.json')
    random_ids = read(ROOT/'audit/complex-tables-20260919-01/api/random-check-policy.json')['sample_ids']
    value = {'frozen_utc':datetime.now(timezone.utc).isoformat(), 'seed':20260919,
        'historical':{'inputs':len(manifest['samples']), 'split':'regression', 'manifest_sha256':sha(HISTORY/'dataset/inputs/test.json')},
        'development_target_tables':[10,20], 'confirmation_target':{'documents':10,'tables':20,'cells':1000},
        'groups':'document and template; all previously inspected material and related templates regression; new inputs inspected for development cannot enter confirmation',
        'qualification':'real inputs with trustworthy image and source; synthetic cases only engineering; native PDF renders are not physical scans/photos',
        'identity':'reference annotation identities fixed independently of predicted slots; unidentifiable cells count unmatched; also report strict-slot metrics',
        'normalization':{'preservation':'raw literal exact, including whitespace and Unicode', 'quality':'NFKC and remove whitespace, alongside literal exactness; never numeric coercion'},
        'random_check_ids':random_ids, 'random_check_selection':'inherited fixed checks plus sha256(seed+sample ID); never labels',
        'metrics':['input/candidate/table funnels','identity and literal conservation','structure-only and structure+text','corrected/harmed/still-wrong/kept-correct','whole-table exact','strict slots','amount/identifier/merged/blank/repeated/full-page groups','affected identities and maximum extent','local latency and candidate budgets'],
        'interval':'2000 document/template cluster bootstrap resamples, seed 20260919, 95% percentile; not computed without independent paired identity events',
        'local_selection':'keep if no safe local patch; otherwise ascending affected identities then patch kind then fingerprint, never reference scores',
        'human_simulation':'counterfactual adopt every hard-validated selected patch; actual human time/effect unmeasured',
        'model':'gpt-5.6-luna; independent context per table; medium; no labels; no automatic retry; no calls without legal input',
        'continue':{'correct_real_repairs':5,'documents':3,'preservation_violations':0},
        'variants':['exclusive same-source identity','independent full-cell geometry/manual binding','evidence-constrained order/adjacency'],
        'patch_budget':{'max_affected_cells':8,'max_local_patches_per_table':6,'reason':'bounded review effort; freeze before quality inspection; full-grid validation still mandatory'},
        'stop':'at most three evidence-based variants; stop branch if continuation gate not met; no answer-driven iteration',
        'input_change':'new OCR/native extraction recorded as separate experiment, not paired replay',
        'formal_quality':'not replaced by this exploratory sample'}
    save(AUDIT/'protocol.json', value)
    save(AUDIT/'split-lock.json', {'historical':[{'id':s['id'],'group':s['group_id'],'split':'regression'} for s in manifest['samples']], 'confirmation':[], 'protocol_sha256':sha(AUDIT/'protocol.json')})


def replay(name, limit=None):
    wall_started=time.perf_counter()
    from compare_tableformer_geometry import checked_artifact, bind_prediction
    from ocr_workbench.store import Store
    from ocr_workbench.imaging import add_image
    from ocr_workbench.structure_store import record_candidates, refresh_proposals, context, structure_snapshot, _current_tables
    from ocr_workbench.structure_diagnostics import identify_tables
    from ocr_workbench.structure_arbitration import build_snapshot
    from ocr_workbench.complex_table_contract import validate_grid
    out = BUILD/name
    out.mkdir(exist_ok=False)
    manifest_path = HISTORY/'dataset/inputs/test.json'
    samples = read(manifest_path)['samples'][:limit]
    store = Store(out/'workspace')
    project = store.project('Structure preservation research')
    rows = []
    for sample in samples:
        started = time.perf_counter()
        image = (manifest_path.parent/sample['image']).resolve()
        row = {'id':sample['id'],'group':sample['group_id'],'input_sha256_before':sha(image)}
        assert row['input_sha256_before'] == sample['sha256']
        ocr,_,_ = checked_artifact(HISTORY/'inference/test/ppocr',sample,'result.json')
        adopted,_,_ = checked_artifact(HISTORY/'adopted/test',sample,'result.json')
        temp = out/(sample['id']+'.import.jpg'); shutil.copy2(image,temp)
        photo = add_image(store,project['id'],sample['id']+'.jpg',temp)
        version = store.one('versions',photo['active_version'])
        task = store.enqueue(project['id'],[version['id']],['ppocr'])[0]; store.claim()
        store.complete(task,{'engine':'ppocr','text':adopted['text'],'tables':adopted['tables'], 'blocks':[],
                            'project_image_version':version['id'],'image':{'width':sample['width'],'height':sample['height']}})
        rid = store.one('tasks',task)['result_id']
        row.update(result_id=rid, adopted=store.result(rid)['edited'], providers=[])
        for provider,filename in [('tableformer','prediction.json'),('geometry','geometry.json')]:
            pred,_,digest = checked_artifact(HISTORY/'inference/test'/provider,sample,filename)
            pred = bind_prediction(pred,ocr['blocks'],sample,provider)
            pred.update(image_version=version['id'],image_sha256=version['sha256'],candidate_provider_key=provider)
            with store.transaction() as db:
                key = record_candidates(db,rid,version,pred,ocr['blocks'],source_result='fixed-ppocr:'+sample['sha256'])
            row['providers'].append({'provider':provider,'prediction_sha256':digest,'set':key})
        view = refresh_proposals(store,rid,store.result(rid)['revision'])
        with store.transaction() as db:
            current = context(db,rid); live = structure_snapshot(db,current)
            tables,originals = _current_tables(db,current)
            row.update(revision=current['revision'],version_id=version['id'],image_sha256=version['sha256'],
                       proposals=live['proposals'], table_tool=live['table_tool'], candidates=[], gates=[])
            for candidate_row in live['candidate_rows']:
                payload = json.loads(candidate_row['payload'])
                matches = identify_tables(tables,payload['tables'])
                for i,candidate in enumerate(payload['tables']):
                    try: validate_grid(candidate['skeleton'],tokens=payload['tokens']); grid_error=None
                    except ValueError as e: grid_error=str(e)
                    row['candidates'].append({'set':candidate_row['id'],'provider':candidate_row['provider'],
                        'table':candidate, 'tokens':payload['tokens'], 'rejected_tokens':payload['rejected_tokens'],
                        'adopted_table_indices':[j for j,m in enumerate(matches) if m and m[1]==i], 'grid_error':grid_error})
            for ti in range(len(tables)):
                try:
                    snap = build_snapshot(db,current,{'table':ti},version)
                    row['gates'].append({'table':ti,'eligible':True,'snapshot':snap})
                except ValueError as e: row['gates'].append({'table':ti,'eligible':False,'reason':str(e)})
        row['input_sha256_after'] = sha(image)
        assert row['input_sha256_after'] == row['input_sha256_before']
        row['seconds'] = time.perf_counter()-started
        rows.append(row)
        save(out/'cases'/(sample['id']+'.json'),row)
        if len(rows)%10==0: print(json.dumps({'replayed':len(rows),'eligible':sum(g['eligible'] for r in rows for g in r['gates'])}),flush=True)
    save(out/'rows.json',rows)
    summary={'inputs':len(rows),'adopted_tables':sum(len(r['adopted']['tables']) for r in rows),
        'candidate_tables':sum(len(r['candidates']) for r in rows),
        'identifiable_candidates':sum(bool(c['adopted_table_indices']) for r in rows for c in r['candidates']),
        'grid_valid_candidates':sum(c['grid_error'] is None for r in rows for c in r['candidates']),
        'proposals':sum(len(r['proposals']) for r in rows),
        'applicable_proposals':sum(p['can_apply'] for r in rows for p in r['proposals']),
        'sendable_tables':sum(g['eligible'] for r in rows for g in r['gates']),
        'model_calls':0,'input_integrity':all(r['input_sha256_before']==r['input_sha256_after'] for r in rows),
        'primary_rejections':dict(Counter(p['conflicts'][0]['kind'] for r in rows for p in r['proposals'] if p['conflicts'])),
        'secondary_rejections':dict(Counter(k for r in rows for p in r['proposals'] for k in set(c['kind'] for c in p['conflicts'][1:]))),
        'seconds':sum(r['seconds'] for r in rows),'wall_seconds_including_checkpoint_io':time.perf_counter()-wall_started,
        'rows_sha256':sha(out/'rows.json')}
    save(AUDIT/(name+'-funnel.json'),summary)
    print(json.dumps(summary,ensure_ascii=False),flush=True)


def checks(name, full=False):
    import unittest
    from contextlib import redirect_stdout, redirect_stderr
    import ocr_workbench
    assert Path(ocr_workbench.__file__).resolve() == ROOT/'src/ocr_workbench/__init__.py'
    folder = AUDIT/'regression'; folder.mkdir(exist_ok=True)
    log = folder/(name+'.log')
    if log.exists(): raise FileExistsError(log)
    patterns = ['test_*.py'] if full else ['test_structure_repair.py','test_structure_workflow.py','test_complex_tables.py','test_external_review.py','test_external_review_integration.py','test_exporting.py','test_pdf_export.py']
    suite = unittest.TestSuite(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=p) for p in patterns)
    before = {p.name:sha(p) for p in (ROOT/'src/ocr_workbench').glob('*.py')}
    started=time.perf_counter()
    with log.open('w',encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    after = {p.name:sha(p) for p in (ROOT/'src/ocr_workbench').glob('*.py')}
    report = {'tests':result.testsRun,'passed':result.wasSuccessful() and before==after,
        'failures':[{'test':t.id(),'traceback':e} for t,e in result.failures],
        'errors':[{'test':t.id(),'traceback':e} for t,e in result.errors],
        'skipped':[{'test':t.id(),'reason':e} for t,e in result.skipped], 'seconds':time.perf_counter()-started,
        'source_root':str(ROOT/'src'),'source_files':after,'source_unchanged':before==after,'python':sys.executable}
    save(folder/(name+'.json'),report)
    print(json.dumps({k:v for k,v in report.items() if k!='source_files'},ensure_ascii=False),flush=True)
    if not report['passed']: raise SystemExit(1)


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('phase',choices=['init','protocol','replay','checks'])
    p.add_argument('--name',default='baseline'); p.add_argument('--limit',type=int); p.add_argument('--full',action='store_true')
    a=p.parse_args()
    if a.phase=='init': initialize()
    elif a.phase=='protocol': protocol()
    elif a.phase=='replay': replay(a.name,a.limit)
    elif a.phase=='checks': checks(a.name,a.full)
