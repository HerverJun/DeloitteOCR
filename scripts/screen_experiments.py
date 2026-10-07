"""Finite offline screen-OCR route experiments; inference receives no reference data.

Inputs are a JSON list (or {"items": [...]}) of {"id", "image", "result"?}.
``result`` is an optional saved actual OCR candidate, never annotated truth.
All routes save requests, untouched model output, timing and failure evidence.
No output from this script is adopted into a user's result or declared accurate.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from PIL import Image
from ocr_workbench.adapter import EngineAdapter
from ocr_workbench.atomic_files import write_json
from ocr_workbench.geometry_providers import candidate_tables, paddle_tables
from ocr_workbench.image_utils import normalized_rgb
from ocr_workbench.multimodal_runtime import load_config, ReviewSession
from ocr_workbench.screen_evidence import (
    build_screen_visual_snapshot, collect_screen_context, merge_local_text_evidence,
    normalize_screen_context, review_screen_visual_proposals,
)
from ocr_workbench.screen_imaging import prepare_screen_image


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def f_path(value):
    path = Path(value).resolve()
    if path.drive.upper() != 'F:':
        raise ValueError('实验输入、运行包、源码与所有产物必须位于本机 F 盘：' + str(path))
    return path


def plain(value):
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if hasattr(value, 'tolist'):
        return value.tolist()
    return value


def set_write_environment(output):
    paths = {name: output / 'environment' / folder for name, folder in (
        ('TEMP', 'temp'), ('TMP', 'temp'), ('APPDATA', 'appdata'),
        ('LOCALAPPDATA', 'localappdata'), ('HF_HOME', 'hf'),
        ('HUGGINGFACE_HUB_CACHE', 'hf-hub'), ('XDG_CACHE_HOME', 'cache'),
        ('HF_HUB_CACHE', 'hf-hub'), ('TRANSFORMERS_CACHE', 'transformers'),
        ('TORCH_HOME', 'torch'), ('PADDLE_HOME', 'paddle'),
        ('TORCH_EXTENSIONS_DIR', 'torch-extensions'), ('CUDA_CACHE_PATH', 'cuda'),
        ('NUMBA_CACHE_DIR', 'numba'), ('TRITON_CACHE_DIR', 'triton'),
        ('PADDLE_PDX_CACHE_HOME', 'paddlex'), ('PADDLEX_HOME', 'paddlex'), ('MPLCONFIGDIR', 'matplotlib'))}
    for name, path in paths.items():
        f_path(path).mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    os.environ.update(PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
                      TRANSFORMERS_OFFLINE='1', PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True')


def load_inputs(path, limit):
    value = json.loads(f_path(path).read_text('utf-8'))
    items = value.get('items', value.get('samples')) if isinstance(value, dict) else value
    if not isinstance(items, list) or not items:
        raise ValueError('inputs 必须为非空列表或 items 列表')
    selected, ids = [], set()
    for index, item in enumerate(items[:limit]):
        if not isinstance(item, dict) or not isinstance(item.get('image'), str):
            raise ValueError('每项须提供实际 image 文件路径')
        ident = str(item.get('id', index + 1))
        if ident in ids:
            raise ValueError('输入 id 重复')
        ids.add(ident)
        image = f_path(item['image'])
        if not image.is_file():
            raise ValueError('图片不存在：' + str(image))
        image_hash = digest(image)
        if item.get('image_sha256') and item['image_sha256'] != image_hash:
            raise ValueError('实际图片与输入清单 hash 不一致')
        candidate = f_path(item['result']) if item.get('result') else None
        if candidate and not candidate.is_file():
            raise ValueError('保存的实际 OCR 候选不存在：' + str(candidate))
        # This whitelist deliberately drops any benchmark labels/ideal ROIs.
        selected.append({'id': ident, 'image': str(image), 'image_sha256': image_hash,
                         'result': str(candidate) if candidate else None,
                         'result_sha256': digest(candidate) if candidate else None,
                         'result_image_sha256': item.get('result_image_sha256')})
    return selected


def actual_candidate(path, expected_image_hash, declared_image_hash=None):
    if not path:
        return None
    value = json.loads(Path(path).read_text('utf-8'))
    raw = value.get('original', value.get('raw', value))
    if not isinstance(raw, dict):
        raise ValueError('保存候选须为 OCR 结果对象')
    actual_image_hash = raw.get('image_sha256') or raw.get('screen', {}).get('input_sha256') or declared_image_hash
    if actual_image_hash != expected_image_hash:
        return None  # Unbound/stale candidate geometry cannot locate visual targets.
    # Never send whole saved metadata to inference, only observed OCR output.
    return {key: deepcopy(raw[key]) for key in ('tables', 'text', 'blocks', 'warnings') if key in raw}


def independent_token_snapshot(evidence, image_path, max_targets):
    targets, originals = [], []
    for block in evidence.get('text', []):
        if block.get('kind') in {'ruler', 'toolbar', 'taskbar', 'ui'} or not block.get('text', '').strip():
            continue
        # Select observed numeric tokens first; the model never sees their OCR text.
        if not any(char.isdigit() for char in block['text']):
            continue
        ident = 'observed-token:' + str(len(targets))
        targets.append({'id': ident, 'before': '',
            'context': '逐字读取标记文字范围的实际可见内容，保留符号、小数点及前导零；不清楚时返回未知。',
            'evidence': {'level': 'text', 'polygon': block['polygon'], 'range_semantics': 'observed_text_extent'}})
        originals.append({'id': ident, 'before': block['text'], 'box': block['box'],
                          'issue_code': 'bounded_observed_numeric_token_probe', 'geometry_level': 'text_extent'})
        if len(targets) >= max_targets:
            break
    return {'image_sha256': digest(image_path), 'targets': targets, 'source_targets': originals,
            'skipped': [], 'human_review': 'not_performed', 'scope': 'actual OCR token extents; no full-cell claim'}


def source_identity(source_root):
    names = ('adapter.py', 'engine_host.py', 'geometry_candidate_worker.py',
             'table_geometry_worker.py', 'geometry_provider_config.py', 'geometry_providers.py')
    return {'source_root': str(source_root), 'files': {
        name: digest(source_root / 'src/ocr_workbench' / name) for name in names}}


def load_source_module(name, path):
    path = f_path(path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verified_saved_context(args, row, folder):
    base = f_path(args.context_run)
    source = (base / row['id']).resolve()
    if not source.is_relative_to(base):
        raise ValueError('context id 路径越界')
    verifier_file = f_path(args.context_verifier) / 'context.py'
    verifier = load_source_module('screen_experiment_context_verifier', verifier_file)
    frozen_normalize = normalize_screen_context
    if args.context_normalizer:
        frozen_normalize = load_source_module('screen_experiment_frozen_normalizer', args.context_normalizer).normalize_screen_context
    with Image.open(row['normalized_image']) as image:
        sample = {'id':row['id'],'image_sha256':row['image_sha256'],'width':image.width,'height':image.height}
    record, context, stored_evidence = verifier.verify_context_record(source, sample, normalize=frozen_normalize)
    # Re-derive the current policy's evidence from actual raw context after verifying
    # the frozen evidence. A new policy never rewrites/rebinds the saved baseline.
    evidence = normalize_screen_context(context)
    folder.mkdir(parents=True)
    write_json(folder/'verified-context-provenance.json',{'record':record,'source_folder':str(source),
        'verifier_sha256':digest(verifier_file),'frozen_normalizer_sha256':digest(args.context_normalizer) if args.context_normalizer else None,
        'stored_evidence_sha256':record['evidence_sha256'],'stored_evidence_reused':False},durable=True)
    write_json(folder/'context.json',context,durable=True)
    write_json(folder/'screen-context-evidence.json',evidence,durable=True)
    return {'context':context,'evidence':evidence,'preparation':prepare_screen_image(
        row['normalized_image'],folder/'prepared',evidence,tile_size=args.tile_size,max_tiles=args.max_tiles)}


def run(args, *, adapter_factory=EngineAdapter, review_factory=ReviewSession):
    if not 1 <= args.limit <= 8 or not 1 <= args.max_tiles <= 12 or not 1 <= args.max_targets <= 16:
        raise ValueError('limit 1..8, max_tiles 1..12, max_targets 1..16')
    if not 30 <= args.budget_seconds <= 7200 or not 5 <= args.task_timeout <= 600:
        raise ValueError('实验总预算 30..7200 秒，单次请求 5..600 秒')
    if not 256 <= args.tile_size <= 1600:
        raise ValueError('tile_size 必须为 256..1600')
    bundle, output = f_path(args.bundle), f_path(args.output)
    source_root = f_path(args.geometry_source_root or ROOT)
    rows = load_inputs(args.inputs, args.limit)
    output.mkdir(parents=True, exist_ok=False)
    set_write_environment(output)
    started, cancelled = time.monotonic(), threading.Event()
    timer = threading.Timer(args.budget_seconds, cancelled.set)
    timer.daemon = True
    timer.start()
    receipt = {'version': 'screen-experiments-v1', 'scope':
        'Actual-image bounded development route measurement; no annotated truth/ideal geometry supplied to inference; proposals only; no OCR accuracy claim',
        'mode': args.mode, 'dry_run': args.dry_run, 'limits': {
            'images': args.limit, 'tiles_per_image': args.max_tiles, 'targets_per_image': args.max_targets,
            'tile_size':args.tile_size, 'budget_seconds': args.budget_seconds, 'task_timeout': args.task_timeout},
        'inference_inputs': 'image pixels, actual model-detected regions and actual OCR tokens only',
        'input_manifest_sha256':digest(args.inputs), 'driver_sha256':digest(__file__),
        'engine_configuration_sha256':digest(bundle/'config/engines.json'),
        'geometry_worker': source_identity(source_root), 'rows': rows, 'stages': [], 'sessions':[]}
    for index, row in enumerate(rows):
        row['directory'] = f'image-{index+1:02d}'
        folder = output / row['directory']
        folder.mkdir()
        with Image.open(row['image']) as image:
            normalized_rgb(image).save(folder / 'normalized.png', format='PNG')
        row['normalized_image'] = str(folder / 'normalized.png')
        row['normalized_sha256'] = digest(row['normalized_image'])
    def checkpoint():
        receipt['wall_seconds'] = time.monotonic() - started
        receipt['budget_expired'] = cancelled.is_set()
        write_json(output / 'receipt.json', plain(receipt), durable=True)
    def remaining():
        return max(0., args.budget_seconds - (time.monotonic()-started))
    def stage(name, row, invoke):
        if cancelled.is_set():
            return None
        record = {'stage': name, 'id': row['id']}
        tick = time.perf_counter()
        try:
            result = invoke()
            record['status'] = 'succeeded'
        except Exception as error:
            result = None
            record.update(status='failed', error=type(error).__name__ + ': ' + str(error))
        record['wall_seconds'] = time.perf_counter() - tick
        receipt['stages'].append(record)
        checkpoint()
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return result
    def adapter(engine, provider=None):
        obj = adapter_factory(bundle, engine, output / 'sessions')
        obj.cancelled = cancelled
        obj.recognition_timeout = max(1, int(min(args.task_timeout, remaining())))
        if provider:
            obj.configure_geometry(provider)
            # Geometry dependencies may remain in a reviewed F-drive source tree.
            # Read source/model assets there, keep IPC/cache/output in this run.
            if provider != 'context' and args.geometry_source_root:
                obj.worker_source = source_root / 'src'
        tick = time.perf_counter()
        obj.load()
        receipt['sessions'].append({'engine':engine,'provider':provider,'load_wall_seconds':time.perf_counter()-tick,
            'model_load_seconds':getattr(obj,'load_seconds',None)})
        return obj
    contexts = {}
    try:
        checkpoint()
        if args.dry_run:
            return receipt
        # Context always precedes later routes. Frozen actual context can be reused
        # only after its per-image identity and both artifacts have been verified.
        obj = None
        try:
            if not args.context_run:
                obj = adapter('geometry', 'context')
            for row in rows:
                folder = output / row['directory'] / 'context'
                def capture(row=row, folder=folder):
                    if args.context_run:
                        return verified_saved_context(args,row,folder)
                    result = collect_screen_context(row['normalized_image'], folder, obj,
                        image_version=row['normalized_sha256'], prepare=False)
                    result['preparation'] = prepare_screen_image(row['normalized_image'],folder/'prepared',
                        result['evidence'],tile_size=args.tile_size,max_tiles=args.max_tiles)
                    return result
                captured = stage('context-reuse' if args.context_run else 'context', row, capture)
                if captured:
                    contexts[row['id']] = captured
        except Exception as error:
            receipt['stages'].append({'stage':'context-load', 'status':'failed', 'error':str(error)})
        finally:
            if obj:
                obj.unload()
        if args.mode in {'tiles', 'all'} and contexts:
            obj = None
            try:
                obj = adapter('ppocr')
                for row in rows:
                    capture = contexts.get(row['id'])
                    if not capture:
                        continue
                    tiles = [p for p in capture['preparation']['prepared_images'] if p['kind'] == 'tile'][:args.max_tiles]
                    results = []
                    if not tiles:
                        receipt['stages'].append({'stage':'tile-ppocr','id':row['id'],'status':'skipped',
                            'reason':'no bounded complete tile collection from actual business region',
                            'preparation_warnings':capture['preparation'].get('warnings',[])})
                    for tile in tiles:
                        folder = output / row['directory'] / 'tiles' / tile['id']
                        folder.mkdir(parents=True, exist_ok=True)
                        def recognize(tile=tile, folder=folder):
                            obj.recognition_timeout = max(1, int(min(args.task_timeout, remaining())))
                            raw = obj.recognize(tile['path'], folder)
                            write_json(folder / 'observed.json', raw, durable=True)
                            return {'candidate': tile, 'context': {'status':'success', 'image':{'width':tile['width'],'height':tile['height']},
                                'ocr_blocks':raw.get('blocks',[]), 'ocr_source':'local-ppocr', 'image_version':row['normalized_sha256']}}
                        observed = stage('tile-ppocr', row, recognize)
                        if observed:
                            results.append(observed)
                    merged = merge_local_text_evidence(results, width=capture['preparation']['width'],
                                                       height=capture['preparation']['height'])
                    merged['selected_tiles'], merged['available_tiles'] = len(tiles), len([p for p in capture['preparation']['prepared_images'] if p['kind']=='tile'])
                    merged['bounded_subset'] = merged['available_tiles'] > len(tiles)
                    if merged['bounded_subset']:
                        merged['complete'] = False
                    write_json(output / row['directory'] / 'local-text-evidence.json', merged, durable=True)
            except Exception as error:
                receipt['stages'].append({'stage':'tiles-load', 'status':'failed', 'error':str(error)})
            finally:
                if obj:
                    obj.unload()
        if args.mode in {'geometry', 'all'} and contexts:
            for provider in args.providers.split(','):
                if provider not in {'paddle', 'tableformer-raw', 'rapidtable'}:
                    raise ValueError('未知 geometry provider：' + provider)
                obj = None
                try:
                    obj = adapter('geometry', provider)
                    for row in rows:
                        capture = contexts.get(row['id'])
                        if not capture:
                            continue
                        regions = capture['context'].get('regions', [])[:4]
                        if not regions:
                            receipt['stages'].append({'stage':'geometry-'+provider,'id':row['id'],
                                'status':'skipped','reason':'actual context detected no table region'})
                            continue
                        request = {'regions':regions, 'ocr_blocks':capture['context'].get('ocr_blocks',[]),
                            'ocr_source':capture['context'].get('ocr_source'), 'allow_auxiliary_ocr':False,
                            'image_version':row['normalized_sha256'], 'image_sha256':row['normalized_sha256']}
                        folder = output / row['directory'] / ('geometry-'+provider)
                        folder.mkdir()
                        write_json(folder / 'actual-input-request.json', request, durable=True)
                        def recognize(folder=folder, request=request):
                            obj.recognition_timeout = max(1, int(min(args.task_timeout, remaining())))
                            raw = obj.recognize(row['normalized_image'], folder, geometry_request=request)
                            write_json(folder / 'observed.json', raw, durable=True)
                            width,height = capture['preparation']['width'],capture['preparation']['height']
                            adapted = paddle_tables(raw,width,height) if provider=='paddle' else candidate_tables(raw,width,height)
                            write_json(folder / 'adapted-geometry.json', plain(adapted), durable=True)
                            return raw
                        stage('geometry-'+provider, row, recognize)
                except Exception as error:
                    receipt['stages'].append({'stage':'geometry-load-'+provider, 'status':'failed', 'error':str(error)})
                finally:
                    if obj:
                        obj.unload()
        if args.mode in {'visual', 'all'} and contexts and not cancelled.is_set():
            config = load_config(bundle, 'qwen35-4b-q4')
            write_json(output/'visual-profile.json',config,durable=True)
            # The caller-owned timer bounds this route without mutating locked config.
            with review_factory(bundle, config, output / 'visual-session', cancelled) as session:
                for row in rows:
                    capture = contexts.get(row['id'])
                    if not capture:
                        continue
                    raw = actual_candidate(row['result'],row['image_sha256'],row['result_image_sha256'])
                    snapshot = None
                    if raw:
                        issues = [{'table':ti,'row':c['row'],'column':c['column'],'code':'bounded_actual_cell_probe'}
                            for ti,t in enumerate(raw.get('tables',[])) for c in t.get('cells',[])]
                        snapshot = build_screen_visual_snapshot(raw,capture['preparation'],issues,
                            row['normalized_image'],max_targets=args.max_targets)
                    if not snapshot or not snapshot['targets']:
                        snapshot = independent_token_snapshot(capture['evidence'],row['normalized_image'],args.max_targets)
                    folder = output / row['directory'] / 'visual'
                    folder.mkdir()
                    write_json(folder / 'source-snapshot.json',snapshot,durable=True)
                    if not snapshot['targets']:
                        receipt['stages'].append({'stage':'visual','id':row['id'],'status':'skipped','reason':'no reliable actual cell or numeric token target'})
                        continue
                    session.output = folder
                    def review():
                        proposals = review_screen_visual_proposals(session,row['normalized_image'],snapshot)
                        write_json(folder / 'proposals-only.json',proposals,durable=True)
                        return proposals
                    stage('visual-qwen35-4b',row,review)
    finally:
        timer.cancel()
        for row in rows:
            row['source_unchanged'] = digest(row['image']) == row['image_sha256'] and (
                not row['result'] or digest(row['result']) == row['result_sha256'])
        checkpoint()
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--geometry-source-root', type=Path)
    parser.add_argument('--context-run', type=Path)
    parser.add_argument('--context-verifier', type=Path, default=Path('F:/OCR/scripts/screen_benchmark'))
    parser.add_argument('--context-normalizer', type=Path, help='Pinned screen_evidence.py used by the saved context run')
    parser.add_argument('--mode', choices=('context','tiles','geometry','visual','all'), default='context')
    parser.add_argument('--providers', default='rapidtable,tableformer-raw,paddle')
    parser.add_argument('--limit', type=int, default=4)
    parser.add_argument('--max-tiles', type=int, default=4)
    parser.add_argument('--max-targets', type=int, default=8)
    parser.add_argument('--tile-size', type=int, default=768)
    parser.add_argument('--budget-seconds', type=int, default=1800)
    parser.add_argument('--task-timeout', type=int, default=180)
    parser.add_argument('--dry-run', action='store_true')
    receipt = run(parser.parse_args())
    if receipt.get('budget_expired') or any(stage['status']=='failed' for stage in receipt['stages']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
