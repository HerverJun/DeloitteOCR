"""Independently check every released header and remaining error coverage.

Reference truth is confined to this post-rule process. Previously exposed
validation is exploration; this cannot authorize a native review policy.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from common import fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment
from quality import is_critical, key, match_tables, norm, paired_report, review_keys, score_files, span, tables


def main():
    require_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('replay', 'truth', 'content-baseline', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    replay, truth_path, content_baseline, output = map(fpath,
        (args.replay, args.truth, args.content_baseline, args.output))
    if output.exists() or truth_path.parent.name not in {'development', 'validation'}:
        raise ValueError('Use a new development/validation coverage identity')
    receipt = read_json(replay / 'result.json')
    observations = read_json(replay / 'observations.json')
    before_path = fpath(receipt['original_observations']['path'])
    original = read_json(before_path)
    if (bound(replay / 'observations.json') != receipt['derived_observations']
            or bound(before_path) != receipt['original_observations']
            or receipt['native_state_changed'] is not False):
        raise ValueError('Frozen replay/native boundary changed')
    truth = {row['id']: row for row in read_json(truth_path)['samples']}
    rows = {row['id']: row for row in observations['records']}
    originals = {row['id']: row for row in original['records']}
    decisions = {row['id']: row for row in read_json(replay / 'decisions.json')['decisions']}
    if not (truth.keys() == rows.keys() == originals.keys() == decisions.keys()):
        raise ValueError('Complete source denominator differs')
    counts, released, uncovered = Counter(), [], []
    for sid, row in rows.items():
        if row['candidate'] != originals[sid]['candidate']:
            raise ValueError('Scope rule altered a candidate')
        actual = tables(row['candidate'])
        review, invalid = review_keys(row, actual)
        if invalid:
            raise ValueError('Review scope has invalid regions')
        full = row.get('review_image') is True or any(r.get('scope') == 'image' for r in row.get('review_regions', []))
        pairs, _ = match_tables(truth[sid]['tables'], actual)
        fields = {(cell['table'], cell['row'], cell['column']): cell for cell in decisions[sid]['released']}
        for ti, expected, ai, got in pairs:
            found = {key(cell): cell for cell in got.get('cells', [])}
            for cell in expected['cells']:
                position = (ai, *key(cell))
                current = found.get(key(cell))
                covered = full or position in review
                exact = current is not None and norm(current['text']) == norm(cell['text']) and span(current) == span(cell)
                visible, critical = cell.get('visibility', 'visible') == 'visible', is_critical(cell)
                counts['reference_cells'] += 1
                counts['reviewed_reference_cells'] += covered
                if visible and not exact:
                    counts['visible_errors'] += 1
                    counts['reviewed_visible_errors'] += covered
                    counts['critical_errors'] += critical
                    counts['reviewed_critical_errors'] += covered and critical
                    if not covered:
                        uncovered.append({'id': sid, 'table': ti, 'row': cell['row'], 'column': cell['column'],
                                          'critical': critical, 'expected': cell['text'], 'actual': current['text'] if current else None})
                if not covered:
                    field = fields.get(position)
                    if not decisions[sid]['eligible'] or field is None or row.get('review_scope_proposal', {}).get('business_metadata_review_required') is not True:
                        raise ValueError('Release is not bound to a header/metadata review contract')
                    released.append({'id': sid, 'table': ti, 'row': cell['row'], 'column': cell['column'],
                        'expected': cell['text'], 'actual': current['text'] if current else None,
                        'exact': exact, 'critical': critical, 'role': field['role'], 'box': field['box']})
        counts['images'] += 1
    before, after, baseline = (score_files(truth_path, path) for path in
                               (before_path, replay / 'observations.json', content_baseline))
    bad_releases = [row for row in released if not row['exact'] or row['critical']]
    totals = after['summary']['totals']
    if (counts['reference_cells'] != totals['all_reference_cells']
            or counts['reviewed_reference_cells'] != totals['review_reference_cells']
            or counts['critical_errors'] != totals['visible_critical_error_cells']
            or counts['reviewed_critical_errors'] != totals['visible_critical_error_cells_reviewed']
            or len(released) != receipt['released_header_cells']):
        raise ValueError('Independent cell enumeration differs from the fixed scorer')
    output.mkdir(parents=True)
    frozen_json(output / 'quality.json', after)
    report = {'format': 'closed-first-header-independent-coverage-v2',
        'status': 'passed_exploration' if not uncovered and not bad_releases else 'rejected_exploration',
        'bindings': [bound(p) for p in (replay / 'result.json', replay / 'decisions.json', replay / 'observations.json',
                                       truth_path, content_baseline, Path(__file__))],
        'counts': dict(counts), 'released_headers': released, 'bad_releases': bad_releases,
        'uncovered_errors': uncovered, 'review_cells_reduced': len(released),
        'scope_only_paired': paired_report(before, after), 'combined_display_and_scope_paired': paired_report(baseline, after),
        'after': after['summary'], 'original_candidate_unchanged_by_scope_rule': True,
        'data_values_and_business_metadata_still_reviewed': True,
        'validation_previously_exposed': True, 'holdout_read': False,
        'native_state_changed': False, 'human_time_measured': False,
        'native_or_independent_new_validation_qualified': False, 'adoption_qualified': False}
    frozen_json(output / 'review.json', report)
    print({key: report[key] for key in ('status', 'counts', 'review_cells_reduced')})


if __name__ == '__main__':
    main()
