"""CPU-only, non-oracle comparison of frozen OCR candidates and fixed routes.

Selection receipts are written before offline truth is opened. Quality report
scores are never passed into routing: that report is only an artifact index.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screen_quality_report as quality_report

digest = quality_report.digest
fpath = quality_report.fpath

RECOVERY_BINDINGS = (
    'inputs_sha256', 'worker_source_sha256', 'engine', 'engine_config_sha256',
    'bundle', 'preprocess_module_sha256', 'request_timeout_seconds',
    'context_binding_sha256', 'context_run', 'context_source_policy',
    'context_worker_source_sha256', 'runner_sha256', 'first_pass',
    'labels_or_ideal_regions_provided',
)
PAIRED_BINDINGS = (
    'inputs_sha256', 'preprocess_module_sha256', 'context_binding_sha256',
    'context_run', 'context_source_policy', 'context_worker_source_sha256',
    'runner_sha256', 'worker_source_sha256', 'request_timeout_seconds',
    'bundle', 'labels_or_ideal_regions_provided',
)
FRAME_FIELDS = ('policy_version', 'input_sha256', 'width', 'height',
                'coordinate_space', 'quality', 'business_regions',
                'excluded_regions', 'warnings')


def read_json(path, expected_hash=None):
    path = fpath(path)
    content = path.read_bytes()
    if expected_hash and hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError('Frozen artifact hash changed: ' + str(path))
    return json.loads(content)


def artifact_fields(model):
    """An explicit allowlist removes all truth-derived report scores/examples."""
    keys = ('run', 'binding', 'binding_sha256', 'progress', 'is_final',
            'record_identities', 'production_v2_quality_replay')
    output = {key: deepcopy(model.get(key)) for key in keys}
    replay = output.get('production_v2_quality_replay')
    if replay:
        output['production_v2_quality_replay'] = {
            'source': replay['source'], 'rows': replay['rows']}
    recovery = model.get('source_input_incident_recovery')
    if recovery:
        output['source_input_incident_recovery'] = {key: deepcopy(recovery[key])
            for key in ('recovery_run', 'recovery_binding', 'recovery_binding_sha256',
                        'receipts', 'content_measurement_complete',
                        'source_input_failures_remaining')}
        replay = recovery.get('production_v2_quality_replay')
        if replay:
            output['source_input_incident_recovery']['production_v2_quality_replay'] = {
                'source': replay['source'], 'rows': deepcopy(replay['rows'])}
    return output


def checked_recovery(model, originals, samples, recovery_map):
    recovery = model.get('source_input_incident_recovery')
    if not recovery:
        return {}
    if not recovery['content_measurement_complete'] or recovery['source_input_failures_remaining']:
        raise ValueError('Source-input incident recovery is incomplete')
    runroot = fpath(recovery['recovery_run'])
    if runroot == fpath(model['run']):
        raise ValueError('Recovery must be an independent preserved run')
    binding = read_json(runroot / 'binding.json', recovery['recovery_binding_sha256'])
    if binding != recovery['recovery_binding'] or any(
            binding.get(key) != model['binding'].get(key) for key in RECOVERY_BINDINGS):
        raise ValueError('Recovery binding differs')
    spec = recovery_map or {}
    receipts = recovery['receipts']
    if (not spec or fpath(spec['run']) != runroot
            or set(spec['sample_ids']) != {row['id'] for row in receipts}
            or not set(spec['sample_ids']) <= set(binding.get('include_sample_ids', []))):
        raise ValueError('Recovery is not covered by the explicit bound receipt map')
    replacements = {}
    replay = {row['id']: row for row in recovery['production_v2_quality_replay']['rows']}
    for receipt in receipts:
        ident = receipt['id']
        source = originals[ident]
        if (not quality_report.source_input_failure(source, samples[ident])
                or fpath(receipt['first_pass_record']) != fpath(model['run']) / ident / 'record.json'
                or receipt['first_pass_record_sha256'] != digest(receipt['first_pass_record'])):
            raise ValueError('Recovery cannot replace this original attempt: ' + ident)
        if not receipt.get('substituted') or receipt.get('recovery_status') != 'success':
            raise ValueError('No successful recovery receipt: ' + ident)
        record_path = runroot / ident / 'record.json'
        if fpath(receipt['recovery_record']) != record_path:
            raise ValueError('Recovery record belongs to another run/image')
        record = read_json(record_path, receipt['recovery_record_sha256'])
        if record.get('status') != 'success' or any(record.get(key) != source.get(key)
                for key in ('id', 'image_sha256', 'engine', 'context_sha256', 'context_evidence_sha256')):
            raise ValueError('Recovery model/image/context identity differs')
        if (fpath(receipt['result']) != runroot / ident / 'result.json'
                or receipt['result_sha256'] != record.get('result_sha256')):
            raise ValueError('Recovery result identity differs')
        replacements[ident] = (runroot, binding, record,
                               receipt['recovery_record_sha256'], replay[ident])
    return replacements


def load_candidates(report_path, inputs_path, tools_root, production_source,
                    required_preprocess_sha256):
    """Verify actual result/context/preparation bindings; never read truth."""
    inputs = read_json(inputs_path)
    if inputs.get('truth_available_to_inference') is not False:
        raise ValueError('Inputs are not isolated from offline truth')
    samples = {row['id']: row for row in inputs['samples']}
    if len(samples) != len(inputs['samples']) or any(
            Path(ident).name != ident or ident in {'.', '..'} for ident in samples):
        raise ValueError('Input IDs must be unique safe directory names')
    report = read_json(report_path)
    if report.get('inputs_sha256') != digest(inputs_path):
        raise ValueError('Report input binding differs')
    runtime = quality_report.production_runtime(production_source, tools_root)
    quality = __import__('ocr_workbench.screen_quality', fromlist=['select_screen_candidates'])
    runtime.update(select=quality.select_screen_candidates, raw=quality._raw)
    recovery_map = {}
    if report.get('recovery_receipt_map'):
        meta = report['recovery_receipt_map']
        recovery_map = read_json(meta['path'], meta['sha256'])['models']
    models, provenance = {}, {}
    common = None
    for label, indexed in report['models'].items():
        model = artifact_fields(indexed)
        if not model['is_final'] or model['progress'].get('completed') != len(samples):
            raise ValueError('All paired model runs must be ended: ' + label)
        runroot = fpath(model['run'])
        binding = read_json(runroot / 'binding.json', model['binding_sha256'])
        if (binding != model['binding'] or binding.get('inputs_sha256') != digest(inputs_path)
                or binding.get('preprocess_module_sha256') != required_preprocess_sha256
                or binding.get('labels_or_ideal_regions_provided') is not False
                or not binding.get('first_pass')):
            raise ValueError('Frozen input/preparation/model binding differs: ' + label)
        paired = {key: binding.get(key) for key in PAIRED_BINDINGS}
        if common is not None and paired != common:
            raise ValueError('Models did not consume the same paired input/context/policy')
        common = paired
        identities = {row['id']: row for row in model['record_identities']}
        if len(identities) != len(samples) or set(identities) != set(samples):
            raise ValueError('Model does not cover the complete unique image cohort')
        originals = {ident: read_json(runroot / ident / 'record.json',
                                     identities[ident]['record_sha256']) for ident in samples}
        for ident, record in originals.items():
            if (record.get('id') != ident or record.get('image_sha256') != samples[ident]['image_sha256']
                    or record.get('engine') != binding['engine']
                    or record.get('status') not in {'success', 'failed'}
                    or record.get('status') != identities[ident]['status']
                    or record.get('result_sha256') != identities[ident].get('result_sha256')):
                raise ValueError('Original record identity differs: ' + ident)
        replacements = checked_recovery(model, originals, samples, recovery_map.get(label))
        replay_index = {row['id']: row for row in model['production_v2_quality_replay']['rows']}
        if model['production_v2_quality_replay']['source'] != runtime['identity']:
            raise ValueError('Production replay source changed: ' + label)
        if (replacements and model['source_input_incident_recovery']
                ['production_v2_quality_replay']['source'] != runtime['identity']):
            raise ValueError('Recovery production replay source changed: ' + label)
        rows = {}
        for ident, sample in samples.items():
            original = originals[ident]
            actual_root, actual_binding, record, record_hash, expected_replay = replacements.get(
                ident, (runroot, binding, original, identities[ident]['record_sha256'], replay_index.get(ident)))
            if quality_report.source_input_failure(record, sample):
                raise ValueError('Unrecovered source-input incident: ' + ident)
            if digest(fpath(sample['image'])) != sample['image_sha256']:
                raise ValueError('Source image changed: ' + ident)
            row = {'record': record, 'record_path': str(actual_root / ident / 'record.json'),
                   'record_sha256': record_hash, 'source_input_recovered': ident in replacements,
                   'result': None, 'preparation': None, 'evidence': None, 'quality': None}
            if record['status'] == 'success':
                raw = read_json(actual_root / ident / 'result.json', record['result_sha256'])
                replay = quality_report.replay_production_quality(record, raw, sample,
                    actual_binding, actual_root, runtime)
                if not expected_replay or replay != expected_replay:
                    raise ValueError('Actual production quality/preparation replay changed: ' + ident)
                preparation = read_json(replay['preparation_manifest'], replay['preparation_manifest_sha256'])
                _, context, _ = runtime['verify'](fpath(actual_binding['context_run']) / ident,
                                                sample, normalize=None)
                effective = runtime['reconcile'](runtime['normalize'](context),
                    width=preparation['width'], height=preparation['height'])
                row.update(result=raw, preparation=preparation,
                    evidence={'blocks': effective['text'], 'provenance': effective['provenance']},
                    quality=replay['effective_quality'], replay_identity={key: replay[key] for key in (
                        'preparation_manifest', 'preparation_manifest_sha256', 'context_sha256',
                        'stored_context_evidence_sha256', 'effective_evidence_sha256', 'result_sha256')})
            rows[ident] = row
        models[label] = rows
        provenance[label] = {'run': str(runroot), 'binding_sha256': model['binding_sha256'],
                            'first_pass_timing': quality_report.ended_run_timing(runroot, list(originals.values()))}
        if replacements:
            recovery = model['source_input_incident_recovery']
            provenance[label]['independent_recovery_timing'] = quality_report.ended_run_timing(
                recovery['recovery_run'], [entry[2] for entry in replacements.values()])
            provenance[label]['recovery_binding_sha256'] = recovery['recovery_binding_sha256']
    return samples, models, runtime, {'quality_report': str(fpath(report_path)),
        'quality_report_sha256': digest(report_path), 'inputs_sha256': digest(inputs_path),
        'production_source': runtime['identity'], 'required_preprocess_sha256': required_preprocess_sha256,
        'models': provenance, 'recovery_receipt_map': report.get('recovery_receipt_map')}


def select_actual(candidates, runtime):
    """Consume only observed candidate content and same-frame evidence."""
    successful = [(label, row) for label, row in candidates if row['record']['status'] == 'success']
    if not successful:
        return {'status': 'failed', 'selected_id': None, 'selected_result': {},
                'automatic_delivery': False, 'review_required': True,
                'conflicts': [], 'modifications': [], 'issues': [{'code': 'all_candidates_failed'}]}
    _, first = successful[0]
    for _, other in successful[1:]:
        if ({key: first['preparation'].get(key) for key in FRAME_FIELDS}
                != {key: other['preparation'].get(key) for key in FRAME_FIELDS}
                or first['evidence'] != other['evidence']):
            raise ValueError('Paired selection candidates have different frame evidence')
    before = deepcopy(successful)
    selection = runtime['select']([{'id': label, 'engine': row['record'].get('engine'),
                                   'result': row['result']} for label, row in successful],
        image_info=first['preparation'], text_evidence=first['evidence'], allow_text_fixes=False)
    chosen = dict(successful)[selection['selected_id']]
    actual = runtime['raw'](chosen['result'])
    if (selection.get('modifications') or successful != before
            or any(selection['selected_result'].get(key) != actual.get(key) for key in ('tables', 'text'))):
        raise ValueError('Selection modified actual OCR text/table values')
    quality = {**selection['selected_assessment'], 'automatic_delivery': selection['automatic_delivery'],
               'review_required': selection['review_required'],
               'issues': selection['selected_assessment']['issues'] + selection['conflicts']}
    return {**selection, 'status': 'success', 'effective_quality': quality}


def freeze_selections(samples, models, runtime, routes):
    if len(routes) > 2 or len(set(routes)) != len(routes):
        raise ValueError('Use at most two unique fixed one-upgrade routes')
    if any(primary == secondary or primary not in models or secondary not in models
           for primary, secondary in routes):
        raise ValueError('Routes must name two different observed models')
    strategies = {label: {'primary': label, 'secondary': None, 'rows': []} for label in models}
    for primary, secondary in routes:
        strategies[primary + '→' + secondary] = {'primary': primary, 'secondary': secondary, 'rows': []}
    for ident in samples:
        singles = {label: select_actual([(label, values[ident])], runtime) for label, values in models.items()}
        for name, strategy in strategies.items():
            primary, secondary = strategy['primary'], strategy['secondary']
            baseline = singles[primary]
            triggered = bool(secondary and (baseline['status'] != 'success' or not baseline['automatic_delivery']))
            observed = [(primary, models[primary][ident])]
            if triggered:
                observed.append((secondary, models[secondary][ident]))
            selection = select_actual(observed, runtime) if triggered else deepcopy(baseline)
            strategy['rows'].append({'id': ident, 'primary': primary, 'secondary': secondary,
                'upgrade_triggered': triggered,
                'trigger': ('primary_technical_failure' if baseline['status'] != 'success' else
                            'production_quality_requires_review') if triggered else None,
                'attempts': [{'model': label, 'status': row['record']['status'],
                    'record': row['record_path'], 'record_sha256': row['record_sha256'],
                    'result_sha256': row['record'].get('result_sha256'),
                    'inference_seconds': row['record'].get('inference_seconds'),
                    'wall_seconds': row['record'].get('wall_seconds'),
                    'error_type': row['record'].get('error_type'),
                    'source_input_recovered': row.get('source_input_recovered', False),
                    'replay_identity': row.get('replay_identity')} for label, row in observed],
                'selection': selection})
    return {'version': 'screen-fixed-strategy-selection-v1',
        'created': datetime.now(timezone.utc).isoformat(), 'image_count': len(samples),
        'policy': 'Native select_screen_candidates; trigger only primary failure or automatic_delivery=false; at most one secondary; allow_text_fixes=false. No truth or offline scores supplied to selection.',
        'strategies': strategies, 'truth_opened': False, 'default_strategy_selected': False}


def error_delta(baseline, actual):
    key = lambda row: (row['table'], row['row'], row['column'])
    before = {key(row): row for row in baseline['errors']}
    after = {key(row): row for row in actual['errors']}
    fixed, new = set(before) - set(after), set(after) - set(before)
    persistent_changed = [position for position in set(before) & set(after)
                          if any(before[position].get(field) != after[position].get(field)
                                 for field in ('actual', 'actual_span'))]
    return {'fixed_error_cells': len(fixed), 'new_error_cells': len(new),
        'fixed_critical_error_cells': sum(before[position]['critical'] for position in fixed),
        'new_critical_error_cells': sum(after[position]['critical'] for position in new),
        'persistent_error_cells_with_changed_wrong_value_or_span': len(persistent_changed),
        'fixed_whole_images': int(not baseline['metrics']['perfect_images'] and actual['metrics']['perfect_images']),
        'new_whole_error_images': int(baseline['metrics']['perfect_images'] and not actual['metrics']['perfect_images']),
        'fixed_critical_error_images': int(baseline['metrics']['critical_error_images'] and not actual['metrics']['critical_error_images']),
        'new_critical_error_images': int(not baseline['metrics']['critical_error_images'] and actual['metrics']['critical_error_images'])}


def score_frozen(receipt, truths, scorer):
    """Offline stage: immutable selections exist before this receives truth."""
    truth_rows = truths
    truths = {row['id']: row for row in truth_rows}
    if len(truths) != len(truth_rows):
        raise ValueError('Offline truth IDs must be unique')
    if set(truths) != {row['id'] for strategy in receipt['strategies'].values() for row in strategy['rows']}:
        raise ValueError('Offline truth does not match frozen selections')
    scored, output = {}, {}
    for name, strategy in receipt['strategies'].items():
        rows = []
        for frozen in strategy['rows']:
            selection = frozen['selection']
            raw = {**selection['selected_result'], 'quality': selection.get('effective_quality',
                   {'automatic_delivery': False, 'review_required': True, 'issues': selection.get('issues', [])})}
            rows.append(scorer.score_sample(truths[frozen['id']], raw, selection['status']))
        scored[name] = rows
        timing = Counter()
        count = Counter()
        for frozen, row in zip(strategy['rows'], rows):
            count['upgrades_attempted'] += frozen['upgrade_triggered']
            count['automatic_silent_critical_error_cells'] += (sum(error['critical'] for error in row['errors'])
                if frozen['selection']['automatic_delivery'] else 0)
            count['automatic_silent_error_cells'] += len(row['errors']) if frozen['selection']['automatic_delivery'] else 0
            count['selection_modifications'] += len(frozen['selection'].get('modifications', []))
            for index, attempt in enumerate(frozen['attempts']):
                prefix = 'primary' if index == 0 else 'extra_secondary'
                value = attempt['inference_seconds']
                timing[prefix + '_known_inference_seconds'] += value or 0
                timing[prefix + '_inference_timing_unknown_attempts'] += value is None
                timing[prefix + '_observed_record_wall_seconds'] += attempt['wall_seconds'] or 0
                timing[prefix + '_record_wall_timing_unknown_attempts'] += attempt['wall_seconds'] is None
                if index == 1:
                    count['secondary_failed_attempts'] += attempt['status'] != 'success'
            count['selected_' + str(frozen['selection']['selected_id'])] += 1
        output[name] = {'primary': strategy['primary'], 'secondary': strategy['secondary'],
            'quality': scorer.summarize(rows), 'counts': dict(count), 'conditional_observed_cost': dict(timing),
            'cohort_level': {str(cohort) + '/' + str(level): scorer.summarize([
                row for row in rows if row['cohort'] == cohort and row['level'] == level])
                for cohort, level in sorted({(row['cohort'], row['level']) for row in rows}, key=str)},
            'examples': {'automatic_errors': [{'id': row['id'], 'errors': row['errors'][:5]}
                for frozen, row in zip(strategy['rows'], rows)
                if frozen['selection']['automatic_delivery'] and not row['metrics']['perfect_images']]}}
    for name, strategy in receipt['strategies'].items():
        if not strategy['secondary']:
            continue
        baseline = {row['id']: row for row in scored[strategy['primary']]}
        counts = Counter()
        examples = []
        for row in scored[name]:
            delta = error_delta(baseline[row['id']], row)
            counts.update(delta)
            if any(delta.values()):
                examples.append({'id': row['id'], **delta, 'remaining_errors': row['errors'][:5]})
        output[name]['change_vs_primary'] = {'counts': dict(counts), 'examples': examples}
    return {'strategies': output,
        'timing_interpretation': 'Conditional sums of actual recorded inference times for the fixed trigger. Not a measured consecutive strategy batch wall time. Cold loads, independent source-input recovery and original first-pass attempts remain separately recorded in provenance; unknown failure inference time is never imputed as zero.',
        'error_delta_interpretation': 'Fixed/new cells compare errors at the same expected offline truth positions; table matching is offline scoring only. Shape, extra table/cell and context errors also affect whole-image changes. No per-image truth-optimal selection.',
        'default_strategy_selected': False}


def write_new(path, value):
    path = fpath(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(value)


def markdown(data):
    lines = ['# 固定质量触发策略离线对比', '',
        '选择回执先于真值读取落盘；仅使用实际原生质量信号，最多一次升级，不自动改任何识别文字或数字。未选择默认策略。', '',
        '|策略|整图正确|关键字段精确|升级次数|新增关键错误格|自动交付/错误/关键错误|额外OCR秒（未知耗时次数）|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for name, row in data['strategies'].items():
        q = row['quality']; ratios = q['ratios']; counts = q['totals']
        delta = row.get('change_vs_primary', {}).get('counts', {})
        timing = row['conditional_observed_cost']
        lines.append(f"|{name}|{ratios['whole_image_correct_rate']:.2%}|{ratios['critical_exact_rate']:.2%}|{row['counts'].get('upgrades_attempted', 0)}|{delta.get('new_critical_error_cells', 0)}|{counts.get('automatic_delivered_images', 0)}/{counts.get('automatic_error_images', 0)}/{counts.get('automatic_critical_error_images', 0)}|{timing.get('extra_secondary_known_inference_seconds', 0):.2f}（{timing.get('extra_secondary_inference_timing_unknown_attempts', 0)}）|")
    lines += ['', '耗时是质量触发后实际已有识别记录的条件求和，不是已实测的连续策略批次墙钟；冷加载、缺输入恢复及失败未知耗时均单列。', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    for key in ('quality-report', 'inputs', 'truth', 'tools', 'production-source', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--required-preprocess-sha256', required=True)
    parser.add_argument('--upgrade-route', action='append', default=[], metavar='PRIMARY:SECONDARY')
    args = parser.parse_args()
    routes = []
    for value in args.upgrade_route:
        parts = value.split(':')
        if len(parts) != 2:
            parser.error('Upgrade route must be PRIMARY:SECONDARY')
        routes.append(tuple(parts))
    output = fpath(args.output)
    receipt_path = output.with_suffix('.selection.json')
    markdown_path = output.with_suffix('.md')
    if any(path.exists() for path in (output, receipt_path, markdown_path)):
        raise ValueError('Use a fresh output path; existing evidence is immutable')
    samples, models, runtime, provenance = load_candidates(args.quality_report,
        args.inputs, args.tools, args.production_source, args.required_preprocess_sha256)
    receipt = freeze_selections(samples, models, runtime, routes)
    receipt['provenance'] = provenance
    write_new(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    # The first truth file read happens only after the native selection receipt
    # has been written. Report scores/ground truth cannot affect routing.
    truth_data = read_json(args.truth)
    scorer = quality_report.load_module(fpath(args.tools) / 'score.py', 'fixed_strategy_offline_score')
    data = score_frozen(receipt, truth_data['samples'], scorer)
    data.update(created=datetime.now(timezone.utc).isoformat(),
        selection_receipt={'path': str(receipt_path), 'sha256': digest(receipt_path)},
        truth_sha256=digest(args.truth), scorer_sha256=digest(fpath(args.tools) / 'score.py'),
        provenance=provenance, image_count=len(samples))
    write_new(output, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    write_new(markdown_path, markdown(data))
    print(json.dumps({'report': str(output), 'selection_receipt': str(receipt_path),
                      'images': len(samples), 'strategies': list(data['strategies'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
