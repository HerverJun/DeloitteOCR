"""Expanded raw TableFormer experiment: inference, identical replay, and test seal.

Inference and replay never read geometry annotations. The reference edit is
shared by all providers in this controlled crop experiment, not a model input.
"""
import argparse
from copy import deepcopy
import importlib.metadata
import json
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geometry_eval_common import (ROOT, ensure_lock, file_lock, mapping_code_lock,
    runtime_info, sha, verified_marker, verify_inputs, verify_test_seal, write_json)

SOURCE = ROOT / 'build/table-matching-v2/n3/source'
MODEL = ROOT / 'build/table-matching-v2/n3'
METHODS = {'Paddle': 'geometry', 'RapidTable': 'rapidtable', 'TableFormer': 'tableformer'}


class IncompleteInput(ValueError):
    """An integrity failure, not an ordinary model failure."""


class ProviderFailure(RuntimeError):
    """Verified model failure; remains in the evaluation denominator."""


def experiment_code():
    return file_lock([Path(__file__), ROOT / 'scripts/freeze_tableformer_holdout.py',
                      ROOT / 'scripts/freeze_geometry_v2.py'])


def model_lock():
    current = {
        'upstream_revision': 'd1569fdffee1d09cb3111d6d5490991cb59d59fd',
        'upstream_code': file_lock((SOURCE / 'docling_ibm_models/tableformer').rglob('*.py')),
        'model_revision': '2199320848bb9a8a519d22e4b528185a4f9a6f64',
        'model_sha256': sha(MODEL / 'tableformer_accurate.safetensors'),
        'config_sha256': sha(MODEL / 'tm_config.json'),
        'disable_post_process': True, 'sort_row_col_indexes': False,
        'source_semantics': 'raw_structure', 'threads': 4, 'seed': 20260913,
    }
    historical = json.loads((MODEL / 'probe-01/run-lock.json').read_text('utf-8'))
    for key in ('upstream_revision', 'upstream_code', 'model_revision', 'model_sha256'):
        if current[key] != historical[key]:
            raise IncompleteInput('TableFormer differs from pinned probe: ' + key)
    config = json.loads((MODEL / 'tm_config.json').read_text('utf-8'))
    config['model']['save_dir'] = str(MODEL.resolve())
    config['predict']['disable_post_process'] = True
    if config != historical['config']:
        raise IncompleteInput('TableFormer configuration differs from pinned probe')
    return current


def verify_expanded_seal(manifest_path, seal_path):
    manifest = verify_inputs(manifest_path)
    base = verify_test_seal(manifest, seal_path)
    if manifest.get('split') != 'test':
        return base
    sealed = json.loads(seal_path.read_text('utf-8'))
    sys.path.insert(0, str(ROOT / 'src'))
    from ocr_workbench.table_matching import default_policy
    expected = {'experiment_code': experiment_code(), 'tableformer': model_lock(),
                'policy': default_policy(), 'test_manifest_sha256': sha(manifest_path),
                'test_split_lock_sha256': sha(manifest_path.parent.parent / 'split-lock.json')}
    for key, value in expected.items():
        if sealed.get(key) != value:
            raise IncompleteInput('Expanded test seal mismatch: ' + key)
    return base


def checked_artifact(root, sample, artifact):
    try:
        lock_path = root / 'run-lock.json'
        marker = verified_marker(root / sample['id'], sha(lock_path), [artifact])
        if marker is None:
            raise IncompleteInput('Missing provider marker')
        if marker['status'] != 'success':
            if marker.get('incomplete_input'):
                raise IncompleteInput(marker.get('error', 'incomplete_provider_input'))
            raise ProviderFailure(marker.get('error', 'provider_inference_failed'))
        path = root / sample['id'] / artifact
        return json.loads(path.read_text('utf-8')), marker, sha(path)
    except (OSError, KeyError, ValueError) as error:
        raise IncompleteInput(str(error)) from error


