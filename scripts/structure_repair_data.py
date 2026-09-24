"""Qualify native full pages and a bounded set of official real-scan examples."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from structure_repair_run import ROOT, AUDIT, BUILD, read, save, sha
from structure_repair_sources import fetch


def collect_scans():
    # One bounded repository batch, selected by upstream sample names, not results.
    base='https://raw.githubusercontent.com/cndplab-founder/ICDAR2019_cTDaR/master/'
    paths=['README.md','samples/readme.txt']
    paths += [f'samples/ground_truth/cTDaR_s{i:03d}.{ext}' for i in [1,2,3,4,101,102,103,104] for ext in ['jpg','xml']]
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows=list(pool.map(fetch,[(f'icdar-sample-{i:02d}',base+p,'official_sample') for i,p in enumerate(paths)]))
    save(AUDIT/'scan-acquisition.json',{'repository_attempt':2,'records':rows,'upstream_paths':paths})


def qualify():
    sys.path.insert(0,str(ROOT/'build/public-quality-20260916/tool/site-packages'))
    import pdfplumber
    from ocr_workbench.pdf_tables import without_shading
    output=BUILD/'development-pages';output.mkdir(exist_ok=False)
    records=[]
    for key in ['hk-budget-2024','hk-budget-2023']:
        path=BUILD/'sources'/key/'source.pdf'
        with pdfplumber.open(path) as doc:
            count=0
            for page in doc.pages:
                if count>=8:break
                tables=page.find_tables()
                if not any(len(t.cells)>=6 for t in tables):continue
                image=output/f'{key}-p{page.page_number}.png'
                page.to_image(resolution=108).save(image)
                alternatives=page.filter(without_shading).find_tables()
                raw={'page':page.page_number,'native_chars':len(page.chars),'words':page.extract_words(),
                     'tables':[{'bbox':t.bbox,'cells':t.cells,'values':t.extract()} for t in tables],
                     'without_shading':[{'bbox':t.bbox,'cells':t.cells,'values':t.extract()} for t in alternatives]}
                evidence=output/f'{key}-p{page.page_number}.json';save(evidence,raw)
                for ti,t in enumerate(tables):
                    if len(t.cells)<6:continue
                    count+=1
                    records.append({'id':f'{key}-p{page.page_number}-t{ti}','document':key,'template':'hk-budget-speech',
                        'split':'regression','reason':'historical template; new year is not an independent confirmation group',
                        'category':'native_pdf','physical_scan':False,'image':str(image),'image_sha256':sha(image),
                        'source_sha256':sha(path),'evidence':str(evidence),'table_index':ti,'cells':len(t.cells),
                        'page_tables':len(tables),'labels':'unlabelled independent quality; vector extraction is production evidence only',
                        'input_change':'fresh native extraction, not paired with historical OCR adoption'})
    scan=read(AUDIT/'scan-acquisition.json')
    scan_rows=[]
    for i,path in enumerate(scan['upstream_paths']):
        if not path.endswith('.jpg'):continue
        item=scan['records'][i];label=scan['records'][i+1]
        if item['status']!='downloaded' or label['status']!='downloaded':continue
        image=output/Path(path).name;image.write_bytes(Path(item['path']).read_bytes())
        xml=ET.fromstring(Path(label['path']).read_bytes())
        tables=xml.findall('.//table')
        cells=xml.findall('.//cell')
        scan_rows.append({'id':image.stem,'image':str(image),'image_sha256':sha(image),
            'xml':label['path'],'tables':len(tables),'cells':len(cells),'split':'development',
            'document_template':'unresolved; upstream sample IDs are not independent document IDs',
            'category':'physical_scan_or_photo_pending_visual_audit','literal_labels':any(c.find('Text') is not None for c in cells),
            'adopted_baseline':None,'gap':'no adopted OCR revision or independent adopted content-to-box association'})
    save(AUDIT/'dataset-qualification.json',{'native':records,'real_image_samples':scan_rows,
         'confirmation':[],'human_verified_labels':0,'not_formal_quality':True})
    print(json.dumps({'native_tables':len(records),'native_documents':2,'real_image_samples':len(scan_rows)}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['scans','qualify']);a=p.parse_args()
    collect_scans() if a.phase=='scans' else qualify()
