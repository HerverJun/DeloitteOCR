"""Comparison UI contract exercised through real local HTTP and uploaded assets.

Only parsing/model computation is injected; workspace authorization, uploads,
configuration versions, checkpoints, revisions and artifact delivery are real.
"""
from copy import deepcopy
import hashlib
import threading
import time

from fastapi.testclient import TestClient
import pytest

from workbench_platform.app import app


LOCAL = {'host': '127.0.0.1:8766', 'origin': 'http://127.0.0.1:8766'}


class TestVault:
    __test__ = False

    def __init__(self):
        self.values = {}

    def save(self, value):
        reference = 'key' + str(len(self.values) + 1)
        self.values[reference] = value
        return reference

    def read(self, reference):
        return self.values[reference]


def prepare(path, source, output, **kwargs):
    output.mkdir(parents=True, exist_ok=True)
    image = output / 'page.png'
    image.write_bytes(b'test-page-image')
    return [{'page_number': 1, 'page_id': source['asset_id'] + 'p1', 'source': source,
             'image_path': str(image), 'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
             'width': 200, 'height': 200, 'blocks': []}]


def parse(page, config, output, **kwargs):
    kwargs['cancel']()
    block_id = page['page_id'] + 'b1'
    table_id = page['page_id'] + 't1'
    block = {'block_id': block_id, 'type': 'table', 'table_id': table_id,
             'text': '项目甲\t50%', 'order': 0, 'bbox': None, 'source': page['source']}
    cells = [{'row': 0, 'column': 0, 'text': '项目甲', 'source': {**page['source'], 'page_number': 1, 'precision': 'page_only'}},
             {'row': 0, 'column': 1, 'text': '50%', 'source': {**page['source'], 'page_number': 1, 'precision': 'page_only'}}]
    for cell in cells:
        cell.update(row_span=1, column_span=1, is_header=False, polygon=None, confidence=None)
    table = {'table_id': table_id, 'block_id': block_id, 'page_number': 1,
             'rows': 1, 'columns': 2, 'cells': cells, 'source': page['source']}
    return {**page, 'status': 'succeeded', 'engine': config['engine'], 'blocks': [block], 'tables': [table]}


def compare(left, right, profile, call, **kwargs):
    kwargs['cancel']()
    return {'kind': 'clear.comparison', 'schema_version': '1.0', 'profile': profile,
            'documents': {'a': left, 'b': right}, 'fact_sets': {}, 'items': [], 'summary': {},
            'coverage': {'a': {'complete': True}, 'b': {'complete': True}}, 'diagnostics': [], 'models': []}


@pytest.fixture(scope='module')
def api_app(tmp_path_factory):
    patch = pytest.MonkeyPatch()
    class LocalBatch:
        def __init__(self, app):
            pass

        def parse_batch(self, pages, config, output_dir, cancel=None, committed=None):
            for page in pages:
                cancel()
                committed(page, {'status': 'succeeded'})
            return {}
    patch.setattr('workbench_platform.comparisons.local_ocr.LocalOcrParser', LocalBatch)
    root = tmp_path_factory.mktemp('comparison-http')
    patch.setenv('WORKBENCH_DATA_DIR', str(root))
    patch.delenv('WORKBENCH_DATA_PYTHON', raising=False)
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        service = app.state.comparison_service
        service.vault = TestVault()
        yield client, service
    patch.undo()


