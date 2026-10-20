"""Architecture boundaries, durable final observations and terminal reads."""
import asyncio
import ast
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from packages.contracts.agent import AgentRef, AgentToolResult
from workbench_platform.application_services.context import ToolContext, snapshot_context
from workbench_platform.application_services.identity import canonical, digest, Conflict, RunSuperseded
from workbench_platform.application_services import job_lifecycle, native
from workbench_platform.agent.harness_service import HarnessService
from workbench_platform.agent.progress import progress_for, snapshot_for
from workbench_platform.agent.store import AgentStore
from workbench_platform.audit.service import AuditService
from workbench_platform.data_api.service import DataError


def test_receipt_encoding_keeps_original_bytes_and_compatibility_exports():
    value = {'中文': [0, 1.5, None, False], 'a': {'z': 'é', 'b': '\n'}}
    expected = '{"a":{"b":"\\n","z":"é"},"中文":[0,1.5,null,false]}'
    assert canonical(value) == expected
    assert digest(value) == hashlib.sha256(expected.encode('utf-8')).hexdigest()
    from workbench_platform.agent import store, registry
    assert store.canonical is canonical and store.digest is digest
    assert store.Conflict is Conflict and store.RunSuperseded is RunSuperseded
    assert registry.ToolContext is ToolContext
    with pytest.raises(ValueError):
        canonical(float('nan'))


def test_application_services_have_no_agent_implementation_imports():
    root = Path(__file__).resolve().parents[2] / 'src/workbench_platform/application_services'
    for path in root.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or '').startswith('workbench_platform.agent'), path
            elif isinstance(node, ast.Import):
                assert not any(name.name.startswith('workbench_platform.agent') for name in node.names), path
    for family in ('ocr', 'pdf', 'classification', 'data', 'forensics', 'multimodal', 'jobs',
                   'ocr_processing', 'ocr_management', 'classification_management', 'platform_management', 'audit'):
        business = importlib.import_module('workbench_platform.application_services.operations.' + family)
        adapter = importlib.import_module('workbench_platform.agent.tools.' + family)
        name = 'settle' if hasattr(business, 'settle') else 'read_reference' if family == 'jobs' else 'read_evidence'
        assert getattr(business, name) is getattr(adapter, name)
        assert not hasattr(business, 'ToolSpec')


def make_context(tmp_path):
    store = AgentStore(tmp_path / 'agent')
    session = store.create_session('wtest', 'Final observation', [], 'session')
    run, _ = store.start_run(session['id'], 'Read result', 'run', 1)
    services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()))
    ctx = ToolContext('wtest', session['id'], run['id'], 1, 0, '', (), services, store)
    call = {'call_id': 'final_call', 'tool': 'ocr_run', 'arguments': {}}
    operation, _ = store.begin_operation(run['id'], 1, call)
    ref = AgentRef(kind='job', application='ocr', workspace_id='wtest', object_id='jobFinal',
                   parent_id='handoffFinal', revision=1).model_dump(mode='json')
    pending = AgentToolResult(status='pending', summary='Queued', job_refs=[AgentRef.model_validate(ref)]).model_dump()
    store.finish_operation(operation['id'], pending)
    return ctx, call, operation['id'], ref, pending


def test_settlement_commits_receipt_before_same_identity_terminal_event_and_snapshot(tmp_path):
    ctx, call, operation_id, ref, pending = make_context(tmp_path)
    service = object.__new__(HarnessService)
    service.store = ctx.store
    service.current = lambda _: ctx.current()
    service.context = lambda _: ctx
    service.activity = lambda *_, **__: None
    service.graph = SimpleNamespace(save=lambda _: None)
    async def collect(*_):
        pass
    service.collect_available_results = collect
    async def settle(*_):
        return AgentToolResult(status='success', summary='Result ready').model_dump()
    service.settle = settle
    observed = job_lifecycle.Lifecycle(lambda *_: {'status': 'succeeded', 'counts': {'succeeded': 1}},
                                      None, None, lambda _: {})
    original_event = ctx.store.public_event
    def event(*args):
        if args[3] == 'job_progress':
            receipt = json.loads(ctx.store.rows('SELECT result FROM operations WHERE id=?', (operation_id,))[0]['result'])
            assert receipt['status'] == 'success'
            assert receipt['job_refs'] == [ref]
        return original_event(*args)
    state = {'run_id': ctx.run_id, 'generation': 1}
    with patch.object(job_lifecycle, 'adapter', return_value=observed), patch.object(ctx.store, 'public_event', side_effect=event):
        result = asyncio.run(service.harness_settle(state, call, pending, operation_id))
        assert result['job_refs'] == [ref]
        events = [json.loads(row['payload']) for row in ctx.store.rows("SELECT payload FROM events WHERE kind='job_progress'")]
        assert len(events) == 1 and events[0]['status'] == 'succeeded'
        assert events[0]['job_ref'] == ref and events[0]['generation'] == 1
        ctx.store.status(ctx.run_id, 1, 'succeeded')
        assert snapshot_for(ref, ctx)['status'] == 'succeeded'
        unavailable = job_lifecycle.Lifecycle(Mock(side_effect=OSError('offline')), None, None, lambda _: {})
        with patch.object(job_lifecycle, 'adapter', return_value=unavailable):
            assert snapshot_for(ref, ctx)['observation'] == 'unavailable'
            assert snapshot_for(ref, ctx)['status'] == 'unknown'


def test_snapshot_context_cannot_prepare_or_cancel_and_checks_generation(tmp_path):
    ctx, _, _, ref, _ = make_context(tmp_path)
    readonly = snapshot_context(ctx)
    assert readonly.grants == frozenset({'read'}) and readonly.operation_id == ''
    ctx.services.launch_service = Mock()
    with pytest.raises(ValueError, match='只读'):
        native.prepare(readonly, 'ocr')
    with pytest.raises(ValueError, match='只读'):
        job_lifecycle.adapter(ref).cancel(ref, readonly)
    ctx.services.launch_service.start.assert_not_called()
    with ctx.store.transaction() as db:
        db.execute('UPDATE runs SET generation=2 WHERE id=?', (ctx.run_id,))
    with pytest.raises(RunSuperseded):
        readonly.current()


def test_workspace_query_uses_workspace_authority_without_batch_service():
    service = object.__new__(AuditService)
    service.app = SimpleNamespace(state=SimpleNamespace(workspace_store=SimpleNamespace(
        get_workspace=lambda identifier: object() if identifier == 'present' else None)))
    service.batches = Mock(side_effect=AssertionError('BatchService is irrelevant to workspace lookup'))
    service.workspace('present')
    service.batches.workspace.assert_not_called()
    with pytest.raises(DataError) as error:
        service.workspace('missing')
    assert error.value.status == 404
