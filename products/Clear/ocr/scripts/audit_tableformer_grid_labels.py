"""Separate, explicit PubTables grid-label protocol; never edits frozen labels.

Official process_pubmed.py saves PDF annotations BEFORE grid dilation. This
reconstructs its non-rotated midpoint dilation for full-grid sensitivity scoring.
No predictions, OCR coordinates or model scores enter label construction.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geometry_eval_common import ROOT, file_lock, mapping_code_lock, sha, verify_inputs, write_json

EXPERIMENT = ROOT / 'build/tableformer-expanded-20260914'
UPSTREAM = EXPERIMENT / 'annotation-audit/process_pubmed.py'
UPSTREAM_SHA = '163a34651c89797c692d85a00e3b2088d475026ab063b7b1c3849899711730d7'


def protocol_code():
    if sha(UPSTREAM) != UPSTREAM_SHA:
        raise ValueError('Official source changed')
    return {'mapping': mapping_code_lock(), 'supplement': file_lock([Path(__file__), UPSTREAM])}


def grid_boxes(canonical):
    """Faithful unrotated midpoint dilation then union-column/intersect-row."""
    rows = [list(r['pdf_row_bbox']) for r in canonical['rows']]
    cols = [list(c['pdf_column_bbox']) for c in canonical['columns']]
    if sorted(range(len(rows)), key=lambda i: rows[i][3]) != list(range(len(rows))):
        raise ValueError('Unsupported rotated or unordered rows')
    if sorted(range(len(cols)), key=lambda i: cols[i][2]) != list(range(len(cols))):
        raise ValueError('Unsupported rotated or unordered columns')
    for before, after in zip(rows, rows[1:]):
        before[3] = after[1] = (before[3] + after[1]) / 2
    for before, after in zip(cols, cols[1:]):
        before[2] = after[0] = (before[2] + after[0]) / 2
    result = {}
    for cell in canonical['cells']:
        rs, cs = cell['row_nums'], cell['column_nums']
        rb = [min(rows[r][0] for r in rs), min(rows[r][1] for r in rs),
              max(rows[r][2] for r in rs), max(rows[r][3] for r in rs)]
        cb = [min(cols[c][0] for c in cs), min(cols[c][1] for c in cs),
              max(cols[c][2] for c in cs), max(cols[c][3] for c in cs)]
        result[(min(rs), min(cs), len(rs), len(cs))] = [max(rb[0], cb[0]), max(rb[1], cb[1]),
                                                      min(rb[2], cb[2]), min(rb[3], cb[3])]
    if len(result) != len(canonical['cells']):
        raise ValueError('Duplicate canonical cell identity')
    return result


def derive(a):
    code = protocol_code()
    manifest = verify_inputs(a.manifest)
    split = manifest['split']
    if a.output.exists():
        raise ValueError('Derived labels are immutable')
    if split == 'test':
        if not a.test_seal:
            raise ValueError('Seal validated grid convention before reading test labels')
        sealed = json.loads(a.test_seal.read_text('utf-8'))
        if sealed['code'] != code or sealed['test_input_sha256'] != sha(a.manifest) or sealed['source_test_annotations_sha256'] != sha(a.annotations):
            raise ValueError('Grid protocol seal mismatch')
    original = json.loads(a.annotations.read_text('utf-8'))
    original_lock = json.loads((a.manifest.parent.parent / 'split-lock.json').read_text('utf-8'))
    relative = str(a.annotations.relative_to(a.manifest.parent.parent)).replace('\\', '/')
    declared = {k.replace('\\', '/'): v for k, v in original_lock['files'].items()}
    if declared[relative] != sha(a.annotations):
        raise ValueError('Frozen source annotations changed')
    annotations = deepcopy(original)
    ratios, canonical_files = [], []
    for sample in annotations['samples']:
        canonical_path = a.canonical / (sample['id'] + '.json')
        canonical_files.append(canonical_path)
        canonical = json.loads(canonical_path.read_text('utf-8'))
        grid = grid_boxes(canonical)
        if len(grid) != len(sample['targets']):
            raise ValueError('Canonical target count mismatch')
        transform = sample['coordinate_transform']
        if transform[1] or transform[3]:
            raise ValueError('Unsupported rotation transform')
        for target in sample['targets']:
            key = tuple(target[k] for k in ('row', 'column', 'row_span', 'column_span'))
            b = grid[key]
            box = [transform[0] * b[0] + transform[2], transform[4] * b[1] + transform[5],
                   transform[0] * b[2] + transform[2], transform[4] * b[3] + transform[5]]
            if box[0] < -1 or box[1] < -1 or box[2] > sample['width'] + 1 or box[3] > sample['height'] + 1:
                raise ValueError('Grid outside image')
            box = [max(0, box[0]), max(0, box[1]), min(sample['width'], box[2]), min(sample['height'], box[3])]
            if box[0] >= box[2] or box[1] >= box[3]:
                raise ValueError('Invalid grid cell')
            old = target['box']
            ratios.append(((box[2] - box[0]) * (box[3] - box[1])) / ((old[2] - old[0]) * (old[3] - old[1])))
            target['box'] = box
            target['original_pdf_annotation_box'] = old
    annotations['geometry_convention'] = 'official-midpoint-dilated-grid-supplement'
    inp = a.output / 'inputs' / (split + '.json')
    inp.parent.mkdir(parents=True)
    shutil.copyfile(a.manifest, inp)
    for sample in manifest['samples']:
        destination = (inp.parent / sample['image']).resolve()
        if not destination.is_relative_to(a.output.resolve()):
            raise ValueError('Image path escapes derived dataset')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(a.manifest.parent / sample['image'], destination)
    label = a.output / 'annotations' / (split + '.annotations.json')
    write_json(label, annotations)
    import statistics
    write_json(a.output / 'protocol.json', {'convention': annotations['geometry_convention'], 'code': code,
        'original_annotations_sha256': sha(a.annotations), 'canonical': file_lock(canonical_files),
        'test_protocol_sha256': sha(a.test_seal) if a.test_seal else None,
        'tables': len(annotations['samples']), 'targets': len(ratios),
        'median_grid_to_original_area_ratio': statistics.median(ratios),
        'cells_grid_area_over_1_5x': sum(v > 1.5 for v in ratios),
        'uses_predictions_or_ocr': False, 'rotated_tables_supported': False})
    paths = [inp, label, a.output / 'protocol.json']
    write_json(a.output / 'split-lock.json', {'version': 2, 'files': {
        str(p.relative_to(a.output)).replace('\\', '/'): sha(p) for p in paths}})
    verify_inputs(inp)
    print(split, len(annotations['samples']), len(ratios), 'grid labels derived', flush=True)


def seal(a):
    if a.output.exists():
        raise ValueError('Grid test seal is immutable')
    if (EXPERIMENT / 'reports/test.json').exists() or (EXPERIMENT / 'reports/test.grid.json').exists():
        raise ValueError('This supplementary protocol must be selected before test scoring')
    code = protocol_code()
    reports = {}
    for split, path, count in [('development', a.development_report, 80), ('validation', a.validation_report, 60)]:
        report = json.loads(path.read_text('utf-8'))
        if not report['complete'] or report['split'] != split or any(g['all']['tables'] != count for g in report['groups'].values()):
            raise ValueError('Require complete expanded development/validation grid scoring')
        derived = EXPERIMENT / 'grid-labels' / split
        protocol = json.loads((derived / 'protocol.json').read_text('utf-8'))
        if protocol['code'] != code or report['annotations_sha256'] != sha(derived / 'annotations' / (split + '.annotations.json')):
            raise ValueError('Grid development/validation code or labels changed')
        reports[split] = {'path': str(path.resolve()), 'sha256': sha(path)}
    test_inputs = EXPERIMENT / 'dataset/inputs/test.json'
    test_labels = EXPERIMENT / 'dataset/sealed/test.annotations.json'
    write_json(a.output, {'version': 1, 'kind': 'supplementary geometry-convention correction before test scoring',
        'code': code, 'reports': reports, 'test_input_sha256': sha(test_inputs),
        'source_test_annotations_sha256': sha(test_labels),
        'original_selection_sha256': sha(EXPERIMENT / 'selection-seal.json'),
        'test_inference_already_completed': True, 'test_scores_examined': False,
        'model_matcher_ocr_thresholds_changed': False, 'original_metric_preserved': True,
        'convention': 'Official midpoint dilation; unrotated rows/columns; union columns intersect union rows; original affine crop transform',
        'created_utc': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
    print(a.output, flush=True)


def main():
    p = argparse.ArgumentParser()
    commands = p.add_subparsers(dest='command', required=True)
    d = commands.add_parser('derive')
    for name in ('manifest', 'annotations', 'canonical', 'output'):
        d.add_argument('--' + name, type=Path, required=True)
    d.add_argument('--test-seal', type=Path)
    s = commands.add_parser('seal')
    for name in ('development-report', 'validation-report', 'output'):
        s.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    {'derive': derive, 'seal': seal}[a.command](a)


if __name__ == '__main__':
    main()
