"""Independent development-only pixel/geometry/content review of reuse proposals.

The source-only proposer has already finished. Reference content is read only
here; shared OCR errors remain errors and no confirmation is fabricated.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment


def require(value, message):
    if not value: raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('probe', 'coordinate-review', 'truth', 'output'): parser.add_argument('--' + name, required=True)
    args = parser.parse_args(); require_environment()
    probe, geometry_path, truth_path, output = map(fpath, (args.probe, args.coordinate_review, args.truth, args.output))
    require(not output.exists() and truth_path.parent.name == 'development', 'Use a new development-only review identity')
    report = read_json(probe / 'report.json')
    require(bound(fpath(report['helper']['path'])) == report['helper'] and
            bound(fpath(report['replay']['path'])) == report['replay'], 'Proposed rule or replay changed')
    replay = read_json(fpath(report['replay']['path']))
    inputs = read_json(fpath(replay['inputs']['path']))
    samples = {row['id']: row for row in inputs['samples']}
    observations = read_json(fpath(replay['observations']['path']))
    actual = {row['id']: row for row in observations['records']}
    truth, freeze = read_json(truth_path), read_json(truth_path.parent / 'freeze.json')
    require(digest(truth_path) == freeze['truth_sha256'] and digest(fpath(replay['inputs']['path'])) == freeze['inputs_sha256'],
            'Development reference freeze differs')
    wanted = {row['id']: row for row in truth['samples']}
    require(samples.keys() == actual.keys() == wanted.keys() and len(samples) == report['counts']['inputs'], 'Full scope differs')
    geometry = read_json(geometry_path)
    require(geometry['replay'] == report['replay'], 'Independent coordinate review belongs to another replay')
    coordinates = {(row['id'], row['table'], row['row'], row['column']): row for row in geometry['cells']}
    from PIL import Image
    from quality import match_tables
    counts, groups, errors = Counter(), [], []
    normal = lambda value: ''.join(unicodedata.normalize('NFC', str(value)).split())
    for group in report['candidate_groups']:
        members, crops = group['members'], []
        require(len(members) >= 2 and len({row['source_sha256'] for row in members}) >= 2, 'Group is not shared across source images')
        for row in members:
            sid = row['id']; counts['occurrences'] += 1
            require(row['source_sha256'] == wanted[sid]['image_sha256'] == digest(samples[sid]['image']), 'Original source changed')
            coordinate = coordinates[(sid, row['table'], row['row'], row['column'])]
            require(coordinate['correct'] and coordinate['reference_semantics'] == 'full_cell'
                    and coordinate['source_polygon'] == row['source_polygon'], 'Proposed cell lacks independent full-cell geometry')
            polygon = row['source_polygon']
            expected_box = [math.floor(min(point[0] for point in polygon)), math.floor(min(point[1] for point in polygon)),
                            math.ceil(max(point[0] for point in polygon)), math.ceil(max(point[1] for point in polygon))]
            require(expected_box == row['crop_box'], 'Source crop boundaries differ')
            with Image.open(samples[sid]['image']) as opened:
                crop = opened.convert('RGB').crop(tuple(expected_box))
                value = (crop.width, crop.height, crop.tobytes())
                require(value[:2] == (row['crop_width'], row['crop_height']) and
                        hashlib.sha256(value[2]).hexdigest() == row['decoded_rgb_sha256'], 'Raw source crop does not match')
                crops.append(value)
            candidate = actual[sid]['candidate']
            tables = candidate.get('edited', {}).get('tables', candidate['original']['tables'])
            cells = {(cell['row'], cell['column']): cell for cell in tables[row['table']]['cells']}
            cell = cells[(row['row'], row['column'])]
            require(cell['text'] == row['literal'] and actual[sid]['result_id'] == row['result_id'] and
                    actual[sid]['result_revision'] == row['result_revision'], 'Proposed literal or revision differs')
            pairs, _ = match_tables(wanted[sid]['tables'], tables)
            expected_table = next(expected for _, expected, index, _ in pairs if index == row['table'])
            reference = next(cell for cell in expected_table['cells'] if (cell['row'], cell['column']) == (row['row'], row['column']))
            critical = 'critical' in reference.get('tags', []) or bool(re.search(r'\d', reference['text']))
            exact = normal(row['literal']) == normal(reference['text'])
            counts['exact_occurrences'] += exact
            counts['shared_error_occurrences'] += not exact
            counts['shared_critical_error_occurrences'] += not exact and critical
            if not exact:
                errors.append({'id': sid, 'table': row['table'], 'row': row['row'], 'column': row['column'],
                               'actual': row['literal'], 'expected': reference['text'], 'critical': critical})
        require(all(value == crops[0] for value in crops), 'Group members are not byte-identical decoded pixels')
        require(len({(row['literal'], row['row_span'], row['column_span']) for row in members}) == 1, 'Group literals/spans disagree')
        counts['groups'] += 1; counts['repeated_checks_after_explicit_confirmation'] += len(members) - 1
        groups.append({'group_id': group['group_id'], 'occurrences': len(members), 'exact_source_pixels_verified': True,
                       'full_cell_coordinates_verified': True, 'human_confirmation_observed': False})
    require(counts['repeated_checks_after_explicit_confirmation'] == report['potential_repeated_checks_after_explicit_confirmation'],
            'Potential count differs')
    output.mkdir(parents=True)
    result = {'format': 'independent-exact-cell-confirmation-development-review-v1',
        'status': 'passed_pixel_and_coordinate_feasibility_only', 'probe': bound(probe / 'report.json'),
        'helper': bound(__file__), 'coordinate_review': bound(geometry_path), 'truth': bound(truth_path),
        'counts': dict(counts), 'groups': groups, 'shared_ocr_errors_preserved': errors,
        'automatic_reuse_rejected': bool(errors), 'requires_explicit_user_confirmation_or_correction': True,
        'human_confirmations_observed': 0, 'actual_review_scope_reduction': 0,
        'product_values_or_review_changed': False, 'model_calls': 0, 'agent_calls': 0,
        'validation_or_holdout_read': False, 'review_workload_reduction_qualified': False, 'adoption_qualified': False}
    frozen_json(output / 'review.json', result)
    print(json.dumps({'status': result['status'], 'counts': result['counts']}, ensure_ascii=False))


if __name__ == '__main__': main()
