"""Stream a complete ZIP64 with a manifest of the exact archived bytes."""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
import zipfile

p = argparse.ArgumentParser()
p.add_argument('--bundle', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
root = a.bundle.resolve()
if a.output.resolve().is_relative_to(root):
    raise SystemExit('Archive must be outside the bundle')
if a.output.exists():
    raise SystemExit('Refusing to overwrite an existing deliverable')
a.output.parent.mkdir(parents=True, exist_ok=True)
partial = a.output.with_suffix('.zip.part')
records = []
started = time.monotonic()
last = started

def included_paths():
    for path in sorted(root.rglob('*')):
        rel=path.relative_to(root)
        if path.is_file() and '__pycache__' not in rel.parts and rel.parts[0] not in {'cache','runs','results'} and rel.as_posix()!='manifest.json':
            yield path

def prefetch(path):
    # Bound queued memory to 64 * 256 KiB; stream large artifacts separately.
    return path, path.read_bytes() if path.stat().st_size<=256*1024 else None

def prefetched_paths():
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths=iter(included_paths())
        pending=deque()
        for _ in range(64):
            if (path:=next(paths,None)) is not None:pending.append(pool.submit(prefetch,path))
        while pending:
            yield pending.popleft().result()
            if (path:=next(paths,None)) is not None:pending.append(pool.submit(prefetch,path))

with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
    for path,content in prefetched_paths():
        rel = path.relative_to(root)
        info = zipfile.ZipInfo.from_file(path, 'OfflineOCR/' + rel.as_posix())
        # Avoid expensive recompression of huge model weights; ZIP64 has no size cap.
        info.compress_type = zipfile.ZIP_STORED if path.suffix in {'.safetensors', '.gguf', '.pdiparams', '.dll', '.pyd', '.lib'} else zipfile.ZIP_DEFLATED
        info._compresslevel = 1
        digest = hashlib.sha256()
        count = 0
        with archive.open(info, 'w', force_zip64=True) as dest:
            if content is not None:
                digest.update(content)
                dest.write(content)
                count=len(content)
            else:
                with path.open('rb') as source:
                    while chunk := source.read(4 * 1024 * 1024):
                        digest.update(chunk)
                        dest.write(chunk)
                        count += len(chunk)
        records.append({'path': rel.as_posix(), 'bytes': count, 'sha256': digest.hexdigest()})
        if time.monotonic() - last > 20:
            print(json.dumps({'files': len(records), 'bytes': sum(r['bytes'] for r in records), 'current': rel.as_posix()}), flush=True)
            last = time.monotonic()
    manifest = json.dumps({'schema_version': 1, 'files': records}, ensure_ascii=False, indent=2)
    (root/'manifest.json').write_text(manifest, encoding='utf-8')
    archive.writestr('OfflineOCR/manifest.json', manifest.encode('utf-8'))
partial.replace(a.output)
with a.output.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
receipt = {'archive': a.output.name, 'bytes': a.output.stat().st_size, 'sha256': digest,
           'files': len(records), 'uncompressed_bytes': sum(r['bytes'] for r in records),
           'seconds': time.monotonic()-started}
a.output.with_suffix('.receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
a.output.with_suffix('.sha256').write_text(digest + '  ' + a.output.name + '\n', encoding='ascii')
print(json.dumps(receipt), flush=True)
