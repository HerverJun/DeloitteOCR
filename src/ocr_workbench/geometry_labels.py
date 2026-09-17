"""Independent geometry label contract. Never accepts OCR or predictions.

PDF row/column extents can contain gutters. Complete grid cells partition those
gutters at midpoints, as in the pinned PubTables structure-image generator.
"""
from copy import deepcopy
import math

from ocr_workbench.coordinates import box_polygon, polygon, bounds
from ocr_workbench.geometry_contract import checked_polygon, fingerprint

CONVENTIONS = ('text_extent', 'pdf_cell_extent', 'full_grid')
GRID_RECIPE = 'pubtables-midpoint-grid-v1'
OFFICIAL_COMMIT = '0bb3ab71c4ec2a8b6c656c67af40eeaf863f306e'


def rectangle(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise ValueError('Invalid annotation rectangle')
    return list(value)


def canonical_grid(canonical):
    if canonical.get('rotation', 0) or canonical.get('rotated', False) or canonical.get('pdf_is_rotated', False):
        raise ValueError('Rotated canonical labels require an explicit conversion')
    rows = [rectangle(r['pdf_row_bbox']) for r in canonical['rows']]
    columns = [rectangle(c['pdf_column_bbox']) for c in canonical['columns']]
    if not rows or not columns:
        raise ValueError('Missing canonical rows or columns')
    for items, low, high in ((rows, 1, 3), (columns, 0, 2)):
        if any(a[low] >= b[low] or a[high] >= b[high] for a, b in zip(items, items[1:])):
            raise ValueError('Unsupported rotated or unordered annotation axes')
        for a, b in zip(items, items[1:]):
            a[high] = b[low] = (a[high] + b[low]) / 2
        for item in items:
            rectangle(item)
    result, occupied = {}, set()
    for cell in canonical['cells']:
        rs, cs = cell['row_nums'], cell['column_nums']
        for indices, size in ((rs, len(rows)), (cs, len(columns))):
            if (not indices or any(type(i) is not int for i in indices)
                    or indices != list(range(min(indices), max(indices) + 1))
                    or min(indices) < 0 or max(indices) >= size):
                raise ValueError('Noncontiguous or invalid annotation span')
        slots = {(r, c) for r in rs for c in cs}
        if slots & occupied:
            raise ValueError('Overlapping annotation identities')
        occupied |= slots
        rb = [min(rows[r][0] for r in rs), min(rows[r][1] for r in rs),
              max(rows[r][2] for r in rs), max(rows[r][3] for r in rs)]
        cb = [min(columns[c][0] for c in cs), min(columns[c][1] for c in cs),
              max(columns[c][2] for c in cs), max(columns[c][3] for c in cs)]
        result[min(rs), min(cs), len(rs), len(cs)] = rectangle(
            [max(rb[0], cb[0]), max(rb[1], cb[1]), min(rb[2], cb[2]), min(rb[3], cb[3])])
    if len(occupied) != len(rows) * len(columns):
        raise ValueError('Annotation grid has unlabelled slots')
    return result


def derive_grid_sample(sample, canonical):
    grid = canonical_grid(canonical)
    if len(grid) != len(sample['targets']):
        raise ValueError('Canonical target count mismatch')
    transform = sample['coordinate_transform']
    if (len(transform) != 9 or transform[1] or transform[3] or transform[6:9] != [0, 0, 1]
            or transform[0] <= 0 or transform[4] <= 0):
        raise ValueError('Unsupported grid transform')
    derived = deepcopy(sample)
    seen = set()
    for target in derived['targets']:
        key = tuple(target[k] for k in ('row', 'column', 'row_span', 'column_span'))
        if key in seen:
            raise ValueError('Duplicate target identity')
        seen.add(key)
        box = bounds(polygon(transform, box_polygon(grid[key])))
        width, height = sample['width'], sample['height']
        if box[0] < -1 or box[1] < -1 or box[2] > width + 1 or box[3] > height + 1:
            raise ValueError('Grid outside image')
        box = rectangle([max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3])])
        checked_polygon(box_polygon(box), width, height)
        target['pdf_cell_extent'] = target.get('original_pdf_annotation_box', target['box'])
        target['box'] = box
        target['boundary_convention'] = 'full_grid'
    derived['boundary_contract'] = {'version': 1, 'convention': 'full_grid', 'recipe': GRID_RECIPE,
        'official_commit': OFFICIAL_COMMIT, 'canonical_sha256': fingerprint(canonical),
        'uses_predictions_or_ocr': False}
    return derived


def validate_annotations(annotations, convention):
    if convention not in CONVENTIONS or annotations.get('boundary_convention') != convention:
        raise ValueError('Explicit label boundary convention required; no implicit metric conversion')
    for sample in annotations['samples']:
        identities = set()
        for target in sample['targets']:
            key = tuple(target.get(k, 0) for k in ('table', 'row', 'column'))
            if key in identities or target.get('boundary_convention') != convention:
                raise ValueError('Duplicate target or mixed boundary conventions')
            identities.add(key)
            checked_polygon(box_polygon(rectangle(target['box'])), sample['width'], sample['height'])
    return annotations
