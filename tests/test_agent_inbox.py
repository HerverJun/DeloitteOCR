import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import unittest

import test_agent_graph
import test_agent_policy
from ocr_workbench.agent.inbox import Inbox
from ocr_workbench.agent.selection import create_selection, grant_selection
from ocr_workbench.agent.state import initial_state
from ocr_workbench.agent.graph import GraphServices, build_graph
from ocr_workbench.store import Conflict
from langgraph.types import Command


class InboxTests(unittest.TestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    def test_concurrent_duplicate_and_replayed_boundary_preserve_order(self):
        inbox = Inbox(self.agent, self.policy)
        request = {'client_request_id': 'append', 'content': '改为查询表格'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            accepted = list(pool.map(lambda _: inbox.enqueue(self.project, self.session['id'], request), range(8)))
        self.assertEqual(len({r['id'] for r in accepted}), 1)
        with self.assertRaises(Conflict):
            inbox.enqueue(self.project, self.session['id'], {**request, 'content': 'different'})
        inbox.enqueue(self.project, self.session['id'], {'client_request_id': 'next', 'content': '并列出页码'})
        state = initial_state(self.run, self.run['goal'])
        first = inbox.receive(state)
        self.assertEqual([m['content'] for m in first], ['改为查询表格', '并列出页码'])
        self.assertEqual(inbox.receive(state), first, 'Replay after business commit keeps stable message IDs')
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_inbox WHERE state='applied'")), 2)
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_events WHERE event_key LIKE 'inbox:%:applied'")), 2)

    def test_existing_model_request_defers_new_message(self):
        inbox = Inbox(self.agent, self.policy)
        self.agent.begin_model_request(self.project, self.run['id'], 1, 0, 'already-sent')
        inbox.enqueue(self.project, self.session['id'], {'client_request_id': 'append', 'content': '新要求'})
        state = initial_state(self.run, self.run['goal'])
        self.assertEqual(inbox.receive(state), [])
        self.assertTrue(inbox.pending(self.run['id']))
        self.assertEqual(len(inbox.receive({**state, 'step': 1})), 1)

    def test_scope_narrowing_cannot_reuse_old_processing_grant(self):
        first = create_selection(self.agent, self.project, {'image_ids': [self.photo['id']], 'engines': ['ppocr'], 'allow_processing': True}, available_engines=['ppocr'])
        with self.store.transaction() as db:
            db.execute('UPDATE agent_runs SET context=? WHERE id=?', (json.dumps({'selection': first['selection']}), self.run['id']))
        self.run = self.agent.run(self.project, self.run['id'])
        grant_selection(self.policy, self.run, first['selection'])
        args = {'document_id': self.photo['id'], 'page_numbers': [1], 'mode': 'native'}
        self.policy.authorize(self.run, 1, 'process_pages', args)
        narrowed = create_selection(self.agent, self.project, {'image_ids': [], 'allow_processing': False}, available_engines=[])
        inbox = Inbox(self.agent, self.policy)
        inbox.enqueue(self.project, self.session['id'], {'client_request_id': 'narrow', 'content': '只查询，不处理页面', 'selection_token': narrowed['selection_token']}, narrowed['selection'])
        self.policy.authorize(self.run, 1, 'process_pages', args)  # Still the old scope before the boundary.
        inbox.receive(initial_state(self.run, self.run['goal']))
        with self.assertRaises(ValueError):
            self.policy.authorize(self.run, 1, 'process_pages', args)


class InboxGraphTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_agent_graph.GraphTests.asyncSetUp
    asyncTearDown = test_agent_graph.GraphTests.asyncTearDown
    model = test_agent_graph.GraphTests.model
    call = test_agent_graph.GraphTests.call

    async def test_remaining_calls_cancel_then_new_message_enters_paired_history(self):
        inbox = Inbox(self.agent, self.registry.policy)
        original = self.registry.handlers['get_workspace_context']
        executed = []
        async def first(args, context):
            executed.append(context['call_id'])
            inbox.enqueue(self.project, self.session['id'], {'client_request_id': 'append', 'content': '改为说明没有文档'})
            return await original(args, context)
        self.registry.handlers['get_workspace_context'] = first
        script = [{'role': 'assistant', 'tool_calls': [self.call('get_workspace_context', call_id='a'), self.call('get_workspace_context', call_id='b')]},
                  {'role': 'assistant', 'content': '当前没有文档。'}]
        graph = build_graph(GraphServices(self.agent, self.registry, self.model(script), inbox=inbox), self.cp.saver)
        result = await graph.ainvoke(initial_state(self.run, '查询'), self.config)
        self.assertEqual(executed, ['a'])
        self.assertEqual(result['outcome'], 'answered')
        paired = [m for m in result['messages'] if m['role'] == 'tool']
        self.assertEqual([m['call_id'] for m in paired], ['a', 'b'])
        self.assertTrue(paired[1]['result']['data']['superseded_by_inbox'])
        self.assertEqual(len([m for m in result['messages'] if m['role'] == 'user' and m['content'] == '改为说明没有文档']), 1)

    async def test_append_supersedes_clarification_without_authorization_reply(self):
        inbox = Inbox(self.agent, self.registry.policy)
        script = [{'role': 'assistant', 'tool_calls': [self.call('ask_user', {'question': '哪一类？', 'options': ['文字', '表格']})]},
                  {'role': 'assistant', 'content': '已按新要求查询。'}]
        graph = build_graph(GraphServices(self.agent, self.registry, self.model(script), inbox=inbox), self.cp.saver)
        await graph.ainvoke(initial_state(self.run, '查询'), self.config)
        snapshot = await graph.aget_state(self.config)
        decision = self.store.rows('SELECT * FROM agent_decisions')[0]
        inbox.enqueue(self.project, self.session['id'], {'client_request_id': 'append', 'content': '不要分类，只说明现状'})
        result = await graph.ainvoke(Command(resume={snapshot.interrupts[0].id: {'notification': True}}), self.config)
        self.assertEqual(result['outcome'], 'answered')
        self.assertEqual(self.store.rows('SELECT status FROM agent_decisions')[0]['status'], 'obsolete')
        self.assertFalse(self.store.rows('SELECT * FROM agent_grants'))
        with self.assertRaises(Conflict):
            self.agent.reply_decision(self.project, self.run['id'], decision['id'], {'client_request_id': 'stale', 'payload_hash': decision['payload_hash'], 'option_id': 'option-0'})
