"""Control-plane checks that never start OCR, a browser, or a real service."""
import asyncio
from collections import Counter
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.agent_eval.mixed_service_bridge import Bridge, QUERY_IDS, QUERIES, RemoteStore, canonical, control_app
from scripts.agent_eval.mixed_stability import Audit, Metrics
from scripts.agent_eval.assess_mixed_stability import assess, publish


class FakeStore:
    def rows(self, sql, params=()):
        return [{'sql': sql, 'params': list(params)}]

    def one(self, table, key):
        return {'id': key}


class FakeAudit:
    bridge_token = 'only-this-process'
    phase = 'setup'
    stop = False
    ui_enabled = False
    base = 'http://127.0.0.1:12345'
    project = 'project'
    photos = ['photo']
    versions = ['version']
    measured_versions = ['version']
    visual_result = 'result'
    visual_version = 'version'
    visual_model = 'visual'
    sessions = {}
    server_task = None
    requests = Counter({'run': 1})
    ui_counts = Counter({'1': 2})

    def __init__(self):
        self.report = {'input_sha256': 'digest', 'ui_clients': {}, 'visual_requests_count': 1}
        self.metrics = SimpleNamespace(report=lambda: {'soak:ui_progress': {'p95_ms': 1500}})
        queue = SimpleNamespace(status=lambda: {'healthy': True}, current=None, adapter=None)
        self.app = SimpleNamespace(state=SimpleNamespace(queue=queue, external_queue=queue,
                              agent_runtime=None, store=FakeStore()))

    async def start(self, enabled):
        self.enabled = enabled

    async def stop_server(self):
        pass


def check_query_whitelist_and_transport_are_distinct():
    assert len(QUERY_IDS) == len(QUERIES)
    assert QUERY_IDS[canonical(' SELECT   COUNT(*) n FROM tasks WHERE engine=\'glm\' ')] == 'task_count'
    class Stub:
        def call(self, op, **kwargs):
            assert op == 'rows' and kwargs['query'] == 'task_count'
            return {'rows': [{'n': 4}]}
    store = RemoteStore(Stub(), 'unused')
    assert store.rows(QUERIES['task_count']) == [{'n': 4}]
    try:
        store.rows('DROP TABLE tasks')
    except KeyError:
        pass
    else:
        raise AssertionError('Arbitrary SQL crossed the bridge')


def check_control_auth_state_counts_and_lifecycle():
    audit = FakeAudit()
    finished = asyncio.Event()
    with TestClient(control_app(audit, finished)) as client:
        assert client.post('/api/audit/bridge', json={'operation': 'counts'}).status_code == 401
        client.headers['Authorization'] = 'Bearer only-this-process'
        start = client.post('/api/audit/bridge', json={'operation': 'start',
                            'enabled': True, 'phase': 'warmup_on'})
        assert start.status_code == 200 and start.json()['pid'] != 0
        assert audit.enabled and audit.phase == 'warmup_on'
        state = client.post('/api/audit/bridge', json={'operation': 'state'}).json()
        assert state['queues']['queue']['status']['healthy']
        assert state['ui_counts'] == {'1': 2}
        assert state['metrics']['soak:ui_progress']['p95_ms'] == 1500
        assert client.post('/api/audit/bridge', json={'operation': 'counts'}).json()['requests'] == {'run': 1}
        assert client.post('/api/audit/bridge', json={'operation': 'rows',
                           'query': 'task_count', 'params': []}).json()['rows'][0]['params'] == []
        assert client.post('/api/audit/bridge', json={'operation': 'rows',
                           'query': 'malicious', 'params': []}).status_code == 400
        assert client.post('/api/audit/bridge', json={'operation': 'configure',
                           'phase': 'soak', 'ui_enabled': True, 'stop': True}).status_code == 200
        assert (audit.phase, audit.ui_enabled, audit.stop) == ('soak', True, True)
        assert client.post('/api/audit/bridge', json={'operation': 'stop'}).json()['closed']
        assert client.post('/api/audit/bridge', json={'operation': 'shutdown'}).status_code == 200
        assert finished.is_set()


