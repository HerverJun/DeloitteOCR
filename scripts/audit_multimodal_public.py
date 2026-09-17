"""Read-only model probe over existing public development outputs, never a holdout score."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from PIL import Image
from ocr_workbench.multimodal_contract import build_targets
from ocr_workbench.multimodal_runtime import load_config, ReviewSession


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.limit <= 32:
        raise ValueError('limit must be 1..32')
    args.output.mkdir(parents=True, exist_ok=False)
    config = load_config(args.bundle)
    rows = []
    for index, item in enumerate(json.loads(args.inputs.read_text('utf-8'))):
        source_image, source_result = Path(item['image']), Path(item['result'])
        original = json.loads(source_result.read_text('utf-8'))['original']
        edit = deepcopy({key: original[key] for key in ('text', 'tables')})
        with Image.open(source_image) as image:
            width, height = image.size
        version = {'id': original.get('project_image_version', 'public-probe'), 'width': width, 'height': height,
                   'sha256': digest(source_image)}
        targets, offset = [], 0
        for line in edit['text'].splitlines(keepends=True):
            value = line.rstrip('\r\n')
            if value.strip():
                target = {'kind': 'text', 'start': offset, 'end': offset+len(value)}
                try:
                    targets.extend(build_targets(edit, original, version, 'target', target))
                except ValueError:
                    pass  # Table source markup is not a plain-text target.
            offset += len(line)
            if len(targets) == args.limit:
                break
        row = {'id': item['id'], 'source_url': item.get('source_url'), 'image_sha256': version['sha256'],
               'source_result_sha256': digest(source_result), 'selected_by': 'first non-empty eligible text lines',
               'targets': targets}
        started = time.perf_counter()
        folder = args.output / f'page-{index+1:02d}'
        try:
            snapshot = {'targets': targets, 'image_sha256': version['sha256'], 'scope': 'bounded public text probe'}
            with ReviewSession(args.bundle, config, folder) as session:
                row['response'] = session.review(source_image, snapshot)
            row['status'] = 'succeeded'
        except Exception as error:
            row['status'], row['error'] = 'failed', str(error)
        row['wall_seconds'] = time.perf_counter()-started
        row['source_unchanged'] = digest(source_image) == version['sha256'] and digest(source_result) == row['source_result_sha256']
        rows.append(row)
        print(json.dumps({'id': row['id'], 'status': row['status'], 'targets': len(targets),
                          'wall_seconds': row['wall_seconds']}, ensure_ascii=False), flush=True)
        (args.output / 'receipt.json').write_text(json.dumps({
            'scope': 'Existing public development material; bounded workflow probe, no independent truth or accuracy claim; no proposals adopted',
            'configuration': config, 'rows': rows}, ensure_ascii=False, indent=2), 'utf-8')
    if any(row['status'] != 'succeeded' or not row['source_unchanged'] for row in rows):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
