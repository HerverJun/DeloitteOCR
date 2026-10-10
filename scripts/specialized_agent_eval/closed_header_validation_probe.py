"""Apply the unchanged development-frozen header policy to validation40.

Only the split adapter is new. It cannot tune labels, spans, geometry,
confidence or source proof. The rule phase forbids truth/holdout reads.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

from common import digest, frozen_json, read_json
from controlled_recovery import bound, plain_path, require_environment


def run(args):
    require_environment()
    capsule, observations_path, inputs_path, source, output = map(plain_path,
        (args.policy, args.observations, args.inputs, args.source, args.output))
    if output.exists() or inputs_path.parent.name != 'validation':
        raise ValueError('New validation40-only probe identity required')
    frozen, policy = read_json(capsule / 'freeze.json'), read_json(capsule / 'policy.json')
    if bound(capsule / 'policy.json') != frozen['policy']:
        raise ValueError('Original frozen development policy changed')
    for entry in frozen['helpers'].values():
        if bound(entry['path']) != entry:
            raise ValueError('Frozen rule/helper bytes changed')
    sys.path.insert(0, str(capsule / 'helper-source'))
    from closed_header_review_scope_probe import header_scope, no_reference
    from strict_review_subset_probe import decide
    from native_diagnostics import diagnostic_source
    observations, inputs = read_json(observations_path), read_json(inputs_path)
    no_reference(observations)
    no_reference(inputs)
    samples = {row['id']: row for row in inputs['samples']}
    records = {row['id']: row for row in observations['records']}
    if (inputs.get('truth_available_to_inference') is not False
            or observations.get('truth_read') is not False
            or observations['inputs_sha256'] != digest(inputs_path)
            or len(samples) != 40 or len(records) != 40 or samples.keys() != records.keys()):
        raise ValueError('Validation source denominator/truth boundary differs')
    for identity, sample in samples.items():
        if digest(sample['image']) != sample['image_sha256'] or records[identity]['image_sha256'] != sample['image_sha256']:
            raise ValueError('Frozen validation source changed')
    source_before = {str(path): digest(path) for path in
                    (source / 'src/ocr_workbench').glob('*.py')}
    output.mkdir(parents=True)
    pixels = []
    def audit(event, values):
        if event in {'subprocess.Popen', 'socket.connect', 'socket.getaddrinfo', 'os.system'}:
            raise ValueError('CPU rule cannot invoke model or network')
        if event != 'open' or not isinstance(values[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(values[0])).absolute()
        mode, flags = values[1:3]
        writing = (isinstance(mode, str) and any(char in mode for char in 'wax+')
                   or isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writing and not path.resolve().is_relative_to(output):
            raise ValueError('Rule write escaped its independent F: output')
        if 'truth' in path.name.lower() or any(part in {'holdout', 'challenge'} for part in path.parts):
            raise ValueError('Reference/final split forbidden during rule execution')
        frame = sys._getframe(1)
        while frame:
            if Path(frame.f_code.co_filename).name == 'screen_diagnostics.py' and path.suffix.lower() == '.png':
                pixels.append(str(plain_path(path)))
                break
            frame = frame.f_back
    sys.addaudithook(audit)
    with diagnostic_source(source) as module:
        decisions = [header_scope(row, decide(row, module), policy) for row in observations['records']]
    derived = deepcopy(observations)
    by_id = {row['id']: row for row in derived['records']}
    for decision in decisions:
        if not decision['eligible']:
            continue
        row = by_id[decision['id']]
        row.update(automatic_delivery=False, review_image=False, human_confirmed=False,
                   review_required=True, requires_human_review=True)
        row['review_regions'] = [{'scope': 'cell', 'table_index': cell['table'],
            'row': cell['row'], 'column': cell['column'], 'reliable': True,
            'evidence_ref': {'policy_sha256': frozen['policy']['sha256'],
                'source_sha256': row['image_sha256'], 'input_version_id': row['input_version_id'],
                'result_id': row['result_id'], 'result_revision': row['result_revision'],
                'box': cell['box'], 'auxiliary_text_indices': cell['evidence_indices']}}
            for cell in decision['reviewed_cells']]
    if any(row['candidate'] != records[row['id']]['candidate'] for row in derived['records']):
        raise ValueError('Rule altered recognition candidate values')
    if any(digest(path) != sha for path, sha in source_before.items()):
        raise ValueError('Native diagnostic source changed')
    derived['evidence_kind'] = 'validation_cpu_frozen_closed_header_rule'
    derived['development_policy_sha256'] = frozen['policy']['sha256']
    frozen_json(output / 'decisions.json', {'scope_inputs': 40, 'decisions': decisions})
    frozen_json(output / 'observations.json', derived)
    report = {'format': 'm2-frozen-closed-header-validation40-cpu-v1',
        'scope_inputs': 40, 'development_policy': frozen['policy'],
        'original_observations': bound(observations_path), 'inputs': bound(inputs_path),
        'derived_observations': bound(output / 'observations.json'),
        'decisions': bound(output / 'decisions.json'), 'helper': bound(__file__),
        'narrowed_images': sum(row['eligible'] for row in decisions),
        'released_header_cells': sum(len(row['freed_fields']) for row in decisions),
        'rejection_reasons': dict(Counter(reason for row in decisions for reason in row['reasons'])),
        'actual_source_pixel_reads': pixels, 'candidate_values_unchanged': True,
        'policy_labels_geometry_confidence_unchanged': True, 'truth_read': False,
        'holdout_read': False, 'gpu_model_calls': 0, 'native_review_state_changed': False,
        'human_time_measured': False, 'adoption_qualified': False}
    frozen_json(output / 'result.json', report)
    print({key: report[key] for key in ('scope_inputs', 'narrowed_images', 'released_header_cells')})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('policy', 'observations', 'inputs', 'source', 'output'):
        parser.add_argument('--' + name, required=True)
    run(parser.parse_args())
