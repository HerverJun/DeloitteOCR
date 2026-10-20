"""J08 resumes a native-proven unsent intent while unknown writes stay fenced."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from workbench_platform.agent.store import AgentStore, digest
from workbench_platform.application_services.context import ToolContext
from workbench_platform.application_services.errors import NativeNotSubmitted
from workbench_platform.application_services.image_text import _operation, ReceiptUnknown
from workbench_platform.application_services.operations import ocr


def original_intent(tmp_path, *, name='ocr_run', arguments=None):
    store = AgentStore(tmp_path / 'agent')
    session = store.create_session('workspace', 'Original OCR then compare', [], 'session')
    run, _ = store.start_run(session['id'], 'Extract then compare', 'original-goal', 0,
        driver='image-text-v1', driver_state={'resource_object': 'project:original', 'scope_workspace_id': 'workspace'})
    arguments = arguments or {'link_id': 'original-link', 'asset_set_id': 'original-set', 'asset_set_revision': 1, 'engines': ['ppocr']}
    call = {'call_id': 'direct-' + name + '-' + digest(arguments)[:24], 'tool': name, 'arguments': arguments}
    operation, _ = store.begin_operation(run['id'], 1, call)
    # User pause fences the old handler; Continue retains operation and input.
    with store.transaction() as db:
        db.execute("UPDATE runs SET status='paused',generation=2 WHERE id=?", (run['id'],))
    store.status(run['id'], 2, 'running')
    services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()))
    ctx = ToolContext('workspace', session['id'], run['id'], 2, 0, '', (), services, store)
    return SimpleNamespace(store=store), ctx, arguments, operation


def test_paused_unsent_intent_continues_same_operation_once_from_authoritative_negative(tmp_path):
    runtime, ctx, arguments, original = original_intent(tmp_path)
    proof = NativeNotSubmitted(original['id'], 'ocr_run', 'original-stable-native-request', 'original-project', 'a' * 64)
    lookup = Mock(return_value=proof)
    accepted = {'status': 'pending', 'summary': 'Original request accepted', 'job_refs': [
        {'kind': 'job', 'workspace_id': 'workspace', 'application': 'ocr', 'object_id': 'original-native-job', 'revision': 1}]}
    handler = Mock(return_value=accepted)
    first, received_ctx = asyncio.run(_operation(runtime, ctx, 'ocr_run', arguments, handler, lookup=lookup))
    again, _ = asyncio.run(_operation(runtime, ctx, 'ocr_run', arguments, handler, lookup=lookup))
    assert first == again and received_ctx.operation_id == original['id']
    assert handler.call_count == lookup.call_count == 1
    assert handler.call_args.args[1].operation_id == original['id']
    assert handler.call_args.args[0] == arguments
    assert len(runtime.store.rows('SELECT * FROM operations')) == 1
    assert len(runtime.store.rows("SELECT * FROM events WHERE kind='native_not_submitted'")) == 1


@pytest.mark.parametrize('receipt', ['unknown', 'wrong_operation', 'transport_error'])
def test_unknown_or_unbound_receipt_never_reenters_native_write(tmp_path, receipt):
    runtime, ctx, arguments, original = original_intent(tmp_path)
    lookup = Mock(return_value=None)
    if receipt == 'wrong_operation':
        lookup.return_value = NativeNotSubmitted('different-operation', 'ocr_run', 'request', 'project', 'a' * 64)
    if receipt == 'transport_error':
        lookup.side_effect = OSError('Native receipt transport unavailable')
    handler = Mock(side_effect=AssertionError('Unknown writes must never be resent'))
    with pytest.raises(ReceiptUnknown):
        asyncio.run(_operation(runtime, ctx, 'ocr_run', arguments, handler, lookup=lookup))
    handler.assert_not_called()
    assert runtime.store.rows('SELECT state FROM operations')[0]['state'] == 'intent'


def test_pdf_unsent_intent_continues_original_key_with_real_native_atomic_page_ledger(tmp_path, monkeypatch):
    from contextlib import closing
    from pathlib import Path
    from workbench_platform.application_services.operations import pdf
    from workbench_platform.jobs.ocr_handoff import OcrHandoff
    from workbench_platform.jobs.native_ocr import OcrAdapter, OcrUnavailable
    monkeypatch.syspath_prepend(str(Path('F:/OCR/output/clear-automation-dev-20261002/worktrees/ocr/src')))
    from ocr_workbench.store import Store, now
    from ocr_workbench import page_requests
    from threading import Event

    native = Store(tmp_path / 'native')
    project = native.project('Original PDF project')['id']
    document = 'original_document'
    with native.transaction() as db:
        db.execute('INSERT INTO documents(id,project_id,name,kind,original_path,sha256,page_count,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
            (document, project, 'original.pdf', 'pdf', 'original.pdf', 'a' * 64, 5, now(), now()))
        for number in range(1, 6):
            db.execute('INSERT INTO pages(id,document_id,page_number,created,updated) VALUES(?,?,?,?,?)',
                       ('page_' + str(number), document, number, now(), now()))
    # Exercise the actual native atomic request/stage ledger without starting
    # PDF CPU workers (the platform runtime intentionally has no Pillow).
    documents = SimpleNamespace(store=native, wake=Event(),
        processing_parameters=lambda mode, engine: {'mode': mode, 'engine': engine})
    args = {'handoff_id': 'original-handoff', 'document_id': document,
            'page_numbers': [2, 5], 'mode': 'native', 'engine': 'ppocr'}
    runtime, ctx, arguments, original = original_intent(tmp_path, name='ocr_process_pages', arguments=args)
    adapter = object.__new__(OcrAdapter)
    adapter.context = SimpleNamespace(module_project_id=project, role='owner')
    adapter.snapshot = Mock(return_value={'documents': [{'id': document}]})
    adapter.import_status = Mock(return_value={'state': 'completed', 'items': [
        {'target_document_id': document, 'page_count': 5}]})
    posts = []
    def request(method, path, body=None, **kwargs):
        request_id = path.rsplit('/', 1)[-1]
        assert path.startswith('/api/platform/documents/' + document + '/requests/')
        if method == 'GET':
            return page_requests.read(native, document, request_id)
        assert method == 'POST'
        posts.append((request_id, body))
        return page_requests.submit(documents, document, request_id, body)
    adapter._request = request
    handoff = OcrHandoff(tmp_path / 'handoff.sqlite3', None, None, None)
    handoff._context = lambda *_: adapter.context
    handoff._adapter = lambda *_: adapter
    ctx.services.ocr_handoff = handoff
    row = {'link_id': 'original-link', 'request_id': 'original-import'}
    with patch.object(pdf, 'scoped_handoff', return_value=row):
        operation_ctx = replace(ctx, operation_id=original['id'])
        assert pdf.lookup(pdf.ProcessPages.model_validate(args), operation_ctx) is None
        proof = pdf.lookup(pdf.ProcessPages.model_validate(args), operation_ctx, include_not_submitted=True)
        assert isinstance(proof, NativeNotSubmitted) and proof.tool == 'ocr_process_pages'
        assert proof.project_id == project and proof.operation_id == original['id'] and posts == []
        handler = lambda args, current: pdf.process(pdf.ProcessPages.model_validate(args), current)
        lookup = lambda args, current: pdf.lookup(pdf.ProcessPages.model_validate(args), current, include_not_submitted=True)
        first, received_ctx = asyncio.run(_operation(runtime, ctx, 'ocr_process_pages', args, handler, lookup=lookup))
        again, _ = asyncio.run(_operation(runtime, ctx, 'ocr_process_pages', args, handler, lookup=lookup))
        assert first == again and received_ctx.operation_id == original['id']
        assert len(posts) == 1 and posts[0][0] == proof.request_id
        assert posts[0][1] == {'page_numbers': [2, 5], 'mode': 'native', 'engine': 'ppocr', 'force': False}
        # Reentering the native handler with the original key also deduplicates.
        assert pdf.process(pdf.ProcessPages.model_validate(args), received_ctx) == first
        assert len(posts) == 1
        assert len(native.rows('SELECT * FROM platform_page_requests')) == 1
        assert len(native.rows('SELECT * FROM document_stages')) == 2
        with closing(handoff._connect()) as db:
            assert db.execute('SELECT request_id FROM ocr_page_submission').fetchone()[0] == proof.request_id
            db.execute("UPDATE ocr_page_submission SET request_id='different-persisted-key'")
        with pytest.raises(ValueError, match='原请求身份'):
            pdf.lookup(pdf.ProcessPages.model_validate(args), received_ctx, include_not_submitted=True)
        with closing(handoff._connect()) as db:
            db.execute('UPDATE ocr_page_submission SET request_id=?', (proof.request_id,))
        # An unavailable receipt or wrong live project is never an unsent proof.
        adapter._request = Mock(side_effect=OcrUnavailable())
        with pytest.raises(OcrUnavailable):
            pdf.lookup(pdf.ProcessPages.model_validate(args), received_ctx, include_not_submitted=True)
        adapter._request = lambda *a, **kw: {'submitted': False, 'document_id': document,
                                          'project_id': 'different-project', 'request_id': proof.request_id}
        with pytest.raises(OcrUnavailable):
            pdf.lookup(pdf.ProcessPages.model_validate(args), received_ctx, include_not_submitted=True)
    assert len(runtime.store.rows('SELECT * FROM operations')) == 1


def test_cold_pdf_intent_prepares_only_original_handoff_link(tmp_path):
    from workbench_platform.application_services.operations import pdf
    runtime, ctx, _, original = original_intent(tmp_path)
    ctx = replace(ctx, operation_id=original['id'])
    ctx.services.ocr_handoff = object()
    args = pdf.ProcessPages(handoff_id='original-handoff', document_id='original-document', page_numbers=[2, 5])
    ready = {'value': False}
    def prepare(launch_context, family, link):
        assert family == 'ocr' and link == 'original-link'
        assert launch_context.operation_id != original['id']
        assert launch_context.run_id == ctx.run_id and launch_context.generation == ctx.generation
        ready['value'] = True
    def submission(received, current, allow_new, **kwargs):
        assert ready['value'] and not allow_new and kwargs == {'include_not_submitted': True}
        assert current.operation_id == original['id'] and received == args
        return NativeNotSubmitted(original['id'], 'ocr_process_pages', 'original-request', 'original-project', 'd' * 64)
    with patch.object(pdf, 'availability', return_value={'state': 'preparable'}), \
         patch.object(pdf, 'scoped_handoff', return_value={'link_id': 'original-link'}), \
         patch.object(pdf, 'prepare', side_effect=prepare) as start, \
         patch.object(pdf, 'submission', side_effect=submission):
        proof = pdf.lookup(args, ctx, include_not_submitted=True)
    assert isinstance(proof, NativeNotSubmitted) and start.call_count == 1


def test_ocr_negative_proof_requires_authority_and_preserves_original_operation_identity():
    arguments = ocr.OcrRun(link_id='link', asset_set_id='original-set', asset_set_revision=1, engines=['ppocr'])
    negative = {'not_submitted': True, 'authoritative_negative': True, 'native_request_id': 'stable-native-request',
                'native_project_id': 'original-project', 'input_fingerprint': 'b' * 64}
    handoff = SimpleNamespace(begin=Mock(return_value={'handoff_id': 'original-handoff'}), submit=Mock(return_value=negative))
    ctx = SimpleNamespace(workspace_id='workspace', operation_id='original-operation', current=Mock(),
                          services=SimpleNamespace(ocr_handoff=handoff))
    frozen = SimpleNamespace(asset_set_id='original-attempt', revision=1)
    with patch.object(ocr, 'scoped_set', return_value=object()), patch.object(ocr, 'attempt_set', return_value=frozen):
        assert ocr.lookup(arguments, ctx) is None
        proof = ocr.lookup(arguments, ctx, include_not_submitted=True)
        assert isinstance(proof, NativeNotSubmitted) and proof.operation_id == 'original-operation'
        assert proof.request_id == 'stable-native-request'
        negative['authoritative_negative'] = False
        assert ocr.lookup(arguments, ctx, include_not_submitted=True) is None
    assert all(call.kwargs['allow_new'] is False for call in handoff.submit.call_args_list)


def test_real_handoff_and_native_adapter_keep_the_negative_receipt_key_on_continuation(tmp_path):
    from contextlib import closing
    from workbench_platform.jobs.ocr_handoff import OcrHandoff
    from workbench_platform.jobs.native_ocr import OcrAdapter, OcrUnavailable
    native_requests, posts = {}, []
    adapter = object.__new__(OcrAdapter)
    adapter.context = SimpleNamespace(module_project_id='original-project', role='owner')
    adapter.import_status = Mock(return_value={'state': 'completed', 'items': [{'target_version_id': 'original-version'}]})
    adapter.capabilities = Mock(return_value=SimpleNamespace(recognition=True))
    adapter.snapshot = Mock(return_value={'versions': [{'id': 'original-version'}]})
    def request(method, path, body=None, **kwargs):
        if method == 'GET' and '/task-requests/' in path:
            tasks = native_requests.get(path.rsplit('/', 1)[-1])
            return {'submitted': tasks is not None, 'task_ids': tasks or []}
        if method == 'GET' and path == '/api/state':
            return {'engines': {'ppocr': {}}}
        assert method == 'POST' and path.endswith('/tasks')
        posts.append(body)
        tasks = native_requests.setdefault(body['request_id'], ['original-native-task'])
        return {'task_ids': tasks}
    adapter._request = request
    jobs = SimpleNamespace(register=Mock(return_value={'platform_job_id': 'original-platform-job'}))
    handoff = OcrHandoff(tmp_path / 'handoff.sqlite3', jobs, None, None)
    handoff._context = lambda *_: adapter.context
    handoff._adapter = lambda *_: adapter
    handoff.status = lambda *_: {'handoff_id': 'original-handoff', 'processing': {'engines': ['ppocr']}}
    with closing(handoff._connect()) as db:
        db.execute('INSERT INTO ocr_handoff VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('original-handoff', 'workspace', 'original-link', 'original-set', 1, 'a' * 64,
             'original-import', '{"items":[{"name":"original.png"}]}', None, 'now', 'now'))
    negative = handoff.submit('workspace', 'original-handoff', ['ppocr'], allow_new=False)
    assert negative['not_submitted'] is True and negative['authoritative_negative'] is True
    assert posts == []
    accepted = handoff.submit('workspace', 'original-handoff', ['ppocr'])
    again = handoff.submit('workspace', 'original-handoff', ['ppocr'])
    assert accepted == again and len(posts) == 1
    assert posts[0]['request_id'] == negative['native_request_id']
    assert native_requests == {negative['native_request_id']: ['original-native-task']}
    with closing(handoff._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM ocr_submission').fetchone()[0] == 1
        db.execute("UPDATE ocr_submission SET request_id='different-persisted-key'")
    from workbench_platform.assets.sets import AssetSetConflict
    with pytest.raises(AssetSetConflict):
        handoff.submit('workspace', 'original-handoff', ['ppocr'], allow_new=False)
    with closing(handoff._connect()) as db:
        db.execute('UPDATE ocr_submission SET request_id=?', (negative['native_request_id'],))

    # An unavailable native query is an exception, never a negative receipt.
    with closing(handoff._connect()) as db:
        db.execute('UPDATE ocr_submission SET native_task_ids_json=NULL,platform_job_id=NULL')
    adapter._request = Mock(side_effect=OcrUnavailable())
    with pytest.raises(OcrUnavailable):
        handoff.submit('workspace', 'original-handoff', ['ppocr'], allow_new=False)
    assert len(posts) == 1


def test_cold_intent_lookup_prepares_original_service_without_changing_business_identity(tmp_path):
    runtime, ctx, arguments, original = original_intent(tmp_path)
    ctx = replace(ctx, operation_id=original['id'])
    negative = {'not_submitted': True, 'authoritative_negative': True, 'native_request_id': 'stable-native-request',
                'native_project_id': 'original-project', 'input_fingerprint': 'c' * 64}
    state = {'ready': False}
    def ready(*args):
        assert state['ready'], 'Cold service must be restored before querying original handoff'
        return {'handoff_id': 'original-handoff'}
    handoff = SimpleNamespace(begin=Mock(side_effect=ready), submit=Mock(return_value=negative))
    ctx.services.ocr_handoff = handoff
    def prepare(launch_context, family, link):
        assert family == 'ocr' and link == arguments['link_id']
        assert launch_context.operation_id != original['id']
        assert launch_context.run_id == ctx.run_id and launch_context.generation == ctx.generation
        state['ready'] = True
    with patch.object(ocr, 'availability', return_value={'state': 'preparable'}), \
         patch.object(ocr, 'prepare', side_effect=prepare) as start, \
         patch.object(ocr, 'scoped_set', return_value=object()), \
         patch.object(ocr, 'attempt_set', return_value=SimpleNamespace(asset_set_id='original-attempt', revision=1)):
        proof = ocr.lookup(ocr.OcrRun.model_validate(arguments), ctx, include_not_submitted=True)
    assert isinstance(proof, NativeNotSubmitted) and proof.operation_id == original['id']
    assert start.call_count == 1 and handoff.submit.call_args.kwargs['allow_new'] is False
    assert runtime.store.rows('SELECT state FROM operations')[0]['state'] == 'intent'