def check_final_receipt_keeps_child_progress_and_parent_collision(tmp_path):
    class BridgeStub:
        def call(self, operation):
            assert operation == 'state'
            return {'metrics': {
                'warmup_on:api': {'samples': 2, 'p95_ms': 999},
                'mixed_two_ui:ui_progress': {'samples': 3, 'p95_ms': 1200},
                'soak:ui_progress': {'samples': 4, 'p95_ms': 1300}},
                'ui_counts': {'1': 3, '2': 4}, 'ui_clients': {'1': {}, '2': {}}}

    audit = Audit.__new__(Audit)
    audit.receipt_lock = threading.RLock()
    audit.metrics = Metrics()
    audit.metrics.add('warmup_on:api', .001)
    audit.child_metrics = {}
    audit.bridge = BridgeStub()
    audit.app = SimpleNamespace(state=SimpleNamespace(store=SimpleNamespace(root=tmp_path)))
    audit.out = tmp_path
    audit.phase = 'soak'
    audit.run_counts = Counter()
    audit.ui_counts = Counter()
    audit.report = {'ui_clients': {}}
    audit.save()
    audit.bridge = None
    audit.save()  # This is the actual post-shutdown overwrite path.
    raw = json.loads((tmp_path / 'mixed-stability.json').read_text(encoding='utf-8'))
    assert raw['metrics']['warmup_on:api']['p95_ms'] == 1
    assert raw['metrics']['service:warmup_on:api']['p95_ms'] == 999
    assert raw['metrics']['mixed_two_ui:ui_progress']['samples'] == 3
    assert raw['metrics']['soak:ui_progress']['samples'] == 4
    assert raw['ui_counts'] == {'1': 3, '2': 4}


def check_independent_assessor_rejects_missing_ui_progress(tmp_path):
    raw = {'service_mode': 'independent_process', 'status': 'pass', 'requested_mixed_seconds': 14400,
           'elapsed_mixed_seconds': 14400, 'source_sha256': 'source', 'asset_manifest_sha256': 'asset',
           'metrics': {}, 'run_counts': {k: 1 for k in ['READ', 'ASK', 'CANCEL', 'OCR', 'visual']},
           'shutdown': {'service_closed': True, 'ui_returncode': 0},
           'comparisons': [{'phase': phase, 'pages_per_second': 1} for phase in
                           ['agent_off', 'agent_idle', 'agent_read', 'agent_ocr', 'mixed_two_ui'] for _ in range(3)]}
    (tmp_path / 'mixed-stability.json').write_text(json.dumps(raw), encoding='utf-8')
    (tmp_path / 'ui-result.json').write_text(json.dumps({'passed': True, 'clients': [
        {'cycles': 1, 'streams': 1, 'errors': []}] * 2}), encoding='utf-8')
    analysis = assess(tmp_path)
    assert analysis['performance_findings']['missing_ui_progress_samples'] == [
        'mixed_two_ui:ui_progress', 'soak:ui_progress']
    assert not analysis['functional_pass']
    performance, stability = publish(tmp_path, tmp_path / 'receipts')
    assert performance['status'] == stability['status'] == 'not_qualified'
    raw['service_mode'] = 'in_process'
    (tmp_path / 'mixed-stability.json').write_text(json.dumps(raw), encoding='utf-8')
    assert assess(tmp_path)['functional_pass']  # Existing track retains its criteria.


def check_bridge_start_stop_timeouts_are_bounded_and_operation_specific():
    seen = {}
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'ok': True}
    bridge = Bridge.__new__(Bridge)
    bridge.client = SimpleNamespace(post=lambda path, json, timeout: (seen.update({json['operation']: timeout}) or Response()))
    for operation in ('start', 'stop', 'shutdown', 'state'):
        bridge.call(operation)
    assert seen == {'start': 600, 'stop': 120, 'shutdown': 60, 'state': 15}


def check_stuck_process_is_killed_and_reaped():
    class ProcessStub:
        returncode = None
        killed = False
        def poll(self):
            return self.returncode
        def terminate(self):
            pass
        def wait(self):
            if not self.killed:
                raise TimeoutError('stuck')
            return self.returncode
        def kill(self):
            self.killed = True
            self.returncode = -9
    process = ProcessStub()
    asyncio.run(Audit.reap_process(None, process, 'ui'))
    assert process.killed and process.returncode == -9


