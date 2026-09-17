"""Isolated CPU/backend audit probes; no user workspace or product edits."""
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))

from PIL import Image
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image
from ocr_workbench.documents import Documents
from ocr_workbench.service import create_app
from ocr_workbench.task_queue import TaskQueue
from ocr_workbench.page_processing import complete_region_task, finalize_waiting_pages

PDF_BUNDLE = ROOT / 'build/document-workflow/bundle'
PDF_FIXTURE = ROOT / 'build/document-workflow/fixtures/native.pdf'


def photo(store, project, folder, n):
    path = folder / f'probe-{n}.png'
    Image.new('RGB', (80, 60), 'white').save(path)
    return add_image(store, project['id'], path.name, path)


def pending_render_drops_process(folder):
    store = Store(folder / 'workspace')
    project = store.project('isolated render/process probe')
    manager = Documents(store, PDF_BUNDLE, review_only=True)
    doc = manager.import_document(project['id'], 'native.pdf', PDF_FIXTURE, dpi=72)
    page = store.document_pages(doc['id'])[0]
    render = store.enqueue_document_stage(page['id'], 'render', {'dpi': 72})
    process = manager.process(page['id'], 'native')
    first_worked = manager.step()
    second_worked = manager.step()
    stages = store.rows('SELECT id,kind,status,parameters FROM document_stages')
    return {
        'render_id': render, 'requested_process_id': process,
        'same_id': render == process, 'first_step_worked': first_worked,
        'second_step_worked': second_worked, 'stages': stages,
        'result_count': len(store.rows('SELECT id FROM results')),
        'image_count': len(store.rows('SELECT id FROM images')),
        'expected': 'The native process request is either queued after render, or explicitly rejected; success must not silently drop it.',
    }


def search_false_negatives(folder):
    bundle = folder / 'bundle'
    (bundle / 'config').mkdir(parents=True)
    (bundle / 'config/engines.json').write_text(json.dumps({'ppocr': {'name': 'probe', 'models': []}}), encoding='utf-8')
    app = create_app(bundle, folder / 'workspace', 'isolated-probe', start_queue=False)
    store = app.state.store
    project = store.project('isolated search probe')
    image = photo(store, project, folder, 0)
    task = store.enqueue(project['id'], [image['active_version']], ['ppocr'])[0]
    store.claim()
    text = '合同写明 "甲方" 应付款，路径 C:\\财务\\报表，收款人 ÉLODIE，参考 STRASSE 与 Straße。'
    store.complete(task, {'text': text, 'tables': [], 'blocks': []})
    search = next(route.endpoint for route in app.routes if route.path == '/api/documents/{key}/search')
    queries = ['甲方', '"甲方"', 'C:\\财务\\报表', 'élodie', 'strasse']
    return {'stored_text': text, 'queries': [
        {'q': query, 'literal_casefold_present': query.casefold() in text.casefold(),
         'actual_total': search(image['id'], query)['total']}
        for query in queries
    ]}


def completed_page_starved_by_paused_prefix(folder):
    store = Store(folder / 'workspace')
    project = store.project('isolated 51-page queue probe')
    queue = TaskQueue(store, ROOT)
    manager = Documents(store, ROOT, queue)
    stage_ids = []
    for n in range(51):
        image = photo(store, project, folder, n)
        stage = manager.process(image['id'], 'ocr')
        assert manager.step()
        stage_ids.append(stage)
    tasks = store.rows('SELECT i.stage_id,t.id,t.status FROM page_ocr_inputs i JOIN tasks t ON t.id=i.task_id ORDER BY t.created,t.id')
    earlier = [row['id'] for row in tasks if row['stage_id'] != stage_ids[-1]]
    queue.action(project['id'], 'pause', earlier)
    last_task = store.claim()
    complete_region_task(store, last_task, {'text': 'READY LAST PAGE', 'tables': [], 'blocks': [], 'engine': 'ppocr'})
    snapshots = []
    for attempt in range(3):
        finalize_waiting_pages(manager)
        snapshots.append({'iteration': attempt + 1, 'last_stage_status': store.one('document_stages', stage_ids[-1])['status'],
                          'published_results': len(store.rows('SELECT id FROM results'))})
    # Free one early slot and show that the already-completed final page publishes.
    queue.action(project['id'], 'resume', [earlier[0]])
    prior = store.claim()
    complete_region_task(store, prior, {'text': 'READY FIRST PAGE', 'tables': [], 'blocks': [], 'engine': 'ppocr'})
    finalize_waiting_pages(manager)
    finalize_waiting_pages(manager)
    return {'stages': len(stage_ids), 'paused_earlier_region_tasks': len(earlier),
            'last_region_status_before_finalization': store.one('tasks', last_task['id'])['status'],
            'attempts_before_unblocking': snapshots,
            'last_stage_status_after_freeing_one_prefix_slot': store.one('document_stages', stage_ids[-1])['status']}


def main():
    output = {}
    probes = (pending_render_drops_process, search_false_negatives, completed_page_starved_by_paused_prefix)
    for probe in probes:
        with tempfile.TemporaryDirectory(prefix='ocr-independent-backend-review-') as temporary:
            output[probe.__name__] = probe(Path(temporary))
    evidence = Path(__file__).with_name('reproduction-results.json')
    evidence.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
