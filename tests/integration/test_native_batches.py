from types import SimpleNamespace

import pytest

from workbench_platform.batches.native import NativeBatchError, NativeBatchRunner


@pytest.fixture
def disposition_bridge(tmp_path, monkeypatch):
    """Real native stores over loopback HTTP; no queue, model or native service."""
    import hashlib
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import socket
    import threading
    from urllib.parse import parse_qs, urlsplit
    from PIL import Image
    from workbench_platform.batches.service import BatchService
    from workbench_platform.batches.store import BatchStore
    from workbench_platform.bridge.context import ResolvedContext
    from workbench_platform.jobs.routes import JobService
    from workbench_platform.jobs.native_classification import ClassificationAdapter
    from workbench_platform.jobs.native_ocr import OcrAdapter

    monkeypatch.syspath_prepend('F:/OCR/output/clear-automation-dev-20261002/worktrees/classification/src')
    monkeypatch.syspath_prepend('F:/OCR/output/clear-automation-dev-20261002/worktrees/ocr/src')
    active = []

    def build(scenario='classification', count=1):
        root = tmp_path / (scenario + str(len(active)))
        root.mkdir()
        original_path = root / 'original.png'
        Image.new('RGB', (20, 20), 'blue').save(original_path)
        source_hash = hashlib.sha256(original_path.read_bytes()).hexdigest()
        state = {'instance_id': 'instanceA', 'posts': 0, 'calls': [], 'drop': False,
                 'missing': False, 'tamper': False, 'before_post': None}
        if scenario == 'classification':
            from image_platform.store import Store, dump, now
            from image_platform.settings import Settings
            native = Store(Settings(root / 'native'))
            project_id = 'projectA'
            with native.connect(write=True) as db:
                db.execute('INSERT INTO projects(id,name,labels,created_at) VALUES(?,?,?,?)',
                    (project_id, 'native', dump([{'label_id': 'A'}, {'label_id': 'B'}]), now()))
                for index in range(count):
                    asset_path = root / ('original-' + str(index) + '.png')
                    asset_path.write_bytes(original_path.read_bytes())
                    db.execute('INSERT INTO assets(id,project_id,path,name,sha256,width,height,group_id,source_id,created_at) '
                        'VALUES(?,?,?,?,?,20,20,?,?,?)', ('nativeAsset' + str(index), project_id, str(asset_path),
                        'original.png', source_hash, 'groupA', 'sourceA', now()))
            assets = native.rows('SELECT * FROM assets ORDER BY id')
            job_id = native.submit(project_id, 'inference', {'model_id': 'modelA'}, assets)
            prediction = {'label_id': 'A', 'score': 0.9, 'status': 'needs_review'}
            with native.connect(write=True) as db:
                db.execute("UPDATE job_items SET state='succeeded',prediction=? WHERE job_id=?", (dump(prediction), job_id))
                db.execute("UPDATE jobs SET state='succeeded',total=?,done=? WHERE id=?", (count, count, job_id))
            native_items = native.rows('SELECT * FROM job_items ORDER BY id')
            raws = [{'native_item_id': row['id'], 'native_asset_id': row['asset_id'], 'prediction_label_id': 'A',
                     'disposition_revision': 0, 'state': 'succeeded'} for row in native_items]
        else:
            from ocr_workbench.store import Store
            from ocr_workbench.imaging import add_image
            native = Store(root / 'native')
            project_id = native.project('native')['id']
            image = add_image(native, project_id, 'original.png', original_path)
            job_id = native.enqueue(project_id, [image['active_version']], ['ppocr'])[0]
            assert native.claim()['id'] == job_id
            assert native.complete(job_id, {'text': 'original A', 'tables': [], 'blocks': [], 'engine': 'ppocr'})
            result_id = native.one('tasks', job_id)['result_id']
            raws = [{'native_result_id': result_id, 'native_task_id': job_id, 'native_asset_id': image['id'],
                     'native_version_id': image['active_version'], 'revision': 0, 'selected': True}]
            state.update(result_id=result_id, image_id=image['id'])

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                state['calls'].append((self.command, self.path))
                status = 200
                try:
                    if self.command == 'POST':
                        state['posts'] += 1
                        body = json.loads(raw)
                        if state['before_post']:
                            state['before_post'](state['posts'])
                        if body['instance_id'] != state['instance_id']:
                            raise ValueError('wrong current instance')
                        if scenario == 'classification':
                            value = native.dispose_results(job_id, body['item_ids'], body['state'], body['label_id'],
                                body['note'], body['expected_revisions'], operation_id=body['operation_id'],
                                request_id=body['request_id'], actor=body['actor'], instance_id=body['instance_id'], intent=body['intent'])
                        else:
                            value = native.adopt_result(parsed.path.split('/')[-1], body)
                        if state['drop']:
                            self.connection.shutdown(socket.SHUT_RDWR)
                            self.connection.close()
                            return
                    elif 'disposition-operations/' in parsed.path:
                        operation_id = parsed.path.split('/')[-1]
                        value = None if state['missing'] else native.disposition_operation(project_id, operation_id)
                        if query['instance_id'] != [state['instance_id']] or (value and query['operation_instance_id'] != [value['instance_id']]):
                            raise ValueError('wrong operation instance')
                        value = value or {'operation_id': operation_id, 'state': 'not_found'}
                    elif '/platform/adoptions/' in parsed.path:
                        operation_id = parsed.path.split('/')[-1]
                        if query['instance_id'] != [state['instance_id']]:
                            raise ValueError('wrong current instance')
                        receipt = None if state['missing'] else native.adoption_operation(operation_id, project_id, query['operation_instance_id'][0])
                        value = {'operation_id': operation_id, 'committed': receipt is not None, 'receipt': receipt}
                    elif scenario == 'classification' and parsed.path.endswith('/dispositions'):
                        item_id = parsed.path.split('/')[-2]
                        items = native.rows('SELECT * FROM result_disposition_events WHERE job_id=? AND item_id=? ORDER BY revision', (job_id, item_id))
                        for item in items:
                            item['actor'] = json.loads(item.pop('actor_json'))
                        value = {'items': items}
                    elif scenario == 'classification' and parsed.path.endswith('/results'):
                        rows = native.rows('SELECT j.*,a.label_id confirmed_label,d.label_id human_label_id,'
                            "coalesce(d.state,'unreviewed') disposition_state,coalesce(d.revision,0) disposition_revision,d.note disposition_note "
                            'FROM job_items j LEFT JOIN assets a ON a.id=j.asset_id LEFT JOIN result_dispositions d ON d.item_id=j.id '
                            'WHERE j.job_id=? ORDER BY j.id LIMIT ? OFFSET ?',
                            (job_id, int(query['limit'][0]), int(query['offset'][0])))
                        for row in rows:
                            row['prediction'] = json.loads(row['prediction'])
                        value = {'items': rows, 'total': count}
                    elif scenario == 'classification' and '/jobs/' in parsed.path:
                        value = native.job(job_id)
                    elif scenario == 'classification':
                        value = native.project(project_id)
                    elif '/api/projects/' in parsed.path:
                        value = native.project_snapshot(project_id)
                    elif parsed.path.endswith('/snapshot'):
                        value = native.result_snapshot(state['result_id'])
                    elif '/revisions/' in parsed.path:
                        value = native.result_snapshot(state['result_id'], int(parsed.path.split('/')[-1]))
                    elif parsed.path.endswith('/revisions'):
                        value = native.result_history(state['result_id'], int(query.get('offset', ['0'])[0]), int(query.get('limit', ['50'])[0]))
                    else:
                        raise LookupError(parsed.path)
                    if state['tamper'] and ('disposition-operations/' in parsed.path or '/platform/adoptions/' in parsed.path):
                        if value.get('receipt'):
                            value['receipt']['original_sha256'] = 'f' * 64
                        elif value.get('state') == 'committed':
                            value['items'][0]['original_sha256'] = 'f' * 64
                except (ValueError, KeyError, LookupError) as error:
                    status, value = 409, {'detail': str(error)}
                output = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(output)))
                self.end_headers()
                self.wfile.write(output)

            do_GET = do_POST = handle_request

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        active.append((server, thread))
        def context(*_):
            return ResolvedContext('workspaceA', scenario, state['instance_id'], 'linkA', project_id, 'owner', '/jobs',
                                   'http://127.0.0.1:' + str(server.server_port))
        def adapter(bound):
            return (ClassificationAdapter(bound, live=lambda current: current.instance_id == state['instance_id'])
                if scenario == 'classification' else OcrAdapter(bound, token=lambda: 'a' * 32,
                    live=lambda current: current.instance_id == state['instance_id']))
        jobs = SimpleNamespace(_context=context, _adapter=adapter, _items=JobService._items)
        service = BatchService.__new__(BatchService)
        service.app = SimpleNamespace(state=SimpleNamespace(job_service=jobs))
        service.native, service.lock = NativeBatchRunner(service.app), threading.RLock()
        service.workspace = lambda _: None
        service.store = BatchStore(root / 'platform-batches')
        saved = service.store.save_template({'schema_version': '1.0', 'workspace_id': 'workspaceA',
            'name': 'conditional native', 'scenario': scenario, 'config': {'link_id': 'linkA'}})
        batch, _ = service.store.accept({'schema_version': '1.0', 'workspace_id': 'workspaceA', 'request_key': 'nativeRun',
            'template_id': saved['template_id'], 'template_version': 1,
            'inputs': [{'role': 'images', 'asset_id': 'assetA', 'asset_revision': 1, 'blob_hash': source_hash}]}, saved)
        issues = [{'item_id': 'item' + str(index), 'category': 'review',
            'source': {'asset_id': 'assetA', 'blob_hash': source_hash}, 'raw_output': {**raw, 'ordinal': index}}
            for index, raw in enumerate(raws)]
        service.store.begin_index(batch['batch_id'], 'fixedNative', len(issues), len(issues), 0, 0)
        service.store.index_page(batch['batch_id'], 'fixedNative', 0, [{'ordinal': index} for index in range(len(issues))], issues)
        service.store.update(batch['batch_id'], status='succeeded', receipts={'native_submission': {
            'module_project_id': project_id, 'instance_id': 'instanceA', 'native_job_ids': [job_id]}})
        return SimpleNamespace(service=service, state=state, native=native, batch_id=batch['batch_id'], issues=issues,
            job_id=job_id, project_id=project_id, source_path=original_path, source_hash=source_hash)

    yield build
    for server, thread in active:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def disposition_request(fixture, request_id, action='confirm', revision=None, item_ids=None, adoption=None):
    value = {'request_id': request_id, 'expected_revision': revision or fixture.service.get(fixture.batch_id)['revision'],
        'actor': {'id': 'localReviewer', 'display_name': '本机复核人', 'source': 'local'},
        'item_ids': item_ids or ['item0'], 'action': action, 'note': 'checked fixed output'}
    if adoption:
        value['adoption'] = adoption
    return value


