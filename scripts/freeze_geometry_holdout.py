"""Freeze canonical full-cell geometry without tuning or model inference."""
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import requests
import pyarrow.parquet as pq
from PIL import Image

root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from ocr_workbench.tables import parse_tables
from ocr_workbench.editing import tables_html
folder=root/'build/document-workflow/geometry-holdout'
frozen=folder/'frozen';frozen.mkdir(exist_ok=True)
manifest_path=frozen/'manifest.json'
if manifest_path.exists():raise SystemExit('Already frozen; do not change after inference')
prior_hashes=set();prior_documents=set();prior_files=0
for base in (root/'audit',root/'fixtures',root/'build'):
    for path in base.rglob('*'):
        if not path.is_file() or folder.parent in path.parents:continue
        if path.suffix.lower() in ('.png','.jpg','.jpeg'):
            prior_hashes.add(hashlib.sha256(path.read_bytes()).hexdigest());prior_files+=1
        if path.suffix in ('.json','.jsonl') and path.stat().st_size < 60_000_000:
            text=path.read_text('utf-8',errors='ignore')
            prior_documents.update(re.findall(r'PMC\d+',text))
samples=[];groups=set()

def save(row,source,document_id,canonical=None):
    if document_id in groups or document_id in prior_documents:return False
    content=row['image']['bytes'];sha=hashlib.sha256(content).hexdigest()
    if sha in prior_hashes:return False
    cells=row['cells'][0]
    if not 12 <= len(cells) <= 250 or not 2 <= row['rows'] <= 60 or not 2 <= row['cols'] <= 20:return False
    tables=parse_tables('<table>'+''.join(row['html_restored'])+'</table>')
    if len(tables)!=1 or len(tables[0]['cells'])!=len(cells):return False
    table=tables[0]
    width,height=Image.open(io.BytesIO(content)).size
    targets=[]
    for cell,annotation in zip(table['cells'],cells):
        cell['text']=''.join(annotation['tokens']);cell['confidence']=None
        box=annotation['bbox'][:4]
        # Clamp the conversion's occasional one-pixel boundary roundoff.
        box=[max(0,box[0]),max(0,box[1]),min(width,box[2]),min(height,box[3])]
        if box[2]<=box[0] or box[3]<=box[1]:return False
        targets.append({'row':cell['row'],'column':cell['column'],'row_span':cell['row_span'],'column_span':cell['column_span'],
            'text':cell['text'],'box':box})
    if canonical:
        originals=sorted(canonical['cells'],key=lambda c:(min(c['row_nums']),min(c['column_nums'])))
        expected=sorted(targets,key=lambda c:(c['row'],c['column']))
        if len(originals)!=len(expected):return False
        bbox=canonical['pdf_table_bbox'];pixels=row['table_bbox']
        sx=(pixels[2]-pixels[0])/(bbox[2]-bbox[0]);sy=(pixels[3]-pixels[1])/(bbox[3]-bbox[1])
        errors=[]
        for original,target in zip(originals,expected):
            if (min(original['row_nums']),min(original['column_nums']))!=(target['row'],target['column']):return False
            b=original['pdf_bbox'];mapped=[pixels[0]+(b[0]-bbox[0])*sx,pixels[1]+(b[1]-bbox[1])*sy,pixels[0]+(b[2]-bbox[0])*sx,pixels[1]+(b[3]-bbox[1])*sy]
            errors.append(max(abs(x-y) for x,y in zip(mapped,target['box'])))
        if max(errors)>2:return False
    key=f'{source}-{len(samples)+1:03d}'
    image=frozen/(key+Path(row['filename']).suffix);image.write_bytes(content)
    group={'id':key,'cohort':source,'original_document_id':document_id,'original_filename':row['filename'],'image':image.name,'sha256':sha,
        'width':width,'height':height,'table':table,'targets':targets,
        'table_box':row.get('table_bbox',[0,0,width,height]),'tags':['merged'] if any(c['row_span']>1 or c['column_span']>1 for c in targets) else [],
        'annotation_source':'Docling OTSL full cell bbox conversion; no equal-grid boxes'}
    if canonical:
        original_file=frozen/(key+'.canonical.json');original_file.write_text(json.dumps(canonical,ensure_ascii=False,indent=2),'utf-8')
        group['canonical_geometry_max_pixel_difference']=max(errors)
        group['canonical_file']=original_file.name
    group['fixed_edit']={'text':tables_html([table]),'tables':[table]}
    groups.add(document_id);samples.append(group);print(key,row['filename'],len(targets),flush=True)
    return True

pub=pq.read_table(folder/'PubTables-1M_OTSL.parquet')
indices={Path(name).stem:i for i,name in enumerate(pub.column('filename').to_pylist())}
session=requests.Session();session.trust_env=False
source='https://hf-mirror.com/datasets/bsmock/pubtables-1m/resolve/35b1c097807e0b07ec5313879b85956b7b3890db/PubTables-1M-PDF_Annotations.tar.gz'
with session.get(source,stream=True,timeout=(30,120)) as response:
    response.raise_for_status()
    with tarfile.open(fileobj=response.raw,mode='r|gz') as archive:
        for member in archive:
            if not member.isfile():continue
            doc=Path(member.name).name.split('_tables')[0]
            if doc in groups or not any(k.startswith(doc+'_') for k in indices):continue
            for table in json.load(archive.extractfile(member)):
                key=table['structure_id']
                if key in indices and table['split']=='test' and not table['exclude_for_structure']:
                    if save(pub.slice(indices[key],1).to_pylist()[0],'pubtables-1m',doc,table):break
            if len(samples)>=60:break
del pub
fin=pq.read_table(folder/'FinTabNet_OTSL.parquet')
order=sorted(range(fin.num_rows),key=lambda i:hashlib.sha256(('geometry-20260913:'+fin.column('filename')[i].as_py()).encode()).digest())
for index in order:
    row=fin.slice(index,1).to_pylist()[0]
    document_id=row['filename'].split('.page_')[0]
    save(row,'fintabnet',document_id)
    if len(samples)>=100:break
if len(samples)<100 or sum(len(s['targets']) for s in samples)<1000:raise SystemExit('Insufficient independent test tables')
manifest={'schema_version':1,'frozen_before_inference':True,'seed':'geometry-20260913','protocol':'60 PubTables canonical matched test tables, then 40 FinTabNet test documents ordered by seeded hash; one table per source document',
    'excluded_prior_image_files':prior_files,'excluded_prior_hashes':len(prior_hashes),'excluded_prior_document_ids':sorted(prior_documents),
    'text_policy':'Reference text and logical structure held fixed; GPU matching receives only independent PP-OCR blocks, never target boxes',
    'table_count':len(samples),'target_count':sum(len(s['targets']) for s in samples),'samples':samples}
manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
(frozen/'manifest.sha256').write_text(hashlib.sha256(manifest_path.read_bytes()).hexdigest()+'\n','utf-8')
print('FROZEN',manifest['table_count'],manifest['target_count'],flush=True)
