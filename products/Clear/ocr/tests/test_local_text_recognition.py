"""CPU contracts for explicit crop text requests; no model or GPU is loaded."""

import base64
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

from ocr_workbench import adapter as adapter_module
from ocr_workbench import engine_host, glm, worker
from ocr_workbench.adapter import Cancelled, EngineAdapter
from ocr_workbench.atomic_files import read_json, write_json
from ocr_workbench.recognition_modes import validate_recognition_mode


ENGINES = ('glm', 'ppocr', 'paddlevl', 'hunyuan', 'geometry', 'dewarp')
BAD_MODES = (None, True, 0, 'text', 'geometry', 'local-text', {}, [])


def fixture_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, value)


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / 'bundle'
    (root / 'config').mkdir(parents=True)
    fixture_json(root / 'config/engines.json', {
        engine: {'name': 'CPU fixture', 'models': [], 'capabilities': {}}
        for engine in ENGINES
    })
    return root


@pytest.fixture
def image(tmp_path):
    path = tmp_path / '原图 crop.png'
    Image.new('RGB', (180, 90), 'white').save(path)
    return path


@pytest.fixture
def adapter(bundle, tmp_path):
    instance = EngineAdapter(bundle, 'glm', tmp_path / 'sessions')
    instance.process = Mock(pid=99)
    instance.process.poll.return_value = None
    return instance


class InputBatch(dict):
    def to(self, device):
        self.device = device
        return self


def fake_session(text, *, token_count=2, max_tokens=8, images=1):
    encoded = io.BytesIO()
    Image.new('RGB', (40, 20), 'white').save(encoded, format='JPEG')
    image_url = {'type': 'image_url', 'image_url': {
        'url': 'data:image/jpeg;base64,' + base64.b64encode(encoded.getvalue()).decode()}}
    payload = {'messages': [{'role': 'user', 'content': [
        *[deepcopy(image_url) for _ in range(images)],
        {'type': 'text', 'text': 'Official text prompt fixture'}]}],
        'max_tokens': max_tokens, 'repetition_penalty': 1.1}
    captures = {}

    def load_request(image, *, task_type):
        captures['loader_image_size'] = image.size
        captures['task_type'] = task_type
        return deepcopy(payload)

    def template(messages, **kwargs):
        captures['messages'] = messages
        captures['template_kwargs'] = kwargs
        return InputBatch(input_ids=SimpleNamespace(shape=(1, 3)))

    def generate(**kwargs):
        captures['generation_kwargs'] = kwargs
        return [[101, 102, 103, *range(token_count)]]

    def decode(ids, **kwargs):
        captures['decoded_ids'] = ids
        captures['decode_kwargs'] = kwargs
        return text

    detector = Mock()
    formatter = Mock()
    session = (detector, SimpleNamespace(device='CPU-fixture', generate=generate),
        SimpleNamespace(apply_chat_template=template, decode=decode),
        SimpleNamespace(build_request_from_image=load_request), formatter, 7.0, None)
    return session, captures, encoded.getvalue()


@pytest.mark.parametrize('engine', ENGINES)
def test_ordinary_page_remains_supported(engine):
    assert validate_recognition_mode(engine) == 'page'


@pytest.mark.parametrize('mode', BAD_MODES)
def test_unknown_mode_is_rejected_without_coercion(mode):
    with pytest.raises(ValueError, match='Unsupported recognition mode'):
        validate_recognition_mode('glm', mode)


@pytest.mark.parametrize('engine', (*ENGINES[1:], 'unknown'))
def test_local_text_is_glm_only(engine):
    with pytest.raises(ValueError, match='only by GLM'):
        validate_recognition_mode(engine, 'local_text')


