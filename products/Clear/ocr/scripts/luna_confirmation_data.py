"""Acquire new documents and freeze bounded inputs before any model labels."""
from datetime import datetime,timezone
from pathlib import Path
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts'),str(ROOT/'build/public-quality-20260916/tool/site-packages')]
from native_repair_pilot import read,save,sha

RUN='structure-repair-native-20260919-03'
BUILD=ROOT/'build'/RUN;AUDIT=ROOT/'audit'/RUN


def main():
    import pdfplumber
    from ocr_workbench.pdf_worker import render_page
    from ocr_workbench.pdf_tables import extract_tables
    from ocr_workbench.page_processing import merge_page
    from ocr_workbench.native_tables import native_table_preview
    from ocr_workbench.structure_repair import content_baseline,native_header_patches
    protocol={'created':datetime.now(timezone.utc).isoformat(),'stage':'new-document confirmation',
        'documents':['audit-86-ch02','audit-86-ch03','audit-86-ch04'],
        'selection':'first two pages per document after page 10 with a default pdfplumber table of 8..40 cells, >=3 rows and >=2 columns, successful native preview and >=1 frozen header patch; scan <=60 pages',
        'source_urls':[f'https://www.aud.gov.hk/pdf_ca/c86ch{i:02d}.pdf' for i in (2,3,4)],
        'scope':'new documents within one exposed audit-report family, not new-template or formal quality acceptance',
        'model':'independent Astra image-only references; independent Luna selection; no labels during sample selection',
        'frozen_files':{p:sha(ROOT/p) for p in ['src/ocr_workbench/structure_repair.py','src/ocr_workbench/structure_wire.py','src/ocr_workbench/structure_arbitration.py']}}
    if (AUDIT/'protocol.json').exists():raise FileExistsError('Already frozen')
    save(AUDIT/'protocol.json',protocol)
    rows=[];discovery=[];acquisition=[]
    for number in (2,3,4):
        document=f'audit-86-ch{number:02d}';url=f'https://www.aud.gov.hk/pdf_ca/c86ch{number:02d}.pdf'
        source=BUILD/'sources'/f'{document}.pdf';source.parent.mkdir(parents=True,exist_ok=True)
        started=time.monotonic()
        with urllib.request.urlopen(url,timeout=30) as response:data=response.read(20*1024*1024+1)
        if len(data)>20*1024*1024 or not data.startswith(b'%PDF-'):raise ValueError('Invalid/oversized PDF')
        source.write_bytes(data);acquisition.append({'document':document,'url':url,'sha256':sha(source),'bytes':len(data)})
        found=0
        with pdfplumber.open(source) as pdf:
            for page in pdf.pages[10:60]:
                number=page.page_number;tables=page.find_tables()
                eligible=[i for i,t in enumerate(tables) if 8<=len(t.cells)<=40 and len(t.rows)>=3 and len(t.columns)>=2]
                if not eligible:continue
                ident=f'{document}-p{number}';folder=BUILD/'pages'/ident;folder.mkdir(parents=True,exist_ok=True)
                rendered=render_page({'path':str(source),'page_number':number,'dpi':150,'image_output':str(folder/'page.png')})
                metadata=rendered['metadata'];digest=sha(folder/'page.png')
                version={'id':ident+':'+digest[:16],'sha256':digest,'width':metadata['width'],'height':metadata['height']}
                prediction=extract_tables(source,number,metadata)
                raw=merge_page(rendered['native'],[],version,{'id':document,'sha256':sha(source)},{'id':ident,'page_number':number},'native')
                preview=native_table_preview(raw,prediction,version['width'],version['height'])
                selected=[]
                if preview and len(preview['tables'])==len(prediction['pdfplumber_tables']):
                    for ti in eligible:
                        if ti>=len(preview['tables']):continue
                        baseline=content_baseline(preview['tables'][ti],result_id=ident,revision=0,image_version=version['id'],image_sha256=digest)
                        patches,_=native_header_patches(baseline)
                        if patches:selected=[ti];break
                discovery.append({'document':document,'page':number,'grid_tables':len(tables),'size_qualified':eligible,'selected':selected})
                if not selected:continue
                row={'id':ident,'document':document,'template':'hk-audit-report','page':number,'selected_indices':selected,
                    'source_sha256':sha(source),'image':str(folder/'page.png'),'image_sha256':digest,'version':version,
                    'metadata':metadata,'raw':raw,'prediction':prediction,'preview':preview,'native_flagged':len(rendered['native']['flagged'])}
                save(folder/'page.json',row);rows.append(row);save(BUILD/'pages.json',rows)
                found+=1;print(document,number,'selected',selected,'cells',len(preview['tables'][selected[0]]['cells']),flush=True)
                if found>=2:break
        print(document,'done',found,'seconds',time.monotonic()-started,flush=True)
    save(AUDIT/'acquisition.json',acquisition);save(AUDIT/'discovery.json',discovery)
    save(AUDIT/'pilot-config.json',{'pages':[[r['document'],r['template'],r['page'],r['selected_indices']] for r in rows]})


if __name__=='__main__':main()
