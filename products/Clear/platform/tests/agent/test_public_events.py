"""Public streaming identities, transaction fences and bounded replay storage."""
import json
from pathlib import Path
import tempfile
import unittest

import test_data_tools as data_tests
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import AgentStore, RunSuperseded, digest
from packages.contracts.agent import AgentResponsePreview, AgentResponseState


class PublicEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=data_tests.RUN / 'temp')
        self.store = AgentStore(Path(self.temp.name))
        self.session = self.store.create_session('wpublic', 'Public check', [], 'public_session')
        self.run, _ = self.store.start_run(self.session['id'], 'hello', 'public_run', 1)
        self.record, _ = self.store.begin_model(self.run['id'], 1, 0, digest('payload'))
        self.state = {'run_id': self.run['id'], 'generation': 1, 'step': 0}
        self.runtime = object.__new__(AgentRuntime)
        self.runtime.store = self.store

    def tearDown(self):
        self.temp.cleanup()

    def preview(self, content, block='call:0', source='answer', **fields):
        return self.store.preview_response(self.run['id'], 1, self.record['id'],
            {'block': block, 'source': source, 'content': content, **fields})

    def events(self):
        return self.store.events(self.session['id'], limit=10000)['items']

    def cancel(self):
        with self.store.transaction() as db:
            db.execute("UPDATE runs SET status='cancelled',generation=generation+1 WHERE id=?", (self.run['id'],))

    def test_unicode_append_metadata_and_full_reconstruction(self):
        self.preview('审计😀', call_id='partial', tool='conversation_answer')
        identifier = self.preview('审计😀资料', call_id='partial', tool='conversation_answer')
        self.preview('审计😀资料', call_id='complete', tool='conversation_answer')
        frames = [event['payload'] for event in self.events() if event['kind'] == 'response_preview']
        self.assertEqual(frames[1]['offset'], 3)
        self.assertEqual(frames[-1]['call_id'], 'complete')
        rebuilt = ''
        for frame in frames:
            AgentResponsePreview.model_validate(frame)
            if frame['mode'] == 'append':
                self.assertEqual(len(rebuilt), frame['offset'])
                rebuilt += frame['content']
            else:
                rebuilt = frame['content']
        self.assertEqual(rebuilt, '审计😀资料')
        self.assertEqual(self.store.rows('SELECT display_id FROM public_responses')[0]['display_id'], identifier)

    def test_truncation_change_emits_without_duplicate_content(self):
        self.preview('已达预览上限', truncated=False)
        self.preview('已达预览上限', truncated=True)
        frames = [e['payload'] for e in self.events() if e['kind'] == 'response_preview']
        self.assertEqual(len(frames), 2)
        self.assertTrue(frames[-1]['truncated'])
        self.assertEqual(frames[-1]['content'], '')

    def test_atomic_generation_fence_rejects_late_write(self):
        self.store.assert_current(self.run['id'], 1)  # A callback read before cancel.
        self.cancel()
        with self.assertRaises(RunSuperseded):
            self.preview('迟到内容')
        with self.assertRaises(RunSuperseded):
            self.store.public_event(self.run['id'], 1, 'late_tool', 'tool_activity', {'phase': 'started'})
        self.assertEqual(self.store.rows('SELECT * FROM public_responses'), [])

    def test_generated_confirmed_idempotent_and_received_rejects_preview(self):
        identifier = self.preview('候选')
        self.store.finish_model(self.record['id'], {'content': ''})
        with self.assertRaises(RunSuperseded):
            self.preview('迟到候选')
        for status in ('generated', 'confirmed', 'confirmed', 'generated'):
            self.store.response_state(self.run['id'], 1, self.record['id'], identifier, status, content='正式答案')
        states = [event['payload'] for event in self.events() if event['kind'] == 'response_state']
        self.assertEqual([item['status'] for item in states], ['generated', 'confirmed'])
        for state in states:
            AgentResponseState.model_validate(state)
        self.assertEqual(self.store.rows('SELECT status FROM public_responses')[0]['status'], 'confirmed')

    def test_terminal_closes_every_draft_but_preserves_confirmed(self):
        confirmed = self.preview('已确认')
        draft = self.preview('未确认', 'call:1')
        text = self.preview('过程', 'text', 'text')
        self.store.response_state(self.run['id'], 1, self.record['id'], confirmed, 'confirmed')
        self.store.response_state(self.run['id'], 1, self.record['id'], draft, 'generated')
        self.cancel()
        self.store.finish_public(self.run['id'], 'cancelled')
        self.store.finish_public(self.run['id'], 'cancelled')
        values = {row['display_id']: row['status'] for row in self.store.rows('SELECT * FROM public_responses')}
        self.assertEqual(values, {confirmed: 'confirmed', draft: 'interrupted', text: 'interrupted'})
        self.assertEqual(sum(e['kind'] == 'agent_activity' for e in self.events()), 1)

    def test_ten_thousand_char_storage_is_bounded_and_replays_exactly(self):
        content = '审计😀证据' * 2000
        for end in range(64, len(content), 64):
            identifier = self.preview(content[:end])
        self.preview(content)
        self.store.response_state(self.run['id'], 1, self.record['id'], identifier, 'generated', content=content)
        frames = [e['payload'] for e in self.events() if e['kind'] == 'response_preview']
        rebuilt = ''
        for frame in frames:
            rebuilt = rebuilt + frame['content'] if frame['mode'] == 'append' else frame['content']
        self.assertEqual(rebuilt, content)
        public_bytes = sum(len(json.dumps(e['payload'], ensure_ascii=False).encode()) for e in self.events())
        self.assertLess(public_bytes, len(content.encode()) * 6)
        self.assertLess(sum(f['mode'] == 'snapshot' for f in frames), 7)
        (data_tests.RUN / 'evidence/public-storage.json').write_text(json.dumps({
            'characters': len(content), 'content_utf8_bytes': len(content.encode()),
            'preview_frames': len(frames), 'snapshots': sum(f['mode'] == 'snapshot' for f in frames),
            'public_event_bytes': public_bytes, 'reconstructed_exactly': rebuilt == content,
            'source': 'isolated store test, code point offsets'}, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_json_text_and_malformed_answer_keep_validation_boundary(self):
        self.runtime.generated_response(self.state, self.record, {'content': '整包正文', 'calls': [
            {'call_id': 'bad', 'tool': 'conversation_answer', 'arguments': {'content': {'bad': True}}}]})
        rows = self.store.rows('SELECT * FROM public_responses')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['content'], rows[0]['source'], rows[0]['status']), ('整包正文', 'text', 'generated'))

    def test_unverified_delivery_does_not_confirm_candidate(self):
        call = {'call_id': 'deliver', 'tool': 'delivery_publish', 'arguments': {}}
        identifier = self.preview('候选交付', source='delivery', call_id='deliver', tool='delivery_publish')
        result = {'status': 'success', 'summary': '伪交付', 'data': {'verified': False}}
        self.assertIsNone(self.runtime.confirm_response(self.state, call, result))
        self.assertEqual(self.store.rows('SELECT status FROM public_responses')[0]['status'], 'generating')
        result.update(summary='真实回执', data={'verified': True})
        self.assertEqual(self.runtime.confirm_response(self.state, call, result), identifier)
        self.assertEqual(self.store.rows('SELECT content FROM public_responses')[0]['content'], '真实回执')

    def test_cancel_after_completion_preserves_actual_terminal_state(self):
        self.store.status(self.run['id'], 1, 'answered')
        cancelled = self.runtime.cancel(self.run['id'])
        self.assertEqual((cancelled['status'], cancelled['generation']), ('answered', 1))
        self.assertFalse(any(e['kind'] == 'cancelled' for e in self.events()))
        self.assertFalse(any(e['kind'] == 'agent_activity' and e['payload']['phase'] == 'stopped' for e in self.events()))


class PublicRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = data_tests.DataToolTests.setUp
    tearDown = data_tests.DataToolTests.tearDown

    async def test_cold_start_repairs_terminal_display_without_replaying(self):
        for status in ('cancelled', 'answered'):
            sends = []
            async def provider(state, schemas):
                sends.append(state['step'])
                raise AssertionError('Terminal recovery must not contact controller')
            root = Path(self.temporary.name) / ('terminal-' + status)
            runtime = AgentRuntime(root, self.services, model_provider=provider)
            session = runtime.store.create_session('wtest', 'Terminal', [], 'cold_session')
            run, _ = runtime.store.start_run(session['id'], 'hello', 'cold_run', 0)
            record, _ = runtime.store.begin_model(run['id'], 1, 0, digest('payload'))
            identifier = runtime.store.preview_response(run['id'], 1, record['id'],
                {'block': 'call:0', 'source': 'answer', 'content': '答案'})
            runtime.store.public_event(run['id'], 1, 'waiting', 'agent_activity', {'phase': 'model_wait'})
            if status == 'answered':
                runtime.store.response_state(run['id'], 1, record['id'], identifier, 'confirmed')
            # Simulate process death between business status and UI terminal.
            with runtime.store.transaction() as db:
                db.execute('UPDATE runs SET status=?,generation=? WHERE id=?',
                           (status, 2 if status == 'cancelled' else 1, run['id']))
            for _ in range(2):
                reopened = AgentRuntime(root, self.services, model_provider=provider)
                self.services.agent = reopened
                await reopened.start()
                await reopened.close()
            events = reopened.store.events(session['id'], limit=10000)['items']
            terminal = [e for e in events if e['kind'] == 'agent_activity' and e['payload']['phase'] in {'stopped','completed'}]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]['payload']['phase'], 'stopped' if status == 'cancelled' else 'completed')
            self.assertEqual(reopened.store.rows('SELECT status FROM public_responses')[0]['status'],
                             'interrupted' if status == 'cancelled' else 'confirmed')
            self.assertEqual(sends, [])
            self.assertEqual(len(reopened.store.rows('SELECT * FROM model_requests')), 1)

    async def test_public_projection_changes_do_not_resend_received_request(self):
        sends = []
        async def provider(state, schemas):
            sends.append(state['step'])
            return {'id': 'reply', 'role': 'assistant', 'content': '', 'calls': [
                {'call_id': 'answer', 'tool': 'conversation_answer', 'arguments': {'content': '你好'}}],
                'protocol': 'openai_chat_completions', 'private': {}, 'blocks': None,
                'usage': {'input_tokens': 1, 'output_tokens': 1}}
        runtime = AgentRuntime(Path(self.temporary.name) / 'public-runtime', self.services, model_provider=provider)
        self.services.agent = runtime
        session = runtime.store.create_session('wtest', 'Recovery', [], 'public_recovery_session')
        run, _ = runtime.store.start_run(session['id'], 'hello', 'public_recovery_run', 0)
        state = runtime.initial_state(run, 'hello')
        first = await runtime.model(state)
        state.update(answer_display_id='display-change', delivery_display_id=None)
        recovered = await runtime.model(state)
        self.assertEqual(first, recovered)
        self.assertEqual(sends, [0])
        self.assertEqual(len(runtime.store.rows('SELECT * FROM model_requests')), 1)
