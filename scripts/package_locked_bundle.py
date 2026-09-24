"""Stream a verified bundle to a NEW ZIP64 without changing its manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import zipfile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle',type=Path,required=True)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--receipt',type=Path,required=True)
    a=p.parse_args();root=a.bundle.resolve()
    manifest=(root/'manifest.json').read_bytes()
    records=json.loads(manifest)['files']
    partial=a.archive.with_suffix('.zip.partial')
    if a.archive.exists() or partial.exists():raise ValueError('Refusing to overwrite an archive')
    started=last=time.monotonic();total=0
    with zipfile.ZipFile(partial,'x',allowZip64=True) as z:
        z.writestr('OfflineOCR/manifest.json',manifest,compress_type=zipfile.ZIP_DEFLATED)
        for i,r in enumerate(records,1):
            path=root/r['path']
            if not path.resolve().is_relative_to(root) or path.is_symlink():raise ValueError(r['path'])
            info=zipfile.ZipInfo.from_file(path,'OfflineOCR/'+r['path'])
            info.compress_type=zipfile.ZIP_STORED if r['bytes']>=16*1024*1024 else zipfile.ZIP_DEFLATED
            info._compresslevel=1
            digest=hashlib.sha256();size=0
            with path.open('rb') as src,z.open(info,'w',force_zip64=True) as dst:
                while chunk:=src.read(4*1024*1024):
                    digest.update(chunk);size+=len(chunk);dst.write(chunk)
            assert size==r['bytes'] and digest.hexdigest()==r['sha256'],r['path']
            total+=size
            if time.monotonic()-last>=15:
                print(json.dumps({'files':i,'total':len(records),'bytes':total}),flush=True);last=time.monotonic()
    assert (root/'manifest.json').read_bytes()==manifest
    partial.rename(a.archive)
    with a.archive.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
    receipt={'archive':str(a.archive),'sha256':checksum,'archive_bytes':a.archive.stat().st_size,
        'files':len(records),'uncompressed_bytes':total,'manifest_sha256':hashlib.sha256(manifest).hexdigest(),
        'source_bytes_verified':True,'seconds':time.monotonic()-started}
    a.receipt.parent.mkdir(parents=True,exist_ok=True)
    a.receipt.write_text(json.dumps(receipt,indent=2),'utf-8')
    print(json.dumps(receipt),flush=True)


if __name__=='__main__':main()