def bind_prediction(prediction, blocks, sample, provider):
    """Reject foreign OCR/coordinates; replay receives the exact shared blocks."""
    source = 'independent-ppocr:' + sample['sha256']
    if prediction.get('image_version') != sample['sha256'] or prediction.get('ocr_source') != source:
        raise IncompleteInput('Candidate image/OCR provenance mismatch')
    expected = blocks
    if provider == 'geometry':
        # The existing Paddle worker drops empty/no-polygon OCR blocks before
        # passing the shared OCR to its structure recognizer. No new OCR runs.
        expected = [b for b in blocks if b.get('polygon') and b.get('text', '').strip()]
    if prediction.get('ocr_blocks') != expected:
        raise IncompleteInput('Candidate uses different OCR blocks')
    if provider == 'tableformer' and prediction.get('source_semantics') != 'raw_structure':
        raise IncompleteInput('Only raw structure cells are eligible')
    result = deepcopy(prediction)
    result['ocr_blocks'] = deepcopy(blocks)
    return result


def snapshot(root, samples, artifact):
    paths = [root / 'run-lock.json']
    paths += [root / s['id'] / name for s in samples for name in (artifact, 'evaluation.json')
              if (root / s['id'] / name).exists()]
    return file_lock(paths)


def infer(a):
    manifest = verify_inputs(a.manifest)
    seal = verify_expanded_seal(a.manifest, a.test_code_lock)
    pinned = model_lock()
    sys.path[:0] = [str(SOURCE), str(ROOT / 'src')]
    import numpy as np
    import torch
    from PIL import Image
    from docling_ibm_models.tableformer.data_management.tf_predictor import TFPredictor
    from ocr_workbench.coordinates import bounds
    lock = {'version': 2, 'provider': 'tableformer-accurate-raw', 'manifest_sha256': sha(a.manifest),
            'experiment_code': experiment_code(), 'tableformer': pinned,
            'ocr_artifacts': snapshot(a.ocr, manifest['samples'], 'result.json'),
            'dependencies': {m: importlib.metadata.version(m) for m in
                             ['torch', 'torchvision', 'safetensors', 'numpy', 'Pillow']},
            'device': a.device, 'runtime': runtime_info(), 'test_selection_sha256': seal,
            'network': 'denied', 'annotations_read': False, 'correspondence': 'separate CPU replay'}
    lock_hash = ensure_lock(a.output / 'run-lock.json', lock)
    pending = [s for s in manifest['samples'] if verified_marker(a.output / s['id'], lock_hash,
                                                               ['prediction.json', 'upstream.json']) is None]
    if not pending:
        return
    def denied(*args, **kwargs):
        raise RuntimeError('Expanded inference is offline')
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    torch.manual_seed(pinned['seed'])
    torch.set_num_threads(pinned['threads'])
    config = json.loads((MODEL / 'tm_config.json').read_text('utf-8'))
    config['model']['save_dir'] = str(MODEL.resolve())
    config['predict']['disable_post_process'] = True
    started = time.perf_counter()
    engine = TFPredictor(config, device=a.device, num_threads=pinned['threads'])
    engine.enable_post_process = False
    write_json(a.output / 'timing.json', {'model_load_seconds': time.perf_counter() - started,
        'device_name': torch.cuda.get_device_name() if a.device.startswith('cuda') else 'CPU'})
    for n, sample in enumerate(pending):
        folder = a.output / sample['id']
        started = time.perf_counter()
        try:
            ocr, _, ocr_sha = checked_artifact(a.ocr, sample, 'result.json')
            blocks = ocr['blocks']
            with Image.open(a.manifest.parent / sample['image']) as im:
                image = np.asarray(im.convert('RGB'))
            page = {'image': image, 'width': sample['width'], 'height': sample['height'],
                    'tokens': [{'id': i, 'text': b['text'], 'bbox': bounds(b['polygon'])}
                               for i, b in enumerate(blocks)]}
            output = engine.multi_table_predict(page, [[0, 0, sample['width'], sample['height']]],
                                                do_matching=True, sort_row_col_indexes=False)[0]
            elapsed = time.perf_counter() - started
            prediction = {'tf_table_cells': deepcopy(output['predict_details']['table_cells']),
                'source_semantics': 'raw_structure', 'model_sha256': pinned['model_sha256'],
                'image_version': sample['sha256'], 'ocr_blocks': blocks,
                'ocr_source': 'independent-ppocr:' + sample['sha256'],
                'shared_ocr_sha256': ocr_sha, 'inference_seconds': elapsed}
            write_json(folder / 'prediction.json', prediction)
            write_json(folder / 'upstream.json', output)
            result = {'status': 'success', 'inference_seconds': elapsed,
                      'artifact_sha256': {name: sha(folder / name) for name in ('prediction.json', 'upstream.json')}}
        except Exception as error:
            import traceback
            result = {'status': 'failed', 'error': str(error), 'traceback': traceback.format_exc(),
                      'incomplete_input': isinstance(error, IncompleteInput), 'artifact_sha256': {}}
        result.update(run_lock_sha256=lock_hash, seconds=time.perf_counter() - started)
        write_json(folder / 'evaluation.json', result)
        print(n + 1, len(pending), sample['id'], result['status'], round(result['seconds'], 3),
              result.get('error', ''), flush=True)


