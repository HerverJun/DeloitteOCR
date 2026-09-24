"""Bounded official saver history across completed runs in one session."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_agent_policy
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.agent.graph import GraphServices, build_graph
from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.agent.store import digest
from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.documents import Documents
from ocr_workbench.store import Conflict


class RolloverTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    async def asyncSetUp(self):
        self.setUp()
        with self.store.transaction() as db:
            db.execute("UPDATE agent_runs SET status='cancelled' WHERE id=?", (self.run['id'],))
        self.seen = []

        async def model(state, run):
            self.seen.append([dict(message) for message in state['messages']])
            message = {'role': 'assistant', 'content': '答复 ' + run['goal']}
            record, fresh = self.agent.begin_model_request(self.project, run['id'], state['generation'], state['step'], digest(message))
            if fresh:
                reply = normalize_response('openai_chat_completions', {'choices': [
                    {'message': message, 'finish_reason': 'stop'}]}, record['request_id'])
                self.agent.finish_model_request(self.project, run['id'], state['generation'], record['request_id'], reply)
            return json.loads(record['normalized_response']) if not fresh else reply

        bundle = Path(self.temp.name) / 'bundle'
        bundle.mkdir()
        services = ApplicationServices(self.store, Documents(self.store, bundle, None),
                                       SimpleNamespace(guard=self.store.file_lock))
        self.runtime = await AgentRuntime(services, policy={'limits': {}}, model_override=model).open()
        self.addAsyncCleanup(self.runtime.close)

    async def run_once(self, index):
        run = self.agent.create_run(self.project, self.session['id'],
                                    {'client_request_id': f'round-{index}', 'content': f'要求 {index}'},
                                    context={}, config={}, limits={})
        self.runtime.schedule(run)
        await self.runtime.tasks[run['id']]
        self.assertEqual(self.agent.run(self.project, run['id'])['status'], 'completed')
        return run

    async def test_rolling_growth_preserves_business_record_and_exact_user_requests(self):
        with patch('ocr_workbench.agent.runtime.THREAD_CHECKPOINT_LIMIT', 6):
            old = self.session['graph_thread_id']
            counts = []
            samples = []
            rounds = 80 if os.environ.get('OCR_AGENT_GROWTH_EVIDENCE') else 25
            for index in range(rounds):
                await self.run_once(index)
                current = self.agent.session(self.project, self.session['id'])['graph_thread_id']
                count, payload = await self.runtime.checkpoints.thread_size(current)
                counts.append(count)
                if index % 10 == 0 or index == rounds - 1:
                    wal = Path(str(self.runtime.checkpoints.path) + '-wal')
                    samples.append({'round': index + 1, 'live_thread_rows': count,
                                    'live_thread_payload_bytes': payload,
                                    'main_file_bytes': self.runtime.checkpoints.path.stat().st_size,
                                    'wal_bytes': wal.stat().st_size if wal.exists() else 0})
            self.assertNotEqual(old, current)
            self.assertLessEqual(max(counts), 6)
            self.assertEqual(await self.runtime.checkpoints.thread_size(old), (0, 0))
            self.assertFalse(self.store.rows('SELECT * FROM agent_checkpoint_cleanup'))
            self.assertEqual(len(self.store.rows('SELECT id FROM agent_runs WHERE session_id=?', (self.session['id'],))), rounds + 1)
            if rounds <= 32:
                self.assertIn('要求 0', [message['content'] for message in self.seen[-1]])
            else:
                self.assertNotIn('要求 0', [message['content'] for message in self.seen[-1]])
                self.assertIn('不在当前模型上下文', self.seen[-1][0]['content'])
            self.assertIn(f'要求 {rounds - 1}', [message['content'] for message in self.seen[-1]])
            self.assertIn(f'答复 要求 {rounds - 2}', [message['content'] for message in self.seen[-1]])
            if os.environ.get('OCR_AGENT_GROWTH_EVIDENCE'):
                print('CHECKPOINT_GROWTH_EVIDENCE=' + json.dumps(samples, ensure_ascii=False), flush=True)

    async def test_pending_effect_keeps_recoverable_thread(self):
        with patch('ocr_workbench.agent.runtime.THREAD_CHECKPOINT_LIMIT', 1):
            old = self.session['graph_thread_id']
            with self.store.transaction() as db:
                db.execute("INSERT INTO agent_operations(id,project_id,run_id,operation_key,input_hash,generation,state,created,updated) "
                           "VALUES('unfinished',?,?, 'operation','hash',1,'submitted','now','now')",
                           (self.project, self.run['id']))
            await self.run_once(0)
            self.assertEqual(self.agent.session(self.project, self.session['id'])['graph_thread_id'], old)
            self.assertGreater((await self.runtime.checkpoints.thread_size(old))[0], 0)
            self.assertFalse(self.store.rows('SELECT * FROM agent_checkpoint_cleanup'))
            count = (await self.runtime.checkpoints.thread_size(old))[0]
            next_run = self.agent.create_run(self.project, self.session['id'],
                {'client_request_id': 'unsafe-next', 'content': '等待旧任务核对'}, context={}, config={}, limits={})
            self.runtime.schedule(next_run)
            await self.runtime.tasks[next_run['id']]
            self.assertEqual(self.agent.run(self.project, next_run['id'])['status'], 'failed')
            self.assertEqual((await self.runtime.checkpoints.thread_size(old))[0], count)
            self.assertEqual(len(self.seen), 1, 'no new model request after deferred retirement')

    async def test_finished_zero_job_effect_does_not_block_retirement(self):
        with patch('ocr_workbench.agent.runtime.THREAD_CHECKPOINT_LIMIT', 1):
            old = self.session['graph_thread_id']
            with self.store.transaction() as db:
                db.execute("INSERT INTO agent_operations(id,project_id,run_id,operation_key,input_hash,generation,state,result,created,updated) "
                           "VALUES('complete-effect',?,?, 'operation','hash',1,'finished','{}','now','now')",
                           (self.project, self.run['id']))
            await self.run_once(0)
            self.assertNotEqual(self.agent.session(self.project, self.session['id'])['graph_thread_id'], old)
            self.assertEqual(await self.runtime.checkpoints.thread_size(old), (0, 0))
            self.assertEqual(self.store.rows("SELECT state FROM agent_operations WHERE id='complete-effect'")[0]['state'], 'finished')

    async def test_cancelled_question_settles_atomically_and_allows_successor_rollover(self):
        async def model(state, run):
            message = {'role': 'assistant', 'content': '已读取'}
            if run['goal'] == 'CANCEL-question':
                message = {'role': 'assistant', 'tool_calls': [{'id': 'ask', 'type': 'function', 'function': {
                    'name': 'ask_user', 'arguments': json.dumps({'question': '继续吗？', 'options': ['继续', '停止']})}}]}
            return normalize_response('openai_chat_completions', {'choices': [{
                'message': message, 'finish_reason': 'tool_calls' if 'tool_calls' in message else 'stop'}]},
                f"synthetic-{run['id']}-{state['step']}")

        self.runtime.graph = build_graph(GraphServices(self.agent, self.runtime.registry, model,
            jobs=self.runtime.jobs, inbox=self.runtime.inbox), self.runtime.checkpoints.saver)
        with patch('ocr_workbench.agent.runtime.THREAD_CHECKPOINT_LIMIT', 1):
            old_thread = self.session['graph_thread_id']
            question = self.agent.create_run(self.project, self.session['id'],
                {'client_request_id': 'cancel-question', 'content': 'CANCEL-question'}, context={}, config={}, limits={})
            self.runtime.schedule(question)
            for _ in range(200):
                if self.agent.run(self.project, question['id'])['status'] == 'waiting_user':
                    break
                await asyncio.sleep(.01)
            self.assertEqual(self.agent.run(self.project, question['id'])['status'], 'waiting_user')
            decision = self.store.rows('SELECT * FROM agent_decisions WHERE run_id=?', (question['id'],))[0]
            self.assertEqual(self.store.rows('SELECT state FROM agent_calls WHERE run_id=?', (question['id'],))[0]['state'], 'validated')
            answers = await asyncio.gather(*(self.runtime.cancel(self.project, question['id'], 1,
                'stop_agent', request_id='same-cancel') for _ in range(4)))
            self.assertEqual(answers, [answers[0]] * 4)
            self.assertEqual(answers[0]['status'], 'cancelled')
            self.assertEqual(self.store.rows('SELECT status FROM agent_decisions WHERE id=?', (decision['id'],))[0]['status'], 'obsolete')
            call = self.store.rows('SELECT state,result_ref FROM agent_calls WHERE run_id=?', (question['id'],))[0]
            self.assertEqual(call['state'], 'finished')
            self.assertEqual(json.loads(call['result_ref'])['error']['code'], 'cancelled')
            self.assertEqual(json.loads(call['result_ref'])['error']['required_action'], 'none')
            finished = self.store.rows("SELECT * FROM agent_events WHERE run_id=? AND type='tool_finished'", (question['id'],))
            self.assertEqual(len(finished), 1)
            with self.assertRaises(Conflict):
                await self.runtime.cancel(self.project, question['id'], 2, 'stop_agent', request_id='new-cancel')
            self.assertEqual(self.agent.run(self.project, question['id'])['generation'], 2)
            self.assertEqual(len(self.store.rows("SELECT * FROM agent_events WHERE run_id=? AND type='tool_finished'", (question['id'],))), 1)
            with self.assertRaises(Conflict):
                self.agent.reply_decision(self.project, question['id'], decision['id'], {
                    'client_request_id': 'stale-reply', 'payload_hash': decision['payload_hash'], 'option_id': 'option-0'})
            successor = self.agent.create_run(self.project, self.session['id'],
                {'client_request_id': 'after-cancel', 'content': 'READ-after-cancel'}, context={}, config={}, limits={})
            self.runtime.schedule(successor)
            await self.runtime.tasks[successor['id']]
            self.assertEqual(self.agent.run(self.project, successor['id'])['status'], 'completed')
            self.assertNotEqual(self.agent.session(self.project, self.session['id'])['graph_thread_id'], old_thread)
            self.assertEqual(await self.runtime.checkpoints.thread_size(old_thread), (0, 0))

    async def test_queued_successor_can_rotate_at_pre_invoke_boundary(self):
        with patch('ocr_workbench.agent.runtime.THREAD_CHECKPOINT_LIMIT', 1):
            old = self.session['graph_thread_id']
            with patch.object(self.runtime, '_rollover_completed_thread', return_value=None):
                await self.run_once(0)
            self.assertEqual(self.agent.session(self.project, self.session['id'])['graph_thread_id'], old)
            await self.run_once(1)
            self.assertNotEqual(self.agent.session(self.project, self.session['id'])['graph_thread_id'], old)
            self.assertEqual(await self.runtime.checkpoints.thread_size(old), (0, 0))
            self.assertIn('要求 0', [message['content'] for message in self.seen[-1]])

    async def test_long_user_history_is_windowed_and_business_originals_remain(self):
        with patch('ocr_workbench.agent.runtime.THREAD_CHECKPOINT_LIMIT', 1):
            long_request = '原始限制' * 3000
            first = self.agent.create_run(self.project, self.session['id'],
                {'client_request_id': 'long-user', 'content': long_request}, context={}, config={}, limits={})
            self.runtime.schedule(first)
            await self.runtime.tasks[first['id']]
            self.assertEqual(self.agent.run(self.project, first['id'])['status'], 'completed')
            second = self.agent.create_run(self.project, self.session['id'],
                {'client_request_id': 'long-user-2', 'content': long_request}, context={}, config={}, limits={})
            self.runtime.schedule(second)
            await self.runtime.tasks[second['id']]
            self.assertEqual(self.agent.run(self.project, second['id'])['status'], 'completed')
            current = self.agent.session(self.project, self.session['id'])['graph_thread_id']
            successor = self.agent.create_run(self.project, self.session['id'],
                {'client_request_id': 'after-long', 'content': '继续'}, context={}, config={}, limits={})
            self.runtime.schedule(successor)
            await self.runtime.tasks[successor['id']]
            self.assertEqual(self.agent.run(self.project, successor['id'])['status'], 'completed')
            self.assertEqual(len(self.seen), 3)
            self.assertLess(len(json.dumps(self.seen[-1], ensure_ascii=False).encode('utf-8')), 64 * 1024)
            self.assertIn('不在当前模型上下文', self.seen[-1][0]['content'])
            self.assertEqual(json.loads(self.store.rows('SELECT payload FROM agent_events WHERE event_key=?',
                (first['id'] + ':goal',))[0]['payload'])['content'], long_request)

    async def test_three_thousand_historical_requests_do_not_expand_new_model_input(self):
        with self.store.transaction() as db:
            for index in range(3000):
                self.agent._event(db, self.session['id'], self.run['id'], 1,
                    f'synthetic-history:{index}', 'message', {'role': 'user', 'content': f'历史要求 {index}'})
        with patch('ocr_workbench.agent.runtime.THREAD_CHECKPOINT_LIMIT', 1):
            await self.run_once(0)
            await self.run_once(1)
            recent = self.seen[-1]
            before_bytes = len(json.dumps(recent, ensure_ascii=False).encode('utf-8'))
            self.assertIn('历史要求 2999', [message['content'] for message in recent])
            self.assertNotIn('历史要求 0', [message['content'] for message in recent])
            self.assertLessEqual(len([message for message in recent if message['role'] == 'user']), 35)
            self.assertLess(before_bytes, 64 * 1024)
            with self.store.transaction() as db:
                for index in range(3000, 6000):
                    self.agent._event(db, self.session['id'], self.run['id'], 1,
                        f'synthetic-history:{index}', 'message', {'role': 'user', 'content': f'历史要求 {index}'})
            await self.run_once(2)
            after_bytes = len(json.dumps(self.seen[-1], ensure_ascii=False).encode('utf-8'))
            self.assertIn('历史要求 5999', [message['content'] for message in self.seen[-1]])
            self.assertLess(after_bytes, 64 * 1024)
            self.assertLess(abs(after_bytes - before_bytes), 2000)
            self.assertEqual(len(self.store.rows("SELECT seq FROM agent_events WHERE event_key LIKE 'synthetic-history:%'")), 6000)
            if os.environ.get('OCR_AGENT_GROWTH_EVIDENCE'):
                print('CONTINUATION_INPUT_BYTES=' + json.dumps({'history_3000': before_bytes,
                    'history_6000': after_bytes}), flush=True)

    async def test_delete_failure_leaves_durable_tombstone_for_startup_retry(self):
        with patch('ocr_workbench.agent.runtime.THREAD_CHECKPOINT_LIMIT', 1):
            old = self.session['graph_thread_id']
            with patch.object(self.runtime.checkpoints.saver, 'adelete_thread', side_effect=RuntimeError('synthetic delete failure')):
                await self.run_once(0)
            self.assertNotEqual(self.agent.session(self.project, self.session['id'])['graph_thread_id'], old)
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_checkpoint_cleanup')), 1)
            self.assertGreater((await self.runtime.checkpoints.thread_size(old))[0], 0)
            await self.runtime.close()
            self.runtime = await AgentRuntime(self.runtime.services, policy={'limits': {}},
                model_override=self.runtime.model_override).open()
            self.addAsyncCleanup(self.runtime.close)
            self.assertFalse(self.store.rows('SELECT * FROM agent_checkpoint_cleanup'))
            self.assertEqual(await self.runtime.checkpoints.thread_size(old), (0, 0))
            await self.run_once(1)
            self.assertIn('要求 0', [message['content'] for message in self.seen[-1]])