def test_classification_b_adoption_and_subsequent_cas_use_saved_receipts(disposition_bridge):
    from packages.contracts.automation import BatchHistoryPage, BatchNativeRevisionPage, ProcessingBatch
    from workbench_platform.data_api.service import DataError
    fixture = disposition_bridge()
    service, native, raw = fixture.service, fixture.native, fixture.issues[0]['raw_output']
    actor = disposition_request(fixture, 'manual')['actor']
    native.dispose_results(fixture.job_id, [raw['native_item_id']], 'confirmed', 'B', 'native manual B',
        {raw['native_item_id']: 0}, operation_id='nativeManualB', request_id='nativeManualB', actor=actor,
        instance_id='instanceA', intent='manual')
    with pytest.raises(DataError, match='人工类别'):
        service.dispositions(fixture.batch_id, disposition_request(fixture, 'oldA'))
    assert fixture.state['posts'] == 0 and service.store.history(fixture.batch_id)['total'] == 0
    versions = service.native_revisions(fixture.batch_id, 'item0')
    BatchNativeRevisionPage.model_validate(versions)
    assert versions['current_revision'] == 1 and versions['current']['human_label_id'] == 'B'
    payload = disposition_request(fixture, 'adoptB', 'adopt', adoption={'native_revision': 1, 'human_label_id': 'B'})
    adopted = service.dispositions(fixture.batch_id, payload)
    ProcessingBatch.model_validate(adopted)
    assert adopted['revision'] == 2 and adopted['summary']['review_complete'] is True
    assert adopted['issues'][0]['raw_output']['prediction_label_id'] == 'A'
    assert adopted['issues'][0]['adoption']['human_label_id'] == 'B'
    assert adopted['issues'][0]['adoption']['committed_native_revision'] == 2
    assert service.dispositions(fixture.batch_id, payload) == adopted and fixture.state['posts'] == 1
    with pytest.raises(DataError, match='版本冲突'):
        service.dispositions(fixture.batch_id, disposition_request(fixture, 'otherWindow', 'defer', revision=1))
    with pytest.raises(DataError, match='不同'):
        service.dispositions(fixture.batch_id, {**payload, 'note': 'different'})
    deferred = service.dispositions(fixture.batch_id, disposition_request(fixture, 'deferB', 'defer'))
    assert deferred['revision'] == 3 and deferred['coverage']['deferred'] == 1
    reopened = service.dispositions(fixture.batch_id, disposition_request(fixture, 'reopenB', 'reopen'))
    assert reopened['revision'] == 4 and reopened['coverage']['pending'] == 1
    assert native.one('SELECT revision FROM result_dispositions')['revision'] == 4
    with pytest.raises(DataError, match='人工类别'):
        service.dispositions(fixture.batch_id, disposition_request(fixture, 'staleAafterB'))
    history = service.disposition_history(fixture.batch_id)
    BatchHistoryPage.model_validate(history)
    assert [item['action'] for item in history['items']] == ['adopt', 'defer', 'reopen']
    assert history['items'][0]['actor'] == actor