def replay(a):
    manifest = verify_inputs(a.manifest)
    seal = verify_expanded_seal(a.manifest, a.test_code_lock)
    sys.path.insert(0, str(ROOT / 'src'))
    from ocr_workbench.table_matching import local_mapping, default_policy
    policy = default_policy()
    roots = {p: a.candidates / p for p in ('geometry', 'rapidtable', 'ppocr')}
    roots['tableformer'] = a.tableformer
    artifacts = {}
    for provider, root in roots.items():
        artifact = 'geometry.json' if provider == 'geometry' else 'result.json' if provider == 'ppocr' else 'prediction.json'
        artifacts.update(snapshot(root, manifest['samples'], artifact))
        provider_lock = json.loads((root / 'run-lock.json').read_text('utf-8'))
        if provider_lock['manifest_sha256'] != sha(a.manifest):
            raise IncompleteInput('Provider manifest mismatch: ' + provider)
    lock = {'version': 2, 'manifest_sha256': sha(a.manifest), 'code': mapping_code_lock(),
            'experiment_code': experiment_code(), 'policy': policy, 'candidate_artifacts': artifacts,
            'matrix': METHODS, 'track': 'control', 'annotations_read': False,
            'runtime': runtime_info(), 'test_selection_sha256': seal,
            'matching_input': 'identical fixed edit, PP-OCR blocks, image_version, result_id, policy',
            'timing_order': 'per sample, rotating provider order; CPU correspondence only'}
    lock_hash = ensure_lock(a.output / 'run-lock.json', lock)
    for index, sample in enumerate(manifest['samples']):
        methods = list(METHODS)
        methods = methods[index % 3:] + methods[:index % 3]
        for name in methods:
            provider = METHODS[name]
            folder = a.output / name / sample['id']
            if verified_marker(folder, lock_hash, ['mapping.json']) is not None:
                continue
            started = time.perf_counter()
            try:
                ocr, _, ocr_sha = checked_artifact(roots['ppocr'], sample, 'result.json')
                artifact = 'geometry.json' if provider == 'geometry' else 'prediction.json'
                prediction, marker, candidate_sha = checked_artifact(roots[provider], sample, artifact)
                prediction = bind_prediction(prediction, ocr['blocks'], sample, provider)
                match_started = time.perf_counter()
                mappings = local_mapping(deepcopy(sample['fixed_edit']), prediction, sample['width'], sample['height'],
                                         policy=deepcopy(policy), result_id=sample['id'], image_version=sample['sha256'])
                match_seconds = time.perf_counter() - match_started
                mapping = {'status': 'success', 'mappings': mappings,
                           'correspondence_seconds': match_seconds, 'shared_ocr_sha256': ocr_sha,
                           'candidate_sha256': candidate_sha,
                           'inference_seconds': prediction.get('inference_seconds', marker.get('inference_seconds'))}
            except Exception as error:
                mapping = {'status': 'failed', 'mappings': [], 'error': str(error),
                           'incomplete_input': not isinstance(error, ProviderFailure)}
            mapping['mapping_seconds'] = time.perf_counter() - started
            write_json(folder / 'mapping.json', mapping)
            write_json(folder / 'evaluation.json', {'status': mapping['status'], 'run_lock_sha256': lock_hash,
                                                    'artifact_sha256': {'mapping.json': sha(folder / 'mapping.json')}})
        if (index + 1) % 10 == 0:
            print(index + 1, len(manifest['samples']), 'all providers replayed', flush=True)


