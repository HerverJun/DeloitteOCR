"""Prepare grouped v2 data BEFORE matcher development. No inference or model labels.

PubTables canonical PDF cell bounds and OTSL crop conversion are cross-checked.
Historical manifests stay immutable. Inference inputs and sealed scoring labels
are separate files, bound by a write-once protocol and hashes.
"""
import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile

import numpy as np
from PIL import Image
import pyarrow.parquet as pq
import requests
from scipy.fft import dctn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from ocr_workbench.tables import parse_tables
from ocr_workbench.editing import tables_html

SEED = 'table-cell-matching-v2-20260913'
REVISION = '35b1c097807e0b07ec5313879b85956b7b3890db'


def digest(data): return hashlib.sha256(data).hexdigest()


def phash(data):
    with Image.open(io.BytesIO(data)) as im:
        a=np.asarray(im.convert('L').resize((32,32)),dtype=float)
    low=dctn(a,norm='ortho')[:8,:8].flatten()[1:]
    return int(''.join('1' if v>np.median(low) else '0' for v in low),2)


def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8')


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--candidate-count',type=int,default=320);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True)
    if (a.output/'split-lock.json').exists(): raise SystemExit('Already frozen; use a new output directory')
    historical_path=ROOT/'build/document-workflow/geometry-holdout/frozen/manifest.json'
    historical=json.loads(historical_path.read_text('utf-8'))
    protocol={'version':2,'seed':SEED,'counts':{'development':80,'validation':60,'test':120},
        'minimum_cells_per_table':50,'maximum_cells_per_table':250,'one_table_per_document':True,
        'historical_manifest_sha256':digest(historical_path.read_bytes()),
        'selection':'first 320 qualified canonical documents in pinned archive order, sorted by seeded hash; near-duplicate exclusions before splitting',
        'near_duplicate_rules':{'phash_hamming_at_most':6,'text_jaccard_at_least':.9,'shape_required_for_text':True},
        'coordinate_policy':'canonical pdf_bbox affine-mapped via pdf_table_bbox to OTSL table_bbox; max discrepancy 2 pixels; boundary roundoff <=1 pixel',
        'sample_scope':'PubTables crop matching only; FinTabNet without newly verified canonical labels and page-level/native/Chinese/photo tracks cannot be claimed by this manifest',
        'scope_adjustment_reason':'Available pinned local shard has canonical PubTables labels accessible in the official archive; other tracks require separately qualified inputs.',
        'source_revision':REVISION,'test_labels':'sealed/test.annotations.json; never read by inference or development replay',
        'text_policy':'reference text/structure is control-track input; independent PP-OCR provides coordinates; no target polygons given to inference'}
    protocol_path=a.output/'protocol.json'
    if protocol_path.exists() and json.loads(protocol_path.read_text('utf-8'))!=protocol:raise SystemExit('Protocol changed during preparation')
    write(protocol_path,protocol)
    excluded_docs=set(historical.get('excluded_prior_document_ids',[])) | {s['original_document_id'] for s in historical['samples']}
    # Include source IDs from all existing tracked/regression metadata, without
    # reading future sealed labels or scanning model/runtime trees.
    for base in (ROOT/'audit',ROOT/'research',ROOT/'fixtures'):
        for file in base.rglob('*.json'):
            if file.stat().st_size<60_000_000:
                excluded_docs.update(re.findall(r'PMC\d+',file.read_text('utf-8',errors='ignore')))
    features=[]
    for sample in historical['samples']:
        values={''.join(c['text'].split()) for c in sample['targets'] if c['text'].strip()}
        features.append((phash((historical_path.parent/sample['image']).read_bytes()),values,
                         (sample['table']['rows'],sample['table']['columns']),sample['id']))
    shard=ROOT/'build/document-workflow/geometry-holdout/PubTables-1M_OTSL.parquet'
    source_receipt=json.loads(shard.with_suffix('.download.json').read_text('utf-8'))
    if digest(shard.read_bytes())!=source_receipt['sha256']:raise ValueError('Source shard hash mismatch')
    table=pq.read_table(shard)
    indices={Path(name).stem:i for i,name in enumerate(table['filename'].to_pylist())}
    relevant_docs={key.split('_table_')[0] for key in indices}
    cache=a.output/'canonical';cache.mkdir(exist_ok=True)
    selected=[];reject=Counter();duplicate_checks=[];seen=set(excluded_docs)

    def consider(canonical):
        key=canonical['structure_id'];doc=canonical['pmc_id']
        if key not in indices or doc in seen:return
        if canonical['split']!='test' or canonical['exclude_for_structure']:return
        row=table.slice(indices[key],1).to_pylist()[0]
        try:
            parsed=parse_tables('<table>'+''.join(row['html_restored'])+'</table>')
            if len(parsed)!=1:raise ValueError('multiple_structures')
            structure=parsed[0];cells=structure['cells']
            originals=sorted(canonical['cells'],key=lambda c:(min(c['row_nums']),min(c['column_nums'])))
            annotations=row['cells'][0]
            if not 50<=len(cells)<=250:raise ValueError('size_outside_scope')
            if len(cells)!=len(originals) or len(cells)!=len(annotations):raise ValueError('slot_count_conflict')
            data=row['image']['bytes']
            with Image.open(io.BytesIO(data)) as im:width,height=im.size
            pb=canonical['pdf_table_bbox'];crop=row['table_bbox'];sx=(crop[2]-crop[0])/(pb[2]-pb[0]);sy=(crop[3]-crop[1])/(pb[3]-pb[1])
            transform=[sx,0,crop[0]-pb[0]*sx,0,sy,crop[1]-pb[1]*sy,0,0,1]
            targets=[];maximum_error=0
            for cell,original,annotation in zip(cells,originals,annotations):
                rs,cs=original['row_nums'],original['column_nums']
                if rs!=list(range(min(rs),max(rs)+1)) or cs!=list(range(min(cs),max(cs)+1)):raise ValueError('noncontiguous_annotation_span')
                if (cell['row'],cell['column'],cell['row_span'],cell['column_span'])!=(min(rs),min(cs),len(rs),len(cs)):raise ValueError('annotation_span_conflict')
                value=''.join(annotation['tokens'])
                if '\ufffd' in value:raise ValueError('transcription_replacement_character')
                cell['text']=value;cell['confidence']=None
                b=original['pdf_bbox'];mapped=[sx*b[0]+transform[2],sy*b[1]+transform[5],sx*b[2]+transform[2],sy*b[3]+transform[5]]
                observed=annotation['bbox'][:4];error=max(abs(x-y) for x,y in zip(mapped,observed));maximum_error=max(maximum_error,error)
                if error>2:raise ValueError('coordinate_conversion_conflict')
                if observed[0]<-1 or observed[1]<-1 or observed[2]>width+1 or observed[3]>height+1:raise ValueError('out_of_bounds_annotation')
                box=[max(0,observed[0]),max(0,observed[1]),min(width,observed[2]),min(height,observed[3])]
                if box[0]>=box[2] or box[1]>=box[3]:raise ValueError('invalid_annotation')
                targets.append({**{k:cell[k] for k in ('row','column','row_span','column_span','text')},'box':box,'canonical_pdf_bbox':b})
            values={''.join(c['text'].split()) for c in cells if c['text'].strip()};shape=(structure['rows'],structure['columns']);h=phash(data)
            for old_h,old_values,old_shape,old_id in features:
                distance=(h^old_h).bit_count();jaccard=len(values&old_values)/max(1,len(values|old_values))
                if distance<=6 or shape==old_shape and jaccard>=.9:
                    duplicate_checks.append({'id':key,'prior':old_id,'phash_distance':distance,'text_jaccard':jaccard,'action':'excluded_before_inference'})
                    raise ValueError('near_duplicate')
            features.append((h,values,shape,key));seen.add(doc)
            counts=Counter(''.join(c['text'].split()) for c in cells)
            for t in targets:t['tags']=[name for name,condition in [('merged',t['row_span']>1 or t['column_span']>1),('empty',not t['text'].strip()),('repeated',bool(t['text'].strip()) and counts[''.join(t['text'].split())]>1)] if condition]
            item={'id':key,'cohort':'pubtables-1m','original_document_id':doc,'group_id':doc,'original_filename':row['filename'],
                'width':width,'height':height,'image':'images/'+row['filename'],'sha256':digest(data),'phash':format(h,'016x'),
                'fixed_edit':{'text':tables_html([structure]),'tables':[structure]},'input_kind':'rendered-table-crop','table_region_source':'input-table-crop',
                'targets':targets,'coordinate_transform':transform,'canonical_geometry_max_pixel_difference':maximum_error,
                'canonical_sha256':digest(json.dumps(canonical,sort_keys=True).encode()),'tags':sorted({tag for t in targets for tag in t['tags']})}
            selected.append(item)
            image=a.output/item['image'];image.parent.mkdir(exist_ok=True);image.write_bytes(data)
            write(cache/(key+'.json'),canonical)
            if len(selected)%20==0:print('qualified',len(selected),'rejected',sum(reject.values()),flush=True)
        except (ValueError,KeyError,ZeroDivisionError) as error:reject[str(error)]+=1

    # Cached canonical objects can resume interrupted downloads. Archive order is
    # recovered from an append-only list, never directory traversal order.
    order_path=a.output/'canonical-order.jsonl'
    if order_path.exists():
        for line in order_path.read_text('utf-8').splitlines():
            canonical_path=cache/(json.loads(line)+'.json')
            if canonical_path.exists():consider(json.loads(canonical_path.read_text('utf-8')))
    if len(selected)<a.candidate_count:
        session=requests.Session();session.trust_env=False
        url=f'https://hf-mirror.com/datasets/bsmock/pubtables-1m/resolve/{REVISION}/PubTables-1M-PDF_Annotations.tar.gz'
        with session.get(url,stream=True,timeout=(30,90)) as response:
            response.raise_for_status()
            with tarfile.open(fileobj=response.raw,mode='r|gz') as archive:
                for member in archive:
                    if not member.isfile():continue
                    doc=Path(member.name).name.split('_tables')[0]
                    if doc not in relevant_docs or doc in seen:continue
                    for canonical in json.load(archive.extractfile(member)):
                        before=len(selected);consider(canonical)
                        if len(selected)>before:
                            with order_path.open('a',encoding='utf-8') as f:f.write(json.dumps(canonical['structure_id'])+'\n')
                            break
                    if len(selected)>=a.candidate_count:break
    if len(selected)<260:
        write(a.output/'qualification-incomplete.json',{'qualified':len(selected),'rejected':reject});raise SystemExit('Insufficient qualified samples; no test split frozen')
    selected.sort(key=lambda s:digest((SEED+':'+s['id']).encode()))
    files={};cursor=0;summary={}
    for split,count in protocol['counts'].items():
        rows=selected[cursor:cursor+count];cursor+=count
        inputs=[];labels=[]
        for row in rows:
            inp={k:v for k,v in row.items() if k not in ('targets','coordinate_transform','canonical_geometry_max_pixel_difference','canonical_sha256','tags')}
            inp['image']='../'+inp['image'];inputs.append(inp)
            labels.append({k:v for k,v in row.items() if k not in ('fixed_edit','image','phash')})
        input_path=a.output/'inputs'/(split+'.json');label_path=a.output/('sealed' if split=='test' else 'annotations')/(split+'.annotations.json')
        write(input_path,{'protocol_version':2,'split':split,'track':'control','samples':inputs})
        write(label_path,{'protocol_version':2,'split':split,'samples':labels})
        files[str(input_path.relative_to(a.output))]=digest(input_path.read_bytes());files[str(label_path.relative_to(a.output))]=digest(label_path.read_bytes())
        summary[split]={'tables':len(rows),'targets':sum(len(r['targets']) for r in rows),'documents':len({r['group_id'] for r in rows}),
            'target_tags':dict(Counter(t for r in rows for c in r['targets'] for t in c['tags']))}
    write(a.output/'qualification.json',{'summary':summary,'rejections_before_inference':dict(reject),'excluded_document_count':len(excluded_docs),
        'near_duplicates':duplicate_checks,'annotation_checks':'all retained cells: bounds, PDF conversion, span and transcription checks; key attributes counted on target cells',
        'unqualified_tracks':['FinTabNet canonical refresh','native PDF end-to-end','page-level detection','Chinese/photo logical matching'],
        'unused_qualified_candidates':len(selected)-cursor,'source':source_receipt})
    files['protocol.json']=digest(protocol_path.read_bytes());files['qualification.json']=digest((a.output/'qualification.json').read_bytes())
    write(a.output/'split-lock.json',{'version':2,'frozen_before_new_algorithm':True,'seed':SEED,'files':files,'summary':summary,'historical_exposed':True})
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':main()
