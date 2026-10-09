"""Truth-free CPU feasibility probe for a fully observed automatic subset.

This experiment never changes product review state. It requires complete
source-pixel cell partitions, unique independent spatial literal readings and
source-bound agreeing business metadata. An omitted check never passes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import math
from pathlib import Path
import unicodedata

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment
from native_diagnostics import diagnostic_source


def literal(value):
    return ''.join(unicodedata.normalize('NFC', str(value)).split())


def intersection(a, b):
    return max(0., min(a[2], b[2])-max(a[0], b[0])) * max(0., min(a[3], b[3])-max(a[1], b[1]))


def area(box):
    return (box[2]-box[0]) * (box[3]-box[1])


def decide(record, module):
    reasons, cells = [], []
    if record['status'] != 'success':
        return {'id': record['id'], 'eligible': False, 'reasons': ['no_successful_candidate'], 'cells': []}
    raw = record['candidate']['original']
    screen = raw.get('screen', {})
    preparation = screen.get('prepared') or {}
    metadata = screen.get('business_metadata_evidence') or {}
    evidence = metadata.get('auxiliary_evidence') or {}
    require = lambda value, reason: reasons.append(reason) if not value else None
    require(record['image_sha256'] == preparation.get('input_sha256') == screen.get('input_sha256'), 'input_binding')
    require(raw.get('project_image_version') == record['input_version_id'] == evidence.get('provenance', {}).get('image_version'), 'image_version_binding')
    require(metadata.get('auxiliary_binding_verified') is True and metadata.get('primary_binding_verified') is True, 'metadata_source_binding')
    require(bool(metadata.get('items')) and all(item.get('review_required') is False for item in metadata['items']), 'metadata_review_pending_or_missing')
    require(bool(metadata.get('primary_metadata_comparisons')) and
            all(part.get('compatible') is True for part in metadata['primary_metadata_comparisons']), 'metadata_literal_agreement_missing')
    require(not any(flag.get('severity') in {'high', 'medium'} for flag in preparation.get('quality', {}).get('flags', []))
            and not preparation.get('quality', {}).get('unreadable'), 'source_quality')
    grid = module.observe_screen_grid_cells(raw, preparation, evidence)
    tables = raw.get('tables', [])
    verified = {part['table']: part for part in grid['tables'] if part['status'] == 'verified'}
    require(bool(tables) and len(verified) == len(tables) == len(preparation.get('business_regions', [])), 'complete_observed_table_cell_partition')
    readings = [(index, part, module.observed_box(part)) for index, part in enumerate(evidence.get('text', []))
                if isinstance(part, dict) and literal(part.get('text', ''))
                and part.get('kind') not in {'toolbar', 'taskbar', 'formula_bar', 'ruler', 'window_border', 'scrollbar', 'ui'}
                and module.observed_box(part)]
    used, eligible_readings = set(), set()
    for ti, table in enumerate(tables):
        proof = verified.get(ti)
        if proof is None:
            continue
        geometries = {(cell['row'], cell['column'], cell.get('row_span', 1), cell.get('column_span', 1)): cell for cell in proof['cells']}
        for cell in table['cells']:
            key = (cell['row'], cell['column'], cell.get('row_span', 1), cell.get('column_span', 1))
            box = geometries[key]['box']
            matches = [(index, part) for index, part, rb in readings if intersection(rb, box)/area(rb) >= .9]
            confidence = matches[0][1].get('confidence', matches[0][1].get('score')) if len(matches) == 1 else None
            match = bool(literal(cell['text'])) and len(matches) == 1 and literal(matches[0][1]['text']) == literal(cell['text'])
            independent = (len(matches) == 1 and str(matches[0][1].get('source', '')).startswith('PP-OCR')
                and type(confidence) in (int, float) and math.isfinite(confidence) and confidence >= .98)
            supported = match and independent
            cells.append({'table': ti, 'row': cell['row'], 'column': cell['column'], 'box': box,
                'literal_agreement': match, 'high_confidence_independent_reading': independent,
                'evidence_indices': [index for index, _ in matches], 'supported': supported})
            if supported:
                used.add(matches[0][0])
            for index, _ in matches:
                eligible_readings.add(index)
    for index, part, rb in readings:
        if any(intersection(rb, region.get('table_box', region.get('box')))/area(rb) >= .9
               for region in preparation.get('business_regions', []) if region.get('table_box', region.get('box'))):
            eligible_readings.add(index)
    require(bool(cells) and len(cells) == sum(len(t['cells']) for t in tables)
            and all(cell['supported'] for cell in cells), 'every_nonempty_cell_independently_agrees')
    require(used == eligible_readings, 'no_unassigned_visible_table_text')
    return {'id': record['id'], 'eligible': not reasons, 'reasons': reasons, 'cells': cells,
        'grid': grid, 'metadata_pending': metadata.get('counts', {}).get('review_required'),
        'evidence_text_count': len(readings), 'input_sha256': record['image_sha256']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'observations', 'inputs', 'output'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args(); require_environment()
    source, observations_path, inputs_path, output = map(fpath, (args.source, args.observations, args.inputs, args.output))
    if output.exists():
        raise ValueError('Use a new immutable probe output')
    observations, inputs = read_json(observations_path), read_json(inputs_path)
    if observations['inputs_sha256'] != digest(inputs_path) or inputs.get('truth_available_to_inference') is not False:
        raise ValueError('Truth-free original input scope binding is missing')
    samples = {sample['id']: sample for sample in inputs['samples']}
    if len(samples) != len(inputs['samples']) or set(samples) != {record['id'] for record in observations['records']}:
        raise ValueError('Probe must consume the entire frozen development scope')
    for record in observations['records']:
        sample = samples[record['id']]
        if record['image_sha256'] != sample['image_sha256'] or digest(fpath(sample['image'])) != record['image_sha256']:
            raise ValueError('Bound source pixel identity changed')
    output.mkdir(parents=True)
    recipe = {'format': 'strict-review-subset-feasibility-v1', 'utc': datetime.now(timezone.utc).isoformat(),
        'helper': bound(__file__), 'observations': bound(observations_path), 'inputs': bound(inputs_path),
        'conditions': ['complete source-pixel partition and independently anchored row/column mapping',
            'every cell nonempty and uniquely spatially covered by PP-OCR literal at confidence >= 0.98',
            'all visible in-table readings assigned', 'bound agreeing primary/auxiliary business metadata without review flags',
            'all observed business regions represented, no medium/high source quality flag'],
        'truth_read': False, 'validation_or_holdout_read': False, 'native_review_state_changed': False,
        'development_diagnostic_only': True, 'automatic_delivery_qualification': False, 'model_calls': 0}
    frozen_json(output/'recipe.json', recipe)
    with diagnostic_source(source) as module:
        decisions = [decide(record, module) for record in observations['records']]
    eligible = {row['id'] for row in decisions if row['eligible']}
    derived = deepcopy(observations)
    derived.update(evidence_kind='development_cpu_derived_subset_feasibility',
        derivation={'recipe': bound(output/'recipe.json'), 'product_state_unchanged': True})
    for record in derived['records']:
        record['native_original_review_snapshot'] = {key: deepcopy(record.get(key)) for key in
            ('automatic_delivery', 'review_image', 'review_regions', 'review_state', 'human_confirmed')}
        record['automatic_delivery'] = record['id'] in eligible
        record['review_image'] = record['id'] not in eligible
        record['review_required'] = record['id'] not in eligible
        record['requires_human_review'] = record['id'] not in eligible
        record['review_regions'] = [] if record['id'] in eligible else [{'scope': 'image'}]
    frozen_json(output/'decisions.json', {'scope_inputs': len(decisions), 'eligible_images': len(eligible), 'decisions': decisions})
    frozen_json(output/'observations.json', derived)
    report = {**recipe, 'scope_inputs': len(decisions), 'eligible_images': len(eligible),
        'fully_mapped_images': sum(bool(row.get('cells')) and 'complete_observed_table_cell_partition' not in row['reasons'] for row in decisions),
        'rejection_reasons': dict(Counter(reason for row in decisions for reason in row['reasons'])),
        'decisions': bound(output/'decisions.json'), 'derived_observations': bound(output/'observations.json')}
    frozen_json(output/'result.json', report)
    print({key: report[key] for key in ('scope_inputs', 'eligible_images', 'fully_mapped_images', 'rejection_reasons')})


if __name__ == '__main__':
    main()
