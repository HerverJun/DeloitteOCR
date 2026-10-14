"""Read every ZIP member, verify CRC and compare bytes to the embedded manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import zipfile

p=argparse.ArgumentParser()
p.add_argument('--archive',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
started=time.monotonic();last=started;count=0;total=0
with zipfile.ZipFile(a.archive) as archive:
    manifest=json.loads(archive.read('OfflineOCR/manifest.json'))
    expected={'OfflineOCR/'+r['path']:r for r in manifest['files']}
    names=archive.namelist()
    assert len(names)==len(set(names)), 'Duplicate archive entries'
    assert set(names)==set(expected)|{'OfflineOCR/manifest.json'}, 'Archive inventory mismatch'
    for name,record in expected.items():
        assert not Path(record['path']).is_absolute() and '..' not in Path(record['path']).parts
        digest=hashlib.sha256();size=0
        with archive.open(name) as stream:
            while chunk:=stream.read(4*1024*1024):
                digest.update(chunk);size+=len(chunk)
        assert size==record['bytes'] and digest.hexdigest()==record['sha256'], name
        count+=1;total+=size
        if time.monotonic()-last>20:
            print(json.dumps({'verified':count,'bytes':total}),flush=True);last=time.monotonic()
report={'passed':True,'archive':a.archive.name,'files':count,'bytes':total,
        'method':'Full ZIP decompression/CRC and SHA-256 for every manifest file',
        'seconds':time.monotonic()-started}
a.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report),flush=True)
