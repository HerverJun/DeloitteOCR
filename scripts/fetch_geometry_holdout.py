"""Build-time download of untouched, pinned public geometry test shards."""
import json
from pathlib import Path
from fetch_assets import download

root=Path(__file__).resolve().parents[1]
metadata=json.loads((root/'build/document-workflow/dataset-metadata.json').read_text('utf-8'))
output=root/'build/document-workflow/geometry-holdout'
output.mkdir(parents=True,exist_ok=True)
files=[]
for repo,number in [('docling-project/PubTables-1M_OTSL',17),('docling-project/FinTabNet_OTSL',1)]:
    info=metadata[repo]
    shard=next(f['rfilename'] for f in info['siblings'] if f['rfilename'].startswith(f'data/test-{number:05d}-'))
    target=output/(repo.split('/')[-1]+'.parquet')
    url=f"https://hf-mirror.com/datasets/{repo}/resolve/{info['sha']}/{shard}"
    if not target.exists():
        receipt=download(url,target)
        files.append(dict(receipt,repo=repo,revision=info['sha'],shard=shard))
        (output/(target.stem+'.download.json')).write_text(json.dumps(files[-1],indent=2),'utf-8')
    print(target,flush=True)
