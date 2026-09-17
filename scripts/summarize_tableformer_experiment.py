"""Post-scoring diagnostics; never used to select candidates or tune the matcher."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geometry_eval_common import ROOT, sha, write_json
sys.path.insert(0, str(ROOT / 'src'))
import numpy as np
from report_geometry_v2 import percentiles
from diagnose_geometry_legacy import matching_upper_bound


def outcomes(rows):
    result = Counter()
    for r in rows:
        if r['counts'].get('offered'):
            key = r.get('scorer_outcome', 'invalid_geometry')
        else:
            key = 'rejected:' + r['primary_failure_stage']
        result[key] += 1
    return dict(result)


def paired(old, new):
    def identity(r):
        return (r['sample_id'], r['target']['table'], r['target']['row'], r['target']['column'])
    before, after = {identity(r): r for r in old}, {identity(r): r for r in new}
    if before.keys() != after.keys():
        raise ValueError('Paired comparison targets differ')
    docs = defaultdict(lambda: [0, 0])
    counts = Counter()
    for key, b in before.items():
        a = after[key]
        bc, ac = bool(b.get('correct')), bool(a.get('correct'))
        counts['new_correct'] += int(ac and not bc)
        counts['lost_correct'] += int(bc and not ac)
        counts['both_correct'] += int(bc and ac)
        counts['new_wrong'] += int(bool(a.get('wrong_cell')) and not b.get('wrong_cell'))
        docs[a['group_id']][0] += int(ac) - int(bc)
        docs[a['group_id']][1] += 1
    data = np.asarray(list(docs.values()))
    rng = np.random.default_rng(20260914)
    draws = data[rng.integers(0, len(data), size=(2000, len(data)))].sum(axis=1)
    return {**counts, 'coverage_delta': float(data[:, 0].sum() / data[:, 1].sum()),
            'paired_document_bootstrap_95pct': np.percentile(draws[:, 0] / draws[:, 1], [2.5, 97.5]).tolist()}


def raw_oracle(annotations, tableformer):
    totals = Counter()
    per_table = []
    for sample in annotations['samples']:
        path = tableformer / sample['id'] / 'prediction.json'
        cells = json.loads(path.read_text('utf-8'))['tf_table_cells'] if path.exists() else []
        boxes = [c['bbox'] for c in cells]
        valid = [b for b in boxes if len(b) == 4 and 0 <= b[0] < b[2] <= sample['width']
                 and 0 <= b[1] < b[3] <= sample['height']]
        count = {'targets': len(sample['targets']), 'raw_candidates': len(boxes),
                 'valid_candidates': len(valid), 'any_iou50': 0, 'one_to_one_iou50': 0}
        if valid:
            target = np.asarray([t['box'] for t in sample['targets']])
            candidate = np.asarray(valid)
            wh = np.maximum(0, np.minimum(target[:, None, 2:], candidate[None, :, 2:])
                            - np.maximum(target[:, None, :2], candidate[None, :, :2]))
            intersection = wh.prod(axis=2)
            union = (target[:, 2:] - target[:, :2]).prod(axis=1)[:, None] + (candidate[:, 2:] - candidate[:, :2]).prod(axis=1)[None, :] - intersection
            edges = [np.flatnonzero(row >= .5).tolist() for row in intersection / union]
            count.update(any_iou50=sum(bool(e) for e in edges), one_to_one_iou50=matching_upper_bound(edges))
        totals.update(count)
        per_table.append({'id': sample['id'], **count})
    return {'counts': dict(totals), 'one_to_one_coverage': totals['one_to_one_iou50'] / totals['targets'],
            'scope': 'GT-assisted diagnostic of raw single rectangles, not achieved accuracy; ignores text, spans and candidate unions',
            'tables': per_table}


def main():
    p = argparse.ArgumentParser()
    for name in ('report', 'replay', 'inference', 'tableformer', 'annotations', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise ValueError('Diagnostic reports are immutable')
    report = json.loads(a.report.read_text('utf-8'))
    if not report['complete'] or report['replay_lock_sha256'] != sha(a.replay / 'run-lock.json'):
        raise ValueError('Only complete verified scoring can be summarized')
    annotations = json.loads(a.annotations.read_text('utf-8'))
    if sha(a.annotations) != report['annotations_sha256']:
        raise ValueError('Scoring annotations changed')
    result = {'split': report['split'], 'complete': report['complete'], 'report_sha256': sha(a.report),
              'scoring_groups': report['groups'], 'methods': {}, 'paired': {}, 'shared_ocr_consistent': True}
    all_ocr = defaultdict(set)
    for method, rows in report['rows'].items():
        tables = defaultdict(Counter)
        for row in rows:
            tables[row['sample_id']].update(row['counts'])
        timings, replay_times, statuses = [], [], Counter()
        for sample_id in tables:
            mapping = json.loads((a.replay / method / sample_id / 'mapping.json').read_text('utf-8'))
            statuses[mapping['status']] += 1
            if mapping.get('shared_ocr_sha256'):
                all_ocr[sample_id].add(mapping['shared_ocr_sha256'])
            if mapping.get('correspondence_seconds') is not None:
                timings.append(mapping['correspondence_seconds'] * 1000)
            if mapping.get('mapping_seconds') is not None:
                replay_times.append(mapping['mapping_seconds'] * 1000)
        result['methods'][method] = {'mutually_exclusive_outcomes': outcomes(rows),
            'tables_with_correct': sum(c['correct'] > 0 for c in tables.values()),
            'tables_without_correct': sum(c['correct'] == 0 for c in tables.values()),
            'tables_at_least_90pct_correct': sum(c['correct'] / c['targets'] >= .9 for c in tables.values()),
            'correspondence_ms': percentiles(timings), 'replay_ms_including_io': percentiles(replay_times),
            'replay_status': dict(statuses), 'tables': [{'id': k, **dict(v)} for k, v in tables.items()]}
    result['shared_ocr_consistent'] = len(all_ocr) == len(annotations['samples']) and all(len(v) == 1 for v in all_ocr.values())
    for baseline in ('Paddle', 'RapidTable'):
        result['paired']['TableFormer-vs-' + baseline] = paired(report['rows'][baseline], report['rows']['TableFormer'])
    result['inference'] = {}
    for provider in ('ppocr', 'geometry', 'rapidtable', 'tableformer'):
        root = a.tableformer if provider == 'tableformer' else a.inference / provider
        times, statuses = [], Counter()
        for sample in annotations['samples']:
            marker = json.loads((root / sample['id'] / 'evaluation.json').read_text('utf-8'))
            statuses[marker['status']] += 1
            elapsed = marker.get('inference_seconds')
            if elapsed is not None:
                times.append(elapsed)
        cold = root / 'timing.json'
        result['inference'][provider] = {'seconds': percentiles(times), 'status': dict(statuses),
            'cold': json.loads(cold.read_text('utf-8')) if cold.exists() else None}
    result['tableformer_raw_oracle'] = raw_oracle(annotations, a.tableformer)
    write_json(a.output, result)
    print(json.dumps({'split': result['split'], 'ocr_consistent': result['shared_ocr_consistent'],
                      'paired': result['paired'], 'oracle': result['tableformer_raw_oracle']['counts']}), flush=True)


if __name__ == '__main__':
    main()
