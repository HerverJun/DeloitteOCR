from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from workbench_platform.comparisons.service import ComparisonService
from workbench_platform.comparisons.store import ComparisonStore
from workbench_platform.data_api.service import DataError
from workbench_platform.agent.providers import ProviderFault


class Vault:
    def __init__(self):
        self.values = {}

    def save(self, value):
        key = str(len(self.values))
        self.values[key] = value
        return key

    def read(self, key):
        return self.values[key]


class Assets:
    def __init__(self, root):
        self.paths, self.assets = {}, {}
        for identity, name in [('a1', '扫描.pdf'), ('a2', '幻灯片.png')]:
            path = root / name
            path.write_bytes(identity.encode())
            self.paths[identity] = path
            self.assets[identity] = SimpleNamespace(asset_id=identity, revision=1,
                blob_hash=hashlib.sha256(path.read_bytes()).hexdigest(), original_name=name,
                media_type='application/pdf' if identity == 'a1' else 'image/png', size_bytes=2)

    def get(self, workspace, identity):
        if workspace != 'w1':
            raise DataError('not found', 404)
        return self.assets[identity]

    @contextmanager
    def stage_verified(self, workspace, identity, revision, blob_hash):
        asset = self.get(workspace, identity)
        if (asset.revision, asset.blob_hash) != (revision, blob_hash):
            raise DataError('changed', 409)
        yield self.paths[identity]


