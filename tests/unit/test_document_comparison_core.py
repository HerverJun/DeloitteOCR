"""Behavioral checks for bounded model calls, evidence and fixed reports."""
import base64
from copy import deepcopy
import json

import httpx
import pytest
from pydantic import ValidationError

from workbench_platform.agent.providers import ProviderFault
from workbench_platform.comparisons.compare import extract_and_compare, normalize_value
from workbench_platform.comparisons.llm import estimate_request_input, structured_call
from workbench_platform.comparisons.models import ComparisonProfile, LayoutParseConfig, ProviderConfig
from workbench_platform.comparisons.report import document_markdown, render_report


def connection(**changes):
    return ProviderConfig(**{'base_url': 'https://example.test/v1', 'model': 'test-model', **changes})


def wire(data, finish='stop', protocol='openai_chat_completions'):
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    if protocol == 'anthropic_messages':
        return {'model': 'test-model', 'role': 'assistant', 'type': 'message', 'stop_reason': finish,
                'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': 11, 'output_tokens': 7}}
    return {'model': 'test-model', 'choices': [{'finish_reason': finish, 'message': {'role': 'assistant', 'content': text}}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}


SCHEMA = {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value'], 'additionalProperties': False}


def test_provider_credentials_are_input_only_and_url_normalized():
    model = connection(api_key='private-value')
    assert 'api_key' not in model.model_dump()
    assert 'private-value' not in repr(model)
    assert connection().base_url == 'https://example.test/v1'
    with pytest.raises(ValidationError):
        LayoutParseConfig(mode='multimodal')
    with pytest.raises(ValidationError):
        connection(base_url='https://user:password@example.test')


def test_multimodal_calls_have_one_page_and_no_history():
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json=wire({'value': 'ok'}))
    image = {'data': base64.b64encode(b'image-content').decode(), 'media_type': 'image/png', 'width': 600, 'height': 800}
    config = connection(supports_vision=True)
    for page in range(2):
        structured_call(config, 'key', '固定解析指令', {'page': page}, image=image, schema=SCHEMA, transport=httpx.MockTransport(handler))
    assert len(requests) == 2
    for index, payload in enumerate(requests):
        assert len(payload['messages']) == 2
        assert [message['role'] for message in payload['messages']] == ['system', 'user']
        assert [block['type'] for block in payload['messages'][1]['content']] == ['text', 'image_url']
        assert json.loads(payload['messages'][1]['content'][0]['text']) == {'page': index}
        assert 'tools' not in payload


@pytest.mark.parametrize('first', [wire('{bad'), wire({'value': 'complete-looking'}, finish='length'), wire({'wrong': 'schema'})])
def test_known_invalid_or_truncated_response_gets_only_one_fresh_retry(first):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=first if len(sent) == 1 else wire({'value': 'ok'}))
    result = structured_call(connection(), 'key', 'test', {'id': 1}, schema=SCHEMA,
                             max_output_tokens=1024, transport=httpx.MockTransport(handler))
    assert result['data'] == {'value': 'ok'}
    assert len(sent) == 2
    assert sent[0]['messages'] == sent[1]['messages']
    assert sent[1]['max_tokens'] == 2048
    assert result['usage'] == {'input_tokens': 22, 'output_tokens': 14}


def test_invalid_after_second_attempt_stops():
    count = []
    def handler(request):
        count.append(request)
        return httpx.Response(200, json=wire('invalid'))
    with pytest.raises(ProviderFault, match='JSON') as caught:
        structured_call(connection(), 'key', 'test', {}, transport=httpx.MockTransport(handler))
    assert caught.value.code == 'invalid_json'
    assert len(count) == 2


def test_unknown_request_result_is_never_repeated_and_emits_durable_events():
    count, events = [], []
    def handler(request):
        count.append(request)
        raise httpx.ReadTimeout('unknown', request=request)
    with pytest.raises(ProviderFault) as caught:
        structured_call(connection(), 'key', 'test', {}, transport=httpx.MockTransport(handler), event=events.append)
    assert caught.value.code == 'response_unknown'
    assert len(count) == 1
    assert [event['type'] for event in events] == ['request_intent', 'response_unknown']


def test_rate_limit_and_refusal_are_not_retried():
    for response in [httpx.Response(429, json={'error': {'type': 'rate_limit_error'}}),
                     httpx.Response(200, json=wire({}, finish='content_filter'))]:
        calls = []
        def handler(request):
            calls.append(request)
            return response
        with pytest.raises(ProviderFault):
            structured_call(connection(), 'key', 'test', {}, transport=httpx.MockTransport(handler))
        assert len(calls) == 1


def test_budget_and_vision_capability_fail_before_post():
    def forbidden(request):
        pytest.fail('must not POST')
    transport = httpx.MockTransport(forbidden)
    with pytest.raises(ProviderFault) as caught:
        structured_call(connection(), 'key', 'test', {'long': 'x' * 40000}, transport=transport)
    assert caught.value.code == 'CONTEXT_WINDOW_EXCEEDED'
    with pytest.raises(ProviderFault) as caught:
        structured_call(connection(), 'key', 'test', {}, image={}, transport=transport)
    assert caught.value.code == 'unsupported_capability'


