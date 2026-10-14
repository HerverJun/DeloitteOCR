"""500 existing engineering images; synthetic multi-engine payloads, no OCR score."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import statistics
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
try:
    import psutil
except ImportError:
    # Reuse a local packaged measurement dependency; never install or load OCR.
    sys.path.append('E:/OCR-week23-build/bundle/runtimes/ppocr/Lib/site-packages')
    import psutil
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image
from ocr_workbench.fusion import default_policy
from ocr_workbench.tables import parse_tables
from ocr_workbench.task_queue import FusionQueue


def worker(out, crash):
    store = Store(out/'workspace')
    if crash:
        # Abrupt process failure immediately after a durable claim, before any
        # result commit. This exercises actual SQLite reopen and recovery.
        task = store.claim(fusion=True)
        (out/'crashed-task.json').write_text(json.dumps(task), 'utf-8')
        import os
        os._exit(73)
    queue = FusionQueue(store, ROOT)
    queue.start()
    try:
        for project in store.rows('SELECT id FROM projects'):
            ids = [t['id'] for t in store.rows("SELECT id FROM tasks WHERE project_id=? AND kind='fusion' AND status IN ('interrupted','paused')", (project['id'],))]
            if ids: queue.action(project['id'], 'resume', ids)
        deadline = time.monotonic()+300
        while store.rows("SELECT count(*) n FROM tasks WHERE kind='fusion' AND status IN ('queued','running','interrupted')")[0]['n']:
            if time.monotonic()>deadline: raise TimeoutError('CPU capacity run exceeded 300s')
            time.sleep(.05)
    finally:
        queue.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, default=Path('E:/OCR-final-build/batch-stress-final/fixtures'))
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--crash', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve()
    if args.worker: return worker(out, args.crash)
    out.mkdir(parents=True, exist_ok=False)
    store = Store(out/'workspace')
    project = store.project('500 张 CPU 融合容量测试')
    paths = sorted(p for p in args.fixtures.glob('*.png') if p.stem.isdigit())
    assert len(paths)==500
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    assert len(set(hashes))==500
    policy = default_policy('table'); policy['baseline']='glm'
    seeded = time.monotonic()
    for n,path in enumerate(paths):
        temporary = out/('import-'+path.name)
        shutil.copy2(path, temporary)
        photo = add_image(store, project['id'], path.name, temporary)
        ids = store.enqueue(project['id'], [photo['active_version']], ['glm','paddlevl','hunyuan'], fusion_policy=policy, request_id='engineering-'+path.stem)
        for _ in range(3):
            task = store.claim()
            assert task['kind']!='fusion'
            rows = ''.join(f'<tr><td>Item {r}</td><td>{n:04d}{r:04d}</td><td>{r if task["engine"]=="glm" or r%5 else r+1}.00</td></tr>' for r in range(1,31))
            text = '<table><tr><td>Item</td><td>Code</td><td>Amount</td></tr>'+rows+'</table>'
            raw = {'engine':task['engine'], 'text':text, 'tables':parse_tables(text), 'blocks':[],
                   'image':{'width':600,'height':400}, 'project_image_version':photo['active_version'],
                   'engineering_fixture':True, 'elapsed_seconds':0, 'load_seconds':0}
            store.complete(task['id'], raw)
        if n%100==99: print(json.dumps({'seeded':n+1}),flush=True)
    seed_seconds=time.monotonic()-seeded
    command=[sys.executable,'-B','-X','utf8',str(Path(__file__).resolve()),'--output',str(out),'--worker']
    crashed=subprocess.run(command+['--crash'])
    assert crashed.returncode==73
    crashed_task=json.loads((out/'crashed-task.json').read_text('utf-8'))
    assert store.one('tasks',crashed_task['id'])['status']=='running'
    started=time.monotonic()
    child=subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process=psutil.Process(child.pid)
    rss=[]; saves=[]; pages=[]; cpu_seconds=0; edits=0
    try:
        while child.poll() is None:
            try:
                rss.append(process.memory_info().rss)
                cpu=process.cpu_times(); cpu_seconds=max(cpu_seconds,cpu.user+cpu.system)
            except psutil.Error: pass
            completed=store.rows("SELECT r.id FROM results r JOIN tasks t ON t.id=r.task_id WHERE t.kind='fusion' ORDER BY t.created LIMIT 1")
            if completed:
                result_id=completed[0]['id']
                saved=store.result(result_id)
                draft=deepcopy(saved['edited']); draft['tables'][0]['cells'][0]['text']='连续编辑 '+str(edits)
                at=time.monotonic(); store.save(result_id,draft,saved['revision']); saves.append(time.monotonic()-at); edits+=1
                at=time.monotonic(); page=store.review_issues(result_id,offset=(edits%2)*5,limit=5); pages.append(time.monotonic()-at)
                assert page['total']==6 and 1<=len(page['issues'])<=5
            if time.monotonic()-started>330: raise TimeoutError('Child did not finish')
            time.sleep(.03)
        stdout,stderr=child.communicate()
        assert child.returncode==0,stderr.decode('utf-8',errors='replace')
    finally:
        if child.poll() is None: child.terminate(); child.wait(timeout=15)
    reopened=Store(out/'workspace')
    counts={r['status']:r['n'] for r in reopened.rows("SELECT status,count(*) n FROM tasks WHERE kind='fusion' GROUP BY status")}
    assert counts=={'succeeded':500},counts
    assert edits>0 and reopened.result(completed[0]['id'])['revision']==edits
    assert reopened.one('tasks',crashed_task['id'])['status']=='succeeded'
    def latency(values): return {'count':len(values),'median_ms':statistics.median(values)*1000,'max_ms':max(values)*1000}
    report={'passed':True,'scope':'500 existing distinct synthetic engineering images; three synthetic original OCR payloads per image, 93 cells each. No model inference, OCR accuracy or human timing claim.',
            'image_sha256':hashes,'count':500,'states':counts,'seed_seconds':seed_seconds,
            'fusion_wall_seconds':time.monotonic()-started,'worker_cpu_seconds':cpu_seconds,
            'worker_peak_rss_bytes':max(rss),'concurrent_save':latency(saves),'issue_pages':latency(pages),
            'evidence_units':reopened.rows('SELECT count(*) n FROM fusion_evidence')[0]['n'],
            'issues':reopened.rows('SELECT count(*) n FROM fusion_issues')[0]['n'],
            'abrupt_exit_code':73,'recovered_task':crashed_task['id'],'reopen_preserved_edits':edits,
            'ocr_seconds':None,'human_seconds':None,'failure_rate_after_resume':0,
            'limits':policy['limits']}
    (out/'capacity.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'image_sha256','limits'}},ensure_ascii=False),flush=True)


if __name__=='__main__': main()
