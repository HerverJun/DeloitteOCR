"""Public failures preserve receipt uncertainty and never duplicate writes."""
import asyncio
import json
from types import SimpleNamespace

from packages.contracts.agent import AgentModel
from workbench_platform.agent.registry import ToolRegistry, ToolSpec
from workbench_platform.application_services.context import ToolContext


class Empty(AgentModel):
    pass


class MemoryStore:
    def __init__(self):
        self.receipts, self.events = {}, []
    def assert_current(self, *_):
        pass
    def begin_operation(self, _, __, call, parent_id=None):
        identifier = 'o' + call['call_id']
        if identifier in self.receipts:
            return {'id': identifier, 'state': 'finished', 'result': json.dumps(self.receipts[identifier])}, False
        return {'id': identifier, 'state': 'started'}, True
    def finish_operation(self, identifier, result):
        self.receipts[identifier] = result
    def public_event(self, *event):
        self.events.append(event)


def context():
    store = MemoryStore()
    return ToolContext('workspace', 'session', 'run', 1, 0, '', (),
        SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: {'workspace_id': 'workspace'})), store)


def test_read_permission_denied_is_actionable_without_private_exception_content():
    registry, ctx = ToolRegistry(), context()
    def denied(*_):
        raise PermissionError('secret-token F:/private/credential')
    registry.register(ToolSpec('read_test', 'test', Empty, denied))
    result = asyncio.run(registry.dispatch({'tool': 'read_test', 'call_id': 'read', 'arguments': {}}, ctx))
    assert result['error']['code'] == 'permission_denied' and result['error']['retryable'] is False
    assert result['data']['failure']['stage'] == 'access'
    assert 'secret-token' not in json.dumps(result)


def test_write_timeout_replays_unknown_receipt_without_resubmitting_handler():
    registry, ctx, submitted = ToolRegistry(), context(), []
    def submit(*_):
        submitted.append('accepted')
        raise TimeoutError('lost response after native accepted')
    registry.register(ToolSpec('write_test', 'test', Empty, submit, effect='derive'))
    call = {'tool': 'write_test', 'call_id': 'write', 'arguments': {}}
    first = asyncio.run(registry.dispatch(call, ctx))
    replay = asyncio.run(registry.dispatch(call, ctx))
    assert first == replay and submitted == ['accepted']
    assert first['status'] == 'needs_verification' and first['error']['code'] == 'response_unknown'
    assert first['data']['failure']['accepted_write'] is None
    assert first['data']['failure']['retryable'] is False
