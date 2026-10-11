"""Counterexamples for independent attempt retention and source route binding."""
from copy import deepcopy
import hashlib
import json

import pytest

from precision_route_readback import expected_route, verify_routed_ledger


@pytest.fixture
def ledger(tmp_path):
    source = tmp_path / 'original.png'
    source.write_bytes(b'explicit synthetic source bytes; no model or accuracy claim')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    info = {'input_path': str(source), 'input_sha256': digest, 'preparation_seconds': 5,
        'business_regions': [{'table_box': [0, 10, 100, 70]}]}
    evidence = {'provenance': {'contains_reference_input': False, 'image_version': 'v1',
        'input_image_version': 'v1', 'source_sha256': digest, 'coordinate_space': 'exif_normalized_original'},
        'text': [{'text': 'abcdefghijklmnopqrstuvwxyz', 'source': 'PP-OCR-synthetic', 'box': [1, 20, 99, 40]}]}
    route = expected_route(info, evidence, digest, 'v1')
    info['engine_route_snapshot'] = route
    policy = {'budget_seconds': 360, 'batch_budget_seconds': 3600, 'technical_fallback_reserve_seconds': 90}
    items = {'v1': {'preparation': json.dumps(info), 'evidence': json.dumps(evidence),
        'source_hash': digest, 'status': 'needs_review'}}
    tasks = [{'id': 'context', 'kind': 'screen_context', 'engine': 'geometry', 'status': 'succeeded'},
        {'id': 'g', 'kind': 'ocr', 'engine': 'glm', 'status': 'failed', 'error': 'synthetic timeout'},
        {'id': 'h', 'kind': 'ocr', 'engine': 'hunyuan', 'status': 'succeeded'}]
    contexts = [{'input_version_id': 'v1', 'task_id': 'context', 'spent_seconds': 20}]
    links = []
    for index, (task, cost, timeout, reserve) in enumerate([('g', 240, 245, 90), ('h', 30, 90, 0)]):
        links.append({'input_version_id': 'v1', 'task_id': task, 'pass_index': index,
            'prepared_id': 'primary', 'attempt': 0, 'spent_seconds': cost,
            'metadata': json.dumps({'engine_route_snapshot': route, 'input_version_id': 'v1',
                'technical_fallback_reserved_seconds': reserve, 'timeout_seconds': timeout})})
    return policy, items, tasks, contexts, links


def test_keeps_failed_primary_and_all_original_costs(ledger):
    models, audit = verify_routed_ledger(*ledger)
    assert [task['id'] for task in models] == ['g', 'h']
    assert audit['attempt_states'] == {'failed': 1, 'succeeded': 1}
    assert audit['records'][0]['actual_seconds'] == 295
    assert audit['records'][0]['original_remaining_seconds'] == 65
    assert audit['source_budget_overshoots'] == {}
    assert audit['failed_attempts_retained'] and not audit['production_route_imported']


@pytest.mark.parametrize('mutation', ['engine', 'reserve', 'snapshot', 'envelope', 'success_then_fallback', 'duplicate', 'omitted_primary', 'missing_input'])
def test_rejects_tampered_or_incomplete_attempts(ledger, mutation):
    policy, items, tasks, contexts, links = deepcopy(ledger)
    if mutation == 'engine': tasks[1]['engine'] = 'hunyuan'
    if mutation in {'reserve', 'snapshot', 'envelope'}:
        metadata = json.loads(links[0]['metadata'])
        if mutation == 'reserve': metadata['technical_fallback_reserved_seconds'] = 0
        if mutation == 'snapshot': metadata['engine_route_snapshot']['source_evidence_sha256'] = 'changed'
        if mutation == 'envelope': metadata['timeout_seconds'] = 360
        links[0]['metadata'] = json.dumps(metadata)
    if mutation == 'success_then_fallback': tasks[1]['status'] = 'succeeded'
    if mutation == 'duplicate': links.append(deepcopy(links[0]))
    if mutation == 'omitted_primary':
        tasks.remove(tasks[1]); links.remove(links[0])
    if mutation == 'missing_input': contexts.clear()
    with pytest.raises(ValueError): verify_routed_ledger(policy, items, tasks, contexts, links)


def test_budget_overshoot_is_visible_and_never_extended(ledger):
    policy, items, tasks, contexts, links = deepcopy(ledger)
    links[1]['spent_seconds'] = 120
    _, audit = verify_routed_ledger(policy, items, tasks, contexts, links)
    assert audit['records'][0]['actual_seconds'] == 385
    assert list(audit['source_budget_overshoots'].values()) == [25]
    assert audit['records'][0]['original_remaining_seconds'] == 0
    assert not audit['budgets_extended']


def test_explicit_original_budget_skip_permits_one_h_attempt(ledger):
    policy, items, tasks, contexts, links = deepcopy(ledger)
    tasks.remove(tasks[1]); links.remove(links[0])
    contexts[0]['spent_seconds'] = 270
    info = json.loads(items['v1']['preparation'])
    info['engine_route_primary_skip'] = {'reason': 'primary_envelope_exhausted', 'model_invoked': False,
        'fallback_reserve_seconds': 90, 'original_remaining_seconds': 85}
    items['v1']['preparation'] = json.dumps(info)
    models, audit = verify_routed_ledger(policy, items, tasks, contexts, links)
    assert [model['id'] for model in models] == ['h']
    assert audit['records'][0]['primary_budget_skip']['model_invoked'] is False
    assert audit['records'][0]['actual_seconds'] == 305
