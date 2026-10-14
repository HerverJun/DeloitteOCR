"""Run the document harness with explicit preview/adopted-result regressions."""
import argparse
import json
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--bundle', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
output = args.output.resolve()
original_run = subprocess.run


def run(command, *positional, **kwargs):
    if len(command) > 1 and str(command[1]).endswith('audit_document_ui.mjs'):
        from ocr_workbench.store import Store
        store = Store(output / 'workspace')
        seed = json.loads((output / 'seed.json').read_text('utf-8'))
        pages = store.document_pages(seed['document'])
        photo = store.one('images', pages[1]['image_id'])
        task = store.enqueue(seed['project'], [photo['active_version']], ['ppocr'])[0]
        assert store.claim()['id'] == task
        store.complete(task, {'engine': 'ppocr', 'text': 'Page two navigation probe',
            'tables': [], 'blocks': [], 'project_image_version': photo['active_version'],
            'image': {'width': 900, 'height': 600}})
        seed['probe_pages'] = pages
        seed['probe_page2_result'] = store.one('tasks', task)['result_id']
        seed['probe_page2_name'] = photo['name']
        adopted = store.rows('SELECT result_id FROM selections WHERE image_id=?', (photo['id'],))[0]['result_id']
        assert adopted != seed['probe_page2_result'], 'Review navigation requires different preview and adopted results'
        seed['probe_page2_adopted_result'] = adopted
        seed_review = runpy.run_path(str(ROOT / 'audit/deep-review-20260917/review-features/seed_multimodal_helper.py'))['seed_pending_multimodal']
        seed['probe_page2_multimodal'] = seed_review(store, adopted)
        seed['probe_page2_adopted_text'] = store.result(adopted)['edited']['text']
        (output / 'seed.json').write_text(json.dumps(seed, ensure_ascii=False, indent=2), 'utf-8')
        return original_run(['node', str(ROOT / 'scripts/audit_bugfix_ui.mjs'), str(output)], *positional, **kwargs)
    return original_run(command, *positional, **kwargs)


sys.argv = ['audit_document_ui.py', '--bundle', str(args.bundle), '--geometry-v2', '--output', str(output)]
with patch.object(subprocess, 'run', side_effect=run):
    runpy.run_path(str(ROOT / 'scripts/audit_document_ui.py'), run_name='__main__')
