"""Package Clear intelligent audit with shared F-drive inference assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from workbench_platform.supervisor.processes import require_f_path
from scripts.product_compatibility import DATA_COMPATIBILITY


QUALIFICATION_GATES = (
    'packaging_contract', 'agent_gpu_chain', 'heldout_quality', 'excel_readback',
    'native_recovery', 'performance_comparison', 'supported_cross_tool_tasks',
    'cold_backup_restore', 'browser',
)
IMPLEMENTED_FEATURES = ('screen-batch-v1', 'screen-directives-v1', 'per-image-excel-zip-v1')
_IGNORED_PARTS = {'__pycache__', '.pytest_cache', '.git', 'node_modules'}


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def inventory_sha(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def qualification_inventory(source):
    """Bind runtime, policies, Skills, frontend sources and built frontend bytes."""
    source = require_f_path(source)
    files = {}
    for family, root, names, frontend in (
        ('platform', source, ('src', 'packages', 'modules', 'config', 'scripts'), 'apps/portal'),
        ('ocr', source.parent / 'ocr', ('src', 'config'), 'frontend'),
        ('classification', source.parent / 'classification', ('src', 'configs'), 'apps/web'),
    ):
        paths = [root / name for name in names]
        paths += [root / frontend / name for name in ('src', 'dist', 'package.json',
            'package-lock.json', 'pnpm-lock.yaml', 'vite.config.ts', 'tsconfig.json')]
        if family == 'platform':
            paths.append(root / 'pyproject.toml')
        for path in paths:
            for member in ([path] if path.is_file() else [path, *sorted(path.rglob('*'))]):
                if member.is_symlink() or member.is_junction():
                    raise ValueError('Qualification inputs must not contain links: ' + str(member))
                if (member.is_file() and not _IGNORED_PARTS.intersection(member.parts)
                        and member.suffix != '.pyc'):
                    member = require_f_path(member)
                    files[family + '/' + member.relative_to(root).as_posix()] = sha(member)
    return dict(sorted(files.items()))


def _file_reference(reference):
    if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
        raise ValueError('Evidence requires an absolute F JSON path and sha256')
    requested = Path(reference['path'])
    if not requested.is_absolute():
        raise ValueError('Evidence requires an absolute F path')
    path = require_f_path(requested, 'Qualification evidence')
    expected = reference.get('sha256')
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('Evidence requires a complete sha256')
    if not path.is_file() or sha(path) != expected:
        raise ValueError('Qualification evidence missing or changed: ' + str(path))
    return path


def _json_reference(reference):
    path = _file_reference(reference)
    if path.suffix.lower() != '.json':
        raise ValueError('Evidence requires an absolute F JSON path')
    result = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(result, dict):
        raise ValueError('Qualification evidence must be a JSON object')
    return result


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _number(value, minimum=0):
    return type(value) in (int, float) and value >= minimum and value < float('inf')


def _source_bindings(tested):
    native = {name.removeprefix('ocr/'): value for name, value in tested.items()
              if name.startswith(('ocr/src/', 'ocr/config/'))}
    platform_names = ('platform/src/workbench_platform/', 'platform/packages/contracts/',
                      'platform/config/skills/')
    platform_scripts = {'platform/scripts/' + name for name in (
        'evaluate_screen_agent_native.py', 'evaluate_screen_agent_replay.py', 'product_compatibility.py')}
    platform = {name.removeprefix('platform/'): value for name, value in tested.items()
                if name.startswith(platform_names) or name in platform_scripts}
    return inventory_sha(native), inventory_sha(platform), tested.get('ocr/config/screen-policy.json')


def _cold_source_files(tested):
    prefixes = ('platform/src/', 'platform/scripts/', 'platform/packages/', 'platform/modules/',
                'platform/config/', 'ocr/src/', 'ocr/config/')
    return {name: value for name, value in tested.items()
            if name.startswith(prefixes) or name == 'platform/pyproject.toml'}


def _cold_references(value):
    return [value[name] for name in ('consumer_source_inventory', 'actual_invocation', 'actual_cold_source_isolation')] + [
        {key: value['actual_native_consumer_reads'][phase][key] for key in ('path', 'sha256')}
        for phase in ('source', 'restored')]


def _check_cold_runtime(runtime, invocation, consumers, expected_denied):
    """Bind actual loaded modules to the consumer bytes frozen before the run."""
    root = require_f_path(Path(consumers['roots']['ocr']['frozen']))
    executable = {'path': runtime['executable'], 'sha256': runtime['executable_sha256']}
    _require(executable == invocation['runtime']['native'], 'Cold reader Python differs from its actual invocation')
    _file_reference(executable)
    _require(runtime.get('diagnostics_version') == 'screen-diagnostics-v2'
             and runtime.get('start_queue') is False and runtime.get('agent_enabled') is False
             and require_f_path(Path(runtime['native_source'])) == root,
             'Cold reader must use its frozen current no-queue diagnostic consumer')
    denied = {require_f_path(Path(path)) for path in runtime.get('forbidden_data_roots', [])}
    _require(expected_denied <= denied, 'Cold reader did not block all old data scopes')
    loaded = runtime.get('loaded_native_modules')
    _require(isinstance(loaded, dict) and 'ocr_workbench.screen_diagnostics' in loaded,
             'Cold reader must retain the actually imported diagnostic module')
    for reference in loaded.values():
        path = _file_reference(reference)
        _require(path.is_relative_to(root), 'Cold reader imported native code outside its frozen consumer')
        key = 'ocr/' + path.relative_to(root).as_posix()
        _require(consumers['files_sha256'].get(key) == reference['sha256'],
                 'Cold reader loaded module differs from its frozen source inventory')
    return _file_reference(loaded['ocr_workbench.screen_diagnostics'])


def _check_gate(gate, report, evidence, tested):
    """Check measured facts in original reports; a passed flag alone is insufficient."""
    native_sha, platform_sha, policy_sha = _source_bindings(tested)
    measurements = report.get('measurements', {})
    _require(isinstance(measurements, dict), 'Gate measurements must be an object')
    if gate == 'packaging_contract':
        value = evidence['verification']
        _require(value.get('format') == 'clear-agent-packaging-contract-v1'
                 and value.get('status') == 'passed' and value.get('cpu_only') is True
                 and _number(value.get('passed_checks'), 1)
                 and value.get('failed_checks') == 0
                 and value.get('tested_inventory_sha256') == inventory_sha(tested),
                 'CPU packaging contract not verified for these source bytes')
    elif gate == 'agent_gpu_chain':
        value = evidence['run']
        _require(value.get('format') == 'screen-agent-actual-native-result-v1'
                 and value.get('status') in {'completed', 'succeeded'}
                 and value.get('real_queue_and_engine_adapter') is True
                 and value.get('truth_read') is False and value.get('frozen_results_loaded') is False
                 and value.get('shared_context_injected') is False
                 and value.get('scope_inputs') == 240
                 and _number(value.get('model_rounds'), 1)
                 and value.get('waiting_controller_count_stable') is True
                 and value.get('verified_delivery') is True
                 and value.get('first_native_final_result') and value.get('first_excel_bundle'),
                 'Requires a complete actual Agent/native chain of 240 frozen inputs')
        _require(value.get('native_source_inventory_sha256') == native_sha
                 and value.get('platform_source_binding_sha256') == platform_sha
                 and value.get('screen_policy_sha256') == policy_sha,
                 'Actual Agent/native tested source or policy differs')
    elif gate == 'heldout_quality':
        value, before, challenge, comparison = (evidence[name] for name in (
            'quality', 'baseline_quality', 'challenge_quality', 'comparison'))
        _require(measurements.get('split') == 'heldout'
                 and measurements.get('truth_used_by_controller') is False,
                 'Requires held-out evaluation without controller truth access')
        dataset = evidence['dataset_qualification']
        _require(dataset.get('format') == 'specialized-agent-eval-v1' and dataset.get('status') == 'passed'
                 and all(dataset.get(name) == 0 for name in ('old_public_document_overlap',
                     'old_source_hash_overlap', 'old_chinese_family_overlap',
                     'development_validation_holdout_group_overlap')),
                 'Requires new held-out source/template qualification')
        for scored, expected in ((value, 240), (before, 240), (challenge, 24)):
            _require(scored.get('format') == 'specialized-agent-eval-v1'
                     and scored.get('actual_model_evidence') is True
                     and scored.get('all_expected_images_in_denominator') is True
                     and not scored.get('partial_run')
                     and scored.get('summary', {}).get('totals', {}).get('images') == expected
                     and len(scored.get('records', [])) == expected,
                     'Quality requires complete actual model receipts for 240 main and 24 challenge inputs')
        for scored, split in ((value, 'holdout'), (before, 'holdout'), (challenge, 'challenge')):
            binding, declared = scored['bindings'], dataset['sets'][split]
            _require(binding.get('inputs_sha256') == declared.get('inputs_sha256')
                     and binding.get('truth_sha256') == declared.get('truth_sha256')
                     and re.fullmatch('[0-9a-f]{64}', str(binding.get('inputs_sha256', '')))
                     and re.fullmatch('[0-9a-f]{64}', str(binding.get('truth_sha256', ''))),
                     'Scored quality must match the qualified frozen input/truth split')
        totals, ratios = value['summary']['totals'], value['summary']['ratios']
        images, automatic = totals.get('images'), totals.get('automatic_images')
        _require(type(images) is int and images == 240 and type(automatic) is int
                 and 0 < automatic <= images, 'Zero automatic coverage is not automation qualification')
        coverage = automatic / images
        _require(_number(ratios.get('automatic_coverage'))
                 and abs(ratios['automatic_coverage'] - coverage) < 1e-12,
                 'Automatic coverage must retain the full sample denominator')
        for name in ('automatic_error_images', 'automatic_critical_error_cells', 'automatic_critical_error_images',
                     'automatic_uncertain_images', 'contradictory_automatic_and_review_images'):
            _require(totals.get(name) == 0, 'Unsafe automatic subset: ' + name)
        _require(comparison['totals'].get('new_silent_critical_release_errors') == 0,
                 'Critical errors newly released silently cannot be canceled by net repairs')
        _require(_number(comparison['totals'].get('net_visible_cell_repairs'), 1),
                 'New automatic handling requires positive net repairs')
        previous = before['summary']['totals']
        reduced_review = (totals.get('review_images', images) < previous.get('review_images', 0)
                          or totals.get('review_reference_cells', float('inf')) < previous.get('review_reference_cells', 0))
        human = evidence.get('human_timing', {})
        observed_relief = (human.get('format') == 'clear-agent-human-review-timing-v1'
            and human.get('independent_timing') is True and human.get('status') == 'passed'
            and _number(human.get('baseline_seconds'), 1) and _number(human.get('candidate_seconds'))
            and human['candidate_seconds'] < human['baseline_seconds'])
        _require(reduced_review or observed_relief,
                 'Automatic coverage alone does not prove reduced review workload')
        left = {row['id']: row for row in before['records']}
        right = {row['id']: row for row in value['records']}
        _require(len(left) == len(before['records']) and len(right) == len(value['records'])
                 and left.keys() == right.keys() and len(right) == images,
                 'Paired quality evidence must share the complete sample denominator')
        fixed, added = 0, 0
        for sid, row in right.items():
            visible = lambda record: {(e['table_index'], e['row'], e['column'])
                                      for e in record['errors'] if e['visibility'] == 'visible'}
            old_positions, new_positions = visible(left[sid]), visible(row)
            fixed += len(old_positions - new_positions)
            added += len(new_positions - old_positions)
        _require(fixed - added == comparison['totals']['net_visible_cell_repairs'],
                 'Net repair count differs from the complete paired error records')
        for sid, row in right.items():
            if not row['metrics'].get('automatic_images'):
                continue
            prior = {(e['table_index'], e['row'], e['column']): e for e in left[sid]['errors']
                     if e['visibility'] == 'visible'}
            for error in row['errors']:
                if error['visibility'] == 'visible' and error['critical'] and not error['reviewed']:
                    old = prior.get((error['table_index'], error['row'], error['column']))
                    _require(old is not None and not old['reviewed'],
                             'Existing critical error moved out of review or newly released silently')
                    raise ValueError('Residual critical error in the automatic subset')
        for row in challenge['records']:
            if row['metrics'].get('automatic_images'):
                _require(not any(error['critical'] for error in row['errors']),
                         'Challenge automatic subset retains critical errors')
    elif gate == 'excel_readback':
        value = evidence['readback']
        _require(value.get('format') == 'specialized-agent-eval-v1'
                 and value.get('status') == 'passed' and value.get('fidelity_status') == 'passed'
                 and value.get('freshness_status') == 'passed'
                 and value.get('current_delivery_verified') is True
                 and _number(value.get('workbooks'), 1) and value.get('inputs') == 240
                 and _number(value.get('no_book'))
                 and value['workbooks'] + value['no_book'] == 240
                 and len(value.get('records', [])) == 240
                 and all(row.get('status') in {'passed', 'no_book'} for row in value['records']),
                 'Actual Excel content/current versions and all 240 inputs must be accounted for')
    elif gate == 'native_recovery':
        value, prepared, run_policy = (evidence[name] for name in ('recovery', 'prepared', 'run_policy'))
        _require(value.get('format') == 'screen-native-eight-recovery-result-v1'
                 and value.get('status') == 'passed' and value.get('scope_inputs') == 8
                 and value.get('truth_read') is False and value.get('frozen_results_loaded') is False
                 and value.get('prefilled_cache') is False
                 and value.get('interrupted_attempt_preserved') is True
                 and value.get('same_batch_and_resume_idempotent') is True
                 and value.get('no_duplicate_batch_or_final_content') is True,
                 'Actual eight-input queue recovery required')
        _require(value.get('native_source_inventory_sha256') == native_sha
                 and prepared.get('native_source_inventory_sha256') == native_sha
                 and prepared.get('source_default_policy_sha256') == policy_sha
                 and prepared.get('run_policy_sha256') == value.get('run_policy_sha256')
                 and value.get('run_policy_sha256') == report['evidence']['run_policy']['sha256']
                 and run_policy == prepared.get('run_policy'), 'Recovery source or policy differs')
        policy_path = require_f_path(Path(prepared['native_source']) / 'config/screen-policy.json')
        _require(sha(policy_path) == policy_sha
                 and json.loads(policy_path.read_text(encoding='utf-8')) == run_policy,
                 'Recovery run policy differs from the tested final default policy')
        release, exit_state = value.get('worker_release'), value.get('service_exit', {})
        _require(isinstance(release, list) and release and all(row.get('released') is True for row in release)
                 and exit_state.get('actual_app_lifespans_closed') == 2
                 and all(exit_state.get(key) is False for key in ('queue_alive', 'coordinator_alive',
                     'fusion_queue_alive', 'external_queue_alive', 'document_threads_alive')),
                 'Recovered workers and services must actually exit')
    elif gate == 'performance_comparison':
        value = evidence['comparison']
        _require(value.get('format') == 'clear-agent-performance-comparison-v1'
                 and value.get('status') == 'passed' and value.get('paired_inputs_verified') is True
                 and value.get('actual_controller_and_models') is True
                 and value.get('scope_inputs') == 240 and value.get('ordinary_batch') is True,
                 'Actual paired ordinary 240-input performance comparison required')
        before, after = value['baseline'], value['candidate']
        metrics = ('wall_seconds', 'first_excel_seconds', 'model_rounds', 'total_tokens')
        _require(all(_number(row.get(name)) for row in (before, after) for name in metrics),
                 'Performance requires observed wall, first Excel, rounds and tokens')
        _require(before.get('inputs_sha256') == after.get('inputs_sha256')
                 and re.fullmatch('[0-9a-f]{64}', str(before.get('inputs_sha256', ''))),
                 'Performance comparison inputs differ')
        _require(all(before.get(key) is not None and before[key] == after.get(key)
                     for key in ('hardware', 'load_state', 'controller', 'export_options')),
                 'Performance requires the same hardware, load state, controller and export options')
        _require(_number(after.get('logical_controller_rounds'), 1) and after['logical_controller_rounds'] <= 5
                 and _number(after.get('batch_tool_calls'), 1) and after['batch_tool_calls'] <= 8
                 and _number(after.get('api_retries')) and value.get('waiting_controller_count_stable') is True,
                 'Ordinary batches require at most 5 logical rounds/8 batch tools and separately counted API retries')
        for key, measured in (('baseline_run', before), ('candidate_run', after)):
            run = evidence[key]
            _require(run.get('format') == 'screen-agent-actual-native-result-v1'
                     and run.get('real_queue_and_engine_adapter') is True
                     and run.get('truth_read') is False and run.get('frozen_results_loaded') is False
                     and run.get('scope_inputs') == 240 and run.get('inputs_sha256') == measured['inputs_sha256']
                     and run.get('model_rounds') == measured['model_rounds']
                     and run.get('input_tokens', 0) + run.get('output_tokens', 0) == measured['total_tokens'],
                     'Performance must retain actual paired model/controller receipts')
        mode = value.get('adoption_mode', 'default')
        _require(mode in {'default', 'accuracy'}, 'Performance adoption mode must be explicit')
        faster = after['first_excel_seconds'] < before['first_excel_seconds'] or after['wall_seconds'] < before['wall_seconds']
        if mode == 'accuracy':
            paired = evidence['quality_comparison']['totals']
            _require(_number(paired.get('net_visible_cell_repairs'), 1)
                     and paired.get('new_silent_critical_release_errors') == 0,
                     'Slower accuracy mode requires actual positive repairs without silent critical releases')
        else:
            _require(faster, 'Default performance comparison must improve first output or total time')
        if mode == 'accuracy' or any(after[name] > before[name] for name in metrics):
            tradeoff = value.get('tradeoff', {})
            _require(isinstance(tradeoff, dict) and isinstance(tradeoff.get('reason'), str)
                     and tradeoff['reason'].strip() and isinstance(tradeoff.get('adoption_scope'), str)
                     and tradeoff['adoption_scope'].strip(),
                     'Measured cost increases require a concrete reason and adoption scope')
        human_times = (before.get('human_review_seconds'), after.get('human_review_seconds'))
        if any(time is not None for time in human_times):
            human = evidence['human_timing']
            _require(human.get('format') == 'clear-agent-human-review-timing-v1'
                     and human.get('independent_timing') is True
                     and all(_number(time) for time in human_times)
                     and human_times == (human.get('baseline_seconds'), human.get('candidate_seconds'))
                     and human.get('status') == 'passed',
                     'Human review seconds require independent observed timing, never flag-count estimates')
    elif gate == 'supported_cross_tool_tasks':
        value = evidence['tasks']
        _require(value.get('format') == 'clear-agent-cross-tool-qualification-v1'
                 and value.get('status') == 'passed' and value.get('actual_controller') is True
                 and value.get('production_tools_only') is True, 'Actual platform-tool tasks required')
        tasks = value.get('tasks', [])
        required = {'published_model_classification', 'ocr_to_data_to_evidence'}
        _require(isinstance(tasks, list) and required <= {row.get('name') for row in tasks}
                 and all(row.get('status') == 'passed' and row.get('actual_result_reads') is True
                         and row.get('source_evidence_verified') is True
                         and row.get('operation_ids') and row.get('artifact_ids') for row in tasks),
                 'Cross-tool evidence must retain actual operations, artifacts and source reads')
    elif gate == 'cold_backup_restore':
        value = evidence['restore']
        _require(value.get('format') == 'clear-agent-screen-cold-restore-v1'
                 and value.get('status') == 'passed' and value.get('source_services_stopped') is True
                 and value.get('actual_screen_data') is True and value.get('backup_verified') is True
                 and value.get('restored_artifact_downloads_verified') is True,
                 'Queue recovery or primitive backup checks are not actual screen cold restore')
        old, new = require_f_path(Path(value['source'])), require_f_path(Path(value['restored']))
        _require(old != new and value.get('source_version_vector')
                 and value['source_version_vector'] == value.get('restored_version_vector')
                 and value.get('source_preferences')
                 and value['source_preferences'] == value.get('restored_preferences'),
                 'Cold restore requires a new F root with matching full versions and preferences')
        vector = value['source_version_vector']
        _require(isinstance(vector, dict)
                 and {'snapshot_id', 'batch_id', 'project_id', 'policy_sha256', 'items', 'export_options'} <= vector.keys()
                 and isinstance(vector['items'], list) and vector['items']
                 and all({'input_version_id', 'result_id', 'result_revision', 'current_revision',
                     'selected_result_id', 'review_epoch', 'review_result_id', 'review_revision'} <= row.keys()
                     for row in vector['items']), 'Cold restore must retain the full delivery version vector')
        _require(value.get('original_source_files_and_sha_unchanged') is True
                 and value.get('consumer_source_bytes_unchanged') is True
                 and value.get('diagnostics_version') == 'screen-diagnostics-v2',
                 'Cold restore must preserve original sources and test the current diagnostics consumer')
        consumers = _json_reference(value['consumer_source_inventory'])
        _require(consumers.get('format') == 'clear-cold-consumer-source-v1'
                 and consumers.get('files_sha256') == _cold_source_files(tested)
                 and consumers.get('inventory_sha256') == inventory_sha(consumers['files_sha256']),
                 'Cold restored consumer source bytes differ from this candidate')
        _require(set(consumers.get('roots', {})) == {'platform', 'ocr'},
                 'Cold consumer source inventory must bind both real consumer roots')
        for name, root in consumers['roots'].items():
            frozen = require_f_path(Path(root['frozen']))
            files = {key.removeprefix(name + '/'): digest for key, digest in consumers['files_sha256'].items()
                     if key.startswith(name + '/')}
            _require(files and root.get('files') == len(files), 'Cold consumer source inventory is incomplete')
            for relative, digest in files.items():
                path = _file_reference({'path': str(frozen / relative), 'sha256': digest})
                _require(path.is_relative_to(frozen), 'Cold frozen consumer file escapes its root')
        invocation = _json_reference(value['actual_invocation'])
        _require(invocation.get('driver_sha256') == value.get('driver_sha256')
                 and isinstance(invocation.get('command_argv'), list) and invocation['command_argv']
                 and set(invocation.get('runtime', {})) == {'platform', 'native', 'data'},
                 'Cold restore must bind its actual driver command and Python runtimes')
        for runtime in invocation['runtime'].values():
            _file_reference(runtime)
        isolation = _json_reference(value['actual_cold_source_isolation'])
        old_scope = require_f_path(Path(isolation['old_owned_source_root']))
        isolated_scope = require_f_path(Path(isolation['isolated_owned_source_root']))
        _require(isolation.get('former_source_root_absent_during_restored_consumption') is True
                 and isolation.get('original_upstream_and_s07_not_moved') is True
                 and isolation.get('files_and_sha256_unchanged') is True
                 and old == old_scope / 'main' and old_scope != isolated_scope
                 and not old_scope.exists() and isolated_scope.is_dir(),
                 'Cold restored consumers must work while their owned cold source is isolated')
        upstream_scopes = {require_f_path(Path(value[name])) for name in ('source_run', 's07_run')}
        for phase in ('source', 'restored'):
            audit = value['actual_native_consumer_reads'][phase]
            path = _file_reference(audit)
            _require(path.suffix == '.jsonl', 'Cold native file reads require actual JSONL audit events')
            events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            diagnostic_module = _check_cold_runtime(audit['runtime'], invocation, consumers,
                upstream_scopes | ({old_scope, isolated_scope} if phase == 'restored' else set()))
            pixels = [row for row in events if any(Path(frame['file']).name == 'screen_diagnostics.py'
                      for frame in row.get('frames', []))]
            _require(events and pixels and len(events) == audit.get('events')
                     and len(pixels) == audit.get('diagnostics_source_pixel_read_events')
                     and audit.get('forbidden_old_scope_reads') == 0
                     and not any(row.get('forbidden_old_scope') for row in events)
                     and audit.get('runtime', {}).get('diagnostics_version') == 'screen-diagnostics-v2',
                     'Cold restore requires actual current diagnostic pixel reads without any old scope access')
            native_root = (old if phase == 'source' else new) / 'native/ocr'
            _require(all(require_f_path(Path(row['path'])).is_relative_to(native_root) for row in events),
                     'Cold native consumer reads escaped its own data root')
            _require(all(require_f_path(Path(frame['file'])) == diagnostic_module for row in pixels
                         for frame in row['frames'] if Path(frame['file']).name == 'screen_diagnostics.py'),
                     'Cold pixel-read frames differ from the loaded frozen diagnostic module')
    elif gate == 'browser':
        value = evidence['browser']
        _require(value.get('format') == 'clear-agent-browser-qualification-v1'
                 and value.get('status') == 'passed' and value.get('browser_validated') is True,
                 'Frontend build or CPU component tests are not browser qualification')
        cases = value.get('cases', [])
        required = {'bulk_import_progress', 'batch_pause_resume', 'grouped_review',
                    'precision_current_revision', 'per_image_excel_zip',
                    'preference_next_boundary', 'stale_delivery_warning'}
        _require(isinstance(cases, list) and required <= {row.get('name') for row in cases}
                 and all(row.get('status') == 'passed' and row.get('evidence') for row in cases),
                 'Required actual browser interactions are missing')
        for row in cases:
            _require(isinstance(row['evidence'], list), 'Browser interaction evidence requires file references')
            for reference in row['evidence']:
                _file_reference(reference)


def qualification_status(source, qualification_index=None, product_version='0.9.4'):
    """Read-only qualification; missing/invalid gates stay explicit for engineering."""
    source = require_f_path(source)
    required = (source / 'config/skills/screen-batch-excel/SKILL.md').is_file()
    tested = qualification_inventory(source)
    result = {'format': 'clear-agent-package-qualification-v1', 'required_for_final': required,
              'status': 'unqualified', 'tested_inventory_sha256': inventory_sha(tested),
              'required_gates': list(QUALIFICATION_GATES), 'gates': {}, 'evidence_files': []}
    index, index_error = {}, None
    if required and (not tested.get('ocr/config/screen-policy.json') or any(
            not any(name.startswith(prefix) for name in tested) for prefix in (
                'platform/src/', 'ocr/src/', 'classification/src/',
                'platform/apps/portal/src/', 'ocr/frontend/src/', 'classification/apps/web/src/'))):
        index_error = 'Qualification requires actual runtime, policy and frontend source bytes'
    if qualification_index is not None and not index_error:
        path = require_f_path(qualification_index, 'Qualification index')
        try:
            index = _json_reference({'path': str(path), 'sha256': sha(path)})
            _require(index.get('format') == 'clear-agent-qualification-index-v1'
                     and index.get('product_version') == product_version, 'Qualification index format/version differs')
            _require(isinstance(index.get('gates'), dict), 'Qualification index gates must be an object')
            result['index'] = {'path': str(path), 'sha256': sha(path)}
            result['evidence_files'].append(result['index'])
        except (ValueError, OSError, TypeError) as error:
            index_error = str(error)
    for gate in QUALIFICATION_GATES:
        reference = index.get('gates', {}).get(gate) if not index_error else None
        if reference is None:
            result['gates'][gate] = {'status': 'invalid' if index_error else 'missing',
                                    'reason': index_error or 'No actual qualification report supplied'}
            continue
        try:
            report = _json_reference(reference)
            _require(report.get('format') == 'clear-agent-qualification-gate-v1'
                     and report.get('gate') == gate and report.get('product_version') == product_version
                     and report.get('status') == 'passed', 'Qualification gate format/version/status differs')
            _require(report.get('tested_files') == tested, 'Tested source/policy/frontend bytes differ')
            references = report.get('evidence')
            _require(isinstance(references, dict) and references, 'Gate requires actual original JSON evidence')
            evidence = {name: _json_reference(ref) for name, ref in references.items()}
            _check_gate(gate, report, evidence, tested)
            result['gates'][gate] = {'status': 'passed', 'report': reference, 'evidence': references}
            result['evidence_files'].extend([reference, *references.values()])
            if gate == 'cold_backup_restore':
                result['evidence_files'].extend(_cold_references(evidence['restore']))
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
            result['gates'][gate] = {'status': 'invalid', 'reason': str(error), 'report': reference}
    if all(row['status'] == 'passed' for row in result['gates'].values()):
        chain = _json_reference(result['gates']['agent_gpu_chain']['evidence']['run'])
        quality = _json_reference(result['gates']['heldout_quality']['evidence']['quality'])
        if chain.get('inputs_sha256') != quality['bindings']['inputs_sha256']:
            result['gates']['agent_gpu_chain'].update(status='invalid', reason='Actual chain and held-out quality inputs differ')
        else:
            result['status'] = 'qualified'
    return result


def _seal_qualification(output, qualification):
    sealed = {}
    for reference in qualification.pop('evidence_files'):
        source = _file_reference(reference)
        _require(source.suffix.lower() in {'.json', '.jsonl'}, 'Sealed qualification requires JSON or JSONL evidence')
        if source.suffix.lower() == '.json':
            _json_reference(reference)
        target = output / 'qualification' / (reference['sha256'] + source.suffix.lower())
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(require_f_path(Path(reference['path'])), target)
        _require(sha(target) == reference['sha256'], 'Evidence changed during packaging')
        sealed[target.relative_to(output).as_posix()] = reference
    qualification['sealed_evidence'] = sealed


def copy_tree(source,target):
    source=require_f_path(source);target=require_f_path(target)
    for item in source.rglob('*'):
        if item.is_symlink() or item.is_junction():
            raise ValueError('Candidate inputs must not contain links: '+str(item))
    shutil.copytree(source,target,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.pytest_cache','.git'))


def skills_manifest(root, layer):
    """Validate packaged Skill bytes and required tools on an isolated F root."""
    from workbench_platform.agent.runtime import AgentRuntime
    from workbench_platform.agent.skills import SkillService
    temp = require_f_path(Path(os.environ['TEMP']), 'Packaging temporary directory')
    os.environ['WORKBENCH_AGENT_LAYER'] = str(require_f_path(layer))
    with tempfile.TemporaryDirectory(prefix='skill-package-', dir=temp) as directory:
        runtime = AgentRuntime(Path(directory), SimpleNamespace())
        host = SkillService(runtime.store, roots=[root], registry=runtime.registry)
        catalog = host.catalog('package-validation', limit=50)
        if catalog['rejected'] or not catalog['items'] or any(not item['compatible'] for item in catalog['items']):
            raise ValueError('Packaged Skill registration is invalid or incompatible')
        return {'format': 'clear-skills-manifest-v1', 'host': 'clear-workbench',
                'scripts_supported': False, 'skills': catalog['items']}


def package(source,program,layer,output,engineering=False,pdf_layer=None,
            product_version='0.9.4',qualification_index=None):
    source,program,layer,output=[require_f_path(p) for p in (source,program,layer,output)]
    if not isinstance(product_version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', product_version):
        raise ValueError('product_version must be an explicit major.minor.patch version')
    pdf_relative='build/public-quality-20260916/tool/site-packages/pdfplumber'
    pdf_layer=require_f_path(pdf_layer if pdf_layer is not None else source.parent/'ocr'/pdf_relative)
    if not (pdf_layer/'__init__.py').is_file():
        raise ValueError('Verified PDF dependency missing; supply its read-only F-drive source with --pdf-layer')
    if output.exists():
        raise ValueError('Use one new candidate directory; existing candidates are never overwritten')
    if not (source/'apps/portal/dist/index.html').is_file():
        raise ValueError('Production portal build missing')
    for family, relative in [('ocr', 'frontend/dist'), ('classification', 'apps/web/dist')]:
        if not (source.parent / family / relative / 'index.html').is_file():
            raise ValueError('Production native build missing: ' + family)
    qualification = qualification_status(source, qualification_index, product_version)
    if not engineering and qualification['required_for_final'] and qualification['status'] != 'qualified':
        incomplete = [name + ':' + row['status'] for name, row in qualification['gates'].items()
                      if row['status'] != 'passed']
        raise ValueError('Final screen-batch-excel requires actual qualification: ' + ', '.join(incomplete))
    output.mkdir(parents=True)
    _seal_qualification(output, qualification)
    for name in ('src','packages','modules','config','scripts','apps/portal/dist','docs/agent'):
        copy_tree(source/name,output/'platform'/name)
    registered_skills = skills_manifest(output/'platform/config/skills', layer)
    (output/'skills.json').write_text(json.dumps(registered_skills,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for family,names in [('ocr',('src','config','frontend/dist')),
                         ('classification',('src','configs','apps/web/dist'))]:
        for name in names:
            copy_tree(source.parent/family/name,output/family/name)
    copy_tree(pdf_layer,output/'ocr'/pdf_relative)
    copy_tree(layer,output/'dependencies/agent')
    receipt=output/'dependencies/agent/layer-receipt.json'
    declaration=json.loads(receipt.read_text(encoding='utf-8'))
    declaration['candidate_destination']=str(output/'dependencies/agent')
    declaration['relocated_from_verified_layer']=str(layer)
    receipt.write_text(json.dumps(declaration,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for name in ('pyproject.toml',):
        shutil.copy2(source/name,output/'platform'/name)
    launcher='''import argparse,json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parent
sys.path[:0]=[str(root/'platform'),str(root/'platform/src')]
from workbench_platform.supervisor.processes import require_f_path,storage_environment
from scripts.package_agent_candidate import sha
manifest=json.loads((root/'candidate.json').read_text(encoding='utf-8'))
qualification=manifest.get('qualification',{})
if manifest['release_stage']=='final' and qualification.get('required_for_final') and qualification.get('status')!='qualified':
    raise ValueError('Final screen-batch-excel qualification is missing')
for name,expected in manifest['files'].items():
    member=require_f_path(root/name)
    if not member.is_relative_to(root) or sha(member)!=expected:
        raise ValueError('Candidate file changed: '+name)
for name,expected in manifest['shared_identity'].items():
    if sha(require_f_path(Path(name)))!=expected:
        raise ValueError('Shared runtime/declaration changed: '+name)
parser=argparse.ArgumentParser()
parser.add_argument('--data',type=Path,default=root/'workspace')
parser.add_argument('--port',type=int,default=8879)
parser.add_argument('--disable-agent',action='store_true')
args=parser.parse_args()
data=require_f_path(args.data)
from scripts.product_compatibility import check
check(data,manifest['data_compatibility'])
os.environ.update(storage_environment(data))
os.environ['WORKBENCH_AGENT_LAYER']=str(root/'dependencies/agent')
os.environ['WORKBENCH_AGENT_ENABLED']='0' if args.disable_agent else '1'
from scripts.agent_native_preview import main
sys.argv=['agent-candidate','--source',str(root/'platform'),'--program',manifest['shared_program'],'--data',str(data),'--port',str(args.port)]
main()
'''
    (output/'launch.py').write_text(launcher,encoding='utf-8')
    runtime=program/'runtimes/platform/python.exe'
    launch_cmd = '@echo off\n"'+str(runtime)+'" -I -B -u "%~dp0launch.py" %*\n'
    for name in ('启动智能审计.cmd', '启动平台Agent.cmd'):
        (output/name).write_text(launch_cmd,encoding='utf-8')
    (output/'README.md').write_text('''# Clear 智能审计

默认首页为大模型对话与审计任务工作台，四类 Clear 手动工具继续可用。
双击“启动智能审计.cmd”，访问 http://127.0.0.1:8879/ 。默认使用本目录 workspace，所有缓存/临时在 F 盘。
首页可保存目标、上传资料、接管任务；连接支持工具调用的大模型后可自动执行。主控设置使用独立 DPAPI 存储。
查看 platform/docs/agent/README.md 获取能力、手动接续、底稿及迁移说明。真实模型调用需用户填写有效配置；功能检查不冒充真实模型验证。
使用 --data 指定新的 F 盘工作区；--port 改端口；--disable-agent 关闭 Agent 保留普通工具。
关闭终端以停止服务。冷备前关闭平台和原生服务，使用 platform/scripts/backup_workspace.py 和分类 backup 模块。
原生模型/运行时来自 candidate.json 登记的共享 F 盘 program；该目录必须保留。未复制32GB模型，未改共享绑定。
不要用旧版本直接打开新数据库。关闭Agent是功能回退，备份恢复到新目录是数据恢复。
''',encoding='utf-8')
    if engineering:
        readme=output/'README.md'
        readme.write_text(readme.read_text(encoding='utf-8').replace('# Clear 智能审计',
            '# Clear 智能审计 — 工程验证候选\n\n本候选不登记为 current。功能已实现不代表真实场景验收通过；candidate.json 的 qualification 逐项记录缺失或无效证据。最终交付须重新按冻结源码与完整真实验收证据打包。',1),encoding='utf-8')
    identity={}
    for name in ('runtimes/platform/python.exe','runtimes/platform/python312._pth',
                 'runtimes/data/python.exe','runtimes/forensics/python.exe',
                 'bundles/ocr/bundle.manifest.json','bundles/classification/bundle.manifest.json',
                 'bundles/ocr/runtimes/control/python.exe','bundles/classification/runtimes/control/python.exe'):
        identity[str(program/name)]=sha(program/name)
    files={p.relative_to(output).as_posix():sha(p) for p in sorted(output.rglob('*')) if p.is_file()}
    sources={}
    for family,path in [('platform',source),('ocr',source.parent/'ocr'),('classification',source.parent/'classification')]:
        head=subprocess.check_output(['git','-c','safe.directory='+path.as_posix(),'rev-parse','HEAD'],cwd=path,text=True).strip()
        sources[family]={'path':str(path),'base_head':head,'candidate_files_are_current_working_contents':True}
    manifest={'format':1,'status':'engineering_candidate' if engineering else 'intelligent_audit_product',
        'release_stage':'engineering' if engineering else 'final','product_version':product_version,
        'controller_configuration_required':True,'shared_program':str(program),'sources':sources,
        'implemented_features':list(IMPLEMENTED_FEATURES),
        'qualified_features':list(IMPLEMENTED_FEATURES) if qualification['status']=='qualified' else [],
        'qualification':qualification,
        'data_compatibility': DATA_COMPATIBILITY,
        'skills_manifest': 'skills.json', 'skills_count': len(registered_skills['skills']),
        'frontend_roots': {'portal': 'platform/apps/portal/dist', 'ocr': 'ocr/frontend/dist', 'classification': 'classification/apps/web/dist'},
        'shared_assets_read_only':True,'copies_models':False,'shared_identity':identity,'files':files}
    (output/'candidate.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return {'candidate':str(output),'files':len(files),'bytes':sum(p.stat().st_size for p in output.rglob('*') if p.is_file()),'copies_models':False}


def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','program','layer','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--engineering',action='store_true',help='Clearly label an unfrozen engineering candidate; never registers current')
    parser.add_argument('--pdf-layer',type=Path,help='Read-only verified pdfplumber source on F: when isolated OCR source omits build dependencies')
    parser.add_argument('--product-version',default='0.9.4',help='Explicit product version; this round uses 0.9.5')
    parser.add_argument('--qualification-index',type=Path,help='F JSON index of actual qualification reports; mandatory for final screen-batch-excel')
    return parser


if __name__=='__main__':
    print(json.dumps(package(**vars(build_parser().parse_args())),ensure_ascii=False))
