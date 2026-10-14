"""Real 1000-page import, concurrent PDF worker process/RSS sampling, lazy cache."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time
import psutil
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from ocr_workbench.store import Store
from ocr_workbench.documents import Documents
out=root/'build/document-workflow/capacity-audit';out.mkdir(exist_ok=True)
store=Store(out/'workspace');manager=Documents(store,root/'build/document-workflow/bundle')
project=store.project('1000 页按需展开与进程内存')
running=True;samples=[];proc=psutil.Process()
def sample():
    while running:
        children=[]
        for child in proc.children(recursive=True):
            try:
                if 'pdf/python.exe' in child.exe().replace('\\','/').lower():children.append(child)
            except psutil.Error:pass
        try:
            values=[p.memory_info().rss for p in children if p.is_running()]
            samples.append({'time':time.perf_counter(),'workers':len(values),'children_rss':sum(values),'parent_rss':proc.memory_info().rss})
        except psutil.Error:pass
        time.sleep(.025)
t=threading.Thread(target=sample);t.start()
try:
    start=time.perf_counter();doc=manager.import_document(project['id'],'thousand.pdf',root/'build/document-workflow/fixtures/thousand.pdf',dpi=150)
    imported=time.perf_counter()-start
    after_import=len(store.rows('SELECT id FROM images'))
    pages=[p for offset in range(0,1000,200) for p in store.document_pages(doc['id'],offset=offset,limit=200)]
    chosen=[pages[n-1] for n in (1,2,500,501,999,1000)]
    with ThreadPoolExecutor(max_workers=6) as executor:images=list(executor.map(lambda p:manager.ensure_rendered(p['id']),chosen))
    after_render=len(store.rows('SELECT id FROM images'))
    start=time.perf_counter();manager.ensure_rendered(chosen[2]['id']);cached_ms=(time.perf_counter()-start)*1000
    report={'page_count':doc['page_count'],'import_seconds':imported,'images_after_import':after_import,'images_after_six_requested_pages':after_render,
        'requested_page_numbers':[p['page_number'] for p in chosen],'render_cache_ms':cached_ms,
        'peak_cpu_workers':max(s['workers'] for s in samples),'peak_parent_rss_mib':max(s['parent_rss'] for s in samples)/1024**2,
        'peak_child_rss_mib':max(s['children_rss'] for s in samples)/1024**2,
        'peak_combined_rss_mib':max(s['parent_rss']+s['children_rss'] for s in samples)/1024**2,
        'rss_scope':'parent service logic plus all its live isolated PDF child processes; excludes OS filesystem cache',
        'samples':samples}
    assert doc['page_count']==1000 and after_import==0 and after_render==6
    assert report['peak_cpu_workers']<=2 and report['peak_cpu_workers']>0
finally:
    running=False;t.join();manager.stop()
(out/'report.json').write_text(json.dumps(report,indent=2),'utf-8');print(json.dumps({k:v for k,v in report.items() if k!='samples'},indent=2))
