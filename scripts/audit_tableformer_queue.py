"""Exercise real provider selection, queue completion and CPU cache reuse."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1] / 'src')]
from geometry_eval_common import write_json
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image
from ocr_workbench.geometry import enqueue_geometry, geometry_view, bind_manual
from ocr_workbench.task_queue import TaskQueue


def main():
    p = argparse.ArgumentParser()
    for name in ('bundle', 'dataset', 'inference', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise ValueError('Use a fresh queue audit directory')
    key = 'camelot-foo-p1'
    context = json.loads((args.inference / 'context' / key / 'geometry.json').read_text('utf-8'))
    adopted = json.loads((args.inference / 'adopted' / key / 'result.json').read_text('utf-8'))
    store = Store(args.output / 'workspace')
    project = store.project('Public PDF geometry integration')
    image = add_image(store, project['id'], 'public-pdf-page.png', args.dataset / 'pages' / key / 'input.png')
    task_id = store.enqueue(project['id'], [image['active_version']], ['ppocr'])[0]
    store.claim()
    store.complete(task_id, {'engine': 'ppocr', 'text': '\n'.join(b['text'] for b in context['ocr_blocks']),
                            'tables': [], 'blocks': context['ocr_blocks']})
    task_id = store.enqueue(project['id'], [image['active_version']], ['paddlevl'])[0]
    store.claim(); store.complete(task_id, adopted)
    result_id = store.one('tasks', task_id)['result_id']
    before = store.result(result_id)
    request = enqueue_geometry(store, result_id, 0, provider='tableformer-raw', algorithm='local-v3')
    queue = TaskQueue(store, args.bundle)
    try:
        queue.step()
    finally:
        queue.unload()
    task = store.one('tasks', request['task_id'])
    if task['status'] != 'succeeded':
        raise ValueError(task.get('error') or task['phase'])
    assert store.result(result_id) == before, 'Geometry changed the adopted text'
    first = geometry_view(store, result_id)
    assert any(e['details']['level'] == 'cell' for e in first['evidence']), 'No complete cell evidence'
    changed = deepcopy(before['edited'])
    changed['tables'][0]['cells'][-1]['text'] += ' revised'
    store.save(result_id, changed, 0)
    request = enqueue_geometry(store, result_id, 1, provider='tableformer-raw', algorithm='local-v3')
    def no_gpu(*args):
        raise AssertionError('Verified candidate replay unexpectedly loaded a GPU worker')
    replay = TaskQueue(store, args.bundle, adapter_factory=no_gpu)
    replay.step()
    assert store.one('tasks', request['task_id'])['status'] == 'succeeded'
    rows = geometry_view(store, result_id)['evidence']
    assert all(e['details']['candidate_cache_hit'] for e in rows)
    target = next(e['target'] for e in rows if e['details']['level'] == 'cell')
    poly = next(e['polygon'] for e in rows if e['target'] == target)
    bind_manual(store, result_id, {'revision': 1, 'version_id': image['active_version'], 'target': target, 'polygon': poly})
    assert geometry_view(store, result_id, target)['evidence'][0]['source'] == 'manual'
    write_json(args.output / 'receipt.json', {'status': 'pass', 'sample': key, 'provider': 'tableformer-raw',
        'algorithm': 'local-v3', 'initial_complete_cells': sum(e['details']['level'] == 'cell' for e in first['evidence']),
        'text_unchanged_by_geometry': True, 'candidate_cache_replay_without_gpu': True, 'manual_binding_precedes_model': True,
        'result_id': result_id, 'version_id': image['active_version']})
    print('Real queue / candidate cache / manual binding: pass', flush=True)


if __name__ == '__main__':
    main()
