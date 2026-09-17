"""Pinned public PDF pilot. No predictions or extracted text become labels."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'),str(ROOT/'scripts')]
from geometry_eval_common import sha, write_json

COMMIT = '9a607c0d560369aa55a792533c516d61b74d8807'
FILES = ['budget.pdf','budget_2014-15.pdf','agstat.pdf','column_span_1.pdf','row_span_2.pdf',
         'twotables_1.pdf','hybrid_multipage.pdf','missing_values.pdf','empty.pdf','tabula/china.pdf','district_health.pdf']


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--bundle',type=Path,required=True)
    p.add_argument('--fetch-only',action='store_true')
    a = p.parse_args()
    out = a.output.resolve()
    if (out/'split-lock.json').exists():
        raise ValueError('Dataset already frozen; use its existing manifest')
    out.mkdir(parents=True,exist_ok=True)
    import urllib.request
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    from ocr_workbench.geometry_contract import fingerprint
    sources = [{'id':'pilot-'+Path(name).stem.replace('_','-'),'url':f'https://raw.githubusercontent.com/camelot-dev/camelot/{COMMIT}/tests/files/{name}',
                'pages':[1,2],'repository_revision':COMMIT,'template_group':'camelot-budget' if name.startswith('budget') else Path(name).stem} for name in FILES]
    sources.append({'id':'hk-budget-2025','url':'https://www.budget.gov.hk/2025/chi/pdf/c_budget_speech_2025-26.pdf','pages':[70,85],
                    'language':'zh','template_group':'hk-budget-speech'})
    def fetch(source):
        folder = out/'sources'/source['id']
        folder.mkdir(parents=True,exist_ok=True)
        receipt = {**source,'usage':'local public engineering evaluation; source PDF not redistributed','independent_cell_annotations':False}
        path = folder/'source.pdf'
        try:
            if path.exists():
                old = json.loads((folder/'receipt.json').read_text('utf-8'))
                if sha(path) != old['sha256']:
                    raise ValueError('Source receipt mismatch')
                return old
            try:
                import requests
                session = requests.Session(); session.trust_env = False
                response = session.get(source['url'],timeout=(10,15));response.raise_for_status()
                content = response.content
            except Exception:
                if 'raw.githubusercontent.com/camelot-dev/camelot/' not in source['url']:
                    raise
                import base64
                relative = source['url'].split(COMMIT+'/',1)[1]
                url = f'https://api.github.com/repos/camelot-dev/camelot/contents/{relative}?ref={COMMIT}'
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url,timeout=30) as response:
                    data = json.load(response)
                content = base64.b64decode(data['content'])
                receipt['transport'] = 'GitHub contents API at pinned revision'
            if not content.startswith(b'%PDF-') or len(content)>50_000_000:
                raise ValueError('Not a bounded PDF')
            path.write_bytes(content)
            receipt.update(status='success',sha256=hashlib.sha256(content).hexdigest(),bytes=len(content))
        except Exception as error:
            receipt.update(status='failed',error=str(error))
        write_json(folder/'receipt.json',receipt)
        print(source['id'],receipt['status'],flush=True)
        return receipt
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(fetch,sources))
    write_json(out/'download-summary.json',{'sources':receipts,'quality_acceptance':False})
    if a.fetch_only:
        return
    from ocr_workbench.documents import PdfCPU
    cpu = PdfCPU(a.bundle,out)
    samples, failures = [], []
    for receipt in receipts:
        if receipt['status'] != 'success':
            failures.append(receipt); continue
        path = out/'sources'/receipt['id']/'source.pdf'
        try:
            meta = cpu.call({'operation':'inspect','path':str(path),'dpi':150})
            write_json(path.parent/'metadata.json',meta)
            numbers = [n for n in receipt['pages'] if n <= len(meta['pages'])]
            if not numbers:
                numbers = [1]
            for number in numbers:
                sample_id = receipt['id']+'-p'+str(number)
                folder = out/'pages'/sample_id
                folder.mkdir(parents=True,exist_ok=True)
                image = folder/'input.png'
                native = cpu.call({'operation':'render','path':str(path),'page_number':number,'dpi':150,'image_output':str(image)})
                write_json(folder/'native.json',native)
                with Image.open(image) as im:
                    width,height = im.size
                samples.append({'id':sample_id,'original_document_id':receipt['id'],'group_id':receipt['template_group'],
                    'image':'../pages/'+sample_id+'/input.png','sha256':sha(image),'width':width,'height':height,
                    'source_pdf_sha256':receipt['sha256'],'page_number':number,'native_result_sha256':fingerprint(native),
                    'cohort':'public-pdf-development','input_kind':'public-pdf-page','independent_cell_annotations':False,
                    'physical_scan_verified':False})
                print(sample_id,'rendered',flush=True)
        except Exception as error:
            failures.append({'id':receipt['id'],'stage':'parse_render','error':str(error)})
    manifest = out/'inputs/development.json'
    write_json(manifest,{'protocol_version':2,'split':'development','track':'public-pdf-real-input','samples':samples})
    write_json(out/'split-lock.json',{'created_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'public development pilot; no independent annotated quality test',
        'files':{'inputs/development.json':sha(manifest)},'documents':len({s['original_document_id'] for s in samples}),
        'template_groups':len({s['group_id'] for s in samples}),'pages':len(samples),'failures':failures})
    print(json.dumps({'documents':len({s['original_document_id'] for s in samples}),'pages':len(samples),'failures':len(failures)}),flush=True)


if __name__ == '__main__':
    main()