def test_anthropic_image_wire_and_json_schema_validation():
    sent = []
    def handler(request):
        assert request.headers['x-api-key'] == 'key'
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=wire({'value': 'ok'}, 'end_turn', 'anthropic_messages'))
    image = {'data': base64.b64encode(b'image').decode(), 'media_type': 'image/png', 'width': 500, 'height': 500}
    result = structured_call(connection(protocol='anthropic_messages', supports_vision=True), 'key', 'system', {},
                             image=image, schema=SCHEMA, transport=httpx.MockTransport(handler))
    assert result['data']['value'] == 'ok'
    assert len(sent[0]['messages']) == 1
    assert sent[0]['messages'][0]['content'][1]['type'] == 'image'
    assert sent[0]['system'].startswith('system')


def document(identifier, text, *, status='succeeded'):
    return {'kind': 'clear.document', 'schema_version': '1.0', 'document_id': identifier, 'revision': 1,
            'pages': [{'page_number': 1, 'status': status, 'blocks': [
                {'block_id': identifier + '-p1-b1', 'type': 'text', 'text': text, 'source': {'blob_hash': identifier}}]}]}


def theme(kind='percentage', **changes):
    return ComparisonProfile(fields=[{'key': 'progress', 'label': '进度', 'type': kind}], **changes)


def extraction_call(a_value='50%', b_value='60%', a_entity='项目A', b_entity='项目A', *, semantic_status='different', bad_evidence=False, as_of_a=None, as_of_b=None):
    calls = []
    def call(*, stage, system, payload, schema):
        calls.append((stage, payload))
        if stage.startswith('extract_'):
            side = stage[-1]
            block = payload['blocks'][0]
            fact = {'field': 'progress', 'entity': a_entity if side == 'a' else b_entity, 'work_item': '',
                    'value': a_value if side == 'a' else b_value, 'as_of': as_of_a if side == 'a' else as_of_b,
                    'evidence': [{'block_id': 'invented' if bad_evidence and side == 'a' else block['block_id'], 'quote': block['text']}]}
            return {'data': {'facts': [fact]}, 'model': 'extract-model', 'usage': {'input_tokens': 3, 'output_tokens': 4}}
        return {'data': {'decisions': [{'pair_id': pair['pair_id'], 'status': semantic_status,
            'explanation': '两侧原文表示不同进度状态。', 'evidence_ids': pair['evidence_ids']} for pair in payload['pairs']]}, 'model': 'compare-model'}
    return call, calls


def test_percentage_comparison_is_numeric_with_verified_evidence():
    call, calls = extraction_call()
    result = extract_and_compare(document('a', '项目A 完成率50%'), document('b', '项目A 完成率60%'), theme(), call)
    assert result['items'][0]['status'] == 'different'
    assert result['items'][0]['a_value'] == '50%'
    assert {entry['side'] for entry in result['items'][0]['evidence']} == {'a', 'b'}
    assert all(stage.startswith('extract') for stage, _ in calls)


def test_configured_alias_aligns_entities():
    call, _ = extraction_call(a_value='50%', b_value='50%', b_entity='A项目')
    result = extract_and_compare(document('a', '项目A 完成率50%'), document('b', 'A项目 完成率50%'),
                                 theme(entity_aliases={'项目A': ['A项目']}), call)
    assert len(result['items']) == 1
    assert result['items'][0]['status'] == 'consistent'


def test_different_cutoff_dates_are_information_insufficient():
    call, _ = extraction_call(as_of_a='2026-10-01', as_of_b='2026-10-15')
    result = extract_and_compare(document('a', '项目A 完成率50% 截至2026-10-01'),
                                 document('b', '项目A 完成率60% 截至2026-10-15'), theme(), call)
    assert result['items'][0]['status'] == 'insufficient'
    assert '截止日期' in result['items'][0]['explanation']


def test_fabricated_quote_is_rejected_and_does_not_prove_absence():
    call, _ = extraction_call(bad_evidence=True)
    result = extract_and_compare(document('a', '项目A 完成率50%'), document('b', '项目A 完成率60%'), theme(), call)
    assert result['fact_sets']['a']['facts'] == []
    assert result['coverage']['a']['complete'] is False
    assert result['items'][0]['status'] == 'insufficient'
    assert any(entry['code'] == 'invalid_evidence' for entry in result['diagnostics'])


def test_missing_fact_is_only_single_side_when_opposite_coverage_complete():
    def call(**kwargs):
        if kwargs['stage'] == 'extract_b':
            return {'data': {'facts': []}}
        return extraction_call()[0](**kwargs)
    a = document('a', '项目A 完成率50%')
    b = document('b', '无相关内容')
    assert extract_and_compare(a, b, theme(), call)['items'][0]['status'] == 'only_a'
    b['pages'][0]['status'] = 'failed'
    assert extract_and_compare(a, b, theme(), call)['items'][0]['status'] == 'insufficient'