@pytest.fixture
def api(api_app):
        client, service = api_app
        service.prepare, service.parse, service.compare = prepare, parse, compare
        calls = []

        def model(config, key, system, payload, **kwargs):
            calls.append({'key': key, 'provider': config.id, 'version': config.version})
            return {'data': {'challenge': payload['challenge']}, 'usage': {'input_tokens': 1, 'output_tokens': 1}, 'model': config.model}

        service.llm_call = model
        workspace_ids = []
        for name in ('对比项目甲', '对比项目乙'):
            response = client.post('/api/platform/v1/workspaces', headers=LOCAL, json={'schema_version': '1.0', 'name': name})
            assert response.status_code == 201, response.text
            workspace_ids.append(response.json()['workspace_id'])
        workspace, other_workspace = workspace_ids
        base = f'/api/platform/v1/workspaces/{workspace}/comparisons'
        other = f'/api/platform/v1/workspaces/{other_workspace}/comparisons'
        profile_response = client.get(base + '/profiles')
        assert profile_response.status_code == 200, profile_response.text
        profile = profile_response.json()['items'][0]
        provider_response = client.post(base + '/connections', headers=LOCAL, json={
            'name': '用户自定义连接', 'protocol': 'openai_chat_completions', 'base_url': 'https://example.test/v1',
            'model': 'text-model', 'api_key': 'private-test-value', 'context_window': 32768, 'max_output_tokens': 8192,
            'supports_vision': False, 'structured_output': 'json_schema', 'max_request_bytes': 12582912, 'total_seconds': 180})
        assert provider_response.status_code == 201, provider_response.text
        provider = provider_response.json()
        references = []
        for name in ('扫描进度.pdf', '汇报幻灯片.pptx'):
            response = client.post(f'/api/platform/v1/workspaces/{workspace}/assets/uploads', headers=LOCAL,
                                   params={'name': name}, content=name.encode('utf-8'))
            assert response.status_code == 201, response.text
            value = response.json()
            references.append({'asset_id': value['asset_id'], 'asset_revision': value['revision'], 'blob_hash': value['blob_hash']})
        body = {'request_id': 'request1', 'title': 'API 项目进度对比',
                'left': {'assets': [references[0]], 'layout': {'mode': 'local_ocr', 'engine': 'paddlevl'}},
                'right': {'assets': [references[1]], 'layout': {'mode': 'local_ocr', 'engine': 'paddlevl'}},
                'profile_id': profile['id'], 'profile_version': profile['version'],
                'comparison_provider_id': provider['id'], 'comparison_provider_version': provider['version']}
        yield client, service, base, other, body, provider, calls


def wait(client, path, statuses=('succeeded', 'failed', 'partial')):
    app.state.comparison_service.executor.submit(lambda: None).result(timeout=60)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200, response.text
        value = response.json()
        if value['status'] in statuses:
            return value
        time.sleep(.2)
    raise AssertionError(value)


def create(api):
    client, _, base, _, body, _, _ = api
    response = client.post(base, headers=LOCAL, json=body)
    assert response.status_code == 202, response.text
    path = base + '/' + response.json()['comparison_id']
    result = wait(client, path)
    assert result['status'] == 'succeeded', result.get('error')
    return path, result


def test_http_provider_versions_blank_key_profile_copy_and_origin(api):
    client, _, base, other, _, provider, calls = api
    assert provider['has_key'] and 'api_key' not in provider
    assert 'private-test-value' not in client.get(base + '/connections').text
    edited = {**provider, 'name': '修改后的连接', 'api_key': ''}
    saved = client.post(base + '/connections', headers=LOCAL, json=edited)
    assert saved.status_code == 201, saved.text
    latest = saved.json()
    assert latest['version'] == 2 and latest['has_key']
    for version in (1, 2):
        probe = client.post(base + f"/connections/{provider['id']}/probe", headers=LOCAL, json={'version': version})
        assert probe.status_code == 200 and probe.json()['passed'], probe.text
    assert [item['key'] for item in calls] == ['private-test-value', 'private-test-value']
    assert [item['version'] for item in calls] == [1, 2]
    assert client.post(base + '/connections', headers=LOCAL, json=edited).status_code == 409
    assert client.post(base + '/connections', json=edited).status_code == 403
    assert client.post(base + '/connections', headers={**LOCAL, 'origin': 'https://elsewhere.test'}, json=edited).status_code == 403
    assert client.get(other + '/connections').json()['items'] == []
    assert client.post(other + f"/connections/{provider['id']}/probe", headers=LOCAL, json={}).status_code == 404
    profile = client.get(base + '/profiles').json()['items'][0]
    profile.pop('version')
    profile.update(id='custom_copy', name='自定义主题')
    copied = client.post(base + '/profiles', headers=LOCAL, json=profile)
    assert copied.status_code == 201 and copied.json()['version'] == 1, copied.text


