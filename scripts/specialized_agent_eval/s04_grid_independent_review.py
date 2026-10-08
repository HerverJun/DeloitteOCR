"""Independently replay frozen pixel/anchor proofs and score unchanged candidates."""
from __future__ import annotations
import argparse
from copy import deepcopy
from pathlib import Path
import sys
from common import digest, fingerprint, fpath, frozen_json, read_json


def execute(root, output):
    root, output = fpath(root), fpath(output)
    summary_path = root / 'evidence/s04-grid-localization-replay-001.json'
    summary = read_json(summary_path)
    source_hashes = {}
    for entry in summary['sources']:
        frozen = fpath(entry['frozen'])
        if digest(frozen) != entry['sha256']:
            raise ValueError('Frozen diagnostic source changed')
        source_hashes[entry['source']] = entry['sha256']
    snapshot = root / 'source-snapshots/grid-localization-replay-001/ocr/src'
    sys.path.insert(0, str(snapshot))
    from ocr_workbench.screen_diagnostics import build_screen_diagnostics, compare_screen_candidates
    originals, computed, reports = {}, {}, {}
    receipt_root = root / 'evidence/s04-grid-localization-replay-001-receipts'
    for route in ('hunyuan', 'glm'):
        route_summary = next(entry for entry in summary['route_summaries'] if entry['route'] == route)
        replay_path = fpath(route_summary['observations'])
        if digest(replay_path) != route_summary['sha256']:
            raise ValueError('Diagnostic observation file changed')
        replay = read_json(replay_path)
        source = fpath(replay['offline_grid_replay']['source_observations'])
        if digest(source) != summary['input_observations_sha256'][str(source)]:
            raise ValueError('Actual route source changed')
        records = {entry['id']: entry for entry in read_json(source)['records']}
        originals[route] = records; computed[route] = {}
        cells = localized = 0
        for observed in replay['records']:
            record = records[observed['id']]
            for key, value in record.items():
                if key not in {'review_regions', 'issues'} and observed.get(key) != value:
                    raise ValueError('Position replay changed actual content/delivery/source field: ' + key)
            raw = {**deepcopy(record['candidate']['original']), **deepcopy(record['candidate']['edited'])}
            preparation = raw['screen']['prepared']
            auxiliary = raw['screen']['business_metadata_evidence']['auxiliary_evidence']
            receipt_path = receipt_root / (route + '-' + record['id'] + '.json')
            receipt = read_json(receipt_path)
            if (digest(fpath(preparation['input_path'])) != record['image_sha256'] or
                    preparation['input_sha256'] != raw['screen']['input_sha256'] or
                    raw['screen']['input_sha256'] != record['image_sha256']):
                raise ValueError('Actual input byte identity differs')
            expected = {'source_record_sha256': fingerprint(record), 'candidate_sha256': fingerprint(record['candidate']),
                'preparation_sha256': fingerprint(preparation), 'auxiliary_sha256': fingerprint(auxiliary)}
            if any(receipt[key] != value for key, value in expected.items()) or receipt['source_files_sha256'] != source_hashes:
                raise ValueError('Actual candidate/preparation/auxiliary/frozen source binding differs')
            before = deepcopy((raw, preparation, auxiliary))
            diagnostics = build_screen_diagnostics(raw, preparation, auxiliary)
            if diagnostics != receipt['diagnostics'] or (raw, preparation, auxiliary) != before:
                raise ValueError('Actual source-pixel/anchor proof cannot be exactly reproduced')
            if digest(receipt_path) != observed['offline_grid_diagnostics']['receipt_sha256']:
                raise ValueError('Observed positions do not bind the actual diagnostic receipt')
            cell_items = [entry for entry in diagnostics['items'] if entry['location']['level'] == 'cell']
            regions = [entry for entry in observed['review_regions'] if entry['scope'] == 'cell']
            if len(cell_items) != len(regions):
                raise ValueError('Reported reliable positions differ from actual reproduced proof')
            for item, region in zip(cell_items, regions):
                if region['observed_location'] != item['location']:
                    raise ValueError('A reliable flag is not backed by the actual full-cell geometry')
            computed[route][record['id']] = (raw, preparation, auxiliary)
            cells += diagnostics['grid_localization']['cell_count']; localized += len(cell_items)
        reports[route] = {'independently_reproduced_records': len(records),
                         'source_pixel_anchor_cells': cells, 'localized_issue_items': localized}
    paired_path = fpath(summary['comparison_observations'])
    if digest(paired_path) != summary['comparison_observations_sha256']:
        raise ValueError('Paired observation file changed')
    for record in read_json(paired_path)['records']:
        sid = record['id']; h, preparation, auxiliary = deepcopy(computed['hunyuan'][sid]); g = deepcopy(computed['glm'][sid][0])
        receipt_path = receipt_root / ('paired-' + sid + '.json'); receipt = read_json(receipt_path)
        versions = {'hunyuan': h.get('project_image_version'), 'glm': g.get('project_image_version')}
        if versions != receipt['original_native_versions'] or h['screen']['input_sha256'] != g['screen']['input_sha256']:
            raise ValueError('Paired canonicalization conceals a different original native identity/pixel source')
        canonical = 'pixel-sha256:' + record['image_sha256']
        h['project_image_version'] = g['project_image_version'] = canonical
        auxiliary['provenance']['image_version'] = canonical
        comparison = compare_screen_candidates(h, g, preparation, auxiliary,
                                               current_id='hunyuan-native', alternative_id='glm-native')
        if comparison != receipt['comparison'] or receipt['canonicalization_applied_to_native']:
            raise ValueError('Explicit offline same-pixel comparison cannot be reproduced')
        if record['candidate'] != originals['hunyuan'][sid]['candidate']:
            raise ValueError('Paired comparison changed the selected native candidate')
        if digest(receipt_path) != record['offline_grid_comparison']['receipt_sha256']:
            raise ValueError('Paired observation is not bound to its actual receipt')
    # Reference truth is read only after all frozen, source-only proofs have
    # been reproduced; it is not passed to diagnostics or any model.
    from quality import score_files
    truth = root / 'evaluation/dev-probe-12-scoring/truth.json'
    for route in ('hunyuan', 'glm', 'hunyuan-glm'):
        stem = route + '-native-dev-001-grid-' + ('comparison' if route == 'hunyuan-glm' else 'localization') + '-replay-001'
        scored = score_files(truth, root / ('evaluation/' + stem + '-observations.json'))
        published = read_json(root / ('evaluation/' + stem + '-quality.json'))
        if scored != published:
            raise ValueError('Independent fixed-denominator content scoring differs')
        totals = scored['summary']['totals']
        reports.setdefault(route, {}).update({key: totals[key] for key in (
            'visible_error_cells', 'local_review_cells', 'local_error_hits', 'local_false_flags',
            'review_images', 'review_reference_cells', 'automatic_images', 'human_confirmed_images')})
        reports[route]['local_errors_not_hit'] = totals['visible_error_cells'] - totals['local_error_hits']
    frozen_json(output, {'format': 'independent-source-pixel-grid-localization-development-review-v1',
        'status': 'pass', 'summary_source': str(summary_path), 'summary_sha256': digest(summary_path),
        'frozen_sources': source_hashes, 'actual_pixel_candidate_auxiliary_bindings_verified': True,
        'all_frozen_diagnostic_proofs_exactly_reproduced': True,
        'offline_same_pixel_comparison_reproduced': True, 'native_ids_preserved': True,
        'candidate_values_and_delivery_flags_unchanged': True, 'reports': reports,
        'net_value_repairs': 0, 'human_workload_reduction_demonstrated': False,
        'new_native_model_calls': 0, 'new_excel_exports': 0,
        'scope': 'Independent CPU replay of frozen development geometry/disagreements only; all 12 images remain full review, not new native inference or adoption qualification'})
    print(reports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args(); execute(args.root, args.output)


if __name__ == '__main__':
    main()
