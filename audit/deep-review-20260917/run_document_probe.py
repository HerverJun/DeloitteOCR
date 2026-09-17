"""Reuse isolated document fixtures, substituting this audit's browser probes only."""
import json
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = Path('D:/OCR-multimodal-workbench-20260917/bundle')
SCRIPT, OUTPUT = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
original_run = subprocess.run


def run(args, *positional, **kwargs):
    if len(args) > 1 and str(args[1]).endswith('audit_document_ui.mjs'):
        from ocr_workbench.store import Store
        store = __import__('ocr_workbench.store', fromlist=['Store']).Store(OUTPUT / 'workspace')
        seed = json.loads((OUTPUT / 'seed.json').read_text('utf-8'))
        pages = store.document_pages(seed['document'])
        page = pages[1]
        photo = store.one('images', page['image_id'])
        task = store.enqueue(seed['project'], [photo['active_version']], ['ppocr'])[0]
        assert store.claim()['id'] == task
        store.complete(task, {'engine': 'ppocr', 'text': 'Page two navigation probe', 'tables': [], 'blocks': [],
                              'project_image_version': photo['active_version'], 'image': {'width': 900, 'height': 600}})
        seed['probe_pages'] = pages
        seed['probe_page2_result'] = store.one('tasks', task)['result_id']
        seed['probe_page2_name'] = photo['name']
        if SCRIPT.name == 'extended-ui.mjs':
            seed_review = runpy.run_path(str(ROOT / 'audit/deep-review-20260917/review-features/seed_multimodal_helper.py'))['seed_pending_multimodal']
            adopted_b = store.rows('SELECT result_id FROM selections WHERE image_id=?', (photo['id'],))[0]['result_id']
            seed['probe_page2_adopted_result'] = adopted_b
            seed['probe_page2_multimodal'] = seed_review(store, adopted_b)
        (OUTPUT / 'seed.json').write_text(json.dumps(seed, ensure_ascii=False, indent=2), 'utf-8')
        return original_run(['node', str(SCRIPT), str(OUTPUT)], *positional, **kwargs)
    return original_run(args, *positional, **kwargs)


sys.argv = ['audit_document_ui.py', '--bundle', str(BUNDLE), '--geometry-v2', '--output', str(OUTPUT)]
with patch.object(subprocess, 'run', side_effect=run):
    runpy.run_path(str(ROOT / 'scripts/audit_document_ui.py'), run_name='__main__')