def test_http_create_asset_isolation_idempotency_and_artifact_center(api):
    client, _, base, other, body, _, _ = api
    path, result = create(api)
    replay = client.post(base, headers=LOCAL, json=body)
    assert replay.status_code == 202 and replay.json()['comparison_id'] == result['comparison_id']
    assert client.post(base, headers=LOCAL, json={**body, 'title': '不同材料请求'}).status_code == 409
    assert client.get(other + '/' + result['comparison_id']).status_code == 404
    assert client.get(other + '/' + result['comparison_id'] + '/pages/left/1').status_code == 404
    assert client.get(other + '/' + result['comparison_id'] + '/files/report.html').status_code == 404
    assert client.post(other, headers=LOCAL, json=body).status_code in (404, 409)
    assert client.post(base, json=body).status_code == 403
    report = client.get(path + '/files/report.html?revision=1')
    assert report.status_code == 200 and '固定模板' in report.text
    assert report.headers['content-security-policy'].startswith('sandbox;')
    page = client.get(path + '/pages/left/1?revision=1').json()
    assert client.get(page['image_url']).status_code == 200
    artifact_base = base.removesuffix('/comparisons') + '/artifacts'
    artifacts = client.get(artifact_base)
    assert artifacts.status_code == 200, artifacts.text
    bundle = next(item for item in artifacts.json()['items'] if item['kind'] == 'document_comparison')
    assert bundle['comparison_id'] == result['comparison_id'] and bundle['comparison_revision'] == 1
    assert {item['name'] for item in bundle['files']} == set(result['files'])
    for item in bundle['files']:
        downloaded = client.get(artifact_base + '/' + bundle['bundle_id'] + '/files/' + item['name'])
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == client.get(path + '/files/' + item['name'] + '?revision=1').content


def test_http_table_review_preserves_sources_and_revision_exports(api):
    client, _, base, _, _, _, _ = api
    path, result = create(api)
    original_report = client.get(path + '/files/report.html?revision=1').content
    original = client.get(path + '/pages/left/1?revision=1').json()
    blocks = deepcopy(original['blocks'])
    original_table = original['tables'][0]
    table = {key: deepcopy(original_table[key]) for key in ('rows', 'columns', 'cells')}
    table['cells'][1]['text'] = '80%'
    table.update(rows=2, columns=3)
    table['cells'][0]['row_span'] = 2
    table['cells'][1]['column'] = 2
    table['cells'].append({'row': 1, 'column': 2, 'text': '90%'})
    blocks[0]['table'] = table
    blocks[0]['source'] = {'asset_id': 'fabricated'}
    reviewed = client.post(path + '/pages/left/1/review', headers=LOCAL, json={'expected_revision': 1, 'blocks': blocks})
    assert reviewed.status_code == 202, reviewed.text
    latest = wait(client, path)
    assert latest['status'] == 'succeeded' and latest['revision'] == 2, latest.get('error')
    current = client.get(path + '/pages/left/1?revision=2').json()
    assert '80%' in current['blocks'][0]['text']
    assert current['blocks'][0]['text'] == '项目甲\t\t80%\n项目甲\t\t90%'
    assert current['blocks'][0]['source'] == original['blocks'][0]['source']
    assert current['tables'][0]['original_cells'][1]['text'] == '50%'
    assert current['tables'][0]['cells'][0]['source'] == original_table['cells'][0]['source']
    assert current['tables'][0]['cells'][1]['source']['precision'] == 'page_only'
    assert current['tables'][0]['cells'][2]['source']['box'] is None
    assert '80%' in client.get(path + '/files/left.md?revision=2').text
    assert client.get(path + '/files/report.html?revision=1').content == original_report
    assert client.get(path + '?revision=1').json()['outdated']
    assert client.get(path + '/pages/left/1?revision=1').json()['blocks'][0]['text'] == original['blocks'][0]['text']
    assert client.post(path + '/pages/left/1/review', headers=LOCAL, json={'expected_revision': 1, 'blocks': blocks}).status_code == 409
    artifacts = client.get(base.removesuffix('/comparisons') + '/artifacts').json()['items']
    old_bundle = next(item for item in artifacts if item.get('comparison_revision') == 1)
    assert old_bundle['status'] == 'outdated'


