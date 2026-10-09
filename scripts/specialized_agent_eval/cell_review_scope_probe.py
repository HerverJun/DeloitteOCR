"""Truth-free conservative cell review scope feasibility, never product release.

Only complete source-pixel/independent-text proofs may propose narrower review.
Decimal digits, Latin identifiers and signs remain reviewed. CJK membership
does not prove label semantics: CJK amounts and identifiers are a known limit.
This leaves the rejected automatic-image experiment unchanged, repairs no
values and cannot itself qualify a product review policy.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import re

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment
from native_diagnostics import diagnostic_source
from strict_review_subset_probe import decide, literal


def conservative_review(value):
    text = literal(value)
    if re.search(r'\d', text):
        return 'numeric_or_identifier_literal'
    if re.search(r'[-−–—﹣－+＋.,，。:/\\%％()（）]', text):
        return 'punctuation_or_sign_literal'
    if re.search(r'[A-Za-z]', text):
        return 'latin_or_identifier_literal'
    # This tests CJK strings only, not their business role. Chinese numerals and
    # names/identifiers need a semantic safeguard before this could be adopted.
    # Symbols and other scripts retain review; Unicode is never made ASCII.
    if not text or any(not '\u4e00' <= char <= '\u9fff' for char in text):
        return 'unknown_script_or_empty_literal'
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'observations', 'inputs', 'output'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args(); require_environment()
    source, observations_path, inputs_path, output = map(fpath,
        (args.source, args.observations, args.inputs, args.output))
    if output.exists():
        raise ValueError('Use a new immutable cell-scope experiment')
    observations, inputs = read_json(observations_path), read_json(inputs_path)
    if observations['inputs_sha256'] != digest(inputs_path) or inputs.get('truth_available_to_inference') is not False:
        raise ValueError('Frozen truth-free input scope binding is missing')
    samples = {row['id']: row for row in inputs['samples']}
    records = {row['id']: row for row in observations['records']}
    if len(samples) != len(inputs['samples']) or len(records) != len(observations['records']) or samples.keys() != records.keys():
        raise ValueError('Every declared input must remain in the denominator')
    for sid, row in records.items():
        if row['image_sha256'] != samples[sid]['image_sha256'] or digest(fpath(samples[sid]['image'])) != row['image_sha256']:
            raise ValueError('Source pixel binding changed')
    output.mkdir(parents=True)
    helpers = ('cell_review_scope_probe.py', 'strict_review_subset_probe.py', 'native_diagnostics.py', 'common.py')
    helper_sources = {}
    for name in helpers:
        original = fpath(__file__).parent/name
        target = output/'helper-source'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original.read_bytes())
        helper_sources[name] = bound(target)
    recipe = {'format': 'conservative-cell-review-scope-feasibility-v1',
        'utc': datetime.now(timezone.utc).isoformat(), 'source': str(source),
        'helpers': helper_sources, 'observations': bound(observations_path), 'inputs': bound(inputs_path),
        'conditions': ['complete observed source-pixel grid with independently anchored mapping',
            'every nonempty cell has unique agreeing PP-OCR literal at confidence >= .98',
            'no unassigned in-table text; all observed business regions represented',
            'source-bound agreeing metadata without review flags; no medium/high source-quality flag',
            'decimal-digit, Latin-identifier, sign, punctuation and unknown-script strings retain local review',
            'only independently agreeing CJK strings may leave image-wide review'],
        'known_limits': ['CJK membership alone does not establish a business label; CJK amounts and identifiers can match this diagnostic predicate.',
            'Requires semantic safeguards and independent validation before any product adoption.'],
        'truth_read': False, 'validation_or_holdout_read': False, 'native_state_changed': False,
        'model_calls': 0, 'automatic_images_permitted': False, 'value_repairs': 0,
        'development_diagnostic_only': True, 'human_review_time_measured': False,
        'automatic_delivery_qualification': False}
    frozen_json(output/'recipe.json', recipe)
    with diagnostic_source(source) as module:
        decisions = [decide(row, module) for row in observations['records']]
    derived = deepcopy(observations)
    derived.update(evidence_kind='development_cpu_conservative_cell_scope_feasibility',
        derivation={'recipe': bound(output/'recipe.json'), 'product_state_unchanged': True})
    derived_records = {row['id']: row for row in derived['records']}
    narrowed = []
    for decision in decisions:
        decision['narrowed_review'] = False
        if not decision['eligible']:
            continue
        row = derived_records[decision['id']]
        raw_tables = row['candidate']['original']['tables']
        positions = {(ti, cell['row'], cell['column']): cell['text']
                     for ti, table in enumerate(raw_tables) for cell in table['cells']}
        reviewed, freed = [], []
        for cell in decision['cells']:
            position = cell['table'], cell['row'], cell['column']
            reason = conservative_review(positions[position])
            entry = {**cell, 'literal': positions[position], 'review_reason': reason}
            (reviewed if reason else freed).append(entry)
        # A task with no remaining review has a different release contract.
        # Keep it at image scope in this bounded experiment.
        if not reviewed or not freed:
            continue
        row['native_original_review_snapshot'] = {key: deepcopy(row.get(key)) for key in
            ('automatic_delivery', 'review_image', 'review_regions', 'review_state', 'human_confirmed')}
        row.update(automatic_delivery=False, review_image=False, review_required=True,
                   requires_human_review=True, human_confirmed=False)
        row['review_regions'] = [{'scope': 'cell', 'table_index': cell['table'],
            'row': cell['row'], 'column': cell['column'], 'reliable': True,
            'evidence_ref': {'recipe': bound(output/'recipe.json'), 'sample_id': row['id'],
                'source_sha256': row['image_sha256'], 'box': cell['box'],
                'auxiliary_text_indices': cell['evidence_indices']},
            'reason': cell['review_reason']} for cell in reviewed]
        decision.update(narrowed_review=True, reviewed_cells=reviewed, freed_label_cells=freed)
        narrowed.append({'id': row['id'], 'reviewed_cells': len(reviewed),
                         'freed_label_cells': len(freed), 'automatic_delivery': False})
    frozen_json(output/'decisions.json', {'scope_inputs': len(decisions), 'decisions': decisions})
    frozen_json(output/'observations.json', derived)
    report = {**recipe, 'scope_inputs': len(decisions), 'narrowed_images': len(narrowed),
        'freed_label_cells': sum(row['freed_label_cells'] for row in narrowed),
        'remaining_local_review_cells': sum(row['reviewed_cells'] for row in narrowed),
        'narrowed': narrowed, 'decisions': bound(output/'decisions.json'),
        'derived_observations': bound(output/'observations.json')}
    frozen_json(output/'result.json', report)
    print({key: report[key] for key in ('scope_inputs', 'narrowed_images',
        'freed_label_cells', 'remaining_local_review_cells')})


if __name__ == '__main__':
    main()
