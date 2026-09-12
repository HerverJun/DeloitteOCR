"""Actual OCR with document pause/cancel/retry and killed model/service processes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from ocr_workbench.store import Store
from ocr_workbench.documents import Documents
from ocr_workbench.task_queue import TaskQueue
from ocr_workbench.page_processing import finalize_waiting_pages
p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);p.add_argument('--child',action='store_true');a=p.parse_args()
out=root/'build/document-workflow/recovery-audit';out.mkdir(exist_ok=True)
store=Store(out/'workspace');queue=TaskQueue(store,a.bundle);manager=Documents(store,a.bundle,queue)
if a.child:
    queue.step()
    queue.unload()
    raise SystemExit()
report={'scope':'actual PP-OCR inference on a 5-page mixed PDF; process faults injected into this audit only','checks':[]}
def record(name,**values):
    report['checks'].append({'name':name,**values});(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(name,flush=True)
def wait_for(predicate,seconds=90):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.03)
    raise TimeoutError('Audit condition timeout')
def complete(stage):
    while store.rows("SELECT id FROM tasks WHERE status='queued'"):queue.step()
    finalize_waiting_pages(manager)
    value=store.one('document_stages',stage)
    assert value['status']=='succeeded',value
    result=store.result(json.loads(value['output'])['result_id'])
    assert '001234' in result['edited']['text'] and 'Page 001' in result['edited']['text'],result['edited']['text']
    return value
try:
    pdf=out/'five-mixed-pages.pdf'
    program="import pikepdf,sys\np=pikepdf.Pdf.new()\nwith pikepdf.open(sys.argv[1]) as s:\n for _ in range(5):p.pages.append(s.pages[0])\np.save(sys.argv[2])"
    subprocess.run([str(a.bundle/'runtimes/pdf/python.exe'),'-I','-X','utf8','-c',program,str(root/'build/document-workflow/fixtures/mixed.pdf'),str(pdf)],check=True)
    project=store.project('真实 OCR 恢复审计');doc=manager.import_document(project['id'],pdf.name,pdf,dpi=100)
    pages=store.document_pages(doc['id'])
    first=manager.process(pages[0]['id']);manager.step();first_done=complete(first)
    completed_task_snapshot=store.rows("SELECT * FROM tasks WHERE status='succeeded'")
    record('first_page_real_ocr_complete',stage=first_done)
    second=manager.process(pages[1]['id']);third=manager.process(pages[2]['id']);manager.step();manager.step()
    manager.action(doc['id'],'pause')
    assert not store.rows("SELECT id FROM tasks WHERE status IN ('queued','running')")
    assert store.one('document_stages',second)['status']=='paused'
    assert store.one('document_stages',third)['status']=='paused'
    record('pause_prevents_waiting_pages_from_starting')
    manager.action(doc['id'],'resume');manager.step();manager.step();queue.step();finalize_waiting_pages(manager)
    done=[s for s in (second,third) if store.one('document_stages',s)['status']=='succeeded'];assert len(done)==1
    pending=next(s for s in (second,third) if s not in done)
    manager.action(doc['id'],'cancel');assert store.one('document_stages',pending)['status']=='cancelled'
    record('cancel_keeps_completed_pages_and_cancels_only_unfinished',completed=done,cancelled=pending)
    manager.action(doc['id'],'retry');manager.step();complete(pending)
    record('retry_cancelled_page_finishes_with_actual_ocr')
    queue.unload()
    fourth=manager.process(pages[3]['id']);manager.step()
    worker=threading.Thread(target=queue.step);worker.start()
    process=wait_for(lambda:queue.adapter.process if queue.adapter and queue.adapter.process else None)
    wait_for(lambda:process.poll() is None)
    time.sleep(.3);pid=process.pid;process.kill();worker.join(timeout=40);assert not worker.is_alive()
    finalize_waiting_pages(manager)
    assert store.one('document_stages',fourth)['status']=='failed'
    record('killed_model_process_becomes_retriable_failure',model_pid=pid,stage=store.one('document_stages',fourth))
    manager.action(doc['id'],'retry');manager.step();complete(fourth)
    record('model_failure_retry_completes_with_actual_ocr')
    queue.unload()
    fifth=manager.process(pages[4]['id']);manager.step()
    with (out/'killed-service.log').open('w',encoding='utf-8') as log:
        child=subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--bundle',str(a.bundle),'--child'],stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        import psutil
        engine_child=wait_for(lambda:next((p for p in psutil.Process(child.pid).children(recursive=True) if 'ppocr' in p.exe()),None))
        time.sleep(.5);engine_pid=engine_child.pid;child.kill();child.wait(timeout=30)
        wait_for(lambda:not psutil.pid_exists(engine_pid),30)
    assert store.rows("SELECT id FROM tasks WHERE status='running'")
    store.recover();store.recover_document_stages()
    assert store.rows("SELECT id FROM tasks WHERE status='interrupted'")
    assert store.one('document_stages',fifth)['status']=='paused'
    record('killed_service_leaves_durable_stage_and_job_object_releases_gpu',service_pid=child.pid,engine_pid=engine_pid)
    manager.action(doc['id'],'resume');manager.step();complete(fifth)
    for row in completed_task_snapshot:assert store.one('tasks',row['id'])==row
    assert store.one('document_stages',first)==first_done
    assert manager.process(pages[0]['id'])==first
    record('restart_resume_finishes_only_unfinished_work',first_page_unchanged=True,first_page_stage_id_reused=True,
           stages=store.rows('SELECT id,status,attempt FROM document_stages'),tasks=store.rows('SELECT id,status,started,finished FROM tasks'))
    report['passed']=True
finally:
    queue.unload();manager.stop();(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
