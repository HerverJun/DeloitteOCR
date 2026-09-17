"""Real local GPU functional checks on explicitly synthetic OCR fixtures."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from PIL import Image, ImageDraw, ImageFont
from ocr_workbench.coordinates import box_polygon
from ocr_workbench.editing import tables_html
from ocr_workbench.geometry import bind_manual
from ocr_workbench.imaging import add_image
from ocr_workbench.multimodal_export import build_review_report
from ocr_workbench.multimodal_runtime import load_config, review_readiness
from ocr_workbench.multimodal_store import enqueue_review, view_review, decide_review
from ocr_workbench.store import Store
from ocr_workbench.tables import parse_tables
from ocr_workbench.task_queue import TaskQueue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cancel-on-request', action='store_true', help='Cancel a real running model after its first HTTP request is saved')
    parser.add_argument('--no-local-geometry', action='store_true', help='Exercise the full-page evidence fallback')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    config = load_config(args.bundle)
    ready = review_readiness(args.bundle, config, verify_hashes=True)
    (out / 'readiness.json').write_text(json.dumps(ready, ensure_ascii=False, indent=2), 'utf-8')
    if not ready['ready']:
        raise RuntimeError(ready['reason'])
    store = Store(out / 'workspace')
    project = store.project('合成功能实验：不用于准确率验收')
    page = Image.new('RGB', (1320, 1000), 'white')
    draw = ImageDraw.Draw(page)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 36)
    title_font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 46)
    truth_lines = ['离线视觉审校功能实验', '记录编号：AB-00128', '备注：按原文保留小数与符号。']
    ocr_lines = [truth_lines[0], '记录编号：AB-O0128', truth_lines[2]]
    blocks = []
    for index, line in enumerate(truth_lines):
        y = 40 + index * 92
        draw.text((60, y), line, font=title_font if index == 0 else font, fill='black')
        blocks.append({'text': ocr_lines[index], 'kind': 'text', 'polygon': box_polygon([50, y, 1250, y+66])})
    values = [['项目', '记录值'], ['数量', '00075'], ['调整金额', '-18.50']]
    wrong = deepcopy(values)
    wrong[1][1] = 'O0075'
    wrong[2][1] = '-18.5O'
    table = {'rows': len(values), 'columns': 2, 'cells': []}
    for row, cells in enumerate(values):
        for column, text in enumerate(cells):
            box = [60+column*600, 340+row*145, 60+(column+1)*600, 340+(row+1)*145]
            draw.rectangle(box, outline='black', width=3)
            draw.text((box[0]+25, box[1]+43), text, font=font, fill='black')
            table['cells'].append({'row': row, 'column': column, 'row_span': 1, 'column_span': 1,
                                   'text': wrong[row][column]})
    draw.text((60, 840), '合成材料，仅验证操作与本地推理。', font=font, fill='#777777')
    source = out / 'synthetic-input.png'
    page.save(source)
    image = add_image(store, project['id'], source.name, source)
    version = image['active_version']
    original_text = '\n'.join(ocr_lines)+'\n'+tables_html([table])
    table['source'] = parse_tables(original_text)[0]['source']
    original = {'engine': 'ppocr', 'text': original_text, 'tables': [table], 'blocks': blocks,
                'image': {'width': page.width, 'height': page.height}, 'project_image_version': version}
    if args.no_local_geometry:
        original['blocks'] = []
    task = store.enqueue(project['id'], [version], ['ppocr'])[0]
    store.claim()
    store.complete(task, original)
    result_id = store.one('tasks', task)['result_id']
    for row in range(3):
        for column in range(2):
            if args.no_local_geometry:
                continue
            bind_manual(store, result_id, {'revision': 0, 'version_id': version,
                'target': {'kind': 'cell', 'table': 0, 'row': row, 'column': column},
                'polygon': box_polygon([60+column*600, 340+row*145, 60+(column+1)*600, 340+(row+1)*145])})
    before = store.result(result_id)
    body = {'request_id': 'real-gpu-functional-page-v1', 'revision': 0, 'version_id': version,
            'model_id': config['profile_id'], 'scope': 'page'}
    review_id = enqueue_review(store, result_id, body, config)
    queue = TaskQueue(store, args.bundle)
    samples, stop = [], threading.Event()
    def monitor():
        while not stop.is_set():
            try:
                value = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                                                text=True, creationflags=subprocess.CREATE_NO_WINDOW).strip()
                samples.append({'seconds': time.perf_counter()-started, 'used_mib': int(value.splitlines()[0])})
            except (OSError, ValueError, subprocess.CalledProcessError):
                pass
            stop.wait(.5)
    started = time.perf_counter()
    monitor_thread = threading.Thread(target=monitor)
    monitor_thread.start()
    cancel_seconds = None
    try:
        if args.cancel_on_request:
            worker = threading.Thread(target=queue.step)
            worker.start()
            deadline = time.monotonic()+90
            while not list((store.root / 'task-results' / review_id).glob('*/batch-0001/request.json')):
                if not worker.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError('Real model did not reach its first request')
                time.sleep(.02)
            cancelled = time.perf_counter()
            queue.action(project['id'], 'cancel', [review_id])
            worker.join(timeout=15)
            cancel_seconds = time.perf_counter()-cancelled
            if worker.is_alive():
                raise RuntimeError('Real cancellation did not release the worker')
        else:
            queue.step()
    finally:
        queue.stop()
        stop.set()
        monitor_thread.join(timeout=10)
    elapsed = time.perf_counter()-started
    task_state = store.one('tasks', review_id)
    review = view_review(store, result_id)
    report = {'scope': 'Synthetic functional experiment; manually bound cells, seeded OCR mistakes; not independent accuracy or A4000 validation',
        'task': task_state, 'wall_seconds': elapsed, 'gpu_samples': samples,
        'cancel_seconds': cancel_seconds, 'no_local_geometry': args.no_local_geometry,
        'peak_device_memory_mib': max((s['used_mib'] for s in samples), default=None),
        'original_and_edited_unchanged_after_inference': store.result(result_id) == before,
        'truth_lines': truth_lines, 'truth_table': values, 'view': review}
    if task_state['status'] == 'succeeded':
        for proposal in review['proposals']:
            if proposal['decision'] == 'replace':
                current = store.result(result_id)
                decide_review(store, result_id, proposal['id'], {'action': 'accept', 'revision': current['revision'],
                    'version_id': version, 'request_id': 'smoke-accept-'+proposal['id']})
        report['adopted'] = store.result(result_id)
        report['original_preserved_after_decisions'] = report['adopted']['original'] == before['original']
        for format in ('json', 'md', 'xlsx'):
            path = build_review_report(store, result_id, format)
            shutil.copy2(path, out / path.name)
            shutil.rmtree(path.parent)
        if report['adopted']['revision']:
            store.history(result_id, -1, report['adopted']['revision'])
            report['after_undo'] = view_review(store, result_id)
    (out / 'receipt.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'status': task_state['status'], 'error': task_state['error'], 'wall_seconds': elapsed,
        'peak_device_memory_mib': report['peak_device_memory_mib'],
        'decisions': [{'before': p['before'], 'after': p['after'], 'decision': p['decision']} for p in review['proposals']]},
        ensure_ascii=False), flush=True)
    expected_status = 'cancelled' if args.cancel_on_request else 'succeeded'
    if task_state['status'] != expected_status or args.cancel_on_request and review['proposals']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