def test_ocr_adoption_pins_revision_two_and_platform_reopen_leaves_native_selection(disposition_bridge):
    from packages.contracts.automation import BatchNativeRevisionPage, BatchHistoryPage, ProcessingBatch
    from workbench_platform.data_api.service import DataError
    fixture = disposition_bridge('ocr')
    service, native, result_id = fixture.service, fixture.native, fixture.state['result_id']
    native.save(result_id, {'text': 'revision 1', 'tables': []}, 0)
    native.save(result_id, {'text': 'revision 2', 'tables': []}, 1)
    with pytest.raises(DataError, match='OCR 来源'):
        service.dispositions(fixture.batch_id, disposition_request(fixture, 'staleRevision0'))
    versions = service.native_revisions(fixture.batch_id, 'item0')
    BatchNativeRevisionPage.model_validate(versions)
    assert versions['current_revision'] == 2 and versions['native_review_qualification'] == 'independent'
    payload = disposition_request(fixture, 'adoptRevision2', 'adopt', adoption={
        'native_revision': 2, 'native_result_id': result_id, 'native_version_id': fixture.issues[0]['raw_output']['native_version_id']})
    adopted = service.dispositions(fixture.batch_id, payload)
    ProcessingBatch.model_validate(adopted)
    item = adopted['issues'][0]
    assert item['raw_output']['revision'] == 0 and item['adoption']['snapshot']['edited']['text'] == 'revision 2'
    assert item['adoption']['native_revision'] == 2 and item['adoption']['native_review_qualification'] == 'independent'
    assert adopted['summary']['review_complete'] is True and adopted['summary']['review_scope'] == 'platform_fixed_result'
    native.save(result_id, {'text': 'revision 3 later edit', 'tables': []}, 2)
    selection = native.rows('SELECT * FROM selections')
    reviews = native.rows('SELECT * FROM reviews')
    calls_before = list(fixture.state['calls'])
    reopened = service.dispositions(fixture.batch_id, disposition_request(fixture, 'reopenRevision2', 'reopen'))
    deferred = service.dispositions(fixture.batch_id, disposition_request(fixture, 'deferRevision2', 'defer'))
    assert fixture.state['calls'] == calls_before and fixture.state['posts'] == 1
    assert native.rows('SELECT * FROM selections') == selection and native.rows('SELECT * FROM reviews') == reviews
    assert reopened['summary']['review_complete'] is False and deferred['coverage']['deferred'] == 1
    assert deferred['issues'][0]['adoption']['snapshot']['edited']['text'] == 'revision 2'
    assert native.result_snapshot(result_id, 2)['edited']['text'] == 'revision 2'
    assert native.result_snapshot(result_id)['revision'] == 3
    with pytest.raises(DataError, match='采用关系'):
        service.dispositions(fixture.batch_id, disposition_request(fixture, 'confirmOldAgain'))
    BatchHistoryPage.model_validate(service.disposition_history(fixture.batch_id))


def test_ocr_original_confirmation_does_not_claim_native_review_qualification(disposition_bridge):
    fixture = disposition_bridge('ocr')
    before = fixture.native.rows('SELECT * FROM reviews')
    confirmed = fixture.service.dispositions(fixture.batch_id, disposition_request(fixture, 'acceptOriginalOCR'))
    assert confirmed['summary']['review_complete'] is True
    assert confirmed['summary']['native_review_qualification'] == 'independent'
    assert fixture.native.rows('SELECT * FROM reviews') == before and fixture.state['posts'] == 1


@pytest.mark.parametrize('scenario', ('classification', 'ocr'))
@pytest.mark.parametrize('interrupt_at', ('lost_reply', 'platform_commit'))
def test_native_effect_recovers_after_restart_without_second_post(disposition_bridge, monkeypatch, scenario, interrupt_at):
    from workbench_platform.batches.store import BatchStore
    from workbench_platform.data_api.service import DataError
    fixture = disposition_bridge(scenario)
    service = fixture.service
    payload = disposition_request(fixture, 'recoverOriginal')
    if interrupt_at == 'lost_reply':
        fixture.state['drop'] = True
    else:
        original_commit = service.store.commit_operation
        def fail_commit(*_, **__):
            raise OSError('platform commit interrupted after native receipt')
        monkeypatch.setattr(service.store, 'commit_operation', fail_commit)
    with pytest.raises(DataError, match='核实'):
        service.dispositions(fixture.batch_id, payload)
    pending = service.get(fixture.batch_id)['summary']['pending_operation']
    assert pending['state'] == 'needs_verification' and service.store.history(fixture.batch_id)['total'] == 0
    assert fixture.state['posts'] == 1
    service.store = BatchStore(service.store.path.parent)
    fixture.state.update(drop=False, instance_id='instanceB')
    recovered = service.recover_operation(fixture.batch_id, pending['operation_id'])
    assert recovered['revision'] == 2 and recovered['summary']['review_complete'] is True
    assert service.dispositions(fixture.batch_id, payload) == recovered and fixture.state['posts'] == 1
    assert service.store.history(fixture.batch_id)['total'] == 1
    operation = service.store.operation(pending['operation_id'], private=True)
    assert operation['intent']['origin']['instance_id'] == 'instanceA'
    assert all(receipt['instance_id'] == 'instanceA' for receipt in operation['native_receipt']['segments'])
    assert any('instance_id=instanceB&operation_instance_id=instanceA' in path for method, path in fixture.state['calls'] if method == 'GET')


@pytest.mark.parametrize('scenario', ('classification', 'ocr'))
def test_unknown_or_wrong_source_receipt_cannot_change_platform_decision(disposition_bridge, scenario):
    from workbench_platform.data_api.service import DataError
    fixture = disposition_bridge(scenario)
    fixture.state['drop'] = True
    with pytest.raises(DataError):
        fixture.service.dispositions(fixture.batch_id, disposition_request(fixture, 'unknownSource'))
    pending = fixture.service.get(fixture.batch_id)['summary']['pending_operation']
    fixture.state.update(drop=False, missing=True, instance_id='instanceB')
    with pytest.raises(DataError, match='不可核实'):
        fixture.service.recover_operation(fixture.batch_id, pending['operation_id'])
    with pytest.raises(DataError, match='待核实'):
        fixture.service.dispositions(fixture.batch_id, disposition_request(fixture, 'blindNewId'))
    fixture.state.update(missing=False, tamper=True)
    with pytest.raises(DataError):
        fixture.service.recover_operation(fixture.batch_id, pending['operation_id'])
    assert fixture.state['posts'] == 1 and fixture.service.store.history(fixture.batch_id)['total'] == 0
    current = fixture.service.get(fixture.batch_id)
    assert current['revision'] == 1 and current['coverage']['pending'] == 1 and current['summary']['review_complete'] is False


