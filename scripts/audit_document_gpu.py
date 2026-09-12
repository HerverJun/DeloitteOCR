"""Actual queue/adapter/GPU integration; synthetic PDFs are correctness cases only."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
from ocr_workbench.store import Store
from ocr_workbench.task_queue import TaskQueue
from ocr_workbench.documents import Documents
from ocr_workbench.geometry import enqueue_geometry,geometry_view

a.output.mkdir(parents=True,exist_ok=True)
store=Store(a.output/'workspace');project=store.project('真实文档 GPU 验证')
queue=TaskQueue(store,a.bundle);manager=Documents(store,a.bundle,queue)
report={'purpose':'real GPU queue integration, not geometry benchmark','stages':[]}
pdf=a.output/'native-table.pdf'
program='''from fpdf import FPDF
import sys
p=FPDF(unit='pt',format=(400,360));p.add_page();p.set_auto_page_break(False);p.set_font('Helvetica',size=12)
values=[['Item','Code','Quantity','Amount'],['Alpha','001234','10','123.45'],['Bravo','000987','20','234.56'],['Charlie','007654','30','345.67'],['Delta','003210','40','456.78']]
for row,cells in enumerate(values):
 for col,value in enumerate(cells):
  x=20+col*90;y=40+row*45;p.rect(x,y,90,45);p.text(x+6,y+26,value)
p.output(sys.argv[1])'''
subprocess.run([str(a.bundle/'runtimes/pdf/python.exe'),'-I','-X','utf8','-c',program,str(pdf)],check=True)
try:
    doc=manager.import_document(project['id'],pdf.name,pdf,dpi=150)
    page=store.document_pages(doc['id'])[0]
    stage=manager.process(page['id'],'native');manager.step()
    initial=store.rows('SELECT * FROM results')[0]
    started=time.perf_counter();queue.step();cold=time.perf_counter()-started
    tasks=store.rows("SELECT id,status,phase,error FROM tasks WHERE kind='geometry'")
    previews=store.rows("SELECT id,result_id FROM tasks WHERE phase='原生表格结构预览'")
    report['stages'].append({'case':'native-table-geometry','cold_queue_seconds':cold,'geometry_tasks':tasks,'preview_count':len(previews),
        'source_unchanged':store.one('results',initial['id'])['original']==initial['original'],'source_revision':store.one('results',initial['id'])['revision']})
    for preview in previews:
        result=store.result(preview['result_id'])
        report['stages'][-1]['preview_tables']=result['edited']['tables']
        report['stages'][-1]['geometry']=geometry_view(store,result['id'])
    queue.unload()
    doc=manager.import_document(project['id'],'mixed.pdf',root/'build/document-workflow/fixtures/mixed.pdf',dpi=100)
    page=store.document_pages(doc['id'])[0];stage=manager.process(page['id']);manager.step()
    waiting=store.one('document_stages',stage)
    started=time.perf_counter()
    while queue.step(): pass
    from ocr_workbench.page_processing import finalize_waiting_pages
    finalize_waiting_pages(manager)
    completed=store.one('document_stages',stage)
    result=store.result(json.loads(completed['output'])['result_id']) if completed['status']=='succeeded' else None
    report['stages'].append({'case':'mixed-native-and-real-ppocr','initial_state':waiting['status'],'final_stage':completed,
        'seconds':time.perf_counter()-started,'text':result['edited']['text'] if result else None,
        'repeat_stage_identical':manager.process(page['id'])==stage,'region_tasks':store.rows('SELECT t.id,t.status,t.started,t.finished FROM tasks t JOIN page_ocr_inputs i ON i.task_id=t.id WHERE i.stage_id=?',(stage,))})
finally:
    queue.unload();manager.stop()
    (a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
print(a.output/'report.json',flush=True)
