"""E40: a published reference survives bounded history and a reopened session."""
from pathlib import Path
import time
import unittest

import httpx

from ocr_workbench.agent.context import compact_messages, summary_message, summary_sources
from ocr_workbench.agent.store import canonical
from ocr_workbench.service import create_app
import test_agent_artifacts


class LongSessionArtifactTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_agent_artifacts.ArtifactTests.setUp
    prepare = test_agent_artifacts.ArtifactTests.prepare

    async def test_old_reference_download_after_compaction_summary_and_session_reopen(self):
        artifacts, args, context = self.prepare()
        published = artifacts.export(args, context)
        ref = published['artifact_refs'][0]
        key = ref['artifact_id']
        target = artifacts.verify(self.project, key)
        original = target.read_bytes()
        self.agent.finish_call(self.project, self.run['id'], 1, 0,
                               {'call_id': 'export', 'tool': 'export_results'}, published)

        # Exercise the same deterministic history transforms used by the runtime.
        # The large old result is reduced, then its complete old call/result group
        # is summarized while the newest round stays intact.
        old_result = {**published, 'data': {**published['data'], 'content': '旧读取结果。' * 1200}}
        history = [
            {'id': 'old-user', 'role': 'user', 'content': '导出后保留引用'},
            {'id': 'old-call', 'role': 'assistant', 'calls': [{'call_id': 'export'}]},
            {'id': 'old-result', 'role': 'tool', 'call_id': 'export', 'result': old_result},
            {'id': 'new-call', 'role': 'assistant', 'calls': [{'call_id': 'read'}]},
            {'id': 'new-result', 'role': 'tool', 'call_id': 'read', 'result':
                {'status': 'success', 'summary': 'recent', 'data': {}}},
        ]
        compacted = compact_messages(history, 2048, enforce_limit=False)
        self.assertTrue(compacted[2]['result']['data']['context_compacted'])
        self.assertEqual(compacted[2]['result']['artifact_refs'], [ref])
        sources = summary_sources(compacted)
        summary, receipt = summary_message(self.run, sources, {'id': 'summary-1', 'content': '旧导出已经发布。'})
        self.assertEqual(receipt['source_message_ids'], ['old-call', 'old-result'])
        self.assertIn(canonical({'artifact_refs': [ref], 'evidence_refs': [], 'job_refs': []}), summary['content'])

        self.agent.transition(self.project, self.run['id'], 1, 'running', event_key='old-running')
        self.agent.transition(self.project, self.run['id'], 1, 'completed', event_key='old-done',
                              outcome='answered', final_message='导出已保存')
        reopened = self.agent.create_run(self.project, self.session['id'],
            {'client_request_id': 'reopen', 'content': '重新下载之前的导出'}, context={}, config={}, limits={})
        bundle = Path(self.temp.name) / 'bundle'
        (bundle / 'config').mkdir(parents=True)
        (bundle / 'config' / 'engines.json').write_text('{}', encoding='utf-8')
        app = create_app(bundle, self.store.root, 'local-fixture-token', start_queue=False, agent_enabled=True)
        url = f'/api/projects/{self.project}/agent/artifacts/{key}'
        async with app.router.lifespan_context(app):
            runtime = app.state.agent_runtime
            continuation = runtime._continuation(reopened, before_seq=runtime._goal_seq(reopened))
            self.assertTrue(any(item.get('id') == 'history:boundary' for item in continuation))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                                         headers={'Authorization': 'Bearer local-fixture-token'}) as client:
                snapshot = await client.get(f'/api/agent/sessions/{self.session["id"]}')
                self.assertEqual(snapshot.status_code, 200)
                self.assertIn(self.run['id'], [run['id'] for run in snapshot.json()['runs']])
                self.assertIn(reopened['id'], [run['id'] for run in snapshot.json()['runs']])
                events = (await client.get(f'/api/agent/sessions/{self.session["id"]}/events')).json()['events']
                self.assertTrue(any(event['type'] == 'tool_finished' and
                    event['payload']['result']['artifact_refs'] == [ref] for event in events))
                response = await client.get(url + '/download')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, original)
                self.assertEqual((await client.get(url)).json()['state'], 'ready')
                self.assertEqual((await client.get(url.replace(self.project, self.other) + '/download')).status_code, 404)

                with self.store.transaction() as db:
                    db.execute('UPDATE agent_artifacts SET expires=? WHERE id=?', (time.time() - 1, key))
                expired = await client.get(url + '/download')
                self.assertEqual(expired.status_code, 400)
                self.assertIn('保留期', expired.json()['message'])
                with self.store.transaction() as db:
                    db.execute('UPDATE agent_artifacts SET expires=? WHERE id=?', (time.time() + 86400, key))
                target.unlink()
                missing = await client.get(url + '/download')
                self.assertEqual(missing.status_code, 400)
                self.assertIn('缺失', missing.json()['message'])
                self.assertEqual((await client.get(url)).json()['state'], 'missing')
                target.write_bytes(original)
                artifacts.reconcile(self.project, key)
                target.write_bytes(b'changed after publication')
                corrupt = await client.get(url + '/download')
                self.assertEqual(corrupt.status_code, 400)
                self.assertIn('校验失败', corrupt.json()['message'])
                self.assertEqual((await client.get(url)).json()['state'], 'corrupt')
                self.assertEqual(len(self.store.rows('SELECT id FROM agent_artifacts')), 1)
                self.assertEqual(self.store.rows('SELECT COUNT(*) count FROM agent_artifact_leases')[0]['count'], 0)