@pytest.mark.parametrize('scenario', ('classification', 'ocr'))
def test_definite_first_post_conflict_preserves_request_without_event_and_allows_explicit_adoption(disposition_bridge, scenario):
    from workbench_platform.data_api.service import DataError
    fixture = disposition_bridge(scenario)
    payload = disposition_request(fixture, 'raceOldVersion')
    def change_native_before_post(count):
        if count != 1:
            return
        if scenario == 'classification':
            native_id = fixture.issues[0]['raw_output']['native_item_id']
            fixture.native.dispose_results(fixture.job_id, [native_id], 'confirmed', 'B', '', {native_id: 0},
                operation_id='manualRaceB', request_id='manualRaceB', actor=payload['actor'], instance_id='instanceA', intent='manual')
        else:
            fixture.native.save(fixture.state['result_id'], {'text': 'manual revision 1', 'tables': []}, 0)
    fixture.state['before_post'] = change_native_before_post
    with pytest.raises(DataError, match='明确拒绝'):
        fixture.service.dispositions(fixture.batch_id, payload)
    from packages.contracts.automation import BatchDisposition
    normalized = BatchDisposition.model_validate(payload).model_dump(mode='json')
    operation = fixture.service.store.request_operation(fixture.batch_id, normalized)
    assert operation['state'] == 'committed' and operation['receipt']['outcome'] == 'conflict'
    assert operation['receipt']['evidence']['status_code'] == 409
    assert fixture.service.get(fixture.batch_id)['summary']['pending_operations'] == 0
    assert fixture.service.store.history(fixture.batch_id)['total'] == 0
    with pytest.raises(DataError, match='版本冲突'):
        fixture.service.dispositions(fixture.batch_id, payload)
    assert fixture.state['posts'] == 1
    adoption = {'native_revision': 1}
    if scenario == 'classification':
        adoption['human_label_id'] = 'B'
    new = fixture.service.dispositions(fixture.batch_id, disposition_request(fixture, 'explicitAfterConflict', 'adopt', adoption=adoption))
    assert new['revision'] == 2 and fixture.state['posts'] == 2
    assert fixture.service.store.history(fixture.batch_id)['total'] == 1


def test_partial_segment_conflict_remains_reserved_and_never_reposts_started_segments(disposition_bridge):
    from workbench_platform.data_api.service import DataError
    fixture = disposition_bridge(count=501)
    payload = disposition_request(fixture, 'segmentedRange', item_ids=[item['item_id'] for item in fixture.issues])
    last_native_id = fixture.issues[-1]['raw_output']['native_item_id']
    def manual_before_second_post(count):
        if count == 2:
            fixture.native.dispose_results(fixture.job_id, [last_native_id], 'confirmed', 'B', '', {last_native_id: 0},
                operation_id='secondSegmentManualB', request_id='secondSegmentManualB', actor=payload['actor'],
                instance_id='instanceA', intent='manual')
    fixture.state['before_post'] = manual_before_second_post
    with pytest.raises(DataError, match='明确拒绝'):
        fixture.service.dispositions(fixture.batch_id, payload)
    pending = fixture.service.get(fixture.batch_id)['summary']['pending_operation']
    assert pending['state'] == 'needs_verification'
    operation = fixture.service.store.operation(pending['operation_id'], private=True)
    assert len(operation['intent']['segments']) == 2 and operation['intent']['segments'][0]['native_receipt']['state'] == 'committed'
    assert operation['intent']['segments'][1]['native_started'] is True
    assert fixture.service.store.history(fixture.batch_id)['total'] == 0
    with pytest.raises(DataError, match='不可核实'):
        fixture.service.recover_operation(fixture.batch_id, pending['operation_id'])
    assert fixture.state['posts'] == 2 and fixture.service.get(fixture.batch_id)['revision'] == 1
    with pytest.raises(DataError, match='待核实'):
        fixture.service.dispositions(fixture.batch_id, disposition_request(fixture, 'partialBlindRetry', 'defer'))


def test_disposition_routes_validate_models_scope_and_recover_original_payload(disposition_bridge):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from packages.contracts.automation import BatchHistoryPage, BatchNativeRevisionPage, BatchOperation, ProcessingBatch
    from workbench_platform.batches.routes import router
    from workbench_platform.data_api.service import DataError
    fixture = disposition_bridge('ocr')
    api = FastAPI()
    api.include_router(router)
    api.state.batch_service = fixture.service
    @api.exception_handler(DataError)
    async def error_handler(_, error):
        return JSONResponse({'detail': str(error)}, status_code=error.status)
    base = '/api/platform/v1/batches/' + fixture.batch_id
    local = {'host': '127.0.0.1:8766', 'origin': 'http://127.0.0.1:8766'}
    with TestClient(api, base_url='http://127.0.0.1:8766') as client:
        revisions = client.get(base + '/items/item0/native-revisions')
        assert revisions.status_code == 200
        BatchNativeRevisionPage.model_validate(revisions.json())
        fixture.state['drop'] = True
        response = client.post(base + '/dispositions', headers=local, json=disposition_request(fixture, 'httpRecover'))
        assert response.status_code == 409
        pending = client.get(base).json()['summary']['pending_operation']['operation_id']
        path = base + '/operations/' + pending
        BatchOperation.model_validate(client.get(path).json())
        assert client.post(path + '/recover', headers=local, json={'expected_revision': 99}).status_code == 400
        assert client.post(path + '/recover', headers={**local, 'origin': 'https://outside.invalid'}, json={}).status_code == 403
        fixture.state.update(drop=False, instance_id='instanceB')
        recovered = client.post(path + '/recover', headers=local, json={})
        assert recovered.status_code == 200, recovered.text
        ProcessingBatch.model_validate(recovered.json())
        BatchOperation.model_validate(client.get(path).json())
        history = client.get(base + '/history?item_id=item0&limit=1')
        BatchHistoryPage.model_validate(history.json())
        assert history.json()['total'] == 1 and history.json()['revision'] == 2
        assert client.get(base.replace(fixture.batch_id, 'otherBatch') + '/operations/' + pending).status_code == 404
        assert fixture.state['posts'] == 1


def test_recovery_worker_with_stale_intent_observes_other_workers_no_effect_conflict(disposition_bridge, monkeypatch):
    from workbench_platform.data_api.service import DataError
    fixture = disposition_bridge()
    store = fixture.service.store
    payload = disposition_request(fixture, 'workerRace')
    snapshots = []
    start = store.start_segment
    def remember_before_dispatch(operation_id, segment_id):
        snapshots.append(store.operation(operation_id, private=True))
        return start(operation_id, segment_id)
    monkeypatch.setattr(store, 'start_segment', remember_before_dispatch)
    def manual_b(_):
        native_id = fixture.issues[0]['raw_output']['native_item_id']
        fixture.native.dispose_results(fixture.job_id, [native_id], 'confirmed', 'B', '', {native_id: 0},
            operation_id='otherWorkerManualB', request_id='otherWorkerManualB', actor=payload['actor'],
            instance_id='instanceA', intent='manual')
    fixture.state['before_post'] = manual_b
    with pytest.raises(DataError, match='明确拒绝'):
        fixture.service.dispositions(fixture.batch_id, payload)
    assert len(snapshots) == 1 and snapshots[0]['state'] == 'intent'
    terminal = store.operation(snapshots[0]['operation_id'])
    assert terminal['state'] == 'committed' and terminal['receipt']['outcome'] == 'conflict'
    # A second worker was holding this intent when the first completed the
    # no-effect rejection. Its loop must report the same conflict, not a receipt
    # interpreted as a successful batch result by its caller.
    with pytest.raises(DataError, match='版本冲突') as error:
        fixture.service.native.finish(store.get(fixture.batch_id), snapshots[0], store)
    assert error.value.status == 409 and fixture.state['posts'] == 1
    assert store.history(fixture.batch_id)['total'] == 0 and store.get(fixture.batch_id)['revision'] == 1


