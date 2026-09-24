"""Real HTTP/UI audit with synthetic controller responses and isolated data.

No real model, credentials, OCR engine or sealed evaluation corpus is used.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
import httpx
from PIL import Image
from fastapi.staticfiles import StaticFiles
import uvicorn
from ocr_workbench.agent.runtime import AgentRuntime
from ocr_workbench.agent.providers import normalize_response
from ocr_workbench.agent.store import digest
from ocr_workbench.external_review import CredentialVault
from ocr_workbench.imaging import add_image
from ocr_workbench.documents import PdfCPU
from ocr_workbench import external_review as ext
from ocr_workbench.agent.visual import targets_for_result
from ocr_workbench.service import create_app
from test_agent_connection import echo_probe
from test_structure_workflow import table
from test_document_processing import BUNDLE, FIXTURES
from test_external_review import ProviderFixture, KEY


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    token = 'isolated-agent-browser-fixture'
    app = create_app(ROOT, out / 'workspace', token, start_queue=False, agent_enabled=True)
    store = app.state.store
    project = store.project('助手合成场景验收')['id']
    image = out / 'financial.png'
    Image.new('RGB', (900, 700), 'white').save(image)
    photo = add_image(store, project, 'Decimal-fixture.png', image)
    task = store.enqueue(project, [photo['active_version']], ['ppocr'])[0]
    store.claim()
    financial = table([['项目', '金额（元）'], ['甲', '0.10'], ['乙', '0.20'], ['合计', '0.31']])
    financial['caption'] = '单位：元'
    store.complete(task, {'engine': 'ppocr', 'project_image_version': photo['active_version'], 'text': '应收账款 0.31 元\nAmount 001.00', 'tables': [financial], 'blocks': [], 'image': {'width': 900, 'height': 700}})
    result_id = store.one('tasks', task)['result_id']
    app.state.agent_connection.vault = CredentialVault(out / 'credentials')
    asyncio.run(app.state.agent_connection.save({'protocol': 'openai_chat_completions', 'base_url': 'https://synthetic.invalid/v1',
        'model': 'synthetic-ui', 'api_key': 'synthetic-ui-key'}, transport=httpx.MockTransport(echo_probe)))
    app.state.documents.cpu = PdfCPU(BUNDLE, store.root)
    pdf_code = "import pikepdf,sys; src=pikepdf.open(sys.argv[1]); out=pikepdf.Pdf.new(); [out.pages.extend(src.pages) for _ in range(int(sys.argv[3]))]; out.save(sys.argv[2])"
    pdfs = {}
    for tag, count in [('S01', 3), ('S04', 2)]:
        source = out / (tag + '.pdf')
        subprocess.run([str(BUNDLE / 'runtimes/pdf/python.exe'), '-c', pdf_code, str(FIXTURES / 'native.pdf'), str(source), str(count)], check=True)
        pdfs[tag] = app.state.documents.import_document(project, tag + '-native.pdf', source, 72)
    provider = ProviderFixture()
    app.state.external_connection.vault = CredentialVault(out / 'visual-credentials')
    probe = io.BytesIO(); Image.new('RGB', (640, 120), 'white').save(probe, format='PNG')
    with patch.object(ext, 'make_probe', return_value=(probe.getvalue(), provider.probe)):
        app.state.external_connection.save({'protocol': 'openai', 'base_url': provider.url, 'model': 'vision-a', 'api_key': KEY})
    visual_model = 'external:' + str(app.state.external_connection.view()['revision'])
    with store.transaction() as db:
        visual_target = next(target for target in targets_for_result(db, result_id) if '001.00' in target['before'])
    failed_once = set()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            app.state.external_queue.start()
            async def work():
                while True:
                    candidates = store.rows("""SELECT s.id FROM document_stages s JOIN pages p ON p.id=s.page_id
                        WHERE p.document_id=? AND p.page_number=2 AND s.kind='process' AND s.status='queued'""", (pdfs['S04']['id'],))
                    for row in candidates:
                        if row['id'] not in failed_once:
                            failed_once.add(row['id'])
                            with store.transaction() as db:
                                db.execute("UPDATE document_stages SET status='running' WHERE id=?", (row['id'],))
                            store.finish_document_stage(row['id'], error='synthetic first-attempt failure')
                    await asyncio.to_thread(app.state.documents.step)
                    await asyncio.sleep(.05)
            worker = asyncio.create_task(work())
            try:
                yield
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                await asyncio.to_thread(app.state.external_queue.stop)
    app.router.lifespan_context = lifespan
    requests = []

    async def model(manager, state, run):
        step, goal = state['step'], run['goal']
        name, arguments = None, None
        if goal.startswith('S02') and step == 0:
            name, arguments = 'inspect_table', {'result_id': result_id, 'revision': 0, 'table_id': 'table:0', 'checks': ['totals', 'rounding']}
        elif goal.startswith('S05') and step == 0:
            name, arguments = 'search_document', {'document_id': photo['id'], 'query': '应收账款'}
        elif goal.startswith('EXPORT') and step == 0:
            name, arguments = 'export_results', {'source': 'explicit_results', 'results': [{'result_id': result_id, 'revision': 0, 'version_id': photo['active_version']}], 'format': 'xlsx'}
        elif goal.startswith(('S01', 'S04')):
            tag = goal[:3]
            if step == 0:
                name, arguments = 'process_pages', {'document_id': pdfs[tag]['id'], 'page_numbers': list(range(1, pdfs[tag]['page_count'] + 1)), 'mode': 'native'}
            elif tag == 'S04' and step == 1:
                previous = next(m['result'] for m in reversed(state['messages']) if m['role'] == 'tool')
                name, arguments = 'retry_failed_jobs', {'previous_run_id': run['id'], 'operation_id': previous['data']['operation_id'], 'action': 'retry',
                    'jobs': [{'kind': job['kind'], 'job_id': job['job_id']} for job in previous['job_refs'] if job['state'] == 'failed']}
            elif step == (2 if tag == 'S04' else 1):
                name, arguments = 'export_results', {'source': 'run_results', 'source_run_id': run['id'], 'format': 'xlsx'}
            elif step == (3 if tag == 'S04' else 2):
                name, arguments = 'export_results', {'source': 'run_results', 'source_run_id': run['id'], 'format': 'txt'}
        elif goal.startswith('S03') and step == 0:
            name, arguments = 'request_visual_review', {'result_id': result_id, 'revision': store.one('results', result_id)['revision'], 'version_id': photo['active_version'], 'target_ids': [visual_target['id']], 'visual_model_id': visual_model}
        elif goal.startswith(('INBOX', 'S06')) and step == 0:
            name, arguments = 'ask_user', {'question': '请选择下一步', 'options': ['继续核对', '暂时结束']}
        message = {'role': 'assistant', 'content': goal.split()[0] + ' 合成场景已完成'}
        if name:
            message = {'role': 'assistant', 'tool_calls': [{'id': 'call-' + str(step), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
        request, fresh = manager.agent.begin_model_request(project, run['id'], state['generation'], step, digest(message))
        if not fresh:
            return json.loads(request['normalized_response'])
        requests.append({'run_id': run['id'], 'step': step, 'tool': name})
        reply = normalize_response('openai_chat_completions', {'choices': [{'message': message, 'finish_reason': 'tool_calls' if name else 'stop'}]}, request['request_id'])
        manager.agent.finish_model_request(project, run['id'], state['generation'], request['request_id'], reply)
        return reply

    @app.post('/api/audit/restart-agent')
    async def restart_agent():
        await app.state.agent_runtime.close()
        await app.state.agent_runtime.open()
        return {'restarted': True}

    @app.get('/api/audit/fixture')
    def receipt():
        return {'requests': requests, 'result_revision': store.one('results', result_id)['revision'],
                'stages': store.rows("SELECT s.id,s.status,p.document_id,p.page_number FROM document_stages s JOIN pages p ON p.id=s.page_id WHERE s.kind='process'"),
                'original_text': json.loads(store.one('results', result_id)['original'])['text'],
                'edited_text': json.loads(store.one('results', result_id)['edited'])['text'],
                'visual_requests': len(provider.requests),
                'runs': store.rows('SELECT id,status,outcome,generation FROM agent_runs'),
                'calls': store.rows('SELECT tool,state FROM agent_calls'),
                'artifacts': store.rows('SELECT id,state FROM agent_artifacts')}

    @app.post('/api/audit/long-history')
    def long_history():
        agent = app.state.agent_runtime.agent
        for index in range(50):
            agent.create_session(project, {'client_request_id': 'older-' + str(index), 'title': '历史会话 ' + str(index)})
        session = agent.create_session(project, {'client_request_id': 'long', 'title': '十万事件会话'})
        for batch in range(100):
            with store.transaction() as db:
                db.execute('BEGIN IMMEDIATE')
                for index in range(batch * 1000, (batch + 1) * 1000):
                    agent._event(db, session['id'], None, None, 'long-' + str(index), 'message', {'role': 'assistant', 'content': '历史记录 ' + str(index + 1)})
        return {'session_id': session['id']}

    app.mount('/', StaticFiles(directory=ROOT / 'frontend/dist', html=True))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning', access_log=False))
    # The service creates its runtime during lifespan startup. Install the
    # explicit synthetic-model hook before that real open, including on reopen.
    # This UI fixture does not make provider-backed semantic-summary requests.
    runtime_open = AgentRuntime.open
    async def open_with_fixture(manager):
        manager.model_override = model.__get__(manager, AgentRuntime)
        return await runtime_open(manager)
    with patch.object(AgentRuntime, 'open', open_with_fixture):
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if time.monotonic() > deadline:
                    raise TimeoutError('Service startup failed')
                time.sleep(.03)
            seed = {'base': 'http://127.0.0.1:' + str(server.servers[0].sockets[0].getsockname()[1]), 'token': token,
                    'project': project, 'result': result_id, 'photo': photo['id'], 'pdfs': pdfs, 'visual_model': visual_model}
            (out / 'seed.json').write_text(json.dumps(seed), encoding='utf-8')
            code = subprocess.run(['node', str(ROOT / 'scripts/agent_eval/audit_ui.mjs'), str(out)], cwd=ROOT).returncode
        finally:
            server.should_exit = True
            thread.join(20)
            provider.close()
            (out / 'shutdown.json').write_text(json.dumps({'service_closed': not thread.is_alive(), 'cloud_api_tested': False}), encoding='utf-8')
    if thread.is_alive():
        raise RuntimeError('Service did not shut down')
    raise SystemExit(code)


if __name__ == '__main__':
    main()
