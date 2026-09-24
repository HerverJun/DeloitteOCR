"""Real lazy PDF -> native worker -> result/evidence -> durable export, synthetic model."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.agent.store import digest
from ocr_workbench.agent.selection import create_selection, grant_selection
from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.documents import Documents
from ocr_workbench.store import Store
import test_document_processing


@unittest.skipUnless((test_document_processing.BUNDLE / 'runtimes/pdf/python.exe').exists(), 'Isolated PDF runtime required')
class MinimalChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_native_page_and_export_with_explicit_question(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            project = store.project('实际原生处理链路')['id']
            documents = Documents(store, test_document_processing.BUNDLE)
            doc = await asyncio.to_thread(documents.import_document, project, 'native.pdf', test_document_processing.FIXTURES / 'native.pdf', 72)
            page = store.rows('SELECT * FROM pages WHERE document_id=? ORDER BY page_number', (doc['id'],))[0]
            self.assertIsNone(page['image_id'])
            services = ApplicationServices(store, documents, SimpleNamespace(guard=store.file_lock))
            trace = []
            async def model(state, run):
                step = state['step']
                if step == 0:
                    name, args = 'get_workspace_context', {}
                elif step == 1:
                    name, args = 'read_page_result', {'page_id': page['id']}
                elif step == 2:
                    name, args = 'process_pages', {'document_id': doc['id'], 'page_numbers': [1], 'mode': 'native'}
                elif step == 3:
                    result_id = store.rows("SELECT json_extract(output,'$.result_id') id FROM document_stages WHERE page_id=? AND kind='process'", (page['id'],))[0]['id']
                    name, args = 'read_page_result', {'page_id': page['id'], 'source': 'run_result', 'result_id': result_id}
                elif step == 4:
                    name, args = 'export_results', {'source': 'run_results', 'source_run_id': run['id'], 'format': 'txt'}
                elif step == 5:
                    name, args = 'ask_user', {'question': '已保存文本，还需要哪种输出？', 'options': ['结束', '稍后处理']}
                else:
                    name, args = None, None
                trace.append(name or 'answer')
                message = {'role': 'assistant', 'content': '已核对实际结果并保存导出。'}
                if name:
                    message = {'role': 'assistant', 'tool_calls': [{'id': f'call-{step}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
                record, fresh = runtime.agent.begin_model_request(project, run['id'], state['generation'], step, digest(message))
                if not fresh:
                    return json.loads(record['normalized_response'])
                reply = normalize_response('openai_chat_completions', {'choices': [{'message': message, 'finish_reason': 'tool_calls' if name else 'stop'}]}, record['request_id'])
                runtime.agent.finish_model_request(project, run['id'], state['generation'], record['request_id'], reply)
                return reply
            runtime = await AgentRuntime(services, policy={}, model_override=model).open()
            try:
                agent = runtime.agent
                session = agent.create_session(project, {'client_request_id': 's', 'title': '最小链路'})
                selected = create_selection(agent, project, {'document_ranges': [{'document_id': doc['id'], 'first_page': 1, 'last_page': 1}],
                    'allow_processing': True, 'allow_export': True}, available_engines=[])
                run = agent.create_run(project, session['id'], {'client_request_id': 'r', 'content': '处理第一页并导出文本'},
                    context={'selection': selected['selection'], 'selection_token': selected['selection_token']}, config={}, limits={})
                grant_selection(runtime.policy, run, selected['selection'])
                runtime.schedule(run)
                for _ in range(400):
                    current = agent.run(project, run['id'])
                    if current['status'] in ('waiting_user', 'failed', 'completed'):
                        break
                    if current['status'] == 'waiting_jobs':
                        await asyncio.to_thread(documents.step)
                    await asyncio.sleep(.03)
                self.assertEqual(current['status'], 'waiting_user', agent.events(project, session['id']))
                self.assertEqual(current['coverage']['completed_count'], 1)
                self.assertEqual(trace, ['get_workspace_context', 'read_page_result', 'process_pages', 'read_page_result', 'export_results', 'ask_user'])
                artifact = store.rows("SELECT * FROM agent_artifacts WHERE state='ready'")[0]
                text = runtime.artifacts.verify(project, artifact['id']).read_text('utf-8-sig')
                self.assertTrue(text.strip())
                self.assertEqual(len(store.rows('SELECT * FROM agent_evidence')), 1)
                decision = store.rows("SELECT * FROM agent_decisions WHERE status='pending'")[0]
                agent.reply_decision(project, run['id'], decision['id'], {'client_request_id': 'reply', 'payload_hash': decision['payload_hash'], 'option_id': 'option-0'})
                if run['id'] in runtime.tasks:
                    await runtime.tasks[run['id']]
                runtime.schedule(current, resume=True)
                await runtime.tasks[run['id']]
                self.assertEqual(agent.run(project, run['id'])['status'], 'completed')
                self.assertEqual(agent.run(project, run['id'])['outcome'], 'success')
                self.assertEqual(len(store.rows('SELECT * FROM agent_artifacts')), 1)
            finally:
                await runtime.close()
                await asyncio.to_thread(documents.stop)