def test_successful_native_checkpoint_indexes_all_fixed_items_without_auto_accepting_b(tmp_path):
    import threading
    from workbench_platform.batches.service import BatchService
    from workbench_platform.batches.store import BatchStore
    runner, batch, _, _, snapshot, _ = fixture(tmp_path)
    snapshot['items'][0].update(disposition_state='confirmed', human_label_id='B', disposition_revision=1)
    runner.app.state.artifact_center.classification.publish = lambda *_, **__: {'bundle_id': 'bundleA'}
    service = BatchService.__new__(BatchService)
    service.store = BatchStore(tmp_path / 'batches')
    saved = service.store.save_template({'schema_version': '1.0', 'workspace_id': 'workspaceA',
        'name': 'fixed all outputs', 'scenario': 'classification', 'config': {'link_id': 'linkA'}})
    accepted, _ = service.store.accept({'schema_version': '1.0', 'workspace_id': 'workspaceA', 'request_key': 'nativeAll',
        'template_id': saved['template_id'], 'template_version': 1,
        'inputs': [{'role': 'images', 'asset_id': 'assetA', 'asset_revision': 1, 'blob_hash': 'a' * 64}]}, saved)
    service.store.update(accepted['batch_id'], receipts=batch['receipts'])
    service.native, service.closing, service.lock = runner, threading.Event(), threading.RLock()
    service.running = {accepted['batch_id']}
    service.workspace = lambda _: None
    service._run(accepted['batch_id'])
    indexed = service.get(accepted['batch_id'])
    assert indexed['status'] == 'succeeded' and indexed['coverage']['index_complete'] is True
    assert indexed['coverage']['result_rows'] == indexed['coverage']['required_items'] == indexed['coverage']['indexed_items'] == 1
    assert indexed['coverage']['pending'] == 1 and indexed['summary']['review_complete'] is False
    assert indexed['issues'][0]['raw_output']['human_label_id'] == 'B'
    assert indexed['issues'][0]['disposition'] == 'pending'


def fixture(tmp_path, *, scenario="classification"):
    trace = []
    job = {"platform_job_id": "jobA", "native_job_ids": ["nativeA"], "instance_id": "instanceA",
           "module_project_id": "projectA", "status": "succeeded",
           "availability": "ready", "capabilities": [], "counts": {"total": 1, "succeeded": 1,
           "failed": 0, "pending": 0, "cancelled": 0}}
    snapshot = {"workspace_id": "workspaceA", "handoff_id": "handoffA", "items": [
        {"native_item_id": "itemA", "asset_id": "assetA", "state": "succeeded",
         "prediction_label_id": "a", "prediction_status": "needs_review", "prediction_score": 0.7}],
        "inputs": [{"asset_id": "assetA", "name": "source.png"}]}
    def result_snapshot(*_):
        trace.append("snapshot")
        return snapshot
    def submit(*args, **kwargs):
        trace.append(("submit", kwargs))
        return {"not_submitted": True} if not kwargs.get("allow_new", True) else {"job": job}
    handoff = SimpleNamespace(result_snapshot=result_snapshot, submit=submit)
    def publish(*_, snapshot):
        trace.append(("publish", snapshot["items"][0]["prediction_label_id"]))
        if len([event for event in trace if isinstance(event, tuple) and event[0] == "publish"]) == 1:
            raise OSError("collection unavailable")
        return {"bundle_id": "bundleA"}
    artifacts = SimpleNamespace(publish=publish, get=lambda *_: {"bundle_id": "bundleA"})
    jobs = SimpleNamespace(ledger=SimpleNamespace(get=lambda *_: job), refresh=lambda *_: job,
        _context=lambda *_: SimpleNamespace(instance_id="instanceA", module_project_id="projectA", base_url="http://127.0.0.1:1/"))
    blob_root = tmp_path / "blobs"
    blob_root.mkdir()
    state = SimpleNamespace(job_service=jobs, classification_handoff=handoff, ocr_handoff=handoff,
        artifact_center=SimpleNamespace(store=SimpleNamespace(blob_root=blob_root), classification=artifacts, ocr=artifacts))
    batch = {"batch_id": "batchA", "workspace_id": "workspaceA", "template": {
        "scenario": scenario, "config": {"link_id": "linkA", "model_id": "modelA", "device": "cpu", "engines": ["engineA"]}},
        "inputs": [], "receipts": {"native_inputs": {"asset_set_id": "setA", "revision": 1},
        "native_import": {"handoff_id": "handoffA", "state": "completed"}, "native_submission": job}}
    def checkpoint(stage, receipt):
        batch["receipts"][stage] = receipt
        trace.append(stage)
    return NativeBatchRunner(SimpleNamespace(state=state)), batch, checkpoint, job, snapshot, trace


def test_collection_retry_uses_pinned_output_without_recomputing(tmp_path):
    runner, batch, checkpoint, job, snapshot, trace = fixture(tmp_path)
    first = runner.run(batch, checkpoint, lambda: False)
    assert first["artifact_state"] == "collection_failed" and first["status"] == "partial"
    snapshot["items"][0]["prediction_label_id"] = "changed-after-pin"
    second = runner.run(batch, checkpoint, lambda: False)
    assert second["artifact_state"] == "collected"
    assert second["issues"][0]["raw_output"]["prediction_label_id"] == "a"
    assert trace.count("snapshot") == 1
    assert [event for event in trace if isinstance(event, tuple) and event[0] == "publish"] == [("publish", "a"), ("publish", "a")]
    runner.run(batch, checkpoint, lambda: False)
    assert len([event for event in trace if isinstance(event, tuple) and event[0] == "publish"]) == 2


def test_lost_submit_response_cancel_queries_before_any_new_submission(tmp_path):
    runner, batch, checkpoint, _, _, trace = fixture(tmp_path)
    del batch["receipts"]["native_submission"]
    batch["receipts"]["native_dispatch"] = {"status": "queued"}
    result = runner.run(batch, checkpoint, lambda: True)
    assert result["status"] == "cancelled"
    assert ("submit", {"allow_new": False}) in trace
    assert "snapshot" not in trace


