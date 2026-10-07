"""Collect real resident PP-DocLayout/PP-OCR context without reference inputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import digest, fpath, gpu_sample, isolate_runtime, source_hash, write


def verify_context_record(folder, sample, *, normalize=None):
    """Verify immutable source/evidence identity before consuming or resuming."""
    folder = fpath(folder)
    record = json.loads((folder / 'record.json').read_text('utf-8'))
    if record.get('id') != sample['id'] or record.get('image_sha256') != sample['image_sha256']:
        raise ValueError('Context input identity differs')
    if record.get('status') != 'success':
        raise ValueError(f"Context is not successful: {record.get('status')}: {record.get('error')}")
    for name in ('context', 'evidence'):
        path = folder / f'{name}.json'
        if not path.is_file() or digest(path) != record[f'{name}_sha256']:
            raise ValueError(f'Saved {name} changed')
    context = json.loads((folder / 'context.json').read_text('utf-8'))
    evidence = json.loads((folder / 'evidence.json').read_text('utf-8'))
    if context.get('image_version') != sample['image_sha256']:
        raise ValueError('Context image version differs')
    if context.get('status') != 'success' or context.get('coordinate_space') != 'exif_normalized_original':
        raise ValueError('Unsupported or failed context geometry')
    for name in ('width', 'height'):
        if sample.get(name) is not None and context.get('image', {}).get(name) != sample[name]:
            raise ValueError('Context normalized image dimensions differ')
    if evidence.get('provenance', {}).get('contains_reference_input') is not False:
        raise ValueError('Context evidence is not isolated from references')
    if normalize is not None and normalize(context) != evidence:
        raise ValueError('Stored normalized evidence differs from actual context')
    return record, context, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('inputs', 'bundle', 'worker-source', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    for name in ('inputs', 'bundle', 'worker_source', 'output'):
        setattr(args, name, fpath(getattr(args, name)))
    if args.offset < 0 or args.limit is not None and args.limit < 1:
        raise ValueError('Offset must be nonnegative and limit positive')
    args.output.mkdir(parents=True, exist_ok=True)
    isolate_runtime(args.output)
    sys.path.insert(0, str(args.worker_source))
    from ocr_workbench.adapter import EngineAdapter
    from ocr_workbench.screen_evidence import collect_screen_context, normalize_screen_context

    inputs = json.loads(args.inputs.read_text('utf-8'))
    if inputs.get('truth_available_to_inference') is not False:
        raise ValueError('Only isolated model-input manifest permitted')
    samples = inputs['samples'][args.offset:args.offset + args.limit if args.limit else None]
    if len({s['id'] for s in samples}) != len(samples):
        raise ValueError('Duplicate sample IDs')
    for sample in samples:
        if digest(fpath(sample['image'])) != sample['image_sha256']:
            raise ValueError('Input hash mismatch')
        if Path(sample['id']).name != sample['id'] or sample['id'] in ('', '.', '..'):
            raise ValueError('Unsafe sample ID')
    binding = {
        'protocol_version': 1, 'stage': 'screen-context', 'engine': 'geometry',
        'geometry_provider': 'context', 'inputs_sha256': digest(args.inputs),
        'worker_source_sha256': source_hash(args.worker_source),
        'engine_config_sha256': digest(args.bundle / 'config/engines.json'),
        'runner_sha256': digest(__file__), 'bundle': str(args.bundle),
        'offset': args.offset, 'limit': args.limit,
        'labels_or_ideal_regions_provided': False,
        'request': {'include_all_layout': True, 'allow_auxiliary_ocr': True},
        'models': ['PP-DocLayoutV3', 'PP-OCRv6 medium'],
    }
    binding_path = args.output / 'binding.json'
    if binding_path.exists() and json.loads(binding_path.read_text('utf-8')) != binding:
        raise ValueError('Context source/config/input changed; use a new output directory')
    if not binding_path.exists():
        write(binding_path, binding)
    adapter = None
    started = time.perf_counter()
    loads, gpu = [], []
    completed = resumed = 0
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            gpu.append(gpu_sample())
            stop.wait(2)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        for sample in samples:
            folder = args.output / sample['id']
            record_path = folder / 'record.json'
            old = json.loads(record_path.read_text('utf-8')) if record_path.exists() else None
            if old and old.get('image_sha256') != sample['image_sha256']:
                raise ValueError('Resume context input identity differs')
            if old and (old.get('status') == 'success' or old.get('status') == 'failed' and not args.retry_failed):
                if old['status'] == 'success':
                    verify_context_record(folder, sample, normalize=normalize_screen_context)
                completed += 1
                resumed += 1
                continue
            number = 1 + len(list(folder.glob('attempt-*'))) if folder.exists() else 1
            attempt = folder / f'attempt-{number:04d}'
            attempt.mkdir(parents=True)
            record = {'id': sample['id'], 'image_sha256': sample['image_sha256'],
                      'status': 'running', 'attempt': number, 'output': str(attempt)}
            write(record_path, record)
            begin = time.perf_counter()
            try:
                if adapter is None:
                    adapter = EngineAdapter(args.bundle, 'geometry', args.output / 'sessions', worker_source=args.worker_source)
                    adapter.configure_geometry('context')
                    tick = time.perf_counter()
                    adapter.load()
                    loads.append({'session_id': adapter.ipc.name, 'wall_seconds': time.perf_counter()-tick,
                                  'reported_load_seconds': adapter.load_seconds})
                value = collect_screen_context(sample['image'], attempt, adapter,
                                               image_version=sample['image_sha256'], prepare=False)
                context, evidence = value['context'], value['evidence']
                write(folder / 'context.json', context)
                write(folder / 'evidence.json', evidence)
                record.update(status='success', context_sha256=digest(folder / 'context.json'),
                              evidence_sha256=digest(folder / 'evidence.json'),
                              engine_session=adapter.ipc.name, inference_seconds=context.get('inference_seconds'),
                              model_load_seconds=context.get('load_seconds'),
                              elapsed_seconds=context.get('elapsed_seconds'),
                              layout_blocks=len(context.get('layout_blocks', [])),
                              text_blocks=len(context.get('ocr_blocks', [])))
                write(record_path, record)
                verify_context_record(folder, sample, normalize=normalize_screen_context)
            except Exception as exc:
                record.update(status='failed', error_type=type(exc).__name__, error=str(exc))
                if adapter is not None:
                    adapter.unload()
                    adapter = None
            finally:
                record['wall_seconds'] = time.perf_counter() - begin
                write(attempt / 'record.json', record)
                write(record_path, record)
                completed += 1
                write(args.output / 'progress.json', {'binding': binding, 'completed': completed,
                      'total': len(samples), 'last': record, 'wall_seconds': time.perf_counter()-started})
                print(json.dumps({'completed': completed, 'total': len(samples), 'id': sample['id'],
                                  'status': record['status'], 'seconds': round(record['wall_seconds'], 3),
                                  'error': record.get('error')}, ensure_ascii=False), flush=True)
    finally:
        if adapter is not None:
            adapter.unload()
        stop.set()
        thread.join(timeout=12)
        write(args.output / 'gpu-samples.json', gpu)
        records = [json.loads((args.output/s['id']/'record.json').read_text('utf-8'))
                   for s in samples if (args.output/s['id']/'record.json').exists()]
        write(args.output / 'run-summary.json', {'binding': binding, 'total': len(samples),
              'completed': sum(r['status'] in {'success', 'failed'} for r in records),
              'success': sum(r['status'] == 'success' for r in records),
              'failed': sum(r['status'] == 'failed' for r in records), 'loads': loads,
              'attempt_wall_seconds': time.perf_counter()-started, 'resumed_records': resumed,
              'cache_resume_separate': bool(resumed),
              'gpu_peak_note': 'Device-wide nvidia-smi includes other applications, not a per-process peak'})


if __name__ == '__main__':
    main()
