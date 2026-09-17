import json, urllib.request, pathlib, concurrent.futures, datetime
out=pathlib.Path('research/multimodal-review-20260917/github')
repos=['datalab-to/marker','scribeocr/scribe.js','docling-project/docling','ibm-granite/granite-docling','allenai/olmocr','opendatalab/MinerU','PaddlePaddle/PaddleOCR','zai-org/GLM-OCR','rednote-hilab/dots.ocr']
def get(url):
 req=urllib.request.Request(url,headers={'User-Agent':'Codex-OCR-Research','Accept':'application/vnd.github+json'})
 with urllib.request.urlopen(req,timeout=40) as r:return r.read(),dict(r.headers)
def one(repo):
 d=out/repo.replace('/','__'); d.mkdir(exist_ok=True)
 try:
  raw,h=get('https://api.github.com/repos/'+repo); data=json.loads(raw); (d/'metadata.json').write_bytes(raw)
  branch=data['default_branch']; files=['README.md','LICENSE','LICENSE.md','LICENSE.txt']
  for f in files:
   try:
    b,_=get(f'https://raw.githubusercontent.com/{repo}/{branch}/{f}'); (d/f.replace('/','__')).write_bytes(b)
   except Exception: pass
  tree,_=get(f'https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1'); (d/'tree.json').write_bytes(tree)
  return {'repo':repo,'branch':branch,'stars':data['stargazers_count'],'pushed_at':data['pushed_at'],'license':data['license'],'url':data['html_url']}
 except Exception as e:return {'repo':repo,'error':str(e)}
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
 results=list(pool.map(one,repos))
(out/'repo-index.json').write_text(json.dumps({'collected_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'repos':results},indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps(results,indent=2,ensure_ascii=False))