def test_text_values_are_compared_by_model():
    call, calls = extraction_call('进行中', '已完成')
    result = extract_and_compare(document('a', '项目A 状态进行中'), document('b', '项目A 状态已完成'), theme('text'), call)
    assert result['items'][0]['status'] == 'different'
    assert calls[-1][0] == 'compare'


def test_model_comparison_cannot_introduce_evidence_ids():
    delegate, _ = extraction_call('进行中', '已完成')
    def call(**kwargs):
        response = delegate(**kwargs)
        if kwargs['stage'] == 'compare':
            response['data']['decisions'][0]['evidence_ids'] = ['invented']
        return response
    result = extract_and_compare(document('a', '项目A 状态进行中'), document('b', '项目A 状态已完成'), theme('text'), call)
    assert result['items'][0]['status'] == 'insufficient'
    assert '引用未通过' in result['items'][0]['explanation']


def test_successful_chunks_are_reused_without_new_requests():
    storage = {}
    def checkpoint(key, *values):
        if values:
            storage[key] = values[0]
        return storage.get(key)
    call, calls = extraction_call()
    a, b = document('a', '项目A 完成率50%'), document('b', '项目A 完成率60%')
    first = extract_and_compare(a, b, theme(), call, checkpoint)
    count = len(calls)
    second = extract_and_compare(a, b, theme(), call, checkpoint)
    assert first == second
    assert len(calls) == count
    b['revision'] = 2
    extract_and_compare(a, b, theme(), call, checkpoint)
    assert len(calls) == count + 1


@pytest.mark.parametrize(('value', 'kind', 'expected'), [
    ('2026年10月2日', 'date', '2026-10-02'), ('2026-02-30', 'date', None),
    ('50％', 'percentage', {'number': '5E+1', 'unit': '%'}),
    ('2万元', 'number', {'number': '2E+4', 'unit': '元'}),
    ('1,00', 'number', None),
    ('0.5', 'percentage', None),
])
def test_numeric_date_normalization(value, kind, expected):
    assert normalize_value(value, kind) == expected


def test_fixed_report_is_offline_deterministic_and_escapes_all_untrusted_text():
    call, _ = extraction_call()
    result = extract_and_compare(document('a', '项目A 完成率50%'), document('b', '项目A 完成率60%'), theme(), call)
    result['title'] = '<script>alert("title")</script>'
    result['items'][0]['entity'] = '<img src=x onerror=alert(1)>'
    result['items'][0]['evidence'][0]['quote'] = '</blockquote><script>alert(2)</script>'
    html = render_report(result)
    assert html == render_report(deepcopy(result))
    assert '<script' not in html
    assert '<img src=x' not in html
    assert '&lt;script&gt;' in html
    assert 'Content-Security-Policy' in html
    assert '@media print' in html
    assert html.count('<section id=') == 7
    assert 'https://' not in html


def test_markdown_preserves_pages_and_escapes_raw_html():
    doc = document('a', '<script>untrusted</script>')
    md = document_markdown(doc)
    assert '## 第 1 页' in md
    assert '&lt;script&gt;' in md


def test_chinese_text_is_rechunked_to_actual_provider_budget_without_dropping_segments():
    observed = []
    def call(*, stage, system, payload, schema):
        assert estimate_request_input(system, payload, schema) + call.max_output_tokens <= call.context_window
        observed.extend(block['text'] for block in payload['blocks'])
        return {'data': {'facts': []}}
    call.context_window = 9000
    call.max_output_tokens = 1024
    text = '\n'.join(f'[segment-{index:04d}] 项目A的进度相关原文与表格记录。' for index in range(250))
    result = extract_and_compare(document('a', text), document('b', text), theme(), call)
    assert len(observed) > 4
    assert result['coverage']['a']['complete']
    for index in range(250):
        assert any(f'[segment-{index:04d}]' in part for part in observed)


def test_extracted_number_cannot_match_only_part_of_a_source_number():
    call, _ = extraction_call('5', '50')
    result = extract_and_compare(document('a', '项目A 数值50'), document('b', '项目A 数值50'), theme('number'), call)
    assert not result['fact_sets']['a']['facts']
    assert result['items'][0]['status'] == 'insufficient'


def test_model_estimated_bbox_remains_labeled_estimated():
    call, _ = extraction_call()
    a = document('a', '项目A 完成率50%')
    a['pages'][0]['blocks'][0].update(bbox=[0, 0, 1, 1], source={'precision': 'approximate', 'coordinate_origin': 'model_estimate'})
    result = extract_and_compare(a, document('b', '项目A 完成率60%'), theme(), call)
    evidence = next(entry for entry in result['items'][0]['evidence'] if entry['side'] == 'a')
    assert evidence['precision'] == 'model_estimated'
    assert '模型估计坐标' in render_report(result)