def test_cancel_waits_for_true_terminal_and_never_collects(tmp_path, monkeypatch):
    runner, batch, checkpoint, job, _, trace = fixture(tmp_path)
    polls = iter(("running", "cancelling", "cancelled"))
    def refresh(*_):
        return {**job, "status": next(polls), "capabilities": ["cancel"]}
    runner.jobs.refresh = refresh
    actions = []
    def action(*args):
        actions.append(args)
        return {"status": "cancelling"}
    runner.jobs.action = action
    monkeypatch.setattr("workbench_platform.batches.native.time.sleep", lambda _: None)
    def cancelled():
        return True
    cancelled.stopping = lambda: False
    result = runner.run(batch, checkpoint, cancelled)
    assert result["status"] == "cancelled"
    assert trace.count("native_progress") == 3
    assert trace.count("native_cancel") == 1
    assert actions == [("workspaceA", "jobA", "cancel")]
    assert "snapshot" not in trace


def test_shutdown_preserves_native_task_for_service_recovery(tmp_path):
    runner, batch, checkpoint, _, _, trace = fixture(tmp_path)
    def cancelled():
        return True
    cancelled.stopping = lambda: True
    with pytest.raises(NativeBatchError) as error:
        runner.run(batch, checkpoint, cancelled)
    assert error.value.status == "queued"
    assert not trace


@pytest.mark.parametrize("shutdown_at", ("refresh", "checkpoint", "cancel_callback"))
def test_shutdown_during_native_poll_never_cancels_and_resumes_same_receipt(tmp_path, shutdown_at):
    runner, batch, checkpoint, job, _, trace = fixture(tmp_path)
    closing = False
    polls = 0
    submitted = batch["receipts"]["native_submission"]
    actions = []

    def refresh(*_):
        nonlocal closing, polls
        polls += 1
        if shutdown_at == "refresh":
            closing = True
        return {**job, "status": "running", "capabilities": ["cancel"]}

    def durable_checkpoint(stage, receipt):
        nonlocal closing
        checkpoint(stage, receipt)
        if stage == "native_progress" and shutdown_at == "checkpoint":
            closing = True

    def cancelled():
        nonlocal closing
        # BatchService's existing callback combines closing and explicit cancel.
        if polls and shutdown_at == "cancel_callback":
            closing = True
        return closing

    cancelled.stopping = lambda: closing
    runner.jobs.refresh = refresh
    runner.jobs.action = lambda *args: actions.append(args)
    with pytest.raises(NativeBatchError) as error:
        runner.run(batch, durable_checkpoint, cancelled)
    assert error.value.status == "queued"
    assert not actions and "native_cancel" not in batch["receipts"]
    assert batch["receipts"]["native_submission"] is submitted
    assert batch["receipts"]["native_progress"]["status"] == "running"
    assert "snapshot" not in trace

    closing = False
    runner.jobs.refresh = lambda *_: job
    runner.app.state.artifact_center.classification.publish = lambda *_, **__: {"bundle_id": "bundleA"}
    recovered = runner.run(batch, checkpoint, lambda: False)
    assert recovered["status"] == "succeeded" and recovered["artifact_state"] == "collected"
    assert batch["receipts"]["native_submission"] is submitted
    assert not actions
    assert not any(isinstance(event, tuple) and event[0] == "submit" for event in trace)


@pytest.mark.parametrize("native_state", ("cancelled", "failed"))
def test_unfinished_classification_items_are_execution_issues_not_model_suggestions(tmp_path, native_state):
    runner, batch, checkpoint, job, snapshot, _ = fixture(tmp_path)
    job.update(status=native_state, counts={"total": 1, "succeeded": 0, "pending": 0,
               "failed": int(native_state == "failed"), "cancelled": int(native_state == "cancelled")})
    snapshot["items"][0].update(state=native_state, prediction_label_id=None,
                               prediction_status=None, prediction_score=None)
    runner.app.state.artifact_center.classification.publish = lambda *_, **__: {"bundle_id": "bundleA"}
    result = runner.run(batch, checkpoint, lambda: False)
    assert result["status"] == native_state
    assert result["summary"]["execution_status"] == native_state
    assert result["summary"]["needs_review"] == 0
    counts = result["summary"]
    assert counts["total"] == sum(counts[key] for key in ("succeeded", "failed", "cancelled", "pending"))
    assert len(result["issues"]) == 1 and result["issues"][0]["category"] == "execution"
    assert result["issues"][0]["raw_output"]["state"] == native_state
    assert result["issues"][0]["source"]["name"] == "source.png"


def test_review_only_preflight_reports_real_capability(tmp_path):
    runner, _, _, _, _, _ = fixture(tmp_path, scenario="ocr")
    runner.app.state.asset_library = (SimpleNamespace(get=lambda *_: SimpleNamespace(
        asset_id="assetA", original_name="page.png", revision=1, blob_hash="a" * 64)),)
    runner.options = lambda *_: {"recognition": False, "reason": "review_only", "engines": []}
    issues = runner.preflight({"workspace_id": "workspaceA", "scenario": "ocr", "config": {
        "link_id": "linkA", "engines": ["engineA"]}}, [{"asset_id": "assetA", "asset_revision": 1, "blob_hash": "a" * 64}])
    assert any(item["category"] == "capability" and "仅校对" in item["detail"] for item in issues)
    assert all(isinstance(item["source"], dict) and isinstance(item["raw_output"], dict) for item in issues)


def test_lost_input_set_receipt_reuses_fixed_source_identity(tmp_path):
    from workbench_platform.assets.registry import AssetRegistry
    from workbench_platform.assets.sets import AssetSetConflict, AssetSetStore
    inputs, blobs = tmp_path / "inputs", tmp_path / "blobs"
    inputs.mkdir()
    blobs.mkdir()
    (inputs / "one.png").write_bytes(b"source-one")
    (inputs / "two.png").write_bytes(b"source-two")
    registry = AssetRegistry(tmp_path / "assets.sqlite3", blobs, {"inputs": inputs})
    first = registry.register("workspaceA", "inputs", "one.png", media_type="image/png", storage_mode="managed")
    second = registry.register("workspaceA", "inputs", "two.png", media_type="image/png", storage_mode="managed")
    sets = AssetSetStore(registry)
    def member(asset):
        identity = {"asset_id": asset.asset_id, "asset_revision": asset.revision, "blob_hash": asset.blob_hash}
        return {"schema_version": "1.0", "item_id": "itemA", **identity, "source_locator": {
            "schema_version": "1.0", "kind": "file", "precision": "unknown", **identity}}
    original = sets.create("workspaceA", [member(first)], asset_set_id="setBatchA")
    assert sets.create("workspaceA", [member(first)], asset_set_id="setBatchA") == original
    with pytest.raises(AssetSetConflict):
        sets.create("workspaceA", [member(second)], asset_set_id="setBatchA")


