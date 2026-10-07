"""Offline crop-retention review; reference regions never reach preprocessing."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from context import verify_context_record
from run import digest, first_pass_images, fpath, isolate_runtime, source_hash, write


def shape(item):
    value = item.get('polygon', item.get('source_polygon'))
    if value is None and item.get('box'):
        x0, y0, x1, y1 = item['box']
        value = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    points = np.asarray(value, dtype=np.float32)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.isfinite(points).all():
        raise ValueError('No valid actual polygon')
    return points


def area(points):
    return abs(float(cv2.contourArea(np.asarray(points, dtype=np.float32))))


def intersection(a, b):
    overlap, _ = cv2.intersectConvexConvex(np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32))
    return max(0., float(overlap))


def coverage(target, selected):
    """Conservative: a field must be retained in one whole-table candidate."""
    points = shape(target)
    size = area(points)
    if size <= 0:
        raise ValueError('Reference polygon has no area')
    return min(1., max((intersection(points, shape(item))/size for item in selected), default=0.))


def inspect_preparation(truth, preparation):
    selected = first_pass_images(preparation)
    counts = Counter({'images': 1})
    lost_cells, lost_context = [], []
    for index, table in enumerate(truth.get('tables', [])):
        for cell in table['cells']:
            retained = coverage(cell, selected)
            counts['cells'] += 1
            counts['retained_cells'] += retained >= .98
            visible = cell.get('visibility', 'visible') == 'visible'
            counts['visible_cells'] += visible
            counts['retained_visible_cells'] += visible and retained >= .98
            if retained < .98:
                lost_cells.append({'table': index, 'row': cell['row'], 'column': cell['column'],
                                   'text': cell['text'], 'visibility': cell.get('visibility', 'visible'),
                                   'geometric_coverage': retained})
    for target in truth.get('context_targets', []):
        retained = coverage(target, selected)
        counts['context_targets'] += 1
        counts['retained_context_targets'] += retained >= .98
        if retained < .98:
            lost_context.append({'text': target['text'], 'kind': target.get('kind'), 'geometric_coverage': retained})
    candidates = []
    for item in selected:
        source = shape(item)
        ui = [{'kind': target['kind'], 'overlap_pixels': intersection(source, shape(target))}
              for target in truth.get('ui_regions', [])]
        ui_fraction = min(1., sum(i['overlap_pixels'] for i in ui)/max(area(source), 1.))
        counts['selected_regions'] += 1
        counts['regions_with_ui_geometry'] += ui_fraction > .001
        candidates.append({'id': item['id'], 'kind': item['kind'], 'region_id': item.get('region_id'),
                           'source_polygon': item['source_polygon'], 'ui_area_fraction': ui_fraction,
                           'ui_overlaps': ui})
    counts['images_with_visible_cell_crop_loss'] = int(any(c['visibility'] == 'visible' for c in lost_cells))
    counts['images_with_context_crop_loss'] = int(bool(lost_context))
    counts['images_with_ui_geometry'] = int(any(i['ui_area_fraction'] > .001 for i in candidates))
    return {'id': truth['id'], 'status': 'success', 'cohort': truth.get('cohort'), 'level': truth.get('level'),
            'source_id': truth.get('source_id'), 'group_id': truth.get('group_id'), 'family': truth.get('family'),
            'primary_id': preparation['primary_id'], 'business_regions': preparation['business_regions'],
            'excluded_regions': preparation['excluded_regions'], 'quality': preparation['quality'],
            'selected': candidates, 'metrics': dict(counts), 'lost_cells': lost_cells, 'lost_context': lost_context}


def summarize(rows):
    totals = Counter()
    for row in rows:
        totals.update(row.get('metrics', {}))
    totals['total_images'] = len(rows)
    totals['context_failed'] = sum(r['status'] == 'failed' for r in rows)
    totals['context_running'] = sum(r['status'] == 'running' for r in rows)
    totals['context_missing'] = sum(r['status'] == 'missing' for r in rows)
    return dict(totals)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('inputs', 'truth', 'context-run', 'worker-source', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    for name in ('inputs', 'truth', 'context_run', 'worker_source', 'output'):
        setattr(args, name, fpath(getattr(args, name)))
    if args.output.exists():
        raise ValueError('Preserve previous review; select a new output filename')
    artifact_root = args.output.parent / (args.output.stem + '-artifacts')
    isolate_runtime(artifact_root)
    sys.path.insert(0, str(args.worker_source))
    from ocr_workbench.screen_evidence import normalize_screen_context
    from ocr_workbench.screen_imaging import prepare_screen_image

    binding = json.loads((args.context_run/'binding.json').read_text('utf-8'))
    if binding['inputs_sha256'] != digest(args.inputs) or binding['worker_source_sha256'] != source_hash(args.worker_source):
        raise ValueError('Context input/source identity differs')
    inputs = json.loads(args.inputs.read_text('utf-8'))
    truth = json.loads(args.truth.read_text('utf-8'))
    if inputs.get('truth_available_to_inference') is not False:
        raise ValueError('Only isolated model inputs accepted')
    samples = {s['id']: s for s in inputs['samples']}
    if len(samples) != len(truth['samples']) or set(samples) != {s['id'] for s in truth['samples']}:
        raise ValueError('Reference/input identity differs')
    rows = []
    for reference in truth['samples']:
        sample = samples[reference['id']]
        record_path = args.context_run/sample['id']/'record.json'
        if not record_path.exists():
            rows.append({'id': sample['id'], 'status': 'missing'})
            continue
        record = json.loads(record_path.read_text('utf-8'))
        if record['status'] != 'success':
            rows.append({'id': sample['id'], 'status': record['status'], 'error': record.get('error')})
            continue
        if digest(sample['image']) != sample['image_sha256']:
            raise ValueError('Actual input image changed')
        _, _, evidence = verify_context_record(args.context_run/sample['id'], sample, normalize=normalize_screen_context)
        # This call sees pixels and actual model evidence only. Truth is used later.
        prepared = prepare_screen_image(sample['image'], artifact_root/sample['id'], evidence=evidence, perspective=True)
        rows.append(inspect_preparation(reference, prepared))
    groups = defaultdict(list)
    strata = defaultdict(list)
    for row in rows:
        groups[row.get('group_id', 'unavailable')].append(row)
        strata[f"{row.get('cohort', 'unavailable')}/{row.get('level', 'unavailable')}"] .append(row)
    report = {'scope': 'Offline geometric crop-retention diagnostics, not OCR accuracy. No oracle crop or truth reached inference/preparation.',
              'input_sha256': digest(args.inputs), 'truth_sha256': digest(args.truth),
              'context_binding': binding, 'is_final': all(r['status'] in {'success', 'failed'} for r in rows),
              'coverage_rule': 'At least 98% polygon area within a single actually selected whole-table candidate. Visibility is separate; area does not prove readability.',
              'summary': summarize(rows), 'by_stratum': {k: summarize(v) for k, v in strata.items()},
              'by_group': {k: summarize(v) for k, v in groups.items()}, 'samples': rows}
    write(args.output, report)
    print(json.dumps(report['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