@pytest.mark.parametrize('engine,mode,geometry', [
    *[('glm', mode, None) for mode in BAD_MODES],
    *[(engine, 'local_text', None) for engine in ENGINES[1:]],
    ('glm', 'local_text', {}), ('glm', 'local_text', {'provider': 'context'}),
])
def test_adapter_rejects_invalid_requests_before_touching_ipc(adapter, monkeypatch, image, tmp_path, engine, mode, geometry):
    adapter.engine = engine
    response = adapter.ipc / 'response.json'
    response.write_text('retained prior receipt', encoding='utf-8')
    publish = Mock()
    monkeypatch.setattr(adapter_module, 'publish', publish)
    with pytest.raises(ValueError):
        adapter.recognize(image, tmp_path / 'result', geometry, recognition_mode=mode)
    publish.assert_not_called()
    assert response.read_text('utf-8') == 'retained prior receipt'
    assert not (adapter.ipc / 'request.json').exists()


def test_page_wire_is_unchanged_and_local_request_keeps_configured_budget(adapter, monkeypatch, image, tmp_path):
    monkeypatch.setattr(adapter_module.uuid, 'uuid4', lambda: SimpleNamespace(hex='request-1'))
    wait = Mock(return_value={'id': 'request-1', 'status': 'success'})
    monkeypatch.setattr(adapter, 'wait_for', wait)
    adapter.recognition_timeout = 17
    output = tmp_path / 'result'
    fixture_json(output / 'result.json', {'status': 'success', 'text': '000123'})
    adapter.recognize(image, output)
    page = read_json(adapter.ipc / 'request.json')
    assert page == {'id': 'request-1', 'image': str(image), 'output': str(output), 'geometry_request': None}
    fixture_json(output / 'result.json', {'status': 'success', 'text': '000123',
        'recognition_mode': 'local_text', 'tables': [], 'raw': {
            'recognition_mode': 'local_text', 'layout_applied': False, 'formatter_applied': False}})
    adapter.recognize_local_text(image, output)
    assert read_json(adapter.ipc / 'request.json') == {**page, 'recognition_mode': 'local_text'}
    assert wait.call_args_list[0].args == wait.call_args_list[1].args == (adapter.ipc / 'response.json', 17)


@pytest.mark.parametrize('cancelled', [False, True])
def test_local_wait_preserves_real_timeout_or_cancellation(adapter, image, tmp_path, cancelled):
    adapter.recognition_timeout = 1 if cancelled else 0
    if cancelled:
        adapter.cancel()
    with pytest.raises(Cancelled if cancelled else TimeoutError):
        adapter.recognize_local_text(image, tmp_path / 'never-published')
    assert not (tmp_path / 'never-published').exists()
    assert adapter.recognition_timeout == (1 if cancelled else 0)


@pytest.mark.parametrize('reply', [
    {'id': 'other-request', 'status': 'success'},
    {'id': 'request-1', 'status': 'failed', 'message': 'failed inference'},
])
def test_local_failed_or_unmatched_response_cannot_consume_old_success(adapter, image, tmp_path, monkeypatch, reply):
    output = tmp_path / 'retained-result'
    fixture_json(output / 'result.json', {'status': 'success', 'text': 'old text'})
    original = (output / 'result.json').read_bytes()
    monkeypatch.setattr(adapter_module.uuid, 'uuid4', lambda: SimpleNamespace(hex='request-1'))
    monkeypatch.setattr(adapter, 'wait_for', Mock(return_value=reply))
    with pytest.raises(RuntimeError):
        adapter.recognize_local_text(image, output)
    assert (output / 'result.json').read_bytes() == original


@pytest.mark.parametrize('change', [
    {'recognition_mode': 'page'}, {'raw': None}, {'raw': {'recognition_mode': 'page'}},
    {'raw': {'recognition_mode': 'local_text', 'layout_applied': True, 'formatter_applied': False}},
    {'raw': {'recognition_mode': 'local_text', 'layout_applied': False, 'formatter_applied': True}},
    {'tables': [{'cells': []}]},
])
def test_local_cannot_accept_page_or_mislabelled_worker_result(adapter, image, tmp_path, monkeypatch, change):
    output = tmp_path / 'result'
    result = {'status': 'success', 'text': 'old worker content', 'recognition_mode': 'local_text',
        'tables': [], 'raw': {'recognition_mode': 'local_text', 'layout_applied': False, 'formatter_applied': False}}
    fixture_json(output / 'result.json', {**result, **change})
    original = (output / 'result.json').read_bytes()
    monkeypatch.setattr(adapter_module.uuid, 'uuid4', lambda: SimpleNamespace(hex='request-1'))
    monkeypatch.setattr(adapter, 'wait_for', Mock(return_value={'id': 'request-1', 'status': 'success'}))
    with pytest.raises(RuntimeError, match='请求模式不一致'):
        adapter.recognize_local_text(image, output)
    assert (output / 'result.json').read_bytes() == original


