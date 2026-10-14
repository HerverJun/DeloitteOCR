"""Keep real exported PDF samples and independent PDFium render/highlight evidence."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from ocr_workbench.store import Store
from ocr_workbench.documents import Documents
from ocr_workbench.pdf_export import build_pdf_export
base=root/'build/document-workflow';out=root/'audit/document-workflow-20260913/pdf-visuals';out.mkdir(exist_ok=True)
bundle=Path('E:/OCR-document-workflow-20260913/bundle')
store=Store(base/'gpu-audit/workspace');manager=Documents(store,bundle)
records=[]
for n,doc in enumerate(store.rows('SELECT * FROM documents ORDER BY created')):
    if doc['kind']!='pdf':continue
    preflight=build_pdf_export(store,manager,{'document_ids':[doc['id']]},preflight=True)
    if not preflight['ready']:
        (out/f'gpu-{n+1:02d}-blocked.json').write_text(json.dumps(preflight,ensure_ascii=False,indent=2),'utf-8')
        continue
    path=build_pdf_export(store,manager,{'document_ids':[doc['id']]})
    target=out/f'gpu-{n+1:02d}.pdf';shutil.copy2(path,target)
    records.append({'name':doc['name'],'pdf':str(target),'original':str(store.file(doc['original_path']))})
fresh=Store(out/'workspace');pdfs=Documents(fresh,bundle);project=fresh.project('PDF中文视觉证据')
doc=pdfs.import_document(project['id'],'chinese-two-column.pdf',base/'fixtures/chinese-two-column.pdf',dpi=100)
page=fresh.document_pages(doc['id'])[0];pdfs.process(page['id'],'native');pdfs.step()
path=build_pdf_export(fresh,pdfs,{'document_ids':[doc['id']]});target=out/'chinese-two-column.pdf';shutil.copy2(path,target)
records.append({'name':doc['name'],'pdf':str(target),'original':str(fresh.file(doc['original_path']))})
from ocr_workbench.task_queue import TaskQueue
from ocr_workbench.page_processing import finalize_waiting_pages
queue=TaskQueue(fresh,bundle);pdfs.gpu=queue
native_table=base/'gpu-audit/native-table.pdf'
doc=pdfs.import_document(project['id'],'native-table-auto.pdf',native_table,dpi=150)
page=fresh.document_pages(doc['id'])[0];stage=pdfs.process(page['id'],'auto');pdfs.step()
try:
    while fresh.rows("SELECT id FROM tasks WHERE status='queued' AND kind='region_ocr'"):queue.step()
    finalize_waiting_pages(pdfs)
    path=build_pdf_export(fresh,pdfs,{'document_ids':[doc['id']]});target=out/'native-table-auto.pdf';shutil.copy2(path,target)
    records.append({'name':doc['name'],'pdf':str(target),'original':str(fresh.file(doc['original_path']))})
finally:queue.unload()
program='''import json,sys,pypdfium2 as f
from PIL import ImageDraw,ImageChops
from pathlib import Path
records=json.loads(Path(sys.argv[1]).read_text('utf-8'))
for record in records:
 p=f.PdfDocument(record['pdf']);q=p[0];t=q.get_textpage();im=q.render(scale=2).to_pil().convert('RGB');target=Path(record['pdf']);im.save(target.with_suffix('.png'))
 original=f.PdfDocument(record['original']);op=original[0];other=op.render(scale=2).to_pil().convert('RGB');record['identical_visible_pixels']=im.size==other.size and ImageChops.difference(im,other).getbbox() is None
 record['independent_text']=t.get_text_range();highlight=im.copy();draw=ImageDraw.Draw(highlight,'RGBA');record['highlight_rectangles']=[]
 for i in range(t.count_rects()):
  l,b,r,a=t.get_rect(i);box=[l*2,(q.get_height()-a)*2,r*2,(q.get_height()-b)*2];draw.rectangle(box,fill=(244,183,46,65),outline=(144,85,10,180));record['highlight_rectangles'].append(box)
 highlight.save(target.with_name(target.stem+'-highlight.png'));t.close();q.close();p.close();op.close();original.close()
Path(sys.argv[1]).write_text(json.dumps(records,ensure_ascii=False,indent=2),'utf-8')'''
receipt=out/'report.json';receipt.write_text(json.dumps(records,ensure_ascii=False,indent=2),'utf-8')
subprocess.run([str(bundle/'runtimes/pdf/python.exe'),'-I','-X','utf8','-c',program,str(receipt)],check=True)
manager.stop();pdfs.stop();print(receipt)
