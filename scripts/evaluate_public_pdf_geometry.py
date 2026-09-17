"""Real PDF-page development: shared OCR, actual adoption, candidate providers, replay.

No reference structure or geometry labels enter inference. Model failures remain
in receipts; missing/tampered artifacts are integrity failures, never success.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1] / 'src')]
from geometry_eval_common import ROOT, ensure_lock, file_lock, sha, verify_inputs, verified_marker, write_json
from ocr_workbench.adapter import EngineAdapter
from ocr_workbench.geometry_contract import fingerprint
from ocr_workbench.geometry_provider_config import provider_identity
from ocr_workbench.table_matching import local_mapping, policy_for_algorithm


def artifact(root, sample, name):
    receipt = verified_marker(root / sample['id'], sha(root / 'run-lock.json'), [name])
    if not receipt:
        raise ValueError('Missing input receipt: ' + str(root / sample['id']))
    if receipt['status'] != 'success':
        raise RuntimeError('Prior stage failed: ' + receipt.get('error', 'unknown'))
    return json.loads((root / sample['id'] / name).read_text('utf-8'))


def infer(args):
    manifest = verify_inputs(args.manifest)
    if manifest['split'] != 'development':
        raise ValueError('Public PDFs are development data, never a new sealed test')
    phases = ['context', 'adopted', 'paddle', 'tableformer-raw', 'rapidtable'] if args.phase == 'all' else [args.phase]
    for phase in phases:
        folder = args.output / phase
        inputs = []
        if phase not in ('context', 'adopted'):
            inputs = [args.output / 'context/run-lock.json']
            inputs += [p for s in manifest['samples'] for p in (args.output / 'context' / s['id']).glob('*.json')]
        lock = {'version': 3, 'phase': phase, 'manifest_sha256': sha(args.manifest),
            'code': file_lock([*(ROOT / 'src/ocr_workbench').rglob('*.py'), Path(__file__)]),
            'provider': provider_identity(phase) if phase in ('paddle', 'tableformer-raw', 'rapidtable') else None,
            'model_lock_sha256': sha(args.bundle / 'config/model-lock.json'), 'input_artifacts': file_lock(inputs),
            'annotations_read': False, 'reference_edits_read': False, 'adoption_rule': 'PaddleOCR-VL only, no sample-level selection'}
        lock_hash = ensure_lock(folder / 'run-lock.json', lock)
        name = 'result.json' if phase == 'adopted' else 'geometry.json'
        names = [name, 'upstream.json'] if phase in ('tableformer-raw', 'rapidtable') else [name]
        pending = [s for s in manifest['samples'] if not verified_marker(folder / s['id'], lock_hash, names)]
        if not pending:
            continue
        adapter = None
        loads = []
        try:
            for sample in pending:
                started = time.perf_counter()
                output = (folder / sample['id']).resolve()
                output.mkdir(parents=True, exist_ok=True)
                try:
                    request = {'image_version': sample['sha256'], 'image_sha256': sample['sha256'], 'ocr_blocks': [], 'regions': []}
                    if phase not in ('context', 'adopted'):
                        context = artifact(args.output / 'context', sample, 'geometry.json')
                        request.update({k: context[k] for k in ('ocr_blocks', 'ocr_source', 'regions')})
                        request['allow_auxiliary_ocr'] = False
                    if adapter is None:
                        adapter = EngineAdapter(args.bundle, 'paddlevl' if phase == 'adopted' else 'geometry',
                                                folder / 'sessions', worker_source=ROOT / 'src')
                        if phase != 'adopted':
                            adapter.configure_geometry(phase)
                        load_started = time.perf_counter()
                        adapter.load()
                        loads.append(time.perf_counter() - load_started)
                    result = adapter.recognize((args.manifest.parent / sample['image']).resolve(), output,
                                               request if phase != 'adopted' else None)
                    receipt = {'status': 'success', 'artifact_sha256': {n: sha(output / n) for n in names},
                        'inference_seconds': result.get('inference_seconds'),
                        'shared_ocr_sha256': fingerprint(result.get('ocr_blocks', [])),
                        'candidate_tables': len(result.get('candidate_tables', result.get('tables', [])))}
                except Exception as error:
                    receipt = {'status': 'failed', 'error': str(error), 'artifact_sha256': {}}
                    if adapter:
                        adapter.unload()
                        adapter = None
                receipt.update(seconds=time.perf_counter() - started, run_lock_sha256=lock_hash)
                write_json(output / 'evaluation.json', receipt)
                print(phase, sample['id'], receipt['status'], round(receipt['seconds'], 2), receipt.get('error', ''), flush=True)
        finally:
            if adapter:
                adapter.unload()
        write_json(folder / 'timing.json', {'worker_load_seconds': loads})


def replay(args):
    manifest = verify_inputs(args.manifest)
    lock = {'version': 3, 'manifest_sha256': sha(args.manifest),
        'code': file_lock([*(ROOT / 'src/ocr_workbench').rglob('*.py'), Path(__file__)]),
        'input_artifacts': file_lock([p for phase in ('context', 'adopted', 'paddle', 'tableformer-raw', 'rapidtable')
            for p in (args.output / phase).rglob('*.json') if 'sessions' not in p.parts]),
        'policies': {a: policy_for_algorithm(a) for a in ('local-v2', 'local-v3')}, 'annotations_read': False}
    root = args.output / args.replay_name
    lock_hash = ensure_lock(root / 'run-lock.json', lock)
    summary = []
    for sample in manifest['samples']:
        for provider, algorithm in (('paddle', 'local-v2'), ('rapidtable', 'local-v2'),
                                     ('tableformer-raw', 'local-v2'), ('tableformer-raw', 'local-v3')):
            output = root / (provider + '-' + algorithm) / sample['id']
            if verified_marker(output, lock_hash, ['mapping.json']):
                summary.append(json.loads((output / 'summary.json').read_text('utf-8')))
                continue
            row = {'id': sample['id'], 'provider': provider, 'algorithm': algorithm, 'quality_acceptance': False}
            try:
                adopted = artifact(args.output / 'adopted', sample, 'result.json')
                prediction = artifact(args.output / provider, sample, 'geometry.json')
                context = artifact(args.output / 'context', sample, 'geometry.json')
                expected = context['ocr_blocks']
                if provider == 'paddle':
                    if any(b not in expected for b in prediction['ocr_blocks']):
                        raise ValueError('Foreign Paddle OCR token')
                elif prediction['ocr_blocks'] != expected:
                    raise ValueError('Shared OCR differs by provider')
                edit = {'text': adopted['text'], 'tables': adopted['tables']}
                started = time.perf_counter()
                mappings = local_mapping(edit, prediction, sample['width'], sample['height'],
                    policy=policy_for_algorithm(algorithm), result_id=sample['id'], image_version=sample['sha256'],
                    ocr_blocks=expected)
                row['mapping_ms'] = (time.perf_counter() - started) * 1000
                cells = [m for m in mappings if m['target']['kind'] == 'cell']
                row.update(status='success', adopted_tables=len(edit['tables']), adopted_cells=len(cells),
                    offered_cells=sum(m['level'] == 'cell' for m in cells),
                    failure_stages=dict(Counter(m['primary_failure_stage'] for m in cells)),
                    shared_ocr_sha256=fingerprint(expected))
                result = {'status': 'success', 'adopted_edit': edit, 'mappings': mappings}
            except Exception as error:
                row.update(status='failed', error=str(error))
                result = {'status': 'failed', 'mappings': [], 'error': str(error)}
            write_json(output / 'mapping.json', result)
            write_json(output / 'summary.json', row)
            write_json(output / 'evaluation.json', {'status': result['status'], 'run_lock_sha256': lock_hash,
                'artifact_sha256': {n: sha(output / n) for n in ('mapping.json', 'summary.json')}})
            summary.append(row)
    write_json(root / 'summary.json', {'quality_acceptance': False, 'independent_cell_annotations': False, 'rows': summary})
    print('Public PDF replay', len(summary), 'receipts', flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['infer', 'replay'])
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--bundle', type=Path)
    p.add_argument('--phase', choices=['all', 'context', 'adopted', 'paddle', 'tableformer-raw', 'rapidtable'], default='all')
    p.add_argument('--replay-name', default='replay')
    args = p.parse_args()
    {'infer': infer, 'replay': replay}[args.command](args)


if __name__ == '__main__':
    main()