def check_failed_bridge_cleanup_reaps_child_and_writes_failure(tmp_path, state_fails):
    class ProcessStub:
        pid = 99999999
        returncode = None
        terminated = False
        def poll(self):
            return self.returncode
        def terminate(self):
            self.terminated = True
            self.returncode = -15
        def wait(self):
            return self.returncode

    class BridgeStub:
        closed = False
        def call(self, operation, **kwargs):
            if operation in {'stop', 'shutdown'}:
                raise TimeoutError(operation)
            if operation == 'state':
                if state_fails:
                    raise TimeoutError('state capture')
                return {'metrics': {'mixed_two_ui:ui_progress': {'samples': 2, 'p95_ms': 1100},
                                    'soak:ui_progress': {'samples': 3, 'p95_ms': 1200}},
                        'ui_counts': {'1': 2, '2': 3}, 'ui_clients': {}}
            return {'ok': True}
        def close(self):
            self.closed = True

    async def fail_start(enabled):
        audit.app = SimpleNamespace(state=SimpleNamespace(store=SimpleNamespace(root=tmp_path)))
        raise RuntimeError('ready/start failed after Popen')
    async def idle_sampler():
        await asyncio.Event().wait()

    audit = Audit.__new__(Audit)
    audit.receipt_lock = threading.RLock()
    audit.metrics = Metrics()
    audit.child_metrics = {}
    audit.bridge = BridgeStub()
    audit.service_process = ProcessStub()
    audit.service_stdout = io.StringIO()
    audit.app = None
    audit.ui_process = None
    audit.http_client = None
    audit.out = tmp_path
    audit.phase = 'setup'
    audit.run_counts = Counter()
    audit.ui_counts = Counter()
    audit.report = {'status': 'running', 'errors': [], 'ui_clients': {}}
    audit.resources = io.StringIO()
    audit.trace = io.StringIO()
    audit.api_spans = None
    audit.db_spans = None
    audit.start = fail_start
    audit.sample = idle_sampler
    audit.publish_formal_receipts = lambda: None
    try:
        asyncio.run(audit.run())
    except RuntimeError as error:
        assert 'ready/start failed' in str(error)
    else:
        raise AssertionError('Original startup failure must propagate')
    raw = json.loads((tmp_path / 'mixed-stability.json').read_text(encoding='utf-8'))
    assert audit.service_process.terminated and audit.bridge is None
    assert raw['status'] == 'failed' and raw['shutdown']['service_closed'] is False
    assert any('Service shutdown' in error for error in raw['errors'])
    if state_fails:
        assert any('Final service metrics' in error for error in raw['errors'])
    else:
        assert raw['metrics']['mixed_two_ui:ui_progress']['samples'] == 2
        assert raw['metrics']['soak:ui_progress']['samples'] == 3


class MixedServiceBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_query_whitelist_and_transport_are_distinct(self):
        check_query_whitelist_and_transport_are_distinct()

    def test_control_auth_state_counts_and_lifecycle(self):
        check_control_auth_state_counts_and_lifecycle()

    def test_final_receipt_keeps_child_progress_and_parent_collision(self):
        check_final_receipt_keeps_child_progress_and_parent_collision(self.tmp_path)

    def test_independent_assessor_rejects_missing_ui_progress(self):
        check_independent_assessor_rejects_missing_ui_progress(self.tmp_path)

    def test_bridge_start_stop_timeouts_are_bounded_and_operation_specific(self):
        check_bridge_start_stop_timeouts_are_bounded_and_operation_specific()

    def test_stuck_process_is_killed_and_reaped(self):
        check_stuck_process_is_killed_and_reaped()

    def test_failed_bridge_cleanup_reaps_child_and_writes_failure_when_state_available(self):
        check_failed_bridge_cleanup_reaps_child_and_writes_failure(self.tmp_path, False)

    def test_failed_bridge_cleanup_reaps_child_and_writes_failure_when_state_fails(self):
        check_failed_bridge_cleanup_reaps_child_and_writes_failure(self.tmp_path, True)