def test_resident_dispatch_preserves_page_call_and_shared_load_accounting(bundle, image, tmp_path, monkeypatch):
    resident = engine_host.Resident.__new__(engine_host.Resident)
    resident.engine, resident.bundle, resident.session = 'glm', bundle, 'fake-session'
    resident.loaded, resident.first = 7.0, True
    page, local = Mock(return_value=({}, [], 7.0)), Mock(return_value=({}, [], 7.0))
    monkeypatch.setattr(glm, 'recognize', page)
    monkeypatch.setattr(glm, 'recognize_local_text', local)
    assert resident.recognize(image, tmp_path)[2] == 7.0
    assert resident.recognize(image, tmp_path, recognition_mode='local_text')[2] == 0
    page.assert_called_once_with(bundle / 'models', image, 'fake-session')
    local.assert_called_once_with(bundle / 'models', image, 'fake-session')
    resident.engine = 'hunyuan'
    with pytest.raises(ValueError):
        resident.recognize(image, tmp_path, recognition_mode='local_text')
    assert local.call_count == 1


def test_local_decoder_uses_official_text_processing_and_binds_pixels_without_layout(image, monkeypatch):
    session, captures, jpeg = fake_session(' 000123\n-10.00\n=SUM(A1) ')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(inference_mode=nullcontext))
    raw, blocks, loaded = glm.recognize_local_text(Path('unused-models'), image, session)
    assert captures['task_type'] == 'text'
    assert captures['loader_image_size'] == (180, 90)
    assert captures['messages'][0]['content'][0]['type'] == 'image'
    assert captures['template_kwargs'] == {'tokenize': True, 'add_generation_prompt': True, 'return_dict': True, 'return_tensors': 'pt'}
    assert captures['decode_kwargs'] == {'skip_special_tokens': True}
    assert captures['generation_kwargs']['max_new_tokens'] == 8
    assert captures['generation_kwargs']['do_sample'] is False
    assert captures['generation_kwargs']['repetition_penalty'] == 1.1
    assert raw['input_image_sha256'] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert raw['input_image_size'] == [180, 90]
    binding = raw['request']['image_bindings'][0]
    assert binding['encoded_image_sha256'] == hashlib.sha256(jpeg).hexdigest()
    assert binding['decoder_image_size'] == [40, 20]
    with Image.open(io.BytesIO(jpeg)) as decoder_image:
        assert binding['decoder_rgb_pixels_sha256'] == hashlib.sha256(decoder_image.convert('RGB').tobytes()).hexdigest()
    assert raw['generated'][0]['content'] == blocks[0]['text'] == ' 000123\n-10.00\n=SUM(A1) '
    assert blocks[0]['polygon'] == [[0, 0], [180, 0], [180, 90], [0, 90]]
    assert blocks[0]['confidence'] is None and loaded == 7.0
    assert raw['layout_applied'] is raw['formatter_applied'] is raw['truncated'] is False
    session[0].process.assert_not_called()
    session[4].process.assert_not_called()


def test_empty_local_decode_is_preserved_and_requires_explicit_empty_acceptance(image, monkeypatch):
    session, _, _ = fake_session('')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(inference_mode=nullcontext))
    raw, blocks, _ = glm.recognize_local_text(Path('unused-models'), image, session)
    result = {'status': 'success', 'text': blocks[0]['text'], 'raw': raw}
    with pytest.raises(RuntimeError):
        adapter_module.check_recognition_result(deepcopy(result))
    assert adapter_module.check_recognition_result(result, allow_empty=True)['content_status'] == 'no_text_detected'
    assert raw['generated'][0]['content'] == ''