def fake_prepare(path, source, output, **kwargs):
    output.mkdir(parents=True, exist_ok=True)
    image = output / 'page.png'
    image.write_bytes(b'page-image')
    return [{'page_number': 1, 'page_id': source['asset_id'] + 'p1', 'source': source,
             'image_path': str(image), 'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest(), 'width': 200, 'height': 200}]


def fake_parse(page, config, output, call_vlm=None, cancel=None, **kwargs):
    cancel()
    response = call_vlm(stage='layout', config=config, system='Read one page', payload={'page_id': page['page_id']},
                        schema={'type': 'object'}, image={'data': 'eA==', 'media_type': 'image/png', 'width': 1, 'height': 1})
    block = {'block_id': page['page_id'] + 'b1', 'type': 'text', 'text': response['data']['text'], 'order': 0,
             'source': {**page['source'], 'page_number': 1}, 'bbox': None}
    return {**page, 'status': 'succeeded', 'engine': 'multimodal', 'blocks': [block], 'tables': []}


def fake_compare(left, right, profile, call, **kwargs):
    return {'kind': 'clear.comparison', 'schema_version': '1.0', 'profile': profile, 'documents': {'a': left, 'b': right},
            'fact_sets': {}, 'items': [], 'summary': {}, 'coverage': {'a': {'complete': True}, 'b': {'complete': True}}, 'diagnostics': [], 'models': []}


@pytest.fixture
def setup(tmp_path):
    registry = Assets(tmp_path)
    app = SimpleNamespace(state=SimpleNamespace(asset_library=(registry, None, None), artifact_center=None, multimodal_service=None))
    calls = []
    def client(config, key, system, payload, **kwargs):
        calls.append((key, payload))
        return {'data': {'text': '项目甲完成率50%'}, 'raw': {}, 'usage': {'input_tokens': 10, 'output_tokens': 10}, 'model': config.model}
    value = ComparisonService(app, tmp_path / 'comparisons', prepare=fake_prepare, parse=fake_parse,
                              compare=fake_compare, llm_call=client, vault=Vault())
    provider = value.save_connection('w1', {'name': 'test', 'base_url': 'https://example.com/v1', 'model': 'test',
                                           'supports_vision': True, 'api_key': 'private-value'})
    profile = value.profiles('w1')['items'][0]
    def side(identity):
        asset = registry.assets[identity]
        return {'assets': [{'asset_id': identity, 'asset_revision': 1, 'blob_hash': asset.blob_hash}],
                'layout': {'mode': 'multimodal', 'provider_id': provider['id'], 'provider_version': provider['version']}}
    body = {'request_id': 'run1', 'title': '项目对比', 'left': side('a1'), 'right': side('a2'),
            'profile_id': profile['id'], 'profile_version': profile['version'],
            'comparison_provider_id': provider['id'], 'comparison_provider_version': provider['version']}
    yield value, body, calls
    value.close()


def wait(service, job, wanted=('succeeded', 'failed', 'partial')):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        value = service.get('w1', job)
        if value['status'] in wanted:
            return value
        time.sleep(.2)
    raise AssertionError(service.get('w1', job))


def test_end_to_end_idempotency_exports_and_private_keys(setup):
    service, body, calls = setup
    first = service.create('w1', body)
    result = wait(service, first['comparison_id'])
    assert result['status'] == 'succeeded', result.get('error')
    assert result['progress'] == {'total': 2, 'processed': 2, 'succeeded': 2, 'failed': 0, 'current_side': None, 'current_page': None}
    assert len(calls) == 2
    replay = service.create('w1', body)
    assert replay['comparison_id'] == first['comparison_id']
    assert 'private-value' not in json.dumps(result)
    assert 'private-value' not in service.store.path.read_bytes().decode('latin1')
    assert set(result['files']) == {'report.html', 'comparison.json', 'left.json', 'right.json', 'left.md', 'right.md', 'manifest.json'}
    assert '固定模板' in service.file('w1', first['comparison_id'], 'report.html').read_text('utf-8')
    manifest = json.loads(service.file('w1', first['comparison_id'], 'manifest.json').read_text('utf-8'))
    assert all(hashlib.sha256(service.file('w1', first['comparison_id'], name).read_bytes()).hexdigest() == checksum
               for name, checksum in manifest['files'].items())
    with pytest.raises(DataError):
        service.get('w2', first['comparison_id'])
    with pytest.raises(DataError):
        service.file('w1', first['comparison_id'], '../credentials')
    with pytest.raises(DataError):
        service.create('w1', {**body, 'title': 'different'})


def test_page_revision_keeps_prior_report_and_other_page(setup):
    service, body, calls = setup
    first = service.create('w1', body)
    original = wait(service, first['comparison_id'])
    original_html = service.file('w1', first['comparison_id'], 'report.html').read_bytes()
    second = service.rerun('w1', first['comparison_id'], {'expected_revision': 1, 'side': 'right', 'pages': [1]})
    assert second['revision'] == 2
    result = wait(service, first['comparison_id'])
    assert result['status'] == 'succeeded', result.get('error')
    assert len(calls) == 3
    assert service.get('w1', first['comparison_id'], 1)['outdated']
    assert service.file('w1', first['comparison_id'], 'report.html', 1).read_bytes() == original_html
    with pytest.raises(DataError):
        service.rerun('w1', first['comparison_id'], {'expected_revision': 1, 'side': 'right', 'pages': [1]})


def test_review_is_new_revision_with_fixed_source_and_no_ocr_charge(setup):
    service, body, calls = setup
    first = service.create('w1', body)
    wait(service, first['comparison_id'])
    page = service.page('w1', first['comparison_id'], 'left', 1)
    blocks = deepcopy(page['blocks'])
    blocks[0]['text'] = '项目甲完成率60%'
    service.review('w1', first['comparison_id'], 'left', 1, {'expected_revision': 1, 'blocks': blocks})
    result = wait(service, first['comparison_id'])
    assert result['status'] == 'succeeded', result.get('error')
    assert len(calls) == 2
    revised = service.page('w1', first['comparison_id'], 'left', 1)
    assert revised['blocks'][0]['source'] == page['blocks'][0]['source']
    assert revised['blocks'][0]['original_text'] == '项目甲完成率50%'
    assert service.page('w1', first['comparison_id'], 'left', 1, 1)['blocks'][0]['text'] == '项目甲完成率50%'


def test_unknown_call_does_not_repost_on_resume(setup):
    service, body, calls = setup
    def unknown(*args, **kwargs):
        calls.append('unknown')
        raise ProviderFault('response_unknown', 'unknown')
    service.llm_call = unknown
    first = service.create('w1', body)
    result = wait(service, first['comparison_id'])
    assert result['status'] == 'failed', result.get('error')
    assert len(calls) == 1
    service.control('w1', first['comparison_id'], 'resume')
    wait(service, first['comparison_id'])
    assert len(calls) == 1
    # Explicit page rerun is a fresh authorized attempt, only for that page.
    current = service.get('w1', first['comparison_id'])
    service.rerun('w1', first['comparison_id'], {'expected_revision': current['revision'], 'side': 'left', 'pages': [1]})
    wait(service, first['comparison_id'])
    assert len(calls) == 2


def test_store_marks_interrupted_receipts_unknown(tmp_path):
    store = ComparisonStore(tmp_path / 'recovery')
    store.save_call('job1', 'call1', 'inflight', {'stage': 'layout'})
    reopened = ComparisonStore(tmp_path / 'recovery')
    assert reopened.call('job1', 'call1')['status'] == 'unknown'


def test_cancel_resume_reuses_completed_page(setup):
    service, body, calls = setup
    second_started, release = threading.Event(), threading.Event()
    def parse(page, config, output, **kwargs):
        if page['source']['asset_id'] == 'a2' and not release.is_set():
            second_started.set()
            release.wait(30)
        return fake_parse(page, config, output, **kwargs)
    service.parse = parse
    first = service.create('w1', body)
    assert second_started.wait(30)
    service.control('w1', first['comparison_id'], 'cancel')
    service.control('w1', first['comparison_id'], 'resume')
    release.set()
    result = wait(service, first['comparison_id'])
    assert result['status'] == 'succeeded', result.get('error')
    assert len(calls) == 2


def test_incomplete_extraction_is_partial_even_when_all_pages_succeed(setup):
    service, body, calls = setup
    def compare(*args, **kwargs):
        result = fake_compare(*args, **kwargs)
        result['coverage']['b']['complete'] = False
        return result
    service.compare = compare
    first = service.create('w1', body)
    result = wait(service, first['comparison_id'])
    assert result['progress']['failed'] == 0
    assert result['status'] == 'partial'


def test_explicit_recompare_can_retry_unknown_without_repeating_ocr(setup):
    service, body, calls = setup
    prior_client = service.llm_call
    unknown_count = []
    def client(config, key, system, payload, **kwargs):
        if system == 'Extract':
            unknown_count.append(1)
            if len(unknown_count) == 1:
                raise ProviderFault('response_unknown', 'unknown comparison')
        return prior_client(config, key, system, payload, **kwargs)
    service.llm_call = client
    def compare(left, right, profile, call, **kwargs):
        call(stage='extract', system='Extract', payload={'text': left['pages'][0]['blocks'][0]['text']}, schema={'type': 'object'})
        return fake_compare(left, right, profile, call, **kwargs)
    service.compare = compare
    first = service.create('w1', body)
    failed = wait(service, first['comparison_id'])
    assert failed['status'] == 'failed'
    assert set(failed['files']) == {'left.md', 'right.md', 'left.json', 'right.json'}
    service.control('w1', first['comparison_id'], 'resume')
    assert wait(service, first['comparison_id'])['status'] == 'failed'
    assert len(unknown_count) == 1
    service.recompare('w1', first['comparison_id'], {'expected_revision': 1, 'profile_id': body['profile_id'],
        'profile_version': body['profile_version'], 'comparison_provider_id': body['comparison_provider_id'],
        'comparison_provider_version': body['comparison_provider_version']})
    assert wait(service, first['comparison_id'])['status'] == 'succeeded'
    assert len(unknown_count) == 2
    assert len(calls) == 3  # two original pages plus one successful extraction


def test_new_image_budget_renders_original_before_page_rerun(setup):
    service, body, calls = setup
    seen = []
    def prepare(path, source, output, **kwargs):
        seen.append((source['asset_id'], kwargs['config']['max_image_edge']))
        return fake_prepare(path, source, output, **kwargs)
    service.prepare = prepare
    first = service.create('w1', body)
    wait(service, first['comparison_id'])
    limits = {**body['right']['layout'], 'max_image_edge': 1024}
    service.rerun('w1', first['comparison_id'], {'expected_revision': 1, 'side': 'right', 'pages': [1], 'layout': limits})
    result = wait(service, first['comparison_id'])
    assert result['status'] == 'succeeded', result.get('error')
    assert seen == [('a1', 2560), ('a2', 2560), ('a2', 1024)]


def test_200_pdf_pages_and_100_slides_are_independent_requests(setup):
    service, body, calls = setup
    def prepare(path, source, output, **kwargs):
        original = fake_prepare(path, source, output, **kwargs)[0]
        return [{**deepcopy(original), 'page_number': i, 'page_id': source['asset_id'] + 'p' + str(i)}
                for i in range(1, kwargs['max_pages'] + 1)]
    service.prepare = prepare
    first = service.create('w1', body)
    service.executor.submit(lambda: None).result(timeout=300)
    result = service.get('w1', first['comparison_id'])
    assert result['status'] == 'succeeded', result.get('error')
    assert result['progress']['succeeded'] == 300
    assert len(calls) == 300
    assert len({payload['page_id'] for _, payload in calls}) == 300


def test_local_pages_use_bounded_batches_and_commit_each_page(setup, monkeypatch):
    service, body, calls = setup
    loads = []
    class Batch:
        def __init__(self, app):
            pass
        def parse_batch(self, pages, config, output_dir, cancel=None, committed=None):
            loads.append(len(pages))
            for page in pages:
                cancel()
                committed(page, {'text': '项目甲完成率50%'})
            return {}
    monkeypatch.setattr('workbench_platform.comparisons.local_ocr.LocalOcrParser', Batch)
    def prepare(path, source, output, **kwargs):
        original = fake_prepare(path, source, output, **kwargs)[0]
        count = 42 if source['asset_id'] == 'a1' else 2
        return [{**deepcopy(original), 'page_number': i, 'page_id': source['asset_id'] + 'p' + str(i)} for i in range(1, count + 1)]
    def parse(page, config, output, call_local=None, cancel=None, **kwargs):
        raw = call_local(page, config, output, cancel)
        return {**page, 'status': 'succeeded', 'engine': config['engine'], 'tables': [],
                'blocks': [{'block_id': page['page_id'] + 'b1', 'text': raw['text'], 'type': 'text', 'order': 0}]}
    service.prepare, service.parse = prepare, parse
    for side in ('left', 'right'):
        body[side]['layout'] = {'mode': 'local_ocr'}
    first = service.create('w1', body)
    service.executor.submit(lambda: None).result(timeout=120)
    result = service.get('w1', first['comparison_id'])
    assert result['status'] == 'succeeded', result.get('error')
    assert loads == [20, 20, 2, 2]
    assert result['progress']['succeeded'] == 44
    assert len(calls) == 0