def seal(a):
    if a.output.exists():
        raise ValueError('Selection lock is immutable')
    sys.path.insert(0, str(ROOT / 'src'))
    from ocr_workbench.table_matching import default_policy
    code, policy, reports = mapping_code_lock(), default_policy(), {}
    for split, report_path, replay_path, expected_n in (
        ('development', a.development_report, a.development_replay, 80),
        ('validation', a.validation_report, a.validation_replay, 60)):
        report = json.loads(report_path.read_text('utf-8'))
        lock_path = replay_path / 'run-lock.json'
        lock = json.loads(lock_path.read_text('utf-8'))
        if not report['complete'] or report['split'] != split or report['replay_lock_sha256'] != sha(lock_path):
            raise IncompleteInput('Expanded report incomplete or unrelated')
        if lock['code'] != code or lock['policy'] != policy or lock['experiment_code'] != experiment_code():
            raise IncompleteInput('Final implementation was not evaluated')
        if set(report['groups']) != set(METHODS) or any(g['all']['tables'] != expected_n for g in report['groups'].values()):
            raise IncompleteInput('Require full development/validation for all three methods')
        reports[split] = {'path': str(report_path.resolve()), 'sha256': sha(report_path),
                          'results': {m: g['all'] for m, g in report['groups'].items()}}
    manifest = verify_inputs(a.manifest)
    split_lock = a.manifest.parent.parent / 'split-lock.json'
    dataset = json.loads(split_lock.read_text('utf-8'))
    if manifest['split'] != 'test' or len(manifest['samples']) != 120 or not dataset.get('new_test'):
        raise IncompleteInput('Require newly frozen 120-document holdout')
    write_json(a.output, {'version': 2, 'experiment': 'TableFormer expanded raw geometry comparison',
        'code': code, 'experiment_code': experiment_code(), 'tableformer': model_lock(), 'policy': policy,
        'test_manifest_sha256': sha(a.manifest), 'test_split_lock_sha256': sha(split_lock),
        'selection': 'Accurate raw structure; no postprocessing or row/column compression; fixed v2 matcher for all providers',
        'reports': reports, 'test_labels_examined_for_selection': False, 'production_default': False,
        'adoption_rule': 'Control experiment only; no default integration or model vote',
        'thresholds': {'precise_coverage': .9, 'wrong_cell_rate_max': .01, 'offered_quality': .95},
        'created_utc': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
    print(a.output, flush=True)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('infer', 'replay', 'seal', 'verify-seal'):
        q = sub.add_parser(name)
        q.add_argument('--manifest', type=Path, required=True)
        if name != 'seal':
            q.add_argument('--test-code-lock', type=Path)
        if name != 'verify-seal':
            q.add_argument('--output', type=Path, required=True)
        if name == 'infer':
            q.add_argument('--ocr', type=Path, required=True)
            q.add_argument('--device', default='cuda:0')
        if name == 'replay':
            q.add_argument('--candidates', type=Path, required=True)
            q.add_argument('--tableformer', type=Path, required=True)
        if name == 'seal':
            for arg in ('development-report', 'validation-report', 'development-replay', 'validation-replay'):
                q.add_argument('--' + arg, type=Path, required=True)
    a = p.parse_args()
    if a.command == 'verify-seal':
        print(verify_expanded_seal(a.manifest, a.test_code_lock))
    else:
        {'infer': infer, 'replay': replay, 'seal': seal}[a.command](a)


if __name__ == '__main__':
    main()