@pytest.fixture
def preparation_bridge(tmp_path, monkeypatch):
    """Real launch-ID SQLite and native read protocols; no model/process startup."""
    import json
    import threading
    from concurrent.futures import Future
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from packages.contracts.models import LaunchReceipt
    from workbench_platform.catalog.launch import LaunchService, LaunchStatus
    from workbench_platform.catalog.recent import RecentWorkStore
    from workbench_platform.jobs.projection import JobProjectionStore
    from workbench_platform.jobs.routes import JobService

    observations = {"calls": [], "starts": [], "review_only": False, "gpu": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observations["calls"].append(self.path)
            if self.path == "/api/v1/runtime":
                value = {"gpu": observations["gpu"]}
            elif self.path == "/api/health":
                value = {"status": "ready", "review_only": observations["review_only"], "queue": {"healthy": True}}
            elif self.path == "/api/state":
                value = {"engines": {"ppocr": {"name": "PPOCR"}}}
            elif self.path.endswith("/models"):
                value = [{"id": "modelA", "project_id": "projectA", "name": "fixed model"}]
            elif "/jobs/" in self.path:
                if "/results" in self.path:
                    value = {"items": [{"id": "itemA", "job_id": "nativeA", "asset_id": "nativeAssetA", "state": "succeeded",
                        "prediction": {"label_id": "A", "score": 0.9, "status": "needs_review"}}]}
                else:
                    value = {"id": "nativeA", "project_id": "projectA", "kind": "inference", "state": "succeeded",
                        "stage": "completed", "total": 1, "done": 1, "failed": 0}
            else:
                value = {"id": "projectA", "labels": [{"label_id": "A"}]}
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    install = SimpleNamespace(installed=True, working_directory=tmp_path / "native",
        bundle_manifest_sha256="a" * 64, executable_sha256="b" * 64)
    snapshot = SimpleNamespace(revision=1, installations={"classification": install, "ocr": install}, verifying=frozenset())
    registry = SimpleNamespace(_snapshot=snapshot, manifests={"classification": {}, "ocr": {}}, verification=lambda _: None)
    supervisor = SimpleNamespace(registry=registry, _lock=threading.RLock(), _entries={}, _current={}, _closed=False)

    def start(app_id, launch_id):
        observations["starts"].append((app_id, launch_id))
        receipt = LaunchReceipt(schema_version="1.0", app_id=app_id, launch_id=launch_id,
            instance_id=observations.get("instance_id", "instanceA"),
            pid=1, creation_time="2026-10-03T00:00:00Z", base_url=f"http://127.0.0.1:{server.server_port}/",
            handshake_nonce="nonceA", ownership="owned", version="0.2.0")
        future = Future()
        future.set_result(receipt)
        supervisor._entries[launch_id] = SimpleNamespace(identity=("owned", app_id, 1), future=future)
        supervisor._current[app_id] = (1, receipt.instance_id)
        return receipt

    supervisor.start = start
    launches = LaunchService(supervisor, RecentWorkStore(tmp_path / "launches.sqlite3"))

    def verify(entry, receipt):
        if supervisor._current.get(receipt.app_id) != (1, receipt.instance_id):
            raise LaunchStatus(410, "APP_UNAVAILABLE")
        return receipt

    monkeypatch.setattr(launches, "_verify", verify)
    links = [SimpleNamespace(app_id=scenario, link_id="linkA", module_project_id="projectA", role="owner")
             for scenario in ("classification", "ocr")]
    workspaces = SimpleNamespace(list_links=lambda _: links, get_workspace=lambda _: {})
    token = tmp_path / "ocr.token"
    token.write_text("a" * 32)
    jobs = JobService(JobProjectionStore(tmp_path / "jobs.sqlite3"), workspaces, launches, ocr_token_file=str(token))
    asset = SimpleNamespace(asset_id="assetA", original_name="page.png", revision=1, blob_hash="c" * 64)
    sets = SimpleNamespace(create=lambda *_, **__: SimpleNamespace(asset_set_id="setA", revision=1, manifest_hash="d" * 64))
    state = SimpleNamespace(job_service=jobs, workspace_store=workspaces, launch_service=launches,
        asset_library=(SimpleNamespace(get=lambda *_: asset), sets))
    runner = NativeBatchRunner(SimpleNamespace(state=state))
    batch = {"batch_id": "batchPrepare", "workspace_id": "workspaceA", "template": {"scenario": "classification",
        "config": {"link_id": "linkA", "model_id": "modelA", "device": "cpu"}}, "receipts": {},
        "inputs": [{"role": "images", "asset_id": "assetA", "asset_revision": 1, "blob_hash": asset.blob_hash}]}
    checkpoints = []

    def checkpoint(stage, receipt):
        batch["receipts"][stage] = receipt
        checkpoints.append((stage, receipt))

    yield SimpleNamespace(runner=runner, batch=batch, checkpoint=checkpoint, checkpoints=checkpoints,
        observations=observations, launches=launches, supervisor=supervisor, registry=registry, asset=asset, tmp_path=tmp_path)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_d15_cold_cpu_worker_prepares_once_and_restart_keeps_original_task(preparation_bridge):
    import threading
    from workbench_platform.batches.service import BatchService
    from workbench_platform.batches.store import BatchStore
    fixture = preparation_bridge
    runner, state = fixture.runner, fixture.runner.app.state
    native_writes = []

    def begin(*_):
        native_writes.append("import")
        return {"handoff_id": "handoffA", "state": "completed"}

    def submit(*_, **__):
        native_writes.append("submit")
        return {"job": runner.jobs.register("workspaceA", "linkA", "classification", ["nativeA"])}

    state.classification_handoff = SimpleNamespace(begin=begin, submit=submit, result_snapshot=lambda *_: {
        "items": [{"native_item_id": "itemA", "asset_id": "assetA", "state": "succeeded",
                   "prediction_label_id": "A"}], "inputs": [{"asset_id": "assetA", "name": "page.png"}]})
    blobs = fixture.tmp_path / "blobs"
    blobs.mkdir()
    state.artifact_center = SimpleNamespace(store=SimpleNamespace(blob_root=blobs),
        classification=SimpleNamespace(publish=lambda *_, **__: {"bundle_id": "bundleA"},
                                       get=lambda *_: {"bundle_id": "bundleA"}))
    service = BatchService.__new__(BatchService)
    service.store = BatchStore(fixture.tmp_path / "batches")
    template = service.store.save_template({"schema_version": "1.0", "workspace_id": "workspaceA", "name": "cold CPU",
        **fixture.batch["template"]})
    batch, _ = service.store.accept({"schema_version": "1.0", "workspace_id": "workspaceA", "request_key": "coldCpu",
        "template_id": template["template_id"], "template_version": 1, "inputs": fixture.batch["inputs"]}, template)
    service.native, service.closing, service.lock = runner, threading.Event(), threading.RLock()
    service.running = {batch["batch_id"]}
    service._run(batch["batch_id"])
    completed = service.store.get(batch["batch_id"])
    assert completed["status"] == "succeeded"
    assert len(fixture.observations["starts"]) == 1 and native_writes == ["import", "submit"]
    assert "/api/v1/runtime" not in fixture.observations["calls"]
    assert completed["receipts"]["native_import"]["instance_id"] == "instanceA"
    submitted = completed["receipts"]["native_submission"]
    service.store = BatchStore(fixture.tmp_path / "batches")
    service.running.add(batch["batch_id"])
    service._run(batch["batch_id"])
    recovered = service.store.get(batch["batch_id"])
    assert recovered["status"] == "succeeded" and recovered["receipts"]["native_submission"] == submitted
    assert len(fixture.observations["starts"]) == 1 and native_writes == ["import", "submit"]


def test_d15_lost_launch_reply_is_queried_by_original_durable_id(preparation_bridge, monkeypatch):
    fixture = preparation_bridge
    start = fixture.launches.start

    def lost(*args):
        start(*args)
        raise TimeoutError("lost receipt")

    monkeypatch.setattr(fixture.launches, "start", lost)
    assert fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: False) is None
    original = fixture.batch["receipts"]["native_prepare"]
    assert original["phase"] == "ready" and original["start_attempted"] is True
    assert fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: False) is None
    assert fixture.observations["starts"] == [("classification", original["launch_id"])]
    assert [receipt for stage, receipt in fixture.checkpoints if stage == "native_prepare" and receipt["phase"] == "launching"]