def test_token_limit_is_failure_with_original_decode_evidence(image, monkeypatch):
    session, _, _ = fake_session('unfinished 000123', token_count=8)
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(inference_mode=nullcontext))
    with pytest.raises(glm.LocalTextTruncatedOutput) as caught:
        glm.recognize_local_text(Path('unused-models'), image, session)
    evidence = caught.value.recognition_evidence
    assert evidence['truncated'] is True
    assert evidence['generated'][0]['content'] == 'unfinished 000123'
    assert evidence['generated'][0]['token_count'] == evidence['generated'][0]['token_limit'] == 8
    assert evidence['input_image_sha256'] == hashlib.sha256(image.read_bytes()).hexdigest()


@pytest.mark.parametrize('images', [0, 2])
def test_bad_official_image_binding_is_rejected_before_decoder(image, monkeypatch, images):
    session, captures, _ = fake_session('not consumed', images=images)
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(inference_mode=nullcontext))
    with pytest.raises(ValueError, match='exactly one crop image'):
        glm.recognize_local_text(Path('unused-models'), image, session)
    assert 'generation_kwargs' not in captures


@pytest.mark.parametrize('mode', ['page', 'local_text'])
def test_worker_mode_preserves_raw_and_only_page_parses_tables(bundle, image, tmp_path, monkeypatch, mode):
    text = '| ID | Amount |\n| --- | --- |\n| 000123 | 10.00 |'
    recognize = Mock(return_value=({'mode': mode}, [{'kind': 'text', 'text': text, 'confidence': None}], 7.0))
    monkeypatch.setattr(worker, 'configure', Mock())
    monkeypatch.setattr('ocr_workbench.runtime_audit.inspect_runtime', lambda _: {'fixture_only': True})
    args = SimpleNamespace(bundle=bundle, engine='glm', image=image, output=tmp_path / 'result', recognition_mode=mode)
    worker.run_image(args, SimpleNamespace(recognize=recognize))
    result = read_json(args.output / 'result.json')
    assert result['text'] == text and result['raw'] == {'mode': mode}
    assert read_json(args.output / 'raw.json') == result['raw']
    assert result['image']['coordinates'] == 'prepared_image_pixels'
    assert result['image']['width'] == 180 and result['image']['height'] == 90
    assert result['image']['sha256'] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert result['image']['version'] == hashlib.sha256((args.output / 'input.png').read_bytes()).hexdigest()
    if mode == 'page':
        assert result['tables'] and (args.output / 'result.xlsx').exists()
        assert 'recognition_mode' not in result and recognize.call_args.kwargs == {}
    else:
        assert result['tables'] == [] and not (args.output / 'result.xlsx').exists()
        assert result['recognition_mode'] == 'local_text'
        assert result['artifacts']['xlsx']['status'] == 'unavailable'
        assert recognize.call_args.kwargs == {'recognition_mode': 'local_text'}


@pytest.mark.parametrize('engine,mode', [('ppocr', 'local_text'), ('glm', None), ('glm', 'text')])
def test_worker_rejects_mode_before_output_or_image_io(bundle, tmp_path, monkeypatch, engine, mode):
    args = SimpleNamespace(bundle=bundle, engine=engine, image=tmp_path / 'missing.png', output=tmp_path / 'result', recognition_mode=mode)
    real_process = worker.process_image
    process, configure = Mock(), Mock()
    monkeypatch.setattr(worker, 'process_image', process)
    monkeypatch.setattr(worker, 'configure', configure)
    with pytest.raises(ValueError):
        worker.run_image(args)
    process.assert_not_called()
    configure.assert_not_called()
    with pytest.raises(ValueError):
        real_process(args, None, 0)
    assert not args.output.exists()


