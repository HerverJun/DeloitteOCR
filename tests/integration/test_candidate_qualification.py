"""Synthetic CPU contract fixtures never constitute product/model qualification."""
import json
from pathlib import Path

import pytest

from scripts.package_agent_candidate import (
    QUALIFICATION_GATES, _cold_source_files, _source_bindings, build_parser, package,
    inventory_sha, qualification_inventory, qualification_status, sha,
)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return {'path': str(path), 'sha256': sha(path)}


def text_file(path, value='synthetic contract bytes'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


@pytest.fixture
def sources(tmp_path):
    assert tmp_path.drive.upper() == tmp_path.resolve().drive.upper() == 'F:'
    source = tmp_path / 'worktrees/platform'
    for name in ('src/workbench_platform/example.py', 'packages/contracts/example.json',
                 'modules/example.py', 'config/skills/screen-batch-excel/SKILL.md',
                 'scripts/evaluate_screen_agent_native.py', 'scripts/evaluate_screen_agent_replay.py',
                 'scripts/product_compatibility.py', 'pyproject.toml',
                 'apps/portal/src/main.tsx', 'apps/portal/dist/index.html'):
        text_file(source / name)
    for family, frontend in (('ocr', 'frontend'), ('classification', 'apps/web')):
        for name in ('src/example.py', frontend + '/src/main.tsx', frontend + '/dist/index.html'):
            text_file(source.parent / family / name)
    text_file(source.parent / 'ocr/config/screen-policy.json', '{}')
    text_file(source.parent / 'ocr/src/ocr_workbench/screen_diagnostics.py')
    text_file(source.parent / 'classification/configs/example.json', '{}')
    return source


def synthetic_cold_reads(sources, tested, tmp_path):
    """Invented files/receipts exercise validation only; no cold restore occurs."""
    cold = tmp_path / 'cold-proof'
    source, restored = cold / 'sources/main', cold / 'restored/main'
    isolated = cold / 'isolated-cold-sources'
    isolated.mkdir(parents=True)
    upstream, s07 = tmp_path / 'upstream-run', tmp_path / 's07-run'
    files = _cold_source_files(tested)
    roots = {}
    for family in ('platform', 'ocr'):
        frozen = cold / 'consumers' / family
        members = {name.removeprefix(family + '/'): digest for name, digest in files.items()
                   if name.startswith(family + '/')}
        for relative in members:
            text_file(frozen / relative, (sources.parent / family / relative).read_text(encoding='utf-8'))
        roots[family] = {'frozen': str(frozen), 'files': len(members)}
    consumer = write(cold / 'consumer-source-inventory.json', {
        'format': 'clear-cold-consumer-source-v1', 'roots': roots,
        'files_sha256': files, 'inventory_sha256': inventory_sha(files)})
    executable = cold / 'python.exe'
    text_file(executable)
    runtime_reference = {'path': str(executable), 'sha256': sha(executable)}
    invocation = write(cold / 'actual-invocation.json', {
        'driver_sha256': 'd' * 64, 'command_argv': [str(executable), '-B', 'synthetic-driver.py'],
        'runtime': {name: runtime_reference for name in ('platform', 'native', 'data')}})
    isolation = write(cold / 'actual-cold-source-isolation.json', {
        'old_owned_source_root': str(source.parent), 'isolated_owned_source_root': str(isolated),
        'former_source_root_absent_during_restored_consumption': True,
        'original_upstream_and_s07_not_moved': True, 'files_and_sha256_unchanged': True})
    diagnostic_module = cold / 'consumers/ocr/src/ocr_workbench/screen_diagnostics.py'
    reads = {}
    for phase, data_root in (('source', source), ('restored', restored)):
        pixel = data_root / 'native/ocr/original.png'
        if phase == 'restored':
            text_file(pixel)
        event = {'path': str(pixel), 'forbidden_old_scope': None,
                 'frames': [{'file': str(diagnostic_module), 'function': 'synthetic', 'line': 1}]}
        log = cold / (phase + '-native-file-reads.jsonl')
        text_file(log, json.dumps(event) + '\n')
        reads[phase] = {'path': str(log), 'sha256': sha(log), 'events': 1,
            'diagnostics_source_pixel_read_events': 1, 'forbidden_old_scope_reads': 0,
            'runtime': {'executable': str(executable), 'executable_sha256': sha(executable),
                'diagnostics_version': 'screen-diagnostics-v2', 'start_queue': False, 'agent_enabled': False,
                'native_source': roots['ocr']['frozen'], 'forbidden_data_roots': [str(upstream), str(s07)] +
                    ([str(source.parent), str(isolated)] if phase == 'restored' else []),
                'loaded_native_modules': {'ocr_workbench.screen_diagnostics': {
                    'path': str(diagnostic_module), 'sha256': sha(diagnostic_module)}}}}
    return {'source': str(source), 'restored': str(restored), 'source_run': str(upstream), 's07_run': str(s07),
        'driver_sha256': 'd' * 64, 'consumer_source_inventory': consumer, 'actual_invocation': invocation,
        'actual_cold_source_isolation': isolation, 'actual_native_consumer_reads': reads,
        'original_source_files_and_sha_unchanged': True, 'consumer_source_bytes_unchanged': True,
        'diagnostics_version': 'screen-diagnostics-v2'}


def quality(images, automatic, review, errors=False):
    records = []
    for position in range(images):
        records.append({'id': str(position), 'metrics': {'automatic_images': int(position < automatic)},
                        'errors': ([{'table_index': 0, 'row': 1, 'column': 1,
                                     'visibility': 'visible', 'critical': True, 'reviewed': True}]
                                   if errors and position < 20 else [])})
    return {'format': 'specialized-agent-eval-v1', 'actual_model_evidence': True,
            'all_expected_images_in_denominator': True, 'records': records,
            'bindings': {'inputs_sha256': ('a' if images == 240 else 'c') * 64,
                         'truth_sha256': ('b' if images == 240 else 'd') * 64},
            'summary': {'totals': {'images': images, 'automatic_images': automatic,
                'automatic_error_images': 0, 'automatic_critical_error_cells': 0,
                'automatic_critical_error_images': 0, 'automatic_uncertain_images': 0,
                'contradictory_automatic_and_review_images': 0, 'review_images': review,
                'review_reference_cells': review * 10},
                'ratios': {'automatic_coverage': automatic / images}}}


@pytest.fixture
def qualified_index(sources, tmp_path):
    """All values are invented unit inputs; no actual candidate/model run is made."""
    tested = qualification_inventory(sources)
    native, platform, policy = _source_bindings(tested)
    run = {'format': 'screen-agent-actual-native-result-v1', 'status': 'completed',
           'real_queue_and_engine_adapter': True, 'truth_read': False,
           'frozen_results_loaded': False, 'shared_context_injected': False,
           'scope_inputs': 240, 'model_rounds': 4, 'input_tokens': 100, 'output_tokens': 50,
           'inputs_sha256': 'a' * 64, 'native_source_inventory_sha256': native,
           'platform_source_binding_sha256': platform, 'screen_policy_sha256': policy,
           'waiting_controller_count_stable': True, 'verified_delivery': True,
           'first_native_final_result': {'id': 'synthetic'}, 'first_excel_bundle': {'id': 'synthetic'}}
    perf_row = {'wall_seconds': 60, 'first_excel_seconds': 30, 'model_rounds': 4,
                'logical_controller_rounds': 4, 'batch_tool_calls': 3, 'api_retries': 0,
                'total_tokens': 150, 'inputs_sha256': 'a' * 64, 'hardware': 'synthetic',
                'load_state': 'cold', 'controller': {'model': 'synthetic'}, 'export_options': {}}
    browser_cases = ('bulk_import_progress', 'batch_pause_resume', 'grouped_review',
        'precision_current_revision', 'per_image_excel_zip', 'preference_next_boundary', 'stale_delivery_warning')
    browser_reference = write(tmp_path / 'reports/browser-case.json', {'synthetic': True})
    raw = {
        'packaging_contract': {'verification': {'format': 'clear-agent-packaging-contract-v1',
            'status': 'passed', 'cpu_only': True, 'passed_checks': 12, 'failed_checks': 0,
            'tested_inventory_sha256': inventory_sha(tested)}},
        'agent_gpu_chain': {'run': run},
        'heldout_quality': {'quality': quality(240, 120, 120),
            'baseline_quality': quality(240, 0, 240, errors=True),
            'challenge_quality': quality(24, 6, 18),
            'comparison': {'totals': {'new_silent_critical_release_errors': 0, 'net_visible_cell_repairs': 20}},
            'dataset_qualification': {'format': 'specialized-agent-eval-v1', 'status': 'passed',
                'old_public_document_overlap': 0, 'old_source_hash_overlap': 0,
                'old_chinese_family_overlap': 0, 'development_validation_holdout_group_overlap': 0,
                'sets': {'holdout': {'inputs_sha256': 'a' * 64, 'truth_sha256': 'b' * 64},
                         'challenge': {'inputs_sha256': 'c' * 64, 'truth_sha256': 'd' * 64}}}},
        'excel_readback': {'readback': {'format': 'specialized-agent-eval-v1', 'status': 'passed',
            'fidelity_status': 'passed', 'freshness_status': 'passed',
            'current_delivery_verified': True, 'workbooks': 238, 'inputs': 240, 'no_book': 2,
            'records': [{'status': 'passed' if position < 238 else 'no_book'} for position in range(240)]}},
        'native_recovery': {'recovery': {'format': 'screen-native-eight-recovery-result-v1',
            'status': 'passed', 'scope_inputs': 8, 'truth_read': False, 'frozen_results_loaded': False,
            'prefilled_cache': False, 'interrupted_attempt_preserved': True,
            'same_batch_and_resume_idempotent': True, 'no_duplicate_batch_or_final_content': True,
            'native_source_inventory_sha256': native, 'run_policy_sha256': policy,
            'worker_release': [{'released': True}], 'service_exit': {
                'actual_app_lifespans_closed': 2, 'queue_alive': False, 'coordinator_alive': False,
                'fusion_queue_alive': False, 'external_queue_alive': False, 'document_threads_alive': False}},
            'prepared': {'native_source_inventory_sha256': native, 'source_default_policy_sha256': policy,
                'run_policy_sha256': policy, 'run_policy': {}, 'native_source': str(sources.parent / 'ocr')},
            'run_policy': {}},
        'performance_comparison': {'comparison': {'format': 'clear-agent-performance-comparison-v1',
            'status': 'passed', 'paired_inputs_verified': True, 'actual_controller_and_models': True,
            'scope_inputs': 240, 'ordinary_batch': True, 'waiting_controller_count_stable': True,
            'baseline': perf_row, 'candidate': {**perf_row, 'wall_seconds': 50, 'first_excel_seconds': 15}},
            'baseline_run': run, 'candidate_run': run},
        'supported_cross_tool_tasks': {'tasks': {'format': 'clear-agent-cross-tool-qualification-v1',
            'status': 'passed', 'actual_controller': True, 'production_tools_only': True,
            'tasks': [{'name': name, 'status': 'passed', 'actual_result_reads': True,
                'source_evidence_verified': True, 'operation_ids': ['synthetic'], 'artifact_ids': ['synthetic']}
                for name in ('published_model_classification', 'ocr_to_data_to_evidence')]}},
        'cold_backup_restore': {'restore': {'format': 'clear-agent-screen-cold-restore-v1',
            'status': 'passed', 'source_services_stopped': True, 'actual_screen_data': True,
            'backup_verified': True, 'restored_artifact_downloads_verified': True,
            'source': str(tmp_path / 'cold-source'), 'restored': str(tmp_path / 'cold-new'),
            'source_version_vector': {'snapshot_id': 'synthetic', 'batch_id': 'synthetic',
                'project_id': 'synthetic', 'policy_sha256': policy, 'export_options': {},
                'items': [{'input_version_id': 'synthetic', 'result_id': 'synthetic', 'result_revision': 1,
                    'current_revision': 1, 'selected_result_id': 'synthetic', 'review_epoch': 'synthetic',
                    'review_result_id': None, 'review_revision': None}]},
            'source_preferences': {'output': 'per_image'}, 'restored_preferences': {'output': 'per_image'}}},
        'browser': {'browser': {'format': 'clear-agent-browser-qualification-v1',
            'status': 'passed', 'browser_validated': True,
            'cases': [{'name': name, 'status': 'passed', 'evidence': [browser_reference]} for name in browser_cases]}},
    }
    raw['cold_backup_restore']['restore']['restored_version_vector'] = raw['cold_backup_restore']['restore']['source_version_vector']
    raw['cold_backup_restore']['restore'].update(synthetic_cold_reads(sources, tested, tmp_path))
    gates = {}
    for gate, evidence in raw.items():
        references = {name: write(tmp_path / f'reports/{gate}-{name}.json', value)
                      for name, value in evidence.items()}
        gates[gate] = write(tmp_path / f'reports/{gate}.json', {
            'format': 'clear-agent-qualification-gate-v1', 'gate': gate, 'status': 'passed',
            'product_version': '0.9.5', 'tested_files': tested, 'evidence': references,
            'measurements': {'split': 'heldout', 'truth_used_by_controller': False} if gate == 'heldout_quality' else {},
        })
    index = tmp_path / 'qualification-index.json'
    write(index, {'format': 'clear-agent-qualification-index-v1', 'product_version': '0.9.5', 'gates': gates})
    return index


def rewrite_gate(index, gate, edit):
    entries = json.loads(index.read_text(encoding='utf-8'))
    path = Path(entries['gates'][gate]['path'])
    contents = json.loads(path.read_text(encoding='utf-8'))
    edit(contents)
    entries['gates'][gate] = write(path, contents)
    write(index, entries)


def rewrite_evidence(index, gate, name, edit):
    def change(report):
        ref = report['evidence'][name]
        path = Path(ref['path'])
        value = json.loads(path.read_text(encoding='utf-8'))
        edit(value)
        report['evidence'][name] = write(path, value)
    rewrite_gate(index, gate, change)


def rewrite_cold_reference(index, name, edit):
    def change(restore):
        reference = restore[name]
        path = Path(reference['path'])
        value = json.loads(path.read_text(encoding='utf-8'))
        edit(value)
        restore[name] = write(path, value)
    rewrite_evidence(index, 'cold_backup_restore', 'restore', change)


def status(sources, index):
    return qualification_status(sources, index, '0.9.5')


def test_synthetic_positive_contract_does_not_run_or_package_models(sources, qualified_index):
    result = status(sources, qualified_index)
    assert result['status'] == 'qualified'
    assert result['required_for_final'] is True
    assert set(result['gates']) == set(QUALIFICATION_GATES)
    assert all(row['status'] == 'passed' for row in result['gates'].values())


def test_missing_reports_are_explicit_engineering_metadata(sources):
    result = qualification_status(sources, product_version='0.9.5')
    assert result['status'] == 'unqualified'
    assert all(row['status'] == 'missing' for row in result['gates'].values())


def test_final_new_skill_rejected_before_output_exists(sources, tmp_path):
    pdf = tmp_path / 'pdf'
    text_file(pdf / '__init__.py')
    output = tmp_path / 'uncreated-final'
    with pytest.raises(ValueError, match='Final screen-batch-excel requires actual qualification'):
        package(sources, tmp_path / 'program', tmp_path / 'layer', output,
                pdf_layer=pdf, product_version='0.9.5')
    assert not output.exists()


@pytest.mark.parametrize('changed', [
    'platform/src/workbench_platform/example.py', 'ocr/config/screen-policy.json',
    'platform/config/skills/screen-batch-excel/SKILL.md', 'platform/apps/portal/src/main.tsx',
    'platform/apps/portal/dist/index.html', 'ocr/frontend/dist/index.html',
    'classification/apps/web/dist/index.html',
])
def test_source_policy_skill_and_frontend_drift_invalidates(sources, qualified_index, changed):
    text_file(sources.parent / changed, 'changed after tests')
    assert all(row['status'] == 'invalid' for row in status(sources, qualified_index)['gates'].values())


def test_sha_tamper_rejected_even_when_report_claims_pass(sources, qualified_index):
    index = json.loads(qualified_index.read_text(encoding='utf-8'))
    text_file(Path(index['gates']['browser']['path']), '{}')
    result = status(sources, qualified_index)
    assert result['gates']['browser']['status'] == 'invalid'
    assert 'missing or changed' in result['gates']['browser']['reason']


@pytest.mark.parametrize('gate', ['performance_comparison', 'supported_cross_tool_tasks', 'cold_backup_restore', 'browser'])
def test_each_missing_additional_gate_blocks_final_qualification(sources, qualified_index, gate):
    value = json.loads(qualified_index.read_text(encoding='utf-8'))
    del value['gates'][gate]
    write(qualified_index, value)
    assert status(sources, qualified_index)['gates'][gate]['status'] == 'missing'
    assert status(sources, qualified_index)['status'] == 'unqualified'


def test_bare_pass_boolean_is_not_evidence(sources, qualified_index):
    rewrite_gate(qualified_index, 'agent_gpu_chain', lambda report: report.update(evidence={'run': {'passed': True}}))
    assert status(sources, qualified_index)['gates']['agent_gpu_chain']['status'] == 'invalid'


def test_cpu_pass_cannot_masquerade_as_native_gpu_chain(sources, qualified_index):
    rewrite_evidence(qualified_index, 'agent_gpu_chain', 'run', lambda report: report.update(
        format='clear-agent-packaging-contract-v1', cpu_only=True))
    result = status(sources, qualified_index)
    assert result['gates']['packaging_contract']['status'] == 'passed'
    assert result['gates']['agent_gpu_chain']['status'] == 'invalid'


def test_twelve_input_development_run_is_not_final(sources, qualified_index):
    rewrite_evidence(qualified_index, 'agent_gpu_chain', 'run', lambda report: report.update(scope_inputs=12))
    assert status(sources, qualified_index)['gates']['agent_gpu_chain']['status'] == 'invalid'


def test_actual_run_bindings_cannot_be_replaced_by_wrapper_bindings(sources, qualified_index):
    rewrite_evidence(qualified_index, 'agent_gpu_chain', 'run', lambda report: report.update(screen_policy_sha256='b' * 64))
    assert status(sources, qualified_index)['gates']['agent_gpu_chain']['status'] == 'invalid'


def test_main_chain_must_share_frozen_quality_inputs(sources, qualified_index):
    rewrite_evidence(qualified_index, 'agent_gpu_chain', 'run', lambda report: report.update(inputs_sha256='e' * 64))
    assert status(sources, qualified_index)['gates']['agent_gpu_chain']['status'] == 'invalid'


def test_old_dataset_or_split_drift_cannot_be_called_new_heldout(sources, qualified_index):
    rewrite_evidence(qualified_index, 'heldout_quality', 'dataset_qualification',
                     lambda report: report.update(development_validation_holdout_group_overlap=1))
    assert status(sources, qualified_index)['gates']['heldout_quality']['status'] == 'invalid'


def test_partial_excel_readback_cannot_qualify_full_batch(sources, qualified_index):
    rewrite_evidence(qualified_index, 'excel_readback', 'readback', lambda report: report.update(inputs=12))
    assert status(sources, qualified_index)['gates']['excel_readback']['status'] == 'invalid'


def test_truncated_matching_vectors_do_not_qualify_cold_restore(sources, qualified_index):
    rewrite_evidence(qualified_index, 'cold_backup_restore', 'restore', lambda report: report.update(
        source_version_vector={'revision': 1}, restored_version_vector={'revision': 1}))
    assert status(sources, qualified_index)['gates']['cold_backup_restore']['status'] == 'invalid'


@pytest.mark.parametrize('field,value', [
    ('automatic_images', 0), ('automatic_error_images', 1), ('automatic_critical_error_cells', 1),
    ('automatic_critical_error_images', 1), ('automatic_uncertain_images', 1),
    ('contradictory_automatic_and_review_images', 1),
])
def test_unsafe_automatic_subset_cannot_pass(sources, qualified_index, field, value):
    rewrite_evidence(qualified_index, 'heldout_quality', 'quality',
                     lambda report: report['summary']['totals'].update({field: value}))
    assert status(sources, qualified_index)['gates']['heldout_quality']['status'] == 'invalid'


def test_positive_repairs_do_not_cancel_new_silent_critical_release(sources, qualified_index):
    rewrite_evidence(qualified_index, 'heldout_quality', 'comparison', lambda report: report['totals'].update(
        new_silent_critical_release_errors=1, net_visible_cell_repairs=99))
    assert status(sources, qualified_index)['gates']['heldout_quality']['status'] == 'invalid'


def test_prior_critical_error_moved_out_of_review_is_independently_detected(sources, qualified_index):
    rewrite_evidence(qualified_index, 'heldout_quality', 'quality', lambda report: report['records'][0]['errors'].append({
        'table_index': 0, 'row': 1, 'column': 1, 'visibility': 'visible', 'critical': True, 'reviewed': False}))
    rewrite_evidence(qualified_index, 'heldout_quality', 'comparison',
                     lambda report: report['totals'].update(net_visible_cell_repairs=19))
    result = status(sources, qualified_index)
    assert result['gates']['heldout_quality']['status'] == 'invalid'
    assert 'moved out of review' in result['gates']['heldout_quality']['reason']


def test_reported_net_repairs_must_match_paired_actual_errors(sources, qualified_index):
    rewrite_evidence(qualified_index, 'heldout_quality', 'comparison',
                     lambda report: report['totals'].update(net_visible_cell_repairs=99))
    assert 'Net repair count differs' in status(sources, qualified_index)['gates']['heldout_quality']['reason']


def test_automatic_coverage_alone_is_not_workload_improvement(sources, qualified_index):
    rewrite_evidence(qualified_index, 'heldout_quality', 'quality', lambda report: report['summary']['totals'].update(
        review_images=240, review_reference_cells=2400))
    result = status(sources, qualified_index)
    assert 'does not prove reduced review workload' in result['gates']['heldout_quality']['reason']


def test_challenge_set_is_required_and_cannot_reuse_main_scope(sources, qualified_index):
    rewrite_evidence(qualified_index, 'heldout_quality', 'challenge_quality', lambda report: report['summary']['totals'].update(images=240))
    assert status(sources, qualified_index)['gates']['heldout_quality']['status'] == 'invalid'


def test_queue_recovery_cannot_substitute_cold_backup_restore(sources, qualified_index):
    rewrite_gate(qualified_index, 'cold_backup_restore', lambda report: report['evidence'].update(
        restore=json.loads(Path(json.loads(qualified_index.read_text())['gates']['native_recovery']['path']).read_text())['evidence']['recovery']))
    assert status(sources, qualified_index)['gates']['cold_backup_restore']['status'] == 'invalid'


@pytest.mark.parametrize('change', [
    {'diagnostics_version': 'screen-diagnostics-v1'},
    {'consumer_source_bytes_unchanged': False},
    {'original_source_files_and_sha_unchanged': False},
    {'actual_native_consumer_reads': {}},
])
def test_old_or_flag_only_cold_proof_cannot_qualify(sources, qualified_index, change):
    rewrite_evidence(qualified_index, 'cold_backup_restore', 'restore', lambda report: report.update(change))
    assert status(sources, qualified_index)['gates']['cold_backup_restore']['status'] == 'invalid'


def test_cold_consumer_inventory_must_match_candidate_bytes(sources, qualified_index):
    rewrite_cold_reference(qualified_index, 'consumer_source_inventory', lambda report: report.update(
        files_sha256={name: 'b' * 64 for name in report['files_sha256']}))
    assert status(sources, qualified_index)['gates']['cold_backup_restore']['status'] == 'invalid'


def test_frozen_cold_module_cannot_be_changed_after_the_run(sources, qualified_index):
    def change(report):
        path = Path(report['actual_native_consumer_reads']['restored']['runtime']['loaded_native_modules']
                    ['ocr_workbench.screen_diagnostics']['path'])
        text_file(path, 'Changed after purported consumer execution')
    rewrite_evidence(qualified_index, 'cold_backup_restore', 'restore', change)
    assert status(sources, qualified_index)['gates']['cold_backup_restore']['status'] == 'invalid'


@pytest.mark.parametrize('change', [
    {'start_queue': True}, {'agent_enabled': True}, {'forbidden_data_roots': []},
    {'loaded_native_modules': {}}, {'executable_sha256': '0' * 64},
])
def test_actual_cold_runtime_must_bind_the_frozen_consumer_and_isolation(sources, qualified_index, change):
    rewrite_evidence(qualified_index, 'cold_backup_restore', 'restore', lambda report:
        report['actual_native_consumer_reads']['restored']['runtime'].update(change))
    assert status(sources, qualified_index)['gates']['cold_backup_restore']['status'] == 'invalid'


def test_actual_pixel_frame_cannot_come_from_another_same_named_module(sources, qualified_index, tmp_path):
    other = tmp_path / 'unbound/screen_diagnostics.py'
    text_file(other)
    def change(report):
        audit = report['actual_native_consumer_reads']['restored']
        path = Path(audit['path'])
        event = json.loads(path.read_text(encoding='utf-8'))
        event['frames'][0]['file'] = str(other)
        text_file(path, json.dumps(event) + '\n')
        audit['sha256'] = sha(path)
    rewrite_evidence(qualified_index, 'cold_backup_restore', 'restore', change)
    assert status(sources, qualified_index)['gates']['cold_backup_restore']['status'] == 'invalid'


@pytest.mark.parametrize('forbidden_marker', [False, True])
def test_restored_native_file_reads_cannot_escape_back_to_upstream(sources, qualified_index, forbidden_marker):
    def change(report):
        audit = report['actual_native_consumer_reads']['restored']
        path = Path(audit['path'])
        event = json.loads(path.read_text(encoding='utf-8'))
        event['path'] = str(Path(report['source_run']) / 'native/ocr/original.png')
        event['forbidden_old_scope'] = report['source_run'] if forbidden_marker else None
        text_file(path, json.dumps(event) + '\n')
        audit['sha256'] = sha(path)
    rewrite_evidence(qualified_index, 'cold_backup_restore', 'restore', change)
    assert status(sources, qualified_index)['gates']['cold_backup_restore']['status'] == 'invalid'


def test_isolation_flag_cannot_replace_the_actual_absence_of_owned_source(sources, qualified_index):
    def change(report):
        isolation = json.loads(Path(report['actual_cold_source_isolation']['path']).read_text(encoding='utf-8'))
        Path(isolation['old_owned_source_root']).mkdir(parents=True)
    rewrite_evidence(qualified_index, 'cold_backup_restore', 'restore', change)
    assert status(sources, qualified_index)['gates']['cold_backup_restore']['status'] == 'invalid'


def test_recovery_accepts_equal_policy_with_different_serialization_bytes(sources, qualified_index):
    # Actual recovery driver serializes its run policy separately from the default.
    index = json.loads(qualified_index.read_text())
    gate = json.loads(Path(index['gates']['native_recovery']['path']).read_text())
    run_policy = Path(gate['evidence']['run_policy']['path'])
    text_file(run_policy, '{\n}\n')
    digest = sha(run_policy)
    rewrite_gate(qualified_index, 'native_recovery',
                 lambda report: report['evidence']['run_policy'].update(sha256=digest))
    rewrite_evidence(qualified_index, 'native_recovery', 'recovery',
                     lambda report: report.update(run_policy_sha256=digest))
    rewrite_evidence(qualified_index, 'native_recovery', 'prepared',
                     lambda report: report.update(run_policy_sha256=digest))
    assert status(sources, qualified_index)['gates']['native_recovery']['status'] == 'passed'


def test_recovery_actual_run_policy_cannot_differ_from_final_default(sources, qualified_index):
    rewrite_evidence(qualified_index, 'native_recovery', 'prepared',
                     lambda report: report.update(run_policy={'primary_engine': 'unverified'}))
    assert status(sources, qualified_index)['gates']['native_recovery']['status'] == 'invalid'


@pytest.mark.parametrize('change', [
    {'logical_controller_rounds': 6}, {'batch_tool_calls': 9}, {'load_state': 'warm'},
    {'wall_seconds': 70}, {'human_review_seconds': 10},
])
def test_performance_comparison_requires_budgets_matched_conditions_and_actual_time(sources, qualified_index, change):
    rewrite_evidence(qualified_index, 'performance_comparison', 'comparison',
                     lambda report: report['candidate'].update(change))
    assert status(sources, qualified_index)['gates']['performance_comparison']['status'] == 'invalid'


def test_browser_proof_file_is_verified(sources, qualified_index):
    rewrite_evidence(qualified_index, 'browser', 'browser', lambda report: report['cases'][0]['evidence'][0].update(sha256='0' * 64))
    assert status(sources, qualified_index)['gates']['browser']['status'] == 'invalid'


def test_first_output_improvement_can_trade_total_time_with_explicit_scope(sources, qualified_index):
    rewrite_evidence(qualified_index, 'performance_comparison', 'comparison', lambda report: report.update(
        candidate={**report['candidate'], 'wall_seconds': 70},
        tradeoff={'reason': 'Measured grouping releases early files while total queue work grows',
                  'adoption_scope': 'First-output priority in the measured 240-input batch'}))
    assert status(sources, qualified_index)['gates']['performance_comparison']['status'] == 'passed'


def test_slower_accuracy_mode_requires_real_quality_comparison_and_scope(sources, qualified_index):
    entries = json.loads(qualified_index.read_text())
    quality_gate = json.loads(Path(entries['gates']['heldout_quality']['path']).read_text())
    rewrite_gate(qualified_index, 'performance_comparison', lambda report: report['evidence'].update(
        quality_comparison=quality_gate['evidence']['comparison']))
    rewrite_evidence(qualified_index, 'performance_comparison', 'comparison', lambda report: report.update(
        adoption_mode='accuracy', candidate={**report['candidate'], 'wall_seconds': 90, 'first_excel_seconds': 40},
        tradeoff={'reason': 'Actual quality repairs require extra bounded model work',
                  'adoption_scope': 'Explicit improve-accuracy mode; default route remains separately assessed'}))
    assert status(sources, qualified_index)['gates']['performance_comparison']['status'] == 'passed'


def test_legacy_version_default_and_explicit_new_version_are_preserved():
    required = [option for name in ('source', 'program', 'layer', 'output') for option in ('--' + name, 'F:/synthetic/' + name)]
    assert build_parser().parse_args(required).product_version == '0.9.4'
    assert build_parser().parse_args(required + ['--product-version', '0.9.5']).product_version == '0.9.5'


def test_index_for_old_product_version_is_invalid(sources, qualified_index):
    result = qualification_status(sources, qualified_index, '0.9.4')
    assert result['status'] == 'unqualified'
    assert all(row['status'] == 'invalid' for row in result['gates'].values())