def test_http_cancel_resume_reuses_completed_pages_and_rerun_targets_one_page(api):
    client, service, base, _, body, _, _ = api
    entered, release = threading.Event(), threading.Event()
    counts = []
    right_asset = body['right']['assets'][0]['asset_id']

    def controlled(page, config, output, **kwargs):
        if page['source']['asset_id'] == right_asset and not release.is_set():
            entered.set()
            while not release.wait(.01):
                kwargs['cancel']()
        value = parse(page, config, output, **kwargs)
        counts.append(page['source']['asset_id'])
        return value

    service.parse = controlled
    response = client.post(base, headers=LOCAL, json=body)
    assert response.status_code == 202, response.text
    path = base + '/' + response.json()['comparison_id']
    try:
        assert entered.wait(5)
        cancelled = client.post(path + '/control/cancel', headers=LOCAL)
        assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled'
        resumed = client.post(path + '/control/resume', headers=LOCAL)
        assert resumed.status_code == 200 and resumed.json()['generation'] == 2
    finally:
        release.set()
    complete = wait(client, path)
    assert complete['status'] == 'succeeded', complete.get('error')
    assert len(counts) == 2
    rerun = client.post(path + '/rerun-pages', headers=LOCAL, json={'side': 'right', 'pages': [1], 'expected_revision': complete['revision'], 'layout': {'mode': 'local_ocr', 'engine': 'mineru_pro'}})
    assert rerun.status_code == 202, rerun.text
    complete = wait(client, path)
    assert complete['status'] == 'succeeded' and complete['revision'] == 2
    assert len(counts) == 3 and counts[-1] == right_asset
    assert client.get(path + '/pages/right/1').json()['engine'] == 'mineru_pro'


def test_http_comparison_failure_still_exports_parsed_documents(api):
    client, service, base, _, body, _, _ = api

    def unavailable(*args, **kwargs):
        raise RuntimeError('Comparison provider unavailable')

    service.compare = unavailable
    response = client.post(base, headers=LOCAL, json=body)
    assert response.status_code == 202, response.text
    path = base + '/' + response.json()['comparison_id']
    failed = wait(client, path)
    assert failed['status'] == 'failed'
    assert set(failed['files']) == {'left.json', 'left.md', 'right.json', 'right.md'}
    for name in failed['files']:
        assert client.get(path + '/files/' + name + '?revision=1').status_code == 200
    assert client.get(path + '/files/report.html?revision=1').status_code == 404


def test_http_recompare_changes_profile_provider_without_repeating_ocr(api):
    client, service, base, _, _, provider, _ = api
    path, original = create(api)
    profile = client.get(base + '/profiles').json()['items'][0]
    profile['instructions'] = '仅比较工作项及计划完成日期'
    response = client.post(base + '/profiles', headers=LOCAL, json=profile)
    assert response.status_code == 201, response.text
    saved_profile = response.json()
    response = client.post(base + '/connections', headers=LOCAL, json={**provider, 'model': 'new-text-model', 'api_key': ''})
    assert response.status_code == 201, response.text
    saved_provider = response.json()
    parse_calls = []

    def forbidden_ocr(*args, **kwargs):
        parse_calls.append(True)
        raise AssertionError('Recompare must not repeat OCR')

    service.parse = forbidden_ocr
    response = client.post(path + '/recompare', headers=LOCAL, json={
        'expected_revision': original['revision'], 'profile_id': saved_profile['id'], 'profile_version': saved_profile['version'],
        'comparison_provider_id': saved_provider['id'], 'comparison_provider_version': saved_provider['version']})
    assert response.status_code == 202, response.text
    latest = wait(client, path)
    assert latest['status'] == 'succeeded' and latest['revision'] == 2, latest.get('error')
    assert latest['profile']['version'] == 2 and latest['comparison_provider']['version'] == 2
    assert not parse_calls
    assert client.get(path + '?revision=1').json()['profile']['version'] == 1
    assert client.get(path + '/files/report.html?revision=1').status_code == 200
