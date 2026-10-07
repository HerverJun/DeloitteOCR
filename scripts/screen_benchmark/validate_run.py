"""Independent input, prepared-image, raw-output and context provenance checks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from context import verify_context_record
from run import digest, fpath, select_input_samples, validate_context_binding, write


def signature(tables):
    # Geometry may legitimately be mapped; transcription/row/column/span may not.
    return [(table.get('rows'), table.get('columns'), table.get('caption'),
             [(cell.get('row'), cell.get('column'), cell.get('row_span', 1),
               cell.get('column_span', 1), cell.get('text')) for cell in table.get('cells', [])])
            for table in tables]


def validate_run(inputs_path, runroot):
    inputs_path, runroot = fpath(inputs_path), fpath(runroot)
    inputs = json.loads(inputs_path.read_text('utf-8'))
    binding = json.loads((runroot/'binding.json').read_text('utf-8'))
    if inputs.get('truth_available_to_inference') is not False or binding.get('labels_or_ideal_regions_provided') is not False:
        raise ValueError('Unisolated benchmark input')
    if binding['inputs_sha256'] != digest(inputs_path):
        raise ValueError('Input manifest changed')
    context_root = None
    if binding.get('context_run'):
        context_root = fpath(binding['context_run'])
        context_binding_path = context_root/'binding.json'
        if digest(context_binding_path) != binding['context_binding_sha256']:
            raise ValueError('Consumed context binding changed')
        validate_context_binding(json.loads(context_binding_path.read_text('utf-8')), binding,source_policy=binding.get('context_source_policy','exact'))
    rows = []
    samples=select_input_samples(inputs['samples'],include_ids=binding.get('include_sample_ids'))
    for sample in samples:
        folder = runroot/sample['id']
        record_path = folder/'record.json'
        if not record_path.exists():
            rows.append({'id': sample['id'], 'status': 'missing'})
            continue
        record = json.loads(record_path.read_text('utf-8'))
        if record.get('id') != sample['id'] or record.get('image_sha256') != sample['image_sha256']:
            raise ValueError('Record identity differs')
        if digest(fpath(sample['image'])) != sample['image_sha256']:
            raise ValueError('Actual input image changed')
        if record['status'] != 'success':
            rows.append({'id': sample['id'], 'status': record['status'], 'error': record.get('error')})
            continue
        result_path = folder/'result.json'
        if digest(result_path) != record['result_sha256']:
            raise ValueError('Saved success result changed')
        result = json.loads(result_path.read_text('utf-8'))
        attempt = fpath(record['output'])
        if attempt.parent != folder:
            raise ValueError('Attempt path escaped its input folder')
        prepared = record.get('prepared_images')
        raw_results = []
        if prepared is None:
            # Historical full-image diagnosis predates regional manifests.
            # Accept only its explicit unprepared route and recorded image hash.
            if binding.get('preprocess_module_sha256') is not None or record.get('prepared_image_sha256') != sample['image_sha256']:
                raise ValueError('Legacy result has no verifiable actual model input')
            raw_results.append(json.loads((attempt/'result.json').read_text('utf-8')))
        else:
            for index, item in enumerate(prepared):
                if digest(fpath(item['path'])) != item['sha256']:
                    raise ValueError('Actual model input changed')
                raw_results.append(json.loads((attempt/f'region-{index:02d}'/'result.json').read_text('utf-8')))
        if any(raw.get('status') != 'success' for raw in raw_results):
            raise ValueError('Success contains failed raw regional output')
        tables = [table for raw in raw_results for table in raw.get('tables', [])]
        if signature(tables) != signature(result.get('tables', [])):
            raise ValueError('Aggregated transcription/structure differs from real raw engine output')
        if '\n\n'.join(raw.get('text', '') for raw in raw_results) != result.get('text', ''):
            raise ValueError('Aggregated text differs from real raw engine output')
        if context_root:
            context_record, _, _ = verify_context_record(context_root/sample['id'], sample)
            if context_record['context_sha256'] != record['context_sha256'] or context_record['evidence_sha256'] != record['context_evidence_sha256']:
                raise ValueError('Consumed context evidence changed')
        rows.append({'id': sample['id'], 'status': 'success', 'raw_regions': len(raw_results),
                     'image_prepared_output_context_identity_verified': True,
                     'transcription_matches_actual_engine_output': True})
    report = {'scope': 'Independent provenance and literal transcription checks; no truth read and no accuracy claim.',
            'binding': binding, 'total': len(rows), 'success': sum(r['status'] == 'success' for r in rows),
            'failed': sum(r['status'] == 'failed' for r in rows),
            'missing': sum(r['status'] == 'missing' for r in rows),
            'running': sum(r['status'] == 'running' for r in rows),
            'is_final': all(r['status'] in {'success', 'failed'} for r in rows), 'records': rows}
    if binding.get('include_sample_ids') is not None:
        report.update(scope='Independent provenance for explicitly bound diagnostic subset; no truth read or full-dataset accuracy claim.',
                      include_sample_ids=[sample['id'] for sample in samples])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('inputs', 'run', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args()
    args.output = fpath(args.output)
    if args.output.exists():
        raise ValueError('Preserve previous validation; select a new output filename')
    report = validate_run(args.inputs, args.run)
    write(args.output, report)
    print(json.dumps({key: report[key] for key in ('total', 'success', 'failed', 'missing', 'running', 'is_final')}))
    if args.require_complete and not report['is_final']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
