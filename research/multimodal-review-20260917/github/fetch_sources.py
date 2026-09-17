import json,urllib.request,pathlib,concurrent.futures,time
root=pathlib.Path('research/multimodal-review-20260917/github')
targets={
'datalab-to/marker':['marker/processors/llm/llm_page_correction.py','marker/processors/llm/llm_table.py','marker/processors/llm/llm_table_merge.py','marker/processors/llm/llm_mathblock.py','marker/processors/llm/__init__.py','marker/services/ollama.py'],
'scribeocr/scribe.js':['js/worker/compareOCRModule.js','js/modifyOCR.js'],
'docling-project/docling':['docs/examples/post_process_ocr_with_vlm.py','docs/examples/code_formula_granite_docling.py','docs/examples/enrich_doclingdocument.py','docs/examples/export_tables.py','docling/datamodel/vlm_model_specs.py','docs/usage/enrichments.md'],
'allenai/olmocr':['olmocr/pipeline.py','olmocr/prompts/anchor.py','olmocr/prompts/prompts.py','olmocr/bench/tests.py'],
'PaddlePaddle/PaddleOCR':['docs/version3.x/pipeline_usage/PP-ChatOCRv4.en.md','paddleocr/_pipelines/pp_chatocrv4_doc.py','docs/version3.x/pipeline_usage/PP-StructureV3.en.md'],
'opendatalab/MinerU':['docs/next/middle-json/structured-content-schema.md','mineru/model/vlm/contracts.py','mineru/backend/postprocess/legacy_schema_adapter.py']}
def fetch(t):
 repo,path=t; d=root/repo.replace('/','__'); sha=json.loads((d/'tree.json').read_text())['sha']; url=f'https://raw.githubusercontent.com/{repo}/{sha}/{path}'
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Codex-OCR-Research'}); content=urllib.request.urlopen(req,timeout=35).read(); (d/path.replace('/','__')).write_bytes(content)
  return {'repo':repo,'path':path,'sha':sha,'url':f'https://github.com/{repo}/blob/{sha}/{path}','bytes':len(content)}
 except Exception as e:return {'repo':repo,'path':path,'error':str(e)}
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as p: results=list(p.map(fetch,[(r,f) for r,fs in targets.items() for f in fs]))
(root/'source-index.json').write_text(json.dumps(results,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps(results,indent=2,ensure_ascii=False))
