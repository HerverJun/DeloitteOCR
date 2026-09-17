"""Freeze human-labelled full pages, including negatives, without inference.

Read selected entries of the official DocLayNet archive using HTTP ranges.
This supplementary protocol is frozen before any page-detector execution.
"""
import argparse
from collections import Counter,defaultdict
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import zipfile
import requests
from PIL import Image
from freeze_geometry_v2 import phash
from geometry_eval_common import ROOT,sha,write_json

URL='https://codait-cos-dax.s3.us.cloud-object-storage.appdomain.cloud/dax-doclaynet/1.0.0/DocLayNet_core.zip'


class RemoteZip(io.RawIOBase):
    def __init__(self,url):
        self.url=url;self.session=requests.Session();self.session.trust_env=False
        response=self.session.head(url,timeout=30);response.raise_for_status()
        self.size=int(response.headers['Content-Length']);self.etag=response.headers['ETag'];self.position=0
    def seekable(self):return True
    def readable(self):return True
    def tell(self):return self.position
    def seek(self,offset,whence=0):
        self.position=offset if whence==0 else self.position+offset if whence==1 else self.size+offset
        return self.position
    def read(self,size=-1):
        size=min(self.size-self.position,size if size>=0 else self.size-self.position)
        if size<=0:return b''
        end=self.position+size-1
        response=self.session.get(self.url,headers={'Range':f'bytes={self.position}-{end}','If-Match':self.etag},timeout=(30,90))
        response.raise_for_status()
        if response.status_code!=206 or response.headers.get('Content-Range')!=f'bytes {self.position}-{end}/{self.size}' or len(response.content)!=size:raise ValueError('Remote archive range or identity mismatch')
        self.position+=size;return response.content


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True)
    if (a.output/'split-lock.json').exists():raise SystemExit('Page supplement already frozen')
    protocol={'version':2,'source_url':URL,'source_version':'DocLayNet 1.0.0','license':'CDLA-Permissive-1.0',
        'source_loader_revision':'5656dbae459cf15b3a112d46bb6b5484cabcd2d2','seed':'pages-v2-20260913',
        'page_count':60,'split_counts':{'development':20,'validation':20,'test':20},
        'strata':{'no_table':20,'single_table':20,'multiple_tables':20},
        'selection':'seeded image ID ordering across official test, val, train annotations; one page per document/company-template stem; pHash <=6 exclusions against retained pages and historical/new crop corpus',
        'scope':'human-labelled page-level table detection only; labels never passed to the model; not a cell matching test',
        'freeze_timing':'supplementary page protocol before any page detector inference; main crop grouping was already frozen',
        'upstream_training_overlap':'not excluded or claimed absent; independent only of local strategy selection'}
    protocol_path=a.output/'protocol.json'
    if protocol_path.exists() and json.loads(protocol_path.read_text('utf-8'))!=protocol:raise ValueError('Page protocol changed')
    write_json(protocol_path,protocol)
    seen=[];excluded_documents=set()
    for path in [ROOT/'build/document-workflow/geometry-holdout/frozen/manifest.json',*[ROOT/'build/table-matching-v2/dataset/inputs'/(s+'.json') for s in ('development','validation','test')]]:
        data=json.loads(path.read_text('utf-8'))
        for sample in data['samples']:
            seen.append(phash((path.parent/sample['image']).read_bytes()))
            excluded_documents.add(sample['original_document_id'])
    stream=RemoteZip(URL);print('archive',stream.size,stream.etag,flush=True)
    with zipfile.ZipFile(stream) as archive:
        names=archive.namelist();print('archive entries',len(names),flush=True)
        selected=[];counts=Counter();documents=set();rejected=Counter();metadata=[]
        for split in ('test','val','train'):
            name=next(n for n in names if n.endswith('COCO/'+split+'.json'))
            data=json.loads(archive.read(name));metadata.append({'entry':name,'sha256':hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()})
            categories={c['id']:c['name'].lower() for c in data['categories']}
            grouped=defaultdict(list)
            for annotation in data['annotations']:
                if categories[annotation['category_id']]=='table':grouped[annotation['image_id']].append(annotation)
            images=sorted(data['images'],key=lambda r:hashlib.sha256(('pages-v2-20260913:'+str(r['id'])).encode()).digest())
            for im in images:
                targets=grouped[im['id']];stratum='no_table' if not targets else 'single_table' if len(targets)==1 else 'multiple_tables'
                if counts[stratum]>=20:continue
                doc=im['doc_name'];stem=re.sub(r'(19|20)\d{2}.*','',Path(doc).stem.lower())
                group=(im.get('collection','')+':'+stem) if len(stem)>=4 else doc
                if group in documents or any(x in doc for x in excluded_documents):continue
                filename=im['file_name'];entry=next(n for n in names if n.endswith('PNG/'+filename))
                content=archive.read(entry)
                with Image.open(io.BytesIO(content)) as image:
                    if image.size!=(im['width'],im['height']):rejected['image_dimensions']+=1;continue
                h=phash(content)
                if any((h^old).bit_count()<=6 for old in seen):rejected['near_duplicate']+=1;continue
                boxes=[]
                for target in targets:
                    x,y,w,height=target['bbox'];box=[x,y,x+w,y+height]
                    if not (0<=x<x+w<=im['width'] and 0<=y<y+height<=im['height']):break
                    boxes.append(box)
                if len(boxes)!=len(targets):rejected['invalid_annotation']+=1;continue
                seen.append(h);documents.add(group);counts[stratum]+=1
                key='doclaynet-'+str(im['id']);image_path=a.output/'images'/(key+'.png');image_path.parent.mkdir(exist_ok=True);image_path.write_bytes(content)
                selected.append({'id':key,'original_document_id':doc,'group_id':group,'cohort':'doclaynet','width':im['width'],'height':im['height'],
                    'image':'../images/'+key+'.png','sha256':hashlib.sha256(content).hexdigest(),'original_split':split,'table_boxes':boxes,
                    'stratum':stratum,'source_annotation_ids':[t['id'] for t in targets],'source_page_no':im.get('page_no'),'phash':format(h,'016x')})
                print('qualified page',len(selected),stratum,flush=True)
                if len(selected)==60:break
            if len(selected)==60:break
    if len(selected)!=60:raise ValueError('Insufficient independent page groups; supplement not frozen')
    allocations=defaultdict(list)
    for stratum in ('no_table','single_table','multiple_tables'):
        rows=[s for s in selected if s['stratum']==stratum]
        offset={'no_table':0,'single_table':1,'multiple_tables':2}[stratum]
        for i,row in enumerate(rows):allocations[('development','validation','test')[(i+offset)%3]].append(row)
    files={};summary={}
    for split,rows in allocations.items():
        inputs=a.output/'inputs'/(split+'.json');labels=a.output/('sealed' if split=='test' else 'annotations')/(split+'.annotations.json')
        write_json(inputs,{'protocol_version':2,'split':split,'track':'page-detection','samples':[{k:v for k,v in r.items() if k not in ('table_boxes','stratum','source_annotation_ids','phash')} for r in rows]})
        write_json(labels,{'protocol_version':2,'split':split,'samples':[{k:v for k,v in r.items() if k not in ('image','phash')} for r in rows]})
        for path in (inputs,labels):files[str(path.relative_to(a.output))]=sha(path)
        summary[split]={'pages':len(rows),'documents':len({r['group_id'] for r in rows}),'tables':sum(len(r['table_boxes']) for r in rows),'strata':dict(Counter(r['stratum'] for r in rows))}
    files['protocol.json']=sha(protocol_path)
    write_json(a.output/'qualification.json',{'summary':summary,'rejections':dict(rejected),'source_metadata':metadata,'archive_etag':stream.etag})
    files['qualification.json']=sha(a.output/'qualification.json')
    write_json(a.output/'split-lock.json',{'version':2,'files':files,'summary':summary,'frozen_before_page_inference':True})
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':main()
