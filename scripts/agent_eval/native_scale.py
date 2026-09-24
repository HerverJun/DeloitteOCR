"""Measure real native-PDF work, graph orchestration and two event readers.

Controller replies are deterministic fixtures. This is CPU PDF extraction, not
GPU OCR throughput, real-controller qualification or a browser performance test.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(out, report):
    temporary = out / 'native-scale.tmp'
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    temporary.replace(out / 'native-scale.json')


async def measure(out, bundle, report):
    from ocr_workbench.store import Store
    from ocr_workbench.documents import Documents
    from ocr_workbench.application_services import ApplicationServices
    from ocr_workbench.agent.runtime import AgentRuntime
    from ocr_workbench.agent.providers import normalize_response
    from ocr_workbench.agent.store import digest
    from ocr_workbench.agent.selection import create_selection, grant_selection
    from performance import resource_sample, distribution

    async def sample(label, count, mode):
        store = Store(out / label / 'workspace')
        project = store.project('Native PDF measurement')['id']
        documents = Documents(store, bundle)
        doc = await asyncio.to_thread(documents.import_document, project, 'synthetic-native.pdf', out / f'native-{count}.pdf', 72)
        pages = store.document_pages(doc['id'], limit=200)
        services = ApplicationServices(store, documents, SimpleNamespace(guard=store.file_lock))
        runtime, stop_readers = None, asyncio.Event()
        reads, clients = [[], []], []
        request_log, stages, resource_before = [], [], resource_sample()
        started, scheduled_at, done_at = time.perf_counter(), None, None

        async def model(state, current):
            name, args = None, None
            if state['step'] == 0:
                name = 'process_pages' if current['goal'] == 'process' else 'get_workspace_context'
                args = {'document_id': doc['id'], 'page_numbers': list(range(1, count + 1)), 'mode': 'native'} if name == 'process_pages' else {}
            elif state['step'] == 1 and current['goal'] == 'process':
                name, args = 'export_results', {'source': 'run_results', 'source_run_id': current['id'], 'format': 'txt'}
            message = {'role': 'assistant', 'content': 'Synthetic controller finished.'}
            if name:
                message = {'role': 'assistant', 'tool_calls': [{'id': f'call-{state["step"]}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
            request, fresh = runtime.agent.begin_model_request(project, current['id'], state['generation'], state['step'], digest(message))
            if not fresh:
                return json.loads(request['normalized_response'])
            request_log.append({'step': state['step'], 'goal': current['goal'], 'tool': name, 'seconds': time.perf_counter() - started,
                                'pending_stages': len(store.rows("SELECT id FROM document_stages WHERE kind='process' AND status NOT IN ('succeeded','failed','cancelled')"))})
            response = normalize_response('openai_chat_completions', {'choices': [{'message': message, 'finish_reason': 'tool_calls' if name else 'stop'}]}, request['request_id'])
            runtime.agent.finish_model_request(project, current['id'], state['generation'], request['request_id'], response)
            return response

        async def reader(index, session):
            cursor = 0
            while not stop_readers.is_set():
                begin = time.perf_counter()
                events = await asyncio.to_thread(runtime.agent.events, project, session, cursor, 500)
                if events:
                    cursor = events[-1]['seq']
                reads[index].append(time.perf_counter() - begin)
                await asyncio.sleep(.1)

        async def read_loop():
            index = 0
            while not stop_readers.is_set():
                session = runtime.agent.create_session(project, {'client_request_id': 'read-' + str(index), 'title': 'Parallel read'})
                current = runtime.agent.create_run(project, session['id'], {'client_request_id': 'run', 'content': 'read'}, context={}, config={}, limits={})
                runtime.schedule(current)
                await asyncio.wait_for(runtime.tasks[current['id']], 30)
                assert runtime.agent.run(project, current['id'])['status'] == 'completed'
                index += 1
                await asyncio.sleep(.25)

        try:
            if mode != 'disabled':
                runtime = await AgentRuntime(services, policy={}, model_override=model).open()
                session = runtime.agent.create_session(project, {'client_request_id': 'process', 'title': label})
                clients = [asyncio.create_task(reader(i, session['id'])) for i in range(2)]
                if mode == 'read':
                    clients.append(asyncio.create_task(read_loop()))
            started = time.perf_counter()
            if mode == 'orchestrated':
                selected = create_selection(runtime.agent, project, {'document_ranges': [{'document_id': doc['id'], 'first_page': 1, 'last_page': count}], 'allow_processing': True, 'allow_export': True}, available_engines=[])
                current = runtime.agent.create_run(project, session['id'], {'client_request_id': 'run', 'content': 'process'},
                    context={'selection': selected['selection'], 'selection_token': selected['selection_token']}, config={}, limits={})
                grant_selection(runtime.policy, current, selected['selection'])
                runtime.schedule(current)
            else:
                documents.process_pages([p['id'] for p in pages], 'native')
                scheduled_at = time.perf_counter()
            deadline = started + max(180, count * 10)
            while time.perf_counter() < deadline:
                stages = store.rows("SELECT * FROM document_stages WHERE kind='process'")
                if stages and scheduled_at is None:
                    scheduled_at = time.perf_counter()
                if stages and all(s['status'] == 'succeeded' for s in stages):
                    done_at = time.perf_counter()
                    break
                assert not any(s['status'] in ('failed', 'cancelled') for s in stages), stages
                await asyncio.to_thread(documents.step)
                await asyncio.sleep(0)
            assert done_at is not None and len(stages) == count, 'Real PDF stages did not all complete'
            artifact_hash, projection_seconds = None, None
            if mode == 'orchestrated':
                while time.perf_counter() < deadline:
                    current = runtime.agent.run(project, current['id'])
                    if current['status'] in ('failed', 'completed', 'waiting_user'):
                        break
                    await asyncio.sleep(.02)
                assert current['status'] == 'completed' and current['outcome'] == 'success', current
                assert current['coverage']['completed_count'] == count, current['coverage']
                projection_seconds = time.perf_counter() - done_at
                assert len(request_log) == 3 and all(r['pending_stages'] == 0 for r in request_log), request_log
                artifact = store.rows("SELECT * FROM agent_artifacts WHERE state='ready'")
                assert len(artifact) == 1
                exported = runtime.artifacts.verify(project, artifact[0]['id'])
                if zipfile.is_zipfile(exported):
                    with zipfile.ZipFile(exported) as archive:
                        text = '\n'.join(archive.read(name).decode('utf-8-sig') for name in archive.namelist() if name.endswith('.txt'))
                else:
                    text = exported.read_text('utf-8-sig')
                for page in range(1, count + 1):
                    assert f'Native page {page:04d}' in text, page
                artifact_hash = checksum(exported)
            if runtime:
                assert not runtime.observer_error, runtime.observer_error
            assert len(store.rows("SELECT id FROM results")) == count
            return {'label': label, 'mode': mode, 'pages': count, 'stage_count': len(stages), 'result_count': count,
                    'processing_seconds': done_at - scheduled_at, 'submit_seconds': scheduled_at - started,
                    'completion_to_final_export_seconds': projection_seconds, 'artifact_sha256': artifact_hash,
                    'model_requests': request_log, 'two_event_readers': [distribution(values) for values in reads],
                    'resources_before': resource_before, 'resources_after_work': resource_sample(),
                    'source_sha256': report['source_sha256']}
        finally:
            stop_readers.set()
            await asyncio.gather(*clients)
            if runtime:
                await runtime.close()
            await asyncio.to_thread(documents.stop)

    report['samples'] = []
    # Rotate order to expose warm-cache/order effects. Each trial gets a fresh DB.
    for repeat, modes in enumerate([('disabled', 'idle', 'read'), ('read', 'disabled', 'idle'), ('idle', 'read', 'disabled')]):
        for mode in modes:
            result = await sample(f'{repeat}-{mode}', 5, mode)
            report['samples'].append(result)
            save(out, report)
            print(json.dumps({'sample': result['label'], 'seconds': result['processing_seconds']}), flush=True)
    report['hundred_page_real_native'] = await sample('100-native', 100, 'orchestrated')
    medians = {mode: statistics.median(s['processing_seconds'] for s in report['samples'] if s['mode'] == mode) for mode in ('disabled', 'idle', 'read')}
    report['cpu_native_comparison'] = {'median_seconds': medians, 'relative_elapsed_change_percent': {mode: (medians[mode] / medians['disabled'] - 1) * 100 for mode in ('idle', 'read')},
        'scope': 'Three five-page CPU extraction samples per state; two application event readers, no browser clients or GPU. Concurrent machine load is uncontrolled.'}
    report['status'] = 'pass'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out, bundle = args.output.resolve(), args.bundle.resolve()
    out.mkdir(parents=True, exist_ok=False)
    shutil.copytree(ROOT / 'src', out / 'source/src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(ROOT / 'config', out / 'source/config', ignore=shutil.ignore_patterns('development-machine.json'))
    sources = {p.relative_to(out / 'source').as_posix(): checksum(p) for p in (out / 'source').rglob('*') if p.is_file()}
    (out / 'source-manifest.json').write_text(json.dumps(sources, indent=2), 'utf-8')
    sys.path.insert(0, str(out / 'source/src'))
    code = """from fpdf import FPDF
from pathlib import Path
import sys
for count in (5,100):
 p=FPDF(unit='pt',format=(300,400));p.set_auto_page_break(False)
 for n in range(1,count+1):
  p.add_page();p.set_font('Helvetica',size=12);p.text(30,70,f'Native page {n:04d}');p.text(30,100,'Amount 123.45 Account 00123456789012345678')
 p.output(Path(sys.argv[1])/f'native-{count}.pdf')
"""
    subprocess.run([str(bundle / 'runtimes/pdf/python.exe'), '-B', '-c', code, str(out)], check=True)
    report = {'status': 'running', 'started_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(), 'python': sys.version,
        'source_sha256': checksum(out / 'source-manifest.json'), 'fixture_sha256': {p.name: checksum(p) for p in out.glob('*.pdf')},
        'bundle_manifest_sha256': checksum(bundle / 'manifest.json'), 'real_controller': 'not_tested', 'gpu_ocr': 'not_tested', 'real_visual_provider': 'not_tested', 'four_hour_stability': 'not_tested'}
    try:
        asyncio.run(measure(out, bundle, report))
    except BaseException as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save(out, report)


if __name__ == '__main__':
    main()
