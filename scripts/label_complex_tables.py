"""Generate clearly weak native-PDF labels and rendered evidence for Agent audit."""
from collections import Counter
import hashlib
import json
import re
import sys

sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parents[1]/'src'))
import pdfplumber
from ocr_workbench.complex_table_contract import validate_annotation
from complex_table_run import ROOT,BUILD,AUDIT,save,sha,update


def prepare():
    sources=[json.loads(l) for l in (BUILD/'sources.jsonl').read_text('utf-8').splitlines()]
    records=[];seen={};duplicates=[]
    for source in sources:
        if source['status']!='success' or source['category']!='native_pdf':continue
        with pdfplumber.open(ROOT/source['path']) as doc:
            count=0
            for page in doc.pages:
                if count>=2:break
                tables=page.find_tables()
                for table in tables:
                    if count>=2:break
                    if len(table.cells)<6:continue
                    key=source['id']+f'-p{page.page_number}-t{tables.index(table)}'
                    xs=sorted({v for box in table.cells for v in (box[0],box[2])})
                    ys=sorted({v for box in table.cells for v in (box[1],box[3])})
                    # Quantize near-coincident vector boundaries before forming slots.
                    def close(values):
                        out=[]
                        for v in values:
                            if not out or v-out[-1]>.5:out.append(v)
                        return out
                    xs,ys=close(xs),close(ys)
                    def at(values,v):return min(range(len(values)),key=lambda i:abs(values[i]-v))
                    scale=1.5
                    image=BUILD/'derived'/f'{source["id"]}-p{page.page_number}.png'
                    if not image.exists():page.to_image(resolution=108).save(image)
                    from PIL import Image
                    with Image.open(image) as im:
                        width,height=im.size
                        crop=BUILD/'derived'/f'{key}-crop.png'
                        im.crop(tuple(round(v*scale) for v in table.bbox)).save(crop)
                    def poly(box):return [[box[0]*scale,box[1]*scale],[box[2]*scale,box[1]*scale],[box[2]*scale,box[3]*scale],[box[0]*scale,box[3]*scale]]
                    cells=[]
                    for i,box in enumerate(sorted(table.cells,key=lambda b:(b[1],b[0]))):
                        r,c=at(ys,box[1]),at(xs,box[0]);rs,cs=at(ys,box[3])-r,at(xs,box[2])-c
                        chars=[ch for ch in page.chars if box[0]<=(ch['x0']+ch['x1'])/2<box[2] and box[1]<=(ch['top']+ch['bottom'])/2<box[3]]
                        text=page.crop(box).extract_text() or ''
                        text_box=[min(ch['x0'] for ch in chars),min(ch['top'] for ch in chars),max(ch['x1'] for ch in chars),max(ch['bottom'] for ch in chars)] if chars else None
                        cells.append({'id':key+':'+str(i),'row':r,'column':c,'row_span':rs,'column_span':cs,
                            'text':text,'text_state':'sourced' if text else 'unknown','text_polygon':poly(text_box) if text_box else None,
                            'cell_polygon':poly(box),'header_ids':[]})
                    label={'id':key,'document_id':source['id'],'template_group':source['template_group'],'page':page.page_number,
                        'polygon':poly(table.bbox),'rows':len(ys)-1,'columns':len(xs)-1,'cells':cells,
                        'image_version':sha(image),'image_sha256':sha(image),'page_to_crop':[[1,0,0],[0,1,0],[0,0,1]],
                        'page_points_to_image':[[scale,0,0],[0,scale,0],[0,0,1]],
                        'label_level':'weak','method':'pdfplumber vector grid and native text; requires visual audit; not independent of PDF tool',
                        'continuation':'unknown','boundary_disputes':[]}
                    try:validation=validate_annotation(label,width,height)
                    except ValueError as e:validation={'valid':False,'error':str(e)}
                    label_path=BUILD/'dataset'/f'{key}.json';save(label_path,label)
                    normalized=re.sub(r'\d','0',re.sub(r'\s+','',''.join(c['text'] for c in cells)))
                    layout=[(c['row'],c['column'],c['row_span'],c['column_span']) for c in cells]
                    near=hashlib.sha256((normalized+json.dumps(layout)).encode()).hexdigest()
                    if near in seen:duplicates.append([seen[near],key])
                    seen[near]=key
                    record={'id':key,'document_id':source['id'],'template_group':source['template_group'],'category':'native_pdf','page':page.page_number,
                        'image':image.relative_to(ROOT).as_posix(),'image_sha256':sha(image),'crop':crop.relative_to(ROOT).as_posix(),
                        'label':label_path.relative_to(ROOT).as_posix(),'label_sha256':sha(label_path),'label_level':'weak','validation':validation,
                        'split':'regression' if source['template_group']=='hk-budget-speech' else 'development',
                        'split_reason':'same historical budget template' if source['template_group']=='hk-budget-speech' else 'Agent exposure required for weak labels',
                        'text_layout_fingerprint':near,'cells':len(cells),'rows':label['rows'],'columns':label['columns'],
                        'merged':sum(c['row_span']*c['column_span']>1 for c in cells),'empty':sum(not c['text'] for c in cells),
                        'repeated':sum(n for t,n in Counter(c['text'] for c in cells).items() if t and n>1)}
                    records.append(record);count+=1
                    print(key,len(cells),validation['valid'],flush=True)
    save(BUILD/'dataset/manifest.json',{'samples':records,'input_scope':'whole page with native vector tables; rendered PDF is not scan'})
    save(BUILD/'dataset/splits.json',{'development':[s['id'] for s in records if s['split']=='development'],
        'regression':[s['id'] for s in records if s['split']=='regression'],'validation':[],'local_sealed':[],'api_sealed':[],
        'reason':'All new labels require Agent inspection. No qualified unexposed group yet.',
        'groups':sorted({s['template_group'] for s in records})})
    save(BUILD/'dataset/duplicates.json',{'exact_or_near_pairs':duplicates,'rules':'document and declared template grouping plus normalized text/layout hashes; historical template conservative exclusion'})
    save(BUILD/'dataset/exposure.json',{'samples':[{'id':s['id'],'scope':'development','reason':'native label generation and Agent visual audit pending'} for s in records]})
    update('A03','done_with_gaps',[f'build/{BUILD.name}/dataset/{n}.json' for n in ('manifest','splits','duplicates','exposure')],engineering='passed',quality='not_met',next_step='Visually inspect candidate labels; score only qualified tracks and retain formal coverage gaps.')


if __name__=='__main__':prepare()
