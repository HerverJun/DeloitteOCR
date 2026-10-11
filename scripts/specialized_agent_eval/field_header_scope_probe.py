"""Explore cell review for closed business headers with independent pixel proof.

This replaces neither the rejected seven-field experiment nor native review
state. Metadata and all data values remain reviewed. Validation was previously
exposed; this exploration still needs fresh independent/native qualification.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import importlib.util
import math
import os
from pathlib import Path
import sys

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment
from native_diagnostics import diagnostic_source


LABELS = ('编号', '序号', '合同', '物料', '凭证', '仓位', '流水', '账号', '账户', '代码',
          '部门', '项目', '科目', '日期', '金额', '数量', '单价', '余额', '账面', '盘点',
          '损溢', '税额', '税率', '差额', '约定', '已付', '付款期', '报销事项', '经办核对',
          '财务核对', '原报', '核准', '处理')


def propose(record, module, policy):
    answer = {'id': record['id'], 'eligible': False, 'released': [], 'reviewed': [], 'reasons': []}
    if record['status'] != 'success':
        answer['reasons'] = ['no_successful_candidate']
        return answer
    raw = deepcopy(record['candidate']['original'])
    raw['tables'] = deepcopy(record['candidate'].get('edited', {}).get('tables', raw.get('tables', [])))
    screen = raw.get('screen', {})
    prep = screen.get('prepared') or {}
    metadata = screen.get('business_metadata_evidence') or {}
    evidence = metadata.get('auxiliary_evidence') or {}
    if (record['image_sha256'] != prep.get('input_sha256')
            or record['image_sha256'] != screen.get('input_sha256')
            or raw.get('project_image_version') != record.get('input_version_id')
            or evidence.get('provenance', {}).get('image_version') != record.get('input_version_id')
            or evidence.get('provenance', {}).get('contains_reference_input') is not False
            or metadata.get('auxiliary_binding_verified') is not True):
        answer['reasons'] = ['unverified_source_or_auxiliary_binding']
        return answer
    if (prep.get('quality', {}).get('unreadable')
            or any(flag.get('severity') in {'medium', 'high'} for flag in prep.get('quality', {}).get('flags', []))
            or any(w.get('code') in {'region_limit', 'roi_uncertain'} for w in prep.get('warnings', []))):
        answer['reasons'] = ['source_quality_or_region_scope_uncertain']
        return answer
    grid = module.observe_screen_grid_cells(raw, prep, evidence)
    answer['grid'] = grid
    tables = raw['tables']
    proofs = {part['table']: part for part in grid['tables'] if part['status'] == 'verified'}
    if not tables or len(proofs) != len(tables) or len(tables) != len(prep.get('business_regions', [])):
        answer['reasons'] = ['complete_independent_table_partition_unavailable']
        return answer
    labels = set(policy['closed_labels'])
    for ti, table in enumerate(tables):
        first = [cell for cell in table['cells'] if cell['row'] == 0]
        covered = [c for cell in first for c in range(cell['column'], cell['column'] + cell.get('column_span', 1))]
        header_role = (table['rows'] >= 3 and table['columns'] >= 3 and len(first) >= 3
            and len({cell['text'] for cell in first}) == len(first)
            and all(cell['text'] in labels for cell in first)
            and sorted(covered) == list(range(table['columns']))
            and max(cell.get('row_span', 1) for cell in first) < table['rows'])
        geometries = {(cell['row'], cell['column']): cell for cell in proofs[ti]['cells']}
        for cell in table['cells']:
            box = geometries[cell['row'], cell['column']]['box']
            entry = {'table': ti, 'row': cell['row'], 'column': cell['column'], 'box': box,
                     'row_span': cell.get('row_span', 1), 'column_span': cell.get('column_span', 1)}
            matches = []
            for index, item in enumerate(evidence.get('text', [])):
                rb = module.observed_box(item)
                if rb is None or item.get('kind') in {'toolbar', 'taskbar', 'formula_bar', 'ruler', 'window_border', 'scrollbar', 'ui'}:
                    continue
                intersection = max(0, min(rb[2], box[2]) - max(rb[0], box[0])) * max(0, min(rb[3], box[3]) - max(rb[1], box[1]))
                if intersection / ((rb[2] - rb[0]) * (rb[3] - rb[1])) >= .9:
                    matches.append((index, item))
            reading = matches[0][1] if len(matches) == 1 else {}
            confidence = reading.get('confidence', reading.get('score'))
            supported = (header_role and cell['row'] == 0 and cell['text'] in labels
                and len(matches) == 1 and reading.get('text') == cell['text']
                and str(reading.get('source', '')).startswith('PP-OCR')
                and type(confidence) in (int, float) and math.isfinite(confidence)
                and confidence >= policy['minimum_auxiliary_confidence'])
            if supported:
                answer['released'].append({**entry, 'literal': cell['text'], 'evidence_indices': [matches[0][0]],
                    'role': 'closed_field_in_complete_first_header_row', 'confidence': confidence})
            else:
                answer['reviewed'].append(entry)
    answer['eligible'] = bool(answer['released'] and answer['reviewed'])
    if not answer['eligible']:
        answer['reasons'].append('no_verified_closed_header_subset')
    return answer


def freeze(args):
    output = fpath(args.output)
    if output.exists():
        raise ValueError('New immutable rule identity required')
    output.mkdir(parents=True)
    target = output / 'rule.py'
    target.write_bytes(Path(__file__).read_bytes())
    frozen_json(output / 'policy.json', {'format': 'closed-first-header-scope-exploration-v2',
        'rule': bound(target), 'closed_labels': LABELS, 'minimum_auxiliary_confidence': .99,
        'header_role': 'Complete observed first row, >=3 distinct closed field labels, >=3 columns and later data rows; exact native topology/pixels/independent anchors.',
        'always_review': 'Every data value, unknown header, metadata and unsupported source; no numeric or name/identifier content release.',
        'literal_comparison': 'Exact Unicode string, no normalization',
        'validation_previously_exposed': True, 'development_informed': True,
        'previous_seven_field_rule_adopted': False, 'holdout_read': False,
        'human_time_measured': False, 'native_state_changed': False, 'adoption_qualified': False})
    print({'policy_sha256': digest(output / 'policy.json'), 'labels': len(LABELS)})


def run(args):
    capsule, source, observations_path, inputs_path, output = map(fpath,
        (args.capsule, args.source, args.observations, args.inputs, args.output))
    if output.exists() or inputs_path.parent.name not in {'development', 'validation'}:
        raise ValueError('New development/validation-only identity required')
    policy = read_json(capsule / 'policy.json')
    if bound(capsule / 'rule.py') != policy['rule']:
        raise ValueError('Frozen scope rule changed')
    original, inputs = read_json(observations_path), read_json(inputs_path)
    samples = {row['id']: row for row in inputs['samples']}
    records = {row['id']: row for row in original['records']}
    if (original['inputs_sha256'] != digest(inputs_path) or inputs.get('truth_available_to_inference') is not False
            or len(samples) != len(inputs['samples']) or len(records) != len(original['records']) or samples.keys() != records.keys()):
        raise ValueError('Full truth-free input denominator differs')
    for sid, row in records.items():
        if digest(samples[sid]['image']) != row['image_sha256'] or samples[sid]['image_sha256'] != row['image_sha256']:
            raise ValueError('Source pixel identity changed')
    output.mkdir(parents=True)
    def audit(event, values):
        if event in {'subprocess.Popen', 'socket.connect', 'socket.getaddrinfo', 'os.system'}:
            raise ValueError('CPU scope rule cannot invoke model/network')
        if event != 'open' or not isinstance(values[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(values[0])).absolute()
        mode, flags = values[1:3]
        writing = (isinstance(mode, str) and any(ch in mode for ch in 'wax+')
            or isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writing and not path.resolve().is_relative_to(output):
            raise ValueError('Scope rule write escaped owned F: output')
        if 'truth' in path.name.lower() or any(part in {'holdout', 'challenge'} for part in path.parts):
            raise ValueError('Scope rule cannot read reference/final split')
    sys.addaudithook(audit)
    spec = importlib.util.spec_from_file_location('frozen_field_scope_rule', capsule / 'rule.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with diagnostic_source(source) as native:
        decisions = [module.propose(row, native, policy) for row in original['records']]
    derived = deepcopy(original)
    by_id = {row['id']: row for row in derived['records']}
    for decision in decisions:
        if not decision['eligible']:
            continue
        row = by_id[decision['id']]
        row.update(review_image=False, review_required=True, requires_human_review=True,
                   automatic_delivery=False, human_confirmed=False)
        row['review_regions'] = [{'scope': 'cell', 'table_index': cell['table'], 'row': cell['row'],
            'column': cell['column'], 'reliable': True, 'reason': 'data_or_unverified_header_keeps_review',
            'evidence_ref': {'policy_sha256': digest(capsule / 'policy.json'),
                'source_sha256': row['image_sha256'], 'input_version_id': row['input_version_id'],
                'result_id': row['result_id'], 'result_revision': row['result_revision'], 'box': cell['box']}}
            for cell in decision['reviewed']]
        row['review_scope_proposal'] = {'released_headers': decision['released'],
            'business_metadata_review_required': True, 'native_review_state_changed': False,
            'adoption_qualified': False, 'validation_previously_exposed': True}
    if any(row['candidate'] != records[row['id']]['candidate'] for row in derived['records']):
        raise ValueError('Scope rule changed a value')
    derived.update(evidence_kind='cpu_closed_first_header_scope_exploration', truth_read=False)
    frozen_json(output / 'decisions.json', {'scope_inputs': len(records), 'decisions': decisions})
    frozen_json(output / 'observations.json', derived)
    result = {'format': 'closed-first-header-scope-exploration-v2', 'scope_inputs': len(records),
        'policy': bound(capsule / 'policy.json'), 'original_observations': bound(observations_path),
        'inputs': bound(inputs_path), 'derived_observations': bound(output / 'observations.json'),
        'narrowed_images': sum(row['eligible'] for row in decisions),
        'released_header_cells': sum(len(row['released']) for row in decisions if row['eligible']),
        'rejection_reasons': dict(Counter(reason for row in decisions for reason in row['reasons'])),
        'values_unchanged': True, 'metadata_and_all_data_values_remain_reviewed': True,
        'rule_truth_read': False, 'holdout_read': False, 'native_state_changed': False,
        'validation_previously_exposed': True, 'adoption_qualified': False}
    frozen_json(output / 'result.json', result)
    print({key: result[key] for key in ('scope_inputs', 'narrowed_images', 'released_header_cells')})


if __name__ == '__main__':
    require_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('freeze', 'run'))
    for name in ('capsule', 'source', 'observations', 'inputs', 'output'):
        parser.add_argument('--' + name)
    args = parser.parse_args()
    (freeze if args.phase == 'freeze' else run)(args)
