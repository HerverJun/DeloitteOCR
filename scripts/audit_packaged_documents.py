"""Compiled launcher + actual packaged PDF/OCR over authenticated HTTP, then PDFium readback."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'scripts'))
sys.path.insert(0,str(a.bundle/'app'))
from audit_application import Application,until
out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
app=Application(a.bundle.resolve(),out);report={'passed':False,'checks':[]}
try:
    app.start();report['health']=app.health
    project=app.api('/projects','POST',{'name':'最终离线文档验收'})['id']
    for name,expected in [('chinese-two-column.pdf','中文'),('mixed.pdf','00123456789012345678')]:
        source=root/'build/document-workflow/fixtures'/name
        boundary=uuid.uuid4().hex
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="dpi"\r\n\r\n150\r\n--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\nContent-Type: application/pdf\r\n\r\n').encode()+source.read_bytes()+f'\r\n--{boundary}--\r\n'.encode()
        imported=app.api('/projects/'+project+'/documents','POST',body,headers={'Authorization':'Bearer '+app.token,'Content-Type':'multipart/form-data; boundary='+boundary})
        assert not imported['errors'],imported
        doc=imported['documents'][0]
        page=app.api('/documents/'+doc['id']+'/pages')['pages'][0]
        app.api('/pages/'+page['id']+'/process','POST',{'mode':'auto'})
        def done():
            stages=app.api('/documents/'+doc['id']+'/stages')['stages']
            assert not any(s['status']=='failed' for s in stages),stages
            pages=app.api('/documents/'+doc['id']+'/pages')['pages']
            return pages[0] if pages[0]['selected_result'] and all(s['status']=='succeeded' for s in stages) else False
        page=until(done,300)
        result=app.api('/results/'+page['selected_result'])
        assert expected in result['edited']['text'],result['edited']['text']
        export={'format':'pdf','document_ids':[doc['id']]}
        preflight=app.api('/export/preflight','POST',export);assert preflight['ready'],preflight
        pdf=out/name;pdf.write_bytes(app.api('/export','POST',export,raw=True))
        code='import json,sys,pypdfium2 as f;p=f.PdfDocument(sys.argv[1]);t=p[0].get_textpage();print(json.dumps({"pages":len(p),"text":t.get_text_range()},ensure_ascii=False))'
        read=json.loads(subprocess.check_output([str(a.bundle/'runtimes/pdf/python.exe'),'-I','-X','utf8','-c',code,str(pdf)],encoding='utf-8'))
        assert expected in read['text'],read
        report['checks'].append({'document':name,'readback':read,'pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'preflight':preflight})
    report['tasks']=[{k:t[k] for k in ('id','kind','engine','status','phase')} for t in app.tasks(project)]
    assert any(t['kind']=='region_ocr' and t['status']=='succeeded' for t in report['tasks'])
    report['passed']=True
finally:
    app.stop()
    (out/'document-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps({'passed':report['passed'],'documents':len(report['checks'])}))
