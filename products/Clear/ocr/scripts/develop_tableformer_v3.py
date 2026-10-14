"""V3 development replay and boundary-contract tools. Historical runs are read-only.

Replay accepts only inference manifests; scoring is a separate command. Exposed
test inputs are rejected. A new test requires its own pre-inference selection seal.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import time
from datetime import datetime, timezone

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1] / 'src')]
from geometry_eval_common import ROOT, ensure_lock, file_lock, sha, verify_inputs, verified_marker, write_json, mapping_code_lock
from compare_tableformer_geometry import checked_artifact, bind_prediction, IncompleteInput, ProviderFailure, snapshot, experiment_code, model_lock
from ocr_workbench.table_matching import local_mapping, policy_for_algorithm
from ocr_workbench.geometry_labels import derive_grid_sample, validate_annotations


def code_lock():
    return file_lock([*(ROOT / 'src/ocr_workbench').rglob('*.py'), Path(__file__),
        ROOT / 'scripts/report_geometry_v2.py', ROOT / 'scripts/geometry_reference_identity.py',
        ROOT / 'scripts/geometry_eval_common.py', ROOT / 'scripts/compare_tableformer_geometry.py',
        ROOT / 'scripts/freeze_tableformer_v3.py', ROOT / 'scripts/audit_geometry_label_contract.py',
        ROOT / 'scripts/evaluate_public_pdf_geometry.py', ROOT / 'scripts/prepare_public_pdf_geometry.py',
        ROOT / 'scripts/run_tableformer_acceptance.py',
        *(ROOT / 'config').glob('geometry-*.json')])


def check_test(manifest, seal, manifest_path=None):
    if manifest.get('split') != 'test':
        return None
    if not seal:
        raise ValueError('New test requires a pre-inference seal; exposed tests cannot be reused')
    sealed = json.loads(seal.read_text('utf-8'))
    if sealed.get('extended_code') != code_lock() or sealed.get('code') != mapping_code_lock() or sealed.get('sample_ids') != [s['id'] for s in manifest['samples']]:
        raise ValueError('Test selection seal mismatch')
    if manifest_path and (sealed.get('test_manifest_sha256') != sha(manifest_path)
                          or sealed.get('test_split_lock_sha256') != sha(manifest_path.parent.parent / 'split-lock.json')):
        raise ValueError('Test inputs changed after selection')
    if not sealed.get('before_inference') or sealed.get('boundary_convention') != 'full_grid':
        raise ValueError('Boundary convention must be sealed before test inference')
    historical_ids = set()
    for dataset in ('table-matching-v2', 'tableformer-expanded-20260914'):
        for path in (ROOT / 'build' / dataset / 'dataset/canonical').glob('*.json'):
            historical_ids.add(path.stem.split('_table_')[0])
    if any(s['original_document_id'] in historical_ids for s in manifest['samples']):
        raise ValueError('Previously prepared document is not an independent test')
    return sha(seal)


def replay(args):
    manifest = verify_inputs(args.manifest)
    seal = check_test(manifest, args.test_seal, args.manifest)
    roots = {'geometry': args.candidates / 'geometry', 'rapidtable': args.candidates / 'rapidtable',
             'tableformer': args.tableformer, 'ppocr': args.candidates / 'ppocr'}
    files = {}
    for provider, root in roots.items():
        name = 'geometry.json' if provider == 'geometry' else 'result.json' if provider == 'ppocr' else 'prediction.json'
        if json.loads((root / 'run-lock.json').read_text('utf-8'))['manifest_sha256'] != sha(args.manifest):
            raise IncompleteInput('Provider manifest mismatch')
        files.update(snapshot(root, manifest['samples'], name))
    matrix = {'TableFormer-local-v2': ['tableformer', policy_for_algorithm('local-v2')],
              'TableFormer-local-v3': ['tableformer', policy_for_algorithm('local-v3')],
              'Paddle-local-v2': ['geometry', policy_for_algorithm('local-v2')],
              'RapidTable-local-v2': ['rapidtable', policy_for_algorithm('local-v2')]}
    if args.adopted_results:
        files.update(snapshot(args.adopted_results, manifest['samples'], 'result.json'))
    lock_hash = ensure_lock(args.output / 'run-lock.json', {'version': 3, 'manifest_sha256': sha(args.manifest),
        'code': code_lock(), 'matrix': matrix, 'artifacts': files, 'annotations_read': False,
        'test_seal_sha256': seal, 'track': 'real-result' if args.adopted_results else 'control'})
    methods = list(matrix)
    for n, sample in enumerate(manifest['samples']):
        for name in methods[n % len(methods):] + methods[:n % len(methods)]:
            provider, policy = matrix[name]
            folder = args.output / name / sample['id']
            if verified_marker(folder, lock_hash, ['mapping.json']):
                continue
            started = time.perf_counter()
            edit = None
            try:
                ocr, _, ocr_sha = checked_artifact(roots['ppocr'], sample, 'result.json')
                artifact = 'geometry.json' if provider == 'geometry' else 'prediction.json'
                prediction, marker, candidate_sha = checked_artifact(roots[provider], sample, artifact)
                prediction = bind_prediction(prediction, ocr['blocks'], sample, provider)
                if args.adopted_results:
                    adopted, _, _ = checked_artifact(args.adopted_results, sample, 'result.json')
                    edit = {'text': adopted['text'], 'tables': adopted['tables']}
                else:
                    edit = sample['fixed_edit']
                mapping_started = time.perf_counter()
                mappings = local_mapping(edit, prediction, sample['width'], sample['height'], policy=policy,
                    result_id=sample['id'], revision=0, image_version=sample['sha256'], ocr_blocks=ocr['blocks'])
                result = {'status': 'success', 'mappings': mappings, 'mapping_seconds': time.perf_counter() - mapping_started,
                    'inference_seconds': prediction.get('inference_seconds'), 'candidate_sha256': candidate_sha,
                    'shared_ocr_sha256': ocr_sha, 'replay_seconds': time.perf_counter() - started}
            except (ProviderFailure, IncompleteInput, OSError, ValueError, KeyError) as error:
                result = {'status': 'failed', 'mappings': [], 'error': str(error),
                          'incomplete_input': not isinstance(error, ProviderFailure), 'replay_seconds': time.perf_counter() - started}
            if args.adopted_results:
                result['adopted_edit'] = edit or {'text': '', 'tables': []}
            write_json(folder / 'mapping.json', result)
            write_json(folder / 'evaluation.json', {'status': result['status'], 'run_lock_sha256': lock_hash,
                'artifact_sha256': {'mapping.json': sha(folder / 'mapping.json')}})
        if (n + 1) % 10 == 0:
            print(n + 1, len(manifest['samples']), 'replayed', flush=True)


def labels(args):
    manifest = verify_inputs(args.manifest)
    check_test(manifest, args.test_seal, args.manifest)
    if args.output.exists():
        raise ValueError('Formal labels are immutable; use a new output directory')
    original = json.loads(args.annotations.read_text('utf-8'))
    source_lock = json.loads((args.manifest.parent.parent / 'split-lock.json').read_text('utf-8'))
    declared = {k.replace('\\', '/'): v for k, v in source_lock['files'].items()}
    if declared.get(args.annotations.relative_to(args.manifest.parent.parent).as_posix()) != sha(args.annotations):
        raise ValueError('Source annotation hash mismatch')
    samples = [derive_grid_sample(sample, json.loads((args.canonical / (sample['id'] + '.json')).read_text('utf-8')))
               for sample in original['samples']]
    if [s['id'] for s in samples] != [s['id'] for s in manifest['samples']]:
        raise ValueError('Input/label sample identity mismatch')
    annotations = {**original, 'protocol_version': 3, 'boundary_convention': 'full_grid', 'samples': samples}
    validate_annotations(annotations, 'full_grid')
    split = manifest['split']
    label = args.output / 'annotations' / (split + '.annotations.json')
    inp = args.output / 'inputs' / (split + '.json')
    write_json(label, annotations)
    inp.parent.mkdir(parents=True)
    shutil.copyfile(args.manifest, inp)
    for sample in manifest['samples']:
        destination = (inp.parent / sample['image']).resolve()
        if not destination.is_relative_to(args.output.resolve()):
            raise ValueError('Image escapes output dataset')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.manifest.parent / sample['image'], destination)
    write_json(args.output / 'protocol.json', {'source_annotations_sha256': sha(args.annotations),
        'evaluation_contract_sha256': sha(ROOT / 'config/geometry-evaluation-v3.json'), 'boundary_convention': 'full_grid',
        'source_code': file_lock([ROOT / 'src/ocr_workbench/geometry_labels.py']), 'predictions_read': False,
        'canonical_files': file_lock([args.canonical / (s['id'] + '.json') for s in samples])})
    write_json(args.output / 'split-lock.json', {'version': 3, 'files': {
        path.relative_to(args.output).as_posix(): sha(path) for path in (label, inp, args.output / 'protocol.json')}})
    print(split, len(samples), 'formal grid labels', flush=True)


def report(args):
    annotations = json.loads(args.annotations.read_text('utf-8'))
    validate_annotations(annotations, 'full_grid')
    import report_geometry_v2
    sys.argv = ['report_geometry_v2', '--annotations', str(args.annotations), '--replay', str(args.replay), '--output', str(args.output)]
    report_geometry_v2.main()


def seal(args):
    if args.output.exists():
        raise ValueError('Selection seals are immutable')
    if args.test_inference.exists() and any(args.test_inference.iterdir()):
        raise ValueError('Seal must precede every test model inference')
    manifest = verify_inputs(args.manifest)
    if manifest['split'] != 'test' or len(manifest['samples']) < 120:
        raise ValueError('Require a fresh 120-document test manifest')
    protocol = json.loads((args.manifest.parent.parent / 'protocol.json').read_text('utf-8'))
    if protocol.get('boundary_convention') != 'full_grid' or protocol.get('evaluation_contract_sha256') != sha(ROOT / 'config/geometry-evaluation-v3.json'):
        raise ValueError('Full-grid contract was not fixed at dataset preparation')
    reports = {}
    for split, report_path, replay_path, count in (
        ('development', args.development_report, args.development_replay, 80),
        ('validation', args.validation_report, args.validation_replay, 60)):
        report = json.loads(report_path.read_text('utf-8'))
        lock = json.loads((replay_path / 'run-lock.json').read_text('utf-8'))
        if not report['complete'] or report['split'] != split or report['replay_lock_sha256'] != sha(replay_path / 'run-lock.json'):
            raise ValueError('Incomplete or unrelated development/validation report')
        if lock['code'] != code_lock() or any(g['all']['tables'] != count for g in report['groups'].values()):
            raise ValueError('Final code must be evaluated on all development/validation tables')
        reports[split] = {'path': str(report_path.resolve()), 'sha256': sha(report_path)}
    crosscheck = json.loads(args.label_audit.read_text('utf-8'))
    if not crosscheck.get('complete') or crosscheck.get('uses_predictions_or_ocr') is not False:
        raise ValueError('Independent official-label audit must pass')
    result = {'version': 3, 'created_utc': datetime.now(timezone.utc).isoformat(), 'code': mapping_code_lock(),
        'extended_code': code_lock(), 'experiment_code': experiment_code(), 'tableformer': model_lock(),
        'policy': policy_for_algorithm('local-v2'), 'experimental_policy': policy_for_algorithm('local-v3'),
        'test_manifest_sha256': sha(args.manifest), 'test_split_lock_sha256': sha(args.manifest.parent.parent / 'split-lock.json'),
        'sample_ids': [s['id'] for s in manifest['samples']], 'before_inference': True,
        'boundary_convention': 'full_grid', 'reports': reports, 'official_label_audit_sha256': sha(args.label_audit),
        'test_labels_examined_for_selection': False, 'production_default': False,
        'adoption_rule': {'engine': 'paddlevl', 'selection': 'only successful PaddleOCR-VL text/tables, no per-sample engine selection', 'revision': 0}}
    # Check exclusions BEFORE publishing a valid-looking seal.
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as directory:
        temporary = Path(directory) / 'seal.json'
        write_json(temporary, result)
        check_test(manifest, temporary, args.manifest)
    write_json(args.output, result)
    print(args.output, flush=True)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('replay')
    for name in ('manifest', 'candidates', 'tableformer', 'output'):
        run.add_argument('--' + name, type=Path, required=True)
    run.add_argument('--adopted-results', type=Path)
    run.add_argument('--test-seal', type=Path)
    lab = commands.add_parser('labels')
    for name in ('manifest', 'annotations', 'canonical', 'output'):
        lab.add_argument('--' + name, type=Path, required=True)
    lab.add_argument('--test-seal', type=Path)
    rep = commands.add_parser('report')
    for name in ('annotations', 'replay', 'output'):
        rep.add_argument('--' + name, type=Path, required=True)
    sealed = commands.add_parser('seal')
    for name in ('manifest', 'test-inference', 'development-report', 'validation-report',
                 'development-replay', 'validation-replay', 'label-audit', 'output'):
        sealed.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    {'replay': replay, 'labels': labels, 'report': report, 'seal': seal}[args.command](args)


if __name__ == '__main__':
    main()
