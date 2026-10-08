"""Engineering-only negative cases for source/denominator/timing comparisons."""
from copy import deepcopy

import pytest

from common import frozen_json
from compare_native_routes import compare, finite_seconds, read_run, validate_comparable


def example_runs():
    # Synthetic contract fixture, never registered as a model qualification.
    policy = {'primary_engine': 'hunyuan', 'group_size': 4, 'context_schedule': 'whole_batch'}
    row = {'id': 'example', 'metrics': {'review_images': 1, 'review_reference_cells': 2,
        'automatic_images': 0}, 'errors': [{'table_index': 0, 'row': 0, 'column': 0,
        'visibility': 'visible', 'critical': True, 'reviewed': True}]}
    before = {
        'prepared': {'inputs_sha256': 'a'*64, 'input_count': 12,
            'platform_source_binding_sha256': 'b'*64, 'bundle_engines_sha256': 'c'*64,
            'native_source_files': {'config/screen-policy.json': 'd'*64, 'src/example.py': 'e'*64}},
        'summary': {'native_batches': [{'policy': policy}]},
        'hardware': {'0': {'name': 'fixture-gpu', 'memory_total_mib': 1000}},
        'quality': {'bindings': {'truth_sha256': 'f'*64}, 'records': [row],
            'summary': {'totals': {'automatic_error_images': 0,
                'contradictory_automatic_and_review_images': 0}}},
        'metrics': {'route_wall_seconds': 30, 'first_native_observed_seconds': 20,
            'first_native_since_agent_submit_seconds': 10, 'input_count': 12},
        'evidence': {},
    }
    after = deepcopy(before)
    after['prepared']['native_source_files']['config/screen-policy.json'] = '0'*64
    after['summary']['native_batches'][0]['policy']['context_schedule'] = 'grouped'
    after['metrics'].update(route_wall_seconds=35, first_native_observed_seconds=18,
                            first_native_since_agent_submit_seconds=8)
    return before, after


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True, None, '1'])
def test_unobserved_or_invalid_time_is_never_reported_as_zero(value):
    with pytest.raises(ValueError):
        finite_seconds(value, 'test')


@pytest.mark.parametrize('mutation', [
    'inputs', 'platform', 'model_assets', 'hardware', 'truth', 'source',
    'extra_source', 'uncontrolled_policy', 'inert_group',
])
def test_rejects_scope_source_hardware_or_uncontrolled_contrast(mutation):
    before, after = example_runs()
    if mutation in {'inputs', 'platform', 'model_assets'}:
        key = {'inputs': 'inputs_sha256', 'platform': 'platform_source_binding_sha256',
               'model_assets': 'bundle_engines_sha256'}[mutation]
        after['prepared'][key] = '9'*64
    elif mutation == 'hardware':
        after['hardware']['0']['name'] = 'another-gpu'
    elif mutation == 'truth':
        after['quality']['bindings']['truth_sha256'] = '9'*64
    elif mutation == 'source':
        after['prepared']['native_source_files']['src/example.py'] = '9'*64
    elif mutation == 'extra_source':
        after['prepared']['native_source_files']['src/additional.py'] = '9'*64
    elif mutation == 'uncontrolled_policy':
        after['summary']['native_batches'][0]['policy']['primary_engine'] = 'glm'
    else:
        for value in (before, after):
            value['summary']['native_batches'][0]['policy']['group_size'] = 32
    with pytest.raises(ValueError):
        validate_comparable(before, after, 'schedule')


def test_first_candidate_improvement_does_not_hide_total_time_tradeoff():
    before, after = example_runs()
    result = compare(before, after, 'schedule')
    assert result['measurements']['first_native_improved']
    assert result['measurements']['whole_route_delta_seconds'] == 5
    assert result['default_route_selected'] is False
    assert any('downloaded Excel' in item for item in result['limitations'])


@pytest.mark.parametrize('regression', ['extra_tables', 'shape_errors', 'metadata_missing_targets'])
def test_cell_repairs_cannot_hide_extra_tables_or_lost_metadata(regression):
    before, after = example_runs()
    after['quality']['records'][0]['errors'] = []
    after['quality']['records'][0]['metrics'][regression] = 1
    result = compare(before, after, 'schedule')
    assert result['paired_quality']['totals']['net_visible_cell_repairs'] == 1
    assert result['measurements']['per_input_content_no_regression'] is False


@pytest.mark.parametrize('field,value', [
    ('truth_read', True), ('frozen_results_loaded', True),
    ('shared_context_injected', True), ('real_queue_and_engine_adapter', False),
])
def test_replay_or_truth_injection_is_rejected_before_loading_evidence(tmp_path, field, value):
    run = tmp_path/'replayed'
    prepared = {'prepared_only': False}
    summary = {'truth_read': False, 'frozen_results_loaded': False,
        'shared_context_injected': False, 'real_queue_and_engine_adapter': True, 'run_error': None}
    summary[field] = value
    frozen_json(run/'prepared.json', prepared)
    frozen_json(run/'agent-native-summary.json', summary)
    with pytest.raises(ValueError, match='non-replay'):
        read_run(run, run/'nonexistent-quality.json', run/'nonexistent-observations.json', run/'nonexistent-readback.json')