def test_worker_truncation_keeps_raw_failure_and_publishes_no_success(bundle, image, tmp_path, monkeypatch):
    evidence = {'truncated': True, 'generated': [{'content': 'unfinished'}]}
    monkeypatch.setattr(worker, 'configure', Mock())
    monkeypatch.setattr(worker, 'process_image', Mock(side_effect=glm.LocalTextTruncatedOutput(evidence)))
    args = SimpleNamespace(bundle=bundle, engine='glm', image=image, output=tmp_path / 'failed', recognition_mode='local_text')
    with pytest.raises(glm.LocalTextTruncatedOutput):
        worker.run_image(args)
    assert read_json(args.output / 'error.json')['recognition_evidence'] == evidence
    assert not (args.output / 'result.json').exists()


def test_worker_cli_rejects_non_glm_mode_before_output_or_network_guard(bundle, tmp_path, monkeypatch):
    output = tmp_path / 'invalid-cli'
    monkeypatch.setattr(sys, 'argv', ['worker', '--bundle', str(bundle), '--engine', 'ppocr', '--image', 'missing.png', '--output', str(output), '--recognition-mode', 'local_text'])
    guard = Mock()
    monkeypatch.setattr(worker, 'install_guard', guard)
    with pytest.raises(ValueError):
        worker.main()
    guard.assert_not_called()
    assert not output.exists()


@pytest.mark.parametrize('engine,mode,geometry', [
    *[('glm', mode, None) for mode in BAD_MODES],
    *[(engine, 'local_text', None) for engine in ENGINES[1:]],
    ('glm', 'local_text', {}),
])
def test_host_rejects_forged_wire_mode_and_closes_owned_session(bundle, tmp_path, monkeypatch, engine, mode, geometry):
    ipc = tmp_path / 'ipc'
    fixture_json(ipc / 'request.json', {'id': 'invalid-request', 'image': 'missing.png', 'output': str(tmp_path / 'never-created'), 'recognition_mode': mode, 'geometry_request': geometry})
    session = SimpleNamespace(loaded=0, close=Mock(), session=SimpleNamespace(write=Mock()), dewarp=Mock())
    monkeypatch.setattr(engine_host, 'Resident', Mock(return_value=session))
    monkeypatch.setattr('ocr_workbench.offline.configure', Mock())
    monkeypatch.setattr('ocr_workbench.offline.install_guard', Mock())
    process = Mock()
    monkeypatch.setattr(worker, 'run_image', process)
    monkeypatch.setattr(sys, 'argv', ['host', '--bundle', str(bundle), '--engine', engine, '--ipc', str(ipc)])
    assert engine_host.main() == 1
    assert read_json(ipc / 'response.json')['status'] == 'failed'
    assert read_json(ipc / 'response.json')['id'] == 'invalid-request'
    assert not (ipc / 'request.json').exists()
    process.assert_not_called()
    session.session.write.assert_not_called()
    session.dewarp.assert_not_called()
    session.close.assert_called_once()
    assert not (tmp_path / 'never-created').exists()


def test_host_forwards_valid_local_mode_with_original_request_identity(bundle, image, tmp_path, monkeypatch):
    ipc = tmp_path / 'ipc'
    fixture_json(ipc / 'request.json', {'id': 'local-request', 'image': str(image), 'output': str(tmp_path / 'result'), 'recognition_mode': 'local_text', 'geometry_request': None})
    session = SimpleNamespace(loaded=0, close=Mock())
    monkeypatch.setattr(engine_host, 'Resident', Mock(return_value=session))
    monkeypatch.setattr('ocr_workbench.offline.configure', Mock())
    monkeypatch.setattr('ocr_workbench.offline.install_guard', Mock())
    calls = []
    def process(args, resident):
        calls.append((args, resident))
        fixture_json(ipc / 'stop.json', {})
    monkeypatch.setattr(worker, 'run_image', process)
    monkeypatch.setattr(sys, 'argv', ['host', '--bundle', str(bundle), '--engine', 'glm', '--ipc', str(ipc)])
    assert engine_host.main() == 0
    assert len(calls) == 1 and calls[0][0].recognition_mode == 'local_text'
    assert calls[0][1] is session
    assert read_json(ipc / 'response.json') == {'id': 'local-request', 'status': 'success'}
    session.close.assert_called_once()
