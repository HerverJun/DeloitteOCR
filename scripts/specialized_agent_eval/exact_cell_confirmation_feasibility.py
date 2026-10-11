"""Source-only development probe for opt-in reuse of explicit cell confirmation.

Exact decoded pixels, dimensions and literal must all agree across different
source images. No fuzzy matching, inferred header role, automatic acceptance,
value replacement or hypothetical human confirmation is allowed.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path
import sys

from common import digest, fingerprint, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment


def axis_box(polygon):
    if not isinstance(polygon, list) or len(polygon) != 4: return None
    try:
        points = [[float(x), float(y)] for x, y in polygon]
        if not all(math.isfinite(value) for point in points for value in point): return None
        xs, ys = sorted(set(p[0] for p in points)), sorted(set(p[1] for p in points))
        if len(xs) != 2 or len(ys) != 2 or set(map(tuple, points)) != {(x, y) for x in xs for y in ys}: return None
        return [math.floor(xs[0]), math.floor(ys[0]), math.ceil(xs[1]), math.ceil(ys[1])]
    except (ValueError, TypeError): return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(); require_environment()
    replay, output = map(fpath, (args.replay, args.output))
    if output.exists(): raise ValueError('Use a new immutable development-only feasibility identity')
    receipt = read_json(replay / 'result.json')
    inputs_path, observations_path = map(lambda key: fpath(receipt[key]['path']), ('inputs', 'observations'))
    if inputs_path.parent.name != 'development': raise ValueError('Only development inputs are permitted')
    for key in ('inputs', 'observations', 'decisions', 'policy'):
        if bound(fpath(receipt[key]['path'])) != receipt[key]: raise ValueError('Bound source replay changed: ' + key)
    inputs, observations = read_json(inputs_path), read_json(observations_path)
    samples = {row['id']: row for row in inputs['samples']}
    records = {row['id']: row for row in observations['records']}
    decisions = read_json(replay / 'decisions.json')['decisions']
    if (inputs['truth_available_to_inference'] is not False or observations['inputs_sha256'] != digest(inputs_path)
            or samples.keys() != records.keys() or samples.keys() != {row['id'] for row in decisions}
            or len(samples) != len(inputs['samples']) or len(records) != len(observations['records'])):
        raise ValueError('Full development denominator differs')
    output.mkdir(parents=True)
    helper = Path(__file__).read_bytes()
    (output / 'executed-helper.py').write_bytes(helper)
    def audit(event, arguments):
        if event in {'subprocess.Popen', 'socket.connect', 'socket.getaddrinfo', 'os.system'}:
            raise ValueError('CPU feasibility cannot invoke models or network')
        if event != 'open' or not isinstance(arguments[0], (str, bytes, os.PathLike)): return
        path = Path(os.fsdecode(arguments[0])).absolute()
        if 'truth' in path.name.lower() or any(part in {'validation', 'holdout', 'challenge'} for part in path.parts):
            raise ValueError('Reference and non-development inputs forbidden')
        mode, flags = arguments[1:3]
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+') or isinstance(flags, int)
                   and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writing and not path.resolve().is_relative_to(output): raise ValueError('Write escaped isolated output')
    sys.addaudithook(audit)
    from PIL import Image
    groups, rejected, counts = defaultdict(list), [], Counter()
    for decision in decisions:
        sid = decision['id']; sample, record = samples[sid], records[sid]
        image = fpath(sample['image'])
        if digest(image) != sample['image_sha256'] or record['image_sha256'] != sample['image_sha256']:
            raise ValueError('Source pixels changed')
        counts['inputs'] += 1
        if not decision['verified']: continue
        if decision['source_sha256'] != sample['image_sha256']: raise ValueError('Grid source binding differs')
        raw = record['candidate']['original']
        tables = record['candidate'].get('edited', {}).get('tables', raw['tables'])
        if decision['candidate_table_sha256'] != fingerprint(tables): raise ValueError('Grid result binding differs')
        with Image.open(image) as opened:
            pixels = opened.convert('RGB')
            for table in decision['proof']['tables']:
                if table['status'] != 'verified': continue
                actual = {(cell['row'], cell['column'], cell.get('row_span', 1), cell.get('column_span', 1)): cell
                          for cell in tables[table['table']]['cells']}
                for geometry in table['cells']:
                    counts['observed_cells'] += 1
                    if geometry.get('range_semantics') != 'full_cell': continue
                    polygon = geometry.get('source_polygon', geometry.get('polygon'))
                    box = axis_box(polygon)
                    if (not box or box[0] < 0 or box[1] < 0 or box[2] > pixels.width or box[3] > pixels.height
                            or box[0] >= box[2] or box[1] >= box[3]):
                        counts['nonaxis_or_invalid_source_polygons'] += 1; continue
                    key = (geometry['row'], geometry['column'], geometry.get('row_span', 1), geometry.get('column_span', 1))
                    cell = actual[key]
                    if not isinstance(cell['text'], str) or not cell['text'].strip():
                        counts['empty_cells_excluded'] += 1; continue
                    crop = pixels.crop(tuple(box))
                    pixel_sha = hashlib.sha256(crop.tobytes()).hexdigest()
                    grouping = (crop.width, crop.height, pixel_sha)
                    groups[grouping].append({'id': sid, 'input_version_id': record['input_version_id'],
                        'source_sha256': record['image_sha256'], 'result_id': record['result_id'],
                        'result_revision': record['result_revision'], 'table': table['table'], 'row': cell['row'],
                        'column': cell['column'], 'row_span': key[2], 'column_span': key[3],
                        'literal': cell['text'], 'source_polygon': deepcopy(polygon), 'crop_box': box,
                        'crop_width': crop.width, 'crop_height': crop.height, 'decoded_rgb_sha256': pixel_sha,
                        'geometry_basis': geometry.get('basis')})
                    counts['exact_source_crops'] += 1
    candidates = []
    for (width, height, sha), members in groups.items():
        if len({row['source_sha256'] for row in members}) < 2: continue
        if len({(row['literal'], row['row_span'], row['column_span']) for row in members}) != 1:
            rejected.append({'reason': 'same_pixels_conflicting_literal_or_span', 'members': members}); continue
        candidates.append({'group_id': fingerprint([width, height, sha, members[0]['literal']]),
            'members': members, 'potential_repeated_checks_after_one_explicit_confirmation': len(members) - 1})
    report = {'format': 'exact-source-cell-confirmation-feasibility-v1', 'status': 'source_only_feasibility_complete',
        'replay': bound(replay / 'result.json'), 'helper': bound(output / 'executed-helper.py'),
        'counts': dict(counts), 'candidate_groups': candidates, 'conflicting_groups_rejected': rejected,
        'potential_repeated_checks_after_explicit_confirmation': sum(row['potential_repeated_checks_after_one_explicit_confirmation'] for row in candidates),
        'human_confirmation_required_before_any_reuse': True, 'human_confirmations_observed': 0,
        'whole_cell_geometry_needs_independent_coordinate_review': True,
        'source_pixel_reads': len(samples), 'truth_read': False, 'validation_or_holdout_read': False,
        'model_calls': 0, 'agent_calls': 0, 'product_values_or_review_changed': False,
        'review_workload_reduction_qualified': False, 'adoption_qualified': False}
    frozen_json(output / 'report.json', report)
    print({key: report[key] for key in ('status', 'counts', 'potential_repeated_checks_after_explicit_confirmation')})


if __name__ == '__main__': main()
