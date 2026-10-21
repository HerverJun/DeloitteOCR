"""Dynamic graph with deterministic controller and actual native data workers.

These are engineering checks, explicitly excluded from real-controller scores.
"""
import asyncio
import ast
import inspect
from pathlib import Path
import textwrap
import unittest

import test_data_tools as data_tests
from workbench_platform.agent.runtime import AgentRuntime


def response(state, tool=None, arguments=None, content=''):
    return {'id': state['run_id'] + ':model:' + str(state['step']), 'role': 'assistant', 'content': content,
            'calls': [{'call_id': 'call_' + str(state['step']), 'tool': tool, 'arguments': arguments or {}}] if tool else [],
            'protocol': 'openai_chat_completions', 'private': {}, 'blocks': None,
            'usage': {'input_tokens': 1, 'output_tokens': 1}}


class GraphTests(unittest.IsolatedAsyncioTestCase):
    setUp = data_tests.DataToolTests.setUp
    tearDown = data_tests.DataToolTests.tearDown
    asset = data_tests.DataToolTests.asset

    async def wait_status(self, runtime, run_id, statuses):
        for _ in range(600):
            run = runtime.store.run(run_id)
            if run['status'] in statuses:
                return run
            await asyncio.sleep(0.1)
        self.fail('Agent did not reach expected status: ' + str(runtime.store.run(run_id)))

    async def make_runtime(self, provider):
        root = Path(self.temporary.name) / 'runtime-agent'
        runtime = AgentRuntime(root, self.services, model_provider=provider)
        # These business/recovery scenarios intentionally begin after discovery.
        # Preload only names explicitly present in this one test, keeping the
        # production request construction and its budget checks active. The
        # dedicated wire-context suite verifies normal core-only discovery.
        scenario = inspect.getsource(getattr(self, self._testMethodName))
        names = {node.value for node in ast.walk(ast.parse(textwrap.dedent(scenario)))
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        initial_state = runtime.initial_state
        def scenario_state(run, content):
            state = initial_state(run, content)
            state['visible'] = list(dict.fromkeys([*state['visible'],
                *(name for name in runtime.registry.tools if name in names)]))
            return state
        runtime.initial_state = scenario_state
        self.services.agent = runtime
        await runtime.start()
        session = runtime.store.create_session('wtest', 'Graph test', [], 'graph_session')
        return runtime, session

    async def test_native_wait_does_not_call_model_until_result_and_delivery_is_readable(self):
        asset = self.asset('suppliers-left.csv')
        observations = []
        async def provider(state, schemas):
            observations.append(state['step'])
            step = state['step']
            if step == 0:
                return response(state, 'data_create_session', {'name': '动态图导入'})
            previous = next(m['result'] for m in reversed(state['messages']) if m['role'] == 'tool')
            if step == 1:
                return response(state, 'data_import', {'data_session_id': previous['data']['data_session_id'],
                    'expected_version': previous['data']['version'], 'side': 'left', 'asset_id': asset.asset_id,
                    'asset_revision': asset.revision, 'blob_hash': asset.blob_hash})
            if step == 2:
                self.assertEqual(previous['status'], 'success')
                self.assertEqual(previous['data']['version'], 2)
                return response(state, 'data_read_table', {'data_session_id': previous['data']['data_session_id'], 'side': 'left'})
            if step == 3:
                self.assertEqual(previous['data']['rows'][0]['cells'][0]['value'], '000001')
                return response(state, 'delivery_publish', {'summary': '已导入并读回台账', 'result_refs': previous['result_refs']})
            return response(state, content='完成')
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '导入并查看台账', 'graph_message')
            final = await self.wait_status(runtime, run['id'], {'succeeded', 'failed', 'incomplete'})
            self.assertEqual(final['status'], 'succeeded', runtime.store.events(session['id']))
            self.assertEqual(observations, [0, 1, 2, 3])
            self.assertEqual(len(self.native.list('wtest')), 1)
            self.assertEqual(len(self.native.get(self.native.list('wtest')[0]['id'])['uploads']), 1)
        finally:
            await runtime.close()

    async def test_bound_question_survives_close_reopen_and_changes_next_tool(self):
        async def provider(state, schemas):
            if state['step'] == 0:
                return response(state, 'ask_user', {'question': '请选择供应商台账还是历史台账', 'options': ['供应商台账', '历史台账']})
            if state['step'] == 1:
                answer = next(m['result'] for m in reversed(state['messages']) if m['role'] == 'tool')['data']['reply']
                return response(state, 'data_create_session', {'name': answer})
            if state['step'] == 2:
                return response(state, content='已根据工作表选择建立数据作业，尚未导出文件')
            return response(state, content='完成')
        runtime, session = await self.make_runtime(provider)
        run = await runtime.submit(session['id'], '比较台账', 'graph_message')
        await self.wait_status(runtime, run['id'], {'waiting_user'})
        decision = runtime.store.rows('SELECT * FROM decisions WHERE run_id=?', (run['id'],))[0]
        await runtime.close()
        reopened = AgentRuntime(runtime.store.root, self.services, model_provider=provider)
        self.services.agent = reopened
        await reopened.start()
        try:
            await reopened.reply(decision['id'], '历史台账', 1, 'graph_reply')
            final = await self.wait_status(reopened, run['id'], {'paused', 'failed'})
            self.assertEqual(final['status'], 'paused', reopened.store.events(session['id']))
            self.assertEqual(self.native.list('wtest')[0]['name'], '历史台账')
        finally:
            await reopened.close()

    async def test_stop_fences_late_controller_response(self):
        pending = asyncio.Event()
        release = asyncio.Event()
        async def provider(state, schemas):
            pending.set()
            await release.wait()
            return response(state, 'data_create_session', {'name': '不能执行迟到响应'})
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '开始任务', 'graph_message')
            await pending.wait()
            runtime.cancel(run['id'])
            release.set()
            await asyncio.sleep(0.3)
            self.assertEqual(runtime.store.run(run['id'])['status'], 'cancelled')
            self.assertEqual(self.native.list('wtest'), [])
            self.assertEqual(runtime.store.rows('SELECT * FROM operations WHERE run_id=?', (run['id'],)), [])
        finally:
            await runtime.close()

    async def test_no_delivery_cannot_be_marked_success(self):
        async def provider(state, schemas):
            return response(state, content='所有任务均已完成并导出了文件')
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '导出文件', 'graph_message')
            final = await self.wait_status(runtime, run['id'], {'paused', 'failed'})
            self.assertEqual(final['status'], 'paused')
            self.assertGreater(final['rounds'], 1)
            self.assertEqual(runtime.store.rows("SELECT * FROM events WHERE run_id=? AND kind='delivery'", (run['id'],)), [])
        finally:
            await runtime.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
