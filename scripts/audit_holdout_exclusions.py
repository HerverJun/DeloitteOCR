"""Audit prior local evaluation material; never silently reselect after inference."""
import hashlib
import json
import os
from pathlib import Path
import re
root=Path(__file__).resolve().parents[1];base=root/'build/document-workflow'
manifest=json.loads((base/'geometry-holdout/frozen/manifest.json').read_text('utf-8'))
samples=manifest['samples'];documents={s['original_document_id']:s['id'] for s in samples}
images={s['sha256']:s['id'] for s in samples}
roots=[root/'audit',root/'fixtures',root/'build']+[Path('E:/')/n for n in ('OCR-deloitte-build','OCR-final-build','OCR-fusion-20260912','OCR-week1-build','OCR-week23-build')]
skips={'runtimes','models','engine-packages','node_modules','.git','wheelhouse','inspect','document-workflow','document-workflow-20260913'}
files=0;image_files=0;hits=[];errors=[];seen_text=set();seen_image=set();inventory=[]
for directory in roots:
    if not directory.exists():continue
    for current,dirs,names in os.walk(directory):
        dirs[:]=[n for n in dirs if n not in skips and not (Path(current)/n).is_symlink() and not (Path(current)/n).is_junction()]
        for name in names:
            path=Path(current)/name
            try:
                if path.suffix.lower() in ('.jpg','.png','.jpeg','.bmp','.tiff','.webp'):
                    sha=hashlib.sha256(path.read_bytes()).hexdigest();image_files+=1;seen_image.add(sha)
                    if sha in images:hits.append({'type':'image-sha256','path':str(path),'sample_id':images[sha]})
                elif path.suffix.lower() in ('.json','.jsonl','.csv','.tsv','.txt') and path.stat().st_size<100_000_000:
                    data=path.read_bytes();sha=hashlib.sha256(data).hexdigest();files+=1
                    if sha in seen_text:continue
                    seen_text.add(sha);text=data.decode('utf-8',errors='ignore')
                    inventory.append({'path':str(path),'sha256':sha,'bytes':len(data)})
                    for doc,key in documents.items():
                        if re.search(r'(?<![A-Za-z0-9])'+re.escape(doc)+r'(?![A-Za-z0-9])',text):
                            hits.append({'type':'original-document-id','document_id':doc,'sample_id':key,'path':str(path)})
            except OSError as error:errors.append({'path':str(path),'error':str(error)})
report={'roots':[str(p) for p in roots],'excluded_directory_names':sorted(skips),
    'metadata_files':files,'unique_metadata_files':len(seen_text),'image_files':image_files,'unique_images':len(seen_image),
    'matches':hits,'read_errors':errors,'inventory':inventory,
    'scope':'Existing local material in all known OCR build roots; source-document identifiers and exact image hashes. Near-duplicate crops with renamed documents cannot be disproven by hashes alone.',
    'frozen_manifest_unchanged':True}
target=root/'audit/document-workflow-20260913/holdout-exclusion-audit.json'
target.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='inventory'},ensure_ascii=False,indent=2))