def test_d15_retired_launch_attempt_never_creates_replacement(preparation_bridge):
    fixture = preparation_bridge
    fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: False)
    launch_id = fixture.batch["receipts"]["native_prepare"]["launch_id"]
    fixture.supervisor._entries.clear()
    fixture.supervisor._current.clear()
    result = fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: False)
    assert result["status"] == "needs_verification"
    assert fixture.batch["receipts"]["native_prepare"]["launch_id"] == launch_id
    assert len(fixture.observations["starts"]) == 1
    assert result["issues"][0]["raw_output"]["next_action"]["code"] == "verify_original_launch"


def test_d15_verification_progress_then_failed_installation_stops_before_launch(preparation_bridge, monkeypatch):
    fixture = preparation_bridge
    fixture.registry._snapshot.verifying = frozenset({"classification"})

    def finish(_):
        fixture.registry._snapshot.verifying = frozenset()
        fixture.registry._snapshot.installations["classification"].installed = False

    monkeypatch.setattr("workbench_platform.batches.native.time.sleep", finish)
    result = fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: False)
    assert result["status"] == "waiting_input" and fixture.observations["starts"] == []
    assert any(receipt["phase"] == "checking" for _, receipt in fixture.checkpoints)
    assert fixture.batch["receipts"]["native_prepare"]["next_action"]["code"] == "repair_installation"
    assert fixture.batch["inputs"][0]["blob_hash"] == "c" * 64


@pytest.mark.parametrize("suffix,scenario", [(".pdf", "ocr"), (".tif", "classification")])
def test_d15_unsupported_input_never_launches_native(preparation_bridge, suffix, scenario):
    fixture = preparation_bridge
    fixture.asset.original_name = "input" + suffix
    fixture.batch["template"]["scenario"] = scenario
    result = fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: False)
    assert result["status"] == "waiting_input" and fixture.observations["starts"] == []
    assert "native_import" not in fixture.batch["receipts"]


@pytest.mark.parametrize("scenario,expected_phase,action", [
    ("classification", "unavailable", "check_gpu_or_select_cpu"), ("ocr", "review_only", "configure_full_mode")])
def test_d15_gpu_and_review_only_are_real_native_capability_boundaries(preparation_bridge, scenario, expected_phase, action):
    fixture = preparation_bridge
    fixture.batch["template"]["scenario"] = scenario
    fixture.batch["template"]["config"].update(device="cuda", engines=["ppocr"])
    fixture.observations["review_only"] = True
    result = fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: False)
    assert result["status"] == "waiting_input" and len(fixture.observations["starts"]) == 1
    prepare = fixture.batch["receipts"]["native_prepare"]
    assert prepare["phase"] == expected_phase and prepare["next_action"]["code"] == action
    assert "native_import" not in fixture.batch["receipts"]


def test_d15_imported_unknown_instance_cannot_prepare_a_new_service(preparation_bridge):
    fixture = preparation_bridge
    fixture.batch["receipts"]["native_import"] = {"state": "pending", "instance_id": "originalInstance"}
    result = fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: False)
    assert result["status"] == "needs_verification" and fixture.observations["starts"] == []
    assert fixture.batch["receipts"]["native_import"]["instance_id"] == "originalInstance"


def test_d15_cancel_or_close_during_preparation_keeps_shared_service_unmodified(preparation_bridge):
    fixture = preparation_bridge
    assert fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: True)["status"] == "cancelled"

    def closing():
        return True

    closing.stopping = lambda: True
    with pytest.raises(NativeBatchError) as error:
        fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, closing)
    assert error.value.status == "queued" and fixture.observations["starts"] == []


def test_d15_submitted_task_uses_existing_verified_generation_rebind(preparation_bridge):
    fixture = preparation_bridge
    fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: False)
    submitted = fixture.runner.jobs.register("workspaceA", "linkA", "classification", ["nativeA"])
    fixture.batch["receipts"]["native_submission"] = submitted
    fixture.supervisor._entries.clear()
    fixture.supervisor._current.clear()
    fixture.observations["instance_id"] = "instanceB"
    fixture.launches.start("classification", "explicitRestoredLaunch")
    assert fixture.runner.prepare_runtime(fixture.batch, fixture.checkpoint, lambda: False) is None
    prepare = fixture.batch["receipts"]["native_prepare"]
    assert prepare["recovery_only"] is True and prepare["original_instance_id"] == "instanceA"
    assert prepare["instance_id"] == "instanceB"
    assert fixture.batch["receipts"]["native_submission"] == submitted
    assert fixture.batch["receipts"]["native_progress"]["native_job_ids"] == ["nativeA"]
    assert len(fixture.observations["starts"]) == 2  # Only the explicit restored launch.


@pytest.mark.parametrize("cancel", [False, True])
def test_d15_prepare_only_receipt_cannot_skip_business_preflight_or_cancellation(preparation_bridge, cancel):
    import threading
    from workbench_platform.batches.service import BatchService
    from workbench_platform.batches.store import BatchStore
    fixture = preparation_bridge
    service = BatchService.__new__(BatchService)
    service.store = BatchStore(fixture.tmp_path / "batches")
    template = service.store.save_template({"schema_version": "1.0", "workspace_id": "workspaceA", "name": "invalid model",
        "scenario": "classification", "config": {"link_id": "linkA", "model_id": "missingModel"}})
    batch, _ = service.store.accept({"schema_version": "1.0", "workspace_id": "workspaceA", "request_key": "prepareOnly",
        "template_id": template["template_id"], "template_version": 1, "inputs": fixture.batch["inputs"]}, template)
    service.store.update(batch["batch_id"], receipts={"native_prepare": {"phase": "checking"}}, cancel_requested=cancel)
    service.native, service.closing, service.lock = fixture.runner, threading.Event(), threading.RLock()
    service.running = {batch["batch_id"]}
    service._run(batch["batch_id"])
    actual = service.store.get(batch["batch_id"])
    assert actual["status"] == ("cancelled" if cancel else "waiting_input")
    assert "native_import" not in actual["receipts"]
    if cancel:
        assert fixture.observations["starts"] == []
    else:
        assert any(item["item_id"] == "model" for item in actual["issues"])
