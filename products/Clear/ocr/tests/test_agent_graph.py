import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from ocr_workbench.agent.checkpoints import Checkpoints
from ocr_workbench.agent.graph import GraphServices, build_graph
from ocr_workbench.agent.state import initial_state
from ocr_workbench.agent.store import AgentStore, digest
from ocr_workbench.agent.policy import AgentPolicy
from ocr_workbench.agent.registry import ToolRegistry
from ocr_workbench.agent.tools import register_read_tools
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.store import Store
from langgraph.types import Command


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.agent = AgentStore(self.store)
        self.project = self.store.project("合成图测试")["id"]
        self.session = self.agent.create_session(self.project, {"client_request_id": "s", "title": "test"})
        self.run = self.agent.create_run(self.project, self.session["id"], {"client_request_id": "r", "content": "查询并澄清"}, context={}, config={}, limits={})
        self.registry = ToolRegistry(AgentPolicy(self.agent))
        register_read_tools(self.registry, ApplicationServices(self.store, None, None), lambda: {"read": True})
        self.cp = await Checkpoints(self.store).open()
        self.config = {"configurable": {"thread_id": self.session["graph_thread_id"]}, "recursion_limit": 100}

    async def asyncTearDown(self):
        await self.cp.close()

    def model(self, script):
        async def invoke(state, run):
            message = script[state["step"]]
            record, fresh = self.agent.begin_model_request(self.project, run["id"], state["generation"], state["step"], digest(message))
            if not fresh:
                return json.loads(record["normalized_response"])
            reply = normalize_response("openai_chat_completions", {"choices": [{"finish_reason": "tool_calls" if message.get("tool_calls") else "stop", "message": message}]}, record["request_id"])
            self.agent.finish_model_request(self.project, run["id"], state["generation"], record["request_id"], reply)
            return reply
        return invoke

    def call(self, name, arguments=None, call_id="call-1"):
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments or {})}}

    async def test_invalid_argument_corrections_are_bounded_and_never_dispatch(self):
        import httpx
        from ocr_workbench.agent.providers import logged_request
        from test_agent_provider import ProviderTests, reply
        requests = []
        def invalid(request):
            requests.append(json.loads(request.content))
            raw = reply('openai_chat_completions')
            raw['choices'][0]['message']['tool_calls'].append(self.call('read_page_result', {'page_id': True}, 'invalid'))
            return httpx.Response(200, json=raw)
        async def model(state, run):
            return await logged_request(self.agent, run, state['generation'], state['step'], ProviderTests().config(), 'synthetic', state['messages'], transport=httpx.MockTransport(invalid))
        graph = build_graph(GraphServices(self.agent, self.registry, model), self.cp.saver)
        result = await graph.ainvoke(initial_state(self.run, '查询'), self.config)
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(len(requests), 3, 'One original request and at most two corrections')
        self.assertFalse(self.store.rows('SELECT * FROM agent_calls'), 'The valid first call in each rejected batch must not execute')
        self.assertTrue(all(r['state'] == 'rejected' for r in self.store.rows('SELECT state FROM agent_model_requests')))
        self.assertIn('未通过整批校验', json.dumps(requests[-1], ensure_ascii=False))

    async def test_same_batch_error_limit_skips_third_call_and_keeps_pairing(self):
        from ocr_workbench.agent.graph import tool_error
        calls = [self.call('get_workspace_context', call_id=str(n)) for n in range(3)]
        responses = [tool_error('not_found', 'missing'), tool_error('not_found', 'missing'),
                     {'status': 'success', 'summary': 'recovered'}]
        executed = []
        async def handler(args, context):
            executed.append(context['call_id'])
            return responses[int(context['call_id'])]
        self.registry.handlers['get_workspace_context'] = handler
        script = [{'role': 'assistant', 'tool_calls': calls}, {'role': 'assistant', 'content': '已查询'}]
        graph = build_graph(GraphServices(self.agent, self.registry, self.model(script)), self.cp.saver)
        result = await graph.ainvoke(initial_state(self.run, '查询'), self.config)
        self.assertIn('连续 2 次', result['final_text'])
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(executed, ['0', '1'])
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests')), 1)
        replies = [m for m in result['messages'] if m['role'] == 'tool']
        self.assertEqual([m['call_id'] for m in replies], ['0', '1', '2'])
        self.assertEqual(replies[-1]['result']['error']['code'], 'cancelled')

    async def test_dynamic_tools_ask_interrupt_resume_and_ordered_pairing(self):
        script = [
            {"role": "assistant", "tool_calls": [self.call("get_workspace_context"), self.call("get_workspace_context", call_id="call-2")]},
            {"role": "assistant", "tool_calls": [self.call("ask_user", {"question": "需要哪一类？", "options": ["表格", "正文"]})]},
            {"role": "assistant", "content": "当前没有文档，请先从导入入口添加。"},
        ]
        graph = build_graph(GraphServices(self.agent, self.registry, self.model(script)), self.cp.saver)
        result = await graph.ainvoke(initial_state(self.run, "查询并澄清"), self.config, durability="sync")
        self.assertTrue(result["__interrupt__"])
        run = self.agent.run(self.project, self.run["id"])
        self.assertEqual(run["status"], "waiting_user")
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_model_requests")), 2)
        decision = self.store.rows("SELECT * FROM agent_decisions")[0]
        self.agent.reply_decision(self.project, run["id"], decision["id"], {"client_request_id": "answer", "payload_hash": decision["payload_hash"], "option_id": "option-0"})
        snapshot = await graph.aget_state(self.config)
        await graph.ainvoke(Command(resume={snapshot.interrupts[0].id: {"notification": True}}), self.config, durability="sync")
        final = await graph.aget_state(self.config)
        self.assertEqual(final.next, ())
        self.assertEqual(self.agent.run(self.project, run["id"])["status"], "completed")
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_calls")), 3)
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_model_requests")), 3)
        messages = final.values["messages"]
        self.assertEqual([m["call_id"] for m in messages if m["role"] == "tool"], ["call-1", "call-2", "call-1"])
        self.assertEqual(len({m["id"] for m in messages}), len(messages))

    async def test_cross_project_second_call_rejects_entire_batch(self):
        script = [{"role": "assistant", "tool_calls": [self.call("get_workspace_context"), self.call("read_page_result", {"page_id": "forged"}, "bad")]},
                  {"role": "assistant", "content": "无法读取指定页面。"}]
        graph = build_graph(GraphServices(self.agent, self.registry, self.model(script)), self.cp.saver)
        result = await graph.ainvoke(initial_state(self.run, "查询"), self.config)
        self.assertEqual(result["outcome"], "partial")
        self.assertFalse([e for e in self.agent.events(self.project, self.session["id"]) if e["type"] == "tool_started"])
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_calls WHERE state='finished'")), 2)

    async def test_unknown_model_post_waits_for_explicit_decision(self):
        from ocr_workbench.agent.providers import ProviderFault
        count = []
        async def unknown(state, run):
            count.append(True)
            raise ProviderFault("response_unknown", "回包未知")
        graph = build_graph(GraphServices(self.agent, self.registry, unknown), self.cp.saver)
        await graph.ainvoke(initial_state(self.run, "查询"), self.config)
        self.assertEqual(len(count), 1)
        self.assertEqual(self.agent.run(self.project, self.run["id"])["status"], "waiting_user")
        snapshot = await graph.aget_state(self.config)
        self.assertEqual(snapshot.next, ("await_user",))
        self.assertEqual(snapshot.values["wait_reason"], "response_unknown")

    async def test_budget_increase_resumes_without_duplicate_model_requests(self):
        from ocr_workbench.agent.budgets import increase
        from ocr_workbench.store import Conflict
        with self.store.transaction() as db:
            db.execute("UPDATE agent_runs SET limits=? WHERE id=?", (json.dumps({"model_requests_per_run": 1}), self.run['id']))
        script = [{"role": "assistant", "tool_calls": [self.call("get_workspace_context")]}, {"role": "assistant", "content": "完成"}]
        graph = build_graph(GraphServices(self.agent, self.registry, self.model(script)), self.cp.saver)
        await graph.ainvoke(initial_state(self.run, "查询"), self.config)
        snapshot = await graph.aget_state(self.config)
        self.assertEqual(snapshot.values['wait_reason'], 'budget')
        decision = self.store.rows("SELECT * FROM agent_decisions")[0]
        answer = {"client_request_id": "reply", "payload_hash": decision['payload_hash'], "option_id": "continue"}
        with self.assertRaises(Conflict):
            self.agent.reply_decision(self.project, self.run['id'], decision['id'], answer)
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_model_requests")), 1)
        increase(self.agent, self.project, self.run['id'], {"client_request_id": "increase", "generation": 1, "limits": {"model_requests_per_run": 2}})
        self.agent.reply_decision(self.project, self.run['id'], decision['id'], answer)
        await graph.ainvoke(Command(resume={snapshot.interrupts[0].id: {"notification": True}}), self.config, durability='sync')
        self.assertEqual(self.agent.run(self.project, self.run['id'])['status'], 'completed')
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_model_requests")), 2)

    async def test_repeated_tool_error_stops_before_third_model_round(self):
        script = [{'role': 'assistant', 'tool_calls': [self.call('read_page_result', {'page_id': 'missing'})]}] * 2
        graph = build_graph(GraphServices(self.agent, self.registry, self.model(script)), self.cp.saver)
        result = await graph.ainvoke(initial_state(self.run, '查询'), self.config)
        self.assertEqual(result['outcome'], 'partial')
        self.assertIn('连续 2 次', result['final_text'])
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests')), 2)
