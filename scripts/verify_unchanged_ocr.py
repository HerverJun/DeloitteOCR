"""Compare independently hashed final OCR runtime inventory to the previous release."""
import json
from pathlib import Path
base=Path('E:/OCR-document-workflow-20260913')
old=json.loads(Path('E:/OCR-fusion-20260912/bundle/manifest.json').read_text('utf-8'))['files']
final={r['path']:r for r in json.loads((base/'tested-manifest.json').read_text('utf-8'))['files']}
prefixes=tuple('runtimes/'+x+'/' for x in ('ppocr','paddlevl','glm','hunyuan','llama'))
selected={r['path']:r for r in old if r['path'].startswith(prefixes)}
changes=[k for k,r in selected.items() if k not in final or final[k]['sha256']!=r['sha256']]
added=[k for k in final if k.startswith(prefixes) and k not in selected]
report={'passed':not changes and not added,'scope':'All prior OCR runtime and llama files compared with independently computed final SHA inventory','files':len(selected),'changed':changes,'added':added}
out=Path(__file__).resolve().parents[1]/'audit/document-workflow-20260913/unchanged-ocr-runtimes.json'
out.write_text(json.dumps(report,indent=2),'utf-8');print(json.dumps(report));assert report['passed']
