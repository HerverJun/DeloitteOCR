"""Publish compact v3 evidence without copying public document contents."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geometry_eval_common import sha, write_json, file_lock


def percentiles(values):
    import numpy as np
    return {'n': len(values), **{name: float(np.percentile(values, p)) if values else None
        for name, p in (('median', 50), ('p95', 95))}}


def inference_summary(folder, expected_ids, evidence):
    """Check original receipts and outputs, including retained failed attempts."""
    lock = folder / 'run-lock.json'
    lock_sha = sha(lock)
    markers = sorted(folder.glob('*/evaluation.json'))
    if {p.parent.name for p in markers} != set(expected_ids):
        raise ValueError('Incomplete inference receipts: ' + str(folder))
    evidence.append(lock)
    rows = []
    for marker in markers:
        row = json.loads(marker.read_text('utf-8'))
        if row['run_lock_sha256'] != lock_sha:
            raise ValueError('Inference lock mismatch: ' + str(marker))
        for name, expected in row['artifact_sha256'].items():
            if sha(marker.parent / name) != expected:
                raise ValueError('Inference artifact mismatch: ' + str(marker.parent / name))
        evidence.append(marker)
        rows.append(row)
    timing = folder / 'timing.json'
    evidence.append(timing)
    return {'items': len(rows), 'statuses': dict(Counter(r['status'] for r in rows)),
        'receipt_wall_seconds': percentiles([r['seconds'] for r in rows if r.get('seconds') is not None]),
        'provider_inference_seconds': percentiles([r['inference_seconds'] for r in rows
                                                 if r.get('inference_seconds') is not None]),
        'load_timing': json.loads(timing.read_text('utf-8')),
        'timing_scope': 'Original provider receipt boundaries; cold load is separate. Not document end-to-end time.',
        'artifact_hashes_verified': True}


def paired_delta(report):
    import numpy as np
    before = report['rows']['TableFormer-local-v2']
    after = report['rows']['TableFormer-local-v3']
    assert [(r['sample_id'], r['target']) for r in before] == [(r['sample_id'], r['target']) for r in after]
    counts, documents = Counter(), defaultdict(lambda: [0, 0])
    for old, new in zip(before, after):
        counts['new_correct'] += int(bool(new.get('correct')) and not old.get('correct'))
        counts['lost_correct'] += int(bool(old.get('correct')) and not new.get('correct'))
        counts['new_wrong'] += int(bool(new.get('wrong_cell')) and not old.get('wrong_cell'))
        counts['resolved_wrong'] += int(bool(old.get('wrong_cell')) and not new.get('wrong_cell'))
        documents[new['group_id']][0] += int(bool(new.get('correct'))) - int(bool(old.get('correct')))
        documents[new['group_id']][1] += 1
    data = np.asarray(list(documents.values()), dtype=float)
    rng = np.random.default_rng(20260915)
    samples = data[rng.integers(0, len(data), size=(2000, len(data)))].sum(axis=1)
    return {**counts, 'coverage_delta': float(data[:, 0].sum() / data[:, 1].sum()),
            'paired_document_bootstrap_95pct': np.percentile(samples[:, 0] / samples[:, 1], [2.5, 97.5]).tolist()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Compact evidence is immutable; use a fresh output directory')
    result = {'version': 3, 'production_default': False, 'boundary_convention': 'full_grid', 'reports': {}}
    inputs = []
    seal = json.loads((args.root / 'selection-seal.json').read_text('utf-8'))
    for section in ('code', 'extended_code', 'experiment_code'):
        for path, expected in seal[section].items():
            if sha(path) != expected:
                raise ValueError('Sealed implementation changed: ' + path)
    result['sealed_implementation_verified'] = True
    for name in ('development-final', 'validation-final', 'test-control', 'test-real-result'):
        path = args.root / 'reports' / (name + '.json')
        report = json.loads(path.read_text('utf-8'))
        if not report['complete']:
            raise ValueError('Incomplete report: ' + name)
        inputs.append(path)
        result['reports'][name] = {'sha256': sha(path), 'track': report['track'], 'complete': True,
            'groups': {method: {group: groups[group] for group in ('all', 'target:merged', 'target:empty', 'target:repeated')}
                       for method, groups in report['groups'].items()}, 'paired_delta': paired_delta(report)}
    public_path = args.root / 'public-inference-02/replay/summary.json'
    public = json.loads(public_path.read_text('utf-8'))
    inputs.append(public_path)
    groups = defaultdict(Counter)
    for row in public['rows']:
        group = groups[row['provider'] + '-' + row['algorithm']]
        group['pages'] += 1
        group['success'] += int(row['status'] == 'success')
        group['adopted_tables'] += row.get('adopted_tables', 0)
        group['adopted_cells'] += row.get('adopted_cells', 0)
        group['offered_cells'] += row.get('offered_cells', 0)
    stages = defaultdict(Counter)
    for row in public['rows']:
        stages[row['provider'] + '-' + row['algorithm']].update(row.get('failure_stages', {}))
    result['public_pdf_development'] = {'independent_cell_annotations': False, 'quality_acceptance': False,
        'groups': dict(groups), 'primary_failure_stages': dict(stages),
        'caution': 'Offered cells are matcher decisions, not independently scored correct locations.'}
    manifest = args.root / 'dataset/inputs/test.json'
    public_manifest = args.root / 'public-pdfs/inputs/development.json'
    inputs += [manifest, public_manifest]
    test_ids = [s['id'] for s in json.loads(manifest.read_text('utf-8'))['samples']]
    public_ids = [s['id'] for s in json.loads(public_manifest.read_text('utf-8'))['samples']]
    result['test_inference'] = {name: inference_summary(args.root / 'inference/test' / folder, test_ids, inputs)
        for name, folder in (('ppocr', 'ppocr'), ('paddle', 'geometry'),
                             ('rapidtable', 'rapidtable'), ('tableformer-raw', 'tableformer'))}
    result['test_inference']['adopted-paddlevl'] = inference_summary(args.root / 'adopted/test', test_ids, inputs)
    result['public_pdf_development']['inference'] = {name: inference_summary(
        args.root / 'public-inference-02' / name, public_ids, inputs)
        for name in ('context', 'adopted', 'paddle', 'rapidtable', 'tableformer-raw')}
    inputs += [args.root / 'selection-seal.json', args.root / 'dataset/split-lock.json',
               args.root / 'label-audit/crosscheck.json', args.root / 'queue-audit/receipt.json',
               args.root / 'ui-audit/ui-results.json', args.root / 'engineering-checks.json',
               args.root / 'visuals/review.json', args.root / 'public-pdfs/download-summary.json']
    result['evidence_sha256'] = file_lock(inputs)
    write_json(args.output / 'summary.json', result)
    for source, target in ((args.root / 'selection-seal.json', 'selection-seal.json'),
        (args.root / 'dataset/split-lock.json', 'test-split-lock.json'),
        (args.root / 'label-audit/crosscheck.json', 'official-label-crosscheck.json'),
        (args.root / 'queue-audit/receipt.json', 'queue-integration.json'),
        (args.root / 'ui-audit/ui-results.json', 'ui-results.json'),
        (args.root / 'engineering-checks.json', 'engineering-checks.json'),
        (args.root / 'visuals/review.json', 'visual-review.json'),
        (args.root / 'public-pdfs/download-summary.json', 'public-pdf-sources.json')):
        shutil.copyfile(source, args.output / target)
    print('Published compact evidence:', args.output, flush=True)


if __name__ == '__main__':
    main()
