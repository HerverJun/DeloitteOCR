"""Correct the Windows cu126 wheel's cuDNN pin; never alter inference binaries.

Upstream 3.3.1 reports compiled cuDNN 9.9.0, but Requires-Dist pins 9.5.1.17.
The local version makes this packaging-only repair explicit and reproducible.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
import re
from pathlib import Path
import zipfile

p=argparse.ArgumentParser()
p.add_argument('--wheel',type=Path,required=True)
p.add_argument('--output-dir',type=Path,required=True)
a=p.parse_args()
expected='paddlepaddle_gpu-3.3.1-cp312-cp312-win_amd64.whl'
if a.wheel.name!=expected:
    raise ValueError('This reviewed patch only targets Paddle 3.3.1 Windows CPython 3.12 CUDA 12.6')
a.output_dir.mkdir(parents=True,exist_ok=True)
target=a.output_dir/expected.replace('3.3.1','3.3.1+ocr.1')
records=[]
with zipfile.ZipFile(a.wheel) as source, zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as out:
    metadata=source.read('paddlepaddle_gpu-3.3.1.dist-info/METADATA').decode()
    if 'Requires-Dist: nvidia-cudnn-cu12==9.5.1.17' not in metadata:
        raise ValueError('Upstream metadata changed; patch requires review')
    for name in source.namelist():
        if name.endswith('/RECORD') or name.endswith('/'):
            continue
        content=source.read(name)
        dest=name.replace('paddlepaddle_gpu-3.3.1.dist-info/','paddlepaddle_gpu-3.3.1+ocr.1.dist-info/')
        if name.endswith('/METADATA'):
            repaired,count=re.subn(r'(?m)^Version: 3\.3\.1(?=\r?$)', 'Version: 3.3.1+ocr.1', metadata)
            if count!=1:
                raise ValueError('Expected exactly one upstream version header')
            content=repaired.replace('Requires-Dist: nvidia-cudnn-cu12==9.5.1.17',
                                     'Requires-Dist: nvidia-cudnn-cu12==9.9.0.52').encode()
        out.writestr(dest,content)
        digest=base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()
        records.append((dest,'sha256='+digest,str(len(content))))
    record_path='paddlepaddle_gpu-3.3.1+ocr.1.dist-info/RECORD'
    stream=io.StringIO(newline='');writer=csv.writer(stream,lineterminator='\n')
    writer.writerows(records+[(record_path,'','')]);out.writestr(record_path,stream.getvalue())
audit={'upstream_file':a.wheel.name,'upstream_sha256':hashlib.file_digest(a.wheel.open('rb'),'sha256').hexdigest(),
       'patched_file':target.name,'patched_sha256':hashlib.file_digest(target.open('rb'),'sha256').hexdigest(),
       'changes':['Distribution version 3.3.1+ocr.1','cuDNN Requires-Dist 9.5.1.17 -> 9.9.0.52','Regenerated wheel RECORD'],
       'inference_code_and_binaries':'byte-for-byte unchanged'}
(a.output_dir/'paddle-wheel-patch.json').write_text(json.dumps(audit,indent=2),encoding='utf-8')
print(json.dumps(audit))
