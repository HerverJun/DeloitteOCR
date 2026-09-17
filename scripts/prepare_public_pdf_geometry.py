"""Download public development PDFs with receipts; render through the product PDF path.

These are exposed engineering samples, not independently labelled quality data.
The source PDF is retained unchanged. Raster PDFs are not claimed physical scans.
"""
import argparse
import concurrent.futures
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1] / 'src')]
from geometry_eval_common import sha, write_json

SOURCES = [
    {'id': 'camelot-foo', 'repository': 'camelot-dev/camelot', 'path': 'tests/files/foo.pdf', 'pages': [1]},
    {'id': 'camelot-column-span', 'repository': 'camelot-dev/camelot', 'path': 'tests/files/column_span_2.pdf', 'pages': [1]},
    {'id': 'camelot-row-span', 'repository': 'camelot-dev/camelot', 'path': 'tests/files/row_span_1.pdf', 'pages': [1]},
    {'id': 'camelot-multiple-tables', 'repository': 'camelot-dev/camelot', 'path': 'tests/files/multiple_tables.pdf', 'pages': [1]},
    {'id': 'camelot-image', 'repository': 'camelot-dev/camelot', 'path': 'tests/files/image.pdf', 'pages': [1]},
    {'id': 'pdfplumber-nics', 'repository': 'jsvine/pdfplumber', 'path': 'tests/pdfs/nics-background-checks-2015-11.pdf', 'pages': [1]},
    {'id': 'hk-budget-2024', 'url': 'https://www.budget.gov.hk/2024/chi/pdf/c_budget_speech_2024-25.pdf', 'pages': [1, 10, 30, 60], 'language': 'zh'},
]


def fetch(args):
    import requests
    args.output.mkdir(parents=True, exist_ok=True)
    def get(source):
        folder = args.output / 'sources' / source['id']
        receipt_path, path = folder / 'receipt.json', folder / 'source.pdf'
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text('utf-8'))
            if receipt['status'] == 'success':
                if sha(path) != receipt['sha256']:
                    raise ValueError('Public PDF changed: ' + source['id'])
                return receipt
        session = requests.Session()
        session.trust_env = False
        receipt = {**source, 'created_utc': datetime.now(timezone.utc).isoformat(),
                   'usage': 'local engineering evaluation; source documents are not redistributed',
                   'independent_cell_annotations': False}
        try:
            if source.get('repository'):
                response = session.get('https://api.github.com/repos/' + source['repository'] + '/commits?per_page=1', timeout=(15, 30))
                response.raise_for_status()
                commit = response.json()[0]['sha']
                receipt['source_commit'] = commit
                receipt['url'] = 'https://raw.githubusercontent.com/' + source['repository'] + '/' + commit + '/' + source['path']
            try:
                response = session.get(receipt['url'], timeout=(20, 30))
                response.raise_for_status()
                content = response.content
            except requests.RequestException:
                if not source.get('repository'):
                    raise
                response = session.get('https://api.github.com/repos/' + source['repository'] + '/contents/' + source['path'],
                                       params={'ref': receipt['source_commit']}, timeout=(15, 30))
                response.raise_for_status()
                payload = response.json()
                if payload.get('encoding') != 'base64':
                    raise ValueError('GitHub content encoding unavailable')
                content = base64.b64decode(payload['content'])
                receipt['transport'] = 'GitHub contents API at the same source commit'
            if not content.startswith(b'%PDF-') or len(content) > 50_000_000:
                raise ValueError('Not a bounded PDF file')
            folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            receipt.update(status='success', sha256=sha(path), bytes=len(content), final_url=response.url)
        except Exception as error:
            receipt.update(status='failed', error=str(error))
        write_json(receipt_path, receipt)
        print(source['id'], receipt['status'], receipt.get('error', ''), flush=True)
        return receipt
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(get, SOURCES))
    write_json(args.output / 'download-summary.json', {'sources': receipts, 'quality_acceptance': False})


def render(args):
    from ocr_workbench.documents import PdfCPU
    from ocr_workbench.geometry_contract import fingerprint
    cpu = PdfCPU(args.bundle, args.output)
    receipts = json.loads((args.output / 'download-summary.json').read_text('utf-8'))['sources']
    samples, failures = [], []
    for receipt in receipts:
        if receipt['status'] != 'success':
            failures.append(receipt)
            continue
        path = (args.output / 'sources' / receipt['id'] / 'source.pdf').resolve()
        if sha(path) != receipt['sha256']:
            raise ValueError('Public PDF receipt mismatch')
        try:
            metadata = cpu.call({'operation': 'inspect', 'path': str(path), 'dpi': args.dpi})
            write_json(path.parent / 'metadata.json', metadata)
            pages = receipt['pages'] + ([70, 85, 88, 90] if receipt.get('language') == 'zh' else [])
            for page_number in pages:
                if page_number > len(metadata['pages']):
                    continue
                key = receipt['id'] + '-p' + str(page_number)
                folder = args.output / 'pages' / key
                folder.mkdir(parents=True, exist_ok=True)
                image = (folder / 'input.png').resolve()
                if image.exists() and (folder / 'native.json').exists():
                    result = json.loads((folder / 'native.json').read_text('utf-8'))
                    if result['metadata']['render_dpi'] != args.dpi:
                        raise ValueError('Public PDF DPI changed; create a new dataset')
                else:
                    result = cpu.call({'operation': 'render', 'path': str(path), 'page_number': page_number,
                                       'dpi': args.dpi, 'image_output': str(image)})
                    write_json(folder / 'native.json', result)
                with __import__('PIL.Image', fromlist=['Image']).open(image) as im:
                    width, height = im.size
                samples.append({'id': key, 'original_document_id': receipt['id'], 'group_id': receipt['sha256'],
                    'image': '../pages/' + key + '/input.png', 'sha256': sha(image), 'width': width, 'height': height,
                    'cohort': 'public-pdf-development', 'input_kind': 'public-pdf-page',
                    'source_pdf_sha256': receipt['sha256'], 'page_number': page_number,
                    'native_result_sha256': fingerprint(result), 'independent_cell_annotations': False})
                print(key, width, height, 'rendered', flush=True)
        except Exception as error:
            failures.append({'id': receipt['id'], 'error': str(error)})
            print(receipt['id'], 'render failed', str(error), flush=True)
    manifest = args.output / 'inputs/development.json'
    write_json(manifest, {'protocol_version': 2, 'split': 'development', 'track': 'public-pdf-real-input', 'samples': samples})
    write_json(args.output / 'split-lock.json', {'version': 3, 'files': {'inputs/development.json': sha(manifest)}})
    write_json(args.output / 'render-summary.json', {'samples': len(samples), 'failures': failures, 'quality_acceptance': False})


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['fetch', 'render'])
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--bundle', type=Path)
    p.add_argument('--dpi', type=int, default=150)
    args = p.parse_args()
    {'fetch': fetch, 'render': render}[args.command](args)


if __name__ == '__main__':
    main()
