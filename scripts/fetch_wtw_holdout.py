"""Freeze the first 20 eligible test images encountered in pinned WTW archives."""
import hashlib
import json
from pathlib import Path
import tarfile
import requests

root=Path(__file__).resolve().parents[1]
output=root/'build/document-workflow/geometry-holdout/wtw'
output.mkdir(parents=True,exist_ok=True)
repo='anonymous-iccv1968/DocLayout_YOLO_WTW_iccv1968'
revision='48303cd6e4cd7a127929501706a6ee385d0e83c4'
base=f'https://hf-mirror.com/datasets/{repo}/resolve/{revision}/'
session=requests.Session();session.trust_env=False
test=session.get(base+'test.txt',timeout=60);test.raise_for_status()
(output/'test.txt').write_bytes(test.content)
names={Path(line.strip()).stem for line in test.text.splitlines() if line.strip()}
print('Test filenames',len(names),flush=True)
with session.get(base+'WTW_labels.tar.gz.aa',stream=True,timeout=(30,90)) as response:
    response.raise_for_status()
    with tarfile.open(fileobj=response.raw,mode='r|gz') as archive:
        for member in archive:
            name=Path(member.name)
            if member.isfile() and name.stem in names and name.suffix=='.txt':
                (output/name.name).write_bytes(archive.extractfile(member).read())
records=[]
# This archive is a single gzip stream despite its .aa extension. No random
# access into tar entries and no extraction of arbitrary archive paths.
with session.get(base+'WTW_images.tar.gz.aa',stream=True,timeout=(30,120)) as response:
    response.raise_for_status()
    with tarfile.open(fileobj=response.raw,mode='r|gz') as archive:
        for member in archive:
            name=Path(member.name)
            if not member.isfile() or name.stem not in names or name.suffix.lower() not in ('.png','.jpg','.jpeg'): continue
            label=output/(name.stem+'.txt')
            if not label.exists():continue
            rows=label.read_text('utf-8').splitlines()
            if not 12 <= len(rows) <= 250:continue
            content=archive.extractfile(member).read()
            target=output/name.name;target.write_bytes(content)
            records.append({'image':name.name,'label':label.name,'image_sha256':hashlib.sha256(content).hexdigest(),
                'label_sha256':hashlib.sha256(label.read_bytes()).hexdigest(),'cells':len(rows),'source_member':member.name})
            print(len(records),name.name,len(rows),flush=True)
            if len(records)>=20:break
(output/'download-manifest.json').write_text(json.dumps({'repo':repo,'revision':revision,'protocol':'first 20 test archive entries with 12–250 annotated cell polygons; selection before inference','samples':records},indent=2),'utf-8')
